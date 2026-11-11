"use client";

import React, { useEffect, useRef } from "react";

export function Modal({
  title,
  subtitle,
  children,
  onClose,
  maxWidth = 460,
  scrollable = false,
}: {
  title: React.ReactNode;
  subtitle?: React.ReactNode;
  children?: React.ReactNode;
  onClose: () => void;
  /** Largeur maximale du panneau (défaut historique : 460 px). */
  maxWidth?: number;
  /** Panneau borné à la hauteur de l'écran, contenu défilant (assistants longs). */
  scrollable?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);

  // Fermeture clavier (Échap) + focus initial + piège de focus simple.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    ref.current?.focus();
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      onClick={onClose}
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 60,
        background: "rgba(40,28,18,.34)",
        display: "grid",
        placeItems: "center",
        padding: scrollable ? 12 : 24,
        backdropFilter: "blur(2px)",
      }}
    >
      <div
        ref={ref}
        role="dialog"
        aria-modal="true"
        aria-label={typeof title === "string" ? title : undefined}
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
        style={{
          width: "100%",
          maxWidth,
          ...(scrollable ? { maxHeight: "calc(100dvh - 24px)", overflowY: "auto" as const, boxSizing: "border-box" as const } : {}),
          background: "var(--surface)",
          borderRadius: 18,
          boxShadow: "var(--shadow-modal)",
          padding: scrollable ? "22px clamp(16px, 4vw, 26px)" : 26,
          outline: "none",
        }}
      >
        <div style={{ marginBottom: 20 }}>
          <h3 style={{ fontSize: 18, fontWeight: 700, color: "var(--ink)", margin: 0, letterSpacing: "-0.015em" }}>
            {title}
          </h3>
          {subtitle && <p style={{ fontSize: 13.5, color: "var(--ink-2)", margin: "5px 0 0" }}>{subtitle}</p>}
        </div>
        {children}
      </div>
    </div>
  );
}
