"""Résolution de l'état EFFECTIF des capacités documentaires.

Lecture seule. Chaque porte appelle un accesseur existant (``extraction/config.py``) ou un
résolveur de portée existant (``audio_admitted_for``, ``email_admitted_for``,
``pdf_vector_regions_enabled_for``, ``pdf_generic_tables_decision``, ``structured.check_scope``,
``curve_shadow.shadow_refusal``, ``orchestration.scope_gate``) : aucune règle d'autorisation n'est
réécrite ici. Une porte qui échoue donne ``UNKNOWN`` — jamais ``ACTIVE``, jamais ``DISABLED``.
"""
from __future__ import annotations

import time
import uuid
from types import SimpleNamespace
from typing import Callable, Optional

from django.conf import settings

from . import gateway_health, runtime
from .registry import CAPABILITIES, FORMATS, MATRIX_COLUMNS, NOT_PRODUCED, CapabilitySpec

ACTIVE_GLOBAL, ACTIVE_SCOPED, ACTIVE_CONDITIONAL = "ACTIVE_GLOBAL", "ACTIVE_SCOPED", "ACTIVE_CONDITIONAL"
DISABLED, UNSUPPORTED, UNAVAILABLE, INCONSISTENT, UNKNOWN = "DISABLED", "UNSUPPORTED", "UNAVAILABLE", "INCONSISTENT", "UNKNOWN"
STATES = (ACTIVE_GLOBAL, ACTIVE_SCOPED, ACTIVE_CONDITIONAL, DISABLED, UNSUPPORTED, UNAVAILABLE, INCONSISTENT, UNKNOWN)
_ACTIVE = (ACTIVE_GLOBAL, ACTIVE_SCOPED, ACTIVE_CONDITIONAL)
_RANK = {ACTIVE_GLOBAL: 0, ACTIVE_CONDITIONAL: 1, ACTIVE_SCOPED: 2, INCONSISTENT: 3, UNAVAILABLE: 4, UNKNOWN: 5,
         DISABLED: 6, UNSUPPORTED: 7}

#: Formats sur lesquels s'appliquent les capacités « visual » (StructX, Vision V2, schémas).
VISUAL_FORMATS = ("pdf", "docx", "pptx", "image")


def _cfg():
    from apps.rag_core.services.ingest.extraction import config

    return config


def _bool(name: str) -> bool:
    v = getattr(settings, name, False)
    return v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")


def _structured():
    from apps.rag_core.services.ingest.visual import structured

    return structured


def _structx_gate(task: str) -> bool:
    s = _structured()
    return (_bool("STRUCTURED_VISUAL_ENABLED") and s.environment_refusal() is None and task in s.allowed_tasks()
            and _cfg().visual_analysis_enabled())


def _v2_gate() -> bool:
    from apps.rag_core.services.raster import orchestration

    if not orchestration.feature_enabled():
        return False
    return not (orchestration.is_production() and not _bool("VISUAL_V2_PRODUCTION_ALLOWED"))


def _image_optin() -> bool:
    c = _cfg()
    return c.image_ingest_enabled() and bool({"gif", "bmp"} & set(c.image_formats()))


def _curve_gate() -> bool:
    env = str(getattr(settings, "DJANGO_ENV", "") or "").strip().lower()
    return env not in ("prod", "production") and _bool("STRUCTURED_VISUAL_ENABLED") and _bool("STRUCTURED_VISUAL_CURVE_SHADOW_ENABLED")


#: Portes (nom → fonction sans argument). Toute porte référencée par le registre doit exister ici.
GATES: dict = {
    "always": lambda: True,
    "never": lambda: False,
    "text_fallback": lambda: True,
    "pptx": lambda: _cfg().pptx_enabled(),
    "excel": lambda: _cfg().excel_enabled(),
    "image": lambda: _cfg().image_ingest_enabled(),
    "image_optin": _image_optin,
    "msg": lambda: _cfg().msg_enabled(),
    "eml": lambda: _cfg().eml_enabled(),
    "audio": lambda: _cfg().audio_ingest_enabled(),
    "xml": lambda: _cfg().xml_enabled(),
    "dxf": lambda: _cfg().dxf_enabled(),
    "dwg": lambda: _cfg().dwg_enabled(),
    "ifc": lambda: _cfg().ifc_enabled(),
    "ocr": lambda: _cfg().ocr_enabled(),
    "table_ocr": lambda: _cfg().table_ocr_enabled(),
    "pdf_generic_tables": lambda: _cfg().pdf_generic_tables_enabled(),
    "visual_image": lambda: _cfg().visual_image_analysis_active(),
    "visual_pdf": lambda: _cfg().visual_pdf_analysis_active(),
    "visual_docx": lambda: _cfg().visual_docx_analysis_active(),
    "visual_pptx": lambda: _cfg().visual_pptx_analysis_active(),
    "pdf_vector": lambda: _bool("DOCUMENT_PDF_VECTOR_REGION_RENDER_ENABLED") and _cfg().visual_pdf_analysis_active(),
    "office_charts": lambda: _cfg().office_chart_data_enabled(),
    "pptx_notes": lambda: _cfg().pptx_enabled() and _cfg().pptx_include_notes(),
    "structx_table": lambda: _structx_gate("table"),
    "structx_chart": lambda: _structx_gate("chart"),
    "curve_shadow": _curve_gate,
    "vision_v2": _v2_gate,
    "mineru": lambda: _bool("DOCUMENT_MINERU_HYBRID_ENABLED"),
    "email_attachments": lambda: _cfg().email_attachment_ingest_enabled(),
    "tabular_query": lambda: _bool("TABULAR_QUERY_ENABLED"),
    "reranker": lambda: _bool("RAG_RERANKER_ENABLED"),
}


def _structx_space(space) -> bool:
    try:
        _structured().check_scope(SimpleNamespace(rag_space_id=space))
        return True
    except Exception:  # noqa: BLE001 - refus nommé
        return False


def _curve_space(space) -> bool:
    from apps.rag_core.services.ingest.visual import curve_shadow

    return curve_shadow.shadow_refusal(space) is None


def _v2_space(space) -> bool:
    from apps.rag_core.services.raster import orchestration

    return orchestration.scope_gate(space).allowed


#: Portées : liste lue, mode général éventuel, décision par espace (résolveur existant).
SCOPES: dict = {
    "email": {"list": "DOCUMENT_EMAIL_RAGSPACE_IDS", "global": lambda: _cfg().email_global_enabled(),
              "decide": lambda s: _cfg().email_admitted_for(s)},
    "audio": {"list": "DOCUMENT_AUDIO_RAGSPACE_IDS", "global": lambda: _cfg().audio_ingest_enabled() and _cfg().audio_global_enabled(),
              "decide": lambda s: _cfg().audio_admitted_for(s)},
    "pdf_vector": {"list": "DOCUMENT_PDF_VECTOR_REGION_RAGSPACE_IDS", "global": None,
                   "decide": lambda s: _cfg().pdf_vector_regions_enabled_for(s) and _cfg().visual_pdf_analysis_active()},
    "pdf_generic_tables": {"list": "DOCUMENT_PDF_GENERIC_TABLES_ALLOWED_RAGSPACE_IDS", "global": None,
                           "decide": lambda s: bool(_cfg().pdf_generic_tables_decision(s)[0])},
    "structx": {"list": "STRUCTURED_VISUAL_RAGSPACE_ALLOWLIST", "global": None, "decide": _structx_space},
    "curve_shadow": {"list": "STRUCTURED_VISUAL_CURVE_SHADOW_RAGSPACE_ALLOWLIST", "global": None, "decide": _curve_space},
    "vision_v2": {"list": "VISUAL_V2_RAGSPACE_ALLOWLIST", "global": None, "decide": _v2_space},
}


def _safe(fn: Callable, *args):
    try:
        return fn(*args), ""
    except Exception as exc:  # noqa: BLE001 - jamais de valeur inventée
        return None, type(exc).__name__


def _listed(setting: str) -> tuple:
    raw = getattr(settings, setting, None) or []
    if isinstance(raw, str):
        raw = raw.split(",")
    valid, invalid = [], 0
    for item in raw:
        item = str(item).strip()
        if not item:
            continue
        try:
            valid.append(str(uuid.UUID(item)))
        except ValueError:
            invalid += 1
    return valid, invalid


def scope_summary(name: str, *, live_space_ids: Optional[list] = None) -> dict:
    """Portée d'une capacité : compteurs seulement (aucun UUID ni nom d'espace)."""
    from apps.rag_core.models import RagSpace

    sc = SCOPES[name]
    listed, invalid = _listed(sc["list"])
    existing = list(RagSpace.objects.filter(id__in=listed, deleted_at__isnull=True).values_list("id", flat=True))
    is_global, err = _safe(sc["global"]) if sc["global"] else (False, "")
    admitted = sum(1 for s in existing if _safe(sc["decide"], s)[0] is True)
    total = RagSpace.objects.filter(deleted_at__isnull=True).count()
    return {"scope": name, "list_setting": sc["list"], "global_mode": bool(is_global), "global_mode_error": err,
            "listed": len(listed) + invalid, "invalid_entries": invalid, "existing": len(existing),
            "stale_entries": len(listed) - len(existing), "admitted_spaces": total if is_global else admitted,
            "total_spaces": total}


def _worst(*statuses: str) -> str:
    order = list(gateway_health.STATUS_ORDER)
    present = [s for s in statuses if s in order]
    return min(present, key=order.index) if present else gateway_health.AVAILABLE


def _runtime_for(spec: CapabilitySpec, gw: dict, roles: dict) -> tuple:
    """Disponibilité : service Gateway, rôle exécutant, dépendance locale. Aucun appel métier."""
    reasons = []
    gw_status = None
    if spec.gateway:
        svc = gw.get(spec.gateway.service) or {}
        gw_status = svc.get("status") or gateway_health.UNKNOWN
        if spec.gateway.service == "vision" and gw_status == gateway_health.AVAILABLE and svc.get("describe_available") is False:
            gw_status = gateway_health.DEGRADED
        if spec.gateway.call_type == "DEPENDS_ON_RAGSPACE":
            gw_status = None                       # résolu par espace (embeddings)
        elif gw_status == gateway_health.DECLARED:
            reasons.append(f"gateway_{spec.gateway.service}_declared_not_tested")
        elif gw_status != gateway_health.AVAILABLE:
            reasons.append(f"gateway_{spec.gateway.service}_{gw_status.lower()}")
    role = spec.executing_service
    role_status = gateway_health.AVAILABLE
    if role != "backend":
        info = roles.get(role)
        if info is None:
            role_status = gateway_health.UNKNOWN
            reasons.append(f"{role}_not_observed")
        elif info.get("state") != runtime.STATE_FRESH:
            role_status = gateway_health.UNKNOWN
            reasons.append(f"{role}_stale")
    lib_status = None
    if spec.format_id == "msg" and spec.gate == "msg":
        from apps.rag_core.services.ingest.extraction import msg_capability

        lib_status = gateway_health.UNAVAILABLE if msg_capability().get("status") == "dependency_missing" else None
        if lib_status:
            reasons.append("msg_parser_missing")
    return _worst(gw_status, role_status, lib_status), reasons


def _numeric(cls: str) -> Optional[bool]:
    from apps.rag_core.services.query import numeric_evidence as NE

    if cls in NE.RELIABLE:
        return True
    if cls in NE.UNVERIFIED:
        return False
    return None


def resolve_capability(spec: CapabilitySpec, *, gw: dict, consistency: dict, scopes: dict, space_id=None) -> dict:
    warnings = []
    if spec.implemented == "NO" or spec.gate == "never":
        enabled, err = False, ""
        state = UNSUPPORTED
    else:
        enabled, err = _safe(GATES[spec.gate])
        if enabled is None:
            state = UNKNOWN
        elif not enabled:
            state = DISABLED
        else:
            state = ACTIVE_GLOBAL
    scope = scopes.get(spec.scope) if spec.scope else None
    activation_scope = "GLOBAL"
    if spec.scope:
        if scope is None:
            activation_scope, state = "UNKNOWN", (UNKNOWN if state == ACTIVE_GLOBAL else state)
        elif scope["global_mode"]:
            activation_scope = "GLOBAL"
        else:
            activation_scope = "SCOPED"
            if state == ACTIVE_GLOBAL:
                if scope["admitted_spaces"] > 0:
                    state = ACTIVE_SCOPED
                else:
                    state = INCONSISTENT
                    warnings.append({"code": "SCOPE_EMPTY", "capability_id": spec.capability_id, "flag": scope["list_setting"]})
    if state in (DISABLED, UNSUPPORTED):
        activation_scope = "NONE"
    if state == ACTIVE_GLOBAL and spec.trigger != "always":
        state = ACTIVE_CONDITIONAL
    runtime_status, reasons = _runtime_for(spec, gw, consistency.get("roles") or {})
    if state in _ACTIVE and runtime_status == gateway_health.UNAVAILABLE:
        state = UNAVAILABLE
    mismatched = [f for f in spec.flags if (consistency.get("flags") or {}).get(f, {}).get("status") == "CONFIGURATION_MISMATCH"]
    if mismatched and state not in (UNSUPPORTED,):
        state = INCONSISTENT
        warnings.extend({"code": "CONFIGURATION_MISMATCH", "capability_id": spec.capability_id, "flag": f} for f in mismatched)
    space_applicable = None
    if space_id is not None and state not in (UNSUPPORTED, DISABLED, UNKNOWN):
        if spec.scope and spec.scope in SCOPES:
            space_applicable = _safe(SCOPES[spec.scope]["decide"], space_id)[0]
        else:
            space_applicable = True
    gw_dep = None
    if spec.gateway:
        g = spec.gateway
        svc = gw.get(g.service) or {}
        model = getattr(settings, g.model_setting, None) if g.model_setting else svc.get("model_logical")
        gw_dep = {"uses_ai_gateway": True, "gateway_service": g.service, "gateway_route": g.route,
                  "gateway_call_type": g.call_type, "stage": g.stage, "gateway_model_setting": g.model_setting,
                  "model_logical": str(model) if model else None, "model_concrete": svc.get("model_concrete"),
                  "service_status": svc.get("status") or gateway_health.UNKNOWN}
    return {
        "id": spec.capability_id, "format_id": spec.format_id, "category": spec.category,
        "label": spec.label_fr, "description": spec.description_fr, "content_type": spec.content_type,
        "implemented": spec.implemented, "configured_enabled": enabled, "configuration_error": err,
        "state": state, "activation_scope": activation_scope,
        "scope": ({k: scope[k] for k in ("admitted_spaces", "listed", "existing", "stale_entries", "global_mode", "list_setting")}
                  if scope else None),
        "content_trigger": spec.trigger, "content_trigger_conditional": spec.trigger != "always",
        "runtime_available": runtime_status, "runtime_reasons": reasons,
        "executing_service": spec.executing_service, "consumers": list(spec.consumers),
        "gateway_dependency": gw_dep or {"uses_ai_gateway": False, "gateway_call_type": "LOCAL_ONLY"},
        "output_type": spec.output_type, "output_indexed": spec.output_indexed,
        "output_searchable": spec.output_indexed == "INDEXED" and state in _ACTIVE,
        "citation_support": spec.citation, "numeric_evidence_class": spec.numeric_evidence_class,
        "numeric_evidence_reliable": _numeric(spec.numeric_evidence_class),
        "qualification_status": spec.qualification_status, "qualification_evidence": spec.qualification_evidence,
        "limitations": list(spec.limitations), "flags": list(spec.flags), "space_applicable": space_applicable,
        "_warnings": warnings,
    }


def _max_bytes(setting: Optional[str]) -> Optional[int]:
    c = _cfg()
    if setting is None:
        return None
    if setting == "DOCUMENT_AUDIO_MAX_UPLOAD_BYTES":
        return c.audio_max_upload_bytes()
    if setting == "DOCUMENT_IMAGE_MAX_BYTES":
        return c.image_max_bytes() or c.max_upload_size_bytes()
    if setting == "DOCUMENT_MSG_MAX_FILE_SIZE_MB":
        return c.msg_max_file_bytes()
    if setting == "DOCUMENT_EML_MAX_FILE_SIZE_MB":
        return c.eml_max_file_bytes()
    return c.max_upload_size_bytes()


def _format_row(spec, caps_by_format: dict, scopes: dict) -> dict:
    if spec.gate == "never":
        status, enabled = "UNSUPPORTED", False
    elif spec.gate == "text_fallback":
        status, enabled = "TEXT_FALLBACK", True
    else:
        enabled, _ = _safe(GATES[spec.gate])
        if enabled is None:
            status = "UNKNOWN"
        elif not enabled:
            status = "DISABLED"
        else:
            status = "SUPPORTED" if spec.gate == "always" else "CONDITIONALLY_SUPPORTED"
    upload = next((c for c in caps_by_format.get(spec.format_id, []) if c["category"] == "upload"), None)
    if upload and upload["state"] in (UNAVAILABLE, INCONSISTENT, UNKNOWN) and status not in ("UNSUPPORTED",):
        status = upload["state"]
    return {"format_id": spec.format_id, "label": spec.label_fr, "family": spec.family, "extensions": list(spec.extensions),
            "status": status, "upload_enabled": enabled, "extractor": spec.extractor, "registry_key": spec.registry_key,
            "executing_service": spec.executing_service, "refusal_code": spec.refusal_code,
            "max_upload_bytes": _max_bytes(spec.max_bytes_setting) if status not in ("UNSUPPORTED",) else None,
            "scope": (upload or {}).get("scope"), "notes": list(spec.notes), "in_matrix": spec.matrix}


def _caps_for_format(fmt: str, caps: list) -> list:
    return [c for c in caps if c["format_id"] == fmt or (c["format_id"] == "visual" and fmt in VISUAL_FORMATS)]


def _matrix(formats: list, caps: list) -> list:
    rows = []
    for f in formats:
        if not f["in_matrix"]:
            continue
        own = _caps_for_format(f["format_id"], caps)
        cells = {}
        for col in MATRIX_COLUMNS:
            items = [c for c in own if c["category"] == col]
            state = min((c["state"] for c in items), key=_RANK.get) if items else "NOT_APPLICABLE"
            cells[col] = {"state": state, "items": [{"id": c["id"], "label": c["label"], "state": c["state"]} for c in items]}
        services = sorted({c["gateway_dependency"]["gateway_service"] for c in own
                           if c["gateway_dependency"].get("uses_ai_gateway") and c["state"] in _ACTIVE})
        cells["gateway"] = {"state": "USED" if services else "NOT_USED", "services": services}
        rows.append({"format_id": f["format_id"], "label": f["label"], "format_status": f["status"], "cells": cells})
    return rows


def embedding_providers(gw: dict, *, space=None) -> dict:
    from django.db.models import Count

    from apps.rag_core.models import RagSpace

    qs = RagSpace.objects.filter(deleted_at__isnull=True)
    groups = list(qs.values("embedding_provider", "embedding_model").annotate(n=Count("id")).order_by("-n"))
    gw_status = (gw.get("embeddings") or {}).get("status") or gateway_health.UNKNOWN
    counts = {"ai_gateway": 0, "ollama_direct": 0, "other": 0, "unknown": 0}
    providers = []
    for g in groups:
        provider = str(g["embedding_provider"] or "").strip().lower()
        key = {"ai_gateway": "ai_gateway", "ollama": "ollama_direct"}.get(provider, "other" if provider else "unknown")
        counts[key] += g["n"]
        providers.append({"provider": provider or None, "model": g["embedding_model"], "spaces": g["n"],
                          "call_type": {"ai_gateway": "DIRECT_GATEWAY", "ollama_direct": "OTHER_PROVIDER"}.get(key, "OTHER_PROVIDER" if key == "other" else "UNKNOWN"),
                          "service_status": gw_status if key == "ai_gateway" else gateway_health.UNKNOWN})
    out = {"total_spaces": sum(g["n"] for g in groups), "ai_gateway_spaces": counts["ai_gateway"],
           "ollama_direct_spaces": counts["ollama_direct"], "other_provider_spaces": counts["other"],
           "unknown_provider_spaces": counts["unknown"], "providers": providers,
           "gateway_fallback_enabled": _bool("AI_GATEWAY_FALLBACK_ENABLED"),
           "default_for_new_spaces": {"provider": getattr(settings, "DEFAULT_RAGSPACE_EMBEDDING_PROVIDER", None),
                                      "model": getattr(settings, "DEFAULT_RAGSPACE_EMBEDDING_MODEL", None)}}
    if space is not None:
        out["selected_space"] = {"provider": space.embedding_provider, "model": space.embedding_model,
                                 "via_ai_gateway": str(space.embedding_provider or "").lower() == "ai_gateway"}
    return out


def build_snapshot(*, rag_space=None, refresh: bool = False) -> dict:
    """Instantané complet, sans aucune écriture métier ni inférence."""
    started = time.time()
    gw_snap = gateway_health.snapshot(refresh=refresh)
    gw = gw_snap.get("services") or {}
    consistency = runtime.consistency(runtime.read_all())
    scopes = {}
    for name in SCOPES:
        value, err = _safe(scope_summary, name)
        if value is not None:
            scopes[name] = value
    space_id = str(rag_space.id) if rag_space is not None else None
    caps = [resolve_capability(s, gw=gw, consistency=consistency, scopes=scopes, space_id=space_id) for s in CAPABILITIES]
    by_format: dict = {}
    for c in caps:
        by_format.setdefault(c["format_id"], []).append(c)
    formats = [_format_row(f, by_format, scopes) for f in FORMATS]
    warnings = [w for c in caps for w in c.pop("_warnings")]
    for name, sc in scopes.items():
        if sc["stale_entries"] or sc["invalid_entries"]:
            warnings.append({"code": "SCOPE_STALE_ENTRY", "scope": name, "flag": sc["list_setting"],
                             "detail": f"{sc['stale_entries']} espace(s) listé(s) inexistant(s), {sc['invalid_entries']} entrée(s) invalide(s)"})
    for role, info in (consistency.get("roles") or {}).items():
        if info.get("state") == runtime.STATE_STALE:
            warnings.append({"code": "WORKER_STALE", "service": role, "detail": f"dernière empreinte il y a {info.get('age_seconds')} s"})
    for role in consistency.get("deployment_sha_mismatches") or []:
        warnings.append({"code": "DEPLOYMENT_SHA_MISMATCH", "service": role})
    if consistency.get("status") == "UNKNOWN":
        warnings.append({"code": "UNKNOWN", "detail": "empreintes des workers illisibles (Redis)"})
    for name, svc in gw.items():
        if svc.get("status") in (gateway_health.UNAVAILABLE, gateway_health.DEGRADED):
            warnings.append({"code": "SERVICE_UNAVAILABLE" if svc["status"] == gateway_health.UNAVAILABLE else "SERVICE_DEGRADED",
                             "service": name, "detail": svc.get("reason", "")})
    state_counts = {s: sum(1 for c in caps if c["state"] == s) for s in STATES}
    return {
        "schema_version": 1,
        "environment": runtime.environment(),
        "deployment_sha": runtime.deployment_sha(),
        "generated_at": started,
        "data_freshness": {"gateway_probed_at": gw_snap.get("probed_at"), "gateway_cached": gw_snap.get("cached"),
                           "gateway_paths_read": gw_snap.get("paths_read") or [],
                           "gateway_runtime": gw_snap.get("gateway_runtime") or {},
                           "gateway_cache_ttl_seconds": gateway_health.CACHE_TTL_SECONDS,
                           "worker_stale_after_seconds": runtime.STALE_AFTER_SECONDS},
        "summary": {"formats": len(formats), "extensions": sum(len(f["extensions"]) for f in formats),
                    "capabilities": len(caps), "states": state_counts, "warnings": len(warnings)},
        "formats": formats,
        "capabilities": caps,
        "matrix": _matrix(formats, caps),
        "gateway_services": [{"service": k, **{kk: vv for kk, vv in v.items() if kk != "service"}} for k, v in gw.items()],
        "scope_summary": scopes,
        "embeddings": embedding_providers(gw, space=rag_space),
        "runtime_consistency": consistency,
        "warnings": warnings,
        "ragspace_view": ({"id": space_id, "name": rag_space.name} if rag_space is not None else None),
        "read_only": True,
    }
