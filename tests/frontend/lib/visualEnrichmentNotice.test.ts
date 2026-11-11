import { describe, expect, it } from "vitest";

import { visualEnrichmentNoticeKey } from "@/app/documents/_lib/types";

/** VISUAL-ENRICHMENT-DEFERRED — READY reste l'état principal ; l'analyse visuelle se lit à part. */
describe("libellé d'enrichissement visuel différé", () => {
  it("READY + enrichissement en attente ou en cours : « analyse visuelle en cours »", () => {
    expect(visualEnrichmentNoticeKey({ status: "READY", visual_enrichment_status: "pending" })).toBe(
      "visualEnrichmentPending",
    );
    expect(visualEnrichmentNoticeKey({ status: "READY", visual_enrichment_status: "running" })).toBe(
      "visualEnrichmentPending",
    );
  });

  it("enrichissement partiel : signalé, jamais bloquant", () => {
    expect(visualEnrichmentNoticeKey({ status: "READY", visual_enrichment_status: "partial" })).toBe(
      "visualEnrichmentPartial",
    );
  });

  it("terminé, non applicable ou champ absent (backend antérieur) : rien", () => {
    for (const s of ["succeeded", "not_applicable", "failed", undefined]) {
      expect(visualEnrichmentNoticeKey({ status: "READY", visual_enrichment_status: s })).toBeNull();
    }
  });

  it("un document non READY n'affiche jamais ce libellé : son statut principal suffit", () => {
    expect(visualEnrichmentNoticeKey({ status: "PENDING", visual_enrichment_status: "pending" })).toBeNull();
  });
});
