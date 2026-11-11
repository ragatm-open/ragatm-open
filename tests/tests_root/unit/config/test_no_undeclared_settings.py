"""Aucun réglage ne se lit sans être déclaré.

Ce test est le filet que le system check `config.E010` ne peut pas tendre.

`config.E010` part du REGISTRE : il vérifie que ce qui est déclaré est projeté.
Il ne voit donc pas le défaut symétrique — un nom lu par le code et déclaré
**nulle part**. Celui-là ne produit ni erreur ni avertissement : `getattr` rend
le défaut d'appel, et la variable a l'air de fonctionner.

Cas réels trouvés par ce balayage, tous silencieux jusque-là :

- `RAG_CHUNK_SIZE_CHARS` — la taille de fragment, qui conditionne TOUT le
  retrieval en aval, n'était pas configurable ;
- `CHAT_MEMORY_ENABLED` — la mémoire de conversation ne pouvait pas être coupée ;
- `RAG_ASYNC_INGEST` — un repli « rétro-compatible » qui n'a jamais pu s'exécuter ;
- `BACKUP_MAX_AGE_SECONDS`, `PREVIEW_CACHE_ENABLED`, `RAGATM_VERSION`.

L'analyse est syntaxique et volontairement simple : elle ne couvre que les noms
LITTÉRAUX. Les lectures à nom calculé (`getattr(settings, name, default)` où
`name` est une variable) lui échappent par construction — c'est précisément
pourquoi l'API `get_registered_setting` refuse les noms inconnus **à l'exécution**.
Les deux mécanismes sont complémentaires : l'un empêche d'écrire le défaut,
l'autre l'attrape s'il est écrit autrement.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from config import registry

BACKEND = pathlib.Path(__file__).resolve().parents[4] / "backend"

#: Arbres balayés. `config` a été ajouté après un angle mort constaté : le jeton
#: `RAG_OPS_METRICS_TOKEN` de l'endpoint `/ops/metrics` est lu depuis
#: `config/urls.py` et échappait donc entièrement à ce filet — un réglage
#: d'exploitation, secret de surcroît, hors gouvernance sans que rien ne le dise.
#: L'extension est étroite et mesurée : sur `config`, elle voyait 15 lectures et
#: un seul nom non déclaré, celui-là précisément. Pas de balayage du dépôt entier :
#: un contrôle bruyant finit désactivé.
SCANNED = ("apps", "api", "config")

#: Sous-arbres exclus du balayage, quel que soit l'arbre parent.
#: - `settings` DÉFINIT les réglages au lieu de les consommer : y lire `settings.X`
#:   pendant la construction des settings n'est pas une lecture applicative ;
#: - `migrations`, `__pycache__`, `tests` : code généré, figé ou de test.
SKIPPED_PARTS = frozenset({"__pycache__", "migrations", "settings", "tests"})

#: Attributs de `settings` qui ne sont PAS des réglages RagATM : Django les
#: définit lui-même, et le registre n'a pas à les décrire.
DJANGO_BUILTINS = frozenset({
    "BASE_DIR", "SECRET_KEY", "SETTINGS_MODULE", "ALLOWED_HOSTS", "DEBUG",
    "MEDIA_ROOT", "MEDIA_URL", "STATIC_ROOT", "STATIC_URL", "TIME_ZONE",
    "CELERY_BEAT_SCHEDULE", "DATABASES", "CACHES", "INSTALLED_APPS",
    "REST_FRAMEWORK", "AUTH_PASSWORD_VALIDATORS", "LANGUAGE_CODE",
    "AUTH_USER_MODEL", "DEFAULT_AUTO_FIELD", "USE_TZ", "ROOT_URLCONF",
})

#: Noms lus mais délibérément NON déclarés, avec leur raison. Toute entrée est
#: une dette : elle décrit un réglage qui devrait être au registre et ne l'est
#: pas encore. Une allowlist vide serait l'objectif ; une allowlist sans
#: justification serait un contournement.
ALLOWLIST: dict[str, str] = {
    # Constante de version de prompt : le registre la déclare, mais le module la
    # lit aussi comme repli local quand les settings ne sont pas chargés.
    "SUMMARY_PROMPT_VERSION": "déclarée au registre ; lue en repli hors Django",
}


def _literal_setting_names() -> dict[str, list[str]]:
    """Noms LITTÉRAUX passés à `getattr(settings, "X", …)` ou lus en `settings.X`."""
    trouves: dict[str, list[str]] = {}

    for racine in SCANNED:
        for chemin in (BACKEND / racine).rglob("*.py"):
            if SKIPPED_PARTS.intersection(chemin.parts):
                continue
            try:
                arbre = ast.parse(chemin.read_text(encoding="utf-8"), filename=str(chemin))
            except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
                continue
            relatif = chemin.relative_to(BACKEND).as_posix()

            for noeud in ast.walk(arbre):
                nom = _name_from_getattr(noeud) or _name_from_attribute(noeud)
                if nom:
                    trouves.setdefault(nom, []).append(relatif)
    return trouves


def _name_from_getattr(noeud: ast.AST) -> str | None:
    """`getattr(settings, "NOM", …)` — le nom doit être une chaîne littérale."""
    if not isinstance(noeud, ast.Call):
        return None
    if not (isinstance(noeud.func, ast.Name) and noeud.func.id == "getattr"):
        return None
    if len(noeud.args) < 2:
        return None
    cible, nom = noeud.args[0], noeud.args[1]
    if not (isinstance(cible, ast.Name) and cible.id == "settings"):
        return None
    if isinstance(nom, ast.Constant) and isinstance(nom.value, str):
        return nom.value if nom.value.isupper() else None
    return None


def _name_from_attribute(noeud: ast.AST) -> str | None:
    """`settings.NOM` — accès direct."""
    if not isinstance(noeud, ast.Attribute):
        return None
    if not (isinstance(noeud.value, ast.Name) and noeud.value.id == "settings"):
        return None
    return noeud.attr if noeud.attr.isupper() and "_" in noeud.attr + "_" else None


@pytest.fixture(scope="module")
def noms_lus() -> dict[str, list[str]]:
    return _literal_setting_names()


def test_the_scan_actually_finds_something(noms_lus):
    """Un balayage qui ne trouve rien passerait toujours — et ne protégerait rien."""
    assert len(noms_lus) > 50, (
        "le balayage doit voir la majorité des lectures de settings ; s'il n'en "
        "trouve presque aucune, c'est le balayage qui est cassé, pas le code"
    )
    assert "RAG_TOP_K" in noms_lus, "témoin : un réglage central doit être vu"


def test_every_literal_setting_read_is_declared(noms_lus):
    inconnus = {
        nom: sorted(set(fichiers))
        for nom, fichiers in noms_lus.items()
        if registry.get(nom) is None
        and nom not in DJANGO_BUILTINS
        and nom not in ALLOWLIST
    }
    assert not inconnus, (
        "réglages lus par le code mais déclarés NULLE PART — `getattr` rend le "
        "défaut d'appel, la variable a l'air de fonctionner et ne fonctionne pas :\n"
        + "\n".join(f"  {nom:<44} {', '.join(f)}" for nom, f in sorted(inconnus.items()))
        + "\n\nDéclarez-les (ConfigDefinition + projection dans les settings), ou "
        "faites-en des constantes nommées du module si ce ne sont pas des réglages."
    )


def test_the_allowlist_stays_justified():
    """Une exemption sans raison finit par couvrir autre chose qu'elle ne couvrait."""
    assert all(str(raison).strip() for raison in ALLOWLIST.values())


def test_the_allowlist_does_not_cover_already_declared_names():
    """Une entrée devenue inutile masquerait une régression future du même nom."""
    inutiles = [nom for nom in ALLOWLIST if registry.get(nom) is not None and "repli" not in ALLOWLIST[nom]]
    assert not inutiles, f"entrées d'allowlist devenues inutiles : {inutiles}"
