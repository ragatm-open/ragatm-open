"""Le repli `settings → os.environ` de `llm_router` ne contourne plus le registre.

Le cas particulier de ce module
-------------------------------
`llm_router` lit plusieurs de ses réglages **directement dans l'environnement**
quand les settings ne les portent pas (`EMBEDDINGS_TIMEOUT_S`, les `DEFAULT_*_MODEL`). Ce repli est utile et il reste.

Ce qu'il permettait
-------------------
`_get_setting(name, default)` acceptait n'importe quel nom. Il suffisait d'en
écrire un dans le code et de le poser dans un `.env` pour obtenir un réglage
**réel mais invisible** : absent du registre, absent de la documentation générée,
non typé, sans bornes, et ignoré par la comparaison de parité staging/production.
C'est la voie de contournement la plus large qui restait après le chantier — la
seule où l'environnement compte sans passer par les settings.

Ce que ce fichier verrouille
----------------------------
1. le repli fonctionne toujours pour un nom DÉCLARÉ ;
2. un nom inconnu LÈVE, au lieu de rendre un défaut ;
3. aucun défaut local ne subsiste dans les appels ;
4. tous les noms lus par le module sont déclarés.
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest
from django.test import override_settings

from config import registry
from config.settings.access import UnregisteredSettingError

LLM_ROUTER = pathlib.Path(__file__).resolve().parents[4] / "backend" / "apps" / "llm_router"

#: Fonctions de lecture du module. Toute nouvelle doit être ajoutée ici — sinon
#: elle échappe au balayage, et la brèche se rouvre par le même chemin.
LECTEURS = ("_get_setting", "_s", "router_setting")


# ===========================================================================
# 1. Le repli sur l'environnement fonctionne encore
# ===========================================================================
def test_a_declared_name_still_falls_back_to_the_environment():
    """C'est la raison d'être du repli : ne pas le casser en le sécurisant."""
    from apps.llm_router.config_access import router_setting

    # EMBEDDINGS_TIMEOUT_S est déclaré mais volontairement NON projeté dans les settings.
    valeur = router_setting("EMBEDDINGS_TIMEOUT_S", environ={"EMBEDDINGS_TIMEOUT_S": "45"})
    assert valeur == 45


def test_settings_win_over_the_environment():
    from apps.llm_router.config_access import router_setting

    with override_settings(EMBEDDINGS_TIMEOUT_S=7):
        assert router_setting("EMBEDDINGS_TIMEOUT_S", environ={"EMBEDDINGS_TIMEOUT_S": "45"}) == 7


def test_an_absent_value_gives_the_registry_default():
    from apps.llm_router.config_access import router_setting

    attendu = registry.require("EMBEDDINGS_TIMEOUT_S").default
    assert router_setting("EMBEDDINGS_TIMEOUT_S", environ={}) == attendu


def test_the_environment_value_is_typed_by_the_registry():
    """Un `.env` ne porte que du texte : le consommateur attend un flottant."""
    from apps.llm_router.config_access import router_setting

    valeur = router_setting("EMBEDDINGS_TIMEOUT_S", environ={"EMBEDDINGS_TIMEOUT_S": "45"})
    assert valeur == 45.0
    assert isinstance(valeur, float)


def test_an_invalid_environment_value_falls_back_to_the_canonical_default():
    from apps.llm_router.config_access import router_setting

    attendu = registry.require("EMBEDDINGS_TIMEOUT_S").default
    assert router_setting("EMBEDDINGS_TIMEOUT_S", environ={"EMBEDDINGS_TIMEOUT_S": "vite"}) == attendu


# ===========================================================================
# 2. Un nom inconnu ne passe plus
# ===========================================================================
def test_an_undeclared_name_is_refused_even_when_posed_in_the_environment():
    """Le cœur du contournement : poser la variable ne suffit plus."""
    from apps.llm_router.config_access import router_setting

    with pytest.raises(UnregisteredSettingError):
        router_setting("ROUTER_SECRET_TUNING_KNOB", environ={"ROUTER_SECRET_TUNING_KNOB": "42"})


def test_the_refusal_explains_the_specific_risk():
    from apps.llm_router.config_access import router_setting

    with pytest.raises(UnregisteredSettingError) as exc:
        router_setting("ROUTER_INEXISTANT")
    message = str(exc.value)
    assert "registre" in message
    assert "environnement" in message


@pytest.mark.parametrize("lecteur", ["_get_setting"])
def test_the_module_level_readers_also_refuse(lecteur):
    from apps.llm_router import router

    with pytest.raises(UnregisteredSettingError):
        getattr(router, lecteur)("ROUTER_INEXISTANT")


def test_the_checks_reader_also_refuses():
    from apps.llm_router import checks

    with pytest.raises(UnregisteredSettingError):
        checks._s("AI_GATEWAY_INEXISTANT")


# ===========================================================================
# 3. Plus aucun défaut local dans les appels
# ===========================================================================
def _appels_avec_defaut() -> list[str]:
    """Appels d'un lecteur avec un second argument — donc un défaut local."""
    coupables: list[str] = []
    motif = re.compile(r"(%s)\(\s*\"[A-Z0-9_]+\"\s*," % "|".join(LECTEURS))
    for chemin in LLM_ROUTER.rglob("*.py"):
        if "__pycache__" in chemin.parts or chemin.name == "config_access.py":
            continue
        for numero, ligne in enumerate(chemin.read_text(encoding="utf-8").splitlines(), 1):
            if motif.search(ligne):
                coupables.append(f"{chemin.name}:{numero}: {ligne.strip()}")
    return coupables


def test_no_call_carries_a_local_default():
    coupables = _appels_avec_defaut()
    assert not coupables, (
        "un défaut local contredit le registre — lequel fait foi ? Le premier "
        "exécuté :\n" + "\n".join(coupables)
    )


# ===========================================================================
# 4. Tous les noms lus sont déclarés
# ===========================================================================
def _noms_lus() -> dict[str, list[str]]:
    trouves: dict[str, list[str]] = {}
    motif = re.compile(r"(?:%s)\(\s*[\"']([A-Z0-9_]+)[\"']" % "|".join(LECTEURS))
    for chemin in LLM_ROUTER.rglob("*.py"):
        if "__pycache__" in chemin.parts:
            continue
        for nom in motif.findall(chemin.read_text(encoding="utf-8")):
            trouves.setdefault(nom, []).append(chemin.name)
    return trouves


def test_the_scan_sees_the_real_calls():
    noms = _noms_lus()
    assert len(noms) >= 15, "le balayage doit voir les lectures réelles du module"
    assert "EMBEDDINGS_TIMEOUT_S" in noms


def test_every_name_read_by_the_router_is_declared():
    inconnus = {n: sorted(set(f)) for n, f in _noms_lus().items() if registry.get(n) is None}
    assert not inconnus, (
        "lus par `llm_router` avec repli environnement, mais absents du registre — "
        "ils seraient configurables SANS être documentés ni comparés :\n"
        + "\n".join(f"  {n:<36} {', '.join(f)}" for n, f in sorted(inconnus.items()))
    )


# ===========================================================================
# 5. Aucun lecteur permissif ne subsiste dans le module
# ===========================================================================
def test_no_permissive_reader_remains():
    """`getattr(settings, name, défaut)` suivi d'un repli `os.environ` — le motif
    exact qui rendait le registre contournable. Il ne doit plus exister ici."""
    coupables = []
    for chemin in LLM_ROUTER.rglob("*.py"):
        if "__pycache__" in chemin.parts or chemin.name == "config_access.py":
            continue
        source = chemin.read_text(encoding="utf-8")
        arbre = ast.parse(source, filename=str(chemin))
        for noeud in ast.walk(arbre):
            if not isinstance(noeud, ast.Call):
                continue
            fonc = noeud.func
            est_environ = (
                isinstance(fonc, ast.Attribute)
                and fonc.attr in ("get",)
                and isinstance(fonc.value, ast.Attribute)
                and fonc.value.attr == "environ"
            ) or (isinstance(fonc, ast.Attribute) and fonc.attr == "getenv")
            if est_environ:
                coupables.append(f"{chemin.name}:{noeud.lineno}")
    assert not coupables, (
        "lecture directe de l'environnement dans `llm_router` — elle échappe au "
        f"registre : {coupables}. Passez par `config_access.router_setting`."
    )
