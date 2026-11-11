import { describe, it, expect } from "vitest";
import {
  srcCellRange,
  srcDoc,
  srcIsExcel,
  srcKind,
  srcMeta,
  srcPage,
  srcSheet,
  srcTitle,
  srcViewerHref,
  type AnySource,
} from "@/app/chat/_lib/sources";

const EXCEL: AnySource = {
  rank: 1,
  schema_version: 3,
  document_id: "d-excel",
  chunk_id: "c-1",
  filename: "Budget_2026.xlsx",
  page_number: null,
  snippet: "| Atlas | 250000 |",
  meta: {
    kind: "excel_table",
    sheet_name: "Synthèse",
    cell_range: "B8:F17",
    row_start: 8,
    row_end: 17,
    title: "Synthèse budgétaire",
  },
} as AnySource;

const PDF: AnySource = {
  rank: 2,
  schema_version: 3,
  document_id: "d-pdf",
  chunk_id: "c-2",
  filename: "Rapport.pdf",
  page_number: 12,
  snippet: "Hausse constatée.",
  meta: {},
} as AnySource;

describe("helpers de citation Excel", () => {
  it("feuille, plage et titre viennent de meta — et de nulle part ailleurs", () => {
    expect(srcSheet(EXCEL)).toBe("Synthèse");
    expect(srcCellRange(EXCEL)).toBe("B8:F17");
    expect(srcTitle(EXCEL)).toBe("Synthèse budgétaire");
    expect(srcKind(EXCEL)).toBe("excel_table");
    expect(srcIsExcel(EXCEL)).toBe(true);
  });

  it("un classeur n'a JAMAIS de page (une feuille n'est pas une page)", () => {
    expect(srcPage(EXCEL)).toBeNull();
  });

  it("un PDF conserve sa page et n'a ni feuille ni plage", () => {
    expect(srcPage(PDF)).toBe(12);
    expect(srcSheet(PDF)).toBeNull();
    expect(srcCellRange(PDF)).toBeNull();
    expect(srcIsExcel(PDF)).toBe(false);
  });

  it("un tableau PDF (P3) n'est pas un classeur", () => {
    const p3 = { ...PDF, meta: { kind: "table", table_type: "pile_head_forces" } } as AnySource;
    expect(srcIsExcel(p3)).toBe(false);
    expect(srcSheet(p3)).toBeNull();
  });

  it("lit aussi meta depuis le format stream (source.meta)", () => {
    const streamed = {
      rank: 1,
      text: "…",
      source: { filename: "B.xlsx", meta: { kind: "excel_table", sheet_name: "Risques", cell_range: "A2:G12" } },
    } as unknown as AnySource;
    expect(srcSheet(streamed)).toBe("Risques");
    expect(srcCellRange(streamed)).toBe("A2:G12");
    expect(srcDoc(streamed)).toBe("B.xlsx");
  });
});

describe("dégradation propre — anciens contrats et données abîmées", () => {
  it("source sans meta : aucune feuille inventée, aucune erreur", () => {
    const old = { rank: 1, filename: "Budget_2026.xlsx", page_number: null } as AnySource;
    expect(srcSheet(old)).toBeNull();
    expect(srcCellRange(old)).toBeNull();
    expect(srcTitle(old)).toBeNull();
    expect(srcDoc(old)).toBe("Budget_2026.xlsx"); // le fichier reste affiché
    expect(srcMeta(old)).toEqual({});
  });

  it("meta partielle : la feuille s'affiche même sans plage", () => {
    const partial = {
      rank: 1,
      filename: "B.xlsx",
      meta: { kind: "excel_table", sheet_name: "Synthèse" },
    } as AnySource;
    expect(srcSheet(partial)).toBe("Synthèse");
    expect(srcCellRange(partial)).toBeNull();
  });

  it("chaînes vides -> null (pas de libellé « Feuille : » vide)", () => {
    const empty = { rank: 1, meta: { sheet_name: "   ", cell_range: "" } } as AnySource;
    expect(srcSheet(empty)).toBeNull();
    expect(srcCellRange(empty)).toBeNull();
  });

  it("types invalides -> null, jamais de crash", () => {
    const bad = { rank: 1, meta: { sheet_name: 42, cell_range: ["x"] } } as unknown as AnySource;
    expect(srcSheet(bad)).toBeNull();
    expect(srcCellRange(bad)).toBeNull();
  });

  it("meta non-objet -> {}", () => {
    const bad = { rank: 1, meta: "oops" } as unknown as AnySource;
    expect(srcMeta(bad)).toEqual({});
  });

  it("source null/undefined -> valeurs neutres", () => {
    expect(srcSheet(null)).toBeNull();
    expect(srcCellRange(undefined)).toBeNull();
    expect(srcPage(null)).toBeNull();
    expect(srcDoc(null)).toBe("Document");
  });

  it("page 0 ou négative -> null (les pages commencent à 1)", () => {
    expect(srcPage({ rank: 1, page_number: 0 } as AnySource)).toBeNull();
    expect(srcPage({ rank: 1, page_number: -3 } as AnySource)).toBeNull();
  });
});

describe("lien d'ouverture", () => {
  it("un classeur n'ajoute JAMAIS page= (pas de faux ancrage)", () => {
    const href = srcViewerHref(EXCEL)!;
    expect(href).toContain("/doc/d-excel");
    expect(href).not.toContain("page=");
    expect(href).toContain("chunk_id=c-1"); // le surlignage reste possible
  });

  it("un PDF conserve page= (non-régression)", () => {
    const href = srcViewerHref(PDF)!;
    expect(href).toContain("page=12");
    expect(href).toContain("chunk_id=c-2");
  });

  it("sans document_id, pas de lien (jamais de bouton inopérant)", () => {
    expect(srcViewerHref({ rank: 1, filename: "x.xlsx" } as AnySource)).toBeNull();
  });
});
