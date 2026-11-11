import { describe, it, expect } from "vitest";
import {
  parseSummaryIntent,
  resolveSummaryTargetFromSources,
  matchDocumentsByName,
} from "@/app/chat/_lib/summaryIntent";
import type { AnySource } from "@/app/chat/_lib/sources";

function src(rank: number, document_id: string | null, filename: string): AnySource {
  return { rank, document_id, filename } as AnySource;
}

describe("parseSummaryIntent", () => {
  it("détecte « Résume ce document » (déictique)", () => {
    expect(parseSummaryIntent("Résume ce document")).toEqual({ isSummary: true, ref: { kind: "deictic" } });
  });

  it("détecte une synthèse d'article (déictique)", () => {
    expect(parseSummaryIntent("Fais-moi une synthèse de cet article")).toEqual({
      isSummary: true,
      ref: { kind: "deictic" },
    });
  });

  it("détecte « les points clés de ce PDF » (déictique)", () => {
    expect(parseSummaryIntent("Donne-moi les points clés de ce PDF")).toEqual({
      isSummary: true,
      ref: { kind: "deictic" },
    });
  });

  it("détecte « la source 2 »", () => {
    expect(parseSummaryIntent("Résume la source 2")).toEqual({ isSummary: true, ref: { kind: "source", n: 2 } });
  });

  it("détecte « source n°3 »", () => {
    expect(parseSummaryIntent("résume la source n°3")).toEqual({ isSummary: true, ref: { kind: "source", n: 3 } });
  });

  it("détecte un nom de fichier explicite", () => {
    expect(parseSummaryIntent("Résume le document Revue_de_presse-2025-08-14.pdf")).toEqual({
      isSummary: true,
      ref: { kind: "filename", name: "Revue_de_presse-2025-08-14.pdf" },
    });
  });

  it("ignore un résumé d'espace (cible non documentaire)", () => {
    expect(parseSummaryIntent("Résume les points clés de cet espace.")).toEqual({ isSummary: false, ref: null });
  });

  it("ignore « résume notre conversation »", () => {
    expect(parseSummaryIntent("Résume notre conversation")).toEqual({ isSummary: false, ref: null });
  });

  it("n'intercepte pas une question sans déclencheur", () => {
    expect(parseSummaryIntent("Quelles sont les règles applicables dans ce document ?")).toEqual({
      isSummary: false,
      ref: null,
    });
  });
});

describe("resolveSummaryTargetFromSources", () => {
  const sources = [src(1, "doc-a", "A.pdf"), src(2, "doc-b", "B.pdf")];

  it("résout « source 2 » vers son document", () => {
    const r = resolveSummaryTargetFromSources({ kind: "source", n: 2 }, sources);
    // Les sources de ce jeu d'essai n'ont pas de `source_type` : elles imitent
    // l'historique antérieur à la v2 du contrat, dont la provenance est `unknown`
    // et qui doit continuer à emprunter l'endpoint privé.
    expect(r).toMatchObject({
      status: "resolved",
      ref: { sourceType: "unknown", documentId: "doc-b" },
      scope: "document",
    });
  });

  it("clarifie un index de source hors limites", () => {
    const r = resolveSummaryTargetFromSources({ kind: "source", n: 9 }, sources);
    expect(r.status).toBe("need_clarification");
  });

  it("clarifie « source N » sans sources", () => {
    const r = resolveSummaryTargetFromSources({ kind: "source", n: 1 }, []);
    expect(r.status).toBe("need_clarification");
  });

  it("résout un déictique quand un seul document est cité", () => {
    const r = resolveSummaryTargetFromSources({ kind: "deictic" }, [src(1, "doc-a", "A.pdf"), src(2, "doc-a", "A.pdf")]);
    expect(r).toMatchObject({ status: "resolved", ref: { documentId: "doc-a" } });
  });

  it("clarifie un déictique ambigu (plusieurs documents)", () => {
    const r = resolveSummaryTargetFromSources({ kind: "deictic" }, sources);
    expect(r.status).toBe("need_clarification");
  });

  it("clarifie un déictique sans sources", () => {
    const r = resolveSummaryTargetFromSources({ kind: "deictic" }, []);
    expect(r.status).toBe("need_clarification");
  });

  it("résout un nom de fichier présent dans les sources", () => {
    const r = resolveSummaryTargetFromSources({ kind: "filename", name: "B.pdf" }, sources);
    expect(r).toMatchObject({ status: "resolved", ref: { documentId: "doc-b" } });
  });

  it("demande une recherche documents si le nom est absent des sources", () => {
    const r = resolveSummaryTargetFromSources({ kind: "filename", name: "Inconnu.pdf" }, sources);
    expect(r).toEqual({ status: "lookup_docs", name: "Inconnu.pdf" });
  });
});

describe("matchDocumentsByName", () => {
  const docs = [
    { id: "d1", filename: "Revue_de_presse-2025-08-14.pdf", status: "READY" },
    { id: "d2", filename: "Rapport annuel.pdf", status: "READY" },
    { id: "d3", filename: "Brouillon.pdf", status: "PENDING" },
  ];

  it("résout une correspondance unique READY", () => {
    expect(matchDocumentsByName("Revue_de_presse-2025-08-14.pdf", docs)).toEqual({
      status: "resolved",
      documentId: "d1",
      filename: "Revue_de_presse-2025-08-14.pdf",
    });
  });

  it("résout par sous-chaîne (insensible à la casse/accents)", () => {
    expect(matchDocumentsByName("revue", docs)).toMatchObject({ status: "resolved", documentId: "d1" });
  });

  it("clarifie plusieurs correspondances", () => {
    const r = matchDocumentsByName("pdf", docs);
    expect(r.status).toBe("need_clarification");
  });

  it("ignore les documents non READY", () => {
    expect(matchDocumentsByName("Brouillon.pdf", docs)).toEqual({ status: "not_found" });
  });

  it("renvoie not_found sans correspondance", () => {
    expect(matchDocumentsByName("xyz", docs)).toEqual({ status: "not_found" });
  });
});
