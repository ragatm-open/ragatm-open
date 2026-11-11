# Produit — vue d'ensemble

RagATM est une plateforme RAG (Retrieval-Augmented Generation) multi-tenant. Elle permet à
des équipes d'interroger en langage naturel leurs propres documents, avec une isolation
stricte entre espaces et une génération IA fondée sur des sources citées.

Ce document décrit le **périmètre réellement implémenté** (le code fait foi). Les
fonctionnalités partielles ou non branchées sont signalées comme telles.

## Concepts clés

- **workspace** — le tenant. Contient des membres (RBAC : owner / admin / member / viewer)
  et des RagSpaces. Voir [glossaire](glossaire.md).
- **RagSpace** — l'unité d'isolation RAG. Chaque RagSpace a ses documents, ses chunks
  vectoriels, sa configuration LLM/embeddings. Toute requête est filtrée par RagSpace.
- **corpus métier** — corpus documentaire **partagé**, géré par le propriétaire global
  (superuser), auquel un workspace accède en lecture via une association explicite. Le
  retrieval est **bi-source** (documents privés du RagSpace + corpus métier autorisés).
- **AI Gateway** — passerelle IA obligatoire en environnement officiel : toute génération
  passe par elle (jamais Ollama en direct). Voir [../06-ai/ai-gateway.md](../06-ai/ai-gateway.md).

## Périmètre réel

| Domaine | Statut | Détail |
|---|---|---|
| Authentification | implémenté | Token DRF via cookie HttpOnly + CSRF ; pas de JWT, pas d'inscription publique |
| Multi-workspace + RBAC | implémenté | owner/admin/member/viewer ; isolation à 3 niveaux |
| RagSpaces isolés | implémenté | filtrage par `rag_space` sur chaque requête et chaque chunk |
| Corpus métier partagé | implémenté | modèles `domain_corpus_*`, admin superuser, retrieval bi-source |
| Ingestion multi-format | implémenté | TXT, Markdown, PDF, DOCX, CSV, HTML, images, XLSX |
| OCR local (tesseract) | implémenté, désactivé par défaut | PDF conditionnel ; images OCRisées systématiquement |
| Recherche hybride | activé par défaut | FTS (`ts_rank`) + vectoriel pgvector (cosine), fusion RRF |
| Reranking | activable par flag | `RAG_RERANK_ENABLED=0` par défaut (provider `stub` / cross-encoder) |
| Reranking via AI Gateway | activable par flag, **en service, non qualifié** | `RAG_RERANKER_ENABLED=0` par défaut ; **activé en staging ET en production** (constaté le 22/08/2026), sans qualification préalable — voir [../06-ai/reranking.md](../06-ai/reranking.md) §6 |
| No-answer (« Je ne sais pas ») | implémenté | seuil de score `should_answer` (`RAG_MIN_SCORE`) |
| Answerability déterministe (IDF) | benchmarké, **non branché** | outil d'évaluation offline (P1.8), pas dans le chemin live |
| Chat + streaming SSE | implémenté | streaming réel hors Gateway ; pseudo-stream quand Gateway active |
| Résumés de documents | implémenté | synchrone court / Celery map-reduce pour longs documents |
| Analyse corpus globale | implémenté | job asynchrone (`CorpusAnalysisJob`, queue `analytics`) |
| Suggestions de reformulation | activable par flag | `RAG_REPHRASING_SUGGESTIONS_ENABLED=0` |
| Routeur d'actions (phrase→action) | activable par flag | `ACTIONS_ROUTER_ENABLED=0` ; mutations jamais déclenchables par phrase |
| Coûts LLM | implémenté | `pricing.py`, `RagQueryLog.llm_cost_estimated`, endpoint `audit/llm-costs` |
| Audit des actions | implémenté | `ActionAuditEvent` append-only (upload/delete/reindex/ingestion) |
| Benchmark qualité RAG | implémenté | datasets versionnés, baseline, garde CI retrieval-only |
| Stockage documentaire | implémenté | volume local `media` (`MEDIA_ROOT`) ; stockage objet écarté — voir [ADR-001](decisions/ADR-001-stockage-documentaire.md) |
| Sauvegarde/restauration | implémenté, **non déployé** | `scripts/backup/` (PostgreSQL + médias + config chiffrée) ; restauration réelle prouvée ; timer systemd fourni mais non installé |

## Cas d'usage typiques

1. Un administrateur de workspace crée un RagSpace, y téléverse des documents, invite des
   membres.
2. Un membre pose une question dans l'assistant ; la réponse cite les passages sources.
3. Le propriétaire global publie un corpus métier partagé et l'associe à plusieurs workspaces.
4. Un lead suit les coûts LLM et l'audit des actions.

## Contraintes non négociables (rappel projet)

- Multi-RAG **étanche** (isolation stricte par espace).
- OCR **local sélectif** (confidentialité).
- Recherche hybride + rerank conditionnel.
- **IA via AI Gateway obligatoire** (`http://ai-gateway:8100`), jamais Ollama
  direct ; aucun provider externe.
- Réponse « je ne sais pas » si sources insuffisantes.

## Pour aller plus loin

- [architecture.md](architecture.md) — architecture logique et d'exécution.
- [structure-depot.md](structure-depot.md) — organisation du monorepo.
- [glossaire.md](glossaire.md) — terminologie canonique.
- [../02-getting-started/developpement-local.md](../02-getting-started/developpement-local.md) — lancer en local.
- [../03-backend/vue-ensemble.md](../03-backend/vue-ensemble.md) — backend Django.
- [../10-reference/limites-connues.md](../10-reference/limites-connues.md) — limites vérifiées.

## Références d'implémentation

- `backend/apps/` — apps Django (accounts, workspaces, rag_core, chat, corpus, audit, llm_router)
- `backend/config/settings/` — configuration et garde-fous
- `frontend/src/app/` — interface Next.js
