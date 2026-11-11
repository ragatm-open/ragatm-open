"""Modèle ActionAuditEvent : append-only, actor SET_NULL, admin read-only, emit."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from apps.audit.action_audit import emit_action_event
from apps.audit.models import ActionAuditEvent

pytestmark = pytest.mark.django_db


def _event(**kw):
    base = dict(event_name="action.completed", action_name="document.delete", status="completed")
    base.update(kw)
    return ActionAuditEvent.objects.create(**base)


def test_create_event():
    e = _event()
    assert e.pk and e.created_at is not None


def test_append_only_update_blocked():
    e = _event()
    e.status = "failed"
    with pytest.raises(ValueError):
        e.save()


def test_append_only_unit_delete_blocked():
    e = _event()
    with pytest.raises(ValueError):
        e.delete()


def test_queryset_delete_allowed_for_retention():
    _event()
    # QuerySet.delete court-circuite le garde unitaire (utilisé par la purge).
    n, _ = ActionAuditEvent.objects.all().delete()
    assert n >= 1


def test_actor_set_null_keeps_event():
    user = get_user_model().objects.create_user(username="a", password="pw")
    e = _event(actor=user)
    user.delete()
    e.refresh_from_db()
    assert e.actor_id is None  # event conservé, acteur détaché


def test_admin_is_read_only():
    from django.contrib.admin.sites import AdminSite

    from apps.audit.admin import ActionAuditEventAdmin

    adm = ActionAuditEventAdmin(ActionAuditEvent, AdminSite())
    assert adm.has_add_permission(None) is False
    assert adm.has_change_permission(None) is False
    assert adm.has_delete_permission(None) is False


# ---- emit helper : flag + fail-open --------------------------------------
def test_emit_disabled_by_flag(settings):
    settings.ACTION_AUDIT_ENABLED = False
    assert emit_action_event(event_name="action.started", action_name="document.upload", status="started") is None
    assert ActionAuditEvent.objects.count() == 0


def test_emit_fail_open(monkeypatch, settings):
    settings.ACTION_AUDIT_ENABLED = True

    def boom(**k):
        raise RuntimeError("db down")

    monkeypatch.setattr(ActionAuditEvent.objects, "create", boom)
    # ne lève pas -> l'action métier n'est jamais cassée par l'audit
    assert emit_action_event(event_name="action.completed", action_name="document.delete", status="completed") is None


def test_emit_status_normalized(settings):
    settings.ACTION_AUDIT_ENABLED = True
    e = emit_action_event(event_name="x", action_name="document.delete", status="not_a_real_status")
    assert e.status == "started"  # valeur inconnue -> repli sûr
