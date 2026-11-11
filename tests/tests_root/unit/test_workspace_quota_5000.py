"""Quota documentaire par défaut porté à 5 000.

Couvre : le défaut effectif (registre ET settings, qui avaient chacun leur valeur),
la création sans/avec quota explicite, la capacité réelle d'un import de 1 000
fichiers selon l'usage existant, le refus partiel quand la capacité manque, la
limite en octets inchangée, et la commande opérateur qui ne relève que les quotas
dont l'origine « défaut standard » est prouvée.
"""
from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command

from apps.audit.models import ActionAuditEvent
from apps.rag_core.models import Document
from apps.workspaces.models import Workspace, WorkspaceQuota
from apps.workspaces.services import quota as quota_service
from config import registry

pytestmark = pytest.mark.django_db

GIB = 1024 ** 3


def _fill(rag_space, n):
    Document.objects.bulk_create(
        [Document(rag_space=rag_space, filename=f"f{i}", size_bytes=1, status="READY", sha256="")
         for i in range(n)]
    )


# --- Défaut effectif ----------------------------------------------------------

def test_registry_and_settings_defaults_are_both_5000(settings):
    # Les deux sources avaient chacune leur 1000 : changer l'une seule laissait
    # l'autre imposer l'ancien défaut.
    assert registry.get("WORKSPACE_DEFAULT_MAX_DOCUMENT_COUNT").default == 5000
    assert settings.WORKSPACE_DEFAULT_MAX_DOCUMENT_COUNT == 5000
    assert quota_service.default_max_document_count() == 5000


def test_other_limits_unchanged(settings):
    assert quota_service.default_max_document_bytes() == 10 * GIB
    assert quota_service.hard_max_document_count() == 100000
    assert quota_service.enforcement_enabled() is True


def test_new_workspace_without_explicit_quota_gets_5000():
    q = quota_service.ensure_quota(Workspace.objects.create(name="neuf"))
    assert (q.max_document_count, q.max_document_bytes) == (5000, 10 * GIB)


def test_explicit_quota_is_kept():
    q = quota_service.ensure_quota(Workspace.objects.create(name="x"), max_document_count=1234)
    assert q.max_document_count == 1234


def test_existing_quota_not_retroactive():
    ws = Workspace.objects.create(name="ancien")
    WorkspaceQuota.objects.create(workspace=ws, max_document_count=1000, max_document_bytes=10 * GIB)
    assert quota_service.ensure_quota(ws).max_document_count == 1000


# --- Capacité réelle pour un import de 1 000 fichiers ----------------------------

@pytest.mark.parametrize("existing", [0, 500, 4000])
def test_import_of_1000_fits_with_enough_capacity(workspace, rag_space, existing):
    quota_service.ensure_quota(workspace)
    _fill(rag_space, existing)
    quota_service.check_upload_allowed(workspace, added_count=1000, added_bytes=1000)


def test_import_of_1000_refused_when_capacity_short(workspace, rag_space):
    quota_service.ensure_quota(workspace)
    _fill(rag_space, 4001)
    with pytest.raises(quota_service.QuotaExceeded) as ei:
        quota_service.check_upload_allowed(workspace, added_count=1000, added_bytes=1000)
    assert {e["code"] for e in ei.value.errors} == {"workspace_file_count_quota_exceeded"}


def test_per_file_reservations_stop_exactly_at_5000(workspace, rag_space):
    # Un import est une suite d'envois unitaires : les premiers passent, le reste est
    # refusé fichier par fichier, et les acceptés restent acquis.
    quota_service.ensure_quota(workspace)
    _fill(rag_space, 4990)
    accepted = refused = 0
    for i in range(20):
        try:
            quota_service.check_and_reserve(
                workspace=workspace, added_count=1, added_bytes=1,
                create_fn=lambda i=i: Document.objects.create(
                    rag_space=rag_space, filename=f"n{i}", size_bytes=1, status="PENDING", sha256=f"s{i}"),
            )
            accepted += 1
        except quota_service.QuotaExceeded:
            refused += 1
    assert (accepted, refused) == (10, 10)
    assert quota_service.quota_consuming_documents(workspace).count() == 5000


def test_byte_limit_still_enforced_under_count(workspace, rag_space):
    quota_service.ensure_quota(workspace)
    with pytest.raises(quota_service.QuotaExceeded) as ei:
        quota_service.check_upload_allowed(workspace, added_count=1, added_bytes=10 * GIB + 1)
    assert {e["code"] for e in ei.value.errors} == {"workspace_storage_quota_exceeded"}


# --- Commande de relèvement -----------------------------------------------------

def _quota(name, count, *, modified=False, audited=None):
    ws = Workspace.objects.create(name=name)
    q = WorkspaceQuota.objects.create(workspace=ws, max_document_count=count, max_document_bytes=10 * GIB)
    if modified:
        WorkspaceQuota.objects.filter(pk=q.pk).update(updated_at=q.created_at + timedelta(minutes=5))
    if audited:
        ActionAuditEvent.objects.create(
            event_name="action.completed", action_name=audited, status="completed",
            workspace_id=str(ws.id), resource_type="workspace",
        )
    return ws


def _run(*args):
    out = StringIO()
    call_command("upgrade_default_workspace_quota", "--from-count", "1000", "--reason", "test", *args, stdout=out)
    return out.getvalue()


@pytest.fixture
def mixed():
    return {
        "lazy": _quota("lazy", 1000),
        "custom_value": _quota("custom_value", 1234),
        "modified": _quota("modified", 1000, modified=True),
        "console_created": _quota("console_created", 1000, audited="workspace.quota.created"),
        "updated": _quota("updated", 1000, audited="workspace.quota.updated"),
    }


def _count(ws):
    return WorkspaceQuota.objects.get(workspace=ws).max_document_count


def test_dry_run_classifies_and_changes_nothing(mixed):
    out = _run()
    expected = {
        "lazy": "candidat",
        "custom_value": "préservé:valeur_différente",
        "modified": "préservé:ligne_modifiée",
        "console_created": "préservé:origine_auditée",
        "updated": "préservé:origine_auditée",
    }
    for key, verdict in expected.items():
        assert f"workspace={mixed[key].id} " in out
        line = next(l for l in out.splitlines() if f"workspace={mixed[key].id} " in l)
        assert line.endswith(f"-> {verdict}"), (key, line)
    assert "candidats=1 " in out
    assert all(_count(ws) in (1000, 1234) for ws in mixed.values())
    assert not ActionAuditEvent.objects.filter(action_name="workspace.quota.updated",
                                               metadata__reason__startswith="upgrade_default").exists()


def test_apply_upgrades_only_proven_defaults_and_audits(mixed):
    _run("--apply")
    assert _count(mixed["lazy"]) == 5000
    assert _count(mixed["custom_value"]) == 1234
    for k in ("modified", "console_created", "updated"):
        assert _count(mixed[k]) == 1000, k
    q = WorkspaceQuota.objects.get(workspace=mixed["lazy"])
    assert q.max_document_bytes == 10 * GIB
    ev = ActionAuditEvent.objects.get(action_name="workspace.quota.updated", workspace_id=str(mixed["lazy"].id))
    assert ev.metadata["old_max_document_count"] == 1000
    assert ev.metadata["new_max_document_count"] == 5000
    assert ev.metadata["reason"] == "upgrade_default_workspace_quota: test"


def test_apply_is_idempotent(mixed):
    _run("--apply")
    assert "candidats=0" in _run("--apply")
    assert ActionAuditEvent.objects.filter(action_name="workspace.quota.updated",
                                           workspace_id=str(mixed["lazy"].id)).count() == 1


def test_workspace_filter_limits_scope(mixed):
    other = _quota("other_lazy", 1000)
    _run("--apply", "--workspace", str(other.id))
    assert _count(other) == 5000
    assert _count(mixed["lazy"]) == 1000


def test_refuses_when_current_default_not_higher(settings, mixed):
    settings.WORKSPACE_DEFAULT_MAX_DOCUMENT_COUNT = 1000
    from django.core.management.base import CommandError
    with pytest.raises(CommandError):
        _run("--apply")
    assert _count(mixed["lazy"]) == 1000
