# rag_core/services

## Emplacement
- Chemin (relatif racine repo) : `backend/apps/rag_core/services`
- Chemin (absolu) : `<racine du dépôt>`


## Rôle
Orchestration des cas d’usage RAG.

## Contenu attendu
- `ingest.py` : ingestion (texte, OCR sélectif, chunking, embeddings)
- `query.py` : recherche (hybrid + rerank conditionnel)
- `feedback.py` : feedback utilisateur (optionnel), logs qualité

## Conventions
- gère l’I/O en s’appuyant sur repositories et services externes (OCR/LLM)

