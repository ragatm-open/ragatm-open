Apps Django (domaines) — Documentation consolidée

======================================================================
Emplacement
======================================================================

- Relatif : backend/apps
- Absolu  : <racine du dépôt>

======================================================================
Rôle
======================================================================

Découpage par domaines fonctionnels. Chaque app regroupe un périmètre métier clair,
avec des dépendances strictement unidirectionnelles.

======================================================================
Apps présentes
======================================================================

| App | Rôle |
|-----|------|
| accounts | Utilisateurs / authentification (hors périmètre actuel) |
| audit | Traçabilité et conformité (stub) |
| billing | Suivi des coûts LLM (stub) |
| chat | Orchestration conversationnelle |
| documents | Gestion documentaire (API uniquement) |
| llm_router | Embeddings et génération (stub local) |
| rag_core | Cœur RAG (ingestion, chunking, retrieval, policies) |

======================================================================
TABLEAU 1 — FONCTIONS
======================================================================

| Fichier | Emplacement | Fonction | Endpoints | Description synthétique | Inputs | Outputs |
|--------|-------------|----------|-----------|--------------------------|--------|---------|
| chat_service.py | apps/chat/services | chat_query | N/A | Orchestration question → RAG → génération | rag_space_id, question | dict (answer, sources) |
| embeddings.py | apps/llm_router | embed_texts | N/A | Génère des embeddings déterministes | texts, dim | list[list[float]] |
| router.py | apps/llm_router | generate_text | N/A | Génération de texte (LLM stub) | prompt | dict |
| policies.py | apps/rag_core/domain | should_answer | N/A | Décide si une réponse est autorisée | chunks, policy | bool |
| chunk_repository.py | apps/rag_core/repositories | search_chunks_hybrid | N/A | Recherche hybride full-text + vector | question, rag_space_id | list[Chunk] |
| chunk_repository.py | apps/rag_core/repositories | search_chunks_vector | N/A | Recherche vectorielle pure | embedding, rag_space_id | list[Chunk] |
| chunking.py | apps/rag_core/services | chunk_text_deterministic | N/A | Découpe déterministe d’un texte | text, spec | list[ChunkSpec] |
| ingest.py | apps/rag_core/services | ingest_plain_text | N/A | Wrapper ingestion texte brut | rag_space_id, text | document_id |
| ingest_document.py | apps/rag_core/services | ingest_document_from_text | N/A | Ingestion complète texte | rag_space_id, text | IngestResult |
| ingest_document.py | apps/rag_core/services | ingest_document_from_upload | N/A | Ingestion complète fichier | rag_space_id, file | IngestResult |
| query.py | apps/rag_core/services | query_rag | N/A | Orchestrateur RAG (retrieval + policy) | rag_space_id, question | dict |

======================================================================
TABLEAU 2 — CLASSES / DATACLASSES / MODELS
======================================================================

| Classe | Fichier | Emplacement | Endpoints | Rôle | Inputs principaux | Outputs |
|------|--------|-------------|-----------|------|------------------|---------|
| RagPolicy | policies.py | apps/rag_core/domain | N/A | Politique de décision RAG | seuils, flags | règle métier |
| RagSpace | rag_space.py | apps/rag_core/models | N/A | Espace RAG étanche | name | instance |
| Document | document.py | apps/rag_core/models | N/A | Document ingéré | rag_space, status | instance |
| Chunk | chunk.py | apps/rag_core/models | N/A | Unité de retrieval | text, embedding | instance |
| ChunkSpec | chunking.py | apps/rag_core/services | N/A | Spécification de chunking | size, overlap | spec |
| IngestResult | ingest_document.py | apps/rag_core/services | N/A | Résultat ingestion | document_id, status | résultat |
| IngestError | ingest_document.py | apps/rag_core/services | N/A | Erreur ingestion | message | exception |

======================================================================
Conventions globales
======================================================================

- Dépendances unidirectionnelles :
  - chat, documents → rag_core
  - rag_core n’appelle jamais chat/documents
- Aucune logique métier dans l’API HTTP
- Aucun accès DB hors repositories / services
- Règle stricte : réponse sans source suffisante interdite
- Fallback obligatoire : "Je ne sais pas."
