import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { __resetAnalysisPolls, pollAnalysis } from "@/app/chat/_lib/analysisPoll";
import * as api from "@/lib/api";

function job(status: string, percent = 0): any {
  return {
    analysis_id: "a-1",
    status,
    progress: { phase: "processing_batches", current: percent, total: 100, percent },
  };
}

describe("polling d'une analyse globale", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    __resetAnalysisPolls();
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("s'arrête dès qu'un statut terminal est atteint", async () => {
    const get = vi
      .spyOn(api, "getCorpusAnalysis")
      .mockResolvedValueOnce(job("running", 40))
      .mockResolvedValueOnce(job("succeeded", 100));

    const seen: string[] = [];
    pollAnalysis("a-1", (a) => seen.push(a.status), { intervalMs: 10 });

    await vi.advanceTimersByTimeAsync(10);
    await vi.advanceTimersByTimeAsync(10);
    await vi.advanceTimersByTimeAsync(100); // aucun appel supplémentaire

    expect(seen).toEqual(["running", "succeeded"]);
    expect(get).toHaveBeenCalledTimes(2);
  });

  it.each(["failed", "cancelled", "partial"])(
    "s'arrête aussi sur le statut terminal %s",
    async (status) => {
      const get = vi.spyOn(api, "getCorpusAnalysis").mockResolvedValue(job(status));
      pollAnalysis("a-1", () => {}, { intervalMs: 10 });

      await vi.advanceTimersByTimeAsync(10);
      await vi.advanceTimersByTimeAsync(100);

      expect(get).toHaveBeenCalledTimes(1);
    },
  );

  it("s'arrête au démontage", async () => {
    const get = vi.spyOn(api, "getCorpusAnalysis").mockResolvedValue(job("running"));
    const h = pollAnalysis("a-1", () => {}, { intervalMs: 10 });

    await vi.advanceTimersByTimeAsync(10);
    h.stop();
    await vi.advanceTimersByTimeAsync(200);

    expect(get).toHaveBeenCalledTimes(1);
  });

  it("n'ouvre jamais deux suivis pour le même job", async () => {
    const get = vi.spyOn(api, "getCorpusAnalysis").mockResolvedValue(job("running"));

    pollAnalysis("a-1", () => {}, { intervalMs: 10 });
    pollAnalysis("a-1", () => {}, { intervalMs: 10 }); // doublon ignoré

    await vi.advanceTimersByTimeAsync(10);

    expect(get).toHaveBeenCalledTimes(1);
  });

  it("survit à une erreur réseau et retente", async () => {
    const get = vi
      .spyOn(api, "getCorpusAnalysis")
      .mockRejectedValueOnce(new Error("network"))
      .mockResolvedValueOnce(job("succeeded"));

    const seen: string[] = [];
    pollAnalysis("a-1", (a) => seen.push(a.status), { intervalMs: 10 });

    await vi.advanceTimersByTimeAsync(10);
    await vi.advanceTimersByTimeAsync(10);

    expect(get).toHaveBeenCalledTimes(2);
    expect(seen).toEqual(["succeeded"]);
  });

  it("cesse de solliciter le serveur au-delà du plafond de durée", async () => {
    const get = vi.spyOn(api, "getCorpusAnalysis").mockResolvedValue(job("running"));
    pollAnalysis("a-1", () => {}, { intervalMs: 10, maxMs: 30 });

    await vi.advanceTimersByTimeAsync(500);

    expect(get.mock.calls.length).toBeLessThanOrEqual(3);
  });
});
