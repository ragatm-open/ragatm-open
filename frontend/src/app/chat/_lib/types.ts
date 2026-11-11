// frontend/src/app/chat/_lib/types.ts

import type { SourcesContract } from "./citations";

export type { SourcesContract };

export type WorkspaceListItem = { id: number; name: string; role: string };

export type RagSpaceListItem = {
  id: string;
  name: string;
  workspace_id: number;
  llm_provider?: string;
  llm_model?: string;
};

/** Type de source (contrat v2, corpus métier). Absent => document privé (fallback). */
// `unknown` n'est PAS un mode dégradé : c'est l'aveu explicite d'une provenance
// absente. Elle était auparavant affichée « Workspace », donc affirmée à tort.
export type SourceType = "workspace_document" | "domain_corpus" | "unknown";

/**
 * Métadonnées de CITATION portées par une source (contrat v3).
 *
 * Le backend projette `Chunk.meta` sur une allowlist : ce qui sert à citer, et rien
 * d'autre. Tous les champs sont optionnels — un TXT n'en a aucun, un PDF a sa page au
 * niveau racine, un classeur a sa feuille et sa plage ici, une présentation son numéro
 * de diapositive.
 *
 * `[key: string]: unknown` (et non `any`) : le contrat est ADDITIF côté backend, on
 * accepte donc des clés futures sans les typer — mais rien ne peut être déréférencé
 * sans vérification.
 */
export type SourceMeta = {
  /** `excel_table` | `excel_narrative` | `pptx_slide` | `pptx_notes` | `table` (P3) | absent. */
  kind?: string | null;
  sheet_name?: string | null;
  sheet_index?: number | null;
  /** `visible` | `hidden` | `veryHidden`. Une feuille masquée est indexée, mais dite. */
  sheet_state?: string | null;
  sheet_hidden?: boolean | null;
  cell_range?: string | null;
  /**
   * Plages RÉELLEMENT couvertes par le chunk. Un chunk découpé par colonnes (ligne
   * trop large) couvre une zone NON CONTIGUË : la colonne d'identification reportée
   * + une fenêtre de colonnes. `cell_range` n'en porte alors que la fenêtre.
   * Quand `data_ranges` est présent, c'est LUI qui fait foi pour le surlignage.
   */
  data_ranges?: string[] | null;
  identity_columns?: string[] | null;
  identity_range?: string | null;
  /** `"columns"` quand le chunk résulte d'un découpage horizontal. */
  chunk_axis?: string | null;
  column_part_index?: number | null;
  column_part_count?: number | null;
  row_start?: number | null;
  row_end?: number | null;
  column_start?: string | null;
  column_end?: string | null;
  header_row?: number | null;
  headers?: string[] | null;
  title?: string | null;
  chunk_of?: number | null;
  chunk_total?: number | null;
  has_formulas?: boolean | null;
  requires_visual_check?: boolean | null;
  /** PowerPoint — numéro HUMAIN de diapositive, à partir de 1. */
  slide_number?: number | null;
  slide_title?: string | null;
  has_images?: boolean | null;
  has_text?: boolean | null;
  /**
   * Formats structurés — repère de provenance GÉNÉRIQUE, sans page.
   * Chaque champ est optionnel ; `type` est technique et n'est jamais affiché.
   */
  locator?: SourceLocator | null;
  /**
   * Transcription audio — passage horodaté d'une transcription
   * automatique. `locator.type === "time_range"` porte l'intervalle lisible ;
   * `timestamp_precision === "part"` signale un horodatage approximatif (partie entière).
   */
  source_is_asr?: boolean | null;
  start_ms?: number | null;
  end_ms?: number | null;
  timestamp_precision?: "segment" | "part" | string | null;
  speaker_ids?: string[] | null;
  /**
   * Courriels — citation HIÉRARCHIQUE : courriel parent → pièce jointe →
   * repère interne (page, diapositive, feuille/plage, portés par leurs clés habituelles).
   */
  email_part?: "body" | "attachment" | string | null;
  email_subject?: string | null;
  email_nested_subject?: string | null;
  email_attachment_index?: number | null;
  email_attachment_path?: string | null;
  email_attachment_filename?: string | null;
  email_attachment_format?: string | null;
  email_nested_depth?: number | null;
  [key: string]: unknown;
};

/** Repère de provenance générique d'un document structuré (`meta.locator`). */
export type SourceLocator = {
  type?: string | null;
  path?: string | null;
  identifier?: string | null;
};

export type SourceV1 = {
  schema_version?: number;
  /** v2 : typage des sources. Optionnel pour rester compatible avec les anciens logs. */
  source_type?: SourceType | null;
  rank: number;
  document_id?: string | number | null;
  chunk_id?: string | number | null;
  score?: number | null;
  filename?: string | null;
  /**
   * PDF uniquement. **null pour un classeur ET pour une présentation** : ni une feuille
   * ni une diapositive n'est une page (cf. `meta.slide_number`).
   */
  page_number?: number | null;
  snippet?: string | null;
  /** v3 : feuille, plage, diapositive, titre… Absent sur les anciens messages. */
  meta?: SourceMeta | null;
  // Métadonnées corpus (présentes uniquement pour source_type === "domain_corpus").
  corpus_id?: string | null;
  corpus_name?: string | null;
  business_domain_id?: string | null;
  business_domain_name?: string | null;
};

export type MsgKind = "message" | "fallback_general" | "no_answer" | "error";

/**
 * Choix de mode pour une demande portant sur tout le corpus. Identifiants STABLES,
 * miroir de `apps.chat.services.corpus_mode_service.CHOICES` : le libellé affiché
 * peut changer sans rien casser, et rien ne dépend du Markdown rendu.
 */
export type CorpusChoice = "quick_response" | "global_analysis";

/**
 * Clés de traduction des libellés affichés (namespace `chat`). Le libellé rendu
 * est résolu par `useTranslations("chat")` au point d'affichage — on ne stocke
 * jamais la traduction finie. Le serveur écrit les siens dans le fil ; ils
 * coïncident.
 */
export const CORPUS_CHOICE_LABEL_KEYS: Record<CorpusChoice, string> = {
  quick_response: "corpusChoiceQuick",
  global_analysis: "corpusChoiceGlobal",
};

/**
 * Nature d'un message persistée par le BACKEND (colonne `kind`). Distincte de
 * `MsgKind`, qui décrit l'issue d'une réponse RAG et reste purement locale.
 * Vide/absent = message standard : c'est le cas de tout l'historique antérieur
 * à la migration chat 0007, qui doit continuer à s'afficher normalement.
 */
export type MsgCategory = "document_summary";

/** Cycle de vie des messages à génération différée. Vide/absent = déjà terminé. */
export type MsgStatus = "pending" | "completed" | "failed";

export type Msg = {
  /** Identifiant serveur. Absent tant que le message est purement optimiste. */
  id?: string;
  /**
   * Identifiant LOCAL, posé à la création optimiste et jamais envoyé au backend.
   *
   * C'est la cible des mises à jour du flux SSE : sans lui, chaque événement
   * écrivait dans « le dernier message », ce qui casse dès qu'une autre insertion
   * s'intercale ou qu'un événement tardif arrive. Il ne participe PAS à la
   * réconciliation avec l'historique (cf. `chatMessages.identity`) : un message
   * qui n'a que cet identifiant n'a jamais été persisté.
   */
  local_id?: string;
  /**
   * Corrélation demande -> message persisté. C'est la clé de réconciliation
   * entre l'affichage optimiste et l'historique : ni le texte ni la position
   * dans le tableau ne sont des identités fiables.
   */
  request_id?: string | null;
  /** Nature persistée (résumé de document…). Absent = message standard. */
  category?: MsgCategory | null;
  /** Statut persisté. Absent = message sans cycle de vie (chat classique). */
  status?: MsgStatus | null;
  /** Liens logiques vers le document résumé et le résumé lui-même. */
  document_id?: string | null;
  summary_id?: string | null;
  role: "user" | "assistant";
  text: string;
  kind?: MsgKind;
  created_at?: string;
  sources?: SourceV1[];
  /**
   * Contrat serveur des sources : retrouvées / transmises au modèle / réellement citées,
   * plus le statut d'abstention. Absent sur les messages relus depuis l'historique
   * (non persisté) : l'affichage retombe alors sur `_lib/citations`.
   */
  sources_contract?: SourcesContract | null;
  /**
   * Reformulations proposées quand le message est une non-réponse (kind
   * "no_answer"). Vide/absent sinon. Purement indicatif : un clic recopie la
   * suggestion dans le composer (pas d'envoi automatique).
   */
  suggestions?: string[];
  /**
   * Identifiant du RagQueryLog côté backend.
   * Sert à activer les pouces + associer le feedback à une réponse précise.
   */
  log_id?: string | null;
  /** Analyse globale lancée par ce message. Fourni par le BACKEND, jamais parsé du texte. */
  analysis_id?: string | null;
  /**
   * Ce message attend-il ENCORE un choix de mode ? Décidé par le SERVEUR, à
   * l'envoi (`meta`) comme à la relecture du fil. Jamais déduit du Markdown, jamais
   * conservé après consommation du choix.
   */
  /**
   * Réponse INTERROMPUE en cours de génération (flux rompu après le premier
   * fragment). Le texte déjà reçu est conservé — l'utilisateur doit savoir
   * qu'il est incomplet, sans quoi une réponse tronquée se lit comme une
   * réponse finie.
   */
  partial?: boolean;
  awaiting_corpus_choice?: boolean;
  /** Le moteur sait-il mener l'analyse globale ? Sinon, on n'offre pas le bouton. */
  global_analysis_available?: boolean;
};

/**
 * API response returned by:
 * - POST /api/v1/chat/query/
 * - SSE "done" event
 */
export type ChatResponse = {
  answer: string;
  sources: SourceV1[];
  thread_id: string | null;
  status?: "ok" | "fallback_general" | "no_answer" | "error";
  /**
   * Présent uniquement sur status "no_answer" (0 à 3 éléments). Rétrocompatible :
   * absent/[] pour les réponses valides et les erreurs.
   */
  suggested_rephrasings?: string[];
  /**
   * Optionnel selon impl backend (mais utile pour feedback).
   */
  log_id?: string | null;
};
