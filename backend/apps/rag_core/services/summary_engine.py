# backend/apps/rag_core/services/summary_engine.py
"""Moteur de résumé **générique** — indépendant de tout modèle Django.

Pourquoi ce module existe
-------------------------
``summary_service`` sait résumer un ``Document`` privé : il lit ``Chunk``, il lit
``document.rag_space`` pour le provider, il écrit ``DocumentSummary``. Ces trois
dépendances sont légitimes pour le privé et **inapplicables** au corpus métier :
un ``DomainCorpusDocument`` n'a ni ``rag_space``, ni ``Chunk``, ni ligne de cache
dans ``rag_document_summary``.

Ce module isole la partie qui, elle, ne dépend d'aucun modèle : le découpage en
blocs, la construction des prompts, la boucle map-reduce et le comptage des
tokens. Les deux familles de documents partagent donc **le même texte de prompt à
l'octet près** — c'est la condition pour qu'un résumé de corpus soit comparable à
un résumé privé, et pour qu'une correction de prompt n'ait pas à être recopiée.

Ce qui reste chez l'appelant (et doit y rester)
----------------------------------------------
- la résolution et l'autorisation du document ;
- la lecture des chunks (modèle concret) ;
- le choix du provider et la politique d'egress ;
- le cache, l'audit et la persistance dans le fil.

Injection de la génération
--------------------------
``generate_summary_from_chunks`` reçoit un callable ``generate``. Ce n'est pas de
la cérémonie : chaque service conserve ainsi SON import de ``generate_text``, donc
son propre point de substitution en test. Un moteur qui importerait lui-même le
routeur rendrait les doubles de test d'un service invisibles depuis l'autre.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Protocol, Sequence

from django.conf import settings

from config.settings.access import get_registered_setting


# --- Budgets contexte -------------------------------------------------------
# Lus À L'APPEL, pour rester surchargeables par `override_settings` dans les
# tests. Le DÉFAUT n'est plus écrit ici : il vient du registre, et il n'y en a
# qu'un. Ces deux réglages étaient lus par `getattr(settings, nom, défaut)` sans
# qu'aucun settings ne les projette — les poser dans un `.env` ne faisait rien.
def setting_int(name: str) -> int:
    return int(get_registered_setting(name))


def max_total_chars() -> int:
    return setting_int("SUMMARY_MAX_CONTEXT_CHARS")


def max_chars_per_chunk() -> int:
    return setting_int("SUMMARY_MAX_CHARS_PER_CHUNK")


ALLOWED_MODES = ("short", "detailed", "bullets", "executive")
ALLOWED_SCOPES = ("document", "page")
DEFAULT_MODE = "bullets"
DEFAULT_SCOPE = "document"
DEFAULT_LANGUAGE = "fr"


class HasSummaryChunkFields(Protocol):
    """Contrat minimal d'un chunk résumable : ``id``, ``text``, ``page_number``.

    ``Chunk`` (privé) et ``DomainCorpusChunk`` (métier) le satisfont tels quels —
    aucune adaptation n'est nécessaire, et aucun DTO intermédiaire n'est imposé.
    """

    id: Any
    text: str
    page_number: int | None


@dataclass(frozen=True)
class SummaryChunk:
    """Chunk résumable détaché de l'ORM.

    Utile aux appelants qui n'ont pas d'objets modèle sous la main (tests, sources
    futures). Les services de production passent directement leurs instances ORM :
    le moteur ne lit que ``id``/``text``/``page_number``.
    """

    text: str
    page_number: int | None = None
    chunk_index: int = 0
    id: Any = ""


@dataclass
class SummaryLLMContext:
    """Politique d'exécution LLM résolue par l'appelant.

    Le moteur ne décide JAMAIS d'un provider ni d'une politique d'egress : il les
    reçoit. C'est ce qui garantit qu'un contenu de corpus métier reste soumis à la
    politique de l'espace consommateur, et non à un défaut global permissif.
    """

    provider: str | None
    model: str | None
    options: dict | None = None
    think: Any = None
    #: ``LlmExecutionContext`` de l'espace consommateur (peut être ``None`` : le
    #: service appelant retombe alors sur son propre repli fail-closed).
    egress_context: Any = None


@dataclass
class GeneratedSummary:
    """Sortie brute du moteur, avant tout cache et toute persistance."""

    answer: str
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    provider: str | None
    model: str | None
    truncated: bool
    prompt_hash: str


# ---------------------------------------------------------------------------
# Empreinte de contenu (invalidation de cache)
# ---------------------------------------------------------------------------
def fingerprint(chunks: Iterable[Any], *, prompt_version: int) -> str:
    """Empreinte des chunks + version de prompt.

    Change dès que :
    - les chunks sont réindexés (nouveaux identifiants / longueurs) → réindex ;
    - la version de prompt est incrémentée → changement d'instructions.
    Dans les deux cas, le cache de l'appelant est automatiquement invalidé.
    """
    h = hashlib.sha256()
    h.update(f"pv={prompt_version};".encode("utf-8"))
    for ch in chunks:
        h.update(str(getattr(ch, "id", "")).encode("utf-8"))
        h.update(b"|")
        h.update(str(len(getattr(ch, "text", "") or "")).encode("utf-8"))
        h.update(b";")
    return h.hexdigest()


def hash_text(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Découpage en blocs
# ---------------------------------------------------------------------------
def split_into_blocks(chunks: Iterable[Any]) -> tuple[list[list[tuple[int | None, str]]], bool]:
    """Découpe les chunks (ordonnés) en blocs tenant chacun dans le budget contexte.

    Un seul bloc → document court (résumé direct). Plusieurs blocs → document
    long → map-reduce (aucun chunk n'est abandonné). Retourne (blocs, capped) où
    ``capped`` indique qu'au moins un chunk a été tronqué à ``max_chars_per_chunk``.
    """
    max_total = max_total_chars()
    max_per = max_chars_per_chunk()
    blocks: list[list[tuple[int | None, str]]] = []
    cur: list[tuple[int | None, str]] = []
    cur_len = 0
    capped = False

    for ch in chunks:
        text = (getattr(ch, "text", "") or "").strip()
        if not text:
            continue
        if len(text) > max_per:
            text = text[:max_per]
            capped = True
        unit = (getattr(ch, "page_number", None), text)
        ulen = len(text) + 2
        # On garde toujours au moins un chunk par bloc (cur vide → on n'ouvre pas).
        if cur and cur_len + ulen > max_total:
            blocks.append(cur)
            cur = []
            cur_len = 0
        cur.append(unit)
        cur_len += ulen

    if cur:
        blocks.append(cur)
    return blocks, capped


def block_context(block: Sequence[tuple[int | None, str]]) -> str:
    return "\n\n".join((f"[p.{pg}] {tx}" if pg is not None else tx) for pg, tx in block)


# ---------------------------------------------------------------------------
# Prompts (identiques, à l'octet près, pour le privé et le corpus métier)
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "Tu résumes UNIQUEMENT le contenu fourni ci-dessous, issu d'un seul document. "
    "Tu ne dois pas utiliser de connaissances externes ni d'informations d'autres documents. "
    "Si une information n'est pas présente dans le contenu fourni, indique explicitement "
    "qu'elle n'est pas disponible. N'invente jamais de faits, de chiffres ou de dates. "
    "Si le contenu est manifestement tronqué, incomplet ou mal extrait (OCR), signale-le "
    "en une ligne au début.\n"
    "Structure ta réponse ainsi : 1) Résumé court (2-3 phrases) ; 2) Points clés (puces) ; "
    "3) Éléments chiffrés / dates ; 4) Acteurs / entités cités ; 5) Conclusion."
)

MODE_INSTRUCTIONS = {
    "short": "Produis un résumé court et dense (3 à 5 phrases), en conservant les sections demandées de façon concise.",
    "detailed": "Produis une synthèse détaillée et structurée, en développant chaque section.",
    "bullets": "Mets l'accent sur les points clés sous forme de puces claires et actionnables.",
    "executive": "Produis une synthèse exécutive orientée décision (enjeux, chiffres, risques, recommandation).",
}

# Variantes ANGLAISES (P1 — politique de langue de réponse). Sélectionnées seulement
# quand la langue RÉSOLUE est « en » ; sinon les prompts français ci-dessus sont
# utilisés À L'OCTET PRÈS (aucune régression, cache FR inchangé). La structure de
# sortie (titres) est traduite, pas seulement « Réponds en … ».
SYSTEM_PROMPT_EN = (
    "You summarise ONLY the content provided below, from a single document. "
    "You must not use external knowledge or information from other documents. "
    "If a piece of information is not present in the provided content, state explicitly "
    "that it is not available. Never invent facts, figures or dates. "
    "If the content is clearly truncated, incomplete or badly extracted (OCR), flag it "
    "in one line at the beginning.\n"
    "Structure your answer as: 1) Short summary (2-3 sentences); 2) Key points (bullets); "
    "3) Figures / dates; 4) Actors / entities mentioned; 5) Conclusion."
)

MODE_INSTRUCTIONS_EN = {
    "short": "Produce a short, dense summary (3 to 5 sentences), keeping the requested sections concise.",
    "detailed": "Produce a detailed, structured summary, developing each section.",
    "bullets": "Emphasise the key points as clear, actionable bullets.",
    "executive": "Produce a decision-oriented executive summary (issues, figures, risks, recommendation).",
}

# Version des prompts/instructions de résumé. Intégrée à l'empreinte du cache :
# INCRÉMENTER à chaque modification des prompts ci-dessus ou des builders, afin
# d'invalider les résumés cachés devenus obsolètes. Surchargeable par
# settings.SUMMARY_PROMPT_VERSION pour forcer une régénération sans déploiement.
SUMMARY_PROMPT_VERSION = 1


def prompt_version() -> int:
    try:
        return int(getattr(settings, "SUMMARY_PROMPT_VERSION", SUMMARY_PROMPT_VERSION))
    except (TypeError, ValueError):
        return SUMMARY_PROMPT_VERSION


def _title_line(title: str, date_iso: str | None) -> str:
    return f"{title}{f' ({date_iso})' if date_iso else ''}"


def build_prompt(
    *, title: str, date_iso: str | None, mode: str, scope: str, page: int | None,
    language: str, context: str,
) -> str:
    if language == "en":
        mode_line = MODE_INSTRUCTIONS_EN.get(mode, MODE_INSTRUCTIONS_EN[DEFAULT_MODE])
        if scope == "page" and page is not None:
            scope_line = f"Scope: page {page} of the document only.\n"
            closing = f"Summarise page {page} of this document strictly following the instructions above."
        else:
            scope_line = ""
            closing = "Summarise this document strictly following the instructions above."
        return (
            f"{SYSTEM_PROMPT_EN}\n\n"
            f"Format instruction: {mode_line}\n"
            f"{scope_line}"
            f"Respond in English.\n\n"
            f"Document: {_title_line(title, date_iso)}\n"
            f"Requested mode: {mode}\n\n"
            f"=== CONTENT TO SUMMARISE (ordered excerpts) ===\n"
            f"{context}\n"
            f"=== END OF CONTENT ===\n\n"
            f"{closing}"
        )

    mode_line = MODE_INSTRUCTIONS.get(mode, MODE_INSTRUCTIONS[DEFAULT_MODE])

    if scope == "page" and page is not None:
        scope_line = f"Périmètre : page {page} du document uniquement.\n"
        closing = f"Résume la page {page} de ce document en respectant strictement les consignes ci-dessus."
    else:
        scope_line = ""
        closing = "Résume ce document en respectant strictement les consignes ci-dessus."

    return (
        f"{SYSTEM_PROMPT}\n\n"
        f"Consigne de format : {mode_line}\n"
        f"{scope_line}"
        f"Réponds en {language}.\n\n"
        f"Document : {_title_line(title, date_iso)}\n"
        f"Mode demandé : {mode}\n\n"
        f"=== CONTENU À RÉSUMER (extraits ordonnés) ===\n"
        f"{context}\n"
        f"=== FIN DU CONTENU ===\n\n"
        f"{closing}"
    )


def build_map_prompt(*, title: str, language: str, index: int, total: int, context: str) -> str:
    """Map : résumé fidèle d'un bloc (partie i/n) d'un document long."""
    if language == "en":
        return (
            "You summarise ONLY the content provided below, which is a PART "
            f"({index}/{total}) of a single document. Do not use any external knowledge. "
            "Do not invent anything. Faithfully restate the facts, figures, dates and actors "
            "present in this part, as dense notes (bullets). Do not write an introduction.\n"
            "Respond in English.\n\n"
            f"Document: {title} — part {index}/{total}\n\n"
            f"=== PART CONTENT ===\n{context}\n=== END ===\n\n"
            "List the key information of this part."
        )
    return (
        "Tu résumes UNIQUEMENT le contenu fourni ci-dessous, qui est une PARTIE "
        f"({index}/{total}) d'un même document. N'utilise aucune connaissance externe. "
        "N'invente rien. Restitue fidèlement les faits, chiffres, dates et acteurs présents "
        "dans cette partie, sous forme de notes denses (puces). Ne rédige pas d'introduction.\n"
        f"Réponds en {language}.\n\n"
        f"Document : {title} — partie {index}/{total}\n\n"
        f"=== CONTENU DE LA PARTIE ===\n{context}\n=== FIN ===\n\n"
        "Liste les informations clés de cette partie."
    )


def build_reduce_prompt(
    *, title: str, date_iso: str | None, mode: str, scope: str, page: int | None,
    language: str, partials: str,
) -> str:
    """Reduce : synthèse finale à partir des résumés partiels (aucun contenu brut)."""
    if language == "en":
        mode_line = MODE_INSTRUCTIONS_EN.get(mode, MODE_INSTRUCTIONS_EN[DEFAULT_MODE])
        return (
            f"{SYSTEM_PROMPT_EN}\n\n"
            "The items below are successive partial summaries of a SINGLE document "
            "(split into several parts). Produce a single, coherent, non-redundant synthesis "
            "by merging the parts. Do not use any external knowledge.\n"
            f"Format instruction: {mode_line}\n"
            f"Respond in English.\n\n"
            f"Document: {_title_line(title, date_iso)}\n"
            f"Requested mode: {mode}\n\n"
            f"=== PARTIAL SUMMARIES (in order) ===\n{partials}\n=== END ===\n\n"
            "Write the final synthesis strictly following the requested structure."
        )
    mode_line = MODE_INSTRUCTIONS.get(mode, MODE_INSTRUCTIONS[DEFAULT_MODE])
    return (
        f"{SYSTEM_PROMPT}\n\n"
        "Les éléments ci-dessous sont des résumés partiels successifs d'UN SEUL document "
        "(découpé en plusieurs parties). Produis une synthèse unique, cohérente et sans "
        "redondance, en fusionnant les parties. N'utilise aucune connaissance externe.\n"
        f"Consigne de format : {mode_line}\n"
        f"Réponds en {language}.\n\n"
        f"Document : {_title_line(title, date_iso)}\n"
        f"Mode demandé : {mode}\n\n"
        f"=== RÉSUMÉS PARTIELS (dans l'ordre) ===\n{partials}\n=== FIN ===\n\n"
        "Rédige la synthèse finale en respectant strictement la structure demandée."
    )


# ---------------------------------------------------------------------------
# Coût / latence : décision sync vs async
# ---------------------------------------------------------------------------
def report_num_predict() -> int:
    """Budget de tokens générés par appel (preset rapport). Défaut 2048."""
    return setting_int("RAG_REPORT_NUM_PREDICT")


def async_max_sync_output_tokens() -> int:
    """Seuil d'estimation au-delà duquel on bascule en async.

    Le registre déclare le défaut « = RAG_REPORT_NUM_PREDICT (2048) » ; il est
    projeté tel quel dans les settings, donc aucune dérivation n'a lieu ici.
    """
    return setting_int("SUMMARY_ASYNC_MAX_SYNC_OUTPUT_TOKENS")


def estimate_generation_calls(blocks) -> int:
    """Nombre d'appels LLM séquentiels : 1 (mono-bloc) ou len(blocks)+1 (map+reduce)."""
    n = len(blocks)
    return 1 if n <= 1 else n + 1


def estimate_output_tokens(blocks) -> int:
    """Estimation grossière des tokens générés = appels × budget par appel.

    Proxy de latence : la génération est séquentielle et chaque appel produit
    jusqu'à ``num_predict`` tokens. Sert uniquement à décider sync/async.
    """
    return estimate_generation_calls(blocks) * report_num_predict()


# ---------------------------------------------------------------------------
# Preset « rapport » — identique pour les deux familles de documents
# ---------------------------------------------------------------------------
def report_generation_options() -> dict:
    """Preset synthèse/rapport (options Gateway) depuis les settings."""
    from apps.llm_router.model_utils import gen_options

    return gen_options(
        temperature=settings.RAG_REPORT_TEMPERATURE,
        num_ctx=settings.RAG_REPORT_NUM_CTX,
        num_predict=settings.RAG_REPORT_NUM_PREDICT,
    )


def report_think(model) -> Any:
    """``think=false`` ciblé (qwen3:14b) : la tâche est un résumé structuré."""
    from apps.llm_router.model_utils import think_for_focused_task

    return think_for_focused_task(model, task="summary")


def resolve_report_model(provider, model) -> tuple[Any, Any]:
    """Routage OPT-IN par tâche (rapport) : override éventuel du modèle local."""
    from apps.llm_router.task_routing import TASK_REPORT, resolve_model_for_task

    return resolve_model_for_task(TASK_REPORT, provider=provider, model=model)


# ---------------------------------------------------------------------------
# Génération : simple (court) ou map-reduce (long)
# ---------------------------------------------------------------------------
class EmptyContentError(Exception):
    """Le contenu à résumer est vide après découpe (aucun chunk exploitable)."""


class EmptyAnswerError(Exception):
    """Le fournisseur LLM a renvoyé une réponse vide."""


@dataclass
class SummaryPromptBuilders:
    """Trois constructeurs de prompt, injectés par l'appelant.

    Injectés plutôt qu'appelés en dur : le service privé expose historiquement
    ``_build_prompt`` comme point de substitution (routage de modèle, presets de
    génération) et cette surface doit rester substituable après l'extraction. Le
    moteur ne connaît donc que trois signatures, jamais un modèle Django.
    """

    #: (*, mode, scope, page, language, context) -> str
    build: Callable[..., str]
    #: (*, language, index, total, context) -> str
    build_map: Callable[..., str]
    #: (*, mode, scope, page, language, partials) -> str
    build_reduce: Callable[..., str]


def default_prompt_builders(*, title: str, date_iso: str | None = None) -> SummaryPromptBuilders:
    """Constructeurs standards, liés à un titre de document et à sa date."""
    return SummaryPromptBuilders(
        build=lambda **kw: build_prompt(title=title, date_iso=date_iso, **kw),
        build_map=lambda **kw: build_map_prompt(title=title, **kw),
        build_reduce=lambda **kw: build_reduce_prompt(title=title, date_iso=date_iso, **kw),
    )


def generate_summary_from_chunks(
    *,
    chunks: Sequence[Any],
    prompts: SummaryPromptBuilders,
    mode: str,
    scope: str,
    page: int | None,
    language: str,
    llm: SummaryLLMContext,
    generate: Callable[..., dict],
    split_tokens: Callable[[str | None, Any, int | None], tuple[int | None, int | None, int | None]],
) -> GeneratedSummary:
    """Produit le résumé d'une séquence de chunks ORDONNÉS.

    Aucun accès base, aucun cache, aucune persistance : uniquement le découpage,
    les prompts et les appels LLM. Lève ``EmptyContentError`` / ``EmptyAnswerError``,
    que l'appelant traduit dans SA hiérarchie d'erreurs métier (les codes HTTP
    restent ainsi la propriété du service, pas du moteur).
    """
    blocks, capped = split_into_blocks(chunks)

    if len(blocks) <= 1:
        ctx = block_context(blocks[0]) if blocks else ""
        if not ctx.strip():
            raise EmptyContentError("Le contenu indexé à résumer est vide.")
        prompt = prompts.build(
            mode=mode, scope=scope, page=page, language=language, context=ctx,
        )
        r = generate(
            prompt, llm.provider, llm.model,
            options=llm.options, think=llm.think, context=llm.egress_context,
        )
        answer = (r.get("text") or "").strip()
        if not answer:
            raise EmptyAnswerError("Le fournisseur LLM a renvoyé une réponse vide.")
        pt, ct, tt = split_tokens(r.get("provider"), r.get("usage"), r.get("tokens"))
        return GeneratedSummary(
            answer=answer, prompt_tokens=pt, completion_tokens=ct, total_tokens=tt,
            provider=r.get("provider"), model=r.get("model"), truncated=capped,
            prompt_hash=hash_text(prompt),
        )

    # --- Document long : map (par bloc) puis reduce (synthèse finale) ---
    #
    # ⚠️ Les appels de MAP ne reçoivent PAS `llm.egress_context` : ils retombent sur
    # le repli fail-closed du service appelant (`system_local_only`). Comportement
    # historique du résumé privé, reproduit ici À L'IDENTIQUE — le repli est PLUS
    # restrictif que le contexte d'espace, donc le reproduire ne peut pas assouplir
    # une politique d'egress. Le corriger serait un élargissement de politique, hors
    # périmètre de cette extraction.
    partials: list[str] = []
    spt = sct = stt = 0
    prov = mdl = None
    total = len(blocks)
    for i, block in enumerate(blocks, 1):
        r = generate(
            prompts.build_map(
                language=language, index=i, total=total, context=block_context(block),
            ),
            llm.provider, llm.model, options=llm.options, think=llm.think,
        )
        partials.append(f"[Partie {i}/{total}]\n{(r.get('text') or '').strip()}")
        pt, ct, tt = split_tokens(r.get("provider"), r.get("usage"), r.get("tokens"))
        spt += pt or 0
        sct += ct or 0
        stt += tt or 0
        prov, mdl = r.get("provider"), r.get("model")

    reduce_prompt = prompts.build_reduce(
        mode=mode, scope=scope, page=page, language=language,
        partials="\n\n".join(partials),
    )
    r = generate(
        reduce_prompt, llm.provider, llm.model,
        options=llm.options, think=llm.think, context=llm.egress_context,
    )
    answer = (r.get("text") or "").strip()
    if not answer:
        raise EmptyAnswerError("Le fournisseur LLM a renvoyé une réponse vide.")
    pt, ct, tt = split_tokens(r.get("provider"), r.get("usage"), r.get("tokens"))
    spt += pt or 0
    sct += ct or 0
    stt += tt or 0
    prov, mdl = r.get("provider"), r.get("model")
    return GeneratedSummary(
        answer=answer, prompt_tokens=spt or None, completion_tokens=sct or None,
        total_tokens=stt or None, provider=prov, model=mdl, truncated=False,
        prompt_hash=hash_text(reduce_prompt),
    )
