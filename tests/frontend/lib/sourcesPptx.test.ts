import { describe, expect, it } from "vitest";

import {
  srcIsExcel,
  srcIsPptx,
  srcIsSlideVisual,
  srcIsSpeakerNotes,
  srcKind,
  srcPage,
  srcSlide,
  srcSlideTitle,
  srcViewerHref,
} from "@/app/chat/_lib/sources";

/**
 * Lot P1 — helpers de citation d'une présentation.
 *
 * L'invariant, symétrique de celui des classeurs : **une diapositive n'est pas une page**.
 * Le numéro vient de `meta.slide_number`, jamais de `page_number`, et réciproquement —
 * aucun des deux ne se fabrique depuis l'autre.
 */

const PPTX_SOURCE = {
  rank: 1,
  document_id: "d-pptx",
  chunk_id: "c-1",
  filename: "architecture.pptx",
  page_number: null,
  snippet: "[Diapositive 4] Titre : Architecture cible",
  meta: {
    kind: "pptx_slide",
    slide_number: 4,
    slide_title: "Architecture cible",
  },
};

const PDF_SOURCE = {
  rank: 1,
  document_id: "d-pdf",
  chunk_id: "c-2",
  filename: "rapport.pdf",
  page_number: 12,
  snippet: "Le rapport indique une hausse.",
  meta: {},
};

describe("citation d'une présentation — la provenance vient de meta, et de nulle part ailleurs", () => {
  it("expose le numéro de diapositive", () => {
    expect(srcSlide(PPTX_SOURCE)).toBe(4);
  });

  it("expose le titre de la diapositive", () => {
    expect(srcSlideTitle(PPTX_SOURCE)).toBe("Architecture cible");
  });

  it("reconnaît le type de source", () => {
    expect(srcIsPptx(PPTX_SOURCE)).toBe(true);
    expect(srcKind(PPTX_SOURCE)).toBe("pptx_slide");
    expect(srcIsExcel(PPTX_SOURCE)).toBe(false);
  });

  it("une présentation n'a JAMAIS de page", () => {
    expect(srcPage(PPTX_SOURCE)).toBeNull();
  });

  it("ne fabrique pas une page depuis le numéro de diapositive", () => {
    expect(srcPage(PPTX_SOURCE)).not.toBe(4);
  });

  it("ne fabrique pas une diapositive depuis un numéro de page", () => {
    expect(srcSlide(PDF_SOURCE)).toBeNull();
  });

  it("ne déduit pas la diapositive du snippet, même s'il la mentionne", () => {
    const s = { ...PPTX_SOURCE, meta: {}, snippet: "[Diapositive 9] du texte" };
    expect(srcSlide(s)).toBeNull();
  });

  it("ne déduit pas la diapositive du nom de fichier", () => {
    const s = { rank: 1, filename: "deck-diapositive-3.pptx", meta: {} };
    expect(srcSlide(s)).toBeNull();
  });
});

describe("numéros humains", () => {
  it("écarte les numéros < 1 (artefacts, pas des informations)", () => {
    expect(srcSlide({ rank: 1, meta: { slide_number: 0 } })).toBeNull();
    expect(srcSlide({ rank: 1, meta: { slide_number: -2 } })).toBeNull();
  });

  it("accepte la première diapositive", () => {
    expect(srcSlide({ rank: 1, meta: { slide_number: 1 } })).toBe(1);
  });

  it("tolère un numéro sérialisé en chaîne", () => {
    expect(srcSlide({ rank: 1, meta: { slide_number: "7" } })).toBe(7);
  });

  it("écarte une valeur non numérique", () => {
    expect(srcSlide({ rank: 1, meta: { slide_number: "quatre" } })).toBeNull();
  });
});

describe("notes du présentateur", () => {
  const NOTES = {
    rank: 1,
    filename: "architecture.pptx",
    page_number: null,
    meta: { kind: "pptx_notes", slide_number: 4, slide_title: "Architecture cible" },
  };

  it("est distinguée de la diapositive", () => {
    expect(srcIsSpeakerNotes(NOTES)).toBe(true);
    expect(srcIsSpeakerNotes(PPTX_SOURCE)).toBe(false);
  });

  it("reste rattachée à sa diapositive", () => {
    expect(srcSlide(NOTES)).toBe(4);
  });

  it("compte comme une source de présentation", () => {
    expect(srcIsPptx(NOTES)).toBe(true);
  });
});

describe("dégradation propre", () => {
  it("une source sans meta ne casse rien", () => {
    const s = { rank: 1, filename: "vieux.pptx" };
    expect(srcSlide(s)).toBeNull();
    expect(srcSlideTitle(s)).toBeNull();
    expect(srcIsPptx(s)).toBe(false);
  });

  it("une diapositive sans titre reste citable", () => {
    const s = { rank: 1, meta: { kind: "pptx_slide", slide_number: 2 } };
    expect(srcSlide(s)).toBe(2);
    expect(srcSlideTitle(s)).toBeNull();
  });

  it("accepte le format RetrievalChunk (meta imbriquée dans source)", () => {
    const s = {
      rank: 1,
      source: { filename: "d.pptx", page_number: null, meta: { kind: "pptx_slide", slide_number: 5 } },
    };
    expect(srcSlide(s)).toBe(5);
  });
});

describe("lien viewer", () => {
  it("n'ajoute AUCUN page= pour une présentation", () => {
    const href = srcViewerHref(PPTX_SOURCE);
    expect(href).toBeTruthy();
    expect(href).not.toContain("page=");
  });

  it("ne fabrique pas un ancrage page=4 depuis la diapositive 4", () => {
    expect(srcViewerHref(PPTX_SOURCE)).not.toContain("page=4");
  });

  it("un PDF garde son ancrage de page (non-régression)", () => {
    expect(srcViewerHref(PDF_SOURCE)).toContain("page=12");
  });
});

describe("évidence visuelle d'une diapositive", () => {
  const VISUEL = {
    rank: 1,
    document_id: "d-pptx",
    chunk_id: "c-visuel",
    filename: "architecture.pptx",
    page_number: null,
    snippet: "Visual type: photograph",
    meta: { slide_number: 3, locator: { type: "pptx" } },
  };

  it("cite la diapositive, jamais une page", () => {
    expect(srcSlide(VISUEL)).toBe(3);
    expect(srcPage(VISUEL)).toBeNull();
  });

  it("n'est pas une diapositive native", () => {
    expect(srcIsPptx(VISUEL)).toBe(false);
    expect(srcKind(VISUEL)).toBeNull();
    expect(srcIsSlideVisual(VISUEL)).toBe(true);
  });

  it("n'ouvre pas l'aperçu de présentation (aucune position de texte à rejouer)", () => {
    expect(srcViewerHref(VISUEL)).toBeNull();
  });

  it("les fragments natifs et les autres formats ne sont pas concernés", () => {
    expect(srcIsSlideVisual(PPTX_SOURCE)).toBe(false);
    expect(srcIsSlideVisual(PDF_SOURCE)).toBe(false);
    expect(srcViewerHref(PPTX_SOURCE)).toContain("chunk_id=c-1");
    expect(srcViewerHref(PDF_SOURCE)).toContain("page=12");
  });
});
