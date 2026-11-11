"""Ordonnancement périodique — schedule, verrous, idempotence (Lot 4-A/B/C).

Verrouille l'invariant central du lot : **une tâche déclarée dans le code n'est
pas une tâche planifiée**, et une tâche planifiée vers une queue que personne ne
consomme n'est pas une tâche exécutée. Les deux défauts étaient présents avant ce
lot et ne produisaient aucun signal.
"""
from __future__ import annotations

import importlib

import pytest
from django.conf import settings

from apps.rag_core.checks_operations import KNOWN_QUEUES, check_beat_schedule
from apps.rag_core.services.periodic import (
    LockNotAcquired,
    distributed_lock,
    periodic_run,
)
from apps.rag_core.tabular import observability as obs

#: Les tâches qui DOIVENT être ordonnancées. Cette liste est le contrat : y
#: retirer une entrée doit être un acte délibéré, visible en revue.
REQUIRED_SCHEDULES = {
    "reap-stale-reindex-items": "apps.rag_core.tasks.reap_stale_reindex_items_task",
    "reap-stale-summaries": "apps.rag_core.tasks.reap_stale_summaries_task",
    "purge-expired-audit-logs": "apps.audit.tasks.purge_expired_audit_logs_task",
    "celery-beat-heartbeat": "apps.rag_core.tasks.celery_beat_heartbeat_task",
}


# --------------------------------------------------------------------------- #
# Le schedule existe et est complet
# --------------------------------------------------------------------------- #

def test_beat_schedule_is_defined_in_settings():
    """Source UNIQUE : le schedule vit dans les settings, plus dans celery.py."""
    schedule = getattr(settings, "CELERY_BEAT_SCHEDULE", None)
    assert isinstance(schedule, dict) and schedule


@pytest.mark.parametrize("name,task", sorted(REQUIRED_SCHEDULES.items()))
def test_required_task_is_scheduled(name, task):
    """La purge d'audit n'était ordonnancée NULLE PART avant ce lot."""
    schedule = settings.CELERY_BEAT_SCHEDULE
    assert name in schedule, f"{name} n'est pas planifiée"
    assert schedule[name]["task"] == task


def test_celery_app_loads_the_schedule():
    """Le schedule doit arriver jusqu'à l'application Celery, pas rester en settings."""
    from config.celery import app

    loaded = app.conf.beat_schedule or {}
    for name in REQUIRED_SCHEDULES:
        assert name in loaded, f"{name} absente de app.conf.beat_schedule"


def test_celery_module_does_not_redefine_schedule():
    """Un schedule défini à deux endroits est un schedule qui divergera.

    Le chemin est dérivé du module réel (`config.celery.__file__`) et non d'un
    chemin relatif au CWD : la CI lance pytest depuis `backend/`, pas depuis la
    racine du dépôt — un `Path("backend/config/celery.py")` y est introuvable.
    """
    import pathlib

    import config.celery

    src = pathlib.Path(config.celery.__file__).read_text()
    assert "beat_schedule[" not in src


# --------------------------------------------------------------------------- #
# Chaque entrée est exécutable
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name", sorted(REQUIRED_SCHEDULES))
def test_scheduled_task_is_importable(name):
    """Une tâche planifiée mais non importable échoue en silence, à chaque cycle."""
    path = settings.CELERY_BEAT_SCHEDULE[name]["task"]
    module_path, _, func = path.rpartition(".")
    module = importlib.import_module(module_path)
    assert hasattr(module, func)


@pytest.mark.parametrize("name", sorted(REQUIRED_SCHEDULES))
def test_scheduled_task_targets_a_consumed_queue(name):
    """Router vers une queue non consommée = messages qui s'empilent sans fin."""
    queue = (settings.CELERY_BEAT_SCHEDULE[name].get("options") or {}).get("queue")
    assert queue in KNOWN_QUEUES, f"{name} route vers une queue inconnue : {queue}"


@pytest.mark.parametrize("name", sorted(REQUIRED_SCHEDULES))
def test_scheduled_task_has_positive_frequency_and_expiry(name):
    entry = settings.CELERY_BEAT_SCHEDULE[name]
    assert float(entry["schedule"]) > 0
    # `expires` évite qu'un worker indisponible fasse s'empiler les déclenchements
    # pour les rejouer tous d'un coup au retour.
    assert (entry.get("options") or {}).get("expires")


def test_schedule_check_passes_on_current_settings():
    assert check_beat_schedule(None) == []


# --------------------------------------------------------------------------- #
# Le check détecte les schedules cassés
# --------------------------------------------------------------------------- #

def test_check_detects_unimportable_task(settings):
    settings.CELERY_BEAT_SCHEDULE = {
        "x": {"task": "apps.nowhere.does_not_exist", "schedule": 60.0}
    }
    ids = [e.id for e in check_beat_schedule(None)]
    assert "ragatm.E011" in ids


def test_check_detects_unknown_queue(settings):
    settings.CELERY_BEAT_SCHEDULE = {
        "x": {
            "task": "apps.rag_core.tasks.celery_beat_heartbeat_task",
            "schedule": 60.0,
            "options": {"queue": "queue-fantome"},
        }
    }
    msgs = " ".join(str(e) for e in check_beat_schedule(None))
    assert "queue-fantome" in msgs


def test_check_detects_invalid_frequency(settings):
    settings.CELERY_BEAT_SCHEDULE = {
        "x": {"task": "apps.rag_core.tasks.celery_beat_heartbeat_task", "schedule": 0}
    }
    ids = [e.id for e in check_beat_schedule(None)]
    assert "ragatm.E011" in ids


def test_check_detects_missing_broker(settings):
    settings.CELERY_TASK_ALWAYS_EAGER = False
    settings.CELERY_BROKER_URL = ""
    ids = [e.id for e in check_beat_schedule(None)]
    assert "ragatm.E012" in ids



# --------------------------------------------------------------------------- #
# Verrou distribué — testé sur un faux Redis EN PROCESSUS
# --------------------------------------------------------------------------- #
# Le verrou s'appuie sur Redis (broker Celery). La CI backend ne fournit PAS de
# service Redis, et tester la sémantique d'exclusion contre un vrai Redis rendrait
# ces tests dépendants de l'environnement — c'est exactement ce qui les a fait
# passer en local et échouer en CI. On teste donc la LOGIQUE (SET NX EX + release
# conditionnelle par jeton) contre un faux fidèle, déterministe et sans dépendance.
# L'intégration à un vrai Redis est prouvée séparément par la qualification runtime.


class _FakeRedis:
    """Implémente le strict nécessaire de `distributed_lock` : `set(nx,ex)` et `eval`."""

    def __init__(self):
        self._store: dict = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self._store:
            return None            # NX : la clé existe déjà -> acquisition refusée
        self._store[key] = value
        return True

    def get(self, key):
        return self._store.get(key)

    def eval(self, script, numkeys, key, token):
        # Release conditionnelle : ne supprime que si le jeton correspond.
        if self._store.get(key) == token:
            del self._store[key]
            return 1
        return 0


@pytest.fixture
def fake_redis(monkeypatch):
    from apps.rag_core.services import periodic

    client = _FakeRedis()
    monkeypatch.setattr(periodic, "_redis_client", lambda: client)
    return client


@pytest.mark.parametrize("second_attempt_expected", [LockNotAcquired])
def test_lock_is_mutually_exclusive(second_attempt_expected, fake_redis):
    """Deux Beat, ou un Beat qui redéclenche trop tôt : une seule exécution."""
    name = "test_lock_exclusive"
    with distributed_lock(name, ttl_seconds=30):
        with pytest.raises(second_attempt_expected):
            with distributed_lock(name, ttl_seconds=30):
                pass  # pragma: no cover


def test_lock_is_released_and_reusable(fake_redis):
    """Un verrou non libéré bloquerait la tâche pour toujours."""
    name = "test_lock_release"
    with distributed_lock(name, ttl_seconds=30):
        pass
    with distributed_lock(name, ttl_seconds=30):
        pass  # réacquisition immédiate


def test_lock_skip_is_counted_not_failed(fake_redis):
    """Sauter un tour n'est PAS un échec : ne pas polluer le compteur d'erreurs."""
    from apps.rag_core.services import periodic

    obs.reset()
    name = "test_lock_metric"
    with distributed_lock(name, ttl_seconds=30):
        with pytest.raises(LockNotAcquired):
            with distributed_lock(name, ttl_seconds=30):
                pass  # pragma: no cover

    snap = obs.snapshot()
    assert snap.get(periodic.PERIODIC_LOCK_SKIPS) == 1
    assert snap.get(periodic.PERIODIC_FAILURES, 0) == 0

def test_lock_without_redis_degrades_gracefully(monkeypatch):
    """Sans Redis joignable, le verrou est ACCORDÉ (documenté : ne pas bloquer
    toute la maintenance sur une panne broker). C'est le seul point non
    fail-closed du lot, assumé, couvert par l'idempotence des tâches."""
    from apps.rag_core.services import periodic

    monkeypatch.setattr(periodic, "_redis_client", lambda: None)
    name = "test_lock_no_redis"
    # Deux acquisitions imbriquées réussissent : aucune exclusion sans Redis.
    with distributed_lock(name):
        with distributed_lock(name):
            pass



def test_periodic_run_counts_success_and_failure():
    from apps.rag_core.services import periodic

    obs.reset()
    with periodic_run("t_ok", lock=False) as run:
        run["n"] = 3

    with pytest.raises(ValueError):
        with periodic_run("t_ko", lock=False):
            raise ValueError("boom")

    snap = obs.snapshot()
    assert snap.get(periodic.PERIODIC_RUNS) == 2
    assert snap.get(periodic.PERIODIC_FAILURES) == 1


def test_periodic_run_never_logs_exception_message(caplog):
    """Un message d'erreur peut contenir des paramètres SQL ou une réponse tierce."""
    obs.reset()
    secret = "CONFIDENTIAL_CANARY_METRIC"
    with caplog.at_level("ERROR"):
        with pytest.raises(ValueError):
            with periodic_run("t_secret", lock=False):
                raise ValueError(secret)
    assert secret not in caplog.text
