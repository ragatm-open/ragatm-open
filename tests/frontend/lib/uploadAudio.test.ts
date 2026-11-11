import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

import {
  AUDIO_FAILURE_CODES,
  AUDIO_I18N_KEYS,
  audioExtensionsOf,
  audioFailureKey,
  audioMaxMegabytes,
  audioProcessingNotice,
  audioReadyNotice,
  audioUploadErrorKey,
  audioUploadOffered,
  formatAudioDuration,
  isAudioFilename,
  STANDARD_UPLOAD_URL,
  uploadTargetFor,
  type AudioCapability,
} from "@/app/documents/_lib/audio";
import { uploadAccept, uploadFormatsLabel } from "@/app/documents/_lib/types";
import { buildFolderPlan, folderFormatCatalog } from "@/app/documents/_lib/folderImport";

/** dépôt et suivi des enregistrements audio (frontend). */

const AUDIO: AudioCapability = {
  format: "audio",
  status: "available",
  extensions: ["flac", "m4a", "mp3", "ogg", "wav", "webm"],
  max_upload_bytes: 600_000_000,
  max_duration_seconds: 43200,
  upload_path: "/api/v1/documents/upload-audio/",
  space_scoped: true,
};
const DISABLED: AudioCapability = { ...AUDIO, status: "disabled_by_config", extensions: [] };
const STD = 50 * 1024 * 1024;

describe("détection audio et offre par espace", () => {
  it("extensions annoncées seulement si la capacité est disponible", () => {
    expect(audioExtensionsOf(AUDIO)).toEqual(["flac", "m4a", "mp3", "ogg", "wav", "webm"]);
    expect(audioExtensionsOf(DISABLED)).toEqual([]);
    expect(audioExtensionsOf(null)).toEqual([]);
  });

  it("détection par extension, insensible à la casse", () => {
    expect(isAudioFilename("Réunion.MP3", AUDIO)).toBe(true);
    expect(isAudioFilename("dossier/x.webm", AUDIO)).toBe(true);
    expect(isAudioFilename("rapport.pdf", AUDIO)).toBe(false);
    expect(isAudioFilename("mp3", AUDIO)).toBe(false);
    expect(isAudioFilename("a.mp3", DISABLED)).toBe(false);
  });

  it("offert seulement si capacité disponible ET espace autorisé", () => {
    expect(audioUploadOffered(AUDIO, true)).toBe(true);
    expect(audioUploadOffered(AUDIO, false)).toBe(false);
    expect(audioUploadOffered(AUDIO, undefined)).toBe(false);
    expect(audioUploadOffered(DISABLED, true)).toBe(false);
  });

  it("accept : extensions audio ajoutées seulement quand elles sont passées", () => {
    const formats = ["pdf", "audio"];
    expect(uploadAccept(null, formats, null, audioExtensionsOf(AUDIO)).split(",")).toEqual([
      ".pdf", ".flac", ".m4a", ".mp3", ".ogg", ".wav", ".webm",
    ]);
    // Espace non autorisé : aucune extension audio, et jamais de pseudo-extension `.audio`.
    expect(uploadAccept(null, formats, null, null).split(",")).toEqual([".pdf"]);
    expect(uploadFormatsLabel(null, formats)).toBe("PDF");
  });

  it("plafond en Mo décimaux", () => {
    expect(audioMaxMegabytes(AUDIO)).toBe(600);
    expect(audioMaxMegabytes(null)).toBe(600);
    expect(audioMaxMegabytes({ max_upload_bytes: 250_000_000 })).toBe(250);
  });
});

describe("uploadTargetFor — chemin et plafond", () => {
  it("non-audio : chemin générique et limite historique, inchangés", () => {
    expect(uploadTargetFor("a.pdf", { audio: AUDIO, spaceAudioEnabled: true, standardMaxBytes: STD })).toEqual({
      kind: "standard", url: STANDARD_UPLOAD_URL, maxBytes: STD,
    });
    expect(STANDARD_UPLOAD_URL).toBe("/api/v1/documents/upload/?async=true");
  });

  it("audio autorisé : chemin dédié ?async=true, plafond 600 000 000", () => {
    expect(uploadTargetFor("a.m4a", { audio: AUDIO, spaceAudioEnabled: true, standardMaxBytes: STD })).toEqual({
      kind: "audio", url: "/api/v1/documents/upload-audio/?async=true", maxBytes: 600_000_000,
    });
  });

  it("audio sans upload_path annoncé : repli sur le chemin dédié", () => {
    const t = uploadTargetFor("a.wav", {
      audio: { ...AUDIO, upload_path: "" }, spaceAudioEnabled: true, standardMaxBytes: STD,
    });
    expect(t).toMatchObject({ kind: "audio", url: "/api/v1/documents/upload-audio/?async=true" });
  });

  it("audio dans un espace non autorisé : refus terminal", () => {
    expect(uploadTargetFor("a.mp3", { audio: AUDIO, spaceAudioEnabled: false, standardMaxBytes: STD })).toEqual({
      kind: "audio_not_enabled_for_space",
    });
  });

  it("dossier : un audio de 100 Mo passe sous le plafond audio, un PDF de 100 Mo non", () => {
    const big = 100 * 1000 * 1000;
    const mk = (name: string) => {
      const f = new File(["x"], name);
      Object.defineProperty(f, "size", { value: big });
      return f;
    };
    const catalog = folderFormatCatalog(".pdf,.mp3");
    const plan = buildFolderPlan([mk("a.mp3"), mk("b.pdf")], {
      maxSizeBytes: STD,
      maxSizeBytesOf: (n) => {
        const t = uploadTargetFor(n, { audio: AUDIO, spaceAudioEnabled: true, standardMaxBytes: STD });
        return t.kind === "audio" ? t.maxBytes : STD;
      },
      catalog,
    });
    expect(plan.compatible.map((c) => c.file.name)).toEqual(["a.mp3"]);
    expect(plan.skipped).toEqual([expect.objectContaining({ name: "b.pdf", reason: "too_large" })]);
    expect(catalog.formats.find((f) => f.key === "audio")?.label).toBe("Audio");
  });

  it("codes de refus d'upload audio -> clés i18n", () => {
    expect(audioUploadErrorKey("audio_file_too_large")).toBe("audioExceedsLimit");
    expect(audioUploadErrorKey("unsupported_audio")).toBe("audioUploadUnsupported");
    expect(audioUploadErrorKey("audio_ingestion_disabled")).toBe("audioUploadDisabled");
    expect(audioUploadErrorKey("empty_file")).toBeNull();
    expect(audioUploadErrorKey(undefined)).toBeNull();
  });
});

describe("sous-statut audio d'un document", () => {
  const pending = (audio: object) => ({ status: "PENDING", audio });

  it("étapes -> clés i18n, partie courante bornée", () => {
    expect(audioProcessingNotice(pending({ stage: "queued" }))).toEqual({ key: "audioStageQueued" });
    expect(audioProcessingNotice(pending({ stage: "preparing_audio" }))).toEqual({ key: "audioStagePreparing" });
    expect(audioProcessingNotice(pending({ stage: "waiting_for_asr" }))).toEqual({ key: "audioStageWaitingAsr" });
    expect(audioProcessingNotice(pending({ stage: "transcribing", parts_total: 5, parts_done: 1 }))).toEqual({
      key: "audioStageTranscribingPart", values: { current: 2, total: 5 },
    });
    expect(audioProcessingNotice(pending({ stage: "transcribing", parts_total: 5, parts_done: 5 }))).toEqual({
      key: "audioStageTranscribingPart", values: { current: 5, total: 5 },
    });
    expect(audioProcessingNotice(pending({ stage: "transcribing", parts_total: 1 }))).toEqual({
      key: "audioStageTranscribing",
    });
    expect(audioProcessingNotice(pending({ stage: "assembling_transcript" }))).toEqual({ key: "audioStageAssembling" });
    expect(audioProcessingNotice(pending({ stage: "indexing" }))).toEqual({ key: "audioStageIndexing" });
    expect(audioProcessingNotice(pending({ stage: "inconnu" }))).toBeNull();
  });

  it("rien pour un document sans audio, prêt ou en échec", () => {
    expect(audioProcessingNotice({ status: "PENDING" })).toBeNull();
    expect(audioProcessingNotice({ status: "READY", audio: { stage: "indexing" } })).toBeNull();
    expect(audioProcessingNotice({ status: "FAILED", audio: { stage: "failed" } })).toBeNull();
  });

  it("prêt : durée et langue", () => {
    expect(formatAudioDuration(3_900_000)).toBe("1 h 05 min");
    expect(formatAudioDuration(750_000)).toBe("12 min 30 s");
    expect(formatAudioDuration(45_000)).toBe("45 s");
    expect(formatAudioDuration(0)).toBeNull();
    expect(audioReadyNotice({ status: "READY", audio: { duration_ms: 3_900_000, language: "fr" } })).toEqual({
      key: "audioReadyDurationLanguage", values: { duration: "1 h 05 min", language: "fr" },
    });
    expect(audioReadyNotice({ status: "READY", audio: { duration_ms: 45_000 } })).toEqual({
      key: "audioReadyDuration", values: { duration: "45 s" },
    });
    expect(audioReadyNotice({ status: "READY", audio: {} })).toBeNull();
    expect(audioReadyNotice({ status: "PENDING", audio: { duration_ms: 45_000 } })).toBeNull();
  });

  it("échec : message spécifique par code, repli null", () => {
    expect(audioFailureKey({ status: "FAILED", audio: { error_code: "asr_saturated" } })).toBe(
      "audioError_asr_saturated"
    );
    expect(audioFailureKey({ status: "FAILED", audio: { error_code: "code_inconnu" } })).toBeNull();
    expect(audioFailureKey({ status: "FAILED" })).toBeNull();
    expect(audioFailureKey({ status: "PENDING", audio: { error_code: "asr_failed" } })).toBeNull();
    expect(AUDIO_FAILURE_CODES).toHaveLength(16);
  });

  it("toutes les clés i18n audio existent en FR et en EN", () => {
    for (const lang of ["fr", "en"]) {
      const dict = JSON.parse(
        readFileSync(path.resolve(__dirname, `../../../frontend/messages/${lang}/documents.json`), "utf8")
      );
      for (const key of AUDIO_I18N_KEYS) expect(dict[key], `${lang}:${key}`).toBeTruthy();
    }
  });
});
