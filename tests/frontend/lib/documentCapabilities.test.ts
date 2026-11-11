/**
 * Aides pures de l'écran « Capacités documentaires » :
 * correspondances de statut, filtres, portée, images intégrées, formatage.
 */
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import path from "node:path";
import type { DocumentCapabilitiesSnapshot, DocumentCapability } from "@/lib/coreConfig";
import {
  capabilitiesForFormat,
  cellCounts,
  DEFAULT_FILTERS,
  familyOf,
  filterFormats,
  filterMatrix,
  formatBytes,
  formatEpoch,
  formatExactBytes,
  groupFormatsByFamily,
  inlineImageRows,
  producesSearchableContent,
  scopeView,
  shortSha,
} from "@/app/admin-core/document-capabilities/_lib/capabilities";
import {
  CAPABILITY_STATES,
  capabilityStateMeta,
  consistencyMeta,
  formatStatusMeta,
  indexedMeta,
  numericEvidenceMeta,
  qualificationMeta,
  serviceStatusMeta,
  warningKey,
  WARNING_CODES,
} from "@/app/admin-core/document-capabilities/_lib/status";
import snapshotJson from "../fixtures/documentCapabilitiesSnapshot.json";

const SNAP = snapshotJson as unknown as DocumentCapabilitiesSnapshot;
const FR = JSON.parse(
  readFileSync(path.resolve(__dirname, "../../../frontend/messages/fr/documentCapabilities.json"), "utf8")
) as Record<string, string>;
const EN = JSON.parse(
  readFileSync(path.resolve(__dirname, "../../../frontend/messages/en/documentCapabilities.json"), "utf8")
) as Record<string, string>;

describe("status → libellé / ton / icône", () => {
  it("chaque état d'activation a une icône et un libellé FR et EN (jamais la couleur seule)", () => {
    for (const s of [...CAPABILITY_STATES, "NOT_APPLICABLE"]) {
      const m = capabilityStateMeta(s);
      expect(m.icon).toBeTruthy();
      expect(FR[m.key], s).toBeTruthy();
      expect(EN[m.key], s).toBeTruthy();
    }
    expect(FR[capabilityStateMeta("ACTIVE_GLOBAL").key]).toBe("Actif (global)");
    expect(FR[capabilityStateMeta("INCONSISTENT").key]).toBe("Incohérent");
  });

  it("un état inconnu retombe sur « Inconnu »", () => {
    expect(capabilityStateMeta("WHATEVER").key).toBe("state_UNKNOWN");
    expect(capabilityStateMeta(null).key).toBe("state_UNKNOWN");
    expect(formatStatusMeta("NOPE").key).toBe("fmt_UNKNOWN");
    expect(consistencyMeta(undefined).key).toBe("cons_UNKNOWN");
  });

  it("statut de service : null ou inconnu → « Inconnu »", () => {
    expect(FR[serviceStatusMeta(null).key]).toBe("Inconnu");
    expect(FR[serviceStatusMeta("UNKNOWN").key]).toBe("Inconnu");
    // « déclaré » ≠ « disponible » (aucune inférence vérifiée).
    const declared = serviceStatusMeta("DECLARED");
    expect(FR[declared.key]).toBe("Déclaré (inférence non vérifiée)");
    expect(declared.tone).not.toBe("success");
    expect(declared.key).not.toBe(serviceStatusMeta("AVAILABLE").key);
    expect(FR[serviceStatusMeta("AVAILABLE").key]).toBe("Disponible");
    expect(serviceStatusMeta("AVAILABLE").tone).toBe("success");
    expect(serviceStatusMeta("DEGRADED").tone).toBe("warning");
    expect(serviceStatusMeta("UNAVAILABLE").tone).toBe("danger");
  });

  it("format refusé → « Refusé »", () => {
    expect(FR[formatStatusMeta("UNSUPPORTED").key]).toBe("Refusé");
  });

  it("preuve chiffrée : false → « Non probant », true → fiable, null → sans objet", () => {
    expect(FR[numericEvidenceMeta(false).key]).toBe("Non probant");
    expect(numericEvidenceMeta(true).key).toBe("num_RELIABLE");
    expect(numericEvidenceMeta(null).key).toBe("num_NA");
  });

  it("qualification : badges attendus, inconnu → Non mesuré", () => {
    expect(FR[qualificationMeta("EXPERIMENTAL").key]).toBe("Expérimental");
    expect(FR[qualificationMeta("NOT_QUALIFIED").key]).toBe("Non qualifié");
    expect(FR[qualificationMeta("PARTIAL").key]).toBe("Partiel");
    expect(FR[qualificationMeta("SYNTHETIC_QUALIFIED").key]).toBe("Qualifié synthétique");
    expect(FR[qualificationMeta("REAL_DOCUMENT_QUALIFIED").key]).toBe("Qualifié documents réels");
    expect(FR[qualificationMeta("NOT_MEASURED").key]).toBe("Non mesuré");
    expect(qualificationMeta("FUTURE_STATUS").key).toBe("qual_NOT_MEASURED");
  });

  it("indexation : trois valeurs distinctes", () => {
    expect(indexedMeta("INDEXED").key).toBe("idx_INDEXED");
    expect(indexedMeta("INTERNAL_ONLY").key).toBe("idx_INTERNAL_ONLY");
    expect(indexedMeta(undefined).key).toBe("idx_NONE");
  });

  it("codes d'avertissement : tous traduits, code inconnu → UNKNOWN", () => {
    for (const c of WARNING_CODES) expect(FR[warningKey(c)], c).toBeTruthy();
    expect(warningKey("SOMETHING_NEW")).toBe("warn_UNKNOWN");
  });
});

describe("familles et filtres", () => {
  it("familyOf classe les familles backend", () => {
    const fam = (family: string, status = "SUPPORTED") => familyOf({ family, status: status as any });
    expect(fam("PDF")).toBe("pdf");
    expect(fam("Office")).toBe("office");
    expect(fam("Image autonome")).toBe("images");
    expect(fam("Courriel")).toBe("email");
    expect(fam("Audio")).toBe("audio");
    expect(fam("Structuré")).toBe("structured");
    expect(fam("CAO / BIM")).toBe("structured");
    expect(fam("Texte tabulaire")).toBe("text");
    expect(fam("Texte brut implicite")).toBe("text");
    expect(fam("Refusé", "UNSUPPORTED")).toBe("refused");
  });

  it("capabilitiesForFormat ajoute « visual » (pdf/docx/pptx/image) et « common »", () => {
    const pdf = capabilitiesForFormat(SNAP.capabilities, "pdf").map((c) => c.id);
    expect(pdf).toContain("pdf.text");
    expect(pdf).toContain("visual.structx.table");
    expect(pdf).toContain("common.embeddings");
    const audio = capabilitiesForFormat(SNAP.capabilities, "audio").map((c) => c.id);
    expect(audio).toContain("audio.asr");
    expect(audio).not.toContain("visual.structx.table");
    expect(audio).toContain("common.chunking");
  });

  it("filtre de famille, masquage des refusés, recherche", () => {
    const refused = filterFormats(SNAP.formats, SNAP.capabilities, { ...DEFAULT_FILTERS, family: "refused" });
    expect(refused.length).toBeGreaterThan(0);
    expect(refused.every((f) => f.status === "UNSUPPORTED")).toBe(true);

    const hidden = filterFormats(SNAP.formats, SNAP.capabilities, { ...DEFAULT_FILTERS, hideUnsupported: true });
    expect(hidden.some((f) => f.status === "UNSUPPORTED")).toBe(false);
    expect(hidden.length).toBeLessThan(SNAP.formats.length);

    const all = filterFormats(SNAP.formats, SNAP.capabilities, DEFAULT_FILTERS);
    expect(all).toHaveLength(SNAP.formats.length);
  });

  it("« seulement recherchable » : pdf (texte actif) oui, audio (tout désactivé) non", () => {
    expect(producesSearchableContent(SNAP.capabilities, "pdf")).toBe(true);
    expect(producesSearchableContent(SNAP.capabilities, "audio")).toBe(false);
    const rows = filterMatrix(SNAP.matrix, SNAP.formats, SNAP.capabilities, { ...DEFAULT_FILTERS, searchableOnly: true });
    const ids = rows.map((r) => r.format_id);
    expect(ids).toContain("pdf");
    expect(ids).not.toContain("audio");
  });

  it("groupFormatsByFamily conserve l'ordre backend", () => {
    const groups = groupFormatsByFamily(SNAP.formats);
    expect(groups[0].family).toBe("PDF");
    expect(groups.reduce((n, g) => n + g.formats.length, 0)).toBe(SNAP.formats.length);
  });

  it("cellCounts compte les items actifs", () => {
    expect(cellCounts([{ state: "ACTIVE_GLOBAL" }, { state: "DISABLED" }, { state: "ACTIVE_CONDITIONAL" }])).toEqual({
      active: 2,
      total: 3,
    });
  });
});

describe("portée", () => {
  const base: Pick<DocumentCapability, "state" | "activation_scope" | "scope"> = {
    state: "ACTIVE_GLOBAL",
    activation_scope: "GLOBAL",
    scope: null,
  };
  it("GLOBAL / SCOPED / NONE / inconnu", () => {
    expect(scopeView(base)).toEqual({ kind: "global" });
    expect(
      scopeView({
        state: "ACTIVE_SCOPED",
        activation_scope: "SCOPED",
        scope: { admitted_spaces: 3, listed: 3, existing: 3, stale_entries: 0, global_mode: false, list_setting: "X" },
      })
    ).toEqual({ kind: "spaces", count: 3 });
    expect(scopeView({ ...base, state: "DISABLED", activation_scope: "NONE" })).toEqual({ kind: "none" });
    expect(scopeView({ ...base, activation_scope: "UNKNOWN" })).toEqual({ kind: "unknown" });
  });
  it("INCONSISTENT + 0 espace admis → inopérant", () => {
    expect(
      scopeView({
        state: "INCONSISTENT",
        activation_scope: "SCOPED",
        scope: { admitted_spaces: 0, listed: 0, existing: 0, stale_entries: 0, global_mode: false, list_setting: "X" },
      })
    ).toEqual({ kind: "inoperative" });
  });
});

describe("images intégrées (calculées depuis les capacités)", () => {
  it("lit les états réels, sans les coder en dur", () => {
    const rows = inlineImageRows(SNAP.capabilities);
    const byFmt = new Map(rows.map((r) => [r.formatId, r]));
    const pdf = byFmt.get("pdf")!;
    expect(pdf.detection?.id).toBe("pdf.inline_image.detection");
    expect(pdf.ocr?.id).toBe("pdf.inline_image.ocr");
    expect(pdf.vision?.id).toBe("pdf.inline_image.vision");
    expect(pdf.structured?.id).toBe("visual.structx.table");
    const xlsx = byFmt.get("xlsx")!;
    expect(xlsx.detection?.id).toBe("xlsx.images");
    expect(xlsx.structured).toBeNull();
    const image = byFmt.get("image")!;
    expect(image.ocr?.id).toBe("image.ocr");
    expect(image.vision?.id).toBe("image.vision");
    // image.ocr est ACTIF et indexé dans l'instantané → recherchable
    expect(image.indexed).toBe(SNAP.capabilities.find((c) => c.id === "image.ocr")!.output_indexed === "INDEXED" ? "yes" : "no");
    const msg = byFmt.get("msg")!;
    expect(msg.ocr?.id).toBe("msg.inline_image.ocr");
    expect(msg.vision?.id).toBe("msg.inline_image.vision");
  });

  it("une capacité basculée active change le résultat (aucune valeur figée)", () => {
    const caps = SNAP.capabilities.map((c) =>
      c.id === "pdf.inline_image.vision" ? { ...c, state: "ACTIVE_GLOBAL" as const, output_indexed: "INDEXED" as const, output_searchable: true } : c
    );
    const pdf = inlineImageRows(caps).find((r) => r.formatId === "pdf")!;
    expect(pdf.indexed).toBe("yes");
    expect(pdf.searchable).toBe("yes");
  });
});

describe("formatage", () => {
  it("formatBytes : binaire pour 50 Mio, décimal pour l'audio 600 000 000 octets", () => {
    expect(formatBytes(52428800, "fr")).toBe("50 Mio");
    expect(formatBytes(26214400, "fr")).toBe("25 Mio");
    expect(formatBytes(600_000_000, "fr")).toBe("600 Mo");
    expect(formatBytes(600_000_000, "en")).toBe("600 MB");
    expect(formatBytes(null)).toBeNull();
    expect(formatBytes(-1)).toBeNull();
    expect(formatBytes(512, "fr")).toBe("512 octets");
  });
  it("formatExactBytes groupe les milliers", () => {
    expect(formatExactBytes(600_000_000, "fr").replace(/\s/g, " ")).toBe("600 000 000");
  });
  it("shortSha / formatEpoch", () => {
    expect(shortSha("a1b2c3d4e5f6")).toBe("a1b2c3d4");
    expect(shortSha("")).toBeNull();
    expect(formatEpoch(0)).toBeNull();
    expect(formatEpoch(1794046541.57, "fr")).toMatch(/2026/);
  });
});
