"use client";

import { useTranslations } from "next-intl";
import { Button, Icon } from "@/components/ui";
import { useWorkspace } from "./WorkspaceContext";

/**
 * Bandeau d'échec TRANSITOIRE du chargement de la coquille (429 persistant, 403,
 * 5xx, réseau). L'utilisateur reste sur sa page et garde sa session : seul un 401
 * renvoie vers /login (cf. WorkspaceContext). La relance est manuelle — un clic,
 * une tentative — pour ne jamais réalimenter le throttle en arrière-plan.
 */
export function AuthIssueNotice() {
  const t = useTranslations("auth");
  const { authIssue, retryAuth, loading } = useWorkspace();
  if (!authIssue) return null;

  const message =
    authIssue.kind === "rate_limited"
      ? t("rateLimited", { seconds: Math.max(1, Math.ceil(authIssue.retryAfterMs / 1000)) })
      : t("serviceUnavailable");

  return (
    <div
      role="status"
      data-testid="auth-issue-notice"
      style={{
        display: "flex",
        gap: 10,
        alignItems: "center",
        margin: "16px 24px 0",
        padding: "10px 14px",
        borderRadius: 10,
        background: "var(--accent-soft)",
        border: "1px solid var(--border)",
        color: "var(--ink-2)",
        fontSize: 13.5,
      }}
    >
      <Icon name="alert" size={17} style={{ flex: "none" }} />
      <span style={{ flex: 1, minWidth: 0 }}>{message}</span>
      <Button size="sm" variant="secondary" icon="refresh" disabled={loading} onClick={retryAuth}>
        {t("retry")}
      </Button>
    </div>
  );
}
