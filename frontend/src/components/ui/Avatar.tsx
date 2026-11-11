import React from "react";

const PALETTE = ["#C2562F", "#0E7C86", "#5B53D9", "#1F8A5B", "#B5710E"];

/** Dérive les initiales d'un nom ou d'un email. */
export function initialsFrom(nameOrEmail: string | null | undefined): string {
  const s = (nameOrEmail || "").trim();
  if (!s) return "?";
  const base = s.includes("@") ? s.split("@")[0] : s;
  const parts = base.split(/[\s._-]+/).filter(Boolean);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return base.slice(0, 2).toUpperCase();
}

export function Avatar({
  initials,
  size = 34,
  color,
  ring,
}: {
  initials: string;
  size?: number;
  color?: string;
  ring?: boolean;
}) {
  const c = color || PALETTE[(initials?.charCodeAt(0) || 0) % PALETTE.length];
  return (
    <div
      style={{
        width: size,
        height: size,
        borderRadius: "50%",
        flex: "none",
        background: `${c}1f`,
        color: c,
        fontWeight: 700,
        fontSize: size * 0.38,
        display: "grid",
        placeItems: "center",
        letterSpacing: "0.01em",
        boxShadow: ring ? `0 0 0 2px var(--surface), 0 0 0 3.5px ${c}55` : "none",
      }}
    >
      {initials}
    </div>
  );
}
