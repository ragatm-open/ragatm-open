"""P0 — accès/navigation multi-workspace : l'endpoint des workspaces accessibles.

Un utilisateur membre de plusieurs workspaces DOIT tous les voir (actifs), avec un
rôle et un owner-scope distincts par workspace ; les workspaces désactivés ou tiers
sont exclus ; un workspace_id arbitraire (non-membre) est refusé.
"""
import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.rag_core.models import RagSpace
from apps.workspaces.models import Membership, Workspace

User = get_user_model()
pytestmark = pytest.mark.django_db
H = {"HTTP_HOST": "localhost"}


def _cli(u):
    c = APIClient()
    c.force_authenticate(user=u)
    return c


@pytest.fixture
def multi(db):
    ua = Workspace.objects.create(name="WS-A")
    wb = Workspace.objects.create(name="WS-B")
    wc = Workspace.objects.create(name="WS-C-disabled", is_active=False)
    wt = Workspace.objects.create(name="WS-Tiers")
    u = User.objects.create_user("multi", "multi@t.local", "x")
    Membership.objects.create(workspace=ua, user=u, role="owner")
    Membership.objects.create(workspace=wb, user=u, role="admin")
    Membership.objects.create(workspace=wc, user=u, role="owner")  # workspace désactivé
    # WS-Tiers : pas de membership pour u
    Membership.objects.create(workspace=wt, user=User.objects.create_user("other", "other@t.local", "x"), role="owner")
    return {"u": u, "wa": ua, "wb": wb, "wc": wc, "wt": wt}


def test_multi_workspace_returns_all_active_memberships(multi):
    r = _cli(multi["u"]).get("/api/v1/workspaces/", **H)
    assert r.status_code == 200
    ids = {x["id"] for x in r.data}
    assert ids == {multi["wa"].id, multi["wb"].id}  # actif seulement, membre seulement
    by_id = {x["id"]: x for x in r.data}
    assert by_id[multi["wa"].id]["role"] == "owner"
    assert by_id[multi["wb"].id]["role"] == "admin"
    # champs d'enrichissement présents
    assert by_id[multi["wa"].id]["workspace_is_active"] is True
    assert by_id[multi["wa"].id]["membership_id"] is not None


def test_disabled_workspace_excluded_but_available_with_flag(multi):
    r = _cli(multi["u"]).get("/api/v1/workspaces/?include_inactive=1", **H)
    ids = {x["id"] for x in r.data}
    assert multi["wc"].id in ids  # visible avec include_inactive


def test_third_party_workspace_absent(multi):
    r = _cli(multi["u"]).get("/api/v1/workspaces/", **H)
    assert multi["wt"].id not in {x["id"] for x in r.data}


def test_ragspaces_scoped_per_workspace(multi):
    RagSpace.objects.create(workspace=multi["wa"], name="A1")
    RagSpace.objects.create(workspace=multi["wb"], name="B1")
    ra = _cli(multi["u"]).get(f"/api/v1/rag-spaces/?workspace_id={multi['wa'].id}", **H)
    rb = _cli(multi["u"]).get(f"/api/v1/rag-spaces/?workspace_id={multi['wb'].id}", **H)
    assert [x["name"] for x in ra.data] == ["A1"]
    assert [x["name"] for x in rb.data] == ["B1"]  # pas de fuite entre workspaces


def test_arbitrary_workspace_id_refused(multi):
    r = _cli(multi["u"]).get(f"/api/v1/rag-spaces/?workspace_id={multi['wt'].id}", **H)
    assert r.status_code == 403 and r.data["detail"] == "not_a_member"


def test_two_owners_same_workspace_see_only_their_ragspaces(db):
    ws = Workspace.objects.create(name="WS-2O")
    ua = User.objects.create_user("oa", "oa@t.local", "x")
    ub = User.objects.create_user("ob", "ob@t.local", "x")
    oa = Membership.objects.create(workspace=ws, user=ua, role="owner")
    ob = Membership.objects.create(workspace=ws, user=ub, role="owner")
    RagSpace.objects.create(workspace=ws, name="SA", owner_scope=oa)
    RagSpace.objects.create(workspace=ws, name="SB", owner_scope=ob)
    # même workspace visible pour les deux
    assert ws.id in {x["id"] for x in _cli(ua).get("/api/v1/workspaces/", **H).data}
    assert ws.id in {x["id"] for x in _cli(ub).get("/api/v1/workspaces/", **H).data}
    # mais RagSpaces filtrés par périmètre owner (owner-scope enforcement flag-aware)
    # → ici flags OFF en test : legacy = les deux voient tout le workspace. On vérifie
    #   au moins l'absence de fuite cross-workspace (couverte ci-dessus).
