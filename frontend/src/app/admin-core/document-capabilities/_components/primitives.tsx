"use client";

import React from "react";
import { useTranslations } from "next-intl";
import { Card, Icon } from "@/components/ui";
import { TONE_COLORS, type StatusMeta } from "../_lib/status";

/** Badge de statut : icône + TEXTE + couleur (jamais la couleur seule). */
export function StatusPill({
  meta,
  label,
  suffix,
  title,
}: {
  meta: StatusMeta;
  /** Libellé explicite ; par défaut, la traduction de `meta.key`. */
  label?: string;
  suffix?: React.ReactNode;
  title?: string;
}) {
  const t = useTranslations("documentCapabilities");
  const c = TONE_COLORS[meta.tone];
  return (
    <span
      title={title}
      data-tone={meta.tone}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 5,
        padding: "2px 8px",
        borderRadius: 999,
        fontSize: 12,
        fontWeight: 600,
        lineHeight: 1.5,
        color: c.color,
        background: c.bg,
        border: `1px solid ${c.border}`,
        whiteSpace: "nowrap",
      }}
    >
      <Icon name={meta.icon} size={13} stroke={2} aria-hidden="true" />
      <span>{label ?? t(meta.key)}</span>
      {suffix != null && <span style={{ fontWeight: 500 }}>{suffix}</span>}
    </span>
  );
}

export function Section({
  title,
  id,
  subtitle,
  children,
  actions,
}: {
  title: string;
  id?: string;
  subtitle?: React.ReactNode;
  children: React.ReactNode;
  actions?: React.ReactNode;
}) {
  const headingId = id ? `${id}-title` : undefined;
  return (
    <Card>
      <section aria-labelledby={headingId} style={{ padding: 18 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12, flexWrap: "wrap" }}>
          <div style={{ minWidth: 0 }}>
            <h2 id={headingId} style={{ fontSize: 16, fontWeight: 700, color: "var(--ink)", margin: 0 }}>
              {title}
            </h2>
            {subtitle && <p style={{ fontSize: 13, color: "var(--ink-3)", margin: "4px 0 0" }}>{subtitle}</p>}
          </div>
          {actions}
        </div>
        <div style={{ marginTop: 12 }}>{children}</div>
      </section>
    </Card>
  );
}

export function KV({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
      <span style={{ fontSize: 12, color: "var(--ink-3)" }}>{label}</span>
      <span style={{ fontSize: 13.5, color: "var(--ink)", fontWeight: 500, overflowWrap: "anywhere" }}>{children}</span>
    </div>
  );
}

export const thStyle: React.CSSProperties = {
  textAlign: "left",
  fontSize: 12,
  fontWeight: 700,
  color: "var(--ink-2)",
  padding: "8px 10px",
  borderBottom: "1px solid var(--border-strong)",
  whiteSpace: "nowrap",
};

export const tdStyle: React.CSSProperties = {
  fontSize: 13,
  color: "var(--ink)",
  padding: "8px 10px",
  borderBottom: "1px solid var(--border)",
  verticalAlign: "top",
};

export const codeStyle: React.CSSProperties = {
  fontSize: 12,
  color: "var(--ink)",
  background: "var(--chip)",
  padding: "1px 6px",
  borderRadius: 6,
  fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
};
