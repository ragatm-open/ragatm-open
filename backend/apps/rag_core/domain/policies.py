# backend/apps/rag_core/domain/policies.py
from dataclasses import dataclass
from typing import Any
import os

# ============================================================
# Defaults des champs de RagPolicy
# ============================================================
#
# ⚠️ Ces constantes ne sont PAS le pilotage de la configuration : l'orchestrateur
# (`services/query/orchestrator.py`) construit `RagPolicy` en passant TOUTES les
# valeurs depuis `settings`. Elles ne servent donc que de défauts de champs, pour
# les rares constructions partielles (tests unitaires du domaine).
#
# `DEFAULT_TOP_K` (6) et `DEFAULT_VECTOR_TOP_K` (40) vivaient ici sans être ni des
# champs de `RagPolicy`, ni importés par personne : deux défauts fantômes qui
# CONTREDISAIENT `settings.base` (8 et 8) et faisaient croire à une divergence de
# réglage. Supprimés — `top_k` et `vector_top_k` sont des variables locales de
# l'orchestrateur, lues sur `settings`, et ne doivent pas réapparaître ici (un
# test de non-régression le verrouille).
DEFAULT_MIN_SCORE = float(os.getenv("RAG_MIN_SCORE", "0.76"))
DEFAULT_MIN_SOURCES = int(os.getenv("RAG_MIN_SOURCES", "1"))
DEFAULT_RERANK_ENABLED = os.getenv("RAG_RERANK_ENABLED", "0") == "1"
DEFAULT_RERANK_TOP_K = int(os.getenv("RAG_RERANK_TOP_K", "5"))

# ============================================================
# Policy RAG
# ============================================================

@dataclass
class RagPolicy:
    """
    Politique de décision RAG.

    Règles :
    - min_sources : nb minimal de chunks requis (strict)
    - min_score   : seuil principal (vector / fallback)
    - min_text_rank : seuil FTS si signal textuel présent
    - rerank_*    : paramètres de reranking (optionnels)
    - allow_partial : autorise une réponse PARTIELLE si au moins 1 source existe
                      (les garde-fous forts restent gérés en amont)
    """

    min_sources: int = DEFAULT_MIN_SOURCES

    # FTS (BM25 / ts_rank)
    min_text_rank: float = 0.0

    # Vector / score unifié
    min_score: float = DEFAULT_MIN_SCORE

    # Rerank (cross-encoder, etc.)
    rerank_enabled: bool = DEFAULT_RERANK_ENABLED
    rerank_trigger_min_candidates: int = 6
    rerank_top_k: int = DEFAULT_RERANK_TOP_K

    # ✅ Nouveau (safe default = False)
    allow_partial: bool = False


# ============================================================
# Helpers internes
# ============================================================

def _best_scores(chunks: list[dict[str, Any]]) -> tuple[float, float, float]:
    """
    Retourne (best_text, best_vec, best_any) de manière robuste.
    """
    if not chunks:
        return 0.0, 0.0, 0.0

    best_text = max((c.get("text_score") or 0.0) for c in chunks)
    best_vec = max((c.get("vector_score") or 0.0) for c in chunks)
    best_any = max((c.get("score") or 0.0) for c in chunks)
    return best_text, best_vec, best_any


def has_any_signal(chunks: list[dict[str, Any]]) -> bool:
    """
    Indique s'il existe AU MOINS un signal exploitable (≥ 1 chunk avec score > 0).
    Utilisé pour autoriser des réponses partielles en amont sans casser la policy stricte.
    """
    if not chunks:
        return False
    return any((c.get("score") or 0.0) > 0.0 for c in chunks)


# ============================================================
# Décision : répondre ou non (STRICT)
# ============================================================

def should_answer(chunks: list[dict[str, Any]], policy: RagPolicy) -> bool:
    """
    Décide si le RAG a suffisamment de signal pour répondre STRICTEMENT.

    Ordre de décision (du plus fiable au plus permissif) :
    1) FTS (text_score / text_rank) si présent
    2) Vector score
    3) Score générique (fallback)

    ⚠️ Important :
    - Cette fonction reste volontairement STRICTE.
    - Les réponses "partielles" sont gérées en amont (query_rag),
      en s'appuyant sur has_any_signal(...) si allow_partial est souhaité.
    """

    # 1️⃣ Nombre minimal de sources (strict)
    if len(chunks) < policy.min_sources:
        return False

    best_text, best_vec, best_any = _best_scores(chunks)

    # 2️⃣ Signal FTS prioritaire (preuve textuelle forte)
    if best_text > 0.0:
        return best_text >= policy.min_text_rank

    # 3️⃣ Vector-only
    if best_vec > 0.0:
        return best_vec >= policy.min_score

    # 4️⃣ Dernier recours (score unifié)
    return best_any >= policy.min_score
