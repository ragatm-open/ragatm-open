"""Tests unitaires du service central des quotas documentaires par workspace.

Couvre : politique d'usage (queryset unique, FAILED compté), calcul get_usage
(bornes, pourcentages, division par zéro), validation des limites, résolution des
défauts de plateforme (override CoreConfig > settings > code, sans effet
rétroactif), réservation verrouillée et concurrence multi-thread.
"""
import threading

import pytest

from apps.rag_core.models import Document
from apps.workspaces.models import Workspace, WorkspaceQuota
from apps.workspaces.services import quota as quota_service

pytestmark = pytest.mark.django_db


def _doc(rag_space, *, size_bytes=1, status="READY", sha=""):
    return Document.objects.create(
        rag_space=rag_space, filename="f", size_bytes=size_bytes,
        status=status, sha256=sha,
    )


# --- Politique d'usage ------------------------------------------------------

def test_usage_counts_all_rows_including_failed(workspace, rag_space):
    quota_service.ensure_quota(workspace)
    _doc(rag_space, size_bytes=100, status="READY")
    _doc(rag_space, size_bytes=50, status="FAILED")   # compte (octets sur disque)
    _doc(rag_space, size_bytes=10, status="PENDING")
    u = quota_service.get_usage(workspace)
    assert u["used_document_count"] == 3
    assert u["used_document_bytes"] == 160


def test_quota_consuming_documents_is_the_single_policy_point(workspace, rag_space):
    _doc(rag_space, status="FAILED")
    assert quota_service.quota_consuming_documents(workspace).count() == 1


# --- get_usage : maths & bornes --------------------------------------------

def test_usage_remaining_never_negative_but_exceeded_flagged(workspace, rag_space):
    quota_service.ensure_quota(
        workspace, max_document_count=1, max_document_bytes=100
    )
    # 2 documents > max_count=1 ; 500 octets > max_bytes=100 => les deux dépassés.
    _doc(rag_space, size_bytes=250)
    _doc(rag_space, size_bytes=250)
    u = quota_service.get_usage(workspace)
    assert u["remaining_document_count"] == 0
    assert u["remaining_document_bytes"] == 0
    assert u["is_count_exceeded"] and u["is_size_exceeded"] and u["is_exceeded"]
    assert u["can_upload"] is False


def test_usage_percent_no_division_by_zero(workspace, rag_space):
    # Contrainte SQL interdit 0, mais le calcul de % doit rester défensif.
    q = quota_service.ensure_quota(workspace, max_document_count=1000, max_document_bytes=1000)
    q.max_document_count = 0  # instance en mémoire uniquement
    u = quota_service.get_usage(workspace, quota=q)
    assert u["count_usage_percent"] == 0.0


def test_usage_percent_rounded(workspace, rag_space):
    quota_service.ensure_quota(workspace, max_document_count=1000, max_document_bytes=10_000)
    for _ in range(742):
        _doc(rag_space, size_bytes=0)
    u = quota_service.get_usage(workspace)
    assert u["count_usage_percent"] == 74.2


# --- Validation -------------------------------------------------------------

@pytest.mark.parametrize("bad", [0, -1, -1000])
def test_validate_count_rejects_zero_and_negative(bad):
    with pytest.raises(quota_service.QuotaValidationError) as ei:
        quota_service.validate_count(bad)
    assert ei.value.code in ("must_be_positive",)


def test_validate_rejects_non_integer():
    for bad in ("abc", 1.5, None, True):
        with pytest.raises(quota_service.QuotaValidationError):
            quota_service.validate_count(bad)


def test_validate_rejects_above_hard_cap(settings):
    settings.WORKSPACE_HARD_MAX_DOCUMENT_COUNT = 100
    with pytest.raises(quota_service.QuotaValidationError) as ei:
        quota_service.validate_count(101)
    assert ei.value.code == "exceeds_hard_cap"


def test_validate_integer_float_accepted():
    assert quota_service.validate_bytes(10.0) == 10


# --- Défauts de plateforme --------------------------------------------------

def test_defaults_from_settings(settings):
    settings.WORKSPACE_DEFAULT_MAX_DOCUMENT_COUNT = 1234
    settings.WORKSPACE_DEFAULT_MAX_DOCUMENT_BYTES = 5678
    assert quota_service.default_max_document_count() == 1234
    assert quota_service.default_max_document_bytes() == 5678


def test_defaults_override_beats_settings(settings, db):
    from apps.rag_core.core.config_write import set_override

    settings.WORKSPACE_DEFAULT_MAX_DOCUMENT_COUNT = 1000
    set_override(
        actor=None, scope_type="global", scope_ref="",
        key="WORKSPACE_DEFAULT_MAX_DOCUMENT_COUNT", value=2000,
    )
    # L'override persisté prime, indépendamment de CORE_CONFIG_RUNTIME_ENABLED.
    assert quota_service.default_max_document_count() == 2000


def test_default_change_not_retroactive(settings, db):
    from apps.rag_core.core.config_write import set_override

    ws_old = Workspace.objects.create(name="old")
    quota_service.ensure_quota(ws_old)  # capture le défaut courant (1000 par défaut)
    old_max = WorkspaceQuota.objects.get(workspace=ws_old).max_document_count

    set_override(
        actor=None, scope_type="global", scope_ref="",
        key="WORKSPACE_DEFAULT_MAX_DOCUMENT_COUNT", value=old_max + 5000,
    )
    ws_new = Workspace.objects.create(name="new")
    quota_service.ensure_quota(ws_new)

    assert WorkspaceQuota.objects.get(workspace=ws_old).max_document_count == old_max
    assert WorkspaceQuota.objects.get(workspace=ws_new).max_document_count == old_max + 5000


# --- Réservation & enforcement ---------------------------------------------

def test_reserve_blocks_when_over_count(workspace, rag_space):
    quota_service.ensure_quota(workspace, max_document_count=1, max_document_bytes=10 ** 9)
    _doc(rag_space)  # used=1 == max
    with pytest.raises(quota_service.QuotaExceeded) as ei:
        quota_service.check_and_reserve(
            workspace=workspace, added_count=1, added_bytes=1,
            create_fn=lambda: _doc(rag_space, sha="x"),
        )
    codes = {e["code"] for e in ei.value.errors}
    assert "workspace_file_count_quota_exceeded" in codes
    # Aucune ligne créée par le create_fn rejeté.
    assert quota_service.quota_consuming_documents(workspace).count() == 1


def test_reserve_allows_exact_fit(workspace, rag_space):
    quota_service.ensure_quota(workspace, max_document_count=2, max_document_bytes=10 ** 9)
    _doc(rag_space)
    quota_service.check_and_reserve(
        workspace=workspace, added_count=1, added_bytes=1,
        create_fn=lambda: _doc(rag_space, sha="y"),
    )
    assert quota_service.quota_consuming_documents(workspace).count() == 2


def test_reserve_noop_when_added_zero_even_if_full(workspace, rag_space):
    # Dédup : ajout nul => jamais bloqué même workspace plein.
    quota_service.ensure_quota(workspace, max_document_count=1, max_document_bytes=1)
    _doc(rag_space, size_bytes=999)
    sentinel = quota_service.check_and_reserve(
        workspace=workspace, added_count=0, added_bytes=0, create_fn=lambda: "ran",
    )
    assert sentinel == "ran"


def test_enforcement_disabled_bypasses_check(settings, workspace, rag_space):
    settings.WORKSPACE_QUOTAS_ENFORCED = False
    quota_service.ensure_quota(workspace, max_document_count=1, max_document_bytes=1)
    _doc(rag_space, size_bytes=999)
    # Aucun blocage : create_fn s'exécute.
    quota_service.check_and_reserve(
        workspace=workspace, added_count=1, added_bytes=10 ** 9,
        create_fn=lambda: _doc(rag_space, sha="z"),
    )
    assert quota_service.quota_consuming_documents(workspace).count() == 2


def test_both_dimensions_exceeded_returns_two_errors(workspace, rag_space):
    quota_service.ensure_quota(workspace, max_document_count=1, max_document_bytes=1)
    _doc(rag_space, size_bytes=10)
    with pytest.raises(quota_service.QuotaExceeded) as ei:
        quota_service.check_upload_allowed(
            workspace=workspace, added_count=1, added_bytes=10,
        )
    assert len(ei.value.errors) == 2


# --- Concurrence (verrou de ligne, transactions réelles) --------------------

@pytest.mark.django_db(transaction=True)
def test_concurrent_reservations_do_not_exceed_quota(django_db_setup):
    ws = Workspace.objects.create(name="conc")
    from apps.rag_core.models import RagSpace
    rs = RagSpace.objects.create(name="rs", workspace=ws)
    quota_service.ensure_quota(ws, max_document_count=1, max_document_bytes=10 ** 12)

    results: dict[int, str] = {}
    barrier = threading.Barrier(2)

    def worker(i: int):
        from django.db import connection
        barrier.wait()
        try:
            quota_service.check_and_reserve(
                workspace=ws, added_count=1, added_bytes=1,
                create_fn=lambda: _doc(rs, sha=f"c{i}"),
            )
            results[i] = "ok"
        except quota_service.QuotaExceeded:
            results[i] = "rejected"
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results.values()) == ["ok", "rejected"]
    assert Document.objects.filter(rag_space=rs).count() == 1
