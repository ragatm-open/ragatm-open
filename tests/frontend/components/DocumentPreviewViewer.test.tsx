import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";

import { DocumentPreviewViewer } from "@/app/doc/[documentId]/_components/DocumentPreviewViewer";

const apiFetchRaw = vi.fn();
vi.mock("@/lib/api", () => ({
  apiFetchRaw: (...a: unknown[]) => apiFetchRaw(...a),
}));

function ok(body: unknown) {
  return { ok: true, status: 200, json: async () => body };
}
function fail(status: number, reason_code: string) {
  return { ok: false, status, json: async () => ({ reason_code, detail: "…" }) };
}

function block(over: Record<string, unknown> = {}) {
  return {
    id: "b-00049",
    index: 49,
    type: "paragraph",
    text: "Le risque principal concerne la disponibilité.",
    highlight: null,
    truncated: false,
    full_length: 46,
    warnings: [],
    ...over,
  };
}

function preview(over: Record<string, unknown> = {}) {
  return {
    schema_version: 1,
    document: {
      id: "doc-1",
      filename: "rapport.docx",
      sha256: "abc",
      preview_kind: "document",
    },
    source: { chunk_id: "chunk-42", char_start: 1200, char_end: 1680 },
    navigation: {
      headings: [
        { id: "h-00012", block_index: 12, level: 1, text: "4. Risques" },
        { id: "h-00020", block_index: 20, level: 2, text: "Risques opérationnels" },
      ],
      headings_truncated: false,
    },
    viewport: {
      first_block: 47,
      last_block: 51,
      total_blocks: 230,
      source_first_block: 49,
      source_last_block: 50,
    },
    blocks: [
      block({ id: "b-00047", index: 47, text: "Contexte avant." }),
      block({ id: "b-00049", index: 49, highlight: { start: 3, end: 11 } }),
      block({
        id: "b-00050",
        index: 50,
        text: "Suite du passage cité ici.",
        highlight: { start: 0, end: 21 },
      }),
      block({ id: "b-00051", index: 51, text: "Contexte après." }),
    ],
    fidelity: {
      simplified: true,
      extractor_version: "docx-v1",
      warnings: ["images_not_extracted", "lists_flattened", "tables_simplified"],
      omitted: ["images", "headers", "computed_pagination"],
    },
    truncation: { truncated: false, reason: null },
    ...over,
  };
}

const base = {
  documentId: "doc-1",
  filename: "rapport.docx",
  chunkId: "chunk-42",
  onUnauthorized: vi.fn(),
  onDownload: vi.fn(),
  onBack: vi.fn(),
};

beforeEach(() => {
  vi.clearAllMocks();
  Element.prototype.scrollIntoView = vi.fn();
});

describe("ouverture sur la source citée", () => {
  it("transmet le chunk_id, jamais le snippet", async () => {
    apiFetchRaw.mockResolvedValue(ok(preview()));
    render(<DocumentPreviewViewer {...base} />);
    await waitFor(() => expect(screen.getByRole("region")).toBeInTheDocument());

    const url = String(apiFetchRaw.mock.calls[0][0]);
    expect(url).toContain("/document-preview/");
    expect(url).toContain("chunk_id=chunk-42");
    expect(url).not.toContain("first_block");
    expect(url).not.toContain("snippet");
  });

  it("surligne au caractère, pas le bloc entier", async () => {
    apiFetchRaw.mockResolvedValue(ok(preview()));
    const { container } = render(<DocumentPreviewViewer {...base} />);
    await waitFor(() => expect(screen.getByRole("region")).toBeInTheDocument());

    const marks = Array.from(container.querySelectorAll("mark"));
    expect(marks.length).toBe(2);
    // Bloc 49 : surlignage [3,11) au milieu du texte.
    expect(marks[0].textContent).toBe("risque p");
    // Bloc 50 : surlignage [0,21), donc partiel en fin.
    expect(marks[1].textContent).toBe("Suite du passage cité");

    // Le texte non cité reste présent, hors du <mark>.
    expect(screen.getByText(/Contexte avant\./)).toBeInTheDocument();
  });

  it("les blocs de contexte ne portent aucun surlignage", async () => {
    apiFetchRaw.mockResolvedValue(ok(preview()));
    const { container } = render(<DocumentPreviewViewer {...base} />);
    await waitFor(() => expect(screen.getByRole("region")).toBeInTheDocument());

    const ctx = container.querySelector('[data-block-index="47"]');
    expect(ctx?.querySelector("mark")).toBeNull();
  });

  it("fait défiler jusqu'au passage cité", async () => {
    apiFetchRaw.mockResolvedValue(ok(preview()));
    const { container } = render(<DocumentPreviewViewer {...base} />);
    await waitFor(() => expect(screen.getByRole("region")).toBeInTheDocument());

    expect(container.querySelector("#docx-source-anchor")).toBeTruthy();
    // Même motif que dans PresentationPreviewViewer.test.tsx : le défilement vient
    // d'un `useEffect` distinct de celui qui fait apparaître la région, et n'est donc
    // pas garanti purgé quand le `waitFor` ci-dessus se débloque. Assertion sèche =
    // course dépendante de la charge machine.
    await waitFor(() => expect(Element.prototype.scrollIntoView).toHaveBeenCalled());
  });
});

describe("rendu des blocs", () => {
  it("un titre est annoncé avec son niveau, borné à 1..6", async () => {
    apiFetchRaw.mockResolvedValue(
      ok(
        preview({
          blocks: [
            block({ type: "heading", level: 2, text: "Risques opérationnels" }),
            block({ id: "b-2", index: 2, type: "heading", level: 99, text: "Hors plage" }),
          ],
        })
      )
    );
    render(<DocumentPreviewViewer {...base} />);
    await waitFor(() => expect(screen.getByRole("region")).toBeInTheDocument());

    expect(
      screen.getByRole("heading", { name: "Risques opérationnels", level: 2 })
    ).toBeInTheDocument();
    // 99 est normalisé à l'affichage, sans modifier la donnée.
    expect(screen.getByRole("heading", { name: "Hors plage", level: 6 })).toBeInTheDocument();
  });

  it("une liste est rendue à plat et déclarée simplifiée", async () => {
    apiFetchRaw.mockResolvedValue(
      ok(
        preview({
          blocks: [
            block({
              type: "list",
              text: "- Premier point\n- Deuxième point",
              list_fidelity: "flattened",
            }),
          ],
        })
      )
    );
    const { container } = render(<DocumentPreviewViewer {...base} />);
    await waitFor(() => expect(screen.getByRole("region")).toBeInTheDocument());

    expect(screen.getByText(/numérotation et niveaux/)).toBeInTheDocument();
    // Aucune liste sémantique n'est fabriquée : ni <ul>, ni <ol>.
    expect(container.querySelector("ul[data-block-type], ol")).toBeNull();
  });

  it("un tableau est préformaté, jamais une grille HTML", async () => {
    apiFetchRaw.mockResolvedValue(
      ok(
        preview({
          blocks: [
            block({
              type: "table",
              text: "Poste | Montant\nAlpha | 1000",
              render_mode: "preformatted",
              warnings: ["table_simplified"],
            }),
          ],
        })
      )
    );
    const { container } = render(<DocumentPreviewViewer {...base} />);
    await waitFor(() => expect(screen.getByRole("region")).toBeInTheDocument());

    expect(container.querySelector("pre")).toBeTruthy();
    expect(container.querySelector("table")).toBeNull();
    expect(screen.getByText(/fusions de cellules/)).toBeInTheDocument();
  });

  it("un bloc tronqué annonce sa longueur réelle", async () => {
    apiFetchRaw.mockResolvedValue(
      ok(
        preview({
          blocks: [block({ truncated: true, full_length: 8400, text: "début…" })],
        })
      )
    );
    render(<DocumentPreviewViewer {...base} />);
    await waitFor(() =>
      expect(screen.getByText(/8[\s  ]?400 caractères au total/)).toBeInTheDocument()
    );
  });

  it("préserve tabulations et sauts de ligne", async () => {
    apiFetchRaw.mockResolvedValue(
      ok(preview({ blocks: [block({ text: "Avant\tTab\nLigne 2" })] }))
    );
    const { container } = render(<DocumentPreviewViewer {...base} />);
    await waitFor(() => expect(screen.getByRole("region")).toBeInTheDocument());
    const p = container.querySelector('[data-block-index="49"] p');
    expect(p?.textContent).toBe("Avant\tTab\nLigne 2");
    expect(getComputedStyle(p as Element).whiteSpace).toBe("pre-wrap");
  });
});

describe("navigation", () => {
  it("affiche le sommaire et charge une fenêtre au clic", async () => {
    apiFetchRaw.mockResolvedValue(ok(preview()));
    render(<DocumentPreviewViewer {...base} />);
    await waitFor(() => expect(screen.getByRole("navigation")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "4. Risques" }));
    await waitFor(() => expect(apiFetchRaw).toHaveBeenCalledTimes(2));

    const url = String(apiFetchRaw.mock.calls[1][0]);
    expect(url).toContain("first_block=12");
    expect(url).not.toContain("chunk_id");
  });

  it("permet de revenir au passage cité", async () => {
    apiFetchRaw.mockResolvedValue(ok(preview()));
    render(<DocumentPreviewViewer {...base} />);
    await waitFor(() => expect(screen.getByRole("navigation")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "4. Risques" }));
    await waitFor(() => expect(apiFetchRaw).toHaveBeenCalledTimes(2));

    fireEvent.click(screen.getByRole("button", { name: /Revenir au passage cité/ }));
    await waitFor(() => expect(apiFetchRaw).toHaveBeenCalledTimes(3));
    expect(String(apiFetchRaw.mock.calls[2][0])).toContain("chunk_id=chunk-42");
  });

  it("sans titre détecté, l'annonce est explicite", async () => {
    apiFetchRaw.mockResolvedValue(
      ok(
        preview({
          navigation: { headings: [], headings_truncated: false },
          fidelity: {
            simplified: true,
            extractor_version: "docx-v1",
            warnings: ["headings_not_detected"],
            omitted: [],
          },
        })
      )
    );
    render(<DocumentPreviewViewer {...base} />);
    await waitFor(() =>
      expect(
        screen.getByText(/Aucun sommaire fiable n'a été détecté/)
      ).toBeInTheDocument()
    );
    expect(screen.getByText(/Aucun titre n'a été détecté/)).toBeInTheDocument();
  });

  it("un sommaire tronqué le dit", async () => {
    apiFetchRaw.mockResolvedValue(
      ok(preview({ navigation: { headings: preview().navigation.headings, headings_truncated: true } }))
    );
    render(<DocumentPreviewViewer {...base} />);
    await waitFor(() =>
      expect(screen.getByText(/Sommaire tronqué/)).toBeInTheDocument()
    );
  });
});

describe("états d'erreur — jamais de zone blanche", () => {
  it("chunk périmé : message dédié, aucun contenu affiché", async () => {
    apiFetchRaw.mockResolvedValue(fail(409, "chunk_stale"));
    render(<DocumentPreviewViewer {...base} />);
    await waitFor(() =>
      expect(
        screen.getByText(/réindexé depuis la création de cette réponse/)
      ).toBeInTheDocument()
    );
    expect(screen.queryByRole("region")).toBeNull();
    expect(
      screen.getByRole("button", { name: /Télécharger l'original/ })
    ).toBeInTheDocument();
  });

  it.each([
    ["document_version_mismatch", /changé depuis l'indexation/],
    ["preview_unavailable", /ne correspond plus à ce qui a été indexé/],
    ["invalid_chunk_offsets", /absente ou incohérente/],
    ["document_unreadable", /illisible ou endommagé/],
    ["document_too_large", /dépasse les limites/],
    ["preview_timeout", /dépassé le temps imparti/],
    ["unsupported_format", /ne prend en charge que les fichiers/],
  ])("%s produit un message explicite", async (code, pattern) => {
    apiFetchRaw.mockResolvedValue(fail(422, code));
    render(<DocumentPreviewViewer {...base} />);
    await waitFor(() => expect(screen.getByText(pattern)).toBeInTheDocument());
  });

  it("401 redirige, sans rendu", async () => {
    const onUnauthorized = vi.fn();
    apiFetchRaw.mockResolvedValue({ ok: false, status: 401, json: async () => ({}) });
    render(<DocumentPreviewViewer {...base} onUnauthorized={onUnauthorized} />);
    await waitFor(() => expect(onUnauthorized).toHaveBeenCalled());
  });

  it("erreur réseau reste explicite", async () => {
    apiFetchRaw.mockRejectedValue(new Error("Failed to fetch"));
    render(<DocumentPreviewViewer {...base} />);
    await waitFor(() =>
      expect(screen.getByText(/n'a pas pu être chargé/)).toBeInTheDocument()
    );
  });

  it("chargement annoncé", () => {
    apiFetchRaw.mockImplementation(() => new Promise(() => {}));
    render(<DocumentPreviewViewer {...base} />);
    expect(screen.getByText(/Chargement de l'aperçu/)).toBeInTheDocument();
  });

  it("document vide : état explicite, pas une erreur", async () => {
    apiFetchRaw.mockResolvedValue(
      ok(
        preview({
          blocks: [],
          source: null,
          viewport: {
            first_block: 0,
            last_block: 0,
            total_blocks: 0,
            source_first_block: null,
            source_last_block: null,
          },
        })
      )
    );
    render(<DocumentPreviewViewer {...base} />);
    await waitFor(() =>
      expect(screen.getByText(/aucun bloc textuel indexable/)).toBeInTheDocument()
    );
    expect(
      screen.getByRole("button", { name: /Télécharger l'original/ })
    ).toBeInTheDocument();
  });

  it("troncature déclarée", async () => {
    apiFetchRaw.mockResolvedValue(
      ok(preview({ truncation: { truncated: true, reason: "max_blocks" } }))
    );
    render(<DocumentPreviewViewer {...base} />);
    await waitFor(() =>
      expect(screen.getByText(/Aperçu tronqué \(max_blocks\)/)).toBeInTheDocument()
    );
  });
});

describe("sécurité du rendu", () => {
  it("le HTML d'un bloc est affiché comme TEXTE", async () => {
    apiFetchRaw.mockResolvedValue(
      ok(
        preview({
          blocks: [
            block({ text: "<script>alert(1)</script>" }),
            block({ id: "b-2", index: 2, text: "<img src=x onerror=alert(1)>" }),
          ],
        })
      )
    );
    const { container } = render(<DocumentPreviewViewer {...base} />);
    await waitFor(() => expect(screen.getByRole("region")).toBeInTheDocument());

    expect(screen.getByText("<script>alert(1)</script>")).toBeInTheDocument();
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
  });

  it("un lien javascript: reste du texte inerte", async () => {
    apiFetchRaw.mockResolvedValue(
      ok(preview({ blocks: [block({ text: "javascript:alert(1)" })] }))
    );
    const { container } = render(<DocumentPreviewViewer {...base} />);
    await waitFor(() => expect(screen.getByRole("region")).toBeInTheDocument());
    expect(screen.getByText("javascript:alert(1)")).toBeInTheDocument();
    expect(container.querySelector("a")).toBeNull();
  });

  it("le HTML reste inerte même à l'intérieur d'un surlignage", async () => {
    apiFetchRaw.mockResolvedValue(
      ok(
        preview({
          blocks: [
            block({ text: "avant <script>x</script> après", highlight: { start: 6, end: 24 } }),
          ],
        })
      )
    );
    const { container } = render(<DocumentPreviewViewer {...base} />);
    await waitFor(() => expect(screen.getByRole("region")).toBeInTheDocument());
    expect(container.querySelector("mark")?.textContent).toBe("<script>x</script>");
    expect(container.querySelector("script")).toBeNull();
  });

  it("le bandeau de fidélité est toujours affiché", async () => {
    apiFetchRaw.mockResolvedValue(ok(preview()));
    render(<DocumentPreviewViewer {...base} />);
    await waitFor(() =>
      expect(screen.getByText(/Aperçu simplifié du contenu indexé/)).toBeInTheDocument()
    );
    expect(screen.getByText(/pagination calculée ne sont pas restitués/)).toBeInTheDocument();
  });
});
