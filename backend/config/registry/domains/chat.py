"""Domaine ``chat`` — mémoire de conversation et résumés de documents."""
from __future__ import annotations

from config.registry.definition import ConfigDefinition as D

DEFINITIONS: tuple[D, ...] = (
    D(
        name="CHAT_MEMORY_ENABLED",
        domain="chat",
        value_type="bool",
        default=True,
        description=(
            "Reprise du contexte de conversation dans le prompt. À 0, chaque question "
            "est traitée isolément : ni fenêtre récente, ni résumé. La mémoire n'entre "
            "JAMAIS dans la requête de recherche, seulement dans le prompt."
        ),
        consumers=("apps.chat.services.chat_service",),
        notes=(
            "Un second nom, `RAG_CHAT_MEMORY_ENABLED`, était consulté en repli. Il "
            "n'a jamais été projeté dans les settings : la branche ne s'est jamais "
            "exécutée. Elle a été retirée plutôt que déclarée — un alias doit "
            "préserver une configuration qui a existé, pas une qui n'a jamais servi."
        ),
    ),
    D(
        name="CHAT_MEMORY_MAX_MESSAGES",
        domain="chat",
        value_type="int",
        default=12,
        minimum=1,
        maximum=200,
        description="Nombre de messages récents repris dans le contexte de conversation.",
        consumers=("apps.chat.services.chat_threads",),
    ),
    D(
        name="CHAT_MEMORY_MAX_CHARS",
        domain="chat",
        value_type="int",
        default=3000,
        minimum=100,
        maximum=100000,
        description="Budget de caractères de la mémoire courte de conversation.",
        consumers=("apps.chat.services.chat_threads",),
    ),
    # --- Résumé glissant (mémoire de niveau 2) --------------------------------
    #
    # Ces réglages étaient DÉCLARÉS sous des noms que le code ne lisait pas : il
    # lisait `CHAT_MEMORY_MIN_MESSAGES_BEFORE_SUMMARY`, `CHAT_MEMORY_SUMMARIZE_
    # CHUNK_MAX_CHARS` et `CHAT_MEMORY_SUMMARY_MAX_CHARS`, aucun n'étant projeté.
    # Deux conséquences : les quatre noms déclarés n'avaient aucun effet, et les
    # trois noms lus retombaient sur des défauts locaux.
    #
    # Les DÉFAUTS ci-dessous reprennent les valeurs RÉELLEMENT appliquées par le
    # code (18, 6000), pas celles qui étaient déclarées (24, 12000) : déclarer une
    # valeur, c'est promettre celle qui s'exécute. Corriger le nom sans corriger la
    # valeur aurait changé le comportement de production en silence.
    D(
        name="CHAT_MEMORY_SUMMARY_ENABLED",
        domain="chat",
        value_type="bool",
        default=True,
        description=(
            "Mémoire de conversation de niveau 2 : résumé glissant des échanges. "
            "À 0, aucun résumé n'est produit ni mis à jour ; les résumés déjà "
            "enregistrés restent utilisés."
        ),
        consumers=("apps.chat.services.chat_threads",),
    ),
    D(
        name="CHAT_MEMORY_SUMMARY_TRIGGER_MESSAGES",
        domain="chat",
        value_type="int",
        default=18,
        minimum=1,
        maximum=1000,
        description=(
            "Nombre de messages du fil à partir duquel un résumé est produit. "
            "En dessous, la fenêtre récente suffit à porter le contexte."
        ),
        consumers=("apps.chat.services.chat_threads",),
    ),
    D(
        name="CHAT_MEMORY_SUMMARY_MAX_INPUT_CHARS",
        domain="chat",
        value_type="int",
        default=6000,
        minimum=100,
        maximum=1000000,
        description=(
            "Volume maximal d'historique soumis au résumeur. Borne le PROMPT : "
            "au-delà, le transcript est tronqué avant l'appel."
        ),
        consumers=("apps.chat.services.chat_threads",),
    ),
    D(
        name="CHAT_MEMORY_SUMMARY_MAX_CHARS",
        domain="chat",
        value_type="int",
        default=4000,
        minimum=100,
        maximum=100000,
        description=(
            "Borne du résumé STOCKÉ. Distincte de MAX_INPUT_CHARS, qui borne "
            "l'entrée : celle-ci borne la sortie, y compris si le modèle ignore "
            "la consigne de concision."
        ),
        consumers=("apps.chat.services.chat_threads",),
    ),
    D(
        name="CHAT_MEMORY_SUMMARY_LLM_TIMEOUT_S",
        domain="chat",
        value_type="float",
        default=0.0,
        minimum=0.0,
        maximum=600.0,
        description=(
            "Budget d'appel du résumeur. 0 = aucun budget propre (comportement "
            "historique : le timeout du provider s'applique). Le résumé est "
            "best-effort : un dépassement laisse le fil sans résumé mis à jour, "
            "jamais sans réponse."
        ),
        consumers=("apps.chat.services.chat_threads",),
    ),
    D(
        name="SUMMARY_ASYNC_ENABLED",
        domain="chat",
        value_type="bool",
        default=True,
        description=(
            "Génération de résumé de document en tâche Celery (202 pending + polling). "
            "À 0, génération inline synchrone."
        ),
        consumers=("apps.rag_core.services.summary",),
    ),
    D(
        name="SUMMARY_ASYNC_MAX_SYNC_OUTPUT_TOKENS",
        domain="chat",
        value_type="int",
        default=2048,
        default_expr="= RAG_REPORT_NUM_PREDICT (2048)",
        minimum=1,
        maximum=65536,
        description=(
            "Au-delà de cette estimation de tokens générés, le résumé bascule en tâche "
            "asynchrone. Défaut = comportement historique préservé."
        ),
        consumers=("apps.rag_core.services.summary",),
        default_is_dynamic=True,
    ),
    D(
        name="SUMMARY_PENDING_TIMEOUT_S",
        domain="chat",
        value_type="int",
        default=900,
        minimum=1,
        maximum=86400,
        description=(
            "Au-delà de ce délai, un résumé resté PENDING est considéré bloqué et passé "
            "à FAILED par le reaper planifié."
        ),
        consumers=("apps.rag_core.services.summary", "apps.rag_core.tasks"),
    ),
    D(
        name="SUMMARY_PROMPT_VERSION",
        domain="chat",
        value_type="int",
        default=1,
        minimum=1,
        description=(
            "Version des prompts de résumé : l'incrémenter invalide les résumés en "
            "cache (intégrée à l'empreinte)."
        ),
        consumers=("apps.rag_core.services.summary",),
    ),
    D(
        name="SUMMARY_MAX_CONTEXT_CHARS",
        domain="chat",
        value_type="int",
        default=24000,
        minimum=1000,
        maximum=1000000,
        description=(
            "Volume total de texte soumis au résumeur de documents. Au-delà, le "
            "contenu est réduit par map-reduce plutôt que tronqué en bloc."
        ),
        consumers=("apps.rag_core.services.summary_engine",),
    ),
    D(
        name="SUMMARY_MAX_CHARS_PER_CHUNK",
        domain="chat",
        value_type="int",
        default=2000,
        minimum=200,
        maximum=100000,
        description=(
            "Taille d'un fragment envoyé au résumeur lors de la passe « map ». "
            "Trop grand, le modèle survole ; trop petit, le nombre d'appels explose."
        ),
        consumers=("apps.rag_core.services.summary_engine",),
    ),
)
