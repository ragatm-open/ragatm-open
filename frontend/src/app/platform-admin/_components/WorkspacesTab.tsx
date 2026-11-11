"use client";

import React, { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Button, Card, EmptyState, Field, Icon, Modal, Pill, SectionLabel } from "@/components/ui";
import { humanAccessError } from "@/lib/apiError";
import {
  listPlatformWorkspaces,
  getPlatformWorkspace,
  type PlatformWorkspaceRow,
  type PlatformWorkspaceDetail,
} from "@/lib/platformAdmin";
import { anomalyLabelKey, ANOMALY_TONE } from "../_lib/anomalies";
import {
  CreateWorkspaceModal,
  WorkspaceQuotaModal,
  AssignOwnerModal,
  AddMemberModal,
  OwnerScopeModal,
  WorkspaceActiveModal,
  TransferPreviewModal,
  MembershipRoleModal,
  MembershipRemoveModal,
} from "./PlatformModals";
import { QuotaUsageMeter } from "./QuotaFields";

const PAGE = 25;
type Filter = "all" | "active" | "disabled" | "without_owner" | "anomalies";

export function WorkspacesTab() {
  const t = useTranslations("platformAdmin");
  const anomalyText = (code: string) => {
    const k = anomalyLabelKey(code);
    return k ? t(k) : code;
  };
  const [rows, setRows] = useState<PlatformWorkspaceRow[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [detailId, setDetailId] = useState<number | null>(null);
  const [creating, setCreating] = useState(false);

  const load = useCallback(async (off: number, q: string, f: Filter) => {
    setLoading(true);
    setErr(null);
    try {
      const p = await listPlatformWorkspaces({
        search: q || undefined,
        is_active: f === "active" ? true : f === "disabled" ? false : undefined,
        without_owner: f === "without_owner" ? true : undefined,
        with_anomalies: f === "anomalies" ? true : undefined,
        limit: PAGE,
        offset: off,
      });
      setRows(p.items);
      setTotal(p.total);
      setOffset(p.offset);
    } catch (e) {
      setErr(humanAccessError(e) || t("workspacesLoadError"));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    load(0, "", "all");
  }, [load]);

  return (
    <div>
      <div style={{ display: "flex", gap: 10, alignItems: "center", marginBottom: 14, flexWrap: "wrap" }}>
        <div style={{ flex: 1, minWidth: 200 }}>
          <Field icon="search" placeholder={t("searchWorkspacePlaceholder")} value={search} onChange={setSearch}
            onKeyDown={(e: React.KeyboardEvent) => e.key === "Enter" && load(0, search, filter)} />
        </div>
        <select value={filter} onChange={(e) => { const f = e.target.value as Filter; setFilter(f); load(0, search, f); }}
          style={{ padding: "9px 10px", borderRadius: 9, border: "1px solid var(--border)", background: "var(--surface)", fontFamily: "inherit", fontSize: 13 }}>
          <option value="all">{t("filterAll")}</option>
          <option value="active">{t("filterActive")}</option>
          <option value="disabled">{t("filterDisabled")}</option>
          <option value="without_owner">{t("filterWithoutOwner")}</option>
          <option value="anomalies">{t("filterAnomalies")}</option>
        </select>
        <Button size="sm" variant="secondary" icon="search" onClick={() => load(0, search, filter)}>{t("search")}</Button>
      </div>

      {err && <div style={{ color: "var(--danger)", fontSize: 13, marginBottom: 12 }}>{err}</div>}

      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 4 }}>
        <SectionLabel>{t("workspacesCount", { count: total })}</SectionLabel>
        <Button size="sm" icon="plus" onClick={() => setCreating(true)}>{t("createWorkspace")}</Button>
      </div>

      {loading && rows.length === 0 ? (
        <Card style={{ padding: 32, color: "var(--ink-3)", fontSize: 14, marginTop: 10 }}>{t("loading")}</Card>
      ) : rows.length === 0 ? (
        <Card style={{ padding: 8, marginTop: 10 }}>
          <EmptyState icon="grid" title={t("noWorkspacesTitle")} body={t("noResultsCriteria")} />
        </Card>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 10, marginTop: 10 }}>
          {rows.map((w) => (
            <Card key={w.id} style={{ padding: 14, display: "flex", alignItems: "center", gap: 14 }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                  <span style={{ fontSize: 14.5, fontWeight: 600, color: "var(--ink)" }}>{w.name}</span>
                  <span style={{ fontSize: 12, color: "var(--ink-3)" }}>ws#{w.id}</span>
                  {!w.is_active && <Pill color="#8A6D3B" bg="var(--warning-soft, #FBF3E0)">{t("disabledPill")}</Pill>}
                  {w.has_integrity_violation && <Pill color="#8A2E20" bg="var(--danger-soft)">{t("integrityPill")}</Pill>}
                  {w.owners.length === 0 && <Pill color="#8A6D3B" bg="var(--warning-soft, #FBF3E0)">{t("withoutOwnerPill")}</Pill>}
                </div>
                <div style={{ fontSize: 12.5, color: "var(--ink-3)", marginTop: 3 }}>
                  {t("ownersLabel")}{w.owners.length ? w.owners.map((o) => o.email || o.display_name).join(", ") : "—"}
                  {"  ·  "}{t("wsCountsSummary", { members: w.members_count, spaces: w.active_ragspaces_count, docs: w.documents_count })}
                </div>
                {w.anomaly_codes.length > 0 && (
                  <div style={{ display: "flex", gap: 5, flexWrap: "wrap", marginTop: 6 }}>
                    {w.anomaly_codes.map((code) => {
                      const tone = ANOMALY_TONE[code] || "info";
                      const bg = tone === "integrity" ? "var(--danger-soft)" : tone === "decision" ? "var(--warning-soft, #FBF3E0)" : "var(--chip)";
                      const color = tone === "integrity" ? "#8A2E20" : tone === "decision" ? "#8A6D3B" : "var(--ink-2)";
                      return <Pill key={code} color={color} bg={bg}>{anomalyText(code)}</Pill>;
                    })}
                  </div>
                )}
              </div>
              <Button size="sm" variant="ghost" icon="settings" onClick={() => setDetailId(w.id)}>{t("manage")}</Button>
            </Card>
          ))}
        </div>
      )}

      {total > PAGE && (
        <div style={{ display: "flex", justifyContent: "center", gap: 10, marginTop: 16, alignItems: "center" }}>
          <Button size="sm" variant="secondary" disabled={offset === 0 || loading} onClick={() => load(Math.max(0, offset - PAGE), search, filter)}>{t("previous")}</Button>
          <span style={{ fontSize: 12.5, color: "var(--ink-3)" }}>{offset + 1}–{Math.min(offset + PAGE, total)} / {total}</span>
          <Button size="sm" variant="secondary" disabled={offset + PAGE >= total || loading} onClick={() => load(offset + PAGE, search, filter)}>{t("next")}</Button>
        </div>
      )}

      {detailId != null && (
        <WorkspaceDetailModal
          workspaceId={detailId}
          onClose={() => setDetailId(null)}
          onChanged={() => load(offset, search, filter)}
        />
      )}
      {creating && (
        <CreateWorkspaceModal
          onClose={() => setCreating(false)}
          onDone={() => { setCreating(false); load(0, "", filter); }}
        />
      )}
    </div>
  );
}

type DetailModalKind =
  | { kind: "add_member" }
  | { kind: "assign_owner" }
  | { kind: "quota" }
  | { kind: "active"; enable: boolean }
  | { kind: "transfer" }
  | { kind: "owner_scope"; membershipId: number; memberName: string }
  | { kind: "role"; membershipId: number; currentRole: string; memberName: string }
  | { kind: "remove"; membershipId: number; memberName: string };

export function WorkspaceDetailModal({ workspaceId, onClose, onChanged }: {
  workspaceId: number; onClose: () => void; onChanged?: () => void;
}) {
  const t = useTranslations("platformAdmin");
  const anomalyText = (code: string) => {
    const k = anomalyLabelKey(code);
    return k ? t(k) : code;
  };
  const [w, setW] = useState<PlatformWorkspaceDetail | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [modal, setModal] = useState<DetailModalKind | null>(null);

  const reload = useCallback(() => {
    getPlatformWorkspace(workspaceId)
      .then((x) => { setW(x); onChanged?.(); })
      .catch((e) => setErr(humanAccessError(e) || t("loadError")));
  }, [workspaceId, onChanged, t]);

  useEffect(() => {
    getPlatformWorkspace(workspaceId)
      .then(setW)
      .catch((e) => setErr(humanAccessError(e) || t("loadError")));
  }, [workspaceId, t]);

  function afterMutation() {
    setModal(null);
    reload();
  }

  return (
    <Modal onClose={onClose} title={w ? w.name : t("workspace")} subtitle={w ? `ws#${w.id}${w.is_active ? "" : ` · ${t("disabledLower")}`}` : undefined}>
      {err && <div style={{ color: "var(--danger)", fontSize: 13 }}>{err}</div>}
      {!w && !err && <div style={{ color: "var(--ink-3)", fontSize: 13 }}>{t("loading")}</div>}
      {w && (
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <Pill>{t("membersCountPill", { count: w.counts.members })}</Pill>
            <Pill>{t("spacesCountPill", { count: w.counts.ragspaces_active })}</Pill>
            <Pill>{t("documentsCountPill", { count: w.counts.documents })}</Pill>
            {w.diagnostic.has_integrity_violation && <Pill color="#8A2E20" bg="var(--danger-soft)">{t("integrityViolatedPill")}</Pill>}
          </div>

          {/* Barre d'actions (mutations gated côté backend superuser). */}
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <Button size="sm" variant="secondary" icon="users" onClick={() => setModal({ kind: "assign_owner" })}>{t("assignOwner")}</Button>
            <Button size="sm" variant="secondary" icon="plus" onClick={() => setModal({ kind: "add_member" })}>{t("addMember")}</Button>
            {w.owners.length >= 2 && (
              <Button size="sm" variant="secondary" icon="refresh" onClick={() => setModal({ kind: "transfer" })}>{t("prepareTransfer")}</Button>
            )}
            {w.is_active ? (
              <Button size="sm" variant="ghost" icon="lock" onClick={() => setModal({ kind: "active", enable: false })}>{t("deactivate")}</Button>
            ) : (
              <Button size="sm" variant="ghost" icon="checkCircle" onClick={() => setModal({ kind: "active", enable: true })}>{t("reactivate")}</Button>
            )}
          </div>

          <Section title={t("storageSection")}>
            <div style={{ padding: "10px 12px", borderRadius: 9, border: "1px solid var(--border)", display: "flex", flexDirection: "column", gap: 12 }}>
              <QuotaUsageMeter usage={w.quota} />
              <div>
                <Button size="sm" variant="secondary" icon="settings" onClick={() => setModal({ kind: "quota" })}>{t("editQuota")}</Button>
              </div>
            </div>
          </Section>

          <Section title={t("sectionOwners", { count: w.owners.length })}>
            {w.owners.length === 0 ? <Empty text={t("noOwner")} /> : w.owners.map((o) => (
              <Row key={o.membership_id} left={o.user.display_name} right={<Pill color="#9A3D17" bg="#F6E7DE">owner</Pill>} sub={o.user.email} />
            ))}
          </Section>

          <Section title={t("sectionMembers", { count: w.memberships.length })}>
            {w.memberships.map((m) => (
              <Row key={m.membership_id} left={m.user.display_name} sub={m.user.email}
                right={
                  <>
                    <Pill>{m.role}</Pill>
                    {m.role !== "owner" && (
                      <Button size="sm" variant="ghost"
                        onClick={() => setModal({ kind: "owner_scope", membershipId: m.membership_id, memberName: m.user.display_name })}>
                        {t("scope")}
                      </Button>
                    )}
                    <Button size="sm" variant="ghost"
                      onClick={() => setModal({ kind: "role", membershipId: m.membership_id, currentRole: m.role, memberName: m.user.display_name })}>
                      {t("role")}
                    </Button>
                    <Button size="sm" variant="ghost"
                      onClick={() => setModal({ kind: "remove", membershipId: m.membership_id, memberName: m.user.display_name })}>
                      {t("remove")}
                    </Button>
                  </>
                } />
            ))}
          </Section>

          <Section title={t("sectionOwnerScopes", { count: w.owner_scopes.length })}>
            {w.owner_scopes.length === 0 ? <Empty text={t("noScope")} /> : w.owner_scopes.map((s) => (
              <Row key={s.membership_id} left={s.user.display_name} sub={s.user.email}
                right={<Pill>{t("attachedCount", { count: s.scoped_memberships_count })}</Pill>} />
            ))}
          </Section>

          <Section title={t("sectionRagspaces", { count: w.ragspaces.length })}>
            {w.ragspaces.length === 0 ? <Empty text={t("noSpace")} /> : w.ragspaces.map((s) => (
              <Row key={s.id} left={s.name} sub={`${t("docsCount", { count: s.documents_count })}${s.owner_scope_id ? "" : t("withoutScopeSuffix")}`}
                right={s.is_active ? null : <Pill color="#8A6D3B" bg="var(--warning-soft, #FBF3E0)">{t("deletedPill")}</Pill>} />
            ))}
          </Section>

          {w.diagnostic.anomalies.length > 0 && (
            <Section title={t("diagnostics")}>
              <div style={{ display: "flex", gap: 5, flexWrap: "wrap" }}>
                {Array.from(new Set(w.diagnostic.anomalies.map((a) => a.code))).map((code) => (
                  <Pill key={code} color="#8A6D3B" bg="var(--warning-soft, #FBF3E0)">{anomalyText(code)}</Pill>
                ))}
              </div>
            </Section>
          )}
        </div>
      )}
      <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 20 }}>
        <Button variant="secondary" onClick={onClose}>{t("close")}</Button>
      </div>

      {w && modal?.kind === "quota" && (
        <WorkspaceQuotaModal workspace={w} onClose={() => setModal(null)} onDone={afterMutation} />
      )}
      {w && modal?.kind === "assign_owner" && (
        <AssignOwnerModal workspace={w} onClose={() => setModal(null)} onDone={afterMutation} />
      )}
      {w && modal?.kind === "add_member" && (
        <AddMemberModal workspace={w} onClose={() => setModal(null)} onDone={afterMutation} />
      )}
      {w && modal?.kind === "active" && (
        <WorkspaceActiveModal workspace={w} enable={modal.enable} onClose={() => setModal(null)} onDone={afterMutation} />
      )}
      {w && modal?.kind === "transfer" && (
        <TransferPreviewModal workspace={w} onClose={() => setModal(null)} />
      )}
      {w && modal?.kind === "owner_scope" && (
        <OwnerScopeModal workspace={w} membershipId={modal.membershipId} memberName={modal.memberName}
          onClose={() => setModal(null)} onDone={afterMutation} />
      )}
      {w && modal?.kind === "role" && (
        <MembershipRoleModal workspace={w} membershipId={modal.membershipId} currentRole={modal.currentRole}
          memberName={modal.memberName} onClose={() => setModal(null)} onDone={afterMutation} />
      )}
      {w && modal?.kind === "remove" && (
        <MembershipRemoveModal workspace={w} membershipId={modal.membershipId} memberName={modal.memberName}
          onClose={() => setModal(null)} onDone={afterMutation} />
      )}
    </Modal>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <SectionLabel>{title}</SectionLabel>
      <div style={{ marginTop: 8, display: "flex", flexDirection: "column", gap: 6 }}>{children}</div>
    </div>
  );
}
function Row({ left, sub, right }: { left: string; sub?: string; right?: React.ReactNode }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "8px 11px", borderRadius: 9, border: "1px solid var(--border)" }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 13, color: "var(--ink)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{left}</div>
        {sub && <div style={{ fontSize: 11.5, color: "var(--ink-3)" }}>{sub}</div>}
      </div>
      {right}
    </div>
  );
}
function Empty({ text }: { text: string }) {
  return <div style={{ fontSize: 13, color: "var(--ink-3)" }}>{text}</div>;
}
