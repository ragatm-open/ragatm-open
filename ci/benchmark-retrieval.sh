#!/usr/bin/env bash
# ci/benchmark-retrieval.sh — garde de non-régression du MOTEUR DE RETRIEVAL.
#
#     ci/benchmark-retrieval.sh
#
# SOURCE DE VÉRITÉ du contrat d'exécution du benchmark retrieval. Le YAML qui
# l'appelle n'est qu'un adaptateur : il ne doit plus porter aucun de ces choix.
#
# Déroulé (identique à celui que `rag-benchmark.yml` exécutait en cinq étapes) :
#   1. manage.py check
#   2. manage.py migrate --noinput
#   3. manage.py seed_demo_benchmark --reset
#   4. manage.py rag_benchmark      run CANDIDAT, validation ACTIVE, --fail-under
#   5. manage.py rag_benchmark_compare   candidat vs baseline committée
#
# DÉTERMINISME — invariants non négociables :
#   - `--embeddings-provider stub` : profil déterministe (seed = sha256 du texte),
#     donc AUCUN appel réseau, AUCUN provider d'embeddings, AUCUN LLM ;
#   - `--skip-validation` n'est JAMAIS passé : la validation documentaire
#     (existence document/chunk, réponse présente dans le chunk) reste active ;
#   - `--with-answers` n'est JAMAIS passé : il déclencherait une génération LLM ;
#   - `--fail-under` est TOUJOURS passé : pas de PASS silencieux si un seuil est
#     violé ou si le run comporte des erreurs / références obsolètes ;
#   - `--decision-k 5` est FIGÉ dans ce script, sans variable d'environnement pour
#     l'assouplir : un seuil de décision qu'on peut baisser depuis l'appelant n'est
#     plus un seuil. (Même valeur que `compare.DEFAULT_DECISION_K`.)
#
# CODES DE SORTIE — la sémantique métier de la comparaison, propagée telle quelle :
#   0   aucune régression bloquante
#   1   régression / erreur introduite
#   2   rapports incompatibles, benchmark obsolète, ou entrée invalide
#  64   prérequis d'exécution manquant (ce script, pas le benchmark)
#
# Un échec des étapes 1 à 4 sort avec le code de la commande concernée et la
# comparaison n'a pas lieu — exactement comme aujourd'hui.
#
# ARTEFACTS — écrits sur disque, jamais dans un résumé propre à un orchestrateur.
# Le résumé de job d'un orchestrateur n'est pas connu de ce script ; c'est à
# l'adaptateur, côté YAML, de recopier le Markdown s'il veut le publier.
#
# Variables (toutes optionnelles, valeurs par défaut = comportement actuel) :
#   BASELINE                  benchmarks/baseline/demo-fr.json    (relatif à backend/)
#   DATASET                   benchmarks/datasets/demo-fr.jsonl   (relatif à backend/)
#   RAGATM_CI_ARTIFACTS_DIR   /tmp   répertoire des artefacts produits
#
# Prérequis : dépendances backend installées, PostgreSQL joignable via DB_*.
# Ce script ne charge AUCUN `.env` et n'affiche aucune valeur de variable.
set -Eeuo pipefail

source "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"

BACKEND_DIR="${CI_REPO_ROOT}/backend"
[ -f "${BACKEND_DIR}/manage.py" ] || ci_die "manage.py introuvable sous ${BACKEND_DIR}"

# Seuil de décision : constante, jamais un réglage.
readonly DECISION_K=5

# Chemins d'entrée, résolus depuis backend/ (comme le faisait le workflow).
BASELINE="${BASELINE:-benchmarks/baseline/demo-fr.json}"
DATASET="${DATASET:-benchmarks/datasets/demo-fr.jsonl}"

# L'absence de la baseline ou du dataset n'est PAS traitée ici : ces cas ont déjà
# une sémantique métier (code 2 pour une baseline introuvable). Un contrôle
# préalable la remplacerait par 64 et ferait diagnostiquer le mauvais problème.

ARTIFACTS_DIR="$(ci_artifacts_dir "${RAGATM_CI_ARTIFACTS_DIR:-/tmp}")"
RAW_OUT="$(ci_artifacts_dir "${ARTIFACTS_DIR}/bench")"
CANDIDATE="${ARTIFACTS_DIR}/demo-fr-candidate.json"
COMPARISON_JSON="${ARTIFACTS_DIR}/demo-fr-comparison.json"
COMPARISON_MD="${ARTIFACTS_DIR}/demo-fr-comparison.md"

PYTHON="$(ci_python)"
cd -- "${BACKEND_DIR}"

ci_step "Django check"
"${PYTHON}" manage.py check

ci_step "Migrations (base réelle, requise par les commandes de gestion)"
"${PYTHON}" manage.py migrate --noinput

ci_step "Amorçage du corpus demo-fr"
"${PYTHON}" manage.py seed_demo_benchmark --reset

ci_step "Benchmark candidat (retrieval seul, validation ACTIVE, --fail-under)"
"${PYTHON}" manage.py rag_benchmark \
    --dataset "${DATASET}" \
    --embeddings-provider stub \
    --format json \
    --out "${RAW_OUT}" \
    --canonical-out "${CANDIDATE}" \
    --fail-under

ci_step "Comparaison candidat vs baseline committée (decision-k=${DECISION_K})"
rc=0
"${PYTHON}" manage.py rag_benchmark_compare \
    "${BASELINE}" \
    "${CANDIDATE}" \
    --decision-k "${DECISION_K}" \
    --output "${COMPARISON_JSON}" \
    --markdown "${COMPARISON_MD}" || rc=$?

ci_info "artefacts : ${CANDIDATE}"
ci_info "artefacts : ${COMPARISON_JSON}"
ci_info "artefacts : ${COMPARISON_MD}"

# Le code de la comparaison EST le code de ce script. Aucune neutralisation.
exit "${rc}"
