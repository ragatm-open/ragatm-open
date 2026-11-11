/**
 * Suivi d'une analyse globale — polling borné.
 *
 * Règles :
 * - intervalle borné, jamais de boucle serrée ;
 * - arrêt immédiat sur statut terminal ;
 * - arrêt au démontage (`stop()`), même si une requête est en vol ;
 * - **aucun polling dupliqué** : un seul suivi actif par `analysis_id`.
 */
import { getCorpusAnalysis, isTerminalAnalysisStatus, type CorpusAnalysis } from "@/lib/api";

export const POLL_INTERVAL_MS = 2000;
/** Plafond de sécurité : au-delà, on cesse de solliciter le serveur. */
export const POLL_MAX_MS = 15 * 60 * 1000;

/** Suivis actifs, par analysis_id. Empêche deux pollers sur le même job. */
const inFlight = new Set<string>();

export type AnalysisPollHandle = { stop: () => void };

export function pollAnalysis(
  analysisId: string,
  onUpdate: (a: CorpusAnalysis) => void,
  opts: { intervalMs?: number; maxMs?: number } = {},
): AnalysisPollHandle {
  const interval = opts.intervalMs ?? POLL_INTERVAL_MS;
  const maxMs = opts.maxMs ?? POLL_MAX_MS;

  if (inFlight.has(analysisId)) {
    // Un suivi existe déjà pour ce job : ne pas en ouvrir un second.
    return { stop: () => {} };
  }
  inFlight.add(analysisId);

  let stopped = false;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let elapsed = 0;

  const stop = () => {
    if (stopped) return;
    stopped = true;
    if (timer) clearTimeout(timer);
    timer = null;
    inFlight.delete(analysisId);
  };

  const tick = async () => {
    if (stopped) return;
    try {
      const a = await getCorpusAnalysis(analysisId);
      if (stopped) return;
      onUpdate(a);
      if (isTerminalAnalysisStatus(a.status)) {
        stop();
        return;
      }
    } catch {
      // Erreur réseau : on retente au prochain tick, sans faire tomber l'UI.
    }
    if (stopped) return;
    elapsed += interval;
    if (elapsed >= maxMs) {
      stop();
      return;
    }
    timer = setTimeout(tick, interval);
  };

  timer = setTimeout(tick, interval);
  return { stop };
}

/** Réservé aux tests : purge les suivis actifs. */
export function __resetAnalysisPolls() {
  inFlight.clear();
}
