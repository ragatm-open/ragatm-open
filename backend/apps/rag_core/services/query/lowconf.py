from __future__ import annotations

from apps.rag_core.domain.policies import RagPolicy
from .settings import setting_bool, setting_float, setting_int

# Les défauts ne sont plus écrits ici : ils sont au registre (domaine `retrieval`),
# et la valeur est projetée dans les settings. Poser ces quatre réglages dans un
# `.env` a désormais un effet — ce n'était pas le cas avant ce lot.
LOWCONF_ENABLED = setting_bool("RAG_LOWCONF_ENABLED")
LOWCONF_FACTOR = setting_float("RAG_LOWCONF_FACTOR")
LOWCONF_MIN_ABS = setting_float("RAG_LOWCONF_MIN_ABS")
LOWCONF_MIN_SOURCES = setting_int("RAG_LOWCONF_MIN_SOURCES")


def low_conf_threshold(policy_obj: RagPolicy) -> float:
    try:
        base = float(getattr(policy_obj, "min_score", 0.0) or 0.0)
    except Exception:
        base = 0.0
    thr = base * float(LOWCONF_FACTOR)
    return max(float(LOWCONF_MIN_ABS), thr)
