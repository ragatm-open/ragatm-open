# llm_router

## Emplacement
- Chemin (relatif racine repo) : `backend/apps/llm_router`
- Chemin (absolu) : `<racine du dépôt>`


## Rôle
Routage LLM : choisir entre LLM local et externe selon policy (privacy/coût).

## Responsabilités
- normaliser l’interface : prompt -> réponse + tokens + coût + latence
- appliquer politiques : forcer local, bloquer externe si budget/contraintes
- gérer secrets provider côté backend (jamais côté frontend)

## Contraintes RAG-ATM
- Router "bête" : pas de logique métier RAG.

