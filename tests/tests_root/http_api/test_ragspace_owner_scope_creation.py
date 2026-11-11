"""Invariant : tout RagSpace créé porte un périmètre owner, atomiquement.

## Le défaut couvert

Sous `OWNER_SCOPE_ENABLED=1`, un RagSpace sans `owner_scope` est **inutilisable** :
`_decide_ragspace` échoue en `ragspace_without_scope` et `require_ragspace_access`
répond **404 anti-énumération**. L'utilisateur voyait son espace dans la liste,
l'ouvrait, et l'upload lui répondait « introuvable ».

Constaté en staging le 2026-07-28 : chaque espace créé par l'API arrivait avec
`owner_scope=None`.

Ces tests vérifient la DONNÉE et le CHEMIN de création. Ils ne touchent pas aux
contrôles d'accès : le fail-closed de `_decide_ragspace` est délibéré et correct,
et les tests d'étanchéité ci-dessous le prouvent toujours actif.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APIClient

from apps.rag_core.models import RagSpace
from apps.workspaces.models import Membership, Workspace
from apps.workspaces.services.ragspace_creation import (
    InvalidOwnerScopeError,
    OwnerScopeUnresolvedError,
    WorkspaceDisabledError,
    create_ragspace,
    resolve_owner_scope_for_ragspace,
)

User = get_user_model()
pytestmark = pytest.mark.django_db
H = {"HTTP_HOST": "localhost"}

ENFORCED = {"OWNER_SCOPE_ENABLED": True, "OWNER_SCOPE_AUDIT_ONLY": False}


def _cli(user):
    c = APIClient()
    c.force_authenticate(user=user)
    return c


@pytest.fixture
def world(db):
    """Un workspace, son owner, un admin et un membre — tous rattachés."""
    ws = Workspace.objects.create(name="OS-WS")
    owner_u = User.objects.create_user("os_owner", "os_owner@t.local", "x")
    admin_u = User.objects.create_user("os_admin", "os_admin@t.local", "x")
    member_u = User.objects.create_user("os_member", "os_member@t.local", "x")
    su = User.objects.create_user(
        "os_su", "os_su@t.local", "x", is_superuser=True, is_staff=True
    )
    owner = Membership.objects.create(workspace=ws, user=owner_u, role=Membership.ROLE_OWNER)
    admin = Membership.objects.create(
        workspace=ws, user=admin_u, role=Membership.ROLE_ADMIN, owner_scope=owner
    )
    member = Membership.objects.create(
        workspace=ws, user=member_u, role=Membership.ROLE_MEMBER, owner_scope=owner
    )
    su_m = Membership.objects.create(
        workspace=ws, user=su, role=Membership.ROLE_ADMIN, owner_scope=owner
    )
    return {
        "ws": ws, "owner": owner, "admin": admin, "member": member, "su_m": su_m,
        "owner_u": owner_u, "admin_u": admin_u, "member_u": member_u, "su": su,
    }


def _url(ws):
    return f"/api/v1/workspaces/{ws.id}/ragspaces/"


# ===========================================================================
# 1. Création via l'API — tous les rôles convergent
# ===========================================================================
@pytest.mark.parametrize("actor_key", ["owner_u", "admin_u", "su"])
def test_api_creation_always_assigns_the_canonical_scope(world, actor_key):
    """Owner, admin et superuser produisent le MÊME périmètre : celui de l'owner.

    Le superuser ne contourne pas la règle — il n'a pas de traitement spécial ici.
    """
    r = _cli(world[actor_key]).post(_url(world["ws"]), {"name": f"S-{actor_key}"},
                                    format="json", **H)
    assert r.status_code == 201, r.data
    space = RagSpace.objects.get(id=r.data["id"])
    assert space.owner_scope_id == world["owner"].pk
    assert space.owner_scope_id is not None


def test_the_client_can_never_choose_the_scope(world):
    """`owner_scope` n'est pas un champ d'entrée : un payload qui le porte est ignoré.

    Le modèle le dit : « Le client ne choisit JAMAIS cette valeur ». Si le
    serializer venait à l'accepter, un acteur pourrait rattacher un espace au
    périmètre d'un autre.
    """
    other_ws = Workspace.objects.create(name="AUTRE")
    other_owner = Membership.objects.create(
        workspace=other_ws,
        user=User.objects.create_user("autre", "autre@t.local", "x"),
        role=Membership.ROLE_OWNER,
    )
    r = _cli(world["owner_u"]).post(
        _url(world["ws"]),
        {"name": "TENTATIVE", "owner_scope": other_owner.pk},
        format="json", **H,
    )
    assert r.status_code == 201, r.data
    space = RagSpace.objects.get(id=r.data["id"])
    assert space.owner_scope_id == world["owner"].pk
    assert space.owner_scope_id != other_owner.pk


def test_creation_records_the_creator_for_audit(world):
    r = _cli(world["admin_u"]).post(_url(world["ws"]), {"name": "TRACE"},
                                    format="json", **H)
    assert RagSpace.objects.get(id=r.data["id"]).created_by_id == world["admin_u"].id


def test_creation_emits_an_audit_event_with_the_assigned_scope(world):
    from apps.audit.models import ActionAuditEvent

    r = _cli(world["owner_u"]).post(_url(world["ws"]), {"name": "AUDITED"},
                                    format="json", **H)
    assert r.status_code == 201
    ev = ActionAuditEvent.objects.filter(
        action_name="ragspace.created", rag_space_id=r.data["id"]
    ).first()
    assert ev is not None, "la création doit être tracée"
    assert ev.metadata.get("owner_scope_membership_id") == world["owner"].pk


# ===========================================================================
# 2. Aucune ligne sans périmètre — quel que soit le drapeau
# ===========================================================================
@pytest.mark.parametrize(
    "flags",
    [pytest.param({}, id="flag-off"), pytest.param(ENFORCED, id="flag-on")],
)
def test_no_ragspace_is_ever_created_without_a_scope(world, flags):
    """Le remplissage est SYSTÉMATIQUE, y compris drapeau éteint.

    Attendre l'activation pour renseigner le périmètre imposerait une migration
    massive le jour J — et laisserait des lignes incomplètes entre-temps.
    """
    with override_settings(**flags):
        _cli(world["owner_u"]).post(_url(world["ws"]), {"name": "F"}, format="json", **H)
    assert not RagSpace.objects.filter(owner_scope__isnull=True).exists()


def test_direct_service_call_never_leaves_a_partial_object(world):
    """Atomicité : une erreur APRÈS résolution du scope ne laisse aucune ligne.

    On force l'échec sur l'écriture de l'audit — étape qui suit la création dans
    la même transaction.
    """
    from unittest.mock import patch

    before = RagSpace.objects.count()
    with patch(
        "apps.workspaces.services.ragspace_creation._audit_created",
        side_effect=RuntimeError("boom"),
    ):
        with pytest.raises(RuntimeError):
            create_ragspace(workspace=world["ws"], name="ROLLBACK", actor=world["owner_u"])
    assert RagSpace.objects.count() == before
    assert not RagSpace.objects.filter(name="ROLLBACK").exists()


# ===========================================================================
# 3. Résolution du périmètre — fonction pure
# ===========================================================================
def test_an_owner_is_its_own_scope(world):
    assert resolve_owner_scope_for_ragspace(
        workspace=world["ws"], actor_membership=world["owner"]
    ) == world["owner"]


def test_a_non_owner_resolves_to_its_owner_scope(world):
    for key in ("admin", "member"):
        assert resolve_owner_scope_for_ragspace(
            workspace=world["ws"], actor_membership=world[key]
        ) == world["owner"]


def test_an_unattached_non_owner_falls_back_to_the_single_owner(world):
    """Période de backfill : un non-owner sans `owner_scope` retombe sur l'owner
    UNIQUE du workspace — exactement la règle de `backfill_workspace`."""
    orphan = Membership.objects.create(
        workspace=world["ws"],
        user=User.objects.create_user("orph", "orph@t.local", "x"),
        role=Membership.ROLE_MEMBER,
    )
    assert resolve_owner_scope_for_ragspace(
        workspace=world["ws"], actor_membership=orphan
    ) == world["owner"]


def test_resolution_refuses_when_the_workspace_has_no_single_owner(world):
    """0 ou >1 owner : on ne devine pas lequel gouverne — refus, aucune écriture."""
    second_owner_ws = Workspace.objects.create(name="MULTI")
    for i in (1, 2):
        Membership.objects.create(
            workspace=second_owner_ws,
            user=User.objects.create_user(f"o{i}", f"o{i}@t.local", "x"),
            role=Membership.ROLE_OWNER,
        )
    orphan = Membership.objects.create(
        workspace=second_owner_ws,
        user=User.objects.create_user("m9", "m9@t.local", "x"),
        role=Membership.ROLE_MEMBER,
    )
    with pytest.raises(OwnerScopeUnresolvedError):
        resolve_owner_scope_for_ragspace(
            workspace=second_owner_ws, actor_membership=orphan
        )
    assert not RagSpace.objects.filter(workspace=second_owner_ws).exists()


@pytest.mark.parametrize("bad", ["non_owner", "other_workspace"])
def test_an_explicit_scope_is_validated(world, bad):
    if bad == "non_owner":
        target = world["member"]
    else:
        other = Workspace.objects.create(name="X")
        target = Membership.objects.create(
            workspace=other,
            user=User.objects.create_user("x1", "x1@t.local", "x"),
            role=Membership.ROLE_OWNER,
        )
    with pytest.raises(InvalidOwnerScopeError):
        resolve_owner_scope_for_ragspace(workspace=world["ws"], requested_scope=target)


# ===========================================================================
# 4. Workspace désactivé
# ===========================================================================
def test_a_disabled_workspace_is_refused_before_any_write(world):
    """Un espace créé là ne serait traité par aucune tâche
    (`task_guard.WORKSPACE_DISABLED`) : autant refuser franchement."""
    world["ws"].is_active = False
    world["ws"].save(update_fields=["is_active"])
    before = RagSpace.objects.count()
    with pytest.raises(WorkspaceDisabledError):
        create_ragspace(workspace=world["ws"], name="MORT", actor=world["owner_u"])
    assert RagSpace.objects.count() == before


def test_the_api_refuses_a_disabled_workspace_with_a_stable_code(world):
    world["ws"].is_active = False
    world["ws"].save(update_fields=["is_active"])
    r = _cli(world["owner_u"]).post(_url(world["ws"]), {"name": "MORT"},
                                    format="json", **H)
    assert r.status_code == 409, r.data
    assert r.data["code"] == "workspace_disabled"
    assert not RagSpace.objects.filter(name="MORT").exists()


# ===========================================================================
# 5. Parcours complet sous ENFORCEMENT — le défaut d'origine
# ===========================================================================
@override_settings(**ENFORCED)
@pytest.mark.parametrize("actor_key", ["owner_u", "admin_u", "su"])
def test_created_space_is_immediately_usable_under_enforcement(world, actor_key):
    """LE test du lot : création -> listing -> détail -> upload, sans 404.

    Avant correction, l'upload répondait 404 `ragspace_without_scope`.
    """
    cli = _cli(world[actor_key])
    r = cli.post(_url(world["ws"]), {"name": f"USABLE-{actor_key}"}, format="json", **H)
    assert r.status_code == 201, r.data
    space_id = r.data["id"]

    # Listing : l'espace apparaît.
    listing = cli.get(_url(world["ws"]), **H)
    assert listing.status_code == 200
    assert any(str(i["id"]) == str(space_id) for i in listing.data)

    # Upload : le point exact qui échouait.
    from django.core.files.uploadedfile import SimpleUploadedFile

    up = cli.post(
        "/api/v1/documents/upload/",
        {"rag_space_id": str(space_id),
         "file": SimpleUploadedFile("n.txt", b"contenu de test", content_type="text/plain")},
        format="multipart", **H,
    )
    assert up.status_code != 404, (
        f"404 anti-énumération sur un espace légitime : {up.data}"
    )
    assert up.status_code in (200, 201, 202), up.data


# ===========================================================================
# 6. Étanchéité — le correctif n'élargit AUCUN droit
# ===========================================================================
@override_settings(**ENFORCED)
def test_an_actor_of_another_scope_cannot_see_or_upload(world):
    """Le fail-closed reste actif : un autre périmètre ne voit rien.

    Deux owners dans le MÊME workspace = deux périmètres distincts. L'espace créé
    par l'un doit rester invisible à l'autre.
    """
    from django.core.files.uploadedfile import SimpleUploadedFile

    other_owner_u = User.objects.create_user("os_owner2", "os_owner2@t.local", "x")
    Membership.objects.create(
        workspace=world["ws"], user=other_owner_u, role=Membership.ROLE_OWNER
    )

    r = _cli(world["owner_u"]).post(_url(world["ws"]), {"name": "PRIVE"},
                                    format="json", **H)
    space_id = r.data["id"]

    intruder = _cli(other_owner_u)
    up = intruder.post(
        "/api/v1/documents/upload/",
        {"rag_space_id": str(space_id),
         "file": SimpleUploadedFile("x.txt", b"intrusion", content_type="text/plain")},
        format="multipart", **H,
    )
    assert up.status_code == 404, (
        f"un acteur hors périmètre doit recevoir 404 anti-énumération, reçu {up.status_code}"
    )


@override_settings(**ENFORCED)
def test_a_non_member_of_the_workspace_is_still_refused(world):
    outsider = User.objects.create_user("out", "out@t.local", "x")
    r = _cli(world["owner_u"]).post(_url(world["ws"]), {"name": "INTERNE"},
                                    format="json", **H)
    assert _cli(outsider).post(
        _url(world["ws"]), {"name": "PIRATE"}, format="json", **H
    ).status_code in (403, 404)
    assert RagSpace.objects.filter(name="PIRATE").count() == 0
    assert RagSpace.objects.filter(id=r.data["id"]).exists()


# ===========================================================================
# 7. Garde : aucun chemin de création ne contourne le service
# ===========================================================================
def test_no_product_code_creates_a_ragspace_outside_the_service():
    """Les seuls `RagSpace.objects.create` tolérés sont énumérés ici.

    Un chemin PRODUIT qui créerait directement le modèle réintroduirait le défaut
    en silence. Les exceptions listées sont des seeds/fixtures dont le workspace
    n'a AUCUN membership : aucun utilisateur ne peut les atteindre, et sous
    enforcement elles sont fail-closed comme il se doit.
    """
    import re
    from pathlib import Path

    repo = Path(__file__).resolve().parents[3]
    allowed = {
        # Migration/replay : le périmètre est fourni EXPLICITEMENT.
        "backend/apps/workspaces/management/commands/owner_scope_replay.py",
        # Seeds de benchmark : workspaces sans membership.
        "backend/apps/rag_core/management/commands/seed_demo_benchmark.py",
        "backend/apps/rag_core/management/commands/seed_demo_benchmark_extended.py",
        "backend/apps/rag_core/management/commands/seed_demo_benchmark_extended_v3.py",
        "backend/apps/rag_core/management/commands/seed_demo_benchmark_extended_v3_1.py",
        "backend/apps/rag_core/management/commands/bench_msg_retrieval.py",
        # Fixtures de tests.
        "backend/testing/fixtures.py",
        # Le service lui-même.
        "backend/apps/workspaces/services/ragspace_creation.py",
    }
    pattern = re.compile(r"RagSpace\.objects\.create\s*\(")
    offenders = []
    for py in (repo / "backend").rglob("*.py"):
        rel = str(py.relative_to(repo))
        if "/migrations/" in rel or rel in allowed:
            continue
        text = py.read_text(encoding="utf-8", errors="replace")
        active = "\n".join(
            l for l in text.splitlines() if not l.lstrip().startswith("#")
        )
        if pattern.search(active):
            offenders.append(rel)
    assert not offenders, (
        "création directe de RagSpace hors du service : " + ", ".join(offenders)
        + " — utiliser apps.workspaces.services.ragspace_creation.create_ragspace"
    )
