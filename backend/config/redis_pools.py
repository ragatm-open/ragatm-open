"""Isolation des pools de connexions Redis entre alias de cache.

Le problème corrigé ici a causé des HTTP 500 en production le 22/08/2026, sur des
requêtes sans aucun rapport entre elles (`/chat/stream/`, `/documents/…/summary/`,
`/chat/threads/…/messages/`), dont deux vues par un utilisateur réel.

Mécanisme, lu dans ``django_redis.pool`` (5.4.0) :

    class ConnectionFactory:
        _pools: Dict[str, Redis] = {}          # attribut de CLASSE, global au processus

        def get_or_create_connection_pool(self, params):
            key = params["url"]                # indexé par la SEULE URL
            if key not in self._pools:
                self._pools[key] = self.get_connection_pool(params)
            return self._pools[key]

``SOCKET_TIMEOUT`` ne fait pas partie de la clé. Deux alias qui visent la même URL
partagent donc le même objet pool, et **le premier qui se connecte impose son délai
de socket à l'autre**.

Conséquence concrète : l'alias ``ops``, doté d'un délai volontairement court
(250 ms) pour que la collecte de métriques ne s'ajoute jamais à une requête
utilisateur, contaminait l'alias ``default`` — celui qu'utilise le contrôle de
débit DRF, sur le chemin de **toutes** les requêtes API. Une lecture Redis
dépassant 250 ms devenait une erreur 500, avant même le corps de la vue.

L'ordre d'initialisation décide : Prometheus interrogeant ``/ops/metrics`` toutes
les 15 secondes, c'est presque toujours ``ops`` qui crée le pool après un
redémarrage de conteneur.

Le correctif tient en une propriété : **une optimisation d'observabilité ne doit
jamais pouvoir changer les paramètres de connexion du cache qui sert les requêtes
utilisateur.** On donne donc à ``ops`` sa propre fabrique, avec son propre cache de
pools — même serveur, même base logique, même espace de clés, mais un objet pool
distinct.
"""
from __future__ import annotations

from typing import Any, Dict

from django_redis.pool import ConnectionFactory


class OpsConnectionFactory(ConnectionFactory):
    """Fabrique dédiée aux compteurs d'exploitation, avec son PROPRE cache de pools.

    ``_pools`` est redéclaré ici : l'attribut de la sous-classe masque celui de
    ``ConnectionFactory``, si bien que les deux fabriques n'indexent plus dans le
    même dictionnaire. L'ordre d'initialisation devient sans effet — c'est ce qui
    rend la correction déterministe plutôt que chanceuse.

    Rien d'autre ne change : ni la classe de pool, ni l'URL, ni la base Redis, ni
    le contenu du cache. Seule l'identité de l'objet pool diffère, donc les
    paramètres de socket cessent de fuir d'un alias à l'autre.
    """

    #: Cache de pools PROPRE à cette fabrique (ne pas retirer : c'est le correctif).
    _pools: Dict[str, Any] = {}
