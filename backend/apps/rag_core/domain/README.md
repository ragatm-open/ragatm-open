# rag_core/domain

## Emplacement
- Chemin (relatif racine repo) : `backend/apps/rag_core/domain`
- Chemin (absolu) : `<racine du dépôt>`


## Rôle
Logique métier RAG pure (sans Django/DB).

## Contenu attendu
- stratégies retrieval (vector/hybrid)
- chunking (découpage + nettoyage)
- ranking/fusion (scoring, BM25+vector fusion si retenu)
- policies (quand rerank, quand dire "je ne sais pas")

## Conventions
- pas d’I/O (pas DB, pas réseau, pas filesystem)
- fonctions testables unitairement

