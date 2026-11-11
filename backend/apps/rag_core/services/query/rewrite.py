# backend/apps/rag_core/services/query/rewrite.py
from __future__ import annotations

import os
import re
from typing import Any

from django.conf import settings

from apps.llm_router.model_utils import gen_options, think_for_focused_task
from apps.llm_router.egress import LlmExecutionContext
from apps.llm_router.router import LLMError, generate_text
from apps.llm_router.task_routing import TASK_REWRITE, resolve_model_for_task

# -------------------------
# Settings (env-backed)
# -------------------------
# Réécriture LLM de la question, en mode EXPLICATIF uniquement.
#
# DÉSACTIVÉE PAR DÉFAUT depuis la mesure du 30/07/2026. Elle ne se déclenchait jamais
# avant l'arrivée de la détection d'intention ; une fois active sur 27 questions sur 100,
# elle a été évaluée en A/B sur le même code, même base, même configuration :
#
#   valeur : 0 source indispensable récupérée, 0 perdue ; décisions d'admission
#            IDENTIQUES (VP 29, FP 1, VN 34, FN 6) ; 570 sources dans les deux bras ;
#            53 documents apportés contre 49 retirés — une permutation, pas un gain ;
#   coût   : P95 +102,6 % (5 699 -> 11 545 ms), +7,9 s médian sur les questions visées.
#
# Cause du non-gain, visible dans les requêtes produites : le modèle TRADUIT la question
# en français (« Retours investissements capital risque ») alors que le corpus métier est
# anglophone. La requête réécrite cherche donc dans une langue que le fonds n'emploie pas.
#
# Le coût, lui, n'est pas celui du LLM (4,4 s à froid, 0,3 s à chaud) mais celui des DEUX
# PASSES DE RETRIEVAL supplémentaires qu'elle engendre : la requête keywordisée est une
# disjonction large, et son `ts_rank` sur 8 713 chunks coûte 2,7 s côté privé et 1,2 s
# côté corpus. Raccourcir le timeout du LLM n'y changerait donc rien.
#
# Remettre à 1 exige de re-mesurer : le verdict dépend de la langue du fonds documentaire.
QUERY_REWRITE_ENABLED = os.getenv("RAG_QUERY_REWRITE_ENABLED", "0").strip() not in ("0", "false", "False", "")
KEYWORDIZE_ENABLED = os.getenv("RAG_KEYWORDIZE_ENABLED", "1").strip() not in ("0", "false", "False", "")
MAX_SEARCH_QUERIES = int(os.getenv("RAG_MAX_SEARCH_QUERIES", "3") or "3")

REWRITE_PROVIDER = os.getenv("RAG_QUERY_REWRITE_PROVIDER", "").strip() or "ollama"
REWRITE_MODEL = os.getenv("RAG_QUERY_REWRITE_MODEL", "").strip() or ""
REWRITE_TIMEOUT_S = float(os.getenv("RAG_QUERY_REWRITE_TIMEOUT_S", "6") or "6")
REWRITE_MAX_CHARS = int(os.getenv("RAG_QUERY_REWRITE_MAX_CHARS", "140") or "140")

KEYWORDIZE_MAX_TOKENS = int(os.getenv("RAG_KEYWORDIZE_MAX_TOKENS", "10") or "10")

_WORD_RE = re.compile(r"[a-zA-ZÀ-ÿ0-9]+")

_REWRITE_CLEAN_RE = re.compile(r"[\r\n\t]+")
_REWRITE_STRIP_QUOTES_RE = re.compile(r'^[\s"“”«»]+|[\s"“”«»]+$')

_STOPWORDS_FR = {
    "le", "la", "les", "un", "une", "des", "du", "de", "d", "a", "à", "au", "aux",
    "et", "ou", "mais", "donc", "or", "ni", "car",
    "est", "sont", "été", "etre", "être",
    "que", "qui", "quoi", "dont", "où",
    "ce", "cet", "cette", "ces", "ça", "cela",
    "je", "tu", "il", "elle", "nous", "vous", "ils", "elles",
    "mon", "ton", "son", "notre", "votre", "leur",
    "quelle", "quelles", "quel", "quels",
    "dans", "sur", "sous", "avec", "sans", "pour", "par", "vers", "chez",
    # verbes/intent
    "explique", "expliquer", "donne", "donner", "liste", "lister",
    "montre", "trouve", "cherche", "recherche", "décris", "decris",
    # bruit
    "moi", "toi", "svp", "merci", "stp", "peux", "peut", "peux-tu", "pouvez",
}


def keywordize_query(question: str) -> str:
    """Fallback ultra robuste: transforme une phrase en requête mots-clés."""
    q = (question or "").strip()
    if not q:
        return ""

    tokens = [t.lower() for t in _WORD_RE.findall(q)]
    kept: list[str] = []
    for t in tokens:
        if len(t) < 2:
            continue
        if t in _STOPWORDS_FR:
            continue
        kept.append(t)

    kept = kept[:KEYWORDIZE_MAX_TOKENS]
    return " ".join(kept).strip()


def rewrite_query_with_llm(question: str, *, context=None) -> str:
    """Rewrite via LLM. Returns "" on failure (safe)."""
    q = (question or "").strip()
    if not q:
        return ""

    prompt = (
        "Tu es un module de réécriture de requêtes pour un moteur de recherche documentaire.\n"
        "Ta tâche: convertir une question en une requête courte (3 à 8 mots), style mots-clés.\n"
        "Règles:\n"
        "- Supprime les mots de politesse et les verbes (explique, peux-tu, etc.).\n"
        "- Garde les noms propres, termes métier, identifiants, titres, dates.\n"
        "- Ne réponds PAS à la question.\n"
        "- Ne produis qu'UNE SEULE ligne, sans puces.\n\n"
        f"Question: {q}\n"
        "Requête:"
    )

    try:
        # Routage OPT-IN par tâche (réécriture) : override éventuel du modèle local.
        rw_provider, rw_model = resolve_model_for_task(
            TASK_REWRITE, provider=REWRITE_PROVIDER, model=REWRITE_MODEL
        )
        # Preset réécriture (extraction courte) : temperature basse, budget serré.
        rewrite_options = gen_options(
            temperature=settings.RAG_REWRITE_TEMPERATURE,
            num_ctx=settings.RAG_REWRITE_NUM_CTX,
            num_predict=settings.RAG_REWRITE_NUM_PREDICT,
        )
        out = generate_text(
            prompt=prompt,
            provider=rw_provider,
            model=rw_model,
            timeout_s=REWRITE_TIMEOUT_S,
            options=rewrite_options,
            think=think_for_focused_task(rw_model, task="extraction"),
            # La question de l'utilisateur partait chez OpenAI sans contrôle
            # (P0-C #9). Défaut local-only : la réécriture est une tâche interne.
            context=context or LlmExecutionContext.system_local_only(
                operation="query.rewrite"
            ),
            operation="query.rewrite",
        )
        txt = (out.get("text") or "").strip()
        txt = _REWRITE_CLEAN_RE.sub(" ", txt).strip()
        txt = _REWRITE_STRIP_QUOTES_RE.sub("", txt).strip()
        txt = " ".join(txt.split())

        if not txt:
            return ""
        if len(txt) > REWRITE_MAX_CHARS:
            txt = txt[:REWRITE_MAX_CHARS].rstrip()
        return txt
    except LLMError:
        return ""
    except Exception:
        return ""


def build_comparative_queries(question: str, objects: tuple[str, str]) -> list[str]:
    """Expansion DÉTERMINISTE d'une comparaison explicite — aucun appel LLM.

    Une question déjà explicite (« différences entre A et B ») n'a rien à faire réécrire :
    ses deux objets sont connus. On ajoute UNE requête portant les deux objets ensemble —
    celle qui remonte les passages traitant des deux côtés à la fois, c'est-à-dire ceux
    qui permettent réellement de comparer.

    Deux requêtes SÉPARÉES (une par objet) ont été essayées et MESURÉES : elles dégradent
    la couverture (chaque jeu de résultats évince l'autre à la fusion — sur 10 questions
    comparatives, 53 chunks portant le 1er objet contre 67 avec la requête combinée, et
    9/10 questions couvrant les deux objets contre 10/10). La question seule, sans
    expansion, fait également moins bien (63/65, 9/10).

    Rien n'est inventé : ni acronyme développé, ni traduction, ni terme métier ajouté —
    seulement les deux segments déjà présents dans la question.
    """
    q0 = (question or "").strip()
    if not q0:
        return []
    pair = " ".join(part.strip() for part in (objects or ()) if (part or "").strip())
    return [q0, pair] if pair else [q0]


def build_queries_for_search(
    *,
    question: str,
    explanatory_mode: bool,
    comparative_objects: tuple[str, str] | None = None,
) -> list[str]:
    """Build multiple search queries (original + rewritten + keywordized).

    ``comparative_objects`` (optionnel) : les deux objets d'une comparaison EXPLICITE,
    résolus en amont par ``heuristics.detect_comparative_intent``. Quand ils sont
    fournis, l'expansion est **déterministe et sans LLM** — elle prime sur le chemin
    explicatif historique, qui reste inchangé pour tout le reste.
    """
    q0 = (question or "").strip()
    if not q0:
        return []

    if comparative_objects:
        queries = build_comparative_queries(q0, comparative_objects)
    else:
        queries = [q0]

    if comparative_objects is None and explanatory_mode and QUERY_REWRITE_ENABLED:
        q_rewritten = rewrite_query_with_llm(q0)
        if q_rewritten and q_rewritten.strip().lower() != q0.lower():
            queries.append(q_rewritten.strip())

        if KEYWORDIZE_ENABLED:
            q_kw = keywordize_query(q0)
            if q_kw:
                low_set = {q.strip().lower() for q in queries}
                if q_kw.strip().lower() not in low_set:
                    queries.append(q_kw.strip())

    out: list[str] = []
    seen: set[str] = set()
    for q in queries:
        key = q.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(q.strip())

    return out[:MAX_SEARCH_QUERIES] if MAX_SEARCH_QUERIES > 0 else out
