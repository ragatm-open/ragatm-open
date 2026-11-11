"""Numérisation des courbes en mode SHADOW.

Branche le prototype FIGÉ du lot 13B (``curve_digitization``, empreinte d'origine
``FROZEN_SHA256``) derrière l'extraction structurée d'un graphique, sans rien lui faire
produire pour le RAG :

    réponse ``structured-extract/1`` déjà obtenue (recadrage + OCR du moteur)
      -> ``run_for_asset`` : un extremum max ET min par série nommée, CPU seul
      -> ``ShadowResult`` en mémoire, rendus dans ``StructuredOutcome.curve_shadow``
      -> une ligne de journal de COMPTES (statuts, durée) — jamais une valeur.

Ce que ce module ne fait PAS, délibérément :

* aucun appel réseau (ni Gateway, ni GPU) : il relit le visuel et l'OCR déjà en main ;
* aucune écriture : ni fragment, ni ``Document.meta``, ni base, ni compteur partagé ;
* aucune valeur dans un texte, une métadonnée de fragment, un prompt ou une API — les
  résultats ne quittent ce module que par l'objet rendu à l'appelant (harnais d'audit) ;
* aucune exception vers l'appelant : tout échec devient ``ERROR`` ou une abstention.

Portée : drapeau ``STRUCTURED_VISUAL_CURVE_SHADOW_ENABLED`` + extraction structurée allumée
+ espace présent dans les DEUX allowlists + ``DJANGO_ENV`` hors ``prod``/``production``.
La production est refusée même drapeaux allumés.

Bornes : image au plus ``MAX_PIXELS`` (borne a priori du temps et de la mémoire : le
prototype alloue quelques copies flottantes du tracé), au plus ``MAX_SERIES`` séries, un
budget par visuel ``ASSET_BUDGET_MS`` vérifié entre deux appels et un plafond par appel
``CALL_LIMIT_MS`` au-delà duquel le résultat est ÉCARTÉ (``ERROR``/``timeout``). Le calcul
n'est pas préemptif : la borne dure est celle des pixels.

Garde-fous de sécurité (lot 16, ``curve_guards``) : second axe Y à droite -> ``UNSUPPORTED`` pour
tout le graphique (``dual_y_axis_unsupported``) ; extremum d'une série neutre posé sur un axe ou une
ligne de quadrillage de même couleur -> ``UNRELIABLE`` (``neutral_series_axis_grid_ambiguity``).

``MEASURED_FROM_PIXELS`` veut dire « toutes les barrières du prototype ont passé », pas
« valeur vérifiée » : la précision numérique n'est pas qualifiée (lots 13 et 13B).
"""
from __future__ import annotations

import io
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

#: Empreinte SHA-256 du prototype figé du lot 13B (``corrected_curve_digitization.py``).
#: ``curve_digitization.py`` en est la copie, à la seule ligne d'import de ``ndimage`` près.
FROZEN_SHA256 = "ac6f7b4ee591ab9a88885ebf6c346fe3650b5925b5c32db4798f01be3e0d9362"
PROTOTYPE_VERSION = "13B-49c8dbca+guards16"
ALGORITHM_PROVENANCE = {
    "source_lot": "curve-digitization-shadow",
    "frozen_sha256": FROZEN_SHA256,
    "substitution": "scipy.ndimage -> curve_ndimage (label 8-connexe, find_objects)",
    "safety_guards": "lot 16 : dual_y_axis_unsupported, neutral_series_axis_grid_ambiguity (curve_guards)",
}

STATUSES = ("MEASURED_FROM_PIXELS", "ESTIMATED_FROM_PIXELS", "UNRELIABLE", "NOT_DETERMINABLE",
            "UNSUPPORTED", "ERROR")
EXTREMA = ("max", "min")
MAX_PIXELS = 2_000_000
MAX_SERIES = 6
ASSET_BUDGET_MS = 2000
CALL_LIMIT_MS = 1000

#: Motif (début du motif du prototype) -> code stable d'abstention.
_REASON_CODES = (
    ("graduations Y lisibles insuffisantes", "axis_ticks_insufficient"),
    ("colonne de graduations Y incohérente", "axis_ticks_inconsistent"),
    ("axe Y non croissant", "axis_not_increasing"),
    ("axe probablement logarithmique", "axis_unsupported_scale"),
    ("étalonnage non linéaire", "axis_calibration_nonlinear"),
    ("PLOT_REGION = UNKNOWN", "plot_region_unknown"),
    ("série introuvable dans la légende", "series_not_in_legend"),
    ("couleur indiscernable", "series_color_ambiguous"),
    ("aucun pixel de la série", "series_absent"),
    ("trace trop lacunaire", "trace_insufficient"),
    ("série superposée", "series_overlapped"),
    ("extremum hors de l'échelle lue", "outside_read_scale"),
    ("extremum hors du domaine étalonné", "extrapolation_refused"),
    ("TRACE_COMPLETENESS = INSUFFICIENT (lacune", "trace_gap_at_extremum"),
    ("TRACE_COMPLETENESS = INSUFFICIENT (extremum contre", "text_interference_at_extremum"),
    ("intervalle trop large", "interval_too_wide"),
    ("neutral_series_axis_grid_ambiguity", "neutral_series_axis_grid_ambiguity"),
    ("neutral_series_background_ambiguity", "neutral_series_background_ambiguity"),
)
_AXIS_CODES = {"axis_ticks_insufficient", "axis_ticks_inconsistent", "axis_not_increasing",
               "axis_unsupported_scale", "axis_calibration_nonlinear"}
_IDENTITY_CODES = {"series_not_in_legend", "series_color_ambiguous"}
_TRACE_CODES = {"series_absent", "trace_insufficient", "series_overlapped", "trace_gap_at_extremum"}


@dataclass
class ShadowResult:
    document_id: str
    page_number: Optional[int]
    visual_id: str
    item_index: int
    series_id: str
    extremum_type: str
    processing_status: str
    extremum_value: Optional[float] = None
    extremum_interval: Optional[list] = None
    #: Valeur calculée mais NON fiable (UNRELIABLE, abstention tardive) : diagnostic seul.
    diagnostic_value: Optional[float] = None
    axis_calibration_status: str = "NOT_EVALUATED"
    series_identity_status: str = "NOT_EVALUATED"
    trace_continuity_status: str = "NOT_EVALUATED"
    text_interference_status: str = "NOT_EVALUATED"
    abstention_reason: Optional[str] = None
    reason_detail: str = ""
    processing_time_ms: float = 0.0
    prototype_version: str = PROTOTYPE_VERSION
    algorithm_provenance: dict = field(default_factory=lambda: dict(ALGORITHM_PROVENANCE))

    def as_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #
# Portée
# --------------------------------------------------------------------------- #
def _list_setting(name: str) -> set:
    from django.conf import settings

    raw = getattr(settings, name, None) or []
    if isinstance(raw, str):
        raw = raw.split(",")
    return {str(v).strip() for v in raw if str(v).strip()}


def shadow_refusal(rag_space_id) -> Optional[str]:
    """``None`` si le shadow peut tourner pour cet espace, sinon le code du refus."""
    from django.conf import settings

    if str(getattr(settings, "DJANGO_ENV", "") or "").strip().lower() in ("prod", "production"):
        return "curve_shadow_production_refused"
    if not getattr(settings, "STRUCTURED_VISUAL_ENABLED", False):
        return "structured_visual_disabled"
    if not getattr(settings, "STRUCTURED_VISUAL_CURVE_SHADOW_ENABLED", False):
        return "curve_shadow_disabled"
    space = str(rag_space_id or "")
    if space not in _list_setting("STRUCTURED_VISUAL_RAGSPACE_ALLOWLIST"):
        return "structured_visual_space_not_allowed"
    if space not in _list_setting("STRUCTURED_VISUAL_CURVE_SHADOW_RAGSPACE_ALLOWLIST"):
        return "curve_shadow_space_not_allowed"
    return None


# --------------------------------------------------------------------------- #
# Un extremum
# --------------------------------------------------------------------------- #
def reason_code(reason: str) -> str:
    for prefix, code in _REASON_CODES:
        if (reason or "").startswith(prefix):
            return code
    return "other"


def _from_prototype(base: dict, r, elapsed_ms: float) -> ShadowResult:
    diag = r.diagnostics or {}
    out = ShadowResult(**base, processing_status=r.status, processing_time_ms=round(elapsed_ms, 2))
    cal = diag.get("calibration") or {}
    code = None if r.status == "MEASURED_FROM_PIXELS" else reason_code(r.reason)
    out.abstention_reason, out.reason_detail = code, r.reason or ""
    out.axis_calibration_status = "PASS" if cal.get("ok") else ("FAIL" if cal else "NOT_EVALUATED")
    if "series_key" in diag:
        out.series_identity_status = "PASS"
    elif code in _IDENTITY_CODES:
        out.series_identity_status = "FAIL"
    if "gates" in diag:
        out.trace_continuity_status = "PASS"
    elif code in _TRACE_CODES:
        out.trace_continuity_status = "FAIL"
    if code == "text_interference_at_extremum":
        out.text_interference_status = "AT_EXTREMUM"
    elif "text_masking" in diag:
        out.text_interference_status = "MASKED"
    if r.status == "MEASURED_FROM_PIXELS":
        out.extremum_value = float(r.value)
        out.extremum_interval = [float(r.interval[0]), float(r.interval[1])]
    elif r.value is not None:
        out.diagnostic_value = float(r.value)
    if code in _AXIS_CODES and out.axis_calibration_status == "NOT_EVALUATED":
        out.axis_calibration_status = "FAIL"
    return out


def _response_for(data: dict, item: dict) -> dict:
    """Vue minimale et COPIÉE de la réponse : région de l'item + OCR. Rien d'autre n'est lu."""
    import copy

    raw = data.get("raw_output") if isinstance(data, dict) else None
    ocr = raw.get("ocr") if isinstance(raw, dict) else None
    return {"items": [{"region_bbox": copy.deepcopy(item.get("region_bbox"))}],
            "raw_output": {"ocr": copy.deepcopy(ocr) if isinstance(ocr, dict) else {}}}


def _has_ocr(response: dict) -> bool:
    ocr = response["raw_output"]["ocr"]
    return bool(ocr.get("rec_texts")) and bool(ocr.get("rec_boxes"))


def run_for_asset(*, doc, asset, data: dict, clock: Callable[[], float] = time.perf_counter,
                  digitize: Optional[Callable] = None) -> List[ShadowResult]:
    """Tous les extrema shadow d'un visuel déjà extrait (tâche ``chart``). Ne lève jamais.

    ``clock`` et ``digitize`` sont des points d'injection de test (temps, échec du moteur)."""
    started = clock()
    results: List[ShadowResult] = []
    try:
        results = _run(doc=doc, asset=asset, data=data, clock=clock, digitize=digitize, started=started)
    except Exception as exc:  # noqa: BLE001 - le shadow n'interrompt jamais l'import
        results = [ShadowResult(document_id=str(getattr(doc, "id", "")), page_number=getattr(asset, "source_page", None),
                                visual_id=str(getattr(asset, "content_hash", "")), item_index=-1, series_id="",
                                extremum_type="", processing_status="ERROR",
                                abstention_reason="shadow_exception", reason_detail=type(exc).__name__)]
    _log_counts(doc, asset, results, (clock() - started) * 1000)
    return results


def _run(*, doc, asset, data, clock, digitize, started) -> List[ShadowResult]:
    base_doc = {"document_id": str(doc.id), "page_number": getattr(asset, "source_page", None),
                "visual_id": str(asset.content_hash)}
    items = [it for it in (data.get("items") or []) if isinstance(it, dict)
             and any((s.get("values") or s.get("raw_values")) for s in it.get("series") or [])]

    def whole(status: str, code: str, detail: str = "") -> List[ShadowResult]:
        return [ShadowResult(**base_doc, item_index=-1, series_id="", extremum_type="",
                             processing_status=status, abstention_reason=code, reason_detail=detail)]

    if not items:
        return []
    if len(items) > 1:
        return whole("UNSUPPORTED", "multiple_chart_items")
    item = items[0]
    index = (data.get("items") or []).index(item)
    names = [str(s.get("name") or "") for s in item.get("series") or []
             if s.get("values") or s.get("raw_values")]
    if len(names) > MAX_SERIES:
        return whole("UNSUPPORTED", "too_many_series", str(len(names)))
    try:
        from PIL import Image

        with Image.open(io.BytesIO(bytes(asset.content))) as probe:
            width, height = probe.size
            probe.verify()
    except Exception as exc:  # noqa: BLE001
        return whole("ERROR", "invalid_image", type(exc).__name__)
    if width * height > MAX_PIXELS:
        return whole("UNSUPPORTED", "image_too_large", f"{width}x{height}")
    response = _response_for(data, item)
    if not _has_ocr(response):
        return whole("NOT_DETERMINABLE", "missing_ocr")
    from . import curve_guards as G

    # Garde-fou 2 (lot 16) : un second axe Y à droite -> aucun extremum, jamais l'échelle gauche par défaut.
    dual = G.right_y_axis(response)
    if dual is not None:
        return whole(dual[0], dual[1], dual[2])
    if digitize is None:
        from . import curve_digitization as CD

        digitize = CD.digitize_extremum
    image = None
    out: List[ShadowResult] = []
    for name in names:
        for extremum in EXTREMA:
            base = {**base_doc, "item_index": index, "series_id": name, "extremum_type": extremum}
            if (clock() - started) * 1000 > ASSET_BUDGET_MS:
                out.append(ShadowResult(**base, processing_status="ERROR", abstention_reason="budget_exhausted"))
                continue
            t0 = clock()
            try:
                r = digitize(io.BytesIO(bytes(asset.content)), response, name, extremum)
            except MemoryError:
                out.append(ShadowResult(**base, processing_status="ERROR", abstention_reason="memory_limit",
                                        processing_time_ms=round((clock() - t0) * 1000, 2)))
                continue
            except Exception as exc:  # noqa: BLE001
                out.append(ShadowResult(**base, processing_status="ERROR", abstention_reason="prototype_exception",
                                        reason_detail=type(exc).__name__,
                                        processing_time_ms=round((clock() - t0) * 1000, 2)))
                continue
            if r.status == "MEASURED_FROM_PIXELS":
                # Garde-fou 1 (lot 16) : extremum d'une série neutre posé sur un axe/une grille de même couleur.
                if image is None:
                    import numpy as np
                    from PIL import Image

                    image = np.asarray(Image.open(io.BytesIO(bytes(asset.content))).convert("RGB"))
                r = G.neutral_axis_grid(r, image)
            elapsed = (clock() - t0) * 1000
            if elapsed > CALL_LIMIT_MS:
                out.append(ShadowResult(**base, processing_status="ERROR", abstention_reason="timeout",
                                        processing_time_ms=round(elapsed, 2)))
                continue
            out.append(_from_prototype(base, r, elapsed))
    return out


def _log_counts(doc, asset, results: List[ShadowResult], total_ms: float) -> None:
    """Comptes seulement : ni valeur, ni nom de série, ni texte OCR dans les journaux."""
    counts = {s: 0 for s in STATUSES}
    for r in results:
        counts[r.processing_status] = counts.get(r.processing_status, 0) + 1
    logger.info("curve shadow document=%s asset=%s results=%d measured=%d unreliable=%d "
                "not_determinable=%d unsupported=%d errors=%d ms=%d",
                getattr(doc, "id", ""), str(getattr(asset, "content_hash", ""))[:12], len(results),
                counts["MEASURED_FROM_PIXELS"], counts["UNRELIABLE"], counts["NOT_DETERMINABLE"],
                counts["UNSUPPORTED"], counts["ERROR"], int(total_ms))
