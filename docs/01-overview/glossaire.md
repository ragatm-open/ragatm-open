# Glossaire

Termes canoniques de RagATM. La documentation emploie **toujours** ces termes (pas de
traductions alternatives).

| Terme | Définition |
|---|---|
| **workspace** | Le tenant. Table `workspace`. Contient des memberships (RBAC) et des RagSpaces. |
| **membership** | Appartenance d'un utilisateur à un workspace, avec un rôle. |
| **rôle** | `owner` / `admin` / `member` / `viewer` (backend). Mappé côté produit en `admin` / `editeur` / `lecteur`. |
| **propriétaire global RagATM** | Utilisateur `is_superuser` ; permission `IsRagATMOwner`. Administre le corpus métier et le Core. Orthogonal aux rôles de workspace. |
| **RagSpace** | Unité d'isolation RAG. Table `rag_space`. Appartient à un workspace ; porte sa config LLM/embeddings. Sa suppression est une **purge irréversible** (données + fichiers). Toute requête RAG est filtrée par RagSpace. |
| **document** | Fichier téléversé dans un RagSpace. Table `rag_document`. Statuts `PENDING`/`INGESTING`/`READY`/`FAILED`. |
| **chunk** | Fragment de texte indexé avec son embedding (vecteur pgvector, dim 768). Table `rag_chunk`. |
| **corpus métier** | Corpus documentaire **partagé** (`domain_corpus_*`), géré par le propriétaire global. Accès workspace via `WorkspaceDomainCorpusAccess`. |
| **retrieval** | Récupération des passages pertinents (FTS + vectoriel + fusion), avant génération. |
| **FTS** | Full-Text Search PostgreSQL (`to_tsvector`/`to_tsquery`, score `ts_rank`). |
| **hybride** | Combinaison FTS + vectoriel, fusionnée par RRF. |
| **RRF** | Reciprocal Rank Fusion (K=60) : fusion par rang réciproque, pas par score brut. |
| **reranking** | Réordonnancement des candidats (étape 8). Désactivé par défaut (`stub` ou cross-encoder). |
| **answerability** | Décision « répondre ou non ». Live = seuil `should_answer` ; module IDF déterministe P1.8 non branché (offline). |
| **no-answer** | Réponse « Je ne sais pas » quand les sources sont insuffisantes (pool vide). |
| **embedding** | Vecteur d'un texte, produit par un provider (ollama-via-Gateway / stub). Dimension figée à 768. |
| **provider** | Fournisseur LLM ou embeddings : `ollama` (via Gateway), `stub` (tests). |
| **AI Gateway** | Passerelle IA (`http://ai-gateway:8100`), accessible en direct ou via AI Gateway Pool. Seul backend d'inférence ; jamais Ollama direct. |
| **repli (fallback)** | Édition ouverte : aucun provider externe, donc aucun repli (`none`). |
| **stub** | Embeddings pseudo-déterministes (sha256), escape hatch tests/CI. Refusé en prod. |
| **RagQueryLog** | Journal d'une requête RAG (statut, sources, coût, latence, feedback). 1 requête = 1 log. |
| **ActionAuditEvent** | Journal append-only des actions (upload/delete/reindex/ingestion). |
| **CorpusAnalysisJob** | Job asynchrone d'analyse corpus globale (queue `analytics`). |
| **Core / RagATM Core** | Façade interne (`apps.rag_core.core`) + overlay de configuration dynamique (inerte par défaut). |

## Voir aussi

- [produit.md](produit.md) — périmètre fonctionnel
- [../03-backend/modele-donnees.md](../03-backend/modele-donnees.md) — modèle de données détaillé
