"""corpus.global_search — recherche thématique sur TOUT le corpus.

Le préfiltre lexical interroge l'INTÉGRALITÉ des contenus textuels exploitables
du périmètre autorisé, puis tous les candidats retenus sont traités par lots.

Ce qui est GARANTI (niveaux A et B) :
- tous les documents visibles ont été considérés ;
- tous les chunks des documents exploitables ont été évalués par le préfiltre.

Ce qui n'est JAMAIS promis (niveaux C et D) :
- que toutes les formulations sémantiques équivalentes aient été reconnues.

Un préfiltre lexical n'est pas une garantie sémantique. Le rendu le dit.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time

from django.conf import settings

from apps.rag_core.analysis.fts_query import (
    QueryTooBroad,
    build_global_fts_query,
    plan_global_search_query,
)
from apps.rag_core.analysis.progress import (
    AnalysisCancelled,
    raise_if_cancel_requested,
    update_job_progress,
)
from apps.rag_core.domain.coverage import (
    REASON_EXTRACTION_FAILED,
    REASON_LIMIT_APPLIED,
    REASON_NO_TEXT_CONTENT,
    REASON_NOT_READY,
    REASON_PROCESSING,
    Coverage,
    CoverageExclusion,
    CoverageScope,
    compute_coverage_ratio,
    coverage_to_dict,
)
from apps.rag_core.models.document import Document
from apps.rag_core.repositories.global_search_repository import (
    count_global_fts_candidates,
    iterate_global_fts_candidates,
    resolve_fts_configuration,
    scan_scope,
    term_document_frequencies,
)

logger = logging.getLogger(__name__)

RESULT_VERSION = "1"

# Phases stables, non sensibles (aucun nom de table, de SQL ni de technologie).
PHASE_COUNTING_SCOPE = "counting_scope"
PHASE_SEARCHING_CANDIDATES = "searching_candidates"
PHASE_PROCESSING_BATCHES = "processing_batches"
PHASE_CONSOLIDATING = "consolidating"
PHASE_PERSISTING_RESULT = "persisting_result"

# Codes d'erreur stables et exposables.
ERROR_TOPIC_REQUIRED = "global_search_topic_required"
ERROR_CANDIDATE_LIMIT = "candidate_limit_exceeded"
ERROR_UNSOURCED_RESULT = "invalid_unsourced_result"
ERROR_GATEWAY_UNAVAILABLE = "gateway_unavailable"
ERROR_QUERY_TOO_BROAD = "query_too_broad"

MAX_QUERY_CHARS = 500
MAX_CHUNK_CHARS_FOR_LLM = 1200
MAX_STATEMENT_CHARS = 600


class GlobalSearchError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class TopicRequired(GlobalSearchError):
    def __init__(self) -> None:
        super().__init__(ERROR_TOPIC_REQUIRED)


class CandidateLimitExceeded(GlobalSearchError):
    def __init__(self, found: int, limit: int) -> None:
        super().__init__(ERROR_CANDIDATE_LIMIT)
        self.found = found
        self.limit = limit


# --------------------------------------------------------------------------- #
# Validation de la requête
# --------------------------------------------------------------------------- #

# Marqueurs de PORTÉE : ils disent « partout », pas « quoi ». Une requête qui ne
# contient QUE de la portée n'a pas de sujet -> on refuse plutôt que de deviner.
# Génériques, sans aucun vocabulaire métier.
_SCOPE_STOPWORDS = frozenset("""
recherche recherches rechercher cherche chercher chercher analyse analyser analysez
parcours parcourir parcoure recense recenser resume resumer synthese synthetise
donne donner fais faire montre montrer liste lister trouve trouver dis dire dit
tous toutes tout toute les le la des du de un une et ou dans sur pour avec
ensemble integralite base documentaire corpus documents document
fichiers fichier ragspace espace informations information elements element
disponibles disponible presents present moi je veux voudrais souhaite
que qui quoi ce cet cette ces est sont y a
concernant concerne relatif relatifs relative relatives propos
limite limites te vous plus pertinents pertinentes sources extraits
ne pas aux au ni non seulement uniquement
what search find all every whole entire the an
in on for about with data across use scan analyze summarize give
show list tell please can you me i want it its
complete complet exhaustive exhaustif globale global vision
""".split())

# Élisions françaises : « l'ensemble » doit se réduire à « ensemble » (mot de
# portée), sinon il passerait pour un sujet.
_ELISION_RE = re.compile(r"\b[ldnjcmst]'")
_WORD_RE = re.compile(r"[0-9a-zA-ZÀ-ÿ][0-9a-zA-ZÀ-ÿ\-]*")


def normalize_query(raw: str) -> str:
    """Normalisation déterministe : espaces compressés, longueur bornée."""
    q = re.sub(r"\s+", " ", (raw or "")).strip()
    return q[:MAX_QUERY_CHARS]


def extract_topic_terms(query: str) -> list[str]:
    """Mots restants une fois la PORTÉE retirée. C'est le « sujet ».

    Les termes sont renvoyés dans leur forme ORIGINALE, **accents conservés** :
    le test de mot-de-portée se fait sur la forme normalisée, mais PostgreSQL ne
    déplie pas les accents (``to_tsvector('french', 'cybersécurité')`` ne matche
    pas ``'cybersecurite'``). Rendre des termes désaccentués produirait zéro
    candidat sur un corpus français — silencieusement.
    """
    from apps.actions.normalize import normalize

    text = _ELISION_RE.sub(" ", query or "")
    terms = []
    for w in _WORD_RE.findall(text):
        if len(w) < 2:
            continue
        if normalize(w) in _SCOPE_STOPWORDS:
            continue
        terms.append(w)
    return terms


def build_fts_query(query: str) -> str:
    """Requête lexicale soumise à ``websearch_to_tsquery``.

    Construite sur les TERMES DU SUJET, jamais sur la question entière :
    ``websearch_to_tsquery`` fait un **ET** de tous les mots, si bien que
    « recherche dans toute la base les informations sur X » n'aurait exigé qu'un
    chunk contenant *aussi* « recherche », « base », « informations »… soit zéro
    candidat.

    Les termes sont joints par ``or`` : une recherche globale privilégie le
    **rappel** (on retient large, le traitement par lots trie ensuite). Un ET
    manquerait les documents ne mentionnant qu'une partie du sujet.
    """
    terms = extract_topic_terms(query)
    return " or ".join(terms)


def has_topic(query: str) -> bool:
    """Une demande globale SANS sujet ne peut pas être recherchée.

    « analyse tous les documents » ne dit pas QUOI chercher : la traiter
    reviendrait à inventer un sujet. On refuse explicitement.
    """
    return len(extract_topic_terms(query)) > 0


def validate_query(raw: str) -> str:
    q = normalize_query(raw)
    if not q:
        raise TopicRequired()
    if not has_topic(q):
        raise TopicRequired()
    return q


# --------------------------------------------------------------------------- #
# Lots
# --------------------------------------------------------------------------- #

def _estimated_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def build_batches(candidates: list[dict], *, max_chunks: int, max_tokens: int) -> list[list[dict]]:
    """Découpe en lots bornés EN CHUNKS et EN TOKENS. Aucun candidat n'est perdu :
    la fonction est une partition — tout candidat entré ressort dans un lot."""
    batches: list[list[dict]] = []
    current: list[dict] = []
    current_tokens = 0

    for c in candidates:
        t = _estimated_tokens(c["text"] or "")
        too_many = len(current) >= max_chunks
        too_big = current and (current_tokens + t) > max_tokens
        if too_many or too_big:
            batches.append(current)
            current, current_tokens = [], 0
        current.append(c)
        current_tokens += t

    if current:
        batches.append(current)
    return batches


_BATCH_PROMPT = """Tu analyses des extraits de documents pour répondre à une recherche.

RECHERCHE : {query}

EXTRAITS :
{extracts}

Consignes :
- N'utilise QUE les extraits ci-dessus. N'invente rien.
- Pour chaque information pertinente trouvée, produis un item.
- Chaque item DOIT citer au moins un identifiant d'extrait parmi ceux fournis.
- Si aucun extrait n'est pertinent, renvoie une liste vide.

Réponds UNIQUEMENT par un JSON valide, sans texte autour :
{{"items": [{{"statement": "...", "source_chunk_ids": ["..."], "confidence": 0.8}}]}}
"""

# Variante ANGLAISE (P1). Utilisée SEULEMENT si la langue résolue est « en » ;
# sinon le prompt français ci-dessus est utilisé À L'OCTET PRÈS. Seuls les
# `statement` (prose générée) changent de langue ; les identifiants d'extraits et
# la structure JSON sont inchangés.
_BATCH_PROMPT_EN = """You analyse document excerpts to answer a search.

SEARCH: {query}

EXCERPTS:
{extracts}

Instructions:
- Use ONLY the excerpts above. Do not invent anything.
- For each relevant piece of information found, produce an item.
- Each item MUST cite at least one excerpt identifier among those provided.
- If no excerpt is relevant, return an empty list.
- Write each "statement" in English.

Respond ONLY with valid JSON, no surrounding text:
{{"items": [{{"statement": "...", "source_chunk_ids": ["..."], "confidence": 0.8}}]}}
"""


def build_batch_prompt(*, query: str, batch: list[dict], answer_language: str | None = None) -> str:
    """Le modèle ne reçoit QUE : identifiant d'extrait, page, texte borné.

    Jamais de nom de fichier, jamais de paramètre technique, jamais de contenu
    hors du RagSpace.
    """
    lines = []
    for c in batch:
        text = (c["text"] or "")[:MAX_CHUNK_CHARS_FOR_LLM]
        page = c.get("page_number")
        page_txt = f" (page {page})" if page is not None else ""
        lines.append(f"[{c['chunk_id']}]{page_txt}\n{text}")
    template = _BATCH_PROMPT_EN if answer_language == "en" else _BATCH_PROMPT
    return template.format(query=query, extracts="\n\n".join(lines))


# --------------------------------------------------------------------------- #
# Validation de la sortie LLM
# --------------------------------------------------------------------------- #

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_batch_output(raw: str, *, allowed_chunk_ids: set[str], max_items: int) -> list[dict]:
    """Valide la sortie d'un lot. Ne fait JAMAIS confiance au modèle.

    - JSON strict ;
    - chaque item doit porter au moins une source ;
    - **chaque `source_chunk_id` doit appartenir au lot transmis** — un
      identifiant inventé (ou emprunté à un autre lot) est écarté ;
    - un item dont toutes les sources sont invalides est REJETÉ, jamais conservé
      « sans source » ;
    - nombre d'items et longueur des énoncés bornés.

    Lève ``ValueError`` si le JSON est inexploitable (le lot sera retenté, puis
    marqué en échec — jamais silencieusement ignoré).
    """
    text = (raw or "").strip()
    m = _JSON_RE.search(text)
    if not m:
        raise ValueError("batch_invalid_json")
    try:
        payload = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise ValueError("batch_invalid_json") from e

    if not isinstance(payload, dict):
        raise ValueError("batch_invalid_json")
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("batch_invalid_json")

    out: list[dict] = []
    for it in items[:max_items]:
        if not isinstance(it, dict):
            continue
        statement = str(it.get("statement") or "").strip()[:MAX_STATEMENT_CHARS]
        if not statement:
            continue

        raw_ids = it.get("source_chunk_ids")
        if not isinstance(raw_ids, list):
            continue
        # Le point non négociable : une source non transmise est une source
        # INVENTÉE. On la retire, et l'item tombe s'il n'en reste aucune.
        valid = [str(cid) for cid in raw_ids if str(cid) in allowed_chunk_ids]
        if not valid:
            continue

        conf = it.get("confidence")
        try:
            confidence = min(1.0, max(0.0, float(conf)))
        except (TypeError, ValueError):
            confidence = None

        out.append({
            "statement": statement,
            "source_chunk_ids": sorted(set(valid)),
            "confidence": confidence,
        })
    return out


# --------------------------------------------------------------------------- #
# Consolidation (déterministe — aucun second appel LLM)
# --------------------------------------------------------------------------- #

def _item_id(statement: str) -> str:
    return hashlib.sha256(statement.encode("utf-8")).hexdigest()[:16]


def _dedup_key(statement: str) -> str:
    from apps.actions.normalize import normalize

    return re.sub(r"[^a-z0-9 ]", "", normalize(statement)).strip()


def consolidate(items: list[dict], *, chunk_index: dict[str, dict]) -> list[dict]:
    """Fusionne les items en conservant TOUTES les sources.

    La déduplication ne fusionne que des énoncés **strictement équivalents** après
    normalisation. Deux formulations différentes sont conservées séparément : une
    divergence n'est jamais supprimée silencieusement — c'est une information.
    """
    merged: dict[str, dict] = {}

    for it in items:
        key = _dedup_key(it["statement"])
        if not key:
            continue
        if key not in merged:
            merged[key] = {
                "id": _item_id(it["statement"]),
                "statement": it["statement"],
                "source_chunk_ids": set(it["source_chunk_ids"]),
                "confidence": it.get("confidence"),
            }
        else:
            # Union des sources : jamais de perte.
            merged[key]["source_chunk_ids"].update(it["source_chunk_ids"])

    out = []
    for m in merged.values():
        sources = []
        for cid in sorted(m["source_chunk_ids"]):
            c = chunk_index.get(cid)
            if not c:
                continue  # source hors périmètre : écartée, jamais publiée
            sources.append({
                "chunk_id": cid,
                "document_id": c["document_id"],
                "page": c.get("page_number"),
            })
        if not sources:
            continue  # aucun item sans source ne sort d'ici
        out.append({
            "id": m["id"],
            "statement": m["statement"],
            "sources": sources,
            "confidence": m["confidence"],
        })
    return out


def attach_document_names(items: list[dict]) -> list[dict]:
    """Estampille le nom du document sur chaque source. **Additif** : rien n'est retiré.

    Le résultat est PERSISTÉ et relu des jours plus tard. Sans ce nom, une source
    n'est qu'un UUID : illisible, et impossible à retrouver si le document a été
    supprimé entre-temps. Les sources du chat figent déjà ``filename`` pour la même
    raison — on applique ici la même convention.

    Une seule requête, sur les seuls documents CITÉS (une poignée), jamais sur les
    candidats. Un document disparu donne ``None``, jamais une exception.
    """
    doc_ids = {s["document_id"] for it in items for s in it["sources"]}
    if not doc_ids:
        return items

    names = {
        str(pk): filename
        for pk, filename in Document.objects.filter(id__in=doc_ids).values_list(
            "id", "filename"
        )
    }
    for it in items:
        for s in it["sources"]:
            s["filename"] = names.get(s["document_id"])
    return items


def _fr_int(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def render_summary_markdown(*, query: str, items: list[dict], search: dict,
                            partial: bool = False) -> str:
    lines = [f"## Recherche globale — {query}", ""]

    if not items:
        # Distinction CRITIQUE. « Zéro candidat » et « candidats trouvés mais non
        # traités » ne sont pas la même chose : afficher « aucune correspondance »
        # après une panne de traitement serait un mensonge — le préfiltre AVAIT
        # trouvé des correspondances.
        if search.get("candidate_chunks", 0) == 0:
            lines += [
                "Aucune correspondance lexicale n’a été trouvée dans les contenus "
                "textuels exploitables.",
                "",
                "Des formulations différentes peuvent ne pas avoir été reconnues : "
                "l’absence de correspondance ne prouve pas l’absence du sujet.",
            ]
        else:
            lines += [
                "⚠️ **Analyse partielle** — aucun extrait n’a pu être traité.",
                "",
                f"Le préfiltre lexical avait pourtant retenu "
                f"**{_fr_int(search['candidate_chunks'])} extraits candidats** dans "
                f"**{_fr_int(search['candidate_documents'])} documents**. "
                f"Le traitement n’a pas abouti : **aucune conclusion ne peut être "
                f"tirée** de cette analyse.",
                "",
                "Relancez l’analyse ultérieurement.",
            ]
        return "\n".join(lines)

    for i, it in enumerate(items, start=1):
        refs = " ".join(f"[{i}]" for i in range(1, len(it["sources"]) + 1))
        lines.append(f"{i}. {it['statement']} {refs}".rstrip())
    lines.append("")

    if partial:
        lines.append("⚠️ **Analyse partielle** — certains lots n’ont pas pu être traités.")
        lines.append("")

    lines += [
        f"Le préfiltre lexical a interrogé **{_fr_int(search['chunks_scanned'])} extraits** "
        f"répartis sur **{_fr_int(search['documents_text_scanned'])} documents exploitables**, "
        f"et retenu **{_fr_int(search['candidate_chunks'])} extraits candidats** dans "
        f"**{_fr_int(search['candidate_documents'])} documents**.",
        "",
        # La ligne qui empêche de lire un préfiltre lexical comme une garantie.
        "La recherche lexicale peut ne pas reconnaître toutes les formulations "
        "équivalentes du sujet.",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Handler
# --------------------------------------------------------------------------- #

def _generate(*, prompt: str, rag_space) -> dict:
    """Génération via le routeur existant (AI Gateway), fallback externe COUPÉ.

    Contexte système *local-only* par défaut : même si le RagSpace autorise un
    repli externe pour les questions ordinaires, une analyse globale enverrait
    *tout le corpus candidat* chez un tiers. Ce n'est pas le même contrat de
    confidentialité. Le flag ``CORPUS_GLOBAL_SEARCH_ALLOW_EXTERNAL_FALLBACK`` ne
    lève cette interdiction qu'en la SOUMETTANT en plus à la politique de
    l'espace — il n'autorise rien à lui seul.
    """
    from apps.llm_router.egress import LlmExecutionContext
    from apps.llm_router.router import generate_text

    allow = bool(getattr(settings, "CORPUS_GLOBAL_SEARCH_ALLOW_EXTERNAL_FALLBACK", False))
    if allow:
        # Le flag GLOBAL ne suffit pas : la politique de l'espace reste le plafond.
        ctx = LlmExecutionContext.for_rag_space(
            rag_space, operation="analysis.global_search"
        )
    else:
        # Défaut : une analyse globale enverrait *tout le corpus candidat* chez un
        # tiers. Refus structurel, indépendant du réglage de l'espace.
        ctx = LlmExecutionContext.system_local_only(
            operation="analysis.global_search"
        )
    return generate_text(
        prompt=prompt,
        provider=getattr(rag_space, "llm_provider", None),
        model=getattr(rag_space, "llm_model", None),
        context=ctx,
        retry_on_empty=True,
    )


# Erreurs TRANSITOIRES : rejouer a un sens (le service peut se rétablir).
_TRANSIENT_MARKERS = (
    "timeout", "timed out", "connection", "refused", "reset", "unavailable",
    "temporarily", "429", "502", "503", "504", "bad gateway", "gateway timeout",
)


def is_transient_error(exc: Exception) -> bool:
    """Un JSON invalide, une source inventée ou une annulation ne sont PAS
    transitoires : les rejouer ne changerait rien et coûterait deux fois."""
    if isinstance(exc, (ValueError, AnalysisCancelled, GlobalSearchError)):
        return False
    text = f"{type(exc).__name__} {exc}".lower()
    return any(m in text for m in _TRANSIENT_MARKERS)


def _generate_with_retry(*, prompt: str, rag_space, job_id) -> dict:
    """Génère un lot, avec retry BORNÉ sur erreur transitoire uniquement.

    L'annulation est vérifiée AVANT chaque nouvelle tentative : un job annulé ne
    doit pas continuer à dépenser.
    """
    max_retries = int(getattr(settings, "CORPUS_GLOBAL_SEARCH_MAX_RETRIES", 2))
    backoff = list(getattr(settings, "CORPUS_GLOBAL_SEARCH_RETRY_BACKOFF_S", [2, 5]))

    attempt = 0
    while True:
        try:
            return _generate(prompt=prompt, rag_space=rag_space)
        except Exception as exc:
            if attempt >= max_retries or not is_transient_error(exc):
                raise
            raise_if_cancel_requested(job_id)  # jamais de retry sur un job annulé
            delay = backoff[min(attempt, len(backoff) - 1)] if backoff else 0
            # Aucun contenu de lot n'est journalisé — seulement le type d'erreur.
            logger.warning(
                "corpus_analysis.batch_retry analysis_id=%s attempt=%s error_type=%s",
                job_id, attempt + 1, type(exc).__name__,
            )
            if delay:
                time.sleep(delay)
            attempt += 1


def run_global_search(*, job, rag_space) -> dict:
    """Handler enregistré pour ``corpus.global_search``."""
    from apps.rag_core.services.coverage_service import compute_rag_space_statistics

    params = job.parameters or {}
    query = validate_query(params.get("query") or "")
    configuration = resolve_fts_configuration(language=params.get("language"))

    # Langue de RÉPONSE résolue (P2) : préférence DÉDIÉE `parameters.answer_language`
    # (auto|fr|en), SÉPARÉE de `parameters.language` (config FTS / recherche). None si
    # politique désactivée → prompt français historique (à l'octet près).
    from apps.rag_core.services.answer_language import resolve_target

    answer_language = resolve_target(pref=params.get("answer_language") or "auto", question=query)

    batch_chunks = int(settings.CORPUS_GLOBAL_SEARCH_BATCH_CHUNKS)
    batch_tokens = int(settings.CORPUS_GLOBAL_SEARCH_MAX_BATCH_TOKENS)
    max_items = int(settings.CORPUS_GLOBAL_SEARCH_MAX_OUTPUT_ITEMS)
    max_batches = int(settings.CORPUS_GLOBAL_SEARCH_MAX_BATCHES)

    # Le vrai plafond est le nombre d'APPELS LLM, pas le nombre de candidats :
    # c'est lui qui fait le coût. On borne donc les candidats par ce que
    # `max_batches` peut absorber.
    max_candidates = min(
        int(settings.CORPUS_GLOBAL_SEARCH_MAX_CANDIDATE_CHUNKS),
        max_batches * batch_chunks,
    )

    # ---- 1) Périmètre --------------------------------------------------- #
    raise_if_cancel_requested(job.id)
    update_job_progress(job.id, phase=PHASE_COUNTING_SCOPE, current=0, total=100)

    stats = compute_rag_space_statistics(rag_space.id)
    base_coverage = stats["coverage"]
    scope = scan_scope(rag_space_id=rag_space.id)
    scanned = scope["chunks_scanned"]

    # ---- 2) Planification de la requête, AVANT toute dépense ------------- #
    # Escalade progressive pilotée par le VOLUME RÉEL : on garde le OU (meilleur
    # rappel) tant qu'il tient, on resserre en ET s'il explose, on REFUSE si rien
    # ne tient. Jamais de troncature — prendre « les N premiers » présenterait un
    # échantillon comme un parcours du corpus.
    raise_if_cancel_requested(job.id)
    update_job_progress(job.id, phase=PHASE_SEARCHING_CANDIDATES, current=5, total=100)

    def _df(terms):
        return term_document_frequencies(
            rag_space_id=rag_space.id, terms=terms, configuration=configuration
        )

    def _counter(expr):
        return count_global_fts_candidates(
            rag_space_id=rag_space.id, query=expr, configuration=configuration,
            mode="boolean",
        )

    gq = build_global_fts_query(
        query, configuration=configuration, df_provider=_df, scanned_chunks=scanned,
    )
    try:
        plan = plan_global_search_query(
            gq, counter=_counter, max_candidate_chunks=max_candidates
        )
    except QueryTooBroad:
        # Aucune formulation ne tient : on refuse, sans dépenser un seul token.
        raise GlobalSearchError(ERROR_QUERY_TOO_BROAD) from None

    fts_query = plan.expression
    candidate_chunks = plan.candidate_chunks

    # ---- Pré-vol dynamique : coût estimé AVANT le premier appel ---------- #
    estimated_batches = max(1, -(-candidate_chunks // batch_chunks)) if candidate_chunks else 0
    estimated_input_tokens = candidate_chunks * (MAX_CHUNK_CHARS_FOR_LLM // 4)
    if estimated_input_tokens > int(settings.CORPUS_GLOBAL_SEARCH_MAX_ESTIMATED_INPUT_TOKENS):
        raise GlobalSearchError(ERROR_QUERY_TOO_BROAD)

    search_meta = {
        "chunks_scanned": scanned,
        "documents_text_scanned": scope["documents_text_scanned"],
        "candidate_chunks": candidate_chunks,
        "candidate_documents": plan.candidate_documents,
        "processed_chunks": 0,
        "processed_documents": 0,
        "batches_total": 0,
        "batches_failed": 0,
        "batches_retried": 0,
        # Le choix de formulation est AUDITABLE : quelles stratégies ont été
        # essayées, et avec quel volume.
        "query_strategy": plan.strategy,
        "query_attempts": list(plan.attempts),
        "estimated_batches": estimated_batches,
        "estimated_input_tokens": estimated_input_tokens,
    }

    # ---- 3) Zéro candidat : aucun appel LLM ------------------------------ #
    if candidate_chunks == 0:
        coverage = _build_coverage(
            rag_space=rag_space, base=base_coverage, stats=stats,
            documents_with_results=0, chunks_analyzed=scope["chunks_scanned"],
            items=[], limits=(), batches_failed=0,
        )
        update_job_progress(job.id, phase=PHASE_PERSISTING_RESULT, current=95, total=100)
        return _package(
            query=query, items=[], search=search_meta, coverage=coverage,
            configuration=configuration, tokens_in=0, tokens_out=0, partial=False,
        )

    # ---- 4) Récupération de TOUS les candidats (keyset, sans troncature) -- #
    candidates = list(iterate_global_fts_candidates(
        rag_space_id=rag_space.id, query=fts_query, configuration=configuration,
        mode="boolean",
    ))
    # Le compte a été établi avant : toute divergence est une anomalie, pas une
    # troncature acceptable.
    chunk_index = {c["chunk_id"]: c for c in candidates}

    batches = build_batches(candidates, max_chunks=batch_chunks, max_tokens=batch_tokens)
    search_meta["batches_total"] = len(batches)

    # ---- 5) Traitement par lots ------------------------------------------ #
    raw_items: list[dict] = []
    processed_chunk_ids: set[str] = set()
    batches_failed = 0
    tokens_in = tokens_out = 0

    for idx, batch in enumerate(batches, start=1):
        raise_if_cancel_requested(job.id)  # AVANT chaque lot

        allowed = {c["chunk_id"] for c in batch}
        prompt = build_batch_prompt(query=query, batch=batch, answer_language=answer_language)

        try:
            resp = _generate_with_retry(prompt=prompt, rag_space=rag_space, job_id=job.id)
            usage = resp.get("usage") or {}
            tokens_in += int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            tokens_out += int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
            items = parse_batch_output(
                resp.get("text") or "", allowed_chunk_ids=allowed, max_items=max_items
            )
            raw_items.extend(items)
            processed_chunk_ids.update(allowed)
        except Exception:
            # Un lot en échec ne fait JAMAIS échouer silencieusement l'analyse :
            # il est compté, et le job finira `partial`, jamais `succeeded`.
            batches_failed += 1
            logger.exception(
                "corpus_analysis.batch_failed analysis_id=%s batch=%s/%s",
                job.id, idx, len(batches),
            )

        raise_if_cancel_requested(job.id)  # APRÈS chaque lot
        percent = 10 + int(80.0 * idx / max(1, len(batches)))
        update_job_progress(job.id, phase=PHASE_PROCESSING_BATCHES,
                            current=percent, total=100)

    search_meta["processed_chunks"] = len(processed_chunk_ids)
    search_meta["processed_documents"] = len({
        chunk_index[cid]["document_id"] for cid in processed_chunk_ids
    })
    search_meta["batches_failed"] = batches_failed

    # ---- 6) Consolidation ------------------------------------------------ #
    raise_if_cancel_requested(job.id)
    update_job_progress(job.id, phase=PHASE_CONSOLIDATING, current=92, total=100)

    items = consolidate(raw_items, chunk_index=chunk_index)

    # Verrou final : aucun item sans source ne sort d'ici.
    unsourced = [it for it in items if not it.get("sources")]
    if unsourced:
        raise GlobalSearchError(ERROR_UNSOURCED_RESULT)

    attach_document_names(items)

    docs_with_results = {
        s["document_id"] for it in items for s in it["sources"]
    }

    limits: tuple[str, ...] = ()
    if batches_failed:
        limits = ("failed_batches",)

    coverage = _build_coverage(
        rag_space=rag_space, base=base_coverage, stats=stats,
        documents_with_results=len(docs_with_results),
        chunks_analyzed=scope["chunks_scanned"],
        items=items, limits=limits, batches_failed=batches_failed,
    )

    raise_if_cancel_requested(job.id)
    update_job_progress(job.id, phase=PHASE_PERSISTING_RESULT, current=97, total=100)

    return _package(
        query=query, items=items, search=search_meta, coverage=coverage,
        configuration=configuration, tokens_in=tokens_in, tokens_out=tokens_out,
        partial=batches_failed > 0,
    )


def _build_coverage(*, rag_space, base, stats, documents_with_results: int,
                    chunks_analyzed: int, items: list[dict],
                    limits: tuple[str, ...], batches_failed: int) -> Coverage:
    """Couverture SPÉCIFIQUE à L4 — jamais celle de /stats/ recopiée.

    ``documents_analyzed`` = documents dont les chunks ont RÉELLEMENT été évalués
    par le préfiltre (les READY avec contenu). Un document FAILED reste considéré
    mais non analysé : la couverture est donc incomplète, et elle le dit.
    """
    documents = stats["documents"]
    considered = int(documents["total"])
    analyzed = int(base.documents_analyzed)  # READY avec chunks = évalués par le FTS

    excluded: list[CoverageExclusion] = []
    for reason, count in (
        (REASON_EXTRACTION_FAILED, int(documents["failed"])),
        (REASON_NOT_READY, int(documents["pending"])),
        (REASON_PROCESSING, int(documents["ingesting"])),
        (REASON_NO_TEXT_CONTENT,
         max(0, int(documents["ready"]) - int(base.documents_analyzed))),
    ):
        if count > 0:
            excluded.append(CoverageExclusion(reason=reason, count=count))

    if batches_failed:
        excluded.append(CoverageExclusion(reason=REASON_LIMIT_APPLIED, count=batches_failed))

    complete = (analyzed == considered) and not excluded and not limits

    return Coverage(
        scope=CoverageScope(
            rag_space_id=str(rag_space.id),
            generated_at=base.scope.generated_at,
            corpus_fingerprint=base.scope.corpus_fingerprint,
            filters_applied=(),
        ),
        documents_total=considered,
        documents_considered=considered,
        documents_analyzed=analyzed,
        documents_with_results=min(documents_with_results, analyzed),
        documents_excluded=tuple(excluded),
        pages_analyzed=0,
        chunks_analyzed=chunks_analyzed,
        items_extracted=len(items),
        items_without_source=0,  # verrouillé en amont
        coverage_level="B" if chunks_analyzed > 0 else "A",
        coverage_complete=complete,
        coverage_ratio=compute_coverage_ratio(
            documents_analyzed=analyzed, documents_considered=considered
        ),
        limits_applied=limits,
    )


def _package(*, query, items, search, coverage, configuration,
             tokens_in, tokens_out, partial) -> dict:
    return {
        "result": {
            "result_version": RESULT_VERSION,
            "query": query,
            "summary_markdown": render_summary_markdown(
                query=query, items=items, search=search, partial=partial
            ),
            "items": items,
            "search": {**search, "fts_configuration": configuration},
        },
        "coverage": coverage_to_dict(coverage),
        "corpus_fingerprint": coverage.scope.corpus_fingerprint,
        "progress_total": 100,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "partial": partial,
    }
