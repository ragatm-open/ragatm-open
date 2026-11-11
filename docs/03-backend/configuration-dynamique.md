# Configuration dynamique (RagATM Core)

Objectif : décrire l'overlay de configuration Core de RagATM — une couche d'overrides stockée en
base, **diagnostique et inerte par défaut**, activable/éditable uniquement sous flags stricts.
Public : administrateurs RagATM (superuser), développeurs backend.

## Principe

RagATM lit sa configuration depuis les settings Django / variables d'environnement. L'overlay Core
ajoute la possibilité de **superposer** des overrides par portée, stockés en base
(`CoreConfigOverride`). Tant qu'aucun override n'existe, le résolveur retombe **intégralement** sur
les settings/env — comportement strictement inchangé.

Deux résolveurs distincts :

- **Diagnostic** (`config_resolver.resolve_effective_config`) : calcule la config effective
  affichée (redactée) pour l'admin. Toujours disponible.
- **Runtime** (`core/runtime_config.py`) : renvoie la vraie valeur typée destinée à l'exécution.
  **Inerte tant que `CORE_CONFIG_RUNTIME_ENABLED=0`** — il renvoie alors strictement les
  settings, sans accès base ni cache.

### Qui consomme réellement le résolveur runtime

Le résolveur **est branché** depuis PR-G3. La documentation antérieure — et le docstring du
module — affirmaient qu'il ne l'était « nulle part » : c'était faux, et cela sous-estimait le
périmètre d'impact d'un override.

| Module | Clés lues | Ce qui est en jeu |
|---|---|---|
| `apps/llm_router/router.py` | activation, repli, provider de repli, provider, modèle | génération |
| `apps/llm_router/embeddings.py` | activation, URL, timeout | **embeddings, donc indexation — y compris dans les workers Celery** |
| `apps/llm_router/egress.py` | repli activé, provider de repli | plafond d'egress externe |

Conséquence : avec le flag runtime ouvert, une ligne d'overlay ne change pas seulement le
chat — elle peut changer le routage de l'**indexation**.

## Durcissement : un override ne peut pas contredire les system checks

Les system checks (`llm_router.E001..E005`, `rag_core.E001/E003`, `config.E00x`) valident les
**settings statiques** ; ils ne lisent pas l'overlay. Un override autorisé pouvait donc
produire une configuration que le démarrage aurait refusée — sans qu'aucun check ne le voie.

Le cas le plus grave, corrigé : un override `AI_GATEWAY_ENABLED=false` désactivait la Gateway
au runtime, et génération **comme embeddings** basculaient sur un appel **Ollama direct** —
précisément ce que la politique IA interdit.

Trois mesures, appliquées ensemble :

1. **Les clés d'infrastructure sortent du périmètre éditable** du registre :
   `AI_GATEWAY_ENABLED`, `AI_GATEWAY_BASE_URL`, `AI_GATEWAY_PROVIDER`, `AI_GATEWAY_MODEL`,
   `AI_GATEWAY_FALLBACK_PROVIDER`, `RAG_EMBEDDING_DIM`. Elles restent **visibles** dans
   l'Admin Core (l'exploitant doit voir l'état réel), mais ne sont plus modifiables. Une
   ligne préexistante en base est **neutralisée** à la résolution.
2. **Validation à l'écriture** (`config_write.set_override`) : un override qui violerait un
   invariant est refusé, rien n'est persisté. On ne découvre plus une valeur invalide au
   moment de s'en servir.
3. **Validation à la résolution** (`config_policy.guard_runtime_value`, appelée par
   `runtime_config`) : dernier rempart avant l'exécution. Une valeur fautive n'est jamais
   retournée — on retombe sur les settings en journalisant
   (`core_config_overlay_rejected`). Aucune exception au runtime : une requête utilisateur
   ou une tâche d'ingestion ne casse pas à cause d'un override.

Les deux validations ne sont pas redondantes : une ligne peut préexister au durcissement ou
être posée par écriture SQL directe.

### Périmètre éditable après durcissement

| Clé | Portée max | Invariant appliqué |
|---|---|---|
| `AI_GATEWAY_TIMEOUT_SECONDS` | environment | bornes 1–300 |
| `AI_GATEWAY_FALLBACK_ENABLED` | environment | activer le repli exige `AI_GATEWAY_FALLBACK_PROVIDER=none` — aucun fournisseur de repli n'existe (miroir de `llm_router.E004`) |
| `WORKSPACE_DEFAULT_MAX_DOCUMENT_COUNT` | global | bornes |
| `WORKSPACE_DEFAULT_MAX_DOCUMENT_BYTES` | global | bornes |

Élargir ce périmètre est verrouillé par un test : c'est un geste conscient, pas un effet de
bord.

## Modèles

- `CoreConfigOverride` (`core_config_override`) — un override par (`scope_type`, `scope_ref`,
  `key`). `scope_type` ∈ {global, environment, workspace, ragspace} ; `scope_ref` est une
  CharField libre (`""` pour global, nom d'env, id workspace ou uuid ragspace en str) ; `value`
  en texte, coercée par type selon le registre. Contrainte
  **unique(`scope_type`, `scope_ref`, `key`)**.
- `CoreConfigAuditEvent` (`core_config_audit_event`) — journal **append-only** des actions
  (`set | unset | rollback | test_gateway`). `save`/`delete` bloqués au niveau modèle. Les valeurs
  sensibles sont **redactées à la lecture** via l'API, jamais stockées en clair sensible.

## Flags de gouvernance

| Flag | Défaut | Effet |
|---|---|---|
| `CORE_CONFIG_EDIT_ENABLED` | off | Autorise l'écriture d'overrides via l'API admin. **Staging uniquement — jamais en prod** (l'endpoint renvoie `403` en prod). |
| `CORE_CONFIG_RUNTIME_ENABLED` | off | Branche l'overlay au runtime. Requiert `DJANGO_CACHE_URL` (sinon **inerte** et échec du system check `rag_core.E003` au démarrage). |

- `RAG_EMBEDDING_DIM` est **exclu en dur** de l'overlay runtime : toujours la valeur settings, quel
  que soit le flag ou un override.
- Toute erreur cache/DB au runtime → fallback settings/default, jamais d'exception.
- L'invalidation est cohérente HTTP + workers via une clé d'epoch partagée en cache (incrémentée à
  chaque écriture), avec TTL de secours.

## System check

`rag_core.E003` : `CORE_CONFIG_RUNTIME_ENABLED=1` **requiert** `DJANGO_CACHE_URL` — sinon échec au
démarrage. Voir [vue-ensemble.md](vue-ensemble.md).

## Endpoints admin (`/api/v1/admin/core-config/`)

Tous protégés par `[IsAuthenticated, IsRagATMOwner]` (superuser). Les verbes HTTP sont restreints
explicitement (pas de PATCH/DELETE accidentel).

| Méthode | Chemin | Notes |
|---|---|---|
| GET | `effective/` | Config effective (env + overlay optionnel), redactée, lecture seule. |
| GET | `health/` | Santé Core/Gateway (sans appel réseau). |
| GET | `audit/` | Historique `LlmSettingsChangeLog`. |
| GET | `config-audit/` | Historique `CoreConfigAuditEvent` (redacté). |
| PUT | `override/` | Pose un override — **gardé** : `403` en prod, `409` sauf si `CORE_CONFIG_EDIT_ENABLED` (staging). |
| POST | `override/unset/` | Retire un override — mêmes gardes. |

L'effet **runtime** n'existe que si `CORE_CONFIG_RUNTIME_ENABLED` ; sinon ces overrides ne changent
que le **diagnostic** affiché.

Détails : [../05-api/reference-endpoints.md](../05-api/reference-endpoints.md).

### Champs d'une entrée de `effective/`

Les champs historiques (`key`, `effective_value`, `source`, `editable`, `sensitive`, `redacted`,
`description`) sont **inchangés**. Les champs suivants ont été ajoutés pour distinguer ce qui était
jusqu'ici confondu — notamment « un override existe » et « un override s'applique » :

| Champ | Signification |
|---|---|
| `domain` / `value_type` | Domaine fonctionnel et type déclarés au registre canonique. |
| `default_value` | Défaut du code (`null` pour une clé sensible). |
| `settings_value` | Valeur issue des settings/`.env`, avant overlay. |
| `override_value` / `override_scope` | Valeur de l'override retenu et sa portée, ou `null`. |
| `effective_source` | `settings` ou `db:<portée>` — quelle source a gagné. |
| `runtime_editable` / `max_scope` | Clé éditable par overlay, et portée maximale autorisée. |
| `runtime_active` | **L'override est-il réellement appliqué au runtime ?** `false` tant que `CORE_CONFIG_RUNTIME_ENABLED=0`, et **toujours** `false` pour `RAG_EMBEDDING_DIM`. |
| `restart_required` | Un redémarrage est nécessaire pour appliquer un changement. |
| `display_value` | Rendu sûr : `••••••••` pour un secret, `http://…:8100` pour une URL privée. |

Les métadonnées (type, défaut, bornes, portée, sensibilité, description) ne sont plus déclarées
dans le résolveur : elles proviennent du **registre canonique**
[`backend/config/registry/`](../../backend/config/registry/), documenté dans
[../10-reference/configuration.md](../10-reference/configuration.md).

## Statut

- Overlay : **inerte tant que `CORE_CONFIG_RUNTIME_ENABLED=0`** (le résolveur renvoie alors
  strictement les settings). Le résolveur est en revanche **branché** — génération,
  embeddings, egress : l'ouverture du flag a donc un effet réel et large.
- Édition : **activable par flag**, staging uniquement. Périmètre éditable **réduit** aux
  cinq clés du tableau ci-dessus ; les clés d'infrastructure ne sont plus pilotables.
- Durcissement : **implémenté** (validation à l'écriture ET à la résolution).

Voir [../10-reference/feature-flags.md](../10-reference/feature-flags.md) et
[../10-reference/variables-environnement.md](../10-reference/variables-environnement.md).

## Références d'implémentation

- `backend/apps/rag_core/models/core_config.py`
- `backend/apps/rag_core/core/` (`runtime_config.py`, `config_resolver.py`, `config_write.py`,
  `config_policy.py`)
- `backend/apps/rag_core/apps.py` (check `rag_core.E003`)
- `backend/api/v1/admin_core_config/`
- `backend/config/registry/` (registre canonique : métadonnées, audit `.env`, génération doc)
- `backend/config/parsers.py`, `backend/config/checks.py`, `backend/config/settings/from_registry.py`
