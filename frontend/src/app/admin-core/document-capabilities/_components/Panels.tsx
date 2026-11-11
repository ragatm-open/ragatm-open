"use client";

import React, { useState } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui";
import type {
  CapabilityWarning,
  DocumentCapability,
  DocumentFormat,
  EmbeddingsSummary,
  GatewayServiceInfo,
  RuntimeConsistency,
} from "@/lib/coreConfig";
import { groupFormatsByFamily, inlineImageRows, shortSha, type InlineImageRow } from "../_lib/capabilities";
import {
  capabilityStateMeta,
  consistencyMeta,
  formatStatusMeta,
  serviceStatusMeta,
  warningKey,
  type StatusMeta,
} from "../_lib/status";
import styles from "./DocumentCapabilities.module.css";
import { codeStyle, KV, Section, StatusPill, tdStyle, thStyle } from "./primitives";

// --- Tous les formats ---------------------------------------------------------

export function AllFormatsSection({
  formats,
  totalFormats,
  totalExtensions,
  onSelect,
}: {
  formats: DocumentFormat[];
  totalFormats: number;
  totalExtensions: number;
  onSelect: (formatId: string) => void;
}) {
  const t = useTranslations("documentCapabilities");
  const groups = groupFormatsByFamily(formats);
  return (
    <Section
      title={t("allFormatsTitle")}
      id="dc-all-formats"
      subtitle={t("allFormatsSubtitle", { formats: totalFormats, extensions: totalExtensions })}
    >
      {groups.length === 0 ? (
        <div style={{ fontSize: 13, color: "var(--ink-3)" }}>{t("allFormatsEmpty")}</div>
      ) : (
        <div data-testid="dc-all-formats" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          {groups.map((g) => (
            <div key={g.family}>
              <h3 style={{ fontSize: 13, fontWeight: 700, color: "var(--ink-2)", margin: "0 0 6px" }}>{g.family}</h3>
              <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column" }}>
                {g.formats.map((f) => (
                  <li
                    key={f.format_id}
                    data-format-id={f.format_id}
                    style={{
                      display: "flex",
                      flexWrap: "wrap",
                      alignItems: "center",
                      gap: 8,
                      padding: "7px 0",
                      borderBottom: "1px solid var(--border)",
                    }}
                  >
                    <a
                      href={`#dc-detail-title`}
                      onClick={() => onSelect(f.format_id)}
                      style={{ fontSize: 13.5, fontWeight: 600, color: "var(--ink)", minWidth: 160 }}
                    >
                      {f.label}
                    </a>
                    <StatusPill meta={formatStatusMeta(f.status)} />
                    <span style={{ fontSize: 12.5, color: "var(--ink-3)", flex: 1, minWidth: 120 }}>
                      {f.extensions.join(", ")}
                    </span>
                    {f.refusal_code ? <code style={codeStyle}>{f.refusal_code}</code> : null}
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      )}
    </Section>
  );
}

// --- Images intégrées ---------------------------------------------------------

function CapCell({ cap }: { cap: DocumentCapability | null }) {
  const t = useTranslations("documentCapabilities");
  if (!cap) return <span style={{ fontSize: 12.5, color: "var(--ink-3)" }}>{t("inl_absent")}</span>;
  return <StatusPill meta={capabilityStateMeta(cap.state)} title={`${cap.label} (${cap.id})`} />;
}

function TriCell({ value }: { value: InlineImageRow["indexed"] }) {
  const meta: StatusMeta =
    value === "yes"
      ? { tone: "success", icon: "check", key: "inl_yes" }
      : value === "inactive"
        ? { tone: "neutral", icon: "stop", key: "inl_inactive" }
        : { tone: "muted", icon: "dots", key: "inl_no" };
  return <StatusPill meta={meta} />;
}

export function InlineImagesSection({ capabilities, formats }: { capabilities: DocumentCapability[]; formats: DocumentFormat[] }) {
  const t = useTranslations("documentCapabilities");
  const rows = inlineImageRows(capabilities);
  const labels = new Map(formats.map((f) => [f.format_id, f.label]));
  return (
    <Section title={t("inlineImagesTitle")} id="dc-inline-images" subtitle={t("inlineImagesBody")}>
      <div className={styles.tableWrap}>
        <table data-testid="dc-inline-images" style={{ borderCollapse: "collapse", width: "100%", minWidth: 760 }}>
          <thead>
            <tr>
              <th scope="col" style={thStyle}>{t("colFormat")}</th>
              <th scope="col" style={thStyle}>{t("inl_detection")}</th>
              <th scope="col" style={thStyle}>{t("inl_ocr")}</th>
              <th scope="col" style={thStyle}>{t("inl_vision")}</th>
              <th scope="col" style={thStyle}>{t("inl_structured")}</th>
              <th scope="col" style={thStyle}>{t("inl_indexed")}</th>
              <th scope="col" style={thStyle}>{t("inl_searchable")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.formatId} data-format-id={r.formatId}>
                <th scope="row" style={{ ...tdStyle, textAlign: "left", fontWeight: 700 }}>
                  {labels.get(r.formatId) ?? r.formatId}
                </th>
                <td style={tdStyle}><CapCell cap={r.detection} /></td>
                <td style={tdStyle}><CapCell cap={r.ocr} /></td>
                <td style={tdStyle}><CapCell cap={r.vision} /></td>
                <td style={tdStyle}><CapCell cap={r.structured} /></td>
                <td style={tdStyle}><TriCell value={r.indexed} /></td>
                <td style={tdStyle}><TriCell value={r.searchable} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

// --- Services AI Gateway ------------------------------------------------------

function GatewayServiceCard({ svc }: { svc: GatewayServiceInfo }) {
  const t = useTranslations("documentCapabilities");
  const isStructured = svc.service === "structured_extract";
  const isVision = svc.service === "vision";
  return (
    <li
      data-service={svc.service}
      style={{ border: "1px solid var(--border)", borderRadius: 12, padding: 12, display: "flex", flexDirection: "column", gap: 8 }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", gap: 8, flexWrap: "wrap" }}>
        <strong style={{ fontSize: 14, color: "var(--ink)" }}>{svc.service}</strong>
        <StatusPill meta={serviceStatusMeta(svc.status)} />
      </div>
      <div className={styles.kvGrid}>
        {svc.reason ? <KV label={t("svcReasonLabel")}>{svc.reason}</KV> : null}
        {svc.model_logical || svc.model_concrete ? (
          <>
            <KV label={t("modelLogicalLabel")}>{svc.model_logical || t("none")}</KV>
            <KV label={t("modelConcreteLabel")}>{svc.model_concrete || t("none")}</KV>
          </>
        ) : null}
        {svc.tasks?.length ? <KV label={t("svcTasksLabel")}>{svc.tasks.join(", ")}</KV> : null}
        {svc.models?.length ? (
          <KV label={t("svcModelsLabel")}>
            {svc.models.map((m) => `${m.model_logical ?? "?"}${m.model_concrete ? ` (${m.model_concrete})` : ""}`).join(", ")}
          </KV>
        ) : null}
        {svc.build ? (
          <KV label={t("svcBuildLabel")}>
            <code style={codeStyle}>{shortSha(svc.build)}</code>
          </KV>
        ) : null}
      </div>
      {isStructured && svc.status !== "UNKNOWN" && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
          <StatusPill
            meta={
              svc.experimental
                ? { tone: "scoped", icon: "sparkles", key: "svcExperimental" }
                : { tone: "neutral", icon: "dots", key: "svcNotExperimental" }
            }
          />
          <StatusPill
            meta={
              svc.qualified_for_production
                ? { tone: "success", icon: "shield", key: "svcQualifiedProd" }
                : { tone: "warning", icon: "alert", key: "svcNotQualifiedProd" }
            }
          />
        </div>
      )}
      {isStructured && svc.task_qualification && Object.keys(svc.task_qualification).length > 0 && (
        <KV label={t("svcTaskQualificationLabel")}>
          {Object.entries(svc.task_qualification)
            .map(([task, st]) => `${task} : ${st}`)
            .join(" · ")}
        </KV>
      )}
      {isVision && svc.status !== "UNKNOWN" && (
        <div style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 12.5, color: "var(--ink-2)" }}>
          <span>{svc.describe_available ? t("svcDescribeAvailable") : t("svcDescribeUnavailable")}</span>
          <span>{svc.semantic_enabled ? t("svcSemanticEnabled") : t("svcSemanticDisabled")}</span>
        </div>
      )}
    </li>
  );
}

export function GatewayServicesSection({ services }: { services: GatewayServiceInfo[] }) {
  const t = useTranslations("documentCapabilities");
  return (
    <Section title={t("gatewayServicesTitle")} id="dc-gateway" subtitle={t("gatewayServicesBody")}>
      {services.length === 0 ? (
        <div style={{ fontSize: 13, color: "var(--ink-3)" }}>{t("gatewayServicesEmpty")}</div>
      ) : (
        <ul data-testid="dc-gateway-services" style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: 10 }}>
          {services.map((s) => (
            <GatewayServiceCard key={s.service} svc={s} />
          ))}
        </ul>
      )}
    </Section>
  );
}

// --- Embeddings ---------------------------------------------------------------

export function EmbeddingsSection({ embeddings }: { embeddings: EmbeddingsSummary }) {
  const t = useTranslations("documentCapabilities");
  const sel = embeddings.selected_space;
  return (
    <Section title={t("embeddingsTitle")} id="dc-embeddings" subtitle={t("embeddingsTotal", { count: embeddings.total_spaces })}>
      <div data-testid="dc-embeddings" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <div className={styles.kvGrid}>
          <KV label={t("embAiGateway")}>{embeddings.ai_gateway_spaces}</KV>
          <KV label={t("embOllamaDirect")}>{embeddings.ollama_direct_spaces}</KV>
          <KV label={t("embOther")}>{embeddings.other_provider_spaces}</KV>
          <KV label={t("embUnknown")}>{embeddings.unknown_provider_spaces}</KV>
          <KV label={t("embDefault")}>
            {embeddings.default_for_new_spaces
              ? `${embeddings.default_for_new_spaces.provider} / ${embeddings.default_for_new_spaces.model}`
              : t("none")}
          </KV>
          <KV label={t("embFallback")}>{embeddings.gateway_fallback_enabled ? t("enabledLabel") : t("disabledLabel")}</KV>
          {sel ? (
            <KV label={t("embSelectedSpace")}>
              <span data-testid="dc-selected-space-embeddings">
                {sel.provider} / {sel.model} · {sel.via_ai_gateway ? t("embViaGateway") : t("embNotViaGateway")}
              </span>
            </KV>
          ) : null}
        </div>
        <div className={styles.tableWrap}>
          <table style={{ borderCollapse: "collapse", width: "100%", minWidth: 520 }}>
            <thead>
              <tr>
                <th scope="col" style={thStyle}>{t("embProvider")}</th>
                <th scope="col" style={thStyle}>{t("embModel")}</th>
                <th scope="col" style={thStyle}>{t("embSpaces")}</th>
                <th scope="col" style={thStyle}>{t("embCallType")}</th>
                <th scope="col" style={thStyle}>{t("embServiceStatus")}</th>
              </tr>
            </thead>
            <tbody>
              {embeddings.providers.map((p) => (
                <tr key={`${p.provider}/${p.model}`}>
                  <th scope="row" style={{ ...tdStyle, textAlign: "left" }}>{p.provider}</th>
                  <td style={tdStyle}>{p.model}</td>
                  <td style={tdStyle}>{p.spaces}</td>
                  <td style={tdStyle}><code style={codeStyle}>{p.call_type}</code></td>
                  <td style={tdStyle}><StatusPill meta={serviceStatusMeta(p.service_status)} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </Section>
  );
}

// --- Cohérence d'exécution ----------------------------------------------------

export function RuntimeConsistencySection({ consistency }: { consistency: RuntimeConsistency }) {
  const t = useTranslations("documentCapabilities");
  const roles = Object.entries(consistency.roles ?? {});
  return (
    <Section
      title={t("runtimeConsistencyTitle")}
      id="dc-runtime"
      actions={<StatusPill meta={consistencyMeta(consistency.status)} />}
    >
      <div data-testid="dc-runtime" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        {consistency.reason ? (
          <div style={{ fontSize: 13, color: "var(--ink-2)" }}>
            {t("reasonLabel")} : <code style={codeStyle}>{consistency.reason}</code>
          </div>
        ) : null}
        {roles.length === 0 ? (
          <div style={{ fontSize: 13, color: "var(--ink-3)" }}>{t("rolesEmpty")}</div>
        ) : (
          <div className={styles.tableWrap}>
            <table style={{ borderCollapse: "collapse", width: "100%", minWidth: 480 }}>
              <thead>
                <tr>
                  <th scope="col" style={thStyle}>{t("roleLabel")}</th>
                  <th scope="col" style={thStyle}>{t("roleState")}</th>
                  <th scope="col" style={thStyle}>{t("roleInstances")}</th>
                  <th scope="col" style={thStyle}>{t("roleAge")}</th>
                  <th scope="col" style={thStyle}>{t("roleSha")}</th>
                </tr>
              </thead>
              <tbody>
                {roles.map(([role, info]) => (
                  <tr key={role}>
                    <th scope="row" style={{ ...tdStyle, textAlign: "left" }}>{role}</th>
                    <td style={tdStyle}>
                      <StatusPill
                        meta={
                          info.state === "FRESH"
                            ? { tone: "success", icon: "checkCircle", key: "role_FRESH" }
                            : { tone: "warning", icon: "clock", key: "role_STALE" }
                        }
                      />
                    </td>
                    <td style={tdStyle}>{info.instances ?? t("none")}</td>
                    <td style={tdStyle}>
                      {info.age_seconds != null ? t("roleAgeValue", { seconds: Math.round(info.age_seconds) }) : t("none")}
                    </td>
                    <td style={tdStyle}>
                      <code style={codeStyle}>{shortSha(info.deployment_sha) ?? t("none")}</code>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <div>
          <h3 style={{ fontSize: 13, fontWeight: 700, color: "var(--ink-2)", margin: "0 0 6px" }}>{t("mismatchesTitle")}</h3>
          {consistency.mismatches?.length ? (
            <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
              {consistency.mismatches.map((m) => (
                <li key={m.flag}>{t("mismatchLine", { flag: m.flag, roles: m.roles.join(", ") })}</li>
              ))}
            </ul>
          ) : (
            <div style={{ fontSize: 13, color: "var(--ink-3)" }}>{t("mismatchesEmpty")}</div>
          )}
        </div>
        {consistency.deployment_sha_mismatches?.length ? (
          <div style={{ fontSize: 13, color: "var(--ink-2)" }}>
            {t("shaMismatches", { roles: consistency.deployment_sha_mismatches.join(", ") })}
          </div>
        ) : null}
        <div style={{ fontSize: 13, color: "var(--ink-2)" }}>
          <strong>{t("ignoredDifferences", { count: consistency.ignored_non_consumer_differences ?? 0 })}</strong>
          <div style={{ fontSize: 12.5, color: "var(--ink-3)", marginTop: 2 }}>{t("ignoredDifferencesExplain")}</div>
        </div>
      </div>
    </Section>
  );
}

// --- Avertissements -----------------------------------------------------------

export function WarningsSection({ warnings }: { warnings: CapabilityWarning[] }) {
  const t = useTranslations("documentCapabilities");
  return (
    <Section title={t("warningsTitle")} id="dc-warnings">
      {warnings.length === 0 ? (
        <div style={{ fontSize: 13, color: "var(--ink-3)" }}>{t("warningsEmpty")}</div>
      ) : (
        <ul data-testid="dc-warnings" style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: 8 }}>
          {warnings.map((w, i) => (
            <li
              key={`${w.code}-${i}`}
              data-code={w.code}
              style={{ border: "1px solid #EED4A4", background: "#FBF0DB", color: "#5C3304", borderRadius: 10, padding: "8px 12px", fontSize: 13 }}
            >
              <div style={{ fontWeight: 600 }}>{t(warningKey(w.code))}</div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: "2px 14px", fontSize: 12, marginTop: 2 }}>
                <span>
                  {t("warnCodeLabel")} : <code>{w.code}</code>
                </span>
                {w.capability_id ? <span>{t("warnCapability")} : <code>{w.capability_id}</code></span> : null}
                {w.flag ? <span>{t("warnFlag")} : <code>{w.flag}</code></span> : null}
                {w.scope ? <span>{t("warnScope")} : {w.scope}</span> : null}
                {w.service ? <span>{t("warnService")} : {w.service}</span> : null}
                {w.detail ? <span>{t("warnDetail")} : {w.detail}</span> : null}
              </div>
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}

// --- Vue par espace -----------------------------------------------------------

export function SpaceFilterSection({
  activeSpace,
  error,
  busy,
  onApply,
  onClear,
}: {
  activeSpace: { id: string; name: string } | null;
  error: string | null;
  busy: boolean;
  onApply: (ragspaceId: string) => void;
  onClear: () => void;
}) {
  const t = useTranslations("documentCapabilities");
  const [value, setValue] = useState(activeSpace?.id ?? "");
  const inputId = "dc-ragspace-id";
  return (
    <Section title={t("spaceFilterTitle")} id="dc-space" subtitle={t("spaceFilterBody")}>
      <form
        role="search"
        onSubmit={(e) => {
          e.preventDefault();
          if (value.trim()) onApply(value.trim());
        }}
        style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "flex-end" }}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 4, flex: "1 1 320px", minWidth: 0 }}>
          <label htmlFor={inputId} style={{ fontSize: 12.5, color: "var(--ink-2)", fontWeight: 600 }}>
            {t("spaceFilterLabel")}
          </label>
          <input
            id={inputId}
            type="text"
            inputMode="text"
            autoComplete="off"
            spellCheck={false}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            aria-invalid={error ? true : undefined}
            aria-describedby={error ? `${inputId}-error` : undefined}
            placeholder="00000000-0000-0000-0000-000000000000"
            style={{
              padding: "8px 10px",
              borderRadius: 8,
              border: "1px solid var(--border-strong)",
              fontSize: 13,
              fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
              background: "var(--surface)",
              color: "var(--ink)",
              width: "100%",
            }}
          />
        </div>
        <Button type="submit" variant="secondary" size="sm" icon="search" disabled={busy || !value.trim()}>
          {t("spaceFilterApply")}
        </Button>
        {activeSpace && (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => {
              setValue("");
              onClear();
            }}
            disabled={busy}
          >
            {t("spaceFilterClear")}
          </Button>
        )}
      </form>
      {error && (
        <div id={`${inputId}-error`} role="alert" style={{ marginTop: 8, fontSize: 13, color: "#A12F20" }}>
          {error}
        </div>
      )}
      {activeSpace && (
        <div data-testid="dc-space-active" style={{ marginTop: 8, fontSize: 13, color: "var(--ink)" }}>
          {t("spaceFilterActive", { name: activeSpace.name })} · <code style={codeStyle}>{activeSpace.id}</code>
        </div>
      )}
    </Section>
  );
}
