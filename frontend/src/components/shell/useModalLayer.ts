"use client";

import { useEffect, useRef } from "react";

const FOCUSABLE =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * Calque modal d'un tiroir (navigation mobile, Sources mobile/tablette).
 *
 * Tant que `active` :
 * - tout ce qui n'est pas le tiroir (ni un élément de `keep`, typiquement le
 *   voile) reçoit l'attribut natif `inert` : ni focusable, ni cliquable, ni lu
 *   par les lecteurs d'écran. C'est le piège de focus — natif, pas un
 *   intercepteur de Tab ;
 * - Échap appelle `onClose` ;
 * - le défilement du document est suspendu ;
 * - le focus est placé dans le tiroir.
 *
 * À la désactivation, tout est restauré à l'identique (seuls les `inert` posés
 * ici sont retirés) et le focus revient à l'élément qui l'avait à l'ouverture
 * — le bouton déclencheur ou la citation cliquée — s'il est encore visible.
 */
export function useModalLayer({
  active,
  containerRef,
  keep = [],
  onClose,
  initialFocusRef,
}: {
  active: boolean;
  containerRef: React.RefObject<HTMLElement | null>;
  keep?: React.RefObject<HTMLElement | null>[];
  onClose: () => void;
  initialFocusRef?: React.RefObject<HTMLElement | null>;
}) {
  // onClose change à chaque rendu chez l'appelant : on lit toujours le dernier
  // sans réarmer le calque (ce qui rendrait le focus puis le reprendrait).
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    const container = containerRef.current;
    if (!active || !container) return;

    const previouslyFocused = document.activeElement as HTMLElement | null;
    const kept = keep.map((r) => r.current).filter(Boolean) as HTMLElement[];

    // Rend inertes les frères du tiroir à chaque niveau jusqu'à <body>.
    const inerted: HTMLElement[] = [];
    let node: HTMLElement | null = container;
    while (node && node !== document.body) {
      const parent: HTMLElement | null = node.parentElement;
      if (!parent) break;
      for (const sibling of Array.from(parent.children) as HTMLElement[]) {
        if (sibling === node || kept.includes(sibling) || sibling.hasAttribute("inert")) continue;
        if (sibling.tagName === "SCRIPT" || sibling.tagName === "STYLE") continue;
        sibling.setAttribute("inert", "");
        inerted.push(sibling);
      }
      node = parent;
    }

    const root = document.documentElement;
    const previousOverflow = root.style.overflow;
    root.style.overflow = "hidden";

    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onCloseRef.current();
      }
    };
    document.addEventListener("keydown", onKey);

    const first = initialFocusRef?.current ?? (container.querySelector(FOCUSABLE) as HTMLElement | null);
    (first ?? container).focus({ preventScroll: true });

    return () => {
      document.removeEventListener("keydown", onKey);
      for (const el of inerted) el.removeAttribute("inert");
      root.style.overflow = previousOverflow;
      // Un élément devenu masqué (bouton Sources après passage en desktop) ignore
      // simplement focus() : pas d'effet de bord à craindre.
      if (previouslyFocused && previouslyFocused.isConnected) {
        previouslyFocused.focus({ preventScroll: true });
      }
    };
    // keep / initialFocusRef sont des refs stables chez les appelants.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, containerRef]);
}
