// Chargement des dictionnaires pour une locale donnée. Les fichiers modulaires
// `messages/<locale>/<ns>.json` sont fusionnés en un objet `{ [ns]: {...} }`
// consommable par next-intl. Fonctionne côté serveur (RSC / request.ts).

import { coerceLocale, NAMESPACES, type SupportedLocale } from "./config";

type Messages = Record<string, unknown>;

/** Charge et fusionne tous les namespaces pour une locale (fallback fr si inconnue). */
export async function loadMessages(locale: SupportedLocale | string): Promise<Messages> {
  const safe = coerceLocale(locale);
  const entries = await Promise.all(
    NAMESPACES.map(async (ns) => {
      const mod = await import(`../../messages/${safe}/${ns}.json`);
      return [ns, mod.default] as const;
    })
  );
  return Object.fromEntries(entries);
}
