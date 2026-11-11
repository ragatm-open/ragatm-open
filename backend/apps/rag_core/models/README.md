# rag_core/models

## Emplacement
- Chemin (relatif racine repo) : `backend/apps/rag_core/models`
- Chemin (absolu) : `<racine du dépôt>`


## Rôle
Modèles Django liés au RAG.

## Modèles typiques (à implémenter)
- RagSpace : espace RAG étanche (multi-RAG)
- Document : métadonnées + état ingestion
- Chunk : texte + embedding + provenance (doc/page/offset)

## Contraintes
- Filtrage rag_space systématique
- Index pgvector + indexes texte (pour hybrid)

