"""Catalogue de chat filtré sur la capacité RÉELLE de `/v1/generate` (GATE-2).

Le défaut corrigé : `/v1/models` relaie les capacités d'Ollama, où `gpt-oss:20b` et
`qwen3:30b-a3b-thinking-2507` portent `completion`. Le registre de la Gateway, lui,
les inscrit en `chat` seul, et `/v1/generate` les refuse en 400
`completion_not_supported` (02/11/2026). RagATM les
proposait et les acceptait : un espace ainsi configuré échouait à chaque question.

Aucun appel réseau, aucun GPU : registres simulés, fidèles à la capture de staging.
"""
import time

import pytest

from apps.llm_router import model_catalog as mc
from apps.llm_router.qualification import chat_output_policy_rejection

from tests_root.unit.test_model_catalog import (
    EXTRA_GATEWAY_MODELS,
    GATEWAY_MODELS,
    GATEWAY_RUNTIME,
    _rt,
)

ALL_MODELS = list(GATEWAY_MODELS) + list(EXTRA_GATEWAY_MODELS)


@pytest.fixture
def cat(monkeypatch, settings):
    """État réel du 02/11/2026 : 4 modèles `completion` côté Ollama mais pas côté registre."""
    settings.AI_GATEWAY_BASE_URL = "http://gw.test:8100"
    settings.AI_GATEWAY_EMBEDDINGS_ENABLED = True
    settings.AI_GATEWAY_EMBEDDINGS_BASE_URL = "http://gw.test:8200"
    settings.RAG_EMBEDDING_DIM = 768
    state = {"models": list(ALL_MODELS), "runtime": dict(GATEWAY_RUNTIME)}
    mc.invalidate_catalog_cache()
    monkeypatch.setattr(mc, "catalog_applicable", lambda: True)
    monkeypatch.setattr(mc, "gateway_models", lambda **kw: (state["models"], False))
    monkeypatch.setattr(mc, "gateway_runtime_models", lambda **kw: (state["runtime"], False))
    yield state
    mc.invalidate_catalog_cache()


def _chat(cat_state=None):
    return {e["id"]: e for e in mc.build_catalog()["chat"]}


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("model, status, code", [
    ("qwen3:30b-instruct", mc.GEN_SUPPORTED, None),
    ("mistral-small3.2:24b", mc.GEN_SUPPORTED, None),
    ("llama3.1:8b", mc.GEN_SUPPORTED, None),
    # inscrits en `chat` seul : /v1/generate les refuse
    ("gpt-oss:20b", mc.GEN_UNSUPPORTED, mc.ERR_GENERATION_UNSUPPORTED),
    ("qwen3:30b-a3b-thinking-2507-q4_K_M", mc.GEN_UNSUPPORTED, mc.ERR_GENERATION_UNSUPPORTED),
    # sortie exposant le raisonnement : politique RagATM
    ("semx-minicpm-v4.5:8b-q8_0", mc.GEN_UNSAFE_OUTPUT, mc.ERR_OUTPUT_POLICY),
    # non inscrit au registre : rien ne garantit /v1/generate — fail-closed
    ("deepseek-r1:32b", mc.GEN_NOT_VERIFIED, mc.ERR_GENERATION_NOT_VERIFIED),
])
def test_classification_et_selection(cat, model, status, code):
    e = _chat()[model]
    assert e["available"] is True, "le modèle reste LISTÉ, avec son motif"
    assert e["generation_status"] == status
    assert e["allowed"] is (code is None)
    assert e.get("reason", "") == (code or "")
    assert mc.validate_chat_model(model) == code


def test_gpt_oss_et_qwen_thinking_ne_sont_exclus_que_par_le_registre(cat):
    """Preuve de capacité, pas exclusion par le nom : inscrits avec `completion`
    demain, ils redeviennent sélectionnables sans toucher RagATM."""
    for name, logical in (("gpt-oss:20b", "oss-reasoning"),
                          ("qwen3:30b-a3b-thinking-2507-q4_K_M", "qwen-reasoning")):
        cat["runtime"][name] = _rt(name, logical, ["completion", "chat"])
    chat = _chat()
    assert chat["gpt-oss:20b"]["allowed"] is True
    assert chat["qwen3:30b-a3b-thinking-2507-q4_K_M"]["allowed"] is True


def test_semx_minicpm_reste_exclu_meme_inscrit(cat):
    """La politique de sortie n'est pas une capacité : l'inscription ne la lève pas."""
    cat["runtime"]["semx-minicpm-v4.5:8b-q8_0"] = _rt(
        "semx-minicpm-v4.5:8b-q8_0", "semx-minicpm", ["completion", "vision"])
    assert _chat()["semx-minicpm-v4.5:8b-q8_0"]["allowed"] is False
    assert chat_output_policy_rejection("SEMX-MINICPM-V4.5:autre-tag")


def test_raisonnement_expose_au_registre_est_refuse(cat):
    cat["runtime"]["llama3.1:8b"] = _rt(
        "llama3.1:8b", "llama-chat", ["completion"],
        reasoning={"type": "toggle", "default": "on", "allowed": ["on"],
                   "disable_supported": False, "expose_thinking": True})
    e = _chat()["llama3.1:8b"]
    assert e["generation_status"] == mc.GEN_UNSAFE_OUTPUT and e["allowed"] is False


def test_modele_interdit_sur_cet_environnement(cat):
    cat["runtime"]["gemma3:27b"] = _rt("gemma3:27b", "gemma-large", ["completion"], allowed_here=False)
    assert mc.validate_chat_model("gemma3:27b") == mc.ERR_GENERATION_UNSUPPORTED


def test_un_modele_vision_n_est_pas_compatible_chat_par_principe(cat):
    """`vision` ne vaut ni admission ni refus : seul le registre de génération décide."""
    chat = _chat()
    assert chat["mistral-small3.2:24b"]["allowed"] is True  # vision + completion inscrite
    assert chat["semx-minicpm-v4.5:8b-q8_0"]["allowed"] is False


# ---------------------------------------------------------------------------
# Fixtures génériques — COMPATIBLE / INCOMPATIBLE / UNAVAILABLE / UNKNOWN
# ---------------------------------------------------------------------------

def test_fixtures_generiques(cat):
    cat["models"] += [
        {"name": "compat-local:1b", "capabilities": ["completion"]},
        {"name": "incompat-local:1b", "capabilities": ["completion"]},
        {"name": "unknown-cap:1b", "capabilities": ["completion"]},
    ]
    cat["runtime"]["compat-local:1b"] = _rt("compat-local:1b", "compat", ["completion"])
    cat["runtime"]["incompat-local:1b"] = _rt("incompat-local:1b", "incompat", ["chat"])
    cat["runtime"]["unavailable-local:1b"] = _rt("unavailable-local:1b", "gone", ["completion"])
    assert mc.validate_chat_model("compat-local:1b") is None
    assert mc.validate_chat_model("incompat-local:1b") == mc.ERR_GENERATION_UNSUPPORTED
    assert mc.validate_chat_model("unknown-cap:1b") == mc.ERR_GENERATION_NOT_VERIFIED
    # inscrit au registre mais absent de /v1/models : pas installé, pas proposable
    assert mc.validate_chat_model("unavailable-local:1b") == mc.ERR_UNKNOWN_MODEL
    assert "unavailable-local:1b" not in _chat()


# ---------------------------------------------------------------------------
# Indisponibilités et évolutions du registre
# ---------------------------------------------------------------------------

def test_registre_de_generation_illisible_fail_closed(cat):
    cat["runtime"] = None
    payload = mc.build_catalog()
    assert payload["available"] is True
    assert payload["generation_registry_available"] is False
    assert all(not e["allowed"] for e in payload["chat"]), "aucun modèle sélectionnable"
    assert mc.validate_chat_model("qwen3:30b-instruct") == mc.ERR_GENERATION_NOT_VERIFIED
    assert payload["default_chat_status"] == mc.ERR_GENERATION_NOT_VERIFIED


def test_liste_vide(cat):
    cat["models"] = []
    payload = mc.build_catalog()
    assert payload["chat"] == []
    assert payload["default_chat_status"] == mc.ERR_UNKNOWN_MODEL
    assert mc.validate_chat_model("qwen3:30b-instruct") == mc.ERR_UNKNOWN_MODEL


def test_catalogue_indisponible(cat):
    cat["models"] = None
    assert mc.build_catalog()["error"] == mc.ERR_UNAVAILABLE
    assert mc.validate_chat_model("qwen3:30b-instruct") == mc.ERR_UNAVAILABLE


def test_modele_ajoute_disparu_capacite_changee(cat):
    cat["models"].append({"name": "nouveau:7b", "capabilities": ["completion"]})
    cat["runtime"]["nouveau:7b"] = _rt("nouveau:7b", "nouveau", ["completion"])
    assert mc.validate_chat_model("nouveau:7b") is None
    cat["models"] = [m for m in cat["models"] if m["name"] != "nouveau:7b"]
    assert mc.validate_chat_model("nouveau:7b") == mc.ERR_UNKNOWN_MODEL
    cat["runtime"]["llama3.1:8b"] = _rt("llama3.1:8b", "llama-chat", ["chat"])
    assert mc.validate_chat_model("llama3.1:8b") == mc.ERR_GENERATION_UNSUPPORTED


def test_registre_runtime_perime_reste_servi(monkeypatch, settings):
    """Même politique que `/v1/models` : dernier état connu servi et signalé."""
    from tests_root.unit.test_model_catalog import _REAL_GATEWAY_RUNTIME

    settings.AI_GATEWAY_BASE_URL = "http://gw.test:8100"
    monkeypatch.setattr(mc, "gateway_runtime_models", _REAL_GATEWAY_RUNTIME)
    mc.invalidate_catalog_cache()
    calls = {"n": 0}

    def _fetch(timeout_s=5.0):
        calls["n"] += 1
        if calls["n"] == 1:
            return dict(GATEWAY_RUNTIME)
        raise RuntimeError("injoignable")

    monkeypatch.setattr(mc, "_fetch_gateway_runtime_models", _fetch)
    first, stale = mc.gateway_runtime_models()
    assert first and stale is False
    mc._RUNTIME_CACHE["fetched_at"] = time.monotonic() - mc.CATALOG_TTL_SECONDS - 1
    again, stale = mc.gateway_runtime_models()
    assert again == first and stale is True
    mc.invalidate_catalog_cache()
    assert mc.gateway_runtime_models() == (None, False)


def test_lecture_du_registre_runtime(monkeypatch, settings):
    """Le parseur indexe `llm_models` par nom amont ET par nom logique, et refuse
    une forme inattendue."""
    settings.AI_GATEWAY_BASE_URL = "http://gw.test:8100"

    class _R:
        def __init__(self, body):
            self._b = body

        def raise_for_status(self):
            return None

        def json(self):
            return self._b

    bodies = [{"llm_models": [GATEWAY_RUNTIME["gpt-oss:20b"]]}, {"status": "ok"}]
    monkeypatch.setattr(mc.requests, "get", lambda url, timeout: _R(bodies.pop(0)))
    assert set(mc._fetch_gateway_runtime_models()) == {"gpt-oss:20b", "oss-reasoning"}
    with pytest.raises(RuntimeError):
        mc._fetch_gateway_runtime_models()


# ---------------------------------------------------------------------------
# Recommandation = défaut effectif
# ---------------------------------------------------------------------------

def test_recommande_est_le_defaut_effectif(cat, settings):
    settings.DEFAULT_RAGSPACE_CHAT_MODEL = "qwen3:30b-instruct"
    payload = mc.build_catalog()
    recos = [e["id"] for e in payload["chat"] if e["recommended"]]
    assert recos == ["qwen3:30b-instruct"]
    assert payload["default_chat_model"] == "qwen3:30b-instruct"
    assert payload["default_chat_status"] == "available"
    assert "par défaut opérationnel" in payload["recommended_chat_reason"]
    assert "meilleur" not in payload["recommended_chat_reason"].lower()


def test_le_badge_suit_un_autre_defaut(cat, settings):
    """Badge et présélection ne peuvent plus diverger."""
    settings.DEFAULT_RAGSPACE_CHAT_MODEL = "mistral-small3.2:24b"
    recos = [e["id"] for e in mc.build_catalog()["chat"] if e["recommended"]]
    assert recos == ["mistral-small3.2:24b"]


@pytest.mark.parametrize("default, status", [
    ("gpt-oss:20b", mc.ERR_GENERATION_UNSUPPORTED),
    ("modele-retire:1b", mc.ERR_UNKNOWN_MODEL),
])
def test_defaut_inutilisable_n_est_ni_recommande_ni_substitue(cat, settings, default, status):
    settings.DEFAULT_RAGSPACE_CHAT_MODEL = default
    payload = mc.build_catalog()
    assert [e for e in payload["chat"] if e["recommended"]] == [], "aucun substitut"
    assert payload["default_chat_status"] == status
