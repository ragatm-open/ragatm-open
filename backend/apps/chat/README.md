# chat

## Emplacement
- Chemin (relatif racine repo) : `backend/apps/chat`
- Chemin (absolu) : `<racine du dépôt>`


## Rôle
Orchestration conversationnelle : assemblage contexte, appel RAG, appel LLM.

## Contenu
- `services/` : cas d’usage chat

## Conventions
- Le chat n’implémente pas le retrieval bas niveau : délègue à `rag_core`.
- Le chat ne parle pas directement à un provider LLM : passe par `llm_router`.

