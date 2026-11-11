// frontend/src/app/chat/_lib/citations.ts
/**
 * Sources CITÉES vs sources CONSULTÉES.
 *
 * Bug corrigé ici : l'interface affichait « N sources citées ✓ » en comptant les sources
 * RETROUVÉES par le moteur — y compris sous une réponse « Je ne sais pas. ». Un badge vert de
 * confiance apparaissait donc exactement là où le produit devait signaler son incertitude.
 *
 * L'autorité est le CONTRAT serveur (`sources_contract`, cf.
 * backend/apps/rag_core/services/answer_contract.py) : lui seul sait ce que le modèle a
 * réellement cité. Les heuristiques de ce module ne servent qu'aux messages RELUS DEPUIS
 * L'HISTORIQUE, persistés avant l'existence du contrat — jamais à contredire le serveur.
 */

/** Contrat serveur : trois populations de sources + statut d'abstention. */
export type SourcesContract = {
  /** Candidats retrouvés par le retrieval (≥ transmis). */
  retrieved_sources_count: number;
  /** Sources réellement transmises au modèle (après diversification + budget de prompt). */
  prompt_sources_count: number;
  /** Identifiants [n] réellement cités par le modèle. */
  cited_source_ids: number[];
  cited_sources_count?: number;
  /** La réponse est-elle une abstention (« je ne sais pas », « sources insuffisantes ») ? */
  answer_is_abstention: boolean;
  /**
   * La génération a-t-elle été coupée par le plafond de tokens ?
   *
   * `null` = inconnu (le compteur de tokens manquait) — à ne PAS confondre avec
   * `false`. Une réponse tronquée reçoit quand même sa ligne « Sources: », ajoutée
   * après la génération : sans ce drapeau, elle est indistinguable d'une réponse
   * complète, y compris pour son lecteur.
   */
  answer_truncated?: boolean | null;
};

const CITATION_RE = /\[(\d{1,3})\]/g;
const SOURCES_LINE_RE = /^[ \t]*sources?[ \t]*:.*$/gim;

/**
 * Formules d'abstention — sous-ensemble VOLONTAIREMENT réduit du détecteur backend, qui
 * reste la référence. Utilisé seulement pour les messages d'historique sans contrat.
 */
const ABSTENTION_RE =
  /(je ne sais pas|je ne dispose pas|je n['’]ai pas (?:assez|suffisamment)|pas assez d['’]informations?|informations? insuffisantes?|ne permettent? pas de r[ée]pondre|impossible de r[ée]pondre|i (?:don['’]t|do not) know|not enough information)/i;

/** Identifiants [n] présents dans un texte, bornés au nombre de sources affichées. */
export function parseCitedSourceIds(text: string, maxId: number): number[] {
  if (!text || maxId <= 0) return [];
  const found = new Set<number>();
  for (const m of text.matchAll(CITATION_RE)) {
    const n = parseInt(m[1], 10);
    if (n >= 1 && n <= maxId) found.add(n);
  }
  return [...found].sort((a, b) => a - b);
}

/** Heuristique de repli (historique uniquement) : le texte est-il une abstention ? */
export function looksLikeAbstention(text: string): boolean {
  const body = (text || "").replace(SOURCES_LINE_RE, "").trim();
  if (!body) return true;
  return ABSTENTION_RE.test(body);
}

export type SourcesDisplay = {
  /** `cited` = le modèle a cité ces sources ; `consulted` = elles ont seulement été lues. */
  kind: "cited" | "consulted";
  /** Numéros de pastilles à afficher (1-indexés, alignés sur le panneau des sources). */
  ids: number[];
};

/**
 * Décide ce que le bandeau sous une réponse a le droit d'affirmer.
 *
 * - abstention (contrat serveur ou repli) → « consultées », jamais de badge positif ;
 * - citations réelles → « citées », limitées aux identifiants cités ;
 * - réponse sans citation → « consultées » : on n'invente pas une traçabilité absente.
 */
export function resolveSourcesDisplay(args: {
  contract?: SourcesContract | null;
  text: string;
  sourcesCount: number;
}): SourcesDisplay {
  const { contract, text, sourcesCount } = args;
  const all = Array.from({ length: Math.max(0, sourcesCount) }, (_, i) => i + 1);

  if (contract) {
    if (contract.answer_is_abstention) return { kind: "consulted", ids: all };
    const cited = (contract.cited_source_ids || []).filter((n) => n >= 1 && n <= sourcesCount);
    return cited.length ? { kind: "cited", ids: cited } : { kind: "consulted", ids: all };
  }

  // Historique : aucun contrat persisté, on retombe sur le texte.
  if (looksLikeAbstention(text)) return { kind: "consulted", ids: all };
  const cited = parseCitedSourceIds(text, sourcesCount);
  return cited.length ? { kind: "cited", ids: cited } : { kind: "consulted", ids: all };
}
