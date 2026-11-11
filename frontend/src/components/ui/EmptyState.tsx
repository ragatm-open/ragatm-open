import React from "react";
import { Icon } from "./Icon";

export function EmptyState({
  icon,
  title,
  body,
  action,
}: {
  icon: string;
  title: string;
  body: React.ReactNode;
  action?: React.ReactNode;
}) {
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        textAlign: "center",
        padding: "56px 24px",
        maxWidth: 420,
        margin: "0 auto",
      }}
    >
      <div
        style={{
          width: 60,
          height: 60,
          borderRadius: 16,
          background: "var(--accent-soft)",
          color: "var(--accent)",
          display: "grid",
          placeItems: "center",
          marginBottom: 18,
        }}
      >
        <Icon name={icon} size={28} stroke={1.6} />
      </div>
      <div style={{ fontSize: 17, fontWeight: 700, color: "var(--ink)", marginBottom: 7 }}>{title}</div>
      <div style={{ fontSize: 14, color: "var(--ink-2)", lineHeight: 1.55, marginBottom: action ? 20 : 0 }}>{body}</div>
      {action}
    </div>
  );
}
