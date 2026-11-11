"use client";

import React, { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Card, EmptyState, Icon, Pill, SectionLabel } from "@/components/ui";
import { getPlatformDiagnostics, type PlatformDiagnostics } from "@/lib/platformAdmin";
import { humanAccessError } from "@/lib/apiError";
import { anomalyLabelKey, ANOMALY_TONE } from "../_lib/anomalies";

export function DiagnosticsTab() {
  const t = useTranslations("platformAdmin");
  const anomalyText = (code: string) => {
    const k = anomalyLabelKey(code);
    return k ? t(k) : code;
  };
  const [d, setD] = useState<PlatformDiagnostics | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    getPlatformDiagnostics()
      .then((x) => alive && setD(x))
      .catch((e) => alive && setErr(humanAccessError(e) || t("diagnosticsLoadError")))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [t]);

  if (loading) return <Card style={{ padding: 32, color: "var(--ink-3)", fontSize: 14 }}>{t("loading")}</Card>;
  if (err) return <Card style={{ padding: 20, color: "var(--danger)", fontSize: 13 }}>{err}</Card>;
  if (!d) return null;

  const withAnomalies = d.workspaces.filter((w) => (w.anomalies || []).length > 0);
  const strictOk = d.summary.strict_exit_code === 0;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <div
        style={{
          display: "flex", alignItems: "center", gap: 10, padding: "12px 16px", borderRadius: 10,
          background: strictOk ? "var(--accent-soft)" : "var(--danger-soft)",
          border: `1px solid ${strictOk ? "var(--border)" : "var(--danger-border)"}`,
        }}
      >
        <Icon name={strictOk ? "checkCircle" : "alert"} size={18} style={{ color: strictOk ? "var(--success)" : "var(--danger)" }} />
        <div style={{ fontSize: 13, color: "var(--ink-2)" }}>
          {strictOk ? t("strictOk") : t("strictKo")}
          {"  "}
          {t("integrityViolated")} <strong>{d.summary.integrity_violations}</strong> · {t("withoutOwnerLabel")}{" "}
          <strong>{d.summary.without_owner}</strong> · {t("multipleOwnersLabel")} <strong>{d.summary.multiple_owners}</strong>
        </div>
      </div>

      <SectionLabel>
        {t("workspacesWithAnomalies", { count: withAnomalies.length, total: d.summary.total })}
      </SectionLabel>

      {withAnomalies.length === 0 ? (
        <Card style={{ padding: 8 }}>
          <EmptyState icon="checkCircle" title={t("noAnomalyTitle")} body={t("noAnomalyBody")} />
        </Card>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {withAnomalies.map((w) => (
            <Card key={w.id} style={{ padding: 14 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8 }}>
                <span style={{ fontSize: 14, fontWeight: 600, color: "var(--ink)" }}>
                  {w.name} <span style={{ color: "var(--ink-3)", fontWeight: 400 }}>· ws#{w.id}</span>
                </span>
                {w.has_integrity_violation && (
                  <Pill color="#8A2E20" bg="var(--danger-soft)">{t("integrityPill")}</Pill>
                )}
                {!w.is_active && <Pill>{t("disabledPill")}</Pill>}
              </div>
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                {Array.from(new Set((w.anomalies || []).map((a) => a.code))).map((code) => {
                  const tone = ANOMALY_TONE[code] || "info";
                  const bg = tone === "integrity" ? "var(--danger-soft)" : tone === "decision" ? "var(--warning-soft, #FBF3E0)" : "var(--chip)";
                  const color = tone === "integrity" ? "#8A2E20" : tone === "decision" ? "#8A6D3B" : "var(--ink-2)";
                  return (
                    <Pill key={code} color={color} bg={bg}>
                      {anomalyText(code)}
                    </Pill>
                  );
                })}
              </div>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
