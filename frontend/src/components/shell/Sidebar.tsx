"use client";

import React, { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useTranslations } from "next-intl";
import { Logo, Icon, Avatar, initialsFrom } from "@/components/ui";
import { useWorkspace, ROLE_INFO } from "./WorkspaceContext";
import { SpaceSwitcher } from "./SpaceSwitcher";
import { WorkspaceSwitcher } from "./WorkspaceSwitcher";
import { useShellNav } from "./shellNav";
import { LanguageSelector } from "@/components/LanguageSelector";
import styles from "./Sidebar.module.css";

type NavItem = {
  href: string;
  // Clé de traduction (namespace `navigation`) plutôt qu'un libellé figé.
  labelKey: string;
  icon: string;
  adminOnly?: boolean;
  globalOwnerOnly?: boolean;
  superuserOnly?: boolean;
};

const NAV: NavItem[] = [
  { href: "/home", labelKey: "home", icon: "home" },
  { href: "/chat", labelKey: "assistant", icon: "chat" },
  { href: "/documents", labelKey: "documents", icon: "doc" },
  { href: "/corpora", labelKey: "corpora", icon: "archive" },
  { href: "/workspace-admin", labelKey: "workspaceAdmin", icon: "users", adminOnly: true },
  { href: "/ragatm-admin", labelKey: "corpusAdmin", icon: "shield", globalOwnerOnly: true },
  { href: "/admin-core", labelKey: "adminCore", icon: "settings", globalOwnerOnly: true },
  { href: "/platform-admin", labelKey: "platformAdmin", icon: "grid", superuserOnly: true },
];

/**
 * Navigation principale. Colonne fixe de 256 px en tablette/desktop ; en mobile
 * (≤ 767 px) la MÊME arborescence devient un tiroir superposé, piloté par
 * AppShell (`open`, `onNavigate`) — aucune navigation dupliquée.
 */
export const Sidebar = React.forwardRef<
  HTMLElement,
  { id?: string; open?: boolean; onNavigate?: () => void }
>(function Sidebar({ id, open = false, onNavigate }, ref) {
  const pathname = usePathname();
  const t = useTranslations("navigation");
  const tn = useTranslations("accessibility");
  const tw = useTranslations("workspaces");
  const { me, role, isAdmin, isGlobalOwner, isSuperuser, logout } = useWorkspace();
  const items = NAV.filter(
    (n) =>
      (!n.adminOnly || isAdmin) &&
      (!n.globalOwnerOnly || isGlobalOwner) &&
      (!n.superuserOnly || isSuperuser)
  );

  return (
    <aside ref={ref} id={id} className={styles.sidebar} data-open={open ? "true" : "false"}>
      <div className={styles.brand}>
        <Logo size={26} />
        {onNavigate && (
          <button type="button" className={styles.closeButton} onClick={onNavigate} aria-label={tn("closeMenu")}>
            <Icon name="close" size={20} />
          </button>
        )}
      </div>

      <div style={{ padding: "4px 12px 6px", display: "flex", flexDirection: "column", gap: 6 }}>
        <WorkspaceSwitcher />
        <SpaceSwitcher />
        <SearchButton />
      </div>

      <nav
        aria-label={tn("mainNavigation")}
        style={{ padding: "6px 12px", display: "flex", flexDirection: "column", gap: 2 }}
      >
        {items.map((n) => {
          const on = pathname === n.href || pathname.startsWith(n.href + "/");
          return (
            <Link
              key={n.href}
              href={n.href}
              aria-current={on ? "page" : undefined}
              onClick={onNavigate}
              className={styles.navLink}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 11,
                padding: "9px 11px",
                borderRadius: 9,
                textDecoration: "none",
                fontSize: 14,
                fontWeight: on ? 600 : 500,
                color: on ? "var(--accent-ink)" : "var(--ink-2)",
                background: on ? "var(--accent-soft)" : "transparent",
                transition: "background .12s, color .12s",
              }}
              onMouseEnter={(e) => {
                if (!on) e.currentTarget.style.background = "var(--hover)";
              }}
              onMouseLeave={(e) => {
                if (!on) e.currentTarget.style.background = "transparent";
              }}
            >
              <Icon name={n.icon} size={19} stroke={on ? 2 : 1.7} />
              {t(n.labelKey)}
            </Link>
          );
        })}
      </nav>

      <div style={{ marginTop: "auto", padding: 12, display: "flex", flexDirection: "column", gap: 10 }}>
        <div style={{ display: "flex", justifyContent: "flex-start" }}>
          <LanguageSelector variant="solid" />
        </div>
        <div
          style={{
            padding: "10px 11px",
            borderRadius: 10,
            background: "var(--surface)",
            border: "1px solid var(--border)",
            display: "flex",
            alignItems: "center",
            gap: 10,
          }}
        >
          <Avatar initials={initialsFrom(me?.username || me?.email)} size={34} />
          <div style={{ minWidth: 0, flex: 1 }}>
            <div
              style={{
                fontSize: 13.5,
                fontWeight: 600,
                color: "var(--ink)",
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
            >
              {me?.username || me?.email || "—"}
            </div>
            <div style={{ fontSize: 12, color: "var(--ink-3)" }}>{tw(ROLE_INFO[role].labelKey)}</div>
          </div>
          <button
            onClick={logout}
            className={styles.signOut}
            aria-label={t("signOut")}
            title={t("signOut")}
            style={{
              border: "none",
              background: "transparent",
              cursor: "pointer",
              color: "var(--ink-3)",
              display: "grid",
              padding: 4,
              borderRadius: 6,
            }}
            onMouseEnter={(e) => (e.currentTarget.style.color = "var(--ink)")}
            onMouseLeave={(e) => (e.currentTarget.style.color = "var(--ink-3)")}
          >
            <Icon name="logout" size={17} />
          </button>
        </div>
      </div>
    </aside>
  );
});

/**
 * Entrée VISIBLE de la palette de recherche : le raccourci clavier n'est jamais le
 * seul moyen de l'ouvrir. Dans la Sidebar, donc aussi dans le tiroir mobile.
 */
function SearchButton() {
  const nav = useShellNav();
  const tn = useTranslations("navigationSearch");
  // Indication du raccourci résolue après montage (pas de différence serveur/client).
  const [shortcut, setShortcut] = useState("Ctrl K");
  useEffect(() => {
    const platform = (typeof navigator !== "undefined" && (navigator.platform || navigator.userAgent)) || "";
    if (/mac|iphone|ipad/i.test(platform)) setShortcut("⌘K");
  }, []);
  if (!nav) return null;
  return (
    <button
      type="button"
      onClick={() => nav.openPalette("all")}
      aria-label={tn("searchButtonAria")}
      aria-keyshortcuts="Control+K Meta+K /"
      style={{
        width: "100%",
        display: "flex",
        alignItems: "center",
        gap: 9,
        padding: "8px 10px",
        borderRadius: 10,
        border: "1px solid var(--border)",
        background: "var(--surface)",
        cursor: "pointer",
        fontFamily: "inherit",
        fontSize: 13,
        color: "var(--ink-3)",
        textAlign: "left",
      }}
    >
      <Icon name="search" size={16} />
      <span style={{ flex: 1 }}>{tn("searchButton")}</span>
      <kbd
        aria-hidden="true"
        style={{
          fontFamily: "inherit",
          fontSize: 11,
          padding: "1px 6px",
          borderRadius: 6,
          border: "1px solid var(--border)",
          color: "var(--ink-3)",
        }}
      >
        {shortcut}
      </kbd>
    </button>
  );
}
