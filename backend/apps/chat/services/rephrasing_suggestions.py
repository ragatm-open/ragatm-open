# backend/apps/chat/services/rephrasing_suggestions.py
"""Suggestions de reformulation pour le cas de VRAIE non-réponse documentaire.

Responsabilité unique : à partir de la question utilisateur (et, si un jour
disponible, d'un contexte documentaire AUTORISÉ minimal), proposer 2-3
reformulations concrètes et actionnables pour augmenter les chances d'obtenir
une réponse au prochain essai.

Garanties :
  - ne lève JAMAIS : toute erreur => ``[]`` ou fallback déterministe sûr, la
    réponse principale (no_answer) n'est jamais bloquée ni remplacée ;
  - réutilise le provider/modèle DÉJÀ RÉSOLUS de l'espace ; ne change jamais le
    provider et n'engage aucun repli cloud implicite
    (contexte système *local-only* : aucun egress externe possible) ;
  - budget de tokens court, température basse ;
  - aucune suggestion ne prétend qu'une information existe ;
  - logs sans contenu sensible (ni question, ni prompt, ni suggestions, ni
    chunks, ni documents).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from django.conf import settings

from apps.llm_router.model_utils import gen_options, think_for_focused_task
from apps.llm_router.egress import LlmExecutionContext
from apps.llm_router.router import generate_text

logger = logging.getLogger(__name__)

# Plafond dur du contrat API (jamais plus de 3 suggestions exposées).
_HARD_MAX = 3

def _enabled() -> bool:
    return bool(getattr(settings, "RAG_REPHRASING_SUGGESTIONS_ENABLED", True))


def _max_suggestions(explicit: int | None) -> int:
    n = explicit if isinstance(explicit, int) and explicit > 0 else int(
        getattr(settings, "RAG_REPHRASING_MAX", 3)
    )
    return max(1, min(_HARD_MAX, n))


def detect_language(question: str) -> str:
    """Détection FR/EN — DÉLÈGue au détecteur COMMUN (unification P2).

    Conserve le contrat historique (défaut ``fr`` : les suggestions sont un chemin
    francophone par nature) tout en supprimant la logique de détection parallèle.
    """
    from apps.rag_core.services.answer_language import detect_question_language

    return detect_question_language(question) or "fr"


def _normalize(s: str) -> str:
    """Forme normalisée pour comparaison (dédup + égalité à la question)."""
    s = (s or "").lower()
    s = re.sub(r"[^\w]+", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip()


def _topic(question: str, *, limit: int = 80) -> str:
    """Sujet court dérivé de la question (pour le fallback déterministe)."""
    q = re.sub(r"\s+", " ", (question or "").strip()).strip(" ?.!:;")
    if len(q) > limit:
        q = q[:limit].rstrip() + "…"
    return q


def _extract_json(text: str) -> Any:
    """Extraction tolérante d'un objet JSON (fences ``` et prose autour)."""
    if not isinstance(text, str) or not text.strip():
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    m = re.search(r"\{.*\}", t, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


def _validate(text: str, *, question: str, max_n: int) -> list[str]:
    """Valide/déduplique la sortie LLM. Retourne ``[]`` si rien d'exploitable."""
    data = _extract_json(text)
    if not isinstance(data, dict):
        return []
    items = data.get("suggestions")
    if not isinstance(items, list):
        return []

    q_norm = _normalize(question)
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if not isinstance(it, str):
            continue
        s = it.strip().strip("-•*").strip()
        if not s:
            continue
        n = _normalize(s)
        if not n or n == q_norm or n in seen:
            continue
        seen.add(n)
        out.append(s)
        if len(out) >= max_n:
            break
    return out


def _deterministic_suggestions(question: str, lang: str, max_n: int) -> list[str]:
    """Fallback SÛR fondé uniquement sur la question. N'affirme aucune information.

    Trois angles distincts : préciser/cibler, décomposer, contextualiser
    (document / période / entité). Toujours en langue de la question.
    """
    topic = _topic(question)
    if not topic:
        return []
    if lang == "en":
        candidates = [
            f"Which document, project or time period does « {topic} » refer to?",
            f"Could you target a more specific detail around « {topic} »?",
            f"Can we break « {topic} » down into simpler questions?",
        ]
    else:
        candidates = [
            f"Sur quel document, projet ou quelle période porte « {topic} » ?",
            f"Pouvez-vous cibler une information plus précise autour de « {topic} » ?",
            f"Peut-on décomposer « {topic} » en questions plus simples ?",
        ]
    return candidates[:max_n]


def _build_prompt(question: str, lang: str, context_snippets: list[str] | None) -> str:
    """Prompt de génération : JSON strict, aucune invention, langue préservée."""
    if lang == "en":
        header = (
            "A user asked a question that returned NO answer from the available "
            "documents. Propose 2 to 3 alternative rephrasings that would improve "
            "the chances of retrieving an answer next time.\n"
            "Rules:\n"
            "- write in ENGLISH (same language as the question);\n"
            "- never assume that any fact, date, amount, name or clause exists;\n"
            "- stay close to the original topic;\n"
            "- vary the angles (be more precise, target a nearby piece of "
            "information, decompose a broad question);\n"
            "- each rephrasing must be a short, standalone question the user can "
            "ask as-is.\n"
            'Answer ONLY with valid JSON: {"suggestions": ["...", "...", "..."]}\n'
        )
        q_label, ctx_label = "Question", "Context terms (optional, non-authoritative)"
    else:
        header = (
            "La question d'un utilisateur n'a trouvé AUCUNE réponse dans les "
            "documents disponibles. Propose 2 à 3 reformulations alternatives qui "
            "augmenteraient les chances d'obtenir une réponse au prochain essai.\n"
            "Règles :\n"
            "- écris en FRANÇAIS (même langue que la question) ;\n"
            "- ne suppose l'existence d'aucune information, date, montant, nom ou "
            "clause ;\n"
            "- reste proche du sujet initial ;\n"
            "- varie les angles (préciser, cibler une information proche, "
            "décomposer une question trop large) ;\n"
            "- chaque reformulation est une question courte, posable telle "
            "quelle.\n"
            'Réponds UNIQUEMENT avec un JSON valide : {"suggestions": ["...", '
            '"...", "..."]}\n'
        )
        q_label, ctx_label = "Question", "Termes de contexte (optionnels, non probants)"

    parts = [header]
    snippets = [s for s in (context_snippets or []) if isinstance(s, str) and s.strip()]
    if snippets:
        joined = " | ".join(s.strip()[:120] for s in snippets[:4])
        parts.append(f"{ctx_label}: {joined}\n")
    parts.append(f"{q_label}: {question.strip()}\nJSON:")
    return "".join(parts)


def generate_rephrasing_suggestions(
    *,
    question: str,
    provider: str | None,
    model: str | None,
    context_snippets: list[str] | None = None,
    language: str | None = None,
    max_suggestions: int | None = None,
) -> list[str]:
    """Retourne 0 à 3 reformulations. Ne lève jamais.

    Chemin : génération LLM bornée (provider/modèle de l'espace, aucun repli
    cloud implicite) -> validation stricte -> fallback déterministe sûr si la
    sortie est vide/invalide ou si la génération échoue.
    """
    if not _enabled():
        return []

    q = (question or "").strip()
    if not q:
        return []

    max_n = _max_suggestions(max_suggestions)
    lang = language or detect_language(q)

    try:
        prompt = _build_prompt(q, lang, context_snippets)
        options = gen_options(
            temperature=float(getattr(settings, "RAG_REPHRASING_TEMPERATURE", 0.1)),
            num_ctx=int(getattr(settings, "RAG_REPHRASING_NUM_CTX", 2048)),
            num_predict=int(getattr(settings, "RAG_REPHRASING_NUM_PREDICT", 256)),
        )
        result = generate_text(
            prompt=prompt,
            provider=provider,
            model=model,
            # Aucun repli externe pour une simple suggestion : refus structurel,
            # valable aussi quand la Gateway est désactivée.
            context=LlmExecutionContext.system_local_only(
                operation="chat.rephrasing_suggestions"
            ),
            options=options,
            # think=false pour qwen3:14b (tâche ciblée courte, évite les vides).
            think=think_for_focused_task(model, task="extraction"),
            # Budget de temps borné + AUCUN retry réponse vide : une lenteur/panne
            # ne peut pas prolonger le traitement (le flux principal est déjà parti).
            timeout_s=float(getattr(settings, "RAG_REPHRASING_TIMEOUT_S", 3)),
            retry_on_empty=False,
        )
        text = result.get("text") if isinstance(result, dict) else ""
        suggestions = _validate(text or "", question=q, max_n=max_n)
        if suggestions:
            logger.info(
                "rephrasing_suggestions status=success count=%d provider=%s model=%s",
                len(suggestions), provider or "(default)", model or "(default)",
            )
            return suggestions

        logger.info(
            "rephrasing_suggestions status=%s count=0 provider=%s model=%s",
            "empty" if (text or "").strip() else "invalid",
            provider or "(default)", model or "(default)",
        )
    except Exception as exc:  # noqa: BLE001 - suggestions ne doivent jamais bloquer
        logger.warning(
            "rephrasing_suggestions status=error error=%s provider=%s model=%s",
            type(exc).__name__, provider or "(default)", model or "(default)",
        )

    # Fallback déterministe sûr (jamais d'affirmation qu'une info existe).
    fallback = _deterministic_suggestions(q, lang, max_n)
    if fallback:
        logger.info(
            "rephrasing_suggestions status=fallback count=%d provider=%s model=%s",
            len(fallback), provider or "(default)", model or "(default)",
        )
    return fallback
