"""Exécution d'un travail dans un processus RÉELLEMENT terminable.

## Pourquoi ce module existe

``limits.run_with_timeout`` rend la main à l'expiration du budget, mais **n'arrête
rien** : ``executor.shutdown(wait=False)`` abandonne le futur et le thread continue.
Python ne sait pas tuer un thread. Un document dont le budget d'extraction expire
laissait donc son extraction se poursuivre, à consommer un cœur, pendant que
l'appelant la déclarait échouée — jusqu'à ce que le worker Celery soit tué par sa
propre limite dure, emportant au passage les autres tâches en vol.

Un budget qui n'annule pas n'est pas un budget : c'est une mesure du temps qu'on
accepte d'attendre avant de mentir sur l'état du travail.

## La primitive retenue, et pourquoi les autres ne conviennent pas

Le worker d'ingestion tourne dans un pool **prefork** Celery, dont les processus sont
marqués ``daemon = True`` (billiard ``pool.py``). Mesuré dans cette topologie exacte :

===========================================  ==========================================
``multiprocessing.Process`` (bibliothèque standard)  REFUSÉ — ``daemonic processes are not
                                             allowed to have children``
``billiard.Process``                         autorisé
``subprocess``                               autorisé
``os.fork``                                  autorisé
===========================================  ==========================================

``billiard`` est déjà une dépendance dure de Celery : aucune dépendance nouvelle.
Le ``fork`` évite de redémarrer Django dans l'enfant — mesuré à 1,4–2,8 ms, contre
plusieurs secondes pour un interpréteur neuf.

## Tuer l'enfant ne suffit pas

L'extraction lance des sous-processus (Tesseract, poppler, convertisseurs). Mesuré :

* descendant ordinaire (Tesseract) : il HÉRITE du groupe de processus, donc un
  ``killpg`` l'emporte ;
* descendant qui s'isole lui-même avec ``start_new_session=True`` (c'est le cas du
  convertisseur DWG) : il **échappe** au groupe, et ``killpg`` seul le laisse vivant.

D'où la séquence retenue : on RELÈVE l'arbre des descendants *avant* de frapper — après,
le reparentage vers ``init`` a effacé la filiation — puis on frappe le groupe ET le
relevé. Les deux topologies sont alors couvertes, ce qui a été vérifié par PID.
"""
from __future__ import annotations

import logging
import os
import shutil
import signal
import tempfile
import threading
import time
from typing import Any, Callable, List, Optional

import billiard

logger = logging.getLogger(__name__)

#: Délai laissé à l'enfant pour sortir proprement sur SIGTERM avant le SIGKILL.
#: Court à dessein : le travail est déjà hors budget, et rien de ce que fait
#: l'extraction ne mérite d'être attendu — elle n'écrit aucune donnée persistante.
GRACE_PERIOD_S = 1.0

#: Attente maximale du `reap` après SIGKILL. Un processus tué est moissonné
#: immédiatement ; cette borne évite seulement de bloquer sur un cas pathologique.
REAP_TIMEOUT_S = 5.0


class ProcessTimeout(Exception):
    """Le budget a expiré et le travail a été TERMINÉ (≠ simplement abandonné)."""


class ProcessCrashed(Exception):
    """L'enfant est mort sans rendre de résultat, SANS que le budget ait expiré.

    Segfault d'une bibliothèque C, tueur de mémoire insuffisante, sortie brutale de
    l'interpréteur. Cette cause doit rester distincte d'un dépassement de délai :
    les confondre enverrait l'exploitant augmenter un budget qui n'est pas en cause.
    """

    def __init__(self, message: str, *, exitcode: Optional[int] = None):
        super().__init__(message)
        self.exitcode = exitcode


def _compter(nom: str) -> None:
    """Compteur d'exploitation entier. Ne lève jamais."""
    try:
        from apps.rag_core.services import ops_counters

        ops_counters.incr(nom)
    except Exception:  # noqa: BLE001 - l'observabilité ne fait jamais échouer l'extraction
        pass


def _descendants(pid: int) -> List[int]:
    """PID des descendants de ``pid``, relevés dans ``/proc``, transitivement.

    Lu dans le système de fichiers de processus, jamais par motif de ligne de commande :
    un motif attrape des processus homonymes qui ne nous appartiennent pas.
    """
    trouves: List[int] = []
    pile = [pid]
    while pile:
        courant = pile.pop()
        for entree in os.listdir("/proc"):
            if not entree.isdigit():
                continue
            fils = int(entree)
            if fils in trouves:
                continue
            try:
                with open(f"/proc/{fils}/stat") as fh:
                    # Le nom de commande peut contenir des espaces et des parenthèses :
                    # on découpe APRÈS la dernière ')' , sinon les champs glissent.
                    ppid = int(fh.read().rsplit(") ", 1)[1].split()[1])
            except (OSError, IndexError, ValueError):
                continue
            if ppid == courant:
                trouves.append(fils)
                pile.append(fils)
    return trouves


def _est_vivant(pid: int) -> bool:
    """Vrai si ``pid`` existe ET n'est pas un zombie.

    Un zombie répond au signal 0 : s'en contenter compterait comme « vivant » un
    processus déjà mort mais pas encore moissonné, et ferait échouer à tort la
    vérification d'absence d'orphelin.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(") ", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def _frapper(pid: int, pgid: Optional[int], sig: int, releve: List[int]) -> None:
    """Envoie ``sig`` au groupe, au relevé de descendants, puis à l'enfant lui-même."""
    if pgid and pgid != os.getpgrp():
        try:
            os.killpg(pgid, sig)
        except OSError:
            pass
    for p in releve:
        try:
            os.kill(p, sig)
        except OSError:
            pass
    try:
        os.kill(pid, sig)
    except OSError:
        pass


def _terminer(proc, pgid: Optional[int]) -> int:
    """Termine l'enfant, son groupe et ses descendants. Renvoie le nombre de survivants.

    Le relevé est pris AVANT la première frappe : une fois l'enfant mort, ses
    descendants sont reparentés à ``init`` et plus rien ne les rattache à nous.
    """
    pid = proc.pid
    if not pid:
        return 0
    releve = _descendants(pid)

    _frapper(pid, pgid, signal.SIGTERM, releve)
    fin = time.monotonic() + GRACE_PERIOD_S
    while time.monotonic() < fin:
        if not _est_vivant(pid) and not any(_est_vivant(p) for p in releve):
            break
        time.sleep(0.05)

    if _est_vivant(pid) or any(_est_vivant(p) for p in releve):
        _frapper(pid, pgid, signal.SIGKILL, releve)

    try:
        proc.join(timeout=REAP_TIMEOUT_S)   # moisson : pas de zombie laissé au worker
    except Exception:  # noqa: BLE001 - la moisson ne doit jamais masquer le timeout
        pass
    return sum(1 for p in [pid, *releve] if _est_vivant(p))


def _dans_l_enfant(conn, fn: Callable[[], Any], bac_temporaire: str) -> None:
    """Corps du processus enfant. Ne DOIT jamais écrire en base."""
    # Tous les temporaires de l'enfant — les siens, ceux de pytesseract, ceux des
    # sous-processus qui héritent de l'environnement — sont confinés dans un répertoire
    # que le PARENT possède et supprimera. Sans cela, une terminaison par signal
    # empêche les `finally` des bibliothèques de s'exécuter : mesuré, un dépassement
    # pendant Tesseract laissait derrière lui son PNG d'entrée et deux fichiers de
    # travail. Sur un worker de longue durée, cela s'accumule.
    os.environ["TMPDIR"] = bac_temporaire
    tempfile.tempdir = bac_temporaire

    try:
        # Chef de sa propre session : c'est ce qui donne au parent un groupe à frapper,
        # et ce qui évite d'emporter le worker Celery lui-même.
        os.setsid()
    except OSError:
        pass

    # Les connexions DB héritées du worker partagent la SOCKET du parent. Les fermer
    # proprement enverrait au serveur un message de terminaison qui casserait la
    # connexion du worker — mesuré. On ABANDONNE donc la référence sans y toucher :
    # si l'enfant avait besoin d'une base, il en ouvrirait une à lui.
    try:
        from django.db import connections

        for c in connections.all():
            c.connection = None
    except Exception:  # noqa: BLE001 - absence de DB configurée : rien à abandonner
        pass

    try:
        conn.send(("ok", fn()))
    except BaseException as exc:  # noqa: BLE001 - toute issue doit revenir au parent
        try:
            conn.send(("err", exc))
        except Exception:  # noqa: BLE001 - exception non transportable
            conn.send(("err_repr", f"{type(exc).__name__}: {exc}"))
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


#: Compteur d'exploitation : nombre de fois où l'isolation a été REFUSÉE faute de
#: pouvoir forker sans risque. Un correctif qui se désactive en silence est pire qu'un
#: correctif absent — on croit le problème réglé. Cette valeur le rend observable.
COUNTER_FORK_REFUSE = "extraction_cancellable_refused_total"


def fork_sans_danger() -> bool:
    """Peut-on forker DEPUIS ce processus sans risquer un interblocage ?

    ``fork()`` ne reproduit que le thread appelant. Un verrou détenu par un autre
    thread à cet instant n'est jamais relâché dans l'enfant, qui se bloque alors pour
    toujours. Python lui-même avertit : *« This process is multi-threaded, use of
    fork() may lead to deadlock »*.

    Ce n'est pas théorique : sur un processus à 4 threads se disputant un verrou,
    40 forks sur 40 ont produit un enfant bloqué. Or les deux contextes appelants
    diffèrent radicalement :

    * enfant du pool **prefork** Celery (ingestion) : MONO-thread — mesuré — donc sûr ;
    * worker **gunicorn** ``--threads 4`` (chemin web synchrone) : multi-thread, donc
      dangereux.

    On tranche sur l'état réel du processus, pas sur une supposition quant à l'endroit
    d'où l'on est appelé.
    """
    return threading.active_count() <= 1


def run_in_cancellable_process(fn: Callable[[], Any], timeout_s: Optional[float]):
    """Exécute ``fn()`` dans un processus terminable, sous un budget opposable.

    ``timeout_s`` nul ou négatif exécute ``fn`` en ligne : aucun budget, donc aucune
    raison de payer une frontière de processus.

    Lève :
      * ``ProcessTimeout`` — budget dépassé, travail réellement arrêté ;
      * ``ProcessCrashed`` — enfant mort sans résultat, budget NON dépassé ;
      * l'exception levée par ``fn`` — transportée telle quelle quand elle est
        sérialisable, ce qui préserve les catégories métier de l'appelant.
    """
    if not timeout_s or timeout_s <= 0:
        return fn()

    if not fork_sans_danger():
        # Se replier est ici le comportement SÛR : on retrouve exactement la sémantique
        # antérieure (budget respecté côté appelant, travail non arrêté). On le DIT,
        # parce qu'un repli muet laisserait croire que l'extraction est annulable alors
        # qu'elle ne l'est pas.
        logger.warning(
            "cancellable extraction unavailable: process has %s threads, "
            "fork would risk a deadlock; falling back to the soft timeout",
            threading.active_count(),
        )
        _compter(COUNTER_FORK_REFUSE)
        from .limits import PdfExtractionTimeout, run_with_timeout

        try:
            return run_with_timeout(fn, timeout_s)
        except PdfExtractionTimeout as e:
            raise ProcessTimeout(str(e)) from e

    ctx = billiard.get_context("fork")  # explicite : ne pas dépendre du défaut de Python
    bac = tempfile.mkdtemp(prefix="ragatm-extraction-")
    lecteur, ecrivain = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_dans_l_enfant, args=(ecrivain, fn, bac))
    proc.daemon = False  # un enfant daemonic ne pourrait pas lancer Tesseract
    proc.start()
    ecrivain.close()     # sinon le parent n'atteint jamais la fin de flux

    pgid = None
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pass

    try:
        return _attendre_resultat(proc, pgid, lecteur, timeout_s)
    finally:
        # L'enfant est mort (quelle qu'en soit la raison) avant d'arriver ici : plus
        # personne n'écrit dans le bac, sa suppression est sans course.
        shutil.rmtree(bac, ignore_errors=True)


def _attendre_resultat(proc, pgid, lecteur, timeout_s):
    """Attend la réponse de l'enfant, ou le termine si le budget expire."""
    try:
        pret = lecteur.poll(timeout_s)
    except Exception:  # noqa: BLE001 - tuyau cassé = enfant disparu
        pret = False

    if not pret:
        survivants = _terminer(proc, pgid)
        if survivants:
            # Dit plutôt que tu : une terminaison incomplète doit être visible.
            logger.error(
                "extraction process termination incomplete: %s survivor(s) after budget %ss",
                survivants, timeout_s,
            )
        raise ProcessTimeout(
            f"Budget d'extraction dépassé (> {timeout_s}s) : le travail a été arrêté."
        )

    try:
        statut, charge = lecteur.recv()
    except EOFError:
        # L'enfant a commencé à répondre puis est mort : ce n'est PAS un timeout.
        _terminer(proc, pgid)
        raise ProcessCrashed(
            "Le processus d'extraction s'est terminé sans rendre de résultat.",
            exitcode=proc.exitcode,
        ) from None
    finally:
        try:
            lecteur.close()
        except Exception:  # noqa: BLE001
            pass

    proc.join(timeout=REAP_TIMEOUT_S)
    if proc.is_alive():                      # a répondu mais ne sort pas : on le termine
        _terminer(proc, pgid)

    if statut == "ok":
        return charge
    if statut == "err":
        raise charge
    raise ProcessCrashed(f"Le processus d'extraction a échoué : {charge}")
