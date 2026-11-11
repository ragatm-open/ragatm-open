"""Routage OPT-IN du modèle par TYPE DE TÂCHE (expérimental).

Permet d'affecter un modèle local (servi via l'AI Gateway) différent selon la
tâche — ex. rapports → ``qwen3:30b-instruct``, RAG factuel → ``mistral-small3.2:24b`` —
SANS changer le modèle par défaut d'un workspace ni le routage par workspace.

Garanties de sécurité :
  - désactivé par défaut (``RAG_TASK_ROUTING_ENABLED=0``) => aucun changement ;
  - n'override QUE le nom de modèle, JAMAIS le provider ;
  - ne s'applique QUE aux espaces locaux (``provider == "ollama"``, servis par la
    Gateway) : un espace OpenAI n'est jamais rerouté (pas de coût externe surprise) ;
  - un override vide laisse le modèle du workspace inchangé.

Pur (lecture de settings uniquement) : aucun I/O, testable via la fixture settings.
"""
from __future__ import annotations

import logging

from django.conf import settings

logger = logging.getLogger(__name__)

# Types de tâche routables.
TASK_FACTUAL = "factual"
TASK_REPORT = "report"
TASK_REWRITE = "rewrite"
# Suggestions de reformulation (cas no_answer). Déclaré pour un usage futur : NON
# ajouté à ``_SETTING_BY_TASK`` => ``resolve_model_for_task`` renvoie toujours le
# provider/modèle de l'espace inchangés (aucun reroutage multi-modèles).
TASK_REPHRASING_SUGGESTIONS = "rephrasing_suggestions"

# Provider local (servi par l'AI Gateway) : seul éligible au reroutage.
_LOCAL_PROVIDER = "ollama"

_SETTING_BY_TASK = {
    TASK_FACTUAL: "RAG_TASK_MODEL_FACTUAL",
    TASK_REPORT: "RAG_TASK_MODEL_REPORT",
    TASK_REWRITE: "RAG_TASK_MODEL_REWRITE",
}


def routing_enabled() -> bool:
    return bool(getattr(settings, "RAG_TASK_ROUTING_ENABLED", False))


def task_model_override(task: str) -> str:
    """Modèle configuré pour une tâche (vide si non défini / tâche inconnue)."""
    name = _SETTING_BY_TASK.get(task)
    if not name:
        return ""
    return str(getattr(settings, name, "") or "").strip()


def resolve_model_for_task(
    task: str, *, provider: str | None, model: str | None
) -> tuple[str | None, str | None]:
    """Renvoie ``(provider, model)`` effectif pour ``task``.

    Applique l'override de modèle configuré pour la tâche UNIQUEMENT si :
      - le routage est activé ; ET
      - un override non vide existe pour cette tâche ; ET
      - le provider courant est local (``ollama``).
    Sinon, renvoie ``(provider, model)`` inchangé. Le provider n'est jamais modifié.
    """
    if not routing_enabled():
        return provider, model

    override = task_model_override(task)
    if not override:
        return provider, model

    if (provider or "").strip().lower() != _LOCAL_PROVIDER:
        # Espace non-local (ex. OpenAI) : jamais rerouté (pas de coût externe).
        return provider, model

    if override == (model or ""):
        return provider, model

    logger.info(
        "task_routing task=%s provider=%s model=%s -> %s",
        task, provider, model, override,
    )
    return provider, override
