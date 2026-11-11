"""Lecture de configuration du routeur LLM — settings PUIS environnement.

Pourquoi ce module existe séparément de ``config.settings.access``
------------------------------------------------------------------
Le reste de RagATM lit ses réglages dans les settings Django. ``llm_router`` a une
particularité assumée : plusieurs de ses réglages (``EMBEDDINGS_TIMEOUT_S``,
les ``DEFAULT_*_MODEL``)
sont lus **directement dans l'environnement** quand les settings ne les portent
pas. Ce repli existe depuis l'origine et il est utile : il permet de changer un
modèle d'embeddings sans toucher aux settings.

Le problème qu'il posait
------------------------
``_get_setting(name, default)`` acceptait **n'importe quel nom**. Le registre
canonique pouvait donc être contourné sans effort : il suffisait d'écrire un nom
dans le code et de le poser dans un ``.env`` pour obtenir un réglage réel,
invisible du registre, absent de la documentation, non typé, sans bornes — et
impossible à comparer entre staging et production, puisque l'inventaire de
configuration ne le connaissait pas.

Ce que ce module change
-----------------------
Le repli sur l'environnement est CONSERVÉ, mais il ne s'applique qu'aux noms
**déclarés au registre**. Un nom inconnu lève, comme partout ailleurs. Le défaut
vient du registre : plus de second défaut local qui contredirait la documentation.
"""
from __future__ import annotations

import os
from typing import Any

from django.conf import settings

from config import parsers, registry
from config.settings.access import UnregisteredSettingError

__all__ = ["router_setting", "UnregisteredSettingError"]

_MESSAGE = (
    "{name} n'est pas déclarée dans le registre de configuration.\n"
    "`llm_router` lit ses réglages dans les settings PUIS dans l'environnement : "
    "un nom non déclaré produirait donc un réglage RÉEL mais invisible — absent "
    "de la documentation générée, non typé, sans bornes, et ignoré par la "
    "comparaison de parité staging/production.\n"
    "Déclarez-le dans `backend/config/registry/domains/` avec "
    "`source=\"backend/apps/llm_router/…\"`, ou lisez-le autrement."
)


def router_setting(name: str, *, environ: dict[str, str] | None = None) -> Any:
    """Valeur d'un réglage DÉCLARÉ : settings, puis environnement, puis registre.

    L'ordre reproduit exactement le comportement historique de ``_get_setting`` ;
    seules deux choses changent : le nom doit être déclaré, et le défaut est
    celui du registre.
    """
    definition = registry.get(name)
    if definition is None:
        raise UnregisteredSettingError(_MESSAGE.format(name=name))

    if hasattr(settings, name):
        return getattr(settings, name)

    env = os.environ if environ is None else environ
    raw = env.get(name)
    if raw is None or str(raw).strip() == "":
        return definition.default

    try:
        return definition.parse(raw)
    except parsers.ConfigValueError:
        # Une valeur d'environnement invalide ne doit pas empêcher le routeur de
        # servir : elle est ignorée au profit du défaut canonique. L'anomalie est
        # signalée séparément par le system check `config.E001` quand la variable
        # passe par la projection des settings.
        return definition.default
