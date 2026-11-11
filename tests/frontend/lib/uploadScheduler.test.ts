/**
 * Ordonnanceur d'envois : cadence, pause coordonnée sur 429, ré-essais bornés,
 * annulation.
 *
 * Ce que ces tests encodent est un incident de production, mesuré deux fois le
 * 23/08/2026 sur le même compte :
 *
 *     17:17   110 acceptés en 11 s, puis 135 × HTTP 429
 *     18:06    86 acceptés en  8 s, puis 158 × HTTP 429
 *
 * Le défaut n'était pas la concurrence — le chemin glisser-déposer, à concurrence 1,
 * y était tout aussi exposé. C'était l'absence de toute notion de TEMPS dans la file.
 *
 * Les tests utilisent des faux timers et un faux serveur : ils mesurent des
 * horodatages et des compteurs d'appels, jamais des durées réelles. Un test qui
 * dormirait vraiment mesurerait la machine, pas le code.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  MAX_UPLOAD_ATTEMPTS,
  RATE_LIMIT_EXHAUSTED,
  UPLOAD_MIN_INTERVAL_MS,
  UploadAborted,
  __resetUploadScheduler,
  clampRetryDelay,
  fallbackRetryDelay,
  runUploadQueue,
  sleepAbortable,
  uploadSchedulerState,
  type UploadAttemptResult,
  type UploadOutcome,
} from "@/app/documents/_lib/uploadScheduler";

type Item = { id: string };

const items = (n: number): Item[] => Array.from({ length: n }, (_, i) => ({ id: `f${i}` }));

/** Journal de ce que l'ordonnanceur a RÉELLEMENT fait, horodaté. */
function recorder() {
  const starts: { id: string; at: number; attempt: number }[] = [];
  const settled: { id: string; outcome: UploadOutcome; attempts: number; reason?: string }[] = [];
  const waits: { id: string; waitMs: number }[] = [];
  return {
    starts,
    settled,
    waits,
    hooks: {
      onAttemptStart: (it: Item, attempt: number) => starts.push({ id: it.id, at: Date.now(), attempt }),
      onWaitingRetry: (it: Item, waitMs: number) => waits.push({ id: it.id, waitMs }),
      onSettled: (it: Item, outcome: UploadOutcome, info: any) =>
        settled.push({ id: it.id, outcome, attempts: info.attempts, reason: info.reason }),
    },
  };
}

/** Fait avancer les timers ET laisse tourner les microtâches, en alternance. */
async function tick(ms: number) {
  await vi.advanceTimersByTimeAsync(ms);
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-08-23T12:00:00Z"));
  __resetUploadScheduler();
});

afterEach(() => {
  vi.useRealTimers();
  __resetUploadScheduler();
});

// --- Primitives -------------------------------------------------------------

describe("sleepAbortable", () => {
  it("efface le timer à l'annulation — aucun timer fantôme", async () => {
    const ac = new AbortController();
    const p = sleepAbortable(10_000, ac.signal);
    expect(vi.getTimerCount()).toBe(1);
    ac.abort();
    await expect(p).rejects.toBeInstanceOf(UploadAborted);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("rejette immédiatement si le signal est déjà déclenché", async () => {
    const ac = new AbortController();
    ac.abort();
    await expect(sleepAbortable(1000, ac.signal)).rejects.toBeInstanceOf(UploadAborted);
    expect(vi.getTimerCount()).toBe(0);
  });
});

describe("clampRetryDelay", () => {
  it.each([
    [500, 1_000],
    [21_000, 21_000],
    [999_999, 120_000],
    [-5_000, 1_000],
    [Number.NaN, 1_000],
  ])("écrête %p en %p", (entree, attendu) => {
    expect(clampRetryDelay(entree as number)).toBe(attendu);
  });
});

describe("fallbackRetryDelay", () => {
  it("croît exponentiellement et reste borné", () => {
    const zero = () => 0;
    expect(fallbackRetryDelay(1, zero)).toBe(2_000);
    expect(fallbackRetryDelay(2, zero)).toBe(4_000);
    expect(fallbackRetryDelay(3, zero)).toBe(8_000);
    expect(fallbackRetryDelay(40, zero)).toBe(120_000);
  });

  it("ajoute un désordre borné (repli seulement)", () => {
    expect(fallbackRetryDelay(1, () => 1)).toBe(2_400);
  });
});

// --- Cadence ----------------------------------------------------------------

/** Plafond serveur observé en production (DRF `user: 120/min`).
 *
 * Écrit ici comme une CONSTANTE DE TEST indépendante, et non importée du code :
 * un test qui se compare à la valeur qu'il vérifie ne vérifie rien. Le premier jet
 * de ce fichier bornait les départs par `60_000 / UPLOAD_MIN_INTERVAL_MS` — passer
 * l'intervalle à 0 rendait la borne infinie et le test restait vert. */
const SERVER_LIMIT_PER_MIN = 120;

describe("cadence", () => {
  it("la constante de cadence laisse une marge réelle sous le plafond serveur", () => {
    // Garde de POLITIQUE, en valeurs absolues : c'est elle qui tombe si quelqu'un
    // annule la cadence ou la porte au ras du plafond.
    const parMinute = 60_000 / UPLOAD_MIN_INTERVAL_MS;
    expect(parMinute).toBeLessThanOrEqual(SERVER_LIMIT_PER_MIN * 0.75);
    expect(parMinute).toBeGreaterThanOrEqual(30); // ni si lent qu'un import devienne inutilisable
  });

  it("200 petits fichiers très rapides ne partent PAS en rafale", async () => {
    // Le scénario dangereux. Sans cadence, 200 envois de 10 ms partent en ~2 s.
    const rec = recorder();
    const run = runUploadQueue<Item>(
      items(200),
      async () => {
        await new Promise((r) => setTimeout(r, 10));
        return { kind: "success" };
      },
      { concurrency: 4, hooks: rec.hooks },
    );
    await tick(10 * 60_000);
    await run;
    expect(rec.settled.filter((s) => s.outcome === "done")).toHaveLength(200);

    // Fenêtre GLISSANTE d'une minute, bornée par le plafond SERVEUR — une valeur
    // absolue, que le code testé ne peut pas déplacer.
    const at = rec.starts.map((s) => s.at).sort((a, b) => a - b);
    let pire = 0;
    for (let i = 0; i < at.length; i++) {
      let j = i;
      while (j < at.length && at[j] - at[i] < 60_000) j++;
      pire = Math.max(pire, j - i);
    }
    expect(pire).toBeLessThan(SERVER_LIMIT_PER_MIN);
  });

  it("espace les démarrages d'au moins l'intervalle", async () => {
    const rec = recorder();
    const run = runUploadQueue<Item>(items(5), async () => ({ kind: "success" }), {
      concurrency: 4,
      hooks: rec.hooks,
    });
    await tick(20 * 60_000);
    await run;
    expect(rec.starts).toHaveLength(5);
    for (let i = 1; i < rec.starts.length; i++) {
      // Borne absolue : 500 ms est un minimum plancher indépendant du réglage.
      expect(rec.starts[i].at - rec.starts[i - 1].at).toBeGreaterThanOrEqual(500);
      expect(rec.starts[i].at - rec.starts[i - 1].at).toBeGreaterThanOrEqual(UPLOAD_MIN_INTERVAL_MS);
    }
  });

  it("un envoi plus long que l'intervalle ne subit AUCUN délai supplémentaire", async () => {
    // Gros fichier / réseau lent : l'intervalle est déjà écoulé quand l'envoi finit.
    const duree = UPLOAD_MIN_INTERVAL_MS * 4;
    const rec = recorder();
    const run = runUploadQueue<Item>(
      items(3),
      async () => {
        await new Promise((r) => setTimeout(r, duree));
        return { kind: "success" };
      },
      { concurrency: 1, hooks: rec.hooks },
    );
    await tick(duree * 4);
    await run;
    // Chaque démarrage suit immédiatement la fin du précédent : l'écart vaut la
    // DURÉE de l'envoi, pas durée + intervalle.
    for (let i = 1; i < rec.starts.length; i++) {
      expect(rec.starts[i].at - rec.starts[i - 1].at).toBe(duree);
    }
  });

  it("respecte la concurrence maximale", async () => {
    let inFlight = 0;
    let max = 0;
    const run = runUploadQueue<Item>(
      items(12),
      async () => {
        inFlight++;
        max = Math.max(max, inFlight);
        await new Promise((r) => setTimeout(r, 5_000));
        inFlight--;
        return { kind: "success" };
      },
      { concurrency: 3 },
    );
    await tick(120_000);
    await run;
    expect(max).toBe(3);
  });
});

// --- L'incident -------------------------------------------------------------

/** Faux serveur : accepte `quota` requêtes par fenêtre, puis répond 429. */
function fauxServeur(quota: number, fenetreMs: number, retryAfterMs: number) {
  let debut = Date.now();
  let compte = 0;
  const posts: number[] = [];
  return {
    posts,
    async handle(): Promise<UploadAttemptResult> {
      posts.push(Date.now());
      if (Date.now() - debut >= fenetreMs) {
        debut = Date.now();
        compte = 0;
      }
      compte += 1;
      if (compte > quota) return { kind: "rate_limited", retryAfterMs };
      return { kind: "success" };
    },
  };
}

describe("incident 429 reproduit", () => {
  it("un import qui sature le quota finit ENTIÈREMENT, sans échec dû au throttle", async () => {
    const srv = fauxServeur(20, 60_000, 5_000);
    const rec = recorder();
    const run = runUploadQueue<Item>(items(50), () => srv.handle(), {
      concurrency: 4,
      hooks: rec.hooks,
    });
    await tick(30 * 60_000);
    await run;

    expect(rec.settled.filter((s) => s.outcome === "done")).toHaveLength(50);
    expect(rec.settled.filter((s) => s.outcome === "error")).toHaveLength(0);
  });

  it("anti-avalanche : après le premier 429, presque plus rien ne part", async () => {
    // Avant correctif, 135 puis 158 POST partaient après le premier refus.
    let refuseApres = 5;
    let vus = 0;
    let premier429At: number | null = null;
    const departs: number[] = [];
    const CONCURRENCE = 4;

    const run = runUploadQueue<Item>(
      items(60),
      async () => {
        departs.push(Date.now());
        vus += 1;
        await new Promise((r) => setTimeout(r, 20));
        if (vus > refuseApres) {
          if (premier429At === null) premier429At = Date.now();
          return { kind: "rate_limited", retryAfterMs: 30_000 };
        }
        return { kind: "success" };
      },
      { concurrency: CONCURRENCE },
    );
    await tick(25_000);

    expect(premier429At).not.toBeNull();
    const apres = departs.filter((t) => t > (premier429At as number));
    // Bornés aux requêtes DÉJÀ engagées quand la pause s'est ouverte.
    expect(apres.length).toBeLessThanOrEqual(CONCURRENCE - 1);

    refuseApres = Number.POSITIVE_INFINITY;
    await tick(20 * 60_000);
    await run;
  });
});

// --- Pause globale ----------------------------------------------------------

describe("pause globale", () => {
  it("aucun nouveau départ avant l'échéance annoncée par Retry-After", async () => {
    // 30 s, très loin du repli exponentiel (2 s au premier essai) : sans cela le test
    // reste vert même si le code ignore l'en-tête et applique son repli — les deux
    // durées coïncidaient dans le premier jet.
    const RETRY = 30_000;
    let premier = true;
    const departs: number[] = [];
    let refusAt = 0;

    const run = runUploadQueue<Item>(
      items(6),
      async () => {
        departs.push(Date.now());
        if (premier) {
          premier = false;
          refusAt = Date.now();
          return { kind: "rate_limited", retryAfterMs: RETRY };
        }
        return { kind: "success" };
      },
      { concurrency: 1 },
    );
    await tick(200);
    const pendant = departs.filter((t) => t > refusAt && t < refusAt + RETRY);
    expect(pendant).toHaveLength(0);

    await tick(60_000);
    await run;
    const apres = departs.filter((t) => t > refusAt);
    expect(apres.length).toBeGreaterThan(0);
    const reprise = Math.min(...apres) - refusAt;
    // Encadré des DEUX côtés : c'est bien le délai du SERVEUR qui est appliqué, ni
    // un repli plus court, ni une attente arbitrairement plus longue.
    expect(reprise).toBeGreaterThanOrEqual(RETRY);
    expect(reprise).toBeLessThan(RETRY + 5_000);
  });

  it("la pause est MONOTONE : un délai plus court ne la raccourcit jamais", async () => {
    // A refuse avec 5 s ; B, déjà en vol, refuse ensuite avec 2 s.
    const delais = [5_000, 2_000];
    let i = 0;
    const run = runUploadQueue<Item>(
      items(2),
      async () => {
        const d = delais[Math.min(i++, delais.length - 1)];
        await new Promise((r) => setTimeout(r, 10));
        return { kind: "rate_limited", retryAfterMs: d };
      },
      { concurrency: 2 },
    );
    await tick(50);
    const { pauseUntil } = uploadSchedulerState();
    expect(pauseUntil - Date.now()).toBeGreaterThan(4_000);

    await tick(10 * 60_000);
    await run;
  });

  it("reprend SEULE après expiration, sans action utilisateur", async () => {
    let refuse = true;
    const rec = recorder();
    const run = runUploadQueue<Item>(
      items(3),
      async () => (refuse ? { kind: "rate_limited", retryAfterMs: 3_000 } : { kind: "success" }),
      { concurrency: 1, hooks: rec.hooks },
    );
    await tick(100);
    expect(rec.waits.length).toBeGreaterThan(0);
    expect(rec.settled).toHaveLength(0);

    refuse = false;
    await tick(60_000);
    await run;
    expect(rec.settled.filter((s) => s.outcome === "done")).toHaveLength(3);
  });

  it("sans Retry-After exploitable, applique le repli", async () => {
    for (const valeur of [undefined, null]) {
      __resetUploadScheduler();
      let premier = true;
      const run = runUploadQueue<Item>(
        items(2),
        async () => {
          if (premier) {
            premier = false;
            return { kind: "rate_limited", retryAfterMs: valeur as any };
          }
          return { kind: "success" };
        },
        { concurrency: 1, random: () => 0 },
      );
      await tick(50);
      const { pauseUntil } = uploadSchedulerState();
      expect(pauseUntil - Date.now()).toBeGreaterThanOrEqual(1_500);
      await tick(10 * 60_000);
      await run;
    }
  });

  it("allonge la cadence une fois après un 429, et ne la réduit pas ensuite", async () => {
    let premier = true;
    const run = runUploadQueue<Item>(
      items(4),
      async () => {
        if (premier) {
          premier = false;
          return { kind: "rate_limited", retryAfterMs: 1_000 };
        }
        return { kind: "success" };
      },
      { concurrency: 1 },
    );
    await tick(50);
    expect(uploadSchedulerState().intervalMs).toBe(UPLOAD_MIN_INTERVAL_MS * 2);
    await tick(60_000);
    // Toujours doublé pendant le lot.
    await run;
    // Lot terminé : l'onglet retrouve sa cadence nominale.
    expect(uploadSchedulerState().intervalMs).toBe(UPLOAD_MIN_INTERVAL_MS);
  });
});

// --- Sorts des éléments -----------------------------------------------------

describe("sorts", () => {
  it("un succès n'est JAMAIS rejoué, même si d'autres déclenchent des pauses", async () => {
    const rec = recorder();
    let n = 0;
    const run = runUploadQueue<Item>(
      items(5),
      async () => {
        n += 1;
        // Le 2e et le 3e envoi sont refusés à répétition ; les autres passent.
        if (n === 2 || n === 3) return { kind: "rate_limited", retryAfterMs: 1_000 };
        return { kind: "success" };
      },
      { concurrency: 1, hooks: rec.hooks },
    );
    await tick(10 * 60_000);
    await run;

    for (const s of rec.settled.filter((x) => x.outcome === "done")) {
      const essais = rec.starts.filter((x) => x.id === s.id).length;
      expect(essais).toBe(s.attempts);
    }
    // Aucun élément arrivé à `done` n'a été redémarré ensuite.
    const doneIds = rec.settled.filter((s) => s.outcome === "done").map((s) => s.id);
    for (const id of doneIds) {
      const dernierDepart = Math.max(...rec.starts.filter((s) => s.id === id).map((s) => s.at));
      const apres = rec.starts.filter((s) => s.id === id && s.at > dernierDepart);
      expect(apres).toHaveLength(0);
    }
  });

  it("un premier envoi accepté n'a qu'UNE tentative", async () => {
    const rec = recorder();
    const run = runUploadQueue<Item>(
      items(3),
      async (it) => (it.id === "f1" ? { kind: "rate_limited", retryAfterMs: 1_000 } : { kind: "success" }),
      { concurrency: 1, hooks: rec.hooks },
    );
    await tick(10 * 60_000);
    await run;
    const f0 = rec.settled.find((s) => s.id === "f0")!;
    expect(f0.outcome).toBe("done");
    expect(f0.attempts).toBe(1);
  });

  it("épuise ses ré-essais puis échoue avec un motif DISTINCT d'une erreur de fichier", async () => {
    const rec = recorder();
    const run = runUploadQueue<Item>(items(1), async () => ({ kind: "rate_limited", retryAfterMs: 1_000 }), {
      concurrency: 1,
      hooks: rec.hooks,
    });
    await tick(10 * 60_000);
    await run;
    expect(rec.settled).toHaveLength(1);
    expect(rec.settled[0].outcome).toBe("error");
    expect(rec.settled[0].reason).toBe(RATE_LIMIT_EXHAUSTED);
    expect(rec.settled[0].attempts).toBe(MAX_UPLOAD_ATTEMPTS);
    expect(rec.starts.filter((s) => s.id === "f0")).toHaveLength(MAX_UPLOAD_ATTEMPTS);
  });

  it("lot de 1 000 : un échec isolé, des 429, aucun succès rejoué, aucun fichier perdu", async () => {
    // Taille maximale d'un import de dossier (MAX_FOLDER_FILES). Un fichier est
    // refusé définitivement, le serveur throttle par fenêtres : les 999 autres
    // aboutissent, chacun reçoit exactement un sort, et rien n'est envoyé deux fois.
    const srv = fauxServeur(120, 60_000, 5_000);
    const rec = recorder();
    const run = runUploadQueue<Item>(
      items(1000),
      async (it) => (it.id === "f500" ? { kind: "terminal_error", error: new Error("400") } : srv.handle()),
      { concurrency: 4, hooks: rec.hooks },
    );
    await tick(120 * 60_000);
    await run;

    expect(rec.settled).toHaveLength(1000);
    expect(new Set(rec.settled.map((s) => s.id)).size).toBe(1000);
    expect(rec.settled.filter((s) => s.outcome === "done")).toHaveLength(999);
    const errors = rec.settled.filter((s) => s.outcome === "error");
    expect(errors.map((s) => s.id)).toEqual(["f500"]);
    expect(rec.starts.filter((s) => s.id === "f500")).toHaveLength(1);
    // Un succès : autant de départs que de tentatives déclarées, et une seule réussie.
    for (const s of rec.settled.filter((x) => x.outcome === "done")) {
      expect(rec.starts.filter((x) => x.id === s.id)).toHaveLength(s.attempts);
    }
  });

  it("une erreur terminale n'est pas rejouée, et n'empêche pas les autres", async () => {
    const rec = recorder();
    let n = 0;
    const run = runUploadQueue<Item>(
      items(4),
      async () => {
        n += 1;
        if (n === 2) return { kind: "rate_limited", retryAfterMs: 1_000 };
        if (n === 3) return { kind: "terminal_error", error: new Error("400") };
        return { kind: "success" };
      },
      { concurrency: 1, hooks: rec.hooks },
    );
    await tick(10 * 60_000);
    await run;
    expect(rec.settled.filter((s) => s.outcome === "done")).toHaveLength(3);
    expect(rec.settled.filter((s) => s.outcome === "error")).toHaveLength(1);
    const enErreur = rec.settled.find((s) => s.outcome === "error")!;
    expect(rec.starts.filter((s) => s.id === enErreur.id)).toHaveLength(1);
  });
});

// --- Annulation -------------------------------------------------------------

describe("annulation", () => {
  it("aucun départ après l'annulation, aucun timer survivant", async () => {
    const ac = new AbortController();
    let premier = true;
    const departs: number[] = [];
    const run = runUploadQueue<Item>(
      items(20),
      async () => {
        departs.push(Date.now());
        if (premier) {
          premier = false;
          return { kind: "rate_limited", retryAfterMs: 60_000 };
        }
        return { kind: "success" };
      },
      { concurrency: 1, signal: ac.signal },
    );
    await tick(100);
    const avant = departs.length;

    ac.abort();
    await tick(10 * 60_000);
    await run;

    expect(departs.length).toBe(avant);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("annuler pendant l'attente de cadence rend `cancelled`, pas `error`", async () => {
    const ac = new AbortController();
    const rec = recorder();
    const run = runUploadQueue<Item>(items(10), async () => ({ kind: "success" }), {
      concurrency: 1,
      signal: ac.signal,
      hooks: rec.hooks,
    });
    await tick(UPLOAD_MIN_INTERVAL_MS + 10);
    ac.abort();
    await tick(60_000);
    await run;

    const annules = rec.settled.filter((s) => s.outcome === "cancelled");
    expect(annules.length).toBeGreaterThan(0);
    expect(rec.settled.filter((s) => s.outcome === "error")).toHaveLength(0);
    // Chaque élément a bien reçu un sort : un lot interrompu reste connu.
    expect(rec.settled).toHaveLength(10);
  });
});

// --- Partage entre imports --------------------------------------------------

describe("deux imports dans le même onglet", () => {
  it("partagent UNE cadence au lieu de la doubler", async () => {
    const recA = recorder();
    const recB = recorder();
    const a = runUploadQueue<Item>(items(5), async () => ({ kind: "success" }), {
      concurrency: 2,
      hooks: recA.hooks,
    });
    const b = runUploadQueue<Item>(items(5), async () => ({ kind: "success" }), {
      concurrency: 2,
      hooks: recB.hooks,
    });
    await tick(20 * UPLOAD_MIN_INTERVAL_MS);
    await Promise.all([a, b]);

    const tous = [...recA.starts, ...recB.starts].map((s) => s.at).sort((x, y) => x - y);
    expect(tous).toHaveLength(10);
    for (let i = 1; i < tous.length; i++) {
      expect(tous[i] - tous[i - 1]).toBeGreaterThanOrEqual(500);
      expect(tous[i] - tous[i - 1]).toBeGreaterThanOrEqual(UPLOAD_MIN_INTERVAL_MS);
    }
  });
});
