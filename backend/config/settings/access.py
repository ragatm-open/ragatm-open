"""Lecture d'un réglage : par le registre, ou pas du tout.

Le problème que ce module ferme
-------------------------------
Le code lisait ses réglages ainsi ::

    def _s(name, default):
        return getattr(settings, name, default)

    top_k = _s("RAG_INVENTORY_TOP_K", 12)

Trois défauts, tous silencieux :

1. **Le nom n'a pas à exister.** Une faute de frappe rend le défaut, sans un mot.
2. **La variable a l'air configurable et ne l'est pas.** ``getattr`` interroge les
   settings Django ; si aucun module de settings ne projette le nom, poser
   ``RAG_INVENTORY_TOP_K=40`` dans un ``.env`` n'a AUCUN effet. Mesuré en
   production : vingt variables dans ce cas.
3. **Le défaut est écrit deux fois.** Ici ``12``, au registre ``10`` : lequel fait
   foi ? Le premier qui s'exécute. Personne ne le sait en lisant le code.

Le contrat
----------
``get_registered_setting("RAG_INVENTORY_TOP_K")`` :

- **refuse** un nom absent du registre — ``ImproperlyConfigured``, immédiatement,
  en développement comme en test comme en production ;
- rend la valeur **typée** par le registre (bool, int, float, liste…) ;
- prend le défaut **du registre**, jamais un second défaut local ;
- lit les settings s'ils portent le nom, sinon retombe sur le défaut canonique.

Il n'y a donc plus qu'un seul endroit où un réglage est décrit, et un seul défaut.
"""
from __future__ import annotations

from typing import Any

from django.core.exceptions import ImproperlyConfigured

from config import parsers, registry

__all__ = ["get_registered_setting", "UnregisteredSettingError"]


class UnregisteredSettingError(ImproperlyConfigured):
    """Nom de réglage inconnu du registre canonique."""


_MESSAGE = (
    "{name} n'est pas déclarée dans le registre de configuration "
    "(`backend/config/registry/domains/`).\n"
    "Un réglage non déclaré n'a ni type, ni bornes, ni documentation, ni "
    "défaut opposable — et rien ne garantit qu'il soit projeté dans les "
    "settings : le poser dans un `.env` peut n'avoir aucun effet.\n"
    "Déclarez-le (ConfigDefinition) si c'est un réglage d'exploitation, ou "
    "faites-en une constante nommée du module si c'est un invariant interne."
)


def get_registered_setting(name: str, *, settings_module: Any = None) -> Any:
    """Valeur effective d'un réglage DÉCLARÉ, typée par le registre.

    ``settings_module`` n'existe que pour les tests (injection d'un faux objet
    settings) ; en production il vaut ``django.conf.settings``.
    """
    definition = registry.get(name)
    if definition is None:
        raise UnregisteredSettingError(_MESSAGE.format(name=name))

    if settings_module is None:  # import tardif : ce module est chargé très tôt
        from django.conf import settings as settings_module  # noqa: PLC0415

    sentinel = object()
    raw = getattr(settings_module, name, sentinel)
    if raw is sentinel:
        # Le registre connaît le nom mais aucun settings ne le projette. Ce n'est
        # pas une erreur de lecture — c'est une anomalie de câblage, signalée par
        # le system check `config.E010`. Ici on rend le défaut CANONIQUE, jamais un
        # défaut local : la valeur reste celle que la documentation annonce.
        return definition.default

    # La valeur portée par les settings est déjà typée (elle vient de `env_value`).
    # Une valeur textuelle ne peut provenir que d'un override de test : on la
    # normalise avec le parseur du registre plutôt que de la propager telle quelle.
    if isinstance(raw, str) and definition.value_type != parsers.TYPE_STR:
        try:
            return definition.parse(raw)
        except parsers.ConfigValueError:
            return definition.default
    return raw
