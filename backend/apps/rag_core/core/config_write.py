"""Service d'écriture des overrides de config Core (PR-F) — staging only.

SEUL point d'écriture de ``CoreConfigOverride``. Valide strictement (clé, niveau de
portée, cohérence/existence du scope, coercion de valeur, **invariants de
configuration**) et journalise chaque écriture via ``record_config_event``
(append-only).

⚠️ Les overrides ne sont PLUS diagnostic-only : depuis PR-G3, le résolveur runtime
est branché dans ``llm_router.router``, ``llm_router.embeddings`` et
``llm_router.egress``. Quand ``CORE_CONFIG_RUNTIME_ENABLED=1``, ce qui est écrit ici
change le comportement d'exécution — génération, embeddings, indexation. C'est
pourquoi l'écriture applique désormais ``config_policy`` : un override ne doit
jamais produire une configuration que les system checks statiques refuseraient
(cf. docstring de ``config_policy``).

Les gates flag/prod/permission sont appliquées AVANT, côté vue.
"""
from __future__ import annotations

from .config_audit import record_config_event
from .config_policy import ConfigPolicyViolation, validate_overlay_value
from .config_resolver import (
    VALID_SCOPE_TYPES,
    allowed_max_scope,
    coerce_value_for_storage,
    current_environment,
    is_editable_key,
    key_spec,
    scope_rank,
)


def _invalidate_runtime_cache() -> None:
    """Incrémente l'epoch de config runtime (invalidation). Best-effort : une
    écriture ne doit jamais échouer à cause du cache."""
    try:
        from .runtime_config import bump_epoch

        bump_epoch()
    except Exception:
        pass


class OverrideValidationError(Exception):
    """Erreur de validation d'override (mappée par la vue ; défaut HTTP 400)."""

    def __init__(self, code: str, http_status: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.http_status = http_status


def normalize_scope_ref(scope_type: str, scope_ref) -> str:
    """Valide la cohérence/existence du scope et renvoie le scope_ref canonique.

    Lève OverrideValidationError en cas d'incohérence.
    """
    ref = "" if scope_ref is None else str(scope_ref).strip()

    if scope_type == "global":
        if ref != "":
            raise OverrideValidationError("scope_ref_must_be_empty_for_global")
        return ""

    if scope_type == "environment":
        if not ref:
            raise OverrideValidationError("scope_ref_required_for_environment")
        if ref.strip().lower() == "prod":
            raise OverrideValidationError("environment_prod_forbidden")
        return ref

    if scope_type == "workspace":
        try:
            ws_id = int(ref)
        except (TypeError, ValueError):
            raise OverrideValidationError("workspace_id_must_be_integer")
        from apps.workspaces.models import Workspace

        if not Workspace.objects.filter(id=ws_id).exists():
            raise OverrideValidationError("workspace_not_found")
        return str(ws_id)

    if scope_type == "ragspace":
        if not ref:
            raise OverrideValidationError("scope_ref_required_for_ragspace")
        from apps.rag_core.models import RagSpace

        if not RagSpace.objects.filter(id=ref).exists():
            raise OverrideValidationError("ragspace_not_found")
        return ref

    raise OverrideValidationError("invalid_scope_type")


def validate_override(scope_type: str, scope_ref, key: str) -> str:
    """Valide clé + cohérence du scope + niveau de portée. Renvoie scope_ref canonique.

    Ordre : type → clé → cohérence/existence du scope → niveau autorisé. La
    cohérence du scope est vérifiée AVANT le niveau pour qu'un scope_ref invalide
    (ex. ragspace inexistant) remonte une erreur précise.
    """
    if scope_type not in VALID_SCOPE_TYPES:
        raise OverrideValidationError("invalid_scope_type")

    if key_spec(key) is None:
        raise OverrideValidationError("unknown_key")

    if not is_editable_key(key):
        raise OverrideValidationError("key_not_editable")

    canonical_ref = normalize_scope_ref(scope_type, scope_ref)

    max_scope = allowed_max_scope(key)
    if max_scope is None or scope_rank(scope_type) > scope_rank(max_scope):
        raise OverrideValidationError("override_level_not_allowed")

    return canonical_ref


def set_override(*, actor, scope_type: str, scope_ref, key: str, value, reason: str = ""):
    """Crée/met à jour un override + journalise (action=set). Renvoie (override, event)."""
    from apps.rag_core.models import CoreConfigOverride

    canonical_ref = validate_override(scope_type, scope_ref, key)
    try:
        canonical_value = coerce_value_for_storage(key, value)
    except ValueError as e:
        raise OverrideValidationError(str(e) or "invalid_value")

    # Invariants de configuration : refuser AVANT de persister. Un override
    # invalide ne doit jamais être découvert au moment de son utilisation, dans
    # une requête utilisateur ou une tâche d'ingestion. La valeur passée est la
    # forme canonique en texte ; `validate_overlay_value` la type via le registre.
    try:
        validate_overlay_value(key, canonical_value, environment=current_environment())
    except ConfigPolicyViolation as violation:
        raise OverrideValidationError(violation.code) from None

    existing = (
        CoreConfigOverride.objects.filter(
            scope_type=scope_type, scope_ref=canonical_ref, key=key
        )
        .values_list("value", flat=True)
        .first()
    )
    old_value = existing if existing is not None else ""

    override, _created = CoreConfigOverride.objects.update_or_create(
        scope_type=scope_type,
        scope_ref=canonical_ref,
        key=key,
        defaults={"value": canonical_value, "updated_by": actor},
    )

    event = record_config_event(
        action="set",
        key=key,
        scope_type=scope_type,
        scope_ref=canonical_ref,
        old_value=old_value,
        new_value=canonical_value,
        reason=reason,
        actor=actor,
    )
    _invalidate_runtime_cache()
    return override, event


def unset_override(*, actor, scope_type: str, scope_ref, key: str, reason: str = ""):
    """Supprime un override + journalise (action=unset). Renvoie (event) ou lève 404.

    Choix documenté : un unset d'un override absent renvoie 404 ``override_not_found``.
    """
    from apps.rag_core.models import CoreConfigOverride

    # On valide scope/clé (mêmes règles), sans le contrôle de niveau bloquant :
    # un unset doit rester possible pour nettoyer, mais on garde la cohérence du scope.
    if scope_type not in VALID_SCOPE_TYPES:
        raise OverrideValidationError("invalid_scope_type")
    if key_spec(key) is None:
        raise OverrideValidationError("unknown_key")
    canonical_ref = normalize_scope_ref(scope_type, scope_ref)

    row = CoreConfigOverride.objects.filter(
        scope_type=scope_type, scope_ref=canonical_ref, key=key
    ).first()
    if row is None:
        raise OverrideValidationError("override_not_found", http_status=404)

    old_value = row.value
    row.delete()

    event = record_config_event(
        action="unset",
        key=key,
        scope_type=scope_type,
        scope_ref=canonical_ref,
        old_value=old_value,
        new_value="",
        reason=reason,
        actor=actor,
    )
    _invalidate_runtime_cache()
    return event
