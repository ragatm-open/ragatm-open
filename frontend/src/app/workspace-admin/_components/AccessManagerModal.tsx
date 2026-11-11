"use client";

import React, { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import {
  listRagSpaceGrants,
  listEligibleUsers,
  grantRagSpaceAccess,
  changeRagSpaceAccessRole,
  revokeRagSpaceAccess,
  type RagSpaceGrant,
  type EligibleUser,
} from "@/lib/ragspaceAccessApi";
import { humanAccessError } from "@/lib/apiError";
import type { SpaceRole } from "@/lib/ragspacePermissions";
import { Avatar, Button, Icon, Modal, SectionLabel, initialsFrom } from "@/components/ui";
import { SPACE_ROLES, spaceRoleInfo, grantCodeMessage } from "../_lib/spaceRoles";

/**
 * Administration des accès (RagSpaceAccess) d'un espace — niveau « manage ».
 *
 * Le backend est l'autorité : ce composant ne fait qu'appeler les endpoints et
 * afficher leur résultat. La révocation renseigne `revoked_at` (jamais de
 * suppression physique). L'owner du périmètre a un accès complet IMPLICITE : il
 * n'apparaît pas ici et n'est pas modifiable (rappelé par un bandeau).
 */
export function AccessManagerModal({
  ragSpaceId,
  ragSpaceName,
  onClose,
}: {
  ragSpaceId: string;
  ragSpaceName: string;
  onClose: () => void;
}) {
  const t = useTranslations("workspaceAdmin");
  const tw = useTranslations("workspaces");
  const [grants, setGrants] = useState<RagSpaceGrant[]>([]);
  const [eligibles, setEligibles] = useState<EligibleUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmRevoke, setConfirmRevoke] = useState<RagSpaceGrant | null>(null);

  // Formulaire d'ajout
  const [addUserId, setAddUserId] = useState<number | "">("");
  const [addRole, setAddRole] = useState<SpaceRole>("viewer");

  const reload = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const [g, e] = await Promise.all([
        listRagSpaceGrants(ragSpaceId),
        listEligibleUsers(ragSpaceId),
      ]);
      setGrants(g.filter((x) => !x.revoked_at));
      setEligibles(e);
    } catch (ex) {
      setErr(humanAccessError(ex) || t("accessLoadError"));
    } finally {
      setLoading(false);
    }
  }, [ragSpaceId, t]);

  useEffect(() => {
    reload();
  }, [reload]);

  // Rôles autorisés (plafond D1) pour l'utilisateur choisi dans le formulaire.
  const selectedEligible = eligibles.find((e) => e.user.id === addUserId) || null;
  const allowedForSelected = selectedEligible?.allowed_space_roles ?? [];

  // Réaligne le rôle par défaut si l'utilisateur change et que le rôle courant
  // n'est plus permis (restrictif : on ne propose jamais un rôle interdit).
  useEffect(() => {
    if (selectedEligible && !allowedForSelected.includes(addRole)) {
      setAddRole(allowedForSelected[allowedForSelected.length - 1] || "viewer");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [addUserId]);

  function applyResult(code: string) {
    const m = grantCodeMessage(code);
    if (m.ok) {
      setNotice(t(m.key));
      setErr(null);
    } else {
      setErr(t(m.key));
      setNotice(null);
    }
    return m.ok;
  }

  async function onGrant() {
    if (busy || addUserId === "") return;
    setBusy(true);
    try {
      const res = await grantRagSpaceAccess(ragSpaceId, addUserId, addRole);
      // Ne recharge QUE si l'opération a abouti : reload() réinitialise `err`
      // et effacerait le message d'un refus (owner_implicit, conflict, invalid…).
      if (applyResult(res.code)) {
        setAddUserId("");
        await reload();
      }
    } catch (ex) {
      setErr(humanAccessError(ex) || t("grantErrorGeneric"));
    } finally {
      setBusy(false);
    }
  }

  async function onChangeRole(grant: RagSpaceGrant, role: SpaceRole) {
    if (busy || role === grant.space_role) return;
    setBusy(true);
    try {
      const res = await changeRagSpaceAccessRole(ragSpaceId, grant.id, role);
      if (applyResult(res.code)) await reload();
    } catch (ex) {
      setErr(humanAccessError(ex) || t("roleChangeErrorGeneric"));
    } finally {
      setBusy(false);
    }
  }

  async function onRevoke(grant: RagSpaceGrant) {
    if (busy) return;
    setBusy(true);
    try {
      const res = await revokeRagSpaceAccess(ragSpaceId, grant.id);
      if (applyResult(res.code)) {
        setConfirmRevoke(null);
        await reload();
      }
    } catch (ex) {
      setErr(humanAccessError(ex) || t("revokeErrorGeneric"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      onClose={onClose}
      title={t("accessModalTitle", { name: ragSpaceName })}
      subtitle={t("accessModalSubtitle")}
    >
      {/* Bandeau owner implicite (non modifiable) */}
      <div
        style={{
          display: "flex",
          gap: 10,
          padding: "10px 13px",
          borderRadius: 10,
          background: "var(--accent-soft)",
          border: "1px solid var(--border)",
          marginBottom: 16,
        }}
      >
        <Icon name="shield" size={17} style={{ color: "var(--accent-ink)", flex: "none", marginTop: 1 }} />
        <div style={{ fontSize: 12.5, color: "var(--ink-2)", lineHeight: 1.5 }}>
          {t.rich("ownerImplicitBanner", { b: (chunks) => <strong>{chunks}</strong> })}
        </div>
      </div>

      {err && (
        <div
          role="alert"
          style={{ display: "flex", gap: 9, alignItems: "center", padding: "9px 13px", marginBottom: 12, borderRadius: 10, background: "var(--danger-soft)", border: "1px solid var(--danger-border)", color: "#8A2E20", fontSize: 13 }}
        >
          <Icon name="alert" size={16} stroke={2} />
          <span style={{ flex: 1 }}>{err}</span>
        </div>
      )}
      {notice && (
        <div
          style={{ display: "flex", gap: 9, alignItems: "center", padding: "9px 13px", marginBottom: 12, borderRadius: 10, background: "var(--accent-soft)", border: "1px solid var(--border)", color: "var(--ink-2)", fontSize: 13 }}
        >
          <Icon name="check" size={16} />
          <span style={{ flex: 1 }}>{notice}</span>
        </div>
      )}

      {/* Formulaire d'ajout */}
      <div style={{ marginBottom: 18 }}>
        <SectionLabel>{t("grantAccessLabel")}</SectionLabel>
        <div style={{ display: "flex", gap: 8, marginTop: 10, flexWrap: "wrap", alignItems: "center" }}>
          <select
            aria-label={t("grantUserAria")}
            value={addUserId === "" ? "" : String(addUserId)}
            onChange={(e) => setAddUserId(e.target.value ? Number(e.target.value) : "")}
            disabled={busy || eligibles.length === 0}
            style={selectStyle}
          >
            <option value="">{eligibles.length === 0 ? t("noEligibleMembers") : t("chooseMember")}</option>
            {eligibles.map((el) => (
              <option key={el.user.id} value={el.user.id}>
                {el.user.email || el.user.username}
              </option>
            ))}
          </select>
          <select
            aria-label={t("spaceRoleAria")}
            value={addRole}
            onChange={(e) => setAddRole(e.target.value as SpaceRole)}
            disabled={busy || addUserId === ""}
            style={selectStyle}
          >
            {SPACE_ROLES.filter((r) => !selectedEligible || allowedForSelected.includes(r.key)).map((r) => (
              <option key={r.key} value={r.key}>
                {tw(r.labelKey)}
              </option>
            ))}
          </select>
          <Button size="sm" icon="plus" disabled={busy || addUserId === ""} onClick={onGrant}>
            {t("grant")}
          </Button>
        </div>
      </div>

      {/* Liste des accès actifs */}
      <SectionLabel>
        {loading ? t("loadingAccess") : t("activeAccessCount", { count: grants.length })}
      </SectionLabel>
      <div style={{ marginTop: 10, display: "flex", flexDirection: "column", gap: 8, maxHeight: 320, overflowY: "auto" }}>
        {loading ? (
          <div style={{ padding: 20, color: "var(--ink-3)", fontSize: 13 }}>{t("loading")}</div>
        ) : grants.length === 0 ? (
          <div style={{ padding: "16px 14px", color: "var(--ink-3)", fontSize: 13, textAlign: "center" }}>
            {t("noExplicitAccess")}
          </div>
        ) : (
          grants.map((g) => {
            const ri = spaceRoleInfo(g.space_role);
            const label = g.user?.email || g.user?.username || t("userFallback", { id: g.user_id });
            return (
              <div
                key={g.id}
                style={{ display: "flex", alignItems: "center", gap: 12, padding: "10px 12px", borderRadius: 10, border: "1px solid var(--border)", background: "var(--surface)" }}
              >
                <Avatar initials={initialsFrom(label)} size={34} />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 13.5, fontWeight: 600, color: "var(--ink)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {label}
                  </div>
                  <div style={{ display: "inline-flex", alignItems: "center", gap: 5, fontSize: 12, color: "var(--ink-3)" }}>
                    <span style={{ width: 8, height: 8, borderRadius: "50%", background: ri.color }} />
                    {tw(ri.labelKey)}
                  </div>
                </div>
                <select
                  aria-label={t("roleOfAria", { name: label })}
                  value={g.space_role}
                  onChange={(e) => onChangeRole(g, e.target.value as SpaceRole)}
                  disabled={busy}
                  style={selectStyle}
                >
                  {SPACE_ROLES.map((r) => (
                    <option key={r.key} value={r.key}>
                      {tw(r.labelKey)}
                    </option>
                  ))}
                </select>
                <button
                  onClick={() => setConfirmRevoke(g)}
                  disabled={busy}
                  aria-label={t("revokeAccessAria", { name: label })}
                  title={t("revokeAccess")}
                  style={{ width: 30, height: 30, borderRadius: 8, border: "none", background: "transparent", cursor: busy ? "default" : "pointer", display: "grid", placeItems: "center", color: "var(--ink-3)" }}
                >
                  <Icon name="trash" size={16} />
                </button>
              </div>
            );
          })
        )}
      </div>

      <div style={{ display: "flex", justifyContent: "flex-end", gap: 10, marginTop: 22 }}>
        <Button variant="secondary" onClick={onClose}>
          {t("close")}
        </Button>
      </div>

      {confirmRevoke && (
        <Modal
          onClose={() => (busy ? undefined : setConfirmRevoke(null))}
          title={t("revokeAccessConfirmTitle")}
          subtitle={t("revokeAccessConfirmSubtitle")}
        >
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 10, marginTop: 8 }}>
            <Button variant="secondary" disabled={busy} onClick={() => setConfirmRevoke(null)}>
              {t("cancel")}
            </Button>
            <Button style={{ background: "var(--danger)", color: "#fff" }} icon="trash" disabled={busy} onClick={() => onRevoke(confirmRevoke)}>
              {busy ? t("revoking") : t("revokeAccess")}
            </Button>
          </div>
        </Modal>
      )}
    </Modal>
  );
}

const selectStyle: React.CSSProperties = {
  padding: "7px 10px",
  borderRadius: 9,
  border: "1px solid var(--border)",
  background: "var(--surface)",
  fontFamily: "inherit",
  fontSize: 13,
  color: "var(--ink)",
};
