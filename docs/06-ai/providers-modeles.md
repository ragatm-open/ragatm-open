# Providers et modèles — routage LLM

Objectif : décrire le routeur de provider (`apps/llm_router/router.py`) : priorité Gateway,
absence de repli externe, retry « réponse vide », sélection par RagSpace, routage par tâche, system checks
et coût. Public : développeurs backend, exploitants.

Le routeur est le point de passage unique de toute **génération** LLM. La policy Gateway est
décrite dans [ai-gateway.md](ai-gateway.md) ; ce document en détaille la mécanique de routage.

---

## 1. Priorité de provider (`generate_text`)

1. **Si `AI_GATEWAY_ENABLED=1`** → chemin **AI Gateway** (`_generate_via_gateway`), seul chemin de
   génération en exploitation. En cas d'échec, l'erreur Gateway typée remonte (§3).
2. **Sinon** (`ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS=1`) → chemin **direct** (`_generate_text_direct`) :
   Ollama local. Ce chemin est réservé au développement et aux tests, et signalé par les checks.

Statut : **implémenté**.

### Chemin Gateway

- Provider/modèle envoyés : override explicite de l'appelant > `AI_GATEWAY_PROVIDER` (défaut
  `ollama`, c'est-à-dire le modèle local servi par la Gateway) / `AI_GATEWAY_MODEL` (défaut `compact-generation-8`, nom logique du registre Gateway Open).
- ⚠️ Pour une question posée dans un RagSpace, « l'appelant » c'est le RagSpace : son
  `llm_model` persisté est envoyé à la Gateway. `AI_GATEWAY_MODEL` n'intervient donc
  **que si le champ est vide**. Hiérarchie complète en [§5.2](#52--préséance-des-modèles-qui-décide-vraiment).
- Client `gateway/client.py::AIGatewayClient.generate` → `POST {AI_GATEWAY_BASE_URL}/v1/generate`.
  Garde-fous : aucun appel réseau si `enabled=False` (`GatewayDisabledError`) ; base URL vide →
  `GatewayConfigError` ; prompt blanc rejeté avant réseau ; HTTP ≥400 / JSON invalide →
  `GatewayResponseError` ; timeout → `GatewayTimeoutError`.
- Payload (`gateway/dto.py`) : `prompt`, `stream=False`, et si présents `provider`, `model`,
  `options`, `request_id`, `system`, `think` → contrat v2.0.0 (plus riche que le service in-repo
  v0.1.0, cf. [ai-gateway.md](ai-gateway.md) §2).

---

## 2. Retry « réponse vide » (chemin Gateway) — **implémenté**

- Si HTTP 200 mais **texte blanc** → **un seul** retry, même modèle/prompt/provider,
  `think=False`, `num_predict = max(initial, 1024)`. Deuxième vide → `LLMError`.
- **Plancher 1024, pas de plafond.** Un budget absent ou trop petit est relevé à 1024 — c'est la
  raison d'être du mécanisme, une réponse vide venant souvent d'un budget trop court pour produire
  quoi que ce soit après le raisonnement interne. Un budget explicite **n'est jamais abaissé** : le
  plafond historique de 2048 abaissait une valeur que l'appelant avait choisie et que la Gateway
  venait d'accepter (le retry ne suit qu'un HTTP 200). Le maximum admissible reste l'affaire de la
  **Gateway**, qui refuse en amont ce qui dépasse le profil du modèle.
- **Coût assumé** : le pire cas d'un retry vide n'est plus borné à 2048 tokens mais vaut le budget
  de l'appelant — au plus 3072 aujourd'hui, sur un GPU partagé avec la production.
- `retry_on_empty=False` (ex. suggestions de reformulation) désactive ce retry.
- **Ne s'applique jamais aux erreurs** (timeout / 4xx / 5xx / JSON invalide) : celles-ci suivent
  le fallback (§3).

---

## 3. Pas de repli externe — **implémenté**

RagATM Open n'a **aucun provider de génération externe**. `AI_GATEWAY_FALLBACK_ENABLED` vaut `0`
par défaut et `AI_GATEWAY_FALLBACK_PROVIDER` n'accepte que `none` : en cas d'échec Gateway,
l'erreur typée remonte (`GatewayUnavailableError` ou erreur Gateway), sans repli. Toute autre
valeur est refusée au démarrage (`llm_router.E004`), de même que toute variable d'environnement de
provider externe (`config.E009`) ou valeur de provider externe (`llm_router.E009`). Le repli n'est **jamais** Ollama direct.
`allow_external_fallback` (champ de RagSpace) est conservé mais n'a **aucun effet**.

```
AI_GATEWAY_ENABLED=1
  └─ POST AI_GATEWAY_BASE_URL/v1/generate
       ├─ OK (texte non vide) → réponse Gateway
       ├─ 200 vide            → 1 retry borné (think=False) → sinon LLMError
       └─ erreur              → erreur Gateway typée (pas de repli)
AI_GATEWAY_ENABLED=0 → chemin direct Ollama (dev / tests uniquement, signalé par les checks)
```

---

## 4. Chemin direct — développement et tests uniquement

| Provider | Endpoint | Notes |
|---|---|---|
| `ollama` | `POST {OLLAMA_BASE_URL}/api/generate` | `stream=False`, options `num_ctx/temperature/top_p/repeat_penalty` |

Tout autre provider → `LLMError("Unknown provider")`. Pas de « stub » côté génération (le stub
n'existe que pour les embeddings, cf. [embeddings.md](embeddings.md)).

- `_default_provider()` : `RAG_LLM_PROVIDER` > `DEFAULT_LLM_PROVIDER` (alias) > `"ollama"`.
- Défaut modèle : `DEFAULT_OLLAMA_MODEL` / `OLLAMA_DEFAULT_MODEL` / `llama3:latest`.

---

## 5. Sélection par RagSpace

La génération chat lit le provider/modèle **par espace**
(`normalize_llm_overrides`, `apps/chat/services/chat_formatting.py`) :

1. `rag_space.llm_provider` / `rag_space.llm_model` si non vides ;
2. sinon `RAG_LLM_PROVIDER` / `RAG_LLM_MODEL` ;
3. sinon défaut du provider (`llama3:latest`).

Champs modèle (`apps/rag_core/models/rag_space.py`) : `llm_provider` (choice unique `ollama` = modèle local servi
par la Gateway, **défaut `ollama`**), `llm_model` (défaut `compact-generation-8`), `allow_external_fallback` (défaut
**False**).

> **Le moteur `ollama` signifie « modèle local servi par l'AI Gateway »** : c'est le seul moteur.
> La *policy opérationnelle* impose la Gateway ; le chemin Ollama direct n'existe que pour le
> développement et les tests.

### 5.1 Verrouillage de la config modèles — superuser only — **implémenté**

Le choix du **provider et du modèle** (chat + embeddings) d'un RagSpace est réservé au
**superuser actif** ; tout autre utilisateur (owner, admin, manager, editor, member, viewer)
reçoit des **valeurs canoniques imposées par le backend** (jamais une simple désactivation
d'input). Règle **toujours active**, indépendante des flags owner-scope / ACL / core-config.

- **Valeurs canoniques** (settings surchargeables env, source unique
  `apps/llm_router/model_policy.py`) : `DEFAULT_RAGSPACE_CHAT_PROVIDER=ollama`,
  `DEFAULT_RAGSPACE_CHAT_MODEL=compact-generation-8` (nom LOGIQUE du registre AI Gateway Open — profil Compact CPU / 8 Go, décision ; le profil 12 Go `compact-generation` se configure explicitement, jamais par repli
  modèle pour le profil Compact), `DEFAULT_RAGSPACE_EMBEDDING_PROVIDER=ai_gateway`,
  `DEFAULT_RAGSPACE_EMBEDDING_MODEL=fr-en-retrieval` (profil FR/EN, 768 dimensions ; `legacy-768` reste admis pour les index existants).
- **Prédicat** : `can_manage_ragspace_models(user) = is_ragatm_owner(user)` (authentifié **actif
  superuser** ; le statut staff seul ne suffit pas).
- **Création** (`POST /api/v1/workspaces/<id>/ragspaces/`) : non-superuser → valeurs canoniques
  imposées ; une valeur **différente** dans le payload → **403** `ragspace_model_configuration_forbidden`
  (jamais appliquée). Superuser → choix libre (validations providers/modèles existantes).
- **Modification** (`PATCH /api/v1/rag-spaces/<id>/`) : les 4 champs modèle ne sont modifiables que
  par un superuser ; un non-superuser qui tente de **changer** un champ → **403** ; une valeur
  **identique** est ignorée. La **configuration historique** d'un espace (ex. `llama3.1:8b`) n'est
  **jamais réécrite** par un PATCH de nom, et reste **consultable** par tous, **modifiable** par le
  seul superuser.
- **Frontend** : chaque réponse RagSpace porte `can_edit_model_configuration` (par utilisateur,
  backend-authoritative) ; `/api/v1/capabilities/` expose `ragspace_model_policy` (valeurs +
  `can_edit`). L'écran de création/édition affiche les modèles en **lecture seule** pour un
  non-superuser (bandeau « Configuration gérée par l'administrateur plateforme »), sans soumettre
  ces champs.
- **Audit** : `ragspace.model_configuration_denied` (tentative interdite, `fields_attempted`) et
  `ragspace.model_configuration_changed` (avant/après, superuser) — sans secret ni contenu.
- **Historiques / autres chemins** : aucune migration ni normalisation automatique des espaces
  existants. Les chemins internes (bootstrap, seeds) ne prennent pas d'entrée utilisateur pour ces
  champs. Django admin reste réservé au superuser Django.

### 5.1bis Administration plateforme hors périmètre owner — **implémenté**

Sous `OWNER_SCOPE_ENABLED=1`, deux gardes indépendantes se superposent :

| Garde | Décide de | Portée |
|---|---|---|
| politique modèles | qui peut changer provider/modèle | superuser actif, **globale** |
| périmètre owner | qui peut atteindre CET espace | détenteur du scope, **par espace** |

Leur intersection peut être **vide** : un espace dont le périmètre appartient à un
compte non-superuser n'était administrable par personne via l'API. La capacité existait
quand même — par `set_ragspace_embedding_pipeline`, en shell, **sans audit applicatif**.

Refuser l'API n'ajoutait donc aucune sécurité : un superuser a déjà l'accès base et le
shell. Cela déplaçait l'opération vers un chemin non tracé — la friction sans la
garantie.

**Décision** : un superuser actif peut administrer la configuration IA de tout espace,
et cette élévation est **strictement bornée**.

| Autorisé en mode élevé | Refusé (403 `platform_admin_scope_exceeded`) |
|---|---|
| `llm_provider`, `llm_model` | `name` — acte métier |
| `embedding_provider`, `embedding_model` | `allow_external_fallback` — champ conservé, sans effet |
| lecture de `llm-settings` (config + compteurs) | tout accès au contenu documentaire |

Le superuser ne devient pas propriétaire métier de l'espace. Toute élévation effective
émet `ragspace.model_configuration_elevated` (acteur, espace, champs, motif).

---

### 5.2  Préséance des modèles : qui décide vraiment — **implémenté**

Quatre endroits déclarent un modèle. Un seul gagne, et ce n'est pas celui qu'on croit.

| Rang | Source | Portée |
|---|---|---|
| **1** | `RagSpace.llm_model` **persisté** | ce que la Gateway reçoit réellement pour une question posée dans cet espace |
| **2** | `DEFAULT_RAGSPACE_CHAT_MODEL` | valeur écrite dans `llm_model` **à la création** d'un espace |
| **3** | `AI_GATEWAY_MODEL` | repli, **uniquement** si `llm_model` est vide |
| **4** | `RAG_TASK_MODEL_*` | override par tâche, si `RAG_TASK_ROUTING_ENABLED=1` (§6) |

Conséquence pratique : **régler `AI_GATEWAY_MODEL` ne change pas le modèle qui répond**
tant que les espaces portent un `llm_model`. Constaté en production le 27/07/2026 :
`AI_GATEWAY_MODEL=qwen3:30b-instruct` et `rag_query_log.llm_model` = `llama3.1:8b`.

Pour changer le modèle servi :

- **nouveaux espaces** → `DEFAULT_RAGSPACE_CHAT_MODEL` (par déploiement) ;
- **espace existant** → `PATCH /api/v1/rag-spaces/<id>/` (superuser), qui journalise
  l'avant/après dans `llm_settings_change_log` et `ActionAuditEvent`.

**Aucune valeur de modèle n'est codée en dur** hors de ces settings. Le défaut du champ
Django est un *callable* qui les lit (`apps/llm_router/model_defaults.py`), et le
formulaire frontend s'initialise depuis `/api/v1/capabilities/`. Trois gardes le
verrouillent : `test_ragspace_default_model_parity.py` (parité des parcours et suivi de
la configuration), `SpaceEditModalNoHardcodedModel.test.ts` (aucun littéral dans le
composant), et un contrôle que les défauts de champ sont bien des callables.

> **Historique du défaut.** Le formulaire portait `llm_model: "llama3.1:8b"`. Le champ
> étant prérempli, il ENVOYAIT cette valeur — le backend la recevait comme un choix
> utilisateur et l'appliquait fidèlement. En parallèle, un superuser qui n'envoyait rien
> retombait sur le défaut littéral du champ Django, là où un non-superuser recevait
> `DEFAULT_RAGSPACE_CHAT_MODEL` : deux parcours, deux résultats, pour la même absence de
> choix. Un défaut dupliqué n'est pas un défaut, c'est une divergence en attente.

---

## 5.3 Catalogue de modèles et allowlist — **implémenté**

Avant ce dispositif, la seule validation d'un nom de modèle était une **regex de
caractères**. `bge-m3` la passait, marquait tous les documents d'un espace
`reindex_required`, puis échouait à la première ingestion sur
`dimension mismatch (got 1024, expected 768)` : l'espace se retrouvait avec un leg
vectoriel vide et aucune réindexation capable d'aboutir. La passerelle publiait pourtant
`capabilities`, `dimension` et `registry_status` — personne ne les lisait.

### Quatre notions, jamais confondues

| Notion | Sens | Source |
|---|---|---|
| `available` | présent dans `GET /v1/models` | registre de la passerelle |
| `allowed` | compatible avec le rôle **et** les invariants de **cet** environnement | dérivé |
| `qualified` | RagATM l'a **mesuré** | `apps/llm_router/qualification.py` (versionné) |
| `recommended` | embeddings : le défaut parmi les qualifiés ; chat : le **défaut effectif** `DEFAULT_RAGSPACE_CHAT_MODEL` s'il est sélectionnable | `qualification.py` (embeddings) / settings (chat) |

Embeddings : `recommended ⇒ qualified ⇒ allowed ⇒ available`, vérifié par test.
Chat : `recommended ⇒ allowed ⇒ available` et `recommended == défaut effectif`. Le badge
et la présélection n'ont plus qu'**une** source ; la justification affichée est
« choix par défaut opérationnel » (`RECOMMENDED_CHAT_REASON`) — le banc à contexte figé
du 02/11/2026 n'a établi aucune supériorité de qualité, seulement une latence plus basse.

### Chat : compatibilité de génération — **implémenté** 

`/v1/models` relaie les capacités d'Ollama : `gpt-oss:20b` et
`qwen3:30b-a3b-thinking-2507` y portent `completion`, alors que la passerelle les inscrit
en `chat` seul et que `/v1/generate` — **seul endpoint du chat RagATM** — les refuse en
400 `completion_not_supported`. La compatibilité se lit donc dans le registre de
génération de la passerelle, `GET /health/runtime` (`llm_models[].capabilities`), avec la
même politique de cache que `/v1/models` :

| `generation_status` | Règle | Sélectionnable |
|---|---|---|
| `supported` | inscrit, `completion` déclarée, `allowed_here` | oui |
| `unsupported` | inscrit sans `completion`, ou `allowed_here=false` | non (`model_generation_not_supported`) |
| `unsafe_output` | `reasoning.expose_thinking=true`, ou `qualification.CHAT_OUTPUT_POLICY_REJECTED` (aujourd'hui `semx-minicpm-v4.5`, raisonnement exposé mesuré) | non (`model_output_policy_rejected`) |
| `not_verified` | absent du registre de génération, ou registre illisible | non (`model_generation_not_verified`) — **fail-closed** |

Conséquence assumée : les modèles **non inscrits** au registre de génération
(`deepseek-r1:32b`, `glm-4.7-flash`, `qwen3-vl:8b`, `semx-internvl3.5:8b`) restent
listés mais ne sont plus proposés, même si certains répondent (le registre n'est pas
strict). Les réadmettre = les inscrire côté passerelle, sans toucher RagATM.

La même règle s'applique à la **création** et à la **modification** (`catalog_config_error`).
Un non-superuser reçoit le défaut canonique ; s'il est **prouvé** inutilisable (absent,
sans génération, exclu), la création est refusée avec le même code — une passerelle
injoignable ne bloque pas (comportement antérieur).

### Chat : moteur unique

Le seul moteur de chat est `ollama` (modèle local servi par l'AI Gateway) ; `provider` n'est jamais
transmis à la Gateway (`gateway/dto.py:to_payload`). `/api/v1/capabilities/` expose les moteurs
sélectionnables.

### Endpoint

```
GET /api/v1/ai/model-catalog/          → {"chat": [...], "embeddings": [...]}
GET /api/v1/ai/model-catalog/?refresh=1  (superuser — force une relecture amont)
```

Le frontend n'appelle **jamais** la passerelle directement : ni son URL ni sa topologie
n'ont à sortir du réseau privé. Le catalogue public ne porte ni digest, ni URL interne.

### Ce qui est refusé, et *quand*

La validation intervient **avant tout effet de bord** — avant `rs.save()`, donc avant le
marquage `reindex_required`. Un modèle refusé ne laisse rien derrière lui.

| Code (HTTP 400) | Cas |
|---|---|
| `model_not_in_catalog` | faute de frappe, modèle absent de cet environnement |
| `model_capability_mismatch` | modèle d'embeddings en chat, ou l'inverse |
| `model_dimension_mismatch` | dimension ≠ `RAG_EMBEDDING_DIM` |
| `model_registry_not_ready` | pipeline non inscrit `ready` au registre |
| `model_not_qualified` | pipeline gouverné jamais mesuré — franchissable par `force` |
| `model_catalog_unavailable` | passerelle déclarée mais injoignable, aucun état connu |
| `model_generation_not_supported` | chat : modèle non servi par `/v1/generate` |
| `model_generation_not_verified` | chat : modèle absent du registre de génération (ou registre illisible) |
| `model_output_policy_rejected` | chat : réponses exposant le raisonnement |

**Seuls les champs qui CHANGENT sont validés.** Un espace historique portant une valeur
que le catalogue ne connaît pas reste utilisable et renommable : une règle arrivée après
lui ne l'invalide pas.

### Indisponibilité de la passerelle

Trois états distincts, et les confondre coûterait cher :

| Situation | Comportement |
|---|---|
| aucune passerelle déclarée (`AI_GATEWAY_BASE_URL` vide) | la gouvernance **ne s'applique pas** — rien à gouverner |
| passerelle joignable | catalogue frais, TTL 300 s |
| passerelle injoignable, état antérieur connu | **dernier état connu**, `stale: true` |
| passerelle injoignable, aucun état connu | **refus** (`model_catalog_unavailable`) |

Le catalogue n'est **pas** sur le chemin chaud : il sert à la création/modification d'un
espace et à l'administration, jamais à la génération. Cache mémoire process-local,
volontairement pas Redis — une panne de cache ne doit pas devenir une panne de
configuration.

### `force` — échappatoire bornée

Superuser uniquement, auditée. Elle franchit **la qualification**, jamais
l'incompatibilité : aucun droit ne rend un modèle 1024 compatible avec un index 768.

### Ce que le catalogue rend impossible de confondre

Le lot `GOVERNED-EMBEDDING-PIPELINE-ENABLEMENT` a mesuré que le pipeline gouverné
`legacy-768` coûte **26,8 ms** et `fr-en-retrieval` **170,5 ms** — même gouvernance,
poids différents. Le choix structurant n'est donc pas « legacy ou gouverné » mais
**quel pipeline gouverné**. Le catalogue les expose comme deux entrées distinctes, avec
leur mesure attachée.

---

## 6. Routage par tâche (`task_routing.py`) — **activable par flag, expérimental**

- `RAG_TASK_ROUTING_ENABLED` (défaut **0/off**). Override le **modèle** (jamais le provider),
  **uniquement** pour les espaces locaux (`provider=ollama`, servis par la Gateway).
- Tâches : `factual`→`RAG_TASK_MODEL_FACTUAL`, `report`→`RAG_TASK_MODEL_REPORT`,
  `rewrite`→`RAG_TASK_MODEL_REWRITE`. `rephrasing_suggestions` déclaré mais **non routé** (futur).

---

## 7. System checks `llm_router` (`checks.py`) — **implémenté**

Actifs quand `DJANGO_ENV ∈ {dev, staging, prod}`. Échappatoire tests :
`ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS=1` dégrade E001/E003 en Warning (W001/W002). Jamais en
déploiement réel. Aucune valeur sensible affichée.

| id | Refuse (env officiel) |
|---|---|
| `llm_router.E001` | `AI_GATEWAY_ENABLED=0` (Gateway obligatoire). Toléré = W001 |
| `llm_router.E002` | `AI_GATEWAY_BASE_URL` vide **ou** hôte loopback/non routable (`127.0.0.1`/`localhost`/`0.0.0.0`/`::1`) quand la Gateway est active |
| `llm_router.E003` | `RAG_LLM_PROVIDER=ollama` avec Gateway ÉTEINTE en environnement officiel (Ollama direct interdit) ; Gateway active, la variable est sans effet. Toléré = W002 |
| `llm_router.E004` | `AI_GATEWAY_FALLBACK_PROVIDER ≠ none` (le provider de repli doit valoir `none`) |
| `llm_router.E007` | l'une des trois URL Gateway (`AI_GATEWAY_BASE_URL`, `AI_GATEWAY_EMBEDDINGS_BASE_URL`, `RAG_RERANKER_BASE_URL`) n'est pas http(s) avec hôte, vise le port 11434 (Ollama direct), est loopback (embeddings/rerank ; la génération relève d'E002). URL vide : non jugée ici |

| `llm_router.E009` | valeur de provider externe configurée (`RAG_LLM_PROVIDER`, `EMBEDDINGS_PROVIDER`, `AI_GATEWAY_FALLBACK_PROVIDER`, `DEFAULT_RAGSPACE_*_PROVIDER`) |
| `config.E009` | variable d'environnement d'un fournisseur externe présente (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `MISTRAL_API_KEY`…) |

`AI_GATEWAY_REQUIRED_BASE_URL` (défaut vide) sert uniquement d'indication dans les messages des
checks. `AI_GATEWAY_POOL_HOSTS` (liste, défaut vide) désigne les noms d'hôte reconnus comme un
AI Gateway Pool ; toute autre URL http(s) est une Gateway directe.

---

## 8. Streaming — **direct uniquement (dev/tests)**

`generate_text_stream` **refuse explicitement** le streaming quand `AI_GATEWAY_ENABLED=1` (le
stream bypasserait la Gateway) → `LLMError`. Le streaming via Gateway est **non implémenté**
(planifié). Chemin direct : `_ollama_stream` (JSON par ligne), émettant
`{type:delta}` puis `{type:result}`. Côté chat, Gateway active ⇒ pseudo-stream (génération
non-streaming émise en un seul `token`), cf. [../03-backend/chat-generation.md](../03-backend/chat-generation.md).

---

## 9. Coût

Le routeur est **agnostique à la tarification** (`cost=None`) ; il n'estime pas le coût. Le coût
estimé est calculé par les consommateurs via `apps/rag_core/services/pricing.py` (édition Open :
aucune grille tarifaire intégrée — `0.0` pour un provider local, `None` sinon, sauf grille fournie
par `LLM_PRICING_PER_1K`)
(`compute_cost_from_usage`) et persisté dans `RagQueryLog.llm_cost_estimated`. Détails et
gouvernance : [politiques-llm.md](politiques-llm.md) et
[../05-api/reference-endpoints.md](../05-api/reference-endpoints.md) (endpoint `audit/llm-costs`).

---

## Références d'implémentation

- `backend/apps/llm_router/router.py`
- `backend/apps/llm_router/gateway/{client,dto}.py`
- `backend/apps/llm_router/task_routing.py`
- `backend/apps/llm_router/model_catalog.py` (projection, allowlist, validation)
- `backend/apps/llm_router/qualification.py` (registre versionné `qualified`, recommandation embeddings, politique de sortie du chat)
- `backend/apps/llm_router/capabilities.py` (`chat_provider_error`, `default_chat_model_error`, `catalog_config_error`)
- `backend/api/v1/model_catalog/views.py` (`GET /api/v1/ai/model-catalog/`)
- `backend/apps/llm_router/checks.py`, `backend/apps/llm_router/apps.py`
- `backend/apps/chat/services/chat_formatting.py`
- `backend/apps/rag_core/models/rag_space.py`
- `backend/apps/rag_core/services/pricing.py`
- `backend/config/settings/base.py`
