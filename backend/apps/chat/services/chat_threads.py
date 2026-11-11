# backend/apps/chat/services/chat_threads.py
from __future__ import annotations

from typing import Iterable


class InvalidThreadError(Exception):
    """Raised when a provided thread_id does not exist for (user, rag_space)."""


def _thread_title_from_question(question: str) -> str:
    q = (question or "").strip()
    if not q:
        return "Conversation"
    return (q[:60] + "…") if len(q) > 60 else q


def get_thread(*, user, rag_space, thread_id):
    """Strict lookup: returns thread or None."""
    if not thread_id:
        return None

    from apps.chat.models import ChatThread

    return ChatThread.objects.filter(id=thread_id, user=user, rag_space=rag_space).first()


def create_thread(*, user, rag_space, question: str):
    from apps.chat.models import ChatThread

    return ChatThread.objects.create(
        user=user,
        rag_space=rag_space,
        title=_thread_title_from_question(question),
    )


def maybe_update_thread_title(*, thread, question: str):
    """
    Auto-update thread title from the latest user question.

    Best-effort: never raises.
    """
    try:
        new_title = _thread_title_from_question(question)
        if not new_title:
            return
        if (thread.title or "") == new_title:
            return
        thread.title = new_title
        thread.save(update_fields=["title"])
    except Exception:
        return


# ---------------------------------------------------------------------
# Mémoire hiérarchique (niveau 2): summary + fenêtre récente
# ---------------------------------------------------------------------


def _s(name: str):
    """Réglage DÉCLARÉ de la mémoire de conversation.

    Ce module portait un `_s(name, default)` permissif : il donnait à n'importe quel
    nom l'apparence d'un réglage. Trois noms en ont profité
    (`CHAT_MEMORY_SUMMARY_MAX_CHARS`, `..._MIN_MESSAGES_BEFORE_SUMMARY`,
    `..._SUMMARIZE_CHUNK_MAX_CHARS`) sans jamais être déclarés ni projetés, pendant
    que quatre autres, eux déclarés, n'étaient lus par personne. Le registre est
    désormais la seule nomenclature.
    """
    from config.settings.access import get_registered_setting

    return get_registered_setting(name)


def _truncate(s: str, max_chars: int) -> str:
    s = (s or "").strip()
    if max_chars <= 0 or len(s) <= max_chars:
        return s
    return s[: max_chars - 1].rstrip() + "…"


def _iter_messages_for_memory(*, thread) -> Iterable:
    """
    Yields ChatMessage ordered by created_at asc.
    """
    from apps.chat.models import ChatMessage

    return (
        ChatMessage.objects.filter(thread=thread)
        .only("id", "role", "content", "created_at")
        .order_by("created_at", "id")
        .iterator()
    )


def _message_is_after_checkpoint(msg_id, checkpoint_id) -> bool:
    """
    We don't have ordering guarantee across UUIDs, so we only compare by created_at in DB query,
    but since we store 'until_message_id', we use it only as a marker to re-start after it:
    we will skip until we meet it, then include subsequent messages.
    """
    if not checkpoint_id:
        return True
    return str(msg_id) != str(checkpoint_id)


def _is_refusal_answer(content: str) -> bool:
    """True si le message assistant est un refus (« Je ne sais pas »).

    Ces tours ne doivent PAS entrer dans la mémoire de conversation (ni la fenêtre
    récente, ni le résumé) : réinjectés dans le prompt, ils poussent le modèle —
    surtout strict (mistral) — à répéter le refus (anchoring), verrouillant le thread
    même quand le retrieval fournit des sources pertinentes.
    """
    # Détection CENTRALISÉE (FR + EN, tournures variées) : le test historique ne voyait
    # qu'un préfixe « je ne sais pas » / « i don't know », donc un refus formulé
    # « Les sources ne permettent pas de répondre. » entrait quand même dans la mémoire et
    # ancrait le refus au tour suivant.
    from apps.rag_core.services.answer_contract import is_abstention

    # Un message VIDE n'est pas un refus : il n'a rien à ancrer (le contrat de réponse, lui,
    # traite le vide comme une non-réponse — les deux usages diffèrent).
    text = (content or "").strip()
    return bool(text) and is_abstention(text)


def get_thread_memory_context(*, thread) -> str:
    """
    Build the context to inject into prompts:
    - optional summary (thread.memory_summary)
    - recent message window after summary checkpoint
    """
    max_messages = int(_s("CHAT_MEMORY_MAX_MESSAGES"))
    max_chars = int(_s("CHAT_MEMORY_MAX_CHARS"))

    summary = (getattr(thread, "memory_summary", "") or "").strip()
    checkpoint_id = getattr(thread, "memory_summary_until_message_id", None)

    # Collect messages after checkpoint
    recent = []
    seen_checkpoint = checkpoint_id is None
    for m in _iter_messages_for_memory(thread=thread):
        if not seen_checkpoint:
            if str(m.id) == str(checkpoint_id):
                seen_checkpoint = True
            continue
        if m.role not in ("user", "assistant"):
            continue
        # Exclut les refus (« Je ne sais pas ») : anti-anchoring (cf. _is_refusal_answer).
        if m.role == "assistant" and _is_refusal_answer(m.content or ""):
            continue
        recent.append(m)

    # Keep last N messages
    if max_messages > 0 and len(recent) > max_messages:
        recent = recent[-max_messages:]

    # Enforce char budget (from the end)
    lines = []
    for m in recent:
        role = "Utilisateur" if m.role == "user" else "Assistant"
        lines.append(f"{role}: {m.content or ''}".strip())

    # Trim to char budget from the end (prefer most recent)
    if max_chars > 0:
        acc = ""
        kept = []
        for line in reversed(lines):
            cand = (line + "\n\n" + acc).strip() if acc else line
            if len(cand) > max_chars and kept:
                break
            if len(cand) > max_chars and not kept:
                # single message too long -> hard truncate
                kept = [_truncate(line, max_chars)]
                break
            acc = cand
            kept.append(line)
        lines = list(reversed(kept)) if kept else []

    parts = []
    if summary:
        parts.append("Résumé de la conversation jusqu’ici:\n" + summary)
    if lines:
        parts.append("Fenêtre récente (messages les plus récents):\n" + "\n\n".join(lines))

    return "\n\n---\n\n".join([p for p in parts if p.strip()]).strip()


def thread_previous_user_language(thread) -> str | None:
    """Langue (`fr`/`en`/`None`) du DERNIER message utilisateur du thread.

    Sert de repli de continuité quand la question courante est indéterminable
    (politique de langue de réponse). Utilise le DÉTECTEUR COMMUN — aucune 2ᵉ
    logique. À appeler AVANT d'ajouter le tour courant (sinon il se détecterait
    lui-même). Best-effort : `None` si pas de thread / pas de message / erreur.
    """
    if thread is None:
        return None
    try:
        from apps.chat.models import ChatMessage
        from apps.rag_core.services.answer_language import detect_question_language

        last = (
            ChatMessage.objects.filter(thread=thread, role="user")
            .order_by("-created_at")
            .values_list("content", flat=True)
            .first()
        )
        return detect_question_language(last) if last else None
    except Exception:
        return None


def maybe_update_thread_summary(*, thread, llm_provider: str | None = None, llm_model: str | None = None) -> None:
    """
    Best-effort automatic summarization when thread gets long:
    - summarizes older messages into thread.memory_summary
    - keeps a recent window intact
    - updates checkpoint to the last summarized message id
    """
    try:
        from django.utils import timezone

        # Interrupteur du niveau 2. Il était DÉCLARÉ et documenté sans qu'aucun
        # code ne le lise : le résumé se produisait quoi qu'il arrive. À 0, les
        # résumés déjà enregistrés restent utilisés — on cesse d'en produire, on
        # n'efface pas la mémoire acquise.
        if not _s("CHAT_MEMORY_SUMMARY_ENABLED"):
            return

        max_messages = int(_s("CHAT_MEMORY_MAX_MESSAGES"))
        summary_max_chars = int(_s("CHAT_MEMORY_SUMMARY_MAX_CHARS"))
        min_messages_before_summary = int(_s("CHAT_MEMORY_SUMMARY_TRIGGER_MESSAGES"))
        chunk_max_chars = int(_s("CHAT_MEMORY_SUMMARY_MAX_INPUT_CHARS"))

        # Collect all user/assistant messages
        msgs = [m for m in _iter_messages_for_memory(thread=thread) if m.role in ("user", "assistant")]
        if len(msgs) < min_messages_before_summary:
            return

        # Define "recent window"
        recent = msgs[-max_messages:] if max_messages > 0 else msgs
        recent_ids = {str(m.id) for m in recent}

        checkpoint_id = getattr(thread, "memory_summary_until_message_id", None)
        summary_existing = (getattr(thread, "memory_summary", "") or "").strip()

        # Determine messages eligible for summarization:
        # - before recent window
        # - and after last checkpoint (avoid re-summarizing)
        eligible = []
        past_checkpoint = checkpoint_id is None
        for m in msgs:
            if not past_checkpoint:
                if str(m.id) == str(checkpoint_id):
                    past_checkpoint = True
                continue
            if str(m.id) in recent_ids:
                continue
            eligible.append(m)

        if not eligible:
            return

        # Build transcript chunk with hard safety truncations
        transcript_lines = []
        for m in eligible:
            # Ne résume pas les refus : le résumé de mémoire ne doit pas mémoriser
            # « l'assistant a répondu Je ne sais pas » (poison persistant du thread).
            if m.role == "assistant" and _is_refusal_answer(m.content or ""):
                continue
            role = "Utilisateur" if m.role == "user" else "Assistant"
            transcript_lines.append(f"{role}: {_truncate(m.content or '', 1200)}")

        transcript = "\n".join(transcript_lines).strip()
        if not transcript:
            return

        # Bound transcript chunk size (avoid giant prompts)
        transcript = _truncate(transcript, chunk_max_chars)

        # Langue de la mémoire (P1). Le caller (append_message) n'a pas de préférence
        # ; on détecte la langue du transcript AVEC LE DÉTECTEUR COMMUN (aucune 2ᵉ
        # logique). Gardé par le flag : OFF → politique française historique inchangée.
        from apps.rag_core.services.answer_language import detect_question_language, policy_enabled

        en = policy_enabled() and detect_question_language(transcript) == "en"

        # Summary policy (content rules)
        if en:
            policy = (
                "You produce a conversation summary to serve as memory.\n"
                "MANDATORY constraints:\n"
                "- Do not invent anything.\n"
                "- Do not include any sensitive data (passwords, API keys, tokens, secrets, emails, numbers).\n"
                "- Do not copy long verbatim excerpts; synthesise.\n"
                "- Keep only what is useful for the rest of the conversation.\n\n"
                "Expected content (short, structured format):\n"
                "1) Objectives / context\n"
                "2) Established facts & decisions\n"
                "3) User preferences/constraints\n"
                "4) Progress / open TODOs\n"
                "5) Important definitions/terms\n"
            )
            prompt = (
                f"{policy}\n"
                f"Existing summary (may be empty):\n{summary_existing}\n\n"
                f"New messages to integrate (older, before the recent window):\n{transcript}\n\n"
                "Task: Update the summary (merge with the existing one) following the format and constraints.\n"
                "Respond only with the summary.\n"
            )
        else:
            policy = (
                "Tu produis un résumé de conversation pour servir de mémoire.\n"
                "Contraintes IMPÉRATIVES:\n"
                "- N'invente rien.\n"
                "- N'inclus aucune donnée sensible (mots de passe, clés API, tokens, secrets, emails, numéros).\n"
                "- Ne recopie pas de longs extraits verbatim; synthétise.\n"
                "- Conserve uniquement ce qui est utile pour la suite.\n\n"
                "Contenu attendu (format court, structuré):\n"
                "1) Objectifs / contexte\n"
                "2) Faits établis & décisions\n"
                "3) Préférences/contraintes de l'utilisateur\n"
                "4) État d'avancement / TODO ouverts\n"
                "5) Définitions/termes importants\n"
            )
            prompt = (
                f"{policy}\n"
                f"Résumé existant (peut être vide):\n{summary_existing}\n\n"
                f"Nouveaux messages à intégrer (plus anciens, avant la fenêtre récente):\n{transcript}\n\n"
                "Tâche: Mets à jour le résumé (fusionne avec l'existant) en respectant le format et les contraintes.\n"
                "Réponds uniquement avec le résumé.\n"
            )

        # Call LLM
        from apps.llm_router.egress import LlmExecutionContext
        from apps.llm_router.router import generate_text

        provider = (llm_provider or "").strip() or str(_s("RAG_LLM_PROVIDER"))
        model = (llm_model or "").strip() or str(_s("RAG_LLM_MODEL"))

        # Budget d'appel du résumeur. 0 => aucun budget propre (comportement
        # historique : seul le timeout du provider s'applique).
        timeout_s = float(_s("CHAT_MEMORY_SUMMARY_LLM_TIMEOUT_S")) or None

        # Le transcript de conversation partait chez OpenAI sans contrôle
        # (P0-C #10). La politique de l'espace du fil s'applique désormais.
        llm = generate_text(
            prompt=prompt, provider=provider, model=model, timeout_s=timeout_s,
            context=LlmExecutionContext.for_rag_space(
                getattr(thread, "rag_space", None), operation="chat.thread_memory"
            ),
            operation="chat.thread_memory",
        )
        new_summary = (llm.get("text") or "").strip()
        if not new_summary:
            return

        # Bound stored summary size
        new_summary = _truncate(new_summary, summary_max_chars)

        # Update checkpoint to the last eligible message id (so we don't re-summarize it)
        last_summarized_id = eligible[-1].id

        thread.memory_summary = new_summary
        thread.memory_summary_until_message_id = last_summarized_id
        thread.memory_summary_updated_at = timezone.now()
        thread.save(
            update_fields=[
                "memory_summary",
                "memory_summary_until_message_id",
                "memory_summary_updated_at",
            ]
        )
    except Exception:
        return


def append_message(*, thread, role: str, content: str, sources=None, request_id=None, rag_query_log_id=None):
    from apps.chat.models import ChatMessage

    ChatMessage.objects.create(
        thread=thread,
        role=role,
        content=content or "",
        sources=sources,
        request_id=request_id or "",
        rag_query_log_id=rag_query_log_id,
    )

    # touch thread.updated_at
    try:
        from django.utils import timezone

        thread.updated_at = timezone.now()
        thread.save(update_fields=["updated_at"])
    except Exception:
        pass

    # Best-effort: update memory summary after assistant messages (turn completed)
    if role == "assistant":
        try:
            # provider/model can be supplied by caller later; keep defaults here
            maybe_update_thread_summary(thread=thread)
        except Exception:
            pass


def resolve_thread_strict(*, user, rag_space, thread_id, question: str):
    """
    Strict semantics:
    - if thread_id provided => must exist for (user, rag_space) else InvalidThreadError
    - else create a new thread
    """
    if thread_id:
        t = get_thread(user=user, rag_space=rag_space, thread_id=thread_id)
        if not t:
            raise InvalidThreadError("invalid_thread_id")
        return t
    return create_thread(user=user, rag_space=rag_space, question=question)
