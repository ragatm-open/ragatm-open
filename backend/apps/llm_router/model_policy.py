"""Politique CANONIQUE de configuration des modèles d'un RagSpace.

Règle (toujours active, indépendante des flags owner-scope / ACL / core-config) :

  - **superuser actif** → peut choisir provider et modèle (chat + embeddings) ;
  - **tout autre utilisateur** (owner, admin, manager, editor, member, viewer) →
    reçoit les valeurs CANONIQUES ci-dessous, IMPOSÉES par le backend. Toute
    tentative d'imposer une autre valeur est **refusée** (jamais appliquée
    silencieusement).

Source de vérité unique : les settings `DEFAULT_RAGSPACE_*` (eux-mêmes surchargeables
par l'environnement). Ne pas redéfinir ces chaînes dans les vues / serializers /
commandes / frontend.
"""
from __future__ import annotations

from apps.accounts.permissions import is_ragatm_owner
from apps.llm_router.model_defaults import MODEL_DEFAULT_FIELDS, ragspace_model_defaults

# Champs de configuration modèle d'un RagSpace (noms réels du modèle).
# Alias de la liste portée par `model_defaults` : une seule énumération.
MODEL_FIELDS: tuple[str, ...] = MODEL_DEFAULT_FIELDS

# Champs « provider » (comparaison insensible à la casse) vs « modèle » (strip).
_PROVIDER_FIELDS = {"llm_provider", "embedding_provider"}


def canonical_model_config() -> dict[str, str]:
    """Configuration modèle canonique de l'environnement.

    Elle remplit DEUX rôles, et c'est volontairement la même valeur :

    - **valeurs imposées** à un non-superuser (il ne choisit pas) ;
    - **défauts de création** préremplis pour un superuser (il peut changer).

    Les distinguer reviendrait à autoriser deux vérités pour un même
    environnement — précisément ce qui a produit la divergence de production.

    Délègue à `model_defaults.ragspace_model_defaults()` : c'est là que vit la
    lecture des settings, dans un module importable depuis `models.py`.
    """
    return ragspace_model_defaults()


def can_manage_ragspace_models(user) -> bool:
    """True ssi l'utilisateur peut modifier provider/modèle d'un RagSpace.

    Décision volontairement stricte : superuser ACTIF uniquement (le statut staff
    seul ne suffit pas). Réutilise le prédicat canonique `is_ragatm_owner`.
    """
    return is_ragatm_owner(user)


# Alias sémantique exposé au frontend (read-only).
def can_edit_model_configuration(user) -> bool:
    return can_manage_ragspace_models(user)


def _norm(field: str, value) -> str:
    s = "" if value is None else str(value).strip()
    return s.lower() if field in _PROVIDER_FIELDS else s


def create_denied_fields(actor, data: dict) -> set[str]:
    """Champs modèle où un NON-superuser tente une valeur ≠ canonique (à la création).

    Superuser → jamais rien (set vide). Non-superuser : un champ présent dans `data`
    avec une valeur différente de la canonique est refusé.
    """
    if can_manage_ragspace_models(actor):
        return set()
    canon = canonical_model_config()
    return {
        f for f in MODEL_FIELDS
        if f in data and _norm(f, data.get(f)) != _norm(f, canon[f])
    }


def patch_denied_fields(actor, instance, data: dict) -> set[str]:
    """Champs modèle où un NON-superuser tente de CHANGER la valeur existante (PATCH).

    Superuser → set vide. Non-superuser : un champ présent avec une valeur différente
    de la valeur ACTUELLE de l'espace est refusé (une valeur identique est un no-op,
    ignorée — jamais réécrite). La configuration historique n'est jamais modifiée.
    """
    if can_manage_ragspace_models(actor):
        return set()
    return {
        f for f in MODEL_FIELDS
        if f in data and _norm(f, data.get(f)) != _norm(f, getattr(instance, f, None))
    }


# --- Audit (append-only, sanitisé ; jamais de secret/contenu) ----------------
def audit_model_config_denied(request, *, rag_space_id, workspace_id, fields) -> None:
    try:
        from apps.audit.action_audit import emit_action_event
    except Exception:  # noqa: BLE001
        return
    emit_action_event(
        event_name="action.denied", action_name="ragspace.model_configuration_denied",
        status="denied", actor=getattr(request, "user", None),
        workspace_id=str(workspace_id) if workspace_id is not None else "",
        rag_space_id=rag_space_id if rag_space_id else None,
        resource_type="rag_space",
        request_id=request.headers.get("x-request-id") or "",
        error_code="ragspace_model_configuration_forbidden",
        metadata={"fields_attempted": sorted(fields)},
    )


def audit_model_config_changed(request, *, rag_space_id, workspace_id, before: dict, after: dict) -> None:
    try:
        from apps.audit.action_audit import emit_action_event
    except Exception:  # noqa: BLE001
        return
    emit_action_event(
        event_name="action.completed", action_name="ragspace.model_configuration_changed",
        status="completed", actor=getattr(request, "user", None),
        workspace_id=str(workspace_id) if workspace_id is not None else "",
        rag_space_id=rag_space_id if rag_space_id else None,
        resource_type="rag_space",
        request_id=request.headers.get("x-request-id") or "",
        metadata={"before": before, "after": after},
    )


# --- Administration PLATEFORME de la configuration modèle --------------------


class PlatformModelAdminScope:
    """Accès d'un superuser à la configuration modèle d'un espace hors de son périmètre.

    ## La question tranchée

    Sous `OWNER_SCOPE_ENABLED=1`, deux gardes indépendantes se superposent :

    - la **politique modèles** — superuser actif uniquement, portée globale ;
    - le **périmètre owner** — détenteur du scope, portée par espace.

    Leur intersection peut être **vide**. Constaté en staging : `QUALIF-SPACE` a son
    périmètre sur un compte non-superuser, et aucun utilisateur ne peut donc changer sa
    configuration modèle par l'API. La capacité existait quand même — par
    `set_ragspace_embedding_pipeline`, en shell, **sans audit applicatif**.

    ## Le raisonnement

    Refuser l'API n'ajoutait aucune sécurité : un superuser a déjà l'accès base et le
    shell. Cela déplaçait simplement l'opération vers un chemin non tracé. C'est le pire
    des deux mondes — la friction sans la garantie.

    L'élévation est donc accordée, et **bornée** :

    | Autorisé | Refusé |
    |---|---|
    | `llm_provider`, `llm_model` | `name` (acte métier) |
    | `embedding_provider`, `embedding_model` | `allow_external_fallback` (confidentialité) |
    | lecture de `llm-settings` | tout accès au contenu documentaire |

    Le superuser ne devient pas propriétaire métier de l'espace : il administre sa
    configuration IA, rien d'autre. Toute élévation effective est auditée
    (`ragspace.model_configuration_elevated`).
    """

    #: Champs qu'une élévation plateforme autorise à modifier. Volontairement figé :
    #: l'étendre revient à élargir un contournement de périmètre.
    ALLOWED_FIELDS = frozenset(MODEL_FIELDS) | {"force"}


def platform_model_admin_denied_fields(data: dict) -> set[str]:
    """Champs du payload qu'une élévation plateforme ne couvre PAS."""
    return {k for k in (data or {}) if k not in PlatformModelAdminScope.ALLOWED_FIELDS}


def audit_model_config_elevated(request, *, rag_space_id, workspace_id, fields) -> None:
    """Trace une administration plateforme hors périmètre owner (§22).

    Émise AVANT l'écriture : une élévation refusée en aval doit tout de même laisser la
    trace de la tentative, sinon l'audit ne décrit que les succès.
    """
    try:
        from apps.audit.action_audit import emit_action_event
    except Exception:  # noqa: BLE001
        return
    emit_action_event(
        event_name="action.completed",
        action_name="ragspace.model_configuration_elevated",
        status="completed",
        actor=getattr(request, "user", None),
        workspace_id=str(workspace_id) if workspace_id is not None else "",
        rag_space_id=rag_space_id if rag_space_id else None,
        resource_type="rag_space",
        request_id=request.headers.get("x-request-id") or "",
        metadata={"fields": sorted(fields), "reason": "platform_model_admin_outside_owner_scope"},
    )
