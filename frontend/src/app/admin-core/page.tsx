"use client";

import React, { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { AppShell, useWorkspace } from "@/components/shell";
import { EmptyState } from "@/components/ui";
import {
  getCoreAudit,
  getCoreHealth,
  getEffectiveConfig,
  type CoreAuditPage,
  type CoreConfigEffective,
  type CoreHealth,
} from "@/lib/coreConfig";
import { AdminCoreView } from "./_components/AdminCoreView";

export default function AdminCorePage() {
  return (
    <AppShell>
      <AdminCoreGate />
    </AppShell>
  );
}

function AdminCoreGate() {
  const t = useTranslations("adminCore");
  const { isGlobalOwner, loading } = useWorkspace();
  if (loading) return <div style={{ padding: 32, color: "var(--ink-3)" }}>{t("loading")}</div>;
  if (!isGlobalOwner) {
    return (
      <div style={{ padding: "48px 32px", maxWidth: 640, margin: "0 auto" }}>
        <EmptyState
          icon="lock"
          title={t("accessRestrictedTitle")}
          body={t("accessRestrictedBody")}
        />
      </div>
    );
  }
  return <AdminCoreData />;
}

function AdminCoreData() {
  const t = useTranslations("adminCore");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [effective, setEffective] = useState<CoreConfigEffective | null>(null);
  const [health, setHealth] = useState<CoreHealth | null>(null);
  const [audit, setAudit] = useState<CoreAuditPage | null>(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      setLoading(true);
      setError(null);
      try {
        const [eff, hp, au] = await Promise.all([
          getEffectiveConfig(),
          getCoreHealth(),
          getCoreAudit({ limit: 50 }),
        ]);
        if (!alive) return;
        setEffective(eff);
        setHealth(hp);
        setAudit(au);
      } catch (e: unknown) {
        if (!alive) return;
        const msg = e instanceof Error ? e.message : String(e);
        setError(msg || t("loadError"));
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  return (
    <AdminCoreView
      loading={loading}
      error={error}
      effective={effective}
      health={health}
      audit={audit}
    />
  );
}
