"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import { usePathname } from "next/navigation";
import { useTranslations } from "next-intl";
import { Icon } from "@/components/ui";
import { WorkspaceProvider } from "./WorkspaceContext";
import { AuthIssueNotice } from "./AuthIssueNotice";
import { Sidebar } from "./Sidebar";
import { MOBILE_QUERY, useMediaQuery } from "./breakpoints";
import { useModalLayer } from "./useModalLayer";
import { CommandPalette } from "./CommandPalette";
import { ShellNavContext, useShellNav, type PaletteTab } from "./shellNav";
import styles from "./AppShell.module.css";

export { useShellNav } from "./shellNav";
export type { ShellNav, PaletteTab } from "./shellNav";

export const NAV_DRAWER_ID = "app-navigation";

/** Cible d'un raccourci « / » : jamais une zone de saisie (on y tape un « / »). */
function isEditableTarget(el: EventTarget | null): boolean {
  const node = el as HTMLElement | null;
  if (!node || typeof node.tagName !== "string") return false;
  const tag = node.tagName.toLowerCase();
  if (tag === "textarea" || tag === "select") return true;
  if (tag === "input") {
    const type = ((node as HTMLInputElement).type || "text").toLowerCase();
    return !["button", "checkbox", "radio", "submit", "reset", "range", "color", "file", "image"].includes(type);
  }
  return Boolean(node.isContentEditable);
}

/** Un calque modal (tiroir, Sources, autre dialogue) est ouvert : `useModalLayer`
 *  pose `inert` sur le reste de la page. */
function otherLayerOpen(): boolean {
  return typeof document !== "undefined" && document.querySelector("[inert]") !== null;
}

/** Topbar sticky : titre + sous-titre à gauche, actions contextuelles à droite. */
export function Topbar({
  title,
  subtitle,
  actions,
}: {
  title: string;
  subtitle?: string | null;
  actions?: React.ReactNode;
}) {
  const nav = useShellNav();
  const tn = useTranslations("accessibility");
  return (
    <header className={styles.topbar}>
      {nav && (
        <button
          ref={nav.menuButtonRef}
          type="button"
          className={styles.menuButton}
          onClick={nav.navOpen ? nav.closeNav : nav.openNav}
          aria-label={tn(nav.navOpen ? "closeMenu" : "openMenu")}
          aria-expanded={nav.navOpen}
          aria-controls={NAV_DRAWER_ID}
        >
          <Icon name="menu" size={22} />
        </button>
      )}
      <div className={styles.titleBlock}>
        <h1
          className={styles.title}
          style={{ fontSize: 19, fontWeight: 700, color: "var(--ink)", letterSpacing: "-0.015em", margin: 0 }}
        >
          {title}
        </h1>
        {subtitle && (
          <p className={styles.subtitle} style={{ fontSize: 13, color: "var(--ink-3)", margin: "2px 0 0" }}>
            {subtitle}
          </p>
        )}
      </div>
      {actions}
    </header>
  );
}

/**
 * Coquille applicative partagée : nav latérale + zone principale.
 * Fournit le WorkspaceProvider — les enfants (et leur <Topbar>) peuvent
 * donc appeler useWorkspace() et réagir à l'espace courant.
 *
 * `fill` : la coquille occupe exactement la hauteur du viewport et le document
 * ne défile plus (page chat : conversation et Sources défilent chacune seule).
 */
export function AppShell({ children, fill = false }: { children: React.ReactNode; fill?: boolean }) {
  const [navOpen, setNavOpen] = useState(false);
  const isMobile = useMediaQuery(MOBILE_QUERY);
  const pathname = usePathname();
  const menuButtonRef = useRef<HTMLButtonElement>(null);
  const drawerRef = useRef<HTMLElement>(null);
  const backdropRef = useRef<HTMLDivElement>(null);

  const openNav = useCallback(() => setNavOpen(true), []);
  const closeNav = useCallback(() => setNavOpen(false), []);

  // Palette de recherche globale.
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [paletteTab, setPaletteTab] = useState<PaletteTab>("all");
  const paletteOpenRef = useRef(false);
  paletteOpenRef.current = paletteOpen;
  const openPalette = useCallback((tab: PaletteTab = "all") => {
    // Ouverte depuis le tiroir mobile : le tiroir se ferme d'abord.
    setNavOpen(false);
    setPaletteTab(tab);
    setPaletteOpen(true);
  }, []);
  const closePalette = useCallback(() => setPaletteOpen(false), []);

  // UNIQUE écouteur clavier global : Ctrl+K / ⌘K partout (y compris dans le composer,
  // qui n'a pas de raccourci propre), « / » seulement hors zone de saisie. Rien si un
  // autre calque modal est ouvert ou pendant une composition IME.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.isComposing || e.defaultPrevented) return;
      const key = e.key;
      if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && (key === "k" || key === "K")) {
        if (paletteOpenRef.current) {
          e.preventDefault();
          return;
        }
        if (otherLayerOpen()) return;
        e.preventDefault();
        openPalette("all");
        return;
      }
      if (key === "/" && !e.ctrlKey && !e.metaKey && !e.altKey) {
        if (paletteOpenRef.current || isEditableTarget(e.target) || otherLayerOpen()) return;
        e.preventDefault();
        openPalette("all");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [openPalette]);

  // Un tiroir ouvert ne survit ni à un changement de page, ni au passage en
  // tablette/desktop (où la sidebar redevient une colonne).
  useEffect(() => setNavOpen(false), [pathname]);
  useEffect(() => {
    if (!isMobile) setNavOpen(false);
  }, [isMobile]);

  useModalLayer({ active: navOpen && isMobile, containerRef: drawerRef, keep: [backdropRef], onClose: closeNav });

  return (
    <WorkspaceProvider>
      <ShellNavContext.Provider
        value={{ navOpen, openNav, closeNav, menuButtonRef, paletteOpen, openPalette, closePalette }}
      >
        <div className={fill ? `${styles.shell} ${styles.fill}` : styles.shell}>
          <Sidebar ref={drawerRef} id={NAV_DRAWER_ID} open={navOpen} onNavigate={closeNav} />
          <div
            ref={backdropRef}
            className={styles.backdrop}
            data-open={navOpen ? "true" : "false"}
            onClick={closeNav}
            aria-hidden="true"
          />
          <main className={styles.main}>
            <AuthIssueNotice />
            {children}
          </main>
        </div>
        {paletteOpen && <CommandPalette open={paletteOpen} onClose={closePalette} initialTab={paletteTab} />}
      </ShellNavContext.Provider>
    </WorkspaceProvider>
  );
}
