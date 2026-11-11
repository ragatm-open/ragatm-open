# chat/services

## Emplacement
- Chemin (relatif racine repo) : `backend/apps/chat/services`
- Chemin (absolu) : `<racine du dépôt>`


## Rôle
Services applicatifs du domaine chat.

## Contenu attendu
- `chat_service.py` : point d’entrée principal (use-case “répondre à un message”)

## Responsabilités typiques
- charger l’historique
- appeler `rag_core.services.query`
- appliquer policy (rerank conditionnel, 'je ne sais pas')
- appeler `llm_router`
- retourner : réponse + sources + métriques

