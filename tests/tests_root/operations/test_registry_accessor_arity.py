"""Les accesseurs du registre n'acceptent JAMAIS de défaut d'appel.

Pourquoi cette garde existe
---------------------------
``setting(name)`` et sa famille ont été volontairement resserrés à un seul
argument : *le défaut vient du registre, il n'y en a qu'un, et un nom inconnu
lève*. Ce resserrement corrigeait un défaut mesuré — vingt-trois noms avaient
l'apparence de réglages, vingt étaient posés en production sans le moindre effet.

Le resserrement a laissé DEUX appelants derrière lui. Résultat : les deux reapers
de résumés ont échoué à chaque exécution pendant 37 jours, en staging comme en
production, sans qu'aucun test ne rougisse.

Ce fichier interdit la récidive sur toute la famille, pas seulement sur les deux
lignes du jour. Il travaille sur l'AST, parce que le motif réel se cache derrière
un ALIAS (``_setting_int = engine.setting_int``) qu'une recherche textuelle par
nom canonique ne voit pas — c'est exactement ce qui a permis au défaut de durer.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[3]
BACKEND = REPO / "backend"

#: Accesseurs du registre. Tous rendent la valeur DÉCLARÉE, sans défaut d'appel.
CANONICAL = frozenset(
    {
        "setting",
        "setting_int",
        "setting_float",
        "setting_bool",
        "get_registered_setting",
    }
)

MESSAGE = (
    "{where} : {call}(...) reçoit {n} arguments.\n"
    "Les accesseurs du registre n'acceptent QUE le nom du réglage : le défaut, le "
    "type et les bornes vivent au registre (backend/config/registry/domains/).\n"
    "Un défaut d'appel est au mieux redondant, au pire faux — il masque une valeur "
    "surchargée par l'exploitant.\n"
    "Correctif : retirer le second argument et, si le réglage n'est pas déclaré, "
    "le déclarer au registre."
)


def invalid_calls(source: str, *, filename: str = "<test>") -> list[tuple[str, str, int]]:
    """Appels d'accesseurs recevant plus d'un argument. Alias locaux RÉSOLUS.

    Fonction pure, exposée pour être testée sur un extrait synthétique : une garde
    qu'on ne peut pas mettre en défaut ne prouve rien.

    Renvoie une liste de ``(nom appelé, nom canonique, ligne)``.
    """
    tree = ast.parse(source, filename=filename)

    # Alias simples : `X = setting_int` ou `X = <module>.setting_int`. C'est la
    # forme EXACTE qui a masqué le défaut (`_setting_int = engine.setting_int`).
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if not isinstance(target, ast.Name):
                continue
            value = node.value
            source_name = (
                value.attr
                if isinstance(value, ast.Attribute)
                else (value.id if isinstance(value, ast.Name) else None)
            )
            if source_name in CANONICAL:
                aliases[target.id] = source_name

    known = CANONICAL | set(aliases)
    found: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.attr
            if isinstance(func, ast.Attribute)
            else (func.id if isinstance(func, ast.Name) else None)
        )
        if name not in known:
            continue
        # `*args`/`**kwargs` comptent : ils peuvent transporter un défaut.
        arity = len(node.args) + len(node.keywords)
        if arity > 1:
            found.append((name, aliases.get(name, name), node.lineno))
    return found


def _python_files() -> list[pathlib.Path]:
    files = [
        p
        for p in BACKEND.rglob("*.py")
        if "__pycache__" not in p.parts and "migrations" not in p.parts
    ]
    assert files, "aucun fichier Python trouvé sous backend/"
    return sorted(files)


# --------------------------------------------------------------------------- #
# L'invariant
# --------------------------------------------------------------------------- #
def test_no_registry_accessor_is_called_with_a_default():
    offenders: list[str] = []
    for path in _python_files():
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:  # pragma: no cover
            continue
        for called, canonical, lineno in invalid_calls(source, filename=str(path)):
            where = f"{path.relative_to(REPO)}:{lineno}"
            label = called if called == canonical else f"{called} [-> {canonical}]"
            offenders.append(MESSAGE.format(where=where, call=label, n="plusieurs"))
    assert not offenders, "\n\n".join(offenders)


# --------------------------------------------------------------------------- #
# La garde elle-même est-elle capable de mettre en défaut ?
#
# Une garde toujours verte est indiscernable d'une garde cassée. Ces cas
# synthétiques prouvent qu'elle détecte réellement le motif — y compris derrière
# l'alias qui a permis au défaut de durer 37 jours.
# --------------------------------------------------------------------------- #
def test_the_guard_detects_a_direct_call_with_a_default():
    found = invalid_calls('setting_int("X", 123)\n')
    assert [(c, n) for c, n, _ in found] == [("setting_int", "setting_int")]


def test_the_guard_detects_a_call_through_an_alias():
    """Le motif RÉEL : `_setting_int = engine.setting_int` puis appel à 2 arguments."""
    source = (
        "from apps.rag_core.services import summary_engine as engine\n"
        "_setting_int = engine.setting_int\n"
        '_setting_int("SUMMARY_PENDING_TIMEOUT_S", 900)\n'
    )
    found = invalid_calls(source)
    assert [(c, n) for c, n, _ in found] == [("_setting_int", "setting_int")]


def test_the_guard_detects_an_attribute_call_with_a_default():
    found = invalid_calls('engine.setting_int("X", 900)\n')
    assert [(c, n) for c, n, _ in found] == [("setting_int", "setting_int")]


def test_the_guard_detects_a_keyword_default():
    """`setting_int("X", default=1)` est tout aussi invalide qu'un positionnel."""
    assert invalid_calls('setting_int("X", default=1)\n')


@pytest.mark.parametrize(
    "source",
    [
        'setting_int("X")\n',
        'setting("X")\n',
        'engine.setting_float("X")\n',
        'get_registered_setting("X")\n',
        '_alias = setting_bool\n_alias("X")\n',
        'other_function("X", 900)\n',          # nom hors famille
        'obj.unrelated("X", 900)\n',
    ],
)
def test_the_guard_accepts_legitimate_calls(source):
    assert invalid_calls(source) == []


def test_the_whole_family_is_covered():
    """Retirer un accesseur de la liste rouvrirait un trou sans que rien ne le dise."""
    assert CANONICAL == {
        "setting",
        "setting_int",
        "setting_float",
        "setting_bool",
        "get_registered_setting",
    }
