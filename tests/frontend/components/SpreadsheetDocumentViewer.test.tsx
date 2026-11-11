import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";

import { SpreadsheetDocumentViewer } from "@/app/doc/[documentId]/_components/SpreadsheetDocumentViewer";

const apiFetchRaw = vi.fn();
vi.mock("@/lib/api", () => ({
  apiFetchRaw: (...a: unknown[]) => apiFetchRaw(...a),
}));

function ok(body: unknown) {
  return { ok: true, status: 200, json: async () => body };
}
function fail(status: number, reason_code: string) {
  return {
    ok: false,
    status,
    json: async () => ({ reason_code, detail: "…" }),
  };
}

function preview(over: Record<string, unknown> = {}) {
  return {
    schema_version: 1,
    document: { id: "doc-1", filename: "REFERENTIEL.xlsx", sha256: "abc" },
    workbook: {
      sheet_count: 3,
      sheets: [
        { index: 1, name: "Accueil", state: "visible", has_citation: false },
        {
          index: 2,
          name: "Proposition liste données princ",
          state: "visible",
          has_citation: true,
        },
        { index: 3, name: "Feuille masquée", state: "hidden", has_citation: false },
      ],
    },
    viewport: {
      sheet_index: 2,
      sheet_name: "Proposition liste données princ",
      sheet_state: "visible",
      requested_ranges: ["A749:D761"],
      display_range: "A746:G764",
      row_start: 746,
      row_end: 764,
      column_start: "A",
      column_end: "D",
    },
    columns: [
      { index: 1, letter: "A", header: "Code" },
      { index: 2, letter: "B", header: "Libelle" },
    ],
    rows: [
      {
        row_number: 748,
        cells: [
          { column_index: 1, column_letter: "A", value: "contexte", highlighted: false, flags: [] },
          { column_index: 2, column_letter: "B", value: "avant", highlighted: false, flags: [] },
        ],
      },
      {
        row_number: 749,
        cells: [
          { column_index: 1, column_letter: "A", value: "CRE 13101", highlighted: true, flags: [] },
          { column_index: 2, column_letter: "B", value: "cité", highlighted: true, flags: [] },
        ],
      },
    ],
    fidelity: { simplified: true, omitted: ["styles"], warnings: [] },
    truncation: { truncated: false, reason: null },
    ...over,
  };
}

const base = {
  documentId: "doc-1",
  filename: "REFERENTIEL.xlsx",
  chunkId: "chunk-42",
  onUnauthorized: vi.fn(),
  onDownload: vi.fn(),
  onBack: vi.fn(),
};

beforeEach(() => vi.clearAllMocks());

describe("ouverture sur la source citée", () => {
  it("transmet le chunk_id et ouvre la feuille citée", async () => {
    apiFetchRaw.mockResolvedValue(ok(preview()));
    render(<SpreadsheetDocumentViewer {...base} />);

    await waitFor(() => expect(screen.getByRole("table")).toBeInTheDocument());

    const url = String(apiFetchRaw.mock.calls[0][0]);
    expect(url).toContain("/tabular-preview/");
    expect(url).toContain("chunk_id=chunk-42");
    // Le frontend ne devine ni la feuille ni la plage : il ne les envoie pas.
    expect(url).not.toContain("sheet_index");

    // Le nom apparaît à plusieurs endroits légitimes (onglet, bandeau, légende
    // accessible) : on vérifie l'onglet ACTIF, qui porte la sélection.
    expect(
      screen.getByRole("tab", { name: /Proposition liste données princ/ })
    ).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("A749:D761")).toBeInTheDocument();
  });

  it("surligne exactement les cellules citées", async () => {
    apiFetchRaw.mockResolvedValue(ok(preview()));
    const { container } = render(<SpreadsheetDocumentViewer {...base} />);
    await waitFor(() => expect(screen.getByRole("table")).toBeInTheDocument());

    const marked = container.querySelectorAll('td[data-highlighted="true"]');
    expect(marked.length).toBe(2);
    expect(Array.from(marked).map((n) => n.textContent)).toEqual(["CRE 13101", "cité"]);

    // Le contexte n'est PAS surligné : l'utilisateur doit distinguer les deux.
    const all = container.querySelectorAll("td");
    expect(all.length).toBe(4);
  });

  it("affiche numéros de ligne Excel et lettres de colonnes", async () => {
    apiFetchRaw.mockResolvedValue(ok(preview()));
    render(<SpreadsheetDocumentViewer {...base} />);
    await waitFor(() => expect(screen.getByRole("table")).toBeInTheDocument());

    expect(screen.getByRole("rowheader", { name: "749" })).toBeInTheDocument();
    expect(screen.getByRole("rowheader", { name: "748" })).toBeInTheDocument();
    const head = screen.getAllByRole("columnheader");
    expect(head.some((h) => within(h).queryByText("A"))).toBe(true);
    expect(head.some((h) => within(h).queryByText("Code"))).toBe(true);
  });

  it("remonte la localisation à la barre d'outils", async () => {
    apiFetchRaw.mockResolvedValue(ok(preview()));
    const onLocationChange = vi.fn();
    render(<SpreadsheetDocumentViewer {...base} onLocationChange={onLocationChange} />);

    await waitFor(() =>
      expect(onLocationChange).toHaveBeenCalledWith({
        sheetName: "Proposition liste données princ",
        ranges: ["A749:D761"],
      })
    );
  });

  it("surligne plusieurs plages non contiguës", async () => {
    apiFetchRaw.mockResolvedValue(
      ok(
        preview({
          viewport: {
            ...preview().viewport,
            requested_ranges: ["A749:A749", "M749:P749"],
          },
        })
      )
    );
    render(<SpreadsheetDocumentViewer {...base} />);
    await waitFor(() => expect(screen.getByRole("table")).toBeInTheDocument());
    expect(screen.getByText("A749:A749, M749:P749")).toBeInTheDocument();
  });
});

describe("navigation entre feuilles", () => {
  it("affiche les feuilles indexées, badge la source et la feuille masquée", async () => {
    apiFetchRaw.mockResolvedValue(ok(preview()));
    render(<SpreadsheetDocumentViewer {...base} />);
    await waitFor(() => expect(screen.getByRole("tablist")).toBeInTheDocument());

    const tabs = screen.getAllByRole("tab");
    expect(tabs.length).toBe(3);
    expect(
      screen.getByRole("tab", { name: /contient la source citée/ })
    ).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /feuille masquée/i })).toBeInTheDocument();
  });

  it("changer d'onglet recharge SANS chunk_id (plus de citation à surligner)", async () => {
    apiFetchRaw.mockResolvedValue(ok(preview()));
    render(<SpreadsheetDocumentViewer {...base} />);
    await waitFor(() => expect(screen.getByRole("tablist")).toBeInTheDocument());

    apiFetchRaw.mockResolvedValue(
      ok(
        preview({
          viewport: {
            ...preview().viewport,
            sheet_index: 1,
            sheet_name: "Accueil",
            requested_ranges: [],
          },
        })
      )
    );
    fireEvent.click(screen.getByRole("tab", { name: "Accueil" }));

    await waitFor(() => expect(apiFetchRaw).toHaveBeenCalledTimes(2));
    const url = String(apiFetchRaw.mock.calls[1][0]);
    expect(url).toContain("sheet_index=1");
    expect(url).not.toContain("chunk_id");

    await waitFor(() =>
      expect(screen.getByText(/Aucune citation sur cette feuille/)).toBeInTheDocument()
    );
  });

  it("revenir sur la feuille citée restaure le chunk_id et le surlignage", async () => {
    apiFetchRaw.mockResolvedValue(ok(preview()));
    render(<SpreadsheetDocumentViewer {...base} />);
    await waitFor(() => expect(screen.getByRole("tablist")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("tab", { name: "Accueil" }));
    await waitFor(() => expect(apiFetchRaw).toHaveBeenCalledTimes(2));

    fireEvent.click(
      screen.getByRole("tab", { name: /Proposition liste données princ/ })
    );
    await waitFor(() => expect(apiFetchRaw).toHaveBeenCalledTimes(3));
    expect(String(apiFetchRaw.mock.calls[2][0])).toContain("chunk_id=chunk-42");
  });
});

describe("états d'erreur — jamais de zone blanche", () => {
  it("chunk périmé affiche le message dédié, pas une autre feuille", async () => {
    apiFetchRaw.mockResolvedValue(fail(409, "chunk_stale"));
    render(<SpreadsheetDocumentViewer {...base} />);

    await waitFor(() =>
      expect(
        screen.getByText(/réindexé depuis la création de cette réponse/)
      ).toBeInTheDocument()
    );
    expect(screen.queryByRole("table")).toBeNull();
    expect(
      screen.getByRole("button", { name: /Télécharger l'original/ })
    ).toBeInTheDocument();
  });

  it.each([
    ["sheet_not_previewable", /ne fait pas partie du contenu indexé/],
    ["workbook_unreadable", /illisible ou endommagé/],
    ["preview_timeout", /dépassé le temps imparti/],
    ["unsupported_format", /classeurs \.xlsx/],
    ["range_out_of_bounds", /hors des dimensions/],
    ["no_file_available", /plus disponible dans le stockage/],
  ])("%s produit un message explicite", async (code, pattern) => {
    apiFetchRaw.mockResolvedValue(fail(422, code));
    render(<SpreadsheetDocumentViewer {...base} />);
    await waitFor(() => expect(screen.getByText(pattern)).toBeInTheDocument());
  });

  it("401 déclenche la redirection, sans rendu", async () => {
    const onUnauthorized = vi.fn();
    apiFetchRaw.mockResolvedValue({ ok: false, status: 401, json: async () => ({}) });
    render(<SpreadsheetDocumentViewer {...base} onUnauthorized={onUnauthorized} />);
    await waitFor(() => expect(onUnauthorized).toHaveBeenCalled());
  });

  it("erreur réseau reste explicite", async () => {
    apiFetchRaw.mockRejectedValue(new Error("Failed to fetch"));
    render(<SpreadsheetDocumentViewer {...base} />);
    await waitFor(() =>
      expect(screen.getByText(/n'a pas pu être chargé/)).toBeInTheDocument()
    );
  });

  it("un état de chargement est annoncé", () => {
    apiFetchRaw.mockImplementation(() => new Promise(() => {}));
    render(<SpreadsheetDocumentViewer {...base} />);
    expect(screen.getByText(/Chargement de l'aperçu/)).toBeInTheDocument();
  });

  it("feuille vide : message, pas une grille vide muette", async () => {
    apiFetchRaw.mockResolvedValue(ok(preview({ rows: [] })));
    render(<SpreadsheetDocumentViewer {...base} />);
    await waitFor(() =>
      expect(screen.getByText(/aucune donnée dans la zone affichée/)).toBeInTheDocument()
    );
  });
});

describe("transparence et sécurité", () => {
  it("le bandeau de fidélité est toujours affiché", async () => {
    apiFetchRaw.mockResolvedValue(ok(preview()));
    render(<SpreadsheetDocumentViewer {...base} />);
    await waitFor(() => expect(screen.getByText(/Aperçu simplifié/)).toBeInTheDocument());
    expect(screen.getByText(/Aucune formule n'est recalculée/)).toBeInTheDocument();
  });

  it("la troncature est déclarée", async () => {
    apiFetchRaw.mockResolvedValue(
      ok(preview({ truncation: { truncated: true, reason: "max_cells" } }))
    );
    render(<SpreadsheetDocumentViewer {...base} />);
    await waitFor(() =>
      expect(screen.getByText(/Aperçu tronqué \(max_cells\)/)).toBeInTheDocument()
    );
  });

  it("les avertissements du classeur sont repris", async () => {
    apiFetchRaw.mockResolvedValue(
      ok(
        preview({
          fidelity: {
            simplified: true,
            omitted: [],
            warnings: ["workbook_requires_recalculation", "merged_cells_not_rendered"],
          },
        })
      )
    );
    render(<SpreadsheetDocumentViewer {...base} />);
    await waitFor(() =>
      expect(screen.getByText(/formules sans valeur enregistrée/)).toBeInTheDocument()
    );
    expect(screen.getByText(/cellules fusionnées/)).toBeInTheDocument();
  });

  it("le HTML d'une cellule est affiché comme TEXTE, jamais interprété", async () => {
    apiFetchRaw.mockResolvedValue(
      ok(
        preview({
          rows: [
            {
              row_number: 1,
              cells: [
                {
                  column_index: 1,
                  column_letter: "A",
                  value: "<script>alert(1)</script>",
                  highlighted: false,
                  flags: [],
                },
                {
                  column_index: 2,
                  column_letter: "B",
                  value: "<img src=x onerror=alert(1)>",
                  highlighted: false,
                  flags: [],
                },
              ],
            },
          ],
        })
      )
    );
    const { container } = render(<SpreadsheetDocumentViewer {...base} />);
    await waitFor(() => expect(screen.getByRole("table")).toBeInTheDocument());

    expect(screen.getByText("<script>alert(1)</script>")).toBeInTheDocument();
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
  });

  it("un lien javascript: reste du texte inerte", async () => {
    apiFetchRaw.mockResolvedValue(
      ok(
        preview({
          rows: [
            {
              row_number: 1,
              cells: [
                {
                  column_index: 1,
                  column_letter: "A",
                  value: "javascript:alert(1)",
                  highlighted: false,
                  flags: [],
                },
              ],
            },
          ],
        })
      )
    );
    const { container } = render(<SpreadsheetDocumentViewer {...base} />);
    await waitFor(() => expect(screen.getByRole("table")).toBeInTheDocument());
    expect(screen.getByText("javascript:alert(1)")).toBeInTheDocument();
    expect(container.querySelector("a")).toBeNull();
  });

  it("les drapeaux de cellule sont exposés aux lecteurs d'écran", async () => {
    apiFetchRaw.mockResolvedValue(
      ok(
        preview({
          rows: [
            {
              row_number: 1,
              cells: [
                {
                  column_index: 1,
                  column_letter: "A",
                  value: "",
                  highlighted: false,
                  flags: ["uncached_formula"],
                },
                {
                  column_index: 2,
                  column_letter: "B",
                  value: "#N/A",
                  highlighted: false,
                  flags: ["error_cell"],
                },
              ],
            },
          ],
        })
      )
    );
    render(<SpreadsheetDocumentViewer {...base} />);
    await waitFor(() => expect(screen.getByRole("table")).toBeInTheDocument());
    expect(screen.getByText(/Formule non recalculée/)).toBeInTheDocument();
    expect(screen.getByText(/Erreur Excel/)).toBeInTheDocument();
  });

  it("une valeur tronquée annonce sa longueur réelle", async () => {
    apiFetchRaw.mockResolvedValue(
      ok(
        preview({
          rows: [
            {
              row_number: 1,
              cells: [
                {
                  column_index: 1,
                  column_letter: "A",
                  value: "x".repeat(20),
                  highlighted: false,
                  flags: ["truncated"],
                  full_length: 4200,
                },
              ],
            },
          ],
        })
      )
    );
    const { container } = render(<SpreadsheetDocumentViewer {...base} />);
    await waitFor(() => expect(screen.getByRole("table")).toBeInTheDocument());
    const td = container.querySelector("td")!;
    expect(td.getAttribute("title")).toMatch(/4[\s  ]?200 caractères/);
  });

  it("la grille est une région accessible et navigable au clavier", async () => {
    apiFetchRaw.mockResolvedValue(ok(preview()));
    render(<SpreadsheetDocumentViewer {...base} />);
    await waitFor(() => expect(screen.getByRole("table")).toBeInTheDocument());

    const region = screen.getByRole("region", {
      name: /Feuille Proposition liste données princ/,
    });
    expect(region.getAttribute("tabindex")).toBe("0");
  });
});
