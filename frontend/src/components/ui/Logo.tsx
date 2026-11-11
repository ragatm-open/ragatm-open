import React from "react";

export function Logo({
  size = 28,
  withText = true,
  color = "var(--accent)",
}: {
  size?: number;
  withText?: boolean;
  color?: string;
}) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
      <div
        style={{
          width: size,
          height: size,
          borderRadius: size * 0.28,
          flex: "none",
          background: color,
          position: "relative",
          display: "grid",
          placeItems: "center",
          boxShadow: "0 1px 2px rgba(120,50,20,.25)",
        }}
      >
        <svg
          width={size * 0.6}
          height={size * 0.6}
          viewBox="0 0 24 24"
          fill="none"
          stroke="#fff"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <path d="M6 4h8l4 4v10a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1Z" />
          <path d="M9 13h6 M9 16h4" />
        </svg>
      </div>
      {withText && (
        <span style={{ fontWeight: 700, fontSize: size * 0.62, letterSpacing: "-0.02em", color: "var(--ink)" }}>
          RAG<span style={{ color }}>atm</span>
        </span>
      )}
    </div>
  );
}

export function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        fontSize: 11.5,
        fontWeight: 700,
        letterSpacing: "0.06em",
        textTransform: "uppercase",
        color: "var(--ink-3)",
      }}
    >
      {children}
    </div>
  );
}
