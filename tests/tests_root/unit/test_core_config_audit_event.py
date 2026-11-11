"""Tests PR-E — journal append-only CoreConfigAuditEvent + helper d'écriture."""
import pytest

from apps.rag_core.core.config_audit import record_config_event


@pytest.fixture(autouse=True)
def _inject_synthetic_key(synthetic_core_key):
    """`CORE_CONFIG_SELFTEST_SCOPE` est une clé de BANC D'ESSAI.

    Elle n'existe pas dans le registre canonique : sans cette injection, ces
    tests échoueraient — et c'est voulu. Aucun réglage de production ne monte
    jusqu'au ragspace, et en promouvoir un pour les besoins du test aurait ouvert
    un levier runtime sur un paramètre réel.
    """


pytestmark = pytest.mark.django_db


def test_record_creates_event(user):
    ev = record_config_event(
        action="set",
        key="CORE_CONFIG_SELFTEST_SCOPE",
        scope_type="global",
        scope_ref="",
        old_value="false",
        new_value="true",
        reason="enable core",
        actor=user,
    )
    from apps.rag_core.models import CoreConfigAuditEvent

    assert CoreConfigAuditEvent.objects.count() == 1
    row = CoreConfigAuditEvent.objects.get(pk=ev.id)
    assert row.action == "set"
    assert row.key == "CORE_CONFIG_SELFTEST_SCOPE"
    assert row.old_value == "false" and row.new_value == "true"
    assert row.actor_id == user.id
    assert row.environment  # rempli par défaut (current_environment)


def test_event_is_append_only_no_update(user):
    ev = record_config_event(action="set", key="X", new_value="1", actor=user)
    ev.reason = "tampered"
    with pytest.raises(ValueError):
        ev.save()


def test_event_is_append_only_no_delete(user):
    ev = record_config_event(action="set", key="X", new_value="1", actor=user)
    with pytest.raises(ValueError):
        ev.delete()


def test_actor_set_null_does_not_break_append_only(django_user_model):
    # La suppression de l'utilisateur (SET_NULL via UPDATE de queryset) ne doit pas
    # être bloquée par le garde-fou append-only (qui ne couvre que save()/delete()).
    u = django_user_model.objects.create_user(username="tmp", password="pw", email="t@e.com")
    ev = record_config_event(action="set", key="X", new_value="1", actor=u)
    u.delete()
    from apps.rag_core.models import CoreConfigAuditEvent

    row = CoreConfigAuditEvent.objects.get(pk=ev.id)
    assert row.actor_id is None  # actor remis à NULL, événement conservé
