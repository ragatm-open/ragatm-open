import { describe, it, expect, vi } from "vitest";
import { pollRephrasingSuggestions, type SuggestionsStatus } from "@/app/chat/_lib/rephrasingPoll";

const noSleep = () => Promise.resolve();

function seqFetcher(seq: SuggestionsStatus[]) {
  let i = 0;
  return () => Promise.resolve(seq[Math.min(i++, seq.length - 1)]);
}

describe("pollRephrasingSuggestions", () => {
  it("pending -> ready : appelle onReady et retourne 'ready'", async () => {
    const onReady = vi.fn();
    const out = await pollRephrasingSuggestions({
      fetcher: seqFetcher([
        { status: "pending", suggestions: [] },
        { status: "pending", suggestions: [] },
        { status: "ready", suggestions: ["A ?", "B ?"] },
      ]),
      onReady,
      sleep: noSleep,
    });
    expect(out).toBe("ready");
    expect(onReady).toHaveBeenCalledWith(["A ?", "B ?"]);
  });

  it("failed : n'appelle pas onReady, retourne 'failed' (pas d'erreur UI)", async () => {
    const onReady = vi.fn();
    const out = await pollRephrasingSuggestions({
      fetcher: seqFetcher([{ status: "failed", suggestions: [] }]),
      onReady,
      sleep: noSleep,
    });
    expect(out).toBe("failed");
    expect(onReady).not.toHaveBeenCalled();
  });

  it("'none' en boucle (flag off) -> abandon rapide, sans épuiser maxMs", async () => {
    const fetcher = vi.fn(() => Promise.resolve<SuggestionsStatus>({ status: "none", suggestions: [] }));
    const out = await pollRephrasingSuggestions({ fetcher, onReady: vi.fn(), sleep: noSleep });
    expect(out).toBe("absent");
    expect(fetcher).toHaveBeenCalledTimes(2); // borne des 'none', pas 8 tours jusqu'à maxMs
  });

  it("'none' transitoire (course avec la fin du flux) puis ready", async () => {
    const onReady = vi.fn();
    const out = await pollRephrasingSuggestions({
      fetcher: seqFetcher([
        { status: "none", suggestions: [] },
        { status: "pending", suggestions: [] },
        { status: "ready", suggestions: ["A ?"] },
      ]),
      onReady,
      sleep: noSleep,
    });
    expect(out).toBe("ready");
    expect(onReady).toHaveBeenCalledWith(["A ?"]);
  });

  it("timeout : 'pending' en boucle -> arrêt borné par maxMs", async () => {
    let t = 0;
    const out = await pollRephrasingSuggestions({
      fetcher: seqFetcher([{ status: "pending", suggestions: [] }]),
      onReady: vi.fn(),
      sleep: noSleep,
      maxMs: 5000,
      intervalMs: 1500,
      now: () => (t += 1500), // horloge simulée : dépasse maxMs en quelques tours
    });
    expect(out).toBe("timeout");
  });

  it("erreur réseau -> 'timeout' silencieux", async () => {
    const out = await pollRephrasingSuggestions({
      fetcher: () => Promise.reject(new Error("network")),
      onReady: vi.fn(),
      sleep: noSleep,
    });
    expect(out).toBe("timeout");
  });

  it("borne dure à 3 suggestions", async () => {
    const onReady = vi.fn();
    await pollRephrasingSuggestions({
      fetcher: seqFetcher([{ status: "ready", suggestions: ["1", "2", "3", "4", "5"] }]),
      onReady,
      sleep: noSleep,
    });
    expect(onReady).toHaveBeenCalledWith(["1", "2", "3"]);
  });
});
