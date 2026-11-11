/**
 * Le résultat d'une analyse globale doit SURVIVRE — à la fin du polling, puis à
 * un rechargement de page.
 *
 * Ces tests exercent le flux de données réel de `page.tsx` (polling → état →
 * carte, puis `getThreadAnalyses` → état → carte) jusqu'au DOM. Ils ne montent
 * pas la page entière : ce qu'ils vérifient, c'est que le résultat arrive
 * jusqu'à l'utilisateur, et qu'aucun job n'est recréé au passage.
 */
import { render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AnalysisCard } from "@/app/chat/_components/AnalysisCard";
import { __resetAnalysisPolls, pollAnalysis } from "@/app/chat/_lib/analysisPoll";
import * as api from "@/lib/api";
import type { CorpusAnalysis } from "@/lib/api";

const SUMMARY = [
  "## Recherche globale — cybersécurité",
  "",
  "1. Le plan de continuité est testé chaque année. [1]",
].join("\n");

const RESULT = {
  result_version: "1",
  query: "cybersécurité",
  summary_markdown: SUMMARY,
  items: [
    {
      id: "i-1",
      statement: "Le plan de continuité est testé chaque année.",
      confidence: 0.9,
      sources: [{ chunk_id: "c-1", document_id: "d-1", page: 4, filename: "PCA.pdf" }],
    },
  ],
  search: {
    chunks_scanned: 10,
    documents_text_scanned: 2,
    candidate_chunks: 3,
    candidate_documents: 1,
    processed_chunks: 3,
    processed_documents: 1,
    batches_total: 1,
    batches_failed: 0,
  },
};

const COVERAGE = {
  documents_considered: 2,
  documents_analyzed: 2,
  documents_excluded: [],
  coverage_ratio: 1,
  coverage_level: "B",
  coverage_complete: true,
  limits_applied: [],
};

function job(status: string, withResult: boolean): CorpusAnalysis {
  return {
    analysis_id: "a-1",
    rag_space_id: "s-1",
    analysis_type: "corpus.global_search",
    parameters: {},
    status,
    progress: { phase: status === "running" ? "processing_batches" : "completed", current: 100, total: 100, percent: 100 },
    preflight: {},
    coverage: withResult ? COVERAGE : null,
    result: withResult ? RESULT : null,
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
  } as unknown as CorpusAnalysis;
}

describe("survie du résultat d'une analyse globale", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    __resetAnalysisPolls();
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it.each([
    ["succeeded", /Analyse globale terminée/],
    ["partial", /Résultat partiel/],
  ])(
    "propage jusqu'à la carte le résultat reçu en fin de polling (%s)",
    async (status, expected) => {
      const get = vi
        .spyOn(api, "getCorpusAnalysis")
        .mockResolvedValueOnce(job("running", false))
        .mockResolvedValueOnce(job(status, true));

      // Réplique de `upsertAnalysis` : le dernier job reçu fait foi.
      let latest: CorpusAnalysis | null = null;
      pollAnalysis("a-1", (a) => (latest = a), { intervalMs: 10 });

      await vi.advanceTimersByTimeAsync(10);
      await vi.advanceTimersByTimeAsync(10);
      await vi.advanceTimersByTimeAsync(200); // le polling doit être arrêté

      // Le polling s'arrête, et n'écrase pas le résultat par un payload plus pauvre.
      expect(get).toHaveBeenCalledTimes(2);
      expect(latest!.status).toBe(status);
      expect((latest!.result as any).summary_markdown).toContain("Recherche globale");

      vi.useRealTimers();
      render(<AnalysisCard analysis={latest!} />);

      expect(screen.getByText(expected)).toBeInTheDocument();
      expect(
        within(screen.getByTestId("analysis-result")).getByText(/plan de continuité/),
      ).toBeInTheDocument();
      expect(within(screen.getByTestId("analysis-sources")).getByText(/PCA\.pdf/)).toBeInTheDocument();
    },
  );

  it("après F5, relit le job du fil et réaffiche le MÊME résultat, sans recréer de job", async () => {
    // Ce que fait page.tsx au montage d'un fil : GET des analyses du fil.
    const list = vi
      .spyOn(api, "getThreadAnalyses")
      .mockResolvedValue({ analyses: [job("succeeded", true)] });
    const create = vi.spyOn(api, "createCorpusAnalysis");
    const get = vi.spyOn(api, "getCorpusAnalysis");

    const { analyses } = await api.getThreadAnalyses("t-1");
    const restored = analyses[0];

    // `trackAnalysis` n'ouvre AUCUN poller sur un job déjà terminal.
    if (!api.isTerminalAnalysisStatus(restored.status)) pollAnalysis(restored.analysis_id, () => {});
    await vi.advanceTimersByTimeAsync(200);

    vi.useRealTimers();
    render(<AnalysisCard analysis={restored} />);

    // Le résultat est bien de retour dans le fil…
    expect(
      within(screen.getByTestId("analysis-result")).getByText(/plan de continuité/),
    ).toBeInTheDocument();
    expect(screen.getByTestId("analysis-sources")).toBeInTheDocument();
    expect(screen.getByTestId("analysis-coverage")).toBeInTheDocument();

    // …sans qu'aucun job n'ait été relancé, ni aucun polling rouvert.
    expect(list).toHaveBeenCalledWith("t-1");
    expect(create).not.toHaveBeenCalled();
    expect(get).not.toHaveBeenCalled();
  });

  it("n'affiche qu'une seule carte, et un seul résultat, pour un job repris", async () => {
    vi.spyOn(api, "getThreadAnalyses").mockResolvedValue({ analyses: [job("succeeded", true)] });

    const { analyses } = await api.getThreadAnalyses("t-1");
    vi.useRealTimers();

    const { rerender } = render(<AnalysisCard analysis={analyses[0]} />);
    rerender(<AnalysisCard analysis={analyses[0]} />);

    expect(screen.getAllByTestId("analysis-card")).toHaveLength(1);
    expect(screen.getAllByTestId("analysis-result")).toHaveLength(1);
  });
});
