# backend/apps/rag_core/services/ingest/docvision/rail.py
from __future__ import annotations

"""
Orchestrateur du rail hybride, appelé depuis `extract_document` DANS LE PARENT.

Pourquoi le parent et pas l'enfant d'extraction : l'enfant est tué brutalement à
l'expiration du budget, et le commentaire qui justifie cette brutalité pose la condition
qui la rend sans conséquence — « l'enfant n'écrit rien de persistant ». Un client réseau
y placerait un appel en vol qu'un SIGKILL laisserait orphelin côté moteur, et ses
exceptions devraient traverser un pickle. Le parent, lui, dispose déjà de tout :
les octets, `result.pages`, `result.tables`, et le texte OCR **déjà produit**.

Le rail ne fabrique jamais un document : il reçoit un `ExtractionResult` complet et le
rend enrichi, ou le rend tel quel. Il n'y a pas d'état intermédiaire publiable.
"""

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from django.conf import settings

from ..extraction.base import ExtractionResult
from . import config
from .client import MineruSidecar, SidecarError
from .normalize import blocs_depuis_middle_json
from .router import Route, router_image_autonome, router_page_post_extraction
from .selector import select_raster_extraction


@dataclass
class RapportRail:
    """Compteurs de décision. Aucun contenu documentaire n'y figure jamais."""

    pages_considered: int = 0
    pages_routed: int = 0
    pages_skipped_max: int = 0
    pages_skipped_budget: int = 0
    mineru_success: int = 0
    technical_fallback: int = 0
    quality_fallback: int = 0
    timeout: int = 0
    native_chars_preserved: int = 0
    raster_chars_added: int = 0
    bbox_blocks: int = 0
    tables_imported: int = 0
    tables_skipped_duplicate: int = 0
    duration_s: float = 0.0
    skipped_reason: Optional[str] = None
    decisions: List[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "decisions"}
        d["decisions"] = self.decisions
        return d


def _budget_celery_disponible(budget_extraction: Optional[int],
                              ecoule_s: float) -> Tuple[bool, str, int]:
    """Le rail tient-il dans ce qui reste du budget de la tâche ?

    Le danger est arithmétique, pas théorique : l'extraction « délai étendu » peut
    consommer 480 s, et trois pages MinerU au pire cas mesuré (60 s) porteraient le
    chemin à 660 s, au-delà de la limite dure de 600 s. Le rail ne peut donc pas
    simplement ajouter son budget après coup.

    Politique conservatrice : sur le chemin étendu, le rail est SAUTÉ. Sur le chemin
    standard, on exige que le temps déjà écoulé plus le budget maximal du rail tienne
    sous la limite douce. Aucune marge n'est inventée : la borne est la limite douce
    elle-même, et le budget du rail est celui qui est configuré.
    """
    etendu = int(getattr(settings, "DOCUMENT_EXTRACTION_EXTENDED_TIMEOUT_SECONDS", 480))
    souple = int(getattr(settings, "CELERY_TASK_SOFT_TIME_LIMIT", 540))
    if budget_extraction and int(budget_extraction) >= etendu:
        return False, "CELERY_BUDGET_GUARD:chemin_etendu", 0
    restant = souple - ecoule_s
    par_page = config.page_timeout_s()
    pages_possibles = int(restant // par_page) if par_page > 0 else 0
    if pages_possibles < 1:
        return False, "CELERY_BUDGET_GUARD:budget_restant_insuffisant", 0
    return True, "", pages_possibles


def _pages_eligibles(result: ExtractionResult, est_image: bool) -> List[Tuple[int, str]]:
    if est_image:
        return [(1, (result.text or ""))]
    return [(n, t or "") for n, t in (result.pages or [])]


def appliquer_rail(
    result: ExtractionResult,
    *,
    data: bytes,
    filename: str,
    est_image: bool = False,
    budget_extraction_s: Optional[int] = None,
    ecoule_extraction_s: float = 0.0,
    sidecar: Optional[MineruSidecar] = None,
) -> Tuple[ExtractionResult, RapportRail]:
    """Enrichit `result` en place. En cas d'échec, `result` est rendu inchangé."""
    rap = RapportRail()
    t0 = time.monotonic()

    ok, raison, pages_possibles = _budget_celery_disponible(
        budget_extraction_s, ecoule_extraction_s)
    if not ok:
        rap.skipped_reason = raison
        rap.duration_s = round(time.monotonic() - t0, 3)
        return result, rap

    plafond = min(int(getattr(settings, "DOCUMENT_MINERU_MAX_PAGES_PER_DOC", 3)),
                  pages_possibles)

    pages = _pages_eligibles(result, est_image)
    rap.pages_considered = len(pages)

    # Ordre déterministe : pages croissantes. Choisir selon les résultats attendus
    # reviendrait à ajuster le pipeline sur les réponses.
    # Seule la trace d'extraction distingue une page rastérisée d'une page native :
    # leurs textes sont indiscernables une fois l'OCR appliqué.
    pages_ocr = set((result.metadata or {}).get("ocr_pages") or [])

    a_router: List[Tuple[int, str]] = []
    for numero, texte in sorted(pages, key=lambda p: p[0]):
        d = (router_image_autonome(numero) if est_image else
             router_page_post_extraction(numero, texte,
                                         ocr_applique=numero in pages_ocr))
        if d.route is Route.MINERU_PRIMAIRE:
            a_router.append((numero, texte))
        else:
            rap.native_chars_preserved += len(texte)
        rap.decisions.append({"page": numero, "route": d.route.value, "raison": d.raison})

    if len(a_router) > plafond:
        rap.pages_skipped_max = len(a_router) - plafond
        a_router = a_router[:plafond]

    if not a_router:
        rap.duration_s = round(time.monotonic() - t0, 3)
        return result, rap

    try:
        client = sidecar or MineruSidecar()
        client.verifier_demarrage()
    except Exception as e:  # sidecar absent, télémétrie douteuse, cible refusée
        rap.technical_fallback = len(a_router)
        rap.skipped_reason = f"sidecar indisponible ({type(e).__name__})"
        rap.duration_s = round(time.monotonic() - t0, 3)
        return result, rap

    blocs_ajoutes: List[Any] = []
    tables_ajoutees: List[dict] = []
    textes_pages: Dict[int, str] = {}

    budget_doc = config.document_timeout_s()
    for numero, texte_ocr in a_router:
        # Budget DOCUMENT, opposable page par page. Le budget par page ne le garantit
        # pas : trois pages sous leur propre limite peuvent dépasser celle du document.
        # Un budget seulement déclaré ne borne rien — on l'a déjà payé une fois.
        if time.monotonic() - t0 >= budget_doc:
            rap.pages_skipped_budget += len(a_router) - rap.pages_routed
            rap.skipped_reason = "budget document epuise"
            break
        rap.pages_routed += 1
        sel = _traiter_page(client, data, filename, numero, texte_ocr, rap, est_image)
        if sel is None:
            continue
        if sel.moteur == "mineru":
            rap.mineru_success += 1
            textes_pages[numero] = sel.texte
            rap.raster_chars_added += len(sel.texte)
            tables_ajoutees.extend(sel.tables)
            # La géométrie ne survit que si elle est rattachée au résultat : sans cela
            # les bbox s'arrêtent ici et n'atteignent jamais le meta des chunks.
            blocs_ajoutes.extend(sel.blocs)
        else:
            rap.quality_fallback += 1 if sel.raison.startswith("vocabulaire") else 0
        rap.decisions.append({"page": numero, "selected_engine": sel.moteur,
                              "selection_reason": sel.raison, **sel.mesures})

    if textes_pages:
        _reinjecter(result, textes_pages)
    if blocs_ajoutes:
        result.blocks = list(result.blocks or []) + blocs_ajoutes
    if tables_ajoutees:
        result.tables = list(result.tables or []) + tables_ajoutees
        rap.tables_imported = len(tables_ajoutees)
    rap.bbox_blocks = sum(1 for b in (result.blocks or [])
                          if getattr(b, "bbox", None) is not None)
    rap.duration_s = round(time.monotonic() - t0, 3)
    return result, rap


def _traiter_page(client, data, filename, numero, texte_ocr, rap, est_image):
    try:
        rep = client.parse_middle_json(
            filename, data=data,
            page_range=None if est_image else str(numero),
            timeout_s=config.page_timeout_s())
    except SidecarError as e:
        if getattr(e, "code", "") == "mineru_timeout":
            rap.timeout += 1
        rap.technical_fallback += 1
        rap.decisions.append({"page": numero, "selected_engine": "tesseract",
                              "selection_reason": f"echec technique ({e.code})"})
        return None
    blocs, tables = blocs_depuis_middle_json(
        rep["middle_json"], page_number=numero, rotation=0,
        provenance={"engine": "mineru", "engine_version": rep.get("parser_version"),
                    "tier": config.tier(), "output_format": config.output_format(),
                    "artefact": rep.get("artefact_id")})
    return select_raster_extraction(
        {"texte": "\n".join(b.text for b in blocs if b.text), "tables": tables,
         "blocs": blocs},
        {"texte": texte_ocr}, {"page_number": numero})


def _reinjecter(result: ExtractionResult, textes: Dict[int, str]) -> None:
    """Remplace le texte des SEULES pages où le moteur a été retenu."""
    if result.pages:
        result.pages = [(n, textes.get(n, t)) for n, t in result.pages]
        result.text = "\n\n".join(t for _, t in result.pages if (t or "").strip())
    elif 1 in textes:
        result.text = textes[1]
