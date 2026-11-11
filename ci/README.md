# `ci/` — points d'entrée CI portables

Les commandes métier de l'intégration continue vivent **ici**, pas dans les fichiers
de workflow. Chaque script est appelable à l'identique :

- depuis un poste de développement ;
- depuis GitHub Actions (aujourd'hui) ;
- depuis un autre orchestrateur (plus tard), sans réécrire la logique.

> **Statut de GitHub : transitoire.** GitHub Actions reste **aujourd'hui**
> l'orchestrateur de référence et la seule porte de qualité automatique du dépôt.
> Les workflows de `.github/workflows/` sont désormais des **adaptateurs minces** :
> ils préparent l'environnement (checkout, Python/Node, cache, installation,
> service PostgreSQL), appellent un script d'ici, puis publient les artefacts.
> Aucun autre orchestrateur n'est installé ni configuré à ce stade.

## Ce que fait un script, ce que fait l'orchestrateur

| Responsabilité | Où |
|---|---|
| checkout, versions de langage, caches, installation des dépendances | **orchestrateur** (YAML) |
| démarrage du service PostgreSQL, variables de connexion | **orchestrateur** (YAML) |
| commandes métier, options, seuils, ordre des étapes | **script `ci/`** |
| chemins des artefacts produits | **script `ci/`** |
| publication et rétention des artefacts, résumé de job | **orchestrateur** (YAML) |

Les scripts **n'installent rien**, ne démarrent aucun service, ne créent aucun
conteneur, ne modifient aucun service système et ne chargent **jamais** un `.env`.
Ils n'affichent jamais la valeur d'une variable — seulement son nom.

## Scripts

| Script | Rôle | Service requis | Artefacts |
|---|---|---|---|
| `lib.sh` | Fonctions communes. **À sourcer**, jamais à exécuter. | — | — |
| `check-docs.sh` | Cohérence de `docs/` — délègue à `scripts/check_docs.py`. | aucun | — |
| `check-config.sh` | `manage.py check`, `config_audit`, `generate_config_reference --check`. | PostgreSQL | — |
| `test-backend-fast.sh` | Profil **FAST** — délègue à `make test-fast` (`--no-db`, aucun service). | aucun | rapport JUnit si `PYTEST_JUNIT_PATH` |
| `test-backend-standard.sh` | Profil **STANDARD** — délègue à `make test-backend-all`. | PostgreSQL | rapport JUnit si `PYTEST_JUNIT_PATH` |
| `test-backend-full.sh` | Profil **FULL** — délègue à `make test-full` (aucun filtre ; `external` sauté). | PostgreSQL | rapport JUnit si `PYTEST_JUNIT_PATH` |
| `test-frontend.sh` | vitest, `tsc --noEmit`, `next build`. | aucun | — |
| `benchmark-retrieval.sh` | Garde de non-régression du retrieval. | PostgreSQL | candidat + comparaison JSON/Markdown |
| `test-tabular-heavy.sh` | Campagne lourde tabulaire — **manuelle**. | PostgreSQL | aucun (voir plus bas) |

## Codes de sortie

| Code | Sens |
|---|---|
| `0` | succès |
| `1` | échec métier (test rouge, lien cassé, régression de benchmark) |
| `2` | `benchmark-retrieval.sh` : rapports **incompatibles** ou entrée invalide. `check-docs.sh` : répertoire de documentation introuvable. |
| `64` | **prérequis ou usage invalide** — commande absente, `manage.py` introuvable, argument inconnu. Jamais un verdict métier. |

`64` (`EX_USAGE`) et non `1` ou `2` : `benchmark-retrieval.sh` réserve `0`, `1` et
`2` à la sémantique de la comparaison. Un prérequis manquant qui sortirait en `2`
serait indiscernable d'un benchmark incompatible.

## Variables

Aucune n'est obligatoire pour un usage local nominal ; toutes ont une valeur par
défaut identique au comportement actuel de la CI.

| Variable | Défaut | Effet | Lue par |
|---|---|---|---|
| `RAGATM_PYTHON` | *(vide)* | Force l'interpréteur. Sinon : `.venv/bin/python` du dépôt, puis `$VIRTUAL_ENV`, puis `python3`. | tous |
| `RAGATM_CI_ARTIFACTS_DIR` | `/tmp` | Répertoire des artefacts du benchmark. | `benchmark-retrieval.sh` |
| `BASELINE` | `benchmarks/baseline/demo-fr.json` | Baseline committée (relatif à `backend/`). | `benchmark-retrieval.sh` |
| `DATASET` | `benchmarks/datasets/demo-fr.jsonl` | Dataset (relatif à `backend/`). | `benchmark-retrieval.sh` |
| `FULL_VOLUME` | *(vide)* | `0\|1\|true\|false`. Toute autre valeur est **refusée**. | `test-tabular-heavy.sh` |
| `PYTEST_WORKERS` | `2` (local et CI) | Workers xdist — lue par le **Makefile**. | `test-backend-standard.sh` |
| `PYTEST_JUNIT_PATH` | *(vide)* | Chemin du rapport JUnit — lue par le **Makefile**. Vide = aucun fichier. | `test-backend-standard.sh` |
| `BACKEND_BASE_URL` | `http://127.0.0.1:8000` | URL backend pour `next build`. | `test-frontend.sh` |

Les variables de connexion (`DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`,
`DB_PASSWORD`, `DJANGO_SECRET_KEY`) sont fournies par l'appelant — en local par
`scripts/dev/up.sh`, en CI par le bloc `env:` du workflow.

## Usage local

```bash
# Documentation (aucun service, aucune installation, ~12 s)
ci/check-docs.sh
ci/check-docs.sh --strict          # les options sont transmises telles quelles

# Gardes de configuration puis suite backend standard (PostgreSQL requis)
ci/check-config.sh
ci/test-backend-standard.sh
PYTEST_WORKERS=2 PYTEST_JUNIT_PATH=artifacts/pytest-standard.xml ci/test-backend-standard.sh

# Frontend (npm ci préalable dans frontend/ — le script n'installe rien)
ci/test-frontend.sh

# Benchmark retrieval : 0 conforme, 1 régression, 2 incompatible
ci/benchmark-retrieval.sh; echo "code=$?"
RAGATM_CI_ARTIFACTS_DIR=./artifacts/bench ci/benchmark-retrieval.sh

# Campagne lourde — MANUELLE, ~21 min (plusieurs heures avec --full-volume)
ci/test-tabular-heavy.sh --dry-run     # affiche le périmètre, ne lance rien
ci/test-tabular-heavy.sh
ci/test-tabular-heavy.sh --full-volume
```

Les scripts fonctionnent depuis **n'importe quel répertoire courant** : ils
résolvent la racine du dépôt depuis leur propre emplacement.

## Invariants du benchmark retrieval

`benchmark-retrieval.sh` est la **source de vérité** du contrat d'exécution. Le YAML
ne porte plus aucun de ces choix :

- `--embeddings-provider stub` — profil déterministe, **aucun appel réseau**,
  aucun provider d'embeddings, aucun LLM ;
- `--skip-validation` **jamais passé** — la validation documentaire reste active ;
- `--with-answers` **jamais passé** — il déclencherait une génération LLM ;
- `--fail-under` **toujours passé** — pas de PASS silencieux ;
- `--decision-k 5` **figé dans le script**, sans variable pour l'assouplir. Même
  valeur que `compare.DEFAULT_DECISION_K` ; le lien entre les deux est testé.

Artefacts (sous `RAGATM_CI_ARTIFACTS_DIR`, `/tmp` par défaut) :
`demo-fr-candidate.json`, `demo-fr-comparison.json`, `demo-fr-comparison.md`, et le
répertoire `bench/` du rapport brut. Le Markdown est écrit **sur disque** : c'est à
l'adaptateur, et à lui seul, de le recopier dans le résumé de job d'un
orchestrateur.

## Aucun appel externe dans la CI standard

Les voies automatiques (`check-docs`, `check-config`, `test-backend-standard`,
`test-frontend`, `benchmark-retrieval`) n'appellent **aucun service externe** :
ni AI Gateway, ni Ollama, ni provider d'embeddings réel.

`RAG_RUN_EXTERNAL_TESTS` n'est **jamais** définie par un script d'ici. La famille
`external` est de surcroît exclue par le marqueur de la voie standard
(`-m "not tabular_volume and not external"`). Les tests à service réel se lancent
uniquement à la main, via `make test-backend-external`.

## Campagne lourde : aucun artefact aujourd'hui

`make test-backend-heavy` n'écrit pas de rapport JUnit, contrairement à la voie
standard. `test-tabular-heavy.sh` n'en ajoute pas : ce serait un changement de
politique CI, hors du périmètre de ce lot.

## Environnement reproductible : lock Python et image CI

Les scripts ci-dessus décrivent **ce que la CI fait**. Les deux fichiers suivants
décrivent **dans quoi elle le fait**.

| Fichier | Rôle |
|---|---|
| `ci/requirements-ci.lock` | Toutes les dépendances Python tierces, versions exactes et **empreintes obligatoires** |
| `ci/Dockerfile.ci` | Image d'exécution : Python 3.12, Node.js 20, outils système, dépendances du lock |
| `ci/requirements-ci.in` | Compléments au pyproject : le `[build-system]`, que `uv pip compile` ne résout pas seul |
| `ci/lock-python.sh` | Régénère le lock (`ci/lock-python.sh`) ou détecte sa dérive (`--check`) |
| `ci/.dockerignore` | Borne le contexte de construction à un seul fichier |
| `ci/audit-build-context.sh` | Test **négatif** : prouve qu'aucun fichier local n'entre dans le contexte |

> **Cette image n'est pas publiée.** Elle est construite de façon **éphémère**
> dans le run qui la valide, utilisée dans ce même run, puis jetée. Aucun
> registre, aucun `docker login`, aucun `docker push`, aucune archive OCI,
> aucun cache de construction externe. Le digest obtenu localement **n'est pas
> un digest promu** : registre, signature et promotion relèvent de lots
> ultérieurs.

### Le lock

```bash
ci/lock-python.sh            # régénère ci/requirements-ci.lock
ci/lock-python.sh --check    # échoue (code 1) si le lock a dérivé du pyproject
```

| Propriété | Valeur |
|---|---|
| Source | `backend/pyproject.toml` |
| Extras **inclus** | `dev`, `ocr`, `tables`, `ifc` — les mêmes que le job `backend` de `ci.yml` |
| Extra **exclu** | `rerank` — il entraînerait `torch` et `sentence-transformers`, inutiles à la CI et très lourds |
| Cible | Python **3.12**, `x86_64-manylinux_2_31` (plancher glibc de la wheel `ifcopenshell` ; image CI 2.41, runner 2.39, runtime 2.36) |
| Résolveur | `uv`, épinglé par **version et digest** dans `ci/Dockerfile.ci` |
| Borne temporelle | `--exclude-newer` = `EXCLUDE_NEWER`, horodatage UTC **littéral** du dernier commit modifiant `backend/pyproject.toml` ou `ci/requirements-ci.in` |
| Empreintes | `--generate-hashes` ; installation en `--require-hashes` |

Le lock est généré **dans l'étage `toolchain` de `ci/Dockerfile.ci`**, où `uv`
est épinglé. Générateur et installateur sont donc le même binaire : ils ne
peuvent pas diverger. Le résultat ne dépend que de sources versionnées — deux
régénérations propres produisent un fichier identique octet pour octet.

**Le paquet local du dépôt n'est pas dans le lock.** Une installation locale n'a
pas d'empreinte de distribution comparable à celle d'une roue publiée : l'y
inclure obligerait à relâcher `--require-hashes` pour tout le monde. Il s'installe
donc **à part**, en deux temps et sans jamais désactiver la vérification des
autres empreintes :

```bash
python -m venv --system-site-packages .venv      # 1. hérite des paquets de l'image
. .venv/bin/activate
pip install --no-deps --no-build-isolation -e backend   # 2. paquet local seul
```

`--no-deps` : rien d'autre ne s'installe. `--no-build-isolation` : pip n'a pas à
télécharger un backend de construction — `setuptools` et `wheel` sont **dans le
lock**, avec leurs empreintes, précisément pour que cette étape reste **hors
ligne**. C'est aussi pourquoi `ci/requirements-ci.in` existe : `uv pip compile` ne
résout que les dépendances d'exécution, pas le `[build-system]`.

Cette installation n'est pas facultative. `tests/tests_root/actions/test_wiring_flag.py`
lance un **subprocess hermétique** pour éprouver le défaut applicatif d'un flag ;
ce subprocess n'importe `config.settings` que grâce au chemin ajouté par
l'installation éditable. Sans elle, dix tests échouent.

### L'image

| Élément | Épinglage |
|---|---|
| Base | `python:3.12-slim` **par digest sha256** |
| `uv` | version **et** digest de l'image officielle |
| Node.js | version **et** SHA-256 de l'archive officielle, vérifié avant extraction |
| Dépendances Python | le lock, en `--require-hashes` |
| Paquets système | `curl git jq make xz-utils zstd` — `bash`, `tar`, `sha256sum` et `flock` sont déjà dans la base |

`jq` et `zstd` ne sont pas décoratifs : `tests/tests_root/deploy/test_backup_scripts.py`
les exige (`REQUIRED_TOOLS`). Sans eux, ces tests seraient **sautés** et la
couverture baisserait sans que rien ne l'annonce.

**Architecture qualifiée : `linux/amd64` uniquement.** Le SHA-256 de Node vaut
pour `linux-x64` ; une autre architecture exigerait d'autres empreintes. Ce
fichier ne prétend pas être multiarchitecture.

L'image tourne sous un utilisateur **non privilégié** (`ci`, UID 1001 — celui du
compte `runner` des runners GitHub hébergés, de sorte qu'un espace de travail
monté reste inscriptible). Un autre hôte peut imposer `--user` à l'exécution.

### Le contexte de construction

**Le contexte est `ci/`, jamais la racine du dépôt.**

```bash
docker build --file ci/Dockerfile.ci --tag ragatm-ci:local ci/
```

Sans cette précaution, `docker build … .` définit comme contexte l'arbre entier —
**9,7 Go** ici, dont `.env` et `.env.prod` (non suivis, en `0600`), `.venv/`,
`backend/media/` et `.git/`. L'image finale n'en gardait rien, mais le contexte,
lui, partait au démon et à son cache.

Deux garanties se cumulent, et la première suffirait :

1. **le contexte est `ci/`** — les fichiers sensibles sont hors d'atteinte par
   construction, sans filtre à maintenir ;
2. **`ci/.dockerignore` est une liste blanche** : `*` exclut tout, une seule
   ligne réadmet `requirements-ci.lock`, le seul fichier que le `COPY` utilise.

> ⚠️ **`<dockerfile>.dockerignore` ne suffit pas.** Cette variante a été essayée
> puis rejetée : elle n'est honorée que par BuildKit. Mesuré ici, une
> construction avec `DOCKER_BUILDKIT=0` envoyait toujours **8,27 Go**. Seul un
> `.dockerignore` à la **racine du contexte** vaut pour les deux constructeurs.

| Mesure | Avant | Après |
|---|---:|---:|
| Contexte, BuildKit | non borné (arbre entier) | **111,80 kB** |
| Contexte, constructeur classique | **8,27 Go** | **121,9 kB** |

**Preuve, rejouable :**

```bash
ci/audit-build-context.sh     # 0 = contexte propre · 1 = fuite · 64 = prérequis
```

Le script dépose neuf sentinelles **factices** (`.env`, `id_rsa_*`, `.netrc`,
`*.pem`, `node_modules/…`, `artifacts/…`, `__pycache__/…`) dans une **copie**
du contexte, hors du dépôt, construit une cible d'audit qui recopie tout, puis
liste ce qui est réellement arrivé. **Aucun `.env` réel n'est lu, copié ni
monté** : les sentinelles ne contiennent qu'une chaîne inerte.

Il détecte les deux défauts symétriques — une fuite (RC 1) **et** une liste
blanche trop stricte qui priverait la construction de son lock (RC 1). Les deux
chemins ont été exercés.

### Le venv attendu par le Makefile

L'image installe les dépendances **en système**. Or les cibles du `Makefile`
font `. ../.venv/bin/activate`. L'appelant crée donc, à la racine du dépôt :

```bash
python -m venv --system-site-packages .venv
```

Un venv vide qui **hérite** des paquets de l'image : le contrat du `Makefile`
est satisfait sans rien réinstaller, sans réseau, en une fraction de seconde. Ce
choix évite de modifier le `Makefile`, dont le contrat appartient au lot
précédent.

### Reproduire en local

```bash
ci/lock-python.sh --check                                    # le lock est-il à jour ?
docker build --file ci/Dockerfile.ci --tag ragatm-ci:local ci/  # construire
docker run --rm ragatm-ci:local bash -lc 'python -V; node -v; uv --version'
```

Pour exécuter la suite, PostgreSQL doit vivre sur un **réseau dédié**, joint par
son nom, **sans port publié sur l'hôte** — deux exécutions ne peuvent alors pas
se disputer le port 5432 :

```bash
docker network create ragatm-lot2-net
docker run -d --name ragatm-lot2-pg --network ragatm-lot2-net \
  -e POSTGRES_USER=ragatm -e POSTGRES_PASSWORD=ragatm -e POSTGRES_DB=ragatm \
  pgvector/pgvector:pg16
docker run --rm --network ragatm-lot2-net -e DB_HOST=ragatm-lot2-pg …  ragatm-ci:local \
  bash -c 'python -m venv --system-site-packages .venv && ci/test-backend-standard.sh'
```

> ⚠️ **En local, masquez `.env`.** Un poste de développement en possède un ; un
> checkout de CI n'en a pas. Monté dans le conteneur, il y injecte des réglages
> de poste — par exemple un cache Redis sur un port de loopback qui n'existe pas
> dans le conteneur — et fait échouer des centaines de tests pour une raison qui
> n'a rien à voir avec l'image. Ajoutez `-v /dev/null:/workspace/.env:ro` pour
> reproduire fidèlement un checkout frais. Aucun contenu de `.env` n'est lu.

Nettoyez **par nom**, jamais par `prune` global — le démon Docker peut être
partagé avec d'autres projets.

### Coût de la qualification, et frontière des runners

Le workflow `ci-image.yml` **ne coûte pas 13 secondes**. C'est la durée du seul
build ; le contrôle complet en dure quarante fois plus, parce que l'essentiel
est la ré-exécution des suites **dans** l'image — ce qui est précisément ce qui
la qualifie.

| Étape | Durée |
|---|---:|
| Vérification du lock (`ci/lock-python.sh --check`) | 30 s |
| Contexte borné (`ci/audit-build-context.sh`) | quelques s |
| Construction de l'image | **13 s** |
| Backend dans l'image | **5 min 14** |
| Frontend dans l'image | **2 min 38** |
| Benchmark dans l'image | 17 s |
| **Workflow complet** | **9 min 17** |

C'est ce chiffre-là qui justifie des filtres étroits : le workflow ne part que
si la **définition** de l'environnement change, ou si change un **script qu'il
exécute** — `ci.yml` les exerce déjà, mais sur `ubuntu-latest`, qui porte bien
plus d'outils que cette image minimale ; un script se mettant à dépendre d'un
binaire absent de l'image passerait là-bas et échouerait ici. Un changement
documentaire isolé, lui, ne déclenche rien.

> ⚠️ **`runs-on: ubuntu-latest`, et cela doit le rester pendant cette phase.**
> Ce job exécute du code venant de la **branche ou de la PR**, et il dispose
> d'un **démon Docker**. Sur un runner GitHub hébergé, la machine est jetable et
> n'appartient à personne : le risque est borné. Transposer ce workflow tel quel
> vers un **runner auto-hébergé** sur un hôte sensible donnerait à toute pull
> request l'autorité de ce démon — c'est-à-dire, sur une machine partagée avec
> de la production, une autorité équivalente à root.
>
> Son adaptation à un autre orchestrateur devra donc vivre dans une **VM CI
> isolée**, avec un Docker Engine propre et **sans jamais monter le socket
> Docker de l'hôte**. Cela relève de lots ultérieurs ; **rien de tel n'est créé
> ici**.

### Maintenir

| Quand | Quoi |
|---|---|
| `backend/pyproject.toml` change | avancer `EXCLUDE_NEWER` (`ci/lock-python.sh`) à l'horodatage de ce commit, puis `ci/lock-python.sh`, relire le diff du lock |
| Rafraîchissement mensuel | régénérer le lock, reconstruire l'image, revalider |
| Montée de `uv`, Python ou Node | changer le digest / la version **et** l'empreinte, dans `ci/Dockerfile.ci` |
| Rollback | revenir au lock et aux digests précédents — tous deux versionnés |

## Tests

Le contrat de ces scripts est exercé par
`tests/tests_root/ci/test_ci_scripts_contract.py` — hermétiquement, avec des shims
qui journalisent `argv` : aucune base, aucun réseau, aucune campagne réelle.

## Préparer la suite

Ces scripts sont volontairement **agnostiques** : ils ne lisent aucune variable
propre à un orchestrateur et ne référencent aucun chemin de configuration
d'orchestrateur. Un test le vérifie. Brancher un autre orchestrateur consistera à
écrire de nouveaux adaptateurs qui appellent **exactement les mêmes scripts** — pas
à réécrire la logique.

## Fichiers de données

| Fichier | Rôle |
|---|---|
| `requirements-ci.in` / `requirements-ci.lock` | lock à empreintes de la CI (`lock-python.sh`) |
| `secrets-triage.txt` | faux positifs connus du scan de secrets (`chemin | type | justification`) ; une entrée ne blanchit jamais un vrai secret |
