# backend/apps/rag_core/services/pricing.py
"""Estimation centralisée, versionnée et configurable du coût LLM (USD).

Source unique de vérité du coût pour TOUS les appels LLM (chat, résumé,
benchmarks avec réponses). Principes (périmètre C+ — coûts réels) :

- prix par 1K tokens ``(entrée, sortie)``, table **versionnée** et **datée**
  (``PRICING_VERSION`` / ``PRICING_SOURCE``), surchargeable par settings ;
- provider **local** (Ollama) : coût ``0.0`` assumé (aucun coût d'API marginal) ;
- modèle **inconnu** d'un provider facturé : coût ``None`` (jamais inventé) ;
- si la **décomposition** prompt/completion n'est pas disponible : coût ``None``
  (on ne devine pas un coût à partir du seul total — cf. contrainte produit).

Aucun secret n'est manipulé ici (uniquement des noms de provider/modèle).
"""
from __future__ import annotations

from typing import Any

from django.conf import settings

# Version de la grille tarifaire. INCRÉMENTER à chaque mise à jour des prix.
PRICING_VERSION = 1
# Référence des prix (à tenir à jour avec PRICING_VERSION).
PRICING_SOURCE = (
    "RAGatm Open : aucune grille intégrée (aucun fournisseur facturé) ; surcharger "
    "LLM_PRICING_PER_1K (USD/1K tokens, (entrée, sortie)) si un modèle facturé est servi."
)

# Providers exécutés localement : pas de coût d'API marginal -> 0.0 (assumé).
LOCAL_PROVIDERS = frozenset({"ollama", "ai_gateway"})

# Prix indicatifs USD / 1K tokens : (entrée, sortie). Surchargeable par settings.
_DEFAULT_PRICING_PER_1K: dict[str, tuple[float, float]] = {}


def _pricing_table() -> dict[str, tuple[float, float]]:
    """Table de prix effective : ``settings.LLM_PRICING_PER_1K`` (générique) ou
    ``settings.SUMMARY_PRICING_PER_1K`` (rétro-compat) sinon le défaut versionné."""
    for name in ("LLM_PRICING_PER_1K", "SUMMARY_PRICING_PER_1K"):
        override = getattr(settings, name, None)
        if isinstance(override, dict) and override:
            return override
    return _DEFAULT_PRICING_PER_1K


def is_local_provider(provider: str | None) -> bool:
    return (provider or "").strip().lower() in LOCAL_PROVIDERS


def split_usage_tokens(
    provider: str | None, usage: Any, *, total: int | None = None
) -> tuple[int | None, int | None, int | None]:
    """Décompose l'``usage`` brut d'un provider en ``(prompt, completion, total)``.

    Best-effort, centralisé (forme générique ``input/output/total_tokens`` ou
    ``prompt/completion_tokens`` ; Ollama ``prompt_eval_count``/``eval_count``). Renvoie ``None`` pour toute
    composante absente — on ne fabrique pas de valeur. Le total est reconstruit
    à partir du split s'il manque, sinon depuis ``total`` fourni.
    """
    p = (provider or "").strip().lower()
    if not isinstance(usage, dict):
        return None, None, (int(total) if isinstance(total, int) else None)

    if p == "ollama":
        pt = usage.get("prompt_eval_count")
        ct = usage.get("eval_count")
        tt = None
    else:
        pt = usage.get("input_tokens", usage.get("prompt_tokens"))
        ct = usage.get("output_tokens", usage.get("completion_tokens"))
        tt = usage.get("total_tokens")

    pt = int(pt) if isinstance(pt, int) else None
    ct = int(ct) if isinstance(ct, int) else None
    if not isinstance(tt, int):
        if pt is not None or ct is not None:
            tt = (pt or 0) + (ct or 0)
        elif isinstance(total, int):
            tt = total
        else:
            tt = None
    return pt, ct, tt


def estimate_cost_usd(
    *, provider: str | None, model: str | None, prompt_tokens: int | None, completion_tokens: int | None
) -> float | None:
    """Coût estimé en USD, ou ``None`` si indéterminé.

    - provider local (Ollama) -> ``0.0`` (assumé, documenté) ;
    - modèle inconnu d'un provider facturé -> ``None`` ;
    - décomposition prompt/completion absente -> ``None`` (jamais inventé).
    """
    if is_local_provider(provider):
        return 0.0

    table = _pricing_table()
    key = (model or "").strip().lower()
    price = table.get(key)
    if price is None:
        return None

    # On exige la décomposition : sans elle, le coût est indéterminé.
    if not isinstance(prompt_tokens, int) or not isinstance(completion_tokens, int):
        return None

    pin, pout = price
    return round(prompt_tokens / 1000.0 * pin + completion_tokens / 1000.0 * pout, 6)


def compute_cost_from_usage(
    *, provider: str | None, model: str | None, usage: Any, total: int | None = None
) -> tuple[int | None, int | None, int | None, float | None]:
    """Point d'entrée unique : ``(prompt, completion, total, cost_usd)``.

    Combine ``split_usage_tokens`` et ``estimate_cost_usd``. Utilisé par le chat,
    le résumé et les benchmarks afin que le coût soit calculé de façon identique
    partout.
    """
    pt, ct, tt = split_usage_tokens(provider, usage, total=total)
    cost = estimate_cost_usd(
        provider=provider, model=model, prompt_tokens=pt, completion_tokens=ct
    )
    return pt, ct, tt, cost
