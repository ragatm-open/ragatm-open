"""Contrats en mémoire de l'analyse visuelle.

Frontière de confidentialité : un ``VisualAsset`` porte sa provenance (source,
repère, page, bbox, origine, ordinal, occurrences) mais ``build_vision_request`` n'en
transmet RIEN au Gateway — seulement l'image, la tâche, la langue, un identifiant
opaque dérivé par hachage, le profil logique et le contexte borné.

Le contexte (légende, texte alternatif, texte voisin) n'est JAMAIS une preuve
visuelle : il aide le modèle, il n'est pas resérialisé comme contenu de l'image.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Tuple

from django.conf import settings

from apps.llm_router.gateway import vision_contract as V1
from apps.llm_router.gateway.vision import VisionAnalyzeRequest

SOURCE_KINDS = frozenset({"pdf", "docx", "pptx", "image"})
ORIGINS = frozenset({"embedded", "rendered"})
CONTEXT_FIELDS = ("caption", "alt_text", "nearby_text")
#: Borne consommateur PROVISOIRE par champ de contexte, en attente de la borne réelle
#: du Gateway (compatibilité à qualifier en MODEL-1). Ne pas la recopier ailleurs.
CONTEXT_MAX_CHARS = 500
REQUEST_ID_PREFIX = "rgv1-"
REQUEST_ID_HASH_CHARS = 32
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_LAST_WHITESPACE_RE = re.compile(r"\s\S*$")


class VisualAssetError(ValueError):
    """Invariant interne d'un visuel ou d'une requête violé (erreur de programmation)."""


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_bbox(value: Any, what: str) -> Optional[Tuple[float, float, float, float]]:
    if value is None:
        return None
    if (not isinstance(value, (tuple, list)) or len(value) != 4
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in value)):
        raise VisualAssetError(f"{what}: 4 numbers expected")
    x0, y0, x1, y1 = (float(v) for v in value)
    if not (x0 < x1 and y0 < y1):
        raise VisualAssetError(f"{what}: min must be lower than max")
    return (x0, y0, x1, y1)


# --------------------------------------------------------------------------- #
# Contexte
# --------------------------------------------------------------------------- #
def bound_context_text(text: Optional[str], limit: int = CONTEXT_MAX_CHARS) -> Optional[str]:
    """Borne déterministe d'un champ de contexte.

    Absent ou blanc -> ``None`` (jamais une chaîne vide fabriquée). Au-delà de
    ``limit`` : coupe à la dernière frontière de mot, sinon coupe dure.
    """
    if text is None:
        return None
    if not isinstance(text, str):
        raise VisualAssetError("context field: string or None expected")
    text = text.strip()
    if not text:
        return None
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if not text[limit].isspace():
        match = _LAST_WHITESPACE_RE.search(cut)
        if match and match.start() > 0:
            cut = cut[:match.start()]
    return cut.rstrip()


@dataclass(frozen=True)
class VisualContext:
    caption: Optional[str] = None
    alt_text: Optional[str] = None
    nearby_text: Optional[str] = None

    def as_request(self) -> dict:
        """Les trois champs, séparés, bornés, ``None`` si absents."""
        return {name: bound_context_text(getattr(self, name)) for name in CONTEXT_FIELDS}

    def fingerprint(self) -> str:
        return sha256_hex(_canonical_json(self.as_request()).encode("utf-8"))


# --------------------------------------------------------------------------- #
# Visuel source
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class VisualOccurrence:
    """Autre emplacement des MÊMES octets dans la source."""

    source_page: Optional[int] = None
    source_bbox: Optional[Tuple[float, float, float, float]] = None


@dataclass(frozen=True)
class VisualAsset:
    content: bytes = field(repr=False)
    mime_type: str
    width: int
    height: int
    source_kind: str
    content_hash: str
    ordinal: int
    source_locator: Optional[dict] = field(default=None, repr=False)
    source_page: Optional[int] = None
    #: bbox de l'image ENVOYÉE, dans le repère où ses régions doivent être remappées.
    #: C'est à l'adaptateur de source de la fournir (rotation/transformations PDF : 1C).
    source_bbox: Optional[Tuple[float, float, float, float]] = None
    context: VisualContext = field(default_factory=VisualContext, repr=False)
    origin: str = "embedded"
    render_dpi: Optional[int] = None
    occurrences: Tuple[VisualOccurrence, ...] = ()
    task: str = "auto"

    def __post_init__(self) -> None:
        if not isinstance(self.content, (bytes, bytearray)) or not self.content:
            raise VisualAssetError("content: non-empty bytes expected")
        if self.mime_type not in V1.MIME_TYPES:
            raise VisualAssetError("mime_type: unsupported")
        if not (_is_int(self.width) and self.width > 0 and _is_int(self.height) and self.height > 0):
            raise VisualAssetError("dimensions: positive integers expected")
        if self.source_kind not in SOURCE_KINDS:
            raise VisualAssetError("source_kind: unsupported")
        if not isinstance(self.content_hash, str) or not _SHA256_RE.match(self.content_hash):
            raise VisualAssetError("content_hash: lowercase sha256 hex expected")
        if not (_is_int(self.ordinal) and self.ordinal >= 0):
            raise VisualAssetError("ordinal: integer >= 0 expected")
        if self.source_page is not None and not (_is_int(self.source_page) and self.source_page > 0):
            raise VisualAssetError("source_page: positive integer expected")
        object.__setattr__(self, "source_bbox", _check_bbox(self.source_bbox, "source_bbox"))
        if self.source_locator is not None and not isinstance(self.source_locator, dict):
            raise VisualAssetError("source_locator: dict expected")
        if not isinstance(self.context, VisualContext):
            raise VisualAssetError("context: VisualContext expected")
        if self.origin not in ORIGINS:
            raise VisualAssetError("origin: unsupported")
        if self.render_dpi is not None and not (_is_int(self.render_dpi) and self.render_dpi > 0):
            raise VisualAssetError("render_dpi: positive integer expected")
        for occ in self.occurrences:
            if not isinstance(occ, VisualOccurrence):
                raise VisualAssetError("occurrences: VisualOccurrence expected")
            if occ.source_page is not None and not (_is_int(occ.source_page) and occ.source_page > 0):
                raise VisualAssetError("occurrences: positive page expected")
            _check_bbox(occ.source_bbox, "occurrences.source_bbox")
        if self.task not in V1.TASKS:
            raise VisualAssetError("task: outside V1 task enumeration")


# --------------------------------------------------------------------------- #
# Requête V1
# --------------------------------------------------------------------------- #
def logical_model(value: Optional[str]) -> Optional[str]:
    """Profil logique ou ``None``. Un nom de modèle concret (``nom:tag``, chemin) est refusé."""
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    if ":" in value or "/" in value or any(ch.isspace() for ch in value):
        raise VisualAssetError("model: logical Gateway profile expected, not a concrete model")
    return value


def vision_model_from_settings() -> Optional[str]:
    return logical_model(getattr(settings, "AI_GATEWAY_VISION_MODEL", "") or None)


def request_id_for(document_opaque_id: str, content_hash: str, task: str,
                   context: VisualContext) -> str:
    """``rgv1-`` + 32 hex : stable d'une tentative à l'autre, sans identifiant en clair."""
    if not isinstance(document_opaque_id, str) or not document_opaque_id:
        raise VisualAssetError("document_opaque_id: non-empty string expected")
    seed = "|".join([document_opaque_id, content_hash, task, context.fingerprint()])
    return REQUEST_ID_PREFIX + sha256_hex(seed.encode("utf-8"))[:REQUEST_ID_HASH_CHARS]


def build_vision_request(asset: VisualAsset, *, document_opaque_id: str, locale: str = "fr",
                         model: Optional[str] = None) -> VisionAnalyzeRequest:
    if locale not in V1.LOCALES:
        raise VisualAssetError("locale: fr or en expected")
    payload: dict = {
        "schema_version": V1.SCHEMA_VERSION,
        "task": asset.task,
        "locale": locale,
        "request_id": request_id_for(document_opaque_id, asset.content_hash, asset.task,
                                     asset.context),
        "image": {
            "data": base64.b64encode(bytes(asset.content)).decode("ascii"),
            "mime_type": asset.mime_type,
        },
        "context": asset.context.as_request(),
    }
    profile = logical_model(model)
    if profile is not None:
        payload["model"] = profile
    return VisionAnalyzeRequest(payload=payload)


def analysis_key(*, content_hash: str, task: str, model: Optional[str], locale: str,
                 context: VisualContext) -> str:
    """Clé de dédoublonnage LOCALE à un document (aucun cache dans ce lot)."""
    parts = [content_hash, task, V1.SCHEMA_VERSION, logical_model(model) or "", locale,
             context.fingerprint()]
    return sha256_hex("|".join(parts).encode("utf-8"))


# --------------------------------------------------------------------------- #
# Bbox
# --------------------------------------------------------------------------- #
def map_bbox(source_bbox: Tuple[float, float, float, float],
             norm_bbox: Iterable[float]) -> Tuple[float, float, float, float]:
    """Remappe une bbox normalisée [0,1] (origine haut-gauche, relative à l'image
    ENVOYÉE) dans le repère de ``source_bbox``. Aucune rotation n'est résolue ici."""
    sx0, sy0, sx1, sy1 = _check_bbox(source_bbox, "source_bbox")
    try:
        nx0, ny0, nx1, ny1 = V1.validate_norm_bbox(list(norm_bbox))
    except V1.ContractViolation as exc:
        raise VisualAssetError(exc.reason) from None
    w, h = sx1 - sx0, sy1 - sy0
    return (round(sx0 + nx0 * w, 4), round(sy0 + ny0 * h, 4),
            round(sx0 + nx1 * w, 4), round(sy0 + ny1 * h, 4))


# --------------------------------------------------------------------------- #
# Artefact validé
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class VisualArtifact:
    asset_hash: str
    ordinal: int
    source_kind: str
    source_locator: Optional[dict] = field(repr=False)
    source_page: Optional[int]
    source_bbox: Optional[Tuple[float, float, float, float]]
    region_index: Optional[int]
    region_bbox_norm: Optional[Tuple[float, float, float, float]]
    region_bbox_source: Optional[Tuple[float, float, float, float]]
    visual_kind: str
    task: str
    schema_version: str
    summary: Optional[str] = field(repr=False)
    warnings: Tuple[str, ...]
    requires_visual_check: bool
    #: TRANSITOIRE : sert à normaliser, jamais à persister.
    payload: Any = field(repr=False, compare=False)
    model: dict
    pipeline: dict
    image: dict
    latency_ms: float
    usable: bool


def _text_region_text(payload: Any) -> str:
    text = payload.get("text") if isinstance(payload, dict) else None
    return text.strip() if isinstance(text, str) else ""


def is_usable(kind: str, payload: Any) -> bool:
    """Le visuel porte-t-il une preuve exploitable ? (≠ document prêt, cf. 1B)."""
    if not isinstance(payload, dict):
        return False
    if kind == "photo":
        return bool(str(payload.get("description") or "").strip())
    if kind == "table":
        return any(c["role"] == "data" and c["state"] == "filled" for c in payload["cells"])
    if kind == "diagram":
        return bool(payload["nodes"])
    if kind == "chart":
        return bool(payload["axes"]) or any(
            pt["precision"] != "unreadable" for s in payload["series"] for pt in s["points"])
    if kind == "technical_drawing":
        return bool(payload["elements"]) or bool(payload["explicit_dimensions"])
    if kind == "text":
        return bool(_text_region_text(payload))
    if kind == "mixed":
        return any(is_usable(r["visual_kind"], r["payload"]) for r in payload["regions"])
    return False


def build_artifact(asset: VisualAsset, data: dict) -> VisualArtifact:
    """Réponse V1 VALIDÉE + provenance du visuel. La réponse ne fournit jamais la
    provenance : repère, page et bbox viennent du ``VisualAsset`` seul."""
    return VisualArtifact(
        asset_hash=asset.content_hash,
        ordinal=asset.ordinal,
        source_kind=asset.source_kind,
        source_locator=dict(asset.source_locator) if asset.source_locator else None,
        source_page=asset.source_page,
        source_bbox=asset.source_bbox,
        region_index=None,
        region_bbox_norm=None,
        region_bbox_source=None,
        visual_kind=data["visual_kind"],
        task=data["task"],
        schema_version=data["schema_version"],
        summary=data["summary"],
        warnings=tuple(data["warnings"]),
        requires_visual_check=data["requires_visual_check"],
        payload=data["payload"],
        model={k: data["model"][k] for k in V1.MODEL_KEYS},
        pipeline={k: data["pipeline"][k] for k in V1.PIPELINE_KEYS},
        image={k: data["image"][k] for k in V1.IMAGE_KEYS},
        latency_ms=data["latency_ms"],
        usable=is_usable(data["visual_kind"], data["payload"]),
    )


def region_artifacts(parent: VisualArtifact) -> list:
    """Un artefact enfant par région d'un visuel ``mixed``, bbox remappée si possible."""
    if parent.visual_kind != "mixed":
        return []
    children = []
    for index, region in enumerate(parent.payload["regions"]):
        norm = tuple(float(v) for v in region["bbox"])
        children.append(VisualArtifact(
            asset_hash=parent.asset_hash,
            ordinal=parent.ordinal,
            source_kind=parent.source_kind,
            source_locator=parent.source_locator,
            source_page=parent.source_page,
            source_bbox=parent.source_bbox,
            region_index=index,
            region_bbox_norm=norm,
            region_bbox_source=(map_bbox(parent.source_bbox, norm)
                                if parent.source_bbox is not None else None),
            visual_kind=region["visual_kind"],
            task=parent.task,
            schema_version=parent.schema_version,
            summary=None,
            warnings=tuple(region["warnings"]),
            requires_visual_check=region["requires_visual_check"],
            payload=region["payload"],
            model=parent.model,
            pipeline=parent.pipeline,
            image=parent.image,
            latency_ms=parent.latency_ms,
            usable=is_usable(region["visual_kind"], region["payload"]),
        ))
    return children


def artifact_meta(artifact: VisualArtifact, unit: str) -> dict:
    """Métadonnées BORNÉES d'une unité normalisée : identités et statuts, jamais
    d'octets, de base64, de contexte ni de charge utile brute."""
    meta = {
        "visual": {
            "kind": artifact.visual_kind,
            "unit": unit,
            "asset_hash": artifact.asset_hash,
            "ordinal": artifact.ordinal,
            "region_index": artifact.region_index,
            "region_bbox_norm": list(artifact.region_bbox_norm) if artifact.region_bbox_norm else None,
            "region_bbox_source": (list(artifact.region_bbox_source)
                                   if artifact.region_bbox_source else None),
            "source_page": artifact.source_page,
            "schema_version": artifact.schema_version,
            "task": artifact.task,
            "warnings": list(artifact.warnings),
            "requires_visual_check": artifact.requires_visual_check,
            "model": dict(artifact.model),
            "pipeline": dict(artifact.pipeline),
        }
    }
    if artifact.source_locator:
        meta["locator"] = dict(artifact.source_locator)
    slide = citation_slide_number(artifact.source_locator)
    if slide is not None:
        # Clé de CITATION déjà générique (projetée par `source_meta`, lue par le panneau
        # Sources) : sans elle, un visuel de diapositive se citerait par le seul nom de
        # fichier. Jamais une page : une diapositive n'en est pas une.
        meta["slide_number"] = slide
    return meta


def citation_slide_number(locator: Optional[dict]) -> Optional[int]:
    """Numéro de diapositive (1-based) porté par un repère, ou ``None``."""
    value = locator.get("slide_number") if isinstance(locator, dict) else None
    if _is_int(value) and value >= 1:
        return value
    return None


# --------------------------------------------------------------------------- #
# Dédoublonnage texte exact
# --------------------------------------------------------------------------- #
def normalize_for_dedup(text: Optional[str]) -> str:
    """NFKC + casefold + compaction des blancs. Comparaison EXACTE ensuite, jamais floue."""
    if not text:
        return ""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def known_text_set(texts: Iterable[str]) -> frozenset:
    """Textes déjà connus (natif/OCR) fournis par l'appelant, normalisés."""
    return frozenset(n for n in (normalize_for_dedup(t) for t in texts) if n)
