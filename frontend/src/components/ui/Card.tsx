"use client";

import React, { useState } from "react";

export function Card({
  children,
  style,
  hover,
  onClick,
  ...rest
}: {
  children?: React.ReactNode;
  style?: React.CSSProperties;
  hover?: boolean;
  onClick?: () => void;
} & React.HTMLAttributes<HTMLDivElement>) {
  const [h, setH] = useState(false);
  return (
    <div
      onClick={onClick}
      onMouseEnter={() => setH(true)}
      onMouseLeave={() => setH(false)}
      style={{
        background: "var(--surface)",
        border: "1px solid var(--border)",
        borderRadius: 14,
        boxShadow: h && hover ? "var(--shadow-card-hover)" : "var(--shadow-card)",
        transition: "box-shadow .18s, transform .18s, border-color .18s",
        transform: h && hover ? "translateY(-2px)" : "none",
        borderColor: h && hover ? "var(--border-strong)" : "var(--border)",
        cursor: onClick ? "pointer" : "default",
        ...style,
      }}
      {...rest}
    >
      {children}
    </div>
  );
}
