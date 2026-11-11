"""Résolution et sonde NON INFÉRENTIELLE des trois URL Gateway — édition Open.

Trois réglages désignent une AI Gateway, chacun avec son consommateur :

==================================  ===================================================
``AI_GATEWAY_BASE_URL``             génération (+flux), vision (OFF), ASR (OFF),
                                    catalogue de modèles, sondes /health/*
``AI_GATEWAY_EMBEDDINGS_BASE_URL``  embeddings GOUVERNÉS (identité ``pipeline_id``)
``RAG_RERANKER_BASE_URL``           reranking (``/v1/rerank``) et sa sonde
==================================  ===================================================

Différence avec l'édition privée : aucun hôte n'est codé en dur. Le type de route est
DÉRIVÉ DE LA CONFIGURATION :

- ``AI_GATEWAY_POOL_HOSTS`` (liste, défaut vide) : noms d'hôte reconnus comme un
  AI Gateway Pool. Une URL dont l'hôte y figure est de type ``pool`` ;
- toute autre URL http(s) avec un hôte est de type ``direct`` (une AI Gateway Open
  jointe directement : nom de service compose, loopback, IP du réseau interne) ;
- une URL vide est de type ``empty`` ; une URL sans schéma http(s) ou sans hôte est
  ``other`` (refusée par le préflight).

Sondes (``probe``) : uniquement ``GET /health/runtime`` (Gateway, relayée par un pool)
et ``GET /pool/health/ready`` (propre au pool). JAMAIS ``/health/ready`` de la
Gateway : elle calcule un embedding.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, FrozenSet, Iterable, Optional
from urllib.parse import urlparse

GATEWAY_URL_SETTINGS = (
    "AI_GATEWAY_BASE_URL",
    "AI_GATEWAY_EMBEDDINGS_BASE_URL",
    "RAG_RERANKER_BASE_URL",
)

CONSUMERS = {
    "AI_GATEWAY_BASE_URL": "generate,generate_stream,vision_v1,vision_v2,asr,"
                           "model_catalog,health_vision,health_asr",
    "AI_GATEWAY_EMBEDDINGS_BASE_URL": "governed_embeddings,health_embeddings",
    "RAG_RERANKER_BASE_URL": "rerank,health_rerank",
}

ROUTE_POOL = "pool"
ROUTE_DIRECT = "direct"
ROUTE_OTHER = "other"
ROUTE_EMPTY = "empty"


def _normalize_hosts(hosts: Optional[Iterable[str]]) -> FrozenSet[str]:
    return frozenset((h or "").strip().lower() for h in (hosts or ()) if (h or "").strip())


def pool_hosts(settings_obj=None) -> FrozenSet[str]:
    """Hôtes déclarés comme pools par la configuration (``AI_GATEWAY_POOL_HOSTS``)."""
    if settings_obj is None:
        from django.conf import settings as settings_obj  # noqa: N813
    return _normalize_hosts(getattr(settings_obj, "AI_GATEWAY_POOL_HOSTS", ()) or ())


def url_host(url: str) -> str:
    return (urlparse((url or "").strip()).hostname or "").lower()


def route_kind(url: str, *, pool: Optional[Iterable[str]] = None, settings_obj=None) -> str:
    """Type de route d'une URL Gateway : ``empty`` / ``other`` / ``pool`` / ``direct``."""
    url = (url or "").strip()
    if not url:
        return ROUTE_EMPTY
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host:
        return ROUTE_OTHER
    hosts = _normalize_hosts(pool) if pool is not None else pool_hosts(settings_obj)
    if host in hosts:
        return ROUTE_POOL
    return ROUTE_DIRECT


def pool_environment(host: str) -> str:
    """Édition Open : aucun alias de pool n'est lié à un environnement. Toujours ''."""
    return ""


def declared_embedding_models(url: str) -> Optional[frozenset]:
    """Profils d'embeddings déclarés par le pool visé ; ``None`` = aucune restriction.

    Édition Open : la restriction par pool n'existe pas en configuration statique — le
    pool répond lui-même 404 ``MODEL_NOT_AVAILABLE`` pour un profil qu'il ne déclare pas,
    et le catalogue le constate à l'appel.
    """
    return None


def expected_gateway_environment(django_env: str) -> str:
    return {"prod": "production", "production": "production", "staging": "staging"}.get(
        (django_env or "").strip().lower(), "")


@dataclass
class EndpointReport:
    name: str
    url: str
    route: str
    consumers: str
    probe: dict = field(default_factory=dict)


@dataclass
class PreflightReport:
    django_env: str
    endpoints: list
    overlay_runtime_enabled: bool = False
    overlay_gateway_overrides: int = -1     # -1 = non lu
    problems: list = field(default_factory=list)

    @property
    def status(self) -> str:
        return "FAIL" if self.problems else "PASS"

    def as_lines(self) -> list[str]:
        out = [f"DJANGO_ENV={self.django_env}"]
        for e in self.endpoints:
            out.append(f"GATEWAY_URL_{e.name}={e.url or '-'}")
            out.append(f"GATEWAY_ROUTE_{e.name}={e.route}")
            out.append(f"GATEWAY_CONSUMERS_{e.name}={e.consumers}")
            for k, v in sorted(e.probe.items()):
                out.append(f"GATEWAY_PROBE_{e.name}_{k.upper()}={v}")
        routes = sorted({e.route for e in self.endpoints if e.route != ROUTE_EMPTY})
        out.append(f"GATEWAY_ROUTES={','.join(routes) or '-'}")
        out.append(f"GATEWAY_OVERLAY_RUNTIME_ENABLED={int(self.overlay_runtime_enabled)}")
        out.append(f"GATEWAY_OVERLAY_URL_OVERRIDES={self.overlay_gateway_overrides}")
        for p in self.problems:
            out.append(f"GATEWAY_PROBLEM={p}")
        out.append(f"GATEWAY_PREFLIGHT={self.status}")
        return out


def _probe(url: str, *, get: Callable, expected_env: str) -> tuple[dict, list]:
    """Deux GET non inférentiels. Rend (constats, problèmes). Ne lève pas."""
    facts: dict = {}
    problems: list = []
    try:
        r = get(url + "/health/runtime", timeout=5)
        facts["runtime_http"] = r.status_code
        data = r.json() if r.status_code == 200 else {}
        if isinstance(data, dict):
            env = str(data.get("environment") or "")
            build = data.get("build") if isinstance(data.get("build"), dict) else {}
            facts["gateway_environment"] = env or "-"
            facts["gateway_build"] = str(build.get("git_sha") or "-")[:12]
            if expected_env and env and env != expected_env:
                problems.append(f"gateway_environment_mismatch:{env}!={expected_env}")
        if r.status_code != 200:
            problems.append(f"runtime_http_{r.status_code}")
    except Exception as exc:  # noqa: BLE001
        facts["runtime_http"] = "error"
        problems.append(f"runtime_unreachable:{type(exc).__name__}")
    try:
        r = get(url + "/pool/health/ready", timeout=5)
        data = r.json() if r.status_code == 200 else None
        via_pool = isinstance(data, dict) and "routing_policy" in data
        facts["via_pool"] = "yes" if via_pool else "no"
        if via_pool:
            facts["pool_status"] = str(data.get("status") or "-")
            facts["pool_routing_policy"] = str(data.get("routing_policy") or "-")
            facts["pool_job_affinity_store"] = str(data.get("job_affinity_store") or "-")
            facts["pool_environment"] = str(data.get("environment") or "-")
            if data.get("status") != "ready":
                problems.append(f"pool_not_ready:{data.get('status')}")
            if expected_env and data.get("environment") not in (None, expected_env):
                problems.append(f"pool_environment_mismatch:{data.get('environment')}!={expected_env}")
    except Exception:  # noqa: BLE001 - une Gateway directe n'a pas cette route
        facts["via_pool"] = "no"
    return facts, problems


def build_preflight(*, settings_obj=None, probe: bool = False, get: Optional[Callable] = None,
                    read_overlay: bool = True) -> PreflightReport:
    if settings_obj is None:
        from django.conf import settings as settings_obj  # noqa: N813
    django_env = str(getattr(settings_obj, "DJANGO_ENV", "") or "").strip().lower()
    expected_env = expected_gateway_environment(django_env)
    hosts = pool_hosts(settings_obj)
    endpoints = []
    problems: list = []
    for name in GATEWAY_URL_SETTINGS:
        url = str(getattr(settings_obj, name, "") or "").strip().rstrip("/")
        e = EndpointReport(name=name, url=url, route=route_kind(url, pool=hosts), consumers=CONSUMERS[name])
        if e.route == ROUTE_EMPTY:
            problems.append(f"{name}:empty")
        elif e.route == ROUTE_OTHER:
            problems.append(f"{name}:invalid_url")
        endpoints.append(e)
    routes = {e.route for e in endpoints if e.route != ROUTE_EMPTY}
    if ROUTE_POOL in routes and len(routes) > 1:
        # Bascule partielle : génération par le pool, embeddings en direct (ou l'inverse)…
        # Échec explicite, jamais de correction silencieuse.
        problems.append("mixed_gateway_routes:" + ",".join(f"{e.name}={e.route}" for e in endpoints))
    pool_used = {url_host(e.url) for e in endpoints if e.route == ROUTE_POOL}
    if len(pool_used) > 1:
        problems.append("mixed_pool_hosts:" + ",".join(sorted(pool_used)))
    if probe:
        if get is None:
            import requests

            get = requests.get
        seen: dict = {}
        for e in endpoints:
            if not e.url:
                continue
            if e.url not in seen:
                seen[e.url] = _probe(e.url, get=get, expected_env=expected_env)
            e.probe, probs = seen[e.url]
            problems.extend(f"{e.name}:{p}" for p in probs)
            if e.route == ROUTE_POOL and e.probe.get("via_pool") != "yes":
                problems.append(f"{e.name}:pool_host_without_pool_answer")
    report = PreflightReport(django_env=django_env, endpoints=endpoints, problems=problems)
    report.overlay_runtime_enabled = bool(getattr(settings_obj, "CORE_CONFIG_RUNTIME_ENABLED", False))
    if read_overlay:
        try:
            from apps.rag_core.models.core_config import CoreConfigOverride

            report.overlay_gateway_overrides = CoreConfigOverride.objects.filter(
                key="AI_GATEWAY_BASE_URL").count()
        except Exception:  # noqa: BLE001
            report.overlay_gateway_overrides = -1
            problems.append("overlay_unreadable")
        if report.overlay_runtime_enabled and report.overlay_gateway_overrides > 0:
            problems.append("db_overlay_overrides_ai_gateway_base_url")
    return report
