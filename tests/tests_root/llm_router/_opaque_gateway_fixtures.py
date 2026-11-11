"""Doubles du contrat Gateway v2 pour les identités de pipeline OPAQUES.

Valeurs publiées par une Gateway de qualification (`/health/embeddings`).
Ce sont des FIXTURES et des contrôles de régression — pas la source de vérité du
client, qui lit la référence sur la Gateway à l'exécution.
"""
from __future__ import annotations

BASE_URL = "http://gw.test:8200"
DIM = 768

DIGESTS = {
    "legacy-768": "0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f",
    "fr-en-retrieval": "1a37926bf842899cbf90583e3932f3820548716d9d07661ba622199ffe95c552",
}
PIPELINE_IDS = {
    ("legacy-768", "query"): "emb-v2:3a49e14ec5df7088cebb7630fd1d27eb",
    ("legacy-768", "document"): "emb-v2:9e883f0fe1d326ef17f86027a1d4a5d8",
    ("fr-en-retrieval", "query"): "emb-v2:da35d8329da5f8dc1b57d0b675fca563",
    ("fr-en-retrieval", "document"): "emb-v2:c8215aca68cbf785bf977a46ead4b23c",
}
#: Familles de pipeline telles que PERSISTÉES dans un espace (scénario SYNTHÉTIQUE : deux familles
#: coexistent, la première nettement majoritaire — ordres de grandeur illustratifs, aucune mesure réelle).
PERSISTED_FAMILIES = {
    "legacy-768": "legacy-768@0a109f42",
    "fr-en-retrieval": "fr-en-retrieval@1a37926b",
}
UNKNOWN_PIPELINE_ID = "emb-v2:ffffffffffffffffffffffffffffffff"


def registry_entry(model: str, **overrides):
    entry = {
        "model_logical": model,
        "ollama_name": "nomic-embed-text" if model == "legacy-768" else "granite-embedding:278m",
        "kind": "embedding",
        "dimension": DIM,
        "expected_digest": DIGESTS[model],
        "observed_digest": DIGESTS[model],
        "registry_status": "ready",
        "identity_verified": True,
        "pipeline_id_query": PIPELINE_IDS[(model, "query")],
        "pipeline_id_document": PIPELINE_IDS[(model, "document")],
        "state": "ready",
    }
    entry.update(overrides)
    return entry


def registry_payload(*entries):
    """Réponse `/health/embeddings` (extrait utile). Sans argument : les deux profils."""
    if not entries:
        entries = (registry_entry("legacy-768"), registry_entry("fr-en-retrieval"))
    return {"environment": "test", "registry_version": 1, "models": list(entries)}


def embeddings_payload(*, model: str, task: str, n: int = 1, dim: int = DIM, vector=None, **overrides):
    """Réponse `POST /v1/embeddings` telle que la Gateway v2 la rend."""
    vec = list(vector) if vector is not None else [0.5] * dim
    payload = {
        "provider": "ai_gateway",
        "upstream_provider": "ollama",
        "model": "nomic-embed-text" if model == "legacy-768" else "granite-embedding:278m",
        "dimension": dim,
        "embeddings": [list(vec) for _ in range(n)],
        "usage": {"total_texts": n},
        "cost": 0.0,
        "governed": True,
        "model_logical": model,
        "model_digest": DIGESTS[model],
        "expected_digest": DIGESTS[model],
        "identity_verified": True,
        "verified_at": "2026-10-05T21:43:53+00:00",
        "pipeline_id": PIPELINE_IDS[(model, task)],
        "task": task,
        "normalized": True,
        "truncated": [False] * n,
        "upstream_endpoint": "/api/embed",
    }
    payload.update(overrides)
    return payload


class Resp:
    def __init__(self, payload, status=200, text=""):
        self._payload, self.status_code, self.text = payload, status, text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeGateway:
    """Transport double : POST /v1/embeddings + GET /health/embeddings, avec compteurs."""

    def __init__(self, *, embeddings=None, registry=None, registry_status=200, embeddings_status=200):
        self.embeddings = embeddings
        self.registry = registry if registry is not None else registry_payload()
        self.registry_status = registry_status
        self.embeddings_status = embeddings_status
        self.registry_error: Exception | None = None
        self.posts: list[dict] = []
        self.gets: list[str] = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append({"url": url, "payload": json, "timeout": timeout})
        return Resp(self.embeddings, status=self.embeddings_status, text="{}")

    def get(self, url, timeout=None, **kw):
        self.gets.append(url)
        if self.registry_error is not None:
            raise self.registry_error
        return Resp(self.registry, status=self.registry_status)

    def install(self, monkeypatch):
        import requests

        monkeypatch.setattr(requests, "post", self.post)
        monkeypatch.setattr(requests, "get", self.get)
        return self


def governed_settings(settings):
    settings.EMBEDDINGS_PROVIDER = "ollama"  # hors escape hatch stub
    settings.AI_GATEWAY_EMBEDDINGS_ENABLED = True
    settings.AI_GATEWAY_EMBEDDINGS_BASE_URL = BASE_URL
    settings.AI_GATEWAY_EMBEDDINGS_TIMEOUT_SECONDS = 5.0
    settings.RAG_EMBEDDING_DIM = DIM
    return settings
