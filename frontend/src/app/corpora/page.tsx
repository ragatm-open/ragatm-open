"use client";

import React, { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { AppShell, useWorkspace } from "@/components/shell";
import { Card, EmptyState, Icon, Pill } from "@/components/ui";
import { corpusCounts, listWorkspaceCorpora, type WorkspaceCorpus } from "@/lib/corpus";

export default function CorporaPage() {
  return (
    <AppShell>
      <CorporaContent />
    </AppShell>
  );
}

function CorporaContent() {
  const t = useTranslations("corpora");
  const { workspaceId, loading: ctxLoading } = useWorkspace();
  const [items, setItems] = useState<WorkspaceCorpus[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (workspaceId === null) return;
    let alive = true;
    setLoading(true);
    listWorkspaceCorpora(workspaceId)
      .then((d) => alive && setItems(d))
      .catch((e) => alive && setError(String(e?.message || e)))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [workspaceId]);

  return (
    <div style={{ padding: "28px 32px", maxWidth: 920, margin: "0 auto" }}>
      <header style={{ marginBottom: 20 }}>
        <h1 style={{ fontSize: 22, fontWeight: 700, color: "var(--ink)", margin: 0 }}>{t("activeTitle")}</h1>
        <p style={{ fontSize: 14, color: "var(--ink-3)", marginTop: 6, lineHeight: 1.5 }}>
          {t("activeSubtitle")}
        </p>
      </header>

      {ctxLoading || loading ? (
        <Card>
          <div style={{ padding: 24, color: "var(--ink-3)", fontSize: 14 }}>{t("loading")}</div>
        </Card>
      ) : error ? (
        <Card>
          <div style={{ padding: 24, color: "var(--danger, #b91c1c)", fontSize: 14 }}>{error}</div>
        </Card>
      ) : items.length === 0 ? (
        <EmptyState
          icon="search"
          title={t("emptyTitle")}
          body={t("emptyBody")}
        />
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {items.map((c) => (
            <Card key={c.corpus_id}>
              <div style={{ padding: 16 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", marginBottom: 8 }}>
                  <Pill color="#7c3aed" bg="#f3e8ff">
                    {c.business_domain_name || t("domainFallback")}
                  </Pill>
                  <span style={{ fontSize: 15, fontWeight: 700, color: "var(--ink)" }}>{c.corpus_name}</span>
                  <Pill color="var(--ink-3)" bg="var(--chip)">
                    <Icon name="users" size={12} /> {t("readOnlyManaged")}
                  </Pill>
                </div>
                {c.description && (
                  <div style={{ fontSize: 13.5, color: "var(--ink-2)", lineHeight: 1.55, marginBottom: 8 }}>
                    {c.description}
                  </div>
                )}
                {/* Total ET documents réellement interrogeables : « 12 documents »
                    seul avait servi de preuve d'exploitabilité, alors qu'un corpus dont
                    l'ingestion a échoué affiche exactement le même nombre. */}
                <div style={{ fontSize: 12.5, color: "var(--ink-3)" }}>
                  {(() => {
                    const n = corpusCounts(c);
                    const parts = [t("documentsCount", { count: n.total })];
                    if (n.ready != null) parts.push(t("readyDocumentsCount", { ready: n.ready }));
                    if (n.processing > 0) {
                      parts.push(t("processingDocumentsCount", { processing: n.processing }));
                    }
                    if (n.failed > 0) parts.push(t("failedDocumentsCount", { failed: n.failed }));
                    return parts.join(" · ");
                  })()}
                </div>
              </div>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
