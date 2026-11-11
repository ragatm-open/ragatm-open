# API v1 – Chat

## Emplacement
- Chemin (relatif racine repo) : `backend/api/v1/chat`
- Chemin (absolu) : `<racine du dépôt>`


## Rôle
Endpoints chat : envoi d’un message, récupération d’une réponse et des sources.

## Contenu
- `views.py` : endpoints (REST)
- `serializers.py` : validation des inputs/outputs

## Flux attendu
1) validation input
2) appel `backend/apps/chat/services/`
3) récupération réponse + sources
4) réponse API (texte + citations + metadata)

