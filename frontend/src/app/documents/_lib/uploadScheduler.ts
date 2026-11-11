// frontend/src/app/documents/_lib/uploadScheduler.ts
//
// Ordonnanceur d'envois : concurrence bornée, CADENCE bornée, pause coordonnée sur
// HTTP 429, ré-essais bornés, annulation sûre.
//
// ## Pourquoi ce module remplace `runPool`
//
// `runPool` ne connaissait que la CONCURRENCE. Or la concurrence borne le
// parallélisme, jamais le débit : quatre envois simultanés de 3 Ko partent à dix par
// seconde, soit 600/min contre un plafond serveur de 120/min. Scénario de référence (données
// synthétiques), deux fois dans la même heure :
//
//     17:17   110 acceptés en 11 s, puis 135 × HTTP 429
//     18:06    86 acceptés en  8 s, puis 158 × HTTP 429
//
// Et le chemin glisser-déposer, pourtant à concurrence 1, y est tout aussi exposé.
// Réduire 4 → 1 n'aurait donc rien corrigé.
//
// ## Les trois invariants
//
// 1. **Un client rapide ne transforme pas sa vitesse locale en rafale serveur** :
//    un intervalle minimal sépare deux DÉMARRAGES.
// 2. **Un 429 suspend le lot, il ne condamne pas le fichier** : la pause est
//    partagée, le fichier revient en file.
// 3. **Un envoi déjà accepté n'est jamais rejoué** parce qu'un autre a été ralenti :
//    `done` est un état terminal, sans transition sortante.
//
// ## Ce que ce module ne fait PAS
//
// Aucune coordination entre onglets (pas de `BroadcastChannel`, pas d'élection de
// leader) : un second onglet consommant le même quota provoquera des 429, que la
// pause globale traite correctement. Le défaut devient une lenteur, plus une
// avalanche d'échecs. Le serveur reste l'arbitre final.

/**
 * Intervalle minimal entre deux DÉMARRAGES d'envoi.
 *
 * ≈ 80 démarrages/minute, soit **66 % du plafond serveur actuel de 120/min**.
 *
 * Cette valeur est une MARGE CLIENT CONSERVATRICE, jamais la source de vérité du
 * quota : le serveur seul connaît l'état de son seau glissant. La marge existe parce
 * que l'import n'est pas seul à consommer le quota — la page émet aussi ses propres
 * requêtes pendant l'envoi (liste, statistiques, quota : trois ont été throttlées à
 * côté des uploads lors de l'incident du 23/08), et un second onglet doit rester
 * possible. Viser 119/min ne laisserait aucune marge.
 *
 * C'est un intervalle entre DÉMARRAGES, et non un délai ajouté après chaque envoi :
 * un envoi de 4 s a déjà consommé l'intervalle quand il se termine, donc un gros
 * fichier ou un réseau lent ne subissent AUCUNE pénalité supplémentaire.
 */
export const UPLOAD_MIN_INTERVAL_MS = 750;

/** Nombre total de tentatives par fichier, première comprise.
 *
 * Quatre, parce que le délai vient du serveur : chaque ré-essai attend exactement ce
 * que `Retry-After` demande, et une fenêtre DRF dure 60 s. Quatre tentatives couvrent
 * donc plusieurs fenêtres complètes. Au-delà, ce n'est plus une saturation passagère
 * mais un quota durablement consommé ailleurs — et le dire à l'utilisateur vaut mieux
 * que de le faire attendre indéfiniment. */
export const MAX_UPLOAD_ATTEMPTS = 4;

/** Bornes appliquées à un délai serveur. Un `Retry-After` aberrant ne doit ni faire
 *  repartir immédiatement, ni geler l'import. */
export const RETRY_DELAY_MIN_MS = 1_000;
export const RETRY_DELAY_MAX_MS = 120_000;

/** Repli quand le serveur ne fournit pas de délai exploitable : exponentiel borné. */
export const FALLBACK_BASE_MS = 2_000;

/** Motif d'échec distinguant un quota épuisé d'un fichier défectueux. */
export const RATE_LIMIT_EXHAUSTED = "rate_limit_exhausted" as const;

/** Verdict d'UNE tentative d'envoi, rendu par l'appelant à l'ordonnanceur.
 *
 * Un type discriminé plutôt que des chaînes libres : l'ordonnanceur doit distinguer
 * quatre situations aux conséquences opposées, et une faute de frappe sur une chaîne
 * ferait silencieusement retomber un 429 dans la branche « erreur terminale ». */
export type UploadAttemptResult =
  | { kind: "success" }
  | { kind: "rate_limited"; retryAfterMs?: number | null }
  | { kind: "terminal_error"; error?: unknown }
  | { kind: "cancelled" };

/** Sort définitif d'un élément. */
export type UploadOutcome = "done" | "error" | "cancelled";

export type UploadSettledInfo = {
  /** Nombre de tentatives réellement émises pour cet élément. */
  attempts: number;
  error?: unknown;
  /** Renseigné quand l'échec vient du plafond de ré-essais, pas du fichier. */
  reason?: typeof RATE_LIMIT_EXHAUSTED;
};

export type UploadSchedulerHooks<T> = {
  /** Une tentative va être émise (1 pour la première). */
  onAttemptStart?: (item: T, attempt: number) => void;
  /** L'élément attend avant un nouvel essai. `waitMs` est le délai retenu. */
  onWaitingRetry?: (item: T, waitMs: number, attempt: number) => void;
  /** L'élément a atteint un état terminal. */
  onSettled?: (item: T, outcome: UploadOutcome, info: UploadSettledInfo) => void;
};

// ---------------------------------------------------------------------------
// État PARTAGÉ par l'onglet
// ---------------------------------------------------------------------------
// Au niveau du module, et non de l'appel : deux imports lancés dans la même page
// (un dossier, puis un glisser-déposer) doivent se partager UNE cadence. Chacun la
// sienne, ils additionneraient leur débit et reproduiraient l'incident à deux.

/** Instant (epoch ms) avant lequel aucun nouvel envoi ne doit démarrer. */
let pauseUntil = 0;
/** Instant (epoch ms) du prochain créneau de démarrage disponible. */
let nextSlot = 0;
/** Intervalle courant. Peut être allongé par un 429 ; jamais raccourci en cours de lot. */
let intervalMs = UPLOAD_MIN_INTERVAL_MS;
/** Cadence de référence à laquelle l'onglet revient une fois inactif. */
let baselineIntervalMs = UPLOAD_MIN_INTERVAL_MS;
/** Lots en cours. Le retour à zéro rend sa cadence nominale à un onglet inactif. */
let activeBatches = 0;

/**
 * Remise à zéro complète — RÉSERVÉE AUX TESTS.
 *
 * `intervalOverride` sert aux suites dont le sujet n'est PAS la cadence (filtrage de
 * formats, verrous Office, ré-essai manuel) : à 750 ms, un cas de 200 fichiers
 * durerait deux minutes et demie en temps réel. La cadence elle-même est vérifiée
 * dans `uploadScheduler.test.ts`, contre des bornes absolues dérivées du plafond
 * SERVEUR — jamais contre cette valeur, qu'un test pourrait justement neutraliser.
 */
export function __resetUploadScheduler(intervalOverride?: number): void {
  pauseUntil = 0;
  nextSlot = 0;
  baselineIntervalMs = intervalOverride ?? UPLOAD_MIN_INTERVAL_MS;
  intervalMs = baselineIntervalMs;
  activeBatches = 0;
}

/** Lecture de l'état partagé — diagnostic et tests. */
export function uploadSchedulerState(): {
  pauseUntil: number;
  nextSlot: number;
  intervalMs: number;
  activeBatches: number;
} {
  return { pauseUntil, nextSlot, intervalMs, activeBatches };
}

// ---------------------------------------------------------------------------
// Primitives
// ---------------------------------------------------------------------------

/** Erreur levée par `sleepAbortable` quand le signal se déclenche pendant l'attente. */
export class UploadAborted extends Error {
  constructor() {
    super("upload_aborted");
    this.name = "UploadAborted";
  }
}

/**
 * Attente interruptible. `clearTimeout` est appelé dans TOUS les cas de sortie.
 *
 * Un `setTimeout` nu est le piège de ce lot : sans effacement, un ré-essai se
 * déclenche des dizaines de secondes après l'annulation, sur un composant démonté.
 */
export function sleepAbortable(ms: number, signal?: AbortSignal): Promise<void> {
  if (ms <= 0) return signal?.aborted ? Promise.reject(new UploadAborted()) : Promise.resolve();
  return new Promise<void>((resolve, reject) => {
    if (signal?.aborted) {
      reject(new UploadAborted());
      return;
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    function onAbort() {
      clearTimeout(timer);
      reject(new UploadAborted());
    }
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

/** Écrête un délai serveur dans des bornes exploitables. */
export function clampRetryDelay(ms: number): number {
  if (!Number.isFinite(ms)) return RETRY_DELAY_MIN_MS;
  return Math.min(RETRY_DELAY_MAX_MS, Math.max(RETRY_DELAY_MIN_MS, Math.round(ms)));
}

/**
 * Repli quand le serveur ne dit rien : exponentiel borné, avec un léger désordre.
 *
 * Le désordre n'a de sens QU'ICI. Sur un `Retry-After` valide il serait nuisible : le
 * serveur a donné une échéance, la décaler au hasard, c'est soit repartir trop tôt,
 * soit perdre du temps — et rendre les tests non déterministes. En repli, en revanche,
 * plusieurs clients privés d'échéance choisiraient exactement le même délai.
 */
export function fallbackRetryDelay(attempt: number, random: () => number = Math.random): number {
  const base = FALLBACK_BASE_MS * Math.pow(2, Math.max(0, attempt - 1));
  const jitter = base * 0.2 * random();
  return clampRetryDelay(base + jitter);
}

// ---------------------------------------------------------------------------
// Ordonnanceur
// ---------------------------------------------------------------------------

type Entry<T> = { item: T; index: number; attempts: number };

export type RunUploadQueueOptions<T> = {
  concurrency: number;
  signal?: AbortSignal;
  hooks?: UploadSchedulerHooks<T>;
  /** Injection pour les tests : source d'aléa du repli. */
  random?: () => number;
};

/**
 * Traite `items` en respectant concurrence, cadence et pauses.
 *
 * `attempt` doit ÉMETTRE la requête de façon synchrone avant sa première suspension
 * (c'est le cas de `XMLHttpRequest.send`) : c'est ce qui rend atomique le contrôle
 * final décrit plus bas.
 *
 * Ne rejette jamais, y compris à l'annulation — chaque élément reçoit un sort via
 * `onSettled`, et un import interrompu reste un import dont on connaît l'état.
 */
export async function runUploadQueue<T>(
  items: readonly T[],
  attempt: (item: T, index: number) => Promise<UploadAttemptResult>,
  opts: RunUploadQueueOptions<T>,
): Promise<void> {
  if (items.length === 0) return;

  const { signal, hooks, random } = opts;
  const queue: Entry<T>[] = items.map((item, index) => ({ item, index, attempts: 0 }));
  const size = Math.max(1, Math.min(Math.floor(opts.concurrency) || 1, items.length));
  // Un seul allongement de cadence par lot : §« ne pas réduire ensuite » interdit
  // l'oscillation, et doubler à chaque 429 ferait diverger un lot un peu chahuté.
  let slowedDown = false;

  const aborted = () => signal?.aborted === true;

  const settle = (entry: Entry<T>, outcome: UploadOutcome, info: Partial<UploadSettledInfo> = {}) => {
    hooks?.onSettled?.(entry.item, outcome, { attempts: entry.attempts, ...info });
  };

  async function runner(): Promise<void> {
    for (;;) {
      const entry = queue.shift();
      if (!entry) return;

      if (aborted()) {
        settle(entry, "cancelled");
        continue;
      }

      // (1) Attente coordonnée. RELUE à chaque réveil : la pause a pu être prolongée
      //     par un autre worker pendant qu'on dormait.
      let interrupted = false;
      while (!aborted() && Date.now() < pauseUntil) {
        try {
          await sleepAbortable(pauseUntil - Date.now(), signal);
        } catch {
          interrupted = true;
          break;
        }
      }
      if (interrupted || aborted()) {
        settle(entry, "cancelled");
        continue;
      }

      // (2) Réservation SYNCHRONE du créneau de cadence. Lecture et écriture de
      //     `nextSlot` sans await entre les deux : deux workers ne peuvent pas
      //     réserver le même créneau.
      const now = Date.now();
      const slot = Math.max(now, nextSlot);
      nextSlot = slot + intervalMs;
      const wait = slot - now;
      if (wait > 0) {
        try {
          await sleepAbortable(wait, signal);
        } catch {
          settle(entry, "cancelled");
          continue;
        }
      }

      // (3) Contrôle final. Une pause a pu s'ouvrir pendant (2) : on rend la main
      //     sans consommer la tentative, l'élément repasse par (1).
      if (aborted()) {
        settle(entry, "cancelled");
        continue;
      }
      if (Date.now() < pauseUntil) {
        queue.unshift(entry);
        continue;
      }

      // AUCUN `await` entre le contrôle ci-dessus et l'émission ci-dessous.
      // JavaScript étant mono-thread, cette séquence est atomique : un worker qui a
      // franchi le contrôle est déjà parti, un worker qui ne l'a pas franchi ne
      // partira pas. C'est toute la synchronisation dont ce module a besoin.
      entry.attempts += 1;
      hooks?.onAttemptStart?.(entry.item, entry.attempts);
      let result: UploadAttemptResult;
      try {
        result = await attempt(entry.item, entry.index);
      } catch (error) {
        // `attempt` ne devrait pas rejeter ; s'il le fait, l'élément échoue seul.
        settle(entry, "error", { error });
        continue;
      }

      if (result.kind === "success") {
        // Terminal. Aucune transition sortante : c'est l'invariant qui garantit
        // qu'un envoi accepté ne repart jamais parce qu'un AUTRE a reçu un 429.
        settle(entry, "done");
        continue;
      }
      if (result.kind === "cancelled") {
        settle(entry, "cancelled");
        continue;
      }
      if (result.kind === "terminal_error") {
        settle(entry, "error", { error: result.error });
        continue;
      }

      // --- 429 -------------------------------------------------------------
      // Le délai du serveur prime : lui seul connaît l'état de son seau. Le repli
      // n'existe que pour l'absence d'en-tête.
      const serverDelay = result.retryAfterMs;
      const delay =
        typeof serverDelay === "number" && Number.isFinite(serverDelay)
          ? clampRetryDelay(serverDelay)
          : fallbackRetryDelay(entry.attempts, random);

      // Monotone : une réponse arrivée plus tard avec un délai plus court ne doit
      // jamais raccourcir une pause déjà décidée.
      pauseUntil = Math.max(pauseUntil, Date.now() + delay);

      // Adaptation minimale : la cadence nominale s'est révélée trop rapide pour
      // ce que le quota autorise réellement (typiquement, un autre onglet le
      // consomme aussi). On l'allonge une fois, on ne la réduit plus.
      if (!slowedDown) {
        intervalMs *= 2;
        slowedDown = true;
      }

      if (entry.attempts >= MAX_UPLOAD_ATTEMPTS) {
        settle(entry, "error", { reason: RATE_LIMIT_EXHAUSTED });
        continue;
      }
      hooks?.onWaitingRetry?.(entry.item, delay, entry.attempts);
      queue.push(entry);
    }
  }

  activeBatches += 1;
  try {
    await Promise.all(Array.from({ length: size }, () => runner()));
  } finally {
    activeBatches -= 1;
    // Dernier lot terminé : l'onglet retrouve sa cadence nominale. Sans cela un 429
    // isolé ralentirait tous les imports suivants jusqu'au rechargement de la page.
    if (activeBatches <= 0) {
      activeBatches = 0;
      // Retour à la cadence de RÉFÉRENCE du module — la nominale en production,
      // l'éventuel override en test.
      intervalMs = baselineIntervalMs;
    }
  }
}
