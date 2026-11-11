# tests/tests_root/rag_core/test_ocr_page_timeout.py
"""
OCR-TIMEOUT-HARDENING-02 — OCR de page réellement annulable, et sémantique d'échec.

Trois défauts distincts sont verrouillés ici.

**1. Le budget n'annulait rien.** `run_with_timeout` rend la main à l'appelant mais
n'arrête pas le travail : `executor.shutdown(wait=False)` abandonne le futur, le
processus Tesseract continue. Mesuré : +24 s de CPU APRÈS l'abandon, processus vivant
18 s au-delà du budget. Le budget est désormais passé à Tesseract lui-même, qui termine
son processus.

**2. Le budget se réarmait à chaque sous-appel.** Une page traverse trois appels OCR
(OSD, texte, confiance). Chacun recevant le budget complet, une page pouvait consommer
trois fois le budget annoncé. Le budget est maintenant une ÉCHÉANCE partagée.

**3. Toute exception devenait « timeout ».** Un `except Exception` unique étiquetait
`ocr_page_timeout` une image corrompue comme un vrai dépassement — et envoyait
l'exploitant augmenter un budget qui n'était pas en cause.

Ces tests remplacent `pytesseract` lui-même, jamais le provider : c'est la seule façon
d'exercer le vrai `_TesseractProvider`, donc le contrat réellement en service.
"""
from __future__ import annotations

import sys
import time
from io import BytesIO

import pytest

from apps.rag_core.services.ingest.extraction import extract_document
from apps.rag_core.services.ingest.extraction import ocr as ocr_mod
from apps.rag_core.services.ingest.extraction.base import ExtractionError

_TEXTE = "PLANCHE E03 - nomenclature\nAppui A12, charge 240 kN."
_MSG_TIMEOUT = "Tesseract process timeout"


class _Faux:
    """`pytesseract` factice : compte les appels, MÉMORISE les budgets reçus, et peut
    consommer du temps ou échouer sur commande."""

    class Output:
        DICT = "dict"

    def __init__(self, *, osd_sleep=0.0, string_exc=None, osd_exc=None, texte=_TEXTE):
        self.appels = {"osd": 0, "string": 0, "data": 0}
        self.budgets = {"osd": None, "string": None, "data": None}
        self._osd_sleep = osd_sleep
        self._string_exc = string_exc
        self._osd_exc = osd_exc
        self._texte = texte

    def image_to_osd(self, im, output_type=None, timeout=0):
        self.appels["osd"] += 1
        self.budgets["osd"] = timeout
        if self._osd_sleep:
            time.sleep(self._osd_sleep)
        if self._osd_exc:
            raise self._osd_exc
        return {"rotate": 0, "orientation_conf": 0.0}

    def image_to_string(self, im, lang=None, timeout=0):
        self.appels["string"] += 1
        self.budgets["string"] = timeout
        if self._string_exc:
            raise self._string_exc
        return self._texte

    def image_to_data(self, im, lang=None, output_type=None, timeout=0):
        self.appels["data"] += 1
        self.budgets["data"] = timeout
        return {
            "conf": ["-1", "88"], "text": ["", "APPUI"],
            "block_num": [0, 1], "par_num": [0, 1], "line_num": [0, 1],
            "left": [0, 10], "top": [0, 20], "width": [0, 40], "height": [0, 12],
        }


@pytest.fixture
def faux(monkeypatch):
    def _poser(**kw):
        f = _Faux(**kw)
        monkeypatch.setitem(sys.modules, "pytesseract", f)
        return f
    return _poser


def _png() -> bytes:
    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (60, 40), "white").save(buf, format="PNG")
    return buf.getvalue()


def _pdf(pages_texte: list[str]) -> bytes:
    """PDF réel. Une page avec `""` n'a aucune couche texte : l'OCR s'y déclenche."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for txt in pages_texte:
        if txt:
            y = 800
            for ligne in txt.split("\n"):
                c.drawString(60, y, ligne)
                y -= 14
        c.showPage()
    c.save()
    return buf.getvalue()


_PAVE = "\n".join(
    f"Article {i} - fourniture et pose, beton arme, coffrage soigne, ligne {i}."
    for i in range(1, 12)
)


# =====================================================================
# 1. Le budget de page est TOTAL, pas par sous-appel
# =====================================================================
def test_budget_partage_entre_osd_et_texte(faux, settings):
    """Le temps consommé par l'OSD est DÉDUIT du budget laissé au texte.

    C'est le cœur du défaut n°2 : avec un budget rearmé, `string` recevrait 10.
    """
    settings.DOCUMENT_OCR_AUTO_ROTATE = True
    settings.DOCUMENT_OCR_COMPUTE_CONFIDENCE = False
    f = faux(osd_sleep=0.6)
    ocr_mod._TesseractProvider().ocr_image_bytes(_png(), languages="fra", budget_s=10)

    assert f.budgets["osd"] == pytest.approx(10, abs=0.2)
    assert f.budgets["string"] < 9.6, "le budget a été réarmé au lieu d'être partagé"
    assert f.budgets["string"] > 0


def test_budget_partage_avec_la_confiance(faux, settings):
    """Le troisième appel hérite de ce qui reste, il ne repart pas du budget complet."""
    settings.DOCUMENT_OCR_AUTO_ROTATE = True
    settings.DOCUMENT_OCR_COMPUTE_CONFIDENCE = True
    f = faux(osd_sleep=0.5)
    ocr_mod._TesseractProvider().ocr_image_bytes(_png(), languages="fra", budget_s=10)

    assert f.budgets["data"] <= f.budgets["string"] <= f.budgets["osd"]


def test_budget_absent_ne_transmet_aucune_echeance(faux, settings):
    """Sans budget explicite, aucun `timeout` contraignant n'est imposé : la
    compatibilité ascendante des appels internes est préservée."""
    settings.DOCUMENT_OCR_AUTO_ROTATE = True
    f = faux()
    ocr_mod._TesseractProvider().ocr_image_bytes(_png(), languages="fra")

    assert f.budgets["string"] == 0, "0 = pas de limite, convention pytesseract"


def test_budget_epuise_avant_le_texte_n_appelle_pas_tesseract(faux, settings):
    """Budget déjà consommé par l'OSD : on ne lance PAS un appel voué à l'échec.

    Lancer `image_to_string` avec un budget négatif ou nul démarrerait un processus
    Tesseract complet sans limite — exactement le comportement que ce lot supprime.
    """
    settings.DOCUMENT_OCR_AUTO_ROTATE = True
    f = faux(osd_sleep=0.4)
    with pytest.raises(ocr_mod.OcrPageTimeout):
        ocr_mod._TesseractProvider().ocr_image_bytes(
            _png(), languages="fra", budget_s=0.3
        )
    assert f.appels["string"] == 0, "aucun appel Tesseract ne doit être lancé hors budget"


# =====================================================================
# 2. Un vrai dépassement est distingué d'une autre défaillance
# =====================================================================
def test_timeout_tesseract_devient_OcrPageTimeout(faux, settings):
    settings.DOCUMENT_OCR_COMPUTE_CONFIDENCE = False
    faux(string_exc=RuntimeError(_MSG_TIMEOUT))
    with pytest.raises(ocr_mod.OcrPageTimeout):
        ocr_mod._TesseractProvider().ocr_image_bytes(_png(), languages="fra", budget_s=30)


def test_autre_runtimeerror_n_est_pas_un_timeout(faux, settings):
    """`RuntimeError` n'est pas synonyme de dépassement : seul le message du gestionnaire
    de délai de pytesseract l'est. Confondre les deux recréerait le défaut n°3."""
    settings.DOCUMENT_OCR_COMPUTE_CONFIDENCE = False
    faux(string_exc=RuntimeError("tesseract: invalid page segmentation mode"))
    with pytest.raises(RuntimeError) as exc:
        ocr_mod._TesseractProvider().ocr_image_bytes(_png(), languages="fra", budget_s=30)
    assert not isinstance(exc.value, ocr_mod.OcrPageTimeout)


def test_erreur_non_runtimeerror_remonte_telle_quelle(faux, settings):
    settings.DOCUMENT_OCR_COMPUTE_CONFIDENCE = False
    faux(string_exc=ValueError("image corrompue"))
    with pytest.raises(ValueError):
        ocr_mod._TesseractProvider().ocr_image_bytes(_png(), languages="fra", budget_s=30)


def test_timeout_OSD_n_est_pas_degrade_en_silence(faux, settings):
    """Une erreur OSD ordinaire dégrade vers « pas de rotation » — c'est voulu. Un
    dépassement de budget, lui, doit remonter : le budget de la page est épuisé, et
    enchaîner sur le texte le ferait déborder."""
    settings.DOCUMENT_OCR_AUTO_ROTATE = True
    f = faux(osd_exc=RuntimeError(_MSG_TIMEOUT))
    with pytest.raises(ocr_mod.OcrPageTimeout):
        ocr_mod._TesseractProvider().ocr_image_bytes(_png(), languages="fra", budget_s=30)
    assert f.appels["string"] == 0


def test_erreur_OSD_ordinaire_degrade_toujours(faux, settings):
    """Le comportement historique best-effort de l'OSD est préservé."""
    settings.DOCUMENT_OCR_AUTO_ROTATE = True
    settings.DOCUMENT_OCR_COMPUTE_CONFIDENCE = False
    f = faux(osd_exc=ValueError("osd indisponible"))
    texte, _, rot = ocr_mod._TesseractProvider().ocr_image_bytes(
        _png(), languages="fra", budget_s=30
    )
    assert rot == 0
    assert texte == _TEXTE
    assert f.appels["string"] == 1


def test_confiance_sautee_si_budget_epuise_sans_perdre_le_texte(faux, settings):
    """Le texte est déjà produit : un budget épuisé au moment de la confiance ne doit
    pas jeter le travail utile. La confiance est absente, jamais inventée."""
    settings.DOCUMENT_OCR_AUTO_ROTATE = False
    settings.DOCUMENT_OCR_COMPUTE_CONFIDENCE = True

    class _Lent(_Faux):
        def image_to_string(self, im, lang=None, timeout=0):
            self.appels["string"] += 1
            self.budgets["string"] = timeout
            time.sleep(0.5)
            return self._texte

    f = _Lent()
    sys.modules["pytesseract"] = f
    try:
        texte, conf, _ = ocr_mod._TesseractProvider().ocr_image_bytes(
            _png(), languages="fra", budget_s=0.4
        )
    finally:
        del sys.modules["pytesseract"]

    assert texte == _TEXTE
    assert conf is None
    assert f.appels["data"] == 0


# =====================================================================
# 3. Orchestration par page : deux causes, deux warnings
# =====================================================================
def _pages(**kw):
    return ocr_mod.ocr_pdf_pages(_pdf(["", ""]), [1, 2], languages="fra", **kw)


def test_page_en_timeout_produit_ocr_page_timeout(faux, settings):
    settings.DOCUMENT_OCR_ENABLED = True
    settings.DOCUMENT_OCR_COMPUTE_CONFIDENCE = False
    faux(string_exc=RuntimeError(_MSG_TIMEOUT))
    res = _pages(page_timeout_s=30)

    assert "ocr_page_timeout" in res.warnings
    assert "ocr_page_failed" not in res.warnings
    assert res.timeout_pages == [1, 2]
    assert res.failed_pages == []
    assert res.partial is True


def test_page_en_erreur_n_est_plus_etiquetee_timeout(faux, settings):
    """LE défaut corrigé : une image corrompue ne doit plus se présenter comme un
    dépassement de délai, sans quoi l'exploitant augmente un budget hors de cause."""
    settings.DOCUMENT_OCR_ENABLED = True
    settings.DOCUMENT_OCR_COMPUTE_CONFIDENCE = False
    faux(string_exc=ValueError("image corrompue"))
    res = _pages(page_timeout_s=30)

    assert "ocr_page_failed" in res.warnings
    assert "ocr_page_timeout" not in res.warnings
    assert res.failed_pages == [1, 2]
    assert res.timeout_pages == []


def test_les_deux_causes_coexistent_sans_se_masquer(settings):
    """Un document peut porter les deux : les listes de pages les gardent distinctes
    là où un warning dédupliqué les confondrait."""
    settings.DOCUMENT_OCR_ENABLED = True
    settings.DOCUMENT_OCR_COMPUTE_CONFIDENCE = False

    class _Alterne(_Faux):
        def image_to_string(self, im, lang=None, timeout=0):
            self.appels["string"] += 1
            if self.appels["string"] == 1:
                raise RuntimeError(_MSG_TIMEOUT)
            raise ValueError("image corrompue")

    sys.modules["pytesseract"] = _Alterne()
    try:
        res = _pages(page_timeout_s=30)
    finally:
        del sys.modules["pytesseract"]

    assert res.timeout_pages == [1]
    assert res.failed_pages == [2]
    assert {"ocr_page_timeout", "ocr_page_failed"} <= set(res.warnings)


def test_une_page_perdue_ne_perd_pas_les_autres(settings):
    """Convention du pipeline préservée : la boucle continue après une page perdue."""
    settings.DOCUMENT_OCR_ENABLED = True
    settings.DOCUMENT_OCR_COMPUTE_CONFIDENCE = False

    class _PremiereEchoue(_Faux):
        def image_to_string(self, im, lang=None, timeout=0):
            self.appels["string"] += 1
            if self.appels["string"] == 1:
                raise RuntimeError(_MSG_TIMEOUT)
            return _TEXTE

    sys.modules["pytesseract"] = _PremiereEchoue()
    try:
        res = _pages(page_timeout_s=30)
    finally:
        del sys.modules["pytesseract"]

    assert [p.page_number for p in res.pages] == [2]
    assert res.timeout_pages == [1]
    assert _TEXTE in res.text


def test_le_budget_par_appel_prime_sur_la_configuration(faux, settings):
    settings.DOCUMENT_OCR_ENABLED = True
    settings.DOCUMENT_OCR_TIMEOUT_SECONDS = 60
    settings.DOCUMENT_OCR_COMPUTE_CONFIDENCE = False
    settings.DOCUMENT_OCR_AUTO_ROTATE = False
    f = faux()
    ocr_mod.ocr_pdf_pages(_pdf([""]), [1], languages="fra", page_timeout_s=11)

    assert f.budgets["string"] == pytest.approx(11, abs=0.5)


def test_sans_budget_par_appel_la_configuration_s_applique(faux, settings):
    settings.DOCUMENT_OCR_ENABLED = True
    settings.DOCUMENT_OCR_TIMEOUT_SECONDS = 23
    settings.DOCUMENT_OCR_COMPUTE_CONFIDENCE = False
    settings.DOCUMENT_OCR_AUTO_ROTATE = False
    f = faux()
    ocr_mod.ocr_pdf_pages(_pdf([""]), [1], languages="fra")

    assert f.budgets["string"] == pytest.approx(23, abs=0.5)


def test_le_filet_de_second_niveau_depasse_strictement_le_budget_natif():
    """Deux délais réglés à la même valeur créeraient une course dont l'issue déciderait
    du code d'erreur affiché. Le filet doit perdre cette course par construction."""
    assert ocr_mod._FILET_MARGE_S > 0


# =====================================================================
# 4. Conséquence documentaire : statut et error_code
# =====================================================================
def _extrait(pdf_pages, faux_kw, settings, **kw):
    settings.DOCUMENT_OCR_ENABLED = True
    settings.DOCUMENT_OCR_COMPUTE_CONFIDENCE = False
    settings.DOCUMENT_OCR_AUTO_ROTATE = False
    sys.modules["pytesseract"] = _Faux(**faux_kw)
    try:
        return extract_document(
            _pdf(pdf_pages), filename="planche.pdf",
            content_type="application/pdf", ocr_enabled=True, **kw
        )
    finally:
        del sys.modules["pytesseract"]


def test_document_sans_texte_et_en_timeout_echoue_explicitement(settings):
    """Sans cela le document tombe en `ocr_no_text` — « illisible » — alors qu'il n'a
    simplement jamais été lu jusqu'au bout."""
    with pytest.raises(ExtractionError) as exc:
        _extrait([""], {"string_exc": RuntimeError(_MSG_TIMEOUT)}, settings)

    assert exc.value.code == "ocr_page_timeout"
    assert exc.value.meta["ocr_timeout_pages"] == [1]


def test_document_avec_texte_natif_ne_echoue_pas_sur_une_page_expiree(settings):
    """Le succès PARTIEL multipage est légitime et doit être PRÉSERVÉ, pas seulement
    « non cassé » : 1 page sur 2 expire, l'autre porte du texte, le document vit."""
    r = _extrait([_PAVE, ""], {"string_exc": RuntimeError(_MSG_TIMEOUT)}, settings)

    assert "Article 1" in r.text
    assert r.metadata["ocr_timeout_pages"] == [2]
    assert "ocr_page_timeout" in r.warnings
    assert r.partial is True


def test_echec_sur_page_non_timeout_porte_le_bon_code(settings):
    """Aucun texte, mais la cause n'est PAS un dépassement : le code historique
    `ocr_no_text` reste le bon. Le nouveau code ne doit pas tout absorber."""
    with pytest.raises(ExtractionError) as exc:
        _extrait([""], {"string_exc": ValueError("image corrompue")}, settings)

    assert exc.value.code == "ocr_no_text"


def test_le_rail_visuel_ne_sauve_pas_un_document_jamais_lu(settings):
    """Rail visuel actif, OCR expiré, aucun texte : le document devait devenir READY
    grâce aux seuls chunks visuels, indistinguable d'un document réellement sans texte
    — donc jamais repris. Il échoue désormais, avec un code qui dit quoi faire."""
    with pytest.raises(ExtractionError) as exc:
        _extrait(
            [""], {"string_exc": RuntimeError(_MSG_TIMEOUT)}, settings,
            visual_sources=frozenset({"pdf"}),
        )

    assert exc.value.code == "ocr_page_timeout"
    assert exc.value.meta["visual_rail_active"] is True


def test_le_rail_visuel_reste_intact_sans_timeout(settings):
    """Contre-épreuve indispensable : sans dépassement, le rail visuel garde exactement
    son comportement — un PDF sans texte atteint la découverte au lieu d'échouer."""
    r = _extrait(
        [""], {"string_exc": ValueError("image corrompue")}, settings,
        visual_sources=frozenset({"pdf"}),
    )

    assert r.metadata["pdf_no_usable_text"] is True
    assert r.metadata["ocr_error_pages"] == [1]


def test_budget_de_page_transporte_de_bout_en_bout(settings, tmp_path):
    """`extract_document(ocr_page_timeout_seconds=…)` doit atteindre Tesseract.

    L'extraction s'exécute dans un processus enfant terminable (HARDENING-03) : les
    mutations d'un faux objet y restent confinées et ne remontent pas au parent. La
    mesure doit donc FRANCHIR la frontière — ici par un fichier. Observer l'objet du
    parent ne prouverait plus rien, et un test qui ne peut plus échouer est pire
    qu'absent.
    """
    settings.DOCUMENT_OCR_ENABLED = True
    settings.DOCUMENT_OCR_TIMEOUT_SECONDS = 60
    settings.DOCUMENT_OCR_COMPUTE_CONFIDENCE = False
    settings.DOCUMENT_OCR_AUTO_ROTATE = False
    temoin = tmp_path / "budget.txt"

    class _Temoin(_Faux):
        def image_to_string(self, im, lang=None, timeout=0):
            temoin.write_text(str(timeout))
            return _TEXTE

    sys.modules["pytesseract"] = _Temoin()
    try:
        extract_document(
            _pdf([""]), filename="planche.pdf", content_type="application/pdf",
            ocr_enabled=True, ocr_page_timeout_seconds=17,
        )
    finally:
        del sys.modules["pytesseract"]

    assert temoin.exists(), "Tesseract n'a pas été appelé dans le processus enfant"
    assert float(temoin.read_text()) == pytest.approx(17, abs=0.5)


def test_les_trois_listes_de_pages_gardent_des_sens_distincts(settings):
    """`ocr_failed_pages` existait AVANT ce lot et désigne autre chose : les pages sans
    natif que l'OCR n'a pas récupérées, quelle qu'en soit la raison. Les deux nouvelles
    clés en sont des causes, pas des synonymes — ce document le prouve en les faisant
    diverger : p1 native, p2 OCRisée, p3 expirée, p4 en erreur.
    """
    settings.DOCUMENT_OCR_ENABLED = True
    settings.DOCUMENT_OCR_COMPUTE_CONFIDENCE = False
    settings.DOCUMENT_OCR_AUTO_ROTATE = False

    class _ParPage(_Faux):
        def image_to_string(self, im, lang=None, timeout=0):
            self.appels["string"] += 1
            if self.appels["string"] == 1:
                return _TEXTE
            if self.appels["string"] == 2:
                raise RuntimeError(_MSG_TIMEOUT)
            raise ValueError("image corrompue")

    sys.modules["pytesseract"] = _ParPage()
    try:
        r = extract_document(
            _pdf([_PAVE, "", "", ""]), filename="planche.pdf",
            content_type="application/pdf", ocr_enabled=True,
        )
    finally:
        del sys.modules["pytesseract"]

    assert r.metadata["ocr_pages"] == [2]
    assert r.metadata["ocr_failed_pages"] == [3, 4], "sens historique : non récupérées"
    assert r.metadata["ocr_timeout_pages"] == [3], "cause = budget expiré"
    assert r.metadata["ocr_error_pages"] == [4], "cause = autre défaillance"


def test_aucune_metadonnee_de_panne_sur_un_document_sain(settings):
    """Un document qui va bien ne porte aucune des nouvelles clés : elles signalent un
    incident, elles ne décorent pas le cas nominal."""
    r = _extrait([_PAVE], {}, settings)

    assert "ocr_timeout_pages" not in r.metadata
    assert "ocr_error_pages" not in r.metadata
    assert r.warnings == [] or "ocr_page_timeout" not in r.warnings


# =====================================================================
# 5. Observabilité : compteurs entiers, jamais bloquants
# =====================================================================
def test_les_compteurs_sont_entiers_et_sans_dimension_libre(faux, settings):
    from apps.rag_core.services import ops_counters

    settings.DOCUMENT_OCR_ENABLED = True
    settings.DOCUMENT_OCR_COMPUTE_CONFIDENCE = False
    noms = ["ocr_page_timeout_total", "ocr_page_failed_total", "ocr_pages_completed_total"]
    avant = ops_counters.snapshot(noms)
    faux(string_exc=RuntimeError(_MSG_TIMEOUT))
    _pages(page_timeout_s=30)
    apres = ops_counters.snapshot(noms)

    assert apres["ocr_page_timeout_total"] - avant["ocr_page_timeout_total"] == 2
    assert all(isinstance(v, int) for v in apres.values())


def test_une_panne_de_compteur_ne_fait_jamais_echouer_l_OCR(faux, settings, monkeypatch):
    """L'observabilité est subordonnée au travail, jamais l'inverse."""
    from apps.rag_core.services import ops_counters

    settings.DOCUMENT_OCR_ENABLED = True
    settings.DOCUMENT_OCR_COMPUTE_CONFIDENCE = False
    settings.DOCUMENT_OCR_AUTO_ROTATE = False

    def _boom(*a, **k):
        raise RuntimeError("redis down")

    monkeypatch.setattr(ops_counters, "incr", _boom)
    faux()
    res = ocr_mod.ocr_pdf_pages(_pdf([""]), [1], languages="fra", page_timeout_s=30)

    assert _TEXTE in res.text


# =====================================================================
# 6. Conséquence sur la reprise : décision EXPLICITE, pas subie
# =====================================================================
def test_un_timeout_de_page_n_est_plus_une_impasse():
    """Avant ce lot, ce document tombait en `ocr_no_text`, classé
    REPLACE_SOURCE_RECOMMENDED : « le pipeline a fait ce qu'il pouvait, fournissez un
    meilleur scan ». C'était faux — le travail avait simplement été interrompu, et le
    conseil envoyait l'utilisateur remplacer un fichier sain.

    Le nouveau code n'est volontairement PAS dans `STRATEGY_BY_ERROR_CODE` : la seule
    stratégie qui allonge un délai (`extended_timeout`) allonge le budget du DOCUMENT,
    pas celui d'une PAGE. La proposer promettrait un effet qu'elle n'a pas. Le repli
    `standard` est donc le bon, et `auto_fallback` dit qu'on ne prétend pas savoir —
    plutôt que d'inventer une valeur de budget de page étendu.
    """
    from types import SimpleNamespace

    from apps.rag_core.models.document import DocumentStatus
    from apps.rag_core.services.ingest import retry_strategy as rs

    d = SimpleNamespace(
        id="D1", status=DocumentStatus.FAILED, filename="planche.pdf",
        content_type="application/pdf", error_message="",
        meta={"extraction": {"error_code": "ocr_page_timeout"}},
    )
    retryabilite, code, strategie = rs.classify_retryability(d)

    assert code == "ocr_page_timeout"
    assert retryabilite == rs.RETRYABLE, "une reprise a un sens : rien ne fut lu"
    assert retryabilite != rs.REPLACE_SOURCE_RECOMMENDED
    assert strategie == rs.STRATEGY_STANDARD
    assert "ocr_page_timeout" not in rs.STRATEGY_BY_ERROR_CODE, (
        "aucun budget de page étendu n'existe : ne pas promettre un effet inexistant"
    )


def test_le_chemin_tableaux_OCR_recoit_aussi_un_budget(faux, settings):
    """`image_to_words` (reconstruction de tableaux OCR) était le DERNIER appel
    Tesseract lancé sans budget opposable : identifié comme risque résiduel au lot
    précédent, refermé ici avec la même primitive et sans toucher à la sémantique
    des tableaux."""
    from PIL import Image

    settings.DOCUMENT_OCR_AUTO_ROTATE = False
    f = faux()
    ocr_mod._TesseractProvider().image_to_words(
        Image.new("RGB", (60, 40), "white"), languages="fra", budget_s=13
    )

    assert f.budgets["data"] == pytest.approx(13, abs=0.5)


def test_le_chemin_tableaux_OCR_reste_sans_budget_si_aucun_n_est_fourni(faux, settings):
    """Compatibilité ascendante : 0 = pas de limite, convention pytesseract."""
    from PIL import Image

    settings.DOCUMENT_OCR_AUTO_ROTATE = False
    f = faux()
    ocr_mod._TesseractProvider().image_to_words(
        Image.new("RGB", (60, 40), "white"), languages="fra"
    )

    assert f.budgets["data"] == 0


# =====================================================================
# 7. Image autonome — la cause PREMIÈRE l'emporte (OCR-IMAGE-FAILURE-CODE-01)
# =====================================================================
def _png_image(w=60, h=40) -> bytes:
    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, format="PNG")
    return buf.getvalue()


def _extrait_image(settings, faux_kw, **kw):
    settings.DOCUMENT_IMAGE_INGEST_ENABLED = True
    settings.DOCUMENT_OCR_ENABLED = True
    settings.DOCUMENT_OCR_COMPUTE_CONFIDENCE = False
    settings.DOCUMENT_OCR_AUTO_ROTATE = False
    sys.modules["pytesseract"] = _Faux(**faux_kw)
    try:
        return extract_document(_png_image(), filename="planche.png",
                                content_type="image/png", **kw)
    finally:
        del sys.modules["pytesseract"]


def test_image_en_timeout_echoue_avec_la_cause_ocr_et_non_celle_du_rail(settings):
    """Le rail visuel échoue APRÈS, et son échec masquait la cause PREMIÈRE : une
    image dont l'OCR a expiré était rapportée `visual_analysis_failed`. Ce qui a
    empêché d'obtenir le texte l'emporte sur ce qui a échoué ensuite."""
    with pytest.raises(ExtractionError) as exc:
        _extrait_image(settings, {"string_exc": RuntimeError(_MSG_TIMEOUT)},
                       visual_sources=frozenset({"image"}))

    assert exc.value.code == "ocr_page_timeout"
    assert exc.value.meta["ocr_timeout_pages"] == [1]
    assert exc.value.meta["visual_rail_active"] is True


def test_image_en_timeout_sans_rail_visuel_n_est_plus_dite_illisible(settings):
    """Rail éteint, le code était `ocr_no_text` — classé « fournissez un meilleur
    fichier source ». Conseiller de remplacer un fichier sain est une impasse."""
    with pytest.raises(ExtractionError) as exc:
        _extrait_image(settings, {"string_exc": RuntimeError(_MSG_TIMEOUT)},
                       visual_sources=frozenset())

    assert exc.value.code == "ocr_page_timeout"
    assert exc.value.meta["visual_rail_active"] is False


def test_image_sans_texte_mais_SANS_timeout_garde_son_contrat(settings):
    """Contre-épreuve indispensable : sans dépassement, une image sans texte suit
    exactement le chemin historique. Le correctif ne doit pas requalifier en
    « timeout » tout ce qui échoue sur la voie image."""
    r = _extrait_image(settings, {"texte": "   "}, visual_sources=frozenset({"image"}))

    assert r.metadata.get("ocr_no_text") is True
    assert r.visual_assets, "le rail visuel doit toujours recevoir son visuel"

    with pytest.raises(ExtractionError) as exc:
        _extrait_image(settings, {"texte": "   "}, visual_sources=frozenset())
    assert exc.value.code == "ocr_no_text"


def test_image_en_erreur_non_timeout_n_est_pas_dite_en_timeout(settings):
    """Une défaillance OCR qui n'est PAS temporelle garde sa classification
    existante. Elle n'est surtout pas requalifiée en dépassement de délai."""
    with pytest.raises(ExtractionError) as exc:
        _extrait_image(settings, {"string_exc": ValueError("image corrompue")},
                       visual_sources=frozenset())

    assert exc.value.code != "ocr_page_timeout"


def test_image_nominale_inchangee(settings):
    """Contrôle négatif : une image lisible n'est en rien affectée."""
    r = _extrait_image(settings, {}, visual_sources=frozenset({"image"}))

    assert r.metadata["ocr_used"] is True
    assert _TEXTE.split()[0] in r.text
    assert "ocr_page_timeout" not in r.warnings


def test_la_voie_PDF_n_est_pas_modifiee_par_le_correctif_image(settings):
    """Non-régression explicite : le contrat PDF, déjà qualifié en staging, doit
    rester identique au caractère près."""
    settings.DOCUMENT_OCR_ENABLED = True
    with pytest.raises(ExtractionError) as exc:
        _extrait([""], {"string_exc": RuntimeError(_MSG_TIMEOUT)}, settings,
                 visual_sources=frozenset({"pdf"}))

    assert exc.value.code == "ocr_page_timeout"
    assert exc.value.meta["visual_rail_active"] is True
