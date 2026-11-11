# API HTTP v1 — vue d'ensemble

Objectif : donner la carte générale de l'API REST de RagATM (base, conventions,
authentification, source OpenAPI) et orienter vers les références détaillées.
Public : intégrateurs, développeurs frontend, exploitants.

Le CODE est la source de vérité. Aucun endpoint de cette section n'est inventé :
tous proviennent de `backend/api/v1/urls.py` et de ses sous-modules.

## Base et périmètre

- Toutes les routes métier sont préfixées par **`/api/v1/`** (routées par
  `backend/api/v1/urls.py`).
- Deux routes hors de ce préfixe (dans `backend/config/urls.py`) :
  - `GET /api/health/` — liveness technique, **sans authentification**, renvoie
    `{"status": "ok"}`. La validation `ALLOWED_HOSTS` s'applique (en-tête `Host`
    autorisé requis).
  - `admin/` — interface d'administration Django (hors API JSON).
- En dev, l'API écoute en loopback sur le port **6811** (voir
  [ports](../10-reference/ports.md)). Les exemples utilisent
  `http://127.0.0.1:6811`.

## Conventions

- **Slash final obligatoire.** `APPEND_SLASH=False` (middleware) : une requête sans
  slash final n'est **pas** redirigée, elle échoue. Toujours terminer le chemin par
  `/` (ex. `/api/v1/workspaces/`, pas `/api/v1/workspaces`).
- **JSON** en entrée/sortie (`Content-Type: application/json`), sauf l'upload de
  fichier qui est `multipart/form-data`, et `/chat/stream/` qui répond en
  `text/event-stream` (SSE).
- **Corrélation de requête** : l'en-tête `X-Request-ID` (ou `x-request-id`) est lu
  en entrée, exposé comme `request.request_id`, et **renvoyé dans la réponse**
  (`config/middleware/request_id.py`). Il se propage jusqu'aux logs et aux
  `RagQueryLog`. Fournissez-le pour tracer une requête bout-en-bout ; sinon le
  serveur en génère un.
- **Pagination** : `limit`/`offset` en query string, à la main, propre à chaque
  endpoint listant (pas d'enveloppe de pagination commune).
- **Throttling** : actif **uniquement en prod/staging** (`config/settings/prod.py`),
  via DRF `UserRateThrottle`/`AnonRateThrottle`, défauts **120/min** (authentifié) /
  **30/min** (anonyme). Aucun throttle en dev.
- **Versioning** : seul `v1` existe (pas de négociation par `Accept`).

## Authentification (résumé)

Auth par **token DRF transporté dans un cookie HttpOnly** (`ragatm_auth_token`) +
**CSRF double-submit** sur les méthodes non sûres. Les clients API/tests peuvent
aussi passer `Authorization: Token <clé>` (sans CSRF). Pas de JWT, pas d'auth de
session API, **pas d'inscription publique**. Détail complet et exemples dans
[authentification.md](authentification.md).

## Carte des routes (par groupe)

Tous les groupes ci-dessous sont montés sous `/api/v1/`. Le détail méthode par
méthode (permissions, corps, curl) est dans
[reference-endpoints.md](reference-endpoints.md).

| Préfixe | Groupe | Rôle |
|---|---|---|
| `auth/` | Authentification | login, logout, me, reset de mot de passe |
| `bootstrap/` | Amorçage | garantit workspace + RagSpace par défaut (idempotent) |
| `capabilities/` | Capacités LLM | providers/modèles disponibles, infos embeddings |
| `workspaces/` | Workspaces (tenants) | liste, membres, RagSpaces, corpus métier associés |
| `rag-spaces/` | RagSpaces | CRUD partiel, stats/couverture, réglages LLM, reindex, analyses |
| `documents/` | Documents privés | liste, recherche, détail, ingest, upload, delete, reindex, download, résumé |
| `chat/` + `feedback/` | Chat | réponse RAG (sync + SSE), threads, messages, suggestions, feedback |
| `analyses/` | Analyses de corpus | détail et annulation d'un job d'analyse globale |
| `prompts/` | Prompt templates | versions de templates par workspace |
| `audit/` | Audit | logs de requêtes RAG, coûts LLM agrégés |
| `core/` | Façade RagATM_Core | health, retrieve (sans génération), statut document |
| `admin/core-config/` | Config dynamique (superuser) | config effective, health, audits, overrides |
| `admin/` | Admin corpus métier (superuser) | domaines, corpus, documents, associations workspace |

Notes de périmètre (cf. `CANON`) :

- Les apps Django `apps/documents/` et `apps/billing/` sont des **placeholders
  vides** : la couche HTTP « documents » vit dans `api/v1/documents/` (modèles
  `apps.rag_core`), et il n'existe **aucune** API de facturation.
- Le corpus métier n'a pas de préfixe dédié : son administration est sous `admin/`
  (superuser) et son accès en lecture côté workspace est
  `workspaces/<id>/domain-corpora/`.

## Source OpenAPI

- Spécification **modulaire, maintenue à la main** sous
  `shared/contracts/openapi/` (`root.yaml`, `paths/`, `components/`).
- Bundle en un fichier unique : `make openapi-bundle` (Redocly/`swagger-cli`) →
  `shared/contracts/openapi.yaml`.
- La spec n'est **pas** générée automatiquement depuis DRF : risque de
  désynchronisation avec le code. En cas de doute, **le code prévaut**.

## Explorer l'API

1. Amorcer un compte de dev (workspace + RagSpace) via `POST /api/v1/bootstrap/`
   après login.
2. Lister ses workspaces (`GET /api/v1/workspaces/`) puis ses RagSpaces
   (`GET /api/v1/rag-spaces/`).
3. Ingérer un document (`POST /api/v1/documents/upload/`) puis interroger
   (`POST /api/v1/chat/`).
4. Consulter le bundle OpenAPI pour les schémas de corps/réponse.

## Voir aussi

- [Authentification](authentification.md)
- [Référence des endpoints](reference-endpoints.md)
- [Contrats d'erreurs](contrats-erreurs.md)
- [Variables d'environnement](../10-reference/variables-environnement.md)
- [Ports](../10-reference/ports.md)
- Sécurité

## Références d'implémentation

- `backend/config/urls.py`
- `backend/api/v1/urls.py`
- `backend/config/middleware/request_id.py`
- `backend/config/settings/base.py`
- `backend/config/settings/prod.py`
- `shared/contracts/openapi/root.yaml`
- `Makefile` (cible `openapi-bundle`)
