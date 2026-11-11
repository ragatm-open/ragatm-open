"""Concurrence et idempotence des invitations (INV-A2).

Tests réels multi-threads (Postgres + select_for_update) : au plus un effet.
"""
import threading

import pytest
from django.db import connections

from apps.workspaces.models import Membership, Workspace, WorkspaceInvitation
from apps.workspaces.services import invitations as inv

STRONG_PW = "Corr3ct-Horse-Staple-88"


def _run_parallel(targets):
    """Lance N callables en parallèle après une barrière ; renvoie la liste des résultats."""
    n = len(targets)
    barrier = threading.Barrier(n)
    results: list = [None] * n

    def wrap(idx, fn):
        def inner():
            barrier.wait()
            try:
                results[idx] = ("ok", fn())
            except Exception as e:  # noqa: BLE001
                results[idx] = ("err", type(e).__name__)
            finally:
                connections.close_all()

        return inner

    threads = [threading.Thread(target=wrap(i, fn)) for i, fn in enumerate(targets)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    return results


@pytest.mark.django_db(transaction=True)
def test_two_simultaneous_acceptances_one_success():
    ws = Workspace.objects.create(name="WC1")
    invitation, raw = inv.create_invitation(workspace_id=ws.id, email="race@ex.com", role="member")

    def accept():
        return inv.accept_invitation(
            raw_token=raw, password=STRONG_PW, password_confirmation=STRONG_PW
        )["membership_created"]

    results = _run_parallel([accept, accept])
    oks = [r for r in results if r and r[0] == "ok"]
    # Exactement une acceptation réussie, exactement un membership.
    assert len(oks) == 1, results
    assert Membership.objects.filter(workspace=ws, user__email__iexact="race@ex.com").count() == 1
    invitation.refresh_from_db()
    assert invitation.accepted_at is not None

    Membership.objects.filter(workspace=ws).delete()
    WorkspaceInvitation.objects.filter(workspace=ws).delete()
    ws.delete()


@pytest.mark.django_db(transaction=True)
def test_two_simultaneous_creations_one_pending_invitation():
    ws = Workspace.objects.create(name="WC2")

    def create():
        return inv.create_invitation(workspace_id=ws.id, email="dupe@ex.com", role="viewer")[0].id

    results = _run_parallel([create, create])
    oks = [r for r in results if r and r[0] == "ok"]
    assert len(oks) == 1, results
    pending = WorkspaceInvitation.objects.filter(
        workspace=ws, email_normalized="dupe@ex.com", accepted_at__isnull=True, revoked_at__isnull=True
    )
    assert pending.count() == 1

    WorkspaceInvitation.objects.filter(workspace=ws).delete()
    ws.delete()


@pytest.mark.django_db(transaction=True)
def test_accept_vs_revoke_is_atomic():
    ws = Workspace.objects.create(name="WC3")
    invitation, raw = inv.create_invitation(workspace_id=ws.id, email="ar@ex.com", role="member")

    def accept():
        return inv.accept_invitation(
            raw_token=raw, password=STRONG_PW, password_confirmation=STRONG_PW
        )["membership_created"]

    def revoke():
        return inv.revoke_invitation(workspace_id=ws.id, invitation_id=invitation.id)[1]

    _run_parallel([accept, revoke])
    invitation.refresh_from_db()
    membership_exists = Membership.objects.filter(
        workspace=ws, user__email__iexact="ar@ex.com"
    ).exists()
    # État cohérent (contrainte xor) : jamais accepté ET révoqué simultanément.
    assert not (invitation.accepted_at is not None and invitation.revoked_at is not None)
    if invitation.accepted_at is not None:
        assert membership_exists
    else:
        # révoquée (ou course perdue) → aucun membership
        assert not membership_exists

    Membership.objects.filter(workspace=ws).delete()
    WorkspaceInvitation.objects.filter(workspace=ws).delete()
    ws.delete()
