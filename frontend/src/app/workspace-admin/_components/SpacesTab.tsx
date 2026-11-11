"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { apiFetch } from "@/lib/api";
import { apiErrorStatus, isApiError } from "@/lib/apiError";
import { rateLimitWaitMs } from "@/lib/authProbe";
import {
  startReindex,
  getReindexJob,
  getActiveReindexJob,
  retryFailed,
  isTerminal,
  type ReindexJob,
} from "@/lib/reindex";
import { accentForId } from "@/lib/theme";
import { useWorkspace, type RagSpaceListItem } from "@/components/shell";
import { canManageRagSpace } from "@/lib/ragspacePermissions";
import { ReindexJobProgress } from "@/components/reindex/ReindexJobProgress";
import { Button, Card, EmptyState, Icon, Modal, SectionLabel } from "@/components/ui";
import type { RagSpace } from "../_lib/types";
import { SpaceEditModal, type SpaceSettingsPayload } from "./SpaceEditModal";
import { AccessManagerModal } from "./AccessManagerModal";

/* ===================== Espaces ===================== */
/**
 * Onglet « Espaces » de l'administration d'un workspace.
 *
 * Exporté nommément pour être testable isolément (cf.
 * tests/frontend/components/SpacesTabDelete.test.tsx) : la purge d'un espace est
 * irréversible, son UI doit être couverte.
 *
 * Il vit ici, et non dans `page.tsx`, parce qu'un fichier de page de l'App
 * Router n'accepte QUE l'export par défaut et la configuration de segment
 * (`metadata`, `revalidate`, `dynamic`…). Tout autre export nommé fait échouer
 * `next build` — pas `next dev`, pas `tsc`, pas les tests : uniquement le build
 * de production. Les dossiers préfixés `_` sont hors routage, donc libres.
 */
export function SpacesTab({
  workspaceId,
  ragSpaces,
  loading,
  onChanged,
  setErr,
}: {
  workspaceId: number;
  ragSpaces: RagSpace[];
  loading: boolean;
  onChanged: () => Promise<void>;
  setErr: (s: string | null) => void;
}) {
  const t = useTranslations("workspaceAdmin");
  // Source PARTAGÉE (sélecteur sidebar) : on la met à jour à la création pour que le
  // nouvel espace apparaisse et soit sélectionné immédiatement, sans reload.
  const { upsertRagSpace, refreshRagSpaces, setRagSpaceId, accessForRagSpace } = useWorkspace();
  const [confirmDel, setConfirmDel] = useState<RagSpace | null>(null);
  const [accessTarget, setAccessTarget] = useState<RagSpace | null>(null);
  // Id de l'espace en cours de purge : verrouille le bouton pendant la requête.
  const [deleting, setDeleting] = useState<string | null>(null);
  const [edit, setEdit] = useState<RagSpace | "new" | null>(null);
  const [reindexTarget, setReindexTarget] = useState<RagSpace | null>(null);
  const [startingId, setStartingId] = useState<string | null>(null);
  const [jobs, setJobs] = useState<Record<string, ReindexJob>>({});
  const [notice, setNotice] = useState<string | null>(null);

  const upsertJob = useCallback((job: ReindexJob) => {
    setJobs((prev) => ({ ...prev, [job.rag_space_id]: job }));
  }, []);

  // Statut de réindexation : ids d'espaces DÉJÀ vérifiés pendant la vie de cet onglet.
  // Pas un cache métier : il naît et meurt avec le composant (changement d'onglet ou
  // de page = rechargement légitime = tout est revérifié).
  const statusCheckedRef = useRef<Set<string>>(new Set());
  // Échéance (ms epoch) avant laquelle aucune vérification ne repart après un 429.
  const statusBlockedUntilRef = useRef(0);
  // Nombre d'espaces dont le statut n'a pas pu être vérifié à cause d'un 429.
  const [statusUnchecked, setStatusUnchecked] = useState(0);

  // Reprise après refresh : retrouver un éventuel job actif par espace.
  //
  // Incident staging du 08/10/2026 : 1 GET par espace (99), relancé pour TOUS les
  // espaces à chaque nouvelle liste, avec l'ancienne boucle toujours en cours (le
  // drapeau d'annulation ne bloquait que la mise à jour d'état) et les 429 ignorés —
  // 98 × 429 en 6 s, puis le seau DRF `user` épuisé faisait échouer /auth/me/.
  //
  // Règles :
  // - seuls les espaces jamais vérifiés sont interrogés : après une création, seul le
  //   nouvel espace l'est ;
  // - l'annulation (nouvelle liste, démontage) abandonne la requête en vol et arrête
  //   la boucle ; les espaces non vérifiés le seront par le cycle suivant ;
  // - le premier 429 arrête le cycle ; aucun cycle ne repart avant `Retry-After` ;
  // - séquentiel, comme avant : on supprime des requêtes, on n'en parallélise pas ;
  // - autres erreurs (401/403/404/5xx/réseau) : politique inchangée — ignorées,
  //   l'espace reste non vérifié et sera réinterrogé au cycle suivant.
  // Un job lancé ou repris depuis cet onglet est connu par la réponse du POST et suivi
  // par le polling ci-dessous : il ne dépend pas de cette boucle.
  useEffect(() => {
    const ctrl = new AbortController();
    const { signal } = ctrl;
    const checked = statusCheckedRef.current;
    const pending = ragSpaces.filter((s) => !checked.has(s.id));
    if (pending.length === 0) {
      setStatusUnchecked(0);
      return () => ctrl.abort();
    }
    if (Date.now() < statusBlockedUntilRef.current) {
      setStatusUnchecked(pending.length);
      return () => ctrl.abort();
    }
    (async () => {
      for (let i = 0; i < pending.length; i++) {
        if (signal.aborted) return;
        const s = pending[i];
        try {
          const { active, job } = await getActiveReindexJob(s.id, { signal });
          if (signal.aborted) return;
          checked.add(s.id);
          if (active && job) upsertJob(job);
        } catch (e) {
          if (signal.aborted) return;
          if (apiErrorStatus(e) === 429) {
            const retryAfterMs = isApiError(e) ? e.retryAfterMs : null;
            statusBlockedUntilRef.current = Date.now() + rateLimitWaitMs(retryAfterMs);
            setStatusUnchecked(pending.length - i);
            return;
          }
          /* espace sans droit / pas de job / erreur transitoire : ignore */
        }
      }
      if (!signal.aborted) setStatusUnchecked(0);
    })();
    return () => ctrl.abort();
  }, [ragSpaces, upsertJob]);

  // Polling des jobs non terminaux (le suivi ne dépend JAMAIS de la requête POST).
  useEffect(() => {
    const hasActive = Object.values(jobs).some((j) => !isTerminal(j.status));
    if (!hasActive) return;
    const t = setInterval(async () => {
      const active = Object.values(jobs).filter((j) => !isTerminal(j.status));
      for (const j of active) {
        try {
          upsertJob(await getReindexJob(j.job_id));
        } catch {
          /* erreur transitoire : on retentera au prochain tick */
        }
      }
    }, 3000);
    return () => clearInterval(t);
  }, [jobs, upsertJob]);

  async function reindexSpace(id: string) {
    setStartingId(id);
    setNotice(null);
    try {
      const r = await startReindex(id);
      upsertJob(r.job ?? (await getReindexJob(r.job_id)));
      setNotice(
        r.already_running
          ? t("reindexAlreadyRunning")
          : t("reindexStarted", { count: r.total_documents ?? "?" })
      );
    } catch (e: any) {
      const msg = e?.message || "";
      setErr(
        msg.includes("reindex_eager_too_large")
          ? t("reindexTooLarge")
          : msg.includes("reindex_enqueue_unavailable")
            ? t("reindexUnavailable")
            : msg || t("reindexFailedError")
      );
    } finally {
      setStartingId(null);
      setReindexTarget(null);
    }
  }

  async function retryFailedJob(jobId: string) {
    try {
      const r = await retryFailed(jobId);
      upsertJob(r.job ?? (await getReindexJob(r.job_id)));
    } catch (e: any) {
      setErr(e?.message || t("retryFailedError"));
    }
  }

  // NB: ces fonctions LÈVENT en cas d'erreur — la modale les gère localement
  // (messages par code : engine_unavailable, invalid_llm_provider/model,
  //  invalid_embedding_provider/model).
  async function createSpace(payload: SpaceSettingsPayload) {
    // La réponse de création renvoie l'espace complet (id + name + llm_* + embedding_*),
    // suffisant pour le type RagSpaceListItem du sélecteur. En cas d'échec, apiFetch
    // rejette AVANT toute mutation d'état : le modal affiche l'erreur et reste ouvert.
    const created = await apiFetch<RagSpaceListItem>(
      `/api/v1/workspaces/${workspaceId}/ragspaces/`,
      { method: "POST", body: JSON.stringify(payload) }
    );
    // 1) MAJ immédiate de la source partagée -> apparaît tout de suite dans la sidebar.
    upsertRagSpace(created);
    // 2) sélection auto + persistance (réutilise le mécanisme existant : localStorage/ws).
    setRagSpaceId(created.id);
    // 3) fermeture du modal.
    setEdit(null);
    // 4) revalidation SILENCIEUSE et non bloquante (liste admin enrichie + contexte).
    //    Un échec ici ne fait PAS disparaître l'espace déjà ajouté/sélectionné.
    void onChanged().catch(() => {});
    void refreshRagSpaces();
  }
  async function patchSpace(id: string, payload: SpaceSettingsPayload) {
    const updated = await apiFetch<RagSpaceListItem>(`/api/v1/rag-spaces/${id}/`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
    // upsert idempotent par id : REMPLACE l'espace (jamais de doublon), la sidebar
    // reflète immédiatement un renommage / changement de provider.
    upsertRagSpace({ ...updated, id });
    setEdit(null);
    void onChanged().catch(() => {});
    void refreshRagSpaces();
  }
  async function deleteSpace(id: string) {
    // Anti double-clic : la purge est irréversible, elle ne doit jamais partir deux fois.
    if (deleting) return;
    setDeleting(id);
    setErr(null);
    try {
      await apiFetch(`/api/v1/rag-spaces/${id}/`, { method: "DELETE" });
      // Succès (204) uniquement : on ne retire la ligne qu'après confirmation backend.
      await onChanged();
      setConfirmDel(null);
    } catch (e: any) {
      // Échec : l'espace RESTE dans la liste. Ne jamais laisser croire à un succès.
      setErr(e?.message || t("deleteNotConfirmed"));
    } finally {
      setDeleting(null);
    }
  }

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 14 }}>
        <SectionLabel>
          {t("spacesCount", { count: ragSpaces.length })}
        </SectionLabel>
        <Button size="sm" icon="plus" onClick={() => setEdit("new")}>
          {t("createSpace")}
        </Button>
      </div>

      {notice && (
        <div
          style={{
            display: "flex",
            gap: 9,
            alignItems: "center",
            padding: "10px 14px",
            marginBottom: 14,
            borderRadius: 10,
            background: "var(--accent-soft)",
            border: "1px solid var(--border)",
            color: "var(--ink-2)",
            fontSize: 13,
          }}
        >
          <Icon name="check" size={16} />
          <span style={{ flex: 1 }}>{notice}</span>
          <button
            onClick={() => setNotice(null)}
            aria-label={t("close")}
            style={{
              border: "none",
              background: "transparent",
              cursor: "pointer",
              color: "var(--ink-3)",
              fontSize: 16,
              lineHeight: 1,
            }}
          >
            ×
          </button>
        </div>
      )}

      {statusUnchecked > 0 && (
        <div
          role="status"
          data-testid="reindex-status-rate-limited"
          style={{
            display: "flex",
            gap: 9,
            alignItems: "center",
            padding: "10px 14px",
            marginBottom: 14,
            borderRadius: 10,
            background: "var(--accent-soft)",
            border: "1px solid var(--border)",
            color: "var(--ink-2)",
            fontSize: 13,
          }}
        >
          <Icon name="alert" size={16} />
          <span style={{ flex: 1 }}>{t("reindexStatusRateLimited", { count: statusUnchecked })}</span>
        </div>
      )}

      {loading && ragSpaces.length === 0 ? (
        <Card style={{ padding: 32, color: "var(--ink-3)", fontSize: 14 }}>{t("loading")}</Card>
      ) : ragSpaces.length === 0 ? (
        <Card style={{ padding: 8 }}>
          <EmptyState
            icon="grid"
            title={t("noSpacesTitle")}
            body={t("noSpacesBody")}
            action={
              <Button icon="plus" onClick={() => setEdit("new")}>
                {t("createSpace")}
              </Button>
            }
          />
        </Card>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {ragSpaces.map((s) => (
            <Card key={s.id} style={{ padding: 16, display: "flex", alignItems: "center", gap: 16 }}>
              <span style={{ width: 44, height: 44, borderRadius: 11, background: accentForId(s.id), display: "grid", placeItems: "center", flex: "none" }}>
                <Icon name="grid" size={21} style={{ color: "#fff" }} />
              </span>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 15, fontWeight: 600, color: "var(--ink)" }}>{s.name}</div>
                <div style={{ fontSize: 12.5, color: "var(--ink-3)", marginTop: 2 }}>
                  {t("chatLabel")} : {s.llm_provider || "—"}
                  {s.llm_model ? ` · ${s.llm_model}` : ""}
                  {"  ·  "}
                  {t("embeddingsLabel")} : {s.embedding_provider || "—"}
                  {s.embedding_model ? ` · ${s.embedding_model}` : ""}
                </div>
                {jobs[s.id] && <ReindexJobProgress job={jobs[s.id]} onRetry={() => retryFailedJob(jobs[s.id].job_id)} />}
              </div>
              <div style={{ display: "flex", gap: 7 }}>
                <Button
                  size="sm"
                  variant="ghost"
                  icon="refresh"
                  disabled={startingId === s.id || (!!jobs[s.id] && !isTerminal(jobs[s.id].status))}
                  onClick={() => setReindexTarget(s)}
                >
                  {jobs[s.id] && !isTerminal(jobs[s.id].status) ? t("inProgress") : t("reindex")}
                </Button>
                {/* Gestion des accès — visible seulement si l'appelant a « manage »
                    sur CET espace (décidé par le backend, jamais recalculé). */}
                {canManageRagSpace(accessForRagSpace(s.id)) && (
                  <Button size="sm" variant="ghost" icon="users" onClick={() => setAccessTarget(s)}>
                    {t("access")}
                  </Button>
                )}
                <Button size="sm" variant="ghost" icon="settings" onClick={() => setEdit(s)}>
                  {t("edit")}
                </Button>
                <Button size="sm" variant="danger" icon="trash" onClick={() => setConfirmDel(s)}>
                  {t("delete")}
                </Button>
              </div>
            </Card>
          ))}
        </div>
      )}

      {edit && (
        <SpaceEditModal
          space={edit === "new" ? null : edit}
          onClose={() => setEdit(null)}
          onCreate={createSpace}
          onPatch={patchSpace}
        />
      )}

      {accessTarget && (
        <AccessManagerModal
          ragSpaceId={accessTarget.id}
          ragSpaceName={accessTarget.name}
          onClose={() => setAccessTarget(null)}
        />
      )}

      {reindexTarget && (
        <Modal
          onClose={() => (startingId ? null : setReindexTarget(null))}
          title={t("reindexModalTitle", { name: reindexTarget.name })}
          subtitle={t("reindexModalSubtitle")}
        >
          <div style={{ display: "flex", gap: 11, padding: "13px 15px", borderRadius: 11, background: "var(--accent-soft)", border: "1px solid var(--border)" }}>
            <Icon name="alert" size={19} style={{ color: "var(--accent-ink)", flex: "none" }} />
            <div style={{ fontSize: 13.5, color: "var(--ink-2)", lineHeight: 1.5 }}>
              {t.rich("reindexModalBody", {
                b: (chunks) => <strong>{chunks}</strong>,
                provider: reindexTarget.embedding_provider || "—",
                model: reindexTarget.embedding_model ? ` · ${reindexTarget.embedding_model}` : "",
              })}
            </div>
          </div>
          <div style={{ fontSize: 12.5, color: "var(--ink-3)", marginTop: 12 }}>
            {t("reindexModalBackgroundNote")}
          </div>
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 10, marginTop: 22 }}>
            <Button variant="secondary" disabled={!!startingId} onClick={() => setReindexTarget(null)}>
              {t("cancel")}
            </Button>
            <Button icon="refresh" disabled={!!startingId} onClick={() => reindexSpace(reindexTarget.id)}>
              {startingId ? t("reindexStarting") : t("reindexStart")}
            </Button>
          </div>
        </Modal>
      )}

      {confirmDel && (
        <Modal onClose={() => (deleting ? undefined : setConfirmDel(null))} title={t("deleteModalTitle", { name: confirmDel.name })} subtitle={t("deleteModalSubtitle")}>
          <div style={{ display: "flex", gap: 11, padding: "13px 15px", borderRadius: 11, background: "var(--danger-soft)", border: "1px solid var(--danger-border)" }}>
            <Icon name="alert" size={19} style={{ color: "var(--danger)", flex: "none" }} />
            <div style={{ fontSize: 13.5, color: "#8A2E20", lineHeight: 1.5 }}>
              {t.rich("deleteModalBody", { b: (chunks) => <strong>{chunks}</strong> })}
            </div>
          </div>
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 10, marginTop: 22 }}>
            <Button variant="secondary" disabled={!!deleting} onClick={() => setConfirmDel(null)}>
              {t("cancel")}
            </Button>
            <Button
              style={{ background: "var(--danger)", color: "#fff" }}
              icon="trash"
              disabled={!!deleting}
              onClick={() => deleteSpace(confirmDel.id)}
            >
              {deleting === confirmDel.id ? t("deleting") : t("deleteConfirm")}
            </Button>
          </div>
        </Modal>
      )}
    </div>
  );
}
