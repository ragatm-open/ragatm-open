"use client";

import React, { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Button, Card } from "@/components/ui";
import { getWorkspaceQuotaDefaults, type WorkspaceQuotaDefaults } from "@/lib/platformAdmin";
import { setGlobalConfigOverride } from "@/lib/coreConfig";
import { isApiError } from "@/lib/apiError";
import { fileSize, formatCount } from "@/lib/format";
import {
  QuotaLimitsFields,
  type QuotaFieldsValue,
} from "@/app/platform-admin/_components/QuotaFields";

/**
 * Administration des DÉFAUTS DE PLATEFORME des quotas documentaires (superuser).
 *
 * Panneau interactif AUTONOME (fetch + save propres) : c'est la seule écriture de
 * l'écran admin-core, volontairement isolée. L'écriture passe par l'endpoint
 * d'override CoreConfig, lui-même soumis au gate serveur (prod interdit +
 * CORE_CONFIG_EDIT_ENABLED) : un 403/409 est restitué comme message, jamais
 * contourné. Les défauts modifiés ne s'appliquent qu'aux workspaces créés ENSUITE.
 */
export function WorkspaceQuotaDefaultsPanel() {
  const t = useTranslations("adminCore");
  const [defaults, setDefaults] = useState<WorkspaceQuotaDefaults | null>(null);
  const [quota, setQuota] = useState<QuotaFieldsValue | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [ok, setOk] = useState(false);

  const reload = () => {
    getWorkspaceQuotaDefaults()
      .then((d) => setDefaults(d))
      .catch(() => setDefaults(null));
  };
  useEffect(() => { reload(); }, []);

  async function save() {
    if (!quota || !quota.valid) return;
    setBusy(true); setErr(null); setOk(false);
    try {
      await setGlobalConfigOverride("WORKSPACE_DEFAULT_MAX_DOCUMENT_COUNT", quota.count);
      await setGlobalConfigOverride("WORKSPACE_DEFAULT_MAX_DOCUMENT_BYTES", quota.bytes);
      setOk(true);
      reload();
    } catch (e) {
      // Gate serveur (prod / flag) => message dédié ; sinon message générique.
      const code = isApiError(e) ? (e.body as { detail?: string } | null)?.detail : null;
      if (code === "editing_disabled_in_prod" || code === "editing_flag_disabled" || (isApiError(e) && (e.status === 403 || e.status === 409))) {
        setErr(t("defaultsEditingDisabled"));
      } else {
        setErr(t("defaultsSaveError"));
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <div style={{ padding: 18, display: "flex", flexDirection: "column", gap: 14 }}>
        <div>
          <h2 style={{ fontSize: 16, fontWeight: 700, color: "var(--ink)", margin: "0 0 4px" }}>
            {t("workspaceDefaultsTitle")}
          </h2>
          <p style={{ fontSize: 12.5, color: "var(--ink-3)", margin: 0 }}>
            {t("workspaceDefaultsSubtitle")}
          </p>
        </div>

        {err && (
          <div role="alert" style={{ padding: "10px 12px", borderRadius: 9, background: "#fef2f2", color: "#b91c1c", fontSize: 13 }}>
            {err}
          </div>
        )}
        {ok && !err && (
          <div role="status" style={{ padding: "10px 12px", borderRadius: 9, background: "#f0fdf4", color: "#15803d", fontSize: 13 }}>
            {t("defaultsSaved")}
          </div>
        )}

        {defaults ? (
          <>
            <QuotaLimitsFields
              initialCount={defaults.default_max_document_count}
              initialBytes={defaults.default_max_document_bytes}
              hardMaxCount={defaults.hard_max_document_count}
              hardMaxBytes={defaults.hard_max_document_bytes}
              onChange={setQuota}
            />
            <div style={{ fontSize: 12, color: "var(--ink-3)" }}>
              {t("hardCapsLine", {
                files: formatCount(defaults.hard_max_document_count),
                size: fileSize(defaults.hard_max_document_bytes),
              })}
            </div>
            <div role="note" style={{ padding: "9px 12px", borderRadius: 9, background: "#fffbeb", color: "#92400e", fontSize: 12.5, border: "1px solid #fde68a" }}>
              {t("defaultsNoRetroNotice")}
            </div>
            <div>
              <Button size="sm" disabled={busy || !quota || !quota.valid} onClick={save}>
                {busy ? t("saving") : t("saveDefaults")}
              </Button>
            </div>
          </>
        ) : (
          <div style={{ fontSize: 13, color: "var(--ink-3)" }}>{t("loading")}</div>
        )}
      </div>
    </Card>
  );
}
