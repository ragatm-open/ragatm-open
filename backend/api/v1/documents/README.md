# API v1 – Documents

## Emplacement
- Chemin (relatif racine repo) : `backend/api/v1/documents`
- Chemin (absolu) : `<racine du dépôt>`


## Rôle
Endpoints documents : upload, listing, suppression, statut d’ingestion.

## Flux attendu
1) upload + validations (type/taille)
2) stockage (selon stratégie)
3) ingestion via `rag_core.services.ingest`
4) indexation pgvector + métadonnées

