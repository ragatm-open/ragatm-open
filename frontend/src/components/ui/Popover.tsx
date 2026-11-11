"use client";

import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

// useLayoutEffect côté client uniquement (évite l'avertissement SSR).
const useIso = typeof window !== "undefined" ? useLayoutEffect : useEffect;

type Align = "start" | "end";

type Pos = { top: number; left?: number; right?: number; width?: number; maxH: number };

/**
 * Menu flottant ancré à un déclencheur, rendu via un portal sur <body>.
 *
 * Pourquoi un portal : les menus rendus en `position: absolute` à l'intérieur
 * d'un conteneur en `overflow: hidden` (ex. la carte des membres) étaient
 * tronqués. En portalisant sur <body> avec `position: fixed`, le menu échappe à
 * tout `overflow`/stacking context parent et reste entièrement visible, y compris
 * pour le dernier élément d'une liste (bascule vers le haut si pas de place en bas).
 *
 * Le composant gère : positionnement depuis le rect du déclencheur, bascule
 * haut/bas selon l'espace disponible, fermeture au clic extérieur, fermeture sur
 * Escape, et repositionnement sur scroll/resize.
 */
export function Popover({
  open,
  onClose,
  anchorRef,
  align = "start",
  matchWidth = false,
  minWidth,
  maxHeight,
  gap = 6,
  padding = 6,
  radius = 12,
  role = "listbox",
  children,
}: {
  open: boolean;
  onClose: () => void;
  anchorRef: React.RefObject<HTMLElement | null>;
  align?: Align;
  /** Aligne la largeur du menu sur celle du déclencheur. */
  matchWidth?: boolean;
  minWidth?: number;
  maxHeight?: number;
  gap?: number;
  padding?: number;
  radius?: number;
  role?: string;
  children: React.ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<Pos | null>(null);

  const place = useCallback(() => {
    const a = anchorRef.current;
    if (!a) return;
    const r = a.getBoundingClientRect();
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const menuH = ref.current?.offsetHeight ?? 0;
    const margin = 8;
    const spaceBelow = vh - r.bottom - gap - margin;
    const spaceAbove = r.top - gap - margin;
    // On bascule vers le haut si le menu ne tient pas en bas ET qu'il y a plus de place au-dessus.
    const openUp = menuH > spaceBelow && spaceAbove > spaceBelow;
    const avail = openUp ? spaceAbove : spaceBelow;
    const maxH = Math.max(120, Math.min(maxHeight ?? Infinity, avail));
    const top = openUp ? Math.max(margin, r.top - gap - Math.min(menuH, maxH)) : r.bottom + gap;

    const next: Pos = { top, maxH };
    if (matchWidth) {
      next.left = r.left;
      next.width = r.width;
    } else if (align === "end") {
      next.right = Math.max(margin, vw - r.right);
    } else {
      next.left = Math.max(margin, Math.min(r.left, vw - (minWidth ?? 0) - margin));
    }
    setPos(next);
  }, [anchorRef, align, gap, matchWidth, maxHeight, minWidth]);

  useIso(() => {
    if (!open) {
      setPos(null);
      return;
    }
    place();
    // Re-mesure après que le contenu ait été peint (hauteur réelle connue).
    const raf = requestAnimationFrame(place);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    window.addEventListener("keydown", onKey);
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
      window.removeEventListener("keydown", onKey);
    };
  }, [open, place, onClose]);

  if (!open || typeof document === "undefined") return null;

  return createPortal(
    <>
      {/* Capteur de clic extérieur (couvre tout le viewport, sous le menu). */}
      <div onClick={onClose} style={{ position: "fixed", inset: 0, zIndex: 1000 }} />
      <div
        ref={ref}
        role={role}
        style={{
          position: "fixed",
          top: pos?.top ?? -9999,
          left: pos?.left,
          right: pos?.right,
          width: pos?.width,
          minWidth,
          maxHeight: pos?.maxH,
          overflowY: "auto",
          zIndex: 1001,
          background: "var(--surface)",
          border: "1px solid var(--border)",
          borderRadius: radius,
          boxShadow: "var(--shadow-pop)",
          padding,
          // Invisible tant que la position n'est pas calculée (évite un flash en haut-gauche).
          visibility: pos ? "visible" : "hidden",
        }}
      >
        {children}
      </div>
    </>,
    document.body
  );
}
