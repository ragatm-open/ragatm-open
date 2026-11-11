# ======================================
# ragATM – Makefile
# ======================================

.PHONY: help openapi-bundle test test-backend test-backend-all \
        test-backend-root test-backend-sequential test-backend-heavy \
        test-backend-external test-backend-full \
        test-backend-compile test-frontend \
        test-fast test-standard test-full \
        hooks check-docs config-audit config-reference check-config

help:
	@echo ""
	@echo "Available targets:"
	@echo "  openapi-bundle      Bundle OpenAPI spec into shared/contracts/openapi.yaml"
	@echo "  test                Run backend + frontend test suites"
	@echo "  test-fast           profil FAST : backend sans base ni service (--no-db, < 1 min)"
	@echo "  test-standard       profil STANDARD : suite backend standard (PostgreSQL + Redis)"
	@echo "  test-full           profil FULL : suite backend complète, aucun filtre"
	@echo "  test-backend        Alias of test-backend-all (la suite backend STANDARD)"
	@echo "  test-backend-all    compileall + suite STANDARD (parallèle, ~2 min)"
	@echo "  test-backend-root   suite STANDARD seule (PYTEST_WORKERS=$(PYTEST_WORKERS) par défaut)"
	@echo "  test-backend-sequential  suite STANDARD sur UN worker (diagnostic, ~8 min)"
	@echo "  test-backend-heavy  campagne LOURDE tabulaire (-m 'tabular_volume', ~21 min)"
	@echo "  test-backend-external    tests à service RÉEL (-m external, opt-in explicite)"
	@echo "  test-backend-full   suite COMPLÈTE, aucun filtre (= test-full)"
	@echo "  test-frontend       Run frontend vitest suite (tests/frontend)"
	@echo "  hooks               Active le hook git pre-commit (.githooks) — vérif doc à chaque commit"
	@echo "  check-docs          Vérifie la documentation (liens cassés / orphelins)"
	@echo "  config-audit        Audite le registre de configuration vs code et .env.example"
	@echo "  config-reference    Régénère docs/10-reference/configuration.md"
	@echo "  check-config        Audit + vérification que la référence générée est à jour"
	@echo ""

# --------------------------------------
# Tests
# --------------------------------------
# Un seul arbre de tests backend : `tests/tests_root` (la convergence a supprimé
# `backend/tests_legacy`). Il s'exécute depuis backend/, avec la config pytest
# unique du repo root (`pyproject.toml`, section [tool.pytest.ini_options]).
#
# TROIS voies automatiques ou manuelles, séparées par des MARQUEURS :
#
#   - STANDARD (`test-backend-root`) : `-m "not tabular_volume and not external"`
#     — 9 310 tests. C'est la voie de la CI bloquante.
#   - LOURDE   (`test-backend-heavy`) : `-m "tabular_volume"` — 49 tests, ~21 min.
#   - EXTERNE  (`test-backend-external`) : `-m external` — 15 tests, opt-in.
#   - COMPLÈTE (`test-backend-full`)  : aucun filtre, les 9 374.
#
# Pourquoi `tabular_volume` est écarté — c'est mesuré, pas supposé : les 49 tests
# coûtent ~21 min sur les ~29 min de la suite complète d'alors (72 %), dont ~16 min
# pour les seuls 12 paramétrages de `test_concurrence_sans_fuite_en_charge`
# (1 → 10 threads).
#
# Pourquoi `external` est écarté — c'est un problème de DÉTERMINISME, pas de durée :
# ces tests dépendent d'un service ou d'une donnée hors dépôt (AI Gateway, Ollama,
# provider d'embeddings réel, corpus `.msg` non versionné). Leurs sondes de
# disponibilité s'évaluaient à la COLLECTE : la suite standard émettait deux
# requêtes réseau avant le premier test, et exécutait pour de bon les tests
# concernés dès que ces services répondaient sur la machine. Le résultat dépendait
# donc du poste. Voir backend/testing/external.py.
#
# Rien n'est perdu de vue : la voie COMPLÈTE reste la référence de qualification
# exhaustive, et les campagnes LOURDE et EXTERNE tournent par un mécanisme
# explicite (cf. docs/08-testing/strategie.md). Le moteur tabulaire reste par
# ailleurs couvert dans la suite standard par `test_tabular_engine.py` (les 13
# formes, en millisecondes) et `test_tabular_limits_enforcement.py`.

# --- Parallélisme -----------------------------------------------------------
# Un SEUL endroit décide du nombre de workers. Défaut : 2, en local comme en CI
# (où le workflow le pose aussi, runners à deux cœurs).
#   PYTEST_WORKERS=1 make test-backend   -> exécution séquentielle (diagnostic)
# Le défaut local était 4 ; il passe à 2 après un OOM global du poste de développement
# (11/09/2026) : chaque worker xdist est un processus complet qui refait toute la
# collecte, la mémoire se multiplie donc par le nombre de workers. 4 ne sera de
# nouveau présenté comme sûr qu'après une mesure dédiée. Une valeur passée
# explicitement n'est PAS plafonnée ici (décision séparée).
# `--dist worksteal` rééquilibre en cours de route : sans lui, un worker tombé sur
# les fichiers `http_api` lents finit bien après les autres.
PYTEST_WORKERS ?= 2

# PYTEST_WORKERS=1 retire `-n` ENTIÈREMENT plutôt que de poser `-n 1` : la voie de
# diagnostic doit s'exécuter dans le processus pytest lui-même, sans la couche
# xdist. C'est ce qui distingue « un seul worker » de « pas de parallélisme », et
# c'est la seule façon d'obtenir une trace d'erreur non relayée.
PYTEST_XDIST_ARGS := $(if $(filter-out 1,$(PYTEST_WORKERS)),-n $(PYTEST_WORKERS) --dist worksteal,)

# Sélection de la voie standard, définie une fois.
PYTEST_STANDARD_MARKERS := not tabular_volume and not external and not slow

# Profil FAST : mêmes exclusions que la voie standard, plus `tabular_concurrency`
# (campagne de concurrence) et surtout `--no-db` (cf. tests/tests_root/conftest.py) :
# tout test demandant PostgreSQL est DÉSÉLECTIONNÉ, le profil tourne sans service.
PYTEST_FAST_MARKERS := not slow and not tabular_volume and not tabular_concurrency and not external

# Rapport JUnit : produit UNIQUEMENT si PYTEST_JUNIT_PATH est défini. La CI le
# pose ; une exécution locale reste silencieuse et n'écrit rien — le développeur
# n'a pas à subir un artefact qu'il n'a pas demandé.
# Le chemin est résolu depuis le REPO ROOT (les recettes font `cd backend`).
PYTEST_JUNIT_ARGS := $(if $(PYTEST_JUNIT_PATH),--junitxml=$(CURDIR)/$(PYTEST_JUNIT_PATH),)
PYTEST_JUNIT_MKDIR := $(if $(PYTEST_JUNIT_PATH),mkdir -p $(CURDIR)/$(dir $(PYTEST_JUNIT_PATH)) &&,)

test: test-backend test-frontend

# --- Profils explicites : FAST / STANDARD / FULL ----------------------
# FAST     : sans base de données ni service — boucle de développement et
#            première porte de CI (`ci/test-backend-fast.sh`).
# STANDARD : la voie bloquante historique (`test-backend-all`), PostgreSQL + Redis.
# FULL     : aucun filtre de marqueur (`test-backend-full`) ; `external` reste
#            collecté mais sauté sans RAG_RUN_EXTERNAL_TESTS=1.
# Un test n'est JAMAIS retiré d'un profil pour le verdir : un test qui échoue se
# corrige ou se documente ; seul un marqueur justifié dans le module le déplace.
test-fast: test-backend-compile
	$(PYTEST_JUNIT_MKDIR) cd backend && . ../.venv/bin/activate && \
		EMBEDDINGS_PROVIDER=stub python -m pytest -q \
			$(PYTEST_XDIST_ARGS) \
			--no-db -m "$(PYTEST_FAST_MARKERS)" \
			$(PYTEST_JUNIT_ARGS) \
			../tests/tests_root

test-standard: test-backend-all

test-full: test-backend-full

# `make test-backend` = la suite backend STANDARD (rapide).
test-backend: test-backend-all

test-backend-all: test-backend-compile test-backend-root

test-backend-compile:
	cd backend && . ../.venv/bin/activate && python -m compileall -q apps ../tests/tests_root

# Suite STANDARD — celle de la CI bloquante.
test-backend-root:
	$(PYTEST_JUNIT_MKDIR) cd backend && . ../.venv/bin/activate && \
		EMBEDDINGS_PROVIDER=stub python -m pytest -q \
			$(PYTEST_XDIST_ARGS) \
			-m "$(PYTEST_STANDARD_MARKERS)" \
			$(PYTEST_JUNIT_ARGS) \
			../tests/tests_root

# Même SÉLECTION que la voie standard, sur un seul worker. Sert à diagnostiquer un
# échec vu en parallèle, à comparer les durées et à obtenir une trace lisible.
# Ce n'est PAS la voie CI : elle coûte environ quatre fois plus cher.
test-backend-sequential:
	$(MAKE) test-backend-root PYTEST_WORKERS=1

# Campagne LOURDE — qualification volumétrique du moteur tabulaire.
# Les tailles 100k/500k/1M restent en plus derrière TABULAR_VOLUME_FULL=1 : ce
# garde-fou évite qu'un appel distrait ne lance plusieurs heures de calcul.
test-backend-heavy:
	cd backend && . ../.venv/bin/activate && EMBEDDINGS_PROVIDER=stub python -m pytest -q -m "tabular_volume" ../tests/tests_root

# Tests à SERVICE RÉEL — jamais automatiques, jamais dans `make test-backend`.
# Prérequis, selon les tests visés :
#   - AI Gateway joignable (cf. AI_GATEWAY_BASE_URL) ;
#   - Ollama local avec le modèle `nomic-embed-text` ;
#   - un provider d'embeddings réel + RAG_BENCH_REAL_EMBEDDINGS=1 ;
#   - un corpus `.msg` fourni via RAGATM_MSG_CORPUS.
# Chaque test saute proprement si SON service est absent : la cible ne suppose pas
# que tous soient disponibles. Aucun secret n'est écrit dans la sortie.
test-backend-external:
	cd backend && . ../.venv/bin/activate && \
		RAG_RUN_EXTERNAL_TESTS=1 python -m pytest -q -m "external" ../tests/tests_root

# Suite COMPLÈTE — aucun filtre de marqueur. Voie de qualification exhaustive.
# Les tests `external` y sont COLLECTÉS mais restent sautés sans
# RAG_RUN_EXTERNAL_TESTS=1 : la voie complète ne devient pas une porte dérobée
# vers le réseau.
test-backend-full: test-backend-compile
	$(PYTEST_JUNIT_MKDIR) cd backend && . ../.venv/bin/activate && \
		EMBEDDINGS_PROVIDER=stub python -m pytest -q \
			$(PYTEST_XDIST_ARGS) \
			$(PYTEST_JUNIT_ARGS) \
			../tests/tests_root

test-frontend:
	cd frontend && npm test

# --------------------------------------
# OpenAPI
# --------------------------------------

openapi-bundle:
	npx swagger-cli@4.0.4 bundle shared/contracts/openapi/root.yaml --type yaml --outfile shared/contracts/openapi.yaml

# --------------------------------------
# Documentation (policy : doc à jour à chaque commit)
# --------------------------------------

# Active le hook git pre-commit versionné (.githooks/). À lancer une fois par clone.
hooks:
	git config core.hooksPath .githooks
	@echo "Hook git activé (core.hooksPath=.githooks) — la doc est vérifiée à chaque commit."

check-docs:
	python3 scripts/check_docs.py

# --------------------------------------
# Configuration (registre canonique)
# --------------------------------------

# Cohérence registre <-> code <-> .env.example. Code de sortie non nul sur erreur.
config-audit:
	cd backend && . ../.venv/bin/activate && python manage.py config_audit

# Régénère docs/10-reference/configuration.md depuis le registre.
config-reference:
	cd backend && . ../.venv/bin/activate && python manage.py generate_config_reference

# Garde CI : échoue si la référence générée n'est plus à jour.
check-config: config-audit
	cd backend && . ../.venv/bin/activate && python manage.py generate_config_reference --check
