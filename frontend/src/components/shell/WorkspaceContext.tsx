"use client";

import React, { createContext, useCallback, useContext, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { apiFetch, bootstrap } from "@/lib/api";
import { guardedCall, type GuardResult } from "@/lib/authProbe";
import { accentForId, applyAccent } from "@/lib/theme";
import { upsertRagSpaceInList } from "./ragSpaceList";
import {
  getFavorites,
  getRecents,
  pushRecent,
  toggleFavorite as toggleStoredFavorite,
} from "@/lib/navigationPrefs";
import {
  toEffectiveAccess,
  canWriteRagSpace,
  canManageRagSpace,
  type PermissionLevel,
  type SpaceRole,
  type EffectiveRagSpaceAccess,
} from "@/lib/ragspacePermissions";
import {
  getStoredWorkspaceId,
  setStoredWorkspaceId,
  getStoredRagSpaceIdForWorkspace,
  setStoredRagSpaceIdForWorkspace,
  setStoredThreadIdForRagSpace,
} from "@/app/chat/_lib/storage";

export type WorkspaceListItem = {
  id: number;
  name: string;
  role: string;
  // Enrichissements navigation multi-workspace (backend-authoritative).
  workspace_is_active?: boolean;
  membership_id?: number;
  effective_owner_scope_id?: number | null;
};
export type RagSpaceListItem = {
  id: string;
  name: string;
  /** Présent dans le catalogue multi-workspace (`GET /api/v1/rag-spaces/`). */
  workspace_id?: number;
  llm_provider?: string;
  llm_model?: string;
  embedding_provider?: string;
  embedding_model?: string;
  // Rôle EFFECTIF de l'appelant sur cet espace (lecture seule, décidé par le
  // backend = autorité). Piloté par ragspacePermissions.ts. Flag-aware.
  permission_level?: PermissionLevel | null;
  space_role?: SpaceRole | null;
  is_owner_implicit?: boolean;
  /** dépôt audio autorisé pour CET espace (flag ET liste d'espaces). */
  audio_upload_enabled?: boolean;
};
export type Me = {
  id: string;
  email: string;
  username: string;
  is_ragatm_owner?: boolean;
  /** Superuser plateforme actif (gating UX ; backend reste autoritaire). */
  is_superuser?: boolean;
};

/** Périmètre réellement analysé. Voir docs/coverage-semantics.md.
 *  `coverage_complete=false` interdit d'employer le mot « exhaustif » dans l'UI. */
export type Coverage = {
  scope: {
    rag_space_id: string;
    generated_at: string;
    corpus_fingerprint: string;
    filters_applied: string[];
  };
  documents_total: number;
  documents_considered: number;
  documents_analyzed: number;
  documents_with_results: number;
  documents_excluded: { reason: string; count: number; label: string }[];
  pages_analyzed: number;
  chunks_analyzed: number;
  items_extracted: number;
  items_without_source: number;
  coverage_level: "A" | "B" | "C" | "D";
  coverage_complete: boolean;
  coverage_ratio: number;
  limits_applied: string[];
};

export type RagSpaceStats = {
  rag_space_id: string;
  documents: {
    total: number;
    ready: number;
    pending: number;
    ingesting: number;
    failed: number;
    other: number;
    // Additifs : un document « prêt » sans extrait n'est pas analysable.
    with_chunks?: number;
    without_chunks?: number;
  };
  chunks_total?: number;
  indexing: { in_progress: boolean; in_progress_count: number };
  last_created_at: string | null;
  // --- Blocs additifs (optionnels : un backend antérieur ne les envoie pas) ---
  content?: {
    chunks_total: number;
    pages_numbered_total: number;
    documents_without_page_numbers: number;
    characters_total: number;
    estimated_tokens_total: number;
  };
  ratios?: { ready: number; failed: number };
  scope?: { generated_at: string; corpus_fingerprint: string };
  coverage?: Coverage;
};

// Mapping rôles backend (owner/admin/member/viewer) → vocabulaire produit.
export type ProductRole = "admin" | "editeur" | "lecteur";

export function toProductRole(role: string | undefined | null): ProductRole {
  const r = (role || "").toLowerCase();
  if (r === "owner" || r === "admin") return "admin";
  if (r === "member" || r === "editor" || r === "editeur") return "editeur";
  return "lecteur";
}

// `labelKey`/`descKey` = clés i18n (namespace `workspaces`), traduites au rendu par
// les consommateurs (ex. Sidebar via useTranslations("workspaces")).
export const ROLE_INFO: Record<ProductRole, { labelKey: string; descKey: string; color: string }> = {
  admin: { labelKey: "administrator", descKey: "administratorDesc", color: "#C2562F" },
  editeur: { labelKey: "contributor", descKey: "contributorDesc", color: "#0E7C86" },
  lecteur: { labelKey: "viewer", descKey: "viewerDesc", color: "#5B53D9" },
};

type Ctx = {
  me: Me | null;
  workspaces: WorkspaceListItem[];
  workspaceId: number | null;
  role: ProductRole;
  canImport: boolean;
  isAdmin: boolean;
  /** Propriétaire global ragATM (administration des corpus métier). UX only. */
  isGlobalOwner: boolean;
  /** Superuser plateforme (console d'administration globale). UX only. */
  isSuperuser: boolean;
  ragSpaces: RagSpaceListItem[];
  ragSpaceId: string;
  ragSpace: RagSpaceListItem | null;
  /** Rôle EFFECTIF de l'appelant sur l'espace COURANT (décidé par le backend).
   *  Restrictif si l'espace/rôle est inconnu. Voir lib/ragspacePermissions.ts. */
  ragSpaceAccess: EffectiveRagSpaceAccess;
  /** Écriture autorisée sur l'espace courant (import, suppression, réindex). */
  canWriteCurrent: boolean;
  /** Gestion autorisée sur l'espace courant (réglages, accès). */
  canManageCurrent: boolean;
  /** Rôle effectif d'un espace arbitraire de la liste (restrictif si absent). */
  accessForRagSpace: (id: string) => EffectiveRagSpaceAccess;
  currentStats: RagSpaceStats | null;
  loading: boolean;
  /** Échec transitoire du chargement (jamais un 401). `null` si tout va bien. */
  authIssue: AuthIssue | null;
  /** Relance MANUELLE du chargement après un `authIssue` (action utilisateur). */
  retryAuth: () => void;
  setWorkspaceId: (id: number) => void;
  setRagSpaceId: (id: string) => void;
  /** Ajoute ou remplace un espace dans la liste partagée (idempotent par id).
   *  Utilisé après création pour un affichage immédiat dans le sélecteur. */
  upsertRagSpace: (space: RagSpaceListItem) => void;
  /** Revalidation silencieuse de la liste des espaces du workspace courant.
   *  N'écrase PAS la liste existante en cas d'échec réseau. */
  refreshRagSpaces: () => Promise<RagSpaceListItem[]>;
  refreshStats: () => void;
  logout: () => void;
  /** Favoris de l'utilisateur courant (ids, localStorage de cet appareil). */
  favorites: string[];
  /** Espaces récemment ouverts (ids, plus récent d'abord, localStorage). */
  recents: string[];
  toggleFavorite: (spaceId: string) => void;
  /**
   * Ouvre un espace — et, si donné, un fil — dans N'IMPORTE QUEL workspace actif de
   * l'utilisateur. Pré-écrit les mémoires existantes (espace par workspace, fil par
   * espace) avant de basculer : aucun état intermédiaire du workspace précédent.
   * Rend `false` si le workspace n'est pas dans la liste des workspaces actifs.
   */
  openTarget: (target: { workspaceId: number; ragSpaceId: string; threadId?: string | null }) => boolean;
  /** Catalogue des espaces de TOUS les workspaces actifs (chargé à la demande). */
  allRagSpaces: RagSpaceListItem[] | null;
  loadAllRagSpaces: () => Promise<RagSpaceListItem[]>;
};

/**
 * Incident transitoire du chargement de la coquille (garde `/auth/me/` ou liste des
 * workspaces). Ce N'EST PAS une perte d'authentification : la session reste ce
 * qu'elle est côté serveur, l'utilisateur n'est pas renvoyé vers /login.
 * - `rate_limited` : 429 persistant après l'unique réessai ; `retryAfterMs` = délai annoncé.
 * - `unavailable`  : 403, 5xx ou erreur réseau (`status` null).
 */
export type AuthIssue =
  | { kind: "rate_limited"; retryAfterMs: number }
  | { kind: "unavailable"; status: number | null };

const WorkspaceCtx = createContext<Ctx | null>(null);

export function useWorkspace(): Ctx {
  const v = useContext(WorkspaceCtx);
  if (!v) throw new Error("useWorkspace doit être utilisé dans <WorkspaceProvider>");
  return v;
}

export function WorkspaceProvider({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const [me, setMe] = useState<Me | null>(null);
  const [workspaces, setWorkspaces] = useState<WorkspaceListItem[]>([]);
  const [workspaceId, setWsId] = useState<number | null>(null);
  const [ragSpaces, setRagSpaces] = useState<RagSpaceListItem[]>([]);
  const [ragSpaceId, setRsId] = useState<string>("");
  const [currentStats, setCurrentStats] = useState<RagSpaceStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [authIssue, setAuthIssue] = useState<AuthIssue | null>(null);
  // Incrémenté par `retryAuth` : relance l'effet de chargement, une fois par clic.
  const [authAttempt, setAuthAttempt] = useState(0);
  const [favorites, setFavorites] = useState<string[]>([]);
  const [recents, setRecents] = useState<string[]>([]);
  const [allRagSpaces, setAllRagSpaces] = useState<RagSpaceListItem[] | null>(null);

  const role = toProductRole(workspaces.find((w) => w.id === workspaceId)?.role);
  const isAdmin = role === "admin";
  const isGlobalOwner = Boolean(me?.is_ragatm_owner);
  const isSuperuser = Boolean(me?.is_superuser);
  const canImport = role === "admin" || role === "editeur";
  const ragSpace = ragSpaces.find((r) => r.id === ragSpaceId) || null;
  // Rôle EFFECTIF (backend-authoritative). Flags OFF => repli legacy identique au
  // rôle workspace ; flags ON => précision par espace. Restrictif si inconnu.
  const ragSpaceAccess = toEffectiveAccess(ragSpace);
  const canWriteCurrent = canWriteRagSpace(ragSpaceAccess);
  const canManageCurrent = canManageRagSpace(ragSpaceAccess);
  const accessForRagSpace = useCallback(
    (id: string) => toEffectiveAccess(ragSpaces.find((r) => r.id === id)),
    [ragSpaces]
  );

  const logout = useCallback(() => {
    // Logout best-effort côté serveur (révoque le token + supprime le cookie),
    // puis retour login. Aucun token côté JS à nettoyer (cookie HttpOnly).
    apiFetch("/api/v1/auth/logout/", { method: "POST" }).catch(() => {});
    router.replace("/login");
  }, [router]);

  // Chargement initial : /me est la source de vérité d'authentification
  // (cookie HttpOnly illisible en JS).
  //
  // Contrat (cf. lib/authProbe.ts) : SEUL un 401 renvoie vers /login. Un 429 attend
  // le `Retry-After` puis réessaie une fois ; 403, 5xx et erreurs réseau exposent un
  // état transitoire (`authIssue`) sans jamais déclarer l'utilisateur déconnecté.
  // Incident staging du 08/10/2026 : l'ancien `catch {}` renvoyait vers /login sur
  // un 429 de /auth/me/ alors que la session serveur était valide.
  useEffect(() => {
    const ctrl = new AbortController();
    const { signal } = ctrl;

    /** Applique un résultat non-`ok`. Renvoie `true` si le chargement doit s'arrêter. */
    const stopOn = (r: GuardResult<unknown>): boolean => {
      if (r.kind === "ok") return false;
      if (r.kind === "aborted") return true;
      if (r.kind === "unauthenticated") router.replace("/login");
      else if (r.kind === "rate_limited") setAuthIssue({ kind: "rate_limited", retryAfterMs: r.retryAfterMs });
      else setAuthIssue({ kind: "unavailable", status: r.status });
      setLoading(false);
      return true;
    };

    (async () => {
      setLoading(true);
      setAuthIssue(null);

      const meRes = await guardedCall(() => apiFetch<Me>("/api/v1/auth/me/", { signal }), signal);
      if (meRes.kind !== "ok") {
        stopOn(meRes);
        return;
      }
      setMe(meRes.value);

      try {
        await bootstrap();
      } catch {
        /* idempotent — on continue */
      }
      if (signal.aborted) return;

      const wsRes = await guardedCall(
        () => apiFetch<WorkspaceListItem[]>("/api/v1/workspaces/", { signal }),
        signal
      );
      if (wsRes.kind !== "ok") {
        stopOn(wsRes);
        return;
      }
      const ws = wsRes.value;
      setWorkspaces(ws);
      const stored = getStoredWorkspaceId();
      const chosen = stored !== null && ws.some((w) => w.id === stored) ? stored : ws[0]?.id ?? null;
      setWsId(chosen);
      if (chosen !== null) setStoredWorkspaceId(chosen);
      setLoading(false);
    })();
    return () => ctrl.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [authAttempt]);

  const retryAuth = useCallback(() => setAuthAttempt((n) => n + 1), []);

  // Chargement des RagSpaces du workspace courant.
  useEffect(() => {
    if (workspaceId === null) return;
    let alive = true;
    (async () => {
      const list = await apiFetch<RagSpaceListItem[]>(
        `/api/v1/rag-spaces/?workspace_id=${encodeURIComponent(String(workspaceId))}`
      ).catch(() => [] as RagSpaceListItem[]);
      if (!alive) return;
      setRagSpaces(list);
      const storedRs = getStoredRagSpaceIdForWorkspace(workspaceId);
      const next = storedRs && list.some((r) => r.id === storedRs) ? storedRs : list[0]?.id ?? "";
      setRsId(next);
      if (next) setStoredRagSpaceIdForWorkspace(workspaceId, next);
    })();
    return () => {
      alive = false;
    };
  }, [workspaceId]);

  // Stats + accent thématique du RagSpace courant.
  const refreshStats = useCallback(() => {
    if (!ragSpaceId) {
      setCurrentStats(null);
      return;
    }
    apiFetch<RagSpaceStats>(`/api/v1/rag-spaces/${encodeURIComponent(ragSpaceId)}/stats/`)
      .then(setCurrentStats)
      .catch(() => setCurrentStats(null));
  }, [ragSpaceId]);

  useEffect(() => {
    applyAccent(accentForId(ragSpaceId));
    refreshStats();
  }, [ragSpaceId, refreshStats]);

  const setWorkspaceId = useCallback((id: number) => {
    setWsId(id);
    setStoredWorkspaceId(id);
    // Vide IMMÉDIATEMENT les données sensibles du workspace précédent (RagSpaces,
    // espace courant, stats) : elles ne doivent jamais rester affichées pendant le
    // rechargement. La liste des RagSpaces du nouveau workspace est rechargée par
    // l'effet sur [workspaceId] ; le rôle effectif se dérive de `workspaces`.
    setRagSpaces([]);
    setRsId("");
    setCurrentStats(null);
  }, []);

  const setRagSpaceId = useCallback(
    (id: string) => {
      setRsId(id);
      if (workspaceId !== null && id) setStoredRagSpaceIdForWorkspace(workspaceId, id);
    },
    [workspaceId]
  );

  // Ajout/remplacement idempotent d'un espace dans la liste partagée (source unique
  // du sélecteur). Après création : affichage immédiat sans refetch ni reload.
  const upsertRagSpace = useCallback((space: RagSpaceListItem) => {
    setRagSpaces((prev) => upsertRagSpaceInList(prev, space));
  }, []);

  // Revalidation silencieuse (remplace l'objet optimiste par la version complète du
  // listing). En cas d'échec réseau, on NE vide PAS la liste : l'espace créé reste
  // visible et sélectionné.
  const refreshRagSpaces = useCallback(async (): Promise<RagSpaceListItem[]> => {
    if (workspaceId === null) return [];
    try {
      const list = await apiFetch<RagSpaceListItem[]>(
        `/api/v1/rag-spaces/?workspace_id=${encodeURIComponent(String(workspaceId))}`
      );
      setRagSpaces(list);
      return list;
    } catch {
      return [];
    }
  }, [workspaceId]);

  // Préférences de navigation : relues à chaque changement d'utilisateur (clés par id).
  const meId = me?.id ?? null;
  useEffect(() => {
    setFavorites(getFavorites(meId));
    setRecents(getRecents(meId));
  }, [meId]);

  // Récents : tout espace effectivement ouvert (sélection, création, lien direct).
  useEffect(() => {
    if (!meId || !ragSpaceId) return;
    setRecents(pushRecent(meId, ragSpaceId));
  }, [meId, ragSpaceId]);

  const toggleFavorite = useCallback(
    (spaceId: string) => {
      if (!meId) return;
      setFavorites(toggleStoredFavorite(meId, spaceId));
    },
    [meId]
  );

  const openTarget = useCallback(
    ({ workspaceId: wid, ragSpaceId: rsId, threadId }: { workspaceId: number; ragSpaceId: string; threadId?: string | null }) => {
      if (!rsId || !workspaces.some((w) => w.id === wid)) return false;
      // Mémoires existantes d'abord : le chargement de la liste du workspace cible
      // (effet sur [workspaceId]) et celui des fils (page chat) les relisent.
      if (threadId) setStoredThreadIdForRagSpace(rsId, threadId);
      setStoredRagSpaceIdForWorkspace(wid, rsId);
      if (wid !== workspaceId) {
        setWorkspaceId(wid);
      } else if (rsId !== ragSpaceId) {
        setRsId(rsId);
      }
      return true;
    },
    [workspaces, workspaceId, ragSpaceId, setWorkspaceId]
  );

  // Catalogue multi-workspace (palette globale). Le backend n'y met que les espaces
  // des workspaces ACTIFS lisibles par l'appelant ; on recroise quand même avec la
  // liste des workspaces connus (défense en profondeur, jamais un élargissement).
  const loadAllRagSpaces = useCallback(async (): Promise<RagSpaceListItem[]> => {
    try {
      const list = await apiFetch<RagSpaceListItem[]>("/api/v1/rag-spaces/");
      const active = new Set(workspaces.map((w) => w.id));
      const filtered = (Array.isArray(list) ? list : []).filter(
        (r) => r.workspace_id === undefined || active.has(r.workspace_id)
      );
      setAllRagSpaces(filtered);
      return filtered;
    } catch {
      return [];
    }
  }, [workspaces]);

  const value: Ctx = {
    me,
    workspaces,
    workspaceId,
    role,
    canImport,
    isAdmin,
    isGlobalOwner,
    isSuperuser,
    ragSpaces,
    ragSpaceId,
    ragSpace,
    ragSpaceAccess,
    canWriteCurrent,
    canManageCurrent,
    accessForRagSpace,
    currentStats,
    loading,
    authIssue,
    retryAuth,
    setWorkspaceId,
    setRagSpaceId,
    upsertRagSpace,
    refreshRagSpaces,
    refreshStats,
    logout,
    favorites,
    recents,
    toggleFavorite,
    openTarget,
    allRagSpaces,
    loadAllRagSpaces,
  };

  return <WorkspaceCtx.Provider value={value}>{children}</WorkspaceCtx.Provider>;
}
