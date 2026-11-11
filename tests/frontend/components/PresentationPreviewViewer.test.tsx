import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";

import { PresentationPreviewViewer } from "@/app/doc/[documentId]/_components/PresentationPreviewViewer";

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

function blk(over: Record<string, unknown> = {}) {
  return {
    id: "s4-b1",
    index: 1,
    type: "paragraph",
    text: "Le backend communique avec la passerelle.",
    highlight: null,
    truncated: false,
    full_length: 41,
    warnings: [],
    ...over,
  };
}

function preview(over: Record<string, unknown> = {}) {
  return {
    schema_version: 1,
    document: {
      id: "doc-1",
      filename: "architecture.pptx",
      sha256: "abc",
      preview_kind: "presentation",
    },
    source: {
      chunk_id: "chunk-42",
      kind: "pptx_slide",
      slide_number: 4,
      char_start: 1842,
      char_end: 2094,
      notes_included: false,
    },
    versions: { extractor_version: "1", chunking_version: "1" },
    presentation: {
      slide_count: 6,
      indexed_slide_count: 6,
      slides: [
        { number: 3, title: "Contexte", has_citation: false, has_images: false },
        { number: 4, title: "Architecture cible", has_citation: true, has_images: true },
        { number: 5, title: null, has_citation: false, has_images: false },
      ],
      inventory_truncated: false,
    },
    viewport: { active_slide: 4, source_slide: 4, first_slide: 3, last_slide: 5 },
    slides: [
      {
        number: 4,
        title: "Architecture cible",
        has_images: true,
        requires_visual_check: false,
        blocks: [
          blk({ id: "s4-b0", index: 0, type: "title", text: "Titre : Architecture cible" }),
          blk({ id: "s4-b1", index: 1, highlight: { start: 3, end: 10 } }),
        ],
        speaker_notes: [],
        warnings: [],
      },
    ],
    fidelity: {
      simplified: true,
      extractor_version: "1",
      chunking_version: "1",
      warnings: [
        "layout_not_rendered",
        "images_not_rendered",
        "charts_not_extracted",
        "smartart_not_extracted",
        "hidden_slides_not_detected",
        "list_numbering_lost",
        "tables_simplified",
        "animations_not_rendered",
        "transitions_not_rendered",
      ],
      omitted: ["images", "charts", "smartart", "animations"],
    },
    truncation: { truncated: false, reason: null },
    ...over,
  };
}

const base = {
  documentId: "doc-1",
  filename: "architecture.pptx",
  chunkId: "chunk-42",
  onUnauthorized: vi.fn(),
  onDownload: vi.fn(),
  onBack: vi.fn(),
};

beforeEach(() => {
  vi.clearAllMocks();
  Element.prototype.scrollIntoView = vi.fn();
});

describe("ouverture sur la diapositive citée", () => {
  it("transmet le chunk_id, jamais le snippet", async () => {
    apiFetchRaw.mockResolvedValue(ok(preview()));
    render(<PresentationPreviewViewer {...base} />);
    await waitFor(() => expect(screen.getByRole("navigation")).toBeInTheDocument());

    const url = String(apiFetchRaw.mock.calls[0][0]);
    expect(url).toContain("/presentation-preview/");
    expect(url).toContain("chunk_id=chunk-42");
    expect(url).not.toContain("slide_number");
    expect(url).not.toContain("snippet");
  });

  it("surligne au caractère, pas le bloc entier", async () => {
    apiFetchRaw.mockResolvedValue(ok(preview()));
    const { container } = render(<PresentationPreviewViewer {...base} />);
    await waitFor(() => expect(screen.getByRole("navigation")).toBeInTheDocument());

    const marks = Array.from(container.querySelectorAll("mark"));
    expect(marks.length).toBe(1);
    expect(marks[0].textContent).toBe("backend");
    // Le reste du bloc est présent, hors du <mark>.
    expect(screen.getByText(/communique avec la passerelle/)).toBeInTheDocument();
  });

  it("signale la diapositive source et fait défiler jusqu'à elle", async () => {
    apiFetchRaw.mockResolvedValue(ok(preview()));
    const { container } = render(<PresentationPreviewViewer {...base} />);
    await waitFor(() => expect(screen.getByRole("navigation")).toBeInTheDocument());

    expect(screen.getByText("source citée")).toBeInTheDocument();
    expect(container.querySelector("#pptx-source-anchor")).toBeTruthy();
    // `waitFor` et non une assertion sèche : le défilement vient d'un SECOND
    // `useEffect` (`[status, preview]`), distinct de celui qui fait apparaître la
    // navigation. `waitFor` ci-dessus se débloque dès la mutation DOM — observable
    // en microtâche — alors que React 18 purge les effets passifs dans un macrotask
    // ordonnancé à part. La fenêtre est invisible sur une machine rapide et s'ouvre
    // sous charge : mesuré, un simple tick suffisait à ce que l'appel survienne.
    await waitFor(() => expect(Element.prototype.scrollIntoView).toHaveBeenCalled());
  });

  it("les blocs sans citation ne portent pas de surlignage", async () => {
    apiFetchRaw.mockResolvedValue(ok(preview()));
    const { container } = render(<PresentationPreviewViewer {...base} />);
    await waitFor(() => expect(screen.getByRole("navigation")).toBeInTheDocument());
    const title = container.querySelector('[data-block-index="0"]');
    expect(title?.querySelector("mark")).toBeNull();
  });
});

describe("navigation", () => {
  it("liste les diapositives avec numéro et titre", async () => {
    apiFetchRaw.mockResolvedValue(ok(preview()));
    render(<PresentationPreviewViewer {...base} />);
    await waitFor(() => expect(screen.getByRole("navigation")).toBeInTheDocument());

    expect(
      screen.getByRole("button", { name: /Architecture cible.*source citée/ })
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Contexte" })).toBeInTheDocument();
    // Une diapositive sans titre porte son numéro, jamais un titre inventé.
    expect(screen.getByRole("button", { name: "Diapositive 5" })).toBeInTheDocument();
  });

  it("cliquer une diapositive recharge SANS chunk_id", async () => {
    apiFetchRaw.mockResolvedValue(ok(preview()));
    render(<PresentationPreviewViewer {...base} />);
    await waitFor(() => expect(screen.getByRole("navigation")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Contexte" }));
    await waitFor(() => expect(apiFetchRaw).toHaveBeenCalledTimes(2));

    const url = String(apiFetchRaw.mock.calls[1][0]);
    expect(url).toContain("slide_number=3");
    expect(url).not.toContain("chunk_id");
  });

  it("précédent / suivant naviguent", async () => {
    apiFetchRaw.mockResolvedValue(ok(preview()));
    render(<PresentationPreviewViewer {...base} />);
    await waitFor(() => expect(screen.getByRole("navigation")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: /Suivante/ }));
    await waitFor(() => expect(apiFetchRaw).toHaveBeenCalledTimes(2));
    expect(String(apiFetchRaw.mock.calls[1][0])).toContain("slide_number=5");
  });

  it("revenir à la source réinjecte le chunk_id", async () => {
    apiFetchRaw.mockResolvedValue(ok(preview()));
    render(<PresentationPreviewViewer {...base} />);
    await waitFor(() => expect(screen.getByRole("navigation")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Contexte" }));
    await waitFor(() => expect(apiFetchRaw).toHaveBeenCalledTimes(2));

    fireEvent.click(screen.getByRole("button", { name: /Revenir à la source/ }));
    await waitFor(() => expect(apiFetchRaw).toHaveBeenCalledTimes(3));
    expect(String(apiFetchRaw.mock.calls[2][0])).toContain("chunk_id=chunk-42");
  });

  it("inventaire tronqué annoncé", async () => {
    apiFetchRaw.mockResolvedValue(
      ok(
        preview({
          presentation: { ...preview().presentation, inventory_truncated: true },
        })
      )
    );
    render(<PresentationPreviewViewer {...base} />);
    await waitFor(() =>
      expect(screen.getByText(/Inventaire tronqué/)).toBeInTheDocument()
    );
  });
});

describe("rendu des blocs", () => {
  it("un tableau est préformaté, jamais une grille", async () => {
    apiFetchRaw.mockResolvedValue(
      ok(
        preview({
          slides: [
            {
              ...preview().slides[0],
              blocks: [
                blk({
                  type: "table",
                  text: "Tableau :\n| Service | Port |\n|---|---|\n| GW | 8100 |",
                  render_mode: "preformatted",
                  warnings: ["table_simplified", "merged_cells_may_repeat"],
                }),
              ],
            },
          ],
        })
      )
    );
    const { container } = render(<PresentationPreviewViewer {...base} />);
    await waitFor(() => expect(container.querySelector("pre")).toBeTruthy());
    expect(container.querySelector("table")).toBeNull();
    expect(
      screen.getByText(/cellules fusionnées peuvent apparaître répétées/)
    ).toBeInTheDocument();
  });

  it("une liste est à plat et déclarée simplifiée", async () => {
    apiFetchRaw.mockResolvedValue(
      ok(
        preview({
          slides: [
            {
              ...preview().slides[0],
              blocks: [
                blk({
                  type: "list",
                  text: "- Frontend\n  - Next.js",
                  list_fidelity: "flattened",
                }),
              ],
            },
          ],
        })
      )
    );
    const { container } = render(<PresentationPreviewViewer {...base} />);
    await waitFor(() =>
      expect(screen.getByText(/numérotation et niveaux non restitués/)).toBeInTheDocument()
    );
    expect(container.querySelector("ul[data-block-type], ol")).toBeNull();
  });

  it("un bloc tronqué annonce sa longueur réelle", async () => {
    apiFetchRaw.mockResolvedValue(
      ok(
        preview({
          slides: [
            {
              ...preview().slides[0],
              blocks: [blk({ truncated: true, full_length: 5400, text: "début…" })],
            },
          ],
        })
      )
    );
    render(<PresentationPreviewViewer {...base} />);
    await waitFor(() =>
      expect(screen.getByText(/5[\s  ]?400 caractères au total/)).toBeInTheDocument()
    );
  });

  it("diapositive sans texte : message explicite, pas un vide", async () => {
    apiFetchRaw.mockResolvedValue(
      ok(
        preview({
          slides: [
            {
              number: 4, title: null, blocks: [], speaker_notes: [],
              warnings: ["slide_has_no_indexable_text"],
            },
          ],
        })
      )
    );
    render(<PresentationPreviewViewer {...base} />);
    await waitFor(() =>
      expect(screen.getByText(/aucun texte indexable/)).toBeInTheDocument()
    );
  });
});

describe("notes et contenu visuel", () => {
  it("les notes sont affichées à part, jamais mêlées au corps", async () => {
    apiFetchRaw.mockResolvedValue(
      ok(
        preview({
          slides: [
            {
              ...preview().slides[0],
              speaker_notes: [
                blk({
                  id: "s4-n0",
                  index: 0,
                  type: "speaker_notes",
                  text: "Insister sur le loopback",
                }),
              ],
            },
          ],
        })
      )
    );
    const { container } = render(<PresentationPreviewViewer {...base} />);
    await waitFor(() =>
      expect(screen.getByText("Notes du présentateur")).toBeInTheDocument()
    );
    expect(screen.getByText("contenu indexé séparément")).toBeInTheDocument();
    expect(
      container.querySelector('[data-block-type="speaker_notes"]')
    ).toBeTruthy();
  });

  it("aucune zone de notes quand il n'y en a pas", async () => {
    apiFetchRaw.mockResolvedValue(ok(preview()));
    render(<PresentationPreviewViewer {...base} />);
    await waitFor(() => expect(screen.getByRole("navigation")).toBeInTheDocument());
    expect(screen.queryByText("Notes du présentateur")).toBeNull();
  });

  it("la présence d'images est signalée, sans placeholder", async () => {
    apiFetchRaw.mockResolvedValue(ok(preview()));
    const { container } = render(<PresentationPreviewViewer {...base} />);
    await waitFor(() =>
      expect(
        screen.getByText(/éléments visuels non affichés/)
      ).toBeInTheDocument()
    );
    // Aucune image n'est créée depuis le contenu.
    expect(container.querySelector("img")).toBeNull();
  });

  it("aucune mention visuelle quand l'information est inconnue", async () => {
    const p = preview();
    apiFetchRaw.mockResolvedValue(
      ok({
        ...p,
        slides: [{ number: 4, title: "T", blocks: [blk()], speaker_notes: [], warnings: [] }],
        fidelity: { ...p.fidelity, warnings: p.fidelity.warnings },
      })
    );
    render(<PresentationPreviewViewer {...base} />);
    await waitFor(() => expect(screen.getByRole("navigation")).toBeInTheDocument());
    // `has_images` absent = INCONNU : on n'affirme rien.
    expect(screen.queryByText(/éléments visuels non affichés/)).toBeNull();
  });
});

describe("états d'erreur — jamais de zone blanche", () => {
  it("réglage des notes changé : message dédié", async () => {
    apiFetchRaw.mockResolvedValue(fail(409, "notes_configuration_mismatch"));
    render(<PresentationPreviewViewer {...base} />);
    await waitFor(() =>
      expect(
        screen.getByText(/traitement des notes du présentateur a changé/)
      ).toBeInTheDocument()
    );
    expect(screen.queryByRole("navigation")).toBeNull();
    expect(
      screen.getByRole("button", { name: /Télécharger l'original/ })
    ).toBeInTheDocument();
  });

  it.each([
    ["chunk_stale", /réindexée depuis la création/],
    ["document_version_mismatch", /changé depuis l'indexation/],
    ["encrypted_presentation", /protégée par un chiffrement/],
    ["presentation_unreadable", /illisible ou endommagée/],
    ["presentation_too_large", /dépasse les limites/],
    ["slide_not_found", /n'existe pas dans la présentation/],
    ["preview_timeout", /dépassé le temps imparti/],
    ["invalid_chunk_offsets", /absente ou incohérente/],
  ])("%s produit un message explicite", async (code, pattern) => {
    apiFetchRaw.mockResolvedValue(fail(422, code));
    render(<PresentationPreviewViewer {...base} />);
    await waitFor(() => expect(screen.getByText(pattern)).toBeInTheDocument());
  });

  it("401 redirige, sans rendu", async () => {
    const onUnauthorized = vi.fn();
    apiFetchRaw.mockResolvedValue({ ok: false, status: 401, json: async () => ({}) });
    render(<PresentationPreviewViewer {...base} onUnauthorized={onUnauthorized} />);
    await waitFor(() => expect(onUnauthorized).toHaveBeenCalled());
  });

  it("erreur réseau reste explicite", async () => {
    apiFetchRaw.mockRejectedValue(new Error("Failed to fetch"));
    render(<PresentationPreviewViewer {...base} />);
    await waitFor(() =>
      expect(screen.getByText(/n'a pas pu être chargé/)).toBeInTheDocument()
    );
  });

  it("chargement annoncé", () => {
    apiFetchRaw.mockImplementation(() => new Promise(() => {}));
    render(<PresentationPreviewViewer {...base} />);
    expect(screen.getByText(/Chargement de l'aperçu/)).toBeInTheDocument();
  });

  it("troncature déclarée", async () => {
    apiFetchRaw.mockResolvedValue(
      ok(preview({ truncation: { truncated: true, reason: "max_slides" } }))
    );
    render(<PresentationPreviewViewer {...base} />);
    await waitFor(() =>
      expect(screen.getByText(/Aperçu tronqué \(max_slides\)/)).toBeInTheDocument()
    );
  });
});

describe("sécurité du rendu", () => {
  it("le HTML d'un bloc est affiché comme TEXTE", async () => {
    apiFetchRaw.mockResolvedValue(
      ok(
        preview({
          slides: [
            {
              ...preview().slides[0],
              blocks: [
                blk({ text: "<script>alert(1)</script>" }),
                blk({ id: "s4-b2", index: 2, text: "<img src=x onerror=alert(1)>" }),
              ],
            },
          ],
        })
      )
    );
    const { container } = render(<PresentationPreviewViewer {...base} />);
    await waitFor(() =>
      expect(screen.getByText("<script>alert(1)</script>")).toBeInTheDocument()
    );
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
  });

  it("un lien javascript: reste inerte", async () => {
    apiFetchRaw.mockResolvedValue(
      ok(
        preview({
          slides: [
            { ...preview().slides[0], blocks: [blk({ text: "javascript:alert(1)" })] },
          ],
        })
      )
    );
    const { container } = render(<PresentationPreviewViewer {...base} />);
    await waitFor(() =>
      expect(screen.getByText("javascript:alert(1)")).toBeInTheDocument()
    );
    expect(container.querySelector("a")).toBeNull();
  });

  it("le HTML reste inerte à l'intérieur d'un surlignage", async () => {
    apiFetchRaw.mockResolvedValue(
      ok(
        preview({
          slides: [
            {
              ...preview().slides[0],
              blocks: [
                blk({
                  text: "avant <script>x</script> après",
                  highlight: { start: 6, end: 24 },
                }),
              ],
            },
          ],
        })
      )
    );
    const { container } = render(<PresentationPreviewViewer {...base} />);
    await waitFor(() => expect(container.querySelector("mark")).toBeTruthy());
    expect(container.querySelector("mark")?.textContent).toBe("<script>x</script>");
    expect(container.querySelector("script")).toBeNull();
  });

  it("le bandeau de fidélité déclare les pertes structurelles", async () => {
    apiFetchRaw.mockResolvedValue(ok(preview()));
    render(<PresentationPreviewViewer {...base} />);
    await waitFor(() =>
      expect(
        screen.getByText(/Aperçu textuel simplifié du contenu indexé/)
      ).toBeInTheDocument()
    );
    expect(screen.getByText(/graphiques et des SmartArt n'est pas extrait/)).toBeInTheDocument();
    expect(screen.getByText(/diapositive masquée » n'est pas détecté/)).toBeInTheDocument();
  });
});

describe("accessibilité", () => {
  it("chaque diapositive est une région nommée", async () => {
    apiFetchRaw.mockResolvedValue(ok(preview()));
    render(<PresentationPreviewViewer {...base} />);
    await waitFor(() =>
      expect(
        screen.getByRole("region", { name: /Diapositive 4 — Architecture cible/ })
      ).toBeInTheDocument()
    );
  });

  it("la navigation est labellisée", async () => {
    apiFetchRaw.mockResolvedValue(ok(preview()));
    render(<PresentationPreviewViewer {...base} />);
    await waitFor(() =>
      expect(
        screen.getByRole("navigation", { name: "Diapositives de la présentation" })
      ).toBeInTheDocument()
    );
  });
});
