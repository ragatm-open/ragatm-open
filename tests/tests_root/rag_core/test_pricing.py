"""Tests unitaires du pricing LLM centralisé (périmètre C+, sans DB).

Édition Open : aucune grille intégrée. Les tests posent une grille SYNTHÉTIQUE par
`LLM_PRICING_PER_1K` pour un provider facturé fictif (`metered`).
"""
import pytest
from django.test import override_settings

from apps.rag_core.services import pricing

TABLE = {"modele-tarife": (0.0004, 0.0016)}
pytestmark = pytest.mark.usefixtures("_table")


@pytest.fixture
def _table(settings):
    settings.LLM_PRICING_PER_1K = dict(TABLE)


# ----------------------------------------------------------------- estimate_cost

def test_known_metered_model_cost():
    # modele-tarife: (0.0004 entrée, 0.0016 sortie) / 1K
    c = pricing.estimate_cost_usd(
        provider="metered", model="modele-tarife", prompt_tokens=1000, completion_tokens=500
    )
    assert c == pytest.approx(0.0004 + 0.0008)


def test_unknown_model_returns_none():
    assert pricing.estimate_cost_usd(
        provider="metered", model="mystery-9000", prompt_tokens=100, completion_tokens=50
    ) is None


def test_missing_split_returns_none_not_invented():
    # contrainte produit : pas de décomposition -> coût indéterminé (None)
    assert pricing.estimate_cost_usd(
        provider="metered", model="modele-tarife", prompt_tokens=None, completion_tokens=None
    ) is None
    assert pricing.estimate_cost_usd(
        provider="metered", model="modele-tarife", prompt_tokens=100, completion_tokens=None
    ) is None


def test_local_provider_is_zero():
    assert pricing.estimate_cost_usd(
        provider="ollama", model="llama3.1:8b", prompt_tokens=100, completion_tokens=50
    ) == 0.0
    assert pricing.is_local_provider("ollama") is True
    assert pricing.is_local_provider("ai_gateway") is True
    assert pricing.is_local_provider("metered") is False


@override_settings(LLM_PRICING_PER_1K={"custom-model": (1.0, 2.0)})
def test_pricing_override_via_settings():
    c = pricing.estimate_cost_usd(
        provider="metered", model="custom-model", prompt_tokens=1000, completion_tokens=1000
    )
    assert c == pytest.approx(1.0 + 2.0)
    # le modèle par défaut n'est plus dans la table surchargée
    assert pricing.estimate_cost_usd(
        provider="metered", model="modele-tarife", prompt_tokens=1000, completion_tokens=1000
    ) is None


def test_no_bundled_price_list_in_open_edition(settings):
    settings.LLM_PRICING_PER_1K = {}
    assert pricing.estimate_cost_usd(
        provider="metered", model="modele-tarife", prompt_tokens=1000, completion_tokens=500
    ) is None


# --------------------------------------------------------------- split_usage

def test_split_generic_input_output():
    assert pricing.split_usage_tokens(
        "metered", {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    ) == (10, 5, 15)


def test_split_generic_legacy_names():
    assert pricing.split_usage_tokens(
        "metered", {"prompt_tokens": 7, "completion_tokens": 3}
    ) == (7, 3, 10)


def test_split_ollama():
    assert pricing.split_usage_tokens(
        "ollama", {"prompt_eval_count": 20, "eval_count": 8}
    ) == (20, 8, 28)


def test_split_total_only_no_split():
    # usage absent mais total connu -> (None, None, total)
    assert pricing.split_usage_tokens("metered", None, total=42) == (None, None, 42)


# --------------------------------------------------------------- compute_cost

def test_compute_cost_from_usage_metered():
    pt, ct, tt, cost = pricing.compute_cost_from_usage(
        provider="metered", model="modele-tarife",
        usage={"input_tokens": 1000, "output_tokens": 500},
    )
    assert (pt, ct, tt) == (1000, 500, 1500)
    assert cost == pytest.approx(0.0012)


def test_compute_cost_total_only_is_indeterminate():
    # usage sans split (total seul) -> tokens total connu, coût None
    pt, ct, tt, cost = pricing.compute_cost_from_usage(
        provider="metered", model="modele-tarife",
        usage={"total_tokens": 1500},
    )
    assert (pt, ct, tt) == (None, None, 1500)
    assert cost is None


def test_compute_cost_ollama_zero():
    pt, ct, tt, cost = pricing.compute_cost_from_usage(
        provider="ollama", model="llama3.1:8b",
        usage={"prompt_eval_count": 100, "eval_count": 40},
    )
    assert (pt, ct, tt) == (100, 40, 140)
    assert cost == 0.0
