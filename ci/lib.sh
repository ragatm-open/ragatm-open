#!/usr/bin/env bash
# ci/lib.sh — fonctions communes aux scripts CI portables de RagATM.
#
# À SOURCER, jamais à exécuter :
#     source "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"
#
# Périmètre volontairement étroit — cinq besoins réels, pas un framework :
#   1. résolution de la racine du dépôt depuis l'emplacement du fichier ;
#   2. affichage cohérent des étapes ;
#   3. validation des commandes requises ;
#   4. validation des variables NON SENSIBLES attendues ;
#   5. création contrôlée d'un répertoire d'artefacts.
#
# Ce fichier ne connaît NI GitHub, NI Forgejo, NI aucun autre orchestrateur : il ne
# lit aucune variable `GITHUB_*`, ne référence aucun chemin `.github`, ne charge
# jamais un `.env` et n'affiche JAMAIS la valeur d'une variable — seulement son nom.

# Racine du dépôt, déduite de l'emplacement de CE fichier. Les scripts fonctionnent
# donc depuis n'importe quel répertoire courant, y compris via un lien symbolique
# résolu par `cd -P`.
CI_LIB_DIR="$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CI_REPO_ROOT="$(cd -P -- "${CI_LIB_DIR}/.." && pwd)"
export CI_REPO_ROOT

# Code de sortie des erreurs de PRÉREQUIS ou d'USAGE.
#
# 64 (EX_USAGE) et non 1 ou 2 : `benchmark-retrieval.sh` réserve 0, 1 et 2 à la
# sémantique métier de la comparaison (conforme, régression, incompatible). Un
# prérequis manquant qui sortirait en 2 serait indiscernable d'un benchmark
# incompatible — l'opérateur diagnostiquerait le mauvais problème.
readonly CI_EXIT_USAGE=64

# --- Affichage ---------------------------------------------------------------
# Pas de couleur : ces sorties sont lues dans des journaux d'orchestrateur.

ci_step() {
    printf '\n── %s\n' "$*"
}

ci_info() {
    printf '   %s\n' "$*"
}

# Erreur explicite puis arrêt. Le message part sur stderr et ne contient jamais
# autre chose que ce que l'appelant lui passe : aucun secret n'y transite.
ci_die() {
    printf 'ERREUR: %s\n' "$*" >&2
    exit "${CI_EXIT_USAGE}"
}

# --- Validations -------------------------------------------------------------

# ci_require_cmd <commande>... — échoue si l'une manque, en les listant toutes.
ci_require_cmd() {
    local missing=() c
    for c in "$@"; do
        command -v -- "$c" >/dev/null 2>&1 || missing+=("$c")
    done
    if [ "${#missing[@]}" -ne 0 ]; then
        ci_die "commande(s) requise(s) absente(s) : ${missing[*]}"
    fi
}

# ci_require_env <NOM>... — échoue si l'une est absente ou vide.
#
# Seuls les NOMS sont affichés. Cette fonction ne doit être utilisée que pour des
# variables non sensibles (hôte, port, chemin, base) ; un mot de passe absent se
# manifestera par l'échec de la commande métier, pas par un message d'ici.
ci_require_env() {
    local missing=() v
    for v in "$@"; do
        if [ -z "${!v:-}" ]; then
            missing+=("$v")
        fi
    done
    if [ "${#missing[@]}" -ne 0 ]; then
        ci_die "variable(s) d'environnement requise(s) non définie(s) : ${missing[*]}"
    fi
}

# --- Interpréteur Python -----------------------------------------------------

# ci_python — chemin de l'interpréteur à utiliser, dans cet ordre :
#   1. $RAGATM_PYTHON      échappatoire explicite (tests, environnement inhabituel) ;
#   2. <racine>/.venv      le venv du dépôt, celui que le Makefile active déjà ;
#   3. $VIRTUAL_ENV        un venv activé par l'appelant ;
#   4. python3, puis python.
#
# L'ordre place le venv du dépôt AVANT $VIRTUAL_ENV pour rester cohérent avec le
# Makefile, qui fait `. ../.venv/bin/activate` sans condition. Deux points d'entrée
# qui choisiraient des interpréteurs différents seraient une source d'écarts
# impossibles à diagnostiquer.
ci_python() {
    if [ -n "${RAGATM_PYTHON:-}" ]; then
        printf '%s\n' "${RAGATM_PYTHON}"
        return 0
    fi
    if [ -x "${CI_REPO_ROOT}/.venv/bin/python" ]; then
        printf '%s\n' "${CI_REPO_ROOT}/.venv/bin/python"
        return 0
    fi
    if [ -n "${VIRTUAL_ENV:-}" ] && [ -x "${VIRTUAL_ENV}/bin/python" ]; then
        printf '%s\n' "${VIRTUAL_ENV}/bin/python"
        return 0
    fi
    local c
    for c in python3 python; do
        if command -v -- "$c" >/dev/null 2>&1; then
            printf '%s\n' "$c"
            return 0
        fi
    done
    ci_die "aucun interpréteur Python : ni RAGATM_PYTHON, ni .venv, ni VIRTUAL_ENV, ni python3/python."
}

# --- Artefacts ---------------------------------------------------------------

# ci_artifacts_dir <chemin> — crée le répertoire si besoin et rend son chemin.
# Refuse un chemin vide plutôt que de créer quoi que ce soit à la racine.
ci_artifacts_dir() {
    local dir="${1:-}"
    [ -n "$dir" ] || ci_die "ci_artifacts_dir : chemin vide."
    mkdir -p -- "$dir" || ci_die "répertoire d'artefacts impossible à créer : ${dir}"
    printf '%s\n' "$dir"
}

# Garde : ce fichier n'a pas de comportement propre.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    printf 'ERREUR: ci/lib.sh doit être SOURCÉ, pas exécuté.\n' >&2
    exit 64
fi
