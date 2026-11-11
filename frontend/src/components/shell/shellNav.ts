"use client";

import { createContext, useContext } from "react";

export type PaletteTab = "all" | "spaces" | "conversations";

/**
 * État de navigation local à la coquille : tiroir mobile (Sidebar) et palette de
 * recherche globale. Module à part pour que Sidebar, AppShell et les pages le lisent
 * sans dépendance circulaire.
 */
export type ShellNav = {
  navOpen: boolean;
  openNav: () => void;
  closeNav: () => void;
  menuButtonRef: React.RefObject<HTMLButtonElement>;
  paletteOpen: boolean;
  openPalette: (tab?: PaletteTab) => void;
  closePalette: () => void;
};

export const ShellNavContext = createContext<ShellNav | null>(null);

/** `null` hors coquille (Sidebar ou Topbar rendus isolément, tests). */
export function useShellNav(): ShellNav | null {
  return useContext(ShellNavContext);
}
