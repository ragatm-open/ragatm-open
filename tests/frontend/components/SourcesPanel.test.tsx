import fs from "node:fs";
import path from "node:path";
import { describe, it, expect, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { SourcesPanel } from "@/app/chat/_components/SourcesPanel";
import type { Msg, SourceV1 } from "@/app/chat/_lib/types";

const EXCEL: SourceV1 = {
  rank: 1,
  schema_version: 3,
  document_id: "d-excel",
  chunk_id: "c-1",
  filename: "Budget_2026.xlsx",
  page_number: null,
  snippet: "| Atlas | 250000 | 220000 |",
  meta: {
    kind: "excel_table",
    sheet_name: "Synthèse",
    cell_range: "B8:F17",
    row_start: 8,
    row_end: 17,
    title: "Synthèse budgétaire",
  },
};

const PDF: SourceV1 = {
  rank: 2,
  schema_version: 3,
  document_id: "d-pdf",
  chunk_id: "c-2",
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

describe("SourcesPanel — citation d'un classeur", () => {
  it("affiche fichier, titre, feuille et plage", () => {
    panel([EXCEL]);
    expect(screen.getByText("Budget_2026.xlsx")).toBeInTheDocument();
    expect(screen.getByText("Synthèse budgétaire")).toBeInTheDocument();
    expect(screen.getByText("Feuille : Synthèse")).toBeInTheDocument();
    expect(screen.getByText("Cellules : B8:F17")).toBeInTheDocument();
  });

  it("n'affiche JAMAIS de numéro de page pour un classeur", () => {
    const { container } = panel([EXCEL]);
    expect(container.textContent).not.toMatch(/p\.\s*\d/);
    expect(container.textContent).not.toMatch(/Page\s*(null|0)/i);
  });

  it("utilise des libellés humains, jamais les noms techniques du contrat", () => {
    const { container } = panel([EXCEL]);
    expect(container.textContent).not.toContain("sheet_name");
    expect(container.textContent).not.toContain("cell_range");
    expect(container.textContent).not.toContain("excel_table");
  });

  it("le lien d'ouverture ne contient pas page=", () => {
    panel([EXCEL]);
    const link = screen.getByRole("link", { name: /Ouvrir le document/i });
    const href = link.getAttribute("href") || "";
    expect(href).toContain("/doc/d-excel");
    expect(href).not.toContain("page=");
  });

  it("nom accessible : fichier, feuille et cellules", () => {
    panel([EXCEL]);
    const card = screen.getByLabelText(
      "Source Budget_2026.xlsx, feuille Synthèse, cellules B8 à F17",
    );
    expect(card).toBeInTheDocument();
  });
});

describe("SourcesPanel — non-régression PDF", () => {
  it("un PDF garde sa page et n'affiche ni feuille ni plage", () => {
    panel([PDF]);
    expect(screen.getByText("Rapport.pdf")).toBeInTheDocument();
    expect(screen.getByText("p. 12")).toBeInTheDocument();
    expect(screen.queryByText(/^Feuille :/)).toBeNull();
    expect(screen.queryByText(/^Cellules :/)).toBeNull();
  });

  it("le lien PDF conserve page=", () => {
    panel([PDF]);
    const href = screen.getByRole("link", { name: /Ouvrir le document/i }).getAttribute("href") || "";
    expect(href).toContain("page=12");
  });
});

describe("SourcesPanel — dégradation propre", () => {
  it("classeur sans feuille ni plage : le fichier reste affiché, rien n'est inventé", () => {
    const incomplete: SourceV1 = {
      rank: 1,
      filename: "Budget_2026.xlsx",
      page_number: null,
      snippet: "x",
      meta: { kind: "excel_table" },
    };
    const { container } = panel([incomplete]);
    expect(screen.getByText("Budget_2026.xlsx")).toBeInTheDocument();
    expect(screen.queryByText(/^Feuille :/)).toBeNull();
    expect(screen.queryByText(/^Cellules :/)).toBeNull();
    // Aucun libellé vide du type « Feuille :  ».
    expect(container.textContent).not.toMatch(/Feuille :\s*$/m);
  });

  it("ancien contrat (aucun meta) : rendu inchangé, aucun crash", () => {
    const legacy = {
      rank: 1,
      schema_version: 2,
      document_id: "d",
      chunk_id: "c",
      filename: "vieux.pdf",
      page_number: 3,
      snippet: "…",
    } as SourceV1;
    panel([legacy]);
    expect(screen.getByText("vieux.pdf")).toBeInTheDocument();
    expect(screen.getByText("p. 3")).toBeInTheDocument();
  });

  it("feuille très longue : elle reste lisible et se replie (pas de scroll horizontal)", () => {
    const long: SourceV1 = {
      ...EXCEL,
      meta: { ...EXCEL.meta, sheet_name: "Synthèse budgétaire consolidée France et Europe 2026" },
    };
    const { container } = panel([long]);
    expect(
      screen.getByText("Feuille : Synthèse budgétaire consolidée France et Europe 2026"),
    ).toBeInTheDocument();
    // Le nom de fichier se replie plutôt que d'élargir le panneau.
    const name = screen.getByText("Budget_2026.xlsx");
    expect(name).toHaveStyle({ overflowWrap: "anywhere" });
    // La largeur fixe du panneau vit désormais dans son CSS module (mise en page
    // responsive : colonne de 340 px en desktop, tiroir en dessous de 1200 px).
    // jsdom ne charge pas les CSS modules : on vérifie la règle desktop à la source.
    expect(container.querySelector("aside")).toHaveAttribute("id", "chat-sources");
    const css = fs.readFileSync(
      path.resolve(__dirname, "../../../frontend/src/app/chat/_components/SourcesPanel.module.css"),
      "utf8",
    );
    expect(css).toMatch(/^\.panel \{\n  width: 340px;\n  flex: none;/m);
  });
});

describe("SourcesPanel — deux plages du même classeur", () => {
  it("restent DEUX sources distinctes", () => {
    const a: SourceV1 = { ...EXCEL, rank: 1, chunk_id: "c-1" };
    const b: SourceV1 = {
      ...EXCEL,
      rank: 2,
      chunk_id: "c-2",
      meta: { kind: "excel_table", sheet_name: "Risques", cell_range: "A2:G12" },
    };
    panel([a, b]);

    expect(screen.getByText("Feuille : Synthèse")).toBeInTheDocument();
    expect(screen.getByText("Cellules : B8:F17")).toBeInTheDocument();
    expect(screen.getByText("Feuille : Risques")).toBeInTheDocument();
    expect(screen.getByText("Cellules : A2:G12")).toBeInTheDocument();

    // Le même fichier apparaît deux fois : ce sont bien deux citations.
    expect(screen.getAllByText("Budget_2026.xlsx")).toHaveLength(2);
  });
});

describe("SourcesPanel — compteur", () => {
  it("annonce le nombre de sources", () => {
    panel([EXCEL, PDF]);
    const header = screen.getByText("Sources").parentElement!;
    expect(within(header).getByText("2")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Badge de provenance : trois états EXCLUSIFS
// ---------------------------------------------------------------------------
const CORPUS_SOURCE: SourceV1 = {
  rank: 1,
  schema_version: 3,
  source_type: "domain_corpus",
  document_id: "d-corpus",
  chunk_id: "c-corpus",
  filename: "reference.pdf",
  page_number: 3,
  snippet: "Définition de référence.",
  corpus_name: "VC Reference Corpus",
  business_domain_name: "Venture Capital",
  meta: {},
} as SourceV1;

const PRIVATE_SOURCE: SourceV1 = {
  rank: 1,
  schema_version: 3,
  source_type: "workspace_document",
  document_id: "d-priv",
  chunk_id: "c-priv",
  filename: "note.pdf",
  page_number: 1,
  snippet: "Note interne.",
  meta: {},
} as SourceV1;

const NO_PROVENANCE_SOURCE: SourceV1 = {
  rank: 1,
  schema_version: 3,
  document_id: "d-old",
  chunk_id: "c-old",
  filename: "ancien.pdf",
  page_number: 1,
  snippet: "Message antérieur au contrat v2.",
  meta: {},
};

describe("SourcesPanel — badge de provenance", () => {
  it("une source corpus affiche le domaine ET le nom du corpus", () => {
    const { container } = panel([CORPUS_SOURCE]);
    expect(screen.getByText(/Corpus Venture Capital/)).toBeInTheDocument();
    expect(container.textContent).toContain("VC Reference Corpus");
    expect(container.textContent).not.toContain("Workspace");
  });

  it("une source privée explicite affiche « Workspace »", () => {
    panel([PRIVATE_SOURCE]);
    expect(screen.getByText("Workspace")).toBeInTheDocument();
  });

  it("une provenance ABSENTE n'est pas présentée comme « Workspace »", () => {
    // C'est le défaut corrigé : une origine perdue en amont était affirmée privée.
    const { container } = panel([NO_PROVENANCE_SOURCE]);
    expect(screen.getByText("Provenance inconnue")).toBeInTheDocument();
    expect(container.textContent).not.toContain("Workspace");
  });

  it("les trois provenances coexistent dans un même message", () => {
    const { container } = panel([
      { ...CORPUS_SOURCE, rank: 1 },
      { ...PRIVATE_SOURCE, rank: 2 },
      { ...NO_PROVENANCE_SOURCE, rank: 3 },
    ]);
    expect(container.textContent).toContain("Corpus Venture Capital");
    expect(container.textContent).toContain("Workspace");
    expect(container.textContent).toContain("Provenance inconnue");
  });
});
