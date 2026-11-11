"use client";

import React, { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Card, Icon } from "@/components/ui";
import { getPlatformDashboard, type PlatformDashboard as Dash } from "@/lib/platformAdmin";
import { humanAccessError } from "@/lib/apiError";

type TabKey = "dashboard" | "users" | "workspaces" | "diagnostics";

function Stat({ label, value, tone = "ink", icon }: { label: string; value: number | string; tone?: string; icon: string }) {
  const color =
    tone === "danger" ? "var(--danger)" : tone === "warning" ? "#8A6D3B" : "var(--ink)";
  return (
    <Card style={{ padding: "16px 18px", display: "flex", alignItems: "center", gap: 14 }}>
      <span style={{ width: 40, height: 40, borderRadius: 10, background: "var(--chip)", display: "grid", placeItems: "center", flex: "none" }}>
        <Icon name={icon} size={19} style={{ color }} />
      </span>
      <div>
        <div style={{ fontSize: 24, fontWeight: 700, color, lineHeight: 1.1 }}>{value}</div>
        <div style={{ fontSize: 12.5, color: "var(--ink-3)" }}>{label}</div>
      </div>
    </Card>
  );
}

export function PlatformDashboard({ onNavigate }: { onNavigate: (t: TabKey) => void }) {
  const t = useTranslations("platformAdmin");
  const [d, setD] = useState<Dash | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    getPlatformDashboard()
      .then((x) => alive && setD(x))
      .catch((e) => alive && setErr(humanAccessError(e) || t("dashboardLoadError")));
    return () => {
      alive = false;
    };
  }, [t]);

  if (err) return <Card style={{ padding: 20, color: "var(--danger)", fontSize: 13 }}>{err}</Card>;
  if (!d) return <Card style={{ padding: 32, color: "var(--ink-3)", fontSize: 14 }}>{t("loading")}</Card>;

  const anomalies = d.workspaces_without_owner + d.integrity_violations;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(190px, 1fr))", gap: 12 }}>
        <Stat label={t("statUsersActive")} value={d.users_active} icon="users" />
        <Stat label={t("statWorkspacesActive")} value={d.workspaces_active} icon="grid" />
        <Stat label={t("statWorkspacesDisabled")} value={d.workspaces_disabled} icon="lock" tone={d.workspaces_disabled ? "warning" : "ink"} />
        <Stat label={t("statWorkspacesWithoutOwner")} value={d.workspaces_without_owner} icon="alert" tone={d.workspaces_without_owner ? "warning" : "ink"} />
        <Stat label={t("statIntegrityViolations")} value={d.integrity_violations} icon="shield" tone={d.integrity_violations ? "danger" : "ink"} />
        <Stat label={t("statRagspacesWithoutScope")} value={d.ragspaces_without_owner_scope} icon="grid" tone={d.ragspaces_without_owner_scope ? "warning" : "ink"} />
      </div>

      <Card style={{ padding: 18 }}>
        <div style={{ fontSize: 13.5, fontWeight: 600, color: "var(--ink)", marginBottom: 10 }}>{t("quickAccess")}</div>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
          {(
            [
              ["users", t("users"), "users"],
              ["workspaces", t("workspaces"), "grid"],
              ["diagnostics", `${t("diagnostics")}${anomalies ? ` (${anomalies})` : ""}`, "shield"],
            ] as [TabKey, string, string][]
          ).map(([k, l, ic]) => (
            <button
              key={k}
              onClick={() => onNavigate(k)}
              style={{
                display: "inline-flex", alignItems: "center", gap: 8, padding: "9px 14px",
                borderRadius: 9, border: "1px solid var(--border)", background: "var(--surface)",
                cursor: "pointer", fontFamily: "inherit", fontSize: 13.5, fontWeight: 600, color: "var(--ink)",
              }}
            >
              <Icon name={ic} size={16} style={{ color: "var(--ink-3)" }} />
              {l}
            </button>
          ))}
        </div>
        <div style={{ fontSize: 12, color: "var(--ink-3)", marginTop: 12, lineHeight: 1.5 }}>
          {t.rich("governanceNote", { strong: (chunks) => <strong>{chunks}</strong> })}
        </div>
      </Card>
    </div>
  );
}
