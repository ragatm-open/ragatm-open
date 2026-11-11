# backend/apps/actions/plan.py
"""Contrats de données : séparation stricte Action vs Politique de réponse."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# 4.1 — Ce que l'utilisateur veut FAIRE
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ActionPlan:
    action: str
    detection_method: str  # "rule" | "keyword_gate" | "llm" | "explicit_command" | "fallback"
    parameters: dict[str, Any] = field(default_factory=dict)
    candidates: tuple[str, ...] = ()
    missing_parameters: tuple[str, ...] = ()
    needs_clarification: bool = False
    clarification_message: str | None = None
    # Optionnel : NE PAS traiter comme une probabilité calibrée tant qu'un corpus
    # d'évaluation ne l'a pas validé (cf. audit §15).
    score: float | None = None


# ---------------------------------------------------------------------------
# 4.2 — COMMENT répondre (politique post-retrieval, distincte des actions)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ResponsePolicy:
    answer_kind: str  # "ok" | "explain" | "compare" | "inventory" | "partial" | "low_confidence" | "no_answer"
    reason: str | None = None


# ---------------------------------------------------------------------------
# Détection déterministe (sortie d'un Matcher)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MatchResult:
    action: str
    parameters: dict[str, Any] = field(default_factory=dict)
    detection_method: str = "rule"
    score: float | None = None
    ambiguous: bool = False
    # Position (dans le texte normalisé) du déclencheur ayant matché — utile aux
    # garde-fous (négation/explicatif/citation). ``None`` si non pertinent.
    trigger_index: int | None = None


# ---------------------------------------------------------------------------
# Contexte d'exécution (routing + futurs handlers)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ActionContext:
    user: Any = None
    rag_space_id: Any = None
    request_id: str = ""
    # Sources de la dernière réponse assistant (pour la résolution déictique
    # « ce document », « la source 2 »). Tuple de dicts opaques.
    last_sources: tuple[dict[str, Any], ...] = ()


# ---------------------------------------------------------------------------
# 6/17/21 — Ressource résolue et résultat d'action (contrats communs sync/SSE)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ResolvedResource:
    resource_type: str  # "document" | "rag_space" | ...
    resource_id: Any
    display_name: str
    rag_space_id: Any


@dataclass
class ActionResult:
    action: str
    status: str  # "ok" | "no_answer" | "clarification" | "denied" | "error"
    answer_kind: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    sources: list[Any] = field(default_factory=list)
    log_id: str | None = None
