"use client";

import React, { useEffect, useId, useMemo, useRef, useState } from "react";
import { Icon } from "@/components/ui";

/**
 * Liste de résultats de navigation PARTAGÉE : un champ de
 * saisie + une liste groupée, au motif ARIA *combobox* / *listbox*.
 *
 * Montée à deux endroits — le sélecteur d'espace et la palette globale — pour qu'il
 * n'existe qu'un seul comportement clavier et une seule présentation :
 * - ↑ / ↓ déplacent l'option active (`aria-activedescendant`), le focus reste dans
 *   le champ ; Origine / Fin vont à la première / dernière option dès qu'une option
 *   est active (sinon elles déplacent le curseur du texte) ;
 * - Entrée active l'option courante (la première s'il n'y en a pas) ;
 * - Échap délègue à `onEscape` (fermeture du calque appelant).
 *
 * Une option porteuse d'un `href` est un VRAI lien : Ctrl/⌘+clic, Maj+clic et clic
 * du milieu gardent le comportement du navigateur (nouvel onglet/fenêtre) ; seul le
 * clic simple est intercepté pour une navigation applicative.
 */

export type NavResultItem = {
  key: string;
  label: string;
  sublabel?: string | null;
  /** Texte brut affiché tel quel (jamais interprété comme du HTML). */
  snippet?: string | null;
  badge?: string | null;
  href?: string;
  leading?: React.ReactNode;
  trailing?: React.ReactNode;
  selected?: boolean;
  onActivate: () => void;
};

export type NavSection = { id: string; label?: string | null; items: NavResultItem[] };

export type NavStatus =
  | { kind: "loading"; message: string }
  | { kind: "empty"; message: string }
  | { kind: "hint"; message: string }
  | { kind: "error"; message: string; retryLabel?: string; onRetry?: () => void }
  | { kind: "rateLimited"; message: string };

export function NavigationResults({
  query,
  onQueryChange,
  placeholder,
  inputLabel,
  sections,
  status,
  footer,
  onEscape,
  autoFocus = false,
  inputRef,
  maxListHeight,
  testId,
}: {
  query: string;
  onQueryChange: (q: string) => void;
  placeholder: string;
  inputLabel: string;
  sections: NavSection[];
  status?: NavStatus | null;
  footer?: React.ReactNode;
  onEscape?: () => void;
  autoFocus?: boolean;
  inputRef?: React.RefObject<HTMLInputElement>;
  maxListHeight?: number | string;
  testId?: string;
}) {
  const uid = useId().replace(/[^a-zA-Z0-9_-]/g, "");
  const listId = `nav-list-${uid}`;
  const ownRef = useRef<HTMLInputElement>(null);
  const ref = inputRef ?? ownRef;
  const listRef = useRef<HTMLDivElement>(null);

  const flat = useMemo(() => sections.flatMap((s) => s.items), [sections]);
  const flatKeys = flat.map((i) => i.key).join("|");
  const [active, setActive] = useState(-1);

  // Nouvelle liste (saisie, chargement) : aucune option active tant que l'utilisateur
  // n'a pas utilisé les flèches — Entrée prendra la première.
  useEffect(() => setActive(-1), [flatKeys]);

  useEffect(() => {
    if (autoFocus) ref.current?.focus({ preventScroll: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const optionId = (i: number) => `${listId}-opt-${i}`;

  useEffect(() => {
    if (active < 0) return;
    const el = document.getElementById(optionId(active));
    el?.scrollIntoView?.({ block: "nearest" });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active]);

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if ((e.nativeEvent as KeyboardEvent).isComposing) return;
    const n = flat.length;
    switch (e.key) {
      case "ArrowDown":
        if (!n) return;
        e.preventDefault();
        setActive((a) => (a + 1 >= n ? 0 : a + 1));
        return;
      case "ArrowUp":
        if (!n) return;
        e.preventDefault();
        setActive((a) => (a <= 0 ? n - 1 : a - 1));
        return;
      case "Home":
        if (!n || active < 0) return;
        e.preventDefault();
        setActive(0);
        return;
      case "End":
        if (!n || active < 0) return;
        e.preventDefault();
        setActive(n - 1);
        return;
      case "Enter": {
        if (!n) return;
        e.preventDefault();
        flat[active >= 0 ? active : 0]?.onActivate();
        return;
      }
      case "Escape":
        if (onEscape) {
          e.preventDefault();
          e.stopPropagation();
          onEscape();
        }
        return;
    }
  };

  let idx = -1;
  const hasItems = flat.length > 0;

  return (
    <div data-testid={testId} style={{ display: "flex", flexDirection: "column", minHeight: 0, flex: 1 }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          padding: "7px 10px",
          border: "1px solid var(--border)",
          borderRadius: 9,
          background: "var(--surface)",
          margin: "2px 2px 6px",
        }}
      >
        <Icon name="search" size={16} style={{ color: "var(--ink-3)" }} />
        <input
          ref={ref}
          type="text"
          role="combobox"
          aria-label={inputLabel}
          aria-expanded={hasItems}
          aria-controls={listId}
          aria-autocomplete="list"
          aria-activedescendant={active >= 0 ? optionId(active) : undefined}
          autoComplete="off"
          spellCheck={false}
          value={query}
          placeholder={placeholder}
          onChange={(e) => onQueryChange(e.target.value)}
          onKeyDown={onKeyDown}
          style={{
            flex: 1,
            minWidth: 0,
            border: "none",
            outline: "none",
            background: "transparent",
            fontFamily: "inherit",
            fontSize: 14,
            color: "var(--ink)",
          }}
        />
      </div>

      <div
        ref={listRef}
        id={listId}
        role="listbox"
        aria-label={inputLabel}
        style={{ overflowY: "auto", maxHeight: maxListHeight, minHeight: 0, flex: 1 }}
      >
        {sections.map((section) =>
          section.items.length === 0 ? null : (
            <div key={section.id} role="group" aria-label={section.label || undefined}>
              {section.label && (
                <div
                  role="presentation"
                  style={{
                    padding: "8px 9px 4px",
                    fontSize: 11,
                    fontWeight: 700,
                    letterSpacing: "0.06em",
                    textTransform: "uppercase",
                    color: "var(--ink-3)",
                  }}
                >
                  {section.label}
                </div>
              )}
              {section.items.map((item) => {
                idx += 1;
                const i = idx;
                const isActive = i === active;
                const body = (
                  <>
                    {item.leading}
                    <span style={{ flex: 1, minWidth: 0 }}>
                      <span
                        style={{
                          display: "flex",
                          alignItems: "center",
                          gap: 6,
                          fontSize: 13.5,
                          fontWeight: item.selected ? 600 : 500,
                          color: "var(--ink)",
                        }}
                      >
                        <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                          {item.label}
                        </span>
                        {item.badge && (
                          <span
                            style={{
                              flex: "none",
                              fontSize: 10.5,
                              fontWeight: 600,
                              padding: "1px 6px",
                              borderRadius: 6,
                              background: "var(--hover)",
                              color: "var(--ink-3)",
                            }}
                          >
                            {item.badge}
                          </span>
                        )}
                      </span>
                      {item.sublabel && (
                        <span
                          style={{
                            display: "block",
                            fontSize: 11.5,
                            color: "var(--ink-3)",
                            overflow: "hidden",
                            textOverflow: "ellipsis",
                            whiteSpace: "nowrap",
                          }}
                        >
                          {item.sublabel}
                        </span>
                      )}
                      {item.snippet && (
                        <span
                          style={{
                            display: "block",
                            fontSize: 12,
                            color: "var(--ink-2)",
                            overflow: "hidden",
                            textOverflow: "ellipsis",
                            whiteSpace: "nowrap",
                          }}
                        >
                          {item.snippet}
                        </span>
                      )}
                    </span>
                    {item.trailing}
                  </>
                );
                const rowStyle: React.CSSProperties = {
                  width: "100%",
                  display: "flex",
                  alignItems: "center",
                  gap: 10,
                  padding: "8px 9px",
                  borderRadius: 8,
                  textDecoration: "none",
                  color: "inherit",
                  cursor: "pointer",
                  boxSizing: "border-box",
                };
                return (
                  <div
                    key={item.key}
                    id={optionId(i)}
                    role="option"
                    aria-selected={isActive}
                    data-current={item.selected ? "true" : undefined}
                    onMouseDown={(e) => e.preventDefault()}
                    onMouseEnter={() => setActive(i)}
                    onClick={item.href ? undefined : () => item.onActivate()}
                    style={{
                      borderRadius: 8,
                      background: isActive ? "var(--hover)" : item.selected ? "var(--accent-soft)" : "transparent",
                    }}
                  >
                    {item.href ? (
                      <a
                        href={item.href}
                        tabIndex={-1}
                        style={rowStyle}
                        onClick={(e) => {
                          // Nouvel onglet / fenêtre : comportement natif du navigateur.
                          if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
                          e.preventDefault();
                          item.onActivate();
                        }}
                      >
                        {body}
                      </a>
                    ) : (
                      <div style={rowStyle}>{body}</div>
                    )}
                  </div>
                );
              })}
            </div>
          )
        )}
      </div>

      {status && (
        <div
          role="status"
          aria-live="polite"
          data-status={status.kind}
          style={{ padding: "10px 9px", fontSize: 12.5, color: status.kind === "error" || status.kind === "rateLimited" ? "var(--danger, #b42318)" : "var(--ink-3)" }}
        >
          {status.message}
          {status.kind === "error" && status.onRetry && (
            <button
              type="button"
              onClick={status.onRetry}
              style={{
                marginLeft: 8,
                border: "none",
                background: "transparent",
                color: "var(--accent)",
                cursor: "pointer",
                fontFamily: "inherit",
                fontSize: 12.5,
                fontWeight: 600,
              }}
            >
              {status.retryLabel}
            </button>
          )}
        </div>
      )}
      {footer}
    </div>
  );
}
