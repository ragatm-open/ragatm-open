"""Tests unitaires — périmètres owner (owner_scope) & ACL RagSpace, Lot 1 « fondations ».

Vérifie :
  - invariants de périmètre (validate_membership_owner_scope) ;
  - résolution get_owner_scope_for_membership ;
  - flags OFF ⇒ comportement legacy STRICTEMENT inchangé ;
  - OWNER_SCOPE_ENABLED ⇒ un owner ne voit que SON périmètre (deux owners d'un
    même workspace sont étanches) ;
  - RAGSPACE_ACL_ENABLED ⇒ un non-owner exige un RagSpaceAccess actif suffisant ;
  - visible_ragspaces_for_user filtre par périmètre quand activé ;
  - contrainte d'unicité d'accès actif.
"""
import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError
from rest_framework.exceptions import NotFound

from apps.rag_core.models import RagSpace, RagSpaceAccess
from apps.workspaces.checks import owner_scope_flags_coherent
from apps.workspaces.models import Membership, Workspace
from apps.workspaces.services import access as acc

User = get_user_model()


@pytest.fixture
def world(db):
    """Un workspace, deux périmètres owner (A, B), un membre rattaché à A, deux espaces."""
    ws = Workspace.objects.create(name="WS")
    u_owner_a = User.objects.create_user("owner_a", "owner_a@t.local", "x")
    u_owner_b = User.objects.create_user("owner_b", "owner_b@t.local", "x")
    u_member_a = User.objects.create_user("member_a", "member_a@t.local", "x")

    owner_a = Membership.objects.create(workspace=ws, user=u_owner_a, role="owner")
    owner_b = Membership.objects.create(workspace=ws, user=u_owner_b, role="owner")
    member_a = Membership.objects.create(workspace=ws, user=u_member_a, role="member", owner_scope=owner_a)

    rs_a = RagSpace.objects.create(workspace=ws, name="A1", owner_scope=owner_a, created_by=u_owner_a)
    rs_b = RagSpace.objects.create(workspace=ws, name="B1", owner_scope=owner_b, created_by=u_owner_b)
    return dict(
        ws=ws, u_owner_a=u_owner_a, u_owner_b=u_owner_b, u_member_a=u_member_a,
        owner_a=owner_a, owner_b=owner_b, member_a=member_a, rs_a=rs_a, rs_b=rs_b,
    )


# --- Résolution & invariants -------------------------------------------------

def test_get_owner_scope_owner_is_its_own_scope(world):
    assert acc.get_owner_scope_for_membership(world["owner_a"]) == world["owner_a"]


def test_get_owner_scope_non_owner_returns_scope(world):
    assert acc.get_owner_scope_for_membership(world["member_a"]) == world["owner_a"]


def test_validate_owner_must_have_none(world):
    with pytest.raises(ValueError):
        acc.validate_membership_owner_scope(world["owner_a"], world["owner_a"])
    acc.validate_membership_owner_scope(world["owner_a"], None)  # ok


def test_validate_non_owner_requires_scope(world):
    with pytest.raises(ValueError):
        acc.validate_membership_owner_scope(world["member_a"], None)


def test_validate_scope_must_be_owner_role(world):
    with pytest.raises(ValueError):
        acc.validate_membership_owner_scope(world["member_a"], world["member_a"])


def test_validate_scope_same_workspace(world):
    other_ws = Workspace.objects.create(name="OTHER")
    foreign_owner = Membership.objects.create(
        workspace=other_ws, user=world["u_owner_a"], role="owner"
    )
    with pytest.raises(ValueError):
        acc.validate_membership_owner_scope(world["member_a"], foreign_owner)


# --- Flags OFF ⇒ legacy inchangé --------------------------------------------

def test_flags_off_owner_can_access_other_scope_space(world, settings):
    """Comportement historique : membership workspace suffit, périmètre ignoré."""
    settings.OWNER_SCOPE_ENABLED = False
    settings.OWNER_SCOPE_AUDIT_ONLY = False
    # owner_b accède à rs_a (périmètre A) — autorisé en legacy (même workspace).
    assert acc.require_ragspace_access(
        user=world["u_owner_b"], rag_space_id=world["rs_a"].id
    ) == world["rs_a"]


def test_flags_off_visible_returns_all(world, settings):
    settings.OWNER_SCOPE_ENABLED = False
    ids = set(
        acc.visible_ragspaces_for_user(user=world["u_owner_a"], workspace_id=world["ws"].id)
        .values_list("id", flat=True)
    )
    assert ids == {world["rs_a"].id, world["rs_b"].id}


# --- OWNER_SCOPE_ENABLED ⇒ isolation par périmètre --------------------------

def test_owner_sees_only_own_scope(world, settings):
    settings.OWNER_SCOPE_ENABLED = True
    settings.OWNER_SCOPE_AUDIT_ONLY = False
    # owner_a → son espace : OK
    assert acc.require_ragspace_access(user=world["u_owner_a"], rag_space_id=world["rs_a"].id)
    # owner_a → espace du périmètre B (même workspace) : REFUSÉ (404 anti-énum)
    with pytest.raises(NotFound):
        acc.require_ragspace_access(user=world["u_owner_a"], rag_space_id=world["rs_b"].id)
    # symétrie owner_b
    with pytest.raises(NotFound):
        acc.require_ragspace_access(user=world["u_owner_b"], rag_space_id=world["rs_a"].id)


def test_nonowner_same_scope_allowed_acl_off(world, settings):
    settings.OWNER_SCOPE_ENABLED = True
    settings.OWNER_SCOPE_AUDIT_ONLY = False
    settings.RAGSPACE_ACL_ENABLED = False
    # member rattaché à A lit rs_a (même périmètre, ACL non exigée)
    assert acc.require_ragspace_access(user=world["u_member_a"], rag_space_id=world["rs_a"].id)
    # mais pas rs_b (périmètre B) — 404 anti-énum
    with pytest.raises(NotFound):
        acc.require_ragspace_access(user=world["u_member_a"], rag_space_id=world["rs_b"].id)


def test_visible_owner_scope_enforced_filters(world, settings):
    settings.OWNER_SCOPE_ENABLED = True
    settings.OWNER_SCOPE_AUDIT_ONLY = False
    settings.RAGSPACE_ACL_ENABLED = False
    a_ids = set(
        acc.visible_ragspaces_for_user(user=world["u_owner_a"], workspace_id=world["ws"].id)
        .values_list("id", flat=True)
    )
    assert a_ids == {world["rs_a"].id}
    b_ids = set(
        acc.visible_ragspaces_for_user(user=world["u_owner_b"], workspace_id=world["ws"].id)
        .values_list("id", flat=True)
    )
    assert b_ids == {world["rs_b"].id}


# --- RAGSPACE_ACL_ENABLED ⇒ RagSpaceAccess requis ---------------------------

def test_acl_requires_active_access(world, settings):
    settings.OWNER_SCOPE_ENABLED = True
    settings.OWNER_SCOPE_AUDIT_ONLY = False
    settings.RAGSPACE_ACL_ENABLED = True
    settings.RAGSPACE_ACL_AUDIT_ONLY = False
    # même périmètre mais aucun RagSpaceAccess → refusé (access_missing → 404 anti-énum)
    with pytest.raises(NotFound):
        acc.require_ragspace_access(user=world["u_member_a"], rag_space_id=world["rs_a"].id)
    # viewer access → lecture OK, écriture refusée (space_role_insufficient → 403 visible)
    RagSpaceAccess.objects.create(
        rag_space=world["rs_a"], user=world["u_member_a"], space_role="viewer"
    )
    assert acc.require_ragspace_access(user=world["u_member_a"], rag_space_id=world["rs_a"].id)
    with pytest.raises(PermissionDenied):
        acc.require_ragspace_write(user=world["u_member_a"], rag_space_id=world["rs_a"].id)


def test_acl_editor_can_write(world, settings):
    settings.OWNER_SCOPE_ENABLED = True
    settings.OWNER_SCOPE_AUDIT_ONLY = False
    settings.RAGSPACE_ACL_ENABLED = True
    settings.RAGSPACE_ACL_AUDIT_ONLY = False
    RagSpaceAccess.objects.create(
        rag_space=world["rs_a"], user=world["u_member_a"], space_role="editor"
    )
    assert acc.require_ragspace_write(user=world["u_member_a"], rag_space_id=world["rs_a"].id)


def test_audit_only_never_blocks(world, settings):
    """OWNER_SCOPE_AUDIT_ONLY : la décision cible est calculée mais ne bloque jamais."""
    settings.OWNER_SCOPE_ENABLED = True
    settings.OWNER_SCOPE_AUDIT_ONLY = True
    # owner_a → rs_b : refusé en enforcement, mais autorisé (legacy) en audit-only
    assert acc.require_ragspace_access(user=world["u_owner_a"], rag_space_id=world["rs_b"].id)


# --- Contrainte d'unicité d'accès actif -------------------------------------

def test_unique_active_access(world):
    RagSpaceAccess.objects.create(rag_space=world["rs_a"], user=world["u_member_a"], space_role="viewer")
    with pytest.raises(IntegrityError):
        RagSpaceAccess.objects.create(rag_space=world["rs_a"], user=world["u_member_a"], space_role="editor")


# --- Résolution canonique (point de revue #1) -------------------------------

def test_effective_owner_scope_pk_owner_resolves_to_self(world):
    assert acc.effective_owner_scope_pk(world["owner_a"]) == world["owner_a"].pk


def test_effective_owner_scope_pk_non_owner(world):
    assert acc.effective_owner_scope_pk(world["member_a"]) == world["owner_a"].pk
    assert acc.effective_owner_scope_pk(None) is None


# --- Fail-closed sur donnée ambiguë (point de revue #7) ---------------------

def test_ragspace_without_scope_denied_even_for_owner(world, settings):
    settings.OWNER_SCOPE_ENABLED = True
    settings.OWNER_SCOPE_AUDIT_ONLY = False
    orphan = RagSpace.objects.create(workspace=world["ws"], name="orphan", owner_scope=None)
    with pytest.raises(NotFound):  # ragspace_without_scope → 404 anti-énum
        acc.require_ragspace_access(user=world["u_owner_a"], rag_space_id=orphan.id)


# --- Invariants RagSpaceAccess + plafond D1 (point de revue #5) -------------

def test_validate_access_member_cannot_be_manager(world):
    with pytest.raises(ValueError):
        acc.validate_ragspace_access_invariants(
            user=world["u_member_a"], rag_space=world["rs_a"], space_role="manager"
        )
    # editor est dans le plafond d'un member
    m = acc.validate_ragspace_access_invariants(
        user=world["u_member_a"], rag_space=world["rs_a"], space_role="editor"
    )
    assert m == world["member_a"]


def test_validate_access_cross_scope_rejected(world):
    # member rattaché à A ne peut pas recevoir d'accès sur un espace du périmètre B
    with pytest.raises(ValueError):
        acc.validate_ragspace_access_invariants(
            user=world["u_member_a"], rag_space=world["rs_b"], space_role="viewer"
        )


# --- System check flags incohérents (point de revue #6) ---------------------

def test_flag_check_acl_without_owner_scope_errors(settings):
    settings.OWNER_SCOPE_ENABLED = False
    settings.OWNER_SCOPE_AUDIT_ONLY = False
    settings.RAGSPACE_ACL_ENABLED = True
    settings.RAGSPACE_ACL_AUDIT_ONLY = False
    errors = owner_scope_flags_coherent(None)
    assert any(e.id == "workspaces.E001" for e in errors)


def test_flag_check_all_off_is_clean(settings):
    settings.OWNER_SCOPE_ENABLED = False
    settings.OWNER_SCOPE_AUDIT_ONLY = False
    settings.RAGSPACE_ACL_ENABLED = False
    settings.RAGSPACE_ACL_AUDIT_ONLY = False
    assert owner_scope_flags_coherent(None) == []


# --- clean() modèle : défense en profondeur (points de revue #2/#3) ---------

def test_membership_clean_owner_with_scope_rejected(world):
    world["owner_a"].owner_scope = world["owner_b"]
    with pytest.raises(ValidationError):
        world["owner_a"].full_clean()


def test_membership_clean_scope_must_target_owner(world):
    u_viewer = User.objects.create_user("v1", "v1@t.local", "x")
    viewer = Membership.objects.create(workspace=world["ws"], user=u_viewer, role="viewer", owner_scope=world["owner_a"])
    # rattacher member_a à un non-owner (viewer) => invalide
    world["member_a"].owner_scope = viewer
    with pytest.raises(ValidationError):
        world["member_a"].full_clean()


def test_membership_clean_valid_passes(world):
    world["member_a"].full_clean()  # owner_scope=owner_a, même workspace => OK
