"""Test — commande d'inventaire ACL (lecture seule, proposition prudente)."""
import io
import json

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command

from apps.rag_core.models import RagSpace, RagSpaceAccess
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
pytestmark = pytest.mark.django_db


def test_inventory_proposes_only_unambiguous_viewer(db):
    ws = Workspace.objects.create(name="INV")
    uo = User.objects.create_user("inv_o", "inv_o@t.local", "x")
    uad = User.objects.create_user("inv_ad", "inv_ad@t.local", "x")
    um = User.objects.create_user("inv_m", "inv_m@t.local", "x")
    uv = User.objects.create_user("inv_v", "inv_v@t.local", "x")
    owner = Membership.objects.create(workspace=ws, user=uo, role="owner")
    Membership.objects.create(workspace=ws, user=uad, role="admin", owner_scope=owner)
    Membership.objects.create(workspace=ws, user=um, role="member", owner_scope=owner)
    Membership.objects.create(workspace=ws, user=uv, role="viewer", owner_scope=owner)
    rs = RagSpace.objects.create(workspace=ws, name="A", owner_scope=owner)

    out = io.StringIO()
    call_command("owner_scope_acl_inventory", "--workspace-id", str(ws.id), "--format", "json", stdout=out)
    data = json.loads(out.getvalue())
    by_role = {m["workspace_role"]: m for m in data["inventory"]["members"]}

    assert by_role["owner"]["proposed_space_role"] is None
    assert by_role["owner"]["proposal_status"] == "no_grant_owner_implicit"
    assert by_role["admin"]["proposed_space_role"] is None      # ambigu -> non mappé
    assert by_role["member"]["proposed_space_role"] is None     # ambigu -> non mappé
    assert by_role["viewer"]["proposed_space_role"] == "viewer"  # non ambigu

    # proposition = uniquement le viewer, pour l'espace du périmètre
    grants = data["proposal"]["grants"]
    assert len(grants) == 1 and grants[0]["user_id"] == uv.id and grants[0]["space_role"] == "viewer"
    # lecture seule : rien écrit
    assert not RagSpaceAccess.objects.exists()


def test_inventory_skips_spaces_with_existing_grant(db):
    ws = Workspace.objects.create(name="INV2")
    uo = User.objects.create_user("inv2_o", "inv2_o@t.local", "x")
    uv = User.objects.create_user("inv2_v", "inv2_v@t.local", "x")
    owner = Membership.objects.create(workspace=ws, user=uo, role="owner")
    Membership.objects.create(workspace=ws, user=uv, role="viewer", owner_scope=owner)
    rs = RagSpace.objects.create(workspace=ws, name="A", owner_scope=owner)
    RagSpaceAccess.objects.create(rag_space=rs, user=uv, space_role="viewer")  # déjà octroyé

    out = io.StringIO()
    call_command("owner_scope_acl_inventory", "--workspace-id", str(ws.id), "--format", "json", stdout=out)
    data = json.loads(out.getvalue())
    assert data["proposal"]["grants"] == []  # rien à proposer, grant déjà actif
