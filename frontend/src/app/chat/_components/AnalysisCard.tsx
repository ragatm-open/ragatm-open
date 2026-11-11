"use client";

/**
 * Carte de suivi d'une analyse globale de corpus.
 *
 * Affiche, dans cet ordre : l'état, l'avertissement éventuel, le RÉSULTAT, ses
 * sources, puis le périmètre. Le résultat vient avant la couverture : on ne fait
 * pas parcourir dix lignes de métriques pour atteindre ce que l'analyse a produit.
 *
 * Le périmètre RÉEL est affiché : documents considérés, analysés, exclus, extraits
 * parcourus, candidats. La carte ne promet jamais une exhaustivité sémantique —
 * `summary_markdown` porte lui-même ce rappel, on ne le redouble pas ici.
 */
import { useTranslations } from "next-intl";
import type { CorpusAnalysis, GlobalSearchResult } from "@/lib/api";
import { AssistantMarkdown } from "./AssistantMarkdown";

const fr = (n: number) => n.toLocaleString("fr-FR").replace(/ | /g, " ");

/** Clés de traduction des états (namespace `chat`) — jamais le libellé fini. */
const STATUS_LABEL_KEYS: Record<string, string> = {
  pending: "analysisStatusPending",
  running: "analysisStatusRunning",
  succeeded: "analysisStatusSucceeded",
  partial: "analysisStatusPartial",
  failed: "analysisStatusFailed",
  cancelled: "analysisStatusCancelled",
};

const PHASE_LABEL_KEYS: Record<string, string> = {
  queued: "analysisPhaseQueued",
  counting_scope: "analysisPhaseCountingScope",
  searching_candidates: "analysisPhaseSearchingCandidates",
  processing_batches: "analysisPhaseProcessingBatches",
  consolidating: "analysisPhaseConsolidating",
  persisting_result: "analysisPhasePersistingResult",
  completed: "analysisPhaseCompleted",
  cancelled: "analysisPhaseCancelled",
  failed: "analysisPhaseFailed",
};

/** Raisons de partialité connues du backend (`coverage.limits_applied`). */
const LIMIT_LABEL_KEYS: Record<string, string> = {
  failed_batches: "analysisLimitFailedBatches",
};

/** Un document cité, avec les pages où il l'a été. */
type CitedDocument = { documentId: string; filename: string | null; pages: number[] };

/**
 * Regroupe les sources de tous les items par document. Un même document cité par
 * dix items n'apparaît qu'UNE fois — la liste des sources n'est pas un doublon de
 * la liste des énoncés.
 */
function citedDocuments(items: GlobalSearchResult["items"]): CitedDocument[] {
  const byDoc = new Map<string, CitedDocument>();

  for (const item of items) {
    for (const source of item.sources) {
      const current = byDoc.get(source.document_id);
      if (!current) {
        byDoc.set(source.document_id, {
          documentId: source.document_id,
          filename: source.filename ?? null,
          pages: source.page == null ? [] : [source.page],
        });
        continue;
      }
      if (source.page != null && !current.pages.includes(source.page)) {
        current.pages.push(source.page);
      }
    }
  }

  return [...byDoc.values()].map((d) => ({ ...d, pages: [...d.pages].sort((a, b) => a - b) }));
}

export function AnalysisCard({
  analysis,
  onCancel,
  cancelling = false,
}: {
  analysis: CorpusAnalysis;
  onCancel?: (id: string) => void;
  cancelling?: boolean;
}) {
  const t = useTranslations("chat");
  const { status, progress, coverage } = analysis;
  const statusLabel = STATUS_LABEL_KEYS[status] ? t(STATUS_LABEL_KEYS[status]) : status;
  const phaseLabel = PHASE_LABEL_KEYS[progress.phase] ? t(PHASE_LABEL_KEYS[progress.phase]) : progress.phase;
  const active = status === "pending" || status === "running";
  const result = analysis.result as GlobalSearchResult | null;
  const search = result?.search;

  // Un résultat n'est affiché QUE dans les états où le backend en produit un.
  // Ni un job en cours, ni un job annulé ou échoué ne doit exposer un reliquat.
  const hasResult = status === "succeeded" || status === "partial";
  const summary = hasResult && typeof result?.summary_markdown === "string"
    ? result.summary_markdown.trim()
    : "";
  const documents = hasResult && Array.isArray(result?.items) ? citedDocuments(result.items) : [];

  const partialReasons = (coverage?.limits_applied ?? [])
    .map((limit) => (LIMIT_LABEL_KEYS[limit] ? t(LIMIT_LABEL_KEYS[limit]) : null))
    .filter(Boolean);

  return (
    <section
      aria-label={statusLabel}
      data-testid="analysis-card"
      data-status={status}
      style={{
        border: "1px solid var(--line, #ddd)",
        borderRadius: 12,
        padding: 16,
        margin: "12px 0",
        display: "flex",
        flexDirection: "column",
        gap: 10,
      }}
    >
      <header style={{ display: "flex", alignItems: "center", gap: 8 }}>
        {/* L'état n'est jamais porté par la seule couleur : il est écrit. */}
        <strong>{statusLabel}</strong>
        {active && (
          <span style={{ fontSize: 12.5, color: "var(--ink-3, #666)" }}>
            · {phaseLabel}
          </span>
        )}
      </header>

      {active && (
        <>
          <div
            role="progressbar"
            aria-valuenow={progress.percent}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-label={t("analysisProgressAria")}
            style={{
              height: 6,
              borderRadius: 999,
              background: "var(--chip, #eee)",
              overflow: "hidden",
            }}
          >
            <div
              style={{
                width: `${progress.percent}%`,
                height: "100%",
                background: "var(--accent, #0E7C86)",
              }}
            />
          </div>
          <div style={{ fontSize: 12.5, color: "var(--ink-3, #666)" }}>
            {t("analysisProgress", { percent: progress.percent })}
          </div>
          {onCancel && (
            <button
              type="button"
              onClick={() => onCancel(analysis.analysis_id)}
              disabled={cancelling}
              aria-label={t("cancelAnalysisAria")}
              style={{ alignSelf: "flex-start", padding: "6px 12px", borderRadius: 8 }}
            >
              {cancelling ? t("cancelling") : t("cancel")}
            </button>
          )}
        </>
      )}

      {status === "failed" && (
        <p style={{ margin: 0, fontSize: 13.5 }}>
          {analysis.error_message ?? t("analysisFailedFallback")}
        </p>
      )}

      {status === "cancelled" && (
        <p style={{ margin: 0, fontSize: 13.5 }}>{t("analysisCancelledMessage")}</p>
      )}

      {/* Avertissement AVANT le résultat : on ne lit pas un résultat partiel en
          croyant qu'il est complet. La raison vient du backend, jamais devinée. */}
      {status === "partial" && (
        <p
          role="status"
          style={{
            margin: 0,
            fontSize: 13,
            padding: "8px 10px",
            borderRadius: 8,
            border: "1px solid var(--danger, #C2562F)",
            color: "var(--danger, #C2562F)",
          }}
        >
          <strong>{t("partialResultLabel")}</strong> {t("partialResultBody")}
          {partialReasons.length > 0 ? t("partialResultReasons", { reasons: partialReasons.join(", ") }) : ""}
          {t("partialResultSuffix")}
        </p>
      )}

      {hasResult && summary && (
        <div data-testid="analysis-result">
          {/* Même moteur de rendu que les réponses du chat : mêmes tableaux, mêmes
              liens sûrs, même refus de l'HTML brut. Aucun second renderer. */}
          <AssistantMarkdown content={summary} />
        </div>
      )}

      {/* Le backend produit toujours un texte, même sans correspondance. Une chaîne
          vide est donc une anomalie : on le dit, plutôt que d'afficher un blanc. */}
      {hasResult && !summary && (
        <p data-testid="analysis-result-empty" style={{ margin: 0, fontSize: 13.5 }}>
          {t("analysisNoTextResult")}
        </p>
      )}

      {documents.length > 0 && (
        <div
          data-testid="analysis-sources"
          style={{ fontSize: 13, display: "flex", flexDirection: "column", gap: 3 }}
        >
          <strong style={{ fontSize: 13 }}>{t("sourcesTitle")}</strong>
          <ul style={{ margin: 0, paddingLeft: 18, display: "flex", flexDirection: "column", gap: 2 }}>
            {documents.map((doc) => (
              <li key={doc.documentId}>
                {doc.filename ?? t("documentNotFound")}
                {doc.pages.length > 0 && (
                  <span style={{ color: "var(--ink-3, #666)" }}>
                    {" "}
                    — {t("analysisPagesLabel", { count: doc.pages.length, pages: doc.pages.join(", ") })}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {coverage && hasResult && (
        <div
          data-testid="analysis-coverage"
          style={{ fontSize: 13, display: "flex", flexDirection: "column", gap: 3 }}
        >
          <strong style={{ fontSize: 13 }}>{t("coverageScope")}</strong>
          <span>{t("coverageDocsConsidered", { count: coverage.documents_considered, value: fr(coverage.documents_considered) })}</span>
          <span>{t("coverageDocsAnalyzed", { count: coverage.documents_analyzed, value: fr(coverage.documents_analyzed) })}</span>
          {coverage.documents_excluded.map((e) => (
            <span key={e.reason} style={{ color: "var(--danger, #C2562F)" }}>
              {t("coverageExcluded", { count: e.count, value: fr(e.count), label: e.label })}
            </span>
          ))}
          {search && (
            <>
              <span>{t("coverageChunksScanned", { count: search.chunks_scanned, value: fr(search.chunks_scanned) })}</span>
              <span>
                {t("coverageCandidates", {
                  count: search.candidate_chunks,
                  value: fr(search.candidate_chunks),
                  docs: fr(search.candidate_documents),
                })}
              </span>
            </>
          )}
          <span>
            {t("coverageRatio", {
              percent: Math.round(coverage.coverage_ratio * 100),
              level: coverage.coverage_level,
            })}
          </span>
        </div>
      )}
    </section>
  );
}
