# Stratégie de tests

Objectif : décrire l'organisation réelle des tests RagATM, les services requis, les
commandes exactes et l'intégration continue. Public : développeurs, mainteneurs CI.
Le CODE prévaut : cette page corrige les affirmations obsolètes de l'ancienne doc.

## Vue d'ensemble — trois racines de tests

RagATM répartit ses tests sur **trois racines** : une **suite backend unique**, le
front et la suite AI Gateway. Backend et front tournent en CI ; AI Gateway n'y est
**pas** intégrée.

| Racine | Framework | Fichiers | Tests | Exécutée depuis | En CI ? |
|---|---|---:|---:|---|---|
| `tests/tests_root` (suite backend) | pytest + pytest-django | 429 | 9 374 | `backend/` | oui |
| `tests/frontend` | Vitest + Testing Library (jsdom) | 75 | 848 | `frontend/` | oui |
| `services/ai_gateway/tests` | pytest (stdlib + `requests`) | 3 | 14 | `services/ai_gateway/` | **non** |

La suite backend se lance via `make test-backend-all` (compileall + la voie **standard**).
Elle est scindée en quatre voies — standard / lourde / externe / complète — décrites plus
bas : [Découpage de la suite backend](#découpage-de-la-suite-backend--implémenté).

### Convergence des deux arbres — *(terminée)*

Le backend comptait historiquement **deux arbres de tests** (`tests/tests_root` et
`backend/tests_legacy`) qui exposaient le même package racine `http_api`, rendant leur
collecte simultanée impossible. La convergence est **achevée** : tout est consolidé sous
`tests/tests_root`, `backend/tests_legacy` a été supprimé, et une **configuration pytest
unique** (`pyproject.toml` au repo root) fixe un `rootdir` commun. Le déroulé complet est
tracé dans la série de commits `refactor(tests)` / `chore(tests)`.

**Fixtures canoniques** — [`backend/testing/fixtures.py`](../../backend/testing/fixtures.py)
héberge les fixtures partagées de la suite. Ce paquet vit sous `backend/`, présent en
tête du `sys.path` (le conftest de `tests_root` y insère `backend/`).

Règle de rédaction : une fixture partagée ne fournit **que les garanties réellement
communes**. Toute variante métier reste explicite au point d'usage. Ainsi `user`
fournit un utilisateur actif minimal (`u1` / `pw`, sans appartenance) mais **sans
email** : seuls les tests qui se connectent réellement — l'endpoint de login
authentifie par email — demandent `user_with_email`. Enrichir la fixture commune
ferait dépendre des centaines de tests de données qu'ils n'ont jamais demandées.

`rag_space` suit la même règle : un espace rattaché au `workspace` **du test appelant**
(la dépendance est déclarée, pas résolue, pour ne pas converger `workspace` au passage),
avec les réglages LLM et embeddings aux défauts du modèle. Le nom `space-a` est
conventionnel — aucun test ne l'observe. Deux variantes restent volontairement locales :
`S-tab` (module construisant une topologie à deux espaces) et `S-obs` (qui fabrique son
propre workspace).

`member` fournit une appartenance simple (`ROLE_MEMBER`) liant le `user` au `workspace`
du test. À ne pas confondre avec `admin_membership`, qui **promeut** l'utilisateur owner
et écrase donc une appartenance existante. *(Un alias transitoire `membership` a existé
pendant la convergence puis a été retiré une fois sans consommateur.)*

`api_client` fournit un `APIClient` DRF authentifié par `force_authenticate` sur le
`user` du test, membre du `workspace` via `member`, sans CSRF ni en-tête ni format par
défaut. Sept définitions locales en réalisaient une copie ; leur équivalence a été
établie **à l'exécution** (identité vue par `/auth/me/`, rôles, absence de mutation du
client, ordre de création des objets), non par lecture des corps.

**Comment chaque convergence est validée** — quatre exécutions (deux par arbre) dans
l'environnement figé du CI, comparées à une baseline : volumes de collecte, nodeids,
statuts, raisons de skip, identifiants paramétrés, et **carte de résolution des
fixtures**. Ce dernier contrôle est le seul capable de détecter qu'un test inchangé
s'est mis à consommer une autre donnée.

Règle d'identité, apprise de deux faux signaux successifs : un test s'identifie par
**module + nom + identifiant paramétré**, un fournisseur par **le fichier qui le
définit**. Le numéro de ligne ne participe jamais à une clé de comparaison — une simple
insertion dans un conftest a fait passer 764 fournisseurs inchangés pour des
changements, et un retrait de fixture a masqué 27 migrations réelles.

`workspace` fournit un espace minimal (`name="W1"`) : aucun propriétaire — le modèle
n'a pas ce champ, les permissions passent par `Membership` —, aucune appartenance,
aucun objet lié. Une variante reste locale : `W-tab` / `W-autre` dans
`test_tabular_integration.py`, qui construit une topologie à deux espaces pour ses
tests d'isolation.

**Toutes les fixtures critiques sont converties.** La suite backend partage `user`,
`workspace`, `member`, `rag_space` et `api_client` via le fournisseur canonique. Restent
hors convergence les seules variantes métier, explicitement locales : `S-tab`/`S-obs`,
`W-tab`/`W-autre`, et les utilisateurs `alice`/`obs-user` qui créent leur propre appartenance.

- **`tests/tests_root`** : suite backend unique. Sous-paquets :
  - `unit/` (fonctions pures : politiques, chunking, rerank, fusion, dates, garde Excel,
    constructeur de prompt), `unit/chat/`, `unit/extraction/` (Excel/CSV/HTML/Markdown/
    sécurité) ;
  - `integration/` (services + DB : ingestion, `query_rag`, chat, mémoire, flux SSE,
    fusion hybride, isolation) ;
  - `http_api/` (endpoints DRF : auth, routeur d'actions chat, documents, prompts,
    bootstrap, politique de mot de passe, liste RagSpace) ;
  - `corpus/` (corpus métier : propriétaire global = superuser, clients DRF par rôle) ;
  - `rag_core/` (le plus gros noyau : retrieval, hybride, config FTS, rerank
    cross-encoder + observabilité + audit, suite `benchmark_*`, embeddings, ingestion,
    entités, OCR à la demande, fallback PDF natif, table/image, couverture, mémoire chat,
    intégration/fallback Gateway, pricing, coût LLM) ;
  - `actions/` (routeur, classifieur, câblage), `audit/`, `chat/`, `deploy/` (cohérence
    config AI Gateway, compose).
- **`tests/frontend`** : ~37 fichiers Vitest + Testing Library (couches `api`/`sse`/
  `auth`/`storage`/utils et composants).
- **`services/ai_gateway/tests`** : `test_health.py`, `test_generate.py`,
  `test_deploy_compose.py`. **Non couverts par aucun workflow CI** — toute régression
  ici passe inaperçue en CI (voir [Limites connues](#limites-et-lacunes-connues)).

## Le stub d'embeddings global (point clé)

Les suites par défaut n'ont **besoin d'aucun LLM ni service d'embeddings réel** grâce
à un stub déterministe :

- **Implémentation** : `backend/apps/llm_router/embeddings.py`, `_stub_embed` génère
  des pseudo-embeddings depuis `sha256(text)` → `random.Random(seed).uniform(-1,1)` par
  dimension. **Reproductible mais non sémantique** (aucune notion de proximité de sens).
- **Escape hatch tests** : `EMBEDDINGS_PROVIDER=stub` (réglage global) **force le stub
  quelle que soit la configuration du RagSpace** ; un provider inconnu/non configuré
  retombe aussi sur le stub. Le stub est le *fallback de code* et le mode **tests/CI**,
  **jamais** un défaut de production (la prod le refuse au démarrage — voir
  [../06-ai/embeddings.md](../06-ai/embeddings.md)).
- **Conséquence** : la recherche vectorielle est validée sur son **contrat technique,
  pas sur la pertinence sémantique**. Les cas `no_answer` reposent sur des espaces
  vides / l'isolation, pas sur des questions hors-sujet. Seuls les benchmarks
  sémantiques (nomic, self-hosted, non bloquants) exercent la qualité vectorielle —
  voir benchmark-rag.md.

Un benchmark de **couverture linguistique** complète ce dispositif :
`tests/tests_root/benchmarks/heuristics/` mesure sur 700 questions synthétiques FR/EN la
reconnaissance d'intention (`factual` / `explain` / `compare`), avec baseline versionnée et
seuils de non-régression. Il est pur (ni base, ni réseau, ni dépendance, ~1 s) et tourne
dans la suite standard — voir
benchmark-heuristiques-intention.md.

`tests/tests_root/conftest.py` fixe, **avant les imports** puis via une fixture
`autouse` (réassertion runtime) :

- `EMBEDDINGS_PROVIDER=stub` (surcharge tout `.env` non-stub) ;
- `AI_GATEWAY_ENABLED=0`, `RAG_LLM_PROVIDER=ollama`,
  `ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS=1` (garde les system checks `llm_router.E001-E005`
  au vert sans réseau) ;
- fonctionnalités **opt-in forcées OFF** : `ACTIONS_ROUTER_ENABLED`,
  `ACTIONS_LLM_CLASSIFIER_ENABLED`, `RAG_REPHRASING_SUGGESTIONS_ENABLED` (un test qui
  les veut ON les active explicitement) ;
- `CELERY_TASK_ALWAYS_EAGER=True` — tâches Celery **synchrones** par défaut, comme la
  CI (qui pose l'env `CELERY_TASK_ALWAYS_EAGER=1`). Sans cet alignement, un `pytest` nu
  en local faisait basculer l'upload en asynchrone (réponse `{queued, document_id}`
  sans `document`) et échouait 9 tests Excel/PPTX verts en CI. Un test qui exerce le
  chemin **asynchrone** pose explicitement `CELERY_TASK_ALWAYS_EAGER=False`.

> Ce forçage des opt-in doit vivre dans le conftest de **l'arbre de tests**, pas dans
> `backend/conftest.py` : `backend/` n'est pas un ancêtre de `tests/tests_root/`, donc
> son conftest ne s'y applique pas. Le défaut ne se voit qu'avec un `.env` posant ces
> flags à `1` — jamais en CI, qui ne les définit pas.

### Tests à service réel — marqueur `external` *(implémenté)*

Un test « externe » a besoin d'autre chose que du dépôt et d'une base Postgres : l'AI
Gateway, un serveur Ollama, un provider d'embeddings réel, un corpus non
versionné. **La voie standard n'en exécute aucun — ni son corps, ni sa sonde de
disponibilité.**

| Module | Nature cumulée | Dépendance |
|---|---|---|
| `rag_core/test_gateway_integration.py` | `external` + `integration` | AI Gateway (`:8100`) |
| `rag_core/test_embeddings_real.py` | `external` + `embeddings` | provider réel + `RAG_BENCH_REAL_EMBEDDINGS=1` |
| `rag_core/test_benchmark_nomic.py::test_live_nomic_candidate_matches_baseline_and_no_llm` | `external` + `embeddings` | Ollama (`:11434`) + `nomic-embed-text` |

Seul le test *live* de `test_benchmark_nomic.py` est marqué : les autres tests de ce
module sont purs et restent dans la voie standard.

**Deux verrous, et le second est le point important.** Le marqueur retire ces tests de la
sélection ; `RAG_RUN_EXTERNAL_TESTS=1` les active. Mais surtout, les sondes de
disponibilité (`_gateway_reachable()`, `_ollama_has_nomic()`) vivaient dans des `skipif`
de **niveau module**, donc évaluées à l'**import**, donc pendant la **collecte** : la
suite « standard » émettait deux requêtes réseau avant son premier test, et exécutait pour
de bon les tests concernés dès que ces services répondaient sur la machine — 14,8 s de
benchmark sémantique réel contre Ollama, mesurées lors de l'évaluation. Son résultat
dépendait donc du poste, ce que le forçage de `conftest.py` cherche précisément à empêcher.

Les sondes sont désormais appelées **depuis une fixture ou depuis le corps du test** (cf.
[`backend/testing/external.py`](../../backend/testing/external.py), `skip_unless_reachable`).
`unit/test_external_suite_gate.py` verrouille les trois conditions de configuration :
famille désactivée par défaut, marqueur déclaré, exclusion présente dans la commande.

```bash
make test-backend-external      # RAG_RUN_EXTERNAL_TESTS=1 pytest -m external
```

Chaque test saute proprement si SON service est absent : la cible ne suppose pas que tous
soient disponibles.

### Modules d'évaluation « answerability » — *(dépendance supprimée)*

Deux modules relèvent de la campagne d'évaluation « answerability » :

| Module | Tests |
|---|---:|
| `tests/tests_root/rag_core/test_answerability_families.py` | 36 |
| `tests/tests_root/rag_core/test_demo_fr_extended_v3_1.py` | 37 |

Ils dépendaient de **`scikit-learn`**, qui n'est déclaré dans aucun extra de
`backend/pyproject.toml`. Un `pytest.importorskip` de **module** les rendait donc
optionnels — mais avec une conséquence disproportionnée : en CI (qui installe
`dev,ocr,tables`, sans `[rerank]`), la bibliothèque était absente et **les 81 tests
d'alors étaient décollectés**, ne laissant que deux lignes de skip dans le log.

Le diagnostic a montré que la dépendance ne concernait que **8 tests sur 81**, et que ces
8-là ne testaient pas RagATM :

- six assertions sur `roc_auc_score` / `average_precision_score` appliquées à des vecteurs
  fabriqués à la main — c'est le contrat de scikit-learn qui était vérifié, aucun code du
  dépôt n'était traversé ;
- un test du contrat de `GroupShuffleSplit` sur des groupes synthétiques ;
- son contrôle négatif, qui construisait un split pair/impair puis affirmait qu'il fuit —
  une tautologie arithmétique, incapable d'échouer.

**Ces 8 tests ont été supprimés**, et les deux `importorskip` avec eux. Les **73 restants**
— ceux qui gardent réellement quelque chose — sont désormais collectés et exécutés
**partout, y compris en CI**, sans aucune dépendance supplémentaire. Vérifié en bloquant
`sklearn` à l'import : 73 collectés, 73 passés.

L'absence de fuite dans le split supervisé reste garantie, et sur les données **réelles**
plutôt que synthétiques, par `test_la_fuite_de_cible_est_documentee` et
`test_le_classifieur_ne_repose_pas_sur_une_variable_fuitee`, qui assertent sur l'artefact
de décision versionné `backend/benchmarks/calibration/answerability/p2-4-decision.json`
(une ROC-AUC de 1.0 y trahirait une fuite).

> ⚠️ Leçon de l'épisode, valable au-delà de ce cas : un `importorskip` **de module** est un
> instrument grossier. Il sacrifie tout un fichier pour une dépendance dont quelques tests
> ont besoin, et la perte se lit comme un skip anodin. Placer la garde au plus près de ce
> qui l'exige — ou supprimer le test qui l'impose quand il ne protège rien.

> ⚠️ Un skip a déjà masqué un vrai problème par le passé : tant que l'arbre racine
> échouait, `make test-backend-all` s'arrêtait avant l'arbre legacy et l'erreur de collecte
> restait invisible. Un job vert sur un seul arbre ne prouve rien.

## Services et environnement requis

- **PostgreSQL avec l'extension pgvector** — requis pour `integration/`, `http_api/`,
  `rag_core/` (la migration 0001 fait `CREATE EXTENSION vector`). La CI utilise l'image
  `pgvector/pgvector:pg16`.
- **Aucun Ollama ni AI Gateway** nécessaire pour les suites par défaut (stub).
- `CELERY_TASK_ALWAYS_EAGER=1` — exécute les tâches Celery en ligne (pas de broker).
- `APPEND_SLASH=False` — toutes les URLs d'API doivent finir par `/`.
- Config pytest **unique** : `pyproject.toml` au **repo root**, section
  `[tool.pytest.ini_options]`. L'emplacement vient de la convergence : les deux arbres
  d'alors vivaient de part et d'autre de `backend/`, dont seul le repo root était
  l'ancêtre commun. Il fixe `rootdir = repo root` et une configuration identique pour
  toute invocation. `DJANGO_SETTINGS_MODULE=config.settings.dev`, `python_files=test_*.py`,
  `addopts=--import-mode=prepend -ra`, plus 6 marqueurs (`embeddings`, `integration`,
  `slow`, `tabular_volume`, `staging_qualification`, `tabular_concurrency`).
  `backend/` reste sur le `sys.path` via les `conftest.py` de chaque arbre, sans
  `pythonpath`. Historique : la config vivait en double (`tests/tests_root/pytest.ini`
  + `backend/pyproject.toml`) ; leur divergence de marqueurs faisait perdre ces derniers
  en invocation conjointe.
- **Frontend** : node 20, `npm ci`. Le hook `pretest` lance `scripts/link-test-deps.mjs`
  (symlink `tests/frontend/node_modules -> frontend/node_modules`, la suite vivant hors
  du paquet `frontend/`). Alias `@ -> frontend/src`, jsdom, setup `tests/frontend/setup.ts`.

## Découpage de la suite backend — *(implémenté)*

La suite est scindée par des **marqueurs**, jamais par des noms de fichiers : un fichier
renommé sortirait silencieusement d'une exclusion par chemin, et rallongerait la voie
standard sans que personne ne s'en aperçoive.

| Voie | Sélection | Tests | Durée mesurée | Déclenchement |
|---|---|---:|---:|---|
| **Standard** | `-m "not tabular_volume and not external"` | 9 313 | **3 min 20** (4 workers, ancien défaut local) / **8 min 06** (séquentiel) | **automatique** (push + PR, `ci.yml`, 2 workers) |
| **Lourde** | `-m "tabular_volume"` | 49 | **21 min 17** | **manuel uniquement** (`tests-heavy.yml`) |
| **Externe** | `-m "external"` | 15 | dépend des services | **manuel uniquement** (`make test-backend-external`) |
| **Complète** | aucun filtre | 9 377 | — | **manuel uniquement** (`make test-backend-full`) |

**Seule la voie standard tourne automatiquement.** Les autres se lancent explicitement —
c'est une décision, pas un oubli : une campagne de 20 min partie toute seule consomme des
runners sans que personne n'ait décidé d'en lire le résultat, et un échec que l'on ne
regarde pas devient du bruit qui finit par masquer les vraies régressions.

> Les chiffres ci-dessus remplacent une mesure devenue fausse (**5 000 tests / 8 min 19**,
> ~217 fichiers) : la suite avait presque doublé sans que la référence ne soit reprise.
> Point de départ mesuré avant optimisation : **9 325 tests, 19 min 50 et 20 min 02** sur
> deux campagnes (variance +1,0 %).

### Ce qui a rendu la voie standard rapide — *(mesuré, pas supposé)*

**1. Le hachage de mot de passe.** 59 % du temps se passait dans les `setup` de fixtures,
et l'essentiel venait d'un défaut jamais surchargé : Django 5.2 hache avec PBKDF2 à
**1 000 000 d'itérations**, soit **~349 ms par `create_user()`**. Le dépôt compte 219 sites
d'appel, dont 147 dans `http_api/` — lequel représentait 44 % du temps total avec 12 % des
tests, dont 83 % de `setup`.

`tests/tests_root/conftest.py` impose donc `PASSWORD_HASHERS = [MD5PasswordHasher]` pour
toute la suite. Mesure isolée : `http_api/` passe de **541 s à 67 s (−88 %)**.

> **Le hasher MD5 n'existe que dans les tests.** Le forçage vit dans une fixture de la
> suite, jamais dans `config/settings/` : la production conserve le hasher par défaut de
> Django. Les tests dont le hachage EST le sujet restaurent explicitement PBKDF2 via
> `override_settings` — `accounts/test_ensure_superuser.py` le fait pour
> `test_password_is_hashed_not_stored_in_clear` (préfixe du hash) et pour
> `test_the_password_never_appears_on_stdout_or_stderr` (sous MD5, son assertion
> `"pbkdf2" not in combined` serait vraie sans rien prouver). Aucune assertion n'a été
> assouplie.
>
> Quatre modules d'`integration/` posaient déjà cette surcharge localement, avec le même
> constat écrit dans leur docstring. Elle est désormais globale et ces quatre copies ont
> été retirées : une seule source de vérité.

**2. La parallélisation.** `pytest-xdist` était installé sur les postes mais **absent de
l'extra `dev`** de `backend/pyproject.toml`, donc indisponible en CI. Il y est déclaré, et
le nombre de workers vient d'une variable unique : `PYTEST_WORKERS`, **2 par défaut, en
local comme en CI** (les runners GitHub ont deux cœurs et partagent leur service Postgres ;
`-n auto` y demanderait plus que la machine ne peut servir). Le défaut local était 4 ; il
est descendu à 2 après un OOM global du poste de développement — voir
[Sécurité de la collecte et du parallélisme local](#sécurité-de-la-collecte-et-du-parallélisme-local--implémenté).
`--dist worksteal` rééquilibre en cours de route.

Chaque worker a **sa propre base** (`test_<db>_gw0`, `gw1`, …) et son propre processus.
Le **cache, lui, est partagé** : `DJANGO_CACHE_URL` pointe sur un vrai Redis, commun aux
quatre workers. C'est ce que la section suivante cloisonne.

**3. L'isolation des tests à service réel** — voir la section suivante. Elle ne fait pas
gagner beaucoup de temps ; elle rend le résultat **reproductible**.

### Dépendances à l'ordre révélées par la parallélisation

La première exécution parallèle a fait échouer les quatre tests de
`rag_core/test_chat_stream_gateway.py`. Ce n'était **pas** un défaut de xdist, mais une
fuite d'état préexistante que l'ordre séquentiel masquait :

`rag_text_rank_sweep._forbid_llm()` neutralise **quatre** cibles de génération et n'en
restaure aucune — légitime pour une commande de gestion, dont le processus s'arrête après.
Mais deux tests l'appelaient en ne déclarant que **trois** de ces cibles à `monkeypatch` ;
la quatrième, `chat_service.chat_query_stream`, restait donc remplacée par le lanceur
d'erreur pour tout le reste du processus. Les tests de flux qui s'exécutaient ensuite
échouaient sur un `LlmCalledError` sans rapport avec leur sujet.

Correction : les deux tests déclarent désormais les quatre cibles — ce qui les rend au
passage conformes à ce qu'ils prétendent vérifier (« toute génération est neutralisée »).
Aucun code de production n'a été touché.

### Cloisonnement du cache Redis entre workers

Les workers partagent un vrai Redis. Deux mécanismes distincts peuvent les faire se
marcher dessus, et **ils ne se corrigent pas de la même façon** — c'est ce qui a fait
qu'une première correction a paru suffisante alors qu'elle laissait la moitié du problème.

**1. Les clés.** Sans préfixe, `ops:counter:*` désigne le même jeu de clés pour tout le
monde. La suite pose donc `KEY_PREFIX = test-<worker>` sur **tous** les alias, via une
fixture de session. Le piège : tout test qui **reconstruit** `settings.CACHES` efface ce
préfixe sans que rien ne le signale. Quatre chemins le faisaient. Ils passent désormais par
la primitive `tests_root.cache_isolation.caches_cloisonnes()`, et une garde structurelle
(`unit/config/test_xdist_cache_isolation.py`) refuse toute nouvelle reconstruction directe.

**2. L'effacement — le cas qui échappait au préfixe.** `cache.clear()` de `django_redis`
est implémenté par un **`FLUSHDB`** : il vide la base **entière**, `KEY_PREFIX` compris. Le
préfixe n'offre donc *aucune* protection contre lui. Or une quinzaine de modules appellent
`cache.clear()` en toute légitimité — pour repartir d'un cache propre, pas pour effacer
celui du voisin. Sous `-n 4`, un worker perdait ainsi ses compteurs d'exploitation **au
milieu d'une assertion**, sans erreur ni journal : le test lisait zéro et l'échec paraissait
aléatoire.

C'était la cause de l'échec intermittent de `test_ops_metrics_export`. Elle a été démontrée,
pas supposée : un worker écrit `7` sous `test-gwA`, un autre appelle `cache.clear()` sous
`test-gwB`, le premier relit `0`. La suite remplace donc `RedisCache.clear` par un
effacement borné au préfixe du worker (`delete_pattern("*")`, que `django_redis` construit
à partir de `key_prefix`).

**Aucun code de production n'est touché.** En production le partage est *voulu* — quatre
workers gunicorn doivent additionner leurs compteurs — et `clear()` y garde son sens de
purge complète. Le cloisonnement ne s'applique qu'à la suite de tests.

> Mesure : sans cette correction, le groupe de tests concerné échoue **6 fois sur 6** sous
> `-n 4` (2 à 4 échecs par exécution). Avec elle, **10 exécutions sur 10** sont vertes,
> plus 5 sous `-n 8` et 20 exécutions de stress ciblé.

### Assertions sensibles à la contention

Les neuf assertions temporelles de la suite ont été inventoriées et classées. **Aucune n'a
vu son seuil relevé** : huit gardent une marge d'au moins un ordre de grandeur (par
exemple `test_feedback_latency` mesure 0,01 s contre un plafond de 0,25 s), et deux
(`tabular_limits_enforcement`) protègent une propriété algorithmique — sans le découpage
par lots, la lecture durait 34 s sous un plafond de 0,5 s.

Un seul cas a demandé une correction, et ce n'était **pas** une assertion temporelle :
`unit/preview/test_tabular_preview_bounds.py::test_la_feuille_entiere_n_est_jamais_chargee`
appelait `read_window()` **sans budget**, héritant donc du délai de PRODUCTION (5 s). Son
sujet est l'empreinte mémoire, mais il mesurait en prime la vitesse de la machine : à
~2,4 s de lecture à froid, la marge tenait dans un facteur 2 que quatre workers effacent.
Le test pose désormais un budget explicite et généreux ; **son assertion mémoire est
inchangée**, et le respect de l'échéance garde ses propres tests dédiés juste au-dessus
(`TestDeadline`, budget dépassé et budget suffisant).

**Quand lancer une campagne lourde**, sans attendre qu'on vous le demande :

- **avant toute modification sensible du moteur tabulaire** (chunking tabulaire, moteur
  DuckDB, plafonds, concurrence) ;
- **avant une mise en production jugée critique**.

Les 49 tests lourds représentent donc **72 %** du temps total, dont l'essentiel tient à
**12 paramétrages** de `test_concurrence_sans_fuite_en_charge` (montée de 1 à 10 threads) :
984 s à eux seuls, de 11 s à 1 thread jusqu'à 183 s à 10 threads.

Le module **se déclarait déjà** « exclu de la suite par défaut » dans son en-tête, mais
aucun `-m` ne l'appliquait — ni dans le Makefile, ni dans un seul workflow. Le lot n'a
donc **rien remarqué de neuf** : il applique une intention déjà écrite.

Ce qui **n'a pas** été retenu, et pourquoi :

- **Filtrer sur `slow`** : ses 32 tests sont entièrement inclus dans `tabular_volume`,
  le marqueur de module est donc le critère plus large et plus stable.
- **Filtrer sur `tabular_concurrency`** : ses 25 autres tests (capacité, libération de
  slot, budget épuisé) coûtent **0,6 s à eux tous**. Les écarter serait une perte de
  couverture sans contrepartie.
- **Écarter les grandes matrices paramétrées** : elles sont volumineuses en *nombre* et
  négligeables en *coût*. `unit/config/test_config_registry.py` produit **1 959 tests
  (21 % du dépôt) pour 2,0 s** — cinq matrices de 388 cas, une par clé du registre.
  Globalement, les tests paramétrés sont 48,7 % du nombre et **10,8 % du coût**, et
  269 fichiers (63 %) portant 4 652 tests (50 %) coûtent **35,7 s à eux tous**. Réduire
  une matrice ferait perdre de la couverture pour un gain non mesurable.

Écarter n'est pas abandonner. Le moteur tabulaire reste couvert **dans la suite
standard** par `test_tabular_engine.py` (les 13 formes de requête, en millisecondes) et
`test_tabular_limits_enforcement.py` (le plafond de temps couvrant l'ingestion). Ce que la
voie lourde ajoute, c'est la tenue **sur corpus réel et sous contention** — une propriété
qui ne se vérifie pas à petite taille, d'où son coût irréductible.

> Les tailles 100 k / 500 k / 1 M restent en plus derrière `TABULAR_VOLUME_FULL=1` : sans
> cette variable elles sont ignorées, même dans la voie lourde. Le garde-fou est
> volontairement redondant avec le marqueur — il évite qu'un appel distrait ne déclenche
> plusieurs heures de calcul sur une machine partagée.

## Sécurité de la collecte et du parallélisme local — *(implémenté)*

**Incident (poste de développement).** Deux OOM globaux (RAM 14 Gio et swap 4 Gio saturés,
deux redémarrages) ont été produits par `timeout 3500 python -m pytest -q -n 8`, lancé
**depuis la racine du dépôt, sans chemin ni marqueur**. pytest parcourait alors tout le dépôt,
y compris `backend/media` — les données d'upload runtime (~9,2 Go, ~373 000 fichiers,
~688 000 dossiers). Mesuré : **330 s de collecte et 1,76 Gio de RSS retenus par processus**,
contre 3 s et ~95 Mio pour une collecte ciblée. Chaque worker xdist refaisant toute la
collecte, 8 workers représentaient ~14 Gio avant le premier test. Les sous-processus Python
lancés par certains tests (~80 Mio chacun) n'étaient pas en cause, pas plus qu'une double
campagne ; le `too many clients` PostgreSQL observé ensuite est un **effet secondaire** de
l'emballement, pas une cause (`max_connections` reste à 100).

**Protections** (configuration unique `pyproject.toml` à la racine) :

| Protection | Effet | Cas couvert |
|---|---|---|
| `testpaths = ["tests/tests_root"]` | sans chemin, depuis la racine, pytest ne cherche que dans `tests/tests_root` | `python -m pytest` à la racine |
| `norecursedirs` += `backend/media` | pytest ne descend jamais dans `backend/media` pendant la découverte | `pytest .`, `pytest backend`, `cd backend && pytest` |
| `PYTEST_WORKERS ?= 2` (Makefile) | défaut local ramené de 4 à 2 ; `1` retire toujours `-n` | toutes les cibles `make test-backend*` |

`norecursedirs` **remplace** la liste par défaut de pytest : les neuf motifs par défaut sont
recopiés. Seule la **découverte** est interdite dans `backend/media` ; un test peut toujours
y **lire** un fichier. Les deux protections sont verrouillées par
`tests/tests_root/unit/test_pytest_collection_safety.py`, qui vérifie la configuration **et**
son effet réel sur une réplique miniature du dépôt (sans jamais parcourir le vrai
`backend/media`).

**Règle d'exécution (impérative, humains comme agents) :**

- la suite backend se lance **par le Makefile** (`make test-backend`, `make test-backend-root`,
  …) — jamais par `python -m pytest` sans chemin explicite ;
- en débogage, pytest direct uniquement avec un **fichier, dossier ou node id explicite**, de
  préférence sans xdist, sinon **`-n 2` maximum** ;
- sur un poste partagé, **aucun `-n` supérieur à 2** hors qualification spécifiquement autorisée ;
  4 workers ne seront de nouveau présentés comme sûrs qu'après une mesure dédiée.

## Profils FAST / STANDARD / FULL — *(implémenté, mesuré sur le candidat de publication)*

Trois profils **explicites** recouvrent les voies ci-dessus. Ils sont définis une seule
fois dans le `Makefile` (`test-fast`, `test-standard`, `test-full`) et relayés par
`ci/test-backend-{fast,standard,full}.sh`.

| Profil | Commande | Sélection | Services | Tests exécutés | Durée (4 workers) |
|---|---|---|---|---:|---:|
| **FAST** | `make test-fast` | `--no-db -m "not slow and not tabular_volume and not tabular_concurrency and not external"` | aucun | 9 966 exécutés (29 sautés, 3 066 désélectionnés) | ≈ 2 min 10 s |
| **STANDARD** | `make test-standard` | `-m "not tabular_volume and not external and not slow"` | PostgreSQL + Redis | 12 965 exécutés (24 sautés) | ≈ 3 min 45 s |
| **FULL** | `make test-full` | aucun filtre (`external` collecté, sauté sans `RAG_RUN_EXTERNAL_TESTS=1`) | PostgreSQL + Redis | 13 011 exécutés (32 sautés : campagne volumétrique longue, services réels, extras optionnels absents) | ≈ 15 min 15 s |

- **FAST** repose sur l'option `--no-db` (`tests/tests_root/conftest.py`) : à la
  collecte, tout test portant le marqueur `django_db` ou demandant une fixture de base
  (`db`, `transactional_db`, `django_db_setup`, `live_server`) est **désélectionné**.
  Rien n'est exécuté en mode dégradé : un test qui a besoin de la base ne tourne pas
  sans elle, il attend le profil STANDARD. Les campagnes `tabular_concurrency` en sont
  aussi écartées (elles saturent le poste, ce n'est pas le but d'une boucle rapide).
- **STANDARD** est la voie bloquante historique (`test-backend-all`). Le marqueur `slow`
  est désormais exclu explicitement, conformément à sa définition dans `pyproject.toml` ;
  au moment de la mesure, tous les tests `slow` vivent déjà dans la campagne
  `tabular_volume`, la sélection effective est donc inchangée.
- **FULL** est la référence de qualification exhaustive (release) : elle inclut la campagne
  `tabular_volume`. Elle écrit un rapport JUnit si `PYTEST_JUNIT_PATH` est posé, comme
  les deux autres profils.

Règles de gouvernance des profils :

1. un test qui échoue n'est **jamais** retiré d'un profil ni supprimé pour verdir la suite ;
2. un test ne change de profil que par un **marqueur** posé dans son module, avec la
   justification en commentaire (coût mesuré, propriété couverte ailleurs) ;
3. une suppression de test exige d'identifier la propriété couverte et le contrôle qui la
   protège désormais (cf. `CONTRIBUTING.md`).

Les fichiers les plus coûteux de la voie standard (mesure sur le candidat précédent, durée cumulée) sont
`corpus/test_corpus_admission_calibration_command.py` (132 s : chaque test ajuste une
régression logistique validée en croisé), `rag_core/test_tabular_query_benchmark.py`
(76 s : une exécution complète du benchmark, partagée par six tests dont une propriété de
sécurité — aucune charge hostile n'atteint le SQL) et `unit/test_space_migration_atomic.py`
(60 s : atomicité des migrations d'espace). Ils restent en STANDARD : leur coût tient à ce
qu'ils prouvent, pas à une redondance.

## Commandes d'exécution

```bash
# Tout : backend STANDARD + frontend
make test

# Backend — partition exacte : 9 313 + 49 + 15 = 9 377
make test-backend-all        # compileall + STANDARD (2 workers)
make test-backend-root       # STANDARD seule
make test-backend-sequential # STANDARD sans xdist — diagnostic           ~8 min
make test-backend-heavy      # LOURDE   -m "tabular_volume"    49 tests  ~21 min
make test-backend-external   # EXTERNE  -m "external"          15 tests  (opt-in)
make test-backend-full       # COMPLÈTE aucun filtre           9 377
make test-frontend           # cd frontend && npm test (vitest run)

# Réglages de la voie backend (une seule variable par sujet)
PYTEST_WORKERS=1 make test-backend                       # sans xdist (trace lisible)
PYTEST_WORKERS=2 make test-backend-root                  # défaut, = ce que fait la CI
PYTEST_JUNIT_PATH=artifacts/x.xml make test-backend-root # rapport JUnit (sinon aucun)

# Manuel — CIBLÉ uniquement (fichier, dossier ou node id explicite ; sans xdist, ou -n 2 max)
cd tests/tests_root && source ../../.venv/bin/activate
python -m pytest unit/ -q        # pur, sans DB, rapide
python -m pytest integration/ -q # services + DB
python -m pytest http_api/ -q    # endpoints

# AI Gateway (NON couvert par la CI)
cd services/ai_gateway && PYTHONPATH=. pytest tests/

# Frontend en watch
cd frontend && npm run test:watch
```

Voir aussi le référentiel des commandes : [../10-reference/commandes.md](../10-reference/commandes.md).

## Intégration continue

**Cinq** workflows sous `.github/workflows/` : `ci.yml`, `docs-check.yml`,
`rag-benchmark.yml`, `tests-heavy.yml` et `ci-image.yml`. Seuls `ci.yml` et
`rag-benchmark.yml` bloquent les PR ; `docs-check.yml` est automatique et rapide ;
`tests-heavy.yml` ne se déclenche **qu'à la main** ; `ci-image.yml` ne part que
lorsque la définition de l'environnement CI change (voir plus bas).

> *Correction.* Cette page annonçait auparavant « deux workflows sémantiques
> self-hosted et non bloquants ». Ils ont été **supprimés** (voir la table des
> workflows supprimés plus bas) et le dépôt ne compte **aucun runner enregistré**.

### Les commandes métier vivent dans `ci/`, pas dans les YAML

Depuis le lot « contrat CI portable », chaque workflow est un **adaptateur mince** :
il prépare l'environnement (checkout, Python/Node, cache, installation, service
PostgreSQL), appelle un script de [`ci/`](../../ci/README.md), puis publie les
artefacts. La logique métier n'est plus dupliquée dans le YAML.

| Script | Remplace | Appelé par |
|---|---|---|
| `ci/check-docs.sh` | `python scripts/check_docs.py` | `docs-check.yml` |
| `ci/check-config.sh` | `manage.py check` + `config_audit` + `generate_config_reference --check` | `ci.yml` |
| `ci/test-backend-standard.sh` | `make test-backend-all` | `ci.yml` |
| `ci/test-frontend.sh` | `npm test` + `npx tsc --noEmit` + `npm run build` | `ci.yml` |
| `ci/benchmark-retrieval.sh` | les cinq étapes du benchmark | `rag-benchmark.yml` |
| `ci/test-tabular-heavy.sh` | `make test-backend-heavy` | `tests-heavy.yml` |

Ces scripts sont appelables **à l'identique en local** — c'est le seul moyen de
reproduire exactement ce que fait la CI sans relire un YAML. Contrat complet,
variables et codes de sortie : [`ci/README.md`](../../ci/README.md).

**La suite backend ne lit plus aucun fichier de workflow.** La garde
`test_workflow_present_and_invariants` ouvrait `rag-benchmark.yml` et y cherchait
des noms d'actions : la suite dépendait donc d'un orchestrateur précis, et toute
réécriture du workflow cassait le test censé le valider. Ces invariants — provider
stub, validation jamais désactivée, `--fail-under`, `--decision-k 5`, artefacts,
propagation des codes `0`/`1`/`2` — sont désormais exercés **fonctionnellement**
sur le script, dans `tests/tests_root/ci/test_ci_scripts_contract.py`.

> **GitHub reste l'orchestrateur de référence** — et le seul aujourd'hui. Aucun
> autre n'est installé ni configuré. L'extraction ci-dessus ne change ni les
> déclencheurs, ni les services, ni les artefacts, ni les résultats.

### L'environnement d'exécution est verrouillé

Les scripts disent *ce que* la CI fait ; les fichiers suivants disent *dans quoi* :

| Fichier | Rôle |
|---|---|
| `ci/requirements-ci.lock` | Dépendances Python tierces, versions exactes et **empreintes obligatoires** (`--require-hashes`) |
| `ci/Dockerfile.ci` | Image d'exécution : Python 3.12, Node.js 20, outils système, dépendances du lock |
| `ci/requirements-ci.in` | Le `[build-system]` du pyproject, que `uv pip compile` ne résout pas seul |
| `ci/lock-python.sh` | Régénère le lock, ou détecte sa dérive (`--check`) |

Tout est épinglé : image de base **par digest**, `uv` par version **et** digest,
Node.js par version **et** SHA-256 de l'archive officielle, dépendances Python
par empreintes. L'extra `rerank` est **exclu** — il entraînerait `torch` et la
pile CUDA, inutiles à la CI.

Le contexte de construction est **`ci/`, jamais la racine du dépôt** : le démon
Docker ne reçoit ni `.env`, ni `.venv/`, ni `backend/media/`, ni `.git/`.
Mesuré : **112 kB** au lieu de **8,27 Go**. `ci/audit-build-context.sh` le prouve
par un test négatif à sentinelles factices, sans lire aucun secret.

Le workflow **`ci-image.yml`** construit cette image **de façon éphémère** et y
exécute les points d'entrée portables. Son coût réel est de **9 min 17** — les
13 s de build n'en sont que 2 %, l'essentiel étant la ré-exécution des suites
dans l'image. Il ne part donc que lorsque la définition de l'environnement
change, lorsqu'un script qu'il exécute change, ou à la demande.

> ⚠️ **`runs-on: ubuntu-latest`.** Ce job exécute du code de la PR **et** dispose
> d'un démon Docker : il ne doit pas être transposé tel quel vers un runner
> auto-hébergé sur un hôte sensible. Son adaptation devra vivre dans une VM CI
> isolée, sans montage du socket Docker de l'hôte — lots ultérieurs.

> **Rien n'est publié.** Aucun registre, aucun `docker login`, aucun `docker
> push`, aucune archive OCI, aucun cache de construction externe. Le digest
> obtenu **n'est pas un digest promu** : registre, signature et promotion
> relèvent de lots ultérieurs. Contrat complet : [`ci/README.md`](../../ci/README.md).

### `ci.yml` — bloquant (push + PR sur `main`)

- **Job `backend`** (ubuntu-latest) : service `pgvector/pgvector:pg16` (health-checked),
  `DJANGO_ENV=dev`, `EMBEDDINGS_PROVIDER=stub`, `CELERY_TASK_ALWAYS_EAGER=1`, Python 3.12,
  `pip install -e "backend[dev,ocr,tables,ifc]"` (wheels OCR requises par les tests image ;
  binaire tesseract **non** requis — OCR mocké ; l'extra `dev` fournit `pytest-xdist` ;
  l'extra `ifc` fournit `ifcopenshell`, sans lequel les tests IFC échouent).
  Lance **`ci/test-backend-standard.sh`**, qui délègue à `make test-backend-all`
  (compileall + la suite **standard**, `-m "not tabular_volume and not external"`),
  avec :
  - `PYTEST_WORKERS: "2"` — **plafond assumé** : les runners ont deux cœurs et partagent
    leur service Postgres ; `-n auto` y demanderait plus que la machine ne peut servir ;
  - `PYTEST_JUNIT_PATH: artifacts/pytest-standard.xml`, publié en artefact avec
    `if: always()` — le rapport est conservé **surtout** quand la suite échoue ;
  - `RAG_RUN_EXTERNAL_TESTS` **délibérément non défini** : la famille `external` reste
    inerte même si un `-m` venait à la sélectionner par accident.

  Les 25 tests les plus lents sont affichés à chaque exécution (`addopts` du
  `pyproject.toml` racine), en CI comme en local : une dérive de durée se voit au lieu de
  s'accumuler.
- **Job `frontend`** (ubuntu-latest) : node 20 et `npm ci` restent dans le YAML
  (préparation d'environnement) ; les contrôles métier — `npm test` (vitest),
  **`npx tsc --noEmit`** (typecheck), puis `npm run build` (`next build`) — sont
  portés par **`ci/test-frontend.sh`**, qui n'installe rien et échoue explicitement
  si `node_modules` est absent.

### `tests-heavy.yml` — **manuel uniquement**, jamais automatique

Campagne tabulaire lourde (`make test-backend-heavy`, 49 tests, ~21 min). Le workflow
n'expose **que** `workflow_dispatch` : aucun `schedule`, aucun `push`, aucun
`pull_request`. Il ne partira donc jamais de lui-même, quelle que soit l'activité du
dépôt — et il ne peut pas non plus être désactivé par GitHub pour inactivité, contrainte
qui pèse sur les workflows planifiés.

Une entrée booléenne **`full_volume`** (défaut `false`) est transmise à
**`ci/test-tabular-heavy.sh`** sous forme de drapeau explicite
(`--full-volume` / `--no-full-volume`) ; le script pose alors `TABULAR_VOLUME_FULL`
et ajoute les tailles 100 k / 500 k / 1 M — plusieurs heures. Sans elle, ces trois
cas sont ignorés même dans la campagne lourde. Toute autre valeur est **refusée**
plutôt qu'interprétée, et `--dry-run` affiche le périmètre sans rien lancer.

### `rag-benchmark.yml` — bloquant, filtré par chemins

Gate de retrieval **stub-only**, déclenché uniquement quand des chemins retrieval /
benchmark changent — la liste inclut désormais `ci/benchmark-retrieval.sh` et
`ci/lib.sh`, qui portent le contrat d'exécution. La source de vérité de ce contrat
est le script, plus le YAML : provider stub, validation **jamais** désactivée,
`--fail-under`, `--decision-k 5` figé. Détails et exit codes :
benchmark-rag.md.

### Workflows supprimés — *(nettoyage)*

Neuf workflows ont été **supprimés** : ils n'avaient **jamais réussi une seule
exécution**. Cinq ciblaient `runs-on: [self-hosted, ollama]` alors que le dépôt ne compte
**aucun runner enregistré** — ils s'accumulaient en file d'attente jusqu'à expiration ;
les autres avaient pourri sans que personne ne le voie, un rouge permanent cessant d'être
lu (`pytest: command not found` faute d'étape d'installation, garde `env_bool` devenue
fausse après un refactor).

| Workflow supprimé | Ce qu'il lançait |
|---|---|
| `rag-answerability-benchmark.yml` | `rag_benchmark` + compare, sémantique (self-hosted) |
| `rag-answerability-cross-encoder.yml` | idem + `test_answerability_cross_encoder.py` |
| `rag-answerability-p2-4-evaluation.yml` | `test_answerability_families.py` |
| `rag-benchmark-nomic.yml` | `rag_benchmark` nomic (self-hosted) |
| `rag-demo-extended-v3-validation.yml` | `build_extended_dataset_v3 --check` + tests v3 |
| `rag-demo-extended-v3-1-validation.yml` | idem v3.1 |
| `rag-embeddings-config-check.yml` | `test_embeddings_profile_config.py` + garde `env_bool` |
| `rag-retrieval-p2-5-b-pdf.yml` | `test_fts_slash_identifiers.py`, `test_rrf_k_setting.py` |
| `rag-text-rank-calibration.yml` | `rag_text_rank_sweep` (self-hosted) |

**Aucune couverture de test n'est perdue** : les 208 tests que ces workflows relançaient
appartiennent tous à `tests/tests_root` et sont donc **déjà exécutés par `ci.yml` à chaque
push et chaque PR**. Le contrôle « JSONL commité ≡ source » est lui aussi doublé côté
tests (`test_le_jsonl_committe_correspond_a_la_source`, `test_benchmark_extended.py`).

Ce qui n'est plus **automatisé** — mais reste disponible à la main via `manage.py` :
les campagnes sémantiques (`rag_benchmark` sur nomic / answerability) et le sweep de
calibration `rag_text_rank_sweep`. Elles supposaient de toute façon un runner Ollama qui
n'existe pas ; les relancer demandera d'en rattacher un.

## Protocoles de benchmark

| Protocole | Page | Statut |
|---|---|---|
| RAG (retrieval, recall@k, MRR) | benchmark-rag.md | branché en CI |
| Modèles LLM | benchmark-modeles-llm.md | manuel |
| Excel — vérité terrain stable | benchmark-stable-ground-truth.md | implémenté |
| Excel — agrégation tabulaire (vérité terrain) | benchmark-excel-tabular-aggregation.md | protocole implémenté, non branché |
| Moteur tabulaire de production | [tabular-query-benchmark.md](tabular-query-benchmark.md) | protocole défini, **baseline planifiée** |
| Admission du retrieval — dataset FR/EN et revue humaine | benchmark-admission-dataset.md | **dataset v3 produit et relu à 100 %** (164 cas) ; calibration non lancée |
| Suffisance des preuves | lot12-suffisance-des-preuves.md | **suspendu avant évaluation** — garde de curation non qualifiée ; aucun signal mesuré |
| Qualification réelle sur documents (noms d'origine, identité par SHA-256, répétitions) | qualification-reelle-documents.md | protocole défini, manuel |

## Commandes qualité

Ces outils sont configurés mais **manuels** (voir les lacunes plus bas) :

```bash
# Python (depuis backend/)
ruff check .                 # E, F, I ; line-length 100 ; ignore E501
ruff check --fix .
black backend/               # ou : black --check backend/
python manage.py check       # checks Django (system checks llm_router.*)
python -m compileall -q apps ../tests/tests_root

# Frontend (depuis frontend/)
npm run lint                 # next lint (ESLint)
npx tsc --noEmit             # typecheck (ce que lance la CI)
npm run build                # next build

# Agrégats
make test                    # backend (suite standard) + frontend
make openapi-bundle          # bundle du contrat OpenAPI
```

## Limites et lacunes connues

Honnêtement documentées (voir aussi [../10-reference/limites-connues.md](../10-reference/limites-connues.md)) :

- **Aucun gate Python lint/type en CI** : `ci.yml` ne lance ni ruff, ni `black --check`,
  ruff/black sont présents en config mais **manuels**. En revanche `manage.py check`
  **tourne bien** dans `ci.yml` (via `ci/check-config.sh`), aux côtés de
  `config_audit` et `generate_config_reference --check` : la mention « ne tourne que
  dans les workflows benchmark » était fausse.
- **Aucun mypy** : aucun typage statique Python nulle part (pas de dépendance, pas de
  `[tool.mypy]`, pas de config).
- **Suite AI Gateway hors CI** : `services/ai_gateway/tests` (14 tests) n'est lancée par
  aucun workflow.
- **Deux arbres backend** : dette technique ; toujours utiliser `make test-backend-all`.
  Fixtures dupliquées (`rag_space`, `api_client`).
- **Aucune couverture mesurée** (pas de config coverage.py), **aucun e2e/Playwright**,
  aucun test de migration, aucun test de charge, aucun test property-based/fuzz sur le
  chunker.
- **Qualité de recherche vectorielle non exercée dans la CI bloquante** : le stub est
  non sémantique. Seuls les benchmarks nomic/answerability (self-hosted, non bloquants)
  la testent.

## Corrections de l'ancienne documentation de tests (obsolète)

- « Pas de CI configurée » → **faux** : `ci.yml`, `docs-check.yml`, `rag-benchmark.yml`
  et `tests-heavy.yml` existent.
- « Pas de tests Vitest/Jest/Playwright côté frontend » → **faux** : ~37 fichiers
  Vitest + Testing Library sous `tests/frontend/`.
- Décomptes « 176 pytest / 74 vitest » → **obsolètes** : les décomptes réels de `def
  test_` sont bien supérieurs (backend ~1198 + ~754, frontend ~294 assertions).
- « OCR / images absents » → **faux** : couverts sous `tests/tests_root/unit/extraction/`.

## Références d'implémentation

- `Makefile` (cibles `test`, `test-backend-all/-root/-legacy`, `test-frontend`, `openapi-bundle`)
- `pyproject.toml` (repo root : `[tool.pytest.ini_options]` — config pytest unique)
- `tests/tests_root/conftest.py`
- `backend/pyproject.toml` (`[tool.ruff]`, `[tool.black]`)
- `backend/conftest.py`, `tests/tests_root/rag_core/conftest.py`, `backend/testing/fixtures.py`
- `backend/apps/llm_router/embeddings.py` (`_stub_embed`, escape hatch stub)
- `frontend/package.json`, `frontend/vitest.config.ts`, `tests/frontend/setup.ts`, `scripts/link-test-deps.mjs`
- `services/ai_gateway/tests/`, `services/ai_gateway/pyproject.toml`
- `.github/workflows/{ci.yml, docs-check.yml, rag-benchmark.yml, tests-heavy.yml}`
