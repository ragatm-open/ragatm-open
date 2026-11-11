"""Service central des quotas documentaires par Workspace.

POINT UNIQUE de vérité du calcul et de l'enforcement des quotas (nombre de
fichiers + octets cumulés). Aucune règle dupliquée ailleurs.

Politique d'usage (décision projet) — ``quota_consuming_documents`` :
    l'usage = TOUTES les lignes ``Document`` du workspace (Count + Sum(size_bytes)),
    y compris FAILED, car leurs octets sont physiquement stockés sur disque.
    Les suppressions sont dures et immédiates dans ce dépôt (pas de corbeille
    documentaire), donc l'usage est un AGRÉGAT calculé à la demande : aucune table
    compteur, aucune réconciliation.

Résolution des défauts de plateforme (INDÉPENDANTE du flag
``CORE_CONFIG_RUNTIME_ENABLED``) :
    override CoreConfig scope=global  >  settings (env)  >  défaut de code.

Concurrence : ``check_and_reserve`` sérialise les uploads concurrents d'un même
workspace via ``select_for_update`` sur la ligne ``WorkspaceQuota`` (verrou de
ligne PostgreSQL, valable multi-processus/multi-workers).
"""
from __future__ import annotations

from typing import Callable

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Sum
from django.db.models.functions import Coalesce

from apps.rag_core.models import Document
from apps.workspaces.models import Workspace, WorkspaceQuota

_KEY_COUNT = "WORKSPACE_DEFAULT_MAX_DOCUMENT_COUNT"
_KEY_BYTES = "WORKSPACE_DEFAULT_MAX_DOCUMENT_BYTES"

_CODE_FILE_COUNT = "workspace_file_count_quota_exceeded"
_CODE_STORAGE = "workspace_storage_quota_exceeded"


# --- Exceptions -------------------------------------------------------------

class QuotaValidationError(Exception):
    """Valeur de quota invalide (mappée en HTTP 400 par les vues)."""

    def __init__(self, code: str, field: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.field = field


class QuotaExceeded(Exception):
    """Dépassement de quota à l'upload (mappé en HTTP 409 par les vues).

    ``errors`` = liste de blocs structurés (un par dimension dépassée).
    """

    def __init__(self, errors: list[dict]) -> None:
        super().__init__("workspace_quota_exceeded")
        self.errors = errors

    def as_response(self) -> dict:
        """Corps de réponse : bloc unique si une seule dimension, sinon liste."""
        if len(self.errors) == 1:
            return self.errors[0]
        return {
            "code": "workspace_quota_exceeded",
            "detail": "The upload would exceed the workspace quota.",
            "errors": self.errors,
        }


# --- Défauts de plateforme + plafonds ---------------------------------------

def _global_override(key: str):
    """Valeur d'override CoreConfig scope=global pour ``key`` (coercée) ou None.

    Lecture directe d'une ligne, INDÉPENDANTE de ``CORE_CONFIG_RUNTIME_ENABLED`` :
    les défauts de quota doivent refléter la config persistée même quand l'overlay
    runtime global est désactivé. Fail-safe : toute erreur => None (fallback
    settings).
    """
    try:
        from apps.rag_core.core.config_resolver import (
            _coerce,
            coerce_value_for_storage,
            key_spec,
        )
        from apps.rag_core.models import CoreConfigOverride

        raw = (
            CoreConfigOverride.objects.filter(scope_type="global", scope_ref="", key=key)
            .values_list("value", flat=True)
            .first()
        )
        if raw is None:
            return None
        spec = key_spec(key)
        canonical = coerce_value_for_storage(key, raw)
        return _coerce(canonical, spec.value_type if spec else "int")
    except Exception:
        return None


def default_max_document_count() -> int:
    v = _global_override(_KEY_COUNT)
    if v is None:
        v = getattr(settings, _KEY_COUNT, 5000)
    return int(v)


def default_max_document_bytes() -> int:
    v = _global_override(_KEY_BYTES)
    if v is None:
        v = getattr(settings, _KEY_BYTES, 10 * 1024 ** 3)
    return int(v)


def hard_max_document_count() -> int:
    return int(getattr(settings, "WORKSPACE_HARD_MAX_DOCUMENT_COUNT", 100000))


def hard_max_document_bytes() -> int:
    return int(getattr(settings, "WORKSPACE_HARD_MAX_DOCUMENT_BYTES", 1024 ** 4))


def platform_defaults() -> dict:
    """Défauts effectifs + plafonds absolus (pour préremplir la modale + admin)."""
    return {
        "default_max_document_count": default_max_document_count(),
        "default_max_document_bytes": default_max_document_bytes(),
        "hard_max_document_count": hard_max_document_count(),
        "hard_max_document_bytes": hard_max_document_bytes(),
    }


def enforcement_enabled() -> bool:
    return bool(getattr(settings, "WORKSPACE_QUOTAS_ENFORCED", True))


# --- Validation -------------------------------------------------------------

def _validate_limit(field: str, value, *, hard_max: int) -> int:
    """Entier STRICTEMENT positif, ≤ plafond absolu. Lève QuotaValidationError."""
    # bool est une sous-classe d'int : le refuser explicitement.
    if isinstance(value, bool) or value is None:
        raise QuotaValidationError("must_be_integer", field)
    try:
        iv = int(value)
    except (TypeError, ValueError):
        raise QuotaValidationError("must_be_integer", field)
    # Un float non entier (ex. 10.5) doit être refusé (octets = quantité entière).
    if isinstance(value, float) and not value.is_integer():
        raise QuotaValidationError("must_be_integer", field)
    if iv <= 0:
        raise QuotaValidationError("must_be_positive", field)
    if iv > hard_max:
        raise QuotaValidationError("exceeds_hard_cap", field)
    return iv


def validate_count(value) -> int:
    return _validate_limit(
        "max_document_count", value, hard_max=hard_max_document_count()
    )


def validate_bytes(value) -> int:
    return _validate_limit(
        "max_document_bytes", value, hard_max=hard_max_document_bytes()
    )


# --- Cycle de vie du quota --------------------------------------------------

def ensure_quota(
    workspace: Workspace,
    *,
    max_document_count: int | None = None,
    max_document_bytes: int | None = None,
) -> WorkspaceQuota:
    """Crée (ou récupère) le WorkspaceQuota du workspace. Idempotent.

    Valeurs = arguments fournis (déjà validés) sinon défauts de plateforme
    COURANTS (résolus à l'instant t, jamais rétroactifs ensuite).
    """
    existing = WorkspaceQuota.objects.filter(workspace=workspace).first()
    if existing is not None:
        return existing
    count = (
        max_document_count
        if max_document_count is not None
        else default_max_document_count()
    )
    byts = (
        max_document_bytes
        if max_document_bytes is not None
        else default_max_document_bytes()
    )
    return WorkspaceQuota.objects.create(
        workspace=workspace, max_document_count=count, max_document_bytes=byts
    )


def get_or_create_quota(workspace: Workspace) -> WorkspaceQuota:
    """Quota du workspace, créé avec les défauts courants s'il manque (workspace
    pré-quota / robustesse)."""
    return ensure_quota(workspace)


# --- Usage ------------------------------------------------------------------

def quota_consuming_documents(workspace: Workspace):
    """Queryset UNIQUE définissant la politique d'usage : toutes les lignes
    ``Document`` du workspace (tous statuts, FAILED inclus).

    POINT DE CHANGEMENT UNIQUE de la politique — testé explicitement.
    """
    return Document.objects.filter(rag_space__workspace=workspace)


def _raw_usage(workspace: Workspace) -> tuple[int, int]:
    agg = quota_consuming_documents(workspace).aggregate(
        files=Count("id"),
        bytes=Coalesce(Sum("size_bytes"), 0),
    )
    return int(agg["files"] or 0), int(agg["bytes"] or 0)


def _percent(used: int, maximum: int) -> float:
    """Pourcentage d'usage borné (évite la division par zéro)."""
    if maximum <= 0:
        return 0.0
    return round(used * 100.0 / maximum, 1)


def get_usage(workspace: Workspace, *, quota: WorkspaceQuota | None = None) -> dict:
    """Snapshot complet quota + usage, prêt pour l'API.

    ``remaining_*`` ne sont jamais négatifs (``max(0, …)``) ; l'état de dépassement
    est conservé séparément via ``is_*_exceeded``.
    """
    q = quota or get_or_create_quota(workspace)
    used_count, used_bytes = _raw_usage(workspace)
    max_count = int(q.max_document_count)
    max_bytes = int(q.max_document_bytes)

    remaining_count = max(0, max_count - used_count)
    remaining_bytes = max(0, max_bytes - used_bytes)
    is_count_exceeded = used_count > max_count
    is_size_exceeded = used_bytes > max_bytes
    is_exceeded = is_count_exceeded or is_size_exceeded
    has_room = remaining_count > 0 and remaining_bytes > 0
    can_upload = (not enforcement_enabled()) or has_room

    return {
        "max_document_count": max_count,
        "used_document_count": used_count,
        "remaining_document_count": remaining_count,
        "max_document_bytes": max_bytes,
        "used_document_bytes": used_bytes,
        "remaining_document_bytes": remaining_bytes,
        "count_usage_percent": _percent(used_count, max_count),
        "size_usage_percent": _percent(used_bytes, max_bytes),
        "is_count_exceeded": is_count_exceeded,
        "is_size_exceeded": is_size_exceeded,
        "is_exceeded": is_exceeded,
        "can_upload": can_upload,
        "enforced": enforcement_enabled(),
    }


# --- Enforcement upload -----------------------------------------------------

def _build_errors(
    quota: WorkspaceQuota,
    used_count: int,
    used_bytes: int,
    added_count: int,
    added_bytes: int,
) -> list[dict]:
    errors: list[dict] = []
    if added_count > 0 and used_count + added_count > quota.max_document_count:
        errors.append({
            "code": _CODE_FILE_COUNT,
            "detail": "The workspace file limit has been reached.",
            "quota": {
                "max": int(quota.max_document_count),
                "used": used_count,
                "requested": added_count,
                "remaining": max(0, int(quota.max_document_count) - used_count),
            },
        })
    if added_bytes > 0 and used_bytes + added_bytes > quota.max_document_bytes:
        errors.append({
            "code": _CODE_STORAGE,
            "detail": "The upload would exceed the workspace storage limit.",
            "quota": {
                "max_bytes": int(quota.max_document_bytes),
                "used_bytes": used_bytes,
                "requested_bytes": added_bytes,
                "remaining_bytes": max(0, int(quota.max_document_bytes) - used_bytes),
            },
        })
    return errors


def check_upload_allowed(
    workspace: Workspace, *, added_count: int, added_bytes: int
) -> None:
    """Pré-vérification NON verrouillée (lève QuotaExceeded si dépassement).

    No-op si l'enforcement est désactivé ou si l'ajout est nul (dédup). Utilisée
    sur les chemins où la création n'est pas facilement enveloppable sous verrou
    (ingestion texte synchrone) ; le chemin d'upload de FICHIER utilise la
    réservation verrouillée ``check_and_reserve``.
    """
    if not enforcement_enabled() or (added_count <= 0 and added_bytes <= 0):
        return
    quota = get_or_create_quota(workspace)
    used_count, used_bytes = _raw_usage(workspace)
    errors = _build_errors(quota, used_count, used_bytes, added_count, added_bytes)
    if errors:
        raise QuotaExceeded(errors)


def check_and_reserve(
    workspace: Workspace,
    *,
    added_count: int,
    added_bytes: int,
    create_fn: Callable[[], object],
):
    """Vérifie le quota SOUS VERROU puis exécute ``create_fn`` dans la MÊME
    transaction — la ligne créée devient immédiatement la « réservation ».

    Sérialise les uploads concurrents du même workspace via
    ``select_for_update`` sur la ligne ``WorkspaceQuota`` : deux requêtes
    simultanées ne peuvent pas être acceptées sur le même usage initial (multi-
    processus). Lève ``QuotaExceeded`` (AUCUNE ligne créée) en cas de dépassement.

    - Enforcement désactivé => aucune vérification, ``create_fn`` exécuté tel quel.
    - Ajout nul (dédup) => aucune conso, ``create_fn`` exécuté même workspace plein.
    """
    if not enforcement_enabled():
        return create_fn()

    with transaction.atomic():
        quota = (
            WorkspaceQuota.objects.select_for_update()
            .filter(workspace=workspace)
            .first()
        )
        if quota is None:
            # Workspace pré-quota : on le crée puis on relit SOUS verrou.
            ensure_quota(workspace)
            quota = WorkspaceQuota.objects.select_for_update().get(workspace=workspace)

        if added_count <= 0 and added_bytes <= 0:
            return create_fn()

        used_count, used_bytes = _raw_usage(workspace)
        errors = _build_errors(quota, used_count, used_bytes, added_count, added_bytes)
        if errors:
            raise QuotaExceeded(errors)
        return create_fn()


# --- Audit ------------------------------------------------------------------

def _emit_quota_event(
    *, actor, workspace: Workspace, status: str, action: str, request_id: str = "",
    metadata: dict | None = None,
) -> None:
    """Émet un événement d'audit quota via le journal append-only existant.

    ``action_name = workspace.quota.<action>`` (created/updated),
    ``resource_type = workspace``. Fail-open (emit_action_event ne lève jamais).
    """
    try:
        from apps.audit.action_audit import emit_action_event
    except Exception:  # noqa: BLE001
        return
    emit_action_event(
        event_name=f"action.{status}",
        action_name=f"workspace.quota.{action}",
        actor=actor,
        status=status,
        workspace_id=str(workspace.id),
        resource_type="workspace",
        request_id=request_id or "",
        metadata=metadata or {},
    )


def audit_quota_created(
    *, actor, workspace: Workspace, max_document_count: int, max_document_bytes: int,
    request_id: str = "",
) -> None:
    _emit_quota_event(
        actor=actor, workspace=workspace, status="completed", action="created",
        request_id=request_id,
        metadata={
            "new_max_document_count": int(max_document_count),
            "new_max_document_bytes": int(max_document_bytes),
        },
    )


def audit_quota_updated(
    *, actor, workspace: Workspace, old_count: int, new_count: int,
    old_bytes: int, new_bytes: int, reason: str = "", is_exceeded: bool = False,
    request_id: str = "",
) -> None:
    _emit_quota_event(
        actor=actor, workspace=workspace, status="completed", action="updated",
        request_id=request_id,
        metadata={
            "old_max_document_count": int(old_count),
            "new_max_document_count": int(new_count),
            "old_max_document_bytes": int(old_bytes),
            "new_max_document_bytes": int(new_bytes),
            "reason": reason or "",
            "is_exceeded_after": bool(is_exceeded),
        },
    )
