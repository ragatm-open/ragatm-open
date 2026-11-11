"use client";

import React, { useEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { apiFetch } from "@/lib/api";
import {
  CatalogEntry,
  ModelCatalog,
  PROVIDER_AI_GATEWAY,
  chatProviderSelectable,
  fetchModelCatalog,
  findEntry,
  isGoverned,
  selectable,
} from "@/lib/modelCatalog";
import { useWorkspace } from "@/components/shell";
import { Button, Field, Icon, Modal } from "@/components/ui";
import type { LlmTestResult, RagSpace } from "../_lib/types";

// AUCUNE liste de providers ni de modèles n'est déclarée ici — c'est le point de ce
// lot, et c'est verrouillé par un test.
//
// Le composant portait une liste de moteurs libre et deux champs de
// TEXTE LIBRE. Trois défauts en découlaient, tous constatés :
//
//   1. le mot « Provider » désignait une préférence LOGIQUE en haut de l'écran
//      (le chat passe par la Gateway quel que soit le choix) et un TRANSPORT réel en
//      bas (un moteur mal choisi changeait réellement le pipeline d'embeddings) ;
//   2. le champ modèle acceptait n'importe quelle chaîne : `bge-m3` était accepté,
//      marquait tous les documents à réindexer, puis échouait à l'ingestion ;
//   3. le pipeline gouverné, seul à porter une identité vectorielle vérifiée,
//      n'apparaissait nulle part.
//
// La source est désormais `GET /api/v1/ai/model-catalog/`, qui projette le registre
// de la passerelle et y ajoute la gouvernance locale.

/** Payload commun création / mise à jour. */
export type SpaceSettingsPayload = {
  name: string;
  llm_provider?: string;
  llm_model?: string;
  embedding_provider?: string;
  embedding_model?: string;
};

type SpaceConfig = {
  llm_provider: string;
  llm_model: string;
  embedding_provider: string;
  embedding_model: string;
  can_edit_model_configuration?: boolean;
  documents?: { total: number; ready: number; failed: number; reindex_required: number };
};

type Policy = {
  llm_provider: string;
  llm_model: string;
  embedding_provider: string;
  embedding_model: string;
  can_edit: boolean;
};

/** Moteur affiché ⇄ provider persisté. Changement d'AFFICHAGE, pas de schéma. */
// RAGatm Open : un seul moteur de chat — le modèle local servi par l'AI Gateway.
const ENGINES = [
  { value: "ollama", labelKey: "engineLocal" },
] as const;

/** Badge textuel — jamais porteur de sens par la seule couleur (a11y). */
function Badge({ tone, children }: { tone: "reco" | "ok" | "warn" | "muted"; children: React.ReactNode }) {
  const palette = {
    reco: { bg: "var(--success-soft, #E7F6EC)", fg: "var(--success, #1B7F4B)", bd: "var(--success, #1B7F4B)" },
    ok: { bg: "var(--chip)", fg: "var(--ink-2)", bd: "var(--border)" },
    warn: { bg: "var(--warning-soft, #FFF6E6)", fg: "#8A5A00", bd: "var(--warning-border, #F0D9A8)" },
    muted: { bg: "transparent", fg: "var(--ink-3)", bd: "var(--border)" },
  }[tone];
  return (
    <span
      style={{
        display: "inline-block", padding: "1px 7px", borderRadius: 999, fontSize: 11,
        fontWeight: 700, background: palette.bg, color: palette.fg,
        border: `1px solid ${palette.bd}`, whiteSpace: "nowrap",
      }}
    >
      {children}
    </span>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div style={{ fontSize: 12, color: "var(--ink-3)", marginBottom: 6 }}>{label}</div>
      {children}
    </div>
  );
}

/** Valeur en lecture seule — pas un input désactivé trompeur. */
function ReadonlyValue({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <Row label={label}>
      <div
        style={{
          padding: "9px 12px", borderRadius: 10, background: "var(--chip)",
          border: "1px solid var(--border)", fontSize: 13.5, color: "var(--ink)",
          fontFamily: "ui-monospace, monospace",
        }}
      >
        {value || "—"}
      </div>
    </Row>
  );
}

function Notice({
  tone, icon, children, testId,
}: {
  tone: "info" | "warn" | "danger";
  icon: string;
  children: React.ReactNode;
  testId?: string;
}) {
  const palette = {
    info: { bg: "var(--chip)", bd: "var(--border)", fg: "var(--ink-2)" },
    warn: { bg: "var(--warning-soft, #FFF6E6)", bd: "var(--warning-border, #F0D9A8)", fg: "var(--ink-2)" },
    danger: { bg: "var(--danger-soft)", bd: "var(--danger-border)", fg: "#8A2E20" },
  }[tone];
  return (
    <div
      data-testid={testId}
      style={{
        display: "flex", gap: 9, alignItems: "flex-start", padding: "9px 12px",
        borderRadius: 10, background: palette.bg, border: `1px solid ${palette.bd}`,
        fontSize: 12.5, color: palette.fg,
      }}
    >
      <Icon name={icon} size={15} style={{ flexShrink: 0, marginTop: 1 }} />
      <span>{children}</span>
    </div>
  );
}

const selectStyle: React.CSSProperties = {
  width: "100%", padding: "9px 12px", borderRadius: 10, fontSize: 13.5,
  border: "1px solid var(--border)", background: "var(--surface)", color: "var(--ink)",
  fontFamily: "inherit",
};

export function SpaceEditModal({
  space, onClose, onCreate, onPatch,
}: {
  space: RagSpace | null;
  onClose: () => void;
  onCreate: (payload: SpaceSettingsPayload) => Promise<void>;
  onPatch: (id: string, payload: SpaceSettingsPayload) => Promise<void>;
}) {
  const t = useTranslations("workspaceAdmin");
  const isNew = !space;
  const { isSuperuser } = useWorkspace();
  // Autorité backend : seul un superuser actif édite provider/modèle. Le composant
  // reflète cette politique, il ne la déduit jamais d'un rôle workspace/space.
  const canEditModels = isSuperuser;

  const [name, setName] = useState(space?.name || "");
  const [catalog, setCatalog] = useState<ModelCatalog | null>(null);
  const [catalogError, setCatalogError] = useState(false);
  const [config, setConfig] = useState<SpaceConfig | null>(null);
  const [loaded, setLoaded] = useState(false);
  // Refus d'accès à la configuration de CET espace. Distinct de « pas superuser » :
  // sous enforcement owner-scope, un superuser peut être autorisé à changer des
  // modèles en général et ne pas atteindre cet espace-ci. L'utilisateur doit le voir
  // AVANT de remplir un formulaire, pas découvrir un 403 après validation.
  const [accessDenied, setAccessDenied] = useState(false);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // Sonde de connexion, conservée du formulaire précédent : le catalogue peut être
  // servi depuis un état antérieur (`stale`), ce test-là interroge la passerelle
  // MAINTENANT. Les deux ne disent pas la même chose.
  const [test, setTest] = useState<LlmTestResult | null>(null);
  const [testing, setTesting] = useState(false);

  // Sélections. Vides tant que le catalogue/la config ne sont pas chargés : on ne
  // devine AUCUNE valeur côté client.
  const [engine, setEngine] = useState("");
  const [chatModel, setChatModel] = useState("");
  const [pipeline, setPipeline] = useState("");
  const touched = useRef(false);

  /** Configuration initiale, pour détecter un changement réel de pipeline. */
  const initial = useRef({ provider: "", model: "" });
  /** Chat initial : une valeur INCHANGÉE d'un espace historique n'est pas rejugée. */
  const initialChat = useRef({ provider: "", model: "" });

  useEffect(() => {
    let alive = true;
    (async () => {
      const cat = await fetchModelCatalog().catch(() => null);
      if (!alive) return;
      if (!cat || !cat.available) {
        setCatalog(cat);
        setCatalogError(true);
      } else {
        setCatalog(cat);
      }

      try {
        if (!isNew && space) {
          const data = await apiFetch<SpaceConfig>(`/api/v1/rag-spaces/${space.id}/llm-settings/`);
          if (!alive) return;
          setConfig(data);
          initial.current = { provider: data.embedding_provider, model: data.embedding_model };
          initialChat.current = { provider: data.llm_provider, model: data.llm_model };
          if (!touched.current) {
            setEngine(data.llm_provider);
            setChatModel(data.llm_model);
            setPipeline(data.embedding_model);
          }
        } else {
          // Création : les défauts viennent du backend (`ragspace_model_policy`),
          // jamais d'un littéral du composant — c'est ce qui avait produit la
          // divergence `llama3.1:8b`.
          const caps = await apiFetch<{ ragspace_model_policy?: Policy }>("/api/v1/capabilities/");
          if (!alive) return;
          const p = caps.ragspace_model_policy;
          if (p) {
            setConfig({
              llm_provider: p.llm_provider, llm_model: p.llm_model,
              embedding_provider: p.embedding_provider, embedding_model: p.embedding_model,
            });
            initial.current = { provider: p.embedding_provider, model: p.embedding_model };
            if (!touched.current) {
              setEngine(p.llm_provider);
              setChatModel(p.llm_model);
              setPipeline(p.embedding_model);
            }
          }
        }
      } catch (e: any) {
        // 403/404 : l'espace existe mais son périmètre nous est fermé. Tout autre
        // échec laisse simplement les champs vides — on n'invente aucune valeur.
        if (alive && (e?.status === 403 || e?.status === 404)) setAccessDenied(true);
      }
      if (alive) setLoaded(true);
    })();
    return () => {
      alive = false;
    };
  }, [isNew, space]);

  const chatOptions = useMemo(() => selectable(catalog?.chat), [catalog]);
  const governedPipelines = useMemo(() => selectable(catalog?.embeddings), [catalog]);

  const spaceIsGoverned = isGoverned(config?.embedding_provider);

  /**
   * Options de pipeline : les pipelines gouvernés, plus le pipeline HISTORIQUE de
   * l'espace s'il en a un. Le second n'est pas proposé aux nouveaux espaces — mais il
   * ne doit pas disparaître pour un espace existant, sous peine de le migrer par la
   * simple ouverture du formulaire (§40).
   */
  const pipelineOptions = useMemo(() => {
    const opts = governedPipelines.map((e) => ({ entry: e, legacy: false }));
    if (!spaceIsGoverned && config?.embedding_model) {
      opts.unshift({
        entry: {
          id: config.embedding_model,
          kind: "embedding",
          display_name: config.embedding_model,
          available: true, allowed: true, qualified: false, recommended: false,
          capabilities: [], registry_status: "", upstream_model: config.embedding_model,
        } as CatalogEntry,
        legacy: true,
      });
    }
    return opts;
  }, [governedPipelines, spaceIsGoverned, config]);

  const selectedPipeline = useMemo(
    () => pipelineOptions.find((o) => o.entry.id === pipeline),
    [pipelineOptions, pipeline]
  );
  const selectedChat = findEntry(catalog?.chat, chatModel);

  const pipelineChanged =
    !isNew && !!selectedPipeline && selectedPipeline.entry.id !== initial.current.model;

  const catalogUsable = !!catalog && catalog.available && !catalogError;
  const fieldsEditable = canEditModels && catalogUsable && !accessDenied;

  // Même règle que le backend (`catalog_config_error`) : seule une valeur qui CHANGE
  // est jugée. Un espace historique garde son modèle tant qu'on n'y touche pas.
  // Un modèle peut être désigné par son identifiant amont ou par son nom LOGIQUE du
  // registre Gateway (alias côté backend) : les deux sont sélectionnables.
  const chatOptionFor = (value: string) =>
    chatOptions.find((o) => o.id === value || (!!o.display_name && o.display_name === value));
  const chatModelSelectable = !!chatOptionFor(chatModel);
  // Valeur affichée par le <select> : l'identifiant canonique de l'entrée correspondante.
  const chatSelectValue = chatOptionFor(chatModel)?.id ?? chatModel;
  const chatModelChanged = isNew || chatModel !== initialChat.current.model;
  const engineChanged = isNew || engine !== initialChat.current.provider;
  const chatModelUnusable = !!chatModel && !chatModelSelectable;
  const chatBlocked =
    fieldsEditable &&
    ((chatModelUnusable && (chatModelChanged || engineChanged)) ||
      (engineChanged && !!engine && !chatProviderSelectable(catalog, engine)));

  /** Motif lisible d'un modèle de chat non utilisable (entrée de catalogue ou absence). */
  function chatUnusableMessage(): string {
    switch (selectedChat?.generation_status) {
      case "unsupported":
        return t("chatModelNotSupported");
      case "unsafe_output":
        return t("chatModelUnsafeOutput");
      case "not_verified":
        return t("chatModelNotVerified");
      default:
        return selectedChat ? t("chatModelNotSupported") : t("chatModelUnavailable");
    }
  }

  function pick<T>(setter: (v: T) => void) {
    return (v: T) => {
      touched.current = true;
      setErr(null);
      setter(v);
    };
  }

  async function runTest() {
    if (!space) return;
    setTesting(true);
    setTest(null);
    try {
      setTest(
        await apiFetch<LlmTestResult>(`/api/v1/rag-spaces/${space.id}/llm-settings/test/`, {
          method: "POST",
          body: JSON.stringify({ provider: engine, model: chatModel }),
        })
      );
    } catch (e: any) {
      setTest({ ok: false, message: e?.message || t("testFailed") });
    } finally {
      setTesting(false);
    }
  }

  async function save() {
    setErr(null);
    if (!name.trim()) {
      setErr(t("spaceNameRequired"));
      return;
    }
    // Un non-superuser ne soumet JAMAIS les champs modèle. Un catalogue indisponible
    // non plus : sans lui, on ne peut pas garantir que la valeur reste admissible.
    const payload: SpaceSettingsPayload = fieldsEditable
      ? {
          name: name.trim(),
          llm_provider: engine,
          llm_model: chatModel,
          // Un pipeline gouverné se persiste en (ai_gateway, <pipeline logique>).
          // Le pipeline historique se renvoie À L'IDENTIQUE : aucune conversion
          // implicite, même quand le modèle amont est le même (§40).
          embedding_provider: selectedPipeline?.legacy
            ? initial.current.provider
            : PROVIDER_AI_GATEWAY,
          embedding_model: pipeline,
        }
      : { name: name.trim() };

    setSaving(true);
    try {
      if (isNew) await onCreate(payload);
      else await onPatch(space!.id, payload);
    } catch (e: any) {
      // Échec : on NE marque rien comme enregistré et on revient à l'état serveur.
      setErr(e?.message || t("saveFailed"));
      setSaving(false);
      if (!isNew && space) {
        apiFetch<SpaceConfig>(`/api/v1/rag-spaces/${space.id}/llm-settings/`)
          .then((data) => {
            setConfig(data);
            setEngine(data.llm_provider);
            setChatModel(data.llm_model);
            setPipeline(data.embedding_model);
            initial.current = { provider: data.embedding_provider, model: data.embedding_model };
            initialChat.current = { provider: data.llm_provider, model: data.llm_model };
          })
          .catch(() => undefined);
      }
    }
  }

  const sectionTitle: React.CSSProperties = {
    fontSize: 13, fontWeight: 700, color: "var(--ink)", marginBottom: 8,
  };
  const card: React.CSSProperties = {
    padding: "14px 15px", borderRadius: 12, border: "1px solid var(--border)",
    background: "var(--surface)", display: "flex", flexDirection: "column", gap: 12,
  };

  function entryBadges(e: CatalogEntry, legacy: boolean) {
    return (
      <span style={{ display: "inline-flex", gap: 6, flexWrap: "wrap" }}>
        {legacy && <Badge tone="warn">{t("badgeLegacy")}</Badge>}
        {e.recommended && <Badge tone="reco">{t("badgeRecommended")}</Badge>}
        {!legacy && e.qualified && !e.recommended && <Badge tone="ok">{t("badgeQualified")}</Badge>}
        {!legacy && !e.qualified && <Badge tone="muted">{t("badgeUnqualified")}</Badge>}
      </span>
    );
  }

  return (
    <Modal
      onClose={onClose}
      title={isNew ? t("createSpace") : t("editSpaceNamed", { name: space?.name ?? "" })}
      subtitle={isNew ? t("spaceModalSubtitle") : undefined}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
        <Field
          label={t("spaceNameLabel")}
          placeholder={t("spaceNamePlaceholder")}
          value={name}
          onChange={setName}
          autoFocus
        />

        {!canEditModels && (
          <Notice tone="info" icon="lock">{t("modelsPlatformManaged")}</Notice>
        )}
        {canEditModels && accessDenied && (
          <Notice tone="warn" icon="lock" testId="permission-denied">
            {t("noPermissionModels")}
          </Notice>
        )}
        {canEditModels && catalogError && (
          <Notice tone="warn" icon="alert" testId="catalog-unavailable">
            <strong>{t("catalogUnavailable")}</strong> {t("catalogUnavailableHelp")}
          </Notice>
        )}
        {canEditModels && catalogUsable && catalog?.stale && (
          <Notice tone="warn" icon="alert">{t("catalogStale")}</Notice>
        )}

        {/* ===================== Chat / génération ===================== */}
        <div style={card}>
          <div style={sectionTitle}>{t("chatSection")}</div>

          {!fieldsEditable ? (
            <>
              <ReadonlyValue
                label={t("engine")}
                value={engine ? t("engineLocal") : ""}
              />
              <ReadonlyValue label={t("generationModel")} value={chatModel} />
            </>
          ) : (
            <>
              <Row label={t("engine")}>
                <div
                  role="radiogroup"
                  aria-label={t("engine")}
                  style={{
                    display: "flex", gap: 4, padding: 4, borderRadius: 10,
                    background: "var(--chip)", border: "1px solid var(--border)",
                    width: "fit-content",
                  }}
                >
                  {ENGINES.map((e) => {
                    // Un moteur non opérationnel n'est pas proposé. Le moteur ACTUEL d'un
                    // espace historique reste affiché comme sélectionné.
                    const off = !chatProviderSelectable(catalog, e.value) && engine !== e.value;
                    return (
                    <button
                      key={e.value}
                      type="button"
                      role="radio"
                      aria-checked={engine === e.value}
                      aria-disabled={off || undefined}
                      disabled={off}
                      title={off ? t("engineUnavailable") : undefined}
                      onClick={() => pick(setEngine)(e.value)}
                      style={{
                        padding: "6px 14px", borderRadius: 7, border: "none",
                        cursor: off ? "not-allowed" : "pointer", opacity: off ? 0.5 : 1,
                        fontFamily: "inherit", fontSize: 13, fontWeight: 600,
                        background: engine === e.value ? "var(--surface)" : "transparent",
                        color: engine === e.value ? "var(--ink)" : "var(--ink-3)",
                        boxShadow: engine === e.value ? "0 1px 2px rgba(60,40,25,.1)" : "none",
                      }}
                    >
                      {t(e.labelKey)}
                    </button>
                    );
                  })}
                </div>
                <div style={{ fontSize: 11.5, color: "var(--ink-3)", marginTop: 6 }}>
                  {t("engineHint")}
                </div>
              </Row>

              <Row label={t("generationModel")}>
                <select
                  aria-label={t("generationModel")}
                  style={selectStyle}
                  value={chatSelectValue}
                  onChange={(ev) => pick(setChatModel)(ev.target.value)}
                >
                  {!chatModelSelectable && chatModel && (
                    <option value={chatModel}>{chatModel}</option>
                  )}
                  {chatOptions.map((e) => (
                    <option key={e.id} value={e.id}>
                      {e.display_name && e.display_name !== e.id ? `${e.display_name} (${e.id})` : e.id}
                      {e.recommended ? ` — ${t("badgeRecommended")}` : ""}
                      {!e.recommended && e.qualified ? ` — ${t("badgeQualified")}` : ""}
                    </option>
                  ))}
                </select>
                {selectedChat && (
                  <div style={{ marginTop: 6 }}>{entryBadges(selectedChat, false)}</div>
                )}
                {selectedChat?.recommended && catalog?.recommended_chat_reason && (
                  <div style={{ fontSize: 11.5, color: "var(--ink-3)", marginTop: 6 }}>
                    {catalog.recommended_chat_reason}
                  </div>
                )}
              </Row>
              {chatModelUnusable && (
                <Notice tone="warn" icon="alert" testId="chat-model-unusable">
                  {chatUnusableMessage()}
                </Notice>
              )}
              {chatOptions.length === 0 && (
                <Notice tone="warn" icon="alert" testId="no-compatible-chat-model">
                  {t("noCompatibleChatModel")}
                </Notice>
              )}
              {isNew && catalog?.default_chat_status && catalog.default_chat_status !== "available" && (
                <Notice tone="warn" icon="alert" testId="default-chat-model-unavailable">
                  {t("defaultChatModelUnavailable", { model: catalog.default_chat_model || "—" })}
                </Notice>
              )}
            </>
          )}

          <Notice tone="info" icon="info">
            <strong>{t("routingLabel")} : {t("routingGateway")}</strong> — {t("routingHelp")}
          </Notice>

          {fieldsEditable && !isNew && (
            <div>
              <Button size="sm" variant="secondary" icon="refresh" disabled={testing} onClick={runTest}>
                {testing ? t("testing") : t("testConnection")}
              </Button>
              {test && (
                <div
                  role="status"
                  style={{
                    marginTop: 8, fontSize: 12.5, fontWeight: 600,
                    color: test.ok ? "var(--success)" : "var(--danger)",
                  }}
                >
                  {test.ok ? t("connectionOk") : test.message || t("testFailed")}
                </div>
              )}
            </div>
          )}
        </div>

        {/* ===================== Embeddings / indexation ===================== */}
        <div style={card}>
          <div style={sectionTitle}>{t("embeddingsSection")}</div>

          {!fieldsEditable ? (
            <ReadonlyValue
              label={t("embeddingPipeline")}
              value={
                spaceIsGoverned
                  ? config?.embedding_model
                  : t("pipelineLegacyCurrent", { model: config?.embedding_model || "—" })
              }
            />
          ) : (
            <Row label={t("embeddingPipeline")}>
              <select
                aria-label={t("embeddingPipeline")}
                style={selectStyle}
                value={pipeline}
                onChange={(ev) => pick(setPipeline)(ev.target.value)}
              >
                {pipelineOptions.map((o) => (
                  <option key={o.entry.id} value={o.entry.id}>
                    {o.legacy
                      ? t("pipelineLegacyCurrent", { model: o.entry.id })
                      : `${o.entry.id}${o.entry.dimension ? ` · ${o.entry.dimension}` : ""}` +
                        (o.entry.recommended ? ` — ${t("badgeRecommended")}` : "") +
                        (!o.entry.recommended && o.entry.qualified ? ` — ${t("badgeQualified")}` : "")}
                  </option>
                ))}
              </select>
              <div style={{ fontSize: 11.5, color: "var(--ink-3)", marginTop: 6 }}>
                {t("embeddingPipelineHint")}
              </div>
              {selectedPipeline && (
                <div style={{ display: "flex", gap: 6, marginTop: 8, flexWrap: "wrap" }}>
                  {entryBadges(selectedPipeline.entry, selectedPipeline.legacy)}
                  {!selectedPipeline.legacy && !selectedPipeline.entry.recommended && (
                    <Badge tone="warn">{t("badgeHigherLatency")}</Badge>
                  )}
                </div>
              )}
            </Row>
          )}

          {selectedPipeline && !selectedPipeline.legacy && (
            <div style={{ display: "flex", gap: 18, fontSize: 12.5, color: "var(--ink-2)" }}>
              <span>
                {t("pipelineDimension")} :{" "}
                <strong>{selectedPipeline.entry.dimension ?? "—"}</strong>
              </span>
              <span style={{ fontFamily: "ui-monospace, monospace" }}>
                {t("pipelineEffectiveModel")} : {selectedPipeline.entry.upstream_model || "—"}
              </span>
            </div>
          )}

          {selectedPipeline?.legacy && (
            <Notice tone="info" icon="info">{t("pipelineLegacyNote")}</Notice>
          )}

          {/* Réindexation : impact CHIFFRÉ, et surtout SÉPARÉ des documents en erreur.
              Les additionner donne le compteur trompeur observé sur l'espace AEC —
              « 46 documents à réindexer », dont 44 sans le moindre chunk. */}
          {canEditModels && (
            <Notice tone={pipelineChanged ? "danger" : "info"} icon="alert">
              <span data-testid="reindex-warning">
                {t("embeddingsReindexNote")}
                {pipelineChanged && config?.documents && (
                  <>
                    {" "}
                    <strong>{t("reindexImpactReady", { count: config.documents.ready })}</strong>
                    {config.documents.failed > 0 && (
                      <> {t("reindexImpactFailed", { count: config.documents.failed })}</>
                    )}{" "}
                    {t("reindexManualNote")}
                  </>
                )}
              </span>
            </Notice>
          )}
        </div>

        {err && (
          <div style={{ fontSize: 12.5, color: "var(--danger)", fontWeight: 600 }} role="alert">
            {err}
          </div>
        )}
      </div>

      <div style={{ display: "flex", justifyContent: "flex-end", gap: 10, marginTop: 24 }}>
        <Button variant="secondary" onClick={onClose}>{t("cancel")}</Button>
        {/* `!loaded` : à la CRÉATION, ne rien soumettre avant de savoir quoi persister. */}
        <Button icon="check" disabled={!name.trim() || saving || !loaded || chatBlocked} onClick={save}>
          {isNew ? t("createSpaceButton") : t("save")}
        </Button>
      </div>
    </Modal>
  );
}
