"use client";

import React, { useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { Icon, Popover } from "@/components/ui";
import { useWorkspace } from "./WorkspaceContext";

// Rôle backend → clé i18n (namespace `workspaces`). Le backend reste l'autorité.
const ROLE_LABEL_KEY: Record<string, string> = {
  owner: "owner",
  admin: "administrator",
  member: "contributor",
  viewer: "viewer",
};

/**
 * Sélecteur d'espace de travail (Workspace). Permet à un utilisateur membre de
 * plusieurs workspaces de basculer entre eux. La bascule recharge les RagSpaces,
 * le rôle effectif et les caches liés (via WorkspaceContext.setWorkspaceId).
 */
export function WorkspaceSwitcher() {
  const { workspaces, workspaceId, setWorkspaceId } = useWorkspace();
  const t = useTranslations("workspaces");
  const [open, setOpen] = useState(false);
  const btnRef = useRef<HTMLButtonElement>(null);

  const roleLabel = (role: string): string => {
    const key = ROLE_LABEL_KEY[(role || "").toLowerCase()];
    return key ? t(key) : role || "—";
  };

  // Rendu uniquement s'il y a un choix réel (>= 2 workspaces). Défensif si la
  // liste n'est pas encore chargée.
  const list = workspaces || [];
  if (list.length < 2) return null;

  const cur = list.find((w) => w.id === workspaceId) || list[0] || null;

  return (
    <div style={{ position: "relative" }}>
      <button
        ref={btnRef}
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={t("switchWorkspace")}
        style={{
          width: "100%",
          display: "flex",
          alignItems: "center",
          gap: 9,
          padding: "8px 10px",
          borderRadius: 10,
          border: "1px solid var(--border)",
          background: "var(--surface)",
          cursor: "pointer",
          fontFamily: "inherit",
          textAlign: "left",
        }}
      >
        <span style={{ width: 22, height: 22, borderRadius: 6, flex: "none", background: "var(--ink-2)", display: "grid", placeItems: "center" }}>
          <Icon name="home" size={13} style={{ color: "#fff" }} />
        </span>
        <span style={{ flex: 1, minWidth: 0 }}>
          <span style={{ display: "block", fontSize: 12.5, fontWeight: 600, color: "var(--ink)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {cur ? cur.name : t("workspace")}
          </span>
          <span style={{ display: "block", fontSize: 11, color: "var(--ink-3)" }}>
            {cur ? roleLabel(cur.role) : "—"}
          </span>
        </span>
        <Icon name="chevronD" size={15} style={{ color: "var(--ink-3)" }} />
      </button>

      <Popover open={open} onClose={() => setOpen(false)} anchorRef={btnRef} matchWidth maxHeight={320}>
        {list.map((w) => {
          const active = w.id === workspaceId;
          return (
            <button
              key={w.id}
              role="option"
              aria-selected={active}
              onClick={() => {
                if (w.id !== workspaceId) setWorkspaceId(w.id);
                setOpen(false);
              }}
              style={{
                width: "100%",
                display: "flex",
                alignItems: "center",
                gap: 10,
                padding: "8px 9px",
                borderRadius: 8,
                border: "none",
                background: active ? "var(--accent-soft)" : "transparent",
                cursor: "pointer",
                fontFamily: "inherit",
                textAlign: "left",
              }}
              onMouseEnter={(e) => { if (!active) e.currentTarget.style.background = "var(--hover)"; }}
              onMouseLeave={(e) => { if (!active) e.currentTarget.style.background = "transparent"; }}
            >
              <span style={{ flex: 1, minWidth: 0 }}>
                <span style={{ display: "block", fontSize: 13, fontWeight: 500, color: "var(--ink)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{w.name}</span>
                <span style={{ display: "block", fontSize: 11, color: "var(--ink-3)" }}>{roleLabel(w.role)}</span>
              </span>
              {active && <Icon name="check" size={15} style={{ color: "var(--accent)" }} stroke={2.4} />}
            </button>
          );
        })}
      </Popover>
    </div>
  );
}
