# rag_core

## Emplacement
- Chemin (relatif racine repo) : `backend/apps/rag_core`
- Chemin (absolu) : `<racine du dépôt>`


## Rôle
Cœur RAG : ingestion, chunking, indexation, retrieval, hybrid search, rerank conditionnel, politiques.

## Dossiers
- `domain/` : règles métier pures (sans I/O)
- `services/` : orchestration des cas d’usage
- `models/` : modèles Django (RagSpace, Document, Chunk…)
- `repositories/` : accès DB (pgvector, hybrid search)

## Contraintes RAG-ATM
- Multi-RAG étanche : **rag_space_id obligatoire partout**
- pgvector = vérité unique
- "je ne sais pas" si sources insuffisantes

