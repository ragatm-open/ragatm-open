"use client";

import { useEffect, useState } from "react";

/**
 * Seuils responsive de l'application.
 *
 *   mobile  : ≤ 767 px   — navigation en tiroir, Sources en tiroir, chat pleine largeur
 *   tablet  : 768–1199 px — sidebar fixe, Sources en tiroir
 *   desktop : ≥ 1200 px  — sidebar + chat + Sources en colonnes
 *
 * La MISE EN PAGE est portée par le CSS (les media queries des `*.module.css` du
 * shell et du chat recopient ces valeurs : une media query ne peut pas lire une
 * variable). Ce module ne sert qu'au comportement : refermer un tiroir quand on
 * franchit un seuil, et savoir si une citation doit ouvrir le tiroir Sources.
 * Valeurs de référence : docs/10-reference/responsive.md.
 */
export const MOBILE_MAX_WIDTH = 767;
export const COMPACT_MAX_WIDTH = 1199;

export const MOBILE_QUERY = `(max-width: ${MOBILE_MAX_WIDTH}px)`;
export const COMPACT_QUERY = `(max-width: ${COMPACT_MAX_WIDTH}px)`;

/**
 * Vrai si la media query correspond. `false` au rendu serveur et là où
 * `matchMedia` n'existe pas (jsdom) : le comportement par défaut est celui du
 * desktop, que le CSS rend de toute façon sans JavaScript.
 */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(false);
  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return;
    const mql = window.matchMedia(query);
    const update = () => setMatches(mql.matches);
    update();
    mql.addEventListener?.("change", update);
    return () => mql.removeEventListener?.("change", update);
  }, [query]);
  return matches;
}
