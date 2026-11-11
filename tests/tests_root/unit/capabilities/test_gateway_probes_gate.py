"""porte des sondes Gateway de la console.

Fermée par défaut : ouvrir `/admin-core` ou l'écran « Capacités documentaires » ne doit émettre
AUCUN appel HTTP vers la Gateway tant que les quatre routes ne sont pas attestées non
inférentielles sur le code déployé. Ouverte : comportement antérieur inchangé."""
from __future__ import annotations

import pytest

from apps.rag_core.services.capabilities import gateway_health as GH


@pytest.fixture
def no_network(monkeypatch):
    calls = []

    def boom(*a, **k):
        calls.append(a)
        raise AssertionError("appel HTTP Gateway interdit porte fermée")

    monkeypatch.setattr("requests.get", boom)
    return calls


def test_defaut_du_registre_ferme():
    from config import registry as config_registry

    d = next(x for x in config_registry.all_definitions() if x.name == "CAPABILITIES_GATEWAY_PROBES_ENABLED")
    assert d.default is False and d.value_type == "bool" and not d.runtime_editable


def test_porte_fermee_aucun_appel_reseau(settings, no_network):
    settings.CAPABILITIES_GATEWAY_PROBES_ENABLED = False
    for refresh in (False, True):
        snap = GH.snapshot(refresh=refresh)
        assert snap["paths_read"] == [] and snap["probes_enabled"] is False
        assert set(snap["services"]) == set(GH.PROBES)
        assert {s["status"] for s in snap["services"].values()} == {GH.UNKNOWN}
        assert {s["reason"] for s in snap["services"].values()} == {"probes_disabled"}
    assert no_network == []


def test_porte_fermee_ignore_un_cache_anterieur(settings, no_network, monkeypatch):
    settings.CAPABILITIES_GATEWAY_PROBES_ENABLED = False
    monkeypatch.setattr(GH.cache, "get", lambda k: {"services": {"vision": {"status": GH.AVAILABLE}}})
    assert GH.snapshot()["services"]["vision"]["status"] == GH.UNKNOWN


def test_porte_ouverte_comportement_inchange(settings, monkeypatch):
    settings.CAPABILITIES_GATEWAY_PROBES_ENABLED = True
    monkeypatch.setattr(GH.cache, "get", lambda k: None)
    monkeypatch.setattr(GH.cache, "set", lambda *a, **k: None)
    monkeypatch.setattr(GH, "probe_all", lambda: {"services": {"vision": {"status": GH.AVAILABLE}}, "paths_read": ["/health/vision"]})
    snap = GH.snapshot()
    assert snap["services"]["vision"]["status"] == GH.AVAILABLE and snap["cached"] is False


def test_les_deux_appelants_passent_par_la_porte(settings, no_network):
    """Accueil de la console (CoreConfigHealthView) et écran Capacités (resolver) : snapshot() seul."""
    import inspect

    from api.v1.admin_core_config import views
    from apps.rag_core.services.capabilities import resolver

    assert "gateway_health.snapshot(" in inspect.getsource(views._gateway_health_summary)
    assert "gateway_health.snapshot(" in inspect.getsource(resolver)
    assert "probe_all(" not in inspect.getsource(views) and "probe_all(" not in inspect.getsource(resolver)
    settings.CAPABILITIES_GATEWAY_PROBES_ENABLED = False
    summary = views._gateway_health_summary()
    assert summary["status"] == GH.UNKNOWN and no_network == []
