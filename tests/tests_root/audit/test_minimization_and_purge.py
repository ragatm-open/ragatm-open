"""Minimisation RagQueryLog (§15) + commande de purge de rétention (§14)."""
from __future__ import annotations

from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.audit.models import ActionAuditEvent
from apps.rag_core.services.query.orchestrator import _minimize_log_text


# ---- minimisation ---------------------------------------------------------
def test_store_on_keeps_text(settings):
    settings.RAG_QUERY_LOG_STORE_QUESTION = True
    assert _minimize_log_text("Quelle durée ?", "RAG_QUERY_LOG_STORE_QUESTION") == "Quelle durée ?"


def test_store_off_redacts_but_keeps_hash_and_len(settings):
    settings.RAG_QUERY_LOG_STORE_ANSWER = False
    out = _minimize_log_text("secret answer", "RAG_QUERY_LOG_STORE_ANSWER")
    assert "secret answer" not in out
    assert "sha256:" in out and "len:13" in out


def test_empty_text_coherent(settings):
    settings.RAG_QUERY_LOG_STORE_QUESTION = False
    out = _minimize_log_text("", "RAG_QUERY_LOG_STORE_QUESTION")
    assert "len:0" in out


# ---- purge ----------------------------------------------------------------
pytestmark_db = pytest.mark.django_db


def _mk(created_days_ago: int):
    e = ActionAuditEvent.objects.create(event_name="action.completed", action_name="document.delete", status="completed")
    old = timezone.now() - timedelta(days=created_days_ago)
    ActionAuditEvent.objects.filter(id=e.id).update(created_at=old)  # queryset.update ok (append-only unitaire seulement)
    return e


@pytest.mark.django_db
def test_purge_dry_run_deletes_nothing(settings):
    settings.ACTION_AUDIT_RETENTION_DAYS = 365
    _mk(400)  # expiré
    _mk(10)   # récent
    out = StringIO()
    call_command("purge_expired_audit_logs", stdout=out)
    assert "DRY-RUN" in out.getvalue()
    assert ActionAuditEvent.objects.count() == 2  # rien supprimé


@pytest.mark.django_db
def test_purge_apply_respects_bounds(settings):
    settings.ACTION_AUDIT_RETENTION_DAYS = 365
    old = _mk(400)
    recent = _mk(10)
    call_command("purge_expired_audit_logs", "--apply", stdout=StringIO())
    ids = set(ActionAuditEvent.objects.values_list("id", flat=True))
    assert recent.id in ids and old.id not in ids  # récent conservé, expiré supprimé


@pytest.mark.django_db
def test_purge_refuses_zero_retention(settings):
    """Lot 4-D : une rétention de 0 jour purgerait TOUT immédiatement.

    L'ancien test forçait précisément ce comportement via ``--action-days 0`` (un
    escape hatch pour tout supprimer). Ce raccourci est désormais interdit : une
    rétention nulle ou négative est refusée par ``retention.validate_settings`` et
    la commande sort en code 2 sans rien détruire. C'est un durcissement assumé,
    pas une régression — le flag de contournement était un pistolet à foot.
    """
    _mk(30)
    settings.ACTION_AUDIT_RETENTION_DAYS = 0
    with pytest.raises(SystemExit) as exc:
        call_command("purge_expired_audit_logs", "--apply", stdout=StringIO(), stderr=StringIO())
    assert exc.value.code == 2
    assert ActionAuditEvent.objects.count() == 1  # rien supprimé


@pytest.mark.django_db
def test_purge_reports_legal_hold_absence():
    out = StringIO()
    call_command("purge_expired_audit_logs", stdout=out)
    assert "legal_hold: NON IMPLÉMENTÉ" in out.getvalue()
