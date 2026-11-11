"""Contrat AI Gateway Vision **V2** côté consommateur — FERMÉ.

V2 = enveloppe V1 (mêmes clés de premier niveau, mêmes charges utiles par
``visual_kind``, mêmes invariants référentiels) + provenance technique :

* ``image``    : + ``source_sha256``, ``normalized_sha256`` (64 hex minuscules),
  ``normalized_format``, ``normalizer_id`` ;
* ``pipeline`` : + ``fingerprint`` (``vision-v1:<64 hex>``), ``system_prompt_sha256``,
  ``task_prompt_sha256``, ``model_schema_sha256``, ``model_schema_hash_algorithm``,
  ``inference`` {temperature, seed, num_ctx, num_predict},
  ``retry`` {max_attempts, trigger, temperature, seed} ;
* ``model.digest`` : 64 hex minuscules ;
* ``usage``    : + ``attempts`` (≥ 1).

Source de la forme : une Gateway de qualification (OpenAPI pour la requête ; réponse
relevée sur un appel synthétique réel et recoupée avec ``/health/vision``). Un champ en
plus, en moins ou mal formé est une DÉRIVE, jamais une extension à ignorer.
"""
from __future__ import annotations

import re
from typing import Any

from . import vision_contract as v1
from .vision_contract import ContractViolation  # noqa: F401 - réexport

SCHEMA_VERSION = "2"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_FINGERPRINT = re.compile(r"^vision-v1:[0-9a-f]{64}$")

PIPELINE_KEYS = ("prompt_profile", "prompt_profile_version", "pipeline_id", "fingerprint",
                 "system_prompt_sha256", "task_prompt_sha256", "model_schema_sha256",
                 "model_schema_hash_algorithm", "inference", "retry")
INFERENCE_KEYS = ("temperature", "seed", "num_ctx", "num_predict")
RETRY_KEYS = ("max_attempts", "trigger", "temperature", "seed")
IMAGE_KEYS = ("width", "height", "pixels", "bytes", "format", "source_sha256",
              "normalized_sha256", "normalized_format", "normalizer_id")
USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens", "attempts")
REQUEST_KEYS = v1.REQUEST_KEYS


def _hex64(value: Any, path: str) -> None:
    if not isinstance(value, str) or not _HEX64.match(value):
        v1._fail(path, "64 lowercase hex characters expected")


def _validate_pipeline(p: Any) -> None:
    v1._obj(p, "pipeline", PIPELINE_KEYS)
    for key in ("prompt_profile", "prompt_profile_version", "pipeline_id", "model_schema_hash_algorithm"):
        v1._str(p[key], f"pipeline.{key}", non_empty=True)
    if not isinstance(p["fingerprint"], str) or not _FINGERPRINT.match(p["fingerprint"]):
        v1._fail("pipeline.fingerprint", "'vision-v1:<64 hex>' expected")
    for key in ("system_prompt_sha256", "task_prompt_sha256", "model_schema_sha256"):
        _hex64(p[key], f"pipeline.{key}")
    inf = v1._obj(p["inference"], "pipeline.inference", INFERENCE_KEYS)
    v1._num(inf["temperature"], "pipeline.inference.temperature")
    for key in ("seed", "num_ctx", "num_predict"):
        v1._int(inf[key], f"pipeline.inference.{key}", minimum=0)
    retry = v1._obj(p["retry"], "pipeline.retry", RETRY_KEYS)
    v1._int(retry["max_attempts"], "pipeline.retry.max_attempts", minimum=1)
    v1._str(retry["trigger"], "pipeline.retry.trigger", non_empty=True)
    v1._num(retry["temperature"], "pipeline.retry.temperature")
    v1._int(retry["seed"], "pipeline.retry.seed", minimum=0)


def _validate_image(img: Any) -> None:
    v1._obj(img, "image", IMAGE_KEYS)
    for key in ("width", "height", "pixels", "bytes"):
        v1._int(img[key], f"image.{key}", minimum=1)
    for key in ("format", "normalized_format", "normalizer_id"):
        v1._str(img[key], f"image.{key}", non_empty=True)
    _hex64(img["source_sha256"], "image.source_sha256")
    _hex64(img["normalized_sha256"], "image.normalized_sha256")


def validate_response(data: Any, *, expected_request_id: str | None,
                      expected_task: str | None = None) -> dict:
    """Valide une réponse de succès V2 (porte K2). Lève ``ContractViolation`` sinon."""
    v1._obj(data, "response", v1.RESPONSE_KEYS)
    if data["schema_version"] != SCHEMA_VERSION:
        v1._fail("schema_version", "'2' expected (no V1 fallback on the V2 rail)")
    if data["status"] != "ok":
        v1._fail("status", "'ok' expected")
    if data["provider"] != "ai_gateway":
        v1._fail("provider", "'ai_gateway' expected")
    v1._str(data["upstream_provider"], "upstream_provider", non_empty=True)
    v1._enum(data["task"], "task", v1.TASKS)
    if expected_task is not None and data["task"] != expected_task:
        v1._fail("task", "does not match the requested task")
    v1._enum(data["visual_kind"], "visual_kind", v1.VISUAL_KINDS)
    v1._str(data["request_id"], "request_id", nullable=True)
    if expected_request_id is not None and data["request_id"] != expected_request_id:
        v1._fail("request_id", "does not match request")
    v1._str(data["summary"], "summary", nullable=True)
    v1._warnings(data["warnings"], "warnings")
    v1._bool(data["requires_visual_check"], "requires_visual_check")
    v1._identity(data["model"], "model", v1.MODEL_KEYS)
    _hex64(data["model"]["digest"], "model.digest")
    _validate_pipeline(data["pipeline"])
    _validate_image(data["image"])
    usage = v1._obj(data["usage"], "usage", USAGE_KEYS)
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        v1._int(usage[key], f"usage.{key}", minimum=0, nullable=True)
    v1._int(usage["attempts"], "usage.attempts", minimum=1)
    v1._num(data["latency_ms"], "latency_ms")
    if data["latency_ms"] < 0:
        v1._fail("latency_ms", "non-negative number expected")
    if data["truncated"] is not False:
        v1._fail("truncated", "a successful response is never truncated")

    kind = data["visual_kind"]
    if kind == "unknown":
        if data["payload"] is not None:
            v1._fail("payload", "null expected for unknown")
        if "insufficient_visual_evidence" not in data["warnings"]:
            v1._fail("warnings", "unknown requires insufficient_visual_evidence")
        if data["requires_visual_check"] is not True:
            v1._fail("requires_visual_check", "unknown requires visual check")
    elif kind == "decorative":
        if data["payload"] is not None:
            v1._fail("payload", "null expected for decorative")
        if data["warnings"]:
            v1._fail("warnings", "decorative carries no warning")
        if data["requires_visual_check"] is not False:
            v1._fail("requires_visual_check", "decorative requires no visual check")
    else:
        v1._PAYLOAD_VALIDATORS[kind](data["payload"], "payload")
    return data


def validate_request(payload: Any) -> None:
    """Garde locale du corps V2 : mêmes clés que V1, ``schema_version == "2"``, PNG."""
    if not isinstance(payload, dict) or not set(payload) <= REQUEST_KEYS:
        v1._fail("request", "fields outside the vision request schema")
    for key in ("schema_version", "task", "locale", "image", "context", "request_id"):
        if key not in payload:
            v1._fail(f"request.{key}", "required")
    if payload["schema_version"] != SCHEMA_VERSION:
        v1._fail("request.schema_version", "'2' required on the V2 rail")
    v1._enum(payload["task"], "request.task", v1.TASKS)
    v1._enum(payload["locale"], "request.locale", v1.LOCALES)
    if "model" in payload:
        v1._str(payload["model"], "request.model", non_empty=True)
    v1._str(payload["request_id"], "request.request_id", non_empty=True)
    if len(payload["request_id"]) > v1.REQUEST_ID_MAX_CHARS:
        v1._fail("request.request_id", "too long")
    v1._obj(payload["image"], "request.image", ("data", "mime_type"))
    v1._str(payload["image"]["data"], "request.image.data", non_empty=True)
    if payload["image"]["mime_type"] != "image/png":
        v1._fail("request.image.mime_type", "crops are PNG (png-rgb8-zl6/1)")
    v1._obj(payload["context"], "request.context", ("caption", "alt_text", "nearby_text"))
    for key in ("caption", "alt_text", "nearby_text"):
        v1._str(payload["context"][key], f"request.context.{key}", nullable=True)
