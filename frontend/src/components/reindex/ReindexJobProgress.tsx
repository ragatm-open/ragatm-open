"use client";

import React from "react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui";
import { isTerminal, type ReindexJob } from "@/lib/reindex";

/**
 * Progression d'un job de réindexation — composant PARTAGÉ.
 *
 * Réindexation globale d'un espace et reprise ciblée des documents en erreur sont
 * le même job, avec le même contrat de suivi : un seul affichage, donc, plutôt que
 * deux qui divergeraient au premier champ ajouté. Le `kind` du job ne change que
 * le libellé de tête ; les compteurs et la barre sont identiques.
 *
 * Les libellés vivent dans le namespace `workspaceAdmin`, où ils existaient déjà.
 */
// Statut de job → CLÉ i18n (namespace `workspaceAdmin`), traduite au rendu.
export const REINDEX_STATUS_KEY: Record<string, string> = {
  pending: "reindexPending",
  queuing: "reindexQueuing",
  running: "reindexRunning",
  completed: "reindexCompleted",
  completed_with_errors: "reindexCompletedWithErrors",
  failed: "reindexFailed",
  cancelled: "reindexCancelled",
  interrupted: "reindexInterrupted",
};

export function ReindexJobProgress({
  job,
  onRetry,
  title,
  style,
}: {
  job: ReindexJob;
  onRetry: () => void;
  /** Intitulé optionnel au-dessus de la barre (ex. « Reprise des documents en erreur »). */
  title?: string;
  style?: React.CSSProperties;
}) {
  const t = useTranslations("workspaceAdmin");
  const terminal = isTerminal(job.status);
  const statusKey = REINDEX_STATUS_KEY[job.status];
  const label = statusKey ? t(statusKey) : job.status;
  const barColor =
    job.status === "completed"
      ? "var(--ok, #16a34a)"
      : job.failed > 0 || job.status === "failed" || job.status === "interrupted"
        ? "var(--danger, #dc2626)"
        : "var(--accent-ink)";
  return (
    <div style={{ marginTop: 7, ...style }} role="status" aria-live="polite">
      {title && (
        <div style={{ fontSize: 12.5, fontWeight: 600, color: "var(--ink-2)", marginBottom: 3 }}>
          {title}
        </div>
      )}
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11.5, color: "var(--ink-3)" }}>
        <span>
          {label} — {t("reindexDone", { done: job.succeeded, total: job.total })}
          {job.failed > 0 ? t("reindexErrorsSuffix", { count: job.failed }) : ""}
          {job.running > 0 ? t("reindexRunningSuffix", { count: job.running }) : ""}
        </span>
        <span>{job.progress_percent}%</span>
      </div>
      <div style={{ height: 5, borderRadius: 3, background: "var(--border)", overflow: "hidden", marginTop: 3 }}>
        <div style={{ height: "100%", width: `${job.progress_percent}%`, background: barColor, transition: "width .4s" }} />
      </div>
      {terminal && job.failed > 0 && (
        <Button size="sm" variant="ghost" icon="refresh" style={{ marginTop: 5 }} onClick={onRetry}>
          {t("reindexRetryFailed", { count: job.failed })}
        </Button>
      )}
    </div>
  );
}
