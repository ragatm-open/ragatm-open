# Commandes utiles

Aide-mémoire des commandes du dépôt : Makefile, `manage.py`, scripts d'environnement, qualité,
benchmarks. **Source unique** : les autres docs pointent ici. Public : développeurs et ops.

Sauf indication, les commandes backend s'exécutent depuis `backend/` avec le venv activé
(`source ../.venv/bin/activate`). Ports dev : voir [ports.md](ports.md).

---

## Makefile (racine du dépôt)

| Cible | Effet |
|---|---|
| `make test` | Backend **standard** + frontend. |
| `make test-backend` / `make test-backend-all` | `compileall` + suite backend **standard** (`-m "not tabular_volume and not external"`, 9 310 tests, **2 workers** par défaut). |
| `make test-backend-root` | La suite **standard** seule (`EMBEDDINGS_PROVIDER=stub`). |
| `make test-backend-sequential` | **Même sélection**, sans xdist (un seul processus). Voie de **diagnostic**. |
| `make test-backend-heavy` | Campagne **lourde** tabulaire (`-m "tabular_volume"`, 49 tests, ~21 min). |
| `make test-backend-external` | Tests à **service réel** (`-m external`, 15 tests). Pose `RAG_RUN_EXTERNAL_TESTS=1`. **Jamais automatique.** |
| `make test-backend-full` | Suite **complète**, aucun filtre (9 374 tests ; les `external` y restent sautés sans `RAG_RUN_EXTERNAL_TESTS=1`). |
| `make test-backend-compile` | `python -m compileall` (garde syntaxique). |
| `make test-frontend` | `cd frontend && npm test` (vitest). |
| `make benchmark DATASET=…` | Lance le benchmark RAG sur un dataset. |
| `make openapi-bundle` | `swagger-cli bundle shared/contracts/openapi/root.yaml`. |
| `make check-docs` | Liens Markdown cassés / références de fichiers inexistantes. |
| `make config-audit` | Audit du registre de configuration ↔ code ↔ `.env.example`. |
| `make config-reference` | Régénère [configuration.md](configuration.md) depuis le registre. |
| `make check-config` | `config-audit` + vérification que la référence générée est à jour. |

> **Les cibles backend forment une partition exacte** : `standard` (9 310) +
> `heavy` (49) + `external` (15) = `full` (9 374), sans recouvrement ni perte.
>
> Seule la **standard est automatique** (push + PR via `ci.yml`). Les voies **lourde**,
> **externe** et **complète** sont **manuelles uniquement** : la lourde via le workflow
> `tests-heavy.yml` (`workflow_dispatch` seul, entrée `full_volume` pour les tailles
> 100k/500k/1M), les deux autres en local. Lancez une campagne lourde **avant toute
> modification sensible du moteur tabulaire ou avant une mise en production critique**.
> Voir [../08-testing/strategie.md](../08-testing/strategie.md).

### Variables de la voie backend

| Variable | Défaut | Effet |
|---|---|---|
| `PYTEST_WORKERS` | **`2`** (local et CI) | Nombre de workers xdist. **`1` retire `-n` entièrement** : exécution dans le processus pytest, sans la couche xdist. Ancien défaut local : 4, abaissé après un OOM du poste de développement ; une valeur explicite n'est pas plafonnée, mais **`-n` > 2 est déconseillé sur un poste partagé** hors qualification autorisée (voir [../08-testing/strategie.md](../08-testing/strategie.md#sécurité-de-la-collecte-et-du-parallélisme-local--implémenté)). |
| `PYTEST_JUNIT_PATH` | *(vide)* | Chemin du rapport JUnit, résolu depuis le repo root. Vide = **aucun fichier écrit** ; la CI pose `artifacts/pytest-standard.xml`. |
| `RAG_RUN_EXTERNAL_TESTS` | *(vide)* | `1` active la famille `external`. Posée par `make test-backend-external`, **jamais** par la CI. |

```bash
# Diagnostic d'un échec vu en parallèle : même sélection, une seule trace
PYTEST_WORKERS=1 make test-backend
# Reproduire exactement la CI en local (cf. ci/test-backend-standard.sh ci-dessous)
PYTEST_WORKERS=2 PYTEST_JUNIT_PATH=artifacts/pytest-standard.xml make test-backend-root
```

> **pytest direct : ciblé uniquement.** La suite se lance par le Makefile. `python -m pytest`
> sans chemin, depuis la racine, ne collecte plus que `tests/tests_root` (`testpaths`), et
> `backend/media` est exclu de toute découverte (`norecursedirs`) — mais une campagne large
> hors Makefile reste interdite. En débogage : fichier ou node id explicite, sans xdist ou
> `-n 2` maximum.

---

## Scripts CI portables (`ci/`)

**Source de vérité des commandes de CI.** Les workflows GitHub sont des adaptateurs
minces : ils préparent l'environnement puis appellent ces scripts. Les lancer en
local exécute donc **exactement** ce que fait la CI — sans relire un YAML.

| Script | Effet | Service requis |
|---|---|---|
| `ci/check-docs.sh` | Cohérence de `docs/` (délègue à `scripts/check_docs.py`). Options transmises telles quelles. | aucun |
| `ci/check-config.sh` | `manage.py check` + `config_audit` + `generate_config_reference --check`. Ne régénère **jamais** la référence. | PostgreSQL |
| `ci/test-backend-standard.sh` | Suite backend **standard** (délègue à `make test-backend-all`). | PostgreSQL |
| `ci/test-frontend.sh` | vitest + `tsc --noEmit` + `next build`. **N'installe rien** : `npm ci` reste à la charge de l'appelant. | aucun |
| `ci/benchmark-retrieval.sh` | Benchmark retrieval candidat + comparaison vs baseline. | PostgreSQL |
| `ci/test-tabular-heavy.sh` | Campagne lourde tabulaire. **Manuelle.** `--dry-run` affiche le périmètre sans rien lancer. | PostgreSQL |
| `ci/lock-python.sh` | Régénère `ci/requirements-ci.lock` ; `--check` détecte sa dérive vis-à-vis de `backend/pyproject.toml`. | Docker |

Codes de sortie : `0` succès · `1` échec métier · `2` benchmark **incompatible** (ou
`docs/` introuvable) · **`64` prérequis ou usage invalide** — jamais un verdict métier.

```bash
ci/check-docs.sh                        # ~12 s, aucun service
ci/check-config.sh                      # gardes de configuration
ci/test-backend-standard.sh             # suite standard
ci/test-frontend.sh                     # après `npm ci` dans frontend/
ci/benchmark-retrieval.sh; echo "code=$?"   # 0 conforme | 1 régression | 2 incompatible
ci/test-tabular-heavy.sh --dry-run      # périmètre seulement
```

Les scripts fonctionnent depuis n'importe quel répertoire courant et ne chargent
**aucun** `.env`. Variables, valeurs par défaut et frontière avec l'orchestrateur :
[`ci/README.md`](../../ci/README.md).

### Environnement reproductible

```bash
ci/lock-python.sh                                             # régénère le lock
ci/lock-python.sh --check                                     # dérive ? code 1
docker build --file ci/Dockerfile.ci --tag ragatm-ci:local ci/ # construit l'image
```

Le lock fige les dépendances tierces **avec leurs empreintes** ; l'image épingle
sa base par digest, `uv` par version et digest, Node.js par SHA-256. L'extra
`rerank` (torch) en est exclu. **Rien n'est publié** : l'image est éphémère et
son digest n'est pas promu.

---

## Qualité / lint / typecheck

```bash
# Backend (manuel, non appliqué par ci.yml)
cd backend && source ../.venv/bin/activate
ruff check .                 # ou: ruff check --fix .
black backend/               # ou: black --check backend/
python manage.py check       # system checks Django (dont llm_router.E001-E005, rag_core.E001)
python -m compileall -q apps ../tests/tests_root

# Frontend
cd frontend
npm run lint                 # next lint (ESLint)
npx tsc --noEmit             # typecheck (ce que fait la CI)
npm run build                # next build
```

> ⚠️ `ci.yml` n'exécute **ni ruff, ni black, ni mypy, ni `manage.py check`** côté Python
> (seul `make test-backend-all` + vitest/tsc). `manage.py check` ne tourne que dans les
> workflows de benchmark. Aucun mypy dans le dépôt. Voir [limites-connues.md](limites-connues.md).

---

## `manage.py` — commandes RagATM

### Amorçage / administration

| Commande | Effet |
|---|---|
| `python manage.py migrate` | Applique les migrations (pgvector inclus). |
| `python manage.py check` | System checks (policy Gateway, dimension embeddings, conformité au registre de configuration…). |
| `python manage.py config_audit [--format json] [--strict] [--no-code-scan]` | Audite le registre de configuration : variables lues par le code mais non déclarées, clés inconnues ou dépréciées dans les `.env.example`, valeurs invalides, doublons, secrets réalistes versionnés, variables requises manquantes. **Ne lit jamais un `.env` réel.** Code de sortie non nul dès une erreur (`--strict` : les avertissements aussi). Voir [configuration.md](configuration.md). |
| `python manage.py generate_config_reference [--check] [--stdout] [--output <f>]` | (Re)génère [configuration.md](configuration.md) depuis le registre. Génération **déterministe** ; `--check` échoue si le fichier versionné est périmé (garde CI). |
| `python manage.py dump_effective_config [--label <l>] [--output <f>]` | Instantané JSON de la configuration **effective** d'un environnement (les valeurs que les processus utilisent, pas celles qu'un `.env` annonce). Les paramètres déclarés `secret` n'y figurent que par leur **présence** (`<posé>` / `<absent>`), jamais par leur valeur. Voir parite-staging-production.md. |
| `python manage.py check_config_parity --reference <f> --candidate <f> [--manifest <f>] [--json <f>]` | Compare deux instantanés selon `deploy/config-parity.yml` : divergences **interdites** (code de sortie 1), **autorisées** (nommées, avec leur raison) et **rapportées**. Travaille sur des fichiers — jamais sur un environnement distant. |
| `python manage.py createsuperuser` | Crée le **propriétaire global RagATM** (`is_superuser`). Interactif ; **échoue** si le compte existe. |
| `python manage.py ensure_superuser [--update-password]` | **Idempotent** — amorçage du superuser en déploiement, piloté par `DJANGO_SUPERUSER_USERNAME` / `_EMAIL` / `_PASSWORD` / `_UPDATE_PASSWORD`. Crée le compte s'il est absent, **garantit** `is_staff`/`is_superuser`/`is_active` s'il existe, met à jour l'email, et ne réécrit le mot de passe **que** sur opt-in explicite (valeur exacte `1`). Applique les validateurs de mot de passe Django et refuse les valeurs d'exemple. N'affiche **jamais** le secret, y compris dans ses messages d'erreur. Sortie : `Superuser created.` / `Superuser already exists; privileges verified.` / `Superuser updated.` |
| `python manage.py bootstrap_workspace` | Crée un workspace + membership owner (amorçage). |
| `python manage.py gateway_endpoints_preflight [--probe] [--expect-route pool\|direct]` | **Lecture seule.** Pour les trois URL Gateway (`AI_GATEWAY_BASE_URL`, `AI_GATEWAY_EMBEDDINGS_BASE_URL`, `RAG_RERANKER_BASE_URL`) vues par CE processus : valeur, acheminement (`direct`/`pool`), consommateurs, override en base de `AI_GATEWAY_BASE_URL`. `--probe` : `GET /health/runtime` et `/pool/health/ready` seulement (jamais `/health/ready`). Code 0 = `PASS`, 1 = `FAIL`. À lancer dans chaque conteneur. |
| `python manage.py asr_drain_preflight [--json] [--verbose]` | **Lecture seule** (transaction `READ ONLY`). Jobs ASR en vol (`submitted`), en cours de dépôt, à venir, inexpliqués, depuis `AudioTranscript`. `ASR_DRAIN_STATUS=PASS` (code 0) / `BLOCKED` (2, attendre) / `UNKNOWN` (3, enquêter). N'annule, ne supprime, ne relance rien. Exigé avant tout changement d'acheminement Gateway. |

### RAG / embeddings

| Commande | Effet |
|---|---|
| `python manage.py reembed_documents --rag-space <uuid> --sync` | Réindexe un RagSpace vers son provider/modèle courant (après changement de dimension/provider). **Pré-vol** : compte les sources réellement présentes et REFUSE de démarrer si l'une manque — lancer N réindexations vouées à l'échec ne produit que N documents `FAILED`. `--continue-with-missing` les IGNORE explicitement (jamais réindexés, jamais réétiquetés). Un `--dry-run` les signale sans rien refuser. |
| `python manage.py reap_stale_summaries` | Passe en `FAILED` les résumés `PENDING` bloqués (équivalent de la tâche beat). |
| `python manage.py set_corpus_embedding_model [--corpus-slug <slug> --model <m> [--provider <p>] [--confirm]]` | Sans argument : inventaire de la provenance déclarée des vecteurs de chaque corpus métier. Avec `--corpus-slug`/`--model` : **prouve** que les vecteurs stockés viennent bien de ce couple (cosinus ≥ 0,999 sur 3 chunks réels) puis, avec `--confirm`, l'écrit. Sans preuve, elle refuse. Aucune réindexation. Voir [../03-backend/corpus-metier.md](../03-backend/corpus-metier.md). |
| `python manage.py backfill_corpus_embedding_fingerprints [--corpus <slug>] [--inventory\|--dry-run\|--apply] [--sample-size 3]` | Renseigne l'**empreinte d'indexation** des documents corpus (`embedding_provider/model/dimension/fingerprint`, `reindex_required`) **sur preuve uniquement** : réencodage de 3 chunks réels et cosinus ≥ 0,999. Trois verdicts — `proven_consistent` (empreinte écrite), `proven_inconsistent` et `unknown` (marqués `reindex_required`, aucune empreinte inventée). `--inventory` n'exécute aucune mesure ; rien n'est écrit sans `--apply`. Ne modifie aucun embedding, ne supprime aucun chunk, ne touche aucune calibration. Voir [../03-backend/corpus-metier.md](../03-backend/corpus-metier.md). |
| `python manage.py calibrate_business_corpus_admission --corpus <slug> --dataset <f.json> [--probability-threshold 0.65] [--vector-top-k 40] [--output <rapport.json>] [--apply]` | Entraîne et valide la calibration d'**admission** d'un corpus métier (signal `corpus_vector_top10_mean`, calibrateur logistique). Trois schémas de validation croisée — stratifié, par document dominant, une famille retirée — toutes métriques **hors pli**. **N'écrit rien sans `--apply`** et **refuse** de persister sous les critères minimaux (ROC-AUC ≥ 0,90 · PR-AUC ≥ 0,90 · rappel ≥ 0,80 · spécificité ≥ 0,80 · exactitude équilibrée ≥ 0,80 · Brier ≤ 0,15 · ≥ 20 positifs · ≥ 20 négatifs · ≥ 3 familles négatives). Aucune génération LLM ; ne persiste ni question ni texte. Voir [../03-backend/pipeline-retrieval.md](../03-backend/pipeline-retrieval.md). |

### Benchmarks RAG

Voir ../08-testing/benchmark-rag.md pour le détail des profils.

| Commande | Effet |
|---|---|
| `python manage.py seed_demo_benchmark --reset` | Amorce le RagSpace demo-fr (4 docs / 12 chunks). |
| `python manage.py seed_demo_benchmark_extended --reset --embed-provider ollama --embed-model nomic-embed-text` | Amorce demo-fr-extended (41 docs). |
| `python manage.py build_extended_dataset [--check]` | (Re)génère le JSONL étendu depuis le code (`--check` en CI). |
| `python manage.py rag_benchmark --dataset <f.jsonl> --embeddings-provider <stub\|ollama> …` | Exécute le benchmark (options : `--profile`, `--canonical-out`, `--thresholds`, `--fail-under`, `--answerability-strategy`, `--with-answers`). |
| `python manage.py rag_benchmark_compare <baseline.json> <candidate.json> --decision-k 5` | Compare candidat vs baseline (aligne par `case_id`). Exit 1 = régression bloquante, 2 = incompatible. |
| `python manage.py mission_generate_xlsx_candidates --rag-space <uuid> --manifest <manifest.json> --out <candidates.jsonl>` | Génère un lot **candidat** de cas XLSX (vérité terrain structurelle, tous `human_validation_status=pending`). Options : `--target-count` (40), `--min-cases` (**30**), `--min-documents` (10), `--min-sheets` (15), `--seed` (1789), `--review-csv`, `--dry-run`, `--overwrite`. Exit 1 = cas acceptés < `--min-cases` (aucun artefact écrit), 2 = corpus dérivé du manifeste. Sous le volume de référence, un avertissement **PILOTE** est émis. Voir ../08-testing/benchmark-excel-methodology.md. |
| `python manage.py mission_inventory_xlsx_aggregation --rag-space <uuid> --manifest <manifest.json> --report <inventory.json>` | **Passe A** : inventaire exhaustif du potentiel `tabular_aggregation`. Scan de toutes les tables **sans plafond** et **sans matérialisation** — les cas sont calculés puis jetés, seuls des compteurs survivent. Ne produit **ni JSONL officiel, ni cas gelé, ni split, ni holdout-state** ; le rapport est non sensible donc versionnable. Options : `--max-tables` (0 = exhaustif, diagnostic), `--progress-every`. Exit 2 = corpus dérivé. Voir ../08-testing/benchmark-excel-tabular-aggregation.md. |
| `python manage.py mission_generate_xlsx_aggregation_candidates --rag-space <uuid> --manifest <manifest.json> --out <candidates.jsonl>` | Génère un lot **candidat** de cas Excel `tabular_aggregation` (opérations calculées). Vérité terrain **calculée depuis les octets du classeur** par l'exécuteur de référence — jamais depuis les chunks, jamais par un LLM. Options : `--seed` (1789), `--min-cases` (**30**), `--max-tables` (0 = toutes), `--review-csv`, `--dry-run`, `--overwrite`. Exit 1 = cas insuffisants, 2 = corpus dérivé. Aucun retrieval, aucun holdout. Voir ../08-testing/benchmark-excel-tabular-aggregation.md. |
| `python manage.py mission_select_xlsx_aggregation_pilot --rag-space <uuid> --manifest <manifest.json> --out <pilot-candidates.jsonl>` | **Passe B** : sélection **déterministe sous contraintes** du lot pilote `tabular_aggregation` (~80 cas de revue). Pilotée par les quotas, jamais par l'ordre du pool ; les plafonds de concentration (2/document, 1/feuille, 1/table, 2/valeur attendue) ne sont **jamais relâchés** pour atteindre un quota secondaire, et les quotas manqués sont **rapportés** au lieu d'être compensés. Produit `pilot-candidates.jsonl`, `pilot-selection-report.json` et `pilot-review.csv` — **ni split, ni holdout, ni métrique moteur, ni dataset gelé**. Options : `--target` (**80**), `--max-tables` (0 = exhaustif ; une sélection bornée n'est **jamais** recevable), `--progress-every`, `--dry-run`, `--overwrite`. Exit 1 = hors critères de recevabilité, 2 = corpus dérivé. Sorties gitignorées (valeurs de cellules réelles). Voir ../08-testing/benchmark-excel-tabular-aggregation.md. |
| `python manage.py mission_build_excel_v2_dataset --rag-space <uuid> --corpus-manifest <manifest.json> --accepted-cases <candidates.jsonl> --out-dir <dir>` | Construit le dataset Excel **v2** officiel (migration schéma v2, split documentaire, init du holdout `used=false`). Options : `--seed` (1789), `--calibration-ratio` (0.70), `--created-at`. Écriture **atomique** ; aucun retrieval, aucun holdout exécuté. Voir ../08-testing/benchmark-excel-v2-dataset-and-holdout.md. |
| `python manage.py build_admission_dataset --rag-space <uuid> --output <ds.jsonl>` | Génère le dataset d'évaluation de l'**admission** : positifs dérivés de chunks réels (le chunk source **est** la vérité terrain) + négatifs par catégorie, questions FR/EN produites par LLM **via le client Gateway commun**. Échantillonnage **ordonné par `id` avant le mélange** — sans ordre SQL explicite la graine ne garantit rien. L'en-tête porte **deux empreintes** : `selection_fingerprint` (identifiants tirés) et `content_fingerprint` (identifiant + sha256 du texte), pour distinguer un tirage qui change d'un corpus modifié en place sous les mêmes identifiants. Modèle résolu depuis `RagSpace.llm_model` puis `AI_GATEWAY_MODEL` ; `--gen-model` n'est qu'un override (`model_source` : `cli` / `ragspace` / `registry`). `--min-negative-score` ne s'applique qu'aux **7** catégories de négatifs *proches* : `out_of_corpus` en est **exempté** (politique `requires_absence_review`), son absence devant être démontrée par `verify_negative_absence` puis par revue humaine. Chaque négatif mesuré porte `best_retrieval_score`, `best_retrieved_document_id`, `best_retrieved_chunk_id` et son statut de filtrage ; l'en-tête porte un bilan par catégorie (scores min/médiane/p95/max, rejets). Options : `--target-count` (total, réparti sur les 4 directions) **ou** `--per-direction` (22), `--per-negative-category` (11), `--positive-only`, `--min-per-direction` (défaut = volume demandé), `--min-negative-score`, `--negative-oversample` (4), `--seed` (20260801), `--exclude-document`, `--dry-run`, `--overwrite`. Codes de sortie : 1 = volume non atteint (**rien n'est écrit**), 2 = corpus inexploitable (familles de pipeline mêlées, passages trop rares), 3 = échec Gateway, 4 = erreur d'usage ou de configuration, 5 = sortie déjà existante, 6 = découpage non étanche. Une panne Gateway **n'est jamais avalée**. Voir ../08-testing/benchmark-admission-dataset.md. |
| `python manage.py review_admission_dataset --dataset <ds.jsonl> --export <revue.csv>` puis `--apply <revue.csv> --reviewer <nom>` | Revue **humaine** du dataset d'admission. Verdicts : `valid`, `valid_with_correction` (exige `corrected_question`), `ambiguous` (gardé, **hors métriques**), `relabel` (**ré-étiquette sans supprimer** : change le label, ou la catégorie d'un négatif à label constant), `reject`. `--reviewer` obligatoire avec `--apply` ; chaque champ modifié est tracé (relecteur, date, ancienne et nouvelle valeur). Une revue partiellement invalide **n'applique rien**, et un `case_id` annoté deux fois est refusé. Un réétiquetage qui rompt l'étanchéité du découpage est **refusé avant toute écriture** (source inchangée). Options : `--split`, `--output`, `--overwrite`. Codes de sortie : 1 = aucun verdict renseigné, 2 = revue invalide, 3 = découpage non étanche, 4 = erreur d'usage. Voir ../08-testing/benchmark-admission-dataset.md. |
| `python manage.py calibrate_retrieval_admission --rag-space <uuid> --dataset <ds.jsonl> --output-dir <dir> [--final-test]` | Rejoue le dataset d'admission contre le **retrieval réel** et balaie une grille de seuils. La grille ne tourne que sur le jeu `calibration` ; le `holdout` n'est évalué qu'une fois, par `--final-test`. `--final-test` évalue **un seul jeu de seuils par appel** (il n'y a pas de mode comparatif) : comparer deux politiques demande deux invocations, dont les paramètres doivent être **figés avant exécution** — sans quoi le holdout devient un second jeu de calibration. **Gardes sur le dataset, AVANT toute écriture** : refuse un dataset `deprecated: true` (**exit 2**), un découpage non étanche (3), des cas non relus parmi les cas **actifs et mesurables** (4 — les cas `excluded_from_metrics` ne bloquent jamais), l'absence du jeu calibration ou holdout (5) ; une panne de la recherche lexicale (**7** — `legacy` décide sur le `ts_rank` seul, sans lui la comparaison entre politiques est invalide) ; erreur d'usage = 6. Un refus ne crée **aucun fichier, pas même le répertoire de sortie**. Les cas `excluded_from_metrics` (verdict `ambiguous`) sont retirés des calculs. `--allow-deprecated-dataset` et `--allow-unvalidated` forcent l'exécution : DIAGNOSTIC uniquement, l'usage est inscrit dans l'artefact (`provenance.is_reference_measurement: false`). Chaque artefact porte la provenance : chemin, version, **sha256**, dépréciation, effectifs, étanchéité. Voir ../08-testing/benchmark-admission-dataset.md. |
| `python manage.py verify_negative_absence --rag-space <uuid> --questions <q.tsv> --output <preuves.csv> [--dataset <ds.jsonl>]` | Cherche dans le corpus une réponse à chaque question **négative** et rend les preuves pour la revue humaine. **Ne pose aucun verdict** : `verdict` et `reviewer_note` restent vides. Rend les **k plus proches** avec, pour chacun, score, document, `document_id`, `chunk_id` et extrait borné (240 car.) — établir une absence exige de voir ce qui s'en rapproche le plus. `--dataset` reporte le score mesuré **à la génération** (`best_retrieval_score`) et le statut de filtrage à côté de la mesure refaite ici : sans les deux, une divergence resterait invisible. Le modèle juge est inscrit dans le fichier. Options : `--top-k` (5), `--judge-model`, `--exclude-document`. Lecture seule en base. |
| `python manage.py mission_holdout_run --dataset <holdout.jsonl> --state <state.json> --out <local.json>` | **Consomme définitivement** un holdout : exécution unique, non rejouable. **Dry-run par défaut** (aucun cas lu). Consomme seulement avec `--confirm-consume-mission-holdout=P2.6-ONE-SHOT`. Refuse un état déjà consommé, un dataset hors holdout, ou une configuration non conforme au gel (exit 2). L'état est écrit **avant** le premier cas : un crash laisse le holdout consommé — c'est voulu. Résultat brut **local** (gitignoré). Voir ../08-testing/benchmark-rag.md. |

### Benchmark modèles LLM

Voir ../08-testing/benchmark-modeles-llm.md.

| Commande | Effet |
|---|---|
| `python manage.py benchmark_llm_models …` | Compare plusieurs modèles LLM sur des corpus de fixtures. |
| `python manage.py bench_action_classifier` / `warmup_action_classifier` | Bench / préchauffage du classifieur d'actions. |
| `python manage.py reembed_preflight --rag-space <uuid> [--provider P] [--model M] [--details] [--json f]` | Chiffre un ré-embedding **sans rien écrire** : documents éligibles / déjà à l'identité / inéligibles par motif, fragments à recalculer, plus long fragment, fragments hors limite, appels estimés, répartition des identités. La garantie est **structurelle** — la commande ne connaît pas la primitive d'écriture, et un test analyse sa syntaxe pour l'interdire. N'appelle pas la Gateway. Avertit si l'espace ne déclare pas encore l'identité visée : les documents migrés seraient alors **invisibles** au retrieval jusqu'à la bascule de l'espace. la documentation d'exploitation (hors périmètre de cette édition). |
| `python manage.py check_embedding_capabilities [--base-url URL] [--json f]` | Confronte les plafonds d'entrée **déclarés** (`GOVERNED_MODEL_MAX_INPUT_CHARS`) au registre publié par la Gateway (`/health/embeddings`), et affiche en regard le plafond **utile** — celui qui reste au texte une fois déduit le préfixe interne de la Gateway (17 car. en `document`, 14 en `query` pour `legacy-768`). Les deux ne se confondent pas : c'est le publié qu'on compare, c'est l'utile qui borne le découpage. Lecture seule. Sortie 1 en cas d'écart : le découpage reposerait alors sur une valeur fausse, et la panne serait **silencieuse et différée** — le découpage passe, l'indexation échoue des semaines plus tard. Signale aussi, sans bloquer, les modèles gouvernés servis ici mais non déclarés : ils recevraient la borne conservatrice sans qu'aucune mesure ne dise si elle est nécessaire. la documentation d'exploitation (hors périmètre de cette édition). |
| `python manage.py check_gateway_quiet [--base-url URL] [--samples N] [--json f]` | Répond à la question qui précède toute opération lourde : la Gateway est-elle libre ? Lecture seule. Croise l'occupation **déclarée** (`/health/runtime` — `inflight`/`queued`, autoritaire mais aveugle à l'instance voisine) et l'occupation **subie** (latence d'une sonde, seul signal qui traverse le GPU partagé). Sortie 1 si la Gateway travaille, ou si rien ne permet de conclure. la documentation d'exploitation (hors périmètre de cette édition). |
| `python manage.py bench_rerank_latency [--appels 200] [--pool N] [--concurrence N] [--json f]` | Mesure la distribution de latence de l'appel de reranking Gateway — **lecture seule**, et **sans incrémenter les compteurs d'exploitation** (elle passe par `_post_rerank`, pas `rerank_chunks`). Utilise des **questions réelles** de l'environnement et des **pools réels** : la longueur des fragments décide du coût, un texte synthétique mesurerait la machine et non le service. `--concurrence` est essentiel — la Gateway **sérialise**, et le p95 passe de 147 ms à un flux à 261 ms à deux, franchissant la porte de 250 ms. Ne jamais lancer contre staging pendant les heures de service : les deux Gateway partagent le calcul et la **production** en souffre (×2,5). la documentation d'exploitation (hors périmètre de cette édition). |

### Audit / coûts / feedback

| Commande | Effet |
|---|---|
| `python manage.py llm_costs [--workspace-id … --days …]` | Agrège coûts et tokens (`RagQueryLog.llm_cost_estimated`). |
| `python manage.py purge_expired_audit_logs` | Purge de rétention (dry-run par défaut). la documentation d'exploitation (hors périmètre de cette édition). |
| `python manage.py analyze_feedback` | Analyse des feedbacks utilisateurs sur les réponses. |
| `python manage.py media_refresh_manifest --emit <fichier> [--environment <nom>] [--role source\|destination] [--checksums]` | Instantané vérifiable d'un environnement (documents + médias, par domaine). Ne copie rien, ne supprime rien. Aucun nom de fichier dans la sortie. la documentation d'exploitation (hors périmètre de cette édition). |
| `python manage.py media_refresh_manifest --verify --source <f> --destination <f>` | Verdict `COMPLETE` / `INCOMPLETE` d'un rafraîchissement. **Échec fermé** : code de sortie non nul dès qu'un fichier ou un document manque en destination. |
| `python manage.py audit_media_storage [--json] [--fail-on-anomaly]` | Audit **non destructif** du stockage documentaire (référencés / références sans fichier / orphelins). Ne supprime rien. `--fail-on-anomaly` : code de sortie non nul pour la supervision. Planifiable via `RAG_MEDIA_STORAGE_AUDIT_ENABLED` — la documentation d'exploitation (hors périmètre de cette édition). |
| `python manage.py monitor_ragspace_deletions [--json] [--fail-on-alert]` | Liste les suppressions **définitives** de RagSpace en **échec** sur une fenêtre (défaut 24 h). **Lecture seule** : n'écrit ni ne modifie aucun audit. Planifiable via `RAG_RAGSPACE_DELETE_MONITORING_ENABLED`. |
| `python manage.py visual_v2_prepare --version-id <uuid> [--dry-run [--check-gateway] \| --reprocess --reason … \| --resume] [--reason …] [--actor-email …]` | Préparation **explicite** Vision V2 d'une version (jamais automatique) : portes de déploiement **et** politique en base `enabled` (CV-3E4A). Trace un `VisualPrepareRun` (`operator`) commité avant l'envoi ; préparation `done` = no-op ; `pending`/`running` = reprise au curseur ; `failed`/`cancelled` = refus (utiliser `--reprocess`, `--reason` obligatoire, nouvelle génération). `--resume` : ré-émet préparations ouvertes et fenêtre de jobs. Voir [../06-ai/ai-gateway.md](../06-ai/ai-gateway.md). |
| `python manage.py visual_v2_scope --space <uuid> [--enable \| --disable] [--mode explicit\|auto] --reason … [--actor-email …]` · `--disable-all --reason …` · `--space <uuid> --show` | Politique Vision V2 d'un RagSpace (CV-3E4A) : ligne absente = fermé ; ne peut que restreindre les portes de déploiement. `--disable` = révocation immédiate sans redémarrage. Idempotente, `ActionAuditEvent` à chaque appel (acteur opérateur ou `visual_v2_operator`), refusée en production. |
| `python manage.py create_visual_validation_identity --workspace-id <id>` | Identité de service staging `svc-visual-validation@example.invalid` (mot de passe inutilisable, groupe `ragatm-service-accounts`), lectrice du seul workspace synthétique désigné. Idempotente ; refusée en production et sur tout workspace non synthétique. |
| `python manage.py purge_soft_deleted_ragspaces` | **Inventaire** des RagSpaces historiques soft-deleted — **dry-run par défaut, ne supprime rien**. Purge ciblée : `--execute --confirm PURGE-SOFT-DELETED-RAGSPACES` + un filtre. la documentation d'exploitation (hors périmètre de cette édition). |

---

## Scripts d'environnement

### Développement (`scripts/dev/`)

| Script | Effet |
|---|---|
| `scripts/dev/up.sh` | Démarre la stack dev (Postgres conteneurisé + backend/front en processus locaux, loopback). |
| `scripts/dev/up_rebuild.sh` | Idem avec reconstruction. |
| `scripts/dev/bootstrap.sh` | venv + deps (`[dev]`,`[ocr]`), `npm install`, migrations, user/workspace/ragspace de test. |
| `scripts/dev/reset_db.sh` | Réinitialise la base dev. |
| `scripts/dev/stop_dev.sh` | Arrête la stack dev. |
| `scripts/dev/set_admin.sh` | Promeut un utilisateur en superuser. |

## Références d'implémentation

- `Makefile`
- `backend/apps/rag_core/management/commands/`, `backend/apps/audit/management/commands/`, `backend/apps/workspaces/management/commands/`
- `scripts/dev/`, `scripts/prod/`, `scripts/staging/`
- `pyproject.toml` (repo root : config pytest) et `backend/pyproject.toml` (ruff/black), `frontend/package.json`
