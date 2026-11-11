"use client";

import React from "react";
import { useLocale, useTranslations } from "next-intl";
import type { DocumentCapability, DocumentFormat } from "@/lib/coreConfig";
import { capabilitiesForFormat, formatBytes, formatExactBytes, scopeView } from "../_lib/capabilities";
import {
  capabilityStateMeta,
  formatStatusMeta,
  indexedMeta,
  numericEvidenceMeta,
  qualificationMeta,
  runtimeMeta,
  serviceStatusMeta,
} from "../_lib/status";
import styles from "./DocumentCapabilities.module.css";
import { codeStyle, KV, Section, StatusPill } from "./primitives";

function List({ items }: { items: string[] }) {
  const t = useTranslations("documentCapabilities");
  if (!items?.length) return <>{t("none")}</>;
  return (
    <ul style={{ margin: 0, paddingLeft: 18 }}>
      {items.map((it, i) => (
        <li key={i}>{it}</li>
      ))}
    </ul>
  );
}

function ScopeText({ cap }: { cap: DocumentCapability }) {
  const t = useTranslations("documentCapabilities");
  const v = scopeView(cap);
  switch (v.kind) {
    case "global":
      return <>{t("scope_global")}</>;
    case "spaces":
      return <>{t("scope_spaces", { count: v.count })}</>;
    case "inoperative":
      return <StatusPill meta={{ tone: "warning", icon: "alert", key: "scope_inoperative" }} />;
    case "none":
      return <>{t("scope_none")}</>;
    default:
      return <>{t("scope_unknown")}</>;
  }
}

function GatewayUsage({ cap }: { cap: DocumentCapability }) {
  const t = useTranslations("documentCapabilities");
  const g = cap.gateway_dependency;
  if (!g || !g.uses_ai_gateway) {
    if (g?.gateway_call_type === "DEPENDS_ON_RAGSPACE") return <>{t("gatewayDependsOnSpace")}</>;
    return <>{t("gatewayLocal")}</>;
  }
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <span>
        {t("gatewayServiceLabel")} : <strong>{g.gateway_service}</strong>{" "}
        <StatusPill meta={serviceStatusMeta(g.service_status)} />
      </span>
      {g.gateway_route && (
        <span>
          {t("gatewayRouteLabel")} : <code style={codeStyle}>{g.gateway_route}</code>
        </span>
      )}
      <span>
        {t("modelLogicalLabel")} : {g.model_logical || t("none")} · {t("modelConcreteLabel")} : {g.model_concrete || t("none")}
      </span>
      {g.stage && (
        <span>
          {t("stageLabel")} : {g.stage}
        </span>
      )}
    </div>
  );
}

function CapabilityCard({ cap, showSpace }: { cap: DocumentCapability; showSpace: boolean }) {
  const t = useTranslations("documentCapabilities");
  const yesNo = (v: boolean) => (v ? t("yes") : t("no"));
  const source =
    cap.format_id === "visual" ? t("capSourceVisual") : cap.format_id === "common" ? t("capSourceCommon") : null;
  return (
    <li
      data-capability-id={cap.id}
      style={{ border: "1px solid var(--border)", borderRadius: 12, padding: 12, display: "flex", flexDirection: "column", gap: 10 }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", gap: 8, flexWrap: "wrap", alignItems: "flex-start" }}>
        <div style={{ minWidth: 0 }}>
          <h4 style={{ fontSize: 14, fontWeight: 700, color: "var(--ink)", margin: 0 }}>{cap.label}</h4>
          {cap.description && <p style={{ fontSize: 12.5, color: "var(--ink-3)", margin: "3px 0 0" }}>{cap.description}</p>}
          {source && <p style={{ fontSize: 12, color: "var(--ink-3)", margin: "3px 0 0", fontStyle: "italic" }}>{source}</p>}
        </div>
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          <StatusPill meta={capabilityStateMeta(cap.state)} />
          <StatusPill meta={qualificationMeta(cap.qualification_status)} />
          {cap.numeric_evidence_reliable === false && <StatusPill meta={numericEvidenceMeta(false)} />}
        </div>
      </div>
      {showSpace && (
        <div style={{ fontSize: 13 }} data-testid="dc-space-applicable">
          {t("spaceApplicableLabel")} :{" "}
          <strong>{cap.space_applicable == null ? t("none") : yesNo(cap.space_applicable)}</strong>
        </div>
      )}
      <div className={styles.kvGrid}>
        <KV label={t("statusLabel")}>{t(capabilityStateMeta(cap.state).key)}</KV>
        <KV label={t("runtimeLabel")}>
          <StatusPill meta={runtimeMeta(cap.runtime_available)} />
          {cap.runtime_reasons?.length ? (
            <span style={{ display: "block", fontSize: 12, color: "var(--ink-3)" }}>
              {t("runtimeReasonsLabel")} : {cap.runtime_reasons.join(", ")}
            </span>
          ) : null}
        </KV>
        <KV label={t("triggerLabel")}>
          {cap.content_trigger || t("none")}
          {cap.content_trigger_conditional ? ` (${t("triggerConditional")})` : ""}
        </KV>
        <KV label={t("scopeLabel")}>
          <ScopeText cap={cap} />
        </KV>
        <KV label={t("executorLabel")}>{cap.executing_service || t("none")}</KV>
        <KV label={t("gatewayUsageLabel")}>
          <GatewayUsage cap={cap} />
        </KV>
        <KV label={t("outputLabel")}>{cap.output_type || t("none")}</KV>
        <KV label={t("indexedLabel")}>
          <StatusPill meta={indexedMeta(cap.output_indexed)} />
        </KV>
        <KV label={t("searchableLabel")}>{yesNo(!!cap.output_searchable)}</KV>
        <KV label={t("citationLabel")}>{cap.citation_support || t("none")}</KV>
        <KV label={t("numericLabel")}>
          <StatusPill meta={numericEvidenceMeta(cap.numeric_evidence_reliable)} />
          {cap.numeric_evidence_class && cap.numeric_evidence_class !== "n/a" ? (
            <span style={{ display: "block", fontSize: 12, color: "var(--ink-3)" }}>{cap.numeric_evidence_class}</span>
          ) : null}
        </KV>
        <KV label={t("qualificationLabel")}>
          <StatusPill meta={qualificationMeta(cap.qualification_status)} />
          {cap.qualification_evidence ? (
            <span style={{ display: "block", fontSize: 12, color: "var(--ink-3)" }}>
              {t("evidenceLabel")} : {cap.qualification_evidence}
            </span>
          ) : null}
        </KV>
      </div>
      {cap.limitations?.length ? (
        <KV label={t("limitationsLabel")}>
          <List items={cap.limitations} />
        </KV>
      ) : null}
      <details>
        <summary style={{ fontSize: 12.5, color: "var(--ink-2)", cursor: "pointer" }}>{t("technicalDetails")}</summary>
        <div className={styles.kvGrid} style={{ marginTop: 8 }}>
          <KV label={t("capabilityIdLabel")}>
            <code style={codeStyle}>{cap.id}</code>
          </KV>
          <KV label={t("flagsLabel")}>
            {cap.flags?.length ? (
              <span style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
                {cap.flags.map((f) => (
                  <code key={f} style={codeStyle}>
                    {f}
                  </code>
                ))}
              </span>
            ) : (
              t("none")
            )}
          </KV>
          <KV label={t("consumersLabel")}>{cap.consumers?.length ? cap.consumers.join(", ") : t("none")}</KV>
          <KV label={t("implementedLabel")}>{cap.implemented}</KV>
          <KV label={t("configuredLabel")}>{yesNo(!!cap.configured_enabled)}</KV>
          <KV label={t("contentTypeLabel")}>{cap.content_type || t("none")}</KV>
          {cap.scope?.list_setting ? (
            <KV label={t("scopeLabel")}>
              <code style={codeStyle}>{cap.scope.list_setting}</code>
            </KV>
          ) : null}
          {cap.configuration_error ? <KV label={t("configurationErrorLabel")}>{cap.configuration_error}</KV> : null}
        </div>
      </details>
    </li>
  );
}

export function FormatDetail({
  format,
  capabilities,
  showSpace,
}: {
  format: DocumentFormat | null;
  capabilities: DocumentCapability[];
  showSpace: boolean;
}) {
  const t = useTranslations("documentCapabilities");
  const locale = useLocale();
  if (!format) {
    return (
      <Section title={t("detailTitle", { format: "—" })} id="dc-detail">
        <div style={{ fontSize: 13, color: "var(--ink-3)" }}>{t("detailEmpty")}</div>
      </Section>
    );
  }
  const caps = capabilitiesForFormat(capabilities, format.format_id);
  const size = formatBytes(format.max_upload_bytes, locale);
  return (
    <Section title={t("detailTitle", { format: format.label })} id="dc-detail">
      <div data-testid="dc-detail" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <div className={styles.kvGrid}>
          <KV label={t("formatStatusLabel")}>
            <StatusPill meta={formatStatusMeta(format.status)} />
          </KV>
          <KV label={t("extensionsLabel")}>{format.extensions.join(", ") || t("none")}</KV>
          <KV label={t("extractorLabel")}>{format.extractor || t("none")}</KV>
          <KV label={t("executorLabel")}>{format.executing_service || t("none")}</KV>
          <KV label={t("uploadEnabledLabel")}>{format.upload_enabled ? t("yes") : t("no")}</KV>
          <KV label={t("maxUploadLabel")}>
            {size && format.max_upload_bytes != null
              ? t("maxUploadValue", { size, exact: formatExactBytes(format.max_upload_bytes, locale) })
              : t("none")}
          </KV>
          {format.refusal_code ? (
            <KV label={t("refusalCodeLabel")}>
              <code style={codeStyle}>{format.refusal_code}</code>
            </KV>
          ) : null}
        </div>
        {format.notes?.length ? (
          <KV label={t("notesLabel")}>
            <List items={format.notes} />
          </KV>
        ) : null}
        <h3 style={{ fontSize: 14, fontWeight: 700, color: "var(--ink)", margin: 0 }}>{t("capabilitiesOfFormat")}</h3>
        {caps.length === 0 ? (
          <div style={{ fontSize: 13, color: "var(--ink-3)" }}>{t("none")}</div>
        ) : (
          <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: 10 }}>
            {caps.map((c) => (
              <CapabilityCard key={c.id} cap={c} showSpace={showSpace} />
            ))}
          </ul>
        )}
      </div>
    </Section>
  );
}
