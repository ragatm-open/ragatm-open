"""Matrice de comportement des modes ACL — chemin HTTP vs chemin Celery.

## Le défaut couvert

`RAGSPACE_ACL_AUDIT_ONLY` est censé OBSERVER sans jamais bloquer. Or, sous
`OWNER_SCOPE_ENABLED=1`, l'ingestion d'un non-owner sans `RagSpaceAccess` était
réellement annulée avec `access_missing`.

La cause n'est pas dans les drapeaux mais dans le CHEMIN :

- `require_ragspace_access` (HTTP) appelle `_decide_ragspace` avec
  `apply_acl=_ragspace_acl_enforced()` — c'est-à-dire `ENABLED and not AUDIT_ONLY` :
  **correct**, l'audit-only n'y bloque pas ;
- `revalidate_task_access` (Celery) appelait `target_ragspace_decision`, qui
  applique `_acl_considered()` — `ENABLED **or** AUDIT_ONLY`. Cette fonction est la
  fonction de SIMULATION ; son verdict était ensuite traité comme effectif par
  `enforce_or_log`.

Autrement dit : le worker utilisait la décision **simulée** comme décision
**effective**. Deux sémantiques pour un même contrat.

Ces tests fixent la matrice pour les DEUX chemins, afin qu'ils ne puissent plus
diverger.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
# `access.py` lève le `PermissionDenied` de DJANGO (pas celui de DRF) et le
# `NotFound` de DRF : on attrape les deux familles, sinon le test se trompe de
# diagnostic.
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from rest_framework.exceptions import NotFound
from rest_framework.exceptions import PermissionDenied as DrfPermissionDenied

from apps.rag_core.models import RagSpace, RagSpaceAccess
from apps.workspaces.models import Membership, Workspace
from apps.workspaces.services import task_guard
from apps.workspaces.services.access import require_ragspace_access

User = get_user_model()
pytestmark = pytest.mark.django_db

# Combinaisons de drapeaux. `OWNER_SCOPE_ENABLED=1` partout : l'ACL n'est évaluée
# qu'à l'intérieur du périmètre owner (cf. system check workspaces.E001).
DISABLED = {"OWNER_SCOPE_ENABLED": True, "OWNER_SCOPE_AUDIT_ONLY": False,
            "RAGSPACE_ACL_ENABLED": False, "RAGSPACE_ACL_AUDIT_ONLY": False}
AUDIT_ONLY = {**DISABLED, "RAGSPACE_ACL_AUDIT_ONLY": True}
ENFORCED = {**DISABLED, "RAGSPACE_ACL_ENABLED": True}
BOTH = {**DISABLED, "RAGSPACE_ACL_ENABLED": True, "RAGSPACE_ACL_AUDIT_ONLY": True}


@pytest.fixture
def world(db):
    """Un espace du périmètre de l'owner, et un ADMIN du même périmètre SANS ACL.

    C'est exactement la configuration qui échouait en staging : l'admin est dans le
    bon périmètre owner, mais ne possède aucun `RagSpaceAccess`.
    """
    ws = Workspace.objects.create(name="ACL-WS")
    owner_u = User.objects.create_user("acl_owner", "acl_owner@t.local", "x")
    admin_u = User.objects.create_user("acl_admin", "acl_admin@t.local", "x")
    owner = Membership.objects.create(workspace=ws, user=owner_u, role=Membership.ROLE_OWNER)
    admin = Membership.objects.create(
        workspace=ws, user=admin_u, role=Membership.ROLE_ADMIN, owner_scope=owner
    )
    space = RagSpace.objects.create(workspace=ws, name="S", owner_scope=owner)
    return {"ws": ws, "owner_u": owner_u, "admin_u": admin_u,
            "owner": owner, "admin": admin, "space": space}


def _http_allows(user, space, permission="write") -> bool:
    """Le chemin HTTP laisse-t-il passer ?"""
    try:
        require_ragspace_access(user=user, rag_space_id=space.id, permission=permission)
        return True
    except (DjangoPermissionDenied, DrfPermissionDenied, NotFound):
        return False


def _celery_allows(user, space, permission="write") -> bool:
    """Le chemin Celery laisse-t-il la tâche continuer ?"""
    proceed, _reason = task_guard.enforce_or_log(
        user_id=user.id, rag_space_id=space.id, required_permission=permission,
        job_label="test", require_context=True,
    )
    return proceed


# ===========================================================================
# 1. Le cœur : audit-only ne bloque JAMAIS, sur AUCUN des deux chemins
# ===========================================================================
@override_settings(**AUDIT_ONLY)
def test_audit_only_never_blocks_http(world):
    assert _http_allows(world["admin_u"], world["space"]) is True


@override_settings(**AUDIT_ONLY)
def test_audit_only_never_blocks_celery(world):
    """LE test du lot : c'est ici que l'ingestion était annulée en staging."""
    assert _celery_allows(world["admin_u"], world["space"]) is True


@override_settings(**AUDIT_ONLY)
def test_audit_only_still_records_what_it_would_have_denied(world):
    """Observer sans bloquer n'a de valeur que si l'observation est écrite."""
    from apps.workspaces.services.access import simulated_ragspace_decision

    allowed, reason = simulated_ragspace_decision(
        user=world["admin_u"], rag_space=world["space"], permission="write"
    )
    assert allowed is False
    assert reason == "access_missing"


# ===========================================================================
# 2. Les deux chemins produisent le MÊME verdict, mode par mode
# ===========================================================================
@pytest.mark.parametrize(
    "flags,expected",
    [
        pytest.param(DISABLED, True, id="acl-disabled"),
        pytest.param(AUDIT_ONLY, True, id="acl-audit-only"),
        pytest.param(ENFORCED, False, id="acl-enforced"),
        # Les deux drapeaux à 1 : `audit_only` l'emporte, comme pour owner-scope
        # (`_owner_scope_enforced` = ENABLED and not AUDIT_ONLY). Une convention
        # unique pour les deux mécanismes ; cf. docs/03-backend/owner-scope.md.
        pytest.param(BOTH, True, id="acl-both-flags"),
    ],
)
def test_http_and_celery_agree_in_every_mode(world, flags, expected):
    with override_settings(**flags):
        http = _http_allows(world["admin_u"], world["space"])
        celery = _celery_allows(world["admin_u"], world["space"])
    assert http == celery, (
        f"DIVERGENCE HTTP={http} Celery={celery} — les deux chemins doivent "
        "appliquer le même contrat"
    )
    assert http is expected


# ===========================================================================
# 3. Enforcement : un refus ACL bloque réellement, des deux côtés
# ===========================================================================
@override_settings(**ENFORCED)
def test_enforcement_blocks_without_access(world):
    assert _http_allows(world["admin_u"], world["space"]) is False
    assert _celery_allows(world["admin_u"], world["space"]) is False


@override_settings(**ENFORCED)
def test_enforcement_allows_with_a_sufficient_grant(world):
    RagSpaceAccess.objects.create(
        rag_space=world["space"], user=world["admin_u"],
        space_role=RagSpaceAccess.ROLE_EDITOR, assigned_by=world["owner_u"],
    )
    assert _http_allows(world["admin_u"], world["space"]) is True
    assert _celery_allows(world["admin_u"], world["space"]) is True


@override_settings(**ENFORCED)
def test_the_owner_of_the_scope_never_needs_a_grant(world):
    """L'owner responsable bypasse l'ACL : c'est la doctrine existante."""
    assert _http_allows(world["owner_u"], world["space"]) is True
    assert _celery_allows(world["owner_u"], world["space"]) is True


# ===========================================================================
# 4. Révocation entre la requête et l'exécution du worker
# ===========================================================================
def test_revocation_between_request_and_worker(world):
    """La revalidation garde tout son intérêt : c'est sa raison d'être.

    En enforcement, un droit retiré après l'enqueue DOIT annuler la tâche. En
    audit-only, la tâche continue — mais l'observation est écrite.
    """
    grant = RagSpaceAccess.objects.create(
        rag_space=world["space"], user=world["admin_u"],
        space_role=RagSpaceAccess.ROLE_EDITOR, assigned_by=world["owner_u"],
    )
    with override_settings(**ENFORCED):
        assert _celery_allows(world["admin_u"], world["space"]) is True

    from django.utils import timezone

    grant.revoked_at = timezone.now()
    grant.save(update_fields=["revoked_at"])

    with override_settings(**ENFORCED):
        proceed, reason = task_guard.enforce_or_log(
            user_id=world["admin_u"].id, rag_space_id=world["space"].id,
            required_permission="write", job_label="test", require_context=True,
        )
        assert proceed is False
        assert reason == task_guard.ACCESS_REVOKED

    with override_settings(**AUDIT_ONLY):
        proceed, _ = task_guard.enforce_or_log(
            user_id=world["admin_u"].id, rag_space_id=world["space"].id,
            required_permission="write", job_label="test", require_context=True,
        )
        assert proceed is True, "audit-only ne doit JAMAIS annuler une tâche"


# ===========================================================================
# 5. L'owner-scope, lui, continue de bloquer — l'ACL n'est pas la seule couche
# ===========================================================================
@override_settings(**AUDIT_ONLY)
def test_audit_only_acl_does_not_disable_owner_scope(world):
    """Neutraliser l'ACL ne doit pas neutraliser le périmètre owner.

    Un acteur d'un AUTRE périmètre reste refusé, quel que soit le mode ACL.
    """
    other_u = User.objects.create_user("acl_other", "acl_other@t.local", "x")
    Membership.objects.create(
        workspace=world["ws"], user=other_u, role=Membership.ROLE_OWNER
    )
    assert _http_allows(other_u, world["space"]) is False
    assert _celery_allows(other_u, world["space"]) is False


@override_settings(**AUDIT_ONLY)
def test_audit_only_acl_does_not_disable_the_membership_check(world):
    outsider = User.objects.create_user("acl_out", "acl_out@t.local", "x")
    assert _http_allows(outsider, world["space"]) is False
    assert _celery_allows(outsider, world["space"]) is False


# ===========================================================================
# 6. Garde structurelle
# ===========================================================================
def test_no_blocking_path_mixes_the_two_acl_flags():
    """`ENABLED or AUDIT_ONLY` ne doit servir qu'à SIMULER, jamais à refuser.

    Un test de comportement ne voit pas une réintroduction dans un chemin non
    couvert ; cette garde lit le source.
    """
    import re
    from pathlib import Path

    repo = Path(__file__).resolve().parents[3]

    def active_code(path: str) -> str:
        """Source sans commentaires ni docstrings.

        Les commentaires CITENT volontairement l'ancien nom pour expliquer le
        défaut ; une garde qui interdirait d'en parler rendrait le code moins
        compréhensible sans rien protéger de plus.
        """
        import ast

        src = (repo / path).read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef, ast.Module)):
                doc = ast.get_docstring(node, clean=False)
                if doc:
                    src = src.replace(doc, "")
        return "\n".join(
            re.sub(r"#.*$", "", line) for line in src.splitlines()
        )

    # Aucun chemin BLOQUANT ne doit relire les deux drapeaux : `resolve_acl_mode()`
    # est la seule lecture autorisée.
    access = active_code("backend/apps/workspaces/services/access.py")
    readers = re.findall(r'RAGSPACE_ACL_(?:ENABLED|AUDIT_ONLY)', access)
    assert len(readers) == 2, (
        f"{len(readers)} lectures brutes des drapeaux ACL dans access.py — seules "
        "les deux de `resolve_acl_mode()` sont tolérées"
    )

    # Le garde de tâche doit décider sur la décision EFFECTIVE.
    guard = active_code("backend/apps/workspaces/services/task_guard.py")
    assert "effective_ragspace_decision" in guard, (
        "task_guard doit décider sur la décision effective"
    )
    assert "target_ragspace_decision" not in guard, (
        "task_guard ne doit plus utiliser la fonction de SIMULATION pour décider "
        "d'annuler une tâche — c'est l'origine du défaut P0-1"
    )
    # Aucun autre module ne doit recomposer le mode à la main.
    for path in ("backend/apps/workspaces/services/task_guard.py",
                 "backend/apps/workspaces/services/scope_audit.py"):
        code = active_code(path)
        assert "RAGSPACE_ACL_ENABLED" not in code, (
            f"{path} relit un drapeau ACL — passer par `resolve_acl_mode()`"
        )


# ===========================================================================
# 7. Accès initial du créateur — l'invariant « créer, c'est pouvoir utiliser »
# ===========================================================================
@pytest.fixture
def creators(db):
    """Un workspace avec un owner et un admin, tous deux habilités à créer."""
    from rest_framework.test import APIClient

    ws = Workspace.objects.create(name="CREA-WS")
    owner_u = User.objects.create_user("crea_owner", "crea_owner@t.local", "x")
    admin_u = User.objects.create_user("crea_admin", "crea_admin@t.local", "x")
    su_u = User.objects.create_user(
        "crea_su", "crea_su@t.local", "x", is_superuser=True, is_staff=True
    )
    owner = Membership.objects.create(workspace=ws, user=owner_u, role=Membership.ROLE_OWNER)
    Membership.objects.create(
        workspace=ws, user=admin_u, role=Membership.ROLE_ADMIN, owner_scope=owner
    )
    Membership.objects.create(
        workspace=ws, user=su_u, role=Membership.ROLE_ADMIN, owner_scope=owner
    )

    def cli(u):
        c = APIClient()
        c.force_authenticate(user=u)
        return c

    return {"ws": ws, "owner_u": owner_u, "admin_u": admin_u, "su_u": su_u,
            "owner": owner, "cli": cli}


def _create_space(creators, user, name):
    r = creators["cli"](user).post(
        f"/api/v1/workspaces/{creators['ws'].id}/ragspaces/",
        {"name": name}, format="json", HTTP_HOST="localhost",
    )
    assert r.status_code == 201, r.data
    return RagSpace.objects.get(id=r.data["id"])


@pytest.mark.parametrize("actor_key", ["admin_u", "su_u"])
def test_a_non_owner_creator_receives_a_minimal_grant(creators, actor_key):
    """Le créateur non-owner reçoit `editor` : utiliser, pas administrer."""
    space = _create_space(creators, creators[actor_key], f"S-{actor_key}")
    grant = RagSpaceAccess.objects.get(
        rag_space=space, user=creators[actor_key], revoked_at__isnull=True
    )
    assert grant.space_role == RagSpaceAccess.ROLE_EDITOR
    assert grant.space_role != RagSpaceAccess.ROLE_MANAGER, (
        "un créateur n'a pas besoin d'administrer les ACL pour utiliser son espace"
    )
    assert grant.assigned_by_id == creators[actor_key].id


def test_an_owner_creator_receives_no_grant(creators):
    """L'owner bypasse l'ACL : matérialiser un grant créerait une seconde source
    de vérité pour un droit déjà acquis."""
    space = _create_space(creators, creators["owner_u"], "S-owner")
    assert not RagSpaceAccess.objects.filter(rag_space=space).exists()


def test_no_grant_is_diffused_to_other_members(creators):
    """Aucun accès n'est accordé automatiquement aux autres membres."""
    space = _create_space(creators, creators["admin_u"], "S-solo")
    holders = set(
        RagSpaceAccess.objects.filter(rag_space=space, revoked_at__isnull=True)
        .values_list("user_id", flat=True)
    )
    assert holders == {creators["admin_u"].id}


def test_a_creator_outside_the_scope_still_creates_but_gets_no_grant(creators):
    """Membership NON rattaché à un périmètre : la création reste valide.

    Régression. Le périmètre de l'espace est alors résolu par la règle de repli
    `single_owner`, donc l'acteur n'appartient pas au périmètre attribué. Poser
    un grant serait hors périmètre ; refuser la création transformerait une
    donnée à corriger en erreur 500. On crée, sans grant.
    """
    unattached_u = User.objects.create_user("crea_free", "crea_free@t.local", "x")
    Membership.objects.create(
        workspace=creators["ws"], user=unattached_u,
        role=Membership.ROLE_ADMIN, owner_scope=None,
    )

    space = _create_space(creators, unattached_u, "S-unattached")

    assert space.owner_scope_id == creators["owner"].id, (
        "le périmètre reste attribué par la règle de repli `single_owner`"
    )
    assert not RagSpaceAccess.objects.filter(rag_space=space).exists(), (
        "aucun grant hors périmètre ne doit être posé"
    )


@override_settings(**ENFORCED)
@pytest.mark.parametrize("actor_key", ["owner_u", "admin_u", "su_u"])
def test_the_creator_can_use_the_space_immediately_under_enforcement(creators, actor_key):
    """L'INVARIANT du lot : créer, c'est pouvoir utiliser — API ET worker."""
    space = _create_space(creators, creators[actor_key], f"USE-{actor_key}")
    assert _http_allows(creators[actor_key], space) is True
    assert _celery_allows(creators[actor_key], space) is True


def test_the_initial_grant_is_idempotent(creators):
    """Un accès existant n'est ni dupliqué, ni abaissé, ni élevé."""
    from apps.workspaces.services.ragspace_creation import _ensure_creator_access

    space = _create_space(creators, creators["admin_u"], "IDEM")
    grant = RagSpaceAccess.objects.get(rag_space=space, user=creators["admin_u"])
    grant.space_role = RagSpaceAccess.ROLE_MANAGER  # octroi délibéré, plus élevé
    grant.save(update_fields=["space_role"])

    again = _ensure_creator_access(
        rag_space=space, owner_scope=creators["owner"],
        actor=creators["admin_u"], actor_membership=None,
    )
    assert again.pk == grant.pk
    assert RagSpaceAccess.objects.filter(
        rag_space=space, revoked_at__isnull=True
    ).count() == 1
    grant.refresh_from_db()
    assert grant.space_role == RagSpaceAccess.ROLE_MANAGER, (
        "un droit délibérément élevé ne doit pas être rabaissé en silence"
    )


def test_a_failure_while_granting_leaves_no_space_behind(creators):
    """Atomicité : l'espace ET son accès, ou rien."""
    from unittest.mock import patch

    from apps.workspaces.services.ragspace_creation import create_ragspace

    before = RagSpace.objects.count()
    with patch(
        "apps.workspaces.services.ragspace_creation._ensure_creator_access",
        side_effect=RuntimeError("boom"),
    ):
        with pytest.raises(RuntimeError):
            create_ragspace(
                workspace=creators["ws"], name="ROLLBACK", actor=creators["admin_u"]
            )
    assert RagSpace.objects.count() == before
    assert not RagSpace.objects.filter(name="ROLLBACK").exists()
    assert not RagSpaceAccess.objects.filter(rag_space__name="ROLLBACK").exists()


def test_the_creation_audit_records_the_granted_level(creators):
    from apps.audit.models import ActionAuditEvent

    space = _create_space(creators, creators["admin_u"], "AUDITED")
    ev = ActionAuditEvent.objects.filter(
        action_name="ragspace.created", rag_space_id=space.id
    ).first()
    assert ev is not None
    assert ev.metadata.get("creator_space_role") == RagSpaceAccess.ROLE_EDITOR

    owner_space = _create_space(creators, creators["owner_u"], "AUDITED-OWNER")
    ev2 = ActionAuditEvent.objects.filter(
        action_name="ragspace.created", rag_space_id=owner_space.id
    ).first()
    assert ev2.metadata.get("creator_space_role") is None, (
        "aucun grant pour un owner : l'audit doit le dire, pas inventer un niveau"
    )
