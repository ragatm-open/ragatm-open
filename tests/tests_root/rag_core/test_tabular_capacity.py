"""Capacité concurrente — une limite APPLIQUÉE, et une portée honnête.

Deux propriétés distinctes, et la seconde compte autant que la première :

1. la limite est réellement appliquée dans le processus, jamais dépassée ;
2. sa PORTÉE est le processus, pas le déploiement — `effective_capacity()` doit le
   dire, parce que présenter une borne locale comme globale serait un mensonge par
   omission, et exactement le motif de NO-GO de la spécification.

Les scénarios de contention utilisent `Barrier` et `Event` plutôt que des `sleep` :
un test temporel finit toujours par échouer sur un runner chargé, pour une raison
sans rapport avec ce qu'il prétend vérifier.
"""
from __future__ import annotations

import threading

import pytest

from apps.rag_core.tabular import capacity as CAP
from apps.rag_core.tabular import deadline as DL
from apps.rag_core.tabular import errors as E
from apps.rag_core.tabular import observability as OBS

pytestmark = pytest.mark.tabular_concurrency


@pytest.fixture(autouse=True)
def _limiteur_propre():
    CAP.reset()
    OBS.reset()
    yield
    CAP.reset()
    OBS.reset()


class _Horloge:
    def __init__(self, depart: float = 1000.0):
        self.maintenant = depart

    def __call__(self) -> float:
        return self.maintenant

    def avancer(self, secondes: float) -> None:
        self.maintenant += secondes


def _deadline(budget: float = 10.0, horloge=None) -> DL.Deadline:
    return DL.Deadline.from_timeout(budget, clock=horloge or DL.time.monotonic)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def test_la_limite_par_defaut_est_celle_que_la_mesure_justifie(settings):
    settings.TABULAR_MAX_CONCURRENT_QUERIES = 2
    assert CAP.configured_limit() == 2


@pytest.mark.parametrize("brut,attendu", [
    (0, 1),            # 0 bloquerait tout : corrigé, pas subi
    (-5, 1),
    ("abc", CAP.DEFAULT_LIMIT),
    (None, CAP.DEFAULT_LIMIT),
    (10_000, CAP.MAX_ALLOWED),
])
def test_une_valeur_aberrante_est_corrigee_et_signalee(settings, brut, attendu):
    settings.TABULAR_MAX_CONCURRENT_QUERIES = brut
    assert CAP.configured_limit() == attendu


# --------------------------------------------------------------------------- #
# Application de la limite
# --------------------------------------------------------------------------- #
def test_la_limite_nest_jamais_depassee(settings):
    """Le contrôle central : au plus N exécutions simultanées.

    `Barrier` rend le scénario déterministe — tous les threads atteignent le même
    point avant qu'on observe le compteur.
    """
    settings.TABULAR_MAX_CONCURRENT_QUERIES = 2
    limite = 2
    observes: list[int] = []
    verrou = threading.Lock()
    relacher = threading.Event()
    entres = threading.Barrier(limite + 1, timeout=10)

    def tache():
        try:
            with CAP.slot(_deadline(5.0)) as limiteur:
                with verrou:
                    observes.append(limiteur.active)
                entres.wait()          # les `limite` premiers se rejoignent ici
                relacher.wait(timeout=10)
        except (E.TabularLimitExceeded, threading.BrokenBarrierError):
            pass

    fils = [threading.Thread(target=tache) for _ in range(limite)]
    for f in fils:
        f.start()
    entres.wait(timeout=10)            # les deux slots sont pris
    assert CAP.get_limiter().active == limite
    relacher.set()
    for f in fils:
        f.join(timeout=10)

    assert observes, "aucun slot acquis"
    assert max(observes) <= limite


def test_une_requete_supplementaire_attend_puis_passe(settings):
    """Le slot libéré doit être réutilisable — sinon la capacité fond à l'usage."""
    settings.TABULAR_MAX_CONCURRENT_QUERIES = 1
    occupe = threading.Event()
    liberer = threading.Event()
    passe = threading.Event()

    def premier():
        with CAP.slot(_deadline(10.0)):
            occupe.set()
            liberer.wait(timeout=10)

    def second():
        occupe.wait(timeout=10)
        with CAP.slot(_deadline(10.0)):
            passe.set()

    t1, t2 = threading.Thread(target=premier), threading.Thread(target=second)
    t1.start()
    t2.start()
    occupe.wait(timeout=10)
    assert passe.is_set() is False, "le second n'aurait pas dû passer"
    liberer.set()
    t1.join(timeout=10)
    t2.join(timeout=10)
    assert passe.is_set() is True


# --------------------------------------------------------------------------- #
# Échéance : l'attente consomme LE budget, elle n'en ouvre pas un second
# --------------------------------------------------------------------------- #
def test_lattente_est_bornee_par_lecheance_globale(settings):
    settings.TABULAR_MAX_CONCURRENT_QUERIES = 1
    occupe = threading.Event()
    liberer = threading.Event()
    resultat: dict = {}

    def occupant():
        with CAP.slot(_deadline(30.0)):
            occupe.set()
            liberer.wait(timeout=10)

    def demandeur():
        occupe.wait(timeout=10)
        try:
            with CAP.slot(_deadline(0.2)):      # budget volontairement court
                resultat["issue"] = "acquis"
        except E.TabularLimitExceeded as exc:
            resultat["issue"] = exc.code
            resultat["phase"] = exc.details.get("phase")

    t1, t2 = threading.Thread(target=occupant), threading.Thread(target=demandeur)
    t1.start()
    t2.start()
    t2.join(timeout=10)
    liberer.set()
    t1.join(timeout=10)

    assert resultat["issue"] == E.ERR_QUERY_TIMEOUT
    assert resultat["phase"] == "capacity_wait"


def test_un_budget_deja_epuise_ne_donne_pas_de_second_budget(settings):
    """Une requête qui a tout consommé ne doit pas gagner une fenêtre d'attente."""
    settings.TABULAR_MAX_CONCURRENT_QUERIES = 1
    horloge = _Horloge()
    d = DL.Deadline.from_timeout(1.0, clock=horloge)
    horloge.avancer(2.0)                        # budget épuisé avant l'attente

    with pytest.raises(E.TabularLimitExceeded) as exc:
        with CAP.slot(d):
            pass
    assert exc.value.code == E.ERR_QUERY_TIMEOUT
    assert exc.value.details.get("phase") == "capacity_wait"


def test_le_refus_immediat_porte_son_propre_code(settings):
    """`tabular_capacity_exceeded` ≠ `query_timeout`.

    Il restait du budget ; c'est la capacité qui manquait. Confondre les deux rendrait
    la supervision illisible : on ne saurait plus s'il faut allonger les délais ou
    ajouter de la capacité.
    """
    settings.TABULAR_MAX_CONCURRENT_QUERIES = 1
    occupe = threading.Event()
    liberer = threading.Event()
    resultat: dict = {}

    def occupant():
        with CAP.slot(_deadline(30.0)):
            occupe.set()
            liberer.wait(timeout=10)

    def demandeur():
        occupe.wait(timeout=10)
        try:
            with CAP.slot(_deadline(30.0), wait=False):
                resultat["issue"] = "acquis"
        except E.TabularLimitExceeded as exc:
            resultat["issue"] = exc.code

    t1, t2 = threading.Thread(target=occupant), threading.Thread(target=demandeur)
    t1.start()
    t2.start()
    t2.join(timeout=10)
    liberer.set()
    t1.join(timeout=10)

    assert resultat["issue"] == E.ERR_TABULAR_CAPACITY_EXCEEDED


# --------------------------------------------------------------------------- #
# Libération : un slot perdu réduit la capacité DÉFINITIVEMENT
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("erreur", [
    E.TabularError("boom"),
    E.TabularClarificationRequired("ambiguous_column", "ambigu"),
    E.TabularPermissionDenied("permission_denied", "refusé"),
    E.TabularLimitExceeded("query_timeout", "expiré"),
    RuntimeError("panne inattendue"),
    KeyboardInterrupt(),
])
def test_le_slot_est_libere_quelle_que_soit_lissue(settings, erreur):
    settings.TABULAR_MAX_CONCURRENT_QUERIES = 2
    avant = CAP.get_limiter().active
    with pytest.raises(type(erreur)):
        with CAP.slot(_deadline(5.0)):
            raise erreur
    assert CAP.get_limiter().active == avant

    # Et la capacité reste utilisable : la requête suivante passe.
    with CAP.slot(_deadline(5.0)):
        pass
    assert CAP.get_limiter().active == avant


def test_une_double_liberation_est_un_bug_visible(settings):
    """`BoundedSemaphore` lève plutôt que de laisser un compteur dériver."""
    settings.TABULAR_MAX_CONCURRENT_QUERIES = 1
    limiteur = CAP.get_limiter()
    with CAP.slot(_deadline(5.0)):
        pass
    with pytest.raises(ValueError):
        limiteur._sem.release()


# --------------------------------------------------------------------------- #
# Portée — la propriété qu'il ne faut pas surestimer
# --------------------------------------------------------------------------- #
def test_la_capacite_effective_dit_la_verite_sur_sa_portee(settings):
    """Un sémaphore mémoire borne un PROCESSUS. gunicorn en lance plusieurs.

    Présenter la limite par processus comme une borne globale est un motif explicite
    de NO-GO. Ce test verrouille l'honnêteté du calcul.
    """
    settings.TABULAR_MAX_CONCURRENT_QUERIES = 2
    capacite = CAP.effective_capacity(workers=4)

    assert capacite["per_process_limit"] == 2
    assert capacite["workers"] == 4
    assert capacite["effective_total"] == 8        # et non 2
    assert capacite["scope"] == "process"
    assert capacite["globally_enforced"] is False


# --------------------------------------------------------------------------- #
# Compteurs
# --------------------------------------------------------------------------- #
def test_les_compteurs_restent_coherents(settings):
    settings.TABULAR_MAX_CONCURRENT_QUERIES = 2
    limiteur = CAP.get_limiter()
    assert limiteur.active >= 0
    assert limiteur.waiting >= 0

    with CAP.slot(_deadline(5.0)):
        assert 0 < limiteur.active <= limiteur.limit
    assert limiteur.active == 0
    assert limiteur.waiting == 0


def test_un_refus_incremente_le_compteur_une_seule_fois(settings):
    settings.TABULAR_MAX_CONCURRENT_QUERIES = 1
    occupe = threading.Event()
    liberer = threading.Event()

    def occupant():
        with CAP.slot(_deadline(30.0)):
            occupe.set()
            liberer.wait(timeout=10)

    t = threading.Thread(target=occupant)
    t.start()
    occupe.wait(timeout=10)
    with pytest.raises(E.TabularLimitExceeded):
        with CAP.slot(_deadline(30.0), wait=False):
            pass
    liberer.set()
    t.join(timeout=10)

    assert OBS.snapshot().get(OBS.CAPACITY_REJECTIONS) == 1
