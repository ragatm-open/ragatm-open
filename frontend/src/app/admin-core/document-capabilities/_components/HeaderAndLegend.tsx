"use client";

import React from "react";
import { useLocale, useTranslations } from "next-intl";
import { Button, Card } from "@/components/ui";
import type { DocumentCapabilitiesSnapshot } from "@/lib/coreConfig";
import { formatEpoch, shortSha } from "../_lib/capabilities";
import {
  CAPABILITY_STATES,
  capabilityStateMeta,
  consistencyMeta,
  indexedMeta,
  numericEvidenceMeta,
  qualificationMeta,
  serviceStatusMeta,
  type StatusMeta,
} from "../_lib/status";
import { KV, StatusPill } from "./primitives";

export function CapabilitiesHeader({
  snapshot,
  onRefresh,
  refreshing,
}: {
  snapshot: DocumentCapabilitiesSnapshot | null;
  onRefresh: () => void;
  refreshing: boolean;
}) {
  const t = useTranslations("documentCapabilities");
  const locale = useLocale();
  const env = (snapshot?.environment ?? "").trim();
  const sha = shortSha(snapshot?.deployment_sha);
  const generated = formatEpoch(snapshot?.generated_at, locale);
  const consStatus = snapshot?.runtime_consistency?.status ?? "UNKNOWN";
  const cons = consistencyMeta(consStatus);
  const warnings = snapshot?.warnings?.length ?? 0;
  return (
    <header style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12, flexWrap: "wrap" }}>
        <div style={{ minWidth: 0 }}>
          <h1 style={{ fontSize: 22, fontWeight: 700, color: "var(--ink)", margin: 0 }}>{t("title")}</h1>
          <p style={{ fontSize: 14, color: "var(--ink-3)", margin: "6px 0 0" }}>{t("subtitle")}</p>
        </div>
        <Button
          variant="secondary"
          size="sm"
          icon="refresh"
          onClick={onRefresh}
          disabled={refreshing}
          aria-label={t("refreshAria")}
        >
          {refreshing ? t("refreshing") : t("refresh")}
        </Button>
      </div>
      {snapshot && (
        <Card>
          <div style={{ padding: 14, display: "flex", flexDirection: "column", gap: 10 }}>
            <div style={{ display: "flex", flexWrap: "wrap", gap: "10px 24px" }}>
              <KV label={t("environmentLabel")}>
                <span
                  data-testid="dc-environment"
                  style={{
                    display: "inline-block",
                    padding: "2px 10px",
                    borderRadius: 6,
                    fontWeight: 800,
                    letterSpacing: ".04em",
                    background: "var(--ink)",
                    color: "var(--surface)",
                  }}
                >
                  {env ? env.toUpperCase() : t("environmentUnknown")}
                </span>
              </KV>
              <KV label={t("versionLabel")}>
                <code data-testid="dc-sha" title={snapshot.deployment_sha || undefined}>
                  {sha ?? t("versionUnknown")}
                </code>
              </KV>
              <KV label={t("generatedAtLabel")}>{generated ?? t("none")}</KV>
              <KV label={t("consistencyLabel")}>
                <StatusPill meta={cons} />
              </KV>
              <KV label={t("warningsLabel")}>
                <StatusPill
                  meta={warnings ? { tone: "warning", icon: "alert", key: "" } : { tone: "success", icon: "check", key: "" }}
                  label={t("warningsCount", { count: warnings })}
                />
              </KV>
            </div>
            <div style={{ fontSize: 12.5, color: "var(--ink-3)" }}>
              {t(`consExplain_${consStatus in { OK: 1, PARTIAL: 1, MISMATCH: 1 } ? consStatus : "UNKNOWN"}`)}
              {snapshot.runtime_consistency?.reason ? ` (${snapshot.runtime_consistency.reason})` : ""}
            </div>
            <div style={{ fontSize: 12.5, color: "var(--ink-3)" }}>
              {t("summaryLine", {
                formats: snapshot.summary.formats,
                extensions: snapshot.summary.extensions,
                capabilities: snapshot.summary.capabilities,
              })}
            </div>
            <div role="note" style={{ fontSize: 12.5, color: "var(--ink-2)" }}>
              {t("readOnlyNotice")}
            </div>
          </div>
        </Card>
      )}
    </header>
  );
}

function LegendGroup({ title, items }: { title: string; items: { meta: StatusMeta; label?: string }[] }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <h3 style={{ fontSize: 13, fontWeight: 700, color: "var(--ink-2)", margin: 0 }}>{title}</h3>
      <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexWrap: "wrap", gap: 6 }}>
        {items.map((it, i) => (
          <li key={`${it.meta.key}-${i}`}>
            <StatusPill meta={it.meta} label={it.label} />
          </li>
        ))}
      </ul>
    </div>
  );
}

export function StatusLegend() {
  const t = useTranslations("documentCapabilities");
  return (
    <details open style={{ background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 14, padding: "12px 16px" }}>
      <summary style={{ fontSize: 14, fontWeight: 700, color: "var(--ink)", cursor: "pointer" }}>{t("legendTitle")}</summary>
      <div style={{ display: "flex", flexDirection: "column", gap: 12, marginTop: 12 }} aria-label={t("legendTitle")}>
        <LegendGroup
          title={t("legendActivation")}
          items={[...CAPABILITY_STATES.map((s) => ({ meta: capabilityStateMeta(s) })), { meta: capabilityStateMeta("NOT_APPLICABLE") }]}
        />
        <LegendGroup
          title={t("legendRuntime")}
          items={(["AVAILABLE", "DECLARED", "DEGRADED", "UNAVAILABLE", "UNKNOWN"] as const).map((s) => ({ meta: serviceStatusMeta(s) }))}
        />
        <LegendGroup
          title={t("legendIndexed")}
          items={(["INDEXED", "INTERNAL_ONLY", "NONE"] as const).map((s) => ({ meta: indexedMeta(s) }))}
        />
        <LegendGroup
          title={t("legendNumeric")}
          items={[true, false, null].map((v) => ({ meta: numericEvidenceMeta(v) }))}
        />
        <LegendGroup
          title={t("legendQualification")}
          items={(
            ["EXPERIMENTAL", "NOT_QUALIFIED", "PARTIAL", "SYNTHETIC_QUALIFIED", "REAL_DOCUMENT_QUALIFIED", "NOT_MEASURED"] as const
          ).map((s) => ({ meta: qualificationMeta(s) }))}
        />
        <p style={{ fontSize: 12.5, color: "var(--ink-3)", margin: 0 }}>{t("legendNote")}</p>
      </div>
    </details>
  );
}
