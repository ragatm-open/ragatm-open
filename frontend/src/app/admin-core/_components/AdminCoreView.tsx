"use client";

import React from "react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { Card, EmptyState, Icon, Pill } from "@/components/ui";
import type {
  CoreAuditPage,
  CoreConfigEffective,
  CoreHealth,
} from "@/lib/coreConfig";
import { WorkspaceQuotaDefaultsPanel } from "./WorkspaceQuotaDefaultsPanel";

/**
 * Vue PRÉSENTATIONNELLE pure de l'admin RagATM_Core (lecture seule).
 * Aucune logique de fetch, aucun contexte, AUCUN bouton d'écriture : les données
 * arrivent par props. La redaction est faite côté backend ; on affiche tel quel.
 */
export type AdminCoreViewProps = {
  loading: boolean;
  error: string | null;
  effective: CoreConfigEffective | null;
  health: CoreHealth | null;
  audit: CoreAuditPage | null;
};

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <Card>
      <div style={{ padding: 18 }}>
        <h2 style={{ fontSize: 16, fontWeight: 700, color: "var(--ink)", margin: "0 0 12px" }}>{title}</h2>
        {children}
      </div>
    </Card>
  );
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", gap: 12, padding: "7px 0", borderBottom: "1px solid var(--border)" }}>
      <span style={{ fontSize: 13.5, color: "var(--ink-3)" }}>{label}</span>
      <span style={{ fontSize: 13.5, fontWeight: 600, color: "var(--ink)" }}>{value}</span>
    </div>
  );
}

const GATEWAY_HEALTH_PILL: Record<string, { color: string; bg: string; icon: string; key: string }> = {
  AVAILABLE: { color: "#166534", bg: "#E7F4EC", icon: "checkCircle", key: "gatewayHealthAvailable" },
  DECLARED: { color: "#1E4E8C", bg: "#E6EEF8", icon: "dots", key: "gatewayHealthDeclared" },
  DEGRADED: { color: "#8A4B06", bg: "#FBF0DB", icon: "alert", key: "gatewayHealthDegraded" },
  UNAVAILABLE: { color: "#A12F20", bg: "#FBE7E3", icon: "alert", key: "gatewayHealthUnavailable" },
  UNKNOWN: { color: "#4F4740", bg: "#F1ECE6", icon: "clock", key: "gatewayHealthUnknown" },
};

/** Pastille de santé Gateway : icône + texte + couleur ; `null`/inconnu → « Inconnu ». */
function GatewayHealthPill({ status }: { status: string | null | undefined }) {
  const t = useTranslations("adminCore");
  const p = GATEWAY_HEALTH_PILL[status ?? ""] ?? GATEWAY_HEALTH_PILL.UNKNOWN;
  return (
    <Pill color={p.color} bg={p.bg}>
      <Icon name={p.icon} size={13} stroke={2} aria-hidden="true" />
      {t(p.key)}
    </Pill>
  );
}

function HealthSection({ health }: { health: CoreHealth }) {
  const t = useTranslations("adminCore");
  const yesNo = (v: boolean): string => (v ? t("yes") : t("no"));
  // le backend ne vérifie pas réellement le Core (`ok: null`,
  // `verified: false`). On n'affiche JAMAIS « OK » sans preuve.
  const core = health.core_health;
  const coreVerified = core?.verified !== false && typeof core?.ok === "boolean";
  const gw = health.gateway_health && typeof health.gateway_health === "object" ? health.gateway_health : null;
  const services = Object.entries(gw?.services ?? {});
  return (
    <Section title={t("health")}>
      <Row
        label={t("coreLabel")}
        value={
          coreVerified ? (
            <Pill color={core.ok ? "#166534" : "#A12F20"} bg={core.ok ? "#E7F4EC" : "#FBE7E3"}>
              {core.ok ? t("healthOk") : t("healthKo")}
            </Pill>
          ) : (
            <span title={t("coreNotVerifiedHint")}>
              <Pill color="#4F4740" bg="#F1ECE6">
                <Icon name="clock" size={13} stroke={2} aria-hidden="true" />
                {t("coreNotVerified")}
              </Pill>
            </span>
          )
        }
      />
      <Row label={t("embeddingDimension")} value={core?.embedding_dim ?? "—"} />
      <Row label={t("gatewayHealthLabel")} value={<GatewayHealthPill status={gw?.status} />} />
      {services.map(([name, st]) => (
        <Row key={name} label={t("gatewayServiceRow", { service: name })} value={<GatewayHealthPill status={st} />} />
      ))}
      <Row label={t("gatewayConfigured")} value={yesNo(health.gateway_configured)} />
      <Row label={t("gatewayEnabled")} value={yesNo(health.gateway_enabled)} />
      <Row label={t("fallbackLabel")} value={health.fallback_enabled ? t("enabled") : t("disabled")} />
    </Section>
  );
}

function DocumentCapabilitiesLink() {
  const t = useTranslations("adminCore");
  return (
    <Link
      href="/admin-core/document-capabilities"
      data-testid="admin-core-doc-caps-link"
      style={{ textDecoration: "none", color: "inherit", display: "block", borderRadius: 14 }}
    >
      <Card hover>
        <div style={{ padding: 18, display: "flex", alignItems: "center", gap: 14 }}>
          <div
            aria-hidden="true"
            style={{ width: 40, height: 40, borderRadius: 12, background: "var(--accent-soft)", color: "var(--accent-ink)", display: "grid", placeItems: "center", flex: "none" }}
          >
            <Icon name="doc" size={20} />
          </div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontSize: 15, fontWeight: 700, color: "var(--ink)" }}>{t("docCapsLinkTitle")}</div>
            <div style={{ fontSize: 13, color: "var(--ink-3)", marginTop: 2 }}>{t("docCapsLinkBody")}</div>
          </div>
          <Icon name="chevronR" size={18} aria-hidden="true" />
        </div>
      </Card>
    </Link>
  );
}

function EffectiveSection({ effective }: { effective: CoreConfigEffective }) {
  const t = useTranslations("adminCore");
  return (
    <Section title={t("effectiveConfigTitle", { environment: effective.environment })}>
      <div style={{ fontSize: 12.5, color: "var(--ink-3)", marginBottom: 10 }}>
        {t("effectiveReadOnlyNote")}
      </div>
      <div style={{ display: "flex", flexDirection: "column" }}>
        {effective.config.map((e) => (
          <div key={e.key} style={{ display: "flex", alignItems: "center", gap: 10, padding: "9px 0", borderBottom: "1px solid var(--border)" }}>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: 13.5, fontWeight: 600, color: "var(--ink)" }}>{e.key}</div>
              {e.description && <div style={{ fontSize: 12, color: "var(--ink-3)" }}>{e.description}</div>}
            </div>
            <code style={{ fontSize: 13, color: "var(--ink)", background: "var(--chip)", padding: "2px 8px", borderRadius: 6 }}>
              {String(e.effective_value)}
            </code>
            <span style={{ fontSize: 12, color: "var(--ink-3)" }}>{e.source}</span>
            {e.redacted && <Pill color="#5B53D9" bg="var(--chip)">{t("redacted")}</Pill>}
            <Pill color="var(--ink-3)" bg="var(--chip)">{t("readOnly")}</Pill>
          </div>
        ))}
      </div>
    </Section>
  );
}

function AuditSection({ audit }: { audit: CoreAuditPage }) {
  const t = useTranslations("adminCore");
  return (
    <Section title={t("auditTitle")}>
      {audit.results.length === 0 ? (
        <div style={{ fontSize: 13, color: "var(--ink-3)" }}>{t("auditEmpty")}</div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {audit.results.map((ev) => (
            <div key={ev.id} style={{ padding: "10px 12px", borderRadius: 10, border: "1px solid var(--border)" }}>
              <div style={{ display: "flex", justifyContent: "space-between", gap: 10, flexWrap: "wrap" }}>
                <span style={{ fontSize: 13, fontWeight: 600, color: "var(--ink)" }}>{ev.user || "—"}</span>
                <span style={{ fontSize: 12, color: "var(--ink-3)" }}>{ev.created_at}</span>
              </div>
              <div style={{ fontSize: 12, color: "var(--ink-3)", marginTop: 2 }}>
                {t("auditScope", { workspace: ev.workspace_id ?? "—", ragspace: ev.rag_space_id ?? "—" })}
              </div>
              <div style={{ fontSize: 12.5, color: "var(--ink-2)", marginTop: 6 }}>
                {t("auditChatLine", { oldProvider: ev.chat.old_provider, oldModel: ev.chat.old_model, newProvider: ev.chat.new_provider, newModel: ev.chat.new_model })}
              </div>
              <div style={{ fontSize: 12.5, color: "var(--ink-2)" }}>
                {t("auditEmbeddingsLine", { oldProvider: ev.embeddings.old_provider, oldModel: ev.embeddings.old_model, newProvider: ev.embeddings.new_provider, newModel: ev.embeddings.new_model })}
              </div>
            </div>
          ))}
        </div>
      )}
    </Section>
  );
}

export function AdminCoreView({ loading, error, effective, health, audit }: AdminCoreViewProps) {
  const t = useTranslations("adminCore");
  return (
    <div style={{ padding: "28px 32px", maxWidth: 1000, margin: "0 auto", display: "flex", flexDirection: "column", gap: 16 }}>
      <header>
        <h1 style={{ fontSize: 22, fontWeight: 700, color: "var(--ink)", margin: 0 }}>{t("title")}</h1>
        <p style={{ fontSize: 14, color: "var(--ink-3)", marginTop: 6 }}>
          {t("subtitle")}
        </p>
      </header>

      <DocumentCapabilitiesLink />

      {loading && <div role="status" style={{ padding: 24, color: "var(--ink-3)" }}>{t("loading")}</div>}

      {!loading && error && (
        <div role="alert" style={{ padding: "12px 14px", borderRadius: 10, background: "#fef2f2", color: "#b91c1c", fontSize: 13 }}>
          {error}
        </div>
      )}

      {!loading && !error && effective?.db_overlay_runtime_active && (
        <div
          role="alert"
          style={{ padding: "10px 14px", borderRadius: 10, background: "#fef2f2", color: "#b91c1c", fontSize: 13, border: "1px solid #fecaca" }}
        >
          ⚠️ {effective.warning || t("overrideActiveWarning")}
          {" "}{t("runtimeSourceLabel")} <strong>{effective.runtime_source_of_truth || "db_overlay"}</strong>.
        </div>
      )}

      {!loading && !error && !effective?.db_overlay_runtime_active && effective?.diagnostic_overlay_only && (
        <div
          role="note"
          style={{ padding: "10px 14px", borderRadius: 10, background: "#fffbeb", color: "#92400e", fontSize: 13, border: "1px solid #fde68a" }}
        >
          {effective.warning || t("overrideDiagnosticNote")}
          {" "}{t("runtimeSourceLabel")} <strong>{effective.runtime_source_of_truth || "settings"}</strong>.
        </div>
      )}

      {!loading && !error && (
        <>
          <WorkspaceQuotaDefaultsPanel />
          {health ? <HealthSection health={health} /> : null}
          {effective ? <EffectiveSection effective={effective} /> : null}
          {audit ? (
            <AuditSection audit={audit} />
          ) : (
            <Section title={t("auditTitle")}>
              <EmptyState icon="clock" title={t("auditUnavailableTitle")} body={t("auditUnavailableBody")} />
            </Section>
          )}
        </>
      )}
    </div>
  );
}
