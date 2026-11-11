"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import Link from "next/link";
import {
  apiFetch,
  apiUploadForm,
  apiFetchBlobUrl,
  deleteDocument,
  getCapabilities,
  getWorkspaceQuota,
  summarizeDocumentAwait,
  summaryErrorKey,
  type DocumentSummaryResponse,
  type SummaryMode,
} from "@/lib/api";
import type { QuotaUsage } from "@/lib/quota";
import { humanAccessError } from "@/lib/apiError";
// `fileTypeLabel` reste employé pour les fichiers EN COURS D'ENVOI : ils n'ont pas
// encore de document côté serveur, donc pas de type décidé par le backend.
import { dateTime, fileSize, fileTypeLabel, formatCount, shortDate } from "@/lib/format";
import { AppShell, Topbar, useWorkspace } from "@/components/shell";
import {
  Button,
  Card,
  EmptyState,
  Field,
  Icon,
  Modal,
  Popover,
  RagSpaceRoleBadge,
  SectionLabel,
  StatusBadge,
  type DocStatus,
} from "@/components/ui";
import {
  type ApiDocument,
  type DocumentCounts,
  type DocumentListResponse,
  documentTypeLabel,
  retryActionsFor,
  toDocStatus,
  FILE_COLORS,
  humanError,
  extractionNotice,
  deleteErrorMessage,
  uploadAccept,
  visualEnrichmentNoticeKey,
  uploadFormatsLabel,
  type MsgCapability,
} from "./_lib/types";
import {
  ariaSort,
  buildDocumentsUrl,
  DEFAULT_PAGE_SIZE,
  DEFAULT_SORT,
  DEFAULT_SORT_DESC,
  FILTER_KEYS,
  nextSort,
  SORT_FIELDS,
  type FilterKey,
  type SortField,
} from "./_lib/documentsQuery";
import { DocumentsPagination } from "./_components/DocumentsPagination";
import {
  EMPTY_SELECTION,
  forgetDocument,
  hasSelection,
  isSelected,
  reconcile,
  selectAllMatching,
  selectedCount,
  toggleOne,
  togglePage,
  toRetrySelection,
  type SelectionState,
} from "./_lib/selection";
import {
  getActiveReindexJob,
  getReindexJob,
  isTerminal,
  previewFailedDocumentsRetry,
  RETRY_STRATEGIES,
  // Alias : `retryFailed` désigne déjà, dans cette page, le ré-essai des FICHIERS
  // dont l'ENVOI a échoué. Deux notions distinctes, deux noms distincts.
  retryFailed as retryFailedJobItems,
  startFailedDocumentsRetry,
  startReindex,
  type ReindexJob,
  type RetryPreview,
  type RetryStrategy,
} from "@/lib/reindex";
import { ReindexJobProgress } from "@/components/reindex/ReindexJobProgress";
import {
  buildFolderPlan,
  buildUploadForm,
  defaultSelectedFormats,
  extensionsOfFormats,
  folderInputSupported,
  isOfficeTemporaryFilename,
  summarizeFolderFormats,
  folderFormatCatalog,
  FOLDER_UPLOAD_CONCURRENCY,
  type FolderFormatSummary,
} from "./_lib/folderImport";
import {
  MAX_UPLOAD_ATTEMPTS,
  RATE_LIMIT_EXHAUSTED,
  runUploadQueue,
  type UploadAttemptResult,
} from "./_lib/uploadScheduler";
import { FolderImportModal } from "./_components/FolderImportModal";
import {
  audioExtensionsOf,
  audioFailureKey,
  audioMaxMegabytes,
  audioProcessingNotice,
  audioReadyNotice,
  audioUploadErrorKey,
  audioUploadOffered,
  uploadTargetFor,
  type AudioCapability,
} from "./_lib/audio";
import { countsSignature, nextPollDelay } from "./_lib/documentsPolling";
import layout from "./_components/DocumentsLayout.module.css";

// Le focus revient sur ce bouton à la fermeture de la modale d'import de dossier.
const FOLDER_BUTTON_ID = "documents-import-folder";

// Libellés i18n des onglets et des colonnes triables. Déclarés une seule fois : les
// onglets, l'en-tête de table et le sélecteur de tri lisent tous ces tables, donc
// aucun des trois ne peut nommer une catégorie autrement que les autres.
const FILTER_LABEL_KEYS: Record<FilterKey, string> = {
  all: "filterAll",
  ready: "filterReady",
  analyzing: "filterAnalyzing",
  queued: "filterQueued",
  error: "filterErrors",
};

const SORT_LABEL_KEYS: Record<SortField, string> = {
  filename: "colDocument",
  file_type: "colType",
  status: "colStatus",
  created_at: "colImportedOn",
};

// Gabarit de colonnes partagé par l'en-tête et les lignes : les deux doivent rester
// alignés au pixel, donc ils lisent la même constante.
const DOC_GRID = "minmax(0,1fr) 78px 150px 130px 168px 44px";

/** En-tête de colonne triable : bouton réel (donc focusable et actionnable au clavier). */
function SortHeader({
  field,
  label,
  sortField,
  sortDesc,
  onSort,
}: {
  field: SortField;
  label: string;
  sortField: SortField;
  sortDesc: boolean;
  onSort: (f: SortField) => void;
}) {
  const active = sortField === field;
  return (
    <div role="columnheader" aria-sort={ariaSort({ sortField, sortDesc }, field)}>
      <button
        type="button"
        onClick={() => onSort(field)}
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 4,
          padding: 0,
          border: "none",
          background: "transparent",
          cursor: "pointer",
          font: "inherit",
          color: "inherit",
        }}
      >
        <SectionLabel>{label}</SectionLabel>
        <Icon
          name="chevronD"
          size={12}
          style={{
            color: "var(--ink-3)",
            // Chevron toujours présent (l'en-tête reste identifiable comme
            // cliquable), retourné et opaque uniquement sur la colonne active.
            opacity: active ? 1 : 0.3,
            transform: active && !sortDesc ? "rotate(180deg)" : undefined,
          }}
        />
      </button>
    </div>
  );
}

// Aligné sur le backend : DOCUMENT_MAX_UPLOAD_SIZE_MB (défaut 50 Mo).
// Calcul BINAIRE (Mio) comme tout le reste de la chaîne — `humanBytes` et les bornes
// backend sont en base 1024 ; l'UI, elle, dit « Mo » (convention produit).
// 50 * 1024 * 1024 = 52 428 800 octets.
const MAX_SIZE_MB = 50;
const MAX_SIZE = MAX_SIZE_MB * 1024 * 1024;
// Formats réellement supportés par le pipeline d'extraction (source unique :
// _lib/types). Images MVP (JPG/PNG/TIFF/WEBP) OCRisées automatiquement côté backend.
// NB: .doc/.xls/.xlsx/.ppt/.pptx ne sont PAS supportés (rejetés côté backend).

export default function DocumentsPage() {
  return (
    <AppShell>
      <DocumentsContent />
    </AppShell>
  );
}

/** Suivi local d'un fichier pendant la phase d'envoi réseau. */
type UploadItem = {
  id: string;
  name: string;
  size: number;
  /**
   * `skipped` : écarté AVANT tout appel réseau (verrou Office `~$`). Distinct d'`error`
   * à dessein — ce n'est pas un échec, il n'y a rien à corriger ni à ré-essayer. Le
   * confondre avec `error` le ferait afficher en rouge et re-tenter par `retryFailed`.
   *
   * `queued` : accepté, pas encore parti. La cadence de l'ordonnanceur rend cet état
   * visible pour la première fois — auparavant tout partait immédiatement.
   *
   * `waiting_retry` : le serveur a répondu 429 et a donné une échéance. Ni succès ni
   * échec — c'est précisément la confusion des deux qui produisait des centaines de
   * lignes rouges pour une saturation de quelques secondes.
   *
   * `cancelled` : interrompu par l'utilisateur ou par le démontage de la page. Pas
   * un échec : il n'y a rien à corriger.
   */
  status: "queued" | "uploading" | "waiting_retry" | "done" | "error" | "cancelled" | "skipped";
  percent: number;
  error?: string;
  /** `waiting_retry` : instant (epoch ms) de la prochaine tentative, pour le décompte. */
  retryAt?: number;
  /** Tentatives déjà émises. Affiché en diagnostic, et vérifié par les tests. */
  attempts?: number;
  // Import de dossier : conservés pour transmettre la provenance à l'API et
  // permettre un ré-essai ciblé des échecs sans re-uploader les réussis.
  file?: File;
  relativePath?: string;
  sourceFolder?: string;
};

/**
 * Ce qui doit RESTER visible dans la zone d'upload après un lot : ce qui appelle une
 * action ou une explication. Les réussis, eux, sont déjà dans la liste des documents.
 */
/**
 * Éléments conservés dans la zone d'envoi après un lot : ce qu'il reste à LIRE.
 *
 * `waiting_retry` en est volontairement absent : un lot terminé n'a plus d'élément
 * en attente, et l'y admettre laisserait un décompte figé tourner à l'écran après la
 * fin de l'import. `cancelled` non plus — une annulation n'appelle aucune action.
 */
function isPersistentUpload(u: UploadItem): boolean {
  return u.status === "error" || u.status === "skipped";
}

// webkitdirectory / directory ne sont pas dans les typings React standard :
// cast LOCAL et borné (jamais de désactivation globale du typage).
const DIRECTORY_INPUT_ATTRS = {
  webkitdirectory: "",
  directory: "",
} as unknown as React.InputHTMLAttributes<HTMLInputElement>;

function DocumentsContent() {
  const t = useTranslations("documents");
  // Gating par RÔLE EFFECTIF de l'espace courant (décidé par le backend, flag-aware).
  // Flags OFF => identique au rôle workspace ; flags ON => précision par espace.
  const {
    ragSpace, ragSpaceId, ragSpaceAccess, canWriteCurrent, canManageCurrent, refreshStats, workspaceId,
    ragSpaces, setRagSpaceId,
  } = useWorkspace();
  const canImport = canWriteCurrent;
  // Seul un espace de cette liste peut être (re)sélectionné.
  const accessibleRagSpaceIds = useMemo(() => (ragSpaces ?? []).map((r) => r.id), [ragSpaces]);

  // Quota documentaire du workspace (indicateur + désactivation UI). Lecture
  // réservée owner/admin côté backend : un membre reçoit 403 => quota null, aucun
  // indicateur (l'enforcement backend reste, lui, toujours actif). Jamais bloquant.
  const [quota, setQuota] = useState<QuotaUsage | null>(null);
  const reloadQuota = useCallback(() => {
    if (workspaceId == null) { setQuota(null); return; }
    getWorkspaceQuota(workspaceId)
      .then(setQuota)
      .catch(() => setQuota(null));
  }, [workspaceId]);
  useEffect(() => { reloadQuota(); }, [reloadQuota]);
  const quotaBlocksUpload = !!quota && !quota.can_upload;
  const quotaWarn =
    !!quota && (quota.count_usage_percent >= 80 || quota.size_usage_percent >= 80);
  const quotaCritical =
    !!quota && (quota.is_exceeded || quota.count_usage_percent >= 95 || quota.size_usage_percent >= 95);
  // --- État de la LISTE ---------------------------------------------------
  // `docs` est une PAGE, jamais le corpus. Total et compteurs viennent du serveur :
  // les déduire de `docs.length` était le défaut corrigé par ce lot (l'écran
  // annonçait « 200 » là où l'espace en contenait 1 094).
  const [docs, setDocs] = useState<ApiDocument[]>([]);
  const [total, setTotal] = useState(0);
  const [counts, setCounts] = useState<DocumentCounts>({
    all: 0, ready: 0, analyzing: 0, queued: 0, error: 0,
  });
  const [limit, setLimit] = useState(DEFAULT_PAGE_SIZE);
  const [offset, setOffset] = useState(0);
  const [sortField, setSortField] = useState<SortField>(DEFAULT_SORT);
  const [sortDesc, setSortDesc] = useState(DEFAULT_SORT_DESC);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [filter, setFilter] = useState<FilterKey>("all");
  // `query` est la saisie brute ; `search` est ce qui part réellement sur le
  // réseau, après temporisation (une requête par frappe serait gaspillée).
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [drag, setDrag] = useState(false);
  // --- Reprise en masse des documents en erreur ---------------------------
  // La sélection ne référence QUE des identifiants persistants : une ligne cochée
  // le reste au changement de page ou de tri, et rien ne dépend d'une position.
  const [selection, setSelection] = useState<SelectionState>(EMPTY_SELECTION);
  const [confirmRetry, setConfirmRetry] = useState(false);
  const [startingRetry, setStartingRetry] = useState(false);
  // `auto` par défaut : c'est la seule méthode qui tient compte de la cause de
  // l'échec. Les autres restent accessibles — on n'enferme pas l'administrateur.
  const [retryStrategy, setRetryStrategy] = useState<RetryStrategy>("auto");
  const [retryPreview, setRetryPreview] = useState<RetryPreview | null>(null);
  // Job durable en cours ou terminé pour CET espace (globale ou reprise ciblée :
  // un seul à la fois, l'exclusivité étant portée par le RagSpace).
  const [job, setJob] = useState<ReindexJob | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploads, setUploads] = useState<UploadItem[]>([]);
  const [embeddingsProvider, setEmbeddingsProvider] = useState<string>("");
  const [confirmReindex, setConfirmReindex] = useState(false);
  const [reindexing, setReindexing] = useState(false);
  const [reindexMsg, setReindexMsg] = useState<string | null>(null);
  const [summaryDoc, setSummaryDoc] = useState<ApiDocument | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<ApiDocument | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteErr, setDeleteErr] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const dirRef = useRef<HTMLInputElement>(null);
  const uploadSeq = useRef(0);
  /** Contrôleur du lot d'envoi en cours. `null` hors import. */
  const uploadAbort = useRef<AbortController | null>(null);

  // Démontage : on annule le lot en cours. Sans cela, une attente de ré-essai
  // (jusqu'à deux minutes) survivrait à la navigation, enverrait des requêtes
  // fantômes et écrirait dans un composant démonté.
  useEffect(() => {
    return () => {
      uploadAbort.current?.abort();
      uploadAbort.current = null;
    };
  }, []);
  // Import de dossier : les fichiers BRUTS sont conservés tels quels ; le plan
  // est RECALCULÉ à chaque changement de sélection de formats. Rien n'est filtré
  // définitivement avant que l'utilisateur ait choisi.
  const [folderFiles, setFolderFiles] = useState<File[]>([]);
  const [folderFormats, setFolderFormats] = useState<FolderFormatSummary[]>([]);
  const [selectedFormats, setSelectedFormats] = useState<Set<string>>(new Set());
  const [folderSupported, setFolderSupported] = useState(false);

  // Détection du support natif de la sélection de dossier (webkitdirectory).
  useEffect(() => {
    setFolderSupported(folderInputSupported());
  }, []);

  // Provider d'embeddings RÉEL de l'espace actif (indexation) — sert à expliquer
  // correctement une erreur d'indisponibilité, indépendamment du provider de chat.
  // Priorité au réglage de l'espace ; repli sur le réglage GLOBAL (capabilities)
  // pour les très anciens espaces sans champ embeddings.
  useEffect(() => {
    const spaceProvider = (ragSpace?.embedding_provider as string) || "";
    if (spaceProvider) {
      setEmbeddingsProvider(spaceProvider);
      return;
    }
    let alive = true;
    getCapabilities()
      .then((c) => {
        if (alive) setEmbeddingsProvider(c.embeddings?.provider || "");
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [ragSpace]);

  // Capacité `.msg` : le format n'est proposé au dépôt que si CET environnement
  // l'annonce disponible (flag serveur ouvert ET parseur déployé). Indépendant du
  // RagSpace, donc récupéré une seule fois.
  const [msgCapability, setMsgCapability] = useState<MsgCapability | null>(null);
  // Formats sous flag annoncés par le serveur (ex. `xml`) : même source, même moment.
  const [serverFormats, setServerFormats] = useState<string[] | null>(null);
  // Sous-formats de la famille `image` : le backend en est la seule autorité.
  const [serverImageFormats, setServerImageFormats] = useState<string[] | null>(null);
  // Dépôt audio : capacité GLOBALE ; l'admission par espace vient du
  // RagSpace courant (`audio_upload_enabled`).
  const [audioCapability, setAudioCapability] = useState<AudioCapability | null>(null);
  useEffect(() => {
    let alive = true;
    getCapabilities()
      .then((c) => {
        if (alive) {
          setMsgCapability(c.ingest?.msg ?? null);
          setServerFormats(c.ingest?.formats ?? null);
          setServerImageFormats(c.ingest?.image_formats ?? null);
          setAudioCapability(c.ingest?.audio ?? null);
        }
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);
  const spaceAudioEnabled = !!ragSpace?.audio_upload_enabled;
  // L'audio n'est proposé (accept + aide) que si l'environnement l'ouvre ET l'espace l'autorise.
  const audioOffered = audioUploadOffered(audioCapability, spaceAudioEnabled);
  const offeredAudioExtensions = audioOffered ? audioExtensionsOf(audioCapability) : null;
  const accept = uploadAccept(msgCapability, serverFormats, serverImageFormats, offeredAudioExtensions);
  // SOURCE UNIQUE des trois chemins d'import : l'`accept` effectif, capacités serveur
  // comprises. L'import de dossier ne décide plus depuis une constante de module.
  const folderCatalog = useMemo(() => folderFormatCatalog(accept), [accept]);

  const folderPlan = useMemo(() => {
    if (folderFiles.length === 0) return null;
    return buildFolderPlan(folderFiles, {
      maxSizeBytes: MAX_SIZE,
      // Audio : plafond dédié (600 Mo décimaux) ; tout le reste garde MAX_SIZE.
      maxSizeBytesOf: (name: string) => {
        const target = uploadTargetFor(name, {
          audio: audioCapability, spaceAudioEnabled, standardMaxBytes: MAX_SIZE,
        });
        return target.kind === "audio" ? target.maxBytes : MAX_SIZE;
      },
      selectedExtensions: extensionsOfFormats(selectedFormats, folderCatalog),
      catalog: folderCatalog,
    });
  }, [folderFiles, selectedFormats, folderCatalog, audioCapability, spaceAudioEnabled]);
  const baseFormatsLabel = uploadFormatsLabel(msgCapability, serverFormats, serverImageFormats);
  // Mention audio : formats, plafond (Mo décimaux) et transcription horodatée. Jamais
  // de résumé ni de noms d'intervenants promis.
  const audioHelp = audioOffered
    ? t("audioUploadHelp", {
        formats: (offeredAudioExtensions ?? []).map((e) => e.toUpperCase()).join(", "),
        mb: audioMaxMegabytes(audioCapability),
      })
    : "";

  // Numéro de la dernière requête de liste ÉMISE. Une réponse dont le numéro n'est
  // plus le dernier est jetée : sans cela, une recherche lente lancée avant une
  // recherche rapide écrasait le résultat le plus récent en arrivant après lui.
  const loadSeq = useRef(0);

  const load = useCallback(
    async (signal?: AbortSignal) => {
      if (!ragSpaceId) {
        setDocs([]);
        setTotal(0);
        setCounts({ all: 0, ready: 0, analyzing: 0, queued: 0, error: 0 });
        return;
      }
      const seq = ++loadSeq.current;
      const res = await apiFetch<DocumentListResponse>(
        buildDocumentsUrl({ ragSpaceId, limit, offset, filter, search, sortField, sortDesc }),
        { signal }
      );
      if (seq !== loadSeq.current) return undefined; // réponse obsolète : on n'y touche pas
      const items = res.items || [];
      const nextTotal = res.total ?? 0;
      setDocs(items);
      setTotal(nextTotal);
      if (res.counts) setCounts(res.counts);
      // Page devenue vide alors que le corpus ne l'est pas (suppressions ici ou
      // ailleurs) : on recule d'une page plutôt que d'afficher un tableau vide au
      // milieu d'une liste qui, elle, contient toujours des documents.
      if (items.length === 0 && offset > 0 && nextTotal > 0) {
        setOffset(Math.max(0, Math.floor((nextTotal - 1) / limit) * limit));
      }
      return res.counts ?? null;
    },
    [ragSpaceId, limit, offset, filter, search, sortField, sortDesc]
  );

  // Recherche temporisée : la saisie ne déclenche une requête qu'après une pause.
  useEffect(() => {
    const id = setTimeout(() => setSearch(query.trim()), 350);
    return () => clearTimeout(id);
  }, [query]);

  // Tout changement de critère ramène à la PREMIÈRE page : rester à l'offset 200
  // d'un résultat qui n'en compte plus que 3 afficherait une page vide.
  const firstLoad = useRef(true);
  useEffect(() => {
    if (firstLoad.current) {
      firstLoad.current = false;
      return;
    }
    setOffset(0);
  }, [ragSpaceId, filter, search, sortField, sortDesc, limit]);

  // Chargement à chaque changement d'espace, de page, de tri, de filtre ou de
  // recherche (toutes ces dépendances vivent dans `load`).
  useEffect(() => {
    const ac = new AbortController();
    setErr(null);
    setLoading(true);
    load(ac.signal)
      .catch((e) => {
        if (e?.name !== "AbortError") setErr(humanAccessError(e) || e?.message || t("loadError"));
      })
      .finally(() => setLoading(false));
    return () => ac.abort();
  }, [load]);

  // Suivi de l'ingestion : cadence ADAPTATIVE (contrat dans `_lib/documentsPolling.ts`).
  // Les compteurs portent sur tout l'espace : une ingestion en cours hors de la page
  // courante maintient bien le suivi actif. Avant : liste + stats toutes les 2,5 s, onglet
  // caché compris — 48 req/min par onglet, 429 dès trois onglets (plafond 120/min/user).
  const hasInFlight = counts.queued + counts.analyzing > 0;
  const [pageHidden, setPageHidden] = useState(
    () => typeof document !== "undefined" && document.visibilityState === "hidden"
  );
  useEffect(() => {
    const onVisibility = () => setPageHidden(document.visibilityState === "hidden");
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, []);
  // Retour au premier plan : UN rafraîchissement immédiat, puis la cadence reprend
  // (l'effet de suivi ci-dessous se réarme de lui-même si une ingestion est en cours).
  const wasHidden = useRef(pageHidden);
  useEffect(() => {
    if (wasHidden.current && !pageHidden) {
      load().then(() => refreshStats()).catch(() => {});
    }
    wasHidden.current = pageHidden;
  }, [pageHidden, load, refreshStats]);

  useEffect(() => {
    if (!hasInFlight || pageHidden) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let previousMs: number | null = null;
    let lastSignature = "";
    const schedule = (delay: number | null) => {
      if (cancelled || delay === null) return;
      previousMs = delay;
      timer = setTimeout(tick, delay);
    };
    const tick = async () => {
      try {
        const next = await load();
        if (cancelled) return;
        const signature = countsSignature(next);
        const changed = signature !== lastSignature;
        // Les stats ne bougent que si les compteurs bougent : une seule requête par
        // tick tant que l'espace attend.
        if (changed) {
          lastSignature = signature;
          refreshStats();
        }
        const inFlight = next ? next.queued + next.analyzing > 0 : true;
        schedule(nextPollDelay({ inFlight, hidden: false, previousMs, changed }));
      } catch (e: any) {
        if (cancelled) return;
        // 429 : on attend l'échéance donnée par le serveur, jamais moins.
        schedule(
          nextPollDelay({
            inFlight: true,
            hidden: false,
            previousMs,
            changed: false,
            rateLimited: e?.status === 429,
            retryAfterMs: e?.retryAfterMs ?? null,
          })
        );
      }
    };
    schedule(nextPollDelay({ inFlight: true, hidden: false, previousMs: null, changed: true }));
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [hasInFlight, pageHidden, load, refreshStats]);

  // La page rendue EST le résultat du serveur : plus aucun filtrage local, qui ne
  // pourrait de toute façon porter que sur la page chargée.
  const shown = docs;

  function onSort(field: SortField) {
    const next = nextSort({ sortField, sortDesc }, field);
    setSortField(next.sortField);
    setSortDesc(next.sortDesc);
  }

  // --- Sélection ----------------------------------------------------------
  // Cochage réservé à l'onglet Erreurs : c'est le seul périmètre que la reprise
  // sait traiter, et le backend refuserait tout le reste. Proposer des cases
  // ailleurs promettrait une action qui n'existe pas.
  const selectionEnabled = filter === "error" && canManageCurrent;
  const selectionSize = selectedCount(selection, total);
  const pageIds = shown.map((d) => d.id);
  const allPageSelected = pageIds.length > 0 && pageIds.every((id) => isSelected(selection, id));
  const canExtendToAll =
    selection.mode === "explicit" && allPageSelected && total > pageIds.length;

  // Un changement de périmètre invalide la sélection : « les 184 en erreur » ne
  // veut plus rien dire une fois le filtre ou la recherche changés. La pagination
  // et le tri, eux, ne changent PAS le périmètre — la sélection y survit.
  useEffect(() => {
    setSelection(EMPTY_SELECTION);
  }, [ragSpaceId, filter, search]);

  // --- Job durable (réindexation globale OU reprise ciblée) ----------------
  // Un seul job actif par espace : un seul état ici, quel que soit son `kind`.
  const jobActive = !!job && !isTerminal(job.status);

  // Reprise du suivi après un rechargement de page : le job vit côté serveur, pas
  // dans cet onglet. C'est ce qui permet de fermer la page sans rien perdre.
  useEffect(() => {
    if (!ragSpaceId || !canManageCurrent) {
      setJob(null);
      return;
    }
    let alive = true;
    getActiveReindexJob(ragSpaceId)
      .then(({ active, job: j }) => {
        if (alive && active && j) setJob(j);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [ragSpaceId, canManageCurrent]);

  // Polling du job tant qu'il n'est pas terminal. À la transition vers un état
  // terminal, la liste est rechargée : les documents redevenus prêts quittent
  // l'onglet Erreurs et les compteurs suivent.
  useEffect(() => {
    // Onglet caché : pas de suivi du job (le retour au premier plan relit la liste).
    if (!job || isTerminal(job.status) || pageHidden) return;
    const id = setInterval(async () => {
      try {
        const fresh = await getReindexJob(job.job_id);
        setJob(fresh);
        if (isTerminal(fresh.status)) {
          await load().catch(() => {});
          refreshStats();
        }
      } catch {
        /* erreur transitoire : on retentera au tick suivant */
      }
    }, 3000);
    return () => clearInterval(id);
  }, [job, load, refreshStats, pageHidden]);

  // Après un job, une sélection explicite peut désigner des documents qui ne sont
  // plus en erreur : on la réconcilie avec ce que la liste montre réellement.
  useEffect(() => {
    if (!job || !isTerminal(job.status) || selection.mode !== "explicit") return;
    setSelection((prev) => reconcile(prev, new Set(docs.map((d) => d.id))));
  }, [job, docs, selection.mode]);

  // Prévisualisation : « voici ce qui se passerait ». Rejouée à chaque changement
  // de méthode, tant que la modale est ouverte — annoncer une répartition puis en
  // exécuter une autre serait pire que ne rien annoncer.
  useEffect(() => {
    if (!confirmRetry || !ragSpaceId) return;
    const payload = toRetrySelection(selection, search);
    if (!payload) return;
    let alive = true;
    setRetryPreview(null);
    previewFailedDocumentsRetry(ragSpaceId, payload, retryStrategy)
      .then((p) => {
        if (alive) setRetryPreview(p);
      })
      .catch(() => {
        // La prévision est un confort : son échec ne doit pas empêcher de lancer.
        if (alive) setRetryPreview(null);
      });
    return () => {
      alive = false;
    };
    // `selection`/`search` sont figés pendant que la modale est ouverte.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [confirmRetry, ragSpaceId, retryStrategy]);

  /** Lance la reprise en masse des documents sélectionnés. */
  async function runRetrySelection() {
    // Anti double-clic : un second POST créerait au mieux un 409, au pire de la
    // confusion. On verrouille le temps de la requête.
    if (!ragSpaceId || startingRetry) return;
    const payload = toRetrySelection(selection, search);
    if (!payload) return;
    setStartingRetry(true);
    setErr(null);
    try {
      const r = await startFailedDocumentsRetry(ragSpaceId, payload, retryStrategy);
      // 202 comme 409 « déjà en cours » ramènent un job à SUIVRE, jamais une erreur.
      setJob(r.job ?? (await getReindexJob(r.job_id)));
      setSelection(EMPTY_SELECTION);
      setConfirmRetry(false);
      setRetryPreview(null);
      if (r.already_running) setReindexMsg(t("retryAlreadyRunning"));
      else setReindexMsg(t("retryStarted", { count: r.total_documents ?? 0 }));
      await load().catch(() => {});
    } catch (e: any) {
      const msg = e?.message || "";
      setErr(
        msg.includes("no_eligible_documents") || msg.includes("no_retryable_documents")
          ? t("retryNoEligible")
          : msg.includes("selection_too_large")
            ? t("retryTooLarge")
            : humanAccessError(e) || msg || t("retryFailed")
      );
      setConfirmRetry(false);
    } finally {
      setStartingRetry(false);
    }
  }

  /** Relance UNIQUEMENT les items en échec du job courant (nouveau job lié).
   *
   * `strategy` omis = rejouer avec la MÊME méthode ; `"auto"` = recalculer sur les
   * causes d'échec que la tentative précédente vient d'écrire. Les deux gestes sont
   * légitimes, l'utilisateur choisit. */
  async function runRetryFailedItems(strategy?: RetryStrategy) {
    if (!job) return;
    try {
      const r = await retryFailedJobItems(job.job_id, strategy);
      setJob(r.job ?? (await getReindexJob(r.job_id)));
    } catch (e: any) {
      const msg = e?.message || "";
      setErr(
        msg.includes("no_retryable_documents")
          ? t("retryNoEligible")
          : humanAccessError(e) || msg || t("retryFailed")
      );
    }
  }

  // Envoi d'UN fichier (réutilisé par l'import unitaire ET l'import de dossier).
  // Chaque fichier => un document distinct via l'endpoint existant (?async=true).
  //
  // Cette fonction ne DÉCIDE plus du sort de l'élément : elle rend un verdict typé et
  // l'ordonnanceur tranche. Auparavant elle absorbait l'exception et écrivait
  // `status:"error"` — l'information « c'était un 429, le serveur demande 21 s »
  // mourait ici, et le pool ne pouvait pas l'apprendre même s'il avait su quoi en
  // faire. C'est ce point exact qui transformait une saturation en échecs définitifs.
  async function uploadOneFile(
    item: UploadItem,
    spaceId: string,
    signal?: AbortSignal,
  ): Promise<UploadAttemptResult> {
    const patch = (p: Partial<UploadItem>) =>
      setUploads((prev) => prev.map((u) => (u.id === item.id ? { ...u, ...p } : u)));
    // Audio : chemin dédié et plafond propre ; tout autre fichier
    // garde exactement le chemin générique et la limite historique.
    const target = uploadTargetFor(item.name, {
      audio: audioCapability,
      spaceAudioEnabled,
      standardMaxBytes: MAX_SIZE,
    });
    if (target.kind === "audio_not_enabled_for_space") {
      return { kind: "terminal_error", error: new Error(t("audioUploadNotEnabledForSpace")) };
    }
    if (item.size > target.maxBytes) {
      return {
        kind: "terminal_error",
        error: new Error(
          target.kind === "audio"
            ? t("audioExceedsLimit", { mb: audioMaxMegabytes(audioCapability) })
            : t("exceedsLimit", { mb: MAX_SIZE_MB })
        ),
      };
    }
    const file = item.file;
    if (!file) {
      return { kind: "terminal_error", error: new Error(t("fileUnavailable")) };
    }
    // Provenance d'import de dossier (métadonnée ; validée/normalisée côté backend).
    const form = buildUploadForm(file, {
      ragSpaceId: spaceId,
      relativePath: item.relativePath,
      sourceFolder: item.sourceFolder,
    });
    try {
      await apiUploadForm(target.url, form, {
        onProgress: (pr) => patch({ percent: pr.percent }),
        signal,
      });
      return { kind: "success" };
    } catch (e: any) {
      // Annulation explicite : ni succès ni échec, rien à corriger.
      if (e?.name === "AbortError") return { kind: "cancelled" };
      // 429 : le SEUL statut rejouable de ce lot. Le serveur dit que la requête
      // n'avait rien de fautif et fournit lui-même l'échéance.
      if (e?.status === 429) {
        return { kind: "rate_limited", retryAfterMs: e?.retryAfterMs ?? null };
      }
      return { kind: "terminal_error", error: e };
    }
  }

  /** Message utilisateur d'un échec définitif d'envoi. */
  function uploadErrorMessage(error: unknown, reason?: string): string {
    if (reason === RATE_LIMIT_EXHAUSTED) return t("uploadRateLimited");
    const e = error as any;
    // Dépassement de quota workspace (409 structuré) : message dédié.
    const code = e?.body?.code as string | undefined;
    if (e?.status === 409 && (code === "workspace_file_count_quota_exceeded" ||
        code === "workspace_storage_quota_exceeded" || code === "workspace_quota_exceeded")) {
      return t("quotaUploadRejected");
    }
    // Refus d'upload audio structurés (413/415/400) : message dédié.
    const audioKey = audioUploadErrorKey(e?.body?.detail);
    if (audioKey) {
      const maxBytes = Number(e?.body?.max_bytes) || audioCapability?.max_upload_bytes || 0;
      return t(audioKey, { mb: audioMaxMegabytes({ max_upload_bytes: maxBytes }) });
    }
    return humanError(e?.message, embeddingsProvider) || t("importFailed");
  }

  // Orchestration commune : envoie `items` avec une concurrence BORNÉE. On capture
  // le RagSpace au démarrage : un changement d'espace pendant un lot n'affecte PAS
  // les envois en cours (le lot reste rattaché à l'espace où il a commencé).
  async function runUploadItems(items: UploadItem[], concurrency: number) {
    if (!ragSpaceId || !canImport || items.length === 0) return;
    const spaceId = ragSpaceId;
    setErr(null);
    setUploading(true);
    // On repart d'une zone propre, en conservant ce qui reste à lire : les erreurs
    // précédentes, et les fichiers écartés sans envoi (verrous Office).
    setUploads((prev) => [...prev.filter(isPersistentUpload), ...items]);
    // Un contrôleur par lot, conservé en ref : c'est lui qu'annulent le bouton
    // « Annuler » et le démontage de la page. Sans lui, une attente de ré-essai
    // survivrait à la navigation et écrirait dans un composant mort.
    const controller = new AbortController();
    uploadAbort.current = controller;
    const patch = (id: string, p: Partial<UploadItem>) =>
      setUploads((prev) => prev.map((u) => (u.id === id ? { ...u, ...p } : u)));
    try {
      await runUploadQueue(
        items,
        (item) => uploadOneFile(item, spaceId, controller.signal),
        {
          concurrency,
          signal: controller.signal,
          hooks: {
            onAttemptStart: (item, attempt) =>
              patch(item.id, { status: "uploading", percent: 0, attempts: attempt, retryAt: undefined }),
            onWaitingRetry: (item, waitMs, attempt) =>
              patch(item.id, {
                status: "waiting_retry",
                attempts: attempt,
                retryAt: Date.now() + waitMs,
              }),
            onSettled: (item, outcome, info) => {
              if (outcome === "done") {
                patch(item.id, { status: "done", percent: 100, attempts: info.attempts, retryAt: undefined });
              } else if (outcome === "cancelled") {
                patch(item.id, { status: "cancelled", attempts: info.attempts, retryAt: undefined });
              } else {
                patch(item.id, {
                  status: "error",
                  attempts: info.attempts,
                  retryAt: undefined,
                  error: uploadErrorMessage(info.error, info.reason),
                });
              }
            },
          },
        },
      );
    } finally {
      if (uploadAbort.current === controller) uploadAbort.current = null;
      // Toujours rafraîchir la liste + les stats (l'indexation est suivie par polling).
      await load().catch(() => {});
      refreshStats();
      reloadQuota();
      setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
      if (dirRef.current) dirRef.current.value = "";
      // Ne garder que ce qui reste à lire : échecs et fichiers écartés (les réussis
      // sont désormais dans la liste des documents).
      setUploads((prev) => prev.filter(isPersistentUpload));
    }
  }

  // Import unitaire (drag-drop / sélection de fichiers) — séquentiel (concurrence 1).
  //
  // Les verrous Office (`~$rapport.docx`) sont écartés ICI, avant toute construction de
  // FormData : ce chemin n'a pas de modale de récapitulatif, donc pas d'autre endroit où
  // les intercepter. Ils apparaissent dans la zone d'upload en `skipped`, pour que
  // l'utilisateur qui vient d'en déposer un comprenne pourquoi il ne se passe rien —
  // les ignorer en silence serait pire qu'un message.
  async function uploadFiles(files: FileList | File[]) {
    if (!ragSpaceId || !canImport) return;
    // Quota atteint (connu côté owner/admin) : on évite un aller-retour voué au 409.
    // L'enforcement backend reste l'autorité pour les rôles sans lecture du quota.
    if (quotaBlocksUpload) { setErr(t("quotaReachedUpload")); return; }
    const list = Array.from(files);
    if (list.length === 0) return;

    const officeTemp = list.filter((f) => isOfficeTemporaryFilename(f.name));
    const importable = list.filter((f) => !isOfficeTemporaryFilename(f.name));

    if (officeTemp.length > 0) {
      const skippedItems: UploadItem[] = officeTemp.map((f) => ({
        id: `up-${uploadSeq.current++}`,
        name: f.name,
        size: f.size,
        status: "skipped",
        percent: 0,
        error: t("officeTempMessage"),
      }));
      setUploads((prev) => [...prev, ...skippedItems]);
    }

    // Aucun fichier importable : on s'arrête là. AUCUN appel réseau.
    if (importable.length === 0) return;

    const items: UploadItem[] = importable.map((f) => ({
      id: `up-${uploadSeq.current++}`,
      name: f.name,
      size: f.size,
      status: "queued",
      percent: 0,
      file: f,
    }));
    await runUploadItems(items, 1);
  }

  // Sélection d'un dossier -> analyse SANS filtrer ni envoyer. La présélection est
  // RECONSTRUITE à chaque sélection : jamais de report d'un import précédent.
  function pickFolder(files: FileList | null) {
    if (!files || files.length === 0) return;
    const list = Array.from(files);
    const formats = summarizeFolderFormats(list, folderCatalog);
    setFolderFiles(list);
    setFolderFormats(formats);
    setSelectedFormats(defaultSelectedFormats(formats)); // tous cochés par défaut
    if (dirRef.current) dirRef.current.value = "";
  }

  function toggleFolderFormat(key: string) {
    setSelectedFormats((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  /** Ferme la modale et purge l'état d'import ; le focus revient au déclencheur. */
  function closeFolderModal() {
    setFolderFiles([]);
    setFolderFormats([]);
    setSelectedFormats(new Set());
    if (dirRef.current) dirRef.current.value = "";
    // Button ne relaie pas les refs : on repasse par l'id du déclencheur.
    document.getElementById(FOLDER_BUTTON_ID)?.focus();
  }

  // Confirmation -> envoi des SEULS fichiers retenus (concurrence bornée).
  async function confirmFolderImport() {
    const plan = folderPlan;
    if (!plan) return;
    closeFolderModal(); // ferme la modale et purge l'état AVANT l'envoi
    const items: UploadItem[] = plan.compatible.map((p) => ({
      id: `up-${uploadSeq.current++}`,
      name: p.file.name,
      size: p.file.size,
      status: "queued",
      percent: 0,
      file: p.file,
      relativePath: p.relativePath,
      sourceFolder: p.sourceFolder,
    }));
    await runUploadItems(items, FOLDER_UPLOAD_CONCURRENCY);
  }

  // Ré-essai MANUEL des seuls fichiers en échec DÉFINITIF (les réussis ne sont PAS
  // re-uploadés). Un élément `waiting_retry` est déjà pris en charge par
  // l'ordonnanceur : l'inclure ici le soumettrait deux fois.
  async function retryFailed() {
    const failed = uploads.filter((u) => u.status === "error" && u.file);
    if (failed.length === 0) return;
    const retryIds = new Set(failed.map((u) => u.id));
    setUploads((prev) => prev.filter((u) => !retryIds.has(u.id)));
    const items: UploadItem[] = failed.map((u) => ({
      ...u,
      id: `up-${uploadSeq.current++}`,
      status: "queued",
      percent: 0,
      error: undefined,
      retryAt: undefined,
      attempts: 0,
    }));
    await runUploadItems(items, FOLDER_UPLOAD_CONCURRENCY);
  }

  async function onReindex(id: string, strategy: RetryStrategy = "standard") {
    try {
      // Contrat unique `{"strategy": …}` : le `?ocr=true` historique reste accepté
      // côté serveur pour les appels existants, mais l'écran ne l'émet plus.
      await apiFetch(`/api/v1/documents/${id}/reindex/`, {
        method: "POST",
        body: JSON.stringify({ strategy }),
      });
      await load();
      refreshStats();
    } catch (e: any) {
      const msg = e?.message || "";
      setErr(
        msg.includes("reindex_already_in_progress")
          ? t("reindexAlreadyInProgress")
          : msg.includes("strategy_not_supported") || msg.includes("document_not_eligible")
            ? t("retryNotPossible")
            : humanAccessError(e) || msg || t("reindexRetryFailed")
      );
    }
  }

  // Suppression définitive : confirmée via modale (doDelete), pas de double-submit.
  async function doDelete() {
    const doc = confirmDelete;
    if (!doc || deleting) return;
    setDeleting(true);
    setDeleteErr(null);
    try {
      await deleteDocument(doc.id);
      setConfirmDelete(null);
      // Rechargement serveur plutôt que retrait local : total, compteurs et plage
      // affichée doivent bouger avec la suppression, pas seulement la ligne.
      await load().catch(() => {});
      refreshStats();
      reloadQuota();
    } catch (e: any) {
      setDeleteErr(humanAccessError(e) || deleteErrorMessage(e?.message));
    } finally {
      setDeleting(false);
    }
  }

  // Réindexation globale de l'espace (ex. après changement d'embeddings).
  //
  // Depuis le passage aux jobs durables, le POST répond **202 avec un job_id** et
  // le travail se suit par polling. Cet écran lisait encore l'ancien contrat
  // (`{queued, total, reindexed…}`) : toutes les clés étaient `undefined`, si bien
  // qu'il annonçait « Réindexation terminée : 0 réussis, 0 en échec » à l'instant
  // même où le job démarrait, et présentait le 409 idempotent comme une erreur.
  async function runReindex() {
    if (!ragSpaceId || reindexing) return;
    setReindexing(true);
    setReindexMsg(null);
    setErr(null);
    try {
      const r = await startReindex(ragSpaceId);
      setJob(r.job ?? (await getReindexJob(r.job_id)));
      // 409 « déjà en cours » n'est pas un échec : c'est le job à suivre.
      setReindexMsg(
        r.already_running
          ? t("reindexAlreadyRunning")
          : t("reindexQueued", { count: r.total_documents ?? 0, skipped: 0 })
      );
      await load().catch(() => {});
      refreshStats();
    } catch (e: any) {
      const msg = e?.message || "";
      setErr(
        humanAccessError(e) ||
          (msg.includes("reindex_eager_too_large")
            ? t("reindexTooLarge")
            : msg.includes("reindex_enqueue_unavailable")
              ? t("reindexUnavailable")
              : msg || t("reindexFailed"))
      );
    } finally {
      setReindexing(false);
      setConfirmReindex(false);
    }
  }

  return (
    <>
      <Topbar
        title={t("title")}
        subtitle={ragSpace ? `${ragSpace.name} · ${t("filesCount", { count: counts.all })}` : undefined}
      />
      <div className={layout.page} style={{ maxWidth: 1080, margin: "0 auto" }}>
        <input
          ref={fileRef}
          type="file"
          multiple
          accept={accept}
          style={{ display: "none" }}
          onChange={(e) => e.target.files && uploadFiles(e.target.files)}
        />
        {/* Sélection native de dossier (récursive) — webkitdirectory. */}
        <input
          ref={dirRef}
          type="file"
          multiple
          {...DIRECTORY_INPUT_ATTRS}
          style={{ display: "none" }}
          onChange={(e) => pickFolder(e.target.files)}
        />

        {/* Barre indexation : provider d'embeddings réel + réindexation (admin) */}
        {ragSpaceId && (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 12,
              padding: "10px 14px",
              marginBottom: 16,
              borderRadius: 10,
              background: "var(--chip)",
              border: "1px solid var(--border)",
              flexWrap: "wrap",
            }}
          >
            <Icon name="spark" size={16} style={{ color: "var(--ink-3)" }} />
            <span style={{ fontSize: 12.5, color: "var(--ink-2)" }}>
              {t("indexingLabel")}{" "}
              <strong style={{ color: "var(--ink)", textTransform: "capitalize" }}>
                {embeddingsProvider || "—"}
              </strong>
              {ragSpace?.embedding_model ? ` · ${ragSpace.embedding_model}` : ""}
            </span>
            <RagSpaceRoleBadge access={ragSpaceAccess} size="sm" />
            {/* Volumétrie de CET espace — distincte du quota ci-dessous, qui est
                compté sur tout le workspace (tous les espaces, tous les statuts).
                Les confondre laissait croire à un écart inexpliqué. */}
            <span style={{ fontSize: 12.5, color: "var(--ink-2)" }}>
              {t("ragSpaceDocuments", { count: counts.all })}
            </span>
            {quota && (
              <span
                style={{
                  fontSize: 12.5,
                  color: quotaCritical ? "var(--danger)" : quotaWarn ? "#8A6D3B" : "var(--ink-3)",
                }}
                title={t("quotaScopeHint")}
              >
                <strong style={{ fontWeight: 600 }}>{t("quotaScopeLabel")}</strong>
                {" · "}
                {t("storageUsage", {
                  used: fileSize(quota.used_document_bytes),
                  total: fileSize(quota.max_document_bytes),
                })}
                {" · "}
                {t("filesUsage", {
                  used: formatCount(quota.used_document_count),
                  total: formatCount(quota.max_document_count),
                })}
              </span>
            )}
            <div style={{ flex: 1 }} />
            {reindexMsg && (
              <span style={{ fontSize: 12, color: "var(--ink-3)" }}>{reindexMsg}</span>
            )}
            {canManageCurrent && (
              <Button
                size="sm"
                variant="secondary"
                icon="refresh"
                // Un job actif occupe l'espace (exclusivité backend) : relancer ne
                // produirait qu'un 409. Le bouton dit l'état plutôt que d'y aller.
                disabled={reindexing || jobActive}
                onClick={() => setConfirmReindex(true)}
              >
                {reindexing || jobActive ? t("reindexing") : t("reindex")}
              </Button>
            )}
          </div>
        )}


        {/* Suivi du job durable (réindexation globale OU reprise ciblée) : même
            composant que l'administration d'espace, même contrat de progression. */}
        {job && canManageCurrent && (
          <Card style={{ padding: "12px 14px", marginBottom: 16 }}>
            <ReindexJobProgress
              job={job}
              title={
                job.kind === "failed_documents_retry"
                  ? t("retryJobTitle")
                  : t("reindexJobTitle")
              }
              // Encapsulé : passer la fonction directement lui transmettrait
              // l'événement de clic en guise de stratégie.
              onRetry={() => runRetryFailedItems()}
              style={{ marginTop: 0 }}
            />
            {/* Répartition par méthode : sur un job `auto`, c'est ce qui permet de
                voir que l'OCR rattrape ses documents pendant que le délai étendu
                échoue encore. Un total agrégé le masquerait. */}
            {(job.strategy_breakdown && Object.keys(job.strategy_breakdown).length > 1) && (
              <div style={{ marginTop: 8, fontSize: 12, color: "var(--ink-3)", display: "flex", gap: 12, flexWrap: "wrap" }}>
                {Object.entries(job.strategy_breakdown).map(([s, v]) => (
                  <span key={s} data-testid={`job-strategy-${s}`}>
                    {t("jobStrategyLine", {
                      method: t(`strategy_${s}` as string),
                      done: v.succeeded,
                      total: v.total,
                      failed: v.failed,
                    })}
                  </span>
                ))}
              </div>
            )}
            {!!job.skipped && (
              <div style={{ marginTop: 6, fontSize: 12, color: "var(--ink-3)" }}>
                {t("jobSkipped", { count: job.skipped })}
                {Object.entries(job.skip_reasons || {}).map(([reason, n]) => (
                  <span key={reason} data-testid={`job-skip-${reason}`}>
                    {" · "}
                    {t(`skipReason_${reason}`, { count: n })}
                  </span>
                ))}
              </div>
            )}
            {isTerminal(job.status) && (
              <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 9, flexWrap: "wrap" }}>
                <span style={{ fontSize: 12.5, color: "var(--ink-2)" }}>
                  {t("jobSummary", { ready: job.succeeded, failed: job.failed })}
                </span>
                {job.failed > 0 && filter !== "error" && (
                  <Button size="sm" variant="ghost" onClick={() => setFilter("error")}>
                    {t("jobSeeRemainingErrors")}
                  </Button>
                )}
                {job.failed > 0 && (
                  <Button size="sm" variant="ghost" icon="refresh" onClick={() => runRetryFailedItems("auto")}>
                    {t("jobRetryAuto", { count: job.failed })}
                  </Button>
                )}
                <Button size="sm" variant="ghost" onClick={() => setJob(null)}>
                  {t("close")}
                </Button>
              </div>
            )}
          </Card>
        )}

        {/* Zone glisser-déposer (éditeurs / admins) */}
        {canImport && (
          <div
            onDragOver={(e) => {
              e.preventDefault();
              setDrag(true);
            }}
            onDragLeave={() => setDrag(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDrag(false);
              if (e.dataTransfer.files?.length) uploadFiles(e.dataTransfer.files);
            }}
            onClick={() => fileRef.current?.click()}
            role="button"
            tabIndex={0}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") fileRef.current?.click();
            }}
            style={{
              display: "flex",
              alignItems: "center",
              gap: 16,
              padding: "18px 22px",
              marginBottom: 22,
              borderRadius: 14,
              cursor: "pointer",
              transition: "border-color .15s, background .15s",
              border: `1.5px dashed ${drag ? "var(--accent)" : "var(--border-strong)"}`,
              background: drag ? "var(--accent-soft)" : "var(--surface)",
            }}
          >
            <span
              style={{
                width: 44,
                height: 44,
                borderRadius: 11,
                background: "var(--accent-soft)",
                color: "var(--accent)",
                display: "grid",
                placeItems: "center",
                flex: "none",
              }}
            >
              <Icon name="upload" size={22} />
            </span>
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 14.5, fontWeight: 600, color: "var(--ink)" }}>
                {uploading ? t("importing") : t("dropOrBrowse")}
              </div>
              <div style={{ fontSize: 12.5, color: quotaBlocksUpload ? "var(--danger)" : "var(--ink-3)", marginTop: 2 }}>
                {quotaBlocksUpload
                  ? t("quotaReachedUpload")
                  : `${baseFormatsLabel} · ${t("maxPerFile", { mb: MAX_SIZE_MB })}${
                      audioHelp ? ` · ${audioHelp}` : ""
                    }`}
              </div>
            </div>
            <div style={{ display: "flex", gap: 8, flex: "none" }} onClick={(e) => e.stopPropagation()}>
              <Button
                icon="plus"
                disabled={uploading || quotaBlocksUpload}
                onClick={(e) => {
                  e.stopPropagation();
                  fileRef.current?.click();
                }}
              >
                {t("importDocument")}
              </Button>
              <Button
                variant="secondary"
                id={FOLDER_BUTTON_ID}
                icon="archive"
                disabled={uploading || quotaBlocksUpload || !folderSupported}
                title={folderSupported ? t("importFolderTooltip") : t("importFolderUnsupported")}
                onClick={(e) => {
                  e.stopPropagation();
                  dirRef.current?.click();
                }}
              >
                {t("importFolder")}
              </Button>
            </div>
          </div>
        )}

        {uploads.length > 0 && (
          <Card style={{ padding: 12, marginBottom: 16 }}>
            {/* Progression globale du lot (distinct du statut d'indexation). */}
            {(() => {
              // Partition : chaque élément est dans EXACTEMENT un seau. La barre suit
              // `done/total` et ne recule jamais — un élément en attente n'y est pas
              // compté comme fini, ce qui est précisément ce qui la rendait mensongère.
              const total = uploads.length;
              const done = uploads.filter((u) => u.status === "done").length;
              const errors = uploads.filter((u) => u.status === "error").length;
              const inFlight = uploads.filter((u) => u.status === "uploading").length;
              const waiting = uploads.filter(
                (u) => u.status === "queued" || u.status === "waiting_retry"
              ).length;
              const cancelled = uploads.filter((u) => u.status === "cancelled").length;
              const canRetry = !uploading && uploads.some((u) => u.status === "error" && u.file);
              return (
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 12,
                    marginBottom: 10,
                    fontSize: 12.5,
                    color: "var(--ink-2)",
                    flexWrap: "wrap",
                  }}
                >
                  <span>
                    {t("sending")} <strong style={{ color: "var(--ink)" }}>{done}/{total}</strong>
                  </span>
                  {inFlight > 0 && <span>· {t("inFlightCount", { count: inFlight })}</span>}
                  {waiting > 0 && <span>· {t("waitingCount", { count: waiting })}</span>}
                  {errors > 0 && <span style={{ color: "#C2562F" }}>· {t("errorCount", { count: errors })}</span>}
                  {cancelled > 0 && <span>· {t("cancelledCount", { count: cancelled })}</span>}
                  <div style={{ flex: 1 }} />
                  {uploading && (
                    <Button
                      size="sm"
                      variant="secondary"
                      icon="stop"
                      onClick={() => uploadAbort.current?.abort()}
                    >
                      {t("cancelImport")}
                    </Button>
                  )}
                  {canRetry && (
                    <Button size="sm" variant="secondary" icon="refresh" onClick={retryFailed}>
                      {t("retryErrors")}
                    </Button>
                  )}
                </div>
              );
            })()}
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              {uploads.map((u) => (
                <UploadRow key={u.id} item={u} onDismiss={() => setUploads((p) => p.filter((x) => x.id !== u.id))} />
              ))}
            </div>
          </Card>
        )}

        {folderPlan && (
          <FolderImportModal
            plan={folderPlan}
            formats={folderFormats}
            selected={selectedFormats}
            onToggleFormat={toggleFolderFormat}
            maxSizeMb={MAX_SIZE_MB}
            onCancel={closeFolderModal}
            onConfirm={confirmFolderImport}
          />
        )}

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

        {/* Filtres + recherche */}
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 16, flexWrap: "wrap" }}>
          <div
            role="tablist"
            className={layout.filterTabs}
            style={{ display: "flex", gap: 4, padding: 4, borderRadius: 10, background: "var(--chip)", border: "1px solid var(--border)" }}
          >
            {/* Onglets dérivés de FILTER_KEYS : la liste des catégories vit à un
                seul endroit, partagé avec la construction de la requête. */}
            {FILTER_KEYS.map((k) => (
              <button
                key={k}
                role="tab"
                aria-selected={filter === k}
                onClick={() => setFilter(k)}
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 6,
                  padding: "6px 12px",
                  borderRadius: 7,
                  border: "none",
                  cursor: "pointer",
                  fontFamily: "inherit",
                  fontSize: 13,
                  fontWeight: 600,
                  background: filter === k ? "var(--surface)" : "transparent",
                  color: filter === k ? "var(--ink)" : "var(--ink-3)",
                  boxShadow: filter === k ? "0 1px 2px rgba(60,40,25,.1)" : "none",
                }}
              >
                {t(FILTER_LABEL_KEYS[k])}
                <span
                  style={{
                    fontSize: 11,
                    fontWeight: 700,
                    color: k === "error" && counts[k] ? "var(--danger)" : "var(--ink-3)",
                  }}
                >
                  {counts[k]}
                </span>
              </button>
            ))}
          </div>
          <div style={{ flex: 1, minWidth: 180, maxWidth: 280 }}>
            <Field icon="search" placeholder={t("searchPlaceholder")} value={query} onChange={setQuery} />
          </div>
          {/* Tri accessible SANS pointeur ni largeur d'écran : les en-têtes de
              colonnes restent le geste naturel sur desktop, mais ils disparaissent
              dès que la table est réduite. Ce sélecteur est le même état, exposé
              autrement — jamais une seconde source de vérité. */}
          <label style={{ display: "inline-flex", alignItems: "center", gap: 7, fontSize: 12.5, color: "var(--ink-3)" }}>
            {t("sortByLabel")}
            <select
              value={`${sortDesc ? "-" : ""}${sortField}`}
              onChange={(e) => {
                const raw = e.target.value;
                setSortDesc(raw.startsWith("-"));
                setSortField((raw.startsWith("-") ? raw.slice(1) : raw) as SortField);
              }}
              style={{
                padding: "7px 9px",
                borderRadius: 8,
                border: "1px solid var(--border)",
                background: "var(--surface)",
                fontFamily: "inherit",
                fontSize: 12.5,
                color: "var(--ink)",
              }}
            >
              {SORT_FIELDS.map((f) => [
                <option key={f} value={f}>{`${t(SORT_LABEL_KEYS[f])} ${t("sortAsc")}`}</option>,
                <option key={`-${f}`} value={`-${f}`}>{`${t(SORT_LABEL_KEYS[f])} ${t("sortDesc")}`}</option>,
              ])}
            </select>
          </label>
        </div>

        {/* Barre d'action de sélection — visible dès qu'un document est coché.
            Elle dit TOUJOURS combien de documents sont réellement visés, et
            distingue « cette page » de « tous les résultats » : confondre les deux
            ferait lancer une campagne sur 50 documents en croyant en traiter 184. */}
        {selectionEnabled && selectionSize > 0 && (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 12,
              flexWrap: "wrap",
              padding: "10px 14px",
              marginBottom: 16,
              borderRadius: 10,
              background: "var(--chip)",
              border: "1px solid var(--border)",
            }}
          >
            <span style={{ fontSize: 13, fontWeight: 600, color: "var(--ink)" }}>
              {t("selectionCount", { count: selectionSize })}
            </span>
            {canExtendToAll && (
              <>
                <span style={{ fontSize: 12.5, color: "var(--ink-3)" }}>
                  {t("selectionPageOnly", { count: pageIds.length })}
                </span>
                <Button size="sm" variant="ghost" onClick={() => setSelection(selectAllMatching())}>
                  {t("selectionSelectAllMatching", { count: total })}
                </Button>
              </>
            )}
            {selection.mode === "allMatching" && (
              <span style={{ fontSize: 12.5, color: "var(--ink-3)" }}>
                {t("selectionAllMatching")}
              </span>
            )}
            <div style={{ flex: 1 }} />
            <Button size="sm" variant="ghost" onClick={() => setSelection(EMPTY_SELECTION)}>
              {t("selectionClear")}
            </Button>
            <Button
              size="sm"
              icon="refresh"
              disabled={jobActive || startingRetry}
              onClick={() => setConfirmRetry(true)}
            >
              {t("selectionRetry")}
            </Button>
          </div>
        )}

        {/* Tableau.
            Le voile de chargement ne remplace la table QUE quand il n'y a encore
            rien à montrer. Un tri ou un changement de page recharge côté serveur :
            démonter la table à chaque fois la ferait clignoter, ferait sauter la
            position de lecture et perdrait le focus clavier de l'en-tête qu'on
            vient d'actionner. */}
        {loading && docs.length === 0 ? (
          <Card style={{ padding: 32, color: "var(--ink-3)", fontSize: 14 }}>{t("loadingDocuments")}</Card>
        ) : shown.length === 0 ? (
          <Card style={{ padding: 8 }}>
            <EmptyState
              icon="doc"
              title={
                search
                  ? t("emptyNoResults")
                  : filter === "all"
                    ? t("emptyNoneYet")
                    : t("emptyNoneCategory")
              }
              body={
                search
                  ? t("emptyTryAnother")
                  : filter === "all"
                    ? t("emptyImportFirst")
                    : t("emptyNoneWithStatus", { status: t(FILTER_LABEL_KEYS[filter]) })
              }
              action={
                canImport && !search && filter === "all" ? (
                  <Button icon="upload" onClick={() => fileRef.current?.click()}>
                    {t("importDocument")}
                  </Button>
                ) : null
              }
            />
          </Card>
        ) : (
          <>
            <Card
              style={{
                overflow: "hidden",
                padding: 0,
                // Rechargement en cours : la table reste en place, atténuée.
                opacity: loading ? 0.55 : 1,
                transition: "opacity .15s ease",
              }}
              aria-busy={loading || undefined}
            >
              <div
                role="row"
                style={{
                  display: "grid",
                  gridTemplateColumns: selectionEnabled ? `28px ${DOC_GRID}` : DOC_GRID,
                  gap: 12,
                  padding: "11px 18px",
                  borderBottom: "1px solid var(--border)",
                  background: "var(--sidebar)",
                }}
              >
                {selectionEnabled && (
                  <input
                    type="checkbox"
                    checked={allPageSelected}
                    onChange={() => setSelection(togglePage(selection, pageIds))}
                    aria-label={t("selectPage")}
                    style={{ width: 15, height: 15, cursor: "pointer" }}
                  />
                )}
                <SortHeader field="filename" label={t("colDocument")} sortField={sortField} sortDesc={sortDesc} onSort={onSort} />
                <SortHeader field="file_type" label={t("colType")} sortField={sortField} sortDesc={sortDesc} onSort={onSort} />
                <SortHeader field="status" label={t("colStatus")} sortField={sortField} sortDesc={sortDesc} onSort={onSort} />
                {/* Le nombre d'extraits n'est pas une colonne de la table documents :
                    il se compte, il ne se trie pas ici. */}
                <SectionLabel>{t("colIndexing")}</SectionLabel>
                <SortHeader field="created_at" label={t("colImportedOn")} sortField={sortField} sortDesc={sortDesc} onSort={onSort} />
                <SectionLabel>{""}</SectionLabel>
              </div>
              {shown.map((d, i) => (
                <DocRow
                  key={d.id}
                  d={d}
                  last={i === shown.length - 1}
                  canManage={canImport}
                  embeddingsProvider={embeddingsProvider}
                  selectable={selectionEnabled}
                  selected={selectionEnabled && isSelected(selection, d.id)}
                  onToggleSelect={() => setSelection(toggleOne(selection, d.id))}
                  onReindex={(strategy: RetryStrategy) => onReindex(d.id, strategy)}
                  onRemove={() => { setDeleteErr(null); setConfirmDelete(d); }}
                  onSummarize={() => setSummaryDoc(d)}
                />
              ))}
            </Card>
            <DocumentsPagination
              total={total}
              limit={limit}
              offset={offset}
              loading={loading}
              onOffset={setOffset}
              onLimit={setLimit}
            />
          </>
        )}
      </div>

      {confirmRetry && (
        <Modal
          onClose={() => (startingRetry ? null : setConfirmRetry(false))}
          title={t("retryModalTitle", { count: selectionSize })}
          subtitle={t("retryModalSubtitle")}
        >
          <div
            style={{
              display: "flex",
              gap: 11,
              padding: "13px 15px",
              borderRadius: 11,
              background: "var(--chip)",
              border: "1px solid var(--border)",
              fontSize: 13.5,
              color: "var(--ink-2)",
              lineHeight: 1.55,
            }}
          >
            <Icon name="alert" size={17} style={{ color: "var(--ink-3)", flex: "none", marginTop: 2 }} />
            <div>
              {/* Dire explicitement ce que l'opération N'EST PAS : sans cette
                  phrase, « réingérer 184 documents » se lit comme un ré-upload,
                  avec la crainte du quota et des doublons qui va avec. */}
              <p style={{ margin: 0 }}>{t("retryModalBodyIntegrity")}</p>
              <p style={{ margin: "8px 0 0" }}>{t("retryModalBodyAsync")}</p>
            </div>
          </div>

          {/* Méthode de reprise. `auto` en tête et par défaut : c'est la seule qui
              tienne compte de la cause de l'échec. */}
          <div style={{ marginTop: 16 }}>
            <SectionLabel>{t("retryStrategyLabel")}</SectionLabel>
            <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 8 }}>
              {RETRY_STRATEGIES.map((s) => (
                <label
                  key={s}
                  style={{ display: "flex", alignItems: "flex-start", gap: 9, fontSize: 13.5, cursor: "pointer" }}
                >
                  <input
                    type="radio"
                    name="retry-strategy"
                    value={s}
                    checked={retryStrategy === s}
                    onChange={() => setRetryStrategy(s)}
                    style={{ marginTop: 3 }}
                  />
                  <span>
                    <span style={{ fontWeight: retryStrategy === s ? 600 : 400, color: "var(--ink)" }}>
                      {t(`strategy_${s}`)}
                    </span>
                    <span style={{ display: "block", fontSize: 12.5, color: "var(--ink-3)" }}>
                      {t(`strategyHint_${s}`)}
                    </span>
                  </span>
                </label>
              ))}
            </div>
          </div>

          {/* Prévision : combien de documents relèvent de quoi, AVANT de lancer.
              Une réponse incomplète (ancien backend, erreur réseau) ne doit pas
              faire tomber la modale : la prévision est un confort, le lancement
              reste possible sans elle. */}
          {retryPreview?.strategy_breakdown && (
            <div
              style={{
                marginTop: 14,
                padding: "11px 13px",
                borderRadius: 10,
                background: "var(--chip)",
                border: "1px solid var(--border)",
                fontSize: 13,
                color: "var(--ink-2)",
              }}
              data-testid="retry-preview"
            >
              <div style={{ fontWeight: 600, color: "var(--ink)", marginBottom: 6 }}>
                {t("retryPreviewTitle", { count: retryPreview.total_documents })}
              </div>
              <ul style={{ margin: 0, paddingLeft: 18, lineHeight: 1.7 }}>
                {RETRY_STRATEGIES.filter((s) => s !== "auto").map((s) => {
                  const n = retryPreview.strategy_breakdown[s] ?? 0;
                  return n > 0 ? (
                    <li key={s}>{t("retryPreviewLine", { count: n, method: t(`strategy_${s}`) })}</li>
                  ) : null;
                })}
                {retryPreview.skipped_documents > 0 && (
                  <li style={{ color: "var(--ink-3)" }}>
                    {t("retryPreviewSkipped", { count: retryPreview.skipped_documents })}
                  </li>
                )}
              </ul>
              {retryPreview.extended_timeout_seconds != null && (
                <div style={{ marginTop: 6, fontSize: 12.5, color: "var(--ink-3)" }}>
                  {t("retryPreviewTimeout", { seconds: retryPreview.extended_timeout_seconds })}
                </div>
              )}
              {retryStrategy === "standard" && retryPreview.actionable_documents > 0 && (
                <div style={{ marginTop: 8, fontSize: 12.5, color: "#8A6D3B" }}>
                  {t("retryStandardWarning")}
                </div>
              )}
            </div>
          )}

          <div style={{ display: "flex", justifyContent: "flex-end", gap: 10, marginTop: 18 }}>
            <Button variant="secondary" disabled={startingRetry} onClick={() => setConfirmRetry(false)}>
              {t("cancel")}
            </Button>
            <Button
              icon="refresh"
              disabled={startingRetry || retryPreview?.actionable_documents === 0}
              onClick={runRetrySelection}
            >
              {startingRetry ? t("retryStarting") : t("retryConfirm")}
            </Button>
          </div>
        </Modal>
      )}

      {confirmReindex && (
        <Modal
          onClose={() => (reindexing ? null : setConfirmReindex(false))}
          title={t("reindexModalTitle")}
          subtitle={t("reindexModalSubtitle")}
        >
          <div
            style={{
              display: "flex",
              gap: 11,
              padding: "13px 15px",
              borderRadius: 11,
              background: "var(--accent-soft)",
              border: "1px solid var(--border)",
            }}
          >
            <Icon name="alert" size={19} style={{ color: "var(--accent-ink)", flex: "none" }} />
            <div style={{ fontSize: 13.5, color: "var(--ink-2)", lineHeight: 1.5 }}>
              {t("reindexModalBodyBefore")}
              <strong style={{ textTransform: "capitalize" }}>{embeddingsProvider || "—"}</strong>
              {ragSpace?.embedding_model ? ` · ${ragSpace.embedding_model}` : ""}
              {t("reindexModalBodyAfter")}
            </div>
          </div>
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 10, marginTop: 22 }}>
            <Button variant="secondary" disabled={reindexing} onClick={() => setConfirmReindex(false)}>
              {t("cancel")}
            </Button>
            <Button icon="refresh" disabled={reindexing} onClick={runReindex}>
              {reindexing ? t("reindexing") : t("reindexNow")}
            </Button>
          </div>
        </Modal>
      )}

      {confirmDelete && (
        <Modal
          onClose={() => (deleting ? null : (setConfirmDelete(null), setDeleteErr(null)))}
          title={t("deleteModalTitle")}
          subtitle={confirmDelete.filename}
        >
          <div
            style={{
              display: "flex",
              gap: 11,
              padding: "13px 15px",
              borderRadius: 11,
              background: "var(--danger-soft, var(--accent-soft))",
              border: "1px solid var(--border)",
            }}
          >
            <Icon name="alert" size={19} style={{ color: "var(--danger)", flex: "none" }} />
            <div style={{ fontSize: 13.5, color: "var(--ink-2)", lineHeight: 1.5 }}>
              {t("deleteModalBodyBefore")}
              <strong>{t("deleteModalBodyIrreversible")}</strong>.
            </div>
          </div>
          {deleteErr && (
            <div style={{ marginTop: 12, fontSize: 13, color: "var(--danger)", display: "flex", gap: 6, alignItems: "center" }}>
              <Icon name="alert" size={14} stroke={2} />
              {deleteErr}
            </div>
          )}
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 10, marginTop: 22 }}>
            <Button
              variant="secondary"
              disabled={deleting}
              onClick={() => { setConfirmDelete(null); setDeleteErr(null); }}
            >
              {t("cancel")}
            </Button>
            <Button variant="danger" icon="trash" disabled={deleting} onClick={doDelete}>
              {deleting ? t("deleting") : t("deleteNow")}
            </Button>
          </div>
        </Modal>
      )}

      {summaryDoc && <SummaryModal doc={summaryDoc} onClose={() => setSummaryDoc(null)} />}
    </>
  );
}

/** Modale de résumé : déclenche l'appel API à l'ouverture, gère chargement/erreur/résultat. */
const SUMMARY_MODES: { key: SummaryMode; labelKey: string }[] = [
  { key: "short", labelKey: "summaryModeShort" },
  { key: "bullets", labelKey: "summaryModeBullets" },
  { key: "detailed", labelKey: "summaryModeDetailed" },
  { key: "executive", labelKey: "summaryModeExecutive" },
];

function SummaryModal({ doc, onClose }: { doc: ApiDocument; onClose: () => void }) {
  const t = useTranslations("documents");
  const tErrors = useTranslations("errors");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [res, setRes] = useState<DocumentSummaryResponse | null>(null);
  const [copied, setCopied] = useState(false);
  const [mode, setMode] = useState<SummaryMode>("bullets");
  const [pending, setPending] = useState(false);

  const run = useCallback(
    async (m: SummaryMode, opts: { refresh?: boolean } = {}) => {
      setLoading(true);
      setError(null);
      setRes(null);
      setPending(false);
      try {
        const r = await summarizeDocumentAwait(
          doc.id,
          // Pas de langue forcée : résolution serveur d'après le profil.
          { mode: m, refresh: opts.refresh },
          { onPending: () => setPending(true) }
        );
        if (r.status === "failed") setError(tErrors(summaryErrorKey(r.error || "")));
        else setRes(r);
      } catch (e: unknown) {
        const msg = e instanceof Error ? e.message : "";
        setError(tErrors(summaryErrorKey(msg)));
      } finally {
        setLoading(false);
        setPending(false);
      }
    },
    [doc.id]
  );

  // Génération initiale (mode par défaut). Le changement de mode déclenche un
  // nouvel appel via pickMode ci-dessous — on ne dépend donc pas de `mode` ici.
  useEffect(() => {
    run("bullets");
  }, [run]);

  function pickMode(m: SummaryMode) {
    if (m === mode && (res || loading)) return;
    setMode(m);
    run(m);
  }

  async function copy() {
    if (!res?.summary) return;
    try {
      await navigator.clipboard.writeText(res.summary);
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } catch {
      /* clipboard indisponible : on ignore silencieusement */
    }
  }

  return (
    <Modal onClose={onClose} title={t("summaryTitle")} subtitle={doc.filename}>
      <div
        style={{ display: "flex", gap: 6, marginBottom: 16, flexWrap: "wrap" }}
        role="group"
        aria-label={t("summaryTypeLabel")}
      >
        {SUMMARY_MODES.map((m) => {
          const on = m.key === mode;
          return (
            <button
              key={m.key}
              type="button"
              disabled={loading}
              aria-pressed={on}
              onClick={() => pickMode(m.key)}
              style={{
                padding: "5px 11px",
                borderRadius: 999,
                border: `1px solid ${on ? "var(--accent)" : "var(--border)"}`,
                background: on ? "var(--accent-soft)" : "var(--surface)",
                color: on ? "var(--accent-ink)" : "var(--ink-2)",
                fontFamily: "inherit",
                fontSize: 12.5,
                fontWeight: 600,
                cursor: loading ? "default" : "pointer",
                opacity: loading && !on ? 0.6 : 1,
              }}
            >
              {t(m.labelKey)}
            </button>
          );
        })}
      </div>
      <div style={{ minHeight: 120 }}>
        {loading && (
          <div style={{ display: "flex", alignItems: "center", gap: 10, color: "var(--ink-2)", fontSize: 13.5, padding: "20px 0" }}>
            <Icon name="refresh" size={16} style={{ animation: "rg-spin 1s linear infinite" }} />
            {pending ? t("summaryGeneratingLong") : t("summaryGenerating")}
          </div>
        )}

        {!loading && error && (
          <div
            style={{
              display: "flex",
              gap: 11,
              padding: "13px 15px",
              borderRadius: 11,
              background: "var(--danger-soft)",
              border: "1px solid var(--border)",
            }}
          >
            <Icon name="alert" size={19} style={{ color: "var(--danger)", flex: "none" }} />
            <div style={{ fontSize: 13.5, color: "var(--ink-2)", lineHeight: 1.5 }}>{error}</div>
          </div>
        )}

        {!loading && res && (
          <>
            {res.truncated && (
              <div style={{ fontSize: 12.5, color: "var(--ink-3)", marginBottom: 10, display: "flex", gap: 6, alignItems: "center" }}>
                <Icon name="alert" size={13} />
                {t("summaryTruncated")}
              </div>
            )}
            <div
              style={{
                whiteSpace: "pre-wrap",
                fontSize: 13.5,
                lineHeight: 1.6,
                color: "var(--ink)",
                maxHeight: "50vh",
                overflowY: "auto",
              }}
            >
              {res.summary}
            </div>
          </>
        )}
      </div>

      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 10, marginTop: 22 }}>
        <span style={{ fontSize: 11.5, color: "var(--ink-3)" }}>
          {res?.provider ? `${res.provider}${res.model ? ` · ${res.model}` : ""}` : ""}
          {res?.generated_at ? `${res.provider ? " · " : ""}${res.cached ? t("summaryCachedPrefix") : ""}${t("summaryGeneratedOn", { date: shortDate(res.generated_at) })}` : ""}
        </span>
        <div style={{ display: "flex", gap: 10 }}>
          {res && (
            <Button variant="secondary" icon={copied ? "check" : "copy"} onClick={copy}>
              {copied ? t("copied") : t("copy")}
            </Button>
          )}
          {!loading && (error || res) && (
            <Button variant="secondary" icon="refresh" onClick={() => run(mode, { refresh: true })}>
              {t("regenerate")}
            </Button>
          )}
          <Button onClick={onClose}>{t("close")}</Button>
        </div>
      </div>
    </Modal>
  );
}

/** Secondes restantes avant le prochain essai, rafraîchies chaque seconde.
 *
 * Le décompte n'existe QUE tant que l'élément attend : pas d'intervalle qui tourne
 * en fond sur une ligne terminée. */
function useRetryCountdown(retryAt: number | undefined, active: boolean): number {
  const [, force] = React.useState(0);
  React.useEffect(() => {
    if (!active || !retryAt) return;
    const id = setInterval(() => force((n) => n + 1), 1000);
    return () => clearInterval(id);
  }, [active, retryAt]);
  if (!active || !retryAt) return 0;
  return Math.max(0, Math.ceil((retryAt - Date.now()) / 1000));
}

/** Ligne de progression d'envoi (phase upload réseau) pour un fichier. */
function UploadRow({ item, onDismiss }: { item: UploadItem; onDismiss: () => void }) {
  const t = useTranslations("documents");
  const type = fileTypeLabel(item.name);
  const color = FILE_COLORS[type] || "#888";
  const isError = item.status === "error";
  const isDone = item.status === "done";
  // Écarté avant tout envoi : ni échec, ni succès. Ton neutre — le rouge dirait à
  // l'utilisateur qu'il a quelque chose à corriger, ce qui est faux.
  const isSkipped = item.status === "skipped";
  const isQueued = item.status === "queued";
  const isWaiting = item.status === "waiting_retry";
  const isCancelled = item.status === "cancelled";
  const secondsLeft = useRetryCountdown(item.retryAt, isWaiting);
  const barColor = isError
    ? "var(--danger)"
    : isDone
    ? "var(--success)"
    : isSkipped || isQueued || isWaiting || isCancelled
    ? "var(--ink-3)"
    : "var(--accent)";
  const statusLabel = isSkipped
    ? t("statusSkipped")
    : isError
    ? t("statusFailed")
    : isDone
    ? t("statusSentIndexing")
    : isCancelled
    ? t("statusCancelled")
    : isQueued
    ? t("statusQueued")
    // Le message dit une ATTENTE, jamais « API 429 » : le code HTTP appartient au
    // journal, pas à l'utilisateur, qui n'a rien à corriger.
    : isWaiting
    ? t("statusWaitingRetry", { seconds: secondsLeft })
    : t("statusUploading", { percent: item.percent });
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
      <span
        style={{
          width: 32,
          height: 32,
          borderRadius: 8,
          flex: "none",
          display: "grid",
          placeItems: "center",
          background: `${color}18`,
          color,
          fontSize: 9.5,
          fontWeight: 700,
        }}
      >
        {type}
      </span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span
            title={item.relativePath || item.name}
            style={{
              flex: 1,
              minWidth: 0,
              fontSize: 13,
              fontWeight: 600,
              color: "var(--ink)",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {item.relativePath || item.name}
          </span>
          <span style={{ fontSize: 12, fontWeight: 600, color: isError ? "var(--danger)" : "var(--ink-3)", flex: "none" }}>
            {statusLabel}
          </span>
        </div>
        <div style={{ height: 4, borderRadius: 999, background: "var(--chip)", marginTop: 6, overflow: "hidden" }}>
          <div
            style={{
              width: `${isError || isSkipped || isCancelled ? 100 : isQueued || isWaiting ? 0 : item.percent}%`,
              height: "100%",
              background: barColor,
              transition: "width .2s ease",
            }}
          />
        </div>
        {isError && item.error && (
          <div style={{ fontSize: 12, color: "var(--danger)", marginTop: 5, display: "flex", gap: 5, alignItems: "center" }}>
            <Icon name="alert" size={13} stroke={2} />
            {item.error}
          </div>
        )}
        {/* Explication sobre : `archive`, jamais `alert` — rien n'a échoué. */}
        {isSkipped && item.error && (
          <div style={{ fontSize: 12, color: "var(--ink-3)", marginTop: 5, display: "flex", gap: 5, alignItems: "center" }}>
            <Icon name="archive" size={13} stroke={2} />
            {item.error}
          </div>
        )}
      </div>
      {(isError || isSkipped) && (
        <button
          onClick={onDismiss}
          aria-label={isSkipped ? t("hideSkipped") : t("hideError")}
          style={{
            width: 28,
            height: 28,
            borderRadius: 7,
            border: "none",
            background: "transparent",
            cursor: "pointer",
            color: "var(--ink-3)",
            fontSize: 18,
            lineHeight: 1,
            flex: "none",
          }}
        >
          ×
        </button>
      )}
    </div>
  );
}

function DocRow({
  d,
  last,
  canManage,
  embeddingsProvider,
  selectable = false,
  selected = false,
  onToggleSelect,
  onReindex,
  onRemove,
  onSummarize,
}: {
  d: ApiDocument;
  last: boolean;
  canManage: boolean;
  embeddingsProvider: string;
  selectable?: boolean;
  selected?: boolean;
  onToggleSelect?: () => void;
  onReindex: (strategy: RetryStrategy) => void;
  onRemove: () => void;
  onSummarize: () => void;
}) {
  const t = useTranslations("documents");
  const [menu, setMenu] = useState(false);
  const menuBtnRef = useRef<HTMLButtonElement>(null);
  const ui = toDocStatus(d.status);
  // Actions de reprise possibles, telles que le BACKEND les a classées.
  const retry = retryActionsFor(d);
  // Type décidé par le backend : c'est celui sur lequel la colonne TYPE trie.
  const type = documentTypeLabel(d);
  const color = FILE_COLORS[type] || "#888";
  // Audio : sous-statut de traitement, durée/langue, échec spécifique.
  const audioProcessing = audioProcessingNotice(d);
  const audioReady = audioReadyNotice(d);
  const audioFailure = audioFailureKey(d);

  // Téléchargement authentifié : on récupère le blob avec le token (un <a href>
  // direct vers l'API renverrait 401), puis on déclenche l'enregistrement.
  async function download() {
    try {
      const { url, revoke } = await apiFetchBlobUrl(`/api/v1/documents/${d.id}/download/`);
      const a = document.createElement("a");
      a.href = url;
      a.download = d.filename || "document";
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(revoke, 4000);
    } catch {
      /* l'erreur réseau est silencieuse ici ; le bouton reste réessayable */
    }
  }

  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: selectable ? `28px ${DOC_GRID}` : DOC_GRID,
        gap: 12,
        alignItems: "center",
        padding: "13px 18px",
        borderBottom: last ? "none" : "1px solid var(--border)",
        background: selected ? "var(--chip)" : undefined,
      }}
    >
      {selectable && (
        <input
          type="checkbox"
          checked={selected}
          onChange={() => onToggleSelect?.()}
          aria-label={t("selectDocument", { name: d.filename })}
          style={{ width: 15, height: 15, cursor: "pointer" }}
        />
      )}

      {/* Document */}
      <div style={{ display: "flex", alignItems: "center", gap: 12, minWidth: 0 }}>
        <span
          style={{
            width: 36,
            height: 36,
            borderRadius: 9,
            flex: "none",
            display: "grid",
            placeItems: "center",
            background: `${color}18`,
            color,
            fontSize: 10,
            fontWeight: 700,
          }}
        >
          {type}
        </span>
        <div style={{ minWidth: 0 }}>
          <div
            style={{
              fontSize: 13.5,
              fontWeight: 600,
              color: "var(--ink)",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
            title={d.filename}
          >
            {d.filename}
          </div>
          <div style={{ fontSize: 12, color: "var(--ink-3)" }}>
            {fileSize(d.size_bytes)}
            {d.content_type ? ` · ${d.content_type}` : ""}
          </div>
          {ui === "error" && (
            <div style={{ fontSize: 12, color: "var(--danger)", marginTop: 4, display: "flex", gap: 5, alignItems: "center" }}>
              <Icon name="alert" size={13} stroke={2} />
              {audioFailure ? t(audioFailure) : humanError(d.error_message, embeddingsProvider)}
            </div>
          )}
          {audioReady && (
            <div style={{ fontSize: 12, color: "var(--ink-3)", marginTop: 4 }}>
              {t(audioReady.key, audioReady.values)}
            </div>
          )}
          {ui === "ready" && extractionNotice(d) && (
            <div style={{ fontSize: 12, color: "var(--ink-3)", marginTop: 4, display: "flex", gap: 5, alignItems: "center" }}>
              <Icon name="alert" size={13} stroke={2} />
              {extractionNotice(d)}
            </div>
          )}
        </div>
      </div>

      {/* Type — colonne dédiée : c'est la valeur RÉELLEMENT triée par le serveur,
          affichée telle quelle plutôt que déduite du badge coloré. */}
      <div style={{ fontSize: 12.5, fontWeight: 600, color: "var(--ink-2)" }}>
        {type || <span style={{ color: "var(--ink-3)", fontWeight: 400 }}>—</span>}
      </div>

      {/* Statut */}
      <div>
        <StatusBadge status={ui} size="sm" />
        {visualEnrichmentNoticeKey(d) && (
          <div style={{ fontSize: 11.5, color: "var(--ink-3)", marginTop: 4 }}>
            {t(visualEnrichmentNoticeKey(d)!)}
          </div>
        )}
        {/* Audio : la transcription peut durer (statut PENDING) — l'étape précise est dite. */}
        {audioProcessing && (
          <div style={{ fontSize: 11.5, color: "var(--ink-3)", marginTop: 4 }}>
            {t(audioProcessing.key, audioProcessing.values)}
          </div>
        )}
        {ui === "analyzing" && (
          <div style={{ height: 3, borderRadius: 999, background: "var(--chip)", marginTop: 7, overflow: "hidden", maxWidth: 120 }}>
            <div
              style={{
                width: "60%",
                height: "100%",
                background: "var(--warning)",
                animation: "rg-bounce 1.4s ease-in-out infinite",
              }}
            />
          </div>
        )}
      </div>

      {/* Indexation */}
      <div style={{ fontSize: 12.5, color: "var(--ink-2)" }}>
        {d.chunks_count ? (
          <span style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
            <Icon name="grid" size={13} style={{ color: "var(--ink-3)" }} />
            {t("chunksCount", { count: d.chunks_count })}
          </span>
        ) : (
          <span style={{ color: "var(--ink-3)" }}>—</span>
        )}
      </div>

      {/* Importé le — date ET heure : c'est l'horodatage sur lequel la colonne
          trie, et deux imports du même jour ne se distinguent pas sans l'heure. */}
      <div style={{ fontSize: 12.5, color: "var(--ink-2)" }}>{dateTime(d.created_at)}</div>

      {/* Actions */}
      <div style={{ position: "relative", justifySelf: "end" }}>
        <button
          ref={menuBtnRef}
          onClick={() => setMenu((m) => !m)}
          aria-label={t("docActions")}
          aria-haspopup="menu"
          aria-expanded={menu}
          style={{
            width: 30,
            height: 30,
            borderRadius: 8,
            border: "none",
            background: menu ? "var(--hover)" : "transparent",
            cursor: "pointer",
            display: "grid",
            placeItems: "center",
            color: "var(--ink-3)",
          }}
        >
          <Icon name="dots" size={18} />
        </button>
        <Popover
          open={menu}
          onClose={() => setMenu(false)}
          anchorRef={menuBtnRef}
          align="end"
          minWidth={190}
          padding={5}
          radius={11}
          role="menu"
        >
          <MenuItem icon="eye" label={t("menuOpen")} href={`/doc/${d.id}?back=/documents`} internal onClick={() => setMenu(false)} />
          {ui === "ready" && (d.chunks_count ?? 0) > 0 && (
            <MenuItem
              icon="sparkles"
              label={t("menuSummarize")}
              onClick={() => {
                onSummarize();
                setMenu(false);
              }}
            />
          )}
          <MenuItem
            icon="download"
            label={t("download")}
            onClick={() => {
              download();
              setMenu(false);
            }}
          />
          {/* Actions de reprise DÉCIDÉES par le backend. On n'affiche donc jamais
              « Réessayer avec OCR » sur un document dont l'OCR vient de ne rien
              produire : c'était exactement la promesse creuse de l'écran d'avant. */}
          {ui === "error" && canManage && retry.strategies.map((s) => (
            <MenuItem
              key={s}
              icon="refresh"
              label={
                s === retry.recommended
                  ? t("menuRetryRecommended", { method: t(`strategy_${s}`) })
                  : t(`menuRetry_${s}`)
              }
              onClick={() => {
                onReindex(s);
                setMenu(false);
              }}
            />
          ))}
          {ui === "error" && canManage && retry.strategies.length === 0 && retry.retryability && (
            <div
              style={{ padding: "7px 10px", fontSize: 12.5, color: "var(--ink-3)", maxWidth: 260 }}
              role="note"
            >
              {t(`retryability_${retry.retryability}`)}
            </div>
          )}
          {canManage && <div style={{ height: 1, background: "var(--border)", margin: "5px 0" }} />}
          {canManage && (
            <MenuItem
              icon="trash"
              label={t("deleteNow")}
              danger
              onClick={() => {
                onRemove();
                setMenu(false);
              }}
            />
          )}
        </Popover>
      </div>
    </div>
  );
}

function MenuItem({
  icon,
  label,
  onClick,
  danger,
  href,
  internal,
}: {
  icon: string;
  label: string;
  onClick?: () => void;
  danger?: boolean;
  href?: string;
  internal?: boolean;
}) {
  const style: React.CSSProperties = {
    width: "100%",
    display: "flex",
    alignItems: "center",
    gap: 10,
    padding: "8px 10px",
    borderRadius: 8,
    border: "none",
    background: "transparent",
    cursor: "pointer",
    fontFamily: "inherit",
    fontSize: 13,
    fontWeight: 500,
    color: danger ? "var(--danger)" : "var(--ink)",
    textAlign: "left",
    textDecoration: "none",
  };
  const enter = (e: React.MouseEvent<HTMLElement>) =>
    (e.currentTarget.style.background = danger ? "var(--danger-soft)" : "var(--hover)");
  const leave = (e: React.MouseEvent<HTMLElement>) => (e.currentTarget.style.background = "transparent");
  const content = (
    <>
      <Icon name={icon} size={16} />
      {label}
    </>
  );
  if (href && internal) {
    return (
      <Link href={href} style={style} role="menuitem" onMouseEnter={enter} onMouseLeave={leave} onClick={onClick}>
        {content}
      </Link>
    );
  }
  if (href) {
    return (
      <a href={href} target="_blank" rel="noreferrer" style={style} role="menuitem" onMouseEnter={enter} onMouseLeave={leave} onClick={onClick}>
        {content}
      </a>
    );
  }
  return (
    <button style={style} role="menuitem" onClick={onClick} onMouseEnter={enter} onMouseLeave={leave}>
      {content}
    </button>
  );
}
