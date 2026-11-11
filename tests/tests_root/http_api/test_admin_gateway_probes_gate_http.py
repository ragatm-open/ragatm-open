"""porte des sondes Gateway, prouvée au niveau HTTP.

Complète `unit/capabilities/test_gateway_probes_gate.py` (niveau service) : ici on ouvre les
VRAIES vues de la console (`/admin-core` → `health/`, `effective/`, `audit/` ; écran « Capacités
documentaires »), sans doubler `gateway_health.snapshot`, et on espionne le réseau aux trois
frontières que le code Gateway emprunte : `requests` (Session.send), `urllib` (OpenerDirector.open)
et le socket TCP lui-même (`http.client.HTTPConnection.connect`). Porte fermée : ZÉRO appel,
quel que soit le nombre d'ouvertures, de rafraîchissements ou de « polls ».
"""
from __future__ import annotations

import http.client
import urllib.request

import pytest
import requests
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.rag_core.services.capabilities import gateway_health as GH

pytestmark = pytest.mark.django_db

BASE = "/api/v1/admin/core-config/"
CONSOLE_URLS = (BASE + "health/", BASE + "effective/", BASE + "audit/?limit=50",
                "/api/v1/platform/workspace-quota-defaults/")  # = les 4 appels de app/admin-core/page.tsx
CAPS_URL = BASE + "document-capabilities/"


@pytest.fixture
def network_spy(monkeypatch):
    """Toute tentative réseau est ENREGISTRÉE puis refusée (aucun appel réel ne part)."""
    calls: list[str] = []

    def _send(self, request, **kw):
        calls.append(f"requests {request.method} {request.url}")
        raise requests.ConnectionError("réseau interdit dans ce test")

    def _open(self, fullurl, *a, **kw):
        url = fullurl.full_url if isinstance(fullurl, urllib.request.Request) else str(fullurl)
        calls.append(f"urllib {url}")
        raise OSError("réseau interdit dans ce test")

    def _connect(self):
        calls.append(f"socket {self.host}:{self.port}")
        raise OSError("réseau interdit dans ce test")

    monkeypatch.setattr(requests.sessions.Session, "send", _send)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", _open)
    monkeypatch.setattr(http.client.HTTPConnection, "connect", _connect)
    return calls


@pytest.fixture
def gate_closed(settings):
    settings.CAPABILITIES_GATEWAY_PROBES_ENABLED = False
    # URL Gateway CONFIGURÉES comme en production Pool ON : seule la porte empêche l'appel.
    for name in ("AI_GATEWAY_BASE_URL", "AI_GATEWAY_EMBEDDINGS_BASE_URL", "RAG_RERANKER_BASE_URL"):
        setattr(settings, name, "http://ai-gateway-pool:8080")
    GH.cache.delete(GH.CACHE_KEY)
    return settings


@pytest.fixture
def root(db):
    user = get_user_model().objects.create_superuser(username="root-pc1", password="pw", email="r@qa.invalid")
    c = APIClient()
    c.force_authenticate(user=user)
    return c


def test_defaut_effectif_des_settings_est_ferme(settings):
    """Valeur CHARGÉE par la configuration de test (= défaut du registre), sans surcharge."""
    assert settings.CAPABILITIES_GATEWAY_PROBES_ENABLED is False
    assert GH.probes_enabled() is False


def test_ouverture_admin_core_aucun_appel_gateway(root, gate_closed, network_spy):
    for url in CONSOLE_URLS:
        resp = root.get(url)
        assert resp.status_code == 200, (url, getattr(resp, "data", resp))
    assert network_spy == []


def test_ouverture_capacites_aucun_appel_gateway(root, gate_closed, network_spy):
    for refresh in ("0", "1"):
        assert root.get(CAPS_URL, {"refresh": refresh}).status_code == 200
    assert network_spy == []


def test_polling_et_rafraichissements_repetes_ne_reactivent_pas_les_sondes(root, gate_closed, network_spy):
    """Simule un onglet laissé ouvert : 10 cycles accueil + capacités, dont des « Actualiser »."""
    for i in range(10):
        assert root.get(BASE + "health/").status_code == 200
        assert root.get(CAPS_URL, {"refresh": "1" if i % 2 else "0"}).status_code == 200
    assert network_spy == []


def test_sante_console_neutre_jamais_ok(root, gate_closed, network_spy):
    body = root.get(BASE + "health/").json()
    gw = body["gateway_health"]
    assert gw["status"] == GH.UNKNOWN and gw["probes_enabled"] is False
    assert set(gw["services"].values()) == {GH.UNKNOWN}
    assert body["core_health"]["ok"] is None and body["core_health"]["verified"] is False


def test_capacites_non_verifiees_jamais_presentees_disponibles(root, gate_closed, network_spy):
    body = root.get(CAPS_URL).json()
    assert body["data_freshness"]["gateway_paths_read"] == []
    services = {s["service"]: s for s in body["gateway_services"]}
    assert set(services) == set(GH.PROBES)
    assert {s["status"] for s in services.values()} == {GH.UNKNOWN}
    assert {s["reason"] for s in services.values()} == {"probes_disabled"}
    # Toute capacité adossée à un service Gateway non sondé : disponibilité NON affirmée.
    dependent = [c for c in body["capabilities"] if any(
        r.startswith("gateway_") for r in c["runtime_reasons"])]
    assert dependent, "le registre doit contenir des capacités adossées à la Gateway"
    for c in dependent:
        assert c["runtime_available"] != GH.AVAILABLE, c["id"]
    for e in body["embeddings"]["providers"]:
        assert e["service_status"] != GH.AVAILABLE


def test_porte_ouverte_erreur_affichee_sans_retry_ni_route_inferentielle(settings, monkeypatch):
    """Échec réseau, porte OUVERTE : une seule lecture par route, jamais de route inférentielle,
    statut dégradé affiché (jamais AVAILABLE)."""
    settings.CAPABILITIES_GATEWAY_PROBES_ENABLED = True
    GH.cache.delete(GH.CACHE_KEY)
    seen: list[str] = []

    def _get(url, timeout=None, **kw):
        seen.append(url)
        raise requests.ConnectionError("gateway down")

    monkeypatch.setattr(requests, "get", _get)
    snap = GH.snapshot(refresh=True)
    from collections import Counter

    expected = Counter(GH._base(s) + p for s, p in GH.READS.values())
    got = Counter(seen)
    assert all(got[u] <= expected[u] for u in got), ("relecture = retry interdit", got, expected)
    assert not {u for u in seen if any(u.endswith(f) for f in GH.FORBIDDEN_PATHS)}
    assert all(s.get("status") != GH.AVAILABLE for s in snap["services"].values())
    GH.cache.delete(GH.CACHE_KEY)
