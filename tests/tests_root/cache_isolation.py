"""Cloisonnement du cache entre workers pytest-xdist — primitive TEST-ONLY.

Pourquoi ce module existe
-------------------------
En test, ``DJANGO_CACHE_URL`` pointe sur un vrai Redis partagé par tous les
workers. Deux mécanismes distincts peuvent y faire se marcher dessus deux
workers, et ils ne se corrigent pas de la même façon :

1. **Les clés.** Sans préfixe, ``ops:counter:*`` est le même jeu de clés pour
   tout le monde. C'est ce que a corrigé en posant
   ``KEY_PREFIX = test-<worker>``. Mais tout test qui **reconstruit**
   ``settings.CACHES`` efface ce préfixe sans s'en apercevoir :
   :func:`caches_cloisonnes` existe pour que la reconstruction le conserve.

2. **L'effacement.** ``cache.clear()`` de ``django_redis`` émet un **FLUSHDB** :
   il vide la base ENTIÈRE, préfixe compris. Le préfixe n'offre donc aucune
   protection contre lui. Un worker qui appelle ``cache.clear()`` détruit les
   clés de tous les autres, y compris au milieu de leurs assertions. C'est la
   cause de l'échec intermittent des compteurs ops observé lors de la qualification :
   :func:`clear_cloisonne` restreint l'effacement au préfixe du worker.

Le partage lui-même reste **voulu en production** : quatre workers gunicorn
doivent additionner leurs compteurs (cf. ``services/ops_counters.py``). Rien ici
ne touche au code produit — ce module ne s'applique qu'à la suite de tests.
"""

from __future__ import annotations

import copy
import os
from typing import Any, Mapping

#: Alias que la suite doit toujours retrouver. ``ops`` porte les compteurs
#: d'exploitation ; le laisser disparaître d'une reconstruction ferait retomber
#: ``ops_counters`` sur ``default`` — un repli licite en production, mais qui
#: masquerait ici ce que le test croit mesurer.
ALIAS_ATTENDUS = ("default", "ops")


def prefixe_de_worker() -> str:
    """``test-gw3`` sous xdist, ``test-maitre`` en exécution simple."""
    return f"test-{os.environ.get('PYTEST_XDIST_WORKER', 'maitre')}"


def caches_cloisonnes(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Copie de ``config`` dont CHAQUE alias porte le préfixe du worker.

    À utiliser partout où un test reconstruit ``settings.CACHES``. La copie est
    profonde : poser le préfixe sur le dictionnaire des réglages en place
    fuiterait vers les autres tests.
    """
    sortie = copy.deepcopy(dict(config or {}))
    prefixe = prefixe_de_worker()
    for alias in sortie:
        sortie[alias]["KEY_PREFIX"] = prefixe
    return sortie


def clear_cloisonne(self) -> None:
    """Remplace ``RedisCache.clear`` : n'efface QUE les clés de ce worker.

    ``delete_pattern`` construit son motif à partir de ``key_prefix`` et de la
    version du backend, donc ``"*"`` désigne « tout ce qui m'appartient » et rien
    d'autre. Sans préfixe configuré, le motif redevient global — ce qui est le
    comportement attendu hors xdist.

    Ne lève jamais : un test qui vide un cache ne doit pas échouer parce que
    Redis est momentanément indisponible, exactement comme ``clear`` d'origine
    qui est décoré par ``omit_exception``.
    """
    try:
        self.delete_pattern("*")
    except Exception:  # noqa: BLE001 - mêmes garanties que le `clear` d'origine
        pass
