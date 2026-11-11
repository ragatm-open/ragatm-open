# -*- coding: utf-8 -*-
"""Porte licence MinerU : périmètre d'usage déclaré et invariants techniques.

Ce que ces tests protègent n'est pas une conformité juridique — le code n'en
décide pas — mais une politique opérationnelle qui doit échouer fermé. Le piège
qu'ils gardent est précis : un périmètre absent ne doit JAMAIS être lu comme un
« usage personnel » implicite.
"""
from __future__ import annotations

import pytest

from apps.rag_core.services.ingest.docvision import config as dv_config
from apps.rag_core.services.ingest.docvision import license_gate as lg
from apps.rag_core.services.ingest.extraction import docvision_hook
from apps.rag_core.services.ingest.extraction.base import ExtractionResult


@pytest.fixture()
def invariants_verts(monkeypatch):
    monkeypatch.setattr(lg, "evaluer_invariants",
                        lambda: {k: True for k in lg.INVARIANTS})


# ------------------------------------------------------------------- périmètre
def test_perimetre_absent_vaut_inconnu(settings):
    """Un réglage non posé ne vaut pas « personnel » : il vaut inconnu."""
    settings.MINERU_SERVICE_USE_SCOPE = ""
    assert dv_config.use_scope() == "unknown"


def test_perimetre_invalide_vaut_inconnu(settings):
    settings.MINERU_SERVICE_USE_SCOPE = "personnel"     # orthographe fantaisiste
    assert dv_config.use_scope() == "unknown"
    settings.MINERU_SERVICE_USE_SCOPE = "PERSONAL_INTERNAL"
    assert dv_config.use_scope() == "personal_internal"  # la casse est tolérée


def test_perimetre_inconnu_bloque(settings):
    settings.MINERU_SERVICE_USE_SCOPE = "unknown"
    d = lg.mineru_license_gate()
    assert d.status == lg.FAIL_SCOPE_UNKNOWN
    assert d.eligible is False


# ------------------------------------------------------- personnel / interne
def test_personnel_interne_avec_invariants_verts(settings, invariants_verts):
    """§26 : le cas cible — attribution FALSE et pourtant éligible, parce que
    l'obligation qui la commande ne s'applique pas au périmètre déclaré."""
    settings.MINERU_SERVICE_USE_SCOPE = "personal_internal"
    settings.MINERU_SERVICE_ATTRIBUTION_SATISFIED = False
    d = lg.mineru_license_gate()
    assert d.status == lg.PASS_NOT_REQUIRED
    assert d.eligible is True
    assert d.attribution_satisfied is False


def test_personnel_interne_avec_invariant_faux_bloque(settings, monkeypatch):
    settings.MINERU_SERVICE_USE_SCOPE = "personal_internal"
    monkeypatch.setattr(lg, "evaluer_invariants",
                        lambda: {**{k: True for k in lg.INVARIANTS},
                                 "anonymous_rag_denied": False})
    d = lg.mineru_license_gate()
    assert d.status == lg.FAIL_SCOPE_INVARIANT
    assert d.eligible is False
    assert d.failed_invariants == ["anonymous_rag_denied"]


def test_invariant_indecidable_vaut_faux(monkeypatch):
    """Échec fermé : ce qu'on ne peut pas démontrer ne passe pas."""
    monkeypatch.setitem(lg.INVARIANTS, "sidecar_not_public", lambda: None)
    assert lg.evaluer_invariants()["sidecar_not_public"] is False


# ------------------------------------------------------------- service à tiers
def test_tiers_sans_attribution_bloque(settings):
    settings.MINERU_SERVICE_USE_SCOPE = "third_party_service"
    settings.MINERU_SERVICE_ATTRIBUTION_SATISFIED = False
    d = lg.mineru_license_gate()
    assert d.status == lg.FAIL_ATTRIBUTION_REQUIRED
    assert d.eligible is False


def test_tiers_avec_attribution_passe(settings):
    settings.MINERU_SERVICE_USE_SCOPE = "third_party_service"
    settings.MINERU_SERVICE_ATTRIBUTION_SATISFIED = True
    d = lg.mineru_license_gate()
    assert d.status == lg.PASS_ATTRIBUTED
    assert d.eligible is True


def test_les_deux_issues_passantes_sont_distinctes(settings, invariants_verts):
    """`PASS_NOT_REQUIRED` et `PASS_ATTRIBUTED` ne doivent pas se confondre :
    la seconde affirme qu'une attribution existe, la première non."""
    settings.MINERU_SERVICE_USE_SCOPE = "personal_internal"
    settings.MINERU_SERVICE_ATTRIBUTION_SATISFIED = False
    a = lg.mineru_license_gate()
    settings.MINERU_SERVICE_USE_SCOPE = "third_party_service"
    settings.MINERU_SERVICE_ATTRIBUTION_SATISFIED = True
    b = lg.mineru_license_gate()
    assert a.eligible and b.eligible
    assert a.status != b.status


# --------------------------------------------------------- effet sur le rail
def test_drapeau_off_ignore_la_porte(settings):
    """§25 : éteint, le rail ne rend pas RAGatm dépendant de la porte."""
    settings.DOCUMENT_MINERU_HYBRID_ENABLED = False
    settings.MINERU_SERVICE_USE_SCOPE = "unknown"
    r = ExtractionResult(text="texte historique")
    sortie = docvision_hook.appliquer_si_actif(r, data=b"", filename="a.pdf")
    assert sortie is r
    assert "docvision" not in (sortie.metadata or {})


def test_drapeau_on_mais_perimetre_inconnu_ne_route_rien(settings, monkeypatch):
    settings.DOCUMENT_MINERU_HYBRID_ENABLED = True
    settings.MINERU_SERVICE_USE_SCOPE = "unknown"
    from apps.rag_core.services.ingest.docvision import rail as dv_rail

    def _interdit(*a, **k):
        raise AssertionError("le rail ne doit pas s'executer, porte bloquee")

    monkeypatch.setattr(dv_rail, "appliquer_rail", _interdit)
    r = ExtractionResult(text="texte historique")
    sortie = docvision_hook.appliquer_si_actif(r, data=b"", filename="a.pdf")
    assert sortie.text == "texte historique"
    assert sortie.metadata["docvision"]["skipped_reason"] == \
        "LICENSE_GATE:FAIL_SCOPE_UNKNOWN"


def test_drapeau_on_et_tiers_sans_attribution_ne_route_rien(settings, monkeypatch):
    settings.DOCUMENT_MINERU_HYBRID_ENABLED = True
    settings.MINERU_SERVICE_USE_SCOPE = "third_party_service"
    settings.MINERU_SERVICE_ATTRIBUTION_SATISFIED = False
    from apps.rag_core.services.ingest.docvision import rail as dv_rail
    monkeypatch.setattr(dv_rail, "appliquer_rail",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("bloque")))
    r = ExtractionResult(text="historique")
    s = docvision_hook.appliquer_si_actif(r, data=b"", filename="a.pdf")
    assert s.metadata["docvision"]["skipped_reason"] == \
        "LICENSE_GATE:FAIL_ATTRIBUTION_REQUIRED"


# ------------------------------------------------------------- observabilité
def test_diagnostic_ne_porte_que_des_valeurs_techniques(settings, invariants_verts):
    settings.MINERU_SERVICE_USE_SCOPE = "personal_internal"
    d = lg.mineru_license_gate().as_dict()
    assert set(d) >= {"mineru_use_scope", "mineru_attribution_satisfied",
                      "mineru_license_gate_status", "mineru_license_gate_reason"}
    assert d["mineru_license_gate_status"] == "PASS_NOT_REQUIRED"
    # aucun contenu documentaire, aucune donnée nominative
    assert "@" not in repr(d)


def test_la_porte_est_independante_de_la_readiness_du_sidecar(settings):
    """§32 : un sidecar apte et une porte bloquante coexistent sans contradiction.
    La porte ne consulte jamais l'état du sidecar."""
    settings.MINERU_SERVICE_USE_SCOPE = "unknown"
    import inspect
    src = inspect.getsource(lg.mineru_license_gate)
    assert "ready" not in src and "sante" not in src
    assert lg.mineru_license_gate().eligible is False
