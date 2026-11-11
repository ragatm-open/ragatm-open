"use client";

import React, { useState } from "react";
import { Icon } from "./Icon";

type Variant = "primary" | "secondary" | "ghost" | "soft" | "danger";
type Size = "sm" | "md" | "lg";

const SIZES: Record<Size, { padding: string; fontSize: number; gap: number; radius: number }> = {
  sm: { padding: "7px 12px", fontSize: 13, gap: 6, radius: 8 },
  md: { padding: "10px 16px", fontSize: 14, gap: 8, radius: 9 },
  lg: { padding: "13px 20px", fontSize: 15, gap: 9, radius: 10 },
};

const VARIANTS: Record<Variant, React.CSSProperties> = {
  primary: { background: "var(--accent)", color: "#fff", border: "1px solid transparent", boxShadow: "0 1px 2px rgba(120,50,20,.18)" },
  secondary: { background: "var(--surface)", color: "var(--ink)", border: "1px solid var(--border)", boxShadow: "0 1px 1px rgba(40,30,20,.03)" },
  ghost: { background: "transparent", color: "var(--ink-2)", border: "1px solid transparent" },
  soft: { background: "var(--accent-soft)", color: "var(--accent-ink)", border: "1px solid transparent" },
  danger: { background: "var(--surface)", color: "var(--danger)", border: "1px solid var(--danger-border)" },
};

export function Button({
  children,
  variant = "primary",
  size = "md",
  icon,
  iconRight,
  onClick,
  disabled,
  style,
  full,
  type = "button",
  ...rest
}: {
  children?: React.ReactNode;
  variant?: Variant;
  size?: Size;
  icon?: string;
  iconRight?: string;
  full?: boolean;
} & React.ButtonHTMLAttributes<HTMLButtonElement>) {
  const sizes = SIZES[size];
  const variants = VARIANTS[variant];
  const [hover, setHover] = useState(false);
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
        gap: sizes.gap,
        padding: sizes.padding,
        fontSize: sizes.fontSize,
        fontWeight: 600,
        borderRadius: sizes.radius,
        cursor: disabled ? "not-allowed" : "pointer",
        fontFamily: "inherit",
        lineHeight: 1.1,
        width: full ? "100%" : "auto",
        whiteSpace: "nowrap",
        opacity: disabled ? 0.5 : 1,
        transition: "filter .15s, transform .04s, background .15s",
        filter: hover && !disabled ? "brightness(0.96)" : "none",
        ...variants,
        ...style,
      }}
      {...rest}
    >
      {icon && <Icon name={icon} size={sizes.fontSize + 3} />}
      {children}
      {iconRight && <Icon name={iconRight} size={sizes.fontSize + 3} />}
    </button>
  );
}
