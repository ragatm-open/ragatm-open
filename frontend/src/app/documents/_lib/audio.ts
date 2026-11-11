// frontend/src/app/documents/_lib/audio.ts
// Helpers PURS (testables) du dépôt et du suivi des enregistrements audio.
//
// Le backend reste l'autorité : capacité GLOBALE (`ingest.audio`) et admission PAR
// ESPACE (`audio_upload_enabled` sur chaque RagSpace). Ces helpers ne font que traduire
// ces deux signaux en décisions d'interface (extensions proposées, plafond, chemin).

/** Capacité audio telle qu'annoncée par `GET /api/v1/capabilities/` (`ingest.audio`). */
export type AudioCapability = {
  format: "audio";
  status: "available" | "disabled_by_config" | string;
  /** Extensions sans point. VIDE quand le dépôt audio est fermé. */
  extensions: string[];
  /** Plafond par fichier, en octets DÉCIMAUX (600 000 000 = 600 Mo). */
  max_upload_bytes: number;
  max_duration_seconds?: number;
  /** Chemin d'upload dédié (seul chemin qui accepte l'audio). */
  upload_path: string;
  space_scoped?: boolean;
};

/** Chemin de repli si le backend n'annonce pas `upload_path`. */
export const DEFAULT_AUDIO_UPLOAD_PATH = "/api/v1/documents/upload-audio/";
/** Chemin d'upload générique (tous les autres formats). */
export const STANDARD_UPLOAD_URL = "/api/v1/documents/upload/?async=true";
/** Repli du plafond audio (octets décimaux) si le backend ne l'annonce pas. */
export const DEFAULT_AUDIO_MAX_UPLOAD_BYTES = 600_000_000;

/** Extensions audio annoncées (minuscules, sans point), vide si le dépôt est fermé. */
export function audioExtensionsOf(audio?: AudioCapability | null): readonly string[] {
  if (!audio || audio.status !== "available") return [];
  return (audio.extensions ?? [])
    .map((e) => String(e).trim().replace(/^\./, "").toLowerCase())
    .filter(Boolean);
}

/**
 * Le dépôt audio est-il PROPOSÉ dans cet espace ? Capacité globale disponible ET espace
 * autorisé. Hors de ce cas, l'audio n'est ni annoncé ni ajouté à l'attribut `accept`.
 */
export function audioUploadOffered(
  audio: AudioCapability | null | undefined,
  spaceAudioEnabled: boolean | null | undefined
): boolean {
  return !!spaceAudioEnabled && audioExtensionsOf(audio).length > 0;
}

/** Extension terminale minuscule, sans point ("" si aucune). */
function extensionOf(name: string): string {
  const base = (name || "").split(/[\\/]/).pop() || "";
  const i = base.lastIndexOf(".");
  return i > 0 ? base.slice(i + 1).toLowerCase() : "";
}

/**
 * Le fichier est-il un audio au sens de la capacité serveur ? On s'appuie sur la liste
 * annoncée ; si le dépôt est fermé globalement (liste vide), aucun fichier n'est audio
 * pour le frontend — il suit alors le chemin générique, que le backend refusera.
 */
export function isAudioFilename(name: string, audio?: AudioCapability | null): boolean {
  const ext = extensionOf(name);
  return !!ext && audioExtensionsOf(audio).includes(ext);
}

/** Plafond audio en Mo DÉCIMAUX (600 000 000 -> 600). */
export function audioMaxMegabytes(audio?: Pick<AudioCapability, "max_upload_bytes"> | null): number {
  const bytes = audio?.max_upload_bytes || DEFAULT_AUDIO_MAX_UPLOAD_BYTES;
  return Math.floor(bytes / 1_000_000);
}

/** Cible d'envoi d'UN fichier. */
export type UploadTarget =
  | { kind: "standard"; url: string; maxBytes: number }
  | { kind: "audio"; url: string; maxBytes: number }
  | { kind: "audio_not_enabled_for_space" };

/**
 * Où et sous quel plafond envoyer `name`.
 *
 * - non-audio : chemin générique, plafond historique (`standardMaxBytes`) — inchangé ;
 * - audio dans un espace non autorisé : refus terminal AVANT tout appel réseau ;
 * - audio autorisé : chemin dédié `upload_path?async=true`, plafond `max_upload_bytes`.
 */
export function uploadTargetFor(
  name: string,
  opts: {
    audio?: AudioCapability | null;
    spaceAudioEnabled?: boolean | null;
    standardMaxBytes: number;
  }
): UploadTarget {
  if (!isAudioFilename(name, opts.audio)) {
    return { kind: "standard", url: STANDARD_UPLOAD_URL, maxBytes: opts.standardMaxBytes };
  }
  if (!opts.spaceAudioEnabled) return { kind: "audio_not_enabled_for_space" };
  const path = (opts.audio?.upload_path || "").trim() || DEFAULT_AUDIO_UPLOAD_PATH;
  return {
    kind: "audio",
    url: `${path}${path.includes("?") ? "&" : "?"}async=true`,
    maxBytes: opts.audio?.max_upload_bytes || DEFAULT_AUDIO_MAX_UPLOAD_BYTES,
  };
}

/** Codes d'erreur d'upload audio -> clé i18n (namespace `documents`). */
const AUDIO_UPLOAD_ERROR_KEYS: Readonly<Record<string, string>> = Object.freeze({
  audio_file_too_large: "audioExceedsLimit",
  unsupported_audio: "audioUploadUnsupported",
  audio_ingestion_disabled: "audioUploadDisabled",
});

/** Clé i18n d'un refus d'upload audio (`detail` du corps d'erreur), ou null. */
export function audioUploadErrorKey(detail: unknown): string | null {
  return typeof detail === "string" ? AUDIO_UPLOAD_ERROR_KEYS[detail] ?? null : null;
}

// --------------------------------------------------------------------------------------
// Suivi d'un document audio (champ `audio` de la sérialisation document)
// --------------------------------------------------------------------------------------

export type AudioStage =
  | "queued"
  | "preparing_audio"
  | "waiting_for_asr"
  | "transcribing"
  | "assembling_transcript"
  | "indexing"
  | "failed";

/** État audio sérialisé par le backend (jamais de texte transcrit). */
export type DocumentAudio = {
  format?: string;
  stage?: AudioStage | string;
  error_code?: string;
  language?: string;
  duration_ms?: number;
  parts_total?: number;
  parts_done?: number;
  segment_count?: number;
};

/** Clé i18n + paramètres, à résoudre par `t(key, values)`. */
export type I18nMessage = { key: string; values?: Record<string, string | number> };

/**
 * Sous-statut de traitement à afficher sous le badge, tant que le document n'est ni
 * prêt ni en échec. La transcription peut être longue : le statut principal reste
 * PENDING, c'est ce sous-statut qui dit où l'on en est.
 */
export function audioProcessingNotice(d: {
  status?: string;
  audio?: DocumentAudio | null;
}): I18nMessage | null {
  const a = d.audio;
  if (!a) return null;
  const status = (d.status || "").toUpperCase();
  if (status === "READY" || status === "FAILED") return null;
  switch (a.stage) {
    case "queued":
      return { key: "audioStageQueued" };
    case "preparing_audio":
      return { key: "audioStagePreparing" };
    case "waiting_for_asr":
      return { key: "audioStageWaitingAsr" };
    case "transcribing": {
      const total = a.parts_total ?? 0;
      if (total > 1) {
        const done = Math.min(Math.max(a.parts_done ?? 0, 0), total);
        // Partie EN COURS = parties terminées + 1, bornée au total.
        return { key: "audioStageTranscribingPart", values: { current: Math.min(done + 1, total), total } };
      }
      return { key: "audioStageTranscribing" };
    }
    case "assembling_transcript":
      return { key: "audioStageAssembling" };
    case "indexing":
      return { key: "audioStageIndexing" };
    default:
      return null;
  }
}

/**
 * Durée lisible : « 1 h 05 min » au-delà d'une heure, « 12 min 30 s » au-delà d'une
 * minute, « 45 s » sinon. Les unités sont universelles (h/min/s) en français et en anglais.
 */
export function formatAudioDuration(ms: number | null | undefined): string | null {
  if (typeof ms !== "number" || !Number.isFinite(ms) || ms <= 0) return null;
  const total = Math.round(ms / 1000);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h} h ${String(m).padStart(2, "0")} min`;
  if (m > 0) return `${m} min ${String(s).padStart(2, "0")} s`;
  return `${s} s`;
}

/** Mention d'un audio prêt : durée et langue, quand le backend les fournit. */
export function audioReadyNotice(d: {
  status?: string;
  audio?: DocumentAudio | null;
}): I18nMessage | null {
  const a = d.audio;
  if (!a || (d.status || "").toUpperCase() !== "READY") return null;
  const duration = formatAudioDuration(a.duration_ms);
  const language = (a.language || "").trim();
  if (duration && language) return { key: "audioReadyDurationLanguage", values: { duration, language } };
  if (duration) return { key: "audioReadyDuration", values: { duration } };
  if (language) return { key: "audioReadyLanguage", values: { language } };
  return null;
}

/** Codes d'échec audio connus -> clé i18n. */
const AUDIO_FAILURE_KEYS: Readonly<Record<string, string>> = Object.freeze({
  audio_ingestion_disabled: "audioError_audio_ingestion_disabled",
  undecodable_audio: "audioError_undecodable_audio",
  no_audio_track: "audioError_no_audio_track",
  audio_contains_video: "audioError_audio_contains_video",
  audio_too_long: "audioError_audio_too_long",
  audio_dependency_unavailable: "audioError_audio_dependency_unavailable",
  audio_preparation_timeout: "audioError_audio_preparation_timeout",
  audio_spool_full: "audioError_audio_spool_full",
  audio_part_too_large: "audioError_audio_part_too_large",
  asr_timeout: "audioError_asr_timeout",
  asr_unavailable: "audioError_asr_unavailable",
  asr_saturated: "audioError_asr_saturated",
  asr_failed: "audioError_asr_failed",
  asr_environment_mismatch: "audioError_asr_environment_mismatch",
  transcript_empty: "audioError_transcript_empty",
  audio_cancelled: "audioError_audio_cancelled",
});

/**
 * Message d'échec spécifique d'un audio (`audio.error_code`), ou null — l'appelant
 * retombe alors sur l'affichage d'erreur générique (`humanError`).
 */
export function audioFailureKey(d: {
  status?: string;
  audio?: DocumentAudio | null;
}): string | null {
  if ((d.status || "").toUpperCase() !== "FAILED") return null;
  const code = d.audio?.error_code;
  return code ? AUDIO_FAILURE_KEYS[code] ?? null : null;
}

/** Tous les codes d'échec audio couverts (verrou de test i18n). */
export const AUDIO_FAILURE_CODES: readonly string[] = Object.freeze(Object.keys(AUDIO_FAILURE_KEYS));
/** Toutes les clés i18n produites par ce module (verrou de test FR/EN). */
export const AUDIO_I18N_KEYS: readonly string[] = Object.freeze([
  ...Object.values(AUDIO_UPLOAD_ERROR_KEYS),
  ...Object.values(AUDIO_FAILURE_KEYS),
  "audioStageQueued",
  "audioStagePreparing",
  "audioStageWaitingAsr",
  "audioStageTranscribing",
  "audioStageTranscribingPart",
  "audioStageAssembling",
  "audioStageIndexing",
  "audioReadyDurationLanguage",
  "audioReadyDuration",
  "audioReadyLanguage",
  "audioUploadNotEnabledForSpace",
  "audioUploadHelp",
]);
