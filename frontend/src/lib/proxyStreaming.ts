/**
 * Choix du mode de transmission du CORPS de requête par le proxy Next.js
 * (`app/api/[...path]/route.ts`).
 *
 * Par défaut, le proxy met le corps en mémoire (`arrayBuffer()`) avant de le relayer :
 * comportement historique, conservé pour toutes les routes. Un enregistrement audio peut
 * peser jusqu'à 600 Mo : le bufferiser en entier dans le processus Node serait un risque
 * mémoire. Le dépôt audio dédié est donc relayé EN FLUX, avec le `content-length` entrant
 * (Django en a besoin pour analyser le multipart).
 *
 * Liste FERMÉE : seul le chemin exact du dépôt audio est concerné. Ajouter un chemin ici
 * change le comportement réseau de cette route — ce n'est pas un réglage anodin.
 */
const STREAMED_BODY_PATHS: ReadonlySet<string> = new Set(["v1/documents/upload-audio"]);

/**
 * `path` = segments capturés par `[...path]` après `/api/` (ex. `v1/documents/upload-audio`),
 * avec ou sans barre finale. Les segments vides sont ignorés.
 */
export function shouldStreamRequestBody(path: string | readonly string[]): boolean {
  const segments = (typeof path === "string" ? path.split("/") : [...path]).filter(
    (seg) => seg !== ""
  );
  return STREAMED_BODY_PATHS.has(segments.join("/"));
}
