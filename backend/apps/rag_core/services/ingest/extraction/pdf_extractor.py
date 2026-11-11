# backend/apps/rag_core/services/ingest/extraction/pdf_extractor.py
from __future__ import annotations

"""
Extracteur PDF.

Stratégie OCR recommandée :
1. extraction du texte natif d'abord (réutilise l'existant, avec limites/timeout) ;
2. OCR UNIQUEMENT pour les pages sans texte (sous seuil caractères/page) et SI
   l'OCR est activé ;
3. on n'OCRise jamais inutilement un PDF déjà textuel.

Comportements :
- PDF scanné + OCR désactivé  -> erreur claire (document marqué non extractible) ;
- OCR activé mais indisponible -> partiel + warning (jamais de crash) ;
- OCR partiel / faible confiance -> métadonnées + warnings visibles pour l'audit.
"""

import io as _io
import logging
from typing import List, Tuple

from . import config
from . import mime as M
from .base import (
    Block,
    BlockType,
    ExtractionContext,
    ExtractionError,
    ExtractionResult,
    ExtractionStatus,
)
from .text_quality import (
    classify_text,
    extraction_quality_score,
    is_unusable_pdf_text,
    sanitize_text,
    usable_chars,
)

logger = logging.getLogger(__name__)


def _native_pages(data: bytes) -> Tuple[List[Tuple[int, str]], dict]:
    """Extrait le texte natif page par page et choisit le meilleur moteur local.

    Renvoie ``(pages, native_info)`` où ``native_info`` décrit le moteur retenu et
    les tentatives de fallback (pour la traçabilité dans Document.meta).

    Le moteur primaire reste ``pypdf`` (et porte seul les limites/timeouts
    applicatifs). Les moteurs de secours ne sont tentés que si pypdf est sous le
    seuil de texte utile, et uniquement s'ils sont installés (import-gardé).
    """
    from ..extractors import (
        extract_text_pages_from_pdf_bytes,
        select_best_native_pdf_extraction,
        useful_pdf_chars,
    )
    from ..limits import PdfExtractionTimeout, UploadLimitExceeded
    from .base import ExtractionTimeout, RejectedFile

    try:
        pypdf_pages = list(extract_text_pages_from_pdf_bytes(data))
    except UploadLimitExceeded as e:
        # harmonise vers une erreur typée d'extraction (message conservé)
        raise RejectedFile(getattr(e, "detail", str(e)), code=getattr(e, "code", "rejected_file"))
    except PdfExtractionTimeout as e:
        raise ExtractionTimeout(getattr(e, "detail", str(e)), code="pdf_extraction_timeout")

    fallback_enabled = config.pdf_native_fallback_enabled()
    info = {
        "pdf_native_engine": "pypdf",
        "pdf_native_fallback_enabled": fallback_enabled,
        "pdf_native_fallback_used": False,
        "pdf_native_fallback_attempted_engines": [],
        "pdf_native_chars_by_engine": {"pypdf": useful_pdf_chars(pypdf_pages)},
    }

    if not fallback_enabled:
        return pypdf_pages, info

    sel = select_best_native_pdf_extraction(
        data,
        pypdf_pages=pypdf_pages,
        engines=config.pdf_native_fallback_engines(),
        max_pages=config.pdf_max_pages(),
        min_total_chars=config.pdf_min_total_chars(),
    )
    info["pdf_native_engine"] = sel.engine
    info["pdf_native_fallback_used"] = sel.fallback_used
    info["pdf_native_fallback_attempted_engines"] = list(sel.attempted_engines)
    info["pdf_native_chars_by_engine"] = dict(sel.chars_by_engine)
    # Porte native (INGEST-NATIVE-CHAIN-02) : une chaîne écourtée doit être
    # DISTINGUABLE d'une chaîne qui a tout tenté sans rien trouver. Sans ce drapeau, les
    # deux cas produisent les mêmes compteurs et la décision devient invisible à l'audit.
    info["pdf_native_gate_applied"] = bool(getattr(sel, "gate_applied", False))
    if sel.fallback_used:
        info["native_fallback_used"] = True
        info["native_fallback_engine"] = sel.engine
        logger.info(
            "pdf native fallback used: engine=%s chars=%s (pypdf=%s)",
            sel.engine,
            sel.chars_by_engine.get(sel.engine),
            sel.chars_by_engine.get("pypdf"),
        )
    return sel.pages, info


def fonts_lack_unicode_mapping(data: bytes, *, max_pages: int = 12,
                               min_ratio: float = 0.5) -> bool:
    """Le PDF utilise-t-il majoritairement des polices Type 3 SANS table ToUnicode ?

    C'est un fait de structure, pas une heuristique de texte : sans ToUnicode, une police
    Type 3 n'associe ses glyphes à AUCUN caractère. Le fichier ne contient tout simplement
    pas le texte — ce qu'un extracteur en tire (`/1 /44`, `(cid:12)`, ou des lettres
    arbitraires) n'a aucun sens, quelle qu'en soit l'apparence.

    Ce signal est nettement plus fiable que l'analyse statistique du texte extrait : sur le
    corpus terrain, il isole les documents cassés (100 % de telles polices) des 69 PDF sains
    (médiane 0 %), là où les mesures textuelles se laissent tromper par un mojibake qui
    contient des voyelles. L'absence de ToUnicode SEULE ne suffit pas (55 à 80 % des PDF
    sains en manquent aussi) : c'est bien la conjonction avec Type 3 qui est décisive.
    """
    try:
        from pypdf import PdfReader
    except Exception:  # noqa: BLE001
        return False
    try:
        reader = PdfReader(_io.BytesIO(data))
        total = bad = 0
        for page in reader.pages[:max_pages]:
            fonts = ((page.get("/Resources") or {}).get("/Font") or {})
            try:
                fonts = fonts.get_object()
            except Exception:  # noqa: BLE001
                continue
            for key in list(fonts.keys()):
                try:
                    font = fonts[key].get_object()
                except Exception:  # noqa: BLE001
                    continue
                total += 1
                if str(font.get("/Subtype")) == "/Type3" and "/ToUnicode" not in font:
                    bad += 1
        return bool(total) and (bad / total) >= min_ratio
    except Exception:  # noqa: BLE001
        return False


def _glyph_only(page_map: dict) -> bool:
    """Toutes les pages porteuses de texte sont-elles des glyphes non mappables ?"""
    classes = [classify_text(t or "") for t in page_map.values() if (t or "").strip()]
    return bool(classes) and all(c in ("glyph_garbage", "cid_garbage") for c in classes)


def _page_is_empty(txt: str, threshold: int) -> bool:
    """Une page est « sans texte » si elle est trop courte OU inexploitable.

    Le second terme est celui qui manquait : un texte long mais inexploitable (glyphes,
    marqueurs cid) doit être traité comme une page vide — donc OCRisé.
    """
    return len((txt or "").strip()) < threshold or is_unusable_pdf_text(txt or "")


def _outcome_counts(report) -> dict:
    """Issue par candidat physique, agrégée — jamais un avertissement par ligne."""
    counts: dict = {}
    for o in report.outcomes:
        key = o.get("outcome", "unknown")
        counts[key] = counts.get(key, 0) + 1
    return counts


def _ocr_enabled(ctx: ExtractionContext) -> bool:
    if ctx.ocr_enabled is not None:
        return bool(ctx.ocr_enabled)
    return config.ocr_enabled()


class PdfExtractor:
    name = "pdf"

    def can_handle(self, *, detected_mime: str, ctx: ExtractionContext) -> bool:
        return detected_mime == M.PDF

    def extract(self, data: bytes, ctx: ExtractionContext) -> ExtractionResult:
        res = ExtractionResult()
        pages, native_info = _native_pages(data)
        # Géométrie déclarée des pages (CV-3B), lue APRÈS les limites de pages appliquées
        # par `_native_pages`. Transitoire : matérialisée en `Page` par le parent.
        from .pdf_geometry import read_pdf_page_geometry

        res.page_geometry = read_pdf_page_geometry(data)
        threshold = config.ocr_min_chars_per_page()

        # Le fichier ne PORTE pas de texte : ses polices n'associent leurs glyphes à aucun
        # caractère. Tout ce qu'un extracteur en tire est dénué de sens, même quand ça
        # ressemble à des lettres — donc aucune page native n'est digne de confiance.
        no_unicode_fonts = fonts_lack_unicode_mapping(data)
        native_info["pdf_fonts_lack_unicode_mapping"] = no_unicode_fonts

        # Pages sans texte natif exploitable.
        #
        # Le critère est l'EXPLOITABILITÉ, pas la longueur : une page dont la police n'a
        # pas de table ToUnicode rend des milliers de caractères de glyphes (`/1 /44 /42`)
        # et passait donc le seuil de longueur — l'OCR ne se déclenchait jamais et le
        # document était indexé mort. `_page_is_empty` combine les deux : trop court OU
        # inexploitable.
        empty_pages = [pn for (pn, txt) in pages
                       if no_unicode_fonts or _page_is_empty(txt, threshold)]
        text_pages = [pn for (pn, txt) in pages
                      if not (no_unicode_fonts or _page_is_empty(txt, threshold))]
        has_native = bool(text_pages)
        # Deux mesures, deux rôles : `native_text_chars` DIAGNOSTIQUE ce que le moteur a
        # produit (même si c'est du bruit), `native_usable_chars` DÉCIDE. Les confondre
        # ferait perdre l'information « le moteur a rendu 7 caractères » — utile pour
        # distinguer un PDF vide d'un PDF qu'on n'arrive pas à lire.
        native_text_chars = sum(len((txt or "").strip()) for (_, txt) in pages)
        native_usable_chars = (
            0 if no_unicode_fonts
            else sum(usable_chars(txt or "") for (_, txt) in pages)
        )

        page_map = {pn: ("" if no_unicode_fonts else (txt or "")) for (pn, txt) in pages}
        ocr_used = False
        ocr_conf = None
        ocr_attempted = False
        ocr_pages: list[int] = []          # pages effectivement OCRisées avec du texte
        page_rotations: dict[int, int] = {}  # rotation appliquée par page (OSD)
        page_ocr_confidences: dict[int, float] = {}
        # Pages dont le budget a expiré vs pages ayant échoué autrement. Les deux causes
        # étaient auparavant confondues sous le seul warning `ocr_page_timeout` : un bug
        # de rendu se présentait comme un dépassement de délai, et menait l'exploitant à
        # augmenter un budget qui n'était pas en cause.
        pages_budget_expire: list[int] = []
        pages_ocr_en_erreur: list[int] = []

        if empty_pages and _ocr_enabled(ctx):
            ocr_attempted = True
            from .ocr import OcrDisabled, OcrUnavailable, ocr_pdf_pages

            try:
                ocr_res = ocr_pdf_pages(
                    data, empty_pages, page_timeout_s=ctx.ocr_page_timeout_s
                )
                ocr_used = ocr_res.used and bool(ocr_res.text.strip())
                ocr_conf = ocr_res.avg_confidence
                for op in ocr_res.pages:
                    if op.text.strip():
                        page_map[op.page_number] = op.text
                        ocr_pages.append(op.page_number)
                    if op.rotation:
                        page_rotations[op.page_number] = op.rotation
                    if op.confidence is not None:
                        page_ocr_confidences[op.page_number] = op.confidence
                for w in ocr_res.warnings:
                    res.add_warning(w)
                if ocr_res.partial:
                    res.partial = True
                pages_budget_expire = list(ocr_res.timeout_pages)
                pages_ocr_en_erreur = list(ocr_res.failed_pages)
            except (OcrUnavailable, OcrDisabled) as e:
                res.partial = True
                res.add_warning(getattr(e, "code", "ocr_unavailable"))

        # --- Gate d'ACCEPTATION du texte natif/OCR (P0) -------------------
        # Un PDF de plan/graphique vectoriel peut laisser un RÉSIDU de bruit
        # (quelques caractères) sans qu'aucune page ne porte de texte exploitable.
        # On n'accepte donc le document que si AU MOINS une page atteint le seuil
        # par page, OU si le total utile atteint DOCUMENT_PDF_MIN_TOTAL_CHARS.
        # Sinon : document image-only -> OCR requis (jamais indexé "vide" en READY).
        #
        # Le total compte les caractères EXPLOITABLES : un document intégralement
        # glyphique totalise des centaines de milliers de caractères et franchissait ce
        # seuil sans porter un seul mot. C'est ce qui l'a laissé passer en READY.
        total_after = sum(usable_chars(t or "") for t in page_map.values())
        has_useful_after = any(
            not _page_is_empty(t, threshold) for t in page_map.values()
        )
        min_total = config.pdf_min_total_chars()
        accept = has_useful_after or total_after >= min_total

        # rail visuel actif sur la source `pdf` => « aucun texte
        # exploitable » n'est plus terminal ICI. Un PDF dont la seule évidence utile est
        # un visuel intégré doit atteindre la découverte, puis le rail visuel, qui
        # décidera PRIMARY ou échec (`visual_no_usable_evidence`/`visual_analysis_failed`).
        # Rail éteint : la terminaison historique est strictement inchangée.
        visual_pdf_active = "pdf" in (ctx.visual_sources or ())

        # Un budget de page expiré n'est PAS un document illisible : c'est un travail
        # interrompu. Le laisser devenir READY sur la seule foi des chunks visuels
        # afficherait un document « traité » dont le texte n'a jamais été lu, et le
        # rendrait indistinguable d'un document réellement sans texte — donc jamais
        # repris. On échoue explicitement, avec un code qui dit quoi faire.
        #
        # La condition est étroite à dessein : il faut À LA FOIS un dépassement réel ET
        # aucun texte exploitable. Un document dont 2 pages sur 40 expirent mais dont les
        # 38 autres portent du texte reste un succès PARTIEL et est indexé (§24) — c'est
        # le comportement légitime que ce lot doit préserver, pas seulement ne pas casser.
        if not accept and pages_budget_expire:
            from .ocr import OcrPageTimeout

            raise OcrPageTimeout(
                "Le budget OCR par page a été dépassé sur "
                f"{len(set(pages_budget_expire))} page(s) et aucun texte exploitable "
                "n'a pu être extrait. Le travail Tesseract a été interrompu : "
                "le document n'est pas nécessairement illisible.",
                code="ocr_page_timeout",
                meta={
                    "is_image_only_pdf": bool(pages),
                    "page_count": len(pages),
                    "ocr_timeout_pages": sorted(set(pages_budget_expire)),
                    "ocr_error_pages": sorted(set(pages_ocr_en_erreur)),
                    "ocr_page_budget_s": ctx.ocr_page_timeout_s,
                    "native_usable_chars": native_usable_chars,
                    "visual_rail_active": visual_pdf_active,
                },
            )

        if not accept and visual_pdf_active:
            res.metadata["pdf_no_usable_text"] = True
            res.metadata["is_image_only_pdf"] = bool(pages)
            res.metadata["image_only_pages"] = [pn for pn, _ in pages]
        elif not accept:
            classes = sorted({classify_text(t or "") for t in page_map.values()})
            err_meta = {
                "is_image_only_pdf": bool(pages),
                "page_count": len(pages),
                # Aucune page ne porte de texte exploitable : toutes sont "image-only".
                "image_only_pages": [pn for pn, _ in pages],
                "text_pages": [],
                "native_text_chars": native_text_chars,
                "native_usable_chars": native_usable_chars,
                "pdf_native_engine": native_info.get("pdf_native_engine"),
                "pdf_native_fallback_used": native_info.get("pdf_native_fallback_used"),
                "ocr_used": ocr_used,
                "text_quality_classes": classes,
                "pdf_fonts_lack_unicode_mapping": no_unicode_fonts,
            }
            if not pages:
                # Rien parsé du tout (PDF corrompu/vide) -> vide réel.
                res.status = ExtractionStatus.EMPTY
            elif (no_unicode_fonts or _glyph_only(page_map)) and not _ocr_enabled(ctx):
                # Cas distinct du scan : le PDF PORTE du texte, mais ses polices n'exposent
                # aucune correspondance vers l'Unicode. Aucun moteur natif ne peut le lire
                # — seul un OCR du rendu le récupère. Le dire précisément évite d'envoyer
                # l'exploitant chercher un problème de scan qui n'existe pas.
                raise ExtractionError(
                    "PDF dont les polices n'exposent aucune table de correspondance "
                    "Unicode (glyphes non mappables) : aucun moteur natif ne peut en "
                    "extraire le texte. Activez l'OCR (DOCUMENT_OCR_ENABLED) pour "
                    "indexer ce document.",
                    code="pdf_text_unrecoverable",
                    meta=err_meta,
                )
            elif not _ocr_enabled(ctx):
                raise ExtractionError(
                    "PDF graphique ou scanné : aucun texte exploitable extrait par "
                    "les moteurs PDF natifs. Activez l'OCR (DOCUMENT_OCR_ENABLED) "
                    "pour indexer les annotations de ce document.",
                    code="pdf_scanned_requires_ocr",
                    meta=err_meta,
                )
            elif ocr_attempted:
                # Distingue "moteur OCR absent du serveur" d'un "OCR sans résultat" :
                # essentiel pour le diagnostic UX (installer l'OCR ≠ document illisible).
                _ENGINE_MISSING = {
                    "ocr_engine_missing", "ocr_pdf_rasterizer_missing",
                    "ocr_unavailable", "ocr_no_provider",
                }
                if any(w in _ENGINE_MISSING for w in res.warnings):
                    raise ExtractionError(
                        "Moteur OCR indisponible sur le serveur (binaire tesseract ou "
                        "rastériseur PDF non installé). Installez l'extra OCR "
                        "(pip install -e \".[ocr]\") et le binaire tesseract.",
                        code="ocr_engine_missing",
                        meta=err_meta,
                    )
                # OCR activé mais aucun texte exploitable produit (page illisible,
                # timeout, ou texte sous le seuil) -> échec explicite.
                raise ExtractionError(
                    "L'OCR n'a produit aucun texte exploitable (page illisible ou "
                    "délai dépassé).",
                    code="ocr_no_text",
                    meta=err_meta,
                )
            else:
                res.status = ExtractionStatus.EMPTY

        # --- Rail tabulaire GÉNÉRIQUE (OPT-IN) ----------------------------
        # Placé AVANT l'assemblage des pages : c'est lui qui peut retirer d'une page
        # la région d'un tableau structuré, pour que le même contenu ne soit pas
        # indexé deux fois — une fois avec ses relations ligne/colonne, une fois
        # aplati et ambigu. Un doublon ambigu concurrencerait la représentation sûre
        # au moment de la recherche.
        generic_report = None
        # décision PAR DOCUMENT, d'après le RagSpace transporté par le contexte.
        apply_generic, generic_reason = config.pdf_generic_tables_decision(ctx.rag_space_id)
        res.metadata["native_tables_decision"] = generic_reason
        if text_pages and apply_generic:
            try:
                from .pdf_tables import extract_generic_tables

                generic_report = extract_generic_tables(data, pages=list(text_pages))
                for pn, residual in (generic_report.residual_page_text or {}).items():
                    if pn in page_map:
                        page_map[pn] = residual
                for w in generic_report.warnings:
                    res.add_warning(w)
            except Exception as e:  # best-effort : jamais bloquant pour l'ingestion
                logger.debug("generic table extraction skipped: %s", e.__class__.__name__)
                generic_report = None

        # blocs + pages (préserve la pagination pour le chunking)
        #
        # Les pages restées inexploitables sont ÉCARTÉES de l'indexation, même si le
        # document est accepté par ailleurs : un OCR qui ne récupère que 8 pages sur 109
        # ne doit pas réinjecter les 101 autres en glyphes. Un chunk mort coûte un
        # embedding, pollue l'index et ne peut jamais être retrouvé.
        # Nettoyage des caractères de contrôle AVANT toute indexation : l'OCR en émet, et
        # PostgreSQL refuse 0x00 — un seul octet faisait échouer le document entier.
        page_map = {pn: sanitize_text(txt) for pn, txt in page_map.items()}

        ordered = sorted(page_map.items(), key=lambda kv: kv[0])
        unusable_pages = [pn for pn, txt in ordered if txt.strip() and is_unusable_pdf_text(txt)]
        indexable = [(pn, txt) for pn, txt in ordered
                     if txt.strip() and not is_unusable_pdf_text(txt)]

        res.pages = indexable
        res.blocks = [
            Block(type=BlockType.OCR_PAGE if (ocr_used and pn in empty_pages) else BlockType.PARAGRAPH,
                  text=txt.strip(), page_number=pn)
            for pn, txt in indexable
        ]
        res.text = "\n\n".join(txt.strip() for _, txt in indexable).strip()
        if unusable_pages:
            res.partial = True
            res.add_warning("pdf_pages_dropped_unreadable")

        res.metadata["pages"] = len(pages)
        res.metadata["ocr_used"] = ocr_used
        res.metadata["unusable_pages_dropped"] = unusable_pages
        res.metadata["text_quality_score"] = extraction_quality_score(res.text)
        res.metadata["text_quality_class"] = classify_text(res.text)
        # Traçabilité moteur natif (P0) — alimente Document.meta["extraction"].
        res.metadata.update(native_info)
        res.metadata["text_pages"] = list(text_pages)
        res.metadata["empty_pages"] = list(empty_pages)
        res.metadata["native_text_chars"] = native_text_chars
        res.metadata["is_image_only_pdf"] = False
        # Score de qualité simple : part des pages porteuses de texte (natif ou OCR).
        total_pages = len(pages)
        if total_pages:
            indexed_pages = len([pn for pn, txt in ordered if txt.strip()])
            res.metadata["extraction_quality_score"] = round(indexed_pages / total_pages, 3)
        if ocr_conf is not None:
            res.metadata["ocr_confidence"] = ocr_conf
            if ocr_conf < 50:
                res.add_warning("ocr_low_confidence")
        if ocr_used:
            # Lève l'ambiguïté que l'absence seule ne lèverait pas : un document OCRisé
            # SANS `ocr_confidence` peut signifier « confiance non calculée » ou « calcul
            # tenté et échoué ». Sans ce drapeau, l'audit devrait la deviner.
            res.metadata["ocr_confidence_computed"] = config.ocr_compute_confidence()
        if ocr_used and has_native:
            res.metadata["ocr_partial_pages"] = len(empty_pages)
        # Pages dont le budget OCR a expiré : le travail Tesseract a été ANNULÉ, pas
        # simplement abandonné. Publié séparément d'`ocr_failed_pages` ci-dessous, qui
        # désigne autre chose : les pages sans natif que l'OCR n'a pas récupérées, quelle
        # qu'en soit la raison. Confondre les deux, c'est perdre la seule information qui
        # oriente vers « relancer avec plus de temps » plutôt que « document illisible ».
        if pages_budget_expire:
            res.metadata["ocr_timeout_pages"] = sorted(set(pages_budget_expire))
        if pages_ocr_en_erreur:
            res.metadata["ocr_error_pages"] = sorted(set(pages_ocr_en_erreur))
        # Métadonnées OCR page-level (P1 bis) — traçabilité / UX.
        if ocr_pages:
            res.metadata["ocr_pages"] = sorted(set(ocr_pages))
        if page_rotations:
            res.metadata["page_rotations"] = {str(k): v for k, v in sorted(page_rotations.items())}
        if page_ocr_confidences:
            res.metadata["page_ocr_confidences"] = {
                str(k): v for k, v in sorted(page_ocr_confidences.items())
            }
        if ocr_used:
            # pages pauvres en natif non récupérées par l'OCR -> à vérifier visuellement.
            res.metadata["ocr_failed_pages"] = sorted(set(empty_pages) - set(ocr_pages))

        # P3-a2 : extraction de tableaux structurés (pile_head_forces) sur les
        # pages à TEXTE NATIF uniquement (pdfplumber a besoin d'une couche texte ;
        # les tableaux OCR/vectoriels = P3-b, hors scope). Best-effort, jamais bloquant.
        specialized_pages: set[int] = set()
        if text_pages:
            try:
                from .table_extractor import (
                    extract_tables_from_pdf_bytes,
                    table_to_markdown,
                    table_to_meta,
                )

                for t in extract_tables_from_pdf_bytes(data, pages=list(text_pages)):
                    res.tables.append({"markdown": table_to_markdown(t), "meta": table_to_meta(t)})
                    specialized_pages.add(int(getattr(t, "page_number", 0) or 0))
            except Exception as e:  # ne jamais faire échouer l'extraction sur les tables
                logger.debug("table extraction skipped: %s", e.__class__.__name__)

        # Rail générique : un fragment = une ligne logique SUR UNE page physique.
        # Un reconnaisseur spécialisé garde la priorité sur ses propres pages (le
        # contrat métier existant est plus riche que la structure nue).
        if generic_report is not None:
            from .pdf_tables import fragment_to_meta, fragment_to_text

            for table in generic_report.tables:
                for row in table.rows:
                    for frag in row.fragments:
                        if frag.page_number in specialized_pages:
                            continue
                        text = fragment_to_text(table, row, frag)
                        if not text:
                            continue
                        meta = fragment_to_meta(table, row, frag)
                        # `page_number` est la clé de citation du rail existant : elle
                        # porte la page PHYSIQUE du fragment, jamais celle du début
                        # de la ligne logique.
                        meta["page_number"] = frag.page_number
                        res.tables.append({"markdown": text, "meta": meta})
            res.metadata["generic_tables"] = len(generic_report.tables)
            res.metadata["generic_table_outcomes"] = _outcome_counts(generic_report)
            # NATIVE-TABLE-GUARD-1 : pages où une grille multi-colonnes a été détectée SANS pouvoir
            # être structurée. Leur texte de page porte ce tableau APLATI (relation valeur/colonne
            # perdue) : il ne doit pas servir de preuve chiffrée. Numéros seulement, aucun contenu.
            #
            # NATIVE-TABLE-RECOVERY-2 : de même pour une page dont le texte résiduel a dû être relu par
            # le moteur de REPLI (mots pdfplumber infidèles). Son contenu est conservé — il reste
            # cherchable — mais sa mise en page n'a pas été recoupée : un tableau ou un graphique non
            # reconnu y est aplati. Constat réel : rendu lisible, un tel texte « prouvait » des valeurs
            # d'une autre colonne par la règle de ligne du texte natif.
            res.metadata["generic_table_unverified_pages"] = sorted({
                int(o["page"]) for o in generic_report.outcomes
                if o.get("outcome") == "fallback_unstructured" and isinstance(o.get("page"), int)} | {
                int(pn) for pn, engine in (generic_report.residual_engine or {}).items() if engine != "pdfplumber"})

        # P3-b : reconstruction de tableaux depuis l'OCR (pages image-only/scannées),
        # OPT-IN (DOCUMENT_TABLE_OCR_ENABLED) et seulement si l'OCR a tourné.
        # Confiance forcée low + requires_visual_check (fragile).
        if ocr_used and empty_pages and config.table_ocr_enabled():
            try:
                from .ocr import ocr_words_for_pages
                from .table_extractor import (
                    extract_ocr_tables_from_pages,
                    table_to_markdown,
                    table_to_meta,
                )

                page_words = ocr_words_for_pages(data, list(empty_pages))
                for t in extract_ocr_tables_from_pages(page_words):
                    res.tables.append({"markdown": table_to_markdown(t), "meta": table_to_meta(t)})
            except Exception as e:
                logger.debug("ocr table reconstruction skipped: %s", e.__class__.__name__)

        if res.tables:
            res.metadata["tables_detected"] = len(res.tables)

        # --- Visuels INTÉGRÉS --------------------------
        # La nature de source est déclarée MÊME quand la découverte est éteinte : c'est
        # elle qui arme la garde « éteindre la fonctionnalité ne retire jamais en
        # silence une évidence visuelle publiée » côté réindexation.
        res.visual_source_kind = "pdf"
        if "pdf" in (ctx.visual_sources or ()):
            from .pdf_visual import PdfVisualLimits, discover_pdf_visual_assets

            regions = generic_report.suppressed_regions if generic_report is not None else None
            discovery = discover_pdf_visual_assets(
                data,
                limits=PdfVisualLimits.for_cap(ctx.pdf_visual_max_assets,
                                               max_per_page=ctx.pdf_visual_max_per_page),
                # Régions tabulaires déjà extraites nativement : le natif fait foi, le
                # visuel qui les recouvre est écarté (aucun doublon appauvri).
                suppressed_regions=regions,
                max_pages=config.pdf_max_pages(),
                # régions vectorielles rendues, par espace (identité transportée).
                vector_regions=config.pdf_vector_regions_enabled_for(ctx.rag_space_id),
            )
            res.visual_suppressed_regions = dict(regions or {})
            res.visual_assets.extend(discovery.assets)
            if discovery.stats.get("images_seen") or discovery.stats.get("admitted"):
                res.metadata["pdf_visual"] = dict(discovery.stats)

        engine = native_info.get("pdf_native_engine", "pypdf")
        method = f"pdf:{engine}" + ("+ocr" if ocr_used else "")
        return res.finalize(fmt="pdf", mime=M.PDF, method=method)


# NOTE: `ImageExtractor` a été déplacé dans `image_extractor.py` (Lot 1 ingestion
# image : ouverture sûre + métadonnées enrichies). Importé par le registre via
# `extraction/__init__.py`.
