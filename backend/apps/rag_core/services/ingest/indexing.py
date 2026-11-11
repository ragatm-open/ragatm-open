# backend/apps/rag_core/services/ingest/indexing.py
from __future__ import annotations

"""
Cœur commun d'indexation : extraction (couche `extraction`) -> chunking ->
embeddings -> Chunk.bulk_create -> statut Document.

Source unique réutilisée par l'upload synchrone, la réindexation et le worker
Celery, afin d'éviter la duplication TXT/PDF historique et d'ajouter MD/DOCX/CSV/
HTML/OCR de façon cohérente.

Mapping des statuts d'extraction sur Document (l'enum DB n'a que PENDING/INGESTING/
READY/FAILED) :
- success / partial            -> READY (meta.extraction_status conserve le détail) ;
- empty / failed / rejected    -> FAILED + error_message clair (jamais indexé vide).
"""

import logging
import re
from dataclasses import dataclass
from typing import Optional

from django.conf import settings
from django.db import DatabaseError, transaction

from apps.llm_router.egress import LlmExecutionContext
from apps.llm_router.embedding_pipeline import TASK_DOCUMENT
from apps.llm_router.embeddings import (
    embed_texts,  # noqa: F401 — conservé : chemin public historique
    embed_texts_with_identity,
    resolve_space_embedding,
)
from apps.rag_core.models import Chunk, Document
from apps.rag_core.models.document import DocumentStatus
from apps.rag_core.services.chunking import chunk_text_deterministic

from .extraction import ExtractionError, ExtractionResult, ExtractionStatus, extract_document
from .extraction.base import RejectedFile, UnsupportedFormat
from .types import IngestError, IngestResult, RetryableIngestError, is_retryable_error
from .utils import filter_nonblank_specs, resolve_chunk_budget, settings_int

logger = logging.getLogger(__name__)


def _merged_meta(current, patch: dict) -> dict:
    """``Document.meta`` courant + ``patch``, clé par clé (les clés étrangères — par
    exemple ``import`` — sont conservées). Pure mémoire : à appliquer sur la ligne
    verrouillée, dans la même transaction que l'état qu'il décrit (INGEST-1F1-R2)."""
    merged = dict(current) if isinstance(current, dict) else {}
    merged.update(patch or {})
    return merged


_CODE_LIKE = re.compile(r"^([a-z][a-z0-9_]{2,63})\b")


def _code_of_exception(exc: Exception) -> str:
    """Code d'erreur STABLE d'une exception, ou `internal_error`.

    Les erreurs métier du pipeline portent leur code comme message
    (`IngestError("no_valid_chunks")`) ; une exception technique n'en a pas. On ne
    fabrique donc pas un code depuis un texte quelconque : à défaut, `internal_error`.
    """
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code.strip():
        return code.strip()
    m = _CODE_LIKE.match(str(exc or "").strip())
    return m.group(1) if m else "internal_error"


def _with_error_code(extraction_meta: dict, code: str) -> dict:
    """Ajoute `error_code`/`extraction_status=failed` au bloc `extraction`.

    Sans écraser ce que l'extraction a déjà renseigné (pages, moteur, warnings) :
    ces métadonnées restent utiles pour comprendre l'échec.
    """
    patch = {k: dict(v) if isinstance(v, dict) else v for k, v in (extraction_meta or {}).items()}
    ext = patch.get("extraction")
    if not isinstance(ext, dict):
        ext = {}
    ext.setdefault("error_code", code)
    ext["extraction_status"] = "failed"
    patch["extraction"] = ext
    return patch


@dataclass(frozen=True)
class PreviousIndex:
    """État de l'index d'un document AVANT une tentative d'indexation (INGEST-1C).

    ``exists`` : des fragments sont déjà publiés. Un fragment n'est jamais écrit
    seul — la publication (`bulk_create` + statut READY) est atomique — donc des
    fragments présents sont toujours un index COMPLET issu d'une publication réussie.

    ``status`` : statut du document tel que l'appelant l'a lu avant de commencer.
    DIAGNOSTIC SEULEMENT (journal, tests) : il dépend du point d'entrée — le worker
    pose INGESTING avant de relire le document — et ne dit donc rien de fiable sur
    l'histoire du document. Il ne participe pas à la décision (INGEST-1C-R2).
    """

    exists: bool
    status: str

    @property
    def restore_ready(self) -> bool:
        """Un échec de CETTE tentative aurait-il rendu le document à READY, vu
        l'état AU DÉPART ? DIAGNOSTIC SEULEMENT (INGEST-1F1-R1) : la décision
        terminale relit l'état COURANT de l'index publié sous verrou
        (``settle_terminal_failure``), car une tentative concurrente peut avoir
        publié — ou retiré — un index pendant la préparation. Le statut du document
        exprime la DISPONIBILITÉ du dernier index publié, pas le résultat de la
        dernière tentative (INGEST-1C-R2).
        """
        return self.exists


def previous_index_state(doc) -> PreviousIndex:
    """Relève l'état antérieur du document — à appeler AVANT toute écriture."""
    return PreviousIndex(
        exists=Chunk.objects.filter(document_id=doc.id).exists(),
        status=str(getattr(doc, "status", "") or ""),
    )


def settle_terminal_failure(document_id, *, message: str, failure_meta_patch: dict | None = None) -> str | None:
    """Décision TERMINALE de disponibilité après l'échec d'une tentative (INGEST-1F1-R1).

    La disponibilité finale est dérivée de l'état COURANT de l'index publié, jamais
    de l'instantané pris au départ de la tentative : depuis INGEST-1F1 la préparation
    est concurrente, et une autre tentative peut avoir publié un index valide (ou un
    autre acteur l'avoir retiré) entre le départ et l'échec.

    Transaction COURTE, document seul (jamais l'espace : aucun cycle possible avec
    la publication, qui verrouille espace PUIS document) :
      verrou de la ligne document → existence courante des fragments →
      READY si un index publié existe, sinon FAILED → ``error_message`` → COMMIT.
    ``failure_meta_patch`` (diagnostic d'échec, ``meta.extraction``) n'est écrit que
    si la décision est FAILED, sous le même verrou (INGEST-1F1-R2) : il ne peut donc
    jamais écraser les métadonnées d'un candidat publié entre-temps.
    Ne touche à aucun fragment. Rend le statut écrit, ou ``None`` si le document
    n'existe plus (rien n'est recréé, rien n'est écrit).
    """
    msg = (message or "")[:1000]
    with transaction.atomic():
        locked = Document.objects.select_for_update().filter(id=document_id).first()
        if locked is None:
            return None
        index_exists = Chunk.objects.filter(document_id=locked.id).exists()
        fields = {"status": DocumentStatus.READY, "error_message": msg}
        if not index_exists:
            fields["status"] = DocumentStatus.FAILED
            if failure_meta_patch:
                fields["meta"] = _merged_meta(locked.meta, failure_meta_patch)
        Document.objects.filter(id=locked.id).update(**fields)
    return fields["status"]


def _settle_failed_attempt(doc, previous: PreviousIndex, *, message: str, meta_patch: dict) -> None:
    """Statut FINAL après l'échec d'une tentative, sans toucher aux fragments.

    - Un index publié existe MAINTENANT (index antérieur conservé, ou publié par une
      tentative concurrente) : le document est READY — son dernier index reste
      l'index actif (et donc éligible au retrieval). L'échec de la tentative est
      consigné dans ``error_message`` (affiché seulement sur un document FAILED) ;
      ``meta.extraction`` n'est PAS réécrit : il décrit l'index actif, pas le
      candidat refusé.
    - Sinon (aucun index publié) : FAILED + code d'erreur structuré en
      ``meta.extraction``, comme avant.
    - Document disparu : rien n'est écrit ni recréé.
    ``previous`` ne sert qu'au diagnostic (INGEST-1F1-R1).
    """
    msg = (message or "")[:1000]
    final = settle_terminal_failure(doc.id, message=msg, failure_meta_patch=meta_patch)
    if final is None:
        logger.warning("index_extraction failed, document vanished before settlement (doc=%s)", doc.id)
        return
    if final == DocumentStatus.READY:
        logger.warning(
            "index_extraction failed, published index kept and document READY "
            "(doc=%s, index_at_start=%s): %s", doc.id, previous.exists, msg[:160],
        )


def _has_indexable_content(result) -> bool:
    """Évidence indexable COMPLÈTE du candidat.

    Texte global non blanc, OU unité structurée non blanche (1A), OU unité visuelle non
    blanche, OU fragment tabulaire porteur d'une valeur lue (``table_is_evidence``).
    Un candidat fait seulement de tableaux, ou seulement d'évidence visuelle, est donc
    indexable ; un résultat sans rien ne l'est jamais.

    Tolérant à un résultat partiel (attributs absents).
    """
    from .extraction.base import table_is_evidence

    if (getattr(result, "text", None) or "").strip():
        return True
    for rail in ("units", "visual_units"):
        for unit in getattr(result, rail, None) or []:
            raw = unit.get("text") if isinstance(unit, dict) else getattr(unit, "text", None)
            if (raw or "").strip():
                return True
    return any(table_is_evidence(t) for t in (getattr(result, "tables", None) or []))


def _extraction_meta(result: ExtractionResult) -> dict:
    """Métadonnées NON sensibles d'extraction destinées à Document.meta."""
    md = dict(result.metadata or {})
    md["extraction_status"] = result.status.value
    md["method"] = result.method
    md["detected_mime"] = result.detected_mime
    if result.warnings:
        md["warnings"] = list(result.warnings)
    # On ne stocke JAMAIS le texte extrait dans Document.meta (seulement des
    # métadonnées techniques ; front_matter est déjà borné par l'extracteur).
    return {"extraction": md}


def _annotate_specs(specs, *, page_text: str, source_is_ocr: bool):
    """Attache à chaque ChunkSpec d'une page ses entités métier (P2).

    Les entités sont calculées UNE fois sur le texte de page complet (le type de
    page — plan/nomenclature/cartouche — est une propriété de page), puis copiées
    dans `spec.meta`. ``source_is_ocr`` => ``requires_visual_check`` (valeurs à
    vérifier sur le plan source)."""
    from apps.rag_core.services.ingest.entities import extract_plan_entities, has_business_entities

    ent = extract_plan_entities(page_text)
    has_biz = has_business_entities(ent)
    for s in specs:
        s.meta.update({
            "page_type": ent["page_type"],
            "entities": ent,
            "source_is_ocr": bool(source_is_ocr),
            # plan/nomenclature issus d'OCR : valeurs techniques à vérifier visuellement.
            "requires_visual_check": bool(source_is_ocr and (has_biz or ent["page_type"] == "nomenclature")),
        })
    return ent


def _build_chunks(*, doc, space, result: ExtractionResult, filename: str,
                  chunk_size: int, overlap: int):
    """Découpe le résultat en ChunkSpec. Préserve la pagination si `pages` fourni.

    Enrichit chaque chunk d'entités métier + type de page (P2). Renvoie
    ``(specs, page_entities)`` où ``page_entities`` liste les entités par page
    (pour l'agrégation document-level)."""
    ocr_pages = set(result.metadata.get("ocr_pages") or [])
    unverified_table_pages = set(result.metadata.get("generic_table_unverified_pages") or [])
    page_entities = []

    if result.pages:
        all_specs = []
        global_offset = 0
        next_idx = 0
        for page_number, page_text in result.pages:
            page_raw = page_text or ""
            specs = chunk_text_deterministic(
                page_raw,
                source=filename,
                chunk_size=chunk_size,
                overlap=overlap,
                page_number=page_number,
                base_char_offset=global_offset,
                start_chunk_index=next_idx,
            )
            ent = _annotate_specs(specs, page_text=page_raw, source_is_ocr=(page_number in ocr_pages))
            if page_number in unverified_table_pages:
                # Tableau détecté mais non structuré sur cette page (rail générique) : son texte
                # aplati n'est jamais une preuve chiffrée (numeric_evidence).
                for spec in specs:
                    spec.meta["native_table_unverified"] = True
            _attacher_geometrie(specs, result, page_number)
            page_entities.append(ent)
            all_specs.extend(specs)
            if specs:
                next_idx = specs[-1].chunk_index + 1
            global_offset += len(page_raw) + 1
        return all_specs, page_entities

    specs = chunk_text_deterministic(
        result.text,
        source=filename,
        chunk_size=chunk_size,
        overlap=overlap,
        page_number=None,
        base_char_offset=0,
        start_chunk_index=0,
    )
    ent = _annotate_specs(specs, page_text=result.text or "", source_is_ocr=bool(result.metadata.get("ocr_used")))
    page_entities.append(ent)
    return specs, page_entities


def _attacher_geometrie(specs, result: ExtractionResult, page_number) -> None:
    """Rattache aux chunks la géométrie des blocs, quand un producteur en fournit.

    Jusqu'ici `_build_chunks` ne lisait que `result.pages` et `result.tables` : la
    géométrie que peut porter un bloc était donc jetée au découpage, avant même
    d'atteindre la base. Elle voyage désormais dans `ChunkSpec.meta`, qui est fusionné
    dans `Chunk.meta` — un `JSONField` libre. Aucune migration.

    Un chunk couvre souvent plusieurs blocs. On n'en dérive AUCUNE boîte englobante :
    réduire plusieurs régions distinctes à un seul rectangle donnerait une géométrie
    fausse, et fausse avec l'apparence de la précision. On liste les blocs sources, avec
    la boîte de chacun.
    """
    blocs = [b for b in (getattr(result, "blocks", None) or [])
             if getattr(b, "bbox", None) is not None
             and getattr(b, "page_number", None) == page_number]
    if not blocs:
        return
    for spec in specs:
        texte = spec.text or ""
        sources = [
            {
                "bbox": list(b.bbox),
                "page_number": b.page_number,
                "reading_order": getattr(b, "reading_order", None),
                "source_engine": getattr(b, "source_engine", None),
                "block_type": getattr(b.type, "value", str(b.type)),
            }
            for b in blocs
            if (b.text or "").strip() and (b.text or "").strip() in texte
        ]
        if sources:
            spec.meta["source_blocks"] = sources
            spec.meta["bbox_space"] = "mediabox"
            spec.meta["bbox_origin"] = "top-left"
            spec.meta["bbox_normalized"] = True


def _next_chunk_index(specs) -> int:
    """Premier ``chunk_index`` libre après ``specs`` (0 si aucun)."""
    return specs[-1].chunk_index + 1 if specs else 0


def _hard_embedding_limit(space) -> int | None:
    """Plafond DUR du modèle d'embeddings de l'espace, ou `None` s'il n'est pas gouverné.

    C'est la seule borne dont le dépassement DÉTRUIT un document : l'AI Gateway
    refuse le lot entier, et les chunks valides partent avec. Elle est distincte
    de la granularité produit (`RAG_CHUNK_SIZE_CHARS`), qui n'est qu'une
    préférence de taille — la dépasser ne casse rien.
    """
    try:
        from apps.llm_router.capabilities import governed_model_max_input_chars
        from apps.llm_router.embeddings import resolve_space_embedding, space_is_governed

        if space is None or not space_is_governed(space):
            return None
        _provider, model = resolve_space_embedding(space)
        return governed_model_max_input_chars(model or "")
    except Exception:  # noqa: BLE001 - une garde illisible ne doit pas casser l'ingestion
        return None


def _enforce_chunk_budget(specs, *, budget: int, doc_id=None):
    """Aucun chunk ne part au-dessus de ce que le modèle d'embeddings ACCEPTE.

    Le découpage est responsable de la granularité ; ce filet ne traite qu'un seul
    risque, celui qui coûte un document entier : un chunk plus long que le plafond
    du modèle gouverné fait refuser le LOT par l'AI Gateway, et tous les chunks
    valides tombent avec lui.

    La borne est donc le plafond DUR du modèle, jamais `RAG_CHUNK_SIZE_CHARS`.
    La première version confondait les deux, et cela s'est vu : l'extracteur Excel
    fenêtre lui-même ses chunks, jusqu'à 3 819 caractères — au-dessus de la
    granularité de 2 500, bien en dessous des 6 000 acceptés. Les écrêter a fait
    perdre 8 517 caractères à trois classeurs qui n'avaient aucun problème. Une
    garde ne doit pas abîmer ce qu'elle est censée protéger.

    Écrêter reste un pis-aller, et il est JOURNALISÉ : un écrêtage signale un
    découpage à corriger en amont, pas un réglage à accepter.
    """
    import dataclasses

    if not budget or int(budget) <= 0:
        return list(specs)
    budget = int(budget)
    corriges, touches = [], 0
    for s in specs:
        texte = s.text or ""
        if len(texte) <= budget:
            corriges.append(s)
            continue
        touches += 1
        # `ChunkSpec` est FROZEN : on reconstruit au lieu de muter. Le dict `meta`
        # est recopié plutôt que partagé, faute de quoi l'annotation d'écrêtage
        # remonterait dans l'objet d'origine.
        coupe = texte[:budget]
        meta = dict(s.meta or {})
        meta["chunk_budget_enforced"] = True
        meta["chunk_original_chars"] = len(texte)
        corriges.append(dataclasses.replace(
            s, text=coupe, char_end=(s.char_start or 0) + len(coupe), meta=meta))
    if touches:
        logger.warning(
            "ingest.chunk_budget_enforced des chunks depassaient le budget de l'espace "
            "et ont ete ecretes avant embeddings — decoupage a corriger en amont",
            extra={"document_id": str(doc_id) if doc_id else None,
                   "chunks_ecretes": touches, "budget": budget},
        )
    return corriges


def _bounded_tables(tables, *, atomic_max: int, row_chunk_max: int):
    """Applique à chaque famille de tableaux la borne qui la concerne."""
    atomic, generic = [], []
    for t in tables:
        ttype = (t.get("meta") or {}).get("table_type") or ""
        (generic if str(ttype).startswith("generic") else atomic).append(t)
    return atomic[:atomic_max] + generic[:row_chunk_max]


def _build_table_specs(*, result: ExtractionResult, filename: str, start_chunk_index: int,
                       chunk_size: int):
    """Construit les ChunkSpec d'un tableau structuré (P3-a2).

    Le Markdown du tableau est le texte du chunk (embeddé + cité) ; le JSON
    complet va dans `spec.meta["table"]`.

    Un tableau qui tient dans le budget reste UN chunk, au caractère près. Au-delà,
    il est fragmenté par lignes logiques avec répétition de l'en-tête, au lieu
    d'être tronqué. La troncature perdait la fin du tableau en silence, et le chunk
    qui en sortait pouvait dépasser ce que le modèle d'embeddings gouverné accepte
    — auquel cas c'est le DOCUMENT ENTIER qui échouait, pas le seul tableau.

    `chunk_size` est le budget de l'espace, déjà écrêté au plafond du modèle
    gouverné par `resolve_chunk_budget`. Il est reçu, jamais recalculé ici : deux
    endroits qui décident du même plafond finiraient par diverger.
    """
    from apps.rag_core.services.chunking import ChunkSpec
    from apps.rag_core.services.ingest.extraction import config as _cfg
    from apps.rag_core.services.ingest.extraction.table_chunking import (
        fragmenter_tableau_markdown,
    )

    tables = getattr(result, "tables", None) or []
    if not tables:
        return []

    max_chars = _cfg.table_max_chars()
    budget = max(1, int(chunk_size))
    max_fragments = _cfg.table_max_fragments()
    # Deux bornes, parce que ce sont deux objets différents : un tableau ATOMIQUE
    # (rail spécialisé) est un chunk entier, alors qu'un tableau GÉNÉRIQUE émet un
    # fragment par ligne logique et par page. Appliquer la borne atomique aux
    # fragments tronquerait un tableau de plusieurs centaines de pages dès sa
    # cinquantième ligne, en silence.
    tables = _bounded_tables(tables, atomic_max=_cfg.table_max_per_doc(),
                             row_chunk_max=_cfg.table_max_row_chunks())
    out = []
    idx = int(start_chunk_index)
    for rang_table, t in enumerate(tables):
        md = (t.get("markdown") or "").strip()
        if not md:
            continue
        if len(md) > max_chars:
            # Borne de VOLUME, pas d'embeddings. Elle s'applique avant découpage et
            # reste signalée dans la meta du fragment : un tableau écourté ne doit
            # pas se lire comme un tableau complet.
            md = md[:max_chars]
            volume_borne = True
        else:
            volume_borne = False

        frag = fragmenter_tableau_markdown(md, budget=budget, max_fragments=max_fragments)
        if not frag.fragments:
            continue

        tmeta = t.get("meta") or {}
        page = tmeta.get("page_number")
        # Identité partagée par les fragments d'un même tableau (§18) : elle vit
        # dans la meta JSON, aucune migration n'est nécessaire.
        table_id = f"{rang_table}:{page if page is not None else '-'}"
        total = len(frag.fragments)
        for i, texte in enumerate(frag.fragments):
            spec_meta = {
                "kind": "table",
                "table_type": tmeta.get("table_type"),
                "table": tmeta,
                "page_number": page,
                "confidence": tmeta.get("confidence"),
                "requires_visual_check": bool(tmeta.get("requires_visual_check", True)),
                # Provenance et géométrie : héritées telles quelles du tableau. On
                # n'invente pas de bbox par fragment — MinerU donne celle du tableau
                # entier, et une bbox plus précise serait une précision fabriquée.
                "table_id": table_id,
                "table_fragment_index": i,
                "table_fragment_count": total,
            }
            if total > 1:
                spec_meta["table_header_repeated"] = bool(frag.entete_lignes)
            if volume_borne or frag.tronque:
                spec_meta["table_truncated"] = True
                if frag.raison:
                    spec_meta["table_truncation_reason"] = frag.raison
                elif volume_borne:
                    spec_meta["table_truncation_reason"] = "table_max_chars"
            if tmeta.get("visual"):
                spec_meta["visual"] = tmeta["visual"]
            if tmeta.get("locator"):
                spec_meta["locator"] = tmeta["locator"]
            out.append(ChunkSpec(
                chunk_index=idx,
                text=texte,
                page_number=page,
                char_start=0,
                char_end=len(texte),
                source=filename,
                meta=spec_meta,
            ))
            idx += 1
    return out


def _build_excel_specs(*, result: ExtractionResult, filename: str):
    """ChunkSpec pour un classeur Excel — voie DÉDIÉE, distincte de `_build_table_specs`.

    Pourquoi ne pas réutiliser le rail des tableaux P3 (PDF/OCR) :

    - il **tronque** au lieu de fenêtrer (`md[:max_chars]`) : un classeur de 4 000
      lignes y perdrait tout ce qui dépasse. L'extracteur Excel a déjà fenêtré ;
    - sa borne est `DOCUMENT_TABLE_MAX_PER_DOC = 50` : une seule feuille dépasse ;
    - il pose `requires_visual_check=True` par défaut — sémantique « valeur issue d'un
      OCR de plan, à vérifier sur la source ». Une cellule Excel est **exacte** : la
      marquer ainsi dégraderait le prompt et brouillerait un signal qui a un sens précis.

    `page_number` reste **None** : une feuille n'est pas une page. Le libellé de source
    afficherait « p. 3 » et le viewer tenterait d'ouvrir la page 3 d'un fichier qui n'en
    a pas. La feuille et la plage vivent dans `Chunk.meta` (et remontent telles quelles
    dans `source.meta`).
    """
    from apps.rag_core.services.chunking import ChunkSpec

    chunks = getattr(result, "excel_chunks", None) or []
    specs = []
    offset = 0
    for idx, ch in enumerate(chunks):
        text = (ch.get("text") or "").strip()
        if not text:
            continue
        specs.append(ChunkSpec(
            chunk_index=idx,
            text=text,
            page_number=None,
            char_start=offset,
            char_end=offset + len(text),
            source=filename,
            meta=dict(ch.get("meta") or {}),
        ))
        offset += len(text) + 1
    return specs


def _build_slide_specs(*, result: ExtractionResult, filename: str, chunk_size: int, overlap: int):
    """ChunkSpec pour une présentation PPTX — voie DÉDIÉE, distincte d'Excel et des pages.

    **Une diapositive est l'unité logique primaire.** Le chunker texte générique est
    appliqué À L'INTÉRIEUR de chaque diapositive, jamais à cheval :

    - une diapositive qui tient dans la limite produit UN chunk ;
    - une diapositive trop longue est découpée avec l'overlap commun ;
    - deux diapositives ne sont JAMAIS fusionnées, même très courtes — la fusion ferait
      porter à un chunk deux numéros de diapositive, donc une provenance ambiguë. Une
      citation fausse coûte plus cher que le surcoût d'un chunk court.

    Différence avec `_build_excel_specs` : les unités Excel sont déjà fenêtrées par
    l'extracteur (repasser le chunker couperait un tableau Markdown en deux) ; une
    diapositive, elle, est du texte libre — on réutilise donc la politique de taille et
    d'overlap commune plutôt que d'en réinventer une.

    Différence avec `_build_chunks` (rail `pages`) : `page_number` reste **None**. Une
    diapositive n'est pas une page — même doctrine qu'une feuille de classeur. Le renseigner
    ferait afficher « p. 4 » et fabriquerait un lien viewer `?page=4` vers un fichier qui
    n'a pas de pages. Le numéro vit dans `meta["slide_number"]`, et remonte tel quel dans
    `source.meta`.
    """
    from apps.rag_core.services.chunking import chunk_text_deterministic

    units = getattr(result, "slides", None) or []
    all_specs = []
    global_offset = 0
    next_idx = 0

    for unit in units:
        text = (unit.get("text") or "").strip()
        if not text:
            continue
        unit_meta = dict(unit.get("meta") or {})
        specs = chunk_text_deterministic(
            text,
            source=filename,
            chunk_size=chunk_size,
            overlap=overlap,
            page_number=None,
            base_char_offset=global_offset,
            start_chunk_index=next_idx,
        )
        for spec in specs:
            spec.meta.update(unit_meta)
        all_specs.extend(specs)
        if specs:
            next_idx = specs[-1].chunk_index + 1
        global_offset += len(text) + 1

    return all_specs


def _build_unit_specs(*, result: ExtractionResult, filename: str, chunk_size: int, overlap: int):
    """ChunkSpec pour des unités structurées GÉNÉRIQUES (``result.units``)
    — voie calquée sur celle des diapositives, sans format.

    **Une unité est l'unité logique primaire.** Le chunker commun s'applique À
    L'INTÉRIEUR de chaque unité (une unité longue est fenêtrée avec la politique de
    taille/overlap commune), jamais à cheval : deux unités ne sont jamais fusionnées,
    sinon un chunk porterait deux repères, donc une provenance ambiguë.

    - ``page_number`` reste **None** : une unité n'est pas une page ;
    - ``unit.meta`` — dont ``meta["locator"]`` — est recopiée (copie PROFONDE) sur
      chaque chunk dérivé : la méta d'un chunk ne partage rien avec celle d'un autre ;
    - ordre déterministe : ordre des unités, puis ordre des fenêtres ; ``chunk_index``
      contigu à partir de 0 ;
    - une unité vide/blanche ne produit AUCUN chunk (et ne consomme aucun index).

    Aucune connaissance de format ici : ni ``if format == "xml"``, ni clé imposée.
    """
    import copy

    from apps.rag_core.services.chunking import chunk_text_deterministic

    units = getattr(result, "units", None) or []
    all_specs = []
    global_offset = 0
    next_idx = 0
    for unit in units:
        raw = unit.get("text") if isinstance(unit, dict) else getattr(unit, "text", None)
        text = (raw or "").strip()
        if not text:
            continue
        raw_meta = unit.get("meta") if isinstance(unit, dict) else getattr(unit, "meta", None)
        unit_meta = raw_meta if isinstance(raw_meta, dict) else {}
        specs = chunk_text_deterministic(
            text,
            source=filename,
            chunk_size=chunk_size,
            overlap=overlap,
            page_number=None,
            base_char_offset=global_offset,
            start_chunk_index=next_idx,
        )
        for spec in specs:
            spec.meta.update(copy.deepcopy(unit_meta))
        all_specs.extend(specs)
        if specs:
            next_idx = specs[-1].chunk_index + 1
        global_offset += len(text) + 1
    return all_specs


def _build_visual_unit_specs(*, result: ExtractionResult, filename: str, chunk_size: int,
                             overlap: int, start_chunk_index: int):
    """ChunkSpec des unités VISUELLES (``result.visual_units``).

    Rail ADDITIF : mêmes règles que ``_build_unit_specs`` — chunker commun À L'INTÉRIEUR
    de chaque unité, jamais à cheval, méta recopiée en profondeur, unité blanche sans
    chunk ni index consommé — mais la numérotation CONTINUE celle des rails précédents :
    ces unités s'ajoutent au texte et aux tableaux, sans les remplacer.
    Liste vide : aucun fragment, aucune métadonnée.

    ``page_number`` suit la PROVENANCE de l'unité : la page du visuel
    quand la source en porte une (PDF), ``None`` sinon (image autonome — elle n'a pas de
    pagination propre, et son texte OCR occupe déjà la page 1). Sans cela, la citation
    d'un visuel de la page 7 d'un PDF renverrait le lecteur vers un document sans page.
    """
    import copy

    from apps.rag_core.services.chunking import chunk_text_deterministic

    all_specs = []
    global_offset = 0
    next_idx = int(start_chunk_index)
    for unit in getattr(result, "visual_units", None) or []:
        raw = unit.get("text") if isinstance(unit, dict) else getattr(unit, "text", None)
        text = (raw or "").strip()
        if not text:
            continue
        raw_meta = unit.get("meta") if isinstance(unit, dict) else getattr(unit, "meta", None)
        unit_meta = raw_meta if isinstance(raw_meta, dict) else {}
        visual_meta = unit_meta.get("visual")
        raw_page = visual_meta.get("source_page") if isinstance(visual_meta, dict) else None
        page_number = (int(raw_page)
                       if isinstance(raw_page, int) and not isinstance(raw_page, bool)
                       and raw_page > 0 else None)
        specs = chunk_text_deterministic(
            text,
            source=filename,
            chunk_size=chunk_size,
            overlap=overlap,
            page_number=page_number,
            base_char_offset=global_offset,
            start_chunk_index=next_idx,
        )
        for spec in specs:
            spec.meta.update(copy.deepcopy(unit_meta))
        all_specs.extend(specs)
        if specs:
            next_idx = specs[-1].chunk_index + 1
        global_offset += len(text) + 1
    return all_specs


def _build_email_specs(*, result: ExtractionResult, filename: str, chunk_size: int, overlap: int):
    """ChunkSpec pour un message Outlook — voie DÉDIÉE, calquée sur celle des diapositives.

    **Une rubrique est l'unité logique primaire.** Le chunker générique s'applique À
    L'INTÉRIEUR de chaque rubrique, jamais à cheval : deux rubriques ne sont jamais
    fusionnées, pour la même raison qu'entre deux diapositives — une citation qui
    couvrirait « Charts » et « Government debt issuance » aurait une provenance fausse.

    La première unité est l'EN-TÊTE (objet, expéditeur, date) : elle rend le message
    trouvable par ses métadonnées sans avoir à les répéter partout ailleurs.

    Chaque chunk de corps est préfixé de l'objet et de la date. Ce préfixe est
    délibéré : le corpus de référence contient sept notes de marché au vocabulaire
    quasi identique (« Brent », « European opening », « $/bl ») dont seules la date et
    la nuance du titre diffèrent. Sans ce rappel, deux chunks issus de messages
    distincts sont vectoriellement presque indiscernables. On s'en tient à l'objet et à
    la date : la liste des destinataires n'entre JAMAIS dans le texte indexé.

    `page_number` reste **None** — un e-mail n'a pas de pages.
    """
    from apps.rag_core.services.chunking import chunk_text_deterministic

    units = getattr(result, "email_sections", None) or []
    md = result.metadata or {}
    subject = (md.get("subject") or "").strip()
    sent_at = (md.get("sent_at") or "")[:10]

    prefix_lines = []
    if subject:
        prefix_lines.append(f"Email : {subject}")
    if sent_at:
        prefix_lines.append(f"Date : {sent_at}")

    all_specs = []
    global_offset = 0
    next_idx = 0

    for unit in units:
        text = (unit.get("text") or "").strip()
        if not text:
            continue
        role = unit.get("role") or "body"
        title = unit.get("title")

        body_text = text
        if role != "header" and prefix_lines:
            head = list(prefix_lines)
            if title and title != text:
                head.append(f"Section : {title}")
            body_text = "\n".join(head) + "\n\n" + text

        specs = chunk_text_deterministic(
            body_text,
            source=filename,
            chunk_size=chunk_size,
            overlap=overlap,
            page_number=None,
            base_char_offset=global_offset,
            start_chunk_index=next_idx,
        )
        unit_meta = {
            "document_type": "email",
            "email_role": role,
            "email_section_index": unit.get("index"),
        }
        if title:
            unit_meta["email_section"] = title
        if subject:
            unit_meta["email_subject"] = subject
        if md.get("sent_at"):
            unit_meta["email_sent_at"] = md.get("sent_at")
        if md.get("sender_name") or md.get("sender_address"):
            unit_meta["email_sender"] = md.get("sender_name") or md.get("sender_address")
        for spec in specs:
            spec.meta.update(unit_meta)
        all_specs.extend(specs)
        if specs:
            next_idx = specs[-1].chunk_index + 1
        global_offset += len(body_text) + 1

    return all_specs


def index_extraction_result(
    *,
    doc: Document,
    space,
    result: ExtractionResult,
    filename: str,
    chunk_size: int,
    overlap: int,
    dim: int,
    allow_retry: bool = False,
    previous_index: PreviousIndex | None = None,
    visual_guard=None,
    deferred_visual=None,
    v2_auto=None,
) -> IngestResult:
    """Chunk + embed + persiste les Chunk pour un ExtractionResult réussi.

    Le document vide n'est jamais indexé : error_message='empty_document'.

    ``allow_retry`` (worker Celery uniquement) : si une erreur **transitoire**
    survient (provider embeddings indisponible, 429, 5xx, timeout, connexion
    reset/refused…), on relève ``RetryableIngestError`` au lieu de marquer FAILED,
    pour laisser la tâche Celery retry. En sync (``allow_retry=False``), on
    conserve le comportement historique : échec propre, jamais de 500.

    Réindexation NON destructive (INGEST-1C) : l'index existant est une donnée à
    protéger. Le candidat (fragments + vecteurs + identité) est construit ENTIER
    avant que l'ancien index ne soit touché ; les anciens fragments ne sont supprimés
    qu'en phase de publication, dans la même transaction que l'écriture des nouveaux.
    Tout échec avant ou pendant la publication laisse l'ancien index intact, et un
    document qui le servait revient à READY (cf. ``PreviousIndex``). Une PREMIÈRE
    ingestion échouée, sans index antérieur, reste FAILED sans fragment.

    Frontières transactionnelles (INGEST-1F1) :
      TX1 (courte)  : statut INGESTING, commité ;
      HORS TX       : chunking, filtrage, embeddings, identité de pipeline
                      (``_prepare_candidate`` — aucune écriture, aucun verrou) ;
      TX2 (courte)  : verrou espace + revalidation d'identité et de ``deleted_at``,
                      verrou document + existence, DELETE anciens, bulk_create,
                      READY (``_publish_candidate``). Aucun réseau ni chunking
                      pendant qu'un verrou de publication est tenu.

    ``previous_index`` : état relevé par l'appelant avant toute écriture ; relevé ici
    à défaut.

    ``v2_auto`` : périmètre Vision V2 AUTOMATIQUE décidé avant la
    publication (``orchestration.resolve_ingest_scope``) ; sa préparation durable est
    écrite dans TX2, avec READY (cf. ``_publish_candidate``).
    """
    extraction_meta = _extraction_meta(result)
    previous = previous_index if previous_index is not None else previous_index_state(doc)

    # Candidat vide => jamais publié ; un résultat vide n'autorise pas à détruire
    # l'index existant. Code STRUCTURÉ (pas seulement un message) : c'est ce que la
    # reprise adaptative consulte pour décider s'il y a mieux à faire (lot 3).
    if result.status in (ExtractionStatus.EMPTY,) or not _has_indexable_content(result):
        _settle_failed_attempt(
            doc, previous, message="empty_document",
            meta_patch=_with_error_code(extraction_meta, "empty_document"),
        )
        return IngestResult(document_id=str(doc.id), status=DocumentStatus.FAILED,
                            chunks_created=0, deduped=False)

    def _failed_attempt(e: Exception) -> IngestResult:
        """Règlement d'une tentative échouée (préparation OU publication).

        Rien du candidat n'a été écrit : la préparation n'écrit pas, et la
        publication est atomique (son rollback rend l'ancien index). Aucun
        nettoyage hors transaction — il détruirait l'index que le rollback vient
        de rendre.
        """
        # Erreur TRANSITOIRE (embeddings provider indisponible, 429, 5xx, timeout,
        # connexion reset/refused, network unreachable…) :
        # - en async (allow_retry) on relève RetryableIngestError pour que la tâche
        #   Celery retry ; le statut reste géré par la tâche (PENDING pendant le
        #   retry, FAILED seulement après épuisement) — l'ancien index reste en place
        #   entre deux tentatives ;
        # - en sync on dégrade proprement (pas de 500, pas de boucle).
        if is_retryable_error(e):
            # Message volontairement sobre : ne PAS exposer le corps/secret provider.
            safe_msg = f"transient_ingest_error: {e.__class__.__name__}"
            if allow_retry:
                logger.warning(
                    "index_extraction transient error (doc=%s cls=%s) -> celery retry",
                    doc.id, e.__class__.__name__,
                )
                raise RetryableIngestError(safe_msg) from e
            _settle_failed_attempt(
                doc, previous, message=safe_msg,
                meta_patch=_with_error_code(extraction_meta, "transient_ingest_error"),
            )
            logger.warning(
                "index_extraction transient error in sync mode (doc=%s): %s",
                doc.id, e.__class__.__name__,
            )
            return IngestResult(document_id=str(doc.id), status=DocumentStatus.FAILED,
                                chunks_created=0, deduped=False)

        # Erreur MÉTIER / définitive (no_valid_chunks, fichier corrompu, DB...) :
        # échec propre, jamais propagé : l'upload ne doit pas renvoyer un 500 pour
        # ces cas. `IngestError("no_valid_chunks")` et consorts portent leur code dans
        # le message ; les exceptions techniques n'en ont pas — `internal_error` dit
        # honnêtement « cause inconnue » plutôt que d'inventer un code plausible.
        _settle_failed_attempt(
            doc, previous, message=str(e),
            meta_patch=_with_error_code(extraction_meta, _code_of_exception(e)),
        )
        logger.warning("index_extraction_result failed (doc=%s): %s",
                       doc.id, e.__class__.__name__)
        return IngestResult(document_id=str(doc.id), status=DocumentStatus.FAILED,
                            chunks_created=0, deduped=False)

    # TX1 — statut INGESTING : écriture COURTE, commitée avant toute préparation
    # (INGEST-1F1). Idempotente avec celle des chemins reindex/worker ; elle rend
    # INGESTING visible aussi pour une première ingestion synchrone. Le verrou de
    # ligne implicite de cet UPDATE est relâché ICI — plus au terme des embeddings.
    # Interne à la tentative : l'horodatage d'une tentative déjà ouverte est conservé
    # (une ingestion synchrone, elle, commence ici).
    from .attempts import ingesting_update_fields

    Document.objects.filter(id=doc.id).update(
        **ingesting_update_fields(new_attempt=False), error_message="",
    )

    # PHASE 1 — PRÉPARATION DU CANDIDAT, HORS TRANSACTION (INGEST-1F1) : chunking,
    # filtrage, résolution provider/modèle, lots d'embeddings (réseau), identité de
    # pipeline. Aucune écriture, aucun verrou : l'ancien index reste publié et
    # visible, le document et l'espace restent libres pour tout autre acteur.
    try:
        candidate = _prepare_candidate(
            doc=doc, space=space, result=result, filename=filename,
            chunk_size=chunk_size, overlap=overlap, dim=dim, deferred_visual=deferred_visual,
        )
    except Exception as e:
        return _failed_attempt(e)

    # PHASE 2 — PUBLICATION : transaction COURTE (verrou espace + revalidation,
    # verrou document + existence, DELETE anciens, bulk_create, READY). Aucun
    # réseau, aucune extraction, aucun chunking à l'intérieur.
    try:
        publish_kwargs = {"visual_guard": visual_guard} if visual_guard is not None else {}
        if v2_auto is not None:
            publish_kwargs["v2_auto"] = v2_auto
        published = _publish_candidate(doc=doc, space=space, candidate=candidate, **publish_kwargs)
    except IngestError as e:
        if str(e) == "not_found":
            # Le document a été supprimé pendant la préparation : rien à régler
            # (aucune ligne), rien à recréer — contrat `not_found` existant
            # (`reindex_existing_document`), terminal et non retryable.
            logger.warning("index_extraction_result: document vanished before publication (doc=%s)", doc.id)
            raise
        return _failed_attempt(e)
    except Exception as e:
        return _failed_attempt(e)

    # Les métadonnées du candidat (meta.extraction, entités, tableaux) ont été
    # publiées DANS la même transaction que ses fragments (INGEST-1F1-R2) : aucune
    # écriture post-commit, donc aucune fusion tardive possible sur un index plus récent.
    return IngestResult(document_id=str(doc.id), status=DocumentStatus.READY,
                        chunks_created=published, deduped=False)


@dataclass(frozen=True)
class _PreparedCandidate:
    """Candidat d'index COMPLET, calculé hors transaction (INGEST-1F1).

    Porte tout ce dont la publication a besoin, et l'identité d'embeddings de
    l'espace sous laquelle il a été préparé — celle que ``lock_ragspace_for_mutation``
    revalide sous verrou avant d'autoriser l'écriture.
    """

    specs: list
    vectors: list
    pipeline_id: str | None
    pipeline_family: str | None
    identity: tuple[str, str]
    page_entities: list
    #: ``{"extraction": {...}}`` — métadonnées SPÉCIFIQUES à ce candidat (statut,
    #: méthode, MIME, avertissements, entités agrégées, index des tableaux),
    #: calculées hors transaction et publiées avec ses fragments (INGEST-1F1-R2).
    success_meta: dict


def _candidate_success_meta(*, result: ExtractionResult, page_entities: list, doc_id) -> dict:
    """``{"extraction": {...}}`` du candidat : métadonnées d'extraction + agrégat des
    entités métier (P2) + index léger des tableaux (P3-a2, sans les lignes). Calcul
    pur mémoire, HORS transaction ; le résultat est publié dans TX2 (INGEST-1F1-R2)."""
    meta = _extraction_meta(result)
    ext = meta["extraction"]
    try:
        from apps.rag_core.services.ingest.entities import aggregate_page_entities

        agg = aggregate_page_entities(page_entities)
        if agg:
            ext.update(agg)
    except Exception:
        logger.debug("entity aggregation skipped (doc=%s)", doc_id)
    try:
        tables = getattr(result, "tables", None) or []
        if tables:
            ext["tables"] = [
                {
                    "table_type": (t.get("meta") or {}).get("table_type"),
                    "page_number": (t.get("meta") or {}).get("page_number"),
                    "confidence": (t.get("meta") or {}).get("confidence"),
                    "rows": len((t.get("meta") or {}).get("rows") or []),
                }
                for t in tables
            ]
    except Exception:
        logger.debug("tables index skipped (doc=%s)", doc_id)
    return meta


def _rail_specs(*, doc, space, result: ExtractionResult, filename: str, chunk_size: int, overlap: int):
    """Fragments d'UN résultat d'extraction selon son rail (Excel > email > diapositives >
    unités > texte + tableaux), puis les unités visuelles (rail ADDITIF). Renvoie
    ``(specs, page_entities)``. Pur : aucune écriture, aucun embedding."""
    # Chaque rail construit ses fragments sans décider seul qu'il n'y a rien : le contrôle
    # `no_valid_chunks` porte sur le candidat COMPLET, une fois que le texte, les
    # tableaux et les unités visuelles ont tous contribué. Un candidat
    # fait seulement de tableaux, ou seulement d'évidence visuelle, n'est plus refusé
    # avant d'avoir été assemblé. Sans évidence visuelle, le résultat est identique :
    # un rail vide lève toujours `no_valid_chunks`, plus bas.
    if getattr(result, "excel_chunks", None):
        # Excel : l'extracteur a DÉJÀ fenêtré (lignes + caractères, sans
        # chevauchement). On ne repasse pas le chunker texte dessus, sinon on
        # couperait des tableaux Markdown en plein milieu.
        specs = _build_excel_specs(result=result, filename=filename)
        page_entities = []
    elif getattr(result, "email_sections", None):
        # Courriel (.msg, .eml) : une rubrique = une unité logique, chunkée en interne.
        specs = _build_email_specs(
            result=result, filename=filename,
            chunk_size=chunk_size, overlap=overlap,
        )
        if (result.metadata or {}).get("attachment_ingestion") == "enabled":
            # Distinction CORPS / PIÈCE JOINTE pour le retrieval et les citations. Posée
            # seulement quand l'ingestion des pièces est ouverte : fermée, les fragments
            # d'un courriel restent identiques à ceux d'avant une version antérieure.
            for spec in specs:
                spec.meta.setdefault("email_part", "body")
        # CONTENU des pièces jointes, par les rails de leur propre format.
        specs.extend(_email_attachment_specs(
            doc=doc, space=space, result=result, chunk_size=chunk_size, overlap=overlap,
            start_chunk_index=_next_chunk_index(specs),
        ))
        page_entities = []
    elif getattr(result, "slides", None):
        # PPTX : une diapositive = une unité logique, chunkée en interne.
        specs = _build_slide_specs(
            result=result, filename=filename,
            chunk_size=chunk_size, overlap=overlap,
        )
        page_entities = []
    elif getattr(result, "units", None):
        # Unités structurées GÉNÉRIQUES : une unité = une unité
        # logique, chunkée en interne. PRIORITÉ : excel > email > slides > units > texte.
        # Quand des unités existent, `result.text` n'est JAMAIS chunké — il resterait
        # sinon indexé deux fois (le texte global et ses unités).
        specs = _build_unit_specs(
            result=result, filename=filename,
            chunk_size=chunk_size, overlap=overlap,
        )
        page_entities = []
    else:
        specs, page_entities = _build_chunks(
            doc=doc, space=space, result=result, filename=filename,
            chunk_size=chunk_size, overlap=overlap,
        )

        # P3-a2 : un tableau structuré donne un chunk s'il tient dans le budget,
        # et des fragments par lignes logiques sinon. Le budget est celui de
        # l'espace, déjà écrêté au plafond du modèle d'embeddings gouverné.
        specs.extend(_build_table_specs(result=result, filename=filename,
                                        start_chunk_index=_next_chunk_index(specs),
                                        chunk_size=chunk_size))

    # Unités VISUELLES : rail ADDITIF, jamais exclusif — elles
    # s'ajoutent à ce que les rails précédents ont produit. Liste vide : no-op.
    specs.extend(_build_visual_unit_specs(
        result=result, filename=filename, chunk_size=chunk_size, overlap=overlap,
        start_chunk_index=_next_chunk_index(specs),
    ))
    return specs, page_entities


def _email_attachment_specs(*, doc, space, result: ExtractionResult, chunk_size: int, overlap: int,
                            start_chunk_index: int) -> list:
    """Fragments des pièces jointes EXTRAITES d'un courriel.

    Chaque pièce passe par `_rail_specs` — mêmes rails, mêmes métadonnées (page PDF,
    diapositive, feuille et plage, OCR, tableaux) que le même fichier déposé seul. On y
    ajoute la PROVENANCE (index, chemin, nom, format, empreinte de la pièce, objet du
    courriel parent) et un court préfixe « Pièce jointe : … / Email : … » qui rend la
    relation interrogeable et distingue deux pièces au contenu voisin.

    Le statut de preuve numérique n'est JAMAIS rehaussé : les clés de provenance de
    l'extracteur (`source_is_ocr`, `kind`, `visual`…) sont conservées telles quelles.
    """
    from dataclasses import replace as _replace

    pairs = getattr(result, "email_attachment_results", None) or []
    if not pairs:
        return []
    md = result.metadata or {}
    entries = {e.get("index"): e for e in (md.get("attachments") or []) if isinstance(e, dict)}
    subject = (md.get("subject") or "").strip()
    # Date d'envoi du courriel PARENT : la citation d'une pièce jointe dit « pièce jointe de
    # l'email du … ».
    sent_at = md.get("sent_at")
    out = []
    nxt = start_chunk_index
    for index, sub in pairs:
        entry = entries.get(index) or {}
        name = entry.get("filename") or f"piece-jointe-{index}"
        sub_specs, _ = _rail_specs(doc=doc, space=space, result=sub, filename=name,
                                   chunk_size=chunk_size, overlap=overlap)
        head = f"Pièce jointe : {name}" + (f"\nEmail : {subject}" if subject else "")
        stamp = {
            "email_part": "attachment",
            "email_attachment_index": index,
            "email_attachment_path": entry.get("path"),
            "email_attachment_filename": name,
            "email_attachment_format": entry.get("detected_format"),
            "email_attachment_sha256": entry.get("sha256"),
            "email_nested_depth": entry.get("depth"),
        }
        for spec in sub_specs:
            meta = dict(spec.meta or {})
            nested_subject = meta.get("email_subject")
            if nested_subject and nested_subject != subject:
                meta["email_nested_subject"] = nested_subject
            meta.update(stamp)
            if subject:
                meta["email_subject"] = subject
            if sent_at:
                meta["email_sent_at"] = sent_at
            text = head + "\n\n" + (spec.text or "")
            out.append(_replace(spec, chunk_index=nxt, text=text, source=name,
                                char_end=spec.char_start + len(text), meta=meta))
            nxt += 1
    return out


def _finalize_email_attachments(result: ExtractionResult, specs: list) -> None:
    """Statut TERMINAL de chaque pièce extraite, d'après les fragments RÉELLEMENT retenus
    (après filtrage et écrêtage) : INDEXED / PARTIALLY_INDEXED, ou FAILED si aucun fragment
    n'a survécu. Une pièce n'est jamais dite recherchable sur la foi de son inventaire."""
    md = result.metadata or {}
    entries = md.get("attachments")
    if not getattr(result, "email_attachment_results", None) or not isinstance(entries, list):
        return
    from .extraction.email_extractor import (
        ATT_FAILED, ATT_INDEXED, ATT_PARTIALLY_INDEXED, ATT_PENDING, PARTIAL_STATUSES, SEARCHABLE_STATUSES,
    )

    counts: dict = {}
    for spec in specs:
        idx = (spec.meta or {}).get("email_attachment_index")
        if idx is not None:
            counts[idx] = counts.get(idx, 0) + 1
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("status") != ATT_PENDING:
            continue
        n = counts.get(entry.get("index"), 0)
        entry["fragment_count"] = n
        if n <= 0:
            entry.update(status=ATT_FAILED, reason="email_attachment_no_fragments")
        else:
            entry["status"] = ATT_PARTIALLY_INDEXED if entry.get("partial") else ATT_INDEXED
    md["attachments_ingested"] = sum(1 for e in entries if isinstance(e, dict) and e.get("status") in SEARCHABLE_STATUSES)
    md["email_status"] = "partial" if any(isinstance(e, dict) and e.get("status") in PARTIAL_STATUSES
                                          for e in entries) else "complete"


def _prepare_candidate(
    *, doc: Document, space, result: ExtractionResult, filename: str,
    chunk_size: int, overlap: int, dim: int, deferred_visual=None,
) -> _PreparedCandidate:
    """Chunking + embeddings + identité de pipeline + métadonnées du candidat —
    SANS transaction ni écriture.

    Toute exception laisse l'ancien index publié tel quel : rien n'a été touché.
    """
    # Chaque rail construit ses fragments sans décider seul qu'il n'y a rien : le contrôle
    # `no_valid_chunks` porte sur le candidat COMPLET. Le choix du rail
    # vit dans `_rail_specs`, réutilisé tel quel pour les pièces jointes d'un courriel
    # une pièce est découpée EXACTEMENT comme le même fichier déposé.
    specs, page_entities = _rail_specs(
        doc=doc, space=space, result=result, filename=filename,
        chunk_size=chunk_size, overlap=overlap,
    )
    # VISUAL-ENRICHMENT-DEFERRED : fragments visuels DÉJÀ publiés, reportés tels quels
    # (visuel toujours présent, même modèle logique) — ré-embarqués avec le candidat,
    # jamais ré-analysés. Une réindexation ne retire donc aucune évidence visuelle valide.
    if deferred_visual is not None:
        from .visual.deferred import carried_specs

        specs.extend(carried_specs(deferred_visual, start_chunk_index=_next_chunk_index(specs)))

    # Pré-filtre les chunks vides/blancs AVANT embeddings (jamais transmis
    # au provider ; préserve l'alignement spec -> vecteur). Évite qu'un seul
    # chunk blanc fasse échouer TOUT le document via la garde d'embed_texts.
    specs = filter_nonblank_specs(specs)
    if not specs:
        raise IngestError("no_valid_chunks")

    # DERNIÈRE BARRIÈRE avant embeddings. Le découpage est censé respecter le
    # budget ; cette garde existe parce qu'un chemin l'a déjà contourné une fois,
    # et que le prix de l'oubli est disproportionné : la passerelle refuse le LOT,
    # donc le document entier échoue pour un seul chunk hors borne. Écrêter ce
    # chunk-là coûte sa fin ; ne pas le faire coûte le document.
    # La borne est le plafond DUR du modèle d'embeddings, pas la granularité
    # produit : seul son dépassement détruit un document. Espace non gouverné
    # (plafond inconnu) : aucune coupe, le contrat historique est préservé.
    specs = _enforce_chunk_budget(specs, budget=_hard_embedding_limit(space),
                                  doc_id=getattr(doc, "id", None))
    # Courriel : statut terminal de chaque pièce d'après les fragments RETENUS, avant que
    # les métadonnées du candidat ne soient figées. No-op sinon.
    _finalize_email_attachments(result, specs)

    texts = [s.text for s in specs]
    # Embeddings PAR espace : provider/modèle lus sur le RagSpace (pas de
    # repli global silencieux qui écraserait la config de l'espace).
    emb_provider, emb_model = resolve_space_embedding(space)

    # 🔴 Le lot est BORNÉ. L'AI Gateway refuse au-delà de 64 textes par appel
    # (« batch trop grand (65 > 64) ») — et l'envoi se faisait en UN seul appel,
    # quel que soit le nombre de chunks. Tout document dépassant la borne
    # échouait donc ENTIÈREMENT, avec un `EmbeddingsError` opaque : pas un
    # chunk perdu, le document entier en `FAILED`.
    #
    # Trouvé en réindexant un classeur qui passait de 62 à 65 chunks. Trois
    # lignes de plus, et le document devenait inexploitable.
    batch = max(1, int(getattr(settings, "RAG_EMBED_BATCH_SIZE", 64)))
    # Politique d'egress de CET espace, résolue UNE fois hors de la boucle
    # de lots : un `SELECT rag_space` par lot serait un coût inutile sur le
    # chemin nominal, qui est local. Sans ce contexte, un espace configuré
    # sur un fournisseur externe enverrait ses documents hors infrastructure
    # sans qu'aucune politique ne soit consultée (P0 du lot
    # SPACE-AI-CONFIG-ROUTING).
    emb_ctx = LlmExecutionContext.for_rag_space(
        space, operation="embedding.document"
    )
    vectors = []
    # Identité du pipeline (lot 3) : `task=document`, puisqu'on encode des
    # passages et non une question. Elle est relevée sur le PREMIER lot et
    # vérifiée sur tous les suivants.
    doc_pipeline_id: str | None = None
    doc_pipeline_family: str | None = None
    for i in range(0, len(texts), batch):
        emb_batch = embed_texts_with_identity(
            texts[i:i + batch],
            dim=dim, provider=emb_provider, model=emb_model,
            task=TASK_DOCUMENT,
            rag_space_id=getattr(space, "id", None),
            document_id=getattr(doc, "id", None),
            context=emb_ctx,
        )
        if doc_pipeline_id is None:
            doc_pipeline_id = emb_batch.pipeline_id
            # Famille de comparabilité PORTÉE par le lot (champs attestés par la
            # Gateway, ou identité legacy) — jamais découpée de l'identifiant, qui est
            # opaque. Un lot gouverné sans famille ne peut pas être publié : ses
            # fragments seraient invisibles au retrieval gouverné, silencieusement.
            doc_pipeline_family = emb_batch.family or None
            if emb_batch.governed and not doc_pipeline_family:
                raise IngestError("pipeline_family_unresolved")
        elif emb_batch.pipeline_id != doc_pipeline_id or (emb_batch.family or None) != doc_pipeline_family:
            # Un document découpé en plusieurs lots peut chevaucher un
            # rechargement de modèle côté Gateway : les lots suivants
            # porteraient alors un AUTRE digest. Mélanger deux pipelines dans
            # un même document produirait un document dont une moitié est
            # comparable et l'autre non — invisible au retrieval, qui ne
            # verrait qu'un rappel dégradé. On refuse le document ENTIER ;
            # rien n'a été écrit : l'ancien index reste publié.
            raise IngestError("embedding_pipeline_changed_mid_document")
        vectors.extend(emb_batch.vectors)

    success_meta = _candidate_success_meta(result=result, page_entities=page_entities, doc_id=doc.id)
    if deferred_visual is not None:
        # État d'enrichissement publié dans la MÊME transaction que les fragments : un
        # document READY porte toujours l'état de sa génération d'enrichissement.
        from .visual.deferred import META_KEY as _VE_KEY

        success_meta[_VE_KEY] = dict(deferred_visual.state)
    return _PreparedCandidate(
        specs=specs, vectors=vectors,
        pipeline_id=doc_pipeline_id or None, pipeline_family=doc_pipeline_family,
        identity=(emb_provider or "", emb_model or ""),
        page_entities=page_entities,
        success_meta=success_meta,
    )


def prepare_visual_asset_candidate(*, doc: Document, space, units, tables, filename: str):
    """Candidat d'UN visuel pour l'enrichissement DIFFÉRÉ (VISUAL-ENRICHMENT-DEFERRED) :
    ses unités et fragments tabulaires seulement, par la préparation EXISTANTE (même
    budget de fragment, mêmes embeddings, même identité de pipeline). ``None`` si rien
    d'indexable (visuel inconnu, décoratif ou vide)."""
    mini = ExtractionResult(text="")
    mini.visual_units = list(units)
    mini.tables = list(tables)
    chunk_size, overlap = resolve_chunk_budget(space)
    try:
        return _prepare_candidate(doc=doc, space=space, result=mini, filename=filename,
                                  chunk_size=chunk_size, overlap=overlap,
                                  dim=int(getattr(settings, "RAG_EMBEDDING_DIM", 768)))
    except IngestError as exc:
        if str(exc) == "no_valid_chunks":
            return None
        raise


def _chunk_rows(locked_space, locked_doc, candidate: _PreparedCandidate, *, index_offset: int = 0) -> list:
    """Lignes ``Chunk`` d'un candidat préparé (publication primaire OU ajout visuel différé)."""
    chunks = []
    for spec, vec in zip(candidate.specs, candidate.vectors, strict=False):
        idx = spec.chunk_index + index_offset
        meta = {
            "chunk_index": idx,
            "page_number": spec.page_number,
            "source": spec.source,
            "char_start": spec.char_start,
            "char_end": spec.char_end,
        }
        # Entités métier + type de page (P2), si présentes.
        if spec.meta:
            meta.update(spec.meta)
            if index_offset:
                meta["chunk_index"] = idx
        chunks.append(
            Chunk(
                rag_space=locked_space,
                document=locked_doc,
                chunk_index=idx,
                text=spec.text,
                embedding=vec,
                page_number=spec.page_number,
                meta=meta,
                # Identité du pipeline qui a produit CE vecteur. Portée par le
                # chunk (et pas seulement par le document) parce que c'est le
                # chunk que la recherche vectorielle filtre.
                pipeline_id=candidate.pipeline_id,
                pipeline_family=candidate.pipeline_family,
            )
        )
    return chunks


def _publish_candidate(*, doc: Document, space, candidate: _PreparedCandidate, visual_guard=None,
                       v2_auto=None) -> int:
    """Transaction COURTE de publication (INGEST-1F1) — uniquement de la DB.

    Ordre de verrou canonique du domaine : RagSpace, puis Document.

    1. ``lock_ragspace_for_mutation`` : verrou + REVALIDATION de l'identité
       d'embeddings capturée pendant la préparation (`RagSpaceIdentityChanged`
       sinon, rien n'est écrit) ;
    2. espace non supprimé (``deleted_at``) — la garde de départ de
       ``reindex_existing_document`` serait sinon inopérante sur une préparation
       longue ; code existant ``rag_space_deleted`` ;
    3. ``SELECT … FOR UPDATE`` du document : disparu pendant la préparation ⇒
       ``not_found`` (rien n'est recréé) ;
    4. DELETE des anciens fragments + bulk_create du candidat + READY + métadonnées
       du candidat (``meta.extraction``, fusionnées clé par clé dans le ``meta``
       de la ligne verrouillée — INGEST-1F1-R2), dans la même transaction : un
       échec d'écriture rend l'ancien index ET ses métadonnées par rollback ;
    5. périmètre Vision V2 automatique : préparation durable
       (``orchestration.persist_auto_prepare``) dans CETTE transaction. READY visible ⇒
       préparation durable présente ; rollback ⇒ ni l'une ni l'autre. Seule une
       inscription ``on_commit`` vise le broker : l'envoi part après le commit, et un
       envoi perdu reste rattrapable par le balayeur. Aucun autre déclencheur
       automatique n'existe hors de cette transaction.

    Les instances ``space``/``doc`` chargées avant la préparation ne font pas
    autorité : ce sont les lignes verrouillées ici qui sont écrites.
    Le statut du document n'est PAS une précondition (reaper/retries peuvent
    légitimement l'avoir modifié) : la sûreté vient de la source immuable, de
    l'identité revalidée, de l'existence revalidée et de l'atomicité.
    """
    from apps.rag_core.services.ragspace_mutation import lock_ragspace_for_mutation

    with transaction.atomic():
        locked_space = lock_ragspace_for_mutation(space.id, expected_identity=candidate.identity)
        if getattr(locked_space, "deleted_at", None) is not None:
            raise IngestError("rag_space_deleted")
        locked_doc = Document.objects.select_for_update().filter(id=doc.id).first()
        if locked_doc is None:
            raise IngestError("not_found")
        # revalidation SOUS VERROU de l'évidence visuelle publiée.
        # Lecture DB seule — aucun réseau, aucune normalisation. Une publication
        # concurrente plus riche, que cette préparation n'a jamais évaluée, n'est pas
        # écrasée (`visual_reindex_degraded`, rien n'est écrit).
        if visual_guard is not None:
            visual_guard.check_locked(locked_doc.id)

        # PUBLICATION : le candidat est complet (fragments, vecteurs, identité
        # unique, espace et document revalidés). C'est ici, et seulement ici, que
        # l'ancien index est remplacé — suppression et insertion dans la même
        # transaction, donc un échec d'écriture rend l'ancien index par rollback.
        Chunk.objects.filter(document_id=locked_doc.id).delete()
        Chunk.objects.bulk_create(_chunk_rows(locked_space, locked_doc, candidate), batch_size=200)
        # Empreinte d'embeddings : snapshot du provider/modèle de l'espace
        # RÉELLEMENT utilisés pour indexer ce document (ligne verrouillée, égale à
        # l'identité préparée). Permet de détecter les incohérences après un
        # changement de config et de filtrer la recherche vectorielle.
        # reindex_required=False : le document vient d'être (ré)indexé avec la
        # config courante.
        Document.objects.filter(id=locked_doc.id).update(
            status=DocumentStatus.READY,
            error_message="",
            embedding_provider=(locked_space.embedding_provider or ""),
            embedding_model=(locked_space.embedding_model or ""),
            reindex_required=False,
            # Métadonnées DU candidat publié, dans la même transaction que ses
            # fragments : le dernier publieur gagne l'index ET ses métadonnées.
            meta=_merged_meta(locked_doc.meta, candidate.success_meta),
        )
        if v2_auto is not None:
            from apps.rag_core.services.raster import orchestration as _v2

            _v2.persist_auto_prepare(v2_auto)
    return len(candidate.specs)


def extract_and_index_bytes(
    *,
    doc: Document,
    space,
    file_bytes: bytes,
    filename: str,
    content_type: str,
    ocr_enabled: Optional[bool] = None,
    allow_retry: bool = False,
    extraction_timeout_seconds: Optional[int] = None,
    last_attempt: bool = False,
    drop_visual_evidence: bool = False,
    visual_primary: bool = False,
    access_context: Optional[dict] = None,
) -> IngestResult:
    """Extrait `file_bytes` via la couche d'extraction puis indexe.

    Mappe toute ExtractionError typée sur un Document FAILED avec un message clair
    et `meta.extraction_status` détaillé. Ne lève pas pour ces cas "métier".

    ``allow_retry`` est transmis à ``index_extraction_result`` : il n'affecte QUE
    les erreurs transitoires d'indexation/embeddings (relevées en
    ``RetryableIngestError`` pour le worker Celery). Les erreurs d'extraction
    (rejet/format/corrompu) restent des FAILED définitifs, jamais retryés.
    """
    # Budget de chunk EFFECTIF (lot 3) : réduit pour un espace GOUVERNÉ, dont le
    # modèle refuse les textes au-delà de sa fenêtre. Inchangé sinon.
    chunk_size, overlap = resolve_chunk_budget(space)
    dim = int(getattr(settings, "RAG_EMBEDDING_DIM", 768))

    # État antérieur relevé AVANT toute écriture (INGEST-1C) : c'est lui qui décide,
    # sur échec, si le document revient à READY (index antérieur conservé) ou FAILED.
    previous = previous_index_state(doc)

    # Rail visuel : natures de source actives. Vide (défaut) =>
    # appel d'extraction strictement identique à la ligne de base.
    from .extraction import config as _xcfg

    visual_sources = _xcfg.visual_analysis_sources()
    extract_kwargs = {"visual_sources": visual_sources} if visual_sources else {}
    if visual_sources:
        # Borne de visuels PDF propre aux espaces de l'extraction structurée (CAP-32) ;
        # None ailleurs : appel strictement identique à la ligne de base.
        from .visual.structured import pdf_visual_max_assets_for, pdf_visual_max_per_page_for

        _cap = pdf_visual_max_assets_for(getattr(space, "id", None))
        if _cap is not None:
            extract_kwargs["pdf_visual_max_assets"] = _cap
        _per_page = pdf_visual_max_per_page_for(getattr(space, "id", None))
        if _per_page is not None:
            extract_kwargs["pdf_visual_max_per_page"] = _per_page

    # Provenance (CV-3B) : la version des octets existe dès qu'ils sont stockés et
    # hachés, indépendamment du succès de l'extraction.
    version = _record_document_version(doc)

    # identité du RagSpace de CE document, transmise à l'extraction (processus enfant
    # compris) pour la décision du rail tabulaire générique. Jamais un état mémorisé par le worker.
    space_id = getattr(space, "id", None) or getattr(doc, "rag_space_id", None)
    try:
        result = extract_document(
            file_bytes or b"",
            filename=filename,
            content_type=content_type or "",
            ocr_enabled=ocr_enabled,
            timeout_seconds=extraction_timeout_seconds,
            rag_space_id=str(space_id) if space_id else None,
            **extract_kwargs,
        )
    except (RejectedFile, UnsupportedFormat) as e:
        # L'index existant n'est jamais touché par un échec d'extraction.
        _settle_failed_attempt(
            doc, previous, message=f"{e.code}: {e.detail}",
            meta_patch={"extraction": {"extraction_status": "rejected", "error_code": e.code}},
        )
        return IngestResult(document_id=str(doc.id), status=DocumentStatus.FAILED,
                            chunks_created=0, deduped=False)
    except ExtractionError as e:
        # Conserver les métadonnées page-level portées par l'erreur (P0) :
        # is_image_only_pdf, page_count, image_only_pages... pour l'UX (OCR requis,
        # pages reconnues) et un futur OCR à la demande. Jamais de contenu extrait.
        ext_meta = {"extraction_status": "failed", "error_code": e.code}
        extra = getattr(e, "meta", None)
        if isinstance(extra, dict):
            ext_meta.update(extra)
        _settle_failed_attempt(
            doc, previous, message=f"{e.code}: {e.detail}", meta_patch={"extraction": ext_meta},
        )
        return IngestResult(document_id=str(doc.id), status=DocumentStatus.FAILED,
                            chunks_created=0, deduped=False)

    _record_pdf_pages(doc, version, result, file_bytes)

    native_decision = (getattr(result, "metadata", None) or {}).get("native_tables_decision")
    if native_decision is not None:
        # Trace NON sensible (identifiants, booléen, raison bornée) : aucun contenu de document.
        logger.info("native_pdf_tables_decision document=%s ragspace=%s enabled=%s reason=%s",
                    doc.id, space_id, native_decision == "native_tables_enabled", native_decision)

    # Vision V2 orchestrée : drapeau éteint → `None` sans aucun travail.
    # DÉCISION (portes, politique `auto`, préflight) prise ICI, avant toute transaction ;
    # son EFFET (préparation durable) est écrit dans la transaction READY (CV-3E4C).
    from apps.rag_core.services.raster import orchestration as _v2

    v2_scope = _v2.resolve_ingest_scope(doc=doc, version=version,
                                        is_pdf=bool(getattr(result, "page_geometry", None)))

    # VISUAL-ENRICHMENT-DEFERRED : la vision ne retarde plus READY quand le document a
    # déjà une évidence non visuelle (décision déterministe, cf. visual/deferred.py).
    from .visual import deferred as _vdeferred

    mode = _vdeferred.decide_mode(result=result, visual_sources=visual_sources,
                                  visual_primary=visual_primary)
    request_id = (access_context or {}).get("request_id")
    if mode == _vdeferred.MODE_HANDOFF:
        # Seule évidence : les visuels. La vision est PRIMAIRE et doit précéder READY —
        # sur le worker visuel, pas ici. Rien n'est publié par cette passe.
        _vdeferred.enqueue_visual_primary(doc=doc, request_id=request_id, access_context=access_context)
        current = Document.objects.filter(id=doc.id).values_list("status", flat=True).first()
        return IngestResult(document_id=str(doc.id), status=current or DocumentStatus.PENDING,
                            chunks_created=0, deduped=False)
    if mode == _vdeferred.MODE_DEFERRED:
        from .utils import sha256_bytes

        plan = _vdeferred.plan_deferred(doc=doc, result=result, content_sha256=sha256_bytes(file_bytes or b""),
                                        drop_visual_evidence=drop_visual_evidence)
        if v2_scope is not None:
            plan = _vdeferred.supersede_for_v2(plan)      # un seul rail visuel par document
        v2_kwargs = {"v2_auto": v2_scope} if v2_scope is not None else {}
        res = index_extraction_result(
            doc=doc, space=space, result=result, filename=filename,
            chunk_size=chunk_size, overlap=overlap, dim=dim, allow_retry=allow_retry,
            previous_index=previous, deferred_visual=plan, **v2_kwargs,
        )
        if res.status == DocumentStatus.READY and plan.enrich:
            _vdeferred.enqueue_visual_enrichment(document_id=doc.id, generation=plan.generation,
                                                 request_id=request_id, access_context=access_context)
        return res

    visual_guard = _apply_visual_rail(
        doc=doc, previous=previous, result=result, visual_sources=visual_sources,
        retries_remain=bool(allow_retry and not last_attempt),
        drop_visual_evidence=drop_visual_evidence,
    )
    if isinstance(visual_guard, IngestResult):
        return visual_guard

    index_kwargs = {"visual_guard": visual_guard} if visual_guard is not None else {}
    if v2_scope is not None:
        index_kwargs["v2_auto"] = v2_scope
    return index_extraction_result(
        doc=doc, space=space, result=result, filename=filename,
        chunk_size=chunk_size, overlap=overlap, dim=dim, allow_retry=allow_retry,
        previous_index=previous, **index_kwargs,
    )


def _record_document_version(doc):
    """``DocumentVersion`` des octets courants (CV-3B), ou ``None``.

    La provenance ne fait jamais échouer une ingestion : un invariant brisé
    (``ProvenanceConflict``) ou une erreur base est journalisé avec un code stable et
    l'ingestion poursuit son contrat historique.
    """
    from apps.rag_core.services.documents.provenance import ProvenanceConflict, ensure_document_version

    try:
        return ensure_document_version(doc.id)
    except ProvenanceConflict as e:
        logger.error("document provenance: %s (doc=%s)", e.code, doc.id)
    except DatabaseError as e:
        logger.error("document provenance: document_version_failed %s (doc=%s)", type(e).__name__, doc.id)
    return None


def _record_pdf_pages(doc, version, result: ExtractionResult, file_bytes: bytes) -> None:
    """Matérialise les ``Page`` d'une version PDF depuis la géométrie de l'extraction.

    Seulement si la géométrie a été lue ET appartient bien aux octets de la version
    (même SHA-256) : sinon aucune page n'est écrite plutôt qu'une page d'autres octets.
    """
    geometry = getattr(result, "page_geometry", None)
    if version is None or not geometry:
        return
    from apps.rag_core.services.documents.provenance import ProvenanceConflict, ensure_pdf_pages
    from .utils import sha256_bytes

    if sha256_bytes(file_bytes or b"") != version.content_sha256:
        logger.error("document provenance: page_geometry_bytes_mismatch (doc=%s)", doc.id)
        return
    try:
        ensure_pdf_pages(version, geometry)
    except ProvenanceConflict as e:
        logger.error("document provenance: %s (doc=%s)", e.code, doc.id)
    except DatabaseError as e:
        logger.error("document provenance: page_write_failed %s (doc=%s)", type(e).__name__, doc.id)


def _apply_visual_rail(*, doc, previous: PreviousIndex, result: ExtractionResult, visual_sources,
                       retries_remain: bool, drop_visual_evidence: bool):
    """Rail visuel du candidat — PRÉPARATION, hors de toute transaction.

    Rend ``None`` (rien de visuel à garder), un ``VisualPublicationGuard`` à revalider
    dans TX2, ou un ``IngestResult`` FAILED déjà réglé par la décision terminale 1F
    (READY conservé si un index publié existe, FAILED sinon). Lève
    ``RetryableIngestError`` quand une défaillance relançable laisse encore une tentative.

    Le règlement terminal passe par un CODE stable comme message, jamais par le code
    d'erreur du Gateway : un ``visual_client_timeout`` ne doit pas ressusciter une relance
    par les marqueurs textuels.
    """
    source_kind = getattr(result, "visual_source_kind", "") or ""
    if not source_kind and not getattr(result, "visual_assets", None):
        return None

    from .visual import rail as visual_rail

    published = (visual_rail.published_visual_hashes(doc.id)
                 if previous.exists and source_kind else frozenset())
    rail_active = bool(source_kind) and source_kind in (visual_sources or ())

    def _reject(code: str) -> IngestResult:
        _settle_failed_attempt(doc, previous, message=code,
                               meta_patch=_with_error_code(_extraction_meta(result), code))
        return IngestResult(document_id=str(doc.id), status=DocumentStatus.FAILED,
                            chunks_created=0, deduped=False)

    # Fonctionnalité éteinte (ou source retirée de l'allowlist) : une réindexation ne
    # retire JAMAIS en silence une évidence visuelle publiée. Seul l'opt-in explicite
    # `drop_visual_evidence` le permet — `force` n'a aucun rapport.
    if published and not rail_active and not drop_visual_evidence:
        logger.warning("visual evidence kept: analysis disabled for this source (doc=%s)", doc.id)
        return _reject(visual_rail.CODE_DISABLED_WOULD_DEGRADE)

    if rail_active and result.visual_assets:
        decision = visual_rail.run_visual_rail(
            result, document_opaque_id=str(doc.id), retries_remain=retries_remain,
            published_hashes=published, model=getattr(settings, "AI_GATEWAY_VISION_MODEL", "") or None,
        )
        if decision.action == visual_rail.ACTION_FAIL:
            return _reject(decision.code)

    if not (rail_active or published) or (drop_visual_evidence and not rail_active):
        return None
    return visual_rail.VisualPublicationGuard(
        baseline=published, candidate=visual_rail.candidate_visual_hashes(result),
    )
