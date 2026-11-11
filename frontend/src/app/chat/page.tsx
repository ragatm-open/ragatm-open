"use client";

import React, { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { apiFetch, listDocuments, summarizeSourceDocumentAwait, summaryErrorKey, getRephrasingSuggestions, type AnswerLanguage, type SummaryScope } from "@/lib/api";
import { AppShell, Topbar, useWorkspace } from "@/components/shell";
// Importés par chemin direct (et non via l'index du shell, que les tests du chat
// remplacent par un mock) : pur comportement, aucun rendu.
import { COMPACT_QUERY, useMediaQuery } from "@/components/shell/breakpoints";
import { useModalLayer } from "@/components/shell/useModalLayer";
import { useShellNav } from "@/components/shell/shellNav";
import { isUuid, resolveThread, threadHref, type ResolvedThread } from "@/lib/navigationSearch";
import { AnswerLanguageSelector } from "@/components/AnswerLanguageSelector";
import { Button, Icon, Popover } from "@/components/ui";
import { readSseStream } from "./_lib/sse";
import { CORPUS_CHOICE_LABEL_KEYS, type CorpusChoice, type Msg, type SourceV1 } from "./_lib/types";
import { CorpusScopeChoice } from "./_components/CorpusScopeChoice";
import { buildChatRequestBody } from "./_lib/chatRequest";
import { getStoredThreadIdForRagSpace, setStoredThreadIdForRagSpace } from "./_lib/storage";
import { srcDoc, srcPage, srcReference, type AnySource, type SourceReference } from "./_lib/sources";
import { resolveSourcesDisplay } from "./_lib/citations";
import {
  parseSummaryIntent,
  resolveSummaryTargetFromSources,
  matchDocumentsByName,
} from "./_lib/summaryIntent";
import { AssistantMarkdown } from "./_components/AssistantMarkdown";
import { SummaryMessageBody } from "./_components/SummaryMessageBody";
import { SourcesPanel, SOURCES_PANEL_ID } from "./_components/SourcesPanel";
import layout from "./_components/ChatLayout.module.css";
import { AnalysisCard } from "./_components/AnalysisCard";
import { pollAnalysis, type AnalysisPollHandle } from "./_lib/analysisPoll";
import {
  cancelCorpusAnalysis,
  getCorpusAnalysis,
  getThreadAnalyses,
  isTerminalAnalysisStatus,
  type CorpusAnalysis,
} from "@/lib/api";
import { DraftComposer, type DraftComposerHandle } from "./_components/Composer";
import { MessageActions } from "./_components/MessageActions";
import { RephrasingSuggestions } from "./_components/RephrasingSuggestions";
import { extractSuggestions } from "./_lib/suggestions";
import { pollRephrasingSuggestions } from "./_lib/rephrasingPoll";
import {
  mapServerMessages,
  messageKey,
  reconcileChatMessages,
  type WireChatMessage,
} from "./_lib/chatMessages";
import {
  appendAssistantDelta,
  clearAwaitingCorpusChoice,
  newLocalId,
  patchAssistant,
  replaceAssistantText,
  setAssistantKind,
  setAssistantSources,
} from "./_lib/chatMessageState";

type ChatThread = { id: string; title?: string | null; updated_at?: string | Date | null };

/** Taille d'une page de la liste des fils (plafond historique du backend). */
const THREADS_PAGE_SIZE = 100;

/** Clés de traduction des suggestions d'amorçage (namespace `chat`). */
const SUGGESTION_KEYS = ["suggestion1", "suggestion2", "suggestion3"];

export default function ChatPage() {
  // `useSearchParams` (lien direct `?thread=`) exige une frontière Suspense en App Router.
  return (
    <AppShell fill>
      <Suspense fallback={null}>
        <ChatContent />
      </Suspense>
    </AppShell>
  );
}

function ChatContent() {
  const t = useTranslations("chat");
  const tErrors = useTranslations("errors");
  const tNav = useTranslations("navigationSearch");
  const { ragSpace, ragSpaceId, currentStats, workspaceId, openTarget } = useWorkspace();
  const shellNav = useShellNav();
  // Lien direct `/chat?thread=<uuid>`. Hors App Router
  // (tests), `useSearchParams` renvoie `null` : aucun lien direct.
  const searchParams = useSearchParams();
  const threadParam = searchParams?.get("thread") ?? null;

  const [threads, setThreads] = useState<ChatThread[]>([]);
  const [threadId, setThreadId] = useState<string>("");
  // Accès progressif aux fils au-delà des 100 plus récents.
  const [hasMoreThreads, setHasMoreThreads] = useState(false);
  const [loadingMoreThreads, setLoadingMoreThreads] = useState(false);
  const threadCursorRef = useRef<string>("");
  /** Fil résolu par un lien direct : gardé sélectionnable même hors des pages chargées. */
  const linkedThreadRef = useRef<ResolvedThread | null>(null);
  /** Dernier `?thread=` traité (évite de résoudre deux fois le même lien). */
  const handledParamRef = useRef<string | null>(null);
  const [linkNotice, setLinkNotice] = useState(false);
  const [messages, setMessages] = useState<Msg[]>([]);
  /** Dernier fil effectivement chargé : distingue « changement de fil » (remplacement)
   *  de « rafraîchissement du même fil » (réconciliation). */
  const loadedThreadRef = useRef<string>("");
  // Le brouillon appartient au composer (DraftComposer) : la page n'en garde qu'une
  // poignée pour le vider ou le remplir, jamais un état qu'une frappe modifie.
  const composerRef = useRef<DraftComposerHandle>(null);
  const setInput = useCallback((v: string) => composerRef.current?.setValue(v), []);
  const [loading, setLoading] = useState(false);
  const [isTyping, setIsTyping] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [abortCtrl, setAbortCtrl] = useState<AbortController | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [summarizing, setSummarizing] = useState(false);
  // Préférence de langue de réponse remontée par le sélecteur. Envoyée DANS chaque
  // requête : la préférence persistée du profil reste le repli côté serveur, mais
  // l'envoi explicite rend un changement effectif dès le message suivant.
  const [answerLanguage, setAnswerLanguage] = useState<AnswerLanguage | null>(null);

  // --- Analyses globales (L4) -------------------------------------------- //
  // Le backend est l'AUTORITÉ : `analysis_id` vient de la réponse chat ou de
  // l'endpoint du fil. On ne parse jamais le Markdown, on ne devine jamais
  // « le dernier job de l'utilisateur ».
  const [analyses, setAnalyses] = useState<Record<string, CorpusAnalysis>>({});
  const [cancelling, setCancelling] = useState<Record<string, boolean>>({});
  /** Choix de mode en cours d'envoi. Bloque les deux boutons — le double clic ne part pas. */
  const [choicePending, setChoicePending] = useState<CorpusChoice | null>(null);
  const pollers = useRef<Record<string, AnalysisPollHandle>>({});

  const upsertAnalysis = useCallback((a: CorpusAnalysis) => {
    setAnalyses((prev) => ({ ...prev, [a.analysis_id]: a }));
  }, []);

  /** Suit un job. Idempotent : jamais deux pollers pour le même analysis_id. */
  const trackAnalysis = useCallback(
    (a: CorpusAnalysis) => {
      upsertAnalysis(a);
      if (isTerminalAnalysisStatus(a.status)) return;
      if (pollers.current[a.analysis_id]) return;
      pollers.current[a.analysis_id] = pollAnalysis(a.analysis_id, (next) => {
        upsertAnalysis(next);
        if (isTerminalAnalysisStatus(next.status)) {
          delete pollers.current[next.analysis_id];
        }
      });
    },
    [upsertAnalysis],
  );

  const onCancelAnalysis = useCallback(
    async (id: string) => {
      if (cancelling[id]) return;                 // pas de double clic
      setCancelling((c) => ({ ...c, [id]: true }));
      try {
        const updated = await cancelCorpusAnalysis(id);
        upsertAnalysis(updated);                  // on continue à suivre jusqu'à cancelled
      } catch {
        setCancelling((c) => ({ ...c, [id]: false }));
      }
    },
    [cancelling, upsertAnalysis],
  );

  // Arrêt de TOUS les pollers au démontage.
  useEffect(() => {
    const active = pollers.current;
    return () => {
      Object.values(active).forEach((h) => h.stop());
      Object.keys(active).forEach((k) => delete active[k]);
    };
  }, []);
  const [activeCite, setActiveCite] = useState<{ msg: number | null; n: number | null }>({ msg: null, n: null });

  // Sources en tiroir sous 1200 px (tablette, mobile). En desktop le panneau
  // est une colonne permanente et cet état est sans effet.
  const isCompact = useMediaQuery(COMPACT_QUERY);
  const [sourcesOpen, setSourcesOpen] = useState(false);
  // Message dont le tiroir montre les sources quand on l'a ouvert depuis une
  // citation ; `null` = dernier message sourcé (comportement desktop).
  const [sourcesMsgIdx, setSourcesMsgIdx] = useState<number | null>(null);
  const sourcesPanelRef = useRef<HTMLElement>(null);
  const sourcesBackdropRef = useRef<HTMLDivElement>(null);
  const closeSources = useCallback(() => setSourcesOpen(false), []);
  useEffect(() => {
    if (!isCompact) setSourcesOpen(false);
  }, [isCompact]);
  // Un index de message ne vaut que dans le fil où il a été pris.
  useEffect(() => setSourcesMsgIdx(null), [threadId]);
  useModalLayer({
    active: sourcesOpen && isCompact,
    containerRef: sourcesPanelRef,
    keep: [sourcesBackdropRef],
    onClose: closeSources,
  });

  const scrollRef = useRef<HTMLDivElement>(null);
  const streamMetaRef = useRef<{ log_id?: string | null; thread_id?: string | null; request_id?: string | null }>({});
  /**
   * Verrou d'exclusion des soumissions. Une REF, pas un état : `loading` n'est
   * visible qu'au rendu suivant, donc deux appels du même tick le voient encore
   * à `false` et partent tous les deux. Il reste utilisé pour l'affichage.
   */
  const submittingRef = useRef(false);

  const canAsk = !!ragSpaceId && !loading && !summarizing;
  const indexing = currentStats?.indexing?.in_progress;

  const stopStreaming = useCallback(() => {
    setAbortCtrl((c) => {
      c?.abort();
      return null;
    });
    setLoading(false);
    setIsTyping(false);
    streamMetaRef.current = {};
  }, []);

  function ensureThreadId(id: string | undefined | null) {
    if (!id) return;
    setThreadId(id);
    if (ragSpaceId) setStoredThreadIdForRagSpace(ragSpaceId, id);
  }

  /* Mises à jour du message assistant en cours.
   *
   * Toutes passent par les transformations pures de `_lib/chatMessageState` et
   * visent un `local_id` : jamais « le dernier message », jamais une mutation en
   * place. C'est ce qui rend le flux correct quand React rejoue un updater
   * (StrictMode) — un delta ne peut plus être concaténé deux fois. */
  function applyDelta(localId: string, delta: string) {
    if (!delta) return;
    setMessages((m) => appendAssistantDelta(m, localId, delta));
  }
  function applySources(localId: string, sources: SourceV1[]) {
    setMessages((m) => setAssistantSources(m, localId, sources));
  }
  function markKind(localId: string, kind: NonNullable<Msg["kind"]>) {
    setMessages((m) => setAssistantKind(m, localId, kind));
  }
  const patchMessage = (localId: string, patch: Partial<Msg>) =>
    setMessages((m) => patchAssistant(m, localId, patch));

  /** Retire un message purement OPTIMISTE (jamais persisté côté serveur). */
  const dropLocalMessage = (localId: string) =>
    setMessages((m) => m.filter((x) => x.local_id !== localId));

  // Clic sur une suggestion de reformulation : recopie dans le composer pour
  // édition. N'envoie PAS (pas de double soumission) et conserve le RagSpace /
  // le thread courants.
  const onPickSuggestion = useCallback((s: string) => {
    setInput(s);
  }, [setInput]);

  const threadsRequestRef = useRef<string>("");
  const loadThreads = useCallback(async (rsId: string) => {
    // Dernier espace demandé : une réponse d'un espace QUITTÉ entre-temps (chargement
    // initial encore en vol pendant la bascule d'un lien direct) est ignorée — sinon
    // elle réécrirait la liste et sélectionnerait un fil de l'ancien espace.
    threadsRequestRef.current = rsId;
    const data = await apiFetch<ChatThread[]>(
      `/api/v1/chat/threads/?rag_space_id=${encodeURIComponent(rsId)}`
    ).catch(() => [] as ChatThread[]);
    if (threadsRequestRef.current !== rsId) return;
    const page = Array.isArray(data) ? data : [];
    threadCursorRef.current = page[page.length - 1]?.id ?? "";
    setHasMoreThreads(page.length >= THREADS_PAGE_SIZE);
    const stored = getStoredThreadIdForRagSpace(rsId);
    // Un fil ouvert par lien direct reste sélectionnable même s'il n'est pas parmi
    // les 100 plus récents : il a déjà été autorisé par le serveur (résolution).
    const linked = linkedThreadRef.current;
    const keepLinked =
      linked && linked.rag_space_id === rsId && linked.id === stored && !page.some((t) => t.id === stored);
    const list = keepLinked && linked ? [...page, linked] : page;
    setThreads(list);
    const next = stored && list.some((t) => t.id === stored) ? stored : list[0]?.id ?? "";
    setThreadId(next);
    if (next) setStoredThreadIdForRagSpace(rsId, next);
  }, []);

  /** Page suivante des fils (strictement plus anciens que le dernier chargé). */
  const loadMoreThreads = useCallback(async () => {
    if (!ragSpaceId || !threadCursorRef.current || loadingMoreThreads) return;
    setLoadingMoreThreads(true);
    const rsId = ragSpaceId;
    try {
      const data = await apiFetch<ChatThread[]>(
        `/api/v1/chat/threads/?rag_space_id=${encodeURIComponent(rsId)}&before=${encodeURIComponent(
          threadCursorRef.current
        )}`
      );
      if (threadsRequestRef.current !== rsId) return; // espace quitté pendant le chargement
      const page = Array.isArray(data) ? data : [];
      threadCursorRef.current = page[page.length - 1]?.id ?? threadCursorRef.current;
      setHasMoreThreads(page.length >= THREADS_PAGE_SIZE);
      setThreads((prev) => {
        const seen = new Set(prev.map((t) => t.id));
        return [...prev, ...page.filter((t) => !seen.has(t.id))];
      });
    } catch {
      setHasMoreThreads(false);
    } finally {
      setLoadingMoreThreads(false);
    }
  }, [ragSpaceId, loadingMoreThreads]);

  const loadHistory = useCallback(async (tid: string) => {
    setLoadingHistory(true);
    try {
      const data = await apiFetch<WireChatMessage[]>(`/api/v1/chat/threads/${tid}/messages/`);
      const server = mapServerMessages(data);
      const switching = loadedThreadRef.current !== tid;
      loadedThreadRef.current = tid;
      // Changement de fil : remplacement franc, les messages locaux appartiennent
      // à l'autre conversation. Même fil : réconciliation par identité, pour ne
      // pas effacer un message dont la requête est encore en vol.
      setMessages((local) => (switching ? server : reconcileChatMessages(local, server)));
    } finally {
      setLoadingHistory(false);
    }
  }, []);

  // Espace courant change → recharge les threads.
  useEffect(() => {
    stopStreaming();
    setMessages([]);
    setThreads([]);
    setThreadId("");
    setHasMoreThreads(false);
    threadCursorRef.current = "";
    setErr(null);
    if (ragSpaceId) loadThreads(ragSpaceId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ragSpaceId]);

  // Lien direct `?thread=<uuid>` : le serveur résout l'espace et le workspace du fil
  // (propriétaire + accès à l'espace revérifiés), puis on active ce contexte. Le
  // contenu est ensuite chargé par le chemin HABITUEL (`/messages/`) : aucun second
  // chemin de lecture. Refus ou id invalide : bandeau explicite, jamais de titre ni
  // de contenu du fil demandé, et la sélection courante est conservée.
  useEffect(() => {
    if (!threadParam || handledParamRef.current === threadParam) return;
    if (threadParam === threadId) {
      handledParamRef.current = threadParam;
      return;
    }
    if (!isUuid(threadParam)) {
      handledParamRef.current = threadParam;
      setLinkNotice(true);
      return;
    }
    // Attendre la liste des workspaces : `openTarget` en a besoin.
    if (workspaceId === null || workspaceId === undefined) return;
    const target = threadParam;
    handledParamRef.current = target;
    let alive = true;
    resolveThread(target)
      .then((r) => {
        if (!alive || !r || r.id !== target) return;
        linkedThreadRef.current = r;
        setLinkNotice(false);
        if (r.rag_space_id === ragSpaceId) {
          stopStreaming();
          setStoredThreadIdForRagSpace(r.rag_space_id, r.id);
          setThreads((prev) => (prev.some((t) => t.id === r.id) ? prev : [...prev, r]));
          setThreadId(r.id);
        } else if (!openTarget({ workspaceId: r.workspace_id, ragSpaceId: r.rag_space_id, threadId: r.id })) {
          setLinkNotice(true);
        }
      })
      .catch(() => {
        if (alive) setLinkNotice(true);
      });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [threadParam, workspaceId]);

  // URL synchronisée sur le fil courant (lien partageable, rafraîchissement, nouvel
  // onglet). `replaceState` : Next 14.2 l'intègre à son routeur, sans entrée
  // d'historique supplémentaire. Pas pendant la résolution d'un lien entrant.
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (threadParam && handledParamRef.current !== threadParam) return;
    const wanted = threadId ? threadHref(threadId) : "/chat";
    const current = window.location.pathname + window.location.search;
    if (window.location.pathname !== "/chat" || current === wanted) return;
    if (!threadId && !window.location.search.includes("thread=")) return;
    window.history.replaceState(window.history.state, "", wanted);
  }, [threadId, threadParam]);

  // Thread courant change → recharge l'historique (sauf pendant un stream).
  useEffect(() => {
    if (loading) return;
    if (!threadId) {
      setMessages([]);
      return;
    }
    loadHistory(threadId);

    // Reprise après rechargement : le lien fil -> job est PERSISTÉ côté serveur.
    // On relance le suivi des jobs encore actifs, on affiche les terminés, et on
    // ne recrée JAMAIS un job.
    getThreadAnalyses(threadId)
      .then((res) => (res?.analyses ?? []).forEach(trackAnalysis))
      .catch(() => {
        // 403/404 (accès perdu) : on masque tout ce qui était visible.
        Object.values(pollers.current).forEach((h) => h.stop());
        pollers.current = {};
        setAnalyses({});
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [threadId, loading]);

  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [messages, isTyping]);

  async function ask(q?: string, corpusChoice?: CorpusChoice) {
    // Un choix vient d'un BOUTON : il n'y a pas de texte à envoyer, et le libellé
    // affiché ici n'a aucune valeur d'autorité — le serveur écrit le sien.
    const text = corpusChoice
      ? t(CORPUS_CHOICE_LABEL_KEYS[corpusChoice])
      : (q ?? "").trim();
    if ((!text && !corpusChoice) || !ragSpaceId || loading || summarizing) return;

    // Verrou SYNCHRONE. `loading` est un état React : deux soumissions du même
    // tick (Entrée maintenue, clic + Entrée, double clic) le lisent toutes les
    // deux à `false` et partent en double — deux générations facturées, deux
    // couples de messages persistés. Une ref, elle, est écrite tout de suite.
    if (submittingRef.current) return;
    submittingRef.current = true;
    try {
      await runAsk(text, corpusChoice);
    } finally {
      submittingRef.current = false;
    }
  }

  async function runAsk(text: string, corpusChoice?: CorpusChoice) {
    if (!ragSpaceId) return;

    // Étape 3 : intercepter « résume la source 2 », « résume ce document »,
    // « résume X.pdf »… avant d'envoyer au RAG. Sinon, flux de chat normal.
    if (!corpusChoice && (await handleSummaryCommand(text))) return;

    setErr(null);
    if (!corpusChoice) setInput("");
    setActiveCite({ msg: null, n: null });
    if (corpusChoice) setChoicePending(corpusChoice);
    // Identité LOCALE stable : tous les événements de cette requête écriront
    // dans CE message, et pas dans « le dernier de la liste ».
    const localId = newLocalId();
    setMessages((m) => [
      // Le choix est fait : les boutons ne doivent plus jamais réapparaître sur ce
      // message, même si la requête échoue à mi-chemin (on les rétablit alors
      // explicitement dans le `catch`).
      ...(corpusChoice ? clearAwaitingCorpusChoice(m) : m),
      { role: "user", text, local_id: newLocalId() },
      { role: "assistant", text: "", local_id: localId },
    ]);
    setLoading(true);
    setIsTyping(true);

    const ctrl = new AbortController();
    setAbortCtrl(ctrl);
    streamMetaRef.current = {};

    let expired = false;
    let failed = false;

    try {
      await readSseStream({
        url: "/api/v1/chat/stream/",
        body: buildChatRequestBody({
          ragSpaceId,
          question: text,
          threadId,
          corpusChoice,
          answerLanguage,
        }),
        signal: ctrl.signal,
        onEvent: (eventName, data) => {
          if (eventName === "meta") {
            const tid = data?.thread_id ?? null;
            streamMetaRef.current = {
              ...streamMetaRef.current,
              thread_id: tid,
              log_id: data?.log_id ?? null,
              request_id: data?.request_id ?? streamMetaRef.current.request_id ?? null,
            };
            // Le SERVEUR décide si ce message attend un choix — jamais le Markdown.
            if (data?.awaiting_corpus_choice) {
              patchMessage(localId, {
                awaiting_corpus_choice: true,
                global_analysis_available: !!data?.global_analysis_available,
              });
            }
            ensureThreadId(tid);
          } else if (eventName === "token") {
            setIsTyping(false);
            applyDelta(localId, data?.delta ?? "");
          } else if (eventName === "sources") {
            applySources(localId, data?.sources ?? []);
          } else if (eventName === "done") {
            setIsTyping(false);
            const tid = data?.thread_id ?? streamMetaRef.current.thread_id ?? null;
            const lid = data?.log_id ?? streamMetaRef.current.log_id ?? null;
            if (tid) ensureThreadId(tid);
            // Réponse tronquée (rupture du flux après le premier fragment) :
            // le serveur l'annonce, l'interface ne doit pas la présenter comme
            // complète.
            if (data?.partial) patchMessage(localId, { partial: true });
            if (data?.status === "no_answer") markKind(localId, "no_answer");
            if (data?.status === "fallback_general") markKind(localId, "fallback_general");
            // ``done`` no_answer n'attend jamais les suggestions (F2) : elles sont
            // générées de façon ASYNCHRONE côté backend et récupérées par polling.
            const analysisId: string | null = data?.analysis_id ?? null;
            // `done.answer` est un SNAPSHOT canonique, jamais un delta : il
            // remplace le texte accumulé. Un ``done`` sans `answer` (aide, choix
            // de corpus, garde Excel, listing) laisse le texte streamé intact.
            if (typeof data?.answer === "string" && data.answer.length) {
              setMessages((m) => replaceAssistantText(m, localId, data.answer));
            }
            patchMessage(localId, {
              log_id: lid ?? null,
              // Contrat des sources : c'est le SERVEUR qui dit ce que le modèle a cité.
              // Il n'arrive qu'avec `done` — l'événement `sources` précède la génération.
              sources_contract: data?.sources_contract ?? null,
              suggestions: extractSuggestions(data), // vide sur done ; complété par le polling
              // L'identifiant vient du BACKEND — il n'est jamais extrait du texte.
              ...(analysisId ? { analysis_id: analysisId } : {}),
            });
            // Une analyse globale vient d'être lancée : on la suit.
            if (analysisId) {
              getCorpusAnalysis(analysisId).then(trackAnalysis).catch(() => {});
            }
            // Polling léger des suggestions (hors flux principal) — F2.
            if (data?.status === "no_answer" && tid) {
              const rid = streamMetaRef.current.request_id ?? null;
              void pollRephrasingSuggestions({
                fetcher: () => getRephrasingSuggestions(tid, rid ?? undefined),
                onReady: (suggestions) => {
                  if (!suggestions.length) return;
                  // Arrivée TARDIVE : le fil a pu avancer entre-temps. On vise le
                  // message de CETTE requête, et seulement s'il est resté une
                  // non-réponse.
                  setMessages((m) => {
                    const target = m.find((x) => x.local_id === localId);
                    if (!target || target.kind !== "no_answer") return m;
                    return patchAssistant(m, localId, { suggestions });
                  });
                },
              });
            }
          } else if (eventName === "error") {
            if (data?.code === "corpus_choice_expired") {
              // Le choix a déjà été consommé (double clic, autre onglet, fil avancé).
              // Rien n'a été exécuté côté serveur : on se resynchronise sur la base
              // plutôt que de rejouer quoi que ce soit.
              expired = true;
              setErr(t("choiceAlreadyTaken"));
            } else if (data?.detail === "ai_gateway_unavailable") {
              // indisponibilité nommée de l'inférence locale (503 côté API) — message
              // explicite, aucun repli, l'utilisateur peut réessayer.
              setErr(t("aiGatewayUnavailable"));
            } else if (data?.detail === "ai_gateway_error") {
              setErr(t("aiGatewayError"));
            } else {
              setErr(data?.detail || t("generationError"));
            }
          }
        },
      });
    } catch (e: any) {
      if (e?.name !== "AbortError") {
        setErr(e?.message || t("genericError"));
        // Erreur réseau AVANT toute confirmation du serveur : le choix n'a pas été
        // enregistré, donc les boutons doivent revenir — une nouvelle tentative
        // doit rester possible.
        if (corpusChoice) failed = true;
      }
    } finally {
      setLoading(false);
      setIsTyping(false);
      setAbortCtrl(null);
      setChoicePending(null);
      if (corpusChoice && (failed || expired)) {
        // On ne devine pas l'état : on le relit. Le serveur dira lui-même si un
        // choix est encore en attente.
        const tid = streamMetaRef.current.thread_id ?? threadId;
        if (tid) loadHistory(tid).catch(() => {});
      }
      if (ragSpaceId) loadThreads(ragSpaceId).catch(() => {});
    }
  }

  /**
   * `request_id` STABLE par cible (document + portée + page). Une relance de la
   * même demande — après un timeout côté client alors que le serveur a terminé —
   * réutilise donc l'identifiant, et le backend ré-ouvre le message existant au
   * lieu d'en créer un jumeau. C'est ce qui rend le retry réellement idempotent.
   */
  const summaryRequestIds = useRef<Record<string, string>>({});
  function requestIdForTarget(
    ref: SourceReference,
    scope: SummaryScope,
    page?: number
  ): string {
    // `sourceType` entre dans la clé : les identifiants privés et ceux du corpus
    // métier vivent dans deux espaces disjoints, et deux cibles distinctes ne
    // doivent jamais partager un request_id (le backend refuserait la seconde).
    const key = `${ref.sourceType}:${ref.documentId}:${scope}:${page ?? ""}`;
    if (!summaryRequestIds.current[key]) {
      summaryRequestIds.current[key] =
        typeof crypto !== "undefined" && "randomUUID" in crypto
          ? crypto.randomUUID()
          : `rq-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    }
    return summaryRequestIds.current[key];
  }

  // Cœur du résumé : pousse (message utilisateur + réponse en cours), appelle
  // l'API dédiée et injecte le résultat. Ne porte QUE sur le document/page ciblé.
  // Le résultat est PERSISTÉ côté serveur : l'affichage optimiste ci-dessous
  // n'est qu'une avance, réconciliée ensuite avec le fil rechargé.
  async function runDocumentSummary(opts: {
    ref: SourceReference;
    scope: SummaryScope;
    page?: number;
    userLabel: string;
  }) {
    if (summarizing || loading) return;
    if (!threadId) {
      // Sans fil, le backend ne peut rien persister. On le dit, plutôt que
      // d'afficher un résumé qui disparaîtrait au premier rechargement.
      setErr(t("summaryNeedThread"));
      return;
    }
    const requestId = requestIdForTarget(opts.ref, opts.scope, opts.page);
    setErr(null);
    setActiveCite({ msg: null, n: null });
    setSummarizing(true);
    setIsTyping(true);
    const localId = newLocalId();
    // Identifiant local du message UTILISATEUR optimiste : sans lui, on ne peut
    // pas le retirer si la demande échoue avant d'avoir été persistée dans le fil.
    const userLocalId = newLocalId();
    setMessages((m) => [
      ...m,
      {
        role: "user",
        text: opts.userLabel,
        request_id: requestId,
        local_id: userLocalId,
        category: "document_summary",
        status: "completed",
      },
      {
        role: "assistant",
        text: "",
        request_id: requestId,
        local_id: localId,
        category: "document_summary",
        status: "pending",
        document_id: opts.ref.documentId,
      },
    ]);
    try {
      const r = await summarizeSourceDocumentAwait(
        {
          sourceType: opts.ref.sourceType,
          documentId: opts.ref.documentId,
          // Espace CONSOMMATEUR : il porte la politique LLM d'un résumé de corpus
          // métier (le corpus n'en a pas). Le backend le confronte à sa garde
          // d'accès — il ne le croit pas sur parole.
          ragSpaceId: ragSpaceId || null,
          corpusId: opts.ref.sourceType === "domain_corpus" ? opts.ref.corpusId ?? null : null,
        },
        {
          scope: opts.scope,
          page: opts.scope === "page" ? opts.page : undefined,
          mode: "bullets",
          // Aucune langue forcée : le backend résout d'après les préférences du
          // compte (`preferred_answer_language`, puis `preferred_locale`).
          threadId,
          requestId,
        },
        {
          onPending: () => {
            setIsTyping(false);
            patchMessage(localId, { text: t("longDocSummarizing") });
          },
        }
      );
      if (r.status === "failed") {
        patchMessage(localId, { text: tErrors(summaryErrorKey(r.error || "")), kind: "error", status: "failed" });
      } else {
        const note = r.truncated ? t("partialSummaryNote") : "";
        patchMessage(localId, {
          text: (r.summary || "").trim() + note,
          log_id: r.log_id ?? null,
          id: r.message_id ?? undefined,
          status: "completed",
        });
      }
    } catch (e: any) {
      // Un refus survenu AVANT la création de l'échange (404 provenance/accès,
      // 400, 409) ne persiste rien côté serveur. Laisser le message utilisateur
      // optimiste afficherait une demande qui n'a jamais existé dans le fil — et
      // qui disparaîtrait au premier rechargement. On le retire tout de suite et
      // on ne garde que l'erreur. Si le serveur a bel et bien persisté l'échange,
      // le rechargement ci-dessous le rétablit depuis la source de vérité.
      dropLocalMessage(userLocalId);
      patchMessage(localId, { text: tErrors(summaryErrorKey(e?.message || "")), kind: "error", status: "failed" });
    } finally {
      setSummarizing(false);
      setIsTyping(false);
      // Resynchronisation sur la source de vérité : le fil rechargé porte le
      // message persistant, qui remplace la version optimiste sans doublon.
      loadHistory(threadId).catch(() => {});
    }
  }

  // Résumé déclenché depuis une carte source. Le panneau fournit une référence
  // DÉJÀ discriminée : cette fonction n'a plus à relire la provenance, donc plus
  // aucune occasion de l'oublier.
  function summarizeFromSource(ref: SourceReference, scope: SummaryScope) {
    const name = sourceNameFor(ref) || t("summaryFallbackDocName");
    const page = ref.pageNumber ?? null;
    const label =
      scope === "page" && page != null
        ? t("summarizePageLabel", { page, name })
        : t("summarizeDocumentLabel", { name });
    // Même verrou que le chat : un double clic sur « Résumer » ne doit pas
    // déclencher deux générations.
    if (submittingRef.current) return;
    submittingRef.current = true;
    void runDocumentSummary({ ref, scope, page: page ?? undefined, userLabel: label }).finally(
      () => {
        submittingRef.current = false;
      }
    );
  }

  /** Nom affichable d'une cible, retrouvé dans les sources du dernier message. */
  function sourceNameFor(ref: SourceReference): string | null {
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      const list = (messages[i].sources || []) as AnySource[];
      for (const s of list) {
        const r = srcReference(s);
        if (r && r.sourceType === ref.sourceType && r.documentId === ref.documentId) {
          return srcDoc(s);
        }
      }
    }
    return null;
  }

  // Affiche une demande de clarification (sans appel LLM) : message utilisateur + note.
  function pushClarification(userText: string, note: string) {
    setActiveCite({ msg: null, n: null });
    setMessages((m) => [
      ...m,
      { role: "user", text: userText, local_id: newLocalId() },
      { role: "assistant", text: note, local_id: newLocalId() },
    ]);
  }

  // Étape 3 — commande de résumé en langage naturel. Retourne true si la commande
  // a été prise en charge (résumé lancé ou clarification), false pour laisser le
  // chat normal répondre.
  async function handleSummaryCommand(text: string): Promise<boolean> {
    const intent = parseSummaryIntent(text);
    if (!intent.isSummary) return false;

    const lastSources = ([...messages].reverse().find((m) => m.role === "assistant" && m.sources && m.sources.length)
      ?.sources || []) as AnySource[];

    const r = resolveSummaryTargetFromSources(intent.ref, lastSources);

    if (r.status === "resolved") {
      setInput("");
      await runDocumentSummary({ ref: r.ref, scope: r.scope, page: r.page, userLabel: text });
      return true;
    }
    if (r.status === "need_clarification") {
      setInput("");
      pushClarification(text, r.message);
      return true;
    }
    if (r.status === "lookup_docs") {
      setInput("");
      if (!ragSpaceId) {
        pushClarification(text, t("docNotFoundNamed", { name: r.name }));
        return true;
      }
      let items: { id: string; filename: string; status: string }[] = [];
      try {
        items = (await listDocuments(ragSpaceId)).items;
      } catch {
        pushClarification(text, t("cannotCheckDocs"));
        return true;
      }
      const m = matchDocumentsByName(r.name, items);
      if (m.status === "resolved") {
        // Résolution par NOM : bornée aux documents PRIVÉS de l'espace
        // (`listDocuments`). Un nom de fichier ne porte aucune provenance, donc
        // la cible est explicitement privée — jamais devinée.
        await runDocumentSummary({
          ref: { sourceType: "workspace_document", documentId: m.documentId },
          scope: "document",
          userLabel: text,
        });
      } else if (m.status === "need_clarification") {
        pushClarification(
          text,
          t("multipleDocsMatch", { name: r.name }) + "\n" +
            m.candidates.map((c) => `• ${c}`).join("\n")
        );
      } else {
        pushClarification(text, t("docNotFoundNamed", { name: r.name }));
      }
      return true;
    }
    return false; // fallthrough → chat normal
  }

  async function onNewConversation() {
    if (!ragSpaceId) return;
    stopStreaming();
    try {
      const created = await apiFetch<ChatThread>("/api/v1/chat/threads/", {
        method: "POST",
        body: JSON.stringify({ rag_space_id: ragSpaceId }),
      });
      setThreads((t) => [created, ...t]);
      setThreadId(created.id);
      setStoredThreadIdForRagSpace(ragSpaceId, created.id);
      setMessages([]);
    } catch (e: any) {
      setErr(e?.message || t("cannotCreateConversation"));
    }
  }

  const onCite = (msgIdx: number, n: number) =>
    setActiveCite((c) => (c.msg === msgIdx && c.n === n ? { msg: null, n: null } : { msg: msgIdx, n }));

  // Citation [n] d'une réponse. Desktop : bascule de surbrillance (inchangé).
  // Tablette/mobile : sélectionne la source PUIS ouvre le tiroir sur elle —
  // une bascule refermerait la sélection qu'on vient demander à voir.
  const onCiteFromMessage = (msgIdx: number, n: number) => {
    if (!isCompact) return onCite(msgIdx, n);
    setActiveCite({ msg: msgIdx, n });
    setSourcesMsgIdx(msgIdx);
    setSourcesOpen(true);
  };

  const openSourcesFromButton = () => {
    setSourcesMsgIdx(null);
    setSourcesOpen(true);
  };

  // Callbacks des lignes `Message` : STABLES, pour que la ligne mémoïsée ne se
  // rerende pas à chaque jeton (une closure recréée par ligne et par rendu cassait
  // le memo). Ils délèguent à la version COURANTE des gestionnaires, qui lisent
  // l'état du rendu le plus récent (`isCompact`, fil courant, `loading`…) : jamais
  // celui du rendu où la ligne a été créée.
  const rowHandlers = useRef({ onCiteFromMessage, ask });
  rowHandlers.current = { onCiteFromMessage, ask };
  const onRowCite = useCallback((msgIdx: number, n: number) => rowHandlers.current.onCiteFromMessage(msgIdx, n), []);
  const onRowCorpusChoice = useCallback((c: CorpusChoice) => rowHandlers.current.ask(undefined, c), []);

  const lastWithSources = [...messages].reverse().find((m) => m.role === "assistant" && m.sources && m.sources.length);
  const lastWithSourcesIdx = lastWithSources ? messages.lastIndexOf(lastWithSources) : -1;
  const citedMsg = isCompact && sourcesMsgIdx !== null ? messages[sourcesMsgIdx] : undefined;
  const panelIdx = citedMsg?.sources?.length ? (sourcesMsgIdx as number) : lastWithSourcesIdx;
  const panelMsg = panelIdx >= 0 ? messages[panelIdx] : null;

  return (
    <>
      <Topbar
        title={t("topbarTitle")}
        subtitle={ragSpace ? t("spaceSubtitle", { name: ragSpace.name }) : undefined}
        actions={
          // `display: contents` : les deux groupes se placent dans la grille de la
          // Topbar — une ligne en desktop, deux en tablette/mobile (cf. ChatLayout.module.css).
          <div className={layout.topbarActions}>
            <div className={layout.toolbar}>
              <AnswerLanguageSelector onChange={setAnswerLanguage} />
              {threads.length > 0 && (
                <ThreadMenu
                  threads={threads}
                  threadId={threadId}
                  hasMore={hasMoreThreads}
                  loadingMore={loadingMoreThreads}
                  onLoadMore={() => void loadMoreThreads()}
                  onPick={(id) => {
                    stopStreaming();
                    setThreadId(id);
                    if (ragSpaceId) setStoredThreadIdForRagSpace(ragSpaceId, id);
                  }}
                />
              )}
              {shellNav && (
                <button
                  type="button"
                  onClick={() => shellNav.openPalette("conversations")}
                  aria-label={tNav("searchConversations")}
                  title={tNav("searchConversations")}
                  style={{
                    display: "inline-grid",
                    placeItems: "center",
                    width: 34,
                    height: 34,
                    borderRadius: 8,
                    border: "1px solid var(--border)",
                    background: "var(--surface)",
                    cursor: "pointer",
                    color: "var(--ink-2)",
                  }}
                >
                  <Icon name="search" size={16} />
                </button>
              )}
            </div>
            <div className={layout.primaryActions}>
              <Button
                variant="secondary"
                size="sm"
                icon="plus"
                onClick={onNewConversation}
                disabled={!ragSpaceId}
                className={layout.newConversation}
              >
                <span className={layout.newConversationLabel}>{t("newConversation")}</span>
              </Button>
              <button
                type="button"
                className={layout.sourcesButton}
                onClick={sourcesOpen ? closeSources : openSourcesFromButton}
                aria-expanded={sourcesOpen}
                aria-controls={SOURCES_PANEL_ID}
              >
                <Icon name="doc" size={17} />
                <span className={layout.sourcesLabel}>{t("sourcesTitle")}</span>
                {lastWithSources?.sources?.length ? (
                  <span className={layout.sourcesCount}>{lastWithSources.sources.length}</span>
                ) : null}
              </button>
            </div>
          </div>
        }
      />

      {/* Hauteur = ce que la Topbar laisse (colonne flex de la coquille `fill`),
          plus de constante de hauteur de header. */}
      <div className={layout.workspace}>
        {/* Colonne conversation */}
        <div className={layout.conversation}>
          <div ref={scrollRef} className={layout.thread}>
            <div className={layout.threadInner}>
              <GroundingBanner spaceName={ragSpace?.name} docCount={currentStats?.documents.total} indexing={!!indexing} />

              {linkNotice && (
                <div
                  role="alert"
                  data-testid="thread-link-notice"
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
                  <span style={{ flex: 1 }}>{tNav("threadNotAccessible")}</span>
                  <button
                    type="button"
                    onClick={() => setLinkNotice(false)}
                    aria-label={tNav("dismiss")}
                    style={{ border: "none", background: "transparent", cursor: "pointer", color: "inherit", display: "grid" }}
                  >
                    <Icon name="close" size={15} />
                  </button>
                </div>
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

              {messages.map((m, i) => (
                <Message
                  // Clé STABLE : avec l'index, le remplacement du fil par
                  // l'historique recyclait les nœuds au mauvais message.
                  key={messageKey(m, i)}
                  m={m}
                  index={i}
                  activeCite={activeCite.msg === i ? activeCite.n : null}
                  onCite={onRowCite}
                  onPickSuggestion={onPickSuggestion}
                  suggestionsDisabled={!canAsk}
                  analysis={m.analysis_id ? analyses[m.analysis_id] : undefined}
                  onCancelAnalysis={onCancelAnalysis}
                  analysisCancelling={m.analysis_id ? !!cancelling[m.analysis_id] : false}
                  onCorpusChoice={onRowCorpusChoice}
                  choicePending={choicePending}
                  choiceDisabled={!canAsk}
                />
              ))}
              {isTyping && <Thinking />}
            </div>
          </div>

          {/* Composer */}
          <div className={layout.composerBar}>
            <div className={layout.composerInner}>
              {messages.length === 0 && !loadingHistory && (
                <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 12 }}>
                  {SUGGESTION_KEYS.map((k) => {
                    const s = t(k);
                    return (
                    <button
                      key={k}
                      onClick={() => ask(s)}
                      disabled={!canAsk}
                      style={{
                        padding: "7px 13px",
                        borderRadius: 999,
                        border: "1px solid var(--border)",
                        background: "var(--surface)",
                        fontFamily: "inherit",
                        fontSize: 12.5,
                        color: "var(--ink-2)",
                        cursor: canAsk ? "pointer" : "default",
                        textAlign: "left",
                      }}
                      onMouseEnter={(e) => {
                        if (canAsk) {
                          e.currentTarget.style.borderColor = "var(--accent)";
                          e.currentTarget.style.color = "var(--accent-ink)";
                        }
                      }}
                      onMouseLeave={(e) => {
                        e.currentTarget.style.borderColor = "var(--border)";
                        e.currentTarget.style.color = "var(--ink-2)";
                      }}
                    >
                      {s}
                    </button>
                    );
                  })}
                </div>
              )}

              <DraftComposer
                ref={composerRef}
                onSend={(text) => ask(text)}
                onStop={stopStreaming}
                loading={loading}
                streaming={!!abortCtrl}
                canAsk={canAsk}
                placeholder={indexing ? t("composerPlaceholderIndexing") : t("composerPlaceholder")}
              />
              <div style={{ textAlign: "center", fontSize: 11.5, color: "var(--ink-3)", marginTop: 9 }}>
                {t("groundingNote")}
              </div>
            </div>
          </div>
        </div>

        {/* Panneau latéral des sources */}
        <div
          ref={sourcesBackdropRef}
          className={layout.sourcesBackdrop}
          data-open={sourcesOpen ? "true" : "false"}
          onClick={closeSources}
          aria-hidden="true"
        />
        <SourcesPanel
          ref={sourcesPanelRef}
          msg={panelMsg}
          activeN={activeCite.msg === panelIdx ? activeCite.n : null}
          onCite={(n) => panelIdx >= 0 && onCite(panelIdx, n)}
          onSummarize={summarizeFromSource}
          busy={summarizing}
          open={sourcesOpen}
          onClose={closeSources}
          revealActive={sourcesOpen && isCompact}
        />
      </div>
    </>
  );
}

/* ---------------- Bannière d'ancrage ---------------- */
function GroundingBanner({ spaceName, docCount, indexing }: { spaceName?: string; docCount?: number; indexing: boolean }) {
  const t = useTranslations("chat");
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 10,
        padding: "10px 14px",
        marginBottom: 22,
        borderRadius: 10,
        background: "var(--accent-soft)",
        border: "1px solid transparent",
      }}
    >
      <Icon name="shield" size={17} style={{ color: "var(--accent)" }} />
      <span style={{ fontSize: 12.5, color: "var(--accent-ink)", lineHeight: 1.4 }}>
        {t("groundingBasedOn")} <strong style={{ fontWeight: 700 }}>{spaceName || "—"}</strong>
        {docCount != null ? ` · ${t("groundingDocsCount", { count: docCount })}` : ""}
        {indexing ? ` · ${t("groundingIndexing")}` : ""}
      </span>
    </div>
  );
}

function CitationChip({ n, active, onCite }: { n: number; active: boolean; onCite: (n: number) => void }) {
  const t = useTranslations("chat");
  const [hover, setHover] = useState(false);
  const on = active || hover;
  return (
    <button
      onClick={() => onCite(n)}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      aria-label={t("highlightSource", { n })}
      className={layout.citationChip}
      style={{
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
        minWidth: 18,
        height: 18,
        padding: "0 5px",
        borderRadius: 5,
        border: "none",
        cursor: "pointer",
        fontFamily: "inherit",
        fontSize: 11,
        fontWeight: 700,
        lineHeight: 1,
        background: on ? "var(--accent)" : "var(--accent-soft)",
        color: on ? "#fff" : "var(--accent-ink)",
        transition: "background .12s, color .12s",
      }}
    >
      {n}
    </button>
  );
}

/* ---------------- Message ----------------
 * Mémoïsée : pendant un flux, seul l'objet du message streamé change, les autres
 * gardent leur référence (cf. `_lib/chatMessageState`). Toutes les props sont des
 * primitives, des objets d'état stables ou des callbacks stables (`onRowCite`,
 * `onRowCorpusChoice`, `useCallback`) : la comparaison superficielle par défaut
 * suffit, et une ligne se rerend dès qu'une de SES props change (sélection de
 * citation, analyse, `canAsk`…). */
const Message = React.memo(function Message({
  m,
  index,
  activeCite,
  onCite,
  onPickSuggestion,
  suggestionsDisabled = false,
  analysis,
  onCancelAnalysis,
  analysisCancelling = false,
  onCorpusChoice,
  choicePending = null,
  choiceDisabled = false,
}: {
  m: Msg;
  /** Position dans le fil : le callback stable la reçoit au clic. */
  index: number;
  activeCite: number | null;
  onCite: (msgIdx: number, n: number) => void;
  onPickSuggestion?: (s: string) => void;
  suggestionsDisabled?: boolean;
  analysis?: CorpusAnalysis;
  onCancelAnalysis?: (id: string) => void;
  analysisCancelling?: boolean;
  onCorpusChoice?: (choice: CorpusChoice) => void;
  choicePending?: CorpusChoice | null;
  choiceDisabled?: boolean;
}) {
  const t = useTranslations("chat");
  if (m.role === "user") {
    return (
      <div className={layout.messageRow} style={{ display: "flex", justifyContent: "flex-end", margin: "8px 0 22px" }}>
        <div
          style={{
            maxWidth: "78%",
            padding: "11px 16px",
            borderRadius: "16px 16px 4px 16px",
            background: "var(--accent)",
            color: "#fff",
            fontSize: 15,
            lineHeight: 1.55,
            whiteSpace: "pre-wrap",
          }}
        >
          {m.text}
        </div>
      </div>
    );
  }

  const noAnswer = m.kind === "no_answer";
  const fallback = m.kind === "fallback_general";
  const sources = (m.sources || []) as AnySource[];
  const sourcesDisplay = resolveSourcesDisplay({
    contract: m.sources_contract,
    text: m.text,
    sourcesCount: sources.length,
  });

  return (
    <div className={layout.messageRow} style={{ display: "flex", gap: 13, margin: "0 0 26px" }}>
      <div
        style={{
          width: 32,
          height: 32,
          borderRadius: 9,
          flex: "none",
          background: "var(--accent)",
          display: "grid",
          placeItems: "center",
          marginTop: 2,
        }}
      >
        <Icon name="sparkles" size={17} style={{ color: "#fff" }} />
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        {noAnswer ? (
          <>
            <NotFound />
            <RephrasingSuggestions
              suggestions={m.suggestions ?? []}
              onPick={(s) => onPickSuggestion?.(s)}
              disabled={suggestionsDisabled}
            />
          </>
        ) : (
          <>
            {/* Suggestions de reformulation hors « no_answer » : la garde analytique
                Excel (E4) en propose deux, sûres et documentaires. Un clic recopie
                dans le composer — jamais d'envoi automatique. */}
            {m.partial && (
              <div
                style={{
                  marginBottom: 12,
                  borderRadius: 10,
                  border: "1px solid #F0DDB0",
                  background: "var(--warning-soft)",
                  padding: "9px 12px",
                  fontSize: 12.5,
                  lineHeight: 1.45,
                  color: "#7A4E0A",
                }}
              >
                {t("partialAnswerNotice")}
              </div>
            )}
            {fallback && (
              <div
                style={{
                  marginBottom: 12,
                  borderRadius: 10,
                  border: "1px solid #F0DDB0",
                  background: "var(--warning-soft)",
                  padding: "9px 12px",
                  fontSize: 12.5,
                  lineHeight: 1.45,
                  color: "#7A4E0A",
                }}
              >
                {t("fallbackGeneralNotice")}
              </div>
            )}

            {/* Le rendu suit le `status` PERSISTÉ : un résumé en cours ou en
                échec se reconstruit donc depuis le seul historique serveur. */}
            <SummaryMessageBody m={m} />


            {/* Reformulations sûres proposées HORS « no_answer » — la garde analytique
                Excel (E4) en propose deux, purement documentaires. Un clic recopie dans
                le composer ; rien n'est renvoyé automatiquement. */}
            {(m.suggestions?.length ?? 0) > 0 && (
              <RephrasingSuggestions
                suggestions={m.suggestions ?? []}
                onPick={(s) => onPickSuggestion?.(s)}
                disabled={suggestionsDisabled}
              />
            )}

            {/* Le choix se prend ici, sous l'explication des deux modes — plus rien
                à recopier. C'est le SERVEUR qui dit si ce message l'attend encore. */}
            {m.awaiting_corpus_choice && onCorpusChoice && (
              <CorpusScopeChoice
                onChoose={onCorpusChoice}
                globalAnalysisAvailable={!!m.global_analysis_available}
                pending={choicePending}
                disabled={choiceDisabled}
              />
            )}

            {analysis && (
              <AnalysisCard
                analysis={analysis}
                onCancel={onCancelAnalysis}
                cancelling={analysisCancelling}
              />
            )}

            {/* « Citées » ≠ « consultées ». Le badge vert n'affirme que ce que le modèle a
                RÉELLEMENT cité : sous une abstention ou une réponse sans citation, on
                annonce des sources consultées, sans marque de validation. */}
            {sources.length > 0 && (
              <div style={{ display: "flex", alignItems: "center", gap: 8, margin: "2px 0 12px", flexWrap: "wrap" }}>
                <div style={{ display: "flex", alignItems: "center", gap: 7, fontSize: 12.5, color: "var(--ink-3)" }}>
                  {sourcesDisplay.kind === "cited" ? (
                    <>
                      <Icon name="checkCircle" size={15} style={{ color: "var(--success)" }} stroke={2} />
                      {t("sourcesCitedCount", { count: sourcesDisplay.ids.length })}
                    </>
                  ) : (
                    <>
                      <Icon name="search" size={15} style={{ color: "var(--ink-3)" }} stroke={2} />
                      {t("sourcesConsultedCount", { count: sources.length })}
                    </>
                  )}
                </div>
                <span style={{ display: "inline-flex", gap: 4, marginLeft: 4 }}>
                  {sourcesDisplay.ids.map((n) => (
                    <CitationChip key={n} n={n} active={activeCite === n} onCite={(c) => onCite(index, c)} />
                  ))}
                </span>
              </div>
            )}

            {/* Une réponse coupée par le plafond de génération reçoit quand même sa
                ligne « Sources: » et son badge de citations : rien ne la distingue
                d'une réponse finie. On le dit. `=== true` et non truthy : `null`
                signifie « inconnu », et l'inconnu ne doit rien afficher. */}
            {m.sources_contract?.answer_truncated === true && (
              <div
                role="status"
                style={{
                  display: "flex", alignItems: "center", gap: 7, margin: "0 0 12px",
                  fontSize: 12.5, color: "var(--warn-ink, var(--ink-2))",
                }}
              >
                <Icon name="alert" size={15} stroke={2} />
                {t("answerTruncated")}
              </div>
            )}

            {m.text.trim().length > 0 && <MessageActions text={m.text} logId={m.log_id ?? null} />}
          </>
        )}
      </div>
    </div>
  );
});

function NotFound() {
  const t = useTranslations("chat");
  return (
    <div
      style={{
        padding: "14px 16px",
        borderRadius: 12,
        background: "var(--warning-soft)",
        border: "1px solid #F0DDB0",
        display: "flex",
        gap: 11,
      }}
    >
      <Icon name="alert" size={19} style={{ color: "var(--warning)", flex: "none", marginTop: 1 }} />
      <div>
        <div style={{ fontSize: 14.5, fontWeight: 600, color: "#7A4E0A", marginBottom: 3 }}>{t("notFoundTitle")}</div>
        <p style={{ fontSize: 14, color: "#7A4E0A", opacity: 0.9, lineHeight: 1.55, margin: 0 }}>
          {t("notFoundBody")}
        </p>
      </div>
    </div>
  );
}

/* ---------------- Thinking ---------------- */
function Thinking() {
  const t = useTranslations("chat");
  return (
    <div style={{ display: "flex", gap: 13, margin: "0 0 26px" }}>
      <div style={{ width: 32, height: 32, borderRadius: 9, flex: "none", background: "var(--accent)", display: "grid", placeItems: "center" }}>
        <Icon name="sparkles" size={17} style={{ color: "#fff" }} />
      </div>
      <div style={{ paddingTop: 4 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13.5, color: "var(--ink-3)" }}>
          <span className="rg-dots">
            <span></span>
            <span></span>
            <span></span>
          </span>
          {t("retrieving")}
        </div>
      </div>
    </div>
  );
}


/* ---------------- Menu de conversations (threads) ---------------- */
function ThreadMenu({
  threads,
  threadId,
  onPick,
  hasMore = false,
  loadingMore = false,
  onLoadMore,
}: {
  threads: ChatThread[];
  threadId: string;
  onPick: (id: string) => void;
  hasMore?: boolean;
  loadingMore?: boolean;
  onLoadMore?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const btnRef = useRef<HTMLButtonElement>(null);
  const tr = useTranslations("chat");
  const tNav = useTranslations("navigationSearch");
  const cur = threads.find((t) => t.id === threadId);
  const label = (t?: ChatThread | null) => (t?.title?.trim() ? t.title : tr("untitledConversation"));
  return (
    <div className={layout.threadMenu} style={{ position: "relative" }}>
      <button
        ref={btnRef}
        className={layout.threadMenuButton}
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="listbox"
        aria-expanded={open}
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 7,
          padding: "7px 11px",
          borderRadius: 8,
          border: "1px solid var(--border)",
          background: "var(--surface)",
          cursor: "pointer",
          fontFamily: "inherit",
          fontSize: 13,
          fontWeight: 600,
          color: "var(--ink-2)",
          maxWidth: 220,
        }}
      >
        <Icon name="chat" size={15} style={{ color: "var(--ink-3)" }} />
        <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{label(cur)}</span>
        <Icon name="chevronD" size={14} style={{ color: "var(--ink-3)" }} />
      </button>
      <Popover
        open={open}
        onClose={() => setOpen(false)}
        anchorRef={btnRef}
        align="end"
        minWidth={260}
        maxHeight={360}
      >
        {threads.map((t) => {
          const active = t.id === threadId;
          return (
            <button
              key={t.id}
              role="option"
              aria-selected={active}
              onClick={() => {
                onPick(t.id);
                setOpen(false);
              }}
              style={{
                width: "100%",
                display: "flex",
                alignItems: "center",
                gap: 8,
                padding: "8px 9px",
                borderRadius: 8,
                border: "none",
                background: active ? "var(--accent-soft)" : "transparent",
                cursor: "pointer",
                fontFamily: "inherit",
                textAlign: "left",
              }}
            >
              <span
                style={{
                  flex: 1,
                  fontSize: 13,
                  color: "var(--ink)",
                  fontWeight: active ? 600 : 500,
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
              >
                {label(t)}
              </span>
              {active && <Icon name="check" size={15} style={{ color: "var(--accent)" }} stroke={2.4} />}
            </button>
          );
        })}
        {hasMore && onLoadMore && (
          <button
            type="button"
            onClick={onLoadMore}
            disabled={loadingMore}
            aria-busy={loadingMore}
            style={{
              width: "100%",
              padding: "8px 9px",
              borderRadius: 8,
              border: "none",
              background: "transparent",
              cursor: loadingMore ? "default" : "pointer",
              fontFamily: "inherit",
              fontSize: 13,
              fontWeight: 600,
              color: "var(--accent)",
              textAlign: "left",
            }}
          >
            {tNav("loadMoreThreads")}
          </button>
        )}
      </Popover>
    </div>
  );
}
