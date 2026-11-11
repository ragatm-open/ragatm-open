"use client";

import React from "react";
import { useTranslations } from "next-intl";
import { Icon } from "./Icon";

export type DocStatus = "ready" | "analyzing" | "queued" | "error";

// Aspect visuel par statut. Le libellé est LOCALISÉ à l'affichage via le
// namespace `statuses` (clé = nom du statut) plutôt que codé en dur ici.
export const RG_STATUS: Record<DocStatus, { color: string; bg: string; icon: string }> = {
  ready: { color: "var(--success)", bg: "var(--success-soft)", icon: "checkCircle" },
  analyzing: { color: "var(--warning)", bg: "var(--warning-soft)", icon: "spark" },
  queued: { color: "#6B635B", bg: "#F0ECE7", icon: "clock" },
  error: { color: "var(--danger)", bg: "var(--danger-soft)", icon: "alert" },
};

export function StatusBadge({ status, size = "md" }: { status: DocStatus; size?: "sm" | "md" }) {
  const t = useTranslations("statuses");
  const s = RG_STATUS[status] || RG_STATUS.queued;
  const key: DocStatus = RG_STATUS[status] ? status : "queued";
  const pad = size === "sm" ? "3px 8px" : "4px 10px";
  const fs = size === "sm" ? 11.5 : 12.5;
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 5,
        padding: pad,
        borderRadius: 999,
        fontSize: fs,
        fontWeight: 600,
        color: s.color,
        background: s.bg,
        whiteSpace: "nowrap",
      }}
    >
      <Icon name={s.icon} size={fs + 1} stroke={2} />
      {t(key)}
    </span>
  );
}

export function Pill({
  children,
  color = "var(--ink-2)",
  bg = "var(--chip)",
  style,
}: {
  children?: React.ReactNode;
  color?: string;
  bg?: string;
  style?: React.CSSProperties;
}) {
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 5,
        padding: "3px 9px",
        borderRadius: 999,
        fontSize: 12,
        fontWeight: 600,
        color,
        background: bg,
        ...style,
      }}
    >
      {children}
    </span>
  );
}
