// frontend/src/lib/apiError.ts
//
// Erreur d'API porteuse du STATUT HTTP, pour un traitement centralisé des 403/404
// liés au cloisonnement owner-scope.
//
// Doctrine (anti-énumération) : le backend renvoie **404** aussi bien pour un espace
// inexistant que pour un espace hors périmètre — l'UI ne doit JAMAIS les distinguer
// ni révéler l'existence d'une ressource. Le **403** signale un rôle d'espace
// insuffisant sur une ressource visible. Les messages affichés sont NEUTRES et ne
// laissent jamais fuiter un code interne.

/**
 * Délai demandé par le serveur, en millisecondes, ou `null` si l'en-tête est absent
 * ou inexploitable.
 *
 * RFC 9110 §10.2.3 autorise DEUX formes : un nombre de secondes (`delta-seconds`) ou
 * une date HTTP. Django REST Framework n'émet aujourd'hui que la première
 * (`headers['Retry-After'] = '%d' % exc.wait`), mais un proxy inverse peut réécrire
 * l'en-tête, et le second cas coûte trois lignes. On ne se lie donc pas au
 * fournisseur actuel.
 *
 * `null` signifie « le serveur ne dit rien d'exploitable », jamais « pas d'attente » :
 * l'appelant doit se rabattre sur sa propre politique. Renvoyer 0 ferait repartir
 * immédiatement, c'est-à-dire exactement le défaut qu'on corrige.
 *
 * AUCUN écrêtage ici. La borne est une décision de politique, elle appartient à
 * l'ordonnanceur ; cette fonction ne fait que lire ce que le serveur a dit.
 */
export function parseRetryAfterMs(raw: string | null | undefined): number | null {
  const v = (raw || "").trim();
  if (!v) return null;

  // delta-seconds : entier non signé.
  if (/^\d+$/.test(v)) {
    const seconds = Number(v);
    return Number.isFinite(seconds) ? seconds * 1000 : null;
  }

  // HTTP-date. Une date passée rend 0 -> traitée comme « pas d'attente exploitable ».
  const at = Date.parse(v);
  if (!Number.isFinite(at)) return null;
  const delta = at - Date.now();
  return delta > 0 ? delta : null;
}

/** Erreur normalisée d'un appel API. `message` conserve le format historique
 *  (`API <status> <statusText>: <detail>`) pour la compat des `.includes(...)`. */
export class ApiError extends Error {
  readonly status: number;
  readonly statusText: string;
  /** `detail` applicatif si présent (usage interne : jamais affiché tel quel). */
  readonly code: string | null;
  readonly body: unknown;
  /**
   * Délai demandé par le serveur (`Retry-After`), en ms, ou `null`.
   *
   * Porté par l'erreur et non décidé dans la couche HTTP : celle-ci EXPOSE ce que le
   * serveur a répondu, elle ne choisit pas de réessayer. Un ré-essai silencieux ici
   * mentirait à tous les appelants sur la durée d'un appel.
   */
  readonly retryAfterMs: number | null;

  constructor(
    status: number,
    statusText: string,
    body: unknown,
    message: string,
    retryAfterMs: number | null = null,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.statusText = statusText;
    this.body = body;
    this.retryAfterMs = retryAfterMs;
    this.code =
      body && typeof body === "object" && typeof (body as any).detail === "string"
        ? ((body as any).detail as string)
        : null;
  }
}

export function isApiError(e: unknown): e is ApiError {
  return e instanceof ApiError;
}

/** Statut HTTP d'une erreur, ou `null` si ce n'est pas une ApiError. */
export function apiErrorStatus(e: unknown): number | null {
  return isApiError(e) ? e.status : null;
}

/**
 * Message FR NEUTRE pour les refus d'accès owner-scope. Renvoie `null` si l'erreur
 * n'est pas un 403/404 (le code appelant garde alors son message générique).
 *
 * - 404 : indistinct « inexistant / hors périmètre » (anti-énumération).
 * - 403 : ressource visible mais rôle insuffisant.
 * Aucun code interne n'est exposé.
 */
export function humanAccessError(e: unknown): string | null {
  const status = apiErrorStatus(e);
  if (status === 404) return "Cet espace est introuvable ou n'est pas accessible.";
  if (status === 403) return "Vous n'avez pas les droits nécessaires pour cette action.";
  return null;
}

/** True si l'erreur est un refus de périmètre/rôle (403 ou 404). */
export function isAccessDenied(e: unknown): boolean {
  const status = apiErrorStatus(e);
  return status === 403 || status === 404;
}
