/**
 * Catalogue des modèles et pipelines — source UNIQUE côté frontend.
 *
 * Le composant ne tient plus sa propre liste. Il portait
 * une liste de moteurs codée en dur et deux champs de texte libre : la même
 * duplication qui avait fait servir `llama3.1:8b` en production alors que la
 * configuration visait `qwen3:30b-instruct`. Une liste recopiée se périme en silence.
 *
 * Le navigateur n'appelle JAMAIS la passerelle IA : il interroge RagATM, qui projette
 * le registre amont et y ajoute la gouvernance locale (allowed, qualified, recommended).
 */
import { apiFetch } from "@/lib/api";

/** Une option proposable, avec ce qui la rend proposable — ou non. */
export type CatalogEntry = {
  id: string;
  kind: "chat" | "embedding";
  display_name: string;
  available: boolean;
  allowed: boolean;
  qualified: boolean;
  recommended: boolean;
  capabilities: string[];
  registry_status: string;
  upstream_model: string;
  dimension?: number;
  /** Motif du refus quand `allowed` est faux — permet d'expliquer une absence. */
  reason?: string;
  /** Chat : compatibilité avec `/v1/generate` d'après le registre de la passerelle. */
  generation_status?: "supported" | "unsupported" | "unsafe_output" | "not_verified";
  qualification?: {
    qualification_id: string;
    qualified_at: string;
    environment: string;
    quality_reference: string;
    latency_reference: string;
    notes: string;
    source_lot: string;
  };
};

export type ModelCatalog = {
  chat: CatalogEntry[];
  embeddings: CatalogEntry[];
  available: boolean;
  stale: boolean;
  error?: string;
  qualification_revision: string;
  expected_embedding_dimension: number;
  recommended_embedding_reason?: string;
  can_edit_model_configuration?: boolean;
  /** Défaut de chat de l'environnement et son état (`available` ou code de refus). */
  default_chat_model?: string;
  default_chat_status?: string;
  recommended_chat_reason?: string;
  generation_registry_available?: boolean;
  /** Moteurs de chat sélectionnables pour un NOUVEL état (règle backend). */
  chat_providers?: Record<string, { selectable: boolean; reason: string }>;
};

/**
 * Le moteur `provider` peut-il être choisi ? Absent de la réponse => non (fail-closed),
 * sauf `ollama`, moteur historique toujours servi par la passerelle.
 */
export function chatProviderSelectable(catalog: ModelCatalog | null, provider: string): boolean {
  const p = catalog?.chat_providers?.[provider];
  return p ? !!p.selectable : provider === "ollama";
}

/** Provider technique d'embeddings du pipeline GOUVERNÉ. */
export const PROVIDER_AI_GATEWAY = "ai_gateway";

export async function fetchModelCatalog(): Promise<ModelCatalog> {
  return apiFetch<ModelCatalog>("/api/v1/ai/model-catalog/");
}

/**
 * Entrées réellement sélectionnables : `allowed` seulement (§7).
 *
 * Défensif sur la forme reçue : une réponse malformée ne doit pas casser l'écran, elle
 * doit produire une liste vide — donc un formulaire non modifiable, pas un formulaire
 * qui accepterait n'importe quoi.
 */
export function selectable(entries: CatalogEntry[] | undefined): CatalogEntry[] {
  return Array.isArray(entries) ? entries.filter((e) => e && e.allowed) : [];
}

export function findEntry(
  entries: CatalogEntry[] | undefined,
  id: string
): CatalogEntry | undefined {
  // Un modèle peut être désigné par son identifiant amont (`qwen2.5:1.5b-instruct`) ou par
  // son nom LOGIQUE du registre Gateway (`compact-generation-8`, porté par `display_name`) :
  // les deux désignent la même entrée (alias côté backend).
  return Array.isArray(entries)
    ? entries.find((e) => e && (e.id === id || (!!e.display_name && e.display_name === id)))
    : undefined;
}

/**
 * Le Space utilise-t-il un pipeline GOUVERNÉ ?
 *
 * La distinction n'est pas cosmétique : `ollama`/`nomic-embed-text` et le pipeline
 * `legacy-768` désignent les mêmes poids sans être le même objet. Le second porte une
 * identité vérifiée (digest, pipeline_id) et filtre la recherche dessus ; le premier
 * non. Les confondre dans l'interface reviendrait à annoncer une gouvernance absente.
 */
export function isGoverned(embeddingProvider: string | undefined): boolean {
  return (embeddingProvider || "").trim().toLowerCase() === PROVIDER_AI_GATEWAY;
}

/** Libellé du pipeline courant, gouverné ou historique. */
export function currentPipelineLabel(
  provider: string | undefined,
  model: string | undefined
): string {
  const m = (model || "").trim() || "—";
  return isGoverned(provider) ? m : `Legacy — ${m}`;
}
