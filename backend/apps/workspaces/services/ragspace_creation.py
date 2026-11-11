"""Création d'un RagSpace — CHEMIN UNIQUE, périmètre owner garanti.

## Le défaut corrigé

Un RagSpace créé sans `owner_scope` est **inutilisable** dès que l'enforcement
owner-scope est actif : `_decide_ragspace` échoue en `ragspace_without_scope`, et
`require_ragspace_access` répond **404 anti-énumération**. L'utilisateur voit son
espace dans la liste, l'ouvre, et l'upload lui répond « introuvable ».

Constaté en staging le 2026-07-28 (`OWNER_SCOPE_ENABLED=1`) : chaque espace créé
par l'API arrivait avec `owner_scope=None`. La production n'était pas touchée
seulement parce que le drapeau y vaut `0` — le défaut serait devenu bloquant à
l'activation.

Le fail-closed de `_decide_ragspace` n'est PAS le problème : il est délibéré et
correct (un espace sans périmètre n'appartient à personne, donc personne ne
l'ouvre). Ce module corrige la **donnée à la source**, sans toucher aux contrôles
d'accès.

## Règle canonique — établie par le code existant, pas inventée

`RagSpace.owner_scope` documente déjà l'intention :

    « Le client ne choisit JAMAIS cette valeur : le backend la dérive du
      membership de l'acteur à la création. »

La dérivation utilise la résolution canonique déjà en place
(`access.get_owner_scope_for_membership`) : un membership `owner` **est** son
propre périmètre (son `owner_scope` reste NULL en base, pour éviter
l'auto-référence) ; un non-owner porte le membership `owner` qui le gouverne.

`Workspace` ne porte AUCUN champ `owner_scope` : il n'existe donc pas de
« périmètre du workspace » dont on pourrait hériter. Le périmètre vient toujours
d'un membership.

Ordre de résolution :

1. **périmètre explicite** fourni par un appelant interne (migration, replay) —
   validé comme les commandes de backfill le font déjà ;
2. **membership de l'acteur** dans ce workspace — le cas nominal ;
3. **owner unique du workspace** (`owner_scope_migration.single_owner`), pour les
   chemins SANS acteur (seeds, fixtures) et pour un acteur non-owner dont le
   membership n'est pas encore rattaché (période de backfill, drapeau OFF). C'est
   exactement la règle que `backfill_workspace` applique déjà, et elle refuse
   l'ambiguïté : 0 ou >1 owner ⇒ aucune écriture ;
4. sinon **refus explicite**, aucune ligne écrite.

## Invariant

    Tout RagSpace créé par ce module porte un `owner_scope` non nul,
    écrit dans la MÊME transaction que l'objet.

Le remplissage est systématique, y compris quand `OWNER_SCOPE_ENABLED=0` : les
données sont ainsi prêtes avant l'activation, et il n'y aura pas de migration
massive à faire le jour J.
"""
from __future__ import annotations

from django.db import transaction

from apps.rag_core.models import RagSpace, RagSpaceAccess
from apps.workspaces.models import Membership, Workspace
from apps.workspaces.services import owner_scope_migration as osm
from apps.workspaces.services.access import (
    effective_owner_scope_pk,
    get_owner_scope_for_membership,
    validate_ragspace_access_invariants,
)

#: Niveau accordé au CRÉATEUR non-owner d'un espace. `editor` = « write » dans
#: `_MIN_SPACE_ROLE` : voir, uploader, déclencher l'ingestion, interroger, gérer
#: ses documents. Volontairement PAS `manager`, qui ouvrirait la modification des
#: ACL et la suppression de l'espace — un créateur n'a pas besoin d'administrer
#: pour utiliser. Le plafond D1 (`_ALLOWED_SPACE_ROLES_BY_WS_ROLE`) autorise
#: `editor` pour tous les rôles habilités à créer (owner, admin).
CREATOR_SPACE_ROLE = RagSpaceAccess.ROLE_EDITOR


# --- Erreurs typées ---------------------------------------------------------
# Les vues les traduisent en réponses HTTP. Aucune ne révèle l'existence d'une
# ressource à un appelant non autorisé : toutes sont levées APRÈS que la garde
# `require_workspace_admin` a prouvé que l'acteur administre CE workspace.

class RagSpaceCreationError(Exception):
    """Refus de créer un RagSpace. `code` est stable (contrat API et audit)."""

    code = "ragspace_creation_refused"

    def __init__(self, message: str = "", *, detail: dict | None = None):
        super().__init__(message or self.code)
        self.detail = detail or {}


class WorkspaceDisabledError(RagSpaceCreationError):
    """Le workspace est désactivé : créer un espace qu'aucune tâche ne pourra
    traiter (cf. `task_guard.WORKSPACE_DISABLED`) produirait un objet mort."""

    code = "workspace_disabled"


class OwnerScopeUnresolvedError(RagSpaceCreationError):
    """Aucun périmètre owner ne peut être déterminé SANS AMBIGUÏTÉ.

    Se produit quand l'acteur est un non-owner non rattaché ET que le workspace
    n'a pas exactement un owner. Deviner ici reviendrait à attribuer des droits :
    on refuse, et l'exploitant tranche avec les commandes de backfill.
    """

    code = "owner_scope_unresolved"


class InvalidOwnerScopeError(RagSpaceCreationError):
    """Périmètre explicite illégal (non-owner, ou owner d'un autre workspace)."""

    code = "invalid_owner_scope"


# --- Résolution du périmètre (pure : lectures ORM, aucun réseau, aucune écriture)
def resolve_owner_scope_for_ragspace(
    *,
    workspace: Workspace,
    actor=None,
    actor_membership: Membership | None = None,
    requested_scope: Membership | None = None,
) -> Membership:
    """Périmètre owner canonique d'un RagSpace à créer dans `workspace`.

    Ne lève que `InvalidOwnerScopeError` / `OwnerScopeUnresolvedError`. N'écrit
    rien : testable isolément, et utilisable pour un diagnostic à blanc.
    """
    # 1. Périmètre explicite (appelants internes uniquement — jamais le client).
    if requested_scope is not None:
        if requested_scope.role != Membership.ROLE_OWNER:
            raise InvalidOwnerScopeError(
                "le périmètre doit référencer un membership de rôle owner",
                detail={"membership_id": requested_scope.pk, "role": requested_scope.role},
            )
        if requested_scope.workspace_id != workspace.pk:
            raise InvalidOwnerScopeError(
                "le périmètre appartient à un autre workspace",
                detail={"membership_id": requested_scope.pk},
            )
        return requested_scope

    # 2. Membership de l'acteur — le cas nominal.
    if actor_membership is None and actor is not None:
        actor_membership = Membership.objects.filter(
            workspace=workspace, user=actor
        ).first()
    if actor_membership is not None:
        if actor_membership.workspace_id != workspace.pk:
            raise InvalidOwnerScopeError(
                "le membership de l'acteur appartient à un autre workspace",
                detail={"membership_id": actor_membership.pk},
            )
        scope = get_owner_scope_for_membership(actor_membership)
        if scope is not None:
            return scope

    # 3. Owner unique du workspace — même règle déterministe que le backfill.
    #    `single_owner` rend None dès qu'il y a 0 ou plusieurs owners : on ne
    #    devine jamais lequel gouverne.
    fallback = osm.single_owner(workspace)
    if fallback is not None:
        return fallback

    # 4. Refus. Aucune écriture n'a eu lieu.
    n_owners = Membership.objects.filter(
        workspace=workspace, role=Membership.ROLE_OWNER
    ).count()
    raise OwnerScopeUnresolvedError(
        "aucun périmètre owner déterminable sans ambiguïté pour ce workspace",
        detail={"workspace_id": workspace.pk, "owners": n_owners},
    )


# --- Création ---------------------------------------------------------------
def create_ragspace(
    *,
    workspace: Workspace,
    name: str,
    actor=None,
    actor_membership: Membership | None = None,
    requested_scope: Membership | None = None,
    request_id: str = "",
    emit_audit: bool = True,
    **fields,
) -> RagSpace:
    """Crée un RagSpace avec son périmètre owner, ATOMIQUEMENT.

    `fields` accepte les champs additionnels du modèle (configuration modèle
    notamment). Les défauts de modèle non fournis sont laissés au modèle, dont
    les `default=` sont des callables lisant `DEFAULT_RAGSPACE_*`.

    Refuse AVANT toute écriture : workspace désactivé, périmètre indéterminable,
    périmètre explicite illégal.
    """
    if not workspace.is_active:
        raise WorkspaceDisabledError(
            "workspace désactivé : aucun traitement ne s'exécuterait dans cet espace",
            detail={"workspace_id": workspace.pk},
        )

    with transaction.atomic():
        # Résolu DANS la transaction : le périmètre et l'objet sont écrits
        # ensemble, ou pas du tout. Aucun état intermédiaire `owner_scope=None`
        # n'est jamais visible.
        owner_scope = resolve_owner_scope_for_ragspace(
            workspace=workspace,
            actor=actor,
            actor_membership=actor_membership,
            requested_scope=requested_scope,
        )
        rag_space = RagSpace.objects.create(
            workspace=workspace,
            name=name,
            owner_scope=owner_scope,
            created_by=actor if actor is not None and getattr(actor, "pk", None) else None,
            **fields,
        )
        access = _ensure_creator_access(
            rag_space=rag_space, owner_scope=owner_scope,
            actor=actor, actor_membership=actor_membership,
        )
        if emit_audit:
            _audit_created(
                rag_space=rag_space, owner_scope=owner_scope, actor=actor,
                request_id=request_id, creator_access=access,
            )
    return rag_space


def _ensure_creator_access(
    *, rag_space: RagSpace, owner_scope: Membership, actor, actor_membership: Membership | None
) -> RagSpaceAccess | None:
    """Octroie au créateur NON-OWNER l'accès minimal pour utiliser son espace.

    ## Pourquoi

    Un administrateur pouvait créer un espace puis se voir refuser l'ingestion :
    sous ACL, un non-owner a besoin d'un `RagSpaceAccess` explicite, et la création
    n'en produisait aucun. Même motif que le défaut owner_scope corrigé au lot
    précédent — créer quelque chose d'inutilisable — une couche au-dessus.

    ## Qui reçoit quoi

    - **owner du périmètre** : RIEN. Son accès découle de son rôle (`_decide_ragspace`
      le fait bypasser l'ACL). Matérialiser un grant ne changerait aucune décision
      et créerait une seconde source de vérité pour le même droit.
    - **créateur non-owner** : un grant `editor`, le minimum permettant d'utiliser
      l'espace créé. Jamais `manager` : administrer n'est pas utiliser.
    - **les autres membres du workspace** : RIEN. Aucun accès n'est diffusé.

    Idempotent : un accès actif existant n'est ni dupliqué, ni abaissé, ni élevé.
    """
    if actor is None or not getattr(actor, "pk", None):
        return None  # chemins internes sans acteur (seeds, bootstrap technique)

    if actor_membership is None:
        actor_membership = Membership.objects.filter(
            workspace_id=rag_space.workspace_id, user=actor
        ).first()
    if actor_membership is None:
        return None

    # L'owner responsable bypasse l'ACL : pas de grant.
    if actor_membership.role == Membership.ROLE_OWNER:
        return None

    existing = RagSpaceAccess.objects.filter(
        rag_space=rag_space, user=actor, revoked_at__isnull=True
    ).first()
    if existing is not None:
        # Ne JAMAIS toucher à un droit déjà posé : ni l'abaisser (on casserait un
        # octroi délibéré), ni l'élever (on accorderait en silence).
        return existing

    # Un acteur peut légitimement créer un espace SANS appartenir lui-même au
    # périmètre attribué : membership non encore rattaché (période de backfill),
    # ou périmètre résolu par la règle de repli `single_owner`. Dans ce cas le
    # grant serait hors périmètre — on n'en pose aucun, et surtout on ne fait pas
    # échouer une création par ailleurs valide. L'absence de rattachement est une
    # donnée à corriger, que `audit_workspace_owner_scopes` signale déjà ; ce
    # n'est pas à la création de la transformer en erreur 500.
    if effective_owner_scope_pk(actor_membership) != rag_space.owner_scope_id:
        return None

    # Revalide les invariants de périmètre et le plafond D1 avant d'écrire.
    validate_ragspace_access_invariants(
        user=actor, rag_space=rag_space, space_role=CREATOR_SPACE_ROLE
    )
    return RagSpaceAccess.objects.create(
        rag_space=rag_space,
        user=actor,
        space_role=CREATOR_SPACE_ROLE,
        assigned_by=actor,
    )


def _audit_created(
    *, rag_space: RagSpace, owner_scope: Membership, actor, request_id: str,
    creator_access: RagSpaceAccess | None = None,
) -> None:
    """Trace append-only de la création et du périmètre attribué.

    Fail-open : un défaut d'audit ne doit pas empêcher une création légitime
    (même doctrine que `emit_action_event` ailleurs). Ne publie que des
    identifiants du workspace concerné — rien d'un autre périmètre.
    """
    try:
        from apps.audit.action_audit import emit_action_event
    except Exception:  # noqa: BLE001
        return
    emit_action_event(
        event_name="action.completed",
        action_name="ragspace.created",
        status="completed",
        actor=actor,
        workspace_id=str(rag_space.workspace_id),
        rag_space_id=rag_space.id,
        resource_type="rag_space",
        request_id=request_id or "",
        metadata={
            "owner_scope_membership_id": owner_scope.pk,
            "owner_scope_role": owner_scope.role,
            # `None` = aucun grant nécessaire (créateur owner du périmètre).
            "creator_space_role": getattr(creator_access, "space_role", None),
        },
    )
