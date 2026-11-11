"""Rétention des journaux d'audit — bornes, catégories, purge (Lot 4-D).

La purge est le seul mécanisme **destructif** ajouté par ce lot. Les tests
portent donc autant sur ce qu'elle supprime que sur ce qu'elle **ne doit pas**
supprimer : un audit de sécurité effacé trop tôt est une preuve perdue.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.audit import retention
from apps.audit.models import (
    ActionAuditEvent,
    EgressPolicyChangeLog,
    LlmEgressAuditEvent,
    RagQueryLog,
)
from apps.rag_core.checks_operations import check_audit_retention

pytestmark = pytest.mark.django_db


def _age(obj, days: int):
    """Vieillit un objet en contournant `auto_now_add`."""
    type(obj).objects.filter(pk=obj.pk).update(
        created_at=timezone.now() - timedelta(days=days)
    )


@pytest.fixture
def space(workspace):
    from apps.rag_core.models import RagSpace

    return RagSpace.objects.create(name="retention-espace", workspace=workspace)


def _query_log(space, user, question="q"):
    """`rag_space`, `workspace` et `user` sont NOT NULL : un journal de requête est
    toujours rattaché à un demandeur et à un périmètre."""
    return RagQueryLog.objects.create(
        rag_space=space, workspace=space.workspace, user=user,
        question=question, status="ok",
    )


# --------------------------------------------------------------------------- #
# Matrice de la spécification
# --------------------------------------------------------------------------- #

def test_recent_conversational_data_is_kept(space, user):
    log = _query_log(space, user)
    _age(log, 1)
    retention.purge_all(apply=True)
    assert RagQueryLog.objects.filter(pk=log.pk).exists()


def test_expired_conversational_data_is_purged(settings, space, user):
    settings.RAG_QUERY_LOG_RETENTION_DAYS = 90
    log = _query_log(space, user)
    _age(log, 100)
    retention.purge_all(apply=True)
    assert not RagQueryLog.objects.filter(pk=log.pk).exists()


def test_security_audit_is_kept_below_its_horizon(settings):
    """100 jours < 365 : un audit de sécurité récent NE DOIT PAS partir."""
    settings.LLM_EGRESS_AUDIT_RETENTION_DAYS = 365
    ev = LlmEgressAuditEvent.objects.create(operation="chat.answer")
    _age(ev, 100)
    retention.purge_all(apply=True)
    assert LlmEgressAuditEvent.objects.filter(pk=ev.pk).exists()


def test_security_audit_is_purged_beyond_its_horizon(settings):
    settings.LLM_EGRESS_AUDIT_RETENTION_DAYS = 365
    ev = LlmEgressAuditEvent.objects.create(operation="chat.answer")
    _age(ev, 400)
    retention.purge_all(apply=True)
    assert not LlmEgressAuditEvent.objects.filter(pk=ev.pk).exists()


def test_policy_changes_have_the_longest_horizon(settings):
    """« Qui a autorisé la sortie des données, et quand » se conserve 3 ans."""
    settings.EGRESS_POLICY_CHANGE_RETENTION_DAYS = 1095
    ev = EgressPolicyChangeLog.objects.create(field_name="allow_external_fallback")
    _age(ev, 800)
    retention.purge_all(apply=True)
    assert EgressPolicyChangeLog.objects.filter(pk=ev.pk).exists()


def test_future_dated_rows_are_reported_never_deleted(space, user):
    """Une ligne datée dans le futur signale une horloge décalée : ne pas la détruire."""
    log = _query_log(space, user)
    RagQueryLog.objects.filter(pk=log.pk).update(
        created_at=timezone.now() + timedelta(days=10)
    )
    report = retention.purge_all(apply=True)
    row = next(r for r in report["results"] if r["key"] == "rag_query_log")
    assert row["future_dated"] == 1
    assert RagQueryLog.objects.filter(pk=log.pk).exists()


# --------------------------------------------------------------------------- #
# Dry-run, bornes, idempotence
# --------------------------------------------------------------------------- #

def test_dry_run_deletes_nothing(settings, space, user):
    settings.RAG_QUERY_LOG_RETENTION_DAYS = 90
    log = _query_log(space, user)
    _age(log, 100)
    report = retention.purge_all(apply=False)
    assert report["eligible_total"] == 1
    assert report["deleted_total"] == 0
    assert RagQueryLog.objects.filter(pk=log.pk).exists()


def test_purge_is_bounded_and_resumes(settings, space, user):
    """Le plafond protège la base ; le reliquat part au passage suivant."""
    settings.RAG_QUERY_LOG_RETENTION_DAYS = 90
    for _ in range(7):
        log = _query_log(space, user)
        _age(log, 100)

    first = retention.purge_all(apply=True, batch_size=2, max_rows=4)
    row = next(r for r in first["results"] if r["key"] == "rag_query_log")
    assert row["deleted"] == 4
    assert row["truncated"] is True
    assert RagQueryLog.objects.count() == 3

    second = retention.purge_all(apply=True, batch_size=2, max_rows=4)
    assert RagQueryLog.objects.count() == 0
    assert second["deleted_total"] == 3


def test_purge_is_idempotent(settings, space, user):
    settings.RAG_QUERY_LOG_RETENTION_DAYS = 90
    log = _query_log(space, user)
    _age(log, 100)
    retention.purge_all(apply=True)
    again = retention.purge_all(apply=True)
    assert again["deleted_total"] == 0


def test_only_restricts_to_a_single_rule(settings, space, user):
    settings.RAG_QUERY_LOG_RETENTION_DAYS = 90
    settings.ACTION_AUDIT_RETENTION_DAYS = 365
    log = _query_log(space, user)
    _age(log, 100)
    ev = ActionAuditEvent.objects.create(event_name="e", action_name="a")
    _age(ev, 400)

    retention.purge_all(apply=True, only="rag_query_log")
    assert not RagQueryLog.objects.filter(pk=log.pk).exists()
    assert ActionAuditEvent.objects.filter(pk=ev.pk).exists()


# --------------------------------------------------------------------------- #
# Validation de configuration (system check ragatm.E010)
# --------------------------------------------------------------------------- #

def test_valid_configuration_passes():
    assert retention.validate_settings() == []
    assert [e for e in check_audit_retention(None) if e.id == "ragatm.E010"] == []


@pytest.mark.parametrize("value", [0, -1, -365])
def test_non_positive_retention_is_rejected(settings, value):
    settings.RAG_QUERY_LOG_RETENTION_DAYS = value
    assert retention.validate_settings()
    assert any(e.id == "ragatm.E010" for e in check_audit_retention(None))


def test_security_audit_floor_is_enforced(settings):
    """LE garde-fou du lot : personne ne peut ramener un audit de sécurité à 7 jours."""
    settings.LLM_EGRESS_AUDIT_RETENTION_DAYS = 7
    errors = retention.validate_settings()
    assert any("plancher" in e for e in errors)
    assert any(e.id == "ragatm.E010" for e in check_audit_retention(None))


def test_security_floor_does_not_apply_to_conversational(settings):
    """30 jours de contenu conversationnel est légitime — ne pas sur-contraindre."""
    settings.RAG_QUERY_LOG_RETENTION_DAYS = 30
    assert retention.validate_settings() == []


def test_incoherent_bounds_are_rejected(settings):
    settings.AUDIT_PURGE_BATCH_SIZE = 5000
    settings.AUDIT_PURGE_MAX_ROWS_PER_RUN = 100
    assert any("plafond" in e for e in retention.validate_settings())


def test_purge_disabled_emits_warning(settings):
    settings.AUDIT_PURGE_ENABLED = False
    assert any(w.id == "ragatm.W011" for w in check_audit_retention(None))


# --------------------------------------------------------------------------- #
# Tâche périodique
# --------------------------------------------------------------------------- #

def test_task_respects_the_disable_switch(settings, space, user):
    """L'interrupteur suspend la PURGE, jamais l'audit lui-même."""
    from apps.audit.tasks import purge_expired_audit_logs_task

    settings.AUDIT_PURGE_ENABLED = False
    settings.RAG_QUERY_LOG_RETENTION_DAYS = 90
    log = _query_log(space, user)
    _age(log, 100)

    result = purge_expired_audit_logs_task()
    assert result["skipped"] is True
    assert RagQueryLog.objects.filter(pk=log.pk).exists()
    # L'écriture d'audit reste possible : on n'a pas coupé la traçabilité.
    assert _query_log(space, user, "q2").pk


def test_task_purges_and_records_last_success(settings, space, user):
    from apps.audit.tasks import PURGE_LAST_SUCCESS, purge_expired_audit_logs_task
    from apps.rag_core.tabular import observability as obs

    obs.reset()
    settings.RAG_QUERY_LOG_RETENTION_DAYS = 90
    log = _query_log(space, user)
    _age(log, 100)

    purge_expired_audit_logs_task()
    assert not RagQueryLog.objects.filter(pk=log.pk).exists()
    # Sans cet horodatage, « la purge n'a jamais tourné » serait indétectable.
    assert obs.snapshot().get(PURGE_LAST_SUCCESS)


def test_all_audit_models_are_covered_by_a_rule():
    """Un modèle d'audit hors politique croît indéfiniment, en silence."""
    covered = {r.key for r in retention.RULES}
    assert covered == {
        "rag_query_log", "action_audit", "llm_egress_audit", "egress_policy_change",
    }
