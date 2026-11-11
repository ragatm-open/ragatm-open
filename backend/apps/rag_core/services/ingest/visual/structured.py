"""Extraction STRUCTURÉE expérimentale d'un visuel.

Branche ``POST /v1/vision/structured-extract`` (PP-StructureV3 derrière le Gateway) sur
l'enrichissement visuel EXISTANT, sans second pipeline documentaire :

    visuel découvert (``enrichment.discover_assets``, la découverte de la passe primaire)
      -> ``StructuredExtractClient`` (tâche ``chart`` ou ``table`` CHOISIE par l'opérateur)
      -> normalisation minimale :
           chart -> une unité textuelle (catégories, séries, valeurs brutes, texte natif) ;
           table -> ``pdf_tables.LogicalTable`` -> ``rail._append_table_fragments``
                    (le rail tabulaire générique existant, une ligne = un fragment)
                    + une unité d'ensemble (grille, cellules fusionnées)
      -> ``prepare_visual_asset_candidate`` (même budget, mêmes embeddings, même pipeline)
      -> publication AJOUTÉE sous verrou RagSpace puis Document, sans republier le reste.

Ce que ce module ne fait PAS, délibérément :

* aucun classifieur : la tâche est explicite ; aucun déclenchement automatique ;
* aucune correction des valeurs : ``raw_values`` est montré tel que rendu, chaîne
  numérique jamais convertie ; aucune orientation séries/catégories inversée ; aucun
  titre, unité ou axe inventé (``title_candidates`` est présenté comme candidat) ;
* aucune confiance inventée : ``requires_visual_check`` vaut True sur tout fragment
  (sortie ``experimental``, valeurs possiblement estimées) — le voisinage visuel du
  retrieval ne les promeut donc jamais ; le retrieval ordinaire les trouve.

Coexistence avec ``describe`` (Vision V1) : les fragments portent ``meta.visual``
(provenance, page, citation) ET le marqueur ``meta.visual.structured``. Ce marqueur les
EXCLUT de ``published_visual_hashes`` (idempotence et garde de réindexation du rail V1),
de ``published_visual_rows`` (report à la réindexation) et de la suppression par visuel
du rail V1 : un visuel extrait reste à décrire, et une description ne supprime jamais
une extraction. Une réindexation du document ne reporte pas l'extraction (expérimentale,
à relancer explicitement).

Idempotence : une publication remplace les SEULS fragments structurés du même
(document, visuel, tâche) — jamais deux fois le même résultat.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.rag_core.services.ingest.extraction import pdf_tables as P
from apps.rag_core.services.ingest.extraction.base import StructuredUnit

logger = logging.getLogger(__name__)

#: Marqueur des fragments structurés dans ``meta.visual``.
STRUCTURED_KEY = "structured"
#: Registre des extractions dans ``Document.meta`` (JSON existant : aucune migration).
DOCUMENT_META_KEY = "structured_visual"
TASKS = ("chart", "table")
#: Une seule relance, et seulement si l'attente demandée tient dans cette borne.
RETRY_MAX_WAIT_S = 30.0
RETRY_DEFAULT_WAIT_S = 10.0
#: Codes 503 du Gateway quand le GPU du serveur IA est pris (admission).
BUSY_CODES = frozenset({"structx_gpu_busy", "structx_busy", "structured_extract_overloaded"})
#: Taille d'une partie de l'unité d'ensemble d'un tableau : sous la taille de fragment
#: (2 500 en staging), pour que le découpeur commun ne coupe jamais au milieu d'une ligne.
TABLE_PART_MAX_CHARS = 1800
EXPERIMENTAL_NOTICE = ("Extraction automatique expérimentale (PP-StructureV3 via AI Gateway). "
                       "Les valeurs peuvent comprendre des estimations visuelles.")


class StructuredVisualRefused(Exception):
    """Refus AVANT tout appel réseau (drapeau, environnement, espace, visuel)."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass
class StructuredOutcome:
    document_id: str
    asset_hash: str
    task: str
    status: str = "pending"
    error_code: Optional[str] = None
    gateway_calls: int = 0
    retries: int = 0
    elapsed_ms: int = 0
    units: int = 0
    table_fragments: int = 0
    chunks_published: int = 0
    chunks_replaced: int = 0
    response: Optional[dict] = field(default=None, repr=False)
    units_text: List[str] = field(default_factory=list, repr=False)
    table_texts: List[str] = field(default_factory=list, repr=False)
    layout_labels: List[str] = field(default_factory=list)
    retry_after_s: Optional[float] = None
    retryable: bool = False
    #: Numérisation des courbes en mode SHADOW (lot 14) : résultats EN MÉMOIRE seulement,
    #: jamais publiés, enregistrés ni journalisés. ``curve_shadow_status`` : ``off:<refus>``,
    #: ``ran`` ou ``error``.
    curve_shadow: list = field(default_factory=list, repr=False)
    curve_shadow_status: Optional[str] = None


# --------------------------------------------------------------------------- #
# Garde de périmètre
# --------------------------------------------------------------------------- #
def _allowlist() -> set:
    from django.conf import settings

    raw = getattr(settings, "STRUCTURED_VISUAL_RAGSPACE_ALLOWLIST", None) or []
    if isinstance(raw, str):
        raw = raw.split(",")
    return {str(v).strip() for v in raw if str(v).strip()}


#: ``DJANGO_ENV`` de production, et environnements connus HORS production (registre ``core``).
#: Toute autre valeur est inconnue : refus, jamais assimilée à un environnement autorisé.
PRODUCTION_ENVS = frozenset({"prod", "production"})
NON_PRODUCTION_ENVS = frozenset({"dev", "test", "staging"})
#: Plafond CODÉ des tâches publiables en production : aucun réglage ne l'élargit. Les valeurs
#: de graphiques (PP-Chart2Table) ne sont pas qualifiées (STRUCTVIS-PRODUCTION-GATE-1).
PRODUCTION_TASK_CEILING = frozenset({"table"})


def _environment() -> str:
    from django.conf import settings

    return str(getattr(settings, "DJANGO_ENV", "") or "").strip().lower()


def is_production() -> bool:
    return _environment() in PRODUCTION_ENVS


def environment_refusal() -> Optional[str]:
    """``None`` si l'environnement admet l'extraction, sinon le code du refus.

    Production : seulement avec STRUCTURED_VISUAL_PRODUCTION_ALLOWED (même modèle que
    VISUAL_V2_PRODUCTION_ALLOWED). Environnement inconnu : toujours refusé."""
    from django.conf import settings

    env = _environment()
    if env in PRODUCTION_ENVS:
        if getattr(settings, "STRUCTURED_VISUAL_PRODUCTION_ALLOWED", False) is True:
            return None
        return "structured_visual_production_refused"
    if env in NON_PRODUCTION_ENVS:
        return None
    return "structured_visual_environment_unknown"


def allowed_tasks() -> frozenset:
    """Tâches admises : STRUCTURED_VISUAL_ALLOWED_TASKS (vide = toutes), valeurs inconnues
    ignorées, puis plafond de production. Une liste explicite sans valeur valide n'admet rien."""
    from django.conf import settings

    raw = getattr(settings, "STRUCTURED_VISUAL_ALLOWED_TASKS", None) or []
    if isinstance(raw, str):
        raw = raw.split(",")
    configured = [str(v).strip().lower() for v in raw if str(v).strip()]
    tasks = set(TASKS) if not configured else {t for t in configured if t in TASKS}
    if is_production():
        tasks &= PRODUCTION_TASK_CEILING
    return frozenset(tasks)


def check_scope(doc, task: Optional[str] = None) -> None:
    """Drapeau allumé, environnement admis, espace listé, tâche admise. Sinon refus sans appel."""
    from django.conf import settings

    if not getattr(settings, "STRUCTURED_VISUAL_ENABLED", False):
        raise StructuredVisualRefused("structured_visual_disabled")
    refusal = environment_refusal()
    if refusal is not None:
        raise StructuredVisualRefused(refusal)
    if str(doc.rag_space_id) not in _allowlist():
        raise StructuredVisualRefused("structured_visual_space_not_allowed")
    if task is not None and task not in allowed_tasks():
        raise StructuredVisualRefused("structured_visual_task_not_allowed")


def _space_override(setting: str, rag_space_id) -> Optional[int]:
    """Valeur > 0 de ``setting`` pour un espace de l'extraction structurée, sinon ``None``.

    Drapeau allumé, environnement admis et espace listé : la valeur remplace la borne
    globale correspondante pour CET espace. Partout ailleurs ``None`` : comportement
    inchangé."""
    from django.conf import settings

    value = int(getattr(settings, setting, 0) or 0)
    if value <= 0 or rag_space_id is None or not getattr(settings, "STRUCTURED_VISUAL_ENABLED", False):
        return None
    if environment_refusal() is not None:
        return None
    return value if str(rag_space_id) in _allowlist() else None


def pdf_visual_max_assets_for(rag_space_id) -> Optional[int]:
    """``STRUCTURED_VISUAL_PDF_MAX_ASSETS`` pour l'espace (remplace DOCUMENT_PDF_VISUAL_MAX_ASSETS)."""
    return _space_override("STRUCTURED_VISUAL_PDF_MAX_ASSETS", rag_space_id)


def pdf_visual_max_per_page_for(rag_space_id) -> Optional[int]:
    """``STRUCTURED_VISUAL_PDF_MAX_PER_PAGE`` pour l'espace (remplace DOCUMENT_PDF_VISUAL_MAX_PER_PAGE)."""
    return _space_override("STRUCTURED_VISUAL_PDF_MAX_PER_PAGE", rag_space_id)


def is_structured_meta(meta) -> bool:
    visual = meta.get("visual") if isinstance(meta, dict) else None
    return isinstance(visual, dict) and STRUCTURED_KEY in visual


#: Raisons de NON-éligibilité d'une sortie structurée comme preuve chiffrée (ordre d'évaluation).
EVIDENCE_NOT_QUALIFIED = "structured_visual_unqualified"
EVIDENCE_NOT_CERTIFIED = "structured_visual_uncertified"
EVIDENCE_CONSISTENCY_ANOMALY = "structured_visual_consistency_anomaly"
EVIDENCE_ELIGIBLE = "structured_visual_certified"


def structured_evidence_decision(meta) -> tuple:
    """``(probante, raison)`` d'une sortie structurée — contrat ``ai-gateway-visual-contract/22A.1``.

    Probante SEULEMENT si TOUT est déclaré par le Gateway, au type exact :
    1. moteur qualifié : ``qualified_for_production is True``, ``experimental is False``, ``extraction_status ==
       "success"`` (22B) ;
    2. valeur CERTIFIÉE : bloc ``evidence`` (schéma "2") avec ``certified is True`` ET
       ``numeric_claims_supported is True``, et ``qualification.business_numeric_use_authorized is True`` ;
    3. aucune anomalie : ``evidence.consistency_status == "no_anomaly_detected"`` et cohérence de l'item non
       ``anomalies_detected`` (« no_anomaly_detected n'est pas une preuve » : condition NÉCESSAIRE, jamais suffisante).
    Schéma "1" (aucun bloc ``evidence``), bloc absent, invalide ou inconnu : jamais probante — une sortie v1 n'est
    jamais plus probante qu'une v2. Aucun score ``confidence`` n'est lu. Le contrat 22A.1 déclare ``certified`` et
    ``numeric_claims_supported`` TOUJOURS faux : aucune sortie actuelle n'est probante."""
    if not is_structured_meta(meta):
        return False, EVIDENCE_NOT_QUALIFIED
    prov = meta["visual"].get(STRUCTURED_KEY)
    if not (isinstance(prov, dict) and prov.get("qualified_for_production") is True
            and prov.get("experimental") is False and prov.get("extraction_status") == "success"):
        return False, EVIDENCE_NOT_QUALIFIED
    evidence = prov.get("evidence")
    qualification = prov.get("qualification")
    if not (isinstance(evidence, dict) and evidence.get("certified") is True
            and evidence.get("numeric_claims_supported") is True
            and isinstance(qualification, dict) and qualification.get("business_numeric_use_authorized") is True):
        return False, EVIDENCE_NOT_CERTIFIED
    item = prov.get("item_consistency")
    if evidence.get("consistency_status") != "no_anomaly_detected" or (
            isinstance(item, dict) and item.get("status") == "anomalies_detected"):
        return False, EVIDENCE_CONSISTENCY_ANOMALY
    return True, EVIDENCE_ELIGIBLE


def is_qualified_structured_meta(meta) -> bool:
    """Compatibilité 22B : vrai seulement si la sortie est PROBANTE au sens de ``structured_evidence_decision``."""
    return structured_evidence_decision(meta)[0]


# --------------------------------------------------------------------------- #
# Découverte : la MÊME que l'enrichissement différé
# --------------------------------------------------------------------------- #
def _source_kind(doc) -> str:
    from . import deferred as D

    kind = D.state_of(doc).get("source_kind") or ""
    if kind:
        return kind
    name = (doc.filename or "").lower()
    for ext in ("pdf", "docx", "pptx"):
        if name.endswith("." + ext):
            return ext
    return ""


def discover(doc) -> list:
    """Visuels du document par la découverte existante (aucun appel, aucune écriture)."""
    from . import deferred as D
    from .enrichment import _read_source, discover_assets

    state = dict(D.state_of(doc))
    state["source_kind"] = _source_kind(doc)
    return discover_assets(doc=doc, data=_read_source(doc), state=state)


def find_asset(assets: list, asset_hash: str):
    prefix = (asset_hash or "").strip().lower()
    if len(prefix) < 12:
        raise StructuredVisualRefused("structured_visual_asset_hash_too_short")
    matches = {a.content_hash: a for a in assets if a.content_hash.startswith(prefix)}
    if len(matches) != 1:
        raise StructuredVisualRefused("structured_visual_asset_not_found" if not matches
                                      else "structured_visual_asset_ambiguous")
    return next(iter(matches.values()))


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #
def _num(value) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _shown(series: dict, i: int) -> Optional[str]:
    """Valeur telle que RENDUE par le moteur (``raw_values``), sinon la valeur numérique."""
    raw = series.get("raw_values") or []
    if i < len(raw) and raw[i] not in (None, ""):
        return str(raw[i])
    values = series.get("values") or []
    if i < len(values) and values[i] is not None:
        return _num(values[i])
    return None


def _source_line(filename: str, page: Optional[int]) -> str:
    where = f", page {page}" if page else ""
    return f"Source : {filename or 'document'}{where}"


#: Champs RÉELS du contrat 22A.1 conservés (gateway-provenance-contract.md) ; tout autre champ est ignoré.
_EVIDENCE_KEYS = ("contract", "certified", "evidence_level", "numeric_claims_supported", "value_origin",
                  "confidence", "consistency_status", "consistency_anomalies", "validation")
_QUALIFICATION_KEYS = ("protocol", "corpus", "task_status", "business_numeric_use_authorized")


def _evidence_meta(evidence) -> Optional[dict]:
    if not isinstance(evidence, dict):
        return None
    return {k: evidence[k] for k in _EVIDENCE_KEYS if k in evidence}


def _qualification_meta(qualification) -> Optional[dict]:
    if not isinstance(qualification, dict):
        return None
    return {k: qualification[k] for k in _QUALIFICATION_KEYS if k in qualification}


def _consistency_meta(consistency) -> Optional[dict]:
    if not isinstance(consistency, dict):
        return None
    anomalies = consistency.get("anomalies")
    return {"status": consistency.get("status"),
            "anomalies": [a for a in anomalies if isinstance(a, str)] if isinstance(anomalies, list) else []}


def visual_meta(asset, data: dict, *, task: str, item_index: int, item: dict,
                response_sha256: str) -> dict:
    """Provenance BORNÉE d'un fragment : identités, statuts — jamais d'octets ni de brut."""
    from .contracts import citation_slide_number

    engine = data.get("engine") or {}
    image = data.get("image") or {}
    meta = {
        "visual": {
            "kind": task,
            "unit": f"structured_{task}",
            "asset_hash": asset.content_hash,
            "ordinal": asset.ordinal,
            "source_page": asset.source_page,
            "region_index": item_index,
            "region_bbox_px": item.get("region_bbox"),
            "schema_version": data.get("schema_version"),
            "task": task,
            "warnings": [w for w in data.get("warnings") or [] if isinstance(w, str)],
            "requires_visual_check": True,
            STRUCTURED_KEY: {
                "contract": data.get("contract"),
                "engine": engine.get("name"),
                "chart_model": engine.get("chart_model"),
                "engine_versions": dict(engine.get("versions") or {}),
                "gateway_build": data.get("gateway_build"),
                "environment": data.get("environment"),
                # Absent ou mal formé : jamais lu comme « non expérimental » ni « qualifié ».
                "experimental": data.get("experimental") is not False,
                "qualified_for_production": data.get("qualified_for_production") is True,
                "extraction_status": data.get("extraction_status"),
                "request_id": data.get("request_id"),
                "response_sha256": response_sha256,
                "image_sha256": image.get("source_sha256"),
                # Contrat 22A.1 : version servie, preuve, qualification, cohérence de
                # CET item. Absents en schéma "1" : la règle de preuve les lit comme non certifiés.
                "schema_version": str(data.get("schema_version") or "1"),
                "evidence": _evidence_meta(data.get("evidence")),
                "qualification": _qualification_meta(data.get("qualification")),
                "item_consistency": _consistency_meta(item.get("consistency")),
            },
        }
    }
    if asset.source_locator:
        meta["locator"] = dict(asset.source_locator)
    slide = citation_slide_number(asset.source_locator)
    if slide is not None:
        meta["slide_number"] = slide
    return meta


# --------------------------------------------------------------------------- #
# Identité et sémantique des valeurs d'un graphique
# --------------------------------------------------------------------------- #
#: Score OCR minimal d'une ligne de titre retenue (une ligne illisible ne nomme rien).
OCR_MIN_SCORE = 0.8
TITLE_MAX_CHARS = 200
MAX_IDENTIFIERS = 6
_TICK_RE = re.compile(r"^[+-]?\d+(?:[.,]\d+)?$")
_DATE_RE = re.compile(r"^\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}$")
_ID_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{6,}")


def _tick_value(text: str) -> Optional[float]:
    t = (text or "").strip().replace(" ", "")
    return float(t.replace(",", ".")) if _TICK_RE.match(t) else None


def _ocr_lines(data: dict, item: dict) -> list:
    """Lignes OCR du moteur DANS la région du graphique : ``(texte, score, boîte)``."""
    ocr = ((data.get("raw_output") or {}).get("ocr") or {}) if isinstance(data, dict) else {}
    texts, scores, boxes = ocr.get("rec_texts") or [], ocr.get("rec_scores") or [], ocr.get("rec_boxes") or []
    region = item.get("region_bbox") or None
    out = []
    for i, text in enumerate(texts):
        box = boxes[i] if i < len(boxes) else None
        if not isinstance(text, str) or not text.strip() or not (isinstance(box, (list, tuple)) and len(box) == 4):
            continue
        if region and len(region) == 4:
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            if not (region[0] <= cx <= region[2] and region[1] <= cy <= region[3]):
                continue
        score = scores[i] if i < len(scores) and isinstance(scores[i], (int, float)) else 0.0
        out.append((text.strip(), float(score), [float(v) for v in box]))
    return out


#: Lecture NETTE d'un identifiant : lettres en préfixe, chiffres, au plus 3 lettres en suffixe.
#: Une lettre AU MILIEU des chiffres (« Scooo0o08766 ») trahit une confusion OCR o/0, l/1.
_CLEAN_ID_RE = re.compile(r"^[A-Za-z]*\d[\d_-]*[A-Za-z]{0,3}$")


def _identifiers(title: str) -> List[str]:
    """Identifiants alphanumériques d'un titre : ≥ 6 caractères dont ≥ 4 chiffres, hors dates."""
    found: List[str] = []
    for token in _ID_TOKEN_RE.findall(title or ""):
        token = token.strip("_-")
        if len(token) < 6 or sum(ch.isdigit() for ch in token) < 4 or _DATE_RE.match(token):
            continue
        if token not in found:
            found.append(token)
    return found[:MAX_IDENTIFIERS]


def split_identifiers(identifiers: List[str]) -> tuple:
    """(lectures nettes, lectures incertaines) — jamais corrigées, jamais devinées."""
    clean = [i for i in identifiers if _CLEAN_ID_RE.match(i)]
    return clean, [i for i in identifiers if i not in clean]


def chart_identity(item: dict, data: dict) -> dict:
    """Titre, identifiants et échelle de l'axe des valeurs, lus dans l'OCR du MOTEUR.

    Titre : lignes OCR lisibles situées AU-DESSUS de la plus haute graduation de l'axe des
    valeurs (ou dans le bandeau supérieur s'il n'y en a pas), regroupées par rangée et lues
    de gauche à droite. Ce sont des textes imprimés lus par le moteur — jamais complétés,
    jamais déduits de la description. Échelle : graduations numériques de l'axe gauche.
    """
    lines = _ocr_lines(data, item)
    region = item.get("region_bbox") or None
    if region and len(region) == 4:
        left, top, right, bottom = (float(v) for v in region)
    elif lines:
        left, top = min(b[0] for _, _, b in lines), min(b[1] for _, _, b in lines)
        right, bottom = max(b[2] for _, _, b in lines), max(b[3] for _, _, b in lines)
    else:
        return {"title": None, "identifiers": [], "value_axis": None, "source": None}
    width, height = max(right - left, 1.0), max(bottom - top, 1.0)
    left_ticks = [(v, b) for t, s, b in lines
                  if (v := _tick_value(t)) is not None and b[2] <= left + 0.15 * width]
    right_ticks = [b for t, s, b in lines
                   if _tick_value(t) is not None and b[0] >= right - 0.15 * width]
    plot_top = min(b[1] for _, b in left_ticks) if left_ticks else top + 0.15 * height
    heads = sorted(((t, b) for t, s, b in lines
                    if s >= OCR_MIN_SCORE and b[3] <= plot_top + 2 and _tick_value(t) is None),
                   key=lambda tb: (tb[1][1], tb[1][0]))
    rows: list = []
    for text, box in heads:
        for row in rows:
            if box[1] < row["bottom"] and box[3] > row["top"]:
                row["parts"].append((box[0], text))
                row["top"], row["bottom"] = min(row["top"], box[1]), max(row["bottom"], box[3])
                break
        else:
            rows.append({"top": box[1], "bottom": box[3], "parts": [(box[0], text)]})
    title = " ".join(" ".join(t for _, t in sorted(r["parts"])) for r in rows).strip() or None
    if title and len(title) > TITLE_MAX_CHARS:
        title = title[:TITLE_MAX_CHARS].rsplit(" ", 1)[0]
    axis = None
    values = sorted(v for v, _ in left_ticks)
    # Un axe secondaire à droite rend l'appartenance des séries ambiguë : pas d'échelle.
    if len(values) >= 3 and not right_ticks:
        steps = [b - a for a, b in zip(values, values[1:]) if b > a]
        axis = {"min": values[0], "max": values[-1], "step": min(steps) if steps else 0.0}
    return {"title": title, "identifiers": _identifiers(title or ""),
            "value_axis": axis, "source": "engine_ocr" if title or axis else None}


def _numeric(shown: Optional[str]) -> Optional[float]:
    t = (shown or "").strip().replace(" ", "").replace(" ", "")
    try:
        return float(t.replace(",", ".")) if t else None
    except ValueError:
        return None


def series_reading(item: dict, identity: dict) -> List[dict]:
    """Par série : extrema PARMI LES POINTS RELEVÉS et valeurs hors de l'échelle lue."""
    categories = [str(c) for c in item.get("categories") or []]
    axis = identity.get("value_axis")
    out = []
    for s in item.get("series") or []:
        n = max(len(s.get("values") or []), len(s.get("raw_values") or []))
        points = []
        for i in range(n):
            value = _numeric(_shown(s, i))
            if value is not None:
                points.append((value, categories[i] if i < len(categories) else f"valeur n°{i + 1}", _shown(s, i)))
        reading = {"name": s.get("name") or "série sans nom", "points": len(points),
                   "sampled_max": None, "sampled_min": None, "outside_axis": []}
        if len(points) >= 2:
            hi, lo = max(points, key=lambda p: p[0]), min(points, key=lambda p: p[0])
            reading["sampled_max"] = {"value": hi[2], "at": hi[1]}
            reading["sampled_min"] = {"value": lo[2], "at": lo[1]}
        if axis:
            margin = axis["step"] or 0.0
            reading["outside_axis"] = [{"value": p[2], "at": p[1]} for p in points
                                       if p[0] > axis["max"] + margin or p[0] < axis["min"] - margin]
        out.append(reading)
    return out


#: Valeurs hors échelle citées au plus par série (le reste est compté).
OUTSIDE_AXIS_MAX_LISTED = 6


def _chart_blocks(item: dict, identity: dict) -> List[tuple]:
    """Corps d'un graphique en BLOCS ``(en-tête répétable, lignes)`` : catégories, une série
    par bloc, lecture des valeurs, échelle, tableau natif (en-tête = sa rangée de titres)."""
    blocks: List[tuple] = []
    categories = [str(c) for c in item.get("categories") or []]
    corner = item.get("corner_label")
    if categories:
        label = f"Catégories ({corner})" if corner else "Catégories"
        blocks.append(([], [f"{label} : " + ", ".join(categories)]))
    for s in item.get("series") or []:
        name = s.get("name") or "série sans nom"
        unit = f" [unité : {s['unit_hint']}]" if s.get("unit_hint") else ""
        body = []
        n = max(len(s.get("values") or []), len(s.get("raw_values") or []))
        for i in range(n):
            shown = _shown(s, i)
            if shown is None:
                continue
            # Aucune catégorie inventée : au-delà des catégories rendues, le rang seul.
            cat = categories[i] if i < len(categories) else f"valeur n°{i + 1}"
            body.append(f"- {cat} : {shown}")
        blocks.append(([f"Série « {name} »{unit} :"], body))
    readings = series_reading(item, identity)
    caveat: List[str] = []
    reading_lines = []
    if any(r["sampled_max"] for r in readings):
        # En-tête du bloc : répété dans chaque partie qui porte un extremum.
        caveat.append("Lecture des valeurs : points relevés par le moteur aux seules catégories "
                      "ci-dessus (souvent les graduations de l'axe). Pour une courbe continue, un pic "
                      "ou un creux situé entre deux points relevés n'est PAS mesuré : le maximum réel "
                      "de la courbe n'est pas déterminable à partir de ces points.")
        for r in readings:
            if r["sampled_max"]:
                reading_lines.append(
                    f"Série « {r['name']} » : maximum parmi les points relevés = "
                    f"{r['sampled_max']['value']} ({r['sampled_max']['at']}) ; minimum parmi "
                    f"les points relevés = {r['sampled_min']['value']} ({r['sampled_min']['at']})")
    axis = identity.get("value_axis")
    if axis:
        reading_lines.append(f"Échelle de l'axe des valeurs lue : de {_num(axis['min'])} à {_num(axis['max'])}")
        for r in readings:
            if r["outside_axis"]:
                listed = r["outside_axis"][:OUTSIDE_AXIS_MAX_LISTED]
                more = len(r["outside_axis"]) - len(listed)
                shown = ", ".join(f"{p['value']} ({p['at']})" for p in listed)
                reading_lines.append(f"Attention, série « {r['name']} » : {len(r['outside_axis'])} valeur(s) "
                                     f"hors de l'échelle lue, non fiables : {shown}"
                                     + (f" et {more} autre(s)" if more else ""))
    if caveat or reading_lines:
        blocks.append((caveat, reading_lines))
    native = (item.get("native_text") or "").strip()
    if native:
        rows = native.splitlines()
        blocks.append((["Tableau natif du moteur :", rows[0]], rows[1:]))
    return blocks


def _chart_head(identity: dict, data: dict) -> List[str]:
    """Identité du graphique, répétée en tête de chaque partie."""
    head = ["Graphique (extraction structurée expérimentale)"]
    if identity["title"]:
        head.append("Titre imprimé (lecture OCR brute du moteur, espaces et caractères possiblement "
                    "altérés) : " + identity["title"])
    clean, uncertain = split_identifiers(identity["identifiers"])
    if clean:
        head.append("Identifiants lus dans le titre (OCR) : " + ", ".join(clean)
                    + " — à comparer caractère par caractère : un identifiant qui diffère d'un seul "
                    "caractère désigne un autre graphique.")
    if uncertain:
        head.append("Identifiant de lecture OCR incertaine : " + ", ".join(uncertain)
                    + " — illisible tel quel, à ne pas assimiler à un identifiant voisin.")
    titles = [t.strip() for t in data.get("title_candidates") or [] if isinstance(t, str) and t.strip()]
    if titles:
        head.append("Titre détecté (candidat) : " + " | ".join(titles))
    return head


def chart_texts(item: dict, data: dict, *, filename: str, page: Optional[int]) -> List[str]:
    """Unité(s) d'un graphique, découpées sur des frontières de LIGNE.

    Comme pour un tableau, chaque partie tient dans ``TABLE_PART_MAX_CHARS`` (sous la taille
    de fragment : le découpeur ne coupe jamais une unité) et répète l'identité du graphique
    (titre, identifiants) et l'en-tête du bloc en cours : une valeur n'est jamais séparée
    du graphique ni de la série qui la qualifient."""
    identity = chart_identity(item, data)
    head = _chart_head(identity, data)
    tail = [_source_line(filename, page), EXPERIMENTAL_NOTICE]
    room = TABLE_PART_MAX_CHARS - len("\n".join(head + tail)) - 80  # 80 : ligne « Partie i/n … »

    parts: List[List[str]] = [[]]

    def size(lines):
        return sum(len(line) + 1 for line in lines)

    for block_head, lines in _chart_blocks(item, identity):
        if parts[-1] and size(parts[-1]) + size(block_head) + (len(lines[0]) + 1 if lines else 0) > room:
            parts.append([])
        parts[-1].extend(block_head)
        for line in lines:
            if parts[-1] and size(parts[-1]) + len(line) + 1 > room:
                parts.append(list(block_head))
            parts[-1].append(line)

    texts = []
    for i, body in enumerate(parts, start=1):
        marker = [f"Partie {i}/{len(parts)} (identité du graphique et en-tête de bloc répétés)"] if len(parts) > 1 else []
        texts.append("\n".join(head + marker + body + tail).strip())
    return texts


def chart_text(item: dict, data: dict, *, filename: str, page: Optional[int]) -> str:
    """Graphique en une seule unité, sans découpe (lecture, diagnostic)."""
    identity = chart_identity(item, data)
    lines = _chart_head(identity, data)
    for block_head, body in _chart_blocks(item, identity):
        lines += block_head + body
    return "\n".join(lines + [_source_line(filename, page), EXPERIMENTAL_NOTICE]).strip()


#: Bornes de la grille d'un tableau (STRUCTVIS-TABLE-SAFETY-3) : au-delà, la grille n'est pas
#: parcourue (une taille déclarée démesurée ferait boucler la normalisation).
MAX_TABLE_CELLS = 10_000
#: Profondeur maximale d'un en-tête hiérarchique représentable (chemins de colonne).
MAX_HEADER_ROWS = 4
#: Part minimale de la région d'un tableau couverte par une boîte de mise en page ``table``.
TABLE_REGION_MIN_COVER = 0.8
#: Tolérance d'arrondi (px) au bord du visuel ; côté minimal (px) d'une région de tableau
#: (une région plus petite n'est pas dans le repère du visuel : page PDF, coordonnées 0-1).
REGION_EDGE_TOLERANCE_PX = 2.0
REGION_MIN_SIDE_PX = 8.0

REFUSAL_GRID_INVALID = "structured_visual_table_grid_invalid"
#: Une seule famille pour l'absence de PREUVE de région tableau ; la raison précise le cas.
REFUSAL_REGION_UNPROVEN = "structured_visual_table_region_unproven"
REFUSAL_COMPLEX_HEADER = "structured_visual_complex_header_unrepresentable"


def _index(value, minimum: int) -> Optional[int]:
    """Entier >= ``minimum`` (booléen, flottant non entier, texte : ``None``)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return value if isinstance(value, int) and value >= minimum else None


def table_grid_problem(item: dict) -> Optional[str]:
    """``None`` si la grille de cellules est cohérente, sinon la raison (sans contenu de cellule).

    Contrôle du CONTRAT de grille, avant toute normalisation : coordonnées entières >= 0,
    spans >= 1 (absents = 1), aucune ancre en double, aucun recouvrement, aucune cellule hors
    de la taille déclarée, taille bornée. Une grille incohérente n'est jamais « réparée »."""
    cells = item.get("cells")
    if not isinstance(cells, list):
        return "cells_not_a_list"
    declared = []
    for key in ("n_rows", "n_cols"):
        raw = item.get(key)
        value = 0 if raw is None else _index(raw, 0)
        if value is None:
            return f"{key}_invalid"
        declared.append(value)
    if declared[0] * declared[1] > MAX_TABLE_CELLS:
        return "too_large"
    cover = set()
    for cell in cells:
        if not isinstance(cell, dict):
            return "cell_not_an_object"
        r, c = _index(cell.get("row"), 0), _index(cell.get("col"), 0)
        rs = 1 if cell.get("row_span") is None else _index(cell.get("row_span"), 1)
        cs = 1 if cell.get("col_span") is None else _index(cell.get("col_span"), 1)
        if None in (r, c, rs, cs):
            return "invalid_coordinate"
        if (r + rs) * (c + cs) > MAX_TABLE_CELLS:
            return "too_large"
        if (r, c) in cover:
            return "duplicate_or_overlapping_cell"
        span = {(r + dr, c + dc) for dr in range(rs) for dc in range(cs)}
        if span & cover:
            return "duplicate_or_overlapping_cell"
        cover |= span
        if (declared[0] and r + rs > declared[0]) or (declared[1] and c + cs > declared[1]):
            return "cell_outside_declared_grid"
    return None


def _box(value) -> Optional[tuple]:
    """Boîte ``[x0, y0, x1, y1]`` de 4 nombres FINIS, non dégénérée et non inversée, sinon ``None``."""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in value):
        return None
    x0, y0, x1, y1 = (float(v) for v in value)
    if not all(math.isfinite(v) for v in (x0, y0, x1, y1)):
        return None
    return (x0, y0, x1, y1) if x1 > x0 and y1 > y0 else None


def _inside(inner: tuple, outer: tuple) -> float:
    """Part de la surface de ``inner`` contenue dans ``outer``."""
    w = min(inner[2], outer[2]) - max(inner[0], outer[0])
    h = min(inner[3], outer[3]) - max(inner[1], outer[1])
    if w <= 0 or h <= 0:
        return 0.0
    return (w * h) / ((inner[2] - inner[0]) * (inner[3] - inner[1]))


def _visual_size(data: dict) -> Optional[tuple]:
    """(largeur, hauteur) en pixels du visuel TRAITÉ par le moteur : repère des boîtes."""
    raw = data.get("raw_output") if isinstance(data.get("raw_output"), dict) else {}
    for source in (data.get("image"), raw):
        if isinstance(source, dict):
            w, h = source.get("width"), source.get("height")
            if all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0
                   for v in (w, h)):
                return float(w), float(h)
    return None


def table_region_proof(item: dict, data: dict) -> Optional[str]:
    """``None`` si une preuve POSITIVE de région tableau existe, sinon la raison du refus.

    Preuve = une boîte de mise en page ``table`` du moteur (``raw_output.layout_boxes``),
    bien formée, finie, dans le visuel traité, qui couvre au moins ``TABLE_REGION_MIN_COVER``
    de la ``region_bbox`` de l'élément, elle-même valide et dans ce même visuel. Toutes deux sont
    en pixels du visuel envoyé (``image.width``/``height``) : rien n'est comparé à un autre
    repère. ``task=table``, des cellules ou des chiffres ne prouvent rien ; aucun repli."""
    raw = data.get("raw_output") if isinstance(data, dict) else None
    boxes = raw.get("layout_boxes") if isinstance(raw, dict) else None
    if not isinstance(boxes, list):
        return "layout_missing"
    if not boxes:
        return "layout_empty"
    size = _visual_size(data)
    if size is None:
        return "visual_size_unknown"
    tol = REGION_EDGE_TOLERANCE_PX

    def within(box: tuple) -> bool:
        return box[0] >= -tol and box[1] >= -tol and box[2] <= size[0] + tol and box[3] <= size[1] + tol

    region = _box(item.get("region_bbox"))
    if (region is None or not within(region) or region[2] - region[0] < REGION_MIN_SIDE_PX
            or region[3] - region[1] < REGION_MIN_SIDE_PX):
        return "region_invalid"
    tables = [box for box in (_box(b.get("coordinate")) for b in boxes
                              if isinstance(b, dict) and b.get("label") == "table") if box and within(box)]
    if not any(_inside(region, box) >= TABLE_REGION_MIN_COVER for box in tables):
        return "no_table_layout_box"
    return None


def _cell_grid(item: dict):
    """(ancres par position, couverture par position) d'après row/col/row_span/col_span."""
    anchors, cover = {}, {}
    for cell in item.get("cells") or []:
        r, c = int(cell.get("row", 0)), int(cell.get("col", 0))
        rs, cs = max(1, int(cell.get("row_span") or 1)), max(1, int(cell.get("col_span") or 1))
        anchors[(r, c)] = cell
        for dr in range(rs):
            for dc in range(cs):
                cover.setdefault((r + dr, c + dc), (r, c))
    return anchors, cover


def _table_shape(item: dict, cover: dict) -> tuple:
    n_rows = int(item.get("n_rows") or 0)
    n_cols = int(item.get("n_cols") or 0)
    if cover:
        n_rows = max(n_rows, 1 + max(r for r, _ in cover))
        n_cols = max(n_cols, 1 + max(c for _, c in cover))
    return n_rows, n_cols


def _simple_header_row(anchors: dict, n_rows: int, n_cols: int) -> bool:
    """La rangée 0 n'est un en-tête que si elle est SIMPLE : une ancre par colonne, aucune
    fusion, au moins deux libellés. Un titre fusionné sur la largeur, un en-tête à
    plusieurs niveaux ou une rangée vide n'en sont pas : colonnes neutres, plutôt qu'un
    libellé qui attribuerait des valeurs à la mauvaise colonne (constat staging, p36)."""
    if n_rows < 2:
        return False
    row = [anchors.get((0, k)) for k in range(n_cols)]
    if any(c is None or int(c.get("row_span") or 1) > 1 or int(c.get("col_span") or 1) > 1 for c in row):
        return False
    return sum(1 for c in row if str(c.get("text") or "").strip()) >= 2


def _cell_text(anchors: dict, cover: dict, r: int, k: int) -> str:
    return str(anchors[cover[(r, k)]].get("text") or "").strip() if (r, k) in cover else ""


def _span(cell: dict, key: str) -> int:
    return int(cell.get(key) or 1)


def header_depth(anchors: dict, cover: dict, n_rows: int, n_cols: int) -> Optional[int]:
    """Nombre de rangées d'en-tête : 1 (simple), h >= 2 (hiérarchique représentable), 0 (aucun),
    ``None`` = en-tête hiérarchique présent mais NON représentable (refus de publication).

    Hiérarchique représentable = forme sans ambiguïté : le coin (0, 0) couvre seul les h
    rangées (h <= MAX_HEADER_ROWS, au moins une rangée de données), chaque position du
    bloc est couverte, aucune cellule d'en-tête ne déborde du bloc, la dernière rangée du
    bloc porte une feuille par colonne, et aucune rangée de données n'est un nouvel en-tête
    (tableau à blocs répétés). Toute autre fusion horizontale dans la rangée 0, hors titre
    pleine largeur, est hiérarchique sans être représentable."""
    if _simple_header_row(anchors, n_rows, n_cols):
        return 1
    corner = anchors.get((0, 0))
    grouped = any(1 < _span(c, "col_span") < n_cols for (r, _), c in anchors.items() if r == 0)
    if corner is None or _span(corner, "row_span") < 2 or _span(corner, "col_span") != 1:
        return None if grouped else 0
    h = _span(corner, "row_span")
    if h > MAX_HEADER_ROWS or h >= n_rows or n_cols < 2:
        return None
    for (r, k), cell in anchors.items():
        if r < h and r + _span(cell, "row_span") > h:
            return None
    if any((r, k) not in cover for r in range(h) for k in range(n_cols)):
        return None
    for k in range(1, n_cols):
        leaf = anchors.get(cover[(h - 1, k)])
        if leaf is None or _span(leaf, "col_span") != 1 or not _cell_text(anchors, cover, h - 1, k):
            return None
    for r in range(h, n_rows):
        if is_header_row([_cell_text(anchors, cover, r, k) for k in range(1, n_cols)]):
            return None
    return h


def _column_path(anchors: dict, cover: dict, depth: int, k: int) -> List[str]:
    path: List[str] = []
    for r in range(depth):
        text = _cell_text(anchors, cover, r, k)
        if text and (not path or path[-1] != text):
            path.append(text)
    return path


def _schema(item: dict, anchors: dict, cover: dict, n_cols: int, depth: int) -> List[List[str]]:
    """Chemins d'en-tête par colonne : rangée 0 si en-tête simple (hypothèse du moteur, avertie),
    toutes les rangées du bloc si hiérarchique ; vide -> neutre ; étiquette répétée ->
    suffixée du rang de colonne, jamais fusionnée."""
    paths = []
    for k in range(n_cols):
        path = _column_path(anchors, cover, depth, k) if depth else []
        paths.append(path or [P.NEUTRAL_COLUMN.format(k + 1)])
    labels = [" / ".join(p) for p in paths]
    return [[*p[:-1], f"{p[-1]} [col {k + 1}]"] if labels.count(" / ".join(p)) > 1 else p
            for k, p in enumerate(paths)]


REFUSAL_ROW_GROUP_AMBIGUOUS = "structured_visual_row_group_ambiguous"
#: Boîtes de mise en page qui peuvent porter le titre d'un tableau (au-dessus de sa région).
TABLE_TITLE_LABELS = frozenset({"table_title", "figure_title", "paragraph_title"})
TABLE_TITLE_MAX_GAP_PX = 60
TABLE_TITLE_MAX_CHARS = 200


def table_title(item: dict, data: dict) -> Optional[str]:
    """Titre IMPRIMÉ du tableau, ou ``None`` : boîte de titre du moteur la plus proche AU-DESSUS
    de la région (écart <= TABLE_TITLE_MAX_GAP_PX, recouvrement horizontal), texte lu dans l'OCR
    du moteur à l'intérieur de cette boîte. Jamais un titre deviné ni le texte d'à côté."""
    raw = data.get("raw_output") if isinstance(data, dict) else None
    region = _box(item.get("region_bbox"))
    if not isinstance(raw, dict) or region is None:
        return None
    best = None
    for b in raw.get("layout_boxes") or []:
        box = _box(b.get("coordinate")) if isinstance(b, dict) else None
        if box is None or b.get("label") not in TABLE_TITLE_LABELS:
            continue
        gap = region[1] - box[3]
        overlap = min(box[2], region[2]) - max(box[0], region[0])
        if -4 <= gap <= TABLE_TITLE_MAX_GAP_PX and overlap > 0.3 * (box[2] - box[0]) and (best is None or gap < best[0]):
            best = (gap, box)
    if best is None:
        return None
    box = best[1]
    ocr = raw.get("ocr") if isinstance(raw.get("ocr"), dict) else {}
    lines = []
    for text, score, rb in zip(ocr.get("rec_texts") or [], ocr.get("rec_scores") or [], ocr.get("rec_boxes") or []):
        r = _box(rb)
        if r is None or not isinstance(text, str) or (score or 0) < OCR_MIN_SCORE:
            continue
        cx, cy = (r[0] + r[2]) / 2, (r[1] + r[3]) / 2
        if box[0] <= cx <= box[2] and box[1] <= cy <= box[3]:
            lines.append((r[1], r[0], text.strip()))
    title = " ".join(t for _, _, t in sorted(lines) if t)
    return title[:TABLE_TITLE_MAX_CHARS] or None


def row_groups(anchors: dict, cover: dict, n_rows: int, n_cols: int, depth: int) -> tuple:
    """(groupe par rangée de données, refus éventuel). Groupe = section pleine largeur.

    Section : rangée de données dont l'ancre (r, 0) couvre TOUTE la largeur — signal de grille,
    jamais un voisinage textuel. Elle n'est pas une donnée. Une section au texte vide (intitulé
    perdu par le moteur) laisse les rangées suivantes SANS groupe déterminable ; si l'une d'elles
    porte un libellé présent ailleurs dans le tableau, la grille est refusée (homonymes
    indistinguables). Libellé répété dans un même groupe (ou sans groupe) : signalé avec son
    rang d'occurrence, aucun groupe n'est attribué."""
    sections, info, current, lost = set(), {}, None, False
    for r in range(depth, n_rows):
        anchor = anchors.get((r, 0))
        if anchor is not None and n_cols >= 2 and _span(anchor, "col_span") >= n_cols:
            sections.add(r)
            current = str(anchor.get("text") or "").strip() or None
            lost = current is None
            continue
        info[r] = {"label": _cell_text(anchors, cover, r, 0), "section": current, "section_lost": lost}
    keys = {}
    for r, i in info.items():
        keys.setdefault((i["section"], i["section_lost"], i["label"].casefold()), []).append(r)
    labels = [i["label"].casefold() for i in info.values() if i["label"]]
    refusal = None
    for r, i in info.items():
        same = keys[(i["section"], i["section_lost"], i["label"].casefold())]
        i["occurrence"] = (same.index(r) + 1, len(same)) if i["label"] and len(same) > 1 else None
        i["ambiguous"] = i["section_lost"] or i["occurrence"] is not None
        if i["section_lost"] and i["label"] and labels.count(i["label"].casefold()) > 1:
            refusal = (REFUSAL_ROW_GROUP_AMBIGUOUS, "repeated_row_label_after_lost_section")
    return sections, info, refusal


REFUSAL_COLUMN_ASSOCIATION_UNSAFE = "structured_visual_column_association_unsafe"
#: Lignes décalées minimales (une seule peut être une coïncidence de données manquantes).
COLUMN_SHIFT_MIN_ROWS = 2
_NUMBER = r"[+\-−]?(?:\d{1,3}(?:[   ]\d{3})+|\d+)(?:[.,]\d+)?(?:\s?%)?"
#: Cellule faite UNIQUEMENT de nombres (un ou plusieurs, séparateur de milliers espace).
_NUMERIC_CELL_RE = re.compile(rf"^{_NUMBER}(?:\s+{_NUMBER})*$")
_LETTER_RE = re.compile(r"[^\W\d_]")


def _numeric_columns(anchors: dict, r: int, n_cols: int) -> frozenset:
    return frozenset(k for k in range(n_cols) if (r, k) in anchors
                     and _NUMERIC_CELL_RE.match(str(anchors[(r, k)].get("text") or "").strip()))


def column_shift(anchors: dict, cover: dict, n_rows: int, n_cols: int, depth: int,
                 sections: set) -> Optional[tuple]:
    """(code, raison) si des valeurs ont glissé d'une colonne vers la GAUCHE, sinon ``None``.

    Constat réel (bilan de puissance, staging) : le moteur fusionne plusieurs libellés de
    lignes ; sur les lignes qui ont perdu leur libellé, la première valeur occupe la colonne
    des libellés et chaque valeur suivante se trouve sous l'en-tête de sa voisine de gauche.
    Signal de GRILLE, sans vocabulaire ni longueur de texte :
      * lignes libellées : texte avec au moins une lettre en colonne 0, valeurs numériques
        dans les colonnes suivantes — leurs ensembles de colonnes numériques sont la référence ;
      * ligne décalée : nombre(s) seul(s) en colonne 0, et ses colonnes numériques, décalées
        d'un cran à droite, reproduisent EXACTEMENT celles d'une ligne libellée, la colonne
        qui suit sa dernière valeur étant vide.
    Refus si au moins ``COLUMN_SHIFT_MIN_ROWS`` lignes décalées ET au moins la moitié des
    lignes à nombre en tête. Un tableau d'identifiants numériques (années, codes) a ses
    valeurs dans les mêmes colonnes que ses lignes libellées (totaux) : jamais décalé d'un
    cran. Rien n'est déplacé : la grille est refusée, jamais réparée. Un décalage qui touche
    TOUTES les lignes (aucune ligne libellée de référence) n'est pas détectable ici."""
    labelled, headed = set(), []
    for r in range(depth, n_rows):
        if r in sections:
            continue
        numeric = _numeric_columns(anchors, r, n_cols)
        if len(numeric) < 2:
            continue
        if 0 in numeric:
            headed.append((r, numeric))
        elif _LETTER_RE.search(_cell_text(anchors, cover, r, 0)):
            labelled.add(numeric)
    shifted = [r for r, numeric in headed
               if frozenset(k + 1 for k in numeric) in labelled and max(numeric) + 1 < n_cols
               and not _cell_text(anchors, cover, r, max(numeric) + 1)]
    if len(shifted) >= COLUMN_SHIFT_MIN_ROWS and 2 * len(shifted) >= len(headed):
        return REFUSAL_COLUMN_ASSOCIATION_UNSAFE, "row_values_shifted_into_label_column"
    return None


def _row_context(title: Optional[str], group: dict) -> Optional[str]:
    parts = []
    if title:
        parts.append(f"Tableau : {title}")
    if group.get("section"):
        parts.append(f"Groupe : {group['section']}")
    if group.get("occurrence"):
        k, n = group["occurrence"]
        parts.append(f"Libellé « {group['label']} » répété dans ce tableau (occurrence {k}/{n}) ; "
                     "groupe parent non déterminé")
    elif group.get("section_lost"):
        parts.append("Groupe parent non déterminé (intitulé de section illisible)")
    return " ; ".join(parts) or None


def build_table(item: dict, *, asset, item_index: int, meta: dict, depth: int = 0,
                groups: Optional[tuple] = None, title: Optional[str] = None):
    """Élément ``table`` -> ``VisualTable`` du rail tabulaire existant + cellules fusionnées.

    ``depth`` : rangées d'en-tête (``header_depth``). Une cellule de données fusionnée sur
    plusieurs LIGNES (et une seule colonne) qualifie chacune des lignes qu'elle couvre ; une
    fusion horizontale reste écrite une fois, à son ancre."""
    from .table_adapter import VisualTable

    anchors, cover = _cell_grid(item)
    n_rows, n_cols = _table_shape(item, cover)
    paths = _schema(item, anchors, cover, n_cols, depth)
    schema = [" / ".join(p) for p in paths]
    page = asset.source_page or 1
    table_id = P._table_id(asset.content_hash, page, schema, item_index)
    table = P.LogicalTable(table_id=table_id, table_type=P.TABLE_TYPE_GENERIC, schema=schema,
                           column_paths=paths, source_pages=[page])
    merged = []
    for (r, c), cell in sorted(anchors.items()):
        rs, cs = int(cell.get("row_span") or 1), int(cell.get("col_span") or 1)
        if rs > 1 or cs > 1:
            merged.append({"row": r, "col": c, "row_span": rs, "col_span": cs,
                           "text": str(cell.get("text") or "")})
    sections, group_info, _ = groups or (set(), {}, None)
    row_info = {}
    for r in range(depth, n_rows):
        if r in sections:
            continue  # intitulé de section : contexte des rangées suivantes, pas une donnée
        values = {}
        for k in range(n_cols):
            cell = anchors.get((r, k))
            if cell is None:
                origin = cover.get((r, k))
                spanning = anchors.get(origin) if origin else None
                if spanning is None or origin[0] < depth or _span(spanning, "col_span") != 1:
                    continue  # fusion horizontale : émise une seule fois, à l'ancre
                cell = spanning
            text = str(cell.get("text") or "").strip()
            if text:
                values[schema[k]] = text
        if not values:
            continue
        group = group_info.get(r, {})
        row = P.LogicalRow(logical_row_id=P._logical_row_id(table_id, len(table.rows)),
                           row_kind=P.ROW_KIND_DATA, context_label=_row_context(title, group))
        row.fragments.append(P.SourceFragment(page_number=page, physical_row_index=r, cells=values))
        table.rows.append(row)
        row_info[row.logical_row_id] = {"row_group_path": [group["section"]] if group.get("section") else [],
                                        "row_group_ambiguous": bool(group.get("ambiguous")), "table_title": title}
    vt = VisualTable(table=table, header_rows=depth, row_header_columns=0,
                     flat_fallback=False, requires_visual_check=True, merged_cells=merged)
    vt.visual_meta = meta
    vt.row_info = row_info
    return vt, anchors, n_rows, n_cols, merged


_DIGIT_RE = re.compile(r"\d")


def is_header_row(cells: List[str]) -> bool:
    """Rangée d'EN-TÊTE de bloc : au moins deux libellés, aucun chiffre (« Juillet | Aout »).
    Règle structurelle, sans vocabulaire : une rangée qui porte un nombre est une donnée."""
    filled = [c for c in cells if c.strip()]
    return len(filled) >= 2 and not any(_DIGIT_RE.search(c) for c in filled)


def table_overview_texts(item: dict, data: dict, *, anchors: dict, n_rows: int, n_cols: int,
                         merged: list, filename: str, page: Optional[int],
                         columns: Optional[List[str]] = None) -> List[str]:
    """Unité(s) d'ensemble d'un tableau, découpées sur des frontières de LIGNE.

    Chaque partie tient dans ``TABLE_PART_MAX_CHARS`` (sous la taille de fragment, pour
    que le découpeur ne coupe jamais une ligne) et répète la dernière rangée d'en-tête
    rencontrée : une valeur n'est jamais séparée de l'en-tête qui la qualifie. Les
    rangées entièrement vides sont omises, et le texte le dit. ``columns`` (en-tête
    hiérarchique) : chemin complet de chaque colonne, écrit dans chaque partie."""
    titles = [t.strip() for t in data.get("title_candidates") or [] if isinstance(t, str) and t.strip()]
    rows, empty = [], 0
    for r in range(n_rows):
        cells = []
        for k in range(n_cols):
            cell = anchors.get((r, k))
            cells.append("" if cell is None else str(cell.get("text") or "").strip())
        if not any(cells):
            empty += 1
            continue
        rows.append((cells, " | ".join(cells)))

    parts: List[List[str]] = []
    current: List[str] = []
    header: Optional[str] = None
    for cells, line in rows:
        if current and len("\n".join(current)) + len(line) + 1 > TABLE_PART_MAX_CHARS:
            parts.append(current)
            current = [header] if header and not is_header_row(cells) else []
        current.append(line)
        if is_header_row(cells):
            header = line
    if current:
        parts.append(current)

    texts = []
    for i, body in enumerate(parts or [[]], start=1):
        lines = ["Tableau (extraction structurée expérimentale)"]
        if titles:
            lines.append("Titre détecté (candidat) : " + " | ".join(titles))
        lines.append(f"Dimensions : {n_rows} lignes × {n_cols} colonnes"
                     + (f" ({empty} lignes vides omises)" if empty else ""))
        if len(parts) > 1:
            lines.append(f"Partie {i}/{len(parts)} (en-tête de bloc répété)")
        if columns:
            lines.append("Colonnes (en-tête hiérarchique) : " + " ; ".join(
                f"{k} = {label}" for k, label in enumerate(columns, start=1)))
        lines += ["Contenu (cellules fusionnées écrites une fois, à leur ancre) :", *body]
        if merged and i == 1:
            lines.append("Cellules fusionnées : " + "; ".join(
                f"ligne {m['row'] + 1} colonne {m['col'] + 1} sur {m['row_span']}×{m['col_span']}"
                + (f" « {m['text']} »" if m["text"] else "") for m in merged))
        lines.append(_source_line(filename, page))
        lines.append(EXPERIMENTAL_NOTICE)
        texts.append("\n".join(lines).strip())
    return texts


@dataclass
class StructuredNormalization:
    units: List[StructuredUnit] = field(default_factory=list)
    tables: list = field(default_factory=list)
    #: Premier refus de tableau (code, raison) : la réponse entière n'est pas publiée.
    refusal: Optional[tuple] = None


def _table_refusal(item: dict, data: dict) -> Optional[tuple]:
    """(code, raison) si ce tableau ne doit pas être publié, sinon ``None``."""
    problem = table_grid_problem(item)
    if problem is not None:
        return REFUSAL_GRID_INVALID, problem
    if not any(str(c.get("text") or "").strip() for c in item.get("cells") or []):
        return None  # rien à publier : statut « vide » inchangé
    reason = table_region_proof(item, data)
    if reason is not None:
        return REFUSAL_REGION_UNPROVEN, reason
    return None


def normalize_response(data: dict, *, asset, task: str, filename: str,
                       response_sha256: str) -> StructuredNormalization:
    """Réponse validée -> unités + fragments tabulaires (dicts ``{"markdown", "meta"}``)."""
    from .rail import _append_table_fragments

    out = StructuredNormalization()
    holder = type("Holder", (), {})()
    holder.tables = []
    for index, item in enumerate(data.get("items") or []):
        meta = visual_meta(asset, data, task=task, item_index=index, item=item,
                           response_sha256=response_sha256)
        if task == "chart":
            if not any((s.get("values") or s.get("raw_values")) for s in item.get("series") or []):
                continue
            identity = chart_identity(item, data)
            clean, uncertain = split_identifiers(identity["identifiers"])
            meta["visual"][STRUCTURED_KEY]["identity"] = {
                "title": identity["title"], "identifiers": clean, "uncertain_identifiers": uncertain,
                "source": identity["source"]}
            meta["visual"][STRUCTURED_KEY]["value_semantics"] = "points_at_categories"
            meta["visual"][STRUCTURED_KEY]["value_axis"] = identity["value_axis"]
            meta["visual"][STRUCTURED_KEY]["outside_axis_values"] = sum(
                len(r["outside_axis"]) for r in series_reading(item, identity))
            texts = chart_texts(item, data, filename=filename, page=asset.source_page)
            for part, text in enumerate(texts, start=1):
                unit_meta = json.loads(json.dumps(meta))
                if len(texts) > 1:
                    unit_meta["visual"][STRUCTURED_KEY]["part"] = [part, len(texts)]
                out.units.append(StructuredUnit(text=text, meta=unit_meta))
            continue
        refusal = _table_refusal(item, data)
        if refusal is None:
            anchors, cover = _cell_grid(item)
            n_rows, n_cols = _table_shape(item, cover)
            depth = header_depth(anchors, cover, n_rows, n_cols)
            if depth is None:
                refusal = (REFUSAL_COMPLEX_HEADER, "hierarchical_header_not_representable")
            else:
                groups = row_groups(anchors, cover, n_rows, n_cols, depth)
                refusal = groups[2] or column_shift(anchors, cover, n_rows, n_cols, depth, groups[0])
        if refusal is not None:
            # Fail-closed : rien de ce tableau, ni d'aucun autre de la réponse, n'est publié.
            out.refusal = out.refusal or refusal
            continue
        title = table_title(item, data)
        vt, anchors, n_rows, n_cols, merged = build_table(item, asset=asset, item_index=index,
                                                           meta=meta, depth=depth, groups=groups, title=title)
        if not vt.table.rows:
            continue
        texts = table_overview_texts(item, data, anchors=anchors, n_rows=n_rows, n_cols=n_cols,
                                     merged=merged, filename=filename, page=asset.source_page,
                                     columns=vt.table.schema if depth > 1 else None)
        for part, text in enumerate(texts, start=1):
            unit_meta = json.loads(json.dumps(meta))
            unit_meta["visual"]["unit"] = "structured_table_overview"
            unit_meta["visual"][STRUCTURED_KEY]["merged_cells"] = merged
            unit_meta["visual"][STRUCTURED_KEY]["has_native_html"] = bool(item.get("html"))
            unit_meta["visual"][STRUCTURED_KEY]["header_rows"] = depth
            unit_meta["visual"][STRUCTURED_KEY]["table_title"] = title
            unit_meta["visual"][STRUCTURED_KEY]["part"] = [part, len(texts)]
            out.units.append(StructuredUnit(text=text, meta=unit_meta))
        first = len(holder.tables)
        _append_table_fragments(holder, vt)
        for entry in holder.tables[first:]:
            # Groupe de lignes et titre imprimé : métadonnées du fragment (JSON existant, sans migration).
            entry["meta"].update(vt.row_info.get(entry["meta"].get("logical_row_id"), {}))
    for entry in holder.tables:
        # Valeur expérimentale : jamais « vérifiée » (voisinage visuel et tableaux).
        entry["meta"]["requires_visual_check"] = True
    out.tables = holder.tables
    return out


# --------------------------------------------------------------------------- #
# Politique de publication (STRUCTVIS-PRODUCTION-GATE-1)
# --------------------------------------------------------------------------- #
#: Clés d'élément propres à chaque type : un élément qui porte celles de l'AUTRE type est un
#: résultat mixte, jamais publié (aucune réponse réelle ne mélange les deux).
_ITEM_KEYS = {"chart": ("categories", "series"), "table": ("cells", "rows", "html")}


def publication_refusal(data: dict, *, task: str, norm: "StructuredNormalization") -> Optional[str]:
    """Dernière barrière avant toute écriture : ``None`` si publiable, sinon le code du refus.

    Tâche admise ; cohérence tâche demandée / réponse / éléments / fragments normalisés.
    Un désaccord refuse la publication : rien n'est converti d'un type à l'autre."""
    if task not in allowed_tasks():
        return "structured_visual_task_not_allowed"
    if not isinstance(data, dict) or data.get("task") != task:
        return "structured_visual_task_mismatch"
    other = _ITEM_KEYS["table" if task == "chart" else "chart"]
    for item in data.get("items") or []:
        if not isinstance(item, dict) or any(item.get(k) for k in other):
            return "structured_visual_mixed_response"
    for meta in [u.meta for u in norm.units] + [t.get("meta") or {} for t in norm.tables]:
        visual = meta.get("visual") if isinstance(meta, dict) else None
        if not (isinstance(visual, dict) and STRUCTURED_KEY in visual and visual.get("task") == task
                and visual.get("kind") == task):
            return "structured_visual_fragment_type_mismatch"
    if norm.refusal is not None:
        return norm.refusal[0]
    return None


# --------------------------------------------------------------------------- #
# Appel borné
# --------------------------------------------------------------------------- #
def call_with_one_retry(client, *, task: str, asset, request_id: str,
                        sleep: Optional[Callable[[float], None]] = None):
    """Un appel, et AU PLUS une relance : seulement GPU occupé / surcharge / Gateway
    injoignable, et seulement si l'attente demandée (``Retry-After``) tient dans
    ``RETRY_MAX_WAIT_S``. Un timeout client n'est jamais rejoué (il doublerait le budget).
    Rend ``(result, calls, retries)``."""
    from apps.llm_router.gateway.exceptions import VisionGatewayError
    from apps.llm_router.gateway.vision import CODE_UNAVAILABLE

    calls = 0
    for attempt in (0, 1):
        calls += 1
        try:
            result = client.extract(task=task, content=bytes(asset.content), mime_type=asset.mime_type,
                                    request_id=request_id)
            return result, calls, attempt
        except VisionGatewayError as exc:
            exc.gateway_calls = calls
            # `retryable` du contrat d'abord : un refus déclaré non réessayable n'est JAMAIS rejoué.
            busy = exc.retryable and (exc.code in BUSY_CODES or exc.http_status == 503
                                      or exc.code == CODE_UNAVAILABLE)
            wait = getattr(exc, "retry_after_s", None)
            wait = float(wait) if wait is not None else RETRY_DEFAULT_WAIT_S
            if attempt or not busy or wait > RETRY_MAX_WAIT_S:
                raise
            logger.warning("structured extract busy code=%s retry in %ss", exc.code, wait)
            (sleep or time.sleep)(wait)
    raise AssertionError("unreachable")  # pragma: no cover


def request_id_for(document_id: str, asset_hash: str, task: str) -> str:
    seed = f"{document_id}|{asset_hash}|{task}"
    return "rgsx1-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


# --------------------------------------------------------------------------- #
# Publication
# --------------------------------------------------------------------------- #
def _record(doc_id, key: str, record: dict) -> None:
    from apps.rag_core.models import Document

    with transaction.atomic():
        locked = Document.objects.select_for_update().filter(id=doc_id).first()
        if locked is None:
            return
        meta = dict(locked.meta or {})
        registry = dict(meta.get(DOCUMENT_META_KEY) or {})
        registry[key] = record
        meta[DOCUMENT_META_KEY] = registry
        Document.objects.filter(id=locked.id).update(meta=meta)


def publish(*, doc, space, asset_hash: str, task: str, content_sha256: str, candidate,
            record: dict, generation: Optional[str] = None) -> tuple:
    """Transaction COURTE : remplace les fragments structurés de (visuel, tâche), rien d'autre.
    Rend ``(créés, remplacés)``. ``generation`` (chemin automatique) : la génération
    d'enrichissement doit être encore celle du document, sinon rien n'est publié."""
    from apps.rag_core.models import Chunk, Document
    from apps.rag_core.models.document import DocumentStatus
    from apps.rag_core.services.ingest.indexing import _chunk_rows
    from apps.rag_core.services.ragspace_mutation import lock_ragspace_for_mutation

    key = f"{asset_hash}:{task}"
    with transaction.atomic():
        locked_space = lock_ragspace_for_mutation(space.id, expected_identity=candidate.identity)
        if getattr(locked_space, "deleted_at", None) is not None:
            raise StructuredVisualRefused("rag_space_deleted")
        locked = Document.objects.select_for_update().filter(id=doc.id).first()
        if locked is None:
            raise StructuredVisualRefused("not_found")
        # Portée revérifiée sous verrou : une configuration refermée pendant l'appel ne publie rien.
        check_scope(locked, task)
        if locked.status != DocumentStatus.READY or (locked.sha256 or "") != content_sha256:
            raise StructuredVisualRefused("source_changed")
        if generation is not None:
            from . import deferred as D

            if D.state_of(locked).get("generation") != generation:
                raise StructuredVisualRefused("superseded")
        existing = (Chunk.objects.filter(document_id=locked.id).exclude(pipeline_id=None)
                    .values_list("pipeline_id", flat=True).first())
        if existing and candidate.pipeline_id and existing != candidate.pipeline_id:
            raise StructuredVisualRefused("embedding_pipeline_changed")
        replaced, _ = (Chunk.objects.filter(document_id=locked.id, meta__visual__asset_hash=asset_hash,
                                            meta__visual__has_key=STRUCTURED_KEY,
                                            meta__visual__task=task).delete())
        start = Chunk.objects.filter(document_id=locked.id).aggregate(m=Max("chunk_index"))["m"]
        offset = 0 if start is None else int(start) + 1
        rows = _chunk_rows(locked_space, locked, candidate, index_offset=offset)
        Chunk.objects.bulk_create(rows, batch_size=200)
        meta = dict(locked.meta or {})
        registry = dict(meta.get(DOCUMENT_META_KEY) or {})
        registry[key] = {**record, "chunks": len(rows)}
        meta[DOCUMENT_META_KEY] = registry
        Document.objects.filter(id=locked.id).update(meta=meta)
    return len(rows), replaced


def layout_labels(data: dict) -> List[str]:
    """Libellés de mise en page détectés par le moteur (preuve du type, jamais un score)."""
    raw = data.get("raw_output") if isinstance(data, dict) else None
    boxes = raw.get("layout_boxes") if isinstance(raw, dict) else None
    return sorted({str(b.get("label")) for b in boxes or [] if isinstance(b, dict) and b.get("label")})


def _curve_shadow(outcome: StructuredOutcome, *, doc, asset, data: dict) -> None:
    """Chemin SHADOW (lot 14), APRÈS la normalisation : il ne lit que le visuel et une copie
    de l'OCR, et n'écrit que dans ``outcome``. Les fragments sont déjà calculés et n'en
    dépendent pas ; aucune exception ne remonte."""
    try:
        from . import curve_shadow as CS

        refusal = CS.shadow_refusal(doc.rag_space_id)
        if refusal is not None:
            outcome.curve_shadow_status = f"off:{refusal}"
            return
        outcome.curve_shadow = CS.run_for_asset(doc=doc, asset=asset, data=data)
        outcome.curve_shadow_status = "ran"
    except Exception:  # noqa: BLE001 - le shadow n'interrompt jamais l'extraction
        logger.warning("curve shadow failed document_id=%s", getattr(doc, "id", ""), exc_info=False)
        outcome.curve_shadow, outcome.curve_shadow_status = [], "error"


def extract_asset(*, doc, asset, task: str, client, do_publish: bool = True, inline_retry: bool = True,
                  generation: Optional[str] = None,
                  sleep: Optional[Callable[[float], None]] = None) -> StructuredOutcome:
    """Noyau commun (commande ET chemin automatique) : UN visuel, UNE tâche.

    ``inline_retry`` : la commande attend et rejoue une fois sur occupation GPU ; le chemin
    automatique ne dort jamais dans un worker — il remonte l'erreur et se reprogramme.
    Un échec Gateway n'écrit que son statut : le document reste intact et READY."""
    from apps.llm_router.gateway.exceptions import VisionGatewayError

    from .enrichment import _asset_candidate

    content_sha256 = doc.sha256 or ""
    outcome = StructuredOutcome(document_id=str(doc.id), asset_hash=asset.content_hash, task=task)
    key = f"{asset.content_hash}:{task}"
    base_record = {"task": task, "asset_hash": asset.content_hash, "source_page": asset.source_page,
                   "content_sha256": content_sha256, "experimental": True}
    if generation is not None:
        base_record["generation"] = generation
    request_id = request_id_for(str(doc.id), asset.content_hash, task)
    if task not in allowed_tasks():
        # Type exclu par la politique : aucun appel, rien d'écrit.
        outcome.status, outcome.error_code = "refused", "structured_visual_task_not_allowed"
        return outcome
    started = time.monotonic()
    try:
        if inline_retry:
            result, calls, retries = call_with_one_retry(client, task=task, asset=asset, sleep=sleep,
                                                         request_id=request_id)
        else:
            outcome.gateway_calls = 1
            result = client.extract(task=task, content=bytes(asset.content), mime_type=asset.mime_type,
                                    request_id=request_id)
            calls, retries = 1, 0
    except VisionGatewayError as exc:
        outcome.gateway_calls = int(getattr(exc, "gateway_calls", 1))
        outcome.retries = max(0, outcome.gateway_calls - 1)
        outcome.elapsed_ms = int((time.monotonic() - started) * 1000)
        outcome.status, outcome.error_code = "failed", exc.code
        outcome.retry_after_s = getattr(exc, "retry_after_s", None)
        outcome.retryable = bool(exc.retryable)
        if do_publish:
            _record(doc.id, key, {**base_record, "status": "failed", "error_code": exc.code,
                                  "http_status": exc.http_status, "at": timezone.now().isoformat()})
        return outcome
    outcome.gateway_calls, outcome.retries = calls, retries
    outcome.elapsed_ms = int((time.monotonic() - started) * 1000)
    data = result.data
    outcome.response = data
    outcome.layout_labels = layout_labels(data)
    response_sha256 = hashlib.sha256(
        json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    norm = normalize_response(data, asset=asset, task=task, filename=doc.filename or "",
                              response_sha256=response_sha256)
    if task == "chart":
        _curve_shadow(outcome, doc=doc, asset=asset, data=data)
    outcome.units = len(norm.units)
    outcome.table_fragments = len(norm.tables)
    outcome.units_text = [u.text for u in norm.units]
    outcome.table_texts = [t["markdown"] for t in norm.tables]
    record = {**base_record, "status": "extracted", "extraction_status": data.get("extraction_status"),
              "items": len(data.get("items") or []), "warnings": list(data.get("warnings") or []),
              "layout_labels": outcome.layout_labels,
              "gateway_build": data.get("gateway_build"), "request_id": data.get("request_id"),
              "response_sha256": response_sha256, "timings": data.get("timings"),
              "at": timezone.now().isoformat()}
    refusal = publication_refusal(data, task=task, norm=norm)
    if refusal is not None:
        outcome.status, outcome.error_code = "refused", refusal
        outcome.units = outcome.table_fragments = 0
        outcome.units_text, outcome.table_texts = [], []
        reason = norm.refusal[1] if norm.refusal and norm.refusal[0] == refusal else None
        logger.warning("structured publication refused code=%s reason=%s task=%s document_id=%s asset=%s",
                       refusal, reason, task, doc.id, asset.content_hash[:12])
        if do_publish:
            detail = {"refusal_reason": reason} if reason else {}
            _record(doc.id, key, {**record, "status": "refused", "error_code": refusal, **detail})
        return outcome
    if not (norm.units or norm.tables):
        outcome.status = "empty"
        if do_publish:
            _record(doc.id, key, {**record, "status": "empty"})
        return outcome
    if not do_publish:
        outcome.status = "extracted"
        return outcome

    candidate = _asset_candidate(doc=doc, space=doc.rag_space, units=norm.units, tables=norm.tables,
                                 filename=doc.filename or "")
    if candidate is None:
        outcome.status = "empty"
        _record(doc.id, key, {**record, "status": "empty"})
        return outcome
    created, replaced = publish(doc=doc, space=doc.rag_space, asset_hash=asset.content_hash, task=task,
                                content_sha256=content_sha256, candidate=candidate,
                                record={**record, "status": "published"}, generation=generation)
    outcome.status = "published"
    outcome.chunks_published, outcome.chunks_replaced = created, replaced
    return outcome


def run_structured_extraction(*, document_id, asset_hash: str, task: str, client=None,
                              do_publish: bool = True,
                              sleep: Optional[Callable[[float], None]] = None) -> StructuredOutcome:
    """Commande opérateur : UN visuel, UNE tâche explicite (diagnostic, rattrapage).

    Un échec (Gateway, normalisation) n'écrit que son statut dans
    ``Document.meta["structured_visual"]`` : le document, son texte et ses autres
    fragments restent intacts et READY."""
    from apps.llm_router.gateway.structured_extract import StructuredExtractClient
    from apps.rag_core.models import Document
    from apps.rag_core.models.document import DocumentStatus

    if task not in TASKS:
        raise StructuredVisualRefused("structured_visual_task_invalid")
    doc = Document.objects.select_related("rag_space").filter(id=document_id).first()
    if doc is None:
        raise StructuredVisualRefused("not_found")
    check_scope(doc, task)
    if doc.status != DocumentStatus.READY:
        raise StructuredVisualRefused("document_not_ready")
    asset = find_asset(discover(doc), asset_hash)
    return extract_asset(doc=doc, asset=asset, task=task, do_publish=do_publish, sleep=sleep,
                         client=client or StructuredExtractClient.from_settings())
