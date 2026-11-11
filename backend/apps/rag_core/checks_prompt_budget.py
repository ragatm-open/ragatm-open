"""System check — le budget de prompt tient-il dans la fenêtre du modèle ?

`RAG_MAX_PROMPT_CHARS` et `RAG_FACTUAL_NUM_CTX` sont **couplés** : le prompt et les
tokens de génération doivent tenir ensemble dans la fenêtre de contexte. Dépasser ne
lève aucune erreur applicative — le provider tronque ou échoue en 400, et l'utilisateur
voit une réponse amputée sans savoir pourquoi.

Le contrôle est volontairement un **avertissement** : le ratio caractères/token dépend
de la langue et du tokenizer, et les providers diffèrent. On signale une configuration
douteuse, on ne bloque pas un démarrage sur une estimation.
"""
from __future__ import annotations

from django.conf import settings
from django.core.checks import Error, Warning as CheckWarning

ID_INVALID = "rag_core.E007"
ID_OVERFLOW = "rag_core.W003"

#: Caractères par token, estimation PRUDENTE pour le français (les modèles usuels
#: tournent entre 3,5 et 4,5). Sous-estimer le ratio revient à surestimer le nombre de
#: tokens : l'avertissement se déclenche plus tôt, ce qui est le bon sens du risque.
CHARS_PER_TOKEN = 3.5


def check_prompt_budget(app_configs, **kwargs):
    """Refuse un budget non positif ; signale un budget qui déborde la fenêtre."""
    issues: list = []

    try:
        max_chars = int(getattr(settings, "RAG_MAX_PROMPT_CHARS", 18000))
        snippet = int(getattr(settings, "RAG_SNIPPET_MAX_CHARS", 1200))
        table = int(getattr(settings, "RAG_SNIPPET_MAX_CHARS_TABLE", 4000))
        num_ctx = int(getattr(settings, "RAG_FACTUAL_NUM_CTX", 8192))
        num_predict = int(getattr(settings, "RAG_FACTUAL_NUM_PREDICT", 768))
    except (TypeError, ValueError):
        return [
            Error(
                "Un budget de prompt n'est pas un entier "
                "(RAG_MAX_PROMPT_CHARS / RAG_SNIPPET_MAX_CHARS / RAG_SNIPPET_MAX_CHARS_TABLE).",
                hint="Posez des entiers strictement positifs.",
                id=ID_INVALID,
            )
        ]

    for name, value in (
        ("RAG_MAX_PROMPT_CHARS", max_chars),
        ("RAG_SNIPPET_MAX_CHARS", snippet),
        ("RAG_SNIPPET_MAX_CHARS_TABLE", table),
    ):
        if value <= 0:
            issues.append(
                Error(
                    f"{name}={value} : un budget doit être strictement positif.",
                    hint="Retirez le réglage pour reprendre le défaut, ou posez une valeur ≥ 1.",
                    id=ID_INVALID,
                )
            )
    if issues:
        return issues

    budget_tokens = max(0, num_ctx - num_predict)
    max_chars_allowed = int(budget_tokens * CHARS_PER_TOKEN)
    if max_chars > max_chars_allowed:
        issues.append(
            CheckWarning(
                f"RAG_MAX_PROMPT_CHARS={max_chars} dépasse ce que la fenêtre du modèle "
                f"peut absorber (~{max_chars_allowed} caractères pour "
                f"RAG_FACTUAL_NUM_CTX={num_ctx} moins RAG_FACTUAL_NUM_PREDICT={num_predict}).",
                hint=(
                    "Le prompt serait tronqué par le provider, ou refusé. Baissez le budget, "
                    "ou augmentez la fenêtre de contexte du modèle."
                ),
                id=ID_OVERFLOW,
            )
        )

    if table > max_chars:
        issues.append(
            CheckWarning(
                f"RAG_SNIPPET_MAX_CHARS_TABLE={table} dépasse RAG_MAX_PROMPT_CHARS={max_chars} : "
                "un seul extrait tabulaire saturerait le prompt.",
                hint="Gardez le gabarit d'extrait nettement inférieur au budget global.",
                id=ID_OVERFLOW,
            )
        )

    return issues
