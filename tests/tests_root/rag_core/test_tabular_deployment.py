"""Capacité de DÉPLOIEMENT — calculable, vérifiée, jamais supposée.

Le moteur borne correctement un processus. Un processus n'est pas un déploiement :

    capacité déploiement = workers × limite locale × réplicas

Le principe que ces tests verrouillent : **une inconnue reste une inconnue**. Un
processus ne peut pas découvrir son nombre de réplicas — cette information vit dans
l'orchestrateur. Lorsqu'elle n'est pas déclarée, le module doit répondre `None`, jamais
une valeur plausible. Une capacité supposée est pire qu'une capacité inconnue : elle
inspire une confiance que rien ne soutient, et c'est un motif explicite de NO-GO.
"""
from __future__ import annotations

import pytest

from apps.rag_core.tabular import capacity as CAP
from apps.rag_core.tabular import deployment as DEP


@pytest.fixture(autouse=True)
def _propre(monkeypatch):
    CAP.reset()
    monkeypatch.delenv("GUNICORN_WORKERS", raising=False)
    monkeypatch.delenv("TABULAR_DEPLOYMENT_REPLICAS", raising=False)
    monkeypatch.delenv("TABULAR_MAX_DEPLOYMENT_CAPACITY", raising=False)
    yield
    CAP.reset()


# --------------------------------------------------------------------------- #
# Calcul
# --------------------------------------------------------------------------- #
def test_la_capacite_instance_est_le_produit_workers_x_limite(settings, monkeypatch):
    settings.TABULAR_MAX_CONCURRENT_QUERIES = 2
    monkeypatch.setenv("GUNICORN_WORKERS", "4")

    etat = DEP.describe()
    assert etat["local_limit"] == 2
    assert etat["worker_count"] == 4
    assert etat["instance_capacity"] == 8            # et non 2


def test_la_capacite_deploiement_multiplie_par_les_replicas(settings, monkeypatch):
    settings.TABULAR_MAX_CONCURRENT_QUERIES = 2
    monkeypatch.setenv("GUNICORN_WORKERS", "4")
    monkeypatch.setenv("TABULAR_DEPLOYMENT_REPLICAS", "3")

    etat = DEP.describe()
    assert etat["deployment_capacity"] == 24         # 2 × 4 × 3
    assert etat["deployment_capacity_known"] is True


def test_des_replicas_non_declares_donnent_une_capacite_INCONNUE(settings):
    """Le cœur du contrat : `None`, jamais une valeur supposée.

    Un conteneur ne peut pas découvrir combien de réplicas tournent. Combler ce trou
    par une hypothèse produirait un chiffre rassurant et faux.
    """
    settings.TABULAR_MAX_CONCURRENT_QUERIES = 2

    etat = DEP.describe()
    assert etat["replica_count"] is None
    assert etat["deployment_capacity"] is None       # et surtout PAS 0, ni 1× l'instance
    assert etat["deployment_capacity_known"] is False


def test_la_capacite_nest_jamais_annoncee_globalement_garantie(settings):
    settings.TABULAR_MAX_CONCURRENT_QUERIES = 2
    assert DEP.describe()["globally_enforced"] is False
    assert DEP.describe()["scope"] == "process"


def test_la_source_de_chaque_valeur_est_tracee(settings, monkeypatch):
    """Un opérateur doit pouvoir vérifier d'où sort un chiffre."""
    settings.TABULAR_MAX_CONCURRENT_QUERIES = 2
    monkeypatch.setenv("GUNICORN_WORKERS", "3")

    etat = DEP.describe()
    assert etat["worker_count_source"] == "GUNICORN_WORKERS"
    assert etat["replica_count_source"] == "non déclaré"


def test_sans_variable_le_defaut_de_lentrypoint_est_reflete(settings):
    """`entrypoint.sh` applique 2 : on le reflète explicitement, on ne le devine pas."""
    settings.TABULAR_MAX_CONCURRENT_QUERIES = 2
    etat = DEP.describe()
    assert etat["worker_count"] == 2
    assert "entrypoint" in etat["worker_count_source"]


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("valeur", ["", "abc", "0", "-3"])
def test_un_nombre_de_workers_illisible_est_signale(settings, monkeypatch, valeur):
    settings.TABULAR_MAX_CONCURRENT_QUERIES = 2
    monkeypatch.setenv("GUNICORN_WORKERS", valeur)

    etat = DEP.describe()
    if valeur in ("0", "-3"):
        assert any(p["code"] == DEP.WORKERS_UNKNOWN for p in etat["problems"])
        assert etat["instance_capacity"] is None
    else:
        # Illisible -> on retombe sur le défaut documenté de l'entrypoint.
        assert etat["worker_count"] == 2


def test_une_limite_locale_aberrante_est_signalee(settings):
    settings.TABULAR_MAX_CONCURRENT_QUERIES = 10_000
    etat = DEP.describe()
    assert any(p["code"] == DEP.CFG_INVALID for p in etat["problems"])
    assert etat["local_limit"] == CAP.MAX_ALLOWED     # corrigée, pas subie


def test_un_plafond_de_deploiement_declare_est_respecte(settings, monkeypatch):
    settings.TABULAR_MAX_CONCURRENT_QUERIES = 2
    monkeypatch.setenv("GUNICORN_WORKERS", "8")
    monkeypatch.setenv("TABULAR_DEPLOYMENT_REPLICAS", "4")
    monkeypatch.setenv("TABULAR_MAX_DEPLOYMENT_CAPACITY", "16")

    etat = DEP.describe()                              # 2 × 8 × 4 = 64 > 16
    assert any(p["code"] == DEP.CFG_UNSAFE for p in etat["problems"])
    assert etat["configuration_valid"] is False


def test_une_memoire_insuffisante_est_signalee(settings, monkeypatch):
    """Une capacité calculable n'est pas une capacité tenable.

    24 exécutions simultanées se calculent très bien ; encore faut-il que la machine
    puisse les porter.
    """
    settings.TABULAR_MAX_CONCURRENT_QUERIES = 32
    settings.TABULAR_MIN_MEMORY_HEADROOM_MB = 512
    monkeypatch.setenv("GUNICORN_WORKERS", "16")
    monkeypatch.setattr(DEP, "available_memory_mb", lambda: 1_024)

    etat = DEP.describe()
    assert any(p["code"] == DEP.CFG_UNSAFE for p in etat["problems"])


def test_le_mode_strict_exige_des_replicas_declares(settings):
    settings.TABULAR_MAX_CONCURRENT_QUERIES = 2
    assert DEP.describe(strict=False)["configuration_valid"] is True
    assert DEP.describe(strict=True)["configuration_valid"] is False


def test_la_garde_bloque_en_strict_et_laisse_passer_sinon(settings):
    settings.TABULAR_MAX_CONCURRENT_QUERIES = 2

    with pytest.raises(DEP.CapacityConfigurationError) as exc:
        DEP.validate_or_raise(strict=True)
    assert exc.value.code == DEP.WORKERS_UNKNOWN

    etat = DEP.validate_or_raise(strict=False)
    assert etat["configuration_valid"] is True


# --------------------------------------------------------------------------- #
# System check : la garde de démarrage
# --------------------------------------------------------------------------- #
def test_le_check_est_silencieux_si_le_moteur_est_desactive(settings):
    """Inutile d'imposer une configuration de capacité à qui n'utilise pas le moteur."""
    from apps.rag_core.checks_tabular_capacity import check_tabular_capacity

    settings.TABULAR_QUERY_ENABLED = False
    assert check_tabular_capacity(None) == []


def test_le_check_avertit_hors_production(settings):
    from django.core.checks import Warning as CheckWarning

    from apps.rag_core.checks_tabular_capacity import check_tabular_capacity

    settings.TABULAR_QUERY_ENABLED = True
    settings.TABULAR_MAX_CONCURRENT_QUERIES = 10_000
    messages = check_tabular_capacity(None)
    assert messages
    assert all(isinstance(m, CheckWarning) for m in messages)


def test_le_check_BLOQUE_en_production(settings, monkeypatch):
    """En production, une capacité inconnue empêche le démarrage.

    Django refuse de servir sur une `Error` : déployer sans savoir combien de requêtes
    lourdes peuvent tourner est exactement le risque que ce lot supprime.
    """
    from django.core.checks import Error

    from apps.rag_core.checks_tabular_capacity import check_tabular_capacity

    settings.TABULAR_QUERY_ENABLED = True
    settings.TABULAR_MAX_CONCURRENT_QUERIES = 2
    monkeypatch.setattr(
        "apps.rag_core.checks_tabular_capacity._is_production", lambda: True)

    messages = check_tabular_capacity(None)
    assert messages, "aucune réplique déclarée : la production doit refuser de démarrer"
    assert all(isinstance(m, Error) for m in messages)
    assert all(m.id.startswith("rag_core.tabular.capacity.") for m in messages)


def test_une_configuration_complete_passe_le_check_en_production(settings, monkeypatch):
    from apps.rag_core.checks_tabular_capacity import check_tabular_capacity

    settings.TABULAR_QUERY_ENABLED = True
    settings.TABULAR_MAX_CONCURRENT_QUERIES = 2
    monkeypatch.setenv("GUNICORN_WORKERS", "2")
    monkeypatch.setenv("TABULAR_DEPLOYMENT_REPLICAS", "1")
    monkeypatch.setattr(
        "apps.rag_core.checks_tabular_capacity._is_production", lambda: True)

    assert check_tabular_capacity(None) == []
