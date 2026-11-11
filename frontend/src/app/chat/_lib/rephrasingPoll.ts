// frontend/src/app/chat/_lib/rephrasingPoll.ts
//
// Polling léger des suggestions de reformulation (générées de façon ASYNCHRONE
// côté backend, F2). Pur/testable : fetcher, sleep et horloge sont injectables.
// Ne bloque jamais la saisie ; n'affiche jamais d'erreur technique (failed -> rien).

export type SuggestionsStatus = {
  status: "none" | "pending" | "ready" | "failed";
  suggestions: string[];
};

export type PollOutcome = "ready" | "failed" | "timeout" | "absent";

export async function pollRephrasingSuggestions(opts: {
  fetcher: () => Promise<SuggestionsStatus>;
  onReady: (suggestions: string[]) => void;
  intervalMs?: number;
  maxMs?: number;
  maxNoneAttempts?: number;
  sleep?: (ms: number) => Promise<void>;
  now?: () => number;
}): Promise<PollOutcome> {
  const interval = opts.intervalMs ?? 1500; // 1,5 s
  const maxMs = opts.maxMs ?? 12000; // ~12 s
  // ``none`` = aucune tâche enfilée (flag off, en pratique) OU la ligne ``pending``
  // n'est pas encore visible (course avec la fin du flux SSE). On tolère cette course
  // quelques tours, puis on abandonne : sans cette borne, un flag off ferait interroger
  // l'API pendant tout ``maxMs`` à chaque no_answer, pour rien.
  const maxNoneAttempts = opts.maxNoneAttempts ?? 2;
  const sleep = opts.sleep ?? ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)));
  const now = opts.now ?? (() => Date.now());

  const start = now();
  let noneAttempts = 0;
  while (now() - start < maxMs) {
    let res: SuggestionsStatus;
    try {
      res = await opts.fetcher();
    } catch {
      return "timeout"; // erreur réseau -> abandon silencieux, aucune erreur affichée
    }
    if (res.status === "ready") {
      const items = (res.suggestions || []).filter((s) => typeof s === "string" && s.trim().length > 0).slice(0, 3);
      opts.onReady(items);
      return "ready";
    }
    if (res.status === "failed") {
      return "failed"; // échec silencieux (pas d'erreur technique côté UI)
    }
    if (res.status === "none" && ++noneAttempts >= maxNoneAttempts) {
      return "absent";
    }
    // "none" (dans la grâce) | "pending" -> on réessaie
    await sleep(interval);
  }
  return "timeout";
}
