"use client";

// Bascule de langue côté client : écrit le cookie de locale, tente de
// synchroniser la préférence du profil (ignoré si l'utilisateur n'est pas
// connecté ou si le backend échoue — fonctionnement dégradé via cookie), puis
// rafraîchit les Server Components pour appliquer la langue SANS navigation.

import { useCallback, useTransition } from "react";
import { useRouter } from "next/navigation";
import { useLocale } from "next-intl";

import { apiPatch } from "@/lib/api";
import { coerceLocale, LOCALE_COOKIE, type SupportedLocale } from "./config";

/** Écrit le cookie de locale (1 an, path racine, SameSite=Lax). NON HttpOnly. */
export function setLocaleCookie(locale: SupportedLocale) {
  if (typeof document === "undefined") return;
  const oneYear = 60 * 60 * 24 * 365;
  document.cookie = `${LOCALE_COOKIE}=${locale}; path=/; max-age=${oneYear}; samesite=lax`;
}

/**
 * Synchronise la préférence du compte. Fail-open : toute erreur (401 anonyme,
 * réseau, 5xx) est avalée — le cookie a déjà été posé et suffit à l'UI.
 */
async function syncProfileLocale(locale: SupportedLocale): Promise<void> {
  try {
    await apiPatch("/api/v1/me/preferences/", { preferred_locale: locale });
  } catch {
    // Dégradé volontaire : la langue reste appliquée via le cookie.
  }
}

/** Hook de changement de langue réutilisable par le sélecteur. */
export function useChangeLocale() {
  const active = coerceLocale(useLocale());
  const router = useRouter();
  const [pending, startTransition] = useTransition();

  const change = useCallback(
    (next: SupportedLocale) => {
      if (next === active) return;
      setLocaleCookie(next);
      // Ne bloque pas l'UI sur le backend : synchronisation en arrière-plan.
      void syncProfileLocale(next);
      // Re-rend les Server Components (nouvelle locale lue depuis le cookie).
      startTransition(() => router.refresh());
    },
    [active, router]
  );

  return { active, change, pending };
}
