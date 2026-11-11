"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { AppShell, useWorkspace } from "@/components/shell";
import { Button, Card, EmptyState, Field, Icon, Pill } from "@/components/ui";
import {
  assignCorpusToWorkspace,
  createBusinessDomain,
  createDomainCorpus,
  deleteCorpusDocument,
  listBusinessDomains,
  listCorpusDocuments,
  listDomainCorpora,
  listWorkspaceCorpusAccess,
  reindexCorpusDocument,
  reindexDomainCorpus,
  removeCorpusFromWorkspace,
  updateDomainCorpus,
  uploadCorpusDocument,
  type BusinessDomain,
  type CorpusDocument,
  type DomainCorpus,
  type WorkspaceCorpusAccess,
} from "@/lib/corpus";

export default function RagatmAdminPage() {
  return (
    <AppShell>
      <AdminGate />
    </AppShell>
  );
}

function AdminGate() {
  const t = useTranslations("corpora");
  const { isGlobalOwner, loading } = useWorkspace();
  if (loading) return <div style={{ padding: 32, color: "var(--ink-3)" }}>{t("loading")}</div>;
  if (!isGlobalOwner) {
    return (
      <div style={{ padding: "48px 32px", maxWidth: 640, margin: "0 auto" }}>
        <EmptyState
          icon="lock"
          title={t("accessRestrictedTitle")}
          body={t("accessRestrictedBody")}
        />
      </div>
    );
  }
  return <AdminContent />;
}

function Section({ title, children, action }: { title: string; children: React.ReactNode; action?: React.ReactNode }) {
  return (
    <Card>
      <div style={{ padding: 18 }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 12 }}>
          <h2 style={{ fontSize: 16, fontWeight: 700, color: "var(--ink)", margin: 0 }}>{title}</h2>
          {action}
        </div>
        {children}
      </div>
    </Card>
  );
}

function AdminContent() {
  const t = useTranslations("corpora");
  const { workspaces } = useWorkspace();

  const [domains, setDomains] = useState<BusinessDomain[]>([]);
  const [selDomain, setSelDomain] = useState<string | null>(null);
  const [corpora, setCorpora] = useState<DomainCorpus[]>([]);
  const [selCorpus, setSelCorpus] = useState<string | null>(null);
  const [docs, setDocs] = useState<CorpusDocument[]>([]);
  const [msg, setMsg] = useState<string | null>(null);

  const reloadDomains = useCallback(() => {
    listBusinessDomains().then(setDomains).catch((e) => setMsg(String(e?.message || e)));
  }, []);
  const reloadCorpora = useCallback((domainId: string | null) => {
    listDomainCorpora(domainId || undefined).then(setCorpora).catch((e) => setMsg(String(e?.message || e)));
  }, []);
  const reloadDocs = useCallback((corpusId: string | null) => {
    if (!corpusId) { setDocs([]); return; }
    listCorpusDocuments(corpusId).then(setDocs).catch((e) => setMsg(String(e?.message || e)));
  }, []);

  useEffect(() => { reloadDomains(); }, [reloadDomains]);
  useEffect(() => { reloadCorpora(selDomain); }, [selDomain, reloadCorpora]);
  useEffect(() => { reloadDocs(selCorpus); }, [selCorpus, reloadDocs]);

  return (
    <div style={{ padding: "28px 32px", maxWidth: 1000, margin: "0 auto", display: "flex", flexDirection: "column", gap: 16 }}>
      <header>
        <h1 style={{ fontSize: 22, fontWeight: 700, color: "var(--ink)", margin: 0 }}>{t("adminTitle")}</h1>
        <p style={{ fontSize: 14, color: "var(--ink-3)", marginTop: 6 }}>
          {t("adminSubtitle")}
        </p>
      </header>

      {msg && (
        <div style={{ padding: "10px 14px", borderRadius: 10, background: "#fef2f2", color: "#b91c1c", fontSize: 13 }}>
          {msg}
        </div>
      )}

      <DomainsSection
        domains={domains}
        selected={selDomain}
        onSelect={setSelDomain}
        onChanged={reloadDomains}
        setMsg={setMsg}
      />

      {selDomain && (
        <CorporaSection
          domainId={selDomain}
          corpora={corpora}
          selected={selCorpus}
          onSelect={setSelCorpus}
          onChanged={() => reloadCorpora(selDomain)}
          setMsg={setMsg}
        />
      )}

      {selCorpus && (
        <DocumentsSection
          corpusId={selCorpus}
          docs={docs}
          onChanged={() => reloadDocs(selCorpus)}
          setMsg={setMsg}
        />
      )}

      {selCorpus && (
        <AssociationsSection corpusId={selCorpus} workspaces={workspaces} setMsg={setMsg} />
      )}
    </div>
  );
}

function DomainsSection({ domains, selected, onSelect, onChanged, setMsg }: {
  domains: BusinessDomain[]; selected: string | null; onSelect: (id: string) => void; onChanged: () => void; setMsg: (m: string | null) => void;
}) {
  const t = useTranslations("corpora");
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const create = async () => {
    if (!name.trim() || !slug.trim()) return;
    try {
      await createBusinessDomain({ name: name.trim(), slug: slug.trim() });
      setName(""); setSlug(""); setMsg(null); onChanged();
    } catch (e: any) { setMsg(String(e?.message || e)); }
  };
  return (
    <Section title={t("domainsSection")}>
      <div style={{ display: "flex", gap: 8, marginBottom: 14, flexWrap: "wrap", alignItems: "flex-end" }}>
        <Field label={t("fieldName")} value={name} onChange={setName} placeholder="Logistique" />
        <Field label={t("fieldSlug")} value={slug} onChange={setSlug} placeholder="finance" />
        <Button icon="plus" onClick={create}>{t("create")}</Button>
      </div>
      {domains.length === 0 ? (
        <div style={{ fontSize: 13, color: "var(--ink-3)" }}>{t("noDomains")}</div>
      ) : (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
          {domains.map((d) => (
            <button key={d.id} onClick={() => onSelect(d.id)}
              style={{
                cursor: "pointer", padding: "8px 12px", borderRadius: 10, fontSize: 13.5, fontWeight: 600,
                border: `1.5px solid ${selected === d.id ? "var(--accent)" : "var(--border)"}`,
                background: selected === d.id ? "var(--accent-soft)" : "var(--surface)", color: "var(--ink)",
              }}>
              {d.name} <span style={{ color: "var(--ink-3)", fontWeight: 400 }}>· {t("corpusCount", { count: d.corpora_count ?? 0 })}</span>
              {!d.is_active && <Pill color="#b91c1c" bg="#fef2f2" style={{ marginLeft: 6 }}>{t("inactive")}</Pill>}
            </button>
          ))}
        </div>
      )}
    </Section>
  );
}

/**
 * Pastilles d'etat de coherence des embeddings d'un corpus.
 *
 * Tout est calcule par le serveur : l'interface n'infere rien. Un corpus dont la
 * recherche vectorielle est indisponible doit se voir au premier coup d'oeil — sans
 * cette information, un exploitant peut croire un corpus operationnel alors que son leg
 * vectoriel est ecarte et que la porte d'admission le rejette.
 */
function EmbeddingConsistencyPills({ corpus }: { corpus: DomainCorpus }) {
  const t = useTranslations("corpora");
  const status = corpus.embedding_consistency_status;
  if (!status) return null;

  const label: Record<string, string> = {
    consistent: t("embeddingStatusConsistent"),
    partially_consistent: t("embeddingStatusPartial"),
    reindex_required: t("embeddingStatusReindex"),
    fingerprint_missing: t("embeddingStatusFingerprintMissing"),
    identity_mismatch: t("embeddingStatusMismatch"),
    no_ready_documents: t("embeddingStatusNoDocuments"),
  };
  const ok = status === "consistent";
  const title = [
    t("embeddingIdentity", {
      provider: corpus.embedding_provider,
      model: corpus.embedding_model || "-",
      dimension: corpus.embedding_dimension ?? 0,
    }),
    t("embeddingConsistent", {
      consistent: corpus.embedding_consistent_document_count ?? 0,
      ready: corpus.ready_documents_count ?? 0,
    }),
    (corpus.embedding_reindex_required_document_count ?? 0) > 0
      ? t("embeddingReindexRequired", { count: corpus.embedding_reindex_required_document_count ?? 0 })
      : "",
    (corpus.embedding_unknown_document_count ?? 0) > 0
      ? t("embeddingUnknown", { count: corpus.embedding_unknown_document_count ?? 0 })
      : "",
    corpus.active_calibration_valid
      ? t("embeddingCalibrationValid", { version: corpus.active_calibration_version ?? 0 })
      : corpus.active_calibration_error_code
        ? t("embeddingCalibrationInvalid", { code: corpus.active_calibration_error_code })
        : t("embeddingCalibrationNone"),
  ].filter(Boolean).join(" - ");

  return (
    <span title={title} style={{ display: "inline-flex", gap: 6 }}>
      <Pill color={ok ? "#166534" : "#b45309"} bg={ok ? "#f0fdf4" : "#fffbeb"}>
        {label[status] ?? status}
      </Pill>
      {corpus.vector_retrieval_available === false && (
        <Pill color="#b91c1c" bg="#fef2f2">{t("embeddingVectorUnavailable")}</Pill>
      )}
    </span>
  );
}

function CorporaSection({ domainId, corpora, selected, onSelect, onChanged, setMsg }: {
  domainId: string; corpora: DomainCorpus[]; selected: string | null; onSelect: (id: string) => void; onChanged: () => void; setMsg: (m: string | null) => void;
}) {
  const t = useTranslations("corpora");
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const create = async () => {
    if (!name.trim() || !slug.trim()) return;
    try {
      await createDomainCorpus({ domain_id: domainId, name: name.trim(), slug: slug.trim() });
      setName(""); setSlug(""); setMsg(null); onChanged();
    } catch (e: any) { setMsg(String(e?.message || e)); }
  };
  const toggleActive = async (c: DomainCorpus) => {
    try { await updateDomainCorpus(c.id, { is_active: !c.is_active }); onChanged(); }
    catch (e: any) { setMsg(String(e?.message || e)); }
  };
  const reindex = async (c: DomainCorpus) => {
    try { const r = await reindexDomainCorpus(c.id); setMsg(t("reindexResult", { reindexed: r.reindexed, failed: r.failed, skipped: r.skipped })); }
    catch (e: any) { setMsg(String(e?.message || e)); }
  };
  return (
    <Section title={t("domainCorporaSection")}>
      <div style={{ display: "flex", gap: 8, marginBottom: 14, flexWrap: "wrap", alignItems: "flex-end" }}>
        <Field label={t("fieldCorpusName")} value={name} onChange={setName} placeholder="Procédures qualité" />
        <Field label={t("fieldSlug")} value={slug} onChange={setSlug} placeholder="amf" />
        <Button icon="plus" onClick={create}>{t("create")}</Button>
      </div>
      {corpora.length === 0 ? (
        <div style={{ fontSize: 13, color: "var(--ink-3)" }}>{t("noCorpora")}</div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {corpora.map((c) => (
            <div key={c.id} style={{
              display: "flex", alignItems: "center", gap: 10, padding: "10px 12px", borderRadius: 10,
              border: `1.5px solid ${selected === c.id ? "var(--accent)" : "var(--border)"}`,
              background: selected === c.id ? "var(--accent-soft)" : "var(--surface)",
            }}>
              <button onClick={() => onSelect(c.id)} style={{ flex: 1, textAlign: "left", cursor: "pointer", background: "transparent", border: "none", fontSize: 14, fontWeight: 600, color: "var(--ink)" }}>
                {c.name}{" "}
                {/* Compteur d'exploitabilité : le total seul ne dit pas combien de
                    documents le chat peut réellement interroger. */}
                <span style={{ color: "var(--ink-3)", fontWeight: 400 }}>
                  · {c.ready_documents_count != null
                      ? t("docsProviderReady", {
                          count: c.documents_count ?? 0,
                          ready: c.ready_documents_count,
                          provider: c.embedding_provider,
                        })
                      : t("docsProvider", {
                          count: c.documents_count ?? 0,
                          provider: c.embedding_provider,
                        })}
                  {(c.failed_documents_count ?? 0) > 0
                    ? ` · ${t("failedDocumentsCount", { failed: c.failed_documents_count })}`
                    : ""}
                </span>
              </button>
              {!c.is_active && <Pill color="#b91c1c" bg="#fef2f2">{t("inactive")}</Pill>}
              <EmbeddingConsistencyPills corpus={c} />
              <Button size="sm" variant="ghost" onClick={() => toggleActive(c)}>{c.is_active ? t("deactivate") : t("activate")}</Button>
              <Button size="sm" variant="ghost" icon="refresh" onClick={() => reindex(c)}>{t("reindex")}</Button>
            </div>
          ))}
        </div>
      )}
    </Section>
  );
}

function DocumentsSection({ corpusId, docs, onChanged, setMsg }: {
  corpusId: string; docs: CorpusDocument[]; onChanged: () => void; setMsg: (m: string | null) => void;
}) {
  const t = useTranslations("corpora");
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [busy, setBusy] = useState(false);

  const onFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (!f) return;
    setBusy(true); setMsg(null);
    try { await uploadCorpusDocument(corpusId, f); onChanged(); }
    catch (err: any) { setMsg(String(err?.message || err)); }
    finally { setBusy(false); if (fileRef.current) fileRef.current.value = ""; }
  };
  const remove = async (d: CorpusDocument) => {
    try { await deleteCorpusDocument(d.id); onChanged(); } catch (e: any) { setMsg(String(e?.message || e)); }
  };
  const reindex = async (d: CorpusDocument) => {
    try { await reindexCorpusDocument(d.id); onChanged(); } catch (e: any) { setMsg(String(e?.message || e)); }
  };

  const statusColor = (s: string) => s === "READY" ? "#15803d" : s === "FAILED" ? "#b91c1c" : "#b45309";
  const statusBg = (s: string) => s === "READY" ? "#f0fdf4" : s === "FAILED" ? "#fef2f2" : "#fffbeb";

  return (
    <Section title={t("documentsSection")} action={
      <>
        <input ref={fileRef} type="file" onChange={onFile} style={{ display: "none" }} />
        <Button icon="upload" disabled={busy} onClick={() => fileRef.current?.click()}>
          {busy ? t("importing") : t("importDocument")}
        </Button>
      </>
    }>
      {docs.length === 0 ? (
        <div style={{ fontSize: 13, color: "var(--ink-3)" }}>{t("noDocuments")}</div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {docs.map((d) => (
            <div key={d.id} style={{ display: "flex", alignItems: "center", gap: 10, padding: "10px 12px", borderRadius: 10, border: "1px solid var(--border)" }}>
              <Icon name="doc" size={16} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 13.5, fontWeight: 600, color: "var(--ink)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {d.title || d.original_filename}
                </div>
                {d.status === "FAILED" && d.error_message && (
                  <div style={{ fontSize: 12, color: "#b91c1c" }}>{d.error_message}</div>
                )}
              </div>
              <Pill color={statusColor(d.status)} bg={statusBg(d.status)}>{d.status}</Pill>
              <span style={{ fontSize: 12, color: "var(--ink-3)" }}>{t("chunksCount", { count: d.chunks_count ?? 0 })}</span>
              <Button size="sm" variant="ghost" icon="refresh" onClick={() => reindex(d)}>{t("reindex")}</Button>
              <Button size="sm" variant="ghost" icon="trash" onClick={() => remove(d)}>{t("deleteShort")}</Button>
            </div>
          ))}
        </div>
      )}
    </Section>
  );
}

function AssociationsSection({ corpusId, workspaces, setMsg }: {
  corpusId: string; workspaces: { id: number; name: string }[]; setMsg: (m: string | null) => void;
}) {
  const t = useTranslations("corpora");
  const [wsId, setWsId] = useState<number | null>(workspaces[0]?.id ?? null);
  const [access, setAccess] = useState<WorkspaceCorpusAccess[]>([]);

  const reload = useCallback(() => {
    if (wsId === null) { setAccess([]); return; }
    listWorkspaceCorpusAccess(wsId).then(setAccess).catch((e) => setMsg(String(e?.message || e)));
  }, [wsId, setMsg]);
  useEffect(() => { reload(); }, [reload]);

  const isAssigned = access.some((a) => a.corpus_id === corpusId && a.enabled);
  const toggle = async () => {
    if (wsId === null) return;
    try {
      if (isAssigned) await removeCorpusFromWorkspace(wsId, corpusId);
      else await assignCorpusToWorkspace(wsId, corpusId, true);
      reload();
    } catch (e: any) { setMsg(String(e?.message || e)); }
  };

  return (
    <Section title={t("associationsSection")}>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <select value={wsId ?? ""} onChange={(e) => setWsId(Number(e.target.value))}
          style={{ padding: "9px 11px", borderRadius: 9, border: "1px solid var(--border)", fontSize: 14, background: "var(--surface)", color: "var(--ink)" }}>
          {workspaces.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
        </select>
        <Button variant={isAssigned ? "ghost" : "primary"} icon={isAssigned ? "trash" : "plus"} onClick={toggle}>
          {isAssigned ? t("removeCorpus") : t("assignCorpus")}
        </Button>
      </div>
      <div style={{ marginTop: 14 }}>
        <div style={{ fontSize: 12.5, color: "var(--ink-3)", marginBottom: 6 }}>{t("alreadyAssignedLabel")}</div>
        {access.length === 0 ? (
          <div style={{ fontSize: 13, color: "var(--ink-3)" }}>{t("none")}</div>
        ) : (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
            {access.map((a) => (
              <Pill key={a.id} color={a.enabled ? "#15803d" : "var(--ink-3)"} bg={a.enabled ? "#f0fdf4" : "var(--chip)"}>
                {a.corpus_name || a.corpus_id}{!a.enabled ? t("disabledSuffix") : ""}
              </Pill>
            ))}
          </div>
        )}
      </div>
    </Section>
  );
}
