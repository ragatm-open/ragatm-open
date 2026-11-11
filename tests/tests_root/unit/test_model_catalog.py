"""Catalogue des modèles — projection, allowlist, qualification, recommandation.

Le catalogue existe parce que la seule validation d'un nom de modèle était une regex
de caractères. `bge-m3` passait la regex, marquait tous les documents d'un espace
`reindex_required`, puis échouait à la première ingestion sur un écart de dimension.
La passerelle publiait pourtant `capabilities`, `dimension` et `registry_status` :
personne ne les lisait.
"""
import time

import pytest

from apps.llm_router import model_catalog as mc
from apps.llm_router.qualification import (
    QUALIFIED_CHAT_MODELS,
    QUALIFIED_EMBEDDING_PIPELINES,
    RECOMMENDED_EMBEDDING_PIPELINE,
)

# Extrait FIDÈLE de `GET /v1/models` de la passerelle de staging (22/08/2026).
# Fidèle et non simplifié : c'est ce qui permet au test d'attraper une régression de
# projection, pas seulement une erreur de logique.
GATEWAY_MODELS = [
    {"name": "bge-m3:latest", "digest": "79076464…",
     "details": {"family": "bert", "context_length": 8192, "embedding_length": 1024},
     "capabilities": ["embedding"]},
    {"name": "granite-embedding:278m", "digest": "1a37926bf842…",
     "details": {"family": "bert", "context_length": 512, "embedding_length": 768},
     "capabilities": ["embedding"], "model_logical": "fr-en-retrieval",
     "kind": "embedding", "dimension": 768, "registry_status": "ready"},
    {"name": "gemma3:27b", "details": {"family": "gemma3"}, "capabilities": ["completion"]},
    {"name": "qwen3:30b-instruct", "details": {"family": "qwen3moe"},
     "capabilities": ["completion", "tools"], "model_logical": "qwen-large", "kind": "llm"},
    {"name": "mistral-small3.2:24b", "details": {},
     "capabilities": ["vision", "completion", "tools"], "model_logical": "vision-large"},
    {"name": "qwen3:14b", "details": {}, "capabilities": ["completion", "tools", "thinking"],
     "model_logical": "qwen-judge"},
    {"name": "nomic-embed-text:latest", "digest": "0a109f42…",
     "details": {"family": "nomic-bert", "embedding_length": 768},
     "capabilities": ["embedding"], "model_logical": "legacy-768",
     "kind": "embedding", "dimension": 768, "registry_status": "ready"},
    {"name": "llama3.1:8b", "details": {}, "capabilities": ["completion", "tools"],
     "model_logical": "llama-chat"},
    # Profil Compact-8 du registre AI Gateway Open : nom amont + nom LOGIQUE.
    {"name": "qwen2.5:1.5b-instruct", "details": {"family": "qwen2"},
     "capabilities": ["completion", "structured_output"],
     "model_logical": "compact-generation-8", "kind": "llm"},
]


def _rt(name, logical, caps, **kw):
    """Entrée `llm_models` de `/health/runtime` — champs réellement publiés (02/11/2026)."""
    d = {"ollama_name": name, "model_logical": logical, "capabilities": caps,
         "allowed_here": True, "production_allowed": True, "staging_allowed": True,
         "reasoning": {"type": "none", "default": None, "allowed": ["none"],
                       "disable_supported": True, "expose_thinking": False}}
    d.update(kw)
    return d


# Registre de GÉNÉRATION de la passerelle de staging (`GET /health/runtime`,
# 02/11/2026). C'est lui, et non `/v1/models`, que `/v1/generate` applique.
GATEWAY_RUNTIME = {
    "llama3.1:8b": _rt("llama3.1:8b", "llama-chat", ["completion", "tools"]),
    "qwen2.5:1.5b-instruct": _rt("qwen2.5:1.5b-instruct", "compact-generation-8",
                                 ["completion", "structured_output"]),
    "qwen3:14b": _rt("qwen3:14b", "qwen-judge", ["completion", "tools", "thinking", "structured_output"]),
    "qwen3:30b-instruct": _rt("qwen3:30b-instruct", "qwen-large", ["completion", "tools", "structured_output"]),
    "mistral-small3.2:24b": _rt("mistral-small3.2:24b", "vision-large", ["completion", "vision", "tools", "structured_output"]),
    "gemma3:27b": _rt("gemma3:27b", "gemma-large", ["completion", "structured_output"]),
    "gpt-oss:20b": _rt("gpt-oss:20b", "oss-reasoning", ["chat", "structured_output"],
                       reasoning={"type": "levels", "default": "low", "allowed": ["low", "medium", "high"],
                                  "disable_supported": False, "expose_thinking": False}),
    "qwen3:30b-a3b-thinking-2507-q4_K_M": _rt(
        "qwen3:30b-a3b-thinking-2507-q4_K_M", "qwen-reasoning", ["chat", "structured_output"],
        reasoning={"type": "toggle", "default": "on", "allowed": ["on"],
                   "disable_supported": False, "expose_thinking": False}),
}

# Modèles que `/v1/models` publie AVEC `completion` mais que le registre de génération
# refuse, ignore ou que RagATM exclut — l'état réel de staging/production au 02/11/2026.
EXTRA_GATEWAY_MODELS = [
    {"name": "gpt-oss:20b", "details": {}, "capabilities": ["completion", "tools", "thinking"],
     "model_logical": "oss-reasoning"},
    {"name": "qwen3:30b-a3b-thinking-2507-q4_K_M", "details": {},
     "capabilities": ["completion", "tools", "thinking"], "model_logical": "qwen-reasoning"},
    {"name": "semx-minicpm-v4.5:8b-q8_0", "details": {}, "capabilities": ["completion", "vision"]},
    {"name": "deepseek-r1:32b", "details": {}, "capabilities": ["completion", "thinking"]},
]


@pytest.fixture
def catalog(monkeypatch, settings):
    """Catalogue réel, sans réseau, avec la passerelle gouvernée exposée."""
    settings.AI_GATEWAY_BASE_URL = "http://gw.test:8100"
    settings.AI_GATEWAY_EMBEDDINGS_ENABLED = True
    settings.AI_GATEWAY_EMBEDDINGS_BASE_URL = "http://gw.test:8200"
    settings.RAG_EMBEDDING_DIM = 768
    mc.invalidate_catalog_cache()
    monkeypatch.setattr(mc, "catalog_applicable", lambda: True)
    monkeypatch.setattr(mc, "gateway_models", lambda **kw: (list(GATEWAY_MODELS), False))
    monkeypatch.setattr(mc, "gateway_runtime_models", lambda **kw: (dict(GATEWAY_RUNTIME), False))
    yield
    mc.invalidate_catalog_cache()


#: Implémentations RÉELLES, capturées avant toute neutralisation : la fixture autouse
#: `_model_catalog_inert` du conftest racine remplace ces symboles pour toute la suite.
_REAL_APPLICABLE = mc.catalog_applicable
_REAL_GATEWAY_MODELS = mc.gateway_models
_REAL_GATEWAY_RUNTIME = mc.gateway_runtime_models


def _by_id(entries):
    return {e["id"]: e for e in entries}


# ---------------------------------------------------------------------------
# Projection — §47
# ---------------------------------------------------------------------------

def test_les_modeles_disponibles_sont_projetes_par_role(catalog):
    cat = mc.build_catalog()
    chat, emb = _by_id(cat["chat"]), _by_id(cat["embeddings"])
    assert set(chat) == {"gemma3:27b", "qwen3:30b-instruct", "mistral-small3.2:24b",
                         "qwen3:14b", "llama3.1:8b", "qwen2.5:1.5b-instruct"}
    # Les embeddings sont indexés par PIPELINE LOGIQUE, pas par nom amont (§2).
    assert "fr-en-retrieval" in emb and "legacy-768" in emb
    assert "granite-embedding:278m" not in emb
    # `bge-m3` n'a pas de modèle logique : il retombe sur son nom amont, et sera refusé.
    assert "bge-m3:latest" in emb


def test_la_mauvaise_dimension_est_refusee_par_le_catalogue(catalog):
    """`bge-m3` : 1024 contre un index en 768. C'est le cas qui a motivé le lot."""
    entry = _by_id(mc.build_catalog()["embeddings"])["bge-m3:latest"]
    assert entry["available"] is True
    assert entry["allowed"] is False
    assert entry["reason"] == mc.ERR_NOT_READY  # pas de modèle logique : pas gouvernable
    assert entry["dimension"] == 1024, "la dimension vient de details.embedding_length"


def test_un_modele_hors_registre_n_est_pas_gouvernable(catalog):
    entry = _by_id(mc.build_catalog()["embeddings"])["bge-m3:latest"]
    assert entry["registry_status"] == ""
    assert entry["qualified"] is False


def test_les_deux_pipelines_gouvernes_sont_allowed_et_qualifies(catalog):
    emb = _by_id(mc.build_catalog()["embeddings"])
    for pid in ("legacy-768", "fr-en-retrieval"):
        assert emb[pid]["allowed"] is True, pid
        assert emb[pid]["qualified"] is True, pid
        assert emb[pid]["dimension"] == 768
        assert emb[pid]["registry_status"] == "ready"
    assert emb["legacy-768"]["upstream_model"] == "nomic-embed-text:latest"
    assert emb["fr-en-retrieval"]["upstream_model"] == "granite-embedding:278m"


def test_la_qualification_est_attachee_a_l_entree(catalog):
    entry = _by_id(mc.build_catalog()["embeddings"])["fr-en-retrieval"]
    q = entry["qualification"]
    assert q["environment"] == "staging"
    assert "170,5 ms" in q["latency_reference"]
    assert q["qualification_id"] and q["qualified_at"] and q["source_lot"]


def test_available_sans_qualified_est_un_etat_valide(catalog):
    """§10 : figurer au catalogue ne vaut pas qualification."""
    entry = _by_id(mc.build_catalog()["chat"])["gemma3:27b"]
    assert (entry["available"], entry["allowed"], entry["qualified"], entry["recommended"]) == (
        True, True, False, False)


def test_le_catalogue_public_ne_porte_ni_digest_ni_url(catalog):
    cat = mc.build_catalog()
    blob = repr(cat)
    assert "1a37926bf842" not in blob
    assert "gw.test" not in blob


# ---------------------------------------------------------------------------
# Environnement — §46
# ---------------------------------------------------------------------------

def test_sans_passerelle_gouvernee_le_pipeline_est_available_mais_pas_allowed(catalog, settings):
    """La même entrée est `allowed` en staging et refusée ailleurs, avec le motif."""
    settings.AI_GATEWAY_EMBEDDINGS_ENABLED = False
    entry = _by_id(mc.build_catalog()["embeddings"])["fr-en-retrieval"]
    assert entry["available"] is True
    assert entry["allowed"] is False
    assert entry["reason"] == "gateway_embeddings_not_enabled"
    assert entry["recommended"] is False, "on ne recommande pas ce qu'on interdit ici"


# ---------------------------------------------------------------------------
# Recommandation — §13
# ---------------------------------------------------------------------------

def test_recommended_implique_qualified_et_allowed(catalog):
    cat = mc.build_catalog()
    for kind in ("chat", "embeddings"):
        for e in cat[kind]:
            if e["recommended"]:
                assert e["allowed"] and e["available"], e["id"]
                if kind == "embeddings":
                    assert e["qualified"], e["id"]


def test_un_seul_recommande_par_usage(catalog):
    cat = mc.build_catalog()
    for kind in ("chat", "embeddings"):
        assert sum(1 for e in cat[kind] if e["recommended"]) == 1, kind


def test_les_recommandes_du_registre_sont_qualifies(settings):
    """Invariant du registre : le pipeline recommandé est qualifié, et le défaut de chat du
    CODE est au moins ADMIS par le catalogue. Édition Open : le défaut est le nom
    LOGIQUE `compact-generation-8` du registre AI Gateway Open (profil CPU / 8 Go), résolu
    par alias vers l'entrée amont `qwen2.5:1.5b-instruct` ; il n'est pas couvert par une
    fiche de qualification héritée (celles-ci décrivent le programme privé)."""
    assert RECOMMENDED_EMBEDDING_PIPELINE in QUALIFIED_EMBEDDING_PIPELINES
    assert settings.DEFAULT_RAGSPACE_CHAT_MODEL == "compact-generation-8"
    assert mc.validate_chat_model(settings.DEFAULT_RAGSPACE_CHAT_MODEL) is None


# ---------------------------------------------------------------------------
# Validation — §17, §19-21
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("model, expected", [
    ("qwen3:30b-instruct", None),
    ("llama3.1:8b", None),                       # allowed sans être qualifié
    ("compact-generation-8", None), # nom LOGIQUE du registre Compact
    ("qwen2.5:1.5b-instruct", None),             # son nom amont : même entrée
    ("qwen3:30b-instrcut", mc.ERR_UNKNOWN_MODEL),  # faute de frappe — §20
    ("nomic-embed-text:latest", mc.ERR_WRONG_CAPABILITY),  # embedding en chat — §21
    ("", None),                                  # vide => défaut du provider — §24
])
def test_validation_chat(catalog, model, expected):
    assert mc.validate_chat_model(model) == expected


@pytest.mark.parametrize("provider, model, expected", [
    ("ai_gateway", "fr-en-retrieval", None),
    ("ai_gateway", "legacy-768", None),
    ("ai_gateway", "bge-m3:latest", mc.ERR_DIMENSION),          # §19
    ("ai_gateway", "granite-embedding:278m", mc.ERR_NOT_READY), # nom amont ≠ pipeline
    ("ai_gateway", "qwen3:30b-instruct", mc.ERR_WRONG_CAPABILITY),  # §21
    ("ai_gateway", "inexistant", mc.ERR_UNKNOWN_MODEL),
    ("ollama", "nomic-embed-text", None),        # chemin historique, par nom amont
    ("ollama", "bge-m3", mc.ERR_DIMENSION),      # §19 sur le chemin historique aussi
    ("ai_gateway", "", None),                    # vide => défaut gouverné
])
def test_validation_embeddings(catalog, provider, model, expected):
    assert mc.validate_embedding_model(provider, model) == expected


# ---------------------------------------------------------------------------
# Indisponibilité — §22, §40
# ---------------------------------------------------------------------------

def test_passerelle_declaree_mais_injoignable_refuse(monkeypatch, settings):
    """Fail-closed : on ne valide pas ce qu'on ne peut pas vérifier."""
    settings.AI_GATEWAY_BASE_URL = "http://gw.test:8100"
    mc.invalidate_catalog_cache()
    monkeypatch.setattr(mc, "catalog_applicable", lambda: True)
    monkeypatch.setattr(mc, "gateway_models", lambda **kw: (None, False))
    assert mc.validate_chat_model("qwen3:30b-instruct") == mc.ERR_UNAVAILABLE
    assert mc.build_catalog()["error"] == mc.ERR_UNAVAILABLE


def test_aucune_passerelle_declaree_ne_gouverne_rien(monkeypatch, settings):
    """Distinct d'une panne : sans passerelle, il n'y a rien à gouverner."""
    settings.AI_GATEWAY_BASE_URL = ""
    monkeypatch.setattr(mc, "catalog_applicable", _REAL_APPLICABLE)
    monkeypatch.setattr(mc, "gateway_models", _REAL_GATEWAY_MODELS)
    mc.invalidate_catalog_cache()
    assert mc.catalog_applicable() is False
    assert mc.validate_chat_model("n-importe-quoi") is None
    assert mc.build_catalog()["error"] == "model_catalog_not_configured"


def test_un_catalogue_perime_reste_servi_et_signale(monkeypatch, settings):
    """Un catalogue périmé vaut mieux qu'un refus — mais l'appelant doit le savoir.

    Ce test exerce le VRAI `gateway_models`, que la fixture autouse neutralise pour le
    reste de la suite : c'est le cache et sa politique de péremption qui sont mesurés.
    """
    settings.AI_GATEWAY_BASE_URL = "http://gw.test:8100"
    monkeypatch.setattr(mc, "catalog_applicable", _REAL_APPLICABLE)
    monkeypatch.setattr(mc, "gateway_models", _REAL_GATEWAY_MODELS)
    mc.invalidate_catalog_cache()
    calls = {"n": 0}

    def _fetch(timeout_s=5.0):
        calls["n"] += 1
        if calls["n"] == 1:
            return list(GATEWAY_MODELS)
        raise RuntimeError("passerelle injoignable")

    monkeypatch.setattr(mc, "_fetch_gateway_models", _fetch)
    first = mc.build_catalog()
    assert first["available"] is True and first["stale"] is False
    assert len(first["chat"]) == 6

    # Expiration forcée RELATIVEMENT à l'horloge, pas en posant zéro.
    #
    # `gateway_models` compare `time.monotonic() - fetched_at` au TTL, et
    # `time.monotonic()` compte depuis le démarrage de la MACHINE. Poser 0.0
    # n'expire donc le cache que si la machine tourne depuis plus de 300 s : vrai
    # sur un poste de développement, faux sur un runner CI fraîchement provisionné,
    # où le cache est alors jugé frais et `stale` reste False. C'est ce qui faisait
    # échouer ce test en CI et nulle part ailleurs.
    mc._CACHE["fetched_at"] = time.monotonic() - mc.CATALOG_TTL_SECONDS - 1
    cat = mc.build_catalog()
    assert cat["available"] is True and cat["stale"] is True
    assert len(cat["chat"]) == 6, "le dernier état connu est servi tel quel"
    mc.invalidate_catalog_cache()
