# rag_core/repositories

## Emplacement
- Chemin (relatif racine repo) : `backend/apps/rag_core/repositories`
- Chemin (absolu) : `<racine du dépôt>`


## Rôle
Accès aux données (Postgres + pgvector) et requêtes de retrieval.

## Contenu attendu
- fonctions requêtes vectorielles (cosine/L2 selon choix)
- hybrid search (texte + vecteur)
- filtres sécurité (rag_space_id, permissions)

## Conventions
- pas de logique métier (uniquement accès données)
- optimisation contrôlée (indexes, EXPLAIN)

