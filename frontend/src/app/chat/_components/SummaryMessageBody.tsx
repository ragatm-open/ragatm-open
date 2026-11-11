"use client";

import React from "react";
import { useTranslations } from "next-intl";

import { AssistantMarkdown } from "./AssistantMarkdown";
import type { Msg } from "../_lib/types";

/**
 * Corps d'un message assistant.
 *
 * Le rendu est piloté par le `status` PERSISTÉ, jamais par un état React : c'est
 * ce qui permet de reconstruire un résumé « en cours » ou « en échec » à partir
 * du seul historique serveur, après un refresh ou une réouverture du fil.
 *
 * Tout statut absent ou inconnu — dont l'intégralité des messages antérieurs à
 * la migration chat 0007 — dégrade vers le rendu Markdown standard.
 */
export function SummaryMessageBody({ m }: { m: Msg }) {
  const t = useTranslations("chat");
  if (m.status === "pending") {
    return (
      <div
        role="status"
        style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13.5, color: "var(--muted)" }}
      >
        <span aria-hidden>⏳</span>
        <span>{t("summaryGenerating")}</span>
      </div>
    );
  }

  if (m.status === "failed") {
    return (
      <div
        role="alert"
        style={{
          borderRadius: 10,
          border: "1px solid #F0C2C2",
          background: "var(--danger-soft, #FDF2F2)",
          padding: "9px 12px",
          fontSize: 13,
          lineHeight: 1.45,
          color: "#8A2B2B",
        }}
      >
        {m.text || t("summaryFailed")}
      </div>
    );
  }

  return <AssistantMarkdown content={m.text} />;
}
