"""RAGatm Open — gardes de l'édition ouverte.

Ces tests prouvent, sur l'arbre et sur le code en exécution, les invariants qui définissent
l'édition Open. Ils raisonnent par **listes fermées de ce qui est public** (allowlists) : tout
module, route, file de tâches, espace de noms ou réglage hors de ces listes fait échouer la
garde, sans que la garde ait à nommer ce qui est exclu.

- le périmètre applicatif est exactement celui déclaré ici (applications Django, paquets d'API,
  routes frontend, espaces de noms i18n, files Celery et rôles de workers) ;
- aucun fournisseur d'IA externe n'est constructible : la génération et les embeddings
  passent par l'AI Gateway, le repli externe n'existe pas, la configuration d'un provider
  externe est refusée au démarrage ;
- l'identité des embeddings est gouvernée (un seul chemin d'encodage réel) ;
- les capacités optionnelles (ASR, Vision) sont éteintes par défaut ;
- aucun défaut de configuration ne désigne une infrastructure privée.
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest
from django.conf import settings
from django.urls import URLPattern, URLResolver, get_resolver

ROOT = pathlib.Path(__file__).resolve().parents[3]
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"

# --------------------------------------------------------------------------- périmètre public
#: Applications Django locales (`apps.<label>`) de l'édition Open — liste FERMÉE.
PUBLIC_APP_LABELS = frozenset({"accounts", "audit", "chat", "corpus", "feedback", "llm_router", "rag_core", "workspaces"})
#: Paquets Python admis sous `backend/apps/` (applications + paquets utilitaires non-Django).
PUBLIC_APP_PACKAGES = PUBLIC_APP_LABELS | frozenset({"actions", "help"})
#: Paquets admis sous `backend/api/v1/`.
PUBLIC_API_PACKAGES = frozenset({
    "admin_core_config", "admin_corpus", "analyses", "audit", "auth", "bootstrap", "capabilities", "chat", "core",
    "corpus_documents", "documents", "invitations", "me", "model_catalog", "platform_admin", "prompts", "ragspaces",
    "tabular", "workspaces",
})
#: Premier segment des routes publiées sous `/api/v1/` — liste FERMÉE.
PUBLIC_API_PREFIXES = frozenset({
    "tabular", "chat", "feedback", "documents", "reindex-jobs", "capabilities", "ai", "prompts", "auth", "me",
    "workspaces", "invitations", "rag-spaces", "analyses", "bootstrap", "core", "audit", "admin", "admin-core",
    "platform-admin", "platform", "business-domains", "domain-corpora", "domain-corpus-documents", "corpus-documents",
    "retrieve", "health", "model-catalog",
})
#: Répertoires de routes de l'application Next.js (`frontend/src/app/`).
PUBLIC_FRONTEND_ROUTES = frozenset({
    "accept-invitation", "admin-core", "api", "chat", "corpora", "doc", "documents", "home", "login",
    "platform-admin", "ragatm-admin", "reset-password", "workspace-admin",
})
#: Files Celery consommées par un worker des composes officiels, et rôles de nœuds.
PUBLIC_QUEUES = frozenset({"celery", "analytics", "reindex", "maintenance"})
PUBLIC_WORKER_ROLES = frozenset({
    "worker", "worker_reindex", "worker_visual", "worker_audio", "worker_ifc", "worker_dwg", "worker_analytics",
    "worker_maintenance",
})
#: Préfixes de noms de réglages désignant un fournisseur d'IA externe (jamais dans le registre Open).
EXTERNAL_PROVIDER_SETTING_PREFIXES = ("OPENAI_", "ANTHROPIC_", "MISTRAL_", "COHERE_", "GOOGLE_OAUTH_", "GEMINI_")
PRIVATE_DEFAULT_RE = re.compile(r"192\.168\.|\b10\.\d+\.\d+\.\d+|\.lan\b|\.local\b|/home/|/mnt/", re.I)


def _python_files():
    for p in BACKEND.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        yield p


def _imported_modules(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


def _packages_under(directory: pathlib.Path) -> set[str]:
    return {p.name for p in directory.iterdir() if p.is_dir() and p.name != "__pycache__"}


def _route_strings(patterns, prefix="") -> list[str]:
    out: list[str] = []
    for p in patterns:
        piece = str(p.pattern).lstrip("^").rstrip("$")
        if isinstance(p, URLResolver):
            out.extend(_route_strings(p.url_patterns, prefix + piece))
        elif isinstance(p, URLPattern):
            out.append(prefix + piece)
    return out


# --------------------------------------------------------------------------- périmètre applicatif
def test_installed_local_apps_are_exactly_the_public_allowlist():
    labels = {a.split(".")[1] for a in settings.INSTALLED_APPS if a.startswith("apps.")}
    assert labels == PUBLIC_APP_LABELS, labels ^ PUBLIC_APP_LABELS


def test_backend_tree_has_no_package_outside_the_allowlist():
    assert _packages_under(BACKEND / "apps") <= PUBLIC_APP_PACKAGES, _packages_under(BACKEND / "apps") - PUBLIC_APP_PACKAGES
    assert _packages_under(BACKEND / "api" / "v1") <= PUBLIC_API_PACKAGES, _packages_under(BACKEND / "api" / "v1") - PUBLIC_API_PACKAGES
    # Toute application locale présente dans l'arbre est installée : pas de module dormant.
    assert PUBLIC_APP_LABELS <= _packages_under(BACKEND / "apps")


def test_no_source_imports_a_module_outside_the_public_tree():
    offenders = []
    for p in _python_files():
        for m in _imported_modules(p):
            if m.startswith("apps.") and m.split(".")[1] not in PUBLIC_APP_PACKAGES:
                offenders.append(f"{p.relative_to(ROOT)} -> {m}")
            if m.startswith("api.v1.") and m.split(".")[2] not in PUBLIC_API_PACKAGES | {"urls", "limits", "exception_handler"}:
                offenders.append(f"{p.relative_to(ROOT)} -> {m}")
    assert offenders == [], offenders


def test_api_routes_use_only_public_prefixes():
    routes = [r for r in _route_strings(get_resolver().url_patterns) if r.startswith("api/v1/")]
    assert routes, "aucune route api/v1 résolue"
    segments = {r[len("api/v1/"):].split("/")[0] for r in routes}
    segments.discard("")  # racine `api/v1/`
    assert segments <= PUBLIC_API_PREFIXES, sorted(segments - PUBLIC_API_PREFIXES)


def test_celery_wiring_stays_in_the_public_perimeter():
    for task, route in settings.CELERY_TASK_ROUTES.items():
        assert task.split(".")[0] == "apps" and task.split(".")[1] in PUBLIC_APP_LABELS, task
        assert route["queue"] in PUBLIC_QUEUES, (task, route)
    for name, entry in settings.CELERY_BEAT_SCHEDULE.items():
        assert entry["task"].split(".")[1] in PUBLIC_APP_LABELS, (name, entry["task"])
        assert entry.get("options", {}).get("queue", "celery") in PUBLIC_QUEUES, name
    from apps.rag_core.checks_operations import KNOWN_QUEUES
    from apps.rag_core.services.capabilities import registry as cap_registry
    from apps.rag_core.services.capabilities.runtime import NODE_ROLES

    assert frozenset(KNOWN_QUEUES) == PUBLIC_QUEUES
    assert set(NODE_ROLES.values()) <= PUBLIC_WORKER_ROLES, set(NODE_ROLES.values()) - PUBLIC_WORKER_ROLES
    joined = " ".join(str(x) for x in vars(cap_registry).values() if isinstance(x, (tuple, list, frozenset, set, dict)))
    roles = set(re.findall(r"\bworker(?:_[a-z]+)?\b", joined))
    assert roles <= PUBLIC_WORKER_ROLES, roles - PUBLIC_WORKER_ROLES


def test_migration_graph_only_contains_public_local_apps():
    from django.db.migrations.loader import MigrationLoader

    loader = MigrationLoader(None, ignore_no_migrations=True)
    apps_with_migrations = {app for app, _ in loader.graph.nodes}
    local = {a for a in apps_with_migrations if (BACKEND / "apps" / a).exists()}
    assert local <= PUBLIC_APP_LABELS, local - PUBLIC_APP_LABELS
    assert {"rag_core", "corpus", "workspaces", "accounts"} <= apps_with_migrations


def test_registry_has_no_external_provider_or_private_definitions():
    from config.registry import all_definitions

    names = [d.name for d in all_definitions()]
    bad = [n for n in names if n.startswith(EXTERNAL_PROVIDER_SETTING_PREFIXES) or n == "DEFAULT_OPENAI_MODEL"]
    assert bad == [], bad
    private_defaults = [d.name for d in all_definitions() if PRIVATE_DEFAULT_RE.search(str(d.default or ""))]
    assert private_defaults == [], private_defaults
    gw = {d.name: d for d in all_definitions()}
    assert gw["AI_GATEWAY_FALLBACK_PROVIDER"].choices == ("none",)
    assert gw["AI_GATEWAY_FALLBACK_PROVIDER"].default == "none"
    assert gw["AI_GATEWAY_FALLBACK_ENABLED"].default is False
    assert gw["EMBEDDINGS_PROVIDER"].default == "ai_gateway"
    assert gw["DEFAULT_RAGSPACE_EMBEDDING_PROVIDER"].default == "ai_gateway"
    assert gw["AI_GATEWAY_REQUIRED_BASE_URL"].default == ""
    assert "AI_GATEWAY_POOL_HOSTS" in gw


@pytest.mark.django_db
def test_capabilities_endpoint_announces_only_public_capabilities(client, django_user_model):
    from rest_framework.test import APIClient

    user = django_user_model.objects.create_user(username="open-cap", email="open-cap@example.test", password="pw")
    api = APIClient()
    api.force_authenticate(user)
    resp = api.get("/api/v1/capabilities/")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"providers", "embeddings", "reindex", "ingest", "ragspace_model_policy"}, sorted(body)
    assert set(body["ingest"]) == {"formats", "image_formats", "msg", "eml", "audio"}, sorted(body["ingest"])
    assert set(body["providers"]) <= {"ollama", "ai_gateway"}, sorted(body["providers"])


def test_frontend_routes_and_namespaces_are_closed_sets():
    routes = {p.name for p in (FRONTEND / "src" / "app").iterdir() if p.is_dir()}
    assert routes == PUBLIC_FRONTEND_ROUTES, routes ^ PUBLIC_FRONTEND_ROUTES
    cfg = (FRONTEND / "src" / "i18n" / "config.ts").read_text(encoding="utf-8")
    declared = set(re.findall(r'^\s*"([A-Za-z]+)",\s*$', cfg.split("NAMESPACES")[1].split("]")[0], re.M))
    for loc in ("en", "fr"):
        on_disk = {p.stem for p in (FRONTEND / "messages" / loc).glob("*.json")}
        assert on_disk == declared, (loc, on_disk ^ declared)
    setup = (ROOT / "tests" / "frontend" / "setup.ts").read_text(encoding="utf-8")
    in_tests = set(re.findall(r'"([A-Za-z]+)"', setup.split("NAMESPACES = [")[1].split("]")[0]))
    assert in_tests == declared, in_tests ^ declared


# --------------------------------------------------------------------------- IA externe
def test_provider_allowlists_are_local_only():
    from apps.llm_router import capabilities as cap
    from apps.llm_router.egress import EXTERNAL_PROVIDERS

    for allowed in (cap.ALLOWED_CHAT_PROVIDERS, cap.ALLOWED_EMBEDDING_PROVIDERS,
                    cap.ALLOWED_RAGSPACE_EMBEDDING_PROVIDERS):
        assert not set(allowed) & set(EXTERNAL_PROVIDERS), allowed
    assert cap.ALLOWED_EMBEDDING_PROVIDERS == ("ai_gateway",)
    assert cap.ALLOWED_RAGSPACE_EMBEDDING_PROVIDERS == ("ai_gateway",)
    assert "openai" not in cap.provider_capabilities()
    from apps.corpus.domain.embedding_identity import ALLOWED_CORPUS_EMBEDDING_PROVIDERS, DEFAULT_CORPUS_EMBEDDING_PROVIDER

    assert ALLOWED_CORPUS_EMBEDDING_PROVIDERS == frozenset({"ai_gateway"})
    assert DEFAULT_CORPUS_EMBEDDING_PROVIDER == "ai_gateway"


def test_router_has_no_external_generation_path(monkeypatch):
    from apps.llm_router import router

    assert not hasattr(router, "_openai_generate") and not hasattr(router, "_openai_stream")
    calls = []
    monkeypatch.setattr(router.requests, "post", lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError("network")))
    monkeypatch.setattr(router.requests, "get", lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError("network")))
    from apps.llm_router.egress import EgressPolicyError

    # Refus AVANT tout appel : soit la politique d'egress (contexte absent = refus), soit
    # l'absence même du provider dans l'édition Open. Jamais une requête réseau.
    with pytest.raises((router.LLMError, EgressPolicyError)):
        router._generate_text_direct(prompt="x", provider="openai", model="gpt-4.1-mini", timeout_s=1)
    with pytest.raises(router.LLMError):
        router._default_model_for_provider("anthropic")
    assert calls == []


def test_gateway_fallback_never_calls_a_provider(monkeypatch, settings):
    from apps.llm_router import router
    from apps.llm_router.gateway import GatewayTemporaryUnavailable, GatewayUnavailableError

    monkeypatch.setattr(router, "_gateway_fallback_enabled", lambda: True)
    monkeypatch.setattr(router, "_gateway_fallback_provider", lambda: "none")
    monkeypatch.setattr(router, "_generate_text_direct", lambda **k: pytest.fail("direct provider called"))
    err = GatewayTemporaryUnavailable("boom")
    with pytest.raises(GatewayUnavailableError):
        router._generate_gateway_fallback(prompt="x", provider=None, model=None, timeout_s=1, gateway_error=err)
    monkeypatch.setattr(router, "_gateway_fallback_provider", lambda: "openai")
    from django.core.exceptions import ImproperlyConfigured

    with pytest.raises(ImproperlyConfigured):
        router._generate_gateway_fallback(prompt="x", provider=None, model=None, timeout_s=1, gateway_error=err)


def test_embeddings_refuse_external_and_direct_providers(settings, monkeypatch):
    from apps.llm_router import embeddings as emb
    from apps.llm_router.egress import EgressPolicyError

    assert not hasattr(emb, "_openai_embed") and not hasattr(emb, "_ollama_embed")
    settings.EMBEDDINGS_PROVIDER = "ai_gateway"
    monkeypatch.setattr(emb.requests, "post", lambda *a, **k: pytest.fail("network call attempted"))
    # fournisseur externe : refusé par la politique d'egress, avant tout octet réseau
    with pytest.raises((EgressPolicyError, emb.EmbeddingsError)):
        emb.embed_texts_with_identity(["bonjour"], dim=768, provider="openai", model="text-embedding-3-small")
    # Ollama direct : chemin retiré (identité legacy non vérifiée)
    with pytest.raises(emb.EmbeddingsError, match="not available in RAGatm Open"):
        emb.embed_texts_with_identity(["bonjour"], dim=768, provider="ollama", model="nomic-embed-text")


def test_domain_corpus_cannot_egress():
    from apps.llm_router.egress import domain_corpus_context

    ctx = domain_corpus_context(corpus_id="c1", provider="openai", operation="embedding.query")
    assert ctx.policy.external_embedding_allowed is False
    assert ctx.policy.allowed_external_providers == ()
    assert ctx.policy.policy_source == "domain-corpus-local-only"


def test_system_checks_refuse_external_configuration(settings, monkeypatch):
    from apps.llm_router import checks

    from config import checks as project_checks

    monkeypatch.setenv("OPENAI_API_KEY", "placeholder-value-never-read")
    ids = {e.id for e in project_checks.check_no_external_provider_env_vars(None)}
    assert ids == {"config.E009"}
    monkeypatch.delenv("OPENAI_API_KEY")
    assert project_checks.check_no_external_provider_env_vars(None) == []
    settings.RAG_LLM_PROVIDER = "openai"
    ids = {e.id for e in checks.check_no_external_provider_configuration(None)}
    assert "llm_router.E009" in ids
    settings.RAG_LLM_PROVIDER = "ollama"
    assert checks.check_no_external_provider_configuration(None) == []
    settings.AI_GATEWAY_FALLBACK_PROVIDER = "openai"
    ids = {e.id for e in checks.check_ai_gateway_fallback_provider(None)}
    assert ids == {"llm_router.E004"}
    settings.AI_GATEWAY_FALLBACK_PROVIDER = "none"
    assert checks.check_ai_gateway_fallback_provider(None) == []
    assert not hasattr(checks, "_has_openai_key")


def test_no_source_builds_an_external_ai_url():
    pattern = re.compile(r"api\.openai\.com|api\.anthropic\.com|generativelanguage\.googleapis|api\.mistral\.ai|api\.cohere")
    offenders = [str(p.relative_to(ROOT)) for p in _python_files() if pattern.search(p.read_text(encoding="utf-8", errors="replace"))]
    assert offenders == [], offenders


# --------------------------------------------------------------------------- identité embeddings
def test_embedding_identity_contract_is_governed(settings):
    from apps.llm_router.embedding_pipeline import is_governed_pipeline_id, is_legacy_pipeline_id, pipeline_family
    from apps.rag_core.models.chunk import EMBEDDING_VECTOR_DIM

    assert EMBEDDING_VECTOR_DIM == 768
    assert settings.RAG_EMBEDDING_DIM == 768
    assert is_governed_pipeline_id("emb-v2:abcdef")
    assert is_legacy_pipeline_id("legacy:stub:x:768:legacy-v1")
    assert pipeline_family("legacy:stub:x:768:legacy-v1") == "legacy:stub:x:768:legacy-v1"
    from apps.llm_router.embeddings import default_embedding_model_for_provider

    assert default_embedding_model_for_provider("ai_gateway")  # profil logique, jamais vide


def test_ragspace_and_corpus_provider_choices():
    from apps.corpus.models import DomainCorpus
    from apps.rag_core.models import RagSpace

    assert [c[0] for c in RagSpace._meta.get_field("llm_provider").choices] == ["ollama"]
    assert [c[0] for c in RagSpace._meta.get_field("embedding_provider").choices] == ["ai_gateway"]
    assert [c[0] for c in DomainCorpus._meta.get_field("embedding_provider").choices] == ["ai_gateway"]
    assert DomainCorpus._meta.get_field("embedding_provider").default == "ai_gateway"


# --------------------------------------------------------------------------- formats / capacités
def test_msg_is_refused_with_a_stable_code():
    from apps.rag_core.services.ingest import extraction as X
    from apps.rag_core.services.ingest.extraction.email_model import MSG_UNSUPPORTED_CODE, parser_available

    assert parser_available() is False
    assert X.msg_capability()["status"] in ("disabled_by_config", "unsupported")
    assert X.msg_capability()["body_ingestion"] is False
    assert MSG_UNSUPPORTED_CODE == "msg_unsupported_in_open_edition"
    assert not (BACKEND / "apps/rag_core/services/ingest/extraction/msg_extractor.py").exists()


def test_asr_and_vision_are_off_by_default():
    from config.registry import get

    assert get("DOCUMENT_AUDIO_INGEST_ENABLED").default is False
    assert get("DOCUMENT_AUDIO_GLOBAL_ENABLED").default is False
    assert get("DOCUMENT_VISUAL_ANALYSIS_ENABLED").default is False
    assert get("DOCUMENT_OCR_ENABLED").default is False


def test_core_dependencies_exclude_copyleft_libraries():
    text = (BACKEND / "pyproject.toml").read_text(encoding="utf-8")
    core = text.split("[project.optional-dependencies]")[0]
    assert '"PyMuPDF' not in core and '"extract-msg' not in core
    assert "pdf-mupdf" in text  # extra optionnel, jamais installé par défaut
