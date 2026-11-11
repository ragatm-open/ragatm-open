"""la porte des sondes ne touche PAS les chemins métier.

- Embeddings gouvernés : porte fermée, `embed_via_gateway` appelle toujours `POST /v1/embeddings`
  et sa référence d'identité (`GET /health/embeddings`, cache 300 s par processus, fail-closed) :
  cette lecture fait partie du CONTRÔLE D'INTÉGRITÉ des embeddings métier, pas d'une sonde
  automatique. Elle n'est déclenchée que par une vraie demande d'embedding.
- Santé du Pool : `build_preflight` sans `--probe` n'émet AUCUN appel réseau et classe les URL
  `ai-gateway-pool:8100` en route pool ; `--probe` (qui lit `/health/runtime`) reste un
  geste opérateur explicite, jamais appelé par le code applicatif.
- Liveness Docker (`/api/health/`) : aucune dépendance Gateway.
"""
from __future__ import annotations

import inspect
import pathlib

import pytest

from apps.llm_router import embedding_reference as ref
from apps.llm_router import embeddings_gateway as gw

from _opaque_gateway_fixtures import DIM, FakeGateway, embeddings_payload, governed_settings  # noqa: E402

REPO_BACKEND = pathlib.Path(gw.__file__).resolve().parents[2]


@pytest.fixture
def gate_closed(settings):
    settings.CAPABILITIES_GATEWAY_PROBES_ENABLED = False
    governed_settings(settings)
    ref.reset_reference_cache()
    yield settings
    ref.reset_reference_cache()


def test_embeddings_metier_preserves_porte_fermee(gate_closed, monkeypatch):
    g = FakeGateway(embeddings=embeddings_payload(model="legacy-768", task="query", n=1)).install(monkeypatch)
    batch = gw.embed_via_gateway(["question"], model="legacy-768", task="query", dim=DIM)
    assert len(batch.vectors[0]) == DIM and batch.governed is True
    assert [p["url"].rsplit("/", 2)[-2:] for p in g.posts] == [["v1", "embeddings"]]
    assert len(g.gets) == 1 and g.gets[0].endswith(ref.REFERENCE_PATH)


def test_reference_embeddings_mise_en_cache_pas_de_lecture_par_requete(gate_closed, monkeypatch):
    g = FakeGateway(embeddings=embeddings_payload(model="legacy-768", task="query", n=1)).install(monkeypatch)
    for _ in range(5):
        gw.embed_via_gateway(["question"], model="legacy-768", task="query", dim=DIM)
    assert len(g.posts) == 5
    assert len(g.gets) == 1, "la référence /health/embeddings est lue une fois par TTL, pas par requête"
    assert ref.REFERENCE_TTL_SECONDS == 300.0


def test_aucune_sonde_automatique_n_appelle_health_embeddings():
    """Seul le chemin d'embedding gouverné lit /health/embeddings ; la console l'interdit."""
    from apps.rag_core.services.capabilities import gateway_health as GH

    assert "/health/embeddings" in GH.FORBIDDEN_PATHS
    assert not {p for _, p in GH.READS.values()} & GH.FORBIDDEN_PATHS
    users = sorted(str(p.relative_to(REPO_BACKEND)) for p in REPO_BACKEND.rglob("*.py")
                   if "/tests/" not in str(p) and "REFERENCE_PATH" in p.read_text(encoding="utf-8", errors="ignore"))
    assert users == ["apps/llm_router/embedding_reference.py"], users


def test_preflight_pool_sans_probe_aucun_appel_reseau(settings):
    from apps.llm_router.gateway import endpoints

    for name in endpoints.GATEWAY_URL_SETTINGS:
        setattr(settings, name, "http://ai-gateway-pool:8080")
    settings.AI_GATEWAY_POOL_HOSTS = ["ai-gateway-pool"]
    settings.DJANGO_ENV = "prod"

    def _no_get(*a, **k):
        raise AssertionError("aucun GET attendu sans --probe")

    report = endpoints.build_preflight(settings_obj=settings, probe=False, get=_no_get, read_overlay=False)
    assert {e.route for e in report.endpoints} == {endpoints.ROUTE_POOL}
    assert report.problems == []


def test_probe_reste_un_geste_operateur_explicite():
    """`probe=True` n'apparaît que dans la commande de gestion (opt-in `--probe`)."""
    hits = []
    for p in REPO_BACKEND.rglob("*.py"):
        if "/tests/" in str(p):
            continue
        src = p.read_text(encoding="utf-8", errors="ignore")
        if "build_preflight(" in src and "def build_preflight" not in src:
            hits.append(str(p.relative_to(REPO_BACKEND)))
    assert hits == ["apps/rag_core/management/commands/gateway_endpoints_preflight.py"], hits
    from apps.rag_core.management.commands import gateway_endpoints_preflight as cmd

    assert 'bool(opts.get("probe"))' in inspect.getsource(cmd)


def test_liveness_docker_sans_dependance_gateway():
    from config import urls

    src = inspect.getsource(urls.healthcheck)
    assert "requests" not in src and "urlopen" not in src and "gateway" not in src.lower()
