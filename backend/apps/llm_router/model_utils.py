"""Helpers PURS de reconnaissance de modèles + réglages de génération par tâche.

Aucune I/O, aucune dépendance Django : uniquement de la logique de nom de modèle
et de construction d'``options`` Gateway. Utilisés par les services métier (chat,
résumé, réécriture) pour appliquer des presets sans mettre de logique métier dans
le routeur.
"""
from __future__ import annotations

from typing import Any


def _base_model_name(model: str | None) -> str:
    """Nom de modèle normalisé (minuscule, sans espaces) pour comparaison robuste.

    Ne retire PAS le tag (``:8b``) : la distinction de taille est significative
    (``qwen3:14b`` ≠ ``qwen3:30b-instruct``).
    """
    return (model or "").strip().lower()


def is_qwen3(model: str | None) -> bool:
    """True si le modèle appartient à la famille Qwen3 (tous tags/tailles confondus).

    Robuste à la casse et aux préfixes de registre (ex. ``org/qwen3:14b``).
    N'inclut PAS les Qwen2/Qwen1 (``qwen2.5`` → False).
    """
    name = _base_model_name(model)
    if not name:
        return False
    # segment après un éventuel préfixe de registre "org/…"
    tail = name.rsplit("/", 1)[-1]
    return tail.startswith("qwen3:") or tail == "qwen3"


def is_qwen3_14b(model: str | None) -> bool:
    """True UNIQUEMENT pour la variante Qwen3 14B (ne matche pas 30b/8b/autres tailles).

    Reconnaît ``qwen3:14b`` et ses variantes de tag (``qwen3:14b-instruct``,
    ``qwen3:14b-q4_0``…), insensible à la casse et au préfixe de registre.
    """
    if not is_qwen3(model):
        return False
    tail = _base_model_name(model).rsplit("/", 1)[-1]
    # tout ce qui suit "qwen3:" ; la taille est le premier segment avant un '-'
    tag = tail.split(":", 1)[1] if ":" in tail else ""
    size = tag.split("-", 1)[0]
    return size == "14b"


def gen_options(
    *,
    temperature: float | None = None,
    num_ctx: int | None = None,
    num_predict: int | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    """Construit un dict ``options`` Gateway en omettant les valeurs None.

    ``seed`` : graine de génération. Absente, AUCUNE clé n'est émise et le
    comportement reste celui d'avant, au caractère près. Posée, la réponse devient
    reproductible à prompt identique — mesuré sur les 87 questions de calibration :
    13 verdicts basculaient entre trois exécutions, plus aucun avec une graine.

    Le test est ``is not None`` et non la véracité de la valeur. Un ``if seed:``
    laisserait tomber ``0`` en silence, et un paramètre qu'on croit transmis alors
    qu'il ne l'est pas est pire que pas de paramètre du tout. Que l'upstream
    traite ``0`` comme « pas de graine » est un fait distinct, traité là où il doit
    l'être — à la configuration, qui refuse cette valeur.
    """
    o: dict[str, Any] = {}
    if temperature is not None:
        o["temperature"] = float(temperature)
    if num_ctx is not None:
        o["num_ctx"] = int(num_ctx)
    if num_predict is not None:
        o["num_predict"] = int(num_predict)
    if seed is not None:
        o["seed"] = int(seed)
    return o


def resolve_options(preset: dict[str, Any] | None, explicit: dict[str, Any] | None) -> dict[str, Any]:
    """Fusionne un preset et d'éventuelles options explicites — l'EXPLICITE prime.

    Utilisé par les services pour garantir qu'un paramètre fourni par l'appelant a
    priorité sur le preset de tâche.
    """
    out = dict(preset or {})
    for k, v in (explicit or {}).items():
        if v is not None:
            out[k] = v
    return out


# Tâches "ciblées" pour lesquelles le raisonnement interne de Qwen3:14b consomme
# le budget de tokens sans valeur ajoutée (réponse factuelle / structurée courte).
FOCUSED_TASKS = frozenset({"factual", "summary", "extraction"})


def think_for_focused_task(model: str | None, *, task: str) -> bool | None:
    """Décision ``think`` pour une tâche ciblée (factual/summary/extraction).

    Renvoie ``False`` UNIQUEMENT pour ``qwen3:14b`` sur une tâche ciblée (évite les
    réponses vides dues au raisonnement interne). Sinon ``None`` (ne pas transmettre
    ``think`` : on laisse le défaut du modèle/Gateway). Ne force JAMAIS globalement
    pour toute la famille Qwen3.
    """
    if task in FOCUSED_TASKS and is_qwen3_14b(model):
        return False
    return None
