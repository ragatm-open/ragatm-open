"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { useWorkspace } from "@/components/shell";
import { EmptyState } from "@/components/ui";
import { ApiError } from "@/lib/apiError";
import { getDocumentCapabilities, type DocumentCapabilitiesSnapshot } from "@/lib/coreConfig";
import { DocumentCapabilitiesView } from "./DocumentCapabilitiesView";

/**
 * Garde + chargement de « Capacités documentaires ». Même porte que /admin-core
 * (`isGlobalOwner`) : AUCUN appel réseau tant que l'accès n'est pas établi ; le
 * backend reste l'autorité (403 → message d'accès refusé).
 */
export function DocumentCapabilitiesScreen() {
  const t = useTranslations("documentCapabilities");
  const { isGlobalOwner, loading } = useWorkspace();
  if (loading) return <div style={{ padding: 32, color: "var(--ink-3)" }}>{t("loading")}</div>;
  if (!isGlobalOwner) {
    return (
      <div style={{ padding: "48px 32px", maxWidth: 640, margin: "0 auto" }}>
        <EmptyState icon="lock" title={t("accessRestrictedTitle")} body={t("accessRestrictedBody")} />
      </div>
    );
  }
  return <DocumentCapabilitiesData />;
}

function errorStatus(e: unknown): number | null {
  if (e instanceof ApiError) return e.status;
  if (e && typeof e === "object" && typeof (e as { status?: unknown }).status === "number") {
    return (e as { status: number }).status;
  }
  return null;
}

function DocumentCapabilitiesData() {
  const t = useTranslations("documentCapabilities");
  const [snapshot, setSnapshot] = useState<DocumentCapabilitiesSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [spaceError, setSpaceError] = useState<string | null>(null);
  const [spaceId, setSpaceId] = useState<string | null>(null);
  const reqSeq = useRef(0);

  const load = useCallback(
    async (opts: { ragspaceId: string | null; refresh: boolean; initial?: boolean }) => {
      const seq = ++reqSeq.current;
      if (opts.initial) setLoading(true);
      else setRefreshing(true);
      try {
        const snap = await getDocumentCapabilities({ ragspaceId: opts.ragspaceId, refresh: opts.refresh });
        if (seq !== reqSeq.current) return;
        setSnapshot(snap);
        setSpaceId(opts.ragspaceId);
        setError(null);
        setSpaceError(null);
      } catch (e: unknown) {
        if (seq !== reqSeq.current) return;
        const status = errorStatus(e);
        if (opts.ragspaceId && (status === 400 || status === 404)) {
          // Vue par espace refusée : on conserve l'instantané courant.
          setSpaceError(status === 400 ? t("spaceInvalid") : t("spaceNotFound"));
        } else if (status === 403) {
          setError(t("accessDenied"));
        } else {
          setError(t("loadError"));
        }
      } finally {
        if (seq === reqSeq.current) {
          setLoading(false);
          setRefreshing(false);
        }
      }
    },
    [t]
  );

  useEffect(() => {
    void load({ ragspaceId: null, refresh: false, initial: true });
  }, [load]);

  return (
    <DocumentCapabilitiesView
      snapshot={snapshot}
      loading={loading}
      refreshing={refreshing}
      error={error}
      spaceError={spaceError}
      onRefresh={() => void load({ ragspaceId: spaceId, refresh: true })}
      onApplySpace={(id) => void load({ ragspaceId: id, refresh: false })}
      onClearSpace={() => void load({ ragspaceId: null, refresh: false })}
    />
  );
}
