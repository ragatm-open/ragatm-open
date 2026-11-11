# AI Gateway — policy et service

Objectif : décrire l'AI Gateway, passerelle IA obligatoire en environnement
officiel, et distinguer clairement le service livré dans le dépôt du contrat client attendu
par le backend. Public : développeurs backend, exploitants, configuration IT.

Ce document est la **référence normative** de la policy IA. Il absorbe et met à jour l'ancien
`AI_GATEWAY.md`.

---

## 1. Règle (policy)

> Tous les appels IA de RagATM (génération, embeddings, reranking, vision, ASR) passent par
> **l'AI Gateway** (directement ou via AI Gateway Pool), jamais par Ollama en direct.

- Cible : `AI_GATEWAY_BASE_URL` sur le réseau privé (`http://ai-gateway:8100` en Compose,
  `http://127.0.0.1:8100` pour un lancement local).
  Statut : **activé par flag** `AI_GATEWAY_ENABLED=1`, **obligatoire** hors tests.
- Il n'existe **aucun provider externe** : si la Gateway est indisponible, l'erreur est
  contrôlée (pas de repli). **Jamais Ollama direct.**
- Interdiction vérifiée au démarrage par les system checks `llm_router.E001`–`E005`
  (voir [providers-modeles.md](providers-modeles.md) pour le détail).

---

### Projection HTTP des pannes

Une défaillance de l'AI Gateway n'est plus un `500 internal_error` anonyme. La frontière
HTTP/SSE la classe (`apps/llm_router/gateway/http_errors.py`) :

| Erreur client Gateway | API `/chat/` | Flux `/chat/stream/` |
|---|---|---|
| `GatewayUnavailableError`, `GatewayTemporaryUnavailable` (connexion refusée, 5xx amont), `GatewayTimeoutError`, `GatewayStreamError`, `GatewayRateLimited` | `503 {"detail": "ai_gateway_unavailable", "request_id", "retryable": true}` + `Retry-After: 10` | `error` nommé puis `done` (`status: error`), aucun `token` fabriqué |
| `GatewayDisabledError`, `GatewayConfigError`, `GatewayAuthenticationError`, `GatewayContractError` | `502 {"detail": "ai_gateway_error", "retryable": false}` | idem |

La cause est recherchée dans la chaîne `__cause__` (une panne Gateway enveloppée par le
retrieval ou les embeddings reste nommée). Aucun détail interne ne sort ; le journal garde
la trace. Les pannes Redis/courtier Celery sont traitées par le gestionnaire DRF
`api/v1/exception_handler.py` (`503 dependency_unavailable`). Tests :
`tests_root/unit/test_open6_gateway_http_errors.py`, `tests_root/http_api/test_open6_dependency_errors.py`.

## 2. Deux « Gateways » distincts — écart de contrat

Point crucial : le dépôt contient **un** service Gateway, mais le backend en attend **un autre**,
plus riche. Les deux ne sont pas alignés en version.

| | (a) Service in-repo | (b) Contrat client attendu |
|---|---|---|
| Nom / version | `services/ai_gateway/` — FastAPI **v0.1.0** | Gateway v2 (hors dépôt) |
| Endpoints | `GET /health` + `POST /v1/generate` | `POST /v1/generate` (enrichi) + `POST /v1/embeddings` |
| Champs `/v1/generate` acceptés | `prompt`, `model`, `temperature`, `max_tokens`, `metadata` | `prompt`, `model`, `options`, `system`, `think`, `format`, `temperature`, `keep_alive` (schéma **fermé** depuis les builds stricts) |
| Embeddings | **absent** | `POST /v1/embeddings` `{model, task, texts}` → `{embeddings:[[…]]}` |
| Auth / retry / streaming | aucun | (hors périmètre in-repo) |

**Écart de contrat à retenir** : le client backend de **génération**
(`apps/llm_router/gateway/dto.py`) envoie `system`/`think`/`options` et le
service in-repo v0.1.0 **n'implémente pas** ces champs ; il n'implémente pas non plus
`/v1/embeddings`. En pratique, une Gateway externe (v2, hors dépôt) répond à ce
contrat ; le service in-repo est une base minimale, pas la Gateway de production.

> **`provider`, `stream` et `request_id` n'appartiennent à aucun corps de requête** —
> ni `/v1/generate` (le flux est porté par la route `/v1/generate/stream`), ni
> `/v1/embeddings`, ni `/v1/rerank`. Ce sont des états internes du client. Le build
> les builds antérieurs les ignoraient en silence ; les builds stricts les refusent en 400
> `extra_forbidden`. Tout corps émis doit être accepté par les **deux** builds (ordre de
> promotion : RAGatm d'abord, Gateway stricte ensuite). Verrou :
> `tests/tests_root/llm_router/test_gateway_strict_request_schemas.py`, avec un relevé
> minimal des schémas des deux builds (`_gateway_request_schemas.json`) et une garde
> statique sur les fonctions qui adressent ces routes. Voir [embeddings.md](embeddings.md) §3.2.

Statut : le service in-repo est **implémenté** (v0.1.0, minimal) ; les extensions v2.0.0 sont
**attendues côté Gateway** (non fournies par le service minimal du dépôt).

---

## 3. Le service in-repo `services/ai_gateway/` (v0.1.0)

### Framework et lancement

- **FastAPI** — `app = FastAPI(title="RagATM AI Gateway", version="0.1.0")`. Couche web mince ;
  la logique testable vit dans `service.py`/`schemas.py`/`ollama_client.py`/`errors.py`.
- Lancement : `uvicorn app.main:app --host 0.0.0.0 --port 8100` (bind `0.0.0.0` **dans** le
  conteneur ; la publication hôte est bornée en loopback par docker-compose, cf. §5).

### Endpoints implémentés

- `GET /health` → `{"status": "ok"}`. **Liveness statique** : pas de `/ready`, aucune
  vérification de disponibilité d'Ollama.
- `POST /v1/generate`.

Requête `/v1/generate` :

```json
{ "prompt": "str (requis, non vide)", "model": "str (requis, non vide)",
  "temperature": 0.2, "max_tokens": 1024, "metadata": { "request_id": "…" } }
```

Défauts : `temperature=0.2`, `max_tokens=1024`. Validation stricte (`prompt`/`model` non vides,
`max_tokens` entier > 0, `metadata` objet) ; échec → `400 bad_request`.

Réponse `/v1/generate` :

```json
{ "text": "str", "provider": "ai_gateway", "model": "str",
  "tokens": {"prompt":N,"completion":N,"total":N},
  "usage": {"prompt_tokens":N,"completion_tokens":N,"total_tokens":N},
  "cost": 0.0, "raw": {…} }
```

`provider` est codé en dur à `"ai_gateway"`. `cost=0.0` par convention (moteur local — ce `0.0`
ne vaut que pour les modèles locaux ; la tarification réelle est calculée côté backend, voir
[providers-modeles.md](providers-modeles.md)).

### Moteur amont : Ollama

- `POST {OLLAMA_BASE_URL}/api/generate`, `stream=False`, `options={temperature, num_predict}`.
- **Pas de streaming, pas de retry** au niveau du service. Un seul appel, timeout explicite.
- Mapping d'erreurs typées :

  | Cas | HTTP | code |
  |---|---|---|
  | Schéma invalide | 400 | `bad_request` |
  | Timeout moteur | 504 | `upstream_timeout` |
  | Moteur injoignable / erreur réseau | 503 | `upstream_unavailable` |
  | Modèle absent (404 / « not found ») | 502 | `model_not_found` |

- Tokens lus depuis `prompt_eval_count` / `eval_count` de la réponse Ollama.

### Ce que le service in-repo NE fait PAS

- **Aucune authentification applicative** (pas d'API key ni token) — sécurité **réseau
  uniquement** (§5).
- **Aucun retry**, **aucun réglage de concurrence** (uvicorn mono-process par défaut), **pas de
  readiness**.
- **Aucune allowlist de modèles** : le `model` de la requête est passé tel quel à Ollama.
- **Aucun endpoint embeddings** (`/v1/embeddings` absent — cf. §2 et [embeddings.md](embeddings.md)).

---

## 4. Variables d'environnement

### Service in-repo (`services/ai_gateway/`, `config.py`)

| Variable | Défaut | Rôle |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | moteur amont |
| `AI_GATEWAY_TIMEOUT_SECONDS` | `30` | timeout `requests` |
| `AI_GATEWAY_DEFAULT_MODEL` | `compact-generation-8` | modèle de config par défaut (nom logique) |
| `AI_GATEWAY_BIND_HOST` | `0.0.0.0` | bind interne conteneur |
| `AI_GATEWAY_PORT` | `8100` | port d'écoute |

### Client backend (`config/settings/base.py`)

| Variable | Défaut | Rôle |
|---|---|---|
| `AI_GATEWAY_ENABLED` | `0` | active le chemin Gateway (obligatoire=1 en env officiel) |
| `AI_GATEWAY_BASE_URL` | `""` (alias `AI_GATEWAY_URL`) | URL de la Gateway |
| `AI_GATEWAY_TIMEOUT_SECONDS` | `30` | timeout client (budget **total** en mode flux, cf. §4bis) |
| `AI_GATEWAY_STREAMING_ENABLED` | `0` | streaming natif du chat (`/v1/generate/stream`) — *activable par flag* |
| `AI_GATEWAY_VISION_MODEL` | `""` | profil **logique** vision (`/v1/vision/analyze`) ; vide = champ omis — lu seulement si `DOCUMENT_VISUAL_ANALYSIS_ENABLED=1` (§4ter) |
| `AI_GATEWAY_VISION_TIMEOUT_SECONDS` | `120` | timeout de lecture vision, **provisoire** (qualification réelle à venir) — lu seulement si `DOCUMENT_VISUAL_ANALYSIS_ENABLED=1` (§4ter) |
| `AI_GATEWAY_PROVIDER` | `ollama` | provider primaire routé vers la Gateway |
| `AI_GATEWAY_MODEL` | `compact-generation-8` | modèle demandé **en dernier recours** (voir l'encadré §Découplage) |
| `AI_GATEWAY_FALLBACK_ENABLED` | `0` | pas de repli en édition ouverte |
| `AI_GATEWAY_FALLBACK_PROVIDER` | `none` | aucun provider de repli |
| `AI_GATEWAY_REQUIRED_BASE_URL` | `http://ai-gateway:8100` | URL attendue (checks) |
| `ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS` | `0` | échappatoire tests uniquement |

Liste complète et à jour : [../10-reference/variables-environnement.md](../10-reference/variables-environnement.md).

Configuration `.env` type (local ou Compose) :

```dotenv
AI_GATEWAY_ENABLED=1
AI_GATEWAY_BASE_URL=http://ai-gateway:8100
AI_GATEWAY_TIMEOUT_SECONDS=30
AI_GATEWAY_STREAMING_ENABLED=0  # 1 = streaming natif du chat (cf. §4bis)
AI_GATEWAY_PROVIDER=ollama          # provider PRIMAIRE routé vers la Gateway
AI_GATEWAY_MODEL=compact-generation-8  # repli seulement (cf. DEFAULT_RAGSPACE_CHAT_MODEL)
AI_GATEWAY_FALLBACK_ENABLED=0
AI_GATEWAY_FALLBACK_PROVIDER=none   # aucun provider externe
```

> **Découplage primaire / repli.** Le provider/modèle envoyés à la Gateway viennent de
> `AI_GATEWAY_PROVIDER` / `AI_GATEWAY_MODEL`, **jamais** de `RAG_LLM_PROVIDER`. Ce dernier ne sert
> qu'au chemin direct (Gateway OFF) et au repli.
>
> ⚠️ **`AI_GATEWAY_MODEL` ne décide pas du modèle qui répond aux questions.** Pour une
> question posée dans un RagSpace, c'est le `llm_model` **persisté de l'espace** qui est
> envoyé à la Gateway ; `AI_GATEWAY_MODEL` n'intervient que si ce champ est vide
> (`router.py` : `model_resolved = (model or "").strip() or AI_GATEWAY_MODEL`). Le modèle
> des nouveaux espaces se pilote par `DEFAULT_RAGSPACE_CHAT_MODEL` — hiérarchie complète
> dans [providers-modeles.md §5.2](providers-modeles.md#52--préséance-des-modèles-qui-décide-vraiment).

---

## 4bis. Streaming du chat (`AI_GATEWAY_STREAMING_ENABLED`) — *activable par flag*

**État : implémenté, OFF par défaut.** Le chat SSE dispose de deux régimes, choisis
par ce seul drapeau ; `AI_GATEWAY_ENABLED=1` est un prérequis dans les deux cas.

| Drapeau | Chemin de génération | Ce que voit l'utilisateur |
|---|---|---|
| `0` (défaut) | `POST /v1/generate` (bloquant) | la réponse arrive d'un bloc, en un seul événement `token` (*pseudo-stream*) |
| `1` | `POST /v1/generate/stream` (NDJSON) | les fragments arrivent au fil de la génération |

### Parité de payload (invariant)

Les deux régimes partagent le **même constructeur de requête**
(`router._build_gateway_call`) : même modèle, mêmes `options`
(`temperature`, `num_ctx`, `num_predict`), même `think`. Seul le chemin d'URL
diffère. Ce n'est pas une convention mais une garantie de construction, testée
(`tests/tests_root/llm_router/test_gateway_streaming.py`).

> Pourquoi c'est critique : un `num_ctx` différent entre les deux modes forcerait
> Ollama à **recharger le runner** à chaque bascule de drapeau — plusieurs secondes
> de latence ajoutée, invisibles dans le code.

### Contrat NDJSON (Gateway 2.0.0)

Une ligne JSON par événement : `0..N` objets `{"type":"delta","delta":"…"}` puis
**exactement un** `{"type":"done", "model", "tokens", "usage"}`. Une rupture amont
produit `{"type":"error", …}` et **jamais** de `done`.

- Fin de flux **sans** `done` ⇒ erreur (`GatewayStreamError`) : une réponse tronquée
  n'est jamais présentée comme complète.
- Le `done` ne porte **ni** `provider` **ni** le texte final : le texte est
  l'accumulation des fragments côté client, et `provider` est forcé à
  `ai_gateway` — la valeur que renvoie `/v1/generate`. Sans cela, le même échange
  serait journalisé sous deux fournisseurs différents (`ollama` en flux,
  `ai_gateway` en bloc) et `RagQueryLog.llm_provider` deviendrait ininterprétable.

L'interface annonce une réponse tronquée par un bandeau dédié
(`partialAnswerNotice`) : le texte reçu est conservé, jamais présenté comme
complet.

### Budget de temps

`AI_GATEWAY_TIMEOUT_SECONDS` borne la **génération entière**, pas chaque fragment :
le budget n'est pas renouvelé à chaque delta (sans quoi un flux d'un token par
seconde durerait indéfiniment). La connexion est plafonnée à 10 s dans ce budget.

### Annulation

Chaîne : `AbortController` navigateur → proxy Next.js → `StreamingHttpResponse`
Django → client Gateway RagATM → AI Gateway → Ollama. Quand le consommateur cesse
d'itérer, le client ferme la socket (`raw.close()` puis `close()`), ce qui fait
constater la déconnexion à la Gateway.

**Mesuré le 21/08/2026 (staging)** : génération de 18 s interrompue à 6 s ; le GPU
redevient libre immédiatement (sonde à 203 ms, contre ~1 100 ms pendant une
génération) alors que 12 s restaient à produire. Aucune génération fantôme.

> ⚠️ Défaut d'observabilité **côté Gateway** : le compteur `inflight` de
> `/health/runtime` **n'est pas décrémenté** quand le client se déconnecte — il reste
> bloqué à 1. Ne pas s'en servir pour conclure qu'une génération tourne encore ;
> la mesure fiable est l'occupation GPU par sonde.

### Repli et réponse partielle

- Panne transitoire **avant** le premier fragment : repli contrôlé habituel
  (`AI_GATEWAY_FALLBACK_PROVIDER`), le flux reste cohérent.
- Rupture **après** le premier fragment : **aucun repli** — recoller la fin d'un
  modèle B au début d'un modèle A produirait une réponse incohérente. Le texte déjà
  affiché est conservé et l'événement `done` porte `partial: true` et
  `termination: "stream_error"` ; `RagQueryLog` garde le texte avec
  `status=error` / `error_stage=llm`.

### Rollback

Repasser `AI_GATEWAY_STREAMING_ENABLED=0` et redémarrer : le chemin pseudo-stream
est restauré à l'identique. Aucun changement de schéma, aucune migration.

---

## 4ter. Client vision V1 (`/v1/vision/analyze`) — *activable par flag*

**État : image autonome et visuels intégrés d'un PDF qualifiés en staging réel
(puis, 16/09/2026) ; visuels intégrés d'un DOCX
implémentés mais jamais qualifiés contre un Gateway réel ; images intégrées
d'un PPTX implémentées, jamais qualifiées ni activées.** Le tout
derrière `DOCUMENT_VISUAL_ANALYSIS_ENABLED=0` (éteint par défaut) et l'allowlist
`DOCUMENT_VISUAL_ANALYSIS_SOURCES`. Un PDF comme un DOCX ne découvrent que leurs **rasters
intégrés** — aucun rendu, donc aucun visuel purement vectoriel (ni tracé PDF, ni SmartArt).
Un DOCX n'étant pas paginé avant rendu, ses fragments visuels sont publiés **sans numéro de
page** ; un PDF, lui, cite les siens à leur page. Un PPTX cite les siens à leur **diapositive**
(`slide_number`), jamais à une page. L'ajout du PPTX ne change **rien** au contrat vision : même
tâche `describe`, même corps ; ni la nature de source ni la provenance ne sont transmises.

**Capacité qualifiée (AI-GATEWAY-VISION-1B) : la tâche `describe` seulement.** Le Gateway
gouverne le profil logique `vision-large` ; le modèle concret qu'il sert n'entre jamais
dans une requête RAGatm. Les tâches `auto`, `table`, `diagram`, `chart` et
`technical_drawing` restent du vocabulaire V1 valide mais sont refusées en échec fermé
(400 `vision_task_not_qualified`, non relançable, aucune inférence). L'adaptateur image
autonome demande donc **`task="describe"`** (`ingest/visual/capability.py`) : aucun repli
vers une autre tâche, aucune devinette, aucune sonde de capacités par document, aucun
réglage d'exploitation. Les normaliseurs des autres types restent en place pour les
capacités futures et les tests de contrat — leur existence ne les qualifie pas.
Sémantique PRIMARY/ENRICHMENT, relances typées, réindexation non destructive et file
`ingest_visual` : [ingestion-extraction.md](../03-backend/ingestion-extraction.md)
(« Analyse visuelle des images autonomes »). Porte éteinte : aucun client construit,
aucun appel.

Le contrat serveur est **gelé** (AI-GATEWAY-VISION-CONTRACT-0) : RAGatm le consomme, il
ne le redessine pas.

### Chemin et requête

`RAGatm -> AI Gateway -> backend modèle` — jamais d'appel direct à un serveur de
modèles, jamais de repli externe (`VisionGatewayError.fallback_eligible = False`).

- Une image par requête, JSON : `schema_version="1"`, `task` (défaut `auto`), `locale`
  (`fr`|`en`), `request_id`, `image{data base64, mime_type png|jpeg|webp}`,
  `context{caption, alt_text, nearby_text}` ; `model` seulement si
  `AI_GATEWAY_VISION_MODEL` est non vide (un nom concret `nom:tag` est refusé).
- **Aucune provenance transmise** : ni nom de fichier, repère, page, bbox, empreinte,
  identifiant de document, d'espace ou d'utilisateur. Une garde locale refuse tout champ
  hors schéma V1 avant l'envoi.
- `request_id = "rgv1-" + sha256(id opaque du document | empreinte | tâche | empreinte du
  contexte)[:32]` : stable d'une tentative à l'autre, sans identifiant en clair.
- Contexte : trois champs séparés, `null` si absents, bornés à **500 caractères** par
  champ à une frontière de mot — borne consommateur **provisoire** en attente de MODEL-1.
  Le contexte n'est **jamais** resérialisé comme preuve visuelle.

### Erreurs

Classement par `detail.code` et `detail.retryable` **uniquement** — le message n'est ni
lu ni conservé. Tout **4xx est forcé non relançable**, quoi qu'en dise le corps.

| Cas | `code` | Relançable |
|---|---|---|
| erreur Gateway avec enveloppe | code Gateway verbatim | `detail.retryable` (forcé `false` en 4xx) |
| 400/422 de validation sans `code` | `vision_request_invalid` | non |
| 5xx sans enveloppe | `visual_gateway_http_error` | oui pour 502/503/504 |
| connexion refusée, DNS, reset | `visual_gateway_unavailable` | oui |
| `ConnectTimeout` / `ReadTimeout` | `visual_client_timeout` | oui |
| Gateway désactivée / sans URL | `visual_gateway_disabled` / `visual_gateway_misconfigured` | non (aucun appel) |
| succès non conforme | `visual_contract_drift` (`VisionContractDrift`) | non |

Un appel `analyze` = **un** appel HTTP : aucune relance cachée (Celery possède les
relances, le Gateway sa propre relance de sortie malformée).

### Réponse V1 fermée

Enveloppe exacte (tout champ ajouté ou manquant = dérive), `status="ok"`,
`provider="ai_gateway"`, `schema_version="1"`, `truncated=false`, `request_id` égal à
celui envoyé, énumérations exactes (`visual_kind`, certitudes, avertissements),
`task` égale à celle demandée (une réponse servie sous une autre tâche est une dérive),
identités `model{logical, concrete, digest}` et `pipeline{prompt_profile,
prompt_profile_version, pipeline_id}` complètes. Charges utiles vérifiées par type :
grille de tableau couvrante sans trou ni chevauchement, sémantique vide/illisible/remplie,
arêtes et groupes vers des nœuds existants, point illisible sans nombre, `mixed` à ≥ 2
régions non imbriquées, bbox normalisée dans [0,1]. `unknown` et `decorative` sont des
résultats sémantiques (charge nulle), jamais des preuves.

> **Écart à réconcilier (1E).** Le texte gelé ne détaille pas les entrées `legend` /
> `annotations` d'un dessin technique, la charge d'une région mixte `text` ni
> l'énumération `orientation` d'un axe : ces formes ne sont contrôlées qu'au niveau
> conteneur, à confronter aux fixtures dorées du Gateway.

### Normalisation (en mémoire)

`apps/rag_core/services/ingest/visual/` : `VisualAsset` → requête ; réponse validée +
provenance du visuel → `VisualArtifact` (le Gateway n'écrase jamais repère, page ni
bbox ; charge utile transitoire) ; sérialisation déterministe photo / diagramme (direction
inconnue sans flèche) / graphique (`=` exact, `≈` approximatif, illisible sans nombre,
« Generated trend summary » séparé) / dessin technique (`value_text` verbatim, valeur
normalisée seulement si exacte, aucune géométrie inférée) / mixte (une sortie par région,
bbox remappée, aucune unité parent). Les tableaux passent par le **rail tabulaire
générique existant** avec `cell_states` — cf.
[ingestion-extraction.md](../03-backend/ingestion-extraction.md). Aucun octet ni base64
dans les métadonnées.

## 4quater. Client vision V2 des crops (`schema_version = "2"`) — *implémenté (CV-3D), staging seulement ; orchestration CV-3E1 désactivée par défaut*

Rail **distinct** du client V1 (§4ter, inchangé et toujours fermé) : il envoie un `Crop`
CV-3C (cf. [modele-donnees.md](../03-backend/modele-donnees.md)) et n'accepte qu'une
réponse V2. **Aucun repli vers V1.** Aucune persistance métier du résultat (`VisualObservation` =
CV-3F) ; orchestration : §4quinquies (CV-3E1, éteinte par défaut).

**Activation** : `AI_GATEWAY_VISION_V2_ALLOWED_FINGERPRINTS` (liste, vide par défaut). Vide ⇒
rail inactif, **aucun appel** (`vision_v2_not_configured`). Gateway **staging** (V2)
sert V2 ; Gateway **production** ne sert que V1 : ne rien renseigner en
production. Valeur qualifiée staging :
`vision-v1:9c68b7db03303789ffb1b34d5ff0da811a8c042cfc912eab89db0fd702dced39`.

**Requête** : `POST /v1/vision/analyze`, `schema_version="2"`, `task="describe"`,
`locale="fr"` (réservé, non effectif côté Gateway), `request_id` = `rgv2-` + 32 hex dérivés
de (crop, `crop_sha256`, tâche, empreinte de contexte) — **corrélation**, pas provenance,
identique d'une relance à l'autre —, `context` canonique existant, `image.data` = Base64 des
**octets exacts du fichier de crop** (aucun ré-encodage), `image/png`. Refus local avant
envoi si le Base64 dépasse la borne Gateway (5 592 408 caractères ≈ 4,19 Mo de PNG) ou si
le crop dépasse 2 MP.

**Portes** (toutes bloquantes, non relançables, jamais dégradées en avertissement) :

| Porte | Contrôle | Échec |
|---|---|---|
| K1 local | SHA-256 des octets relus == `Crop.crop_sha256`, **avant** tout appel | `local_crop_integrity_failure` (0 appel) |
| K2 | `schema_version == "2"`, enveloppe fermée (cf. ci-dessous) | `vision_v2_contract_error` |
| K1 | `image.source_sha256 == Crop.crop_sha256` | `provenance_integrity_failure` |
| K3 | `pipeline.fingerprint` ∈ allowlist **configurée** | `pipeline_not_allowed` |

`/health/vision` sert au préflight et à la détection de dérive ; il **n'approuve jamais** un
fingerprint.

**Réponse V2 (fermée)** : clés de premier niveau de V1 ; `image` + `source_sha256`,
`normalized_sha256` (64 hex minuscules), `normalized_format`, `normalizer_id` ; `pipeline` +
`fingerprint` (`vision-v1:<64 hex>`), `system_prompt_sha256`, `task_prompt_sha256`,
`model_schema_sha256`, `model_schema_hash_algorithm`, `inference` {temperature, seed, num_ctx,
num_predict}, `retry` {max_attempts, trigger, temperature, seed} ; `model.digest` 64 hex ;
`usage.attempts` ≥ 1. Un champ en plus ou en moins est une dérive. Forme relevée sur la
une Gateway de qualification (l'OpenAPI ne type pas la réponse) et figée en fixture de test.
`normalizer_id` est vérifié présent ; l'autorité pipeline reste le fingerprint (qui couvre
normaliseur, prompts, schéma et digest) — pas de seconde allowlist.

**Erreurs** : enveloppe `{"detail": {status:"error", provider:"ai_gateway", message, code,
retryable, request_id[, retry_after_seconds]}}` validée strictement (malformée ⇒
`vision_v2_contract_error`) ; le message n'est jamais lu.

| Cas | Code interne | Relançable |
|---|---|---|
| 503 `vision_overloaded` | `vision_gateway_overloaded` | oui, `Retry-After` honoré |
| 504 `vision_timeout` | `vision_gateway_timeout` | oui (borné) |
| 502 / autre 5xx | `vision_upstream_error` | selon `detail.retryable` (sans enveloppe : 502/503/504) |
| 400 / 413 / 422 | `vision_request_invalid` | **non** (`gateway_code` conservé ; 413 sur un crop conforme = incohérence de contrat) |
| timeout client / connexion | `visual_client_timeout` / `visual_gateway_unavailable` | oui |
| Base64 du crop > 5 592 408 car. (garde **avant** envoi, CV-3E1) | `crop_encoded_oversize` | **non** (aucun POST ; ne doit pas arriver sous `ragatm-crop/2`) |

**Relances** : `RETRY_OWNER = apps.rag_core.services.raster.vision.analyze_crop` ; le client
HTTP ne relance jamais. Politique du rail historique (`ASSET_MAX_RETRIES = 3`, attente
2/4/8 s) : **au plus 4 appels par crop**, attente = max(backoff, `Retry-After`) (borné à
300 s, lu défensivement par la primitive partagée `retry_after.parse_retry_after` : en-tête ou
`retry_after_seconds`, date HTTP ignorée), et budget
**mural strict** (défaut 270 s) : une relance n'est tentée que si attente + timeout client
tiennent avant l'échéance. À l'épuisement l'erreur devient **non relançable**
(`exhausted`) : aucune couche appelante ne peut multiplier les appels.

**Timeout** : le budget modèle Gateway est de 60 s (504 structuré à son terme). Le client
exige `AI_GATEWAY_VISION_TIMEOUT_SECONDS ≥ 70` (60 + 10 s de marge) — sinon rail non
configuré ; défaut 120, staging 75.

**Journaux / métriques** : identifiants de requête, crop, code, statut, tentative ; ni
octets, ni Base64, ni hash d'image. Compteurs : ceux de l'orchestration (§4quinquies).

**Rail V1 historique — borné par CV-3D1** : un visuel en 504 permanent était repris à
chaque continuation d'enrichissement (8 au plus) avec un budget neuf — jusqu'à ≈ 36 appels
(27 reproduits en test), trafic « toutes les 60 s » qui monopolisait la Gateway staging.
Désormais **au plus 4 appels par visuel et par génération**, épuisement terminal persisté,
`Retry-After` honoré par le client V1 (`retry_after_s`) — cf.
[ingestion-extraction.md](../03-backend/ingestion-extraction.md). Le rail V2 n'est pas
concerné (déjà borné).

## 4quinquies. Orchestration Vision V2 des crops — *implémenté (CV-3E1), désactivé par défaut*

`DocumentVersion → Page → PageRaster → Crop ragatm-crop/2 → VisualCropJob → GET
/v1/vision/capacity → POST /v1/vision/analyze (schéma 2) → résultat validé INTERNE`.
**Rien n'est actif** : drapeau éteint, allowlist d'espaces vide, allowlist d'empreintes
inchangée, production interdite, aucune citation visuelle publique.

**Portes** (toutes requises ; drapeau éteint ⇒ aucune tâche, aucun appel réseau) :

| Porte | Réglage | Défaut |
|---|---|---|
| drapeau | `VISUAL_V2_ORCHESTRATION_ENABLED` | `0` |
| garde dure de production (`DJANGO_ENV` = `prod`/`production`) | `VISUAL_V2_PRODUCTION_ALLOWED` | `0` |
| espaces | `VISUAL_V2_RAGSPACE_ALLOWLIST` | vide = **aucun** espace |
| DPI unique de rendu | `VISUAL_V2_RASTER_DPI` | `200` (150/200/300) |
| préflight `GET /health/vision` | schéma `"2"` servi pour `describe` ET empreinte servie ∈ `AI_GATEWAY_VISION_V2_ALLOWED_FINGERPRINTS` | allowlist vide ⇒ refus sans réseau |
| **politique en base** (CV-3E4A) | `VisualScopePolicy` (`visual_v2_scope`) : `enabled` ; `mode = auto` pour tout déclenchement automatique | **ligne absente = fermé** |

**Formule de porte** (CV-3E4A) : `drapeau ∧ ¬production ∧ espace ∈ VISUAL_V2_RAGSPACE_ALLOWLIST ∧
empreinte attendue autorisée ∧ VisualScopePolicy.enabled` ; déclenchement automatique : en plus
`mode = auto` ; préparation explicite : `mode` `explicit` **ou** `auto`. La politique ne fait que
restreindre : l'allowlist d'environnement reste la borne **externe** de déploiement.

- **Mode `explicit`** (défaut d'une nouvelle politique) : un upload PDF READY ou une
  réindexation ne déclenche **aucune** V2 et le rail V1 différé reste **inchangé** ; seule
  `visual_v2_prepare --version-id …` démarre la V2.
- **Mode `auto`** : comportement CV-3E2/3E3 — V1 différé évincé (`superseded_by=visual_v2`) ;
  mêmes octets ⇒ état visuel réutilisé, 0 appel. Depuis CV-3E4C, la préparation est écrite
  **dans la transaction READY** (`persist_auto_prepare`) et envoyée en `on_commit` — cf.
  [ingestion-extraction.md](../03-backend/ingestion-extraction.md) (atomicité READY / préparation).

**Préparations durables** : tout déclenchement (automatique ou opérateur) crée/réutilise un
`VisualPrepareRun` (`version × génération × empreinte`) commité **avant** l'envoi ; la
progression (`next_page_cursor` = prochaine page physique) est commitée après chaque page,
avant la continuation. Un déclenchement ou une continuation perdus restent retrouvables.

Le préflight précède l'enfilement (un seul GET, sans relance) ; l'empreinte retenue devient
`expected_pipeline_fingerprint` du job (K3+). Valeurs : [configuration.md](../10-reference/configuration.md).

**Capacité — `GET /v1/vision/capacity`** (contrat `"1"`, `scope = instance`, `advisory =
true`) : **consultative**, le POST reste l'autorité. Une sonde par activation, aucune boucle,
aucun cache. Validation stricte des champs utilisés ; `status = saturated` **ou**
`available_slots = 0` ⇒ report sans POST ; `model.resident = false` n'est **pas** une
saturation ; timeout, connexion, 5xx, JSON ou contrat invalide, `vision.enabled = false` ⇒
**repli sur le POST** (aucun report compté).

**Propriété des relances et budgets** (par clé de job × génération) :

| Couche | Propriétaire | Borne |
|---|---|---|
| sonde capacité | `run_crop_job` | 1 par activation |
| admission POST (503 `vision_overloaded`) | Gateway ; relance par `analyze_crop` | 4 POST par activation ; épuisé ⇒ **report de capacité** |
| relance HTTP | `apps.rag_core.services.raster.vision.analyze_crop` (inchangé) | 4 appels, `Retry-After`, budget mural |
| budget modèle | `VisualCropJob.model_attempts` | **4**, réservé et **commité avant chaque POST**, rendu **uniquement** sur un 503 `vision_overloaded` observé |
| report de capacité | `VisualCropJob.capacity_deferrals` | **60**, puis `parked` (pas un échec modèle) |
| continuation / reprise | `VisualCropJob.activations` | **72** (60 + 4 + 8), puis `parked` |
| relance Celery | aucune | `max_retries=0`, jamais `self.retry()` |
| récupération d'envois perdus (CV-3E4A) | balayeur `visual_v2_sweep_task` (Beat, file `maintenance`) | ≤ 50 envois par passe, fenêtre de 4 |

**Point de contrôle avant CHAQUE tentative HTTP** (CV-3E4A, crochet `before_attempt` appelé
par `analyze_crop` avant chaque envoi, relances 2 à 4 comprises), en **une** transaction courte :
verrou du job → état/jeton/échéance du bail → bail restant ≥ timeout client + 30 s → politique en
base → portes du processus → empreinte → échéance de la tâche → budget modèle → réservation →
commit ; puis seulement le POST (aucune transaction pendant le POST). Porte fermée ⇒
`ScopeClosed` : **aucun POST, aucune unité consommée**, job `pending` sans ré-enfilement ; bail
perdu ou insuffisant ⇒ `LeaseLost` : l'ancien propriétaire s'arrête. Avant chaque attente de
relance, un contrôle léger (`before_wait`) abandonne sans attendre. Les crochets **consultent** :
`analyze_crop` reste l'unique propriétaire des relances HTTP.

**Garantie de fermeture** : après le commit de `VisualScopePolicy.enabled = false`, aucune
tentative dont la réservation commence après ce commit n'envoie de POST. Peuvent encore partir :
le POST déjà en vol, ou **1** POST dont la réservation a été commitée juste avant la fermeture —
avec `worker_visual` à concurrence 1, **≤ 1 POST au total**.

**Ordre de révocation** : (1) `visual_v2_scope --space … --disable` (ou `--disable-all`) —
immédiat, sans redémarrage ; (2) attendre la fin/l'abandon des activations en vol ; (3) seulement
ensuite modifier l'environnement si nécessaire ; (4) recréer les services si l'environnement a
changé. Un changement d'environnement seul ne prend effet qu'au redémarrage (et l'arrêt
progressif laisse jusqu'à 120 s à l'ancien processus) : ne jamais s'y fier pour stopper les
relances.

**Balayeur** (`visual_v2_sweep_task`, toutes les `CELERY_BEAT_VISUAL_V2_SWEEP_SECONDS` = 300 s,
planifié seulement drapeau allumé hors production, sous `periodic_run`) : fail-closed (drapeau
éteint ou production ⇒ aucune requête). Lit l'index partiel `rag_vcj_open_idx` et
l'index des préparations. Ré-émet : préparation `pending`/`running` inactive depuis 10 min
(déclenchement ou continuation perdus) ; job `pending` jamais enfilé (fenêtre de 4) ; `pending`
enfilé depuis 10 min sans claim ; `deferred` échu depuis 5 min ; `running` à bail expiré (le
claim conserve tous les compteurs). Ne rouvre **jamais** `succeeded`, `terminal_failed`,
`parked`, `ineligible`, ni une préparation `done`/`failed`/`cancelled` ; n'envoie rien pour un
espace dont la porte est fermée. `enqueued_at` est commité avant chaque envoi (pas de ré-envoi à
chaque passe) ; préparations sélectionnées en `SKIP LOCKED`.

200, 504, timeout client, coupure ou crash après émission **consomment** l'unité réservée
(conservateur). 504 ×4 ⇒ `terminal_failed` / `vision_gateway_timeout`, jamais rouvert
automatiquement. Report = transition persistée + `apply_async(countdown)`, délai
`retry_after × U(0,8 ; 1,3)` borné à [1 s ; 120 s] (10 s si absent), jamais de `sleep`
dans l'orchestrateur. Budget mural : un POST n'est commencé que si `maintenant + attente +
timeout client ≤ limite souple (540 s) − 30 s` ; sinon état reprenable et ré-enfilement.

**Borne globale prouvée** : au plus `72 × 4 = 288` POST émis par clé de job et génération
(atteinte par le test adverse « 4 × 503 puis crash avant persistance du report » à chaque
activation), dont **au plus 4** consomment une inférence.

**Bail** : claim en transaction courte (`select_for_update(skip_locked=True)`), `running` +
`lease_token` + `lease_expires_at = maintenant + 660 s` (> limite dure 600 s). Bail expiré ⇒
réclamable, **compteurs conservés**. Toute écriture (réservation, remboursement, transition,
publication) revérifie état + jeton + échéance : un propriétaire périmé n'écrit rien.
Garantie : **au moins une fois** avec budget persistant (un POST réussi suivi d'un crash
avant publication peut être rejoué, et reste compté) — jamais « exactement une fois ».

**Publication** (une transaction) : K1 (`source_sha256 == crop_sha256`), K2 (contrat fermé,
déjà validé par le client), K3 (empreinte allowlistée), **K3+** (empreinte de la réponse ==
`expected_pipeline_fingerprint` du job) ; toute violation ⇒ `terminal_failed`, sans relance.
Le résultat reste dans `VisualCropJob.result_raw` : aucun serializer, aucune route, aucun
champ `Chunk.meta`, aucune citation.

**Équité** : une activation = un crop ; au plus 4 jobs enfilés ou en cours par version ×
génération (`fill_crop_job_window`, idempotente, qui ré-émet aussi un envoi perdu après
15 min) ; file `ingest_visual`.

**Métriques** (`/ops/metrics`, étiquettes fermées) : `visual_crops_total`,
`visual_success_total`, `visual_terminal_failure_total{code}`,
`visual_capacity_deferred_total`, `visual_parked_total`, `visual_gateway_503_total`,
`visual_gateway_504_total`, `visual_ineligible_total{reason}`, `visual_sweeper_runs_total`,
`visual_sweeper_recovered_jobs_total{reason}`, `visual_sweeper_recovered_prepares_total{reason}`,
`visual_scope_closed_attempt_total`. Journaux
`apps.rag_core.visual_v2` : identifiants, transition, compteurs, statut, code, latence —
jamais d'image, de Base64, de hash complet de crop ni de texte.

**Identité de validation** (CV-3E4A) : `create_visual_validation_identity --workspace-id <id>`
crée `svc-visual-validation@example.invalid` (mot de passe inutilisable, groupe
`ragatm-service-accounts`), lecteur du SEUL workspace synthétique désigné ; refusée en production
ou sur un workspace non synthétique. Les contrôles HTTP opératoires se font sous cette identité,
jamais au nom d'un propriétaire réel ; sur les espaces réels, la non-fuite se prouve sans HTTP
(tests de contrat, invariance des `Chunk`). Aucun contournement d'autorisation.

**Séquence d'activation staging (historique)** : (1) promotion du code
et de la migration `0045`, drapeau éteint, allowlists inchangées, observer zéro activité V2 ;
(2) activation étroite : un RagSpace synthétique dédié dans `VISUAL_V2_RAGSPACE_ALLOWLIST`,
empreinte V2 qualifiée dans `AI_GATEWAY_VISION_V2_ALLOWED_FINGERPRINTS`, drapeau allumé ;
(3) élargissement contrôlé. **Production** : refusée tant que `VISUAL_V2_PRODUCTION_ALLOWED`
est faux — et la Gateway de production ne sert que V1.

---

---

## 4sexies. Extraction structurée `chart` / `table` (`/v1/vision/structured-extract`) — *expérimental ; production activable par flag (tableaux seuls)*

**Contrat `ai-gateway-visual-contract/22A.1` (*implémenté*).** Version de schéma CHOISIE par
`STRUCTURED_VISUAL_SCHEMA_VERSION` (`1` par défaut, `2` opt-in), jamais découverte par essais : ne poser `2` qu'après
avoir vu `"2"` dans `schema_versions` de `GET /health/structured-extract` de l'environnement visé (Gateway production
V1 : `1` seulement, `2` → 400 ; staging V2 : `1` et `2`). Retour arrière : `1`, recréation des services.
Réponse : écho de `schema_version` exigé (sinon dérive de contrat, non relancée) ; en `2`, blocs `evidence`
(`certified` booléen) et `qualification` OBLIGATOIRES ; champs inconnus ignorés. Persistés par fragment dans
`meta.visual.structured` : `schema_version`, `evidence` (champs du contrat : `certified`, `evidence_level`,
`numeric_claims_supported`, `value_origin`, `confidence`, `consistency_status`, `consistency_anomalies`, `validation`),
`qualification` (`protocol`, `corpus`, `task_status`, `business_numeric_use_authorized`), `item_consistency`
(`status`, `anomalies`). Relance : au plus une, seulement si le Gateway déclare `retryable`, après `Retry-After`
(`structx_gpu_busy`, `structx_busy`, `structured_extract_overloaded`, 503 réessayables, Gateway injoignable) ; un
refus déclaré non réessayable n'est jamais rejoué. describe : un 502 `retryable: false` (troncature déterministe)
n'est jamais rejoué. Valeur probante : voir [pipeline-retrieval.md](../03-backend/pipeline-retrieval.md) — aucune
sortie actuelle (`certified` toujours faux) n'est preuve chiffrée.

Le Gateway expose
`POST /v1/vision/structured-extract` (contrat `structured-extract/1`, moteur PP-StructureV3 /
PP-Chart2Table côté Gateway, `experimental: true`, `qualified_for_production: false`).
RAGatm l'appelle **toujours via le Gateway**, jamais le backend `structx` directement.

**Porte** : `STRUCTURED_VISUAL_ENABLED` ∧ environnement admis ∧ espace ∈
`STRUCTURED_VISUAL_RAGSPACE_ALLOWLIST` (vide = aucun espace) ∧ tâche admise. Tout est **éteint par
défaut** (drapeaux faux, bornes à 0 = héritage, allowlist vide).

**Garde de production et politique des types** :

| Réglage | Défaut | Effet |
|---|---|---|
| `STRUCTURED_VISUAL_PRODUCTION_ALLOWED` | `0` | Avec `DJANGO_ENV` = `prod`/`production`, toute extraction est refusée tant qu'il n'est pas **vrai** (`structured_visual_production_refused`). Ne suffit jamais seul : drapeau principal et allowlist restent exigés. Sans effet hors production. |
| `STRUCTURED_VISUAL_ALLOWED_TASKS` | vide | Tâches admises (`chart`, `table`) ; vide = toutes hors production. En production, **plafond codé à `table`** qu'aucune valeur n'élargit. Valeur inconnue ignorée ; liste sans valeur valide = rien. |

`DJANGO_ENV` inconnu (hors `dev`, `test`, `staging`, `prod`, `production`) : refus
(`structured_visual_environment_unknown`). Une tâche exclue n'est **jamais appelée** (classe ignorée
par le chemin automatique avec `skip_reason`, commande refusée, bascule de tâche interdite vers elle).
**Dernière barrière avant écriture** (`publication_refusal`) : tâche admise, `task` de la réponse =
tâche demandée, aucun élément portant les clés de l'autre type (réponse mixte), chaque fragment
normalisé marqué structuré et du même type ; sinon rien n'est publié (statut `refused` consigné).
La portée est revérifiée sous verrou à la publication : une dérogation retirée pendant l'appel ne
publie rien. `qualified_for_production` n'est vrai que si le Gateway rend exactement `true` ;
`experimental` n'est faux que si le Gateway rend exactement `false`. Publier des tableaux est une
décision de **périmètre**, pas une certification de leurs valeurs. Le shadow des courbes garde sa
propre garde de production, dérogation comprise. Sans `STRUCTURED_VISUAL_AUTO_ENABLED`, seul
un opérateur déclenche une extraction (visuel et tâche explicites) ; avec, voir « Chemin
automatique » ci-dessous.

```bash
python manage.py structured_visual_extract --document <uuid> --list            # lecture seule
python manage.py structured_visual_extract --document <uuid> --asset <sha256> --task chart [--dry-run]
```

**Requête** : `{schema_version: "1", task, image: {data, mime_type}, request_id}` — rien d'autre
(ni contexte, ni provenance). **Réponse** : `items` (chart : `categories`, `series[].values`,
`series[].raw_values`, `native_text` ; table : `n_rows`, `n_cols`, `headers`, `cells[]` avec
`row_span`/`col_span`, `html`), `title_candidates`, `warnings`, `engine`, `raw_output`, `timings`.

**Erreurs et délais** : classement identique au client V1 (`detail.code`, `retryable`,
`Retry-After`). `503 structx_gpu_busy` / `structured_extract_overloaded` : **une** relance au plus,
si `Retry-After` ≤ 30 s ; un timeout client n'est jamais rejoué. `STRUCTURED_VISUAL_TIMEOUT_SECONDS`
= 180 s (démarrage à froid ~15 s, timeout Gateway 150 s).

**Normalisation et publication** (`visual/structured.py`) :

- graphique → une unité texte (catégories, séries, valeurs **telles que rendues** par
  `raw_values`, texte natif du moteur, source et page, mention expérimentale). Aucun titre, unité
  ni axe inventé ; `title_candidates` est présenté comme candidat ; orientation jamais inversée ;
- **identité du graphique** (lot EXTRACTION-FIDELITY-10) : le titre imprimé est lu dans l'OCR du
  moteur (`raw_output.ocr` : lignes de score ≥ 0,8, dans la région du graphique, au-dessus de la
  plus haute graduation de l'axe des valeurs), regroupé par rangée ; les identifiants
  alphanumériques du titre (≥ 6 caractères dont ≥ 4 chiffres, hors dates) sont listés à part :
  lecture **nette** (lettres en préfixe, chiffres, ≤ 3 lettres en suffixe), avec la consigne de
  comparaison caractère par caractère, ou lecture **incertaine** (lettre au milieu des chiffres,
  ex. `Scooo0o08766`), déclarée comme telle et jamais corrigée. Le titre est présenté comme une
  lecture OCR brute. Ils entrent dans le texte du fragment (donc dans le lexical et
  l'embedding) et dans `meta.visual.structured.identity`. Jamais lus dans la description
  `describe` (qui peut mal recopier un identifiant) ;
- **sémantique des valeurs** : le moteur relève les séries aux seules catégories (souvent les
  graduations). Le fragment le déclare (`value_semantics = points_at_categories`), donne par série
  le maximum et le minimum **parmi les points relevés** et précise que le maximum réel d'une courbe
  continue n'est pas déterminable entre deux points. L'échelle de l'axe des valeurs est lue dans
  l'OCR (graduations gauche, pas d'axe secondaire) ; une valeur hors de cette échelle est signalée
  « non fiable ». Aucune valeur nouvelle n'est calculée ni estimée ;
- **unité d'un graphique long** : découpée comme celle d'un tableau (parties ≤ 1 800 c sur
  frontières de ligne, sous la taille de fragment) ; chaque partie répète le titre, les
  identifiants, l'en-tête du bloc en cours (série, réserve de lecture, rangée de titres du tableau
  natif) et porte `meta.visual.structured.part` — sans quoi le découpeur générique coupait
  l'unité à 2 500 c et la suite perdait l'identité du graphique ;
- tableau → `LogicalTable` passé au rail tabulaire générique existant (un fragment par ligne,
  chaînes jamais converties en nombres) + une unité d'ensemble (grille, cellules fusionnées). La
  rangée 0 n'étiquette les colonnes que si elle est un en-tête SIMPLE (une ancre par colonne, aucune
  fusion, ≥ 2 libellés) ; sinon colonnes neutres `Column N` — un titre fusionné sur la largeur ou
  un tableau en plusieurs blocs d'en-têtes rattacherait sinon des valeurs à la mauvaise colonne ;
- **sûreté des tableaux** (lot STRUCTVIS-TABLE-SAFETY-3) — contrôles **avant** toute normalisation,
  refus de la réponse entière (`refused` + `refusal_reason` au registre, aucun fragment, aucun
  embedding, rien au retrieval ni au prompt), jamais de réparation :
  - **grille** (`structured_visual_table_grid_invalid`) : coordonnées entières ≥ 0, spans ≥ 1
    (absents = 1), ancre en double ou recouvrement, cellule hors de `n_rows`/`n_cols` déclarés,
    grille > 10 000 cellules (une taille démesurée faisait boucler la normalisation) ;
  - **preuve positive de région tableau** (`structured_visual_table_region_unproven`, lot
    NO-LAYOUT-FAIL-CLOSED-4) : un tableau dont au moins une cellule porte du texte n'est publié que
    si une boîte de mise en page `table` du moteur (`raw_output.layout_boxes`) — 4 nombres finis,
    non dégénérée, non inversée, dans le visuel traité (`image.width`/`height`, tolérance 2 px) —
    couvre ≥ 80 % de la `region_bbox` de l'élément, elle-même valide, dans le visuel et d'au moins
    8 px de côté (une région en coordonnées de page PDF ou normalisées 0-1 n'est jamais comparée).
    `task=table`, des cellules ou des chiffres ne prouvent rien ; aucun repli. Raison consignée
    (`refusal_reason`) : `layout_missing`, `layout_empty`, `visual_size_unknown`, `region_invalid`,
    `no_table_layout_box` (graphique pris pour un tableau, boîte mal formée ou hors visuel,
    plusieurs boîtes dont aucune ne porte le tableau). Les réponses sans aucune boîte (2/113 en
    staging historique, publiées jusqu'au lot TABLE-SAFETY-3) sont désormais **refusées**.
    Un élément sans cellule renseignée n'a rien à publier : statut `empty` inchangé ;
  - **en-tête hiérarchique** : représenté seulement sous sa forme sans ambiguïté — coin (0, 0)
    fusionné sur les h rangées du bloc (h ≤ 4, au moins une rangée de données), bloc entièrement
    couvert, aucune cellule d'en-tête débordant dans les données, une feuille par colonne, aucun
    bloc d'en-tête répété plus bas. Chaque valeur porte alors le **chemin complet** de sa colonne
    (`Consommation 2025 (MWh) / S1 / Jan`), l'unité d'ensemble liste ces chemins et les rangées
    d'en-tête ne sont plus publiées comme des données. Toute autre fusion horizontale de la
    rangée 0 (hors titre pleine largeur) : refus `structured_visual_complex_header_unrepresentable`,
    jamais une forme aplatie qui changerait le sens des valeurs ;
  - **groupe de lignes** (lot PAGE-TABLE-ROUTING-1) : une rangée de données dont l'ancre couvre
    toute la largeur est un intitulé de **section**, pas une donnée. Chaque ligne qui suit porte
    `Context: … Groupe : <section>` dans son fragment et `meta.row_group_path`. Un libellé
    répété dans un même groupe (ou sans groupe, ex. lignes en gras sans signal de grille) est
    **signalé** : occurrence k/n, « groupe parent non déterminé », `meta.row_group_ambiguous` ;
    aucun groupe n'est deviné. Une section rendue vide par le moteur suivie de libellés
    homonymes : refus `structured_visual_row_group_ambiguous` ;
  - **identité du tableau** : titre imprimé lu dans l'OCR du moteur à l'intérieur de la boîte
    de titre (`table_title`, `figure_title`, `paragraph_title`) la plus proche **au-dessus** de
    la région (écart ≤ 60 px, recouvrement horizontal). Porté par chaque ligne
    (`Context: Tableau : …`, `meta.table_title`) pour qu'une ligne ne réponde pas à une question
    d'un autre document ;
  - **cellule fusionnée verticalement** (une colonne, plusieurs lignes de données) : qualifie
    chaque ligne couverte ; une fusion horizontale reste écrite une fois, à son ancre ;
  - **valeurs glissées dans la colonne des libellés** (lot COLUMN-SHIFT-GUARD-1, constat réel
    staging : libellés de lignes fusionnés par le moteur, 162 valeurs sous la colonne voisine) :
    les colonnes numériques des lignes **libellées** (lettre en colonne 0) servent de référence ;
    une ligne dont la colonne 0 ne porte que des nombres, et dont les colonnes numériques
    décalées d'un cran à droite reproduisent exactement celles d'une ligne libellée (colonne
    suivante vide), est **décalée**. Au moins 2 lignes décalées, formant au moins la moitié des
    lignes à nombre en tête : refus `structured_visual_column_association_unsafe` (raison
    `row_values_shifted_into_label_column`). Rien n'est déplacé ni reconstruit. Un tableau
    d'identifiants numériques (années, repères) garde ses valeurs dans les mêmes colonnes que
    ses totaux : non concerné (0 refus sur 15 grilles réelles valides) ;
  - **limite** : un décalage de colonnes qui laisse une grille cohérente (mois glissés d'un cran,
    ou toutes les lignes décalées sans ligne libellée de référence) n'est **pas détectable** dans
    la sortie seule et reste publié — voir
    [limites connues](../10-reference/limites-connues.md) ;
- fragments ajoutés par `prepare_visual_asset_candidate` (mêmes embeddings, même pipeline), sous
  verrou RagSpace puis Document, **sans republier le document** ; provenance `meta.visual`
  (empreinte du visuel, page, repère) + `meta.visual.structured` (moteur, versions, build Gateway,
  `request_id`, empreinte de la réponse) ; `requires_visual_check = true` partout ;
- second passage identique : remplace les seuls fragments du même (document, visuel, tâche) ;
- registre `Document.meta["structured_visual"]["<sha256>:<tâche>"]` (statut, avertissements,
  empreinte de la réponse ; échec Gateway consigné sans toucher au document).

**Coexistence avec `describe`** : le marqueur `meta.visual.structured` exclut ces fragments de
`published_visual_hashes` (un visuel extrait reste à décrire), de la suppression par visuel du
rail V1 (une description n'efface pas l'extraction) et du report à la réindexation (une
réindexation ne la reporte pas : le chemin automatique la **régénère**). Le voisinage visuel du
retrieval ne les promeut pas (`requires_visual_check`) ; le retrieval ordinaire les trouve.

**Chemin automatique** (*activable par flag*, qualification interne,
`visual/structured_auto.py`) : porte = `STRUCTURED_VISUAL_AUTO_ENABLED` ∧ porte ci-dessus.
En fin de `visual_enrichment_task` (génération réglée), `structured_visual_task` est enfilée
sur `ingest_visual` :

1. **Classification** déterministe de chaque visuel de la génération, depuis son évidence
   `describe` PUBLIÉE (fraîche ou reportée) : `visual_kind` `chart`/`table`, sinon la tête
   nominale de la première phrase de `Description:` (« graphique(s) », « histogramme(s) »,
   « courbe(s) » hors adjectif « ligne(s) courbe(s) », « diagramme(s) circulaire(s) / en anneau /
   en barres » → CHART ; « tableau », « registre » → TABLE, hors « tableau électrique/blanc… ») ;
   sinon (lot PAGE-TABLE-ROUTING-1) les **noms** d'objets de la section `Objects:` du contrat
   `describe` (« - Tableau (count: 1; …) ») : un tableau → TABLE, à défaut un graphique → CHART.
   Cas d'une page entière décrite comme « un rapport… » qui liste un tableau. Sinon (lot
   TABLE-RECOVERY-1, constat réel : objet « Section 5. … » d'attributs « tableau avec colonnes »,
   page jamais routée) le **corps** de `Description:` et les **attributs** d'objets qui nomment un
   tableau → TABLE (candidat), sauf si un graphique y est nommé. Le texte visible (OCR de la page)
   ne déclenche jamais rien ; la tête de la première phrase garde la priorité. Coût : un appel
   d'extraction de plus, refusé (`structured_visual_table_region_unproven`) si le moteur ne voit
   aucune boîte `table`. Aucune tête ni objet → OTHER (photo, plan, carte, diagramme, logo) ; pas de
   description → UNKNOWN. Router plus de pages ne publie rien de plus sans preuve : région,
   grille et politique de tâches restent appliquées.
   OTHER/UNKNOWN : **aucun appel** (repli = commande `--task auto|chart|table`).
2. **Routage** CHART → `task=chart`, TABLE → `task=table` ; si le moteur ne rend rien et que
   sa mise en page (`raw_output.layout_boxes`) voit l'autre type, **une** bascule de tâche.
3. **GPU occupé** : aucun sommeil dans le worker ; état persisté
   (`Document.meta["structured_visual"]["auto"]`, statut par visuel) et reprise
   `apply_async(countdown = max(Retry-After, 30/60/120/180/240/300 s))`, 7 passes au plus,
   puis `failed` (rattrapage : `--auto-document`). Le document n'est jamais modifié.
4. **Cycle de vie** : publication refusée sous verrou si la génération d'enrichissement a
   changé (réindexation), si le document a disparu ou l'espace est supprimé ; réindexation =
   nouvelle génération = fragments supprimés puis régénérés ; suppression = cascade.

**Borne de visuels PDF des espaces structurés** (*expérimental*) :
`STRUCTURED_VISUAL_PDF_MAX_ASSETS` (0 = hériter de `DOCUMENT_PDF_VISUAL_MAX_ASSETS`, 12) remplace la
borne de visuels PDF admis pour les seuls espaces de l'allowlist structurée (drapeau allumé,
hors production), à la passe primaire ET à la redécouverte de l'enrichissement (mêmes
empreintes). Seul le budget change : heuristiques, borne par page (4), sélection S4-prop au-delà
du budget et classification sont inchangées. Plafonnée à 32 (borne du rail) ; la passe
automatique suit la borne du rail (32, au lieu de 16). Coût : un appel `describe` par visuel
admis, plus un `structured-extract` par graphique/tableau.

**Borne de visuels PDF PAR PAGE des espaces structurés** (*expérimental*) :
`STRUCTURED_VISUAL_PDF_MAX_PER_PAGE` (0 = hériter de
`DOCUMENT_PDF_VISUAL_MAX_PER_PAGE`, 4 ; ≤ 8) remplace la borne par page pour les mêmes seuls espaces,
avec la même porte et le même transport que la borne par document. La borne par page agit à deux
endroits : pendant S4-prop (vivier au-delà du budget du document) et à l'admission (ordre de
découverte) ; la borne par document (32) reste la seule borne globale.

**Unité d'ensemble d'un tableau** : découpée en parties ≤ 1 800 c sur frontières de ligne, la
dernière rangée d'en-tête de bloc (≥ 2 libellés, sans chiffre) répétée en tête de partie ;
rangées vides omises (et signalées).

**Contexte du chat** : un fragment `meta.visual.structured` n'est plus fenêtré à
`RAG_SNIPPET_MAX_CHARS` (800 en staging) mais suit `RAG_SNIPPET_MAX_CHARS_STRUCTURED` (4 000) :
entier s'il tient ; sinon contexte et en-têtes de bloc gardés, lignes de données choisies par
appariement à la question puis remises dans l'ordre, marqueur `[Extrait structuré PARTIEL :
k/n lignes…]`. Même règle pour le rognage de la dernière source. Le budget GLOBAL
(`RAG_MAX_PROMPT_CHARS`) s'applique inchangé ; les autres extraits ne changent pas.

**Contrôles au démarrage** (`checks_structured_visual.py`, *jamais bloquants* — le code refuse
déjà ; seul le constat manquait) :

| ID | Condition |
|---|---|
| `rag_core.W130` | réglage structuré posé en production sans `STRUCTURED_VISUAL_PRODUCTION_ALLOWED`, ou shadow des courbes configuré en production : ignoré, aucun appel |
| `rag_core.W131` | `STRUCTURED_VISUAL_ENABLED` allumé, allowlist vide : aucun espace concerné |
| `rag_core.W132` | `STRUCTURED_VISUAL_AUTO_ENABLED` / `_PDF_MAX_ASSETS` / `_PDF_MAX_PER_PAGE` / `_CURVE_SHADOW_ENABLED` / `_PRODUCTION_ALLOWED` sans le drapeau : inertes |
| `rag_core.W133` | `STRUCTURED_VISUAL_CURVE_SHADOW_ENABLED` allumé, allowlist shadow vide : aucun espace concerné |
| `rag_core.W134` | drapeau allumé, `DJANGO_ENV` inconnu : refusé |
| `rag_core.W135` | `STRUCTURED_VISUAL_ALLOWED_TASKS` : valeur inconnue, ou `chart` écarté par le plafond de production |

**Dépendance Gateway** : la route est exposée par le Gateway staging (8200) **et** production (8100)
depuis le build de référence (qualification interne, 02/11/2026). Un Gateway sans la route répond 404 : erreur typée **non relancée**,
visuel marqué `failed` dans `Document.meta["structured_visual"]["auto"]`, document intact. Ne pas
allumer le drapeau dans un environnement dont le Gateway n'expose pas la route.

**Limites connues** (corpus pilote, 22 PDF) : voir
[limites connues](../10-reference/limites-connues.md#extraction-structurée-charttable--désactivée-par-défaut).

### Numérisation des courbes en mode SHADOW — *expérimental, activable par flag, staging seulement*

Après l'extraction structurée d'un graphique
(`extract_asset`, tâche `chart` — commande opérateur ET chemin automatique), le prototype CPU
**figé** du lot 13B calcule le maximum et le minimum de chaque série nommée **depuis les pixels**
du visuel et l'OCR du moteur déjà présents dans la réponse. Aucun appel Gateway, aucun GPU.

**Isolation** : le chemin shadow s'exécute après la normalisation, sur une copie de l'OCR ;
ses résultats (`ShadowResult`) ne vivent que dans `StructuredOutcome.curve_shadow`, en mémoire.
Ils n'entrent **jamais** dans un fragment, une métadonnée, `Document.meta`, l'index lexical, les
embeddings, le retrieval, le prompt ni une API ; aucune base n'est écrite. Le journal ne porte
qu'une ligne de **comptes** par visuel (`curve shadow … measured=… errors=… ms=…`) : ni valeur,
ni nom de série, ni texte OCR. Les valeurs ne sont lisibles que par un harnais d'audit qui
appelle `extract_asset` lui-même.

**Porte** : `STRUCTURED_VISUAL_CURVE_SHADOW_ENABLED` ∧ `STRUCTURED_VISUAL_ENABLED` ∧ espace dans
`STRUCTURED_VISUAL_RAGSPACE_ALLOWLIST` **et** dans `STRUCTURED_VISUAL_CURVE_SHADOW_RAGSPACE_ALLOWLIST`
∧ `DJANGO_ENV` ∉ {`prod`, `production`} — la production est refusée même drapeaux allumés
(`curve_shadow_production_refused`). Aucune numérisation rétroactive : seul un nouveau passage
d'extraction déclenche le shadow.

**Statuts** : `MEASURED_FROM_PIXELS` (toutes les barrières du prototype passent : zone de tracé
= domaine étalonné, textes OCR et traits de légende masqués, composantes connexes larges, pas
d'extrapolation, pas de lacune ni de texte au pic, intervalle ≤ 4 % de l'étendue),
`UNRELIABLE` (valeur gardée en `diagnostic_value` seulement), `NOT_DETERMINABLE`, `UNSUPPORTED`
(plusieurs graphiques dans la réponse, plus de 6 séries, image > 2 Mpx), `ERROR` (image
illisible, exception, `MemoryError`, appel > 1 s écarté, budget de 2 s par visuel épuisé).
`ESTIMATED_FROM_PIXELS` est réservé et jamais produit. **`MEASURED_FROM_PIXELS` ne veut pas dire
« valeur vérifiée »** : la précision numérique n'est pas qualifiée (lot 13B : couverture 41,7 %
du jeu tenu à l'écart, aucune annotation humaine indépendante).

**Garde-fous de sécurité** (qualification interne, `visual/curve_guards.py`) : le
lot 15 a montré, sur vérité synthétique, deux façons pour le moteur figé de rendre une valeur fausse avec
le statut fiable. Sans changer aucun calcul du moteur, le shadow s'abstient désormais :

- **second axe Y à droite** (vérifié AVANT la mesure, OCR seul) : une colonne d'au moins 3 graduations
  numériques alignées à droite, distinctes et décroissantes vers le bas -> `UNSUPPORTED`
  (`dual_y_axis_unsupported`) pour tout le graphique, jamais l'échelle gauche par défaut ; 2 graduations
  dans cette configuration -> `NOT_DETERMINABLE` (`possible_right_y_axis`). Un nombre isolé, une légende
  textuelle ou une rangée d'étiquettes X ne déclenchent rien. Les graphiques à deux axes ne sont PAS pris
  en charge ;
- **série neutre sur un axe ou une grille de même couleur** (APRÈS la mesure) : pour une série grise ou
  noire, on repère les rangées/colonnes droites de pixels neutres couvrant au moins 60 % du tracé ; si
  leur couleur entre dans le masque de la série et que l'extremum tombe dessus -> `UNRELIABLE`
  (`neutral_series_axis_grid_ambiguity`), valeur seulement diagnostique. Une série neutre distincte de la
  grille, et toute série colorée, restent mesurées.

Effet mesuré (vérité synthétique) : 0 valeur fausse fiable sur 216 + 136 extrema (contre 8 + 12) ; prix :
abstention sur les graphiques à deux axes (y compris la série de gauche) et sur un axe unique portant deux
étiquettes de valeur alignées à droite.

**Graduations Y dupliquées — C2** (qualification interne) : quand
une même valeur est lue deux fois dans la colonne des graduations gauches (typiquement le « 0 » de l'axe X
sous le « 0 » de l'axe Y), `calibrate_y` garde la boîte la plus à gauche au lieu de faire échouer
l'étalonnage. Seules les valeurs STRICTEMENT égales sont concernées ; une étiquette X de valeur différente
dans la colonne Y reste une cause d'abstention. C2 agit APRÈS le garde double axe (OCR brut) et AVANT le
garde série neutre. Mesuré : 0 valeur fausse fiable sur 472 extrema synthétiques ; +24 justes ; +3 mesures
réelles techniquement acceptées (non validées indépendamment).

**Prototype intégré** : `visual/curve_digitization.py` est le fichier figé du lot 13B (SHA-256
`49c8dbca…`) à **deux écarts près** — le bloc C2 ci-dessus et l'import `from scipy import ndimage`, remplacé par
`visual/curve_ndimage.py` (étiquetage 8-connexe et boîtes englobantes, même numérotation que
scipy) parce que scipy n'est pas dans les images. Un test reconstitue le fichier d'origine (en retirant
exactement le bloc C2 et en rétablissant l'import) et vérifie son empreinte. Bornes : le calcul n'est pas préemptif — la borne dure est la taille de
l'image (2 Mpx).

## 4septies. AI Gateway Pool — refus propres au pool — *implémenté*

RAGatm peut joindre la Gateway **à travers AI Gateway Pool** (routeur interne)
sans changer de client : mêmes routes, mêmes corps, réponses de la Gateway relayées à
l'identique (`{"detail": …}`, statut, `Retry-After`). Qualifiés en live via le pool : embeddings, rerank, ASR, transport génération/flux sur
`llama3.1:8b`. **Non qualifiés en live** : génération/flux sur `qwen3:30b-instruct`, vision,
StructX ([limites connues](../10-reference/limites-connues.md)).

Le pool émet aussi **ses propres refus**, reconnaissables à l'en-tête `X-AIGP-Error: <CODE>`
(jamais posé sur une réponse relayée) et au corps `{"error": {"code", "message", "request_id"}}`,
codes en **majuscules**. `apps.llm_router.gateway.pool_errors` les lit **uniquement** si
l'en-tête est présent, et rend un constat typé : code d'origine, code normalisé
`pool_<code>` (préfixe : aucune synonymie avec un code Gateway), statut HTTP, `request_id`,
`Retry-After`, catégorie, issue amont prouvée (`not_executed` / `uncertain`). Le `message`
n'est jamais lu.

| Client | Effet d'un refus du pool |
|---|---|
| génération / flux | exception typée inchangée par statut, `pool_error` rattaché, `pool_code=` dans le message. Catégories configuration, sécurité, contrat, modèle, interne ou code inconnu → `GatewayContractError` (**jamais éligible au repli**, même sous 500/501/503). |
| vision V1/V2, StructX | `code` client inchangé ; `pool_error` et `request_id` rattachés ; relance retirée hors indisponibilité (routage, transport, affinité). |
| embeddings gouvernés | `code` reste `http_<status>` ; `pool_error` rattaché, `pool_code=` dans le message. |
| rerank | motif `HTTP <statut> pool=<CODE>` (préfixe des compteurs inchangé). |
| ASR | voir [ingestion audio](../03-backend/ingestion-audio.md) : jamais terminal en relecture, jamais rejouable au dépôt. |

**Aucun refus du pool n'autorise le rejeu d'un POST** : `UPSTREAM_TIMEOUT`,
`UPSTREAM_UNAVAILABLE`, `AFFINITY_STORE_UNAVAILABLE` et `JOB_AFFINITY_CONFLICT` laissent
l'issue amont incertaine. `MODEL_REQUIRED` (400) n'apparaît que si plusieurs nœuds n'ont pas
le même modèle par défaut : la génération RAGatm envoie toujours un modèle (espace ou
`AI_GATEWAY_MODEL`), la vision dès que `AI_GATEWAY_VISION_MODEL` est posé ; StructX n'a pas
de champ `model` dans le contrat Gateway.

## 5. Sécurité

- **Jamais exposé à Internet.** Publication hôte bornée en loopback `127.0.0.1`
  (port 8100). Ollama (11434) **jamais publié**.
  Ces invariants sont **testés** (`tests/test_deploy_compose.py` : loopback only, pas de 11434).
- Accès depuis un autre hôte = allowlist de pare-feu, jamais via binding réseau.
- **Aucun prompt / secret journalisé** : les logs ne contiennent que code d'erreur, modèle et
  `request_id` (testé `test_error_body_does_not_leak_prompt`).
- Le check `llm_router.E002` refuse une `AI_GATEWAY_BASE_URL` vide **ou** loopback quand la
  Gateway est active en env officiel (l'URL doit être routable). `llm_router.E007` étend le
  contrôle aux trois URL (`AI_GATEWAY_BASE_URL`, `AI_GATEWAY_EMBEDDINGS_BASE_URL`,
  `RAG_RERANKER_BASE_URL`) : schéma http(s) et hôte requis, loopback refusé pour les
  embeddings et le rerank, port 11434 (Ollama direct) refusé. La Gateway directe et le pool
  sont acceptés.
  `llm_router.E008` refuse les trois URL acheminées différemment (bascule partielle, ou deux
  pools distincts) : aucune correction silencieuse.
- Profils d'embeddings : un espace `ai_gateway` dont l'URL d'embeddings vise un pool qui
  **déclare** ses profils (production : `legacy-768` seul) ne peut pas choisir un autre profil
  (`embedding_model_not_declared_by_pool`) ; aucun espace existant n'est modifié.

---

## 6. Exploitation

- **Contrôle démarrage** : `cd backend && python manage.py check` doit être vert (Gateway ON,
  base URL routable). Les system checks bloquent tout démarrage non
  conforme en env officiel.
- **Rollback** : **ne jamais** revenir à `RAG_LLM_PROVIDER=ollama` ni `AI_GATEWAY_ENABLED=0`
  en déploiement réel. Dépannage/tests seulement : `ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS=1`.

---

## Références d'implémentation

- `backend/apps/llm_router/gateway/{vision_v2,vision_contract_v2}.py`, `backend/apps/rag_core/services/raster/vision.py` (rail V2 des crops)
- `backend/apps/llm_router/gateway/vision_probe.py` (sondes capacité et `/health/vision`), `backend/apps/rag_core/services/raster/orchestration.py`, `backend/apps/rag_core/tasks.py` (`visual_v2_prepare_version`, `analyze_crop_job`), `backend/apps/rag_core/management/commands/{visual_v2_prepare,visual_v2_scope,create_visual_validation_identity}.py`, `backend/apps/rag_core/models/visual_policy.py` (orchestration CV-3E1, durcissement CV-3E4A)
- `backend/apps/llm_router/gateway/retry_after.py` — lecture `Retry-After` PARTAGÉE par les clients vision V1 et V2 (`parse_retry_after`)
- `backend/apps/llm_router/gateway/pool_errors.py` — refus propres à AI Gateway Pool (`parse_pool_error`, `attach_pool_error`) ; `backend/apps/llm_router/gateway/endpoints.py` (résolution et sonde non inférentielle des trois URL)
- `services/ai_gateway/app/{main,service,schemas,ollama_client,errors,config}.py`
- `services/ai_gateway/tests/{test_generate,test_health,test_deploy_compose}.py`
- `backend/apps/llm_router/router.py`
- `backend/apps/llm_router/gateway/{client,dto}.py`
- `backend/apps/llm_router/gateway/{vision,vision_contract,exceptions}.py` (client vision V1)
- `backend/apps/rag_core/services/ingest/visual/{contracts,table_adapter,normalizers}.py`
- `tests/tests_root/llm_router/test_vision_gateway_client.py`, `tests/tests_root/unit/visual/`
- `backend/apps/llm_router/checks.py`, `backend/apps/llm_router/apps.py`
- `backend/config/settings/base.py`
- `backend/apps/llm_router/gateway/structured_extract.py` — client `structured-extract/1` (schémas `1` / `2`, contrat 22A.1) ; `tests/tests_root/unit/visual/test_gateway_contract_22a.py` + `fixtures/gateway_22A1_response_examples.json` (exemples RÉELS du contrat, empreinte du manifeste)
- `backend/apps/rag_core/services/ingest/visual/structured.py` — normalisation et publication
- `backend/apps/rag_core/management/commands/structured_visual_extract.py` — commande opérateur
- `tests/tests_root/integration/test_structured_visual_extract.py`
- `backend/apps/rag_core/services/ingest/visual/structured_auto.py` — classification et passe automatique
- `backend/apps/rag_core/checks_structured_visual.py` — avertissements de démarrage `rag_core.W130`–`W135`
- `tests/tests_root/integration/test_structured_visual_production_gates.py` — garde de production, politique tables seules, barrière de publication
- `backend/apps/rag_core/services/ingest/visual/{curve_shadow,curve_digitization,curve_ndimage,curve_guards}.py` — numérisation des courbes en mode shadow et garde-fous
- `tests/tests_root/unit/visual/test_curve_shadow.py`, `tests/tests_root/unit/visual/test_curve_guards.py`, `tests/tests_root/integration/test_curve_shadow_publication.py`
- `tests/tests_root/integration/test_structured_visual_default_off.py` — inertie par défaut et garde de production
- `backend/apps/rag_core/services/query/prompt_builder.py` — `_select_structured_text`
- `tests/tests_root/integration/test_structured_visual_auto.py`
