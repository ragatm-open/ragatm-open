// Configuration i18n centrale — SOURCE UNIQUE des locales, du cookie et des
// namespaces de dictionnaires. Aucune de ces valeurs ne doit être dupliquée
// ailleurs : loader (`messages.ts`), résolution (`locale.ts`), requête serveur
// (`request.ts`), sélecteur (`LanguageSelector`) et tests importent d'ici.

export const SUPPORTED_LOCALES = ["fr", "en"] as const;
export type SupportedLocale = (typeof SUPPORTED_LOCALES)[number];

export const DEFAULT_LOCALE: SupportedLocale = "fr";

/** Cookie de locale (NON HttpOnly : lisible côté serveur ET client). */
export const LOCALE_COOKIE = "ragatm_locale";

/** Libellés natifs affichés dans le sélecteur (jamais traduits). */
export const LOCALE_LABELS: Record<SupportedLocale, string> = {
  fr: "Français",
  en: "English",
};

/** Namespaces de dictionnaires. Les fichiers `messages/<locale>/<ns>.json`
 *  doivent exister pour CHAQUE locale et CHAQUE namespace (cf. test de symétrie). */
export const NAMESPACES = [
  "common",
  "navigation",
  "auth",
  "login",
  "passwordReset",
  "invitations",
  "home",
  "chat",
  "documents",
  "documentViewer",
  "corpora",
  "workspaces",
  "workspaceAdmin",
  "platformAdmin",
  "adminCore",
  "statuses",
  "errors",
  "accessibility",
  "answerLanguage",
  "navigationSearch",
  "documentCapabilities",
] as const;

export type Namespace = (typeof NAMESPACES)[number];

/** Garde de type : `true` si `value` est une locale supportée. */
export function isSupportedLocale(value: unknown): value is SupportedLocale {
  return typeof value === "string" && (SUPPORTED_LOCALES as readonly string[]).includes(value);
}

/** Ramène n'importe quelle valeur à une locale supportée (fallback `DEFAULT_LOCALE`). */
export function coerceLocale(value: unknown): SupportedLocale {
  return isSupportedLocale(value) ? value : DEFAULT_LOCALE;
}
