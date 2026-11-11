"use client";

import React, { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Button, Field, Icon, Modal, Pill } from "@/components/ui";
import {
  listPlatformUsers,
  createWorkspace,
  getWorkspaceQuotaDefaults,
  setWorkspaceQuota,
  assignWorkspaceOwner,
  addMembership,
  changeMembershipOwnerScope,
  disableWorkspace,
  enableWorkspace,
  previewOwnerScopeTransfer,
  disableUser,
  enableUser,
  changeMembershipRole,
  removeMembership,
  humanPlatformError,
  type PlatformUserRow,
  type PlatformUserDetail,
  type PlatformWorkspaceDetail,
  type TransferPreview,
  type WorkspaceQuotaDefaults,
} from "@/lib/platformAdmin";
import { QuotaLimitsFields, QuotaUsageMeter, type QuotaFieldsValue } from "./QuotaFields";
import { fileSize, formatCount } from "@/lib/format";

const WS_ROLES = [
  { key: "owner", labelKey: "roleOwner" },
  { key: "admin", labelKey: "roleAdmin" },
  { key: "member", labelKey: "roleMember" },
  { key: "viewer", labelKey: "roleViewer" },
];

/** Sélecteur d'utilisateur par recherche (source : annuaire plateforme). */
function UserPicker({ onPick, picked }: { onPick: (u: PlatformUserRow | null) => void; picked: PlatformUserRow | null }) {
  const t = useTranslations("platformAdmin");
  const [q, setQ] = useState("");
  const [rows, setRows] = useState<PlatformUserRow[]>([]);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!q || picked) {
      setRows([]);
      return;
    }
    let alive = true;
    const t = setTimeout(() => {
      listPlatformUsers({ search: q, is_active: true, limit: 8 })
        .then((p) => alive && setRows(p.items))
        .catch(() => alive && setRows([]));
    }, 250);
    return () => {
      alive = false;
      clearTimeout(t);
    };
  }, [q, picked]);

  if (picked) {
    return (
      <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "9px 12px", borderRadius: 10, border: "1px solid var(--border)" }}>
        <span style={{ flex: 1, fontSize: 13.5, color: "var(--ink)" }}>{picked.display_name} <span style={{ color: "var(--ink-3)" }}>· {picked.email}</span></span>
        <button onClick={() => { onPick(null); setQ(""); }} aria-label={t("change")} style={{ border: "none", background: "transparent", cursor: "pointer", color: "var(--ink-3)", fontSize: 16 }}>×</button>
      </div>
    );
  }
  return (
    <div style={{ position: "relative" }}>
      <Field icon="search" placeholder={t("searchUserPlaceholder")} value={q} onChange={(v: string) => { setQ(v); setOpen(true); }} />
      {open && rows.length > 0 && (
        <div style={{ marginTop: 6, border: "1px solid var(--border)", borderRadius: 10, overflow: "hidden", background: "var(--surface)" }}>
          {rows.map((u) => (
            <button key={u.id} onClick={() => { onPick(u); setOpen(false); }}
              style={{ width: "100%", textAlign: "left", padding: "9px 12px", border: "none", background: "transparent", cursor: "pointer", fontFamily: "inherit", display: "block", borderBottom: "1px solid var(--border)" }}>
              <span style={{ fontSize: 13, color: "var(--ink)" }}>{u.display_name}</span>
              <span style={{ fontSize: 11.5, color: "var(--ink-3)" }}> · {u.email}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function Err({ msg }: { msg: string | null }) {
  if (!msg) return null;
  return (
    <div role="alert" style={{ display: "flex", gap: 9, alignItems: "center", padding: "9px 13px", borderRadius: 10, background: "var(--danger-soft)", border: "1px solid var(--danger-border)", color: "#8A2E20", fontSize: 13 }}>
      <Icon name="alert" size={16} stroke={2} /><span>{msg}</span>
    </div>
  );
}

function Footer({ busy, onClose, onConfirm, confirmLabel, confirmBusyLabel, danger, disabled }: {
  busy: boolean; onClose: () => void; onConfirm: () => void; confirmLabel: string; confirmBusyLabel: string; danger?: boolean; disabled?: boolean;
}) {
  const t = useTranslations("platformAdmin");
  return (
    <div style={{ display: "flex", justifyContent: "flex-end", gap: 10, marginTop: 22 }}>
      <Button variant="secondary" disabled={busy} onClick={onClose}>{t("cancel")}</Button>
      <Button disabled={busy || disabled} onClick={onConfirm}
        style={danger ? { background: "var(--danger)", color: "#fff" } : undefined}>
        {busy ? confirmBusyLabel : confirmLabel}
      </Button>
    </div>
  );
}

export function CreateWorkspaceModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const t = useTranslations("platformAdmin");
  const [name, setName] = useState("");
  const [owner, setOwner] = useState<PlatformUserRow | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // Défauts de plateforme chargés depuis le backend (jamais de constantes
  // dupliquées côté frontend). Prérremplit la section « limites de stockage ».
  const [defaults, setDefaults] = useState<WorkspaceQuotaDefaults | null>(null);
  const [quota, setQuota] = useState<QuotaFieldsValue | null>(null);

  useEffect(() => {
    let alive = true;
    getWorkspaceQuotaDefaults()
      .then((d) => alive && setDefaults(d))
      .catch(() => alive && setDefaults(null));
    return () => {
      alive = false;
    };
  }, []);

  // Section quota présente mais invalide => bloque la création (jamais de valeur
  // incohérente envoyée). Si les défauts n'ont pas pu charger, on crée sans quota
  // (le backend appliquera ses défauts).
  const quotaBlocks = defaults != null && quota != null && !quota.valid;

  async function submit() {
    if (!name.trim() || !owner || quotaBlocks) return;
    setBusy(true); setErr(null);
    try {
      const quotaArg =
        defaults != null && quota != null && quota.valid
          ? { max_document_count: quota.count, max_document_bytes: quota.bytes }
          : undefined;
      await createWorkspace(name.trim(), owner.id, quotaArg);
      onDone();
    } catch (e) {
      setErr(humanPlatformError(e, t("createWorkspaceError")));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal onClose={onClose} title={t("createWorkspace")} subtitle={t("createWorkspaceSubtitle")}>
      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <Err msg={err} />
        <Field label={t("workspaceNameLabel")} placeholder={t("workspaceNamePlaceholder")} value={name} onChange={setName} autoFocus />
        <div>
          <div style={{ fontSize: 13, fontWeight: 600, color: "var(--ink)", marginBottom: 7 }}>{t("firstOwner")}</div>
          <UserPicker onPick={setOwner} picked={owner} />
        </div>
        {defaults && (
          <div style={{ borderTop: "1px solid var(--border)", paddingTop: 14, display: "flex", flexDirection: "column", gap: 12 }}>
            <div style={{ fontSize: 13.5, fontWeight: 600, color: "var(--ink)" }}>{t("storageLimitsSection")}</div>
            <QuotaLimitsFields
              initialCount={defaults.default_max_document_count}
              initialBytes={defaults.default_max_document_bytes}
              hardMaxCount={defaults.hard_max_document_count}
              hardMaxBytes={defaults.hard_max_document_bytes}
              onChange={setQuota}
            />
            <div style={{ fontSize: 12, color: "var(--ink-3)" }}>
              {t("platformDefaultsHint", {
                files: formatCount(defaults.default_max_document_count),
                size: fileSize(defaults.default_max_document_bytes),
              })}
            </div>
          </div>
        )}
      </div>
      <Footer busy={busy} disabled={!name.trim() || !owner || quotaBlocks} onClose={onClose} onConfirm={submit} confirmLabel={t("createWorkspaceConfirm")} confirmBusyLabel={t("creating")} />
    </Modal>
  );
}

export function WorkspaceQuotaModal({ workspace, onClose, onDone }: {
  workspace: PlatformWorkspaceDetail; onClose: () => void; onDone: () => void;
}) {
  const t = useTranslations("platformAdmin");
  const usage = workspace.quota;
  const [defaults, setDefaults] = useState<WorkspaceQuotaDefaults | null>(null);
  const [quota, setQuota] = useState<QuotaFieldsValue | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [confirmReduction, setConfirmReduction] = useState(false);

  useEffect(() => {
    let alive = true;
    getWorkspaceQuotaDefaults()
      .then((d) => alive && setDefaults(d))
      .catch(() => alive && setDefaults(null));
    return () => { alive = false; };
  }, []);

  // Réduction SOUS l'usage courant : nécessite une confirmation explicite avant
  // application (aucun document n'est supprimé, mais les uploads seront bloqués).
  const reducesBelowUsage =
    quota != null && quota.valid &&
    (quota.count < usage.used_document_count || quota.bytes < usage.used_document_bytes);

  async function apply() {
    if (!quota || !quota.valid) return;
    setBusy(true); setErr(null);
    try {
      await setWorkspaceQuota(workspace.id, {
        max_document_count: quota.count,
        max_document_bytes: quota.bytes,
        ...(reason.trim() ? { reason: reason.trim() } : {}),
      });
      onDone();
    } catch (e) {
      setErr(humanPlatformError(e, t("quotaSaveError")));
    } finally {
      setBusy(false);
    }
  }

  function onConfirm() {
    if (reducesBelowUsage && !confirmReduction) { setConfirmReduction(true); return; }
    apply();
  }

  const confirmLabel =
    reducesBelowUsage && !confirmReduction ? t("reviewReduction")
      : reducesBelowUsage ? t("saveAnyway")
        : t("saveChanges");

  return (
    <Modal onClose={onClose} title={t("editQuotaTitle")} subtitle={t("editQuotaSubtitle")}>
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        <Err msg={err} />
        <QuotaUsageMeter usage={usage} />
        {defaults ? (
          <div style={{ borderTop: "1px solid var(--border)", paddingTop: 14, display: "flex", flexDirection: "column", gap: 12 }}>
            <QuotaLimitsFields
              initialCount={usage.max_document_count}
              initialBytes={usage.max_document_bytes}
              hardMaxCount={defaults.hard_max_document_count}
              hardMaxBytes={defaults.hard_max_document_bytes}
              onChange={(v) => { setQuota(v); setConfirmReduction(false); }}
            />
            <Field label={t("reasonOptional")} placeholder={t("quotaReasonPlaceholder")} value={reason} onChange={setReason} />
            {reducesBelowUsage && (
              <div role="alert" style={{ padding: "11px 13px", borderRadius: 10, background: "var(--warning-soft, #FBF3E0)", border: "1px solid var(--border)", fontSize: 12.5, color: "#8A6D3B" }}>
                {t.rich("reductionBelowUsageWarning", { strong: (c) => <strong>{c}</strong> })}
              </div>
            )}
          </div>
        ) : (
          <div style={{ fontSize: 13, color: "var(--ink-3)" }}>{t("loading")}</div>
        )}
      </div>
      <Footer busy={busy} disabled={!quota || !quota.valid} onClose={onClose} onConfirm={onConfirm}
        confirmLabel={confirmLabel} confirmBusyLabel={t("saving")} danger={reducesBelowUsage && confirmReduction} />
    </Modal>
  );
}

export function AssignOwnerModal({ workspace, onClose, onDone }: { workspace: PlatformWorkspaceDetail; onClose: () => void; onDone: () => void }) {
  const t = useTranslations("platformAdmin");
  const [owner, setOwner] = useState<PlatformUserRow | null>(null);
  const [allowAdditional, setAllowAdditional] = useState(false);
  const [promote, setPromote] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const hasOwner = workspace.owners.length > 0;

  async function submit() {
    if (!owner) return;
    setBusy(true); setErr(null);
    try {
      await assignWorkspaceOwner(workspace.id, owner.id, { allow_additional: allowAdditional, promote });
      onDone();
    } catch (e) {
      setErr(humanPlatformError(e, t("assignOwnerError")));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal onClose={onClose} title={t("assignOwner")} subtitle={t("assignOwnerSubtitle")}>
      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <Err msg={err} />
        <UserPicker onPick={setOwner} picked={owner} />
        {hasOwner && (
          <label style={{ display: "flex", gap: 9, fontSize: 12.5, color: "var(--ink-2)" }}>
            <input type="checkbox" checked={allowAdditional} onChange={(e) => setAllowAdditional(e.target.checked)} style={{ accentColor: "var(--accent)" }} />
            {t("allowAdditionalOwner")}
          </label>
        )}
        <label style={{ display: "flex", gap: 9, fontSize: 12.5, color: "var(--ink-2)" }}>
          <input type="checkbox" checked={promote} onChange={(e) => setPromote(e.target.checked)} style={{ accentColor: "var(--accent)" }} />
          {t("promoteToOwner")}
        </label>
      </div>
      <Footer busy={busy} disabled={!owner} onClose={onClose} onConfirm={submit} confirmLabel={t("assignOwnerConfirm")} confirmBusyLabel={t("assigning")} />
    </Modal>
  );
}

export function AddMemberModal({ workspace, onClose, onDone }: { workspace: PlatformWorkspaceDetail; onClose: () => void; onDone: () => void }) {
  const t = useTranslations("platformAdmin");
  const [user, setUser] = useState<PlatformUserRow | null>(null);
  const [role, setRole] = useState("member");
  const [ownerMid, setOwnerMid] = useState<number | "">(workspace.owners[0]?.membership_id ?? "");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const needScope = role !== "owner";

  async function submit() {
    if (!user) return;
    if (needScope && !ownerMid) { setErr(t("chooseMemberScope")); return; }
    setBusy(true); setErr(null);
    try {
      await addMembership(workspace.id, {
        user_id: user.id, role,
        ...(needScope ? { owner_membership_id: Number(ownerMid) } : {}),
      });
      onDone();
    } catch (e) {
      setErr(humanPlatformError(e, t("addMemberError")));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal onClose={onClose} title={t("addMember")} subtitle={t("addMemberSubtitle")}>
      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <Err msg={err} />
        <UserPicker onPick={setUser} picked={user} />
        <div>
          <div style={{ fontSize: 13, fontWeight: 600, color: "var(--ink)", marginBottom: 7 }}>{t("role")}</div>
          <select value={role} onChange={(e) => setRole(e.target.value)} style={selectStyle}>
            {WS_ROLES.map((r) => <option key={r.key} value={r.key}>{t(r.labelKey)}</option>)}
          </select>
        </div>
        {needScope && (
          <div>
            <div style={{ fontSize: 13, fontWeight: 600, color: "var(--ink)", marginBottom: 7 }}>{t("ownerScope")}</div>
            {workspace.owners.length === 0 ? (
              <div style={{ fontSize: 12.5, color: "#8A6D3B" }}>{t("noOwnerAssignFirst")}</div>
            ) : (
              <select value={ownerMid === "" ? "" : String(ownerMid)} onChange={(e) => setOwnerMid(e.target.value ? Number(e.target.value) : "")} style={selectStyle}>
                {workspace.owners.map((o) => <option key={o.membership_id} value={o.membership_id}>{o.user.display_name}</option>)}
              </select>
            )}
          </div>
        )}
      </div>
      <Footer busy={busy} disabled={!user || (needScope && workspace.owners.length === 0)} onClose={onClose} onConfirm={submit} confirmLabel={t("addMemberConfirm")} confirmBusyLabel={t("adding")} />
    </Modal>
  );
}

export function OwnerScopeModal({ workspace, membershipId, memberName, onClose, onDone }: {
  workspace: PlatformWorkspaceDetail; membershipId: number; memberName: string; onClose: () => void; onDone: () => void;
}) {
  const t = useTranslations("platformAdmin");
  const [ownerMid, setOwnerMid] = useState<number | "">(workspace.owners[0]?.membership_id ?? "");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [incompatible, setIncompatible] = useState<{ rag_space_name: string; space_role: string }[] | null>(null);

  async function submit(revokeIncompatible: boolean) {
    if (!ownerMid) return;
    setBusy(true); setErr(null);
    try {
      const res = await changeMembershipOwnerScope(workspace.id, membershipId, {
        owner_membership_id: Number(ownerMid), revoke_incompatible: revokeIncompatible,
      });
      if (res.code === "membership_owner_scope_changed") { onDone(); return; }
      setErr(humanPlatformError({}, t("operationFailed")));
    } catch (e: any) {
      // 409 grants_incompatible : proposer la révocation explicite.
      const body = e?.body;
      if (body?.code === "grants_incompatible" && Array.isArray(body.incompatible_grants)) {
        setIncompatible(body.incompatible_grants);
      } else {
        setErr(humanPlatformError(e, t("attachError")));
      }
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal onClose={onClose} title={t("attachToScope")} subtitle={t("attachToScopeSubtitle", { name: memberName })}>
      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <Err msg={err} />
        <div>
          <div style={{ fontSize: 13, fontWeight: 600, color: "var(--ink)", marginBottom: 7 }}>{t("newOwnerScope")}</div>
          <select value={ownerMid === "" ? "" : String(ownerMid)} onChange={(e) => setOwnerMid(e.target.value ? Number(e.target.value) : "")} style={selectStyle}>
            {workspace.owners.map((o) => <option key={o.membership_id} value={o.membership_id}>{o.user.display_name}</option>)}
          </select>
        </div>
        {incompatible && (
          <div style={{ padding: "11px 13px", borderRadius: 10, background: "var(--warning-soft, #FBF3E0)", border: "1px solid var(--border)" }}>
            <div style={{ fontSize: 12.5, color: "#8A6D3B", marginBottom: 6 }}>
              {t.rich("accessesWillBeRevoked", { strong: (chunks) => <strong>{chunks}</strong> })}
            </div>
            <div style={{ display: "flex", gap: 5, flexWrap: "wrap" }}>
              {incompatible.map((g, i) => <Pill key={i}>{g.rag_space_name} · {g.space_role}</Pill>)}
            </div>
          </div>
        )}
      </div>
      {incompatible ? (
        <Footer busy={busy} onClose={onClose} onConfirm={() => submit(true)} confirmLabel={t("revokeAndAttach")} confirmBusyLabel={t("attaching")} danger />
      ) : (
        <Footer busy={busy} disabled={!ownerMid} onClose={onClose} onConfirm={() => submit(false)} confirmLabel={t("attach")} confirmBusyLabel={t("attaching")} />
      )}
    </Modal>
  );
}

export function WorkspaceActiveModal({ workspace, enable, onClose, onDone }: {
  workspace: PlatformWorkspaceDetail; enable: boolean; onClose: () => void; onDone: () => void;
}) {
  const t = useTranslations("platformAdmin");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function submit() {
    if (!enable && !reason.trim()) { setErr(t("reasonRequiredDisable")); return; }
    setBusy(true); setErr(null);
    try {
      if (enable) await enableWorkspace(workspace.id, reason.trim());
      else await disableWorkspace(workspace.id, reason.trim());
      onDone();
    } catch (e) {
      setErr(humanPlatformError(e, t("operationError")));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal onClose={onClose} title={enable ? t("reactivateWorkspace") : t("deactivateWorkspace")}
      subtitle={enable ? t("reactivateWorkspaceSubtitle") : t("deactivateWorkspaceSubtitle")}>
      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <Err msg={err} />
        <Field label={enable ? t("reasonOptional") : t("reasonRequired")} placeholder={t("reasonWorkspacePlaceholder")} value={reason} onChange={setReason} autoFocus />
      </div>
      <Footer busy={busy} disabled={!enable && !reason.trim()} onClose={onClose} onConfirm={submit}
        confirmLabel={enable ? t("reactivate") : t("deactivate")} confirmBusyLabel={t("inProgress")} danger={!enable} />
    </Modal>
  );
}

export function TransferPreviewModal({ workspace, onClose }: { workspace: PlatformWorkspaceDetail; onClose: () => void }) {
  const t = useTranslations("platformAdmin");
  const owners = workspace.owners;
  const [src, setSrc] = useState<number | "">(owners[0]?.membership_id ?? "");
  const [tgt, setTgt] = useState<number | "">(owners[1]?.membership_id ?? "");
  const [preview, setPreview] = useState<TransferPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function run() {
    if (!src || !tgt || src === tgt) { setErr(t("chooseSourceTarget")); return; }
    setBusy(true); setErr(null); setPreview(null);
    try {
      const p = await previewOwnerScopeTransfer({
        workspace_id: workspace.id, source_owner_membership_id: Number(src), target_owner_membership_id: Number(tgt),
      });
      setPreview(p);
    } catch (e) {
      setErr(humanPlatformError(e, t("previewError")));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal onClose={onClose} title={t("prepareTransferTitle")} subtitle={t("transferPreviewSubtitle")}>
      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <Err msg={err} />
        {owners.length < 2 ? (
          <div style={{ fontSize: 13, color: "#8A6D3B" }}>{t("transferNeedsTwoOwners")}</div>
        ) : (
          <>
            <div style={{ display: "flex", gap: 10 }}>
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: 12.5, fontWeight: 600, color: "var(--ink)", marginBottom: 6 }}>{t("source")}</div>
                <select value={src === "" ? "" : String(src)} onChange={(e) => setSrc(e.target.value ? Number(e.target.value) : "")} style={selectStyle}>
                  {owners.map((o) => <option key={o.membership_id} value={o.membership_id}>{o.user.display_name}</option>)}
                </select>
              </div>
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: 12.5, fontWeight: 600, color: "var(--ink)", marginBottom: 6 }}>{t("target")}</div>
                <select value={tgt === "" ? "" : String(tgt)} onChange={(e) => setTgt(e.target.value ? Number(e.target.value) : "")} style={selectStyle}>
                  {owners.map((o) => <option key={o.membership_id} value={o.membership_id}>{o.user.display_name}</option>)}
                </select>
              </div>
            </div>
            <Button size="sm" variant="secondary" icon="refresh" disabled={busy} onClick={run}>{busy ? t("computing") : t("previewImpact")}</Button>

            {preview && (
              <div style={{ display: "flex", flexDirection: "column", gap: 8, padding: "12px 14px", borderRadius: 10, border: "1px solid var(--border)", background: "var(--chip)" }}>
                <Row label={t("rowMembersMoved")} value={preview.affected_memberships.length} />
                <Row label={t("rowSpacesMoved")} value={preview.affected_ragspaces.length} />
                <Row label={t("rowAccessKept")} value={preview.ragspace_accesses_valid.length} />
                <Row label={t("rowAccessInvalidated")} value={preview.ragspace_accesses_invalid.length} tone={preview.ragspace_accesses_invalid.length ? "warn" : undefined} />
                <Row label={t("rowDocumentsInProgress")} value={preview.documents_in_progress} tone={preview.documents_in_progress ? "warn" : undefined} />
                <Row label={t("rowActiveReindexJobs")} value={preview.active_reindex_jobs} tone={preview.active_reindex_jobs ? "warn" : undefined} />
                <div style={{ marginTop: 6, fontSize: 12.5 }}>
                  {preview.allowed
                    ? <span style={{ color: "#2E7D32" }}>{t("transferApplicable")}</span>
                    : <span style={{ color: "#8A2E20" }}>{t("transferBlocked", { blockers: preview.blockers.join(", ") })}</span>}
                </div>
              </div>
            )}
            <div style={{ fontSize: 12, color: "var(--ink-3)", lineHeight: 1.5 }}>
              {t.rich("transferNote", { strong: (chunks) => <strong>{chunks}</strong> })}
            </div>
          </>
        )}
      </div>
      <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 20 }}>
        <Button variant="secondary" onClick={onClose}>{t("close")}</Button>
      </div>
    </Modal>
  );
}

function Row({ label, value, tone }: { label: string; value: number; tone?: "warn" }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12.5 }}>
      <span style={{ color: "var(--ink-2)" }}>{label}</span>
      <strong style={{ color: tone === "warn" ? "#8A6D3B" : "var(--ink)" }}>{value}</strong>
    </div>
  );
}

// ============================================================================
// P1 — utilisateurs & memberships
// ============================================================================

export function UserActiveModal({ user, isSelf, onClose, onDone }: {
  user: PlatformUserRow | PlatformUserDetail; isSelf: boolean; onClose: () => void; onDone: () => void;
}) {
  const t = useTranslations("platformAdmin");
  const enable = !user.is_active;
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function submit() {
    if (!reason.trim()) { setErr(t("reasonRequiredSimple")); return; }
    setBusy(true); setErr(null);
    try {
      if (enable) await enableUser(user.id, reason.trim());
      else await disableUser(user.id, reason.trim(), isSelf);
      onDone();
    } catch (e) {
      setErr(humanPlatformError(e, t("operationError")));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal onClose={onClose} title={enable ? t("reactivateAccount") : t("deactivateAccount")}
      subtitle={enable ? t("reactivateAccountSubtitle") : t("deactivateAccountSubtitle")}>
      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <Err msg={err} />
        {!enable && isSelf && (
          <div style={{ fontSize: 12.5, color: "#8A2E20", padding: "9px 12px", borderRadius: 9, background: "var(--danger-soft)", border: "1px solid var(--danger-border)" }}>
            {t.rich("disableOwnAccountWarning", { strong: (chunks) => <strong>{chunks}</strong> })}
          </div>
        )}
        <Field label={t("reasonRequired")} placeholder={t("reasonAccountPlaceholder")} value={reason} onChange={setReason} autoFocus />
      </div>
      <Footer busy={busy} disabled={!reason.trim()} onClose={onClose} onConfirm={submit}
        confirmLabel={enable ? t("reactivate") : t("deactivate")} confirmBusyLabel={t("inProgress")} danger={!enable} />
    </Modal>
  );
}

export function MembershipRoleModal({ workspace, membershipId, currentRole, memberName, onClose, onDone }: {
  workspace: PlatformWorkspaceDetail; membershipId: number; currentRole: string; memberName: string;
  onClose: () => void; onDone: () => void;
}) {
  const t = useTranslations("platformAdmin");
  const [role, setRole] = useState(currentRole);
  const [ownerMid, setOwnerMid] = useState<number | "">(workspace.owners.find((o) => o.membership_id !== membershipId)?.membership_id ?? "");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [incompatible, setIncompatible] = useState<{ rag_space_name: string; space_role: string }[] | null>(null);
  const demotingOwner = currentRole === "owner" && role !== "owner";

  async function submit(revoke: boolean) {
    if (role === "owner" && currentRole !== "owner") { setErr(t("promotionViaAssign")); return; }
    if (demotingOwner && !ownerMid) { setErr(t("chooseAttachScope")); return; }
    setBusy(true); setErr(null);
    try {
      const res = await changeMembershipRole(workspace.id, membershipId, {
        role,
        ...(demotingOwner && ownerMid ? { owner_membership_id: Number(ownerMid) } : {}),
        revoke_incompatible: revoke,
      });
      if (res.code === "role_changed") { onDone(); return; }
      setErr(humanPlatformError({}, t("operationFailed")));
    } catch (e: any) {
      const body = e?.body;
      if (body?.code === "grants_exceed_role" && Array.isArray(body.incompatible_grants)) {
        setIncompatible(body.incompatible_grants);
      } else {
        setErr(humanPlatformError(e, t("roleChangeError")));
      }
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal onClose={onClose} title={t("editRoleTitle", { name: memberName })}
      subtitle={t("editRoleSubtitle")}>
      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <Err msg={err} />
        <div>
          <div style={{ fontSize: 13, fontWeight: 600, color: "var(--ink)", marginBottom: 7 }}>{t("newRole")}</div>
          <select value={role} onChange={(e) => setRole(e.target.value)} style={selectStyle}>
            {WS_ROLES.map((r) => <option key={r.key} value={r.key} disabled={r.key === "owner" && currentRole !== "owner"}>{t(r.labelKey)}</option>)}
          </select>
        </div>
        {demotingOwner && (
          <div>
            <div style={{ fontSize: 13, fontWeight: 600, color: "var(--ink)", marginBottom: 7 }}>{t("attachToOwnerScope")}</div>
            <select value={ownerMid === "" ? "" : String(ownerMid)} onChange={(e) => setOwnerMid(e.target.value ? Number(e.target.value) : "")} style={selectStyle}>
              <option value="">{t("choose")}</option>
              {workspace.owners.filter((o) => o.membership_id !== membershipId).map((o) => <option key={o.membership_id} value={o.membership_id}>{o.user.display_name}</option>)}
            </select>
          </div>
        )}
        {incompatible && (
          <div style={{ padding: "11px 13px", borderRadius: 10, background: "var(--warning-soft, #FBF3E0)", border: "1px solid var(--border)" }}>
            <div style={{ fontSize: 12.5, color: "#8A6D3B", marginBottom: 6 }}>{t.rich("accessesExceedRole", { strong: (chunks) => <strong>{chunks}</strong> })}</div>
            <div style={{ display: "flex", gap: 5, flexWrap: "wrap" }}>{incompatible.map((g, i) => <Pill key={i}>{g.rag_space_name} · {g.space_role}</Pill>)}</div>
          </div>
        )}
      </div>
      {incompatible ? (
        <Footer busy={busy} onClose={onClose} onConfirm={() => submit(true)} confirmLabel={t("revokeAndChange")} confirmBusyLabel={t("ellipsis")} danger />
      ) : (
        <Footer busy={busy} disabled={role === currentRole} onClose={onClose} onConfirm={() => submit(false)} confirmLabel={t("editRoleConfirm")} confirmBusyLabel={t("ellipsis")} />
      )}
    </Modal>
  );
}

export function MembershipRemoveModal({ workspace, membershipId, memberName, onClose, onDone }: {
  workspace: PlatformWorkspaceDetail; membershipId: number; memberName: string; onClose: () => void; onDone: () => void;
}) {
  const t = useTranslations("platformAdmin");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [grants, setGrants] = useState<{ rag_space_name: string; space_role: string }[] | null>(null);

  async function submit(revoke: boolean) {
    setBusy(true); setErr(null);
    try {
      const res = await removeMembership(workspace.id, membershipId, revoke);
      if (res.code === "membership_removed") { onDone(); return; }
      setErr(humanPlatformError({}, t("operationFailed")));
    } catch (e: any) {
      const body = e?.body;
      if (body?.code === "active_grants" && Array.isArray(body.active_grants)) setGrants(body.active_grants);
      else setErr(humanPlatformError(e, t("removeError")));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal onClose={onClose} title={t("removeFromWorkspaceTitle", { name: memberName })}
      subtitle={t("removeFromWorkspaceSubtitle")}>
      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <Err msg={err} />
        {grants && (
          <div style={{ padding: "11px 13px", borderRadius: 10, background: "var(--warning-soft, #FBF3E0)", border: "1px solid var(--border)" }}>
            <div style={{ fontSize: 12.5, color: "#8A6D3B", marginBottom: 6 }}>{t.rich("memberHasActiveGrants", { strong: (chunks) => <strong>{chunks}</strong> })}</div>
            <div style={{ display: "flex", gap: 5, flexWrap: "wrap" }}>{grants.map((g, i) => <Pill key={i}>{g.rag_space_name} · {g.space_role}</Pill>)}</div>
          </div>
        )}
      </div>
      {grants ? (
        <Footer busy={busy} onClose={onClose} onConfirm={() => submit(true)} confirmLabel={t("revokeAndRemove")} confirmBusyLabel={t("ellipsis")} danger />
      ) : (
        <Footer busy={busy} onClose={onClose} onConfirm={() => submit(false)} confirmLabel={t("removeFromWorkspaceConfirm")} confirmBusyLabel={t("ellipsis")} danger />
      )}
    </Modal>
  );
}

const selectStyle: React.CSSProperties = {
  width: "100%", padding: "9px 11px", borderRadius: 9, border: "1px solid var(--border)",
  background: "var(--surface)", fontFamily: "inherit", fontSize: 13, color: "var(--ink)",
};
