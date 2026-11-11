# Variables d'environnement

> **Référence machine : [configuration.md](configuration.md)** — document **généré** depuis le
> registre canonique [`backend/config/registry/`](../../backend/config/registry/) et vérifié par
> la CI (types, défauts, bornes, portées, secrets, dépréciations). En cas de divergence entre les
> deux pages, `configuration.md` fait foi pour les **métadonnées** ; la présente page reste la
> référence pour la **prose d'exploitation** (effets, anomalies, procédures).
>
> Vérifier la cohérence : `make config-audit`. Régénérer : `make config-reference`.

Référence exhaustive des variables lues par RagATM. **Source unique** : les autres docs
pointent ici plutôt que de dupliquer les valeurs. Public : ops, développeurs, intégration CI.

Le CODE fait foi (`backend/config/settings/base.py`, `.../prod.py`, `entrypoint.sh`,
`config/celery.py`, `apps/rag_core/services/query/rewrite.py`, `apps/llm_router/embeddings.py`,
composes `deploy/docker/`). Les défauts cités sont les défauts *de code* ; le `.env.example`
et la policy opérationnelle peuvent diverger (voir la section [Anomalies](#anomalies--divergences)).

Conventions :

- **Requis ?** = « oui en prod » signifie que `config.settings.prod` lève une `RuntimeError`
  au démarrage si la variable est absente/invalide. En dev tout a un défaut.
- **Secret ?** = ne jamais journaliser ni committer la valeur réelle. Seuls les noms et des
  **placeholders** apparaissent dans cette doc.
- Flags booléens : voir [feature-flags.md](feature-flags.md). Ports : voir [ports.md](ports.md).

---

## Django / cœur

| Variable | Composant | Défaut | Requis ? | Secret ? | Effet |
|---|---|---|---|---|---|
| `DJANGO_ENV` | backend | `dev` | non | non | Sélectionne le profil (`dev`/`staging`/`prod`). `prod.py` n'accepte que `prod`/`staging`. `base.py` refuse un boot prod-like sous le module `base`. |
| `DJANGO_SETTINGS_MODULE` | backend | `config.settings.dev` | oui (staging/prod = `config.settings.prod`) | non | Module settings actif (imposé par les composes). |
| `DJANGO_SECRET_KEY` | backend | `dev-secret-key` | **oui en prod** | **OUI** | Clé Django. `RuntimeError` en prod si égale au fallback. |
| `DEBUG` | backend | `False` (base) / `True` (dev) / forcé `False` (prod/staging) | non | non | Mode debug. |
| `DJANGO_ALLOWED_HOSTS` | backend | vide | **oui en prod** | non | Liste CSV des hosts. Wildcard `*` refusé en prod. |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | backend | vide | recommandé prod | non | Origines CSRF de confiance (CSV). |
| `FRONTEND_APP_ORIGIN` | backend | `http://127.0.0.1:6300` | **oui en prod** (URL absolue) | non | **Source unique** des liens email (invitation/reset). Remplace les anciens `FRONTEND_BASE_URL`/`FRONTEND_URL`. |
| `LOAD_DOTENV` | backend | `0` | non | non | Charge `.env` hors dev quand `1`. |
| `DJANGO_SECURE_SSL_REDIRECT` | backend (prod) | `0` | non | non | Redirection HTTPS (compose met `0`, TLS géré au proxy). |
| `DJANGO_USE_X_FORWARDED_HOST` | backend (prod) | `0` (compose met `1`) | non | non | Respect du header `X-Forwarded-Host`. |
| `DRF_THROTTLE_ANON` | backend (prod) | `30/min` | non | non | Throttle DRF anonyme. |
| `DRF_THROTTLE_USER` | backend (prod) | `120/min` | non | non | Throttle DRF authentifié. |
| `PUBLIC_BASE_URL` | frontend/compose | — | non | non | Origine publique injectée au front. Lue par compose/front, **pas** par `settings`. |
| `APP_ENV` | compose | — | non | non | Alias de lisibilité staging (posé par compose, non lu par le code). |

---

## Cookies / authentification

Voir [../05-api/authentification.md](../05-api/authentification.md) pour le mécanisme
(jeton d'accès post-MFA transporté par cookie HttpOnly + CSRF double-submit). Le
second facteur est **obligatoire et non désactivable** : aucune variable ne le coupe
([authentification-mfa.md](../03-backend/authentification-mfa.md)).

| Variable | Composant | Défaut | Requis ? | Secret ? | Effet |
|---|---|---|---|---|---|
| `AUTH_COOKIE_NAME` | backend | `ragatm_auth_token` | non | non | Nom du cookie porteur du token DRF. |
| `AUTH_COOKIE_MAX_AGE` | backend | `1209600` (14 j) | non | non | Durée de vie du cookie **et** expiration serveur du jeton d'accès. |
| `AUTH_COOKIE_SECURE` | backend | `0` (base) / forcé `True` (prod) | non | non | Flag `Secure`. |
| `AUTH_COOKIE_SAMESITE` | backend | `Lax` | non | non | `SameSite` (Lax volontaire même en prod). |
| `AUTH_COOKIE_PATH` | backend | `/` | non | non | Path du cookie. |
| `AUTH_PASSWORD_MIN_LENGTH` | backend | `10` | non | non | Longueur minimale des mots de passe. |
| `MFA_CHALLENGE_TTL_SECONDS` | backend | `600` | non | non | Durée du challenge entre mot de passe et code TOTP (et du premier enrôlement). |
| `MFA_CHALLENGE_MAX_ATTEMPTS` | backend | `5` | non | non | Codes faux tolérés par challenge avant invalidation. |
| `DRF_THROTTLE_AUTH` | backend | `20/min` | non | non | Débit du login (la force brute est bloquée par django-axes). |
| `DRF_THROTTLE_MFA` | backend | `10/min` | non | non | Débit des étapes MFA. |
| `DRF_THROTTLE_CHAT_THREAD_SEARCH` | backend | `60/min` | non | non | Débit de la recherche de conversations, par utilisateur, dans un seau **dédié** (la vue remplace les classes par défaut : la frappe ne consomme pas le seau `user` de `/auth/me/`). |
| `INVITE_RETURN_RESET_LINK` | backend | `0` / forcé `False` en prod | non | non (fuite si activé) | **Legacy** : expose le `reset_link`/`email_error` dans l'ancien `POST /members/` (rollback DEV uniquement, sans effet sous v2). |

> Ces variables sont **lues par le code mais absentes des `.env*.example`** (voir Anomalies).

### Invitations workspace (v2)

Domaine dédié — voir [../03-backend/invitations.md](../03-backend/invitations.md) et le flag
`WORKSPACE_INVITATIONS_V2_ENABLED` dans [feature-flags.md](feature-flags.md).

| Variable | Composant | Défaut | Requis ? | Secret ? | Effet |
|---|---|---|---|---|---|
| `WORKSPACE_INVITATION_TTL_HOURS` | backend | `72` | non (positif obligatoire) | non | Durée de vie du lien d'invitation (heures, expiration stricte UTC). |
| `DRF_THROTTLE_INVITATION_CREATE` | backend | vide (base) / `20/min` (prod) | non | non | Débit de création d'invitations (ScopedRateThrottle). |
| `DRF_THROTTLE_INVITATION_REGENERATE` | backend | vide (base) / `20/min` (prod) | non | non | Débit de régénération. |
| `DRF_THROTTLE_INVITATION_ACCEPT` | backend | vide (base) / `10/min` (prod) | non | non | Débit d'acceptation (public + authentifié). |

---

## Email (SMTP)

Tous côté backend, non requis, non secrets sauf indication. **Aucune n'est documentée dans
les `.env*.example`** (used-but-undocumented).

| Variable | Défaut | Secret ? | Effet |
|---|---|---|---|
| `EMAIL_BACKEND` | `django.core.mail.backends.console.EmailBackend` | non | Backend d'envoi (console par défaut → pas d'envoi réel). |
| `EMAIL_HOST` | `localhost` | non | Hôte SMTP. |
| `EMAIL_PORT` | `25` | non | Port SMTP. |
| `EMAIL_HOST_USER` | vide | non | Utilisateur SMTP. |
| `EMAIL_HOST_PASSWORD` | vide | **OUI** | Mot de passe SMTP. |
| `EMAIL_USE_TLS` | `False` | non | STARTTLS. |
| `EMAIL_USE_SSL` | `False` | non | SSL implicite. |
| `EMAIL_TIMEOUT` | `0` (→ None) | non | Timeout d'envoi. |
| `DEFAULT_FROM_EMAIL` | `ragatm@localhost` | non | Expéditeur par défaut. |
| `SERVER_EMAIL` | = `DEFAULT_FROM_EMAIL` | non | Expéditeur des erreurs serveur. |
| `EMAIL_SUBJECT_PREFIX` | vide | non | Préfixe des sujets. |

---

## Base de données

| Variable | Composant | Défaut | Requis ? | Secret ? | Effet |
|---|---|---|---|---|---|
| `DB_NAME` | backend | `ragatm` | non (compose force la valeur de l'environnement) | non | Base PostgreSQL. |
| `DB_USER` | backend | `ragatm` | non | non | Utilisateur PG. |
| `DB_PASSWORD` | backend | `ragatm` | **oui en prod** | **OUI** | Mot de passe PG. |
| `DB_HOST` | backend | `localhost` (compose `db`) | non | non | Hôte PG. |
| `DB_PORT` | backend | `5432` | non | non | Port PG. |

PostgreSQL doit avoir l'extension **pgvector** (migration `0001` fait `CREATE EXTENSION vector`).

---

## Cache / Redis / Celery

### Convention de numérotation des DB Redis

**Source de vérité** de l'affectation des DB Redis. Toute nouvelle configuration doit s'y
conformer ; un test la verrouille (`tests/tests_root/deploy/test_redis_db_convention.py`).

| DB | Rôle | Où c'est posé |
|---|---|---|
| **0** | Applicatif **production** (broker + result Celery) | `docker-compose.prod.yml` (`environment:`), `.env.prod` |
| **1** | Applicatif **staging** (broker + result Celery) | `docker-compose.staging.yml` (`environment:`), `.env.staging.example` |
| **2** | **Cache Django / runtime config** (epoch `CORE_CONFIG_RUNTIME_ENABLED`) | `DJANGO_CACHE_URL` (`.env.prod` ; commenté dans les templates staging) |

Précisions importantes :

- **Aucune collision inter-environnements n'est possible aujourd'hui** : chaque stack tourne
  dans un projet Compose distinct (un par environnement) avec **son propre**
  conteneur `redis`, son volume et son réseau `internal` ; Redis n'est **jamais** publié
  (`ports:` absent). `redis://redis:6379/N` désigne donc une instance différente par projet.
  La convention supprime le risque **latent** (partage futur d'une instance) et rend les deux
  stagings identiques.
- Les valeurs sont **forcées dans `environment:`** des composes (qui gagne sur `env_file`) :
  un `.env` périmé ne peut pas réintroduire une DB de prod en staging.
- En staging, `DJANGO_CACHE_URL` est **non défini** par défaut (→ `LocMemCache`). S'il est
  activé, il doit viser la **DB 2**.

| Variable | Composant | Défaut | Requis ? | Secret ? | Effet |
|---|---|---|---|---|---|
| `DJANGO_CACHE_URL` | backend | vide → `LocMemCache` | recommandé si `CORE_CONFIG_RUNTIME_ENABLED=1` | non | Cache Redis partagé (cohérence epoch inter-process). **DB 2** par convention : `redis://redis:6379/2`. |
| `CELERY_BROKER_URL` | backend | `redis://redis:6379/0` | non | non | Broker Celery. **DB 0 = prod, DB 1 = staging** (les deux stagings), cf. convention ci-dessus. Forcé dans `environment:` des composes. |
| `CELERY_RESULT_BACKEND` | backend | = broker | non | non | Backend de résultats. |
| `CELERY_TASK_ALWAYS_EAGER` | backend | vide (off) | non | non | Exécution inline (tests / sans broker). |
| `CELERY_TIMEZONE` | backend | `Europe/Paris` | non | non | Fuseau Celery. |
| `CELERY_WORKER_PREFETCH_MULTIPLIER` | backend | `1` | non | non | Prefetch worker. |
| `CELERY_TASK_SOFT_TIME_LIMIT` | backend | `540` | non | non | Soft limit (queue défaut). |
| `CELERY_TASK_TIME_LIMIT` | backend | `600` | non | non | Hard limit (queue défaut). |
| `CELERYD_CONCURRENCY` | compose (worker `celery`) | `4` prod / `2` staging | non | non | Concurrence worker général. Lu par compose (`--concurrency`), pas par settings. |
| `CELERY_ANALYTICS_CONCURRENCY` | compose (worker_analytics) | `2` | non | non | Concurrence worker analytics. |

La queue `analytics` porte ses propres limites (soft 3300 s / hard 3600 s). Voir
[../03-backend/taches-asynchrones.md](../03-backend/taches-asynchrones.md).

---

## LLM / génération / AI Gateway

Policy : génération obligatoirement via **AI Gateway** en env officiel ; jamais
Ollama en direct. Voir [../06-ai/ai-gateway.md](../06-ai/ai-gateway.md) et
[../06-ai/politiques-llm.md](../06-ai/politiques-llm.md).

| Variable | Composant | Défaut | Requis ? | Secret ? | Effet |
|---|---|---|---|---|---|
| `RAG_LLM_PROVIDER` | backend | `ollama` (via `DEFAULT_LLM_PROVIDER`) | non | non | Provider de génération : `ollama` = modèle local servi par la Gateway (seul moteur). Appel direct réservé au dev/tests (check E003). |
| `RAG_LLM_MODEL` | backend | `compact-generation-8` (via `OLLAMA_DEFAULT_MODEL`) | non | non | Modèle global de repli (si RagSpace vide). |
| `DEFAULT_LLM_PROVIDER` | backend | `ollama` | non | non | Alias/fallback de `RAG_LLM_PROVIDER`. |
| `OLLAMA_DEFAULT_MODEL` | backend | `compact-generation-8` | non | non | Alias/fallback de `RAG_LLM_MODEL`. |
| `LLM_TIMEOUT_S` | backend | `120` | non | non | Timeout de génération. |
| `AI_GATEWAY_ENABLED` | backend | `0` | **oui = 1 en env officiel** (check E001) | non | Route la génération via la Gateway. |
| `AI_GATEWAY_BASE_URL` | backend | `""` (repli sur `AI_GATEWAY_URL`) | oui en env officiel (routable, non loopback) | non | URL canonique ; le client construit `{base}/v1/generate`. |
| `AI_GATEWAY_URL` | backend | — | non | non | **Alias DÉPRÉCIÉ** de `AI_GATEWAY_BASE_URL` (encore posé par compose prod/worker). |
| `AI_GATEWAY_REQUIRED_BASE_URL` | backend (checks) | vide | non | non | Indication seule dans les **messages** des checks E001/E002 (aucun appel ne l'utilise). |
| `AI_GATEWAY_POOL_HOSTS` | backend | vide (liste) | non | non | Noms d'hôte reconnus comme un AI Gateway Pool ; toute autre URL http(s) est une Gateway directe. |
| `AI_GATEWAY_TIMEOUT_SECONDS` | backend + gateway | `30` | non | non | Timeout d'appel Gateway. |
| `AI_GATEWAY_PROVIDER` | backend | `ollama` | non | non | Provider **primaire** routé vers la Gateway (découplé de `RAG_LLM_PROVIDER`). |
| `AI_GATEWAY_MODEL` | backend | `compact-generation-8` | non | non | Modèle demandé à la Gateway **en dernier recours**, quand la requête n'en porte aucun. **N'écrase jamais** le `llm_model` persisté d'un RagSpace — voir [../06-ai/providers-modeles.md §5.2](../06-ai/providers-modeles.md). |
| `CAPABILITIES_GATEWAY_PROBES_ENABLED` | backend | `0` | non | non | Sondes Gateway de la console d'administration ; fermées par défaut (aucun appel tant que non attestées). Voir [feature-flags.md](feature-flags.md). |
| `AI_GATEWAY_FALLBACK_ENABLED` | backend | `0` | non | non | Réservé : aucun provider externe n'existe, donc aucun repli possible. |
| `AI_GATEWAY_FALLBACK_PROVIDER` | backend | `none` | non | non | Seule valeur acceptée : `none` (check E004). Toute variable d'environnement de provider externe est refusée (`config.E009`), toute valeur externe configurée aussi (`llm_router.E009`). |
| `ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS` | backend | `0` | non | non | Dégrade E001/E003 en Warning (tests seulement). |
| `OLLAMA_BASE_URL` | backend | `http://ollama:11434` | non | non | URL Ollama (embeddings routés/gateway ; jamais d'appel direct en env officiel). |
| `OLLAMA_NUM_CTX` | backend | `4096` | non | non | Contexte Ollama. |
| `OLLAMA_TEMPERATURE` | backend | `0.2` | non | non | Température Ollama. |
| `OLLAMA_TOP_P` | backend | `0.9` | non | non | Top-p Ollama. |
| `OLLAMA_REPEAT_PENALTY` | backend | `1.1` | non | non | Pénalité de répétition Ollama. |
| `LLM_LOCAL_BASE_URL` | backend | `http://llm_local:8000` | non | non | Ancien service `llm_local` (legacy, aucun conteneur en compose). |
| `DEFAULT_RAGSPACE_CHAT_PROVIDER` | backend | `ollama` | non | non | Provider chat imposé aux **non-superusers** (verrou config modèles RagSpace). |
| `DEFAULT_RAGSPACE_CHAT_MODEL` | backend | `compact-generation-8` (nom logique Gateway Open, décision) | non | non | Modèle chat des RagSpaces **nouvellement créés** : imposé à un non-superuser, prérempli pour un superuser. **C'est cette variable qui pilote le modèle réellement servi**, pas `AI_GATEWAY_MODEL`. Voir [../06-ai/providers-modeles.md §5.1](../06-ai/providers-modeles.md). |
| `DEFAULT_RAGSPACE_EMBEDDING_PROVIDER` | backend | `ai_gateway` | non | non | Provider embeddings imposé aux non-superusers. |
| `DEFAULT_RAGSPACE_EMBEDDING_MODEL` | backend | `legacy-768` | non | non | Modèle embeddings (profil logique de la Gateway) imposé aux non-superusers. |

Les trois URL Gateway (`AI_GATEWAY_BASE_URL`, `AI_GATEWAY_EMBEDDINGS_BASE_URL`,
`RAG_RERANKER_BASE_URL`) acceptent la Gateway **directe** ou **AI Gateway Pool**
(p. ex. `http://ai-gateway-pool:8080`, via le réseau Docker privé du pool) ; check `llm_router.E007` : http(s) +
hôte, ni loopback (embeddings/rerank), ni port 11434 ; check `llm_router.E008` : les trois URL suivent le **même** chemin
(toutes vers le même pool, ou toutes en direct) — une bascule partielle refuse le démarrage.

## Embeddings

Voir [../06-ai/embeddings.md](../06-ai/embeddings.md). Dimension **figée à 768** (colonne
pgvector, check `rag_core.E001`).

| Variable | Composant | Défaut | Requis ? | Secret ? | Effet |
|---|---|---|---|---|---|
| `EMBEDDINGS_PROVIDER` | backend | `ai_gateway` | **oui en prod** (refuse `""`/`stub`) | non | Provider d'embeddings global (défaut des RagSpaces). `ai_gateway` = seul provider réel ; `stub` = tests/CI. |
| `EMBEDDINGS_MODEL` | backend | `default` (sentinelle ignorée) | non | non | Modèle générique. **Non documenté** dans `.env.example`. |
| `RAG_EMBEDDING_DIM` | backend | `768` | non | non | Dimension pgvector (doit matcher la migration 0001 ; check `rag_core.E001`). |
| `RAG_EMBED_BATCH_SIZE` | backend | `64` | non | non | Taille des lots d'embeddings (la Gateway refuse > 64). |
| `EMBEDDINGS_TIMEOUT_S` | backend | `30` | non | non | Timeout d'appel embeddings. |

---

## Retrieval / RAG

Voir [../03-backend/pipeline-retrieval.md](../03-backend/pipeline-retrieval.md). Toutes backend,
non requises, non secrètes.

**Retrieval de base** : `RAG_MIN_SOURCES`(1), `RAG_MIN_TEXT_RANK`(0.0), `RAG_MIN_SCORE`(0.20),
`RAG_TOP_K`(8), `RAG_VECTOR_TOP_K`(8), `RAG_HNSW_EF_SEARCH`(40),
`RAG_FTS_CONFIG`(`simple` ; `french`/`english` possibles),
`RAG_WORKSPACE_SOURCE_BOOST`(1.10), `RAG_DOMAIN_CORPUS_SOURCE_BOOST`(1.00),
`RAG_RETRIEVAL_CANDIDATE_MULTIPLIER`(3), `RAG_RETRIEVAL_CANDIDATE_K`(0),
`RAG_RETRIEVAL_MAX_PER_GROUP`(2), `RAG_RRF_STRONG_IDENTIFIER_LEXICAL_WEIGHT`(1.5),
`RAG_RRF_K`(60), `RAG_FTS_SLASH_IDENTIFIERS_ENABLED`(0),
`RAG_FTS_STORED_VECTOR_ENABLED`(0), `RAG_FTS_QUERY_NORMALIZATION_V2`(0).

`RAG_FTS_STORED_VECTOR_ENABLED` — lit le vecteur lexical dans la colonne générée
`text_tsv_french` (index GIN) au lieu de recalculer `to_tsvector` trois fois par ligne
candidate (*activable par flag*, désactivé par défaut dans le code ; **activé en production**
depuis la migration du 20/08/2026). Sémantique de classement identique — la colonne matérialise la même expression.
**Exige `RAG_FTS_CONFIG=french`** et le schéma migré (`rag_core.0039/0040`,
`corpus.0006/0007`) : la config est figée dans le schéma, et une divergence est refusée
au démarrage (`rag_core.E120/E121/E122`). Rollback par flag seul. Détail et fenêtre de
verrou : [../03-backend/pipeline-retrieval.md](../03-backend/pipeline-retrieval.md).

`RAG_FTS_QUERY_NORMALIZATION_V2` — normalisation de la requête lexicale (*activable par
flag*, **désactivé par défaut**). Le plan FTS se réduit à **une** disjonction des termes
porteurs de la question : mots vides **anglais** retirés, **tirets et apostrophes
conservés** (`bring-along`, `anti-dilution`, `d'ajustement` sont les termes les plus
discriminants de leur question), longueur minimale 3, 12 termes au maximum.

Défaut corrigé, reproduit en SQL : la configuration FTS est `french` pour les deux
magasins alors que le corpus métier est anglophone. `what`/`does`/`the` n'y sont pas des
mots vides, l'entrée « sélective » du plan historique se réduisait à `what:*`, elle
ramenait des résultats **non vides** — et le repli, conditionné à **zéro** résultat,
n'était donc jamais atteint.

**Compromis mesuré, à connaître avant activation** : la qualité monte (+8 preuves au
contexte du générateur sur le benchmark gelé, les deux magasins en hausse, aucune
régression) ; la **médiane** de latence FTS passe de ~1 095 ms à ~1 641 ms (**×1,50**) ;
le **p95 s'améliore** (~4 356 ms → ~3 243 ms, **×0,74**). Ce n'est pas « un FTS plus
rapide » : c'est une médiane plus lente contre une queue plus courte.

Le **repli de plan disparaît** quand le flag est actif : un plan à une entrée n'a pas de
repli à déclencher. C'est délibéré — l'entrée sélective était précisément le défaut.

**Indépendance des leviers.** `RAG_FTS_QUERY_NORMALIZATION_V2` agit sur la *construction
du plan* `tsquery` ; `RAG_FTS_STORED_VECTOR_ENABLED` agit sur la *source du vecteur* comparé.
Les deux sont **orthogonaux** et se combinent librement — aucun n'exige l'autre. L'activation
de V2 **ne déclenche aucun reranking** : `RAG_RERANKER_ENABLED` reste un levier distinct.
Comme le chemin stocké, V2 **exige `RAG_FTS_CONFIG=french`** pour produire l'effet mesuré,
puisque le défaut corrigé tient précisément à cette configuration.

Limites connues, non traitées ici : le produit n'a **aucune métadonnée de langue**
(corpus, document, fragment), la normalisation est donc **générique** et non une
politique linguistique ; la saturation par la fréquence de `ts_rank` reste une limite
lexicale connue (pondération IDF et score de couverture mesurés, tous deux rejetés).

**Retour arrière** : repasser la variable à `0`. Aucune réindexation, aucune migration,
aucune opération de données.

`RAG_FTS_SLASH_IDENTIFIERS_ENABLED` — références à slash (`FIN01/FIN01-01`) (*activable
par flag*, désactivé par défaut). PostgreSQL (`simple`) tokenise ces références en **un
seul lexème** `'fin01/fin01-01'` ; le plan historique les décompose en `fin01:* &
fin01-01`, deux termes absents du vecteur — aucun match possible. Le flag ajoute le
lexème quoté en tête de plan, en repli sur le plan historique. Mesuré en P2.5-b :
Recall@5 Mission 0,745 → 0,882, 0 cas perdu (voir
../08-testing/benchmark-rag.md).

`RAG_RRF_K` — constante d'amortissement de la fusion par rang réciproque (*benchmarké*,
défaut inchangé). 60 est la valeur de référence de la littérature ; plus K est petit,
plus l'écart entre les premiers rangs pèse. Un réglage absent, non entier ou ≤ 0 retombe
sur 60. Rendu réglable en P2.5 pour être **mesurable** ; la mesure n'a pas justifié de le
changer (voir ../08-testing/benchmark-rag.md).

**Rerank** (étape 8, désactivé par défaut — voir [../06-ai/reranking.md](../06-ai/reranking.md)) :
`RAG_RERANK_ENABLED`(0), `RAG_RERANK_TRIGGER_MIN_CANDIDATES`(6), `RAG_RERANK_TOP_K`(4),
`RAG_RERANK_PROVIDER`(`stub` — **uniquement `stub` ou `cross_encoder`** ; toute autre valeur
refuse le démarrage via le check `rag_core.E004` quand `RAG_RERANK_ENABLED=1`),
`RAG_RERANK_MODEL_NAME`(`BAAI/bge-reranker-base`),
`RAG_RERANK_DEVICE`(`cuda`), `RAG_RERANK_BATCH_SIZE`(16), `RAG_RERANK_MAX_TEXT_CHARS`(2000).

**Préambules de politique de prompt** — lus via `getattr()` (surcharge facultative ; la valeur
par défaut vit dans `services/query/prompt_builder.py`) : `RAG_LOWCONF_PROMPT_PREAMBLE`,
`RAG_PARTIAL_PROMPT_PREAMBLE`, `RAG_EXPLAIN_PROMPT_PREAMBLE`, `RAG_COMPARE_PROMPT_PREAMBLE`
(politique `compare` des questions comparatives), `RAG_NEWS_PROMPT_HINT`. Voir
[../06-ai/answerability.md](../06-ai/answerability.md).

**Budgets analytiques** (modes explicatif ET comparatif) — **déclarés** dans
`settings/base.py`, défauts **égaux aux budgets standards** (aucun élargissement implicite) :
`RAG_EXPLAIN_TOP_K`(=`RAG_TOP_K`), `RAG_EXPLAIN_VECTOR_TOP_K`(=`RAG_VECTOR_TOP_K`),
`RAG_EXPLAIN_MIN_TEXT_RANK`(=`RAG_MIN_TEXT_RANK`),
`RAG_EXPLAIN_RERANK_TRIGGER_MIN_CANDIDATES`(=`RAG_RERANK_TRIGGER_MIN_CANDIDATES`),
`RAG_EXPLAIN_MIN_SCORE`(**non défini** : optionnel, car le figer plafonnerait le seuil des
espaces qui en portent un plus élevé). Ils ne s'appliquent qu'en ÉLARGISSEMENT (`max` pour les
tailles, `min` pour les seuils) : une valeur inerte est signalée au démarrage
(`rag_core.W001`), une taille non positive est bloquante (`rag_core.E005`). Détail :
[../06-ai/politique-comparative.md](../06-ai/politique-comparative.md).

**Budget de prompt** (déclarés) : `RAG_MAX_PROMPT_CHARS`(18000), `RAG_SNIPPET_MAX_CHARS`(1200),
`RAG_SNIPPET_MAX_CHARS_TABLE`(4000), `RAG_SNIPPET_MAX_CHARS_STRUCTURED`(4000 — fragments
d'extraction structurée, unités logiques, cf. [ai-gateway.md §4sexies](../06-ai/ai-gateway.md)). **C'est ce budget qui tronque le contexte** : quand il
sature, les dernières sources sélectionnées ne sont pas transmises. Mesuré sur 55 questions
et 2 corpus : à 18 000, **6 questions** voyaient leur contexte tronqué ; à **24 000**, aucune,
pour **+4,7 %** de contenu transmis et +22 ms de latence médiane — au-delà, aucun gain.
Ce défaut de code (18000) n'est **pas** la valeur déployée : staging et production sont posés à
**8300**, valeur qualifiée après le chantier d'intention (le gabarit a grossi de 573 c et se paie
sur les sources). Arithmétique et mesures : [configuration.md](configuration.md) et
[../03-backend/pipeline-retrieval.md](../03-backend/pipeline-retrieval.md).
⚠️ **Couplé à `RAG_FACTUAL_NUM_CTX`** : le prompt doit tenir dans la fenêtre du modèle moins
les tokens de génération (~25 984 caractères pour 8192/768). Débordement signalé au démarrage
(`rag_core.W003`) ; budget non positif bloquant (`rag_core.E007`).

**Bruit structurel du contexte** (déclarés) : `RAG_STRUCTURAL_NOISE_PENALTY_ENABLED`(0),
`RAG_STRUCTURAL_NOISE_PENALTY_FACTOR`(0.75, borné à [0.50 ; 1.00]). Dévalorisation
conservatrice des passages interrogatifs et assimilables à des titres — non destructive,
`score` brut conservé. Hors bornes : `rag_core.E006` (bloquant) ; actif mais neutre :
`rag_core.W002`. Détail : [../03-backend/bruit-structurel-contexte.md](../03-backend/bruit-structurel-contexte.md).

**Réécriture de requête** — lues directement dans `services/query/rewrite.py`, **hors settings,
absentes des `.env.example`** (used-but-undocumented) : `RAG_QUERY_REWRITE_ENABLED`(0),
`RAG_KEYWORDIZE_ENABLED`(1), `RAG_MAX_SEARCH_QUERIES`(3), `RAG_QUERY_REWRITE_PROVIDER`(→ollama),
`RAG_QUERY_REWRITE_MODEL`(vide), `RAG_QUERY_REWRITE_TIMEOUT_S`(6), `RAG_QUERY_REWRITE_MAX_CHARS`(140),
`RAG_KEYWORDIZE_MAX_TOKENS`(10).

**Reformulation no-answer** : `RAG_REPHRASING_SUGGESTIONS_ENABLED`(0), `RAG_REPHRASING_MAX`(3),
`RAG_REPHRASING_TEMPERATURE`(0.1), `RAG_REPHRASING_NUM_CTX`(2048), `RAG_REPHRASING_NUM_PREDICT`(256),
`RAG_REPHRASING_TIMEOUT_S`(3).

**Mémoire de conversation** : `CHAT_MEMORY_MAX_MESSAGES`(12), `CHAT_MEMORY_MAX_CHARS`(3000),
`CHAT_MEMORY_SUMMARY_ENABLED`(1), `CHAT_MEMORY_SUMMARY_TRIGGER_MESSAGES`(24),
`CHAT_MEMORY_SUMMARY_TARGET_CHARS`(1400), `CHAT_MEMORY_SUMMARY_MAX_INPUT_CHARS`(12000).

> ⚠️ **~17 réglages `RAG_*`** (`RAG_RERANK_POOL_K`, les
> `RAG_INVENTORY_*`/`RAG_NEWS_*`, `RAG_EXPLAIN_RERANK_*`, `RAG_ENABLE_NEWS_HEURISTIC`…) sont
> lus via `getattr()` mais **jamais déclarés dans les settings** : les poser dans `.env` est un
> **no-op silencieux**. Les budgets analytiques `RAG_EXPLAIN_TOP_K`,
> `RAG_EXPLAIN_VECTOR_TOP_K`, `RAG_EXPLAIN_MIN_TEXT_RANK`, `RAG_EXPLAIN_MIN_SCORE` et
> `RAG_EXPLAIN_RERANK_TRIGGER_MIN_CANDIDATES`, ainsi que `RAG_MAX_PROMPT_CHARS`,
> `RAG_SNIPPET_MAX_CHARS` et `RAG_SNIPPET_MAX_CHARS_TABLE`, **ont été sortis de cette
> liste** (déclarés et testés). Voir [limites-connues.md](limites-connues.md).

---

## Résumés / analyses corpus / audit

**Résumés** : `SUMMARY_ASYNC_ENABLED`(1), `SUMMARY_ASYNC_MAX_SYNC_OUTPUT_TOKENS`(2048),
`SUMMARY_PENDING_TIMEOUT_S`(900), `SUMMARY_PROMPT_VERSION`(1),
`DOMAIN_CORPUS_SUMMARY_ENABLED`(1 — résumé des documents de corpus métier, activé par
défaut ; mettre `0` pour désactiver, cf.
[../03-backend/resume-corpus-metier.md](../03-backend/resume-corpus-metier.md)).

**Analyses globales de corpus (L2/L4)** : `CORPUS_ANALYTICS_MAX_DOCUMENTS`(2000),
`CORPUS_ANALYTICS_MAX_CHUNKS`(100000), `CORPUS_ANALYTICS_MAX_ESTIMATED_TOKENS`(5000000),
`CORPUS_ANALYTICS_MAX_CONCURRENT_PER_WORKSPACE`(2), `CORPUS_ANALYTICS_RESULT_TTL_DAYS`(30),
`CORPUS_ANALYTICS_TASK_SOFT_TIME_LIMIT`(3300), `CORPUS_ANALYTICS_TASK_TIME_LIMIT`(3600),
plus `CORPUS_GLOBAL_SEARCH_*` (~13 bornes : `max_candidate_chunks` 2000, `max_batches` 20,
`max_estimated_cost_usd` 1.0, `max_retries` 2, `CORPUS_GLOBAL_SEARCH_ALLOW_EXTERNAL_FALLBACK` 0…).

**Audit / rétention** : `ACTION_AUDIT_RETENTION_DAYS`(365),
`RAG_QUERY_LOG_RETENTION_DAYS`(90), `LLM_EGRESS_AUDIT_RETENTION_DAYS`(365),
`EGRESS_POLICY_CHANGE_RETENTION_DAYS`(1095), `AUDIT_PURGE_ENABLED`(1),
`AUDIT_PURGE_BATCH_SIZE`(1000), `AUDIT_PURGE_MAX_ROWS_PER_RUN`(50000),
`RAG_QUERY_LOG_STORE_QUESTION`(1), `RAG_QUERY_LOG_STORE_ANSWER`(1).
Les deux rétentions d'audit de **sécurité** ont un plancher de 90 jours : une valeur
inférieure BLOQUE le démarrage (`ragatm.E010`). Voir
la documentation d'exploitation (hors périmètre de cette édition)
et la documentation d'exploitation (hors périmètre de cette édition).

**Ordonnancement périodique** : `CELERY_BEAT_REAP_REINDEX_SECONDS`(300),
`CELERY_BEAT_REAP_SUMMARIES_SECONDS`(600), `CELERY_BEAT_AUDIT_PURGE_SECONDS`(86400),
`CELERY_BEAT_HEARTBEAT_SECONDS`(60), `CELERY_BEAT_HEARTBEAT_MAX_AGE_SECONDS`(180),
`PERIODIC_TASK_LOCK_TTL_SECONDS`(900).

**Contrôles de sûreté du hard-delete des RagSpaces** — tous backend, non secrets,
**désactivés par défaut** (l'activation est un geste d'exploitation explicite, le
rollback est la même variable, sans redéploiement) :
`RAG_MEDIA_STORAGE_AUDIT_ENABLED`(0), `CELERY_BEAT_MEDIA_STORAGE_AUDIT_SECONDS`(86400),
`RAG_RAGSPACE_DELETE_MONITORING_ENABLED`(0),
`CELERY_BEAT_RAGSPACE_DELETE_MONITOR_SECONDS`(3600),
`RAG_RAGSPACE_DELETE_MONITORING_WINDOW_SECONDS`(86400).
Les deux tâches associées sont **strictement en lecture** : elles signalent, elles ne
suppriment ni ne réparent jamais. Voir
la documentation d'exploitation (hors périmètre de cette édition).

---

## Extraction / documents

Voir [../03-backend/ingestion-extraction.md](../03-backend/ingestion-extraction.md). Toutes
backend, non requises, non secrètes.

**Global** : `UPLOAD_MAX_FILE_SIZE_MB`(25), `UPLOAD_PDF_MAX_PAGES`(500),
`UPLOAD_EXTRACTION_TIMEOUT_S`(**180** ; ≥ OCR 60s pour éviter les faux « timeout »),
`DOCUMENT_MAX_UPLOAD_SIZE_MB`(= upload),
`DOCUMENT_MAX_EXTRACTED_CHARS`(5000000), `DOCUMENT_EXTRACTION_TIMEOUT_SECONDS`(= upload),
`DOCUMENT_PDF_MAX_PAGES`(= upload).

**PDF natif** : `DOCUMENT_PDF_NATIVE_FALLBACK_ENABLED`(1),
`DOCUMENT_PDF_NATIVE_FALLBACK_ENGINES`(`pymupdf,pdfminer,pdfplumber`), `DOCUMENT_PDF_MIN_TOTAL_CHARS`(200).
`DOCUMENT_OCR_COMPUTE_CONFIDENCE`(`1`) : second passage Tesseract (`image_to_data`) pour la confiance OCR.
À `0`, le texte indexé est **identique** et seuls `ocr_confidence`, `page_ocr_confidences` et le
warning `ocr_low_confidence` disparaissent — 36 % du temps OCR médian économisé.

**OCR** (désactivé par défaut) : `DOCUMENT_OCR_ENABLED`(0), `DOCUMENT_OCR_PROVIDER`(`tesseract`),
`DOCUMENT_OCR_TIMEOUT_SECONDS`(60), `DOCUMENT_OCR_MAX_PAGES`(50), `DOCUMENT_OCR_MIN_CHARS_PER_PAGE`(24),
`DOCUMENT_OCR_LANGUAGES`(`fra+eng`), `DOCUMENT_OCR_RASTER_SCALE`(**2.0** en base.py), `DOCUMENT_OCR_AUTO_ROTATE`(1).

**Image** : `DOCUMENT_IMAGE_INGEST_ENABLED`(1), `DOCUMENT_IMAGE_FORMATS`(`jpeg,png,tiff,webp`),
`DOCUMENT_IMAGE_MAX_BYTES`(0), `DOCUMENT_IMAGE_MAX_PIXELS`(40000000), `DOCUMENT_IMAGE_PREPROCESS_ENABLED`(1).

**Réglages de type `list`** (`DOCUMENT_IMAGE_FORMATS`, `DOCUMENT_PDF_NATIVE_FALLBACK_ENGINES`,
`DOCUMENT_VISUAL_ANALYSIS_SOURCES`) : l'environnement les écrit en texte séparé par des virgules,
mais `settings/base.py` les projette **en liste** via le registre (`reg(...)`), comme il le déclare.
Les lecteurs (`extraction/config.py`, helper `_list`) acceptent cette liste canonique et, à
l'identique, la forme texte d'un `override_settings`. Avant le lot FORMATS-DWG, ces réglages
restaient des chaînes et deux lecteurs faisaient `str(valeur).split(",")` : une liste y devenait
`"['jpeg'"`, ce qui vidait l'allowlist sans erreur. Le contrat registre → settings → lecteur est
verrouillé par `tests/tests_root/unit/config/test_list_setting_type_contract.py`. Comme pour tout
réglage du registre, une variable posée **vide** vaut son défaut déclaré (et non plus une liste vide) :
pour fermer les images, utiliser `DOCUMENT_IMAGE_INGEST_ENABLED=0`.

**Analyse visuelle** (PPTX) : `DOCUMENT_VISUAL_ANALYSIS_ENABLED` (**0** — fermé
par défaut ; ouvert, les sources actives passent en asynchrone sur la file `ingest_visual`),
`DOCUMENT_VISUAL_ANALYSIS_SOURCES`(`image` par défaut ; `image`, `pdf`, `docx` et `pptx`
opérationnelles, chacune derrière sa **propre** porte ; `pptx` exige aussi
`DOCUMENT_PPTX_ENABLED=1`, sans drapeau visuel propre au PPTX). Client : `AI_GATEWAY_VISION_MODEL`(vide),
`AI_GATEWAY_VISION_TIMEOUT_SECONDS`(120, provisoire ; ≥ 70 exigé par le rail V2) —
cf. [ai-gateway.md §4ter](../06-ai/ai-gateway.md). Rail V2 des crops :
`AI_GATEWAY_VISION_V2_ALLOWED_FINGERPRINTS`(liste, vide = rail inactif ; staging seulement) —
cf. [ai-gateway.md §4quater](../06-ai/ai-gateway.md).
Orchestration V2 des crops (CV-3E1, **désactivée**) : `VISUAL_V2_ORCHESTRATION_ENABLED`(**0**),
`VISUAL_V2_RAGSPACE_ALLOWLIST`(liste, **vide = aucun espace**), `VISUAL_V2_PRODUCTION_ALLOWED`(**0** —
garde dure : en `DJANGO_ENV=prod` le drapeau seul ne suffit jamais), `VISUAL_V2_RASTER_DPI`(200 ;
150/200/300) — cf. [ai-gateway.md §4quinquies](../06-ai/ai-gateway.md). Balayeur V2 (CV-3E4A) :
`CELERY_BEAT_VISUAL_V2_SWEEP_SECONDS`(300 ; planifié seulement drapeau allumé hors production).
La politique fine par espace vit en **base** (`visual_v2_scope`), pas dans l'environnement.
Extraction structurée chart/table (*expérimental*) :
`STRUCTURED_VISUAL_ENABLED`(**0**), `STRUCTURED_VISUAL_AUTO_ENABLED`(**0** — déclenchement
automatique après le describe), `STRUCTURED_VISUAL_PDF_MAX_ASSETS`(**0** = hériter de
`DOCUMENT_PDF_VISUAL_MAX_ASSETS` ; borne de visuels PDF des seuls espaces de l'allowlist, ≤ 32), `STRUCTURED_VISUAL_PDF_MAX_PER_PAGE`(**0** = hériter de
`DOCUMENT_PDF_VISUAL_MAX_PER_PAGE` ; borne par page des mêmes espaces, ≤ 8), `STRUCTURED_VISUAL_RAGSPACE_ALLOWLIST`(liste, **vide = aucun
espace**), `STRUCTURED_VISUAL_TIMEOUT_SECONDS`(180), `STRUCTURED_VISUAL_SCHEMA_VERSION`(1 — `2` seulement si le Gateway visé le sert), `STRUCTURED_VISUAL_PRODUCTION_ALLOWED`(**0** — garde
dure : en `DJANGO_ENV=prod`, refus tant qu'il est faux ; jamais suffisant seul),
`STRUCTURED_VISUAL_ALLOWED_TASKS`(liste, **vide = toutes hors production** ; plafond codé `table` en
production) ; `DJANGO_ENV` inconnu refusé —
cf. [ai-gateway.md §4sexies](../06-ai/ai-gateway.md). Numérisation des courbes en mode **shadow**
(*expérimental*) : `STRUCTURED_VISUAL_CURVE_SHADOW_ENABLED`(**0**),
`STRUCTURED_VISUAL_CURVE_SHADOW_RAGSPACE_ALLOWLIST`(liste, **vide = aucun espace** ; s'ajoute à
l'allowlist structurée) ; refusée en production ; aucune sortie vers le RAG.

**Visuels intégrés** (bornes génériques — **réutilisées telles quelles par le
DOCX** en 1D : le préfixe `PDF` de ces noms est une dette de nommage assumée, préférable à huit
réglages en double ; `MAX_PER_PAGE` et `TABLE_OVERLAP_RATIO` restent sans objet pour un DOCX, qui
n'a ni page ni géométrie de tableau natif. Le PPTX (PPTX-1B) les réutilise aussi, `MAX_PER_PAGE`
s'appliquant **par diapositive** — nom inchangé ; `TABLE_OVERLAP_RATIO` y est sans objet) : `DOCUMENT_PDF_VISUAL_MAX_ASSETS`(12),
`DOCUMENT_PDF_VISUAL_MAX_PER_PAGE`(4), `DOCUMENT_PDF_VISUAL_MIN_SIDE_PX`(96),
`DOCUMENT_PDF_VISUAL_MIN_PIXELS`(20000), `DOCUMENT_PDF_VISUAL_MAX_PIXELS`(2000000 — borne du
contrat V1 ; au-delà l'image est réduite, jamais écartée), `DOCUMENT_PDF_VISUAL_MAX_BYTES`(8000000),
`DOCUMENT_PDF_VISUAL_TABLE_OVERLAP_RATIO`(0.6 — au-delà, le tableau natif fait foi),
`DOCUMENT_PDF_VISUAL_MAX_REPEATS`(3 — au-delà, empreinte répétée réputée décorative).
Régions vectorielles : `DOCUMENT_PDF_VECTOR_REGION_RENDER_ENABLED`(0),
`DOCUMENT_PDF_VECTOR_REGION_RAGSPACE_IDS`(vide = aucun espace), `DOCUMENT_PDF_VECTOR_REGION_RENDER_DPI`(150),
`DOCUMENT_PDF_VECTOR_REGION_MAX_CANDIDATES`(24).

**Tables (P3)** : `DOCUMENT_TABLE_MAX_PER_DOC`(50), `DOCUMENT_TABLE_MAX_CHARS`(100000), `DOCUMENT_TABLE_MAX_FRAGMENTS`(40), `DOCUMENT_TABLE_OCR_ENABLED`(0).

**Tables PDF génériques** : `DOCUMENT_PDF_GENERIC_TABLES_ENABLED`(0), `DOCUMENT_PDF_GENERIC_TABLES_ALLOWED_RAGSPACE_IDS`(vide = aucun espace), `DOCUMENT_TABLE_MAX_ROW_CHUNKS`(2000).

**Tableaux structurés** : `DOCUMENT_TABLE_MAX_PER_DOC`(50), `DOCUMENT_TABLE_MAX_CHARS`(100000 — borne de VOLUME d'un tableau, **pas** une borne d'embeddings ; 12000 était calibré pour un tableau = un chunk, un monde révolu depuis la fragmentation), `DOCUMENT_TABLE_MAX_FRAGMENTS`(40). La borne d'embeddings d'un fragment de tableau est le budget de découpage de l'espace, qui applique déjà le plafond du modèle gouverné.

**Rail hybride Document Vision (MinerU)** : `DOCUMENT_MINERU_HYBRID_ENABLED`(0), `DOCUMENT_MINERU_SIDECAR_URL`(http://127.0.0.1:16580/v1), `DOCUMENT_MINERU_PAGE_TIMEOUT_S`(60), `DOCUMENT_MINERU_DOCUMENT_TIMEOUT_S`(300), `DOCUMENT_MINERU_TIER`(basic), `DOCUMENT_MINERU_OUTPUT_FORMAT`(middle_json), `DOCUMENT_MINERU_MAX_PAGES_PER_DOC`(3), `DOCUMENT_MINERU_SIDECAR_ALLOWED_HOSTS`(vide), `DOCUMENT_MINERU_MAX_INLINE_BYTES`(33554432), `MINERU_SERVICE_USE_SCOPE`(unknown), `MINERU_SERVICE_ATTRIBUTION_SATISFIED`(0).

**Sidecar Document Vision** (variables du CONTENEUR sidecar, pas du backend RAGatm) : `MINERU_CPU_THREAD_LIMIT`(vide). Le bornage des pools CPU est dérivé du quota du conteneur (`cpu.max`, cpuset, affinité) et non d'une valeur figée ; cette variable permet seulement de **restreindre** davantage. Une valeur supérieure au budget détecté est ramenée à celui-ci, une valeur invalide est ignorée avec un avertissement. Détail : [../03-backend/ingestion-extraction.md](../03-backend/ingestion-extraction.md).

`DOCUMENT_MINERU_MAX_PAGES_PER_DOC` borne le coût du rail pour un document ; le plafond réellement appliqué est le plus petit entre cette valeur et ce que le budget Celery restant permet. `DOCUMENT_MINERU_SIDECAR_ALLOWED_HOSTS` n'est utile que si le sidecar vit sur un pont Docker interne : déclarer un nom ne l'autorise pas à lui seul, toutes les adresses résolues doivent être privées.
`DOCUMENT_TABLE_MAX_PER_DOC` borne des tableaux **atomiques** ; `DOCUMENT_TABLE_MAX_ROW_CHUNKS`
borne les fragments *(ligne logique × page)* du rail générique, qui sont des objets différents.

**DOCX** (anti zip-bomb) : `DOCUMENT_DOCX_MAX_UNCOMPRESSED_SIZE_MB`(200), `DOCUMENT_DOCX_MAX_ZIP_ENTRIES`(2000),
`DOCUMENT_DOCX_MAX_COMPRESSION_RATIO`(200).

**CSV** : `DOCUMENT_CSV_MAX_ROWS`(100000), `DOCUMENT_CSV_MAX_COLUMNS`(256),
`DOCUMENT_CSV_MAX_CELL_CHARS`(10000), `DOCUMENT_CSV_ROWS_PER_BLOCK`(1).

**HTML / Markdown** : `DOCUMENT_HTML_MAX_CHARS`(2000000), `DOCUMENT_MARKDOWN_MAX_CHARS`(2000000).

**Excel** : `DOCUMENT_EXCEL_ENABLED`(1) + ~16 bornes (`DOCUMENT_EXCEL_MAX_SHARED_STRINGS_MB` 50,
`DOCUMENT_EXCEL_MAX_CELLS` 1e6, `DOCUMENT_EXCEL_MAX_DECLARED_COLS` 2048, `DOCUMENT_EXCEL_MAX_SHEETS` 100,
`DOCUMENT_EXCEL_HIDDEN_SHEETS_POLICY` `hidden_only`, `DOCUMENT_EXCEL_INCLUDE_HIDDEN_SHEETS` 0 déprécié,
`DOCUMENT_EXCEL_ROWS_PER_CHUNK` 40, `DOCUMENT_EXCEL_CHUNK_MAX_CHARS` 4000…).

**Aperçu tabulaire** (visualiseur XLSX) : `PREVIEW_CONTEXT_ROWS_DEFAULT` 15 /
`PREVIEW_CONTEXT_ROWS_MAX` 50, `PREVIEW_CONTEXT_COLUMNS_DEFAULT` 3 /
`PREVIEW_CONTEXT_COLUMNS_MAX` 10, `PREVIEW_MAX_ROWS` 300, `PREVIEW_MAX_COLUMNS` 60,
`PREVIEW_MAX_CELLS` 10000 (borne **cardinale**), `PREVIEW_MAX_CELL_CHARS` 500
(affichage — la normalisation garde la limite d'ingestion),
`PREVIEW_TIMEOUT_SECONDS` 5 / `PREVIEW_HARD_TIMEOUT_SECONDS` 10,
`PREVIEW_CACHE_ENABLED` 1 / `PREVIEW_CACHE_TTL_SECONDS` 300.
Détail : [../03-backend/apercu-tabulaire.md](../03-backend/apercu-tabulaire.md).

**Aperçu documentaire** (visualiseur DOCX) : `DOCUMENT_DOCX_PREVIEW_ENABLED` (défaut
**activé** ; fermé ⇒ état explicite + téléchargement), `DOCX_PREVIEW_CONTEXT_BEFORE` 10 /
`_MAX` 50, `DOCX_PREVIEW_CONTEXT_AFTER` 20 / `_MAX` 100, `DOCX_PREVIEW_MAX_BLOCKS` 300,
`DOCX_PREVIEW_MAX_BLOCK_CHARS` 2000 (affichage), `DOCX_PREVIEW_MAX_TOTAL_CHARS` 400000
(borne **cardinale**), `DOCX_PREVIEW_MAX_HEADINGS` 500,
`DOCX_PREVIEW_MAX_DOCUMENT_BLOCKS` 50000. Le délai et le cache réutilisent les
variables génériques `PREVIEW_TIMEOUT_SECONDS` / `PREVIEW_CACHE_*`.
Détail : [../03-backend/apercu-documentaire.md](../03-backend/apercu-documentaire.md).

**Aperçu de présentation** (visualiseur PPTX) : `DOCUMENT_PPTX_PREVIEW_ENABLED`
(défaut **activé**), `PPTX_PREVIEW_CONTEXT_BEFORE` 1 / `_MAX` 5,
`PPTX_PREVIEW_CONTEXT_AFTER` 1 / `_MAX` 5, `PPTX_PREVIEW_MAX_SLIDES` 10,
`PPTX_PREVIEW_MAX_BLOCKS_PER_SLIDE` 200, `PPTX_PREVIEW_MAX_BLOCK_CHARS` 2000
(affichage), `PPTX_PREVIEW_MAX_TOTAL_CHARS` 400000 (borne **cardinale**),
`PPTX_PREVIEW_MAX_INVENTORY` 500. Délai et cache réutilisent les variables
génériques `PREVIEW_TIMEOUT_SECONDS` / `PREVIEW_CACHE_*`.
Détail : [../03-backend/apercu-presentation.md](../03-backend/apercu-presentation.md).

**PowerPoint** : `DOCUMENT_PPTX_ENABLED`(1), `DOCUMENT_PPTX_INCLUDE_NOTES`(0),
`DOCUMENT_PPTX_INCLUDE_ALT_TEXT`(1), `DOCUMENT_OFFICE_CHART_DATA_ENABLED`(0 — graphiques natifs DOCX / PPTX) + bornes (`DOCUMENT_PPTX_MAX_SLIDES` 500,
`DOCUMENT_PPTX_MAX_SHAPES_PER_SLIDE` 500, `DOCUMENT_PPTX_MAX_TABLE_CELLS` 50000,
`DOCUMENT_PPTX_MAX_CHARS` 2000000, `DOCUMENT_PPTX_GROUP_MAX_DEPTH` 10,
`DOCUMENT_PPTX_MAX_UNCOMPRESSED_SIZE_MB` 200, `DOCUMENT_PPTX_MAX_ZIP_ENTRIES` 5000,
`DOCUMENT_PPTX_MAX_COMPRESSION_RATIO` 200).

**Courriels `.eml` et pièces jointes** : `DOCUMENT_EML_ENABLED` (**0**),
`DOCUMENT_EML_MAX_FILE_SIZE_MB` (25), `DOCUMENT_EMAIL_GLOBAL_ENABLED` (**0** ; 1 = tout espace,
droits et quotas inchangés), `DOCUMENT_EMAIL_RAGSPACE_IDS` (**vide = aucun espace** ;
UUID complets ; porte d'espace des `.eml`), `DOCUMENT_EMAIL_ATTACHMENT_INGEST_ENABLED`
(**0**), `DOCUMENT_EMAIL_MAX_ATTACHMENTS` (20, 1–200), `DOCUMENT_EMAIL_MAX_ATTACHMENT_SIZE_MB` (20),
`DOCUMENT_EMAIL_MAX_TOTAL_DECODED_MB` (50, cumul partagé avec les imbriqués),
`DOCUMENT_EMAIL_MAX_NESTING_DEPTH` (2, 0–5). Consommés par `backend` (admission au dépôt) et
par les processus d'extraction (`worker`, `worker_reindex`, `worker_visual`). Voir
[ingestion des courriels](../03-backend/ingestion-email.md).

**XML générique `.xml`** : `DOCUMENT_XML_ENABLED` (**0** — fermé
par défaut ; à 0 un `.xml` reconnu est refusé `xml_ingestion_disabled`) + limites
STRUCTURELLES `DOCUMENT_XML_MAX_DEPTH` 64 (racine = 1) et `DOCUMENT_XML_MAX_NODES`
200000 (éléments). Dépassement = refus `xml_complexity_limit`, analyse interrompue
immédiatement ; ces limites ne sont jamais réglables par le client. La taille reste
bornée par la limite d'upload globale.

**DXF `.dxf`** : `DOCUMENT_DXF_ENABLED` (**0** — fermé par défaut ;
à 0 un `.dxf` reconnu est refusé `dxf_ingestion_disabled`) + limite STRUCTURELLE
`DOCUMENT_DXF_MAX_ENTITIES` 250000 (entités des layouts et des définitions de blocs).
Dépassement = refus `dxf_complexity_limit` avant toute extraction sémantique. Toujours
asynchrone (décision produit, comme Excel/PPTX, non surchargeable).

**IFC `.ifc`** : `DOCUMENT_IFC_ENABLED` (**0** — fermé par défaut ;
à 0 un `.ifc` reconnu est refusé `ifc_ingestion_disabled`) + limite STRUCTURELLE
`DOCUMENT_IFC_MAX_ENTITIES` 250000 (entités STEP, pré-scan textuel puis compte du modèle ;
dépassement = refus `ifc_complexity_limit` avant tout chargement). Toujours asynchrone,
routé vers la file dédiée `ingest_ifc` : **n'activer que si un worker
`--queues=ingest_ifc` tourne** (recommandation : concurrence 1, l'import d'IfcOpenShell
coûte ~115 Mo RSS par processus).

**Audio** : `DOCUMENT_AUDIO_INGEST_ENABLED` (**0**), `DOCUMENT_AUDIO_RAGSPACE_IDS` (vide = aucun espace), `DOCUMENT_AUDIO_GLOBAL_ENABLED` (**0** ; 1 = tout espace, droits et quotas inchangés), `DOCUMENT_AUDIO_MAX_UPLOAD_BYTES` (**600000000**, octets décimaux, audio seulement), `DOCUMENT_AUDIO_MAX_DURATION_SECONDS` (43200), `AUDIO_ASR_MODEL_LOGICAL` (`asr-whisper-turbo`), `AUDIO_ASR_PART_MAX_SECONDS` (5400, borné à 6000), `AUDIO_ASR_PART_MAX_BYTES` (201326592, borné à 256 Mio − 12 Mio), `AUDIO_ASR_POLL_INTERVAL_SECONDS` (10), `AUDIO_ASR_JOB_DEADLINE_SECONDS` (4800), `AUDIO_SPOOL_DIR` (`/tmp/ragatm-audio`). Détail : [configuration.md](configuration.md), [ingestion-audio.md](../03-backend/ingestion-audio.md).

**DWG `.dwg`** : `DOCUMENT_DWG_ENABLED` (**0** — fermé par défaut ;
à 0 un `.dwg` reconnu est refusé `dwg_ingestion_disabled`), `DOCUMENT_DWG_CONVERTER_PATH`
(`/usr/local/bin/dwg2dxf`, configuration serveur seule — une version ≠ 0.14 est refusée
`dwg_dependency_unavailable`), `DOCUMENT_DWG_CONVERSION_TIMEOUT_SECONDS` 60 (dépassement =
groupe de processus tué, `dwg_conversion_timeout`) et `DOCUMENT_DWG_MAX_INTERMEDIATE_DXF_MB`
128 (plafond du DXF temporaire, vérifié avant lecture : `dwg_complexity_limit`). RagATM ne
parse pas le DWG : GNU LibreDWG 0.14 le convertit localement en DXF ASCII temporaire, lu
par le DxfExtractor. Toujours asynchrone, routé vers la file dédiée `ingest_dwg` :
**n'activer que si un worker `--queues=ingest_dwg` portant LibreDWG 0.14 tourne**, et
seulement après la revue de conformité GPLv3+ et la qualification d'un DWG réel.
`.pptm` / `.ppt` / `.odp` restent refusés ; l'ingestion `.pptx` est toujours asynchrone.

---

## Quotas documentaires par workspace

Deux quotas par workspace : **nombre de fichiers** et **taille cumulée des fichiers
sources** (octets). Détail complet : [quotas-workspace.md](quotas-workspace.md). Statut :
*implémenté / enforcement activé par défaut (flag)*.

**Défauts de plateforme** (proposés à la création, copiés dans le workspace, **jamais
rétroactifs**) : `WORKSPACE_DEFAULT_MAX_DOCUMENT_COUNT`(5000),
`WORKSPACE_DEFAULT_MAX_DOCUMENT_BYTES`(10737418240 = 10 Gio). Ordre de résolution
effectif : **override CoreConfig `global` > ces settings (env) > défaut de code**. Ces
variables sont un **fallback / initialisation** ; elles ne remplacent pas l'administration
superuser (console `admin-core`, soumise au gate `CORE_CONFIG_EDIT_ENABLED` + prod
interdit).

**Plafonds absolus** (garde-fous serveur, settings only, non éditables à chaud) :
`WORKSPACE_HARD_MAX_DOCUMENT_COUNT`(100000), `WORKSPACE_HARD_MAX_DOCUMENT_BYTES`(1099511627776
= 1 Tio). Un superuser ne peut pas fixer un quota au-delà sans changer la configuration
serveur.

**Enforcement** : `WORKSPACE_QUOTAS_ENFORCED`(**True**) — voir
[feature-flags.md](feature-flags.md). À `0`, les quotas restent **calculés et affichés**
mais aucun upload n'est bloqué (rollout progressif / rollback sans perte des valeurs).

```bash
WORKSPACE_DEFAULT_MAX_DOCUMENT_COUNT=5000
WORKSPACE_DEFAULT_MAX_DOCUMENT_BYTES=10737418240
WORKSPACE_HARD_MAX_DOCUMENT_COUNT=100000
WORKSPACE_HARD_MAX_DOCUMENT_BYTES=1099511627776
WORKSPACE_QUOTAS_ENFORCED=1
```

---

## Moteur tabulaire (Excel / CSV)

Moteur de calcul déterministe sur classeur. Drapeaux : voir
[feature-flags.md](feature-flags.md). Détail du comportement :
[../03-backend/tabular-query-architecture.md](../03-backend/tabular-query-architecture.md).

| Variable | Composant | Défaut | Requis ? | Secret ? | Effet |
|---|---|---|---|---|---|
| `TABULAR_QUERY_ENABLED` | backend | `0` | non | non | Active le moteur tabulaire. À `0`, `/api/v1/tabular/query/` répond `unsupported`/`tabular_query_disabled` et le refus `excel_guard` du chat reste inchangé. |
| `TABULAR_LLM_PLANNER_ENABLED` | backend | `0` | non | non | Planner LLM — **hors périmètre / non branché**. |
| `TABULAR_NARRATION_ENABLED` | backend | `0` | non | non | Narration LLM d'un résultat déjà calculé — **non branché**. Ne peut modifier aucune valeur. |
| `TABULAR_MAX_ROWS_SCANNED` | backend | `200000` | non | non | Plafond de lignes chargées et parcourues. Dépassement → `too_many_rows_scanned`. |
| `TABULAR_MAX_COLUMNS` | backend | `256` | non | non | Plafond de colonnes d'une table. Dépassement → `too_many_columns`. |
| `TABULAR_MAX_FILTERS` | backend | `16` | non | non | Plafond de filtres d'un plan. Dépassement → `too_many_filters`. |
| `TABULAR_MAX_RESULT_ROWS` | backend | `1000` | non | non | Plafond de lignes rendues ; borne aussi la `limit` d'un plan. Dépassement → `too_many_result_rows`. |
| `TABULAR_MAX_GROUPS` | backend | `1000` | non | non | Plafond de groupes (opération `group_by` **différée**). Dépassement → `too_many_groups`. |
| `TABULAR_MAX_QUERY_SECONDS` | backend | `10` | non | non | Délai maximal d'exécution DuckDB (`conn.interrupt()`). Dépassement → `query_timeout`. |
| `TABULAR_MAX_MEMORY_MB` | backend | `512` | non | non | `memory_limit` de la connexion DuckDB. Dépassement → `memory_exceeded`. |
| `TABULAR_MAX_CANDIDATE_TABLES` | backend | `50` | non | non | Tables candidates à la sélection. Dépassement → `too_many_candidate_tables`. |
| `TABULAR_MAX_CANDIDATE_COLUMNS` | backend | `512` | non | non | Colonnes candidates à la résolution. Dépassement → `too_many_candidate_columns`. |
| `TABULAR_COLUMN_MATCH_MIN_SCORE` | backend | `0.60` | non | non | Score minimal pour qu'une colonne soit retenue. Sous ce seuil → `column_not_found`. |
| `TABULAR_COLUMN_MATCH_MIN_MARGIN` | backend | `0.15` | non | non | Écart minimal avec le second candidat pour juger le premier **dominant**. Sous cet écart → `ambiguous_column`, jamais un choix arbitraire par position. |

> Toute limite atteinte produit un **code explicite**, jamais une troncature silencieuse.
> Les bornes sont lues **une seule fois par requête** : validation et exécution partagent
> les mêmes limites. Aucune de ces variables n'est présente dans les `.env*.example`
> (les défauts de code suffisent).

---

## Docker build / frontend

| Variable | Composant | Défaut | Effet |
|---|---|---|---|
| `WITH_OCR` | build arg (backend) | `0` (base) / `1` pour l'image OCR | Embarque Tesseract + extra `[ocr]`. |
| `BACKEND_PORT` | build arg / entrypoint | `8010` | Port gunicorn exposé (interne). |
| `GUNICORN_WORKERS` | entrypoint | `2` | Workers gunicorn. |
| `GUNICORN_THREADS` | entrypoint | `4` | Threads gunicorn. |
| `GUNICORN_TIMEOUT` | entrypoint | `120` | Timeout gunicorn. |
| `GUNICORN_BIND` | entrypoint | `0.0.0.0:8010` | Bind gunicorn (interne conteneur). |
| `DJANGO_MIGRATE` | entrypoint | `1` (`0` en déploiement : migration explicite) | `migrate` au boot. À `0`, la migration est **explicite** : sinon elle s'exécute sans lecteur et `restart: unless-stopped` la rejoue en boucle si elle échoue. |
| `DJANGO_COLLECTSTATIC` | entrypoint | `0` | `collectstatic` au boot. |
| `BACKEND_HEALTHCHECK_HOST` | compose backend | `localhost` | Host du healthcheck interne (ALLOWED_HOSTS strict). |
| `BACKEND_BASE_URL` / `BACKEND_BASE` | **frontend** | `http://backend:8010` | Cible du proxy SSR Next → backend. |
| `PRIVACY_POLICY_PUBLISHED` | **frontend** (exécution) | absent | `1` publie `/privacy` **seulement** si le texte est `approved` et sans marqueur `[VALIDATION REQUIRED]` (FR et EN) ; sinon 404 (fail-closed). |
| `PRIVACY_POLICY_PREVIEW` | **frontend** (exécution) | absent | `1` rend le **projet** de politique sur `/privacy` avec bandeau « projet » et `noindex` (staging, relecture). Jamais en production avant approbation. |
| `APP_HOST_PORT` | compose | `6100` | Port hôte loopback publié pour le frontend. |

Les variables `DEV_*` (ports/hosts dev loopback) sont détaillées dans [ports.md](ports.md).

> Le `.env.example` du build ne définit **pas** de `GUNICORN_*` (used-but-undocumented) ;
> les valeurs ci-dessus viennent de `entrypoint.sh`.

---

## Stockage objet

Aucune variable. RagATM n'utilise **pas** de stockage objet : les documents sont écrits sur le
volume local `media` (`MEDIA_ROOT`). Décision :
[../01-overview/decisions/ADR-001-stockage-documentaire.md](../01-overview/decisions/ADR-001-stockage-documentaire.md).

---

## AI Gateway (service, hors stack RagATM)


| Variable | Défaut | Effet |
|---|---|---|
| `AI_GATEWAY_HOST_BIND` | `127.0.0.1` | Bind hôte (loopback). |
| `AI_GATEWAY_HOST_PORT` | `8100` prod / `8200` staging | Port hôte publié. |
| `AI_GATEWAY_PORT` | `8100` | Port interne conteneur. |
| `OLLAMA_BASE_URL` | `http://host.docker.internal:11434` | Ollama amont (jamais publié). |
| `AI_GATEWAY_DEFAULT_MODEL` | `compact-generation-8` | Modèle par défaut (nom logique). |
| `AI_GATEWAY_TIMEOUT_SECONDS` | `30` | Timeout d'appel Ollama. |

---

## Anomalies / divergences

À connaître avant de configurer un environnement — le CODE fait foi, les `.env.example` sont
parfois en décalage.

### Used-but-undocumented (lues par le code, absentes de tous les `.env*.example`)

- **Email** : toutes les `EMAIL_*` + `DEFAULT_FROM_EMAIL`/`SERVER_EMAIL`.
- **Réécriture de requête** (dans `rewrite.py`) : `RAG_QUERY_REWRITE_*`, `RAG_KEYWORDIZE_*`, `RAG_MAX_SEARCH_QUERIES`.
- **Cookies/auth** : `AUTH_COOKIE_*`, `AUTH_PASSWORD_MIN_LENGTH`, `INVITE_RETURN_RESET_LINK`.
- **Sécurité prod** : `DRF_THROTTLE_ANON`/`DRF_THROTTLE_USER`, `DJANGO_SECURE_SSL_REDIRECT`, `DJANGO_USE_X_FORWARDED_HOST`.
- **Embeddings** : `EMBEDDINGS_MODEL`. (`EMBEDDINGS_TIMEOUT_S` est **déclaré au registre** ;
  les variables de provider externe et `OLLAMA_EMBEDDINGS_MODEL` n'existent plus.)
- **Gunicorn** : `GUNICORN_*`. (`LLM_LOCAL_BASE_URL` a été **supprimé** : le service
  `llm_local` n'existe plus dans aucun compose, et aucun code ne lisait le réglage.)

### Réglages « fantômes » — corrigés le 31/07/2026

Un défaut structurel, mesuré sur les trois environnements, a été fermé par le
chantier de rationalisation de la configuration.

Le pipeline lisait ses réglages ainsi :

```python
def _s(name, default):
    return getattr(settings, name, default)   # interroge les SETTINGS, pas l'env

top_k = _s("RAG_INVENTORY_TOP_K", top_k)
```

`getattr(settings, …)` interroge les **settings Django**. Si aucun module de
settings ne projette le nom, poser la variable dans un `.env` n'a **aucun effet** :
le code retient son défaut local, en silence. **Vingt variables étaient dans ce cas
en production**, dont `RAG_INVENTORY_*`, `RAG_LOWCONF_*`, `RAG_EXPLAIN_RERANK_*`,
`RAG_RERANK_POOL_K`, `RAG_MIN_SOURCE_SCORE`.

Traitement, paramètre par paramètre :

| Devenu | Paramètres | Pourquoi |
|---|---|---|
| **Réglage réel** (déclaré + projeté) | `RAG_INVENTORY_TOP_K`, `RAG_INVENTORY_VECTOR_TOP_K`, `RAG_INVENTORY_MIN_UNIQUE_DOCS`, `RAG_INVENTORY_RERANK_POOL_K`, `RAG_INVENTORY_RERANK_TAKE`, `RAG_EXPLAIN_RERANK_POOL_K`, `RAG_EXPLAIN_RERANK_TAKE`, `RAG_RERANK_POOL_K`, `RAG_LOWCONF_ENABLED`, `RAG_LOWCONF_FACTOR`, `RAG_LOWCONF_MIN_ABS`, `RAG_LOWCONF_MIN_SOURCES` | budgets d'exploitation qu'un opérateur a une raison légitime de calibrer |
| **Constante nommée** | `RAG_RRF_K`, `RAG_MIN_SOURCE_SCORE`, `RAG_MAX_SNIPPET_CHARS_AUDIT`, `RAG_MAX_REQUEST_ID_CHARS`, `RAG_COMPARE_PROMPT_PREAMBLE`, `RAG_EXPLAIN_PROMPT_PREAMBLE`, `RAG_LOWCONF_PROMPT_PREAMBLE`, `RAG_PARTIAL_PROMPT_PREAMBLE`, `RAG_NEWS_PROMPT_HINT` | invariants d'algorithme ou gabarits de prompt : les exposer inviterait à les changer sans mesure ni revue |
| **Constante d'une branche morte** | `RAG_ENABLE_NEWS_HEURISTIC`, `RAG_NEWS_TOP_K`, `RAG_NEWS_VECTOR_TOP_K`, `RAG_NEWS_RERANK_POOL_K`, `RAG_NEWS_RERANK_TAKE` | le mode « news » n'est pas branché : un réglage sur une branche inatteignable est une promesse fausse |

Cas particulier de `llm_router` : ce module lit certains réglages
(`EMBEDDINGS_TIMEOUT_S`,
`DEFAULT_*_MODEL`) dans les settings **puis dans l'environnement**. Ce repli est
utile et il reste — mais il n'accepte plus qu'un nom **déclaré**. Auparavant,
n'importe quel nom écrit dans le code devenait un réglage réel mais invisible :
absent du registre, absent de la documentation générée, non typé, sans bornes, et
ignoré par la comparaison de parité. Voir
`backend/apps/llm_router/config_access.py` et son test architectural.

Le lecteur permissif a disparu. `setting(name)` n'accepte **aucun défaut d'appel** :
il vient du registre, il n'y en a qu'un, et un nom inconnu lève
(`config/settings/access.py`). Trois system checks ferment la boucle :

| Contrôle | Ce qu'il attrape |
|---|---|
| `config.E010` | réglage lu par une application mais absent des settings (le défaut ci-dessus) |
| `config.W010` | réglage déclaré sans aucun consommateur |
| `config.W012` | variable posée avec un préfixe RagATM mais inconnue du registre (vestige d'un flag retiré, faute de frappe) |

### Alias de compatibilité / doublons

- `RAG_LLM_PROVIDER` ⇐ `DEFAULT_LLM_PROVIDER` ; `RAG_LLM_MODEL` ⇐ `OLLAMA_DEFAULT_MODEL`.
- `AI_GATEWAY_BASE_URL` (canonique) ⇐ `AI_GATEWAY_URL` (**déprécié**, encore posé par compose prod/worker).
- `RAG_HYBRID_ENABLED` défini **deux fois** dans `base.py` (même défaut, redondant — cosmétique).

### Divergences défaut de code vs `.env.example` / policy

- `EMBEDDINGS_PROVIDER` : défaut code `ai_gateway` (`stub` réservé aux tests, refusé en prod).
- `AI_GATEWAY_ENABLED` : défaut code `0` vs env officiel exige `1`.

## Références d'implémentation

- `backend/config/settings/base.py`, `backend/config/settings/prod.py`, `backend/config/settings/dev.py`
- `backend/config/celery.py`, `backend/entrypoint.sh`
- `backend/apps/rag_core/services/query/rewrite.py`
- `backend/apps/llm_router/embeddings.py`, `backend/apps/llm_router/checks.py`
- `.env.example`, `.env.staging.example`
- `deploy/docker/docker-compose*.yml`
