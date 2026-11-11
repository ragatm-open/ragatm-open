# backend/apps/actions/resolvers.py
"""Résolution de la cible d'un résumé — **autorité backend** (lot 3b).

Résout « Rapport.pdf » / « ce document » / « la source 2 » vers un ``Document``
concret, **strictement dans le RagSpace autorisé** (isolation + anti-énumération :
un document d'un autre espace n'est jamais trouvé, donc jamais révélé).

Aucune génération ici : uniquement de la résolution. DB en lecture seule.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .normalize import normalize

_EXT_RE = re.compile(r"\.[a-z0-9]+$")


def _stem(s: str) -> str:
    return _EXT_RE.sub("", normalize(s))


def _name_matches(query: str, filename: str) -> bool:
    """Port de ``nameMatches`` (summaryIntent.ts) : insensible casse/accents."""
    q = normalize(query)
    f = normalize(filename)
    if not q or not f:
        return False
    if q in f or f in q:
        return True
    fs, qs = _stem(f), _stem(q)
    return bool(fs) and (qs in fs or fs in qs)


def _src_doc_id(src: dict[str, Any]) -> str | None:
    """Extraction tolérante de l'id document d'une source (cf. srcDocId front)."""
    if not isinstance(src, dict):
        return None
    inner = src.get("source") if isinstance(src.get("source"), dict) else {}
    for cand in (src.get("document_id"), inner.get("document_id"), inner.get("document"), src.get("document")):
        if cand:
            return str(cand)
    return None


#: Provenances d'une source citée. Miroir backend de ``srcType`` côté frontend.
SOURCE_TYPE_WORKSPACE = "workspace_document"
SOURCE_TYPE_DOMAIN_CORPUS = "domain_corpus"


def _src_source_type(src: dict[str, Any]) -> str:
    """Provenance d'une source, avec repli historique sur le document privé.

    Le repli n'est pas une devinette : les sources sans ``source_type`` datent
    d'avant la v2 du contrat, époque à laquelle seules des sources privées
    existaient. En revanche, un ``document_id`` de corpus ne doit JAMAIS être
    cherché dans ``Document`` — c'est précisément ce que ce discriminant empêche.
    """
    if not isinstance(src, dict):
        return SOURCE_TYPE_WORKSPACE
    inner = src.get("source") if isinstance(src.get("source"), dict) else {}
    raw = str(src.get("source_type") or inner.get("source_type") or "").strip()
    return raw if raw in (SOURCE_TYPE_WORKSPACE, SOURCE_TYPE_DOMAIN_CORPUS) else SOURCE_TYPE_WORKSPACE


@dataclass
class SummaryTarget:
    status: str  # "resolved" | "clarification"
    document: Any = None
    display_name: str = ""
    scope: str = "document"
    page: int | None = None
    message: str = ""
    candidates: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    #: Famille du document résolu. Détermine quel service de résumé exécuter :
    #: le routeur ne doit jamais deviner à partir de la forme de l'UUID.
    source_type: str = SOURCE_TYPE_WORKSPACE


def _clarify(message: str, *, missing: list[str] | None = None, candidates: list[str] | None = None) -> SummaryTarget:
    return SummaryTarget(
        status="clarification",
        message=message,
        missing=missing or ["document_reference"],
        candidates=candidates or ["document.summarize"],
    )


def _ready_docs(rag_space):
    from apps.rag_core.models import Document
    from apps.rag_core.models.document import DocumentStatus

    return Document.objects.filter(rag_space=rag_space, status=DocumentStatus.READY)


def _resolve_by_name(rag_space, name: str) -> SummaryTarget:
    """Résolution par NOM — documents privés de l'espace uniquement.

    Volontairement non étendue au corpus métier : un nom de fichier ne porte
    aucune provenance, et chercher dans les deux familles rendrait le résultat
    dépendant d'une collision de noms. Une source de corpus se résume depuis sa
    citation (« résume la source 2 »), où la provenance est explicite.
    """
    name = (name or "").strip()
    if not name:
        return _clarify("Quel document souhaitez-vous résumer ?")
    seen: set[str] = set()
    matches = []
    for d in _ready_docs(rag_space).only("id", "filename"):
        if _name_matches(name, d.filename) and str(d.id) not in seen:
            seen.add(str(d.id))
            matches.append(d)
    if len(matches) == 1:
        return SummaryTarget(status="resolved", document=matches[0], display_name=matches[0].filename)
    if len(matches) > 1:
        noms = ", ".join(m.filename for m in matches[:8])
        return _clarify(
            f"Plusieurs documents correspondent à « {name} » : {noms}. Précisez lequel résumer."
        )
    return _clarify(f"Je n’ai pas trouvé de document nommé « {name} » dans cet espace.")


def _last_answer_sources(user, rag_space, thread_id) -> list[dict[str, Any]]:
    if not thread_id:
        return []
    from apps.chat.models import ChatMessage, ChatThread

    thread = ChatThread.objects.filter(id=thread_id, user=user, rag_space=rag_space).first()
    if thread is None:
        return []
    msg = (
        ChatMessage.objects.filter(thread=thread, role="assistant")
        .order_by("-created_at")
        .only("sources")
        .first()
    )
    srcs = getattr(msg, "sources", None) if msg else None
    return srcs if isinstance(srcs, list) else []


def _doc_in_space(rag_space, document_id):
    from apps.rag_core.models import Document
    from apps.rag_core.models.document import DocumentStatus

    return Document.objects.filter(
        id=document_id, rag_space=rag_space, status=DocumentStatus.READY
    ).only("id", "filename").first()


def _corpus_doc_for_space(user, rag_space, document_id):
    """Document de corpus métier accessible depuis le workspace de cet espace.

    Passe par le résolveur canonique du corpus (association workspace ↔ corpus,
    corpus actif) : le routeur d'actions ne réimplémente aucun contrôle d'accès.
    ``None`` si inaccessible — l'appelant produit alors une clarification qui ne
    révèle ni l'existence du document ni celle du corpus.
    """
    from apps.corpus.services.access import load_accessible_domain_corpus_document
    from apps.corpus.services.summary import is_enabled

    if not is_enabled():
        return None
    workspace = getattr(rag_space, "workspace", None)
    try:
        return load_accessible_domain_corpus_document(
            user=user, workspace=workspace, document_id=document_id
        )
    except Exception:
        return None


def _resolve_cited_source(user, rag_space, src: dict) -> SummaryTarget | None:
    """Résout UNE source citée vers sa cible, selon sa provenance déclarée.

    Retourne ``None`` quand la source n'est reliée à aucun document résumable :
    l'appelant décide alors du texte de clarification. Aucune tentative croisée —
    un identifiant de corpus n'est jamais cherché dans ``Document``, et
    réciproquement.
    """
    did = _src_doc_id(src)
    if not did:
        return None

    if _src_source_type(src) == SOURCE_TYPE_DOMAIN_CORPUS:
        doc = _corpus_doc_for_space(user, rag_space, did)
        if doc is None:
            return None
        return SummaryTarget(
            status="resolved", document=doc, display_name=doc.display_title,
            source_type=SOURCE_TYPE_DOMAIN_CORPUS,
        )

    doc = _doc_in_space(rag_space, did)
    if doc is None:
        return None
    return SummaryTarget(status="resolved", document=doc, display_name=doc.filename)


def _resolve_from_thread(user, rag_space, thread_id, ref: str, params: dict) -> SummaryTarget:
    sources = _last_answer_sources(user, rag_space, thread_id)
    if not sources:
        return _clarify(
            "Précisez le document à résumer (par son nom), ou posez d’abord une question "
            "pour afficher des sources."
        )

    if ref == "source":
        n = int(params.get("source_n") or 0)
        if n < 1 or n > len(sources):
            return _clarify(f"La dernière réponse ne contient que {len(sources)} source(s).")
        target = _resolve_cited_source(user, rag_space, sources[n - 1])
        if target is None:
            return _clarify("Cette source n’est pas reliée à un document résumable de cet espace.")
        return target

    # deictic / implicit : documents distincts cités dans la dernière réponse.
    # La clé de distinction inclut la PROVENANCE : deux espaces de noms d'UUID
    # disjoints peuvent en théorie collisionner, et « un seul document cité » doit
    # rester vrai au sens de la cible, pas au sens de la chaîne.
    distinct: list[tuple[str, str]] = []
    by_key: dict[tuple[str, str], dict] = {}
    for s in sources:
        did = _src_doc_id(s)
        if not did:
            continue
        key = (_src_source_type(s), did)
        if key not in by_key:
            by_key[key] = s
            distinct.append(key)
    if len(distinct) == 1:
        target = _resolve_cited_source(user, rag_space, by_key[distinct[0]])
        if target is not None:
            return target
        return _clarify("Le document cité n’est pas résumable dans cet espace.")
    if len(distinct) > 1:
        return _clarify(
            "Plusieurs documents sont cités dans la dernière réponse. Précisez lequel résumer "
            "(ex. « résume la source 2 »)."
        )
    return _clarify("Précisez le document à résumer (par son nom).")


def resolve_summary_target(*, user, rag_space, thread_id, plan) -> SummaryTarget:
    """Point d'entrée : renvoie une cible ``resolved`` (avec ``document``) ou une
    ``clarification`` (aucune génération). Tout est borné au ``rag_space`` autorisé."""
    params = dict(plan.parameters or {})
    ref = params.get("ref_kind")

    if rag_space is None:
        return _clarify("Précisez le document à résumer.")

    if ref == "filename":
        return _resolve_by_name(rag_space, params.get("document_reference") or "")
    if ref in ("source", "deictic", "implicit"):
        return _resolve_from_thread(user, rag_space, thread_id, ref, params)
    return _clarify("Quel document souhaitez-vous résumer ?")
