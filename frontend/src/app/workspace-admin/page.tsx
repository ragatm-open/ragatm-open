"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { apiFetch } from "@/lib/api";
import { relativeTime, shortDate } from "@/lib/format";
import { AppShell, Topbar, useWorkspace } from "@/components/shell";
import {
  Avatar,
  Button,
  Card,
  EmptyState,
  Field,
  Icon,
  Modal,
  Pill,
  Popover,
  SectionLabel,
  initialsFrom,
} from "@/components/ui";
import type { Member, RagSpace, RagQueryLogItem, Invitation, InvitationCreated } from "./_lib/types";
import { WS_ROLES, INVITE_ROLES, roleInfo, type WsRoleKey } from "./_lib/roles";
// Un `page.tsx` de l'App Router n'accepte que l'export par défaut : `SpacesTab`
// vit donc dans `_components/` (hors routage), d'où il reste testable isolément.
import { SpacesTab } from "./_components/SpacesTab";

export default function WorkspaceAdminPage() {
  return (
    <AppShell>
      <AdminContent />
    </AppShell>
  );
}

type Tab = "members" | "spaces" | "audit";

function AdminContent() {
  const t = useTranslations("workspaceAdmin");
  const { workspaceId, isAdmin, loading: ctxLoading } = useWorkspace();
  const [tab, setTab] = useState<Tab>("members");
  const [members, setMembers] = useState<Member[]>([]);
  const [ragSpaces, setRagSpaces] = useState<RagSpace[]>([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (workspaceId === null) return;
    setLoading(true);
    setErr(null);
    try {
      const [mem, rs] = await Promise.all([
        apiFetch<Member[]>(`/api/v1/workspaces/${workspaceId}/members/`),
        apiFetch<RagSpace[]>(`/api/v1/rag-spaces/?workspace_id=${workspaceId}`),
      ]);
      setMembers(mem);
      setRagSpaces(rs);
    } catch (e: any) {
      setErr(e?.message || t("loadError"));
    } finally {
      setLoading(false);
    }
  }, [workspaceId, t]);

  useEffect(() => {
    if (isAdmin) refresh();
  }, [isAdmin, refresh]);

  if (ctxLoading) {
    return (
      <>
        <Topbar title={t("title")} />
        <div style={{ padding: 32, color: "var(--ink-3)" }}>{t("loading")}</div>
      </>
    );
  }

  if (!isAdmin) {
    return (
      <>
        <Topbar title={t("title")} />
        <div style={{ padding: "24px 32px", maxWidth: 1000, margin: "0 auto" }}>
          <Card style={{ padding: 8 }}>
            <EmptyState
              icon="lock"
              title={t("adminOnlyTitle")}
              body={t("adminOnlyBody")}
            />
          </Card>
        </div>
      </>
    );
  }

  return (
    <>
      <Topbar title={t("title")} subtitle={t("subtitle")} />
      <div style={{ padding: "24px 32px 56px", maxWidth: 1000, margin: "0 auto" }}>
        {/* Onglets */}
        <div
          role="tablist"
          style={{
            display: "flex",
            gap: 4,
            padding: 4,
            borderRadius: 10,
            background: "var(--chip)",
            border: "1px solid var(--border)",
            width: "fit-content",
            marginBottom: 22,
          }}
        >
          {(
            [
              ["members", t("members"), "users"],
              ["spaces", t("spaces"), "grid"],
              ["audit", t("audit"), "clock"],
            ] as [Tab, string, string][]
          ).map(([k, l, ic]) => (
            <button
              key={k}
              role="tab"
              aria-selected={tab === k}
              onClick={() => setTab(k)}
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 7,
                padding: "7px 14px",
                borderRadius: 7,
                border: "none",
                cursor: "pointer",
                fontFamily: "inherit",
                fontSize: 13.5,
                fontWeight: 600,
                background: tab === k ? "var(--surface)" : "transparent",
                color: tab === k ? "var(--ink)" : "var(--ink-3)",
                boxShadow: tab === k ? "0 1px 2px rgba(60,40,25,.1)" : "none",
              }}
            >
              <Icon name={ic} size={16} />
              {l}
            </button>
          ))}
        </div>

        {err && (
          <div
            style={{
              display: "flex",
              gap: 9,
              alignItems: "center",
              padding: "10px 14px",
              marginBottom: 16,
              borderRadius: 10,
              background: "var(--danger-soft)",
              border: "1px solid var(--danger-border)",
              color: "#8A2E20",
              fontSize: 13,
            }}
          >
            <Icon name="alert" size={16} stroke={2} />
            {err}
          </div>
        )}

        {tab === "members" && (
          <MembersTab workspaceId={workspaceId!} members={members} loading={loading} onChanged={refresh} setErr={setErr} />
        )}
        {tab === "spaces" && (
          <SpacesTab workspaceId={workspaceId!} ragSpaces={ragSpaces} loading={loading} onChanged={refresh} setErr={setErr} />
        )}
        {tab === "audit" && <AuditTab workspaceId={workspaceId!} ragSpaces={ragSpaces} />}
      </div>
    </>
  );
}

/* ===================== Membres ===================== */
function MembersTab({
  workspaceId,
  members,
  loading,
  onChanged,
  setErr,
}: {
  workspaceId: number;
  members: Member[];
  loading: boolean;
  onChanged: () => Promise<void>;
  setErr: (s: string | null) => void;
}) {
  const t = useTranslations("workspaceAdmin");
  const tw = useTranslations("workspaces");
  const { me } = useWorkspace();
  const [invite, setInvite] = useState(false);
  // Lien one-shot renvoyé après création / régénération (affiché une seule fois).
  const [created, setCreated] = useState<InvitationCreated | null>(null);
  const [invitations, setInvitations] = useState<Invitation[]>([]);

  const loadInvitations = useCallback(async () => {
    try {
      const data = await apiFetch<Invitation[]>(`/api/v1/workspaces/${workspaceId}/invitations/`);
      setInvitations(data);
    } catch (e: any) {
      setErr(e?.message || t("invitationsLoadError"));
    }
  }, [workspaceId, setErr, t]);

  useEffect(() => {
    loadInvitations();
  }, [loadInvitations]);

  async function createInvitation(email: string, role: WsRoleKey) {
    try {
      const res = await apiFetch<InvitationCreated>(`/api/v1/workspaces/${workspaceId}/invitations/`, {
        method: "POST",
        body: JSON.stringify({ email, role }),
      });
      setInvite(false);
      setCreated(res); // affiche la modale one-shot
      await loadInvitations();
    } catch (e: any) {
      setErr(e?.message || t("invitationCreateError"));
    }
  }
  async function regenerateInvitation(id: string) {
    try {
      const res = await apiFetch<InvitationCreated>(
        `/api/v1/workspaces/${workspaceId}/invitations/${id}/regenerate/`,
        { method: "POST", body: JSON.stringify({}) }
      );
      setCreated(res);
      await loadInvitations();
    } catch (e: any) {
      setErr(e?.message || t("regenerateError"));
    }
  }
  async function revokeInvitation(id: string) {
    try {
      await apiFetch(`/api/v1/workspaces/${workspaceId}/invitations/${id}/revoke/`, {
        method: "POST",
        body: JSON.stringify({}),
      });
      await loadInvitations();
    } catch (e: any) {
      setErr(e?.message || t("revokeError"));
    }
  }
  async function updateRole(mid: number, role: WsRoleKey) {
    try {
      await apiFetch(`/api/v1/workspaces/${workspaceId}/members/${mid}/`, {
        method: "PATCH",
        body: JSON.stringify({ role }),
      });
      await onChanged();
    } catch (e: any) {
      setErr(e?.message || t("roleUpdateError"));
    }
  }
  async function removeMember(mid: number) {
    try {
      await apiFetch(`/api/v1/workspaces/${workspaceId}/members/${mid}/`, { method: "DELETE" });
      await onChanged();
    } catch (e: any) {
      setErr(e?.message || t("memberDeleteError"));
    }
  }

  return (
    <div>
      {/* Légende des rôles */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(210px, 1fr))", gap: 12, marginBottom: 22 }}>
        {WS_ROLES.map((r) => (
          <div key={r.key} style={{ padding: "13px 15px", borderRadius: 12, background: "var(--surface)", border: "1px solid var(--border)" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 5 }}>
              <span style={{ width: 9, height: 9, borderRadius: "50%", background: r.color }} />
              <span style={{ fontSize: 13.5, fontWeight: 700, color: "var(--ink)" }}>{tw(r.labelKey)}</span>
            </div>
            <div style={{ fontSize: 12.5, color: "var(--ink-2)", lineHeight: 1.45 }}>{tw(r.descKey)}</div>
          </div>
        ))}
      </div>

      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 14 }}>
        <SectionLabel>
          {tw("membersCount", { count: members.length })}
        </SectionLabel>
        <Button size="sm" icon="plus" onClick={() => setInvite(true)}>
          {t("inviteMember")}
        </Button>
      </div>

      {loading && members.length === 0 ? (
        <Card style={{ padding: 32, color: "var(--ink-3)", fontSize: 14 }}>{t("loading")}</Card>
      ) : members.length === 0 ? (
        <Card style={{ padding: 8 }}>
          <EmptyState icon="users" title={t("noMembersTitle")} body={t("noMembersBody")} />
        </Card>
      ) : (
        <Card style={{ overflow: "hidden", padding: 0 }}>
          {members.map((m, i) => (
            <div
              key={m.id}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 14,
                padding: "13px 18px",
                borderBottom: i === members.length - 1 ? "none" : "1px solid var(--border)",
              }}
            >
              <Avatar initials={initialsFrom(m.email)} size={38} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
                  <span
                    style={{
                      fontSize: 14,
                      fontWeight: 600,
                      color: "var(--ink)",
                      whiteSpace: "nowrap",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                    }}
                  >
                    {m.email}
                  </span>
                  {me?.email && m.email === me.email && (
                    <Pill color="var(--accent-ink)" bg="var(--accent-soft)" style={{ flex: "none" }}>
                      {t("you")}
                    </Pill>
                  )}
                </div>
                <div style={{ fontSize: 12.5, color: "var(--ink-3)" }}>
                  {t("memberSince", { date: shortDate(m.created_at) })}
                </div>
              </div>
              <RoleDropdown value={m.role} onChange={(r) => updateRole(m.id, r)} />
              <button
                onClick={() => removeMember(m.id)}
                aria-label={t("removeMemberAria", { email: m.email })}
                title={t("removeMemberTitle")}
                style={{
                  width: 30,
                  height: 30,
                  borderRadius: 8,
                  border: "none",
                  background: "transparent",
                  cursor: "pointer",
                  display: "grid",
                  placeItems: "center",
                  color: "var(--ink-3)",
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.background = "var(--danger-soft)";
                  e.currentTarget.style.color = "var(--danger)";
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.background = "transparent";
                  e.currentTarget.style.color = "var(--ink-3)";
                }}
              >
                <Icon name="trash" size={16} />
              </button>
            </div>
          ))}
        </Card>
      )}

      <InvitationsPanel
        invitations={invitations}
        onRegenerate={regenerateInvitation}
        onRevoke={revokeInvitation}
      />

      {invite && <InviteModal onClose={() => setInvite(false)} onInvite={createInvitation} />}
      {created && <InvitationLinkModal data={created} onClose={() => setCreated(null)} />}
    </div>
  );
}

/* ===================== Invitations en attente ===================== */
const INV_STATUS: Record<string, { labelKey: string; color: string; bg: string }> = {
  pending: { labelKey: "statusPending", color: "#0E7C86", bg: "var(--accent-soft)" },
  expired: { labelKey: "statusExpired", color: "#8A6D3B", bg: "var(--warning-soft, #FBF3E0)" },
  accepted: { labelKey: "statusAccepted", color: "#2E7D32", bg: "var(--success-soft, #E7F4E8)" },
  revoked: { labelKey: "statusRevoked", color: "#8A2E20", bg: "var(--danger-soft)" },
};

function InvitationsPanel({
  invitations,
  onRegenerate,
  onRevoke,
}: {
  invitations: Invitation[];
  onRegenerate: (id: string) => void;
  onRevoke: (id: string) => void;
}) {
  const t = useTranslations("workspaceAdmin");
  const tw = useTranslations("workspaces");
  // Les invitations acceptées deviennent des membres : on ne montre ici que ce
  // qui n'est pas encore un membre actif (pending / expired / revoked).
  const rows = invitations.filter((i) => i.status !== "accepted");
  if (rows.length === 0) return null;

  return (
    <div style={{ marginTop: 28 }}>
      <SectionLabel>
        {t("pendingInvitationsCount", { count: rows.length })}
      </SectionLabel>
      <Card style={{ overflow: "hidden", padding: 0, marginTop: 12 }}>
        {rows.map((inv, i) => {
          const st = INV_STATUS[inv.status] || INV_STATUS.pending;
          const r = roleInfo(inv.role);
          return (
            <div
              key={inv.id}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 14,
                padding: "13px 18px",
                borderBottom: i === rows.length - 1 ? "none" : "1px solid var(--border)",
              }}
            >
              <div style={{ width: 38, height: 38, borderRadius: 10, display: "grid", placeItems: "center", background: "var(--chip)", color: "var(--ink-3)", flex: "none" }}>
                <Icon name="mail" size={18} />
              </div>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
                  <span style={{ fontSize: 14, fontWeight: 600, color: "var(--ink)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {inv.email}
                  </span>
                  <Pill color={st.color} bg={st.bg} style={{ flex: "none" }}>
                    {t(st.labelKey)}
                  </Pill>
                </div>
                <div style={{ fontSize: 12.5, color: "var(--ink-3)" }}>
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
                    <span style={{ width: 8, height: 8, borderRadius: "50%", background: r.color }} />
                    {tw(r.labelKey)}
                  </span>
                  {" · "}
                  {t("expiresOn", { date: shortDate(inv.expires_at) })}
                  {inv.generation > 1 ? ` · ${t("generation", { generation: inv.generation })}` : ""}
                </div>
              </div>
              {inv.can_regenerate && (
                <Button size="sm" variant="secondary" icon="refresh" onClick={() => onRegenerate(inv.id)}>
                  {t("regenerate")}
                </Button>
              )}
              {inv.can_revoke && (
                <button
                  onClick={() => onRevoke(inv.id)}
                  aria-label={t("revokeInvitationAria", { email: inv.email })}
                  title={t("revokeInvitationTitle")}
                  style={{ width: 30, height: 30, borderRadius: 8, border: "none", background: "transparent", cursor: "pointer", display: "grid", placeItems: "center", color: "var(--ink-3)" }}
                  onMouseEnter={(e) => {
                    e.currentTarget.style.background = "var(--danger-soft)";
                    e.currentTarget.style.color = "var(--danger)";
                  }}
                  onMouseLeave={(e) => {
                    e.currentTarget.style.background = "transparent";
                    e.currentTarget.style.color = "var(--ink-3)";
                  }}
                >
                  <Icon name="trash" size={16} />
                </button>
              )}
            </div>
          );
        })}
      </Card>
    </div>
  );
}

/* ===================== Lien one-shot (création / régénération) ============= */
function InvitationLinkModal({ data, onClose }: { data: InvitationCreated; onClose: () => void }) {
  const t = useTranslations("workspaceAdmin");
  const [copied, setCopied] = useState(false);
  async function copy() {
    try {
      await navigator.clipboard.writeText(data.invitation_url);
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } catch {
      setCopied(false);
    }
  }
  return (
    <Modal
      onClose={onClose}
      title={t("invitationCreatedTitle")}
      subtitle={t("invitationCreatedSubtitle")}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <div
          style={{
            display: "flex",
            gap: 8,
            alignItems: "center",
            padding: "10px 12px",
            borderRadius: 10,
            border: "1px solid var(--border)",
            background: "var(--chip)",
          }}
        >
          <input
            readOnly
            value={data.invitation_url}
            onFocus={(e) => e.currentTarget.select()}
            aria-label={t("invitationLinkAria")}
            style={{ flex: 1, minWidth: 0, border: "none", background: "transparent", fontFamily: "ui-monospace, monospace", fontSize: 12.5, color: "var(--ink)", outline: "none" }}
          />
          <Button size="sm" icon={copied ? "check" : "copy"} onClick={copy}>
            {copied ? t("linkCopied") : t("copyLink")}
          </Button>
        </div>
        <div style={{ fontSize: 12.5, color: "var(--ink-2)", lineHeight: 1.5 }}>
          {data.email_sent ? t("emailSent") : t("emailNotSent")}
          {t("linkExpiryNote", { date: shortDate(data.expires_at) })}
        </div>
      </div>
      <div style={{ display: "flex", justifyContent: "flex-end", gap: 10, marginTop: 22 }}>
        <Button onClick={onClose}>{t("iCopiedLink")}</Button>
      </div>
    </Modal>
  );
}

function RoleDropdown({ value, onChange }: { value: string; onChange: (r: WsRoleKey) => void }) {
  const tw = useTranslations("workspaces");
  const [open, setOpen] = useState(false);
  const btnRef = useRef<HTMLButtonElement>(null);
  const r = roleInfo(value);
  return (
    <div style={{ position: "relative" }}>
      <button
        ref={btnRef}
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="listbox"
        aria-expanded={open}
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 7,
          padding: "6px 11px",
          borderRadius: 8,
          border: "1px solid var(--border)",
          background: "var(--surface)",
          cursor: "pointer",
          fontFamily: "inherit",
          fontSize: 12.5,
          fontWeight: 600,
          color: "var(--ink)",
          minWidth: 140,
        }}
      >
        <span style={{ width: 8, height: 8, borderRadius: "50%", background: r.color, flex: "none" }} />
        <span style={{ flex: 1, textAlign: "left" }}>{tw(r.labelKey)}</span>
        <Icon name="chevronD" size={14} style={{ color: "var(--ink-3)" }} />
      </button>
      <Popover
        open={open}
        onClose={() => setOpen(false)}
        anchorRef={btnRef}
        align="end"
        minWidth={240}
        padding={5}
        radius={11}
      >
        {INVITE_ROLES.map((ri) => (
          <button
            key={ri.key}
            role="option"
            aria-selected={ri.key === value}
            onClick={() => {
              onChange(ri.key);
              setOpen(false);
            }}
            style={{
              width: "100%",
              display: "flex",
              gap: 9,
              padding: "8px 10px",
              borderRadius: 8,
              border: "none",
              background: ri.key === value ? "var(--accent-soft)" : "transparent",
              cursor: "pointer",
              fontFamily: "inherit",
              textAlign: "left",
            }}
            onMouseEnter={(e) => {
              if (ri.key !== value) e.currentTarget.style.background = "var(--hover)";
            }}
            onMouseLeave={(e) => {
              if (ri.key !== value) e.currentTarget.style.background = "transparent";
            }}
          >
            <span style={{ width: 8, height: 8, borderRadius: "50%", background: ri.color, flex: "none", marginTop: 5 }} />
            <span>
              <span style={{ display: "block", fontSize: 13, fontWeight: 600, color: "var(--ink)" }}>{tw(ri.labelKey)}</span>
              <span style={{ display: "block", fontSize: 11.5, color: "var(--ink-3)", lineHeight: 1.4 }}>{tw(ri.descKey)}</span>
            </span>
          </button>
        ))}
      </Popover>
    </div>
  );
}

function InviteModal({ onClose, onInvite }: { onClose: () => void; onInvite: (email: string, role: WsRoleKey) => void }) {
  const t = useTranslations("workspaceAdmin");
  const tw = useTranslations("workspaces");
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<WsRoleKey>("viewer");
  return (
    <Modal
      onClose={onClose}
      title={t("inviteMember")}
      subtitle={t("inviteModalSubtitle")}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        <Field label={t("emailLabel")} icon="mail" placeholder={t("emailPlaceholder")} value={email} onChange={setEmail} autoFocus />
        <div>
          <div style={{ fontSize: 13, fontWeight: 600, color: "var(--ink)", marginBottom: 8 }}>{t("role")}</div>
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {INVITE_ROLES.map((ri) => (
              <label
                key={ri.key}
                style={{
                  display: "flex",
                  gap: 11,
                  padding: "11px 13px",
                  borderRadius: 11,
                  cursor: "pointer",
                  border: `1.5px solid ${role === ri.key ? "var(--accent)" : "var(--border)"}`,
                  background: role === ri.key ? "var(--accent-soft)" : "var(--surface)",
                }}
              >
                <input
                  type="radio"
                  name="invite-role"
                  checked={role === ri.key}
                  onChange={() => setRole(ri.key)}
                  style={{ accentColor: "var(--accent)", marginTop: 2 }}
                />
                <span>
                  <span style={{ display: "block", fontSize: 13.5, fontWeight: 600, color: "var(--ink)" }}>{tw(ri.labelKey)}</span>
                  <span style={{ display: "block", fontSize: 12.5, color: "var(--ink-2)", lineHeight: 1.4 }}>{tw(ri.descKey)}</span>
                </span>
              </label>
            ))}
          </div>
        </div>
      </div>
      <div style={{ display: "flex", justifyContent: "flex-end", gap: 10, marginTop: 24 }}>
        <Button variant="secondary" onClick={onClose}>
          {t("cancel")}
        </Button>
        <Button icon="send" disabled={!email.includes("@")} onClick={() => onInvite(email.trim(), role)}>
          {t("sendInvitation")}
        </Button>
      </div>
    </Modal>
  );
}

/* ===================== Journal (audit) ===================== */
function AuditTab({ workspaceId, ragSpaces }: { workspaceId: number; ragSpaces: RagSpace[] }) {
  const t = useTranslations("workspaceAdmin");
  const [rows, setRows] = useState<RagQueryLogItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [ragSpaceId, setRagSpaceId] = useState("");
  const [err, setErr] = useState<string | null>(null);

  const fetchAudit = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const qs = new URLSearchParams({
        workspace_id: String(workspaceId),
        limit: "50",
        ...(ragSpaceId ? { rag_space_id: ragSpaceId } : {}),
      }).toString();
      const data = await apiFetch<RagQueryLogItem[]>(`/api/v1/audit/rag-queries/?${qs}`);
      setRows(data);
    } catch (e: any) {
      setErr(e?.message || t("auditLoadError"));
      setRows([]);
    } finally {
      setLoading(false);
    }
  }, [workspaceId, ragSpaceId, t]);

  useEffect(() => {
    fetchAudit();
  }, [fetchAudit]);

  const statusColor = (s: string) => {
    const x = (s || "").toLowerCase();
    if (x.includes("ok") || x.includes("answer")) return "var(--success)";
    if (x.includes("fallback") || x.includes("no_answer")) return "var(--warning)";
    if (x.includes("error") || x.includes("fail")) return "var(--danger)";
    return "var(--ink-3)";
  };

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 14, flexWrap: "wrap" }}>
        <SectionLabel>{t("auditQueriesLabel")}</SectionLabel>
        <div style={{ flex: 1 }} />
        <select
          value={ragSpaceId}
          onChange={(e) => setRagSpaceId(e.target.value)}
          style={{ padding: "8px 10px", borderRadius: 9, border: "1px solid var(--border)", background: "var(--surface)", fontFamily: "inherit", fontSize: 13 }}
        >
          <option value="">{t("allSpaces")}</option>
          {ragSpaces.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}
            </option>
          ))}
        </select>
        <Button size="sm" variant="secondary" icon="refresh" onClick={fetchAudit}>
          {t("refresh")}
        </Button>
      </div>

      {err && <div style={{ color: "var(--danger)", fontSize: 13, marginBottom: 12 }}>{err}</div>}

      {loading && rows.length === 0 ? (
        <Card style={{ padding: 32, color: "var(--ink-3)", fontSize: 14 }}>{t("loading")}</Card>
      ) : rows.length === 0 ? (
        <Card style={{ padding: 8 }}>
          <EmptyState icon="search" title={t("noQueriesTitle")} body={t("noQueriesBody")} />
        </Card>
      ) : (
        <Card style={{ overflow: "hidden", padding: 0 }}>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 110px 90px 120px", gap: 12, padding: "11px 18px", borderBottom: "1px solid var(--border)", background: "var(--sidebar)" }}>
            {[t("colQuestion"), t("colStatus"), t("colLatency"), t("colWhen")].map((h, i) => (
              <SectionLabel key={i}>{h}</SectionLabel>
            ))}
          </div>
          {rows.map((r, i) => (
            <div
              key={r.id}
              style={{
                display: "grid",
                gridTemplateColumns: "1fr 110px 90px 120px",
                gap: 12,
                alignItems: "center",
                padding: "11px 18px",
                borderBottom: i === rows.length - 1 ? "none" : "1px solid var(--border)",
              }}
            >
              <div style={{ minWidth: 0 }}>
                <div style={{ fontSize: 13, color: "var(--ink)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {r.question || <span style={{ color: "var(--ink-3)" }}>{t("noText")}</span>}
                </div>
                <div style={{ fontSize: 11.5, color: "var(--ink-3)" }}>
                  {t("sourcesCount", { count: r.sources_count })}
                </div>
              </div>
              <span style={{ fontSize: 12.5, fontWeight: 600, color: statusColor(r.status) }}>{r.status}</span>
              <span style={{ fontSize: 12.5, color: "var(--ink-2)" }}>{r.latency_ms != null ? `${r.latency_ms} ms` : "—"}</span>
              <span style={{ fontSize: 12.5, color: "var(--ink-3)" }}>{relativeTime(r.created_at)}</span>
            </div>
          ))}
        </Card>
      )}
    </div>
  );
}
