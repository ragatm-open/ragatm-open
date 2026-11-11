"""Isolation des pools Redis entre l'alias `default` et l'alias `ops`.

Le défaut corrigé a produit des HTTP 500 en production le 22/08/2026 sur des
requêtes sans rapport entre elles. Sa mécanique est subtile et invisible à la
lecture des réglages :

``django_redis.pool.ConnectionFactory._pools`` est un attribut de CLASSE, indexé
par la SEULE URL de connexion. ``SOCKET_TIMEOUT`` n'entre pas dans la clé. Deux
alias visant la même URL partagent donc le même objet pool, et le premier qui se
connecte impose son délai de socket à l'autre.

L'alias ``ops`` porte volontairement un délai court (250 ms) pour que la collecte
de métriques ne s'ajoute jamais à une requête utilisateur. Sans isolation, ce
délai devenait celui du contrôle de débit DRF — sur le chemin de TOUTES les
requêtes API.

Ce que ces tests verrouillent :

* les deux alias n'utilisent pas le même objet pool ;
* **quel que soit l'ordre d'initialisation** — c'est là que se jouait le défaut ;
* chaque pool conserve son propre délai de socket ;
* la tolérance d'erreur reste propre à ``ops`` : une panne Redis ne doit pas
  devenir silencieuse pour le cache applicatif.
"""
from __future__ import annotations

import importlib

import pytest
from django.core.cache import caches

from tests_root.cache_isolation import caches_cloisonnes

from config.redis_pools import OpsConnectionFactory
from django_redis.pool import ConnectionFactory

OPS_TIMEOUT = 0.25


def _redis_configure(settings) -> None:
    """Configure les deux alias sur la MÊME URL — la condition du défaut."""
    url = "redis://127.0.0.1:63790/9"
    # `caches_cloisonnes` : ces tests visent un Redis RÉEL, donc une reconstruction
    # sans préfixe partagerait ses clés avec tous les autres workers.
    settings.CACHES = caches_cloisonnes({
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": url,
            "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
        },
        "ops": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": url,
            "OPTIONS": {
                "CLIENT_CLASS": "django_redis.client.DefaultClient",
                "CONNECTION_FACTORY": "config.redis_pools.OpsConnectionFactory",
                "SOCKET_TIMEOUT": OPS_TIMEOUT,
                "SOCKET_CONNECT_TIMEOUT": OPS_TIMEOUT,
                "IGNORE_EXCEPTIONS": True,
            },
        },
    })


def _vider_les_pools() -> None:
    """Repart de zéro : les caches de pools sont des attributs de CLASSE.

    On vide aussi les backends mémorisés par Django, sans quoi un test hériterait
    du client construit par le précédent — et l'ordre d'initialisation, qui est
    justement l'objet de ces tests, ne serait plus maîtrisé.
    """
    ConnectionFactory._pools.clear()
    OpsConnectionFactory._pools.clear()
    for attribut in ("caches", "_caches"):
        local = getattr(caches, "_caches", None)
        if local is not None and hasattr(local, "caches"):
            local.caches = {}
            break


@pytest.fixture(autouse=True)
def pools_vierges(settings):
    _redis_configure(settings)
    _vider_les_pools()
    yield
    _vider_les_pools()


def _pool(alias: str):
    """Pool RÉELLEMENT utilisé par l'alias (pas l'objet backend Django)."""
    return caches[alias].client.get_client(write=True).connection_pool


# ---------------------------------------------------------------------------
# Identité des pools
# ---------------------------------------------------------------------------
def test_les_deux_alias_nutilisent_pas_le_meme_pool():
    """Le contrôle porte sur l'objet POOL, pas sur les backends Django.

    Deux backends distincts peuvent parfaitement partager le pool sous-jacent :
    c'était précisément le cas en production.
    """
    assert _pool("default") is not _pool("ops")


def test_les_fabriques_ont_des_caches_de_pools_distincts():
    """`_pools` de la sous-classe masque celui du parent — c'est le correctif."""
    assert OpsConnectionFactory._pools is not ConnectionFactory._pools


# ---------------------------------------------------------------------------
# Ordre d'initialisation — le cœur du défaut
# ---------------------------------------------------------------------------
def test_ordre_ops_puis_default():
    """Scénario réel : Prometheus interroge /ops/metrics avant toute requête API."""
    p_ops = _pool("ops")
    p_def = _pool("default")
    assert p_ops is not p_def
    assert p_ops.connection_kwargs.get("socket_timeout") == OPS_TIMEOUT
    assert p_def.connection_kwargs.get("socket_timeout") is None


def test_ordre_default_puis_ops():
    """Ordre inverse : le résultat doit être identique."""
    p_def = _pool("default")
    p_ops = _pool("ops")
    assert p_ops is not p_def
    assert p_ops.connection_kwargs.get("socket_timeout") == OPS_TIMEOUT
    assert p_def.connection_kwargs.get("socket_timeout") is None


@pytest.mark.parametrize("ordre", [("ops", "default"), ("default", "ops")])
def test_le_delai_court_ne_fuit_jamais_vers_default(ordre):
    """Propriété centrale, verrouillée dans les DEUX sens.

    Avant correction, cette assertion échouait pour l'ordre (ops, default) :
    `default` héritait de `socket_timeout=0.25`.
    """
    pools = {alias: _pool(alias) for alias in ordre}
    assert pools["default"].connection_kwargs.get("socket_timeout") is None, (
        "le cache applicatif a hérité du délai des métriques"
    )
    assert pools["ops"].connection_kwargs.get("socket_timeout") == OPS_TIMEOUT


def test_le_delai_de_connexion_ne_fuit_pas_non_plus():
    _pool("ops")
    assert _pool("default").connection_kwargs.get("socket_connect_timeout") is None


# ---------------------------------------------------------------------------
# Sémantique des pannes : ne rien masquer sur `default`
# ---------------------------------------------------------------------------
def test_default_ne_masque_pas_les_pannes_redis(settings):
    """`IGNORE_EXCEPTIONS` reste propre à `ops`.

    L'ajouter à `default` supprimerait aussi les 500 — en rendant toute
    indisponibilité de Redis silencieuse. Ce n'est pas le correctif retenu.
    """
    assert settings.CACHES["default"]["OPTIONS"].get("IGNORE_EXCEPTIONS") in (None, False)
    assert settings.CACHES["ops"]["OPTIONS"].get("IGNORE_EXCEPTIONS") is True


def test_configuration_reelle_du_projet_porte_la_fabrique_dediee():
    """La correction vit dans la configuration canonique, pas dans un .env de serveur."""
    from config.settings import base

    caches_reels = base._build_caches("redis://redis:6379/2")
    ops_options = caches_reels["ops"]["OPTIONS"]
    assert ops_options["CONNECTION_FACTORY"] == "config.redis_pools.OpsConnectionFactory"
    assert ops_options["SOCKET_TIMEOUT"] == base.OPS_COUNTERS_IO_TIMEOUT_SECONDS
    assert "CONNECTION_FACTORY" not in caches_reels["default"]["OPTIONS"]
    assert "SOCKET_TIMEOUT" not in caches_reels["default"]["OPTIONS"]


# ---------------------------------------------------------------------------
# Témoin : le défaut existe bel et bien sans la fabrique dédiée
# ---------------------------------------------------------------------------
def test_temoin_sans_fabrique_dediee_le_pool_est_partage(settings):
    """Preuve causale : SANS `CONNECTION_FACTORY`, le délai court fuit vers `default`.

    Ce test ne valide pas le correctif — il valide que les autres tests ont un
    objet. Sans lui, rien ne distinguerait une isolation réelle d'une bibliothèque
    qui n'aurait jamais partagé ses pools.

    S'il venait à échouer, cela signifierait que `django_redis` a changé son
    indexation de pools : la fabrique dédiée deviendrait alors superflue, et ce
    dossier serait à reconsidérer — pas à rafistoler.
    """
    url = "redis://127.0.0.1:63790/9"
    settings.CACHES = caches_cloisonnes({
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": url,
            "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
        },
        "ops": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": url,
            "OPTIONS": {
                "CLIENT_CLASS": "django_redis.client.DefaultClient",
                # PAS de CONNECTION_FACTORY : configuration d'AVANT le correctif.
                "SOCKET_TIMEOUT": OPS_TIMEOUT,
                "SOCKET_CONNECT_TIMEOUT": OPS_TIMEOUT,
                "IGNORE_EXCEPTIONS": True,
            },
        },
    })
    _vider_les_pools()

    p_ops = _pool("ops")        # l'observabilité se connecte la première
    p_def = _pool("default")    # …et le cache applicatif hérite de son pool

    assert p_ops is p_def, "django_redis ne partage plus les pools par URL"
    assert p_def.connection_kwargs.get("socket_timeout") == OPS_TIMEOUT, (
        "le délai de 250 ms n'a pas contaminé `default` — le défaut a disparu ailleurs"
    )


def test_sans_url_redis_les_deux_alias_restent_locaux():
    """Dev/test sans Redis : aucun pool, aucune régression."""
    from config.settings import base

    locaux = base._build_caches("")
    assert "locmem" in locaux["default"]["BACKEND"]
    assert "locmem" in locaux["ops"]["BACKEND"]


# ---------------------------------------------------------------------------
# Scénario de production : les métriques se connectent en PREMIER
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_scenario_prometheus_dabord_puis_requete_api(settings, client, django_user_model):
    """Reproduit la séquence exacte de l'incident du 22/08/2026.

        démarrage du conteneur
          -> Prometheus interroge /ops/metrics (toutes les 15 s)
          -> première requête API d'un utilisateur

    Avant correction, la troisième étape renvoyait 500 : le contrôle de débit DRF
    lisait Redis via le pool créé par les métriques, avec 250 ms de budget.
    """
    _vider_les_pools()

    # 1) L'observabilité se connecte la première.
    caches["ops"].get("compteur-inexistant")
    pool_ops = _pool("ops")

    # 2) Le cache applicatif obtient un pool DISTINCT, sans délai contaminé.
    pool_default = _pool("default")
    assert pool_ops is not pool_default
    assert pool_default.connection_kwargs.get("socket_timeout") is None

    # 3) Une requête API traverse `initial()` — donc le contrôle de débit — sans 500.
    django_user_model.objects.create_user(username="isolation", password="x" * 12)
    reponse = client.get("/api/v1/health/", HTTP_HOST="testserver")
    assert reponse.status_code != 500, "le contrôle de débit a échoué sur Redis"
