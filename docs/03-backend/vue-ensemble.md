# Backend — vue d'ensemble

Objectif : cartographier l'architecture backend de RagATM (couche Django/DRF, apps métier,
routage HTTP, réglages, garde-fous). Public : développeurs backend et intégrateurs API.

## Pile technique

- **Django + Django REST Framework (DRF)** — couche HTTP, sérialisation, permissions.
- **PostgreSQL + `pgvector`** — stockage relationnel + colonne vectorielle (dim **768**, figée).
- **Celery + Redis** — tâches asynchrones (ingestion, résumés, analyses, benchmark) ; Redis
  sert de broker et de backend de résultats. Voir [taches-asynchrones.md](taches-asynchrones.md).
- **AI Gateway** — passerelle IA obligatoire en environnement officiel pour toute
  génération LLM. Voir [../06-ai/ai-gateway.md](../06-ai/ai-gateway.md).

## Organisation des réglages (`backend/config/settings/`)

| Module | Rôle |
|---|---|
| `base.py` | Socle partagé : `INSTALLED_APPS`, middleware, DRF, Celery, tous les `RAG_*`/flags. Lit `.env` en dev. `DEBUG=False` par défaut. |
| `dev.py` | `from .base import *` ; `DEBUG=True`, redirection SSL désactivée. Module par défaut. |
| `prod.py` | `from .base import *` durci. Utilisé en **staging ET prod** (distingués par `DJANGO_ENV`). |
| `__init__.py` | Vide. |

Sélection : `dev` → `config.settings.dev` ; `staging` et `prod` → `config.settings.prod`.

### Garde-fous de démarrage

- **Guardrail `base.py`** : si `DJANGO_ENV ∈ {prod, production, staging}` alors que le module
  actif reste `base` (ou non défini), lève `RuntimeError` — empêche de booter la prod sur un
  `ALLOWED_HOSTS=["*"]` permissif.
- **`prod.py`** exige : `DJANGO_SECRET_KEY` non par défaut, `DJANGO_ALLOWED_HOSTS` non vide,
  `EMBEDDINGS_PROVIDER` réel (**refuse `stub`**), `FRONTEND_APP_ORIGIN` absolu. Force cookies
  sécurisés, HSTS, CSP, `SECURE_PROXY_SSL_HEADER`, throttling DRF (`120/min` user, `30/min`
  anon), `INVITE_RETURN_RESET_LINK=False`. `CSRF_COOKIE_HTTPONLY=False` (le JS doit lire le
  jeton CSRF pour le double-submit).

### System checks (échec au démarrage)

| Check | Portée | Règle |
|---|---|---|
| `rag_core.E001` / `E002` | Embeddings | `RAG_EMBEDDING_DIM` doit égaler la dimension pgvector figée (768). |
| `rag_core.E003` | Config dynamique | `CORE_CONFIG_RUNTIME_ENABLED=1` requiert `DJANGO_CACHE_URL`. |
| `llm_router.E001–E004`, `E009` (+ `W001/W002`) | Policy IA | Gateway obligatoire en env officiel ; pas de base URL loopback ; pas de `RAG_LLM_PROVIDER=ollama` direct ; provider de repli = `none` (E004) ; aucune variable de provider externe ni provider externe (E009). `ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS=1` rétrograde A/C en warnings. |

Voir [../06-ai/politiques-llm.md](../06-ai/politiques-llm.md) et [../10-reference/feature-flags.md](../10-reference/feature-flags.md).

## Applications Django

`INSTALLED_APPS` (partie locale, `base.py`) : `apps.accounts`, `apps.audit`, `apps.billing`,
`apps.chat`, `apps.corpus`, `apps.documents`, `apps.feedback`, `apps.llm_router`,
`apps.rag_core`, `apps.workspaces`.

| App | Rôle (une ligne) |
|---|---|
| `accounts` | Authentification cookie-token + permission propriétaire global (`IsRagATMOwner`). Aucun modèle. |
| `workspaces` | Racine de tenancy : `Workspace` + `Membership` (RBAC), primitives d'isolation. |
| `rag_core` | Moteur RAG : RagSpaces, documents, chunks pgvector, ingestion/retrieval, résumés, analyses, benchmark, façade Core, overlay de config. |
| `chat` | Couche conversationnelle : threads/messages, prompt templates, orchestration chat (sync + SSE), mémoire, suggestions de reformulation. |
| `corpus` | Corpus métier partagé (`domain_corpus_*`), admin superuser, retrieval bi-source. |
| `audit` | Journaux persistants : `RagQueryLog`, `LlmSettingsChangeLog`, `ActionAuditEvent` (append-only), agrégation de coûts, purge de rétention. |
| `feedback` | Signal utilisateur **durable** (`AnswerFeedback`, survit à la suppression de l'espace) + diagnostic **expirable** (`FeedbackDiagnosticSnapshot`), export d'incident approuvé/chiffré, replay en dev. → [feedback-incidents.md](feedback-incidents.md). |
| `llm_router` | Routage de génération (Gateway ; pas de repli externe), routage embeddings, capacités provider, system checks IA. Aucun modèle. |

**Hors `INSTALLED_APPS`** (packages Python simples, sans modèles/migrations, importés
directement) :

- `apps.actions` — routeur central phrase→action (derrière `ACTIONS_ROUTER_ENABLED`, off).
- `apps.help` — catalogue d'aide déterministe (`/help`), fonctions pures.

**Apps vides (placeholders)** : `apps.documents` et `apps.billing` sont enregistrées mais ne
contiennent que `apps.py`/`__init__.py`. La couche HTTP documents vit dans `api/v1/documents/`
(qui utilise les modèles `apps.rag_core`) — pas dans `apps/documents/`.

## Routage des URLs

`ROOT_URLCONF = config.urls` :

- `admin/` → admin Django
- `api/health/` → liveness JSON non authentifié
- `api/v1/` → `api.v1.urls`

Arbre sous `/api/v1/` :

| Préfixe | Cible | Notes |
|---|---|---|
| `chat/`, `chat/stream/`, `chat/threads/…`, `feedback/` | vues chat | RAG (sync + SSE), threads, feedback |
| `documents/…` | `api/v1/documents/` | liste, search, ingest, upload, delete, reindex, download, summary |
| `capabilities/` | llm_router | capacités provider + infos embeddings (sans secret) |
| `prompts/` | `api.v1.prompts.urls` | prompt templates |
| `auth/` | `api.v1.auth.urls` | login/logout/me/reset |
| `workspaces/` | `api.v1.workspaces.urls` | workspaces, membres, ragspaces, domain-corpora (lecture) |
| `rag-spaces/` | `api.v1.ragspaces.urls` | RagSpaces, llm-settings, reindex, analyses |
| `analyses/` | `api.v1.analyses.urls` | détail/annulation de jobs d'analyse |
| `bootstrap/` | `api.v1.bootstrap.urls` | init idempotente workspace + RagSpace |
| `audit/` | `api.v1.audit.urls` | rag-queries, llm-costs |
| `core/` | `api.v1.core.urls` | façade RagATM_Core (health/retrieve/status) |
| `admin/core-config/` | `api.v1.admin_core_config.urls` | diagnostic/édition config (superuser) |
| `admin/` | `api.v1.admin_corpus.urls` | admin corpus métier (superuser) |

Les vues sont câblées via **factories paresseuses** (import de la vue à l'intérieur de la
fonction) pour éviter les cycles d'import. `APPEND_SLASH=False`.

Détails endpoints : [../05-api/reference-endpoints.md](../05-api/reference-endpoints.md).

## Middleware

Ordre (`base.py`) : `SecurityMiddleware`, `RequestIdMiddleware` (`config.middleware.request_id`,
propage `X-Request-ID`), `SessionMiddleware`, `CommonMiddleware`, `CsrfViewMiddleware`,
`AuthenticationMiddleware`, `MessageMiddleware`, `XFrameOptionsMiddleware`.

## Authentification (résumé)

Jeton DRF (`rest_framework.authtoken`) transporté par **cookie HttpOnly** + protection **CSRF**
double-submit sur les méthodes non sûres. Le header `Authorization: Token <clé>` reste accepté
pour clients API/tests. Pas de JWT, pas d'auth de session API, pas d'inscription publique.
Détails et couches d'isolation : [permissions-isolation.md](permissions-isolation.md).

## Modèle de données

Entités, relations, contraintes et cycles de vie : [modele-donnees.md](modele-donnees.md).

## Moteur tabulaire déterministe (Excel / CSV)

Calcul exact sur classeur (`.xlsx`, `.csv`, `.tsv`) : plan structuré → SQL DuckDB
paramétré → résultat prouvé. Le LLM ne produit ni SQL, ni Python, ni le résultat.
**Activable par flag** — `TABULAR_QUERY_ENABLED` vaut `0` par défaut ; à `0`, le refus
historique `excel_guard` du chat est inchangé.

- Pipeline, plan, catalogue, modèle de données : [tabular-query-architecture.md](tabular-query-architecture.md)
- Frontière de confiance, cas adversariaux, limites : [tabular-query-security.md](tabular-query-security.md)
- Opérations, typage, ambiguïtés, provenance : [tabular-query-operations.md](tabular-query-operations.md)
- Activation et rollback : la documentation d'exploitation (hors périmètre de cette édition)
- Protocole de mesure : [../08-testing/tabular-query-benchmark.md](../08-testing/tabular-query-benchmark.md)

## Suppression d'un RagSpace

`DELETE /api/v1/rag-spaces/<id>/` est une **purge irréversible** (données + fichiers
physiques), pas un soft delete. Ordre, garanties, audit et limites :
[suppression-ragspace.md](suppression-ragspace.md).

## Références d'implémentation

- `backend/config/settings/base.py`, `backend/config/settings/dev.py`, `backend/config/settings/prod.py`
- `backend/config/urls.py`, `backend/api/v1/urls.py`
- `backend/config/celery.py`, `backend/config/middleware/request_id.py`
- `backend/apps/rag_core/apps.py`, `backend/apps/llm_router/apps.py`, `backend/apps/llm_router/checks.py`
- `backend/apps/accounts/authentication.py`, `backend/apps/accounts/permissions.py`
