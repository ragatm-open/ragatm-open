"""recherche et ouverture des conversations.

Sécurité (plan §9/§14), paramétrée sur les TROIS régimes d'accès :
  * ``legacy``      : flags OFF (= production) ;
  * ``owner_scope`` : OWNER_SCOPE_ENABLED, ACL en observation (= staging) ;
  * ``acl``         : owner_scope + RAGSPACE_ACL_ENABLED.

Invariant central : un fil n'apparaît que si son AUTEUR est l'appelant ET que
``require_ragspace_access(read)`` réussirait sur son espace (workspaces inactifs
exclus en plus). Fonctionnel (§16), débit dédié, borne de temps, lien direct et
accès progressif au-delà des 100 fils.
"""
from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.db import connection
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle

from apps.chat.models import ChatMessage, ChatThread
from apps.chat.services import thread_search as TS
from apps.rag_core.models import RagSpace, RagSpaceAccess
from apps.workspaces.models import Membership, Workspace
from apps.workspaces.services.access import require_ragspace_access

User = get_user_model()
pytestmark = pytest.mark.django_db

SEARCH = "/api/v1/chat/threads/search/"
MODES = ("legacy", "owner_scope", "acl")


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(params=MODES)
def mode(request, settings):
    settings.OWNER_SCOPE_ENABLED = request.param != "legacy"
    settings.OWNER_SCOPE_AUDIT_ONLY = False
    settings.RAGSPACE_ACL_ENABLED = request.param == "acl"
    settings.RAGSPACE_ACL_AUDIT_ONLY = request.param == "owner_scope"
    return request.param


@pytest.fixture
def world(db):
    """ws1 (actif) : owner A, owner B, admin/membre/viewer du périmètre A, espaces A, A2, B.
    ws2 (actif) : A owner, espace W2. ws_off (INACTIF) : A owner, espace OFF. Un étranger."""
    ws1 = Workspace.objects.create(name="WS-NAV-1")
    ws2 = Workspace.objects.create(name="WS-NAV-2")
    ws_off = Workspace.objects.create(name="WS-NAV-OFF", is_active=False)
    ua = User.objects.create_user("nav_a", "nav_a@t.local", "x")
    ub = User.objects.create_user("nav_b", "nav_b@t.local", "x")
    uad = User.objects.create_user("nav_ad", "nav_ad@t.local", "x")
    um = User.objects.create_user("nav_m", "nav_m@t.local", "x")
    uv = User.objects.create_user("nav_v", "nav_v@t.local", "x")
    ux = User.objects.create_user("nav_x", "nav_x@t.local", "x")
    owner_a = Membership.objects.create(workspace=ws1, user=ua, role="owner")
    owner_b = Membership.objects.create(workspace=ws1, user=ub, role="owner")
    m_ad = Membership.objects.create(workspace=ws1, user=uad, role="admin", owner_scope=owner_a)
    m_m = Membership.objects.create(workspace=ws1, user=um, role="member", owner_scope=owner_a)
    m_v = Membership.objects.create(workspace=ws1, user=uv, role="viewer", owner_scope=owner_a)
    owner_a2 = Membership.objects.create(workspace=ws2, user=ua, role="owner")
    owner_off = Membership.objects.create(workspace=ws_off, user=ua, role="owner")
    rs_a = RagSpace.objects.create(workspace=ws1, name="Espace Alpha", owner_scope=owner_a)
    rs_a2 = RagSpace.objects.create(workspace=ws1, name="Espace Alpha Deux", owner_scope=owner_a)
    rs_b = RagSpace.objects.create(workspace=ws1, name="Espace Bravo Secret", owner_scope=owner_b)
    rs_w2 = RagSpace.objects.create(workspace=ws2, name="Espace W2", owner_scope=owner_a2)
    rs_off = RagSpace.objects.create(workspace=ws_off, name="Espace Off", owner_scope=owner_off)
    for u in (um, uv):  # accès d'espace (n'agit que sous ACL appliquée)
        RagSpaceAccess.objects.create(rag_space=rs_a, user=u, space_role="viewer")
    return dict(ws1=ws1, ws2=ws2, ws_off=ws_off, ua=ua, ub=ub, uad=uad, um=um, uv=uv, ux=ux,
                owner_a=owner_a, owner_b=owner_b, m_ad=m_ad, m_m=m_m, m_v=m_v,
                rs_a=rs_a, rs_a2=rs_a2, rs_b=rs_b, rs_w2=rs_w2, rs_off=rs_off)


def cli(u):
    c = APIClient()
    c.force_authenticate(user=u)
    return c


def thread(user, space, title, *messages):
    t = ChatThread.objects.create(user=user, rag_space=space, title=title)
    for role, content, *status in messages:
        ChatMessage.objects.create(thread=t, role=role, content=content,
                                   status=status[0] if status else "")
    return t


def search(user, q, **params):
    r = cli(user).get(SEARCH, {"q": q, **params})
    return r


def ids(r):
    return [x["id"] for x in r.json()["results"]]


def can_read(user, space) -> bool:
    try:
        require_ragspace_access(user=user, rag_space_id=space.id, permission="read")
        return True
    except (PermissionDenied, DjangoPermissionDenied, NotFound):
        return False


# --------------------------------------------------------------------------- #
# A. Équivalence des prédicats (barrière du lot)
# --------------------------------------------------------------------------- #
def test_visible_set_matches_require_ragspace_access(world, mode):
    """Pour chaque (utilisateur, espace) : visible par la recherche ⇒ ouvrable, et
    réciproquement sur les workspaces actifs. Workspace inactif : jamais visible."""
    users = ("ua", "ub", "uad", "um", "uv", "ux")
    spaces = ("rs_a", "rs_a2", "rs_b", "rs_w2", "rs_off")
    for uk in users:
        visible = {str(s) for s in TS.searchable_rag_space_ids(world[uk])}
        for sk in spaces:
            space = world[sk]
            readable = can_read(world[uk], space)
            in_search = str(space.id) in visible
            assert not in_search or readable, f"{mode}: {uk}/{sk} visible mais non ouvrable"
            if space.workspace.is_active:
                assert in_search == readable, f"{mode}: {uk}/{sk} écart {in_search} ≠ {readable}"
            else:
                assert not in_search, f"{mode}: {uk}/{sk} workspace inactif visible"


def test_soft_deleted_and_revoked_states_stay_equivalent(world, mode):
    world["rs_a2"].deleted_at = timezone.now()
    world["rs_a2"].save(update_fields=["deleted_at"])
    RagSpaceAccess.objects.filter(user=world["uv"]).update(revoked_at=timezone.now())
    test_visible_set_matches_require_ragspace_access(world, mode)


# --------------------------------------------------------------------------- #
# B. Propriété et fuites
# --------------------------------------------------------------------------- #
def test_user_b_never_sees_user_a_threads_same_space(world, mode):
    thread(world["ua"], world["rs_a"], "Zorglub alpha", ("user", "zorglub contenu"))
    for other in ("ub", "uad", "um", "uv"):
        r = search(world[other], "zorglub")
        assert r.status_code == 200 and r.json()["results"] == [], f"{mode}: {other} voit le fil de A"


def test_admin_cannot_search_member_threads(world, mode):
    thread(world["um"], world["rs_a"], "Note privée du membre", ("user", "quetzal"))
    assert search(world["uad"], "quetzal").json()["results"] == []
    assert search(world["ua"], "quetzal").json()["results"] == []  # même l'owner du périmètre


def test_cross_tenant_zero_results_identical_to_absent_term(world, mode):
    thread(world["ua"], world["rs_a"], "Planification", ("user", "xylophone"))
    hit = search(world["ux"], "xylophone")
    miss = search(world["ux"], "inexistant")
    assert hit.status_code == miss.status_code == 200
    assert hit.json() == miss.json() == {"results": [], "next": None}


def test_unauthorized_title_snippet_space_name_absent(world, mode):
    thread(world["ua"], world["rs_b"] if mode == "legacy" else world["rs_a"],
           "Titre Confidentiel Kappa", ("user", "kappa extrait confidentiel"))
    raw = search(world["ub"], "kappa").content.decode()
    for leaked in ("Confidentiel", "extrait", "Espace Alpha", "Espace Bravo"):
        assert leaked not in raw


def test_no_count_disclosed(world, mode):
    thread(world["ua"], world["rs_a"], "Lambda", ("user", "lambda"))
    body = search(world["ua"], "lambda").json()
    assert set(body) == {"results", "next"} and body["next"] is None
    assert not any(k in json.dumps(body) for k in ('"count"', '"total"'))


def test_revoked_membership_hides_own_threads(world, mode):
    thread(world["um"], world["rs_a"], "Mon fil", ("user", "omicron"))
    expected = 1
    assert len(ids(search(world["um"], "omicron"))) == expected
    world["m_m"].delete()
    assert search(world["um"], "omicron").json()["results"] == []


def test_revoked_acl_hides_own_threads(world, settings):
    settings.OWNER_SCOPE_ENABLED, settings.OWNER_SCOPE_AUDIT_ONLY = True, False
    settings.RAGSPACE_ACL_ENABLED, settings.RAGSPACE_ACL_AUDIT_ONLY = True, False
    thread(world["um"], world["rs_a"], "Mon fil ACL", ("user", "upsilon"))
    assert len(ids(search(world["um"], "upsilon"))) == 1
    RagSpaceAccess.objects.filter(user=world["um"], rag_space=world["rs_a"]).update(revoked_at=timezone.now())
    assert search(world["um"], "upsilon").json()["results"] == []


def test_owner_scope_mismatch_hides(world, settings):
    settings.OWNER_SCOPE_ENABLED, settings.OWNER_SCOPE_AUDIT_ONLY = True, False
    settings.RAGSPACE_ACL_ENABLED, settings.RAGSPACE_ACL_AUDIT_ONLY = False, True
    thread(world["um"], world["rs_a"], "Mon fil périmètre", ("user", "epsilon"))
    assert len(ids(search(world["um"], "epsilon"))) == 1
    Membership.objects.filter(pk=world["m_m"].pk).update(owner_scope=world["owner_b"])
    assert search(world["um"], "epsilon").json()["results"] == []


def test_soft_deleted_space_hidden(world, mode):
    thread(world["ua"], world["rs_a"], "Archive", ("user", "sigma"))
    world["rs_a"].deleted_at = timezone.now()
    world["rs_a"].save(update_fields=["deleted_at"])
    assert search(world["ua"], "sigma").json()["results"] == []


def test_inactive_workspace_hidden(world, mode):
    thread(world["ua"], world["rs_off"], "Ancien", ("user", "tau"))
    assert search(world["ua"], "tau").json()["results"] == []


def test_foreign_or_unknown_rag_space_filter_returns_empty_200(world, mode):
    thread(world["ua"], world["rs_a"], "Phi", ("user", "phi"))
    for space_id in (world["rs_b"].id, "00000000-0000-0000-0000-000000000000"):
        r = search(world["ub"], "phi", rag_space_id=str(space_id))
        assert r.status_code == 200 and r.json() == {"results": [], "next": None}
    r = search(world["ua"], "phi", workspace_id=999999)
    assert r.status_code == 200 and r.json()["results"] == []


def test_system_pending_failed_messages_and_memory_not_searchable(world, mode):
    t = thread(world["ua"], world["rs_a"], "Fil neutre",
               ("system", "chi-systeme"), ("assistant", "chi-pending", "pending"),
               ("assistant", "chi-failed", "failed"))
    t.memory_summary = "chi-memoire"
    t.save(update_fields=["memory_summary"])
    ChatMessage.objects.filter(thread=t, role="system").update(sources={"x": "chi-sources"}, meta={"y": "chi-meta"})
    for term in ("chi-systeme", "chi-pending", "chi-failed", "chi-memoire", "chi-sources", "chi-meta"):
        assert search(world["ua"], term).json()["results"] == [], term


@pytest.mark.parametrize("q", ["') OR 1=1 --", ".*", "(a+)+$", "[", "\\", "|", "a{2}", "x?y", "+++", "(()"])
def test_pattern_is_parameterized_and_literal(world, q):
    thread(world["ua"], world["rs_a"], "Contenu ordinaire", ("user", "aa xy texte banal"))
    r = search(world["ua"], q)
    assert r.status_code in (200, 400)
    if r.status_code == 200:
        assert r.json()["results"] == [], f"{q!r} interprété comme regex"


def test_literal_special_characters_still_match(world):
    thread(world["ua"], world["rs_a"], "Coûts (HT) [lot 2]", ("user", "tarif 12.5% + taxe"))
    assert len(ids(search(world["ua"], "(HT)"))) == 1
    assert len(ids(search(world["ua"], "12.5%"))) == 1
    assert ids(search(world["ua"], "1205%")) == []  # le point n'est pas un joker


def test_query_length_bounds(world):
    assert search(world["ua"], "a").json() == {"detail": "q_too_short"}
    assert search(world["ua"], "a b").json() == {"detail": "q_too_short"}
    assert search(world["ua"], "x" * 201).json() == {"detail": "q_too_long"}
    assert search(world["ua"], "x" * 200).status_code == 200


# --------------------------------------------------------------------------- #
# C. Fonctionnel
# --------------------------------------------------------------------------- #
def test_title_content_cross_space_cross_workspace(world, mode):
    t1 = thread(world["ua"], world["rs_a"], "Budget dépenses publiques", ("user", "rien"))
    t2 = thread(world["ua"], world["rs_a2"], "Autre", ("assistant", "Les DEPENSES ont baissé", "completed"))
    t3 = thread(world["ua"], world["rs_w2"], "Troisième", ("user", "dépenses W2"))
    r = search(world["ua"], "dépenses").json()["results"]
    by_id = {x["id"]: x for x in r}
    assert set(by_id) == {str(t1.id), str(t2.id), str(t3.id)}
    assert by_id[str(t1.id)]["match"] == "title" and by_id[str(t1.id)]["snippet"] is None
    assert by_id[str(t2.id)]["match"] == "content" and "DEPENSES" in by_id[str(t2.id)]["snippet"]
    assert by_id[str(t3.id)]["workspace_id"] == world["ws2"].id
    assert by_id[str(t3.id)]["rag_space_name"] == "Espace W2"
    assert ids(search(world["ua"], "dépenses", scope="title")) == [str(t1.id)]


def test_accent_and_case_insensitive(world):
    t = thread(world["ua"], world["rs_a"], "Réunion", ("user", "les dépenses d'intérêts"))
    for q in ("DEPENSES", "depenses", "Dépenses", "DÉPENSES", "interets", "réunion", "REUNION"):
        assert ids(search(world["ua"], q)) == [str(t.id)], q


def test_identifier_phrase_and_multi_token_and(world):
    t1 = thread(world["ua"], world["rs_a"], "Norme", ("user", "selon NF-EN-1992 partie 1"))
    t2 = thread(world["ua"], world["rs_a"], "Phrase", ("user", "les dépenses publiques augmentent"))
    t3 = thread(world["ua"], world["rs_a"], "Inversée", ("user", "publiques puis dépenses"))
    assert ids(search(world["ua"], "NF-EN-1992")) == [str(t1.id)]
    assert ids(search(world["ua"], '"dépenses publiques"')) == [str(t2.id)]
    assert set(ids(search(world["ua"], "dépenses publiques"))) == {str(t2.id), str(t3.id)}
    assert ids(search(world["ua"], "dépenses absentissime")) == []


def test_deduplication(world):
    t = thread(world["ua"], world["rs_a"], "Multi", *[("user", f"mot-clé numéro {i}") for i in range(5)])
    assert ids(search(world["ua"], "numéro")) == [str(t.id)]


def test_snippet_comes_only_from_eligible_message(world):
    thread(world["ua"], world["rs_a"], "Fil", ("assistant", "brouillon secret omega", "pending"),
           ("user", "question omega publique"))
    snippet = search(world["ua"], "omega").json()["results"][0]["snippet"]
    assert "publique" in snippet and "secret" not in snippet


def test_pagination_stable_without_duplicates_or_holes(world):
    stamp = timezone.now() - timedelta(days=1)
    created = [thread(world["ua"], world["rs_a"], f"Page {i}", ("user", "pagination")) for i in range(7)]
    ChatThread.objects.filter(id__in=[t.id for t in created]).update(updated_at=stamp)  # ex aequo
    seen, cursor, pages = [], None, 0
    while True:
        params = {"limit": 3, **({"cursor": cursor} if cursor else {})}
        body = search(world["ua"], "pagination", **params).json()
        seen += [x["id"] for x in body["results"]]
        pages += 1
        cursor = body["next"]
        if not cursor:
            break
    assert pages == 3 and len(seen) == len(set(seen)) == 7
    assert seen == sorted(seen, reverse=True)  # updated_at égal → id DESC


def test_new_activity_between_pages_does_not_duplicate(world):
    stamp = timezone.now() - timedelta(days=1)
    created = [thread(world["ua"], world["rs_a"], f"Act {i}", ("user", "activite")) for i in range(4)]
    ChatThread.objects.filter(id__in=[t.id for t in created]).update(updated_at=stamp)
    first = search(world["ua"], "activite", limit=2).json()
    ChatThread.objects.filter(id=first["results"][0]["id"]).update(updated_at=timezone.now())  # remonte
    second = search(world["ua"], "activite", limit=2, cursor=first["next"]).json()
    assert not set(x["id"] for x in first["results"]) & set(x["id"] for x in second["results"])


@pytest.mark.parametrize("params,code", [
    ({"cursor": "pas-un-curseur"}, "invalid_cursor"),
    ({"limit": "0"}, "invalid_parameter"),
    ({"limit": "51"}, "invalid_parameter"),
    ({"limit": "abc"}, "invalid_parameter"),
    ({"scope": "body"}, "invalid_parameter"),
    ({"rag_space_id": "pas-un-uuid"}, "invalid_parameter"),
    ({"workspace_id": "x"}, "invalid_parameter"),
])
def test_invalid_parameters(world, params, code):
    r = search(world["ua"], "valide", **params)
    assert r.status_code == 400 and r.json() == {"detail": code}


def test_anonymous_rejected():
    assert APIClient().get(SEARCH, {"q": "test"}).status_code in (401, 403)


# --------------------------------------------------------------------------- #
# D. Borne de temps, coût, débit
# --------------------------------------------------------------------------- #
def test_statement_timeout_is_local_and_maps_to_503(world, monkeypatch):
    thread(world["ua"], world["rs_a"], "Lent", ("user", "lenteur"))
    monkeypatch.setattr(TS, "STATEMENT_TIMEOUT", "50ms")

    def slow(execute, sql, params, many, context):
        if "chat_thread" in sql and sql.lstrip().upper().startswith("SELECT"):
            context["cursor"].execute("SELECT pg_sleep(0.3)")
        return execute(sql, params, many, context)

    with connection.execute_wrapper(slow):
        r = search(world["ua"], "lenteur")
    assert r.status_code == 503 and r.json() == {"detail": "search_timeout"}
    with connection.cursor() as c:
        c.execute("SHOW statement_timeout")
        assert c.fetchone()[0] != "50ms"  # SET LOCAL : la connexion n'est pas altérée


def test_query_count_bounded(world, django_assert_max_num_queries):
    for i in range(3):
        thread(world["ua"], world["rs_a"], f"Q {i}", ("user", "borne"))
    with django_assert_max_num_queries(12) as small:
        search(world["ua"], "borne")
    for i in range(30):
        thread(world["ua"], world["rs_a2"], f"R {i}", ("user", "borne"))
    with django_assert_max_num_queries(len(small.captured_queries)):
        search(world["ua"], "borne")


def test_search_uses_its_own_throttle_bucket(world, monkeypatch, settings):
    from django.core.cache import cache

    from api.v1.chat.views import ChatThreadSearchView

    assert ChatThreadSearchView.throttle_classes == [ScopedRateThrottle]  # remplace les défauts
    assert ChatThreadSearchView.throttle_scope == "chat_thread_search"
    assert "chat_thread_search" in settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]
    cache.clear()
    monkeypatch.setattr(ScopedRateThrottle, "THROTTLE_RATES", {"chat_thread_search": "2/min"})
    c = cli(world["ua"])
    codes = [c.get(SEARCH, {"q": "throttle"}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    assert c.get("/api/v1/auth/me/").status_code == 200  # seau d'authentification intact
    cache.clear()


# --------------------------------------------------------------------------- #
# E. Ouverture d'un fil (GET /chat/threads/<uuid>/) — séparée de la recherche
# --------------------------------------------------------------------------- #
def test_detail_get_owner_ok_with_context(world, mode):
    t = thread(world["ua"], world["rs_w2"], "Lien direct", ("user", "x"))
    r = cli(world["ua"]).get(f"/api/v1/chat/threads/{t.id}/")
    assert r.status_code == 200
    assert r.json()["rag_space_id"] == str(world["rs_w2"].id) and r.json()["workspace_id"] == world["ws2"].id
    assert set(r.json()) == {"id", "title", "rag_space_id", "workspace_id", "created_at", "updated_at"}


def test_detail_get_other_user_404_without_leak(world, mode):
    t = thread(world["ua"], world["rs_a"], "Titre Interdit", ("user", "x"))
    for other in ("ub", "uad", "um", "ux"):
        r = cli(world[other]).get(f"/api/v1/chat/threads/{t.id}/")
        assert r.status_code == 404 and "Interdit" not in r.content.decode()


def test_detail_get_revoked_or_deleted_denied(world, mode):
    t = thread(world["um"], world["rs_a"], "Fil du membre", ("user", "x"))
    world["m_m"].delete()
    assert cli(world["um"]).get(f"/api/v1/chat/threads/{t.id}/").status_code in (403, 404)
    t2 = thread(world["ua"], world["rs_a2"], "Fil supprimé", ("user", "x"))
    world["rs_a2"].deleted_at = timezone.now()
    world["rs_a2"].save(update_fields=["deleted_at"])
    assert cli(world["ua"]).get(f"/api/v1/chat/threads/{t2.id}/").status_code in (403, 404)


def test_detail_get_owner_scope_mismatch_404(world, settings):
    settings.OWNER_SCOPE_ENABLED, settings.OWNER_SCOPE_AUDIT_ONLY = True, False
    t = thread(world["um"], world["rs_a"], "Périmètre", ("user", "x"))
    Membership.objects.filter(pk=world["m_m"].pk).update(owner_scope=world["owner_b"])
    assert cli(world["um"]).get(f"/api/v1/chat/threads/{t.id}/").status_code == 404


def test_detail_get_invalid_or_unknown_id(world):
    c = cli(world["ua"])
    assert c.get("/api/v1/chat/threads/pas-un-uuid/").status_code == 404
    assert c.get("/api/v1/chat/threads/00000000-0000-0000-0000-000000000000/").status_code == 404


# --------------------------------------------------------------------------- #
# F. Accès progressif au-delà des 100 fils (GET /chat/threads/?before=)
# --------------------------------------------------------------------------- #
def test_threads_beyond_100_reachable_by_before(world):
    base = timezone.now() - timedelta(days=10)
    created = [ChatThread.objects.create(user=world["ua"], rag_space=world["rs_a"], title=f"T{i}")
               for i in range(105)]
    for i, t in enumerate(created):
        ChatThread.objects.filter(id=t.id).update(updated_at=base + timedelta(minutes=i % 50))  # ex aequo
    c = cli(world["ua"])
    first = c.get("/api/v1/chat/threads/", {"rag_space_id": str(world["rs_a"].id)}).json()
    assert len(first) == 100
    second = c.get("/api/v1/chat/threads/", {"rag_space_id": str(world["rs_a"].id),
                                             "before": first[-1]["id"]}).json()
    got = [x["id"] for x in first] + [x["id"] for x in second]
    assert len(second) == 5 and len(got) == len(set(got)) == 105


def test_threads_before_validation(world):
    mine = thread(world["ua"], world["rs_a"], "A", ("user", "x"))
    other = thread(world["ub"], world["rs_b"], "B", ("user", "x"))
    elsewhere = thread(world["ua"], world["rs_a2"], "C", ("user", "x"))
    c = cli(world["ua"])
    url = "/api/v1/chat/threads/"
    space = str(world["rs_a"].id)
    assert c.get(url, {"rag_space_id": space, "before": "x"}).status_code == 400
    assert c.get(url, {"rag_space_id": space, "before": str(other.id)}).status_code == 404
    assert c.get(url, {"rag_space_id": space, "before": str(elsewhere.id)}).status_code == 404
    assert c.get(url, {"rag_space_id": space, "before": str(mine.id)}).json() == []
    assert len(c.get(url, {"rag_space_id": space}).json()) == 1  # sans `before` : inchangé


# --------------------------------------------------------------------------- #
# G. Catalogue multi-workspace : workspaces inactifs exclus
# --------------------------------------------------------------------------- #
def test_ragspace_global_list_excludes_inactive_workspaces(world, mode):
    r = cli(world["ua"]).get("/api/v1/rag-spaces/")
    listed = {x["id"] for x in r.json()}
    assert str(world["rs_off"].id) not in listed
    assert {str(world["rs_a"].id), str(world["rs_w2"].id)} <= listed
    # Liste ciblée inchangée, et contenu toujours lisible : aucune règle d'accès modifiée.
    assert cli(world["ua"]).get(f"/api/v1/rag-spaces/?workspace_id={world['ws_off'].id}").status_code == 200
    assert can_read(world["ua"], world["rs_off"])


@pytest.mark.parametrize("q,scope", [("cible", "all"), ("cible bonus", "all"), ("Titre", "title")])
def test_fast_path_and_full_pass_return_identical_pages(world, monkeypatch, q, scope):
    """Le chemin rapide par lots et le passage complet appliquent le même prédicat :
    pages identiques quel que soit le découpage (lots de 2, bascule, complet seul)."""
    stamp = timezone.now() - timedelta(days=2)
    created = []
    for i in range(23):
        msgs = [("user", "cible" + (" bonus" if i % 3 == 0 else ""))] if i % 2 == 0 else [("user", "autre")]
        created.append(thread(world["ua"], world["rs_a"] if i % 2 else world["rs_a2"], f"Titre {i}", *msgs))
    for i, t in enumerate(created):
        ChatThread.objects.filter(id=t.id).update(updated_at=stamp + timedelta(minutes=i // 4))  # ex aequo

    def all_pages(batch, batches):
        monkeypatch.setattr(TS, "FAST_BATCH", batch)
        monkeypatch.setattr(TS, "FAST_MAX_BATCHES", batches)
        out, cursor = [], None
        while True:
            page = TS.search_threads(world["ua"], q, scope=scope, limit=3, cursor=cursor)
            out.append([r["id"] for r in page.results])
            cursor = page.next
            if not cursor:
                return out

    reference = all_pages(500, 0)  # passage complet seul
    assert sum(len(p) for p in reference) > 3
    for batch, batches in ((2, 1), (2, 3), (3, 2), (500, 2)):
        assert all_pages(batch, batches) == reference, (batch, batches)
