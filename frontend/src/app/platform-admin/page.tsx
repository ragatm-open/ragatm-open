"use client";

import React, { useState } from "react";
import { useTranslations } from "next-intl";
import { AppShell, Topbar, useWorkspace } from "@/components/shell";
import { Card, EmptyState, Icon } from "@/components/ui";
import { PlatformDashboard } from "./_components/PlatformDashboard";
import { DiagnosticsTab } from "./_components/DiagnosticsTab";
import { UsersTab } from "./_components/UsersTab";
import { WorkspacesTab } from "./_components/WorkspacesTab";
import layout from "./_components/PlatformAdmin.module.css";

export default function PlatformAdminPage() {
  return (
    <AppShell>
      <PlatformGate />
    </AppShell>
  );
}

type Tab = "dashboard" | "users" | "workspaces" | "diagnostics";

function PlatformGate() {
  const t = useTranslations("platformAdmin");
  const { isSuperuser, loading } = useWorkspace();
  if (loading) {
    return (
      <>
        <Topbar title={t("title")} />
        <div style={{ padding: 32, color: "var(--ink-3)" }}>{t("loading")}</div>
      </>
    );
  }
  if (!isSuperuser) {
    return (
      <>
        <Topbar title={t("title")} />
        <div style={{ padding: "48px 32px", maxWidth: 640, margin: "0 auto" }}>
          <Card style={{ padding: 8 }}>
            <EmptyState
              icon="lock"
              title={t("superuserOnlyTitle")}
              body={t("superuserOnlyBody")}
            />
          </Card>
        </div>
      </>
    );
  }
  return <PlatformContent />;
}

function PlatformContent() {
  const t = useTranslations("platformAdmin");
  const [tab, setTab] = useState<Tab>("dashboard");
  return (
    <>
      <Topbar title={t("title")} subtitle={t("subtitle")} />
      <div className={layout.page} style={{ maxWidth: 1100, margin: "0 auto" }}>
        <div
          role="tablist"
          className={layout.tabs}
          style={{
            display: "flex",
            gap: 4,
            padding: 4,
            borderRadius: 10,
            background: "var(--chip)",
            border: "1px solid var(--border)",
            width: "fit-content",
            marginBottom: 22,
          }}
        >
          {(
            [
              ["dashboard", t("tabDashboard"), "home"],
              ["users", t("users"), "users"],
              ["workspaces", t("workspaces"), "grid"],
              ["diagnostics", t("diagnostics"), "shield"],
            ] as [Tab, string, string][]
          ).map(([k, l, ic]) => (
            <button
              key={k}
              role="tab"
              aria-selected={tab === k}
              onClick={() => setTab(k)}
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 7,
                padding: "7px 14px",
                borderRadius: 7,
                border: "none",
                cursor: "pointer",
                fontFamily: "inherit",
                fontSize: 13.5,
                fontWeight: 600,
                background: tab === k ? "var(--surface)" : "transparent",
                color: tab === k ? "var(--ink)" : "var(--ink-3)",
                boxShadow: tab === k ? "0 1px 2px rgba(60,40,25,.1)" : "none",
              }}
            >
              <Icon name={ic} size={16} />
              {l}
            </button>
          ))}
        </div>

        {tab === "dashboard" && <PlatformDashboard onNavigate={setTab} />}
        {tab === "users" && <UsersTab />}
        {tab === "workspaces" && <WorkspacesTab />}
        {tab === "diagnostics" && <DiagnosticsTab />}
      </div>
    </>
  );
}
