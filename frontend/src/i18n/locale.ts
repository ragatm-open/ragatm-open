// Résolution de la locale active — logique PURE et testable, sans dépendance à
// Next. La stratégie suit la doctrine du projet :
//
//   Utilisateur authentifié : preferred_locale > cookie > navigateur > fr
//   Utilisateur anonyme      :                    cookie > navigateur > fr
//
// La synchronisation profil→cookie est faite à la connexion et au changement de
// langue (cf. LanguageSelector) : le rendu serveur lit donc TOUJOURS le cookie
// comme source immédiate, le profil restant la source persistante synchronisée.

import { DEFAULT_LOCALE, SUPPORTED_LOCALES, isSupportedLocale, type SupportedLocale } from "./config";

/**
 * Parse un en-tête `Accept-Language` et renvoie la première locale supportée,
 * ou `null`. Tolère les variantes régionales (`en-US` → `en`) et les poids `;q=`.
 */
export function localeFromAcceptLanguage(header: string | null | undefined): SupportedLocale | null {
  if (!header) return null;
  const ranked = header
    .split(",")
    .map((part) => {
      const [tag, ...params] = part.trim().split(";");
      const q = params.find((p) => p.trim().startsWith("q="));
      const weight = q ? Number.parseFloat(q.split("=")[1]) : 1;
      return { tag: tag.trim().toLowerCase(), weight: Number.isFinite(weight) ? weight : 1 };
    })
    .filter((e) => e.tag)
    .sort((a, b) => b.weight - a.weight);

  for (const { tag } of ranked) {
    const base = tag.split("-")[0];
    if (isSupportedLocale(base)) return base;
    const exact = SUPPORTED_LOCALES.find((l) => l === tag);
    if (exact) return exact;
  }
  return null;
}

/**
 * Résout la locale à partir des signaux disponibles, dans l'ordre de priorité.
 * `preferred` (profil compte) l'emporte quand il est fourni et valide.
 */
export function resolveLocale(opts: {
  preferred?: string | null;
  cookie?: string | null;
  acceptLanguage?: string | null;
}): SupportedLocale {
  if (isSupportedLocale(opts.preferred)) return opts.preferred;
  if (isSupportedLocale(opts.cookie)) return opts.cookie;
  const fromHeader = localeFromAcceptLanguage(opts.acceptLanguage);
  if (fromHeader) return fromHeader;
  return DEFAULT_LOCALE;
}
