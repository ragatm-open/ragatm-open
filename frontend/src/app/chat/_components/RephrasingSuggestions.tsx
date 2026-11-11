"use client";

import React from "react";
import { useTranslations } from "next-intl";

/**
 * Suggestions de reformulation affichées SOUS un message de non-réponse
 * (status "no_answer"). Chaque suggestion est un bouton accessible : au clic,
 * la suggestion est RECOPIÉE dans le composer (via ``onPick``) — elle n'est
 * JAMAIS envoyée automatiquement, l'utilisateur peut l'éditer avant d'envoyer.
 *
 * - masqué si la liste est vide ;
 * - navigation clavier native (<button>) + focus visible ;
 * - flex-wrap => lisible sur mobile.
 */
export function RephrasingSuggestions({
  suggestions,
  onPick,
  disabled = false,
}: {
  suggestions: string[];
  onPick: (s: string) => void;
  disabled?: boolean;
}) {
  const t = useTranslations("chat");
  const items = (suggestions || []).filter((s) => typeof s === "string" && s.trim().length > 0);
  if (items.length === 0) return null;

  return (
    <div style={{ marginTop: 12 }}>
      <div
        style={{
          fontSize: 12.5,
          fontWeight: 600,
          color: "var(--ink-3)",
          marginBottom: 8,
        }}
      >
        {t("tryInstead")}
      </div>
      <div
        role="group"
        aria-label={t("rephrasingSuggestionsAria")}
        style={{ display: "flex", flexDirection: "column", gap: 8 }}
      >
        {items.map((s, i) => (
          <button
            key={`${i}-${s}`}
            type="button"
            onClick={() => onPick(s)}
            disabled={disabled}
            aria-label={t("rephraseAria", { text: s })}
            style={{
              display: "block",
              width: "100%",
              textAlign: "left",
              padding: "9px 13px",
              borderRadius: 10,
              border: "1px solid var(--border)",
              background: "var(--surface)",
              fontFamily: "inherit",
              fontSize: 13.5,
              lineHeight: 1.5,
              color: "var(--ink-2)",
              cursor: disabled ? "default" : "pointer",
            }}
            onMouseEnter={(e) => {
              if (!disabled) {
                e.currentTarget.style.borderColor = "var(--accent)";
                e.currentTarget.style.color = "var(--accent-ink)";
              }
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.borderColor = "var(--border)";
              e.currentTarget.style.color = "var(--ink-2)";
            }}
          >
            {s}
          </button>
        ))}
      </div>
    </div>
  );
}
