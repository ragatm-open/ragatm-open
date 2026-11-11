// Aides de formatage côté UI, sensibles à la locale active (fr | en).
//
// La locale est déduite du cookie `ragatm_locale` (source de rendu immédiate,
// synchronisée avec le profil). Les fonctions gardent leur signature historique
// mais acceptent une locale optionnelle pour les appels serveur/tests. Le
// formatage passe par l'API `Intl` (dates, nombres) — jamais de concaténation
// manuelle ni de séparateur codé en dur.

import { LOCALE_COOKIE, coerceLocale, DEFAULT_LOCALE, type SupportedLocale } from "@/i18n/config";

/** Locale active côté client (cookie), `fr` par défaut / côté serveur. */
export function activeLocale(): SupportedLocale {
  if (typeof document === "undefined") return DEFAULT_LOCALE;
  const m = document.cookie.match(new RegExp(`(?:^|;\\s*)${LOCALE_COOKIE}=([^;]*)`));
  return coerceLocale(m ? decodeURIComponent(m[1]) : DEFAULT_LOCALE);
}

/** Étiquette BCP-47 pour Intl à partir d'une locale supportée. */
function intlLocale(locale?: SupportedLocale): string {
  const l = locale ?? activeLocale();
  return l === "en" ? "en-US" : "fr-FR";
}

/** Date ISO → libellé relatif court, localisé (« il y a 2 h » / “2 h ago »). */
export function relativeTime(iso: string | null | undefined, locale?: SupportedLocale): string {
  if (!iso) return "—";
  const d = new Date(iso).getTime();
  if (!Number.isFinite(d)) return "—";
  const l = locale ?? activeLocale();
  const rtf = new Intl.RelativeTimeFormat(intlLocale(l), { numeric: "auto", style: "short" });
  const diffMs = d - Date.now();
  const min = Math.round(diffMs / 60000);
  const absMin = Math.abs(min);
  if (absMin < 1) return l === "en" ? "just now" : "à l'instant";
  if (absMin < 60) return rtf.format(min, "minute");
  const h = Math.round(min / 60);
  if (Math.abs(h) < 24) return rtf.format(h, "hour");
  const day = Math.round(h / 24);
  if (Math.abs(day) < 7) return rtf.format(day, "day");
  const week = Math.round(day / 7);
  if (Math.abs(week) < 5) return rtf.format(week, "week");
  return new Date(iso).toLocaleDateString(intlLocale(l));
}

/** Entier → nombre localisé (fr : « 1 094 » ; en : “1,094”).
 *
 * Existe parce que `n.toLocaleString()` — SANS locale — formate selon la locale du
 * RUNTIME, pas selon la langue de l'interface. Un serveur ou un navigateur en
 * `en-US` affichait donc « 1,094 » à un utilisateur francophone, et le rendu
 * différait entre serveur et client. Le défaut a été trouvé par un test qui
 * échouait en CI (locale `und`) et passait en local (`LANG=fr_FR.UTF-8`).
 */
export function formatCount(n: number | null | undefined, locale?: SupportedLocale): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return new Intl.NumberFormat(intlLocale(locale), { maximumFractionDigits: 0 }).format(n);
}

/** Octets → taille lisible localisée (fr : « 2,5 Mo » ; en : “2.5 MB »). */
export function fileSize(bytes: number | null | undefined, locale?: SupportedLocale): string {
  if (bytes == null || !Number.isFinite(bytes)) return "—";
  const l = locale ?? activeLocale();
  const nf = new Intl.NumberFormat(intlLocale(l), { maximumFractionDigits: 1 });
  if (bytes < 1024) {
    const unit = l === "en" ? "B" : "o";
    return `${nf.format(bytes)} ${unit}`;
  }
  const units = l === "en" ? ["KB", "MB", "GB", "TB"] : ["Ko", "Mo", "Go", "To"];
  let v = bytes / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  const rounded = v >= 10 ? Math.round(v) : Math.round(v * 10) / 10;
  return `${nf.format(rounded)} ${units[i]}`;
}

/** Libellé court de type de fichier dérivé du nom (PDF, DOCX, …). Non localisé. */
export function fileTypeLabel(filename: string | null | undefined): string {
  const ext = (filename || "").split(".").pop()?.toUpperCase() || "";
  return ext.length <= 4 && /^[A-Z0-9]+$/.test(ext) ? ext : (activeLocale() === "en" ? "FILE" : "FIC");
}

/** Date ISO → date longue localisée (« 12 mai 2025 » / “May 12, 2025 »). */
export function shortDate(iso: string | null | undefined, locale?: SupportedLocale): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString(intlLocale(locale), { day: "numeric", month: "short", year: "numeric" });
}

/**
 * Date ISO → date **et heure** localisées (« 28 juil. 2026 à 14:37 » /
 * “Jul 28, 2026 at 2:37 PM »). Employé là où l'heure porte une information : deux
 * documents importés le même jour ne sont pas discernables sans elle, et une liste
 * triable par date d'import doit montrer ce sur quoi elle trie.
 */
export function dateTime(iso: string | null | undefined, locale?: SupportedLocale): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString(intlLocale(locale), {
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** Premier prénom à partir d'un nom d'utilisateur ou d'un email. Non localisé. */
export function firstName(nameOrEmail: string | null | undefined): string {
  const s = (nameOrEmail || "").trim();
  if (!s) return "";
  const base = s.includes("@") ? s.split("@")[0] : s;
  const part = base.split(/[\s._-]+/).filter(Boolean)[0] || base;
  return part.charAt(0).toUpperCase() + part.slice(1);
}
