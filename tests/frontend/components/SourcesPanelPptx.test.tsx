import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

import { SourcesPanel } from "@/app/chat/_components/SourcesPanel";
import type { Msg, SourceV1 } from "@/app/chat/_lib/types";

/**
 * Lot P1 — le rendu d'une citation de présentation.
 *
 * Le critère visible par l'utilisateur : on affiche « Diapositive 4 », jamais « p. 4 ».
 */

const PPTX: SourceV1 = {
  rank: 1,
  schema_version: 3,
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

const PPTX_NOTES: SourceV1 = {
  rank: 2,
  schema_version: 3,
  document_id: "d-pptx",
  chunk_id: "c-2",
  filename: "architecture.pptx",
  page_number: null,
  snippet: "Penser à mentionner le budget.",
  meta: {
    kind: "pptx_notes",
    slide_number: 4,
    slide_title: "Architecture cible",
  },
};

const PDF: SourceV1 = {
  rank: 3,
  schema_version: 3,
  document_id: "d-pdf",
  chunk_id: "c-3",
  filename: "Rapport.pdf",
  page_number: 12,
  snippet: "Hausse constatée.",
  meta: {},
};

function panel(sources: SourceV1[]) {
  const msg = { role: "assistant", text: "…", sources } as Msg;
  return render(
    <SourcesPanel
      msg={msg}
      activeN={null}
      onCite={vi.fn()}
      onSummarize={vi.fn()}
      busy={false}
    />,
  );
}

describe("SourcesPanel — citation d'une présentation", () => {
  it("affiche le nom du fichier et le numéro de diapositive", () => {
    panel([PPTX]);
    expect(screen.getByText("architecture.pptx")).toBeTruthy();
    expect(screen.getByText("Diapositive 4")).toBeTruthy();
  });

  it("n'affiche JAMAIS « p. N » pour une présentation", () => {
    panel([PPTX]);
    expect(screen.queryByText(/^p\. \d+$/)).toBeNull();
  });

  it("affiche le titre de la diapositive", () => {
    panel([PPTX]);
    expect(screen.getByText("Architecture cible")).toBeTruthy();
  });

  it("le libellé est explicite, jamais le nom technique du contrat", () => {
    panel([PPTX]);
    expect(screen.queryByText(/slide_number/)).toBeNull();
    expect(screen.queryByText(/pptx_slide/)).toBeNull();
  });

  it("expose une étiquette d'accessibilité qui dit la diapositive", () => {
    panel([PPTX]);
    const el = screen.getByLabelText(/diapositive 4/i);
    expect(el).toBeTruthy();
  });
});

describe("SourcesPanel — notes du présentateur", () => {
  it("sont distinguées de la diapositive, en toutes lettres", () => {
    panel([PPTX_NOTES]);
    expect(screen.getByText("Notes du présentateur")).toBeTruthy();
    expect(screen.getByText("Diapositive 4")).toBeTruthy();
  });

  it("une diapositive ordinaire ne porte pas cette pastille", () => {
    panel([PPTX]);
    expect(screen.queryByText("Notes du présentateur")).toBeNull();
  });
});

describe("SourcesPanel — non-régression", () => {
  it("un PDF garde sa page", () => {
    panel([PDF]);
    expect(screen.getByText("p. 12")).toBeTruthy();
    expect(screen.queryByText(/^Diapositive/)).toBeNull();
  });

  it("un panneau mixte rend chaque source selon son format", () => {
    panel([PPTX, PDF]);
    expect(screen.getByText("Diapositive 4")).toBeTruthy();
    expect(screen.getByText("p. 12")).toBeTruthy();
  });

  it("deux diapositives du même deck sont deux sources distinctes", () => {
    panel([PPTX, { ...PPTX, rank: 2, chunk_id: "c-9", meta: { ...PPTX.meta, slide_number: 9, slide_title: "Sauvegarde" } }]);
    expect(screen.getByText("Diapositive 4")).toBeTruthy();
    expect(screen.getByText("Diapositive 9")).toBeTruthy();
  });
});

describe("SourcesPanel — dégradation propre", () => {
  it("un ancien message sans meta ne casse rien", () => {
    const vieux = { rank: 1, filename: "vieux.pptx", page_number: null } as SourceV1;
    panel([vieux]);
    expect(screen.getByText("vieux.pptx")).toBeTruthy();
    expect(screen.queryByText(/^Diapositive/)).toBeNull();
  });

  it("une diapositive sans titre reste citable", () => {
    const sans = { ...PPTX, meta: { kind: "pptx_slide", slide_number: 2 } };
    panel([sans]);
    expect(screen.getByText("Diapositive 2")).toBeTruthy();
  });
});

/**
 * évidence VISUELLE d'une diapositive (image intégrée analysée).
 *
 * Forme exacte projetée par `source_meta` : `slide_number` + repère `{type: "pptx"}`, sans
 * `kind` natif. L'utilisateur voit « Diapositive N », jamais « p. N ». L'aperçu, qui rejoue
 * des positions de texte natif, n'est pas proposé pour ce fragment.
 */
const PPTX_VISUEL: SourceV1 = {
  rank: 4,
  schema_version: 3,
  document_id: "d-pptx",
  chunk_id: "c-visuel",
  filename: "architecture.pptx",
  page_number: null,
  snippet: "Visual type: photograph Description: schéma de principe",
  meta: {
    slide_number: 3,
    locator: { type: "pptx" },
  },
} as SourceV1;

describe("SourcesPanel — évidence visuelle d'une diapositive", () => {
  it("affiche le fichier et « Diapositive N »", () => {
    panel([PPTX_VISUEL]);
    expect(screen.getByText("architecture.pptx")).toBeTruthy();
    expect(screen.getByText("Diapositive 3")).toBeTruthy();
  });

  it("n'affiche JAMAIS « p. N »", () => {
    panel([PPTX_VISUEL]);
    expect(screen.queryByText(/^p\. \d+$/)).toBeNull();
  });

  it("ne propose pas d'aperçu lié au fragment visuel", () => {
    panel([PPTX_VISUEL]);
    expect(screen.queryByText(/Ouvrir le document/)).toBeNull();
  });

  it("la diapositive native voisine garde son lien", () => {
    panel([PPTX, PPTX_VISUEL]);
    expect(screen.getAllByText(/Ouvrir le document/)).toHaveLength(1);
    expect(screen.getByText("Diapositive 4")).toBeTruthy();
    expect(screen.getByText("Diapositive 3")).toBeTruthy();
  });

  it("n'affiche aucun repère technique brut", () => {
    panel([PPTX_VISUEL]);
    expect(screen.queryByText("pptx")).toBeNull();
  });
});
