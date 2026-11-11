# backend/api/v1/admin_core_config/views.py
"""Admin RagATM_Core — endpoints LECTURE SEULE (PR-A).

  GET /api/v1/admin/core-config/effective/   -> config effective (env-only, redacted)
  GET /api/v1/admin/core-config/health/      -> santé Core/Gateway (sans appel réseau)

Réservés au propriétaire global ragATM (superuser) via IsRagATMOwner. Aucune écriture
n'est exposée : http_method_names est restreint à GET pour garantir l'absence de
PATCH/POST/DELETE. Ces vues ne modifient aucun comportement runtime.
"""
from __future__ import annotations

from django.conf import settings
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsRagATMOwner
from apps.rag_core.core.config_resolver import (
    current_environment,
    is_sensitive_key,
    redact_base_url,
    resolve_effective_config,
    resolve_health,
)
from apps.rag_core.core.config_write import (
    OverrideValidationError,
    set_override,
    unset_override,
)

OWNER_PERMS = [IsAuthenticated, IsRagATMOwner]

_AUDIT_DEFAULT_LIMIT = 50
_AUDIT_MAX_LIMIT = 200
_MAX_REASON_LEN = 2000

# Notice de runtime (PR-D → PR-G4) : DYNAMIQUE selon CORE_CONFIG_RUNTIME_ENABLED.
# - flag off : overlay DB diagnostic-only (le runtime lit les settings) ;
# - flag on  : les overrides DB sont ACTIFS au runtime (chat via RAG_USE_CORE_RETRIEVE,
#   gateway via AI_GATEWAY_*). RAG_EMBEDDING_DIM reste toujours piloté par settings.
def _diagnostic_notice() -> dict:
    runtime_on = bool(getattr(settings, "CORE_CONFIG_RUNTIME_ENABLED", False))
    if runtime_on:
        return {
            "runtime_source_of_truth": "db_overlay",
            "db_overlay_runtime_active": True,
            "diagnostic_overlay_only": False,
            "warning": (
                "DB overrides are ACTIVE at runtime (CORE_CONFIG_RUNTIME_ENABLED=1). "
                "RAG_EMBEDDING_DIM remains driven by settings."
            ),
        }
    return {
        "runtime_source_of_truth": "settings",
        "db_overlay_runtime_active": False,
        "diagnostic_overlay_only": True,
        "warning": "DB overrides are diagnostic-only and do not affect runtime behavior.",
    }


def _effective_block(*, environment, workspace_id=None, rag_space_id=None) -> dict:
    """Bloc 'config effective' réutilisé par GET /effective/ et les réponses d'écriture."""
    entries = resolve_effective_config(
        environment=environment, workspace_id=workspace_id, rag_space_id=rag_space_id
    )
    return {
        "environment": environment,
        "read_only": True,
        **_diagnostic_notice(),
        "config": [entry.as_dict() for entry in entries],
    }


def _editing_gate_error():
    """Gates d'écriture (PR-F) : prod interdit (403), puis flag requis (409).

    Ordre : prod d'abord => refus même si le flag est activé.
    """
    if current_environment() == "prod":
        return Response(
            {"detail": "editing_forbidden_in_prod"}, status=status.HTTP_403_FORBIDDEN
        )
    if not bool(getattr(settings, "CORE_CONFIG_EDIT_ENABLED", False)):
        return Response({"detail": "editing_disabled"}, status=status.HTTP_409_CONFLICT)
    return None


def _safe_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class CoreConfigEffectiveView(APIView):
    """GET /api/v1/admin/core-config/effective/"""

    permission_classes = OWNER_PERMS
    http_method_names = ["get", "head", "options"]

    def get(self, request):
        # Portée optionnelle (overlay DB lecture seule). Sans paramètre :
        # global + environnement courant => identique au comportement env-only.
        ws = request.query_params.get("workspace_id")
        ws_id = _safe_int(ws) if ws not in (None, "") else None
        if ws not in (None, "") and ws_id is None:
            return Response(
                {"detail": "workspace_id must be an integer"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        rs = request.query_params.get("rag_space_id") or None

        env = current_environment()
        return Response(
            _effective_block(environment=env, workspace_id=ws_id, rag_space_id=rs),
            status=status.HTTP_200_OK,
        )


class CoreConfigHealthView(APIView):
    """GET /api/v1/admin/core-config/health/"""

    permission_classes = OWNER_PERMS
    http_method_names = ["get", "head", "options"]

    def get(self, request):
        body = {**resolve_health(), **_diagnostic_notice()}
        body["gateway_health"] = _gateway_health_summary()
        return Response(body, status=status.HTTP_200_OK)


def _gateway_health_summary() -> dict:
    """Santé RÉELLE des services Gateway (sondes GET /health/* en cache 30 s).

    Jamais « ok » sans preuve : un service non sondé, injoignable ou illisible est rapporté
    comme tel ; l'état global est le plus défavorable des services sondés."""
    from apps.rag_core.services.capabilities import gateway_health

    snap = gateway_health.snapshot()
    raw = snap.get("services") or {}
    services = {name: svc.get("status") or gateway_health.UNKNOWN for name, svc in raw.items()}
    # Un service NON SONDABLE sans inférence (StructX) est affiché tel quel mais n'entre pas
    # dans l'état global : l'état global ne porte que sur ce qui a été réellement lu.
    probed = {n: st for n, st in services.items() if (raw.get(n) or {}).get("reason") != "no_non_inferential_probe"}
    order = list(gateway_health.STATUS_ORDER)
    known = [s for s in probed.values() if s in order]
    overall = min(known, key=order.index) if known else gateway_health.UNKNOWN
    # « DECLARED » = publié par un inventaire, inférence non vérifiée : jamais « ok ».
    return {"status": overall, "services": services, "probed_at": snap.get("probed_at"), "cached": snap.get("cached"),
            "runtime": (snap.get("gateway_runtime") or {}).get("status"), "verification": "non_inferential_reads_only",
            "probes_enabled": snap.get("probes_enabled", True)}


class DocumentCapabilitiesView(APIView):
    """GET /api/v1/admin/core-config/document-capabilities/[?ragspace_id=<uuid>][&refresh=1]

    Capacités documentaires EFFECTIVES de l'environnement interrogé.
    Superuser actif seulement ; LECTURE SEULE (GET/HEAD/OPTIONS) ; aucune ingestion, aucune
    inférence : seules les sondes GET /health/* d'AI Gateway sont appelées, en cache.
    """

    permission_classes = OWNER_PERMS
    http_method_names = ["get", "head", "options"]

    #: Débit du bouton « Actualiser » (contournement du cache des sondes), par utilisateur.
    REFRESH_MIN_INTERVAL_SECONDS = 10

    def get(self, request):
        import uuid as _uuid

        from django.core.cache import cache

        from apps.rag_core.models import RagSpace
        from apps.rag_core.services.capabilities.resolver import build_snapshot

        raw = (request.query_params.get("ragspace_id") or request.query_params.get("rag_space_id") or "").strip()
        space = None
        if raw:
            try:
                space_id = _uuid.UUID(raw)
            except ValueError:
                return Response({"detail": "ragspace_id_invalid"}, status=status.HTTP_400_BAD_REQUEST)
            space = RagSpace.objects.filter(id=space_id, deleted_at__isnull=True).only(
                "id", "name", "embedding_provider", "embedding_model").first()
            if space is None:
                return Response({"detail": "ragspace_not_found"}, status=status.HTTP_404_NOT_FOUND)
        refresh = str(request.query_params.get("refresh") or "") in ("1", "true")
        if refresh:
            try:
                refresh = bool(cache.add(f"ragatm:capabilities:refresh:{request.user.pk}", 1,
                                         self.REFRESH_MIN_INTERVAL_SECONDS))
            except Exception:  # noqa: BLE001
                refresh = False
        resp = Response(build_snapshot(rag_space=space, refresh=refresh), status=status.HTTP_200_OK)
        resp["Cache-Control"] = "no-store"
        return resp


def _serialize_llm_change(row) -> dict:
    """Sérialise un LlmSettingsChangeLog (aucun secret n'y est stocké).

    `user` est exposé par username (pas d'email/PII). Tous les champs sont des
    noms de provider/modèle, jamais de clé/token/prompt.
    """
    user = getattr(row, "user", None)
    return {
        "id": str(row.id),
        "created_at": row.created_at,
        "user": getattr(user, "username", None) if user else None,
        "user_id": getattr(user, "id", None) if user else None,
        "workspace_id": row.workspace_id,
        "rag_space_id": str(row.rag_space_id) if row.rag_space_id else None,
        "chat": {
            "old_provider": row.old_provider,
            "old_model": row.old_model,
            "new_provider": row.new_provider,
            "new_model": row.new_model,
        },
        "embeddings": {
            "old_provider": row.old_embedding_provider,
            "old_model": row.old_embedding_model,
            "new_provider": row.new_embedding_provider,
            "new_model": row.new_embedding_model,
        },
    }


class CoreConfigAuditView(APIView):
    """GET /api/v1/admin/core-config/audit/

    Historique LECTURE SEULE des changements de réglages LLM (chat + embeddings),
    réutilisant le journal existant ``LlmSettingsChangeLog``. Filtres optionnels :
    ``workspace_id`` (int), ``rag_space_id`` (uuid). Pagination ``limit``/``offset``.
    """

    permission_classes = OWNER_PERMS
    http_method_names = ["get", "head", "options"]

    def get(self, request):
        from apps.audit.models import LlmSettingsChangeLog

        qs = LlmSettingsChangeLog.objects.select_related("user").order_by("-created_at")

        ws = request.query_params.get("workspace_id")
        if ws not in (None, ""):
            ws_id = _safe_int(ws)
            if ws_id is None:
                return Response(
                    {"detail": "workspace_id must be an integer"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            qs = qs.filter(workspace_id=ws_id)

        rs = request.query_params.get("rag_space_id")
        if rs not in (None, ""):
            qs = qs.filter(rag_space_id=rs)

        limit = _safe_int(request.query_params.get("limit"), _AUDIT_DEFAULT_LIMIT)
        limit = max(1, min(limit or _AUDIT_DEFAULT_LIMIT, _AUDIT_MAX_LIMIT))
        offset = _safe_int(request.query_params.get("offset"), 0)
        offset = max(0, offset or 0)

        total = qs.count()
        rows = list(qs[offset:offset + limit])

        return Response(
            {
                "count": total,
                "limit": limit,
                "offset": offset,
                "results": [_serialize_llm_change(r) for r in rows],
            },
            status=status.HTTP_200_OK,
        )


def _redact_value(key: str, value: str) -> str:
    """Redacte la valeur d'une clé sensible (ex. AI_GATEWAY_BASE_URL)."""
    if not value:
        return value
    if is_sensitive_key(key):
        return redact_base_url(value) if key == "AI_GATEWAY_BASE_URL" else "***"
    return value


def _serialize_config_event(row) -> dict:
    """Sérialise un CoreConfigAuditEvent. Valeurs sensibles redactées ; actor par
    username (pas d'email). Jamais de secret en clair."""
    user = getattr(row, "actor", None)
    sensitive = is_sensitive_key(row.key)
    return {
        "id": str(row.id),
        "created_at": row.created_at,
        "action": row.action,
        "key": row.key,
        "scope_type": row.scope_type,
        "scope_ref": row.scope_ref,
        "environment": row.environment,
        "actor": getattr(user, "username", None) if user else None,
        "actor_id": getattr(user, "id", None) if user else None,
        "old_value": _redact_value(row.key, row.old_value),
        "new_value": _redact_value(row.key, row.new_value),
        "redacted": sensitive,
        "reason": row.reason,
    }


class CoreConfigAuditEventsView(APIView):
    """GET /api/v1/admin/core-config/config-audit/

    Journal LECTURE SEULE des événements de configuration Core (set/unset/rollback/
    test_gateway). Vide tant qu'aucune édition n'a eu lieu (PR-F). Pagination
    ``limit``/``offset`` ; valeurs sensibles redactées.
    """

    permission_classes = OWNER_PERMS
    http_method_names = ["get", "head", "options"]

    def get(self, request):
        from apps.rag_core.models import CoreConfigAuditEvent

        qs = CoreConfigAuditEvent.objects.select_related("actor").order_by("-created_at")

        limit = _safe_int(request.query_params.get("limit"), _AUDIT_DEFAULT_LIMIT)
        limit = max(1, min(limit or _AUDIT_DEFAULT_LIMIT, _AUDIT_MAX_LIMIT))
        offset = _safe_int(request.query_params.get("offset"), 0)
        offset = max(0, offset or 0)

        total = qs.count()
        rows = list(qs[offset:offset + limit])
        return Response(
            {
                "count": total,
                "limit": limit,
                "offset": offset,
                **_diagnostic_notice(),
                "results": [_serialize_config_event(r) for r in rows],
            },
            status=status.HTTP_200_OK,
        )


def _parse_write_payload(request):
    """Extrait/normalise le corps commun aux écritures. Lève une Response 400 si
    `reason` est trop longue (renvoyée telle quelle par l'appelant)."""
    data = request.data or {}
    reason = data.get("reason") or ""
    if len(str(reason)) > _MAX_REASON_LEN:
        return None, Response({"detail": "reason_too_long"}, status=status.HTTP_400_BAD_REQUEST)
    parsed = {
        "scope_type": (data.get("scope_type") or "").strip(),
        "scope_ref": data.get("scope_ref"),
        "key": (data.get("key") or "").strip(),
        "value": data.get("value"),
        "reason": str(reason),
    }
    return parsed, None


class CoreConfigOverrideWriteView(APIView):
    """PUT /api/v1/admin/core-config/override/ — crée/met à jour un override (staging only)."""

    permission_classes = OWNER_PERMS
    http_method_names = ["put", "options"]

    def put(self, request):
        gate = _editing_gate_error()
        if gate is not None:
            return gate
        parsed, err = _parse_write_payload(request)
        if err is not None:
            return err

        try:
            override, event = set_override(
                actor=request.user,
                scope_type=parsed["scope_type"],
                scope_ref=parsed["scope_ref"],
                key=parsed["key"],
                value=parsed["value"],
                reason=parsed["reason"],
            )
        except OverrideValidationError as e:
            return Response({"detail": e.code}, status=e.http_status)

        env = current_environment()
        ws = override.scope_ref if override.scope_type == "workspace" else None
        rs = override.scope_ref if override.scope_type == "ragspace" else None
        return Response(
            {
                "ok": True,
                "action": "set",
                "override": {
                    "scope_type": override.scope_type,
                    "scope_ref": override.scope_ref,
                    "key": override.key,
                    "value": _redact_value(override.key, override.value),
                    "source": f"db:{override.scope_type}",
                },
                "event_id": str(event.id),
                "effective": _effective_block(environment=env, workspace_id=ws, rag_space_id=rs),
                **_diagnostic_notice(),
            },
            status=status.HTTP_200_OK,
        )


class CoreConfigOverrideUnsetView(APIView):
    """POST /api/v1/admin/core-config/override/unset/ — supprime un override (staging only)."""

    permission_classes = OWNER_PERMS
    http_method_names = ["post", "options"]

    def post(self, request):
        gate = _editing_gate_error()
        if gate is not None:
            return gate
        parsed, err = _parse_write_payload(request)
        if err is not None:
            return err

        try:
            event = unset_override(
                actor=request.user,
                scope_type=parsed["scope_type"],
                scope_ref=parsed["scope_ref"],
                key=parsed["key"],
                reason=parsed["reason"],
            )
        except OverrideValidationError as e:
            return Response({"detail": e.code}, status=e.http_status)

        env = current_environment()
        ws = parsed["scope_ref"] if parsed["scope_type"] == "workspace" else None
        rs = parsed["scope_ref"] if parsed["scope_type"] == "ragspace" else None
        return Response(
            {
                "ok": True,
                "action": "unset",
                "event_id": str(event.id),
                "effective": _effective_block(environment=env, workspace_id=ws, rag_space_id=rs),
                **_diagnostic_notice(),
            },
            status=status.HTTP_200_OK,
        )
