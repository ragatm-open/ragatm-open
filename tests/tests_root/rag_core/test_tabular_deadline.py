"""Contrat d'échéance — une requête, un budget.

Le défaut supprimé : chaque phase créait son propre budget complet depuis
`TABULAR_MAX_QUERY_SECONDS`. Une requête filtrée passait par `execute` puis
`count_selected`, chacun en ouvrant un entier — mesuré **2,0× le budget annoncé**,
sans compter catalogue, lecture et typage qui étaient entièrement hors budget.

Un plafond multipliable par le nombre de phases n'est pas un plafond.

Ces tests utilisent une horloge INJECTÉE : ils vérifient le contrat sans dormir, donc
sans dépendre de la charge de la machine — un test de temps qui dort est un test qui
échouera un jour sur un runner chargé, pour une raison qui n'a rien à voir.
"""
from __future__ import annotations

import pytest

from apps.rag_core.tabular import deadline as DL
from apps.rag_core.tabular import errors as E


class _Horloge:
    """Horloge contrôlée : le temps n'avance que si on le lui demande."""

    def __init__(self, depart: float = 1000.0):
        self.maintenant = depart

    def __call__(self) -> float:
        return self.maintenant

    def avancer(self, secondes: float) -> None:
        self.maintenant += secondes


def test_le_budget_initial_est_respecte():
    horloge = _Horloge()
    d = DL.Deadline.from_timeout(10.0, clock=horloge)
    assert d.remaining_seconds() == pytest.approx(10.0)
    assert d.expired() is False


def test_le_temps_restant_decroit_avec_lhorloge():
    horloge = _Horloge()
    d = DL.Deadline.from_timeout(10.0, clock=horloge)
    horloge.avancer(4.0)
    assert d.remaining_seconds() == pytest.approx(6.0)
    horloge.avancer(6.0)
    assert d.expired() is True


def test_le_temps_restant_nest_jamais_negatif():
    """Un restant négatif serait pris pour « infini » par la plupart des API."""
    horloge = _Horloge()
    d = DL.Deadline.from_timeout(1.0, clock=horloge)
    horloge.avancer(100.0)
    assert d.remaining_seconds() == 0.0


def test_lexpiration_leve_avec_la_phase():
    horloge = _Horloge()
    d = DL.Deadline.from_timeout(1.0, clock=horloge)
    d.raise_if_expired(phase="catalog")          # pas encore expiré
    horloge.avancer(1.5)
    with pytest.raises(E.TabularLimitExceeded) as exc:
        d.raise_if_expired(phase="canonical_csv_write")
    assert exc.value.code == E.ERR_QUERY_TIMEOUT
    assert exc.value.details["phase"] == "canonical_csv_write"


def test_plusieurs_checkpoints_partagent_le_meme_budget():
    """Le cœur du contrat : dix phases ne donnent pas dix budgets."""
    horloge = _Horloge()
    d = DL.Deadline.from_timeout(10.0, clock=horloge)
    for _ in range(9):
        horloge.avancer(1.0)
        d.raise_if_expired(phase="phase")
    horloge.avancer(1.0)
    with pytest.raises(E.TabularLimitExceeded):
        d.raise_if_expired(phase="phase")


def test_le_temps_est_impute_par_phase():
    horloge = _Horloge()
    d = DL.Deadline.from_timeout(10.0, clock=horloge)
    horloge.avancer(2.0)
    d.mark("catalog")
    horloge.avancer(3.0)
    d.mark("read_and_type")

    timings = d.timings_ms()
    assert timings["catalog"] == pytest.approx(2000.0)
    assert timings["read_and_type"] == pytest.approx(3000.0)


def test_le_resume_ne_contient_aucune_donnee_utilisateur():
    horloge = _Horloge()
    d = DL.Deadline.from_timeout(5.0, clock=horloge)
    horloge.avancer(1.0)
    d.mark("preflight")
    resume = d.summary()
    assert set(resume) == {"budget_s", "elapsed_s", "remaining_s", "phases_ms"}


def test_ensure_ne_recree_pas_une_echeance_existante():
    """`ensure` doit RELAYER, jamais rouvrir un budget — c'est tout l'enjeu."""
    horloge = _Horloge()
    d = DL.Deadline.from_timeout(10.0, clock=horloge)
    horloge.avancer(7.0)

    class _Limits:
        max_query_seconds = 10.0

    relaye = DL.ensure(d, _Limits())
    assert relaye is d
    assert relaye.remaining_seconds() == pytest.approx(3.0)


def test_ensure_cree_une_echeance_quand_il_ny_en_a_pas():
    class _Limits:
        max_query_seconds = 4.0

    d = DL.ensure(None, _Limits())
    assert d.budget_seconds == pytest.approx(4.0)


def test_le_budget_a_un_plancher():
    """Sous le plancher, produire le refus coûterait plus cher que la requête."""
    assert DL.Deadline.from_timeout(0.0).budget_seconds >= DL.MIN_BUDGET_SECONDS
    assert DL.Deadline.from_timeout(-5.0).budget_seconds >= DL.MIN_BUDGET_SECONDS


def test_lechance_nutilise_pas_lhorloge_murale():
    """Un ajustement NTP ne doit ni allonger ni raccourcir un budget en cours."""
    import inspect
    import time as _time

    source = inspect.getsource(DL)
    assert "time.time()" not in source
    assert DL.Deadline.from_timeout(1.0).clock is _time.monotonic


def test_lechance_ne_lance_ni_thread_ni_minuteur():
    """L'objet est passif : le contrôle est coopératif, pas concurrent.

    Un minuteur par requête coûterait un thread par requête — précisément ce qu'on
    ne veut pas sous concurrence.
    """
    import inspect

    source = inspect.getsource(DL)
    assert "Thread" not in source
    assert "Timer" not in source
