import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { AnalysisCard } from "@/app/chat/_components/AnalysisCard";
import type { CorpusAnalysis } from "@/lib/api";

function analysis(over: Partial<CorpusAnalysis> = {}): CorpusAnalysis {
  return {
    analysis_id: "a-1",
    rag_space_id: "s-1",
    analysis_type: "corpus.global_search",
    parameters: {},
    status: "running",
    progress: { phase: "processing_batches", current: 42, total: 100, percent: 42 },
    preflight: {} as any,
    coverage: null,
    result: null,
    error_code: null,
    error_message: null,
    corpus_fingerprint: "sha256:x",
    is_stale: null,
    created_at: null,
    started_at: null,
    finished_at: null,
    expires_at: null,
    tokens_in: 0,
    tokens_out: 0,
    cost_usd: null,
    ...over,
  } as CorpusAnalysis;
}

const coverage = {
  documents_considered: 216,
  documents_analyzed: 203,
  documents_excluded: [{ reason: "extraction_failed", count: 13, label: "échec d’extraction" }],
  coverage_ratio: 0.9398,
  coverage_level: "B",
  coverage_complete: false,
  limits_applied: [],
} as any;

const partialCoverage = { ...coverage, limits_applied: ["failed_batches"] } as any;

/**
 * Fidèle à `render_summary_markdown` (backend). L'ancienne fixture valait « … » :
 * elle ne pouvait donc PAS révéler que la carte n'affichait pas le résultat.
 */
const summaryMarkdown = [
  "## Recherche globale — cybersécurité",
  "",
  "1. Le plan de continuité est testé chaque année. [1] [2]",
  "2. Les accès distants exigent une authentification forte. [1]",
  "",
  "| Mesure | Statut |",
  "| --- | --- |",
  "| Chiffrement | Actif |",
  "",
  "La recherche lexicale peut ne pas reconnaître toutes les formulations",
  "équivalentes du sujet.",
].join("\n");

const search = {
  chunks_scanned: 9478,
  documents_text_scanned: 203,
  candidate_chunks: 84,
  candidate_documents: 58,
  processed_chunks: 84,
  processed_documents: 58,
  batches_total: 5,
  batches_failed: 0,
};

const result = {
  result_version: "1",
  query: "cybersécurité",
  summary_markdown: summaryMarkdown,
  items: [
    {
      id: "i-1",
      statement: "Le plan de continuité est testé chaque année.",
      confidence: 0.9,
      sources: [
        { chunk_id: "c-1", document_id: "d-1", page: 4, filename: "PCA.pdf" },
        { chunk_id: "c-2", document_id: "d-2", page: 12, filename: "Politique.pdf" },
      ],
    },
    {
      id: "i-2",
      statement: "Les accès distants exigent une authentification forte.",
      confidence: 0.8,
      // Même document que l'item 1, autre page : ne doit PAS créer une 2e entrée.
      sources: [{ chunk_id: "c-3", document_id: "d-1", page: 7, filename: "PCA.pdf" }],
    },
  ],
  search,
} as any;

describe("AnalysisCard", () => {
  it("affiche la progression avec aria-valuenow", () => {
    render(<AnalysisCard analysis={analysis()} />);
    const bar = screen.getByRole("progressbar");
    expect(bar).toHaveAttribute("aria-valuenow", "42");
    expect(screen.getByText(/Analyse globale en cours/)).toBeInTheDocument();
    expect(screen.getByText(/Traitement des extraits/)).toBeInTheDocument();
  });

  it("permet l'annulation, et désactive le bouton après clic", async () => {
    const onCancel = vi.fn();
    const { rerender } = render(<AnalysisCard analysis={analysis()} onCancel={onCancel} />);

    const btn = screen.getByRole("button", { name: /annuler/i });
    await userEvent.click(btn);
    expect(onCancel).toHaveBeenCalledWith("a-1");

    rerender(<AnalysisCard analysis={analysis()} onCancel={onCancel} cancelling />);
    expect(screen.getByRole("button", { name: /annuler/i })).toBeDisabled();
  });

  it("affiche le périmètre réel et les exclusions une fois terminée", () => {
    render(<AnalysisCard analysis={analysis({ status: "succeeded", coverage, result })} />);
    const scope = within(screen.getByTestId("analysis-coverage"));
    expect(scope.getByText(/216 documents considérés/)).toBeInTheDocument();
    expect(scope.getByText(/203 documents analysés/)).toBeInTheDocument();
    expect(scope.getByText(/13 non analysés : échec d’extraction/)).toBeInTheDocument();
    expect(scope.getByText(/9 478 extraits parcourus/)).toBeInTheDocument();
    expect(scope.getByText(/84 extraits candidats dans 58 documents/)).toBeInTheDocument();
    expect(scope.getByText(/Couverture : 94 %/)).toBeInTheDocument();
  });

  it("ne promet jamais une exhaustivité sémantique", () => {
    render(<AnalysisCard analysis={analysis({ status: "succeeded", coverage, result })} />);
    expect(
      screen.getByText(/peut ne pas reconnaître toutes les formulations/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/exhaustif/i)).not.toBeInTheDocument();
  });

  it("n'affiche ni progression ni annulation sur un état terminal", () => {
    render(<AnalysisCard analysis={analysis({ status: "cancelled" })} onCancel={vi.fn()} />);
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /annuler/i })).not.toBeInTheDocument();
    expect(screen.getByText(/L’analyse a été annulée/)).toBeInTheDocument();
  });

  it("affiche un message sûr en cas d'échec (jamais une trace)", () => {
    render(
      <AnalysisCard
        analysis={analysis({
          status: "failed",
          error_code: "candidate_limit_exceeded",
          error_message: "La recherche a trouvé trop de correspondances.",
        })}
      />,
    );
    expect(screen.getByText(/trop de correspondances/)).toBeInTheDocument();
    expect(screen.queryByText(/Traceback/i)).not.toBeInTheDocument();
  });

  it("distingue l'analyse partielle d'une analyse terminée", () => {
    render(<AnalysisCard analysis={analysis({ status: "partial", coverage, result })} />);
    expect(screen.getByText(/Analyse partielle/)).toBeInTheDocument();
  });

  /* ------------------------------------------------------------------ *
   * Lot 1 — le résultat lui-même. C'est ce qui manquait.
   * ------------------------------------------------------------------ */

  it("rend summary_markdown quand l'analyse est terminée", () => {
    render(<AnalysisCard analysis={analysis({ status: "succeeded", coverage, result })} />);

    const body = within(screen.getByTestId("analysis-result"));
    expect(body.getByRole("heading", { name: /Recherche globale — cybersécurité/ })).toBeInTheDocument();
    expect(body.getByText(/Le plan de continuité est testé chaque année/)).toBeInTheDocument();
  });

  it("rend le Markdown avec le renderer du chat : listes et tableaux deviennent du vrai DOM", () => {
    render(<AnalysisCard analysis={analysis({ status: "succeeded", coverage, result })} />);

    const body = within(screen.getByTestId("analysis-result"));
    // La liste numérotée du backend est une VRAIE liste, pas du texte.
    expect(body.getAllByRole("listitem").length).toBeGreaterThanOrEqual(2);
    // Le tableau GFM est une VRAIE table (remark-gfm), via AssistantMarkdown.
    expect(body.getByRole("table")).toBeInTheDocument();
    expect(body.getByRole("columnheader", { name: "Mesure" })).toBeInTheDocument();
    expect(body.getByRole("cell", { name: "Chiffrement" })).toBeInTheDocument();
  });

  it("affiche les documents cités, une seule fois chacun, avec leurs pages", () => {
    render(<AnalysisCard analysis={analysis({ status: "succeeded", coverage, result })} />);

    const sources = within(screen.getByTestId("analysis-sources"));
    // d-1 est cité par les deux items : une seule entrée, deux pages.
    expect(sources.getAllByRole("listitem")).toHaveLength(2);
    expect(sources.getByText(/PCA\.pdf/)).toBeInTheDocument();
    expect(sources.getByText(/pages 4, 7/)).toBeInTheDocument();
    expect(sources.getByText(/Politique\.pdf/)).toBeInTheDocument();
    expect(sources.getByText(/page 12/)).toBeInTheDocument();
  });

  it("affiche le résultat partiel, avec un avertissement et sa raison", () => {
    render(
      <AnalysisCard analysis={analysis({ status: "partial", coverage: partialCoverage, result })} />,
    );

    expect(screen.getByRole("status")).toHaveTextContent(/Résultat partiel/);
    expect(screen.getByRole("status")).toHaveTextContent(/lots d’extraits n’ont pas pu être traités/);
    // Le résultat disponible n'est PAS masqué sous prétexte de partialité.
    expect(screen.getByTestId("analysis-result")).toBeInTheDocument();
    expect(screen.getByTestId("analysis-sources")).toBeInTheDocument();
    expect(
      within(screen.getByTestId("analysis-coverage")).getByText(/13 non analysés/),
    ).toBeInTheDocument();
  });

  it("ne dévoile aucun résultat tant que l'analyse n'est pas terminée", () => {
    for (const status of ["pending", "running"] as const) {
      const { unmount } = render(
        // Un reliquat de `result` ne doit jamais s'afficher pendant l'exécution.
        <AnalysisCard analysis={analysis({ status, coverage, result })} />,
      );
      expect(screen.queryByTestId("analysis-result")).not.toBeInTheDocument();
      expect(screen.queryByTestId("analysis-sources")).not.toBeInTheDocument();
      unmount();
    }
  });

  it("n'invente aucun résultat sur un job annulé ou échoué", () => {
    for (const status of ["cancelled", "failed"] as const) {
      const { unmount } = render(
        <AnalysisCard analysis={analysis({ status, coverage, result })} />,
      );
      expect(screen.queryByTestId("analysis-result")).not.toBeInTheDocument();
      expect(screen.queryByTestId("analysis-sources")).not.toBeInTheDocument();
      unmount();
    }
  });

  it("gère proprement un résultat vide, sans section fantôme", () => {
    const blank = { ...result, summary_markdown: "   \n  ", items: [] };
    render(<AnalysisCard analysis={analysis({ status: "succeeded", coverage, result: blank })} />);

    expect(screen.queryByTestId("analysis-result")).not.toBeInTheDocument();
    expect(screen.queryByTestId("analysis-sources")).not.toBeInTheDocument();
    expect(screen.getByTestId("analysis-result-empty")).toHaveTextContent(
      /aucun résultat textuel exploitable/,
    );
    // La couverture, elle, reste due à l'utilisateur.
    expect(screen.getByTestId("analysis-coverage")).toBeInTheDocument();
  });

  it("tolère un résultat ancien, sans filename sur les sources", () => {
    const legacy = {
      ...result,
      items: [
        {
          id: "i-1",
          statement: "Énoncé.",
          confidence: null,
          sources: [{ chunk_id: "c-1", document_id: "d-9", page: null }],
        },
      ],
    };
    render(<AnalysisCard analysis={analysis({ status: "succeeded", coverage, result: legacy })} />);

    const sources = within(screen.getByTestId("analysis-sources"));
    expect(sources.getAllByRole("listitem")).toHaveLength(1);
    expect(sources.getByText(/Document introuvable/)).toBeInTheDocument();
  });

  it("hérite de la sanitisation d'AssistantMarkdown (aucun HTML brut, aucun lien dangereux)", () => {
    const hostile = {
      ...result,
      summary_markdown: [
        "<img src=x onerror=alert(1)>",
        "",
        "[cliquer](javascript:alert(1))",
      ].join("\n"),
      items: [],
    };
    const { container } = render(
      <AnalysisCard analysis={analysis({ status: "succeeded", coverage, result: hostile })} />,
    );

    // L'HTML brut n'est jamais interprété (pas de rehype-raw).
    expect(container.querySelector("img")).toBeNull();
    // Le lien `javascript:` est neutralisé : jamais d'ancre cliquable.
    expect(container.querySelector('a[href^="javascript:"]')).toBeNull();
  });

  it("n'affiche le résultat qu'une fois, même après un rerender du même job", () => {
    const a = analysis({ status: "succeeded", coverage, result });
    const { rerender } = render(<AnalysisCard analysis={a} />);
    rerender(<AnalysisCard analysis={a} />);

    expect(screen.getAllByTestId("analysis-result")).toHaveLength(1);
    expect(screen.getAllByTestId("analysis-sources")).toHaveLength(1);
    expect(
      screen.getAllByRole("heading", { name: /Recherche globale — cybersécurité/ }),
    ).toHaveLength(1);
  });
});
