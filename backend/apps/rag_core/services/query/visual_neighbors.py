"""
Voisinage visuel de page — étape de SÉLECTION, jamais de score (VISUAL-RETRIEVAL-QUALITY-1B).

Le défaut corrigé, mesuré (VISUAL-RETRIEVAL-QUALITY-1A). Une question sur un plan de
phasage remontait le texte natif de la page 22 en tête ; la description visuelle de la
MÊME page, qui seule portait la réponse, était bien candidate, bien soumise au
reranking (9e), puis reléguée au 24e rang par la diversification inter-documents (sa
passe 1 prend d'abord trois passages de CHAQUE document, même très mal classés) — et
coupée par le budget de prompt. Le modèle ne lisait que le texte natif et répondait
faux.

L'étape ci-dessous se place APRÈS la diversification et AVANT le budget de prompt.
Pour chacun des premiers passages NATIFS retenus, elle place juste derrière lui les
passages VISUELS du même document et de la même page, pris dans le pool DÉJÀ récupéré.

Ce qu'elle ne fait pas, délibérément :

* aucune requête : le pool est FERMÉ ; un visuel absent du pool n'est jamais chargé ;
* aucun score : ni vectoriel, ni lexical, ni RRF, ni rerank n'est lu pour décider ou
  réécrit — seul l'ordre de la séquence destinée au prompt change ;
* aucune rétrogradation : un visuel déjà placé AVANT son passage natif y reste ;
* aucun budget supplémentaire : la séquence garde sa longueur, le budget de prompt
  existant tranche ensuite comme avant ;
* aucune page inventée : un passage sans ``page_number`` n'a pas de voisin. DOCX,
  image autonome et diapositives PPTX (dont la position est ``slide_number``, pas une
  page) ne sont donc jamais concernés.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from apps.rag_core.domain.ranking import sort_candidates

from .diversify import _rrf_key

logger = logging.getLogger(__name__)

#: Nombre de passages NATIFS retenus (dans l'ordre final) pour lesquels on cherche un
#: voisin visuel. Qualifié sur le corpus figé de 1A (cf. docs/03-backend/pipeline-retrieval.md §5bis).
MAX_NATIVE_NEIGHBOR_EXPANSIONS = 3
#: Voisins visuels au plus par passage natif.
MAX_VISUAL_NEIGHBORS_PER_NATIVE = 1
#: Motif consigné dans la trace de retrieval.
PROMOTION_REASON = "same_page_visual_neighbor"


@dataclass(frozen=True)
class NeighborPromotion:
    """Un visuel placé derrière son passage natif. Identifiants et positions seulement."""

    native_chunk_id: str
    visual_chunk_id: str
    document_id: str
    page_number: int
    #: Position (1-based) dans la séquence diversifiée ; ``None`` si le visuel venait
    #: du pool sans avoir été retenu.
    original_position: int | None
    promoted_position: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": PROMOTION_REASON,
            "native_chunk_id": self.native_chunk_id,
            "visual_chunk_id": self.visual_chunk_id,
            "document_id": self.document_id,
            "page_number": self.page_number,
            "original_position": self.original_position,
            "promoted_position": self.promoted_position,
        }


def _source(chunk: dict[str, Any]) -> dict[str, Any]:
    src = chunk.get("source")
    return src if isinstance(src, dict) else {}


def _chunk_id(chunk: dict[str, Any]) -> str:
    cid = chunk.get("chunk_id")
    return str(cid) if cid not in (None, "") else ""


def _visual_meta(chunk: dict[str, Any]) -> dict[str, Any] | None:
    """``source.meta.visual`` — le contrat persisté de l'évidence visuelle."""
    meta = _source(chunk).get("meta")
    visual = meta.get("visual") if isinstance(meta, dict) else None
    return visual if isinstance(visual, dict) else None


def _page(chunk: dict[str, Any]) -> int | None:
    page = _source(chunk).get("page_number")
    if isinstance(page, bool) or not isinstance(page, int):
        return None
    return page


def _page_key(chunk: dict[str, Any]) -> tuple[str, str, int] | None:
    """(provenance, document, page) — ``None`` si l'une des trois manque."""
    src = _source(chunk)
    document_id = src.get("document_id")
    page = _page(chunk)
    if document_id in (None, "") or page is None:
        return None
    provenance = str(src.get("source_type") or chunk.get("source_type") or "")
    return provenance, str(document_id), page


def _eligible_visual(chunk: dict[str, Any]) -> bool:
    """Visuel publiable et sûr : texte présent, aucune vérification humaine demandée."""
    visual = _visual_meta(chunk)
    if visual is None or visual.get("requires_visual_check") is True:
        return False
    if not _chunk_id(chunk):
        return False
    return bool(str(chunk.get("text") or "").strip())


def promote_same_page_visuals(
    selected: list[dict[str, Any]],
    pool: list[dict[str, Any]],
    *,
    max_natives: int = MAX_NATIVE_NEIGHBOR_EXPANSIONS,
    max_per_native: int = MAX_VISUAL_NEIGHBORS_PER_NATIVE,
) -> tuple[list[dict[str, Any]], list[NeighborPromotion]]:
    """Séquence enrichie + promotions. Ne mute ni ``selected``, ni ``pool``, ni leurs éléments.

    Les visuels d'une même page sont pris dans l'ordre CANONIQUE du pool (rerank s'il a
    eu lieu, RRF sinon — la clé même de la diversification), départagé de façon
    déterministe. La séquence rendue a la même longueur que ``selected`` : un visuel
    ajouté depuis le pool pousse hors de la séquence le dernier passage.
    """
    if not selected or max_natives <= 0 or max_per_native <= 0:
        return list(selected or []), []

    ordered_pool = sort_candidates(list(pool or []), relevance_key=_rrf_key)
    by_id: dict[str, dict[str, Any]] = {}
    for c in list(selected) + ordered_pool:
        cid = _chunk_id(c)
        if cid and cid not in by_id:
            by_id[cid] = c
    visuals_by_page: dict[tuple[str, str, int], list[str]] = {}
    for c in ordered_pool:
        cid = _chunk_id(c)
        key = _page_key(c)
        if key is not None and _eligible_visual(c) and cid not in visuals_by_page.get(key, ()):
            visuals_by_page.setdefault(key, []).append(cid)
    if not visuals_by_page:
        return list(selected), []

    position = {}
    for i, c in enumerate(selected, 1):
        position.setdefault(_chunk_id(c), i)

    attached: dict[str, list[str]] = {}
    claimed: set[str] = set()
    natives_seen = 0
    for c in selected:
        if natives_seen >= max_natives:
            break
        if _visual_meta(c) is not None:
            continue
        natives_seen += 1
        key = _page_key(c)
        native_id = _chunk_id(c)
        if key is None or not native_id:
            continue
        picks: list[str] = []
        taken = 0
        for vid in visuals_by_page.get(key, ()):
            if taken >= max_per_native:
                break
            if vid in claimed:
                continue
            claimed.add(vid)
            taken += 1
            vpos = position.get(vid)
            if vpos is not None and vpos < position[native_id]:
                # Déjà devant son natif : mieux placé qu'un voisin, on ne le rétrograde pas.
                continue
            picks.append(vid)
        if picks:
            attached[native_id] = picks

    if not attached:
        return list(selected), []

    moved = {vid for vids in attached.values() for vid in vids}
    out: list[dict[str, Any]] = []
    emitted: set[str] = set()
    promotions: list[NeighborPromotion] = []
    for c in selected:
        cid = _chunk_id(c)
        if cid in moved or (cid and cid in emitted):
            continue
        out.append(c)
        if cid:
            emitted.add(cid)
        for vid in attached.get(cid, ()):
            out.append(by_id[vid])
            emitted.add(vid)
            key = _page_key(c)
            promotions.append(NeighborPromotion(
                native_chunk_id=cid,
                visual_chunk_id=vid,
                document_id=key[1],
                page_number=key[2],
                original_position=position.get(vid),
                promoted_position=len(out),
            ))

    out = out[:len(selected)]
    kept = {_chunk_id(c) for c in out}
    promotions = [p for p in promotions if p.visual_chunk_id in kept]
    for p in promotions:
        logger.debug(
            "retrieval.%s native=%s visual=%s document=%s page=%s from=%s to=%s",
            PROMOTION_REASON, p.native_chunk_id, p.visual_chunk_id, p.document_id,
            p.page_number, p.original_position, p.promoted_position,
        )
    return out, promotions
