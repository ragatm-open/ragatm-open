/**
 * La couture backend ↔ carte, sur un payload NON inventé.
 *
 * `fixtures/globalSearchResult.json` est la sortie EXACTE du pipeline réel
 * (`apps.rag_core.analysis.global_search`) : FTS Postgres réel, consolidation
 * réelle, `render_summary_markdown` réel, `attach_document_names` réel. Une
 * fixture écrite à la main ne prouverait rien — c'est précisément une fixture
 * complaisante (`summary_markdown: "…"`) qui a laissé le bug passer.
 *
 * Si le backend change la forme de son résultat, ce test tombe.
 */
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AnalysisCard } from "@/app/chat/_components/AnalysisCard";
import type { CorpusAnalysis } from "@/lib/api";

import payload from "../fixtures/globalSearchResult.json";

function fromBackend(over: Partial<CorpusAnalysis> = {}): CorpusAnalysis {
  return {
    analysis_id: "a-1",
    rag_space_id: "s-1",
    analysis_type: "corpus.global_search",
    parameters: {},
    progress: { phase: "completed", current: 100, total: 100, percent: 100 },
    preflight: {},
    error_code: null,
    error_message: null,
    corpus_fingerprint: "sha256:x",
    is_stale: false,
    created_at: null,
    started_at: null,
    finished_at: null,
    expires_at: null,
    tokens_in: 0,
    tokens_out: 0,
    cost_usd: null,
    ...payload,
    ...over,
  } as unknown as CorpusAnalysis;
}

describe("carte d'analyse, alimentée par le payload réel du backend", () => {
  it("affiche le résultat que le backend a réellement produit", () => {
    render(<AnalysisCard analysis={fromBackend()} />);

    const body = within(screen.getByTestId("analysis-result"));
    expect(body.getByRole("heading", { name: /Recherche globale/ })).toBeInTheDocument();
    expect(body.getByText(/La cybersécurité fait l’objet d’un audit/)).toBeInTheDocument();
    // L'énoncé numéroté du backend devient une VRAIE liste.
    expect(body.getAllByRole("listitem").length).toBeGreaterThanOrEqual(1);
  });

  it("affiche le nom réel du document cité, jamais son UUID", () => {
    render(<AnalysisCard analysis={fromBackend()} />);

    const sources = within(screen.getByTestId("analysis-sources"));
    expect(sources.getByText(/d0\.txt/)).toBeInTheDocument();
    expect(sources.getByText(/page 1/)).toBeInTheDocument();
    // L'identifiant technique ne fuit jamais dans l'UI.
    expect(screen.queryByText(/342286b6-1797-4288-b4c9-1db04e31ac29/)).not.toBeInTheDocument();
  });

  it("affiche la couverture réelle, exclusions comprises", () => {
    render(<AnalysisCard analysis={fromBackend()} />);

    const scope = within(screen.getByTestId("analysis-coverage"));
    expect(scope.getByText(/6 documents considérés/)).toBeInTheDocument();
    expect(scope.getByText(/5 documents analysés/)).toBeInTheDocument();
    expect(scope.getByText(/1 non analysés : échec d’extraction/)).toBeInTheDocument();
    expect(scope.getByText(/Couverture : 83 %/)).toBeInTheDocument();
  });

  it("garde le rappel du backend sur les limites de la recherche lexicale", () => {
    render(<AnalysisCard analysis={fromBackend()} />);

    // Porté par summary_markdown, et affiché UNE seule fois.
    expect(
      screen.getAllByText(/peut ne pas reconnaître toutes les formulations/),
    ).toHaveLength(1);
    expect(screen.queryByText(/exhaustif/i)).not.toBeInTheDocument();
  });
});
