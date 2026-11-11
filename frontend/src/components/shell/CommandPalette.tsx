"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useRouter } from "next/navigation";
import { useLocale, useTranslations } from "next-intl";
import { Icon } from "@/components/ui";
import { ApiError } from "@/lib/apiError";
import { accentForId } from "@/lib/theme";
import { visibleIds } from "@/lib/navigationPrefs";
import {
  THREAD_QUERY_MAX,
  THREAD_QUERY_MIN,
  THREAD_SEARCH_DEBOUNCE_MS,
  rankSpaces,
  searchThreads,
  threadHref,
  type ThreadSearchResult,
} from "@/lib/navigationSearch";
import { useWorkspace, type RagSpaceListItem } from "./WorkspaceContext";
import { NavigationResults, type NavResultItem, type NavSection, type NavStatus } from "./NavigationResults";
import { MOBILE_QUERY, useMediaQuery } from "./breakpoints";
import { useModalLayer } from "./useModalLayer";
import type { PaletteTab } from "./shellNav";

/** Espaces montrés dans l'onglet « Tous » (l'onglet « Espaces » en montre 50). */
const SPACES_IN_ALL = 6;

type ThreadState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "ready" }
  | { kind: "error" }
  | { kind: "rateLimited" }
  | { kind: "timeout" }
  | { kind: "tooLong" };

/**
 * Palette de recherche globale.
 *
 * - Espaces : TOUS les workspaces actifs, filtrés localement et immédiatement.
 * - Conversations : `GET /api/v1/chat/threads/search/` après 2 caractères et 250 ms
 *   de pause ; la requête précédente est annulée (AbortController). Un 429 affiche un
 *   état dédié — jamais de déconnexion, jamais de nouvel essai automatique.
 * - Ouvrir un espace : `openTarget` (bascule de workspace si besoin).
 * - Ouvrir une conversation : navigation vers `/chat?thread=<uuid>` — la palette ne
 *   lit JAMAIS le contenu d'un fil ; c'est la page Assistant qui le charge.
 */
export function CommandPalette({
  open,
  onClose,
  initialTab = "all",
}: {
  open: boolean;
  onClose: () => void;
  initialTab?: PaletteTab;
}) {
  const router = useRouter();
  const locale = useLocale();
  const tn = useTranslations("navigationSearch");
  const {
    workspaces,
    workspaceId,
    ragSpaces,
    ragSpaceId,
    favorites,
    recents,
    openTarget,
    allRagSpaces,
    loadAllRagSpaces,
  } = useWorkspace();
  const isMobile = useMediaQuery(MOBILE_QUERY);

  const overlayRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const [tab, setTab] = useState<PaletteTab>(initialTab);
  const [query, setQuery] = useState("");
  const [threads, setThreads] = useState<ThreadSearchResult[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [threadState, setThreadState] = useState<ThreadState>({ kind: "idle" });
  const [loadingMore, setLoadingMore] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const ctrlRef = useRef<AbortController | null>(null);

  useModalLayer({ active: open, containerRef: overlayRef, onClose, initialFocusRef: inputRef });

  // Catalogue multi-workspace : une seule fois par ouverture (les autorisations
  // peuvent avoir changé depuis la précédente).
  useEffect(() => {
    if (open) void loadAllRagSpaces();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // Espaces candidats : catalogue global si chargé, sinon l'espace courant seul.
  const spaces: RagSpaceListItem[] = useMemo(() => {
    if (allRagSpaces && allRagSpaces.length) return allRagSpaces;
    return ragSpaces.map((s) => ({ ...s, workspace_id: s.workspace_id ?? workspaceId ?? undefined }));
  }, [allRagSpaces, ragSpaces, workspaceId]);

  const wsName = useMemo(() => new Map(workspaces.map((w) => [w.id, w.name])), [workspaces]);
  const showWorkspace = workspaces.length >= 2;

  const trimmed = query.trim();
  const wantsThreads = tab !== "spaces" && trimmed.length >= THREAD_QUERY_MIN;

  // Recherche des conversations : debounce + annulation de la requête précédente.
  useEffect(() => {
    ctrlRef.current?.abort();
    ctrlRef.current = null;
    if (!open || !wantsThreads) {
      setThreads([]);
      setNext(null);
      setThreadState({ kind: "idle" });
      return;
    }
    if (trimmed.length > THREAD_QUERY_MAX) {
      setThreads([]);
      setNext(null);
      setThreadState({ kind: "tooLong" });
      return;
    }
    setThreadState({ kind: "loading" });
    const ctrl = new AbortController();
    const timer = window.setTimeout(() => {
      ctrlRef.current = ctrl;
      searchThreads(trimmed, { signal: ctrl.signal })
        .then((res) => {
          if (ctrl.signal.aborted) return;
          setThreads(res?.results ?? []);
          setNext(res?.next ?? null);
          setThreadState({ kind: "ready" });
        })
        .catch((e: unknown) => {
          if (ctrl.signal.aborted || (e as { name?: string })?.name === "AbortError") return;
          setThreads([]);
          setNext(null);
          setThreadState(classifyError(e));
        });
    }, THREAD_SEARCH_DEBOUNCE_MS);
    return () => {
      window.clearTimeout(timer);
      ctrl.abort();
    };
  }, [open, wantsThreads, trimmed, attempt]);

  const loadMore = useCallback(async () => {
    if (!next || loadingMore) return;
    setLoadingMore(true);
    try {
      const res = await searchThreads(trimmed, { cursor: next });
      setThreads((prev) => {
        const seen = new Set(prev.map((r) => r.id));
        return [...prev, ...(res?.results ?? []).filter((r) => !seen.has(r.id))];
      });
      setNext(res?.next ?? null);
    } catch (e) {
      setThreadState(classifyError(e));
    } finally {
      setLoadingMore(false);
    }
  }, [next, loadingMore, trimmed]);

  const openSpace = useCallback(
    (s: RagSpaceListItem) => {
      const wid = s.workspace_id ?? workspaceId;
      if (wid === null || wid === undefined) return;
      openTarget({ workspaceId: wid, ragSpaceId: s.id });
      onClose();
    },
    [openTarget, workspaceId, onClose]
  );

  const openThread = useCallback(
    (r: ThreadSearchResult) => {
      onClose();
      router.push(threadHref(r.id));
    },
    [onClose, router]
  );

  const sections = useMemo<NavSection[]>(() => {
    const favIds = visibleIds(favorites, spaces);
    const recIds = visibleIds(recents, spaces);
    const byId = new Map(spaces.map((s) => [s.id, s]));

    const spaceItem = (s: RagSpaceListItem, section: string): NavResultItem => ({
      key: `${section}:${s.id}`,
      label: s.name,
      sublabel: showWorkspace && s.workspace_id !== undefined ? wsName.get(s.workspace_id) ?? null : null,
      selected: s.id === ragSpaceId,
      onActivate: () => openSpace(s),
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
          <Icon name={favIds.includes(s.id) ? "star" : "grid"} size={13} style={{ color: "#fff" }} />
        </span>
      ),
    });

    const threadItem = (r: ThreadSearchResult): NavResultItem => {
      const where = showWorkspace ? `${r.rag_space_name} · ${r.workspace_name}` : r.rag_space_name;
      return {
        key: `thread:${r.id}`,
        label: r.title?.trim() ? r.title : tn("untitled"),
        sublabel: `${where} · ${formatDate(r.updated_at, locale)}`,
        snippet: r.snippet,
        badge: r.match === "title" ? tn("matchTitle") : tn("matchContent"),
        href: threadHref(r.id),
        onActivate: () => openThread(r),
        leading: <Icon name="chat" size={17} style={{ color: "var(--ink-3)" }} />,
      };
    };

    const out: NavSection[] = [];
    if (!trimmed) {
      if (tab !== "conversations") {
        const pick = (ids: string[]) => ids.map((id) => byId.get(id)).filter(Boolean) as RagSpaceListItem[];
        out.push({ id: "favorites", label: tn("sections.favorites"), items: pick(favIds).map((s) => spaceItem(s, "favorites")) });
        out.push({
          id: "recents",
          label: tn("sections.recents"),
          items: pick(recIds.filter((id) => !favIds.includes(id))).map((s) => spaceItem(s, "recents")),
        });
        if (tab === "spaces") {
          out.push({ id: "all", label: tn("sections.allSpaces"), items: spaces.slice(0, 50).map((s) => spaceItem(s, "all")) });
        }
      }
      return out;
    }
    if (tab !== "conversations") {
      const ranked = rankSpaces(spaces, trimmed, { favorites: favIds, recents: recIds }).slice(
        0,
        tab === "all" ? SPACES_IN_ALL : undefined
      );
      out.push({ id: "spaces", label: tn("sections.spaceResults"), items: ranked.map((s) => spaceItem(s, "spaces")) });
    }
    if (tab !== "spaces") {
      out.push({ id: "conversations", label: tn("sections.conversationResults"), items: threads.map(threadItem) });
    }
    return out;
  }, [favorites, recents, spaces, showWorkspace, wsName, ragSpaceId, openSpace, tn, trimmed, tab, threads, locale, openThread]);

  const status: NavStatus | null = useMemo(() => {
    const anyItems = sections.some((s) => s.items.length > 0);
    if (tab !== "spaces") {
      switch (threadState.kind) {
        case "loading":
          return { kind: "loading", message: tn("loading") };
        case "rateLimited":
          return { kind: "rateLimited", message: tn("rateLimited") };
        case "timeout":
          return { kind: "error", message: tn("timeout") };
        case "tooLong":
          return { kind: "error", message: tn("queryTooLong") };
        case "error":
          return { kind: "error", message: tn("error"), retryLabel: tn("retry"), onRetry: () => setAttempt((n) => n + 1) };
      }
    }
    if (!trimmed) {
      if (tab === "conversations" || !anyItems) return { kind: "hint", message: tn("typeToSearchConversations") };
      return null;
    }
    if (tab === "conversations" && trimmed.length < THREAD_QUERY_MIN) {
      return { kind: "hint", message: tn("typeToSearchConversations") };
    }
    if (!anyItems && (tab === "spaces" || threadState.kind === "ready")) {
      return { kind: "empty", message: tn("noResults", { query: trimmed }) };
    }
    return null;
  }, [sections, tab, threadState, trimmed, tn]);

  if (!open || typeof document === "undefined") return null;

  const tabs: PaletteTab[] = ["all", "spaces", "conversations"];

  return createPortal(
    <div
      ref={overlayRef}
      data-testid="command-palette"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 1100,
        background: "rgba(40, 28, 18, 0.34)",
        display: "flex",
        alignItems: isMobile ? "stretch" : "flex-start",
        justifyContent: "center",
        padding: isMobile ? 0 : "12vh 16px 16px",
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={tn("paletteTitle")}
        style={{
          width: isMobile ? "100%" : "min(640px, 100%)",
          height: isMobile ? "100%" : undefined,
          maxHeight: isMobile ? "100%" : "72vh",
          display: "flex",
          flexDirection: "column",
          background: "var(--surface)",
          border: isMobile ? "none" : "1px solid var(--border)",
          borderRadius: isMobile ? 0 : 14,
          boxShadow: "var(--shadow-pop)",
          padding: isMobile ? "max(10px, env(safe-area-inset-top)) 12px 12px" : 12,
          boxSizing: "border-box",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, marginBottom: 6 }}>
          <div role="tablist" aria-label={tn("tabsLabel")} style={{ display: "flex", gap: 4 }}>
            {tabs.map((k) => (
              <button
                key={k}
                type="button"
                role="tab"
                aria-selected={tab === k}
                onClick={() => {
                  setTab(k);
                  inputRef.current?.focus({ preventScroll: true });
                }}
                style={{
                  border: "1px solid var(--border)",
                  borderRadius: 8,
                  padding: "5px 10px",
                  fontFamily: "inherit",
                  fontSize: 12.5,
                  fontWeight: 600,
                  cursor: "pointer",
                  background: tab === k ? "var(--accent-soft)" : "transparent",
                  color: tab === k ? "var(--accent-ink)" : "var(--ink-2)",
                }}
              >
                {tn(`tabs.${k}`)}
              </button>
            ))}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label={tn("close")}
            title={tn("close")}
            style={{
              border: "none",
              background: "transparent",
              cursor: "pointer",
              color: "var(--ink-2)",
              display: "grid",
              placeItems: "center",
              width: 36,
              height: 36,
              borderRadius: 8,
            }}
          >
            <Icon name="close" size={20} />
          </button>
        </div>

        <NavigationResults
          query={query}
          onQueryChange={setQuery}
          placeholder={tn("palettePlaceholder")}
          inputLabel={tn("paletteTitle")}
          sections={sections}
          status={status}
          inputRef={inputRef}
          onEscape={onClose}
          testId="palette-results"
          footer={
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, paddingTop: 6 }}>
              {next && tab !== "spaces" ? (
                <button
                  type="button"
                  onClick={() => void loadMore()}
                  disabled={loadingMore}
                  style={{
                    border: "none",
                    background: "transparent",
                    color: "var(--accent)",
                    cursor: "pointer",
                    fontFamily: "inherit",
                    fontSize: 13,
                    fontWeight: 600,
                    padding: "6px 9px",
                  }}
                >
                  {tn("showMore")}
                </button>
              ) : (
                <span />
              )}
              {!isMobile && <span style={{ fontSize: 11, color: "var(--ink-3)" }}>{tn("keyboardHelp")}</span>}
            </div>
          }
        />
      </div>
    </div>,
    document.body
  );
}

function classifyError(e: unknown): ThreadState {
  if (e instanceof ApiError) {
    if (e.status === 429) return { kind: "rateLimited" };
    if (e.status === 503 && e.code === "search_timeout") return { kind: "timeout" };
    if (e.status === 400 && e.code === "q_too_long") return { kind: "tooLong" };
  }
  return { kind: "error" };
}

function formatDate(iso: string, locale: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  try {
    return d.toLocaleDateString(locale, { day: "numeric", month: "short", year: "numeric" });
  } catch {
    return d.toISOString().slice(0, 10);
  }
}
