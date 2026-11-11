"use client";

import React, { useState } from "react";
import { useTranslations } from "next-intl";
import { apiFetch } from "@/lib/api";
import { ApiError } from "@/lib/apiError";
import { Button, Icon } from "@/components/ui";

/**
 * Catégories de retour négatif.
 *
 * `info_in_docs` est la catégorie à plus forte valeur : elle affirme une vérité
 * vérifiable (le terme EST dans le corpus) là où les autres expriment une
 * insatisfaction. C'est la seule qui ouvre deux champs supplémentaires — quel
 * document, quel terme — parce qu'une réponse à ces deux questions se promeut
 * directement en test de non-régression.
 */
export type FeedbackKind =
  | "info_in_docs"
  | "wrong_sources"
  | "wrong_answer"
  | "wrong_language"
  | "other";

const FEEDBACK_KINDS: Array<{ key: FeedbackKind; labelKey: string }> = [
  { key: "info_in_docs", labelKey: "feedbackKindInfoInDocs" },
  { key: "wrong_sources", labelKey: "feedbackKindWrongSources" },
  { key: "wrong_answer", labelKey: "feedbackKindWrongAnswer" },
  { key: "wrong_language", labelKey: "feedbackKindWrongLanguage" },
  { key: "other", labelKey: "feedbackKindOther" },
];

/** Endpoint CANONIQUE et unique. */
const FEEDBACK_ENDPOINT = "/api/v1/chat/feedback/";

export function MessageActions({ text, logId }: { text: string; logId: string | null }) {
  const t = useTranslations("chat");
  const [copied, setCopied] = useState(false);
  const [vote, setVote] = useState<1 | -1 | null>(null);
  const [sending, setSending] = useState(false);
  const [openReason, setOpenReason] = useState(false);
  const [kind, setKind] = useState<FeedbackKind>("info_in_docs");
  const [comment, setComment] = useState("");
  const [expectedDocument, setExpectedDocument] = useState("");
  const [expectedTerm, setExpectedTerm] = useState("");
  const [status, setStatus] = useState<string | null>(null);

  async function postFeedback(score: 1 | -1, k?: FeedbackKind) {
    if (!logId) return;
    setVote(score);
    setSending(true);
    setStatus(t("sending"));

    const payload: Record<string, unknown> = { log_id: logId, score };
    if (k) {
      payload.kind = k;
      if (comment.trim()) payload.comment = comment.trim();
      if (k === "info_in_docs") {
        if (expectedDocument.trim()) payload.expected_document = expectedDocument.trim();
        if (expectedTerm.trim()) payload.expected_term = expectedTerm.trim();
      }
    }

    try {
      // Un SEUL appel, vers un SEUL endpoint. Le repli historique visait
      // `/api/v1/audit/feedback/`, une route qui n'a jamais existé : tout échec
      // légitime (404, 403, 409) produisait donc deux appels et un message
      // d'erreur générique qui masquait la vraie cause.
      await apiFetch(FEEDBACK_ENDPOINT, { method: "POST", body: JSON.stringify(payload) });
      setStatus(score === 1 ? t("feedbackThanksPositive") : t("feedbackThanks"));
      setOpenReason(false);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        // Le retour a bien été pris en compte — précédemment. On le dit, au
        // lieu d'afficher un échec qui inciterait à recommencer.
        setStatus(t("feedbackAlreadySent"));
        setOpenReason(false);
      } else {
        setVote(null);
        setStatus(t("sendFailed"));
      }
    } finally {
      setSending(false);
    }
  }

  const actionBtn = (on: boolean): React.CSSProperties => ({
    display: "inline-flex",
    alignItems: "center",
    gap: 5,
    padding: "5px 9px",
    borderRadius: 7,
    border: "1px solid var(--border)",
    background: on ? "var(--accent-soft)" : "var(--surface)",
    color: on ? "var(--accent-ink)" : "var(--ink-3)",
    fontFamily: "inherit",
    fontSize: 12,
    fontWeight: 600,
  });

  const fieldStyle: React.CSSProperties = {
    padding: 8,
    borderRadius: 9,
    border: "1px solid var(--border)",
    background: "var(--surface)",
    fontFamily: "inherit",
    fontSize: 13,
  };

  return (
    <div>
      <div style={{ display: "flex", gap: 7, marginTop: 4, alignItems: "center" }}>
        <button
          onClick={() => {
            navigator.clipboard?.writeText(text).catch(() => {});
            setCopied(true);
            setTimeout(() => setCopied(false), 1400);
          }}
          style={{ ...actionBtn(copied), cursor: "pointer" }}
        >
          <Icon name={copied ? "check" : "copy"} size={14} stroke={2} />
          {copied ? t("copied") : t("copy")}
        </button>

        {/* Le pouce haut reste volontairement SANS formulaire : demander un
            motif pour un compliment ferait chuter le taux de retour positif
            sans rien apporter au diagnostic. */}
        <button
          onClick={() => logId && postFeedback(1)}
          disabled={!logId || sending || vote != null}
          style={{ ...actionBtn(vote === 1), opacity: !logId ? 0.5 : 1, cursor: !logId || vote != null ? "default" : "pointer" }}
          title={!logId ? t("feedbackUnavailable") : t("feedbackHelpful")}
          aria-label={t("feedbackHelpful")}
        >
          <Icon name="thumbUp" size={14} stroke={1.9} />
        </button>

        <button
          onClick={() => logId && setOpenReason((o) => !o)}
          disabled={!logId || sending || vote != null}
          style={{ ...actionBtn(vote === -1), opacity: !logId ? 0.5 : 1, cursor: !logId || vote != null ? "default" : "pointer" }}
          title={!logId ? t("feedbackUnavailable") : t("feedbackImprove")}
          aria-label={t("feedbackImprove")}
        >
          <Icon name="thumbDown" size={14} stroke={1.9} />
        </button>

        {status && <span style={{ fontSize: 12, color: "var(--ink-3)" }}>{status}</span>}
      </div>

      {openReason && logId && (
        <div
          style={{
            marginTop: 8,
            border: "1px solid var(--border)",
            borderRadius: 12,
            padding: 12,
            background: "var(--surface)",
            display: "grid",
            gap: 8,
            maxWidth: 420,
          }}
        >
          <div style={{ fontSize: 12.5, color: "var(--ink-2)", fontWeight: 600 }}>{t("feedbackWhy")}</div>
          <select
            aria-label={t("feedbackReasonAria")}
            value={kind}
            onChange={(e) => setKind(e.target.value as FeedbackKind)}
            style={fieldStyle}
          >
            {FEEDBACK_KINDS.map((k) => (
              <option key={k.key} value={k.key}>
                {t(k.labelKey)}
              </option>
            ))}
          </select>

          {kind === "info_in_docs" && (
            <>
              <input
                aria-label={t("feedbackExpectedDocumentAria")}
                value={expectedDocument}
                onChange={(e) => setExpectedDocument(e.target.value)}
                placeholder={t("feedbackExpectedDocumentPlaceholder")}
                style={fieldStyle}
              />
              <input
                aria-label={t("feedbackExpectedTermAria")}
                value={expectedTerm}
                onChange={(e) => setExpectedTerm(e.target.value)}
                placeholder={t("feedbackExpectedTermPlaceholder")}
                style={fieldStyle}
              />
            </>
          )}

          <textarea
            aria-label={t("feedbackDetailsAria")}
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            placeholder={t("feedbackDetailsPlaceholder")}
            rows={3}
            style={{ ...fieldStyle, resize: "vertical" }}
          />
          <div style={{ display: "flex", gap: 8 }}>
            <Button size="sm" onClick={() => postFeedback(-1, kind)} disabled={sending}>
              {t("send")}
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setOpenReason(false)} disabled={sending}>
              {t("cancel")}
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
