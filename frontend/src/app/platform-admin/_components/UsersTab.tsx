"use client";

import React, { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Avatar, Button, Card, EmptyState, Field, Icon, Modal, Pill, SectionLabel, initialsFrom } from "@/components/ui";
import { useWorkspace } from "@/components/shell";
import { shortDate } from "@/lib/format";
import { humanAccessError } from "@/lib/apiError";
import {
  listPlatformUsers,
  getPlatformUser,
  type PlatformUserRow,
  type PlatformUserDetail,
} from "@/lib/platformAdmin";
import { UserActiveModal } from "./PlatformModals";

const PAGE = 25;

export function UsersTab() {
  const t = useTranslations("platformAdmin");
  const [rows, setRows] = useState<PlatformUserRow[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [search, setSearch] = useState("");
  const [onlyActive, setOnlyActive] = useState(false);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [detailId, setDetailId] = useState<number | null>(null);

  const load = useCallback(async (off: number, q: string, active: boolean) => {
    setLoading(true);
    setErr(null);
    try {
      const p = await listPlatformUsers({
        search: q || undefined,
        is_active: active ? true : undefined,
        limit: PAGE,
        offset: off,
      });
      setRows(p.items);
      setTotal(p.total);
      setOffset(p.offset);
    } catch (e) {
      setErr(humanAccessError(e) || t("usersLoadError"));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    load(0, "", false);
  }, [load]);

  return (
    <div>
      <div style={{ display: "flex", gap: 10, alignItems: "center", marginBottom: 14, flexWrap: "wrap" }}>
        <div style={{ flex: 1, minWidth: 220 }}>
          <Field
            icon="search"
            placeholder={t("searchUsersPlaceholder")}
            value={search}
            onChange={setSearch}
            onKeyDown={(e: React.KeyboardEvent) => e.key === "Enter" && load(0, search, onlyActive)}
          />
        </div>
        <label style={{ display: "inline-flex", alignItems: "center", gap: 7, fontSize: 13, color: "var(--ink-2)" }}>
          <input type="checkbox" checked={onlyActive} onChange={(e) => { setOnlyActive(e.target.checked); load(0, search, e.target.checked); }} style={{ accentColor: "var(--accent)" }} />
          {t("activeOnly")}
        </label>
        <Button size="sm" variant="secondary" icon="search" onClick={() => load(0, search, onlyActive)}>
          {t("search")}
        </Button>
      </div>

      {err && <div style={{ color: "var(--danger)", fontSize: 13, marginBottom: 12 }}>{err}</div>}

      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
        <SectionLabel>{t("usersCount", { count: total })}</SectionLabel>
      </div>

      {loading && rows.length === 0 ? (
        <Card style={{ padding: 32, color: "var(--ink-3)", fontSize: 14 }}>{t("loading")}</Card>
      ) : rows.length === 0 ? (
        <Card style={{ padding: 8 }}>
          <EmptyState icon="users" title={t("noUsersTitle")} body={t("noResultsCriteria")} />
        </Card>
      ) : (
        <Card style={{ overflow: "hidden", padding: 0 }}>
          <div style={{ overflowX: "auto" }}>
            <div style={{ minWidth: 720 }}>
              <div style={{ display: "grid", gridTemplateColumns: "1.6fr 90px 90px 110px 110px 80px", gap: 10, padding: "11px 18px", borderBottom: "1px solid var(--border)", background: "var(--sidebar)" }}>
                {[t("colUser"), t("colActive"), t("colSuperuser"), t("workspaces"), t("colOwnerOf"), ""].map((h, i) => (
                  <SectionLabel key={i}>{h}</SectionLabel>
                ))}
              </div>
              {rows.map((u, i) => (
                <div key={u.id} style={{ display: "grid", gridTemplateColumns: "1.6fr 90px 90px 110px 110px 80px", gap: 10, alignItems: "center", padding: "11px 18px", borderBottom: i === rows.length - 1 ? "none" : "1px solid var(--border)" }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 0 }}>
                    <Avatar initials={initialsFrom(u.email || u.username)} size={32} />
                    <div style={{ minWidth: 0 }}>
                      <div style={{ fontSize: 13.5, fontWeight: 600, color: "var(--ink)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{u.display_name}</div>
                      <div style={{ fontSize: 11.5, color: "var(--ink-3)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{u.email}</div>
                    </div>
                  </div>
                  <span>{u.is_active ? <Pill color="#2E7D32" bg="var(--success-soft, #E7F4E8)">{t("yes")}</Pill> : <Pill color="#8A2E20" bg="var(--danger-soft)">{t("no")}</Pill>}</span>
                  <span>{u.is_superuser ? <Pill color="#9A3D17" bg="#F6E7DE">{t("yes")}</Pill> : <span style={{ color: "var(--ink-3)", fontSize: 12.5 }}>—</span>}</span>
                  <span style={{ fontSize: 12.5, color: "var(--ink-2)" }}>{u.memberships_count}</span>
                  <span style={{ fontSize: 12.5, color: "var(--ink-2)" }}>{u.owned_workspaces_count}</span>
                  <Button size="sm" variant="ghost" onClick={() => setDetailId(u.id)}>{t("details")}</Button>
                </div>
              ))}
            </div>
          </div>
        </Card>
      )}

      {/* Pagination */}
      {total > PAGE && (
        <div style={{ display: "flex", justifyContent: "center", gap: 10, marginTop: 16, alignItems: "center" }}>
          <Button size="sm" variant="secondary" disabled={offset === 0 || loading} onClick={() => load(Math.max(0, offset - PAGE), search, onlyActive)}>{t("previous")}</Button>
          <span style={{ fontSize: 12.5, color: "var(--ink-3)" }}>{offset + 1}–{Math.min(offset + PAGE, total)} / {total}</span>
          <Button size="sm" variant="secondary" disabled={offset + PAGE >= total || loading} onClick={() => load(offset + PAGE, search, onlyActive)}>{t("next")}</Button>
        </div>
      )}

      {detailId != null && (
        <UserDetailModal userId={detailId} onClose={() => setDetailId(null)} onChanged={() => load(offset, search, onlyActive)} />
      )}
    </div>
  );
}

function UserDetailModal({ userId, onClose, onChanged }: { userId: number; onClose: () => void; onChanged?: () => void }) {
  const t = useTranslations("platformAdmin");
  const { me } = useWorkspace();
  const [u, setU] = useState<PlatformUserDetail | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [activeModal, setActiveModal] = useState(false);

  const reload = useCallback(() => {
    getPlatformUser(userId)
      .then((x) => { setU(x); onChanged?.(); })
      .catch((e) => setErr(humanAccessError(e) || t("loadError")));
  }, [userId, onChanged, t]);

  useEffect(() => {
    let alive = true;
    getPlatformUser(userId)
      .then((x) => alive && setU(x))
      .catch((e) => alive && setErr(humanAccessError(e) || t("loadError")));
    return () => {
      alive = false;
    };
  }, [userId, t]);

  const isSelf = !!me && String(me.id) === String(userId);

  return (
    <Modal onClose={onClose} title={u ? u.display_name : t("user")} subtitle={u?.email}>
      {err && <div style={{ color: "var(--danger)", fontSize: 13 }}>{err}</div>}
      {!u && !err && <div style={{ color: "var(--ink-3)", fontSize: 13 }}>{t("loading")}</div>}
      {u && (
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
            {u.is_superuser && <Pill color="#9A3D17" bg="#F6E7DE">{t("colSuperuser")}</Pill>}
            {u.is_active ? <Pill color="#2E7D32" bg="var(--success-soft, #E7F4E8)">{t("colActive")}</Pill> : <Pill color="#8A2E20" bg="var(--danger-soft)">{t("disabledPill")}</Pill>}
            <Pill>{t("registeredOn", { date: shortDate(u.date_joined || "") })}</Pill>
            <Pill>{t("spaceAccessCount", { count: u.active_ragspace_accesses_count })}</Pill>
            <div style={{ flex: 1 }} />
            <Button size="sm" variant={u.is_active ? "ghost" : "secondary"} icon={u.is_active ? "lock" : "checkCircle"} onClick={() => setActiveModal(true)}>
              {u.is_active ? t("deactivate") : t("reactivate")}
            </Button>
          </div>

          <div>
            <SectionLabel>{t("membershipsCount", { count: u.memberships.length })}</SectionLabel>
            <div style={{ marginTop: 8, display: "flex", flexDirection: "column", gap: 6 }}>
              {u.memberships.length === 0 && <div style={{ fontSize: 13, color: "var(--ink-3)" }}>{t("noWorkspace")}</div>}
              {u.memberships.map((m) => (
                <div key={m.membership_id} style={{ display: "flex", alignItems: "center", gap: 10, padding: "8px 11px", borderRadius: 9, border: "1px solid var(--border)" }}>
                  <span style={{ flex: 1, fontSize: 13, color: "var(--ink)" }}>
                    {m.workspace_name} <span style={{ color: "var(--ink-3)" }}>· ws#{m.workspace_id}</span>
                    {!m.workspace_is_active && <span style={{ color: "#8A6D3B" }}>{t("disabledSuffix")}</span>}
                  </span>
                  <Pill>{m.role}</Pill>
                  {m.role !== "owner" && (
                    <span style={{ fontSize: 11.5, color: "var(--ink-3)" }}>
                      {t("scopeLabel", { name: m.owner_membership ? m.owner_membership.display_name : "—" })}
                    </span>
                  )}
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
      <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 20 }}>
        <Button variant="secondary" onClick={onClose}>{t("close")}</Button>
      </div>

      {u && activeModal && (
        <UserActiveModal user={u} isSelf={isSelf} onClose={() => setActiveModal(false)} onDone={() => { setActiveModal(false); reload(); }} />
      )}
    </Modal>
  );
}
