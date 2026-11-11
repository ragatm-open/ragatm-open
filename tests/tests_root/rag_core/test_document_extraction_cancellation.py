# tests/tests_root/rag_core/test_document_extraction_cancellation.py
"""
EXTRACTION-TIMEOUT-HARDENING-03 — le budget document arrête RÉELLEMENT le travail.

Le budget d'extraction reposait sur `run_with_timeout` : un `ThreadPoolExecutor` dont
on abandonne le futur. Python ne sait pas tuer un thread, donc l'appelant rendait la
main pendant que l'extraction continuait. Mesuré sur une planche A3 scannée, budget
2 s : **7,47 s de CPU brûlées dans les 6 s SUIVANT l'abandon**, 3 threads encore
vivants. Avec le processus annulable : **0,00 s**, 1 seul thread.

Ce qui est verrouillé ici :

- le travail est réellement terminé, avec ses descendants (Tesseract, rastériseur) ;
- un enfant mort sans résultat n'est PAS confondu avec un dépassement de délai ;
- les exceptions métier traversent la frontière sans perdre leur type ni leur code ;
- l'`ExtractionResult` traverse complètement, visuels et tableaux compris ;
- le fork est REFUSÉ depuis un processus multi-thread, où il provoquerait un
  interblocage — et ce refus est dit, jamais silencieux.

Les preuves d'absence d'orphelin portent sur des PID observés dans `/proc`, jamais sur
« le futur est terminé » : c'est précisément la confusion que ce lot corrige.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from io import BytesIO

import pytest

# RAGatm Open : moteur natif PyMuPDF = extra optionnel `pdf-mupdf` (AGPL), jamais installé par défaut.
fitz = pytest.importorskip("fitz")

from apps.rag_core.services.ingest import process_isolation as PI
from apps.rag_core.services.ingest.extraction import extract_document
from apps.rag_core.services.ingest.extraction.base import (
    ExtractionError,
    ExtractionTimeout,
)

# Budgets courts et déterministes : un harnais de 180 s ne prouverait rien de plus
# et rendrait la suite inutilisable (§13).
COURT = 1.0
LONG = 30.0


# =====================================================================
# Outils d'observation — le système de processus, pas les objets Python
# =====================================================================
def _vivant(pid: int) -> bool:
    """Vrai si le PID existe ET n'est pas un zombie (un zombie répond au signal 0)."""
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


def _espionner(monkeypatch) -> dict:
    """PID de l'enfant et bac temporaire créés PAR CET APPEL (et non par un autre worker xdist).

    L'enfant appelle `os.setsid()` : son PID devient l'identifiant de SESSION, conservé par tous ses
    descendants — même orphelins rattachés à init, même zombies. Restreindre les constats à cette session
    n'affaiblit donc pas la détection ; cela retire seulement les processus et bacs des tests concurrents
    (`/proc` et `/tmp` sont partagés entre workers), cause des échecs intermittents en CI."""
    vu: dict = {"enfants": [], "bacs": []}
    attendre, mkdtemp = PI._attendre_resultat, PI.tempfile.mkdtemp

    def _attendre(proc, *a, **kw):
        vu["enfants"].append(proc.pid)
        return attendre(proc, *a, **kw)

    def _mkdtemp(*a, **kw):
        d = mkdtemp(*a, **kw)
        vu["bacs"].append(d)
        return d

    monkeypatch.setattr(PI, "_attendre_resultat", _attendre)
    monkeypatch.setattr(PI.tempfile, "mkdtemp", _mkdtemp)
    return vu


def _pids_de_session(sid: int) -> set[int]:
    """PID (zombies compris) dont la session est `sid`, lus dans `/proc/<pid>/stat`."""
    out = set()
    for e in os.listdir("/proc"):
        if not e.isdigit():
            continue
        try:
            with open(f"/proc/{e}/stat") as fh:
                champs = fh.read().rsplit(") ", 1)[1].split()
            if int(champs[3]) == sid:  # état, ppid, pgrp, SESSION
                out.add(int(e))
        except (OSError, IndexError, ValueError):
            pass
    return out


def _pids_par_nom(noms: set[str]) -> set[int]:
    """PID dont `/proc/<pid>/comm` est dans `noms`. Jamais par motif de ligne de
    commande : un motif attrape des processus homonymes qui ne nous appartiennent pas."""
    out = set()
    for e in os.listdir("/proc"):
        if not e.isdigit():
            continue
        try:
            with open(f"/proc/{e}/comm") as fh:
                if fh.read().strip() in noms:
                    out.add(int(e))
        except OSError:
            pass
    return out


def _cpu_total() -> float:
    t = os.times()
    return t.user + t.system + t.children_user + t.children_system


# =====================================================================
# 1. Fonctionnement nominal
# =====================================================================
def test_un_travail_qui_tient_dans_le_budget_rend_son_resultat():
    assert PI.run_in_cancellable_process(lambda: 21 * 2, LONG) == 42


def test_le_travail_s_execute_bien_dans_UN_AUTRE_processus():
    """Sans quoi rien de ce qui suit n'aurait de sens : un travail resté dans le
    processus courant ne peut pas être tué."""
    enfant = PI.run_in_cancellable_process(os.getpid, LONG)
    assert enfant != os.getpid()


def test_budget_nul_execute_en_ligne_sans_payer_la_frontiere():
    """Aucun budget = rien à faire respecter, donc aucune raison de forker."""
    assert PI.run_in_cancellable_process(os.getpid, 0) == os.getpid()


# =====================================================================
# 2. Le budget arrête vraiment le travail
# =====================================================================
def _dormeur():
    time.sleep(120)


def test_le_budget_depasse_leve_un_timeout():
    t0 = time.monotonic()
    with pytest.raises(PI.ProcessTimeout):
        PI.run_in_cancellable_process(_dormeur, COURT)
    assert time.monotonic() - t0 < COURT + 8


def _enfant_avec_descendant(fichier):
    """Lance un descendant, note son PID, puis dort — topologie Tesseract."""
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    open(fichier, "w").write(str(p.pid))
    time.sleep(120)


def test_aucun_processus_enfant_ni_descendant_ne_survit(tmp_path, monkeypatch):
    """LE test du lot. La fenêtre T+2 s est celle du contrat : un processus tué mais
    pas encore moissonné ne doit pas passer pour vivant, ni l'inverse."""
    fichier = tmp_path / "descendant.pid"
    vu = _espionner(monkeypatch)

    with pytest.raises(PI.ProcessTimeout):
        PI.run_in_cancellable_process(lambda: _enfant_avec_descendant(str(fichier)), COURT)

    assert fichier.exists(), "le descendant n'a jamais démarré : le test ne prouverait rien"
    pid_descendant = int(fichier.read_text())

    time.sleep(2.0)
    assert not _vivant(pid_descendant), "descendant abandonné : le killpg n'a pas porté"
    [enfant] = vu["enfants"]
    survivants = _pids_de_session(enfant)
    assert not survivants, f"processus survivants : {sorted(survivants)}"


def test_un_descendant_qui_s_isole_est_tout_de_meme_termine(tmp_path):
    """Un descendant lancé avec `start_new_session=True` ÉCHAPPE au groupe de
    processus : c'est le cas du convertisseur DWG. Un `killpg` seul le laisserait
    vivant — mesuré. Le relevé de l'arbre avant la frappe est ce qui le rattrape."""
    fichier = tmp_path / "isole.pid"

    def _travail():
        p = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True
        )
        open(fichier, "w").write(str(p.pid))
        time.sleep(120)

    with pytest.raises(PI.ProcessTimeout):
        PI.run_in_cancellable_process(_travail, COURT)

    assert fichier.exists()
    pid = int(fichier.read_text())
    time.sleep(2.0)
    vivant = _vivant(pid)
    if vivant:  # nettoyage avant de faire échouer, pour ne pas laisser de résidu
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    assert not vivant, "descendant isolé par setsid laissé vivant"


def test_plus_aucun_cpu_n_est_brule_apres_le_rendu():
    """La différence de fond avec l'ancien mécanisme : il rendait la main SANS arrêter
    le travail. Un budget qui n'annule pas n'est pas un budget."""
    def _brule():
        fin = time.monotonic() + 30
        x = 0
        while time.monotonic() < fin:
            x += 1
        return x

    with pytest.raises(PI.ProcessTimeout):
        PI.run_in_cancellable_process(_brule, COURT)

    c0 = _cpu_total()
    time.sleep(2.0)
    assert _cpu_total() - c0 < 0.5, "du travail abandonné consomme encore du processeur"


# =====================================================================
# 3. Une mort n'est pas un dépassement
# =====================================================================
def _suicide():
    os._exit(9)          # sortie brutale : ni exception, ni résultat


def test_un_enfant_mort_sans_resultat_n_est_pas_un_timeout():
    """Confondre les deux enverrait l'exploitant relever un budget hors de cause,
    et masquerait un segfault ou une mémoire insuffisante."""
    with pytest.raises(PI.ProcessCrashed) as exc:
        PI.run_in_cancellable_process(_suicide, LONG)
    assert not isinstance(exc.value, PI.ProcessTimeout)
    assert exc.value.exitcode is not None


def _tue_moi():
    os.kill(os.getpid(), signal.SIGKILL)


def test_un_enfant_tue_par_le_systeme_est_signale_comme_tel():
    with pytest.raises(PI.ProcessCrashed):
        PI.run_in_cancellable_process(_tue_moi, LONG)


# =====================================================================
# 4. Transport des exceptions et du résultat
# =====================================================================
def _leve_metier():
    raise ExtractionError("scanné", code="pdf_scanned_requires_ocr", meta={"pages": 3})


def test_une_exception_metier_traverse_sans_perdre_son_type_ni_son_code():
    with pytest.raises(ExtractionError) as exc:
        PI.run_in_cancellable_process(_leve_metier, LONG)
    assert exc.value.code == "pdf_scanned_requires_ocr"
    assert exc.value.meta == {"pages": 3}


def _leve_imprevue():
    raise ValueError("imprévue")


def test_une_exception_imprevue_reste_distincte_du_timeout():
    with pytest.raises(ValueError):
        PI.run_in_cancellable_process(_leve_imprevue, LONG)


def test_l_exception_de_limite_d_upload_traverse_la_frontiere():
    """`UploadLimitExceeded.__init__` exige DEUX arguments alors que `args` n'en portait
    qu'un : le dépicklage échouait. Sans le correctif, « PDF trop volumineux » revenait
    à l'appelant comme une panne générique."""
    from apps.rag_core.services.ingest.limits import UploadLimitExceeded

    def _leve():
        raise UploadLimitExceeded("pdf_too_many_pages", "1200 pages")

    with pytest.raises(UploadLimitExceeded) as exc:
        PI.run_in_cancellable_process(_leve, LONG)
    assert exc.value.code == "pdf_too_many_pages"
    assert exc.value.detail == "1200 pages"


# =====================================================================
# 5. Sécurité du fork
# =====================================================================
def test_le_fork_est_refuse_depuis_un_processus_multi_thread(caplog):
    """`fork()` ne reproduit que le thread appelant : un verrou détenu ailleurs n'est
    jamais relâché dans l'enfant. Mesuré sur 4 threads en contention : 40 enfants
    bloqués sur 40. Le repli est SÛR (sémantique antérieure) et doit être DIT."""
    barriere = threading.Event()
    t = threading.Thread(target=barriere.wait, daemon=True)
    t.start()
    try:
        assert PI.fork_sans_danger() is False
        with caplog.at_level("WARNING"):
            pid = PI.run_in_cancellable_process(os.getpid, LONG)
        assert pid == os.getpid(), "un fork a eu lieu malgré le risque d'interblocage"
        assert any("fork would risk a deadlock" in r.getMessage() for r in caplog.records)
    finally:
        barriere.set()
        t.join(timeout=5)


def test_le_fork_est_autorise_depuis_un_processus_mono_thread():
    assert PI.fork_sans_danger() is True
    assert PI.run_in_cancellable_process(os.getpid, LONG) != os.getpid()


# =====================================================================
# 6. Chemin réel d'extraction
# =====================================================================
def _pdf(pages: list[str]) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    b = BytesIO()
    c = canvas.Canvas(b, pagesize=A4)
    for t in pages:
        y = 800
        for l in t.split("\n"):
            c.drawString(60, y, l)
            y -= 14
        c.showPage()
    c.save()
    return b.getvalue()


_PAVE = "\n".join(
    f"Article {i} - fourniture et pose, beton arme, coffrage soigne, ligne {i}."
    for i in range(1, 15)
)


def test_extraction_reelle_reussie_a_travers_la_frontiere():
    r = extract_document(_pdf([_PAVE]), filename="p.pdf", content_type="application/pdf")
    assert "Article 1" in r.text
    assert r.metadata["pages"] == 1


def test_le_resultat_traverse_COMPLET_visuels_compris():
    """`VisualAsset` porte des octets d'image. Perdre un champ au passage de la
    frontière produirait un document indexé silencieusement appauvri."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (600, 400), "white")
    d = ImageDraw.Draw(im)
    for i in range(0, 600, 40):
        d.line([(i, 0), (i, 400)], fill="black", width=2)
    b = BytesIO()
    c = canvas.Canvas(b, pagesize=A4)
    c.drawString(60, 800, "Coupe transversale, appuis A12 a A18, voir planche.")
    c.drawImage(ImageReader(im), 40, 200, width=400, height=260)
    c.showPage()
    c.save()

    r = extract_document(b.getvalue(), filename="planche.pdf",
                         content_type="application/pdf",
                         visual_sources=frozenset({"pdf"}))
    assert r.visual_assets, "aucun visuel n'a traversé la frontière"
    a = r.visual_assets[0]
    assert isinstance(a.content, bytes) and len(a.content) > 0
    assert a.content_hash and a.mime_type
    assert r.metadata["pages"] == 1


def test_timeout_document_donne_extraction_timeout(settings, monkeypatch):
    """Le code métier ne change pas parce que la primitive change (§23).

    L'extracteur est ralenti volontairement : le but est de verrouiller le MAPPING du
    dépassement vers `extraction_timeout` de façon déterministe. La preuve sur un
    document réellement lent (planche A3 scannée, OCR en cours) est conduite hors
    suite : 2,07 s de rendu, `extraction_timeout`, zéro orphelin.
    """
    from apps.rag_core.services.ingest.extraction import _REGISTRY, FMT_PDF

    settings.DOCUMENT_OCR_ENABLED = False

    def _lent(self, data, ctx):
        time.sleep(60)

    monkeypatch.setattr(type(_REGISTRY[FMT_PDF]), "extract", _lent)
    t0 = time.monotonic()
    with pytest.raises(ExtractionTimeout) as exc:
        extract_document(_pdf([_PAVE]), filename="p.pdf",
                         content_type="application/pdf", timeout_seconds=1)
    assert exc.value.code == "extraction_timeout"
    assert time.monotonic() - t0 < 12, "le budget n'a pas été rendu à temps"


def test_le_budget_document_est_bien_transporte_par_appel(settings):
    """Un budget large laisse passer ce qu'un budget court refuse : c'est ce qui rend
    la reprise « délai étendu » opérante sur la nouvelle primitive."""
    settings.DOCUMENT_OCR_ENABLED = False
    data = _pdf([_PAVE] * 3)
    r = extract_document(data, filename="p.pdf", content_type="application/pdf",
                         timeout_seconds=120)
    assert r.metadata["pages"] == 3


def test_un_crash_du_chemin_reel_porte_son_propre_code(monkeypatch):
    """`extraction_worker_crashed` ≠ `extraction_timeout` : le budget n'est pas en cause."""
    from apps.rag_core.services.ingest.extraction import _REGISTRY, FMT_PDF

    def _plante(self, data, ctx):
        os._exit(9)

    monkeypatch.setattr(type(_REGISTRY[FMT_PDF]), "extract", _plante)
    with pytest.raises(ExtractionError) as exc:
        extract_document(_pdf([_PAVE]), filename="p.pdf", content_type="application/pdf")
    assert exc.value.code == "extraction_worker_crashed"


# =====================================================================
# 7. Le processus appelant reste sain
# =====================================================================
def test_le_processus_appelant_reste_utilisable_apres_plusieurs_timeouts():
    """Un worker Celery enchaîne les documents : si un timeout le laissait abîmé, le
    correctif déplacerait la panne au lieu de la corriger."""
    for _ in range(3):
        with pytest.raises(PI.ProcessTimeout):
            PI.run_in_cancellable_process(_dormeur, COURT)
    assert PI.run_in_cancellable_process(lambda: 7 * 6, LONG) == 42


@pytest.mark.django_db
def test_la_connexion_base_du_parent_survit_a_la_terminaison_de_l_enfant():
    """L'enfant HÉRITE la socket du parent. La refermer proprement enverrait au serveur
    un message de terminaison qui casserait la connexion du worker — mesuré. L'enfant
    doit donc ABANDONNER la référence, jamais la fermer."""
    from django.db import connections

    with connections["default"].cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone()[0] == 1

    with pytest.raises(PI.ProcessTimeout):
        PI.run_in_cancellable_process(_dormeur, COURT)

    with connections["default"].cursor() as cur:
        cur.execute("SELECT 42")
        assert cur.fetchone()[0] == 42


def test_aucun_zombie_n_est_laisse_derriere(tmp_path, monkeypatch):
    """Un enfant tué mais non moissonné reste en zombie et consomme une entrée de
    table de processus : sur un worker de longue durée, cela finit par mordre."""
    vu = _espionner(monkeypatch)
    with pytest.raises(PI.ProcessTimeout):
        PI.run_in_cancellable_process(_dormeur, COURT)
    time.sleep(1.0)
    [enfant] = vu["enfants"]
    restants = _pids_de_session(enfant)  # un zombie garde sa session : il serait compté
    assert not restants, f"processus non moissonnés : {sorted(restants)}"


# =====================================================================
# 8. Non-régression de la reprise
# =====================================================================
def test_le_mapping_de_reprise_du_timeout_document_est_conserve():
    """`extraction_timeout → extended_timeout` doit continuer de fonctionner : changer
    la primitive d'annulation ne doit pas changer la décision métier (§35)."""
    from types import SimpleNamespace

    from apps.rag_core.models.document import DocumentStatus
    from apps.rag_core.services.ingest import retry_strategy as rs

    d = SimpleNamespace(
        id="D1", status=DocumentStatus.FAILED, filename="p.pdf",
        content_type="application/pdf", error_message="",
        meta={"extraction": {"error_code": "extraction_timeout"}},
    )
    retryabilite, code, strategie = rs.classify_retryability(d)
    assert code == "extraction_timeout"
    assert retryabilite == rs.RETRYABLE_WITH_STRATEGY
    assert strategie == rs.STRATEGY_EXTENDED_TIMEOUT


def test_un_crash_de_worker_n_est_pas_traite_comme_un_timeout_a_la_reprise():
    """Proposer « délai étendu » sur un segfault promettrait un effet inexistant."""
    from apps.rag_core.services.ingest import retry_strategy as rs

    assert "extraction_worker_crashed" not in rs.STRATEGY_BY_ERROR_CODE


# =====================================================================
# 9. Fichiers temporaires
# =====================================================================
def test_les_temporaires_de_l_enfant_sont_nettoyes_meme_apres_une_terminaison(tmp_path, monkeypatch):
    """Une terminaison par signal empêche les `finally` des bibliothèques de tourner :
    mesuré, un dépassement pendant Tesseract laissait son PNG d'entrée et deux fichiers
    de travail. Confiner les temporaires de l'enfant dans un répertoire que le PARENT
    possède rend le nettoyage indépendant de la façon dont l'enfant meurt."""
    vu = _espionner(monkeypatch)

    def _salit():
        import tempfile as t

        d = t.mkdtemp()
        with open(os.path.join(d, "gros.bin"), "wb") as fh:
            fh.write(b"x" * 100000)
        time.sleep(120)

    with pytest.raises(PI.ProcessTimeout):
        PI.run_in_cancellable_process(_salit, COURT)

    time.sleep(1.0)
    restes = [d for d in vu["bacs"] if os.path.exists(d)]
    assert vu["bacs"] and not restes, f"bacs temporaires laissés derrière : {restes}"


def test_le_bac_temporaire_est_aussi_nettoye_apres_un_succes(monkeypatch):
    vu = _espionner(monkeypatch)
    assert PI.run_in_cancellable_process(lambda: 1, LONG) == 1
    assert vu["bacs"] and not [d for d in vu["bacs"] if os.path.exists(d)]
