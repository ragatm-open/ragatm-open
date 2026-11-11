"use client";

import React, { useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { Icon, Popover, RagSpaceRoleBadge } from "@/components/ui";
import { accentForId } from "@/lib/theme";
import { toEffectiveAccess } from "@/lib/ragspacePermissions";
import { rankSpaces } from "@/lib/navigationSearch";
import { visibleIds } from "@/lib/navigationPrefs";
import { useWorkspace, type RagSpaceListItem } from "./WorkspaceContext";
import { NavigationResults, type NavResultItem, type NavSection } from "./NavigationResults";

/** Récents affichés sans saisie (le stockage en garde davantage). */
const RECENTS_SHOWN = 5;

/**
 * Sélecteur d'espace documentaire (RagSpace) — pilote docs/chat/stats.
 *
 * Périmètre : le WORKSPACE COURANT (la recherche multi-workspace vit dans la palette
 * globale). Sans saisie : Favoris, Récents, puis tous les espaces dans l'ordre du
 * backend. Avec saisie : une seule section de résultats classés localement.
 */
export function SpaceSwitcher() {
  const { ragSpaces, ragSpaceId, ragSpace, ragSpaceAccess, currentStats, setRagSpaceId, favorites, recents, toggleFavorite } =
    useWorkspace();
  const t = useTranslations("workspaces");
  const tn = useTranslations("navigationSearch");
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const btnRef = useRef<HTMLButtonElement>(null);
  const cur = ragSpace || ragSpaces[0] || null;
  const docCount = currentStats?.documents.total;

  const close = () => {
    setOpen(false);
    setQuery("");
  };

  const sections = useMemo<NavSection[]>(() => {
    const favIds = visibleIds(favorites, ragSpaces);
    const recIds = visibleIds(recents, ragSpaces);
    const byId = new Map(ragSpaces.map((s) => [s.id, s]));

    const toItem = (s: RagSpaceListItem, section: string): NavResultItem => {
      const active = s.id === ragSpaceId;
      const isFav = favIds.includes(s.id);
      return {
        key: `${section}:${s.id}`,
        label: s.name,
        selected: active,
        onActivate: () => {
          setRagSpaceId(s.id);
          close();
        },
        leading: (
          <span
            style={{
              width: 24,
              height: 24,
              borderRadius: 6,
              flex: "none",
              background: accentForId(s.id),
              display: "grid",
              placeItems: "center",
            }}
          >
            <Icon name="grid" size={13} style={{ color: "#fff" }} />
          </span>
        ),
        trailing: (
          <>
            <RagSpaceRoleBadge access={toEffectiveAccess(s)} size="sm" />
            <button
              type="button"
              aria-pressed={isFav}
              aria-label={tn(isFav ? "removeFavorite" : "addFavorite", { name: s.name })}
              title={tn(isFav ? "removeFavorite" : "addFavorite", { name: s.name })}
              onMouseDown={(e) => e.preventDefault()}
              onClick={(e) => {
                e.stopPropagation();
                toggleFavorite(s.id);
              }}
              style={{
                border: "none",
                background: "transparent",
                cursor: "pointer",
                padding: 2,
                display: "grid",
                color: isFav ? "var(--accent)" : "var(--ink-3)",
              }}
            >
              <Icon name="star" size={15} fill={isFav ? "currentColor" : "none"} />
            </button>
            {active && <Icon name="check" size={16} style={{ color: "var(--accent)" }} stroke={2.4} />}
          </>
        ),
      };
    };

    if (query.trim()) {
      const ranked = rankSpaces(ragSpaces, query, { favorites: favIds, recents: recIds });
      return [{ id: "results", label: tn("sections.results"), items: ranked.map((s) => toItem(s, "results")) }];
    }
    const pick = (ids: string[]) => ids.map((id) => byId.get(id)).filter(Boolean) as RagSpaceListItem[];
    return [
      { id: "favorites", label: tn("sections.favorites"), items: pick(favIds).map((s) => toItem(s, "favorites")) },
      {
        id: "recents",
        label: tn("sections.recents"),
        items: pick(recIds.filter((id) => !favIds.includes(id)).slice(0, RECENTS_SHOWN)).map((s) => toItem(s, "recents")),
      },
      { id: "all", label: tn("sections.allSpaces"), items: ragSpaces.map((s) => toItem(s, "all")) },
    ];
    // `close` ne dépend que de setters stables.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ragSpaces, ragSpaceId, favorites, recents, query, setRagSpaceId, toggleFavorite, tn]);

  const noMatch = query.trim() !== "" && sections.every((s) => s.items.length === 0);

  return (
    <div style={{ position: "relative" }}>
      <button
        ref={btnRef}
        onClick={() => (open ? close() : setOpen(true))}
        aria-haspopup="dialog"
        aria-expanded={open}
        disabled={ragSpaces.length === 0}
        style={{
          width: "100%",
          display: "flex",
          alignItems: "center",
          gap: 10,
          padding: "9px 10px",
          borderRadius: 10,
          border: "1px solid var(--border)",
          background: "var(--surface)",
          cursor: ragSpaces.length ? "pointer" : "default",
          fontFamily: "inherit",
          textAlign: "left",
        }}
      >
        <span
          style={{
            width: 26,
            height: 26,
            borderRadius: 7,
            flex: "none",
            background: cur ? accentForId(cur.id) : "var(--ink-3)",
            display: "grid",
            placeItems: "center",
          }}
        >
          <Icon name="grid" size={14} style={{ color: "#fff" }} />
        </span>
        <span style={{ flex: 1, minWidth: 0 }}>
          <span
            style={{
              display: "block",
              fontSize: 13.5,
              fontWeight: 600,
              color: "var(--ink)",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {cur ? cur.name : t("noSpace")}
          </span>
          <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <span style={{ fontSize: 11.5, color: "var(--ink-3)" }}>
              {docCount != null ? t("documentsCount", { count: docCount }) : "—"}
            </span>
            {cur && <RagSpaceRoleBadge access={ragSpaceAccess} size="sm" />}
          </span>
        </span>
        <Icon name="chevronD" size={16} style={{ color: "var(--ink-3)" }} />
      </button>

      <Popover open={open} onClose={close} anchorRef={btnRef} matchWidth minWidth={260} maxHeight={420} role="dialog">
        <NavigationResults
          query={query}
          onQueryChange={setQuery}
          placeholder={tn("spacePlaceholder")}
          inputLabel={tn("spaceInputLabel")}
          sections={sections}
          autoFocus
          onEscape={close}
          maxListHeight={320}
          status={noMatch ? { kind: "empty", message: tn("noSpaceMatch", { query: query.trim() }) } : null}
          footer={
            <div style={{ padding: "6px 9px 2px", fontSize: 11, color: "var(--ink-3)" }}>{tn("favoritesNote")}</div>
          }
          testId="space-switcher-results"
        />
      </Popover>
    </div>
  );
}
