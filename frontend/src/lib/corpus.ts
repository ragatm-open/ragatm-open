// frontend/src/lib/corpus.ts
// Client API « corpus documentaire métier ».
// - lecture seule côté workspace (tout membre) ;
// - administration globale réservée au propriétaire global ragATM (backend = source de vérité).
import { apiDelete, apiFetch, apiGet, apiPatch, apiPost, apiUploadForm } from "./api";

// ----- Types -----
/**
 * Compteurs de documents d'un corpus.
 *
 * `documents_count` est le TOTAL et ne dit rien de l'exploitabilité : un corpus dont
 * tous les documents ont échoué à l'ingestion affiche le même nombre qu'un corpus
 * pleinement indexé. `ready_documents_count` applique la définition d'éligibilité du
 * retrieval — c'est le seul compteur qui répond à « combien le chat peut-il utiliser ? ».
 *
 * Optionnels : un backend antérieur à ce lot ne les renvoie pas.
 */
export type CorpusDocumentCounts = {
  documents_count: number;
  ready_documents_count?: number;
  processing_documents_count?: number;
  failed_documents_count?: number;
};

export type WorkspaceCorpus = {
  corpus_id: string;
  corpus_name: string;
  corpus_slug: string;
  description: string;
  business_domain_id: string;
  business_domain_name: string | null;
  is_active: boolean;
  read_only: boolean;
  managed_by: string;
  updated_at: string | null;
} & CorpusDocumentCounts;

export type BusinessDomain = {
  id: string;
  slug: string;
  name: string;
  description: string;
  is_active: boolean;
  corpora_count?: number;
  created_at?: string;
  updated_at?: string;
};

export type DomainCorpus = {
  id: string;
  domain_id: string;
  domain_name?: string | null;
  slug: string;
  name: string;
  description: string;
  is_active: boolean;
  embedding_provider: string;
  embedding_model: string;
  ready_documents_count?: number;
  processing_documents_count?: number;
  failed_documents_count?: number;
  documents_count?: number;
  created_at?: string;
  updated_at?: string;
  // État de cohérence des embeddings — CALCULÉ PAR LE SERVEUR. Jamais renvoyé au
  // serveur : une cohérence affirmée par le client serait invérifiable.
  embedding_dimension?: number;
  embedding_identity_complete?: boolean;
  embedding_consistency_status?: string;
  embedding_consistency_error_code?: string | null;
  embedding_consistent_document_count?: number;
  embedding_inconsistent_document_count?: number;
  embedding_unknown_document_count?: number;
  embedding_reindex_required_document_count?: number;
  embeddings_reindex_required?: boolean;
  vector_retrieval_available?: boolean;
  active_calibration_valid?: boolean;
  active_calibration_error_code?: string | null;
  active_calibration_version?: number | null;
};

export type CorpusDocument = {
  id: string;
  corpus_id: string;
  title: string;
  original_filename: string;
  mime_type: string;
  size_bytes: number;
  status: string;
  error_message: string;
  source_url: string;
  source_reference: string;
  version_label: string;
  chunks_count?: number;
  created_at?: string;
  updated_at?: string;
};

export type WorkspaceCorpusAccess = {
  id: string;
  workspace_id: number;
  corpus_id: string;
  corpus_name?: string;
  enabled: boolean;
  created_at?: string;
};

/**
 * Compteurs normalisés pour l'affichage.
 *
 * `ready` vaut `null` quand le backend ne fournit pas encore le compteur : on n'affiche
 * alors AUCUNE mention de documents prêts, plutôt que d'en inventer une. Ne jamais
 * dériver `ready` de `total` — c'est exactement la confusion que ce lot supprime.
 */
export type CorpusCountsSummary = {
  total: number;
  ready: number | null;
  processing: number;
  failed: number;
};

export function corpusCounts(c: CorpusDocumentCounts | null | undefined): CorpusCountsSummary {
  const num = (v: unknown): number | null =>
    typeof v === "number" && Number.isFinite(v) ? v : null;
  return {
    total: num(c?.documents_count) ?? 0,
    ready: num(c?.ready_documents_count),
    processing: num(c?.processing_documents_count) ?? 0,
    failed: num(c?.failed_documents_count) ?? 0,
  };
}

// ----- Lecture seule (workspace) -----
export function listWorkspaceCorpora(workspaceId: number): Promise<WorkspaceCorpus[]> {
  return apiGet<WorkspaceCorpus[]>(`/api/v1/workspaces/${workspaceId}/domain-corpora/`);
}

// ----- Admin global : domaines -----
export function listBusinessDomains(): Promise<BusinessDomain[]> {
  return apiGet<BusinessDomain[]>(`/api/v1/admin/business-domains/`);
}
export function createBusinessDomain(data: { slug: string; name: string; description?: string }): Promise<BusinessDomain> {
  return apiPost<BusinessDomain>(`/api/v1/admin/business-domains/`, data);
}
export function updateBusinessDomain(id: string, data: Partial<BusinessDomain>): Promise<BusinessDomain> {
  return apiPatch<BusinessDomain>(`/api/v1/admin/business-domains/${id}/`, data);
}
export function deleteBusinessDomain(id: string): Promise<null> {
  return apiDelete<null>(`/api/v1/admin/business-domains/${id}/`);
}

// ----- Admin global : corpus -----
export function listDomainCorpora(domainId?: string): Promise<DomainCorpus[]> {
  const qs = domainId ? `?domain_id=${encodeURIComponent(domainId)}` : "";
  return apiGet<DomainCorpus[]>(`/api/v1/admin/domain-corpora/${qs}`);
}
export function createDomainCorpus(data: {
  domain_id: string;
  slug: string;
  name: string;
  description?: string;
  embedding_provider?: string;
  embedding_model?: string;
}): Promise<DomainCorpus> {
  return apiPost<DomainCorpus>(`/api/v1/admin/domain-corpora/`, data);
}
export function updateDomainCorpus(id: string, data: Partial<DomainCorpus>): Promise<DomainCorpus> {
  return apiPatch<DomainCorpus>(`/api/v1/admin/domain-corpora/${id}/`, data);
}
export function deleteDomainCorpus(id: string): Promise<null> {
  return apiDelete<null>(`/api/v1/admin/domain-corpora/${id}/`);
}
export function reindexDomainCorpus(id: string): Promise<{ ok: boolean; reindexed: number; failed: number; skipped: number; total: number }> {
  return apiPost(`/api/v1/admin/domain-corpora/${id}/reindex/`, {});
}

// ----- Admin global : documents -----
export function listCorpusDocuments(corpusId: string): Promise<CorpusDocument[]> {
  return apiGet<CorpusDocument[]>(`/api/v1/admin/domain-corpora/${corpusId}/documents/`);
}
export function uploadCorpusDocument(corpusId: string, file: File): Promise<{ ok: boolean; deduped: boolean; document: CorpusDocument | null }> {
  const form = new FormData();
  form.append("file", file);
  return apiUploadForm(`/api/v1/admin/domain-corpora/${corpusId}/documents/`, form);
}
export function deleteCorpusDocument(documentId: string): Promise<null> {
  return apiDelete<null>(`/api/v1/admin/domain-corpus-documents/${documentId}/`);
}
export function reindexCorpusDocument(documentId: string): Promise<{ ok: boolean; document: CorpusDocument }> {
  return apiPost(`/api/v1/admin/domain-corpus-documents/${documentId}/reindex/`, {});
}

// ----- Admin global : associations workspace <-> corpus -----
export function listWorkspaceCorpusAccess(workspaceId: number): Promise<WorkspaceCorpusAccess[]> {
  return apiGet<WorkspaceCorpusAccess[]>(`/api/v1/admin/workspaces/${workspaceId}/domain-corpora/`);
}
export function assignCorpusToWorkspace(workspaceId: number, corpusId: string, enabled = true): Promise<WorkspaceCorpusAccess> {
  return apiPost<WorkspaceCorpusAccess>(`/api/v1/admin/workspaces/${workspaceId}/domain-corpora/`, {
    corpus_id: corpusId,
    enabled,
  });
}
export function removeCorpusFromWorkspace(workspaceId: number, corpusId: string): Promise<null> {
  return apiDelete<null>(`/api/v1/admin/workspaces/${workspaceId}/domain-corpora/${corpusId}/`);
}
