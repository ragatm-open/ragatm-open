"""Tests PR-F — édition staging-only des overrides Core (PUT/unset)."""
import uuid

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient


@pytest.fixture(autouse=True)
def _inject_synthetic_key(synthetic_core_key):
    """`CORE_CONFIG_SELFTEST_SCOPE` est une clé de BANC D'ESSAI.

    Elle n'existe pas dans le registre canonique : sans cette injection, ces
    tests échoueraient — et c'est voulu. Aucun réglage de production ne monte
    jusqu'au ragspace, et en promouvoir un pour les besoins du test aurait ouvert
    un levier runtime sur un paramètre réel.
    """


PUT = "/api/v1/admin/core-config/override/"
UNSET = "/api/v1/admin/core-config/override/unset/"
EFFECTIVE = "/api/v1/admin/core-config/effective/"
CONFIG_AUDIT = "/api/v1/admin/core-config/config-audit/"


def _superuser():
    U = get_user_model()
    return U.objects.create_superuser(username="root", password="pw", email="root@e.com")


@pytest.fixture
def edit_client(db, settings):
    """Superuser + édition activée + environnement non-prod (staging-like)."""
    settings.CORE_CONFIG_EDIT_ENABLED = True
    settings.DJANGO_ENV = "dev"
    c = APIClient()
    c.force_authenticate(user=_superuser())
    return c


# =========================== A. Gates =====================================

@pytest.mark.django_db
def test_flag_off_put_refused(db, settings):
    settings.CORE_CONFIG_EDIT_ENABLED = False
    settings.DJANGO_ENV = "dev"
    c = APIClient()
    c.force_authenticate(user=_superuser())
    r = c.put(PUT, {"scope_type": "global", "scope_ref": "", "key": "CORE_CONFIG_SELFTEST_SCOPE", "value": "true"}, format="json")
    assert r.status_code == 409
    assert r.json()["detail"] == "editing_disabled"


@pytest.mark.django_db
def test_flag_off_unset_refused(db, settings):
    settings.CORE_CONFIG_EDIT_ENABLED = False
    settings.DJANGO_ENV = "dev"
    c = APIClient()
    c.force_authenticate(user=_superuser())
    r = c.post(UNSET, {"scope_type": "global", "scope_ref": "", "key": "CORE_CONFIG_SELFTEST_SCOPE"}, format="json")
    assert r.status_code == 409


@pytest.mark.django_db
def test_prod_put_refused_even_flag_on(db, settings):
    settings.CORE_CONFIG_EDIT_ENABLED = True
    settings.DJANGO_ENV = "prod"
    c = APIClient()
    c.force_authenticate(user=_superuser())
    r = c.put(PUT, {"scope_type": "global", "scope_ref": "", "key": "CORE_CONFIG_SELFTEST_SCOPE", "value": "true"}, format="json")
    assert r.status_code == 403
    assert r.json()["detail"] == "editing_forbidden_in_prod"


@pytest.mark.django_db
def test_prod_unset_refused_even_flag_on(db, settings):
    settings.CORE_CONFIG_EDIT_ENABLED = True
    settings.DJANGO_ENV = "prod"
    c = APIClient()
    c.force_authenticate(user=_superuser())
    r = c.post(UNSET, {"scope_type": "global", "scope_ref": "", "key": "CORE_CONFIG_SELFTEST_SCOPE"}, format="json")
    assert r.status_code == 403


# =========================== B. Permissions ================================

@pytest.mark.django_db
def test_unauthenticated_refused(client, settings):
    settings.CORE_CONFIG_EDIT_ENABLED = True
    assert client.put(PUT, {}, content_type="application/json").status_code in (401, 403)


@pytest.mark.django_db
def test_normal_user_refused(api_client, settings):
    settings.CORE_CONFIG_EDIT_ENABLED = True
    settings.DJANGO_ENV = "dev"
    assert api_client.put(PUT, {"scope_type": "global", "key": "CORE_CONFIG_SELFTEST_SCOPE", "value": "true"}, format="json").status_code == 403


@pytest.mark.django_db
def test_workspace_owner_refused(admin_client, settings):
    settings.CORE_CONFIG_EDIT_ENABLED = True
    settings.DJANGO_ENV = "dev"
    assert admin_client.put(PUT, {"scope_type": "global", "key": "CORE_CONFIG_SELFTEST_SCOPE", "value": "true"}, format="json").status_code == 403


@pytest.mark.django_db
def test_superuser_accepted(edit_client):
    r = edit_client.put(PUT, {"scope_type": "global", "scope_ref": "", "key": "CORE_CONFIG_SELFTEST_SCOPE", "value": "true"}, format="json")
    assert r.status_code == 200
    assert r.json()["ok"] is True


# =========================== C. Validation =================================

def _put(client, **kw):
    return client.put(PUT, kw, format="json")


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("NOPE", "x"),  # clé inconnue du registre
        ("AI_GATEWAY_TIMEOUT_SECONDS", "abc"),  # type invalide
        ("AI_GATEWAY_TIMEOUT_SECONDS", "0"),  # sous la borne basse
        ("AI_GATEWAY_TIMEOUT_SECONDS", "301"),  # au-dessus de la borne haute
    ],
    ids=["unknown-key", "timeout-not-int", "timeout-zero", "timeout-too-big"],
)
def test_invalid_global_override_is_400(edit_client, key, value):
    r = _put(edit_client, scope_type="global", scope_ref="", key=key, value=value)
    assert r.status_code == 400


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("request_kwargs", "detail"),
    [
        (
            dict(scope_type="global", scope_ref="", key="RAG_EMBEDDING_DIM", value="1024"),
            "key_not_editable",
        ),
        (
            dict(scope_type="environment", scope_ref="prod", key="AI_GATEWAY_TIMEOUT_SECONDS", value="45"),
            "environment_prod_forbidden",
        ),
        (
            dict(scope_type="workspace", scope_ref="999999", key="CORE_CONFIG_SELFTEST_SCOPE", value="true"),
            "workspace_not_found",
        ),
    ],
    ids=["embedding-dim-not-editable", "environment-prod-scope-refused", "workspace-not-found"],
)
def test_refused_override_names_its_reason(edit_client, request_kwargs, detail):
    r = _put(edit_client, **request_kwargs)
    assert r.status_code == 400
    assert r.json()["detail"] == detail


@pytest.mark.django_db
def test_gateway_timeout_workspace_scope_400(edit_client, workspace):
    r = _put(edit_client, scope_type="workspace", scope_ref=str(workspace.id), key="AI_GATEWAY_TIMEOUT_SECONDS", value="45")
    assert r.status_code == 400
    assert r.json()["detail"] == "override_level_not_allowed"


@pytest.mark.django_db
def test_gateway_enabled_not_editable_400(edit_client):
    """Durcissement P0 : la politique IA n'est plus pilotable par overlay."""
    r = _put(edit_client, scope_type="environment", scope_ref="dev", key="AI_GATEWAY_ENABLED", value="false")
    assert r.status_code == 400
    assert r.json()["detail"] == "key_not_editable"


@pytest.mark.django_db
def test_gateway_base_url_not_editable_400(edit_client):
    """Clé d'INFRASTRUCTURE : elle route aussi les embeddings, donc l'indexation."""
    from apps.rag_core.models import CoreConfigOverride

    r = _put(edit_client, scope_type="global", scope_ref="", key="AI_GATEWAY_BASE_URL", value="http://198.51.100.9:8100")
    assert r.status_code == 400
    assert r.json()["detail"] == "key_not_editable"
    assert not CoreConfigOverride.objects.filter(key="AI_GATEWAY_BASE_URL").exists()
    # La valeur refusée n'est jamais reproduite dans la réponse.
    assert "198.51.100.9" not in str(r.json())


@pytest.mark.django_db
def test_scope_beyond_the_declared_maximum_is_400(edit_client, rag_space):
    """Une portée plus fine que `max_scope` est refusée.

    Le témoin était `RAG_USE_CORE_RETRIEVE` (max_scope=workspace), retirée par le
    chantier de configuration. On teste désormais sur `AI_GATEWAY_TIMEOUT_SECONDS`
    (max_scope=environment) : demander une portée `ragspace` reste interdit.
    `CORE_CONFIG_SELFTEST_SCOPE` ne conviendrait PAS ici — elle monte justement
    jusqu'au ragspace, c'est sa raison d'être.
    """
    r = _put(edit_client, scope_type="ragspace", scope_ref=str(rag_space.id),
             key="AI_GATEWAY_TIMEOUT_SECONDS", value="42")
    assert r.status_code == 400
    assert r.json()["detail"] == "override_level_not_allowed"


@pytest.mark.django_db
def test_ragspace_not_found_400(edit_client):
    r = _put(edit_client, scope_type="ragspace", scope_ref=str(uuid.uuid4()), key="CORE_CONFIG_SELFTEST_SCOPE", value="true")
    assert r.status_code == 400
    assert r.json()["detail"] == "ragspace_not_found"


@pytest.mark.django_db
def test_reason_too_long_400(edit_client):
    r = _put(edit_client, scope_type="global", scope_ref="", key="CORE_CONFIG_SELFTEST_SCOPE", value="true", reason="x" * 5000)
    assert r.status_code == 400
    assert r.json()["detail"] == "reason_too_long"


# =========================== D. Success set ================================

@pytest.mark.django_db
def test_set_global_creates_override(edit_client):
    from apps.rag_core.models import CoreConfigOverride

    r = _put(edit_client, scope_type="global", scope_ref="", key="CORE_CONFIG_SELFTEST_SCOPE", value="true", reason="enable")
    assert r.status_code == 200
    body = r.json()
    assert body["action"] == "set"
    assert body["override"]["source"] == "db:global"
    assert body["event_id"]
    assert body["diagnostic_overlay_only"] is True
    row = CoreConfigOverride.objects.get(scope_type="global", scope_ref="", key="CORE_CONFIG_SELFTEST_SCOPE")
    assert row.value == "true"
    # effective reflète l'override
    cfg = {e["key"]: e for e in body["effective"]["config"]}
    assert cfg["CORE_CONFIG_SELFTEST_SCOPE"]["effective_value"] is True
    assert cfg["CORE_CONFIG_SELFTEST_SCOPE"]["source"] == "db:global"


@pytest.mark.django_db
def test_set_environment_scope(edit_client):
    from apps.rag_core.models import CoreConfigOverride

    r = _put(edit_client, scope_type="environment", scope_ref="dev", key="AI_GATEWAY_TIMEOUT_SECONDS", value="45")
    assert r.status_code == 200
    assert CoreConfigOverride.objects.filter(
        scope_type="environment", scope_ref="dev", key="AI_GATEWAY_TIMEOUT_SECONDS"
    ).exists()


@pytest.mark.django_db
def test_set_timeout_stores_canonical(edit_client):
    from apps.rag_core.models import CoreConfigOverride

    _put(edit_client, scope_type="global", scope_ref="", key="AI_GATEWAY_TIMEOUT_SECONDS", value="30")
    assert CoreConfigOverride.objects.get(key="AI_GATEWAY_TIMEOUT_SECONDS").value == "30"


@pytest.mark.django_db
@pytest.mark.parametrize("raw", ["1", "yes", "on", "true", "True"])
def test_bool_truthy_coercion(edit_client, raw):
    from apps.rag_core.models import CoreConfigOverride

    _put(edit_client, scope_type="global", scope_ref="", key="CORE_CONFIG_SELFTEST_SCOPE", value=raw)
    assert CoreConfigOverride.objects.get(key="CORE_CONFIG_SELFTEST_SCOPE").value == "true"


@pytest.mark.django_db
@pytest.mark.parametrize("raw", ["0", "no", "off", "false"])
def test_bool_falsy_coercion(edit_client, raw):
    from apps.rag_core.models import CoreConfigOverride

    _put(edit_client, scope_type="global", scope_ref="", key="CORE_CONFIG_SELFTEST_SCOPE", value=raw)
    assert CoreConfigOverride.objects.get(key="CORE_CONFIG_SELFTEST_SCOPE").value == "false"


@pytest.mark.django_db
def test_set_creates_audit_event_with_old_value(edit_client):
    from apps.rag_core.models import CoreConfigAuditEvent

    _put(edit_client, scope_type="global", scope_ref="", key="AI_GATEWAY_TIMEOUT_SECONDS", value="30")
    _put(edit_client, scope_type="global", scope_ref="", key="AI_GATEWAY_TIMEOUT_SECONDS", value="45")
    evs = list(CoreConfigAuditEvent.objects.filter(key="AI_GATEWAY_TIMEOUT_SECONDS", action="set").order_by("created_at"))
    assert len(evs) == 2
    assert evs[0].old_value == "" and evs[0].new_value == "30"
    assert evs[1].old_value == "30" and evs[1].new_value == "45"


# =========================== E. Success unset =============================

@pytest.mark.django_db
def test_unset_removes_override_and_audits(edit_client):
    from apps.rag_core.models import CoreConfigAuditEvent, CoreConfigOverride

    _put(edit_client, scope_type="global", scope_ref="", key="CORE_CONFIG_SELFTEST_SCOPE", value="true")
    r = edit_client.post(UNSET, {"scope_type": "global", "scope_ref": "", "key": "CORE_CONFIG_SELFTEST_SCOPE", "reason": "rollback"}, format="json")
    assert r.status_code == 200
    assert r.json()["action"] == "unset"
    assert not CoreConfigOverride.objects.filter(key="CORE_CONFIG_SELFTEST_SCOPE").exists()
    ev = CoreConfigAuditEvent.objects.get(action="unset", key="CORE_CONFIG_SELFTEST_SCOPE")
    assert ev.old_value == "true" and ev.new_value == ""
    # effective retombe sur settings
    cfg = {e["key"]: e for e in r.json()["effective"]["config"]}
    assert cfg["CORE_CONFIG_SELFTEST_SCOPE"]["source"] == "settings"


@pytest.mark.django_db
def test_unset_absent_returns_404(edit_client):
    r = edit_client.post(UNSET, {"scope_type": "global", "scope_ref": "", "key": "CORE_CONFIG_SELFTEST_SCOPE"}, format="json")
    assert r.status_code == 404
    assert r.json()["detail"] == "override_not_found"


# =========================== F. Runtime unchanged =========================

@pytest.mark.django_db
def test_runtime_gateway_unchanged_after_set(edit_client, settings):
    import apps.llm_router.router as router

    settings.AI_GATEWAY_ENABLED = False
    _put(edit_client, scope_type="global", scope_ref="", key="AI_GATEWAY_ENABLED", value="true")
    assert router._gateway_enabled() is False  # runtime lit les settings, pas l'overlay


@pytest.mark.django_db
def test_runtime_still_reads_settings_after_an_override(edit_client, settings):
    """Overlay écrit ≠ overlay actif : tant que `CORE_CONFIG_RUNTIME_ENABLED=0`,
    le runtime lit les settings et ignore l'override enregistré."""
    from config.settings.access import get_registered_setting

    settings.CORE_CONFIG_SELFTEST_SCOPE = False
    _put(edit_client, scope_type="global", scope_ref="", key="CORE_CONFIG_SELFTEST_SCOPE", value="true")
    assert get_registered_setting("CORE_CONFIG_SELFTEST_SCOPE") is False


# =========================== G. Redaction / sécurité ======================

@pytest.mark.django_db
def test_base_url_redacted_in_effective_config(edit_client, settings):
    """La redaction de l'URL privée reste vérifiée, hors chemin d'écriture.

    `AI_GATEWAY_BASE_URL` n'est plus éditable (durcissement P0) : on ne peut plus
    l'observer via une réponse d'écriture. Sa redaction dans la config effective
    reste, elle, un invariant de sécurité.
    """
    settings.AI_GATEWAY_BASE_URL = "http://198.51.100.9:8100"
    r = _put(edit_client, scope_type="global", scope_ref="", key="CORE_CONFIG_SELFTEST_SCOPE", value="true")
    body = r.json()
    assert "198.51.100.9" not in str(body)
    cfg = {e["key"]: e for e in body["effective"]["config"]}
    assert cfg["AI_GATEWAY_BASE_URL"]["effective_value"] == "http://…:8100"
    assert cfg["AI_GATEWAY_BASE_URL"]["redacted"] is True


@pytest.mark.django_db
def test_base_url_redacted_in_config_audit(edit_client):
    """Le journal redacte la clé sensible, quelle que soit son origine.

    L'événement est enregistré directement : le journal doit rester sûr même pour
    une clé qu'aucune écriture d'API ne peut plus produire (rollback manuel,
    migration, écriture SQL).
    """
    from apps.rag_core.core.config_audit import record_config_event

    record_config_event(
        action="set",
        key="AI_GATEWAY_BASE_URL",
        scope_type="global",
        scope_ref="",
        old_value="",
        new_value="http://198.51.100.9:8100",
        reason="test de redaction",
        actor=None,
    )
    body = edit_client.get(CONFIG_AUDIT).json()
    assert "198.51.100.9" not in str(body)
    assert body["results"][0]["new_value"] == "http://…:8100"
    assert body["results"][0]["redacted"] is True


@pytest.mark.django_db
def test_no_email_in_responses(edit_client):
    r = _put(edit_client, scope_type="global", scope_ref="", key="CORE_CONFIG_SELFTEST_SCOPE", value="true")
    assert "root@e.com" not in str(r.json())
    assert "root@e.com" not in str(edit_client.get(CONFIG_AUDIT).json())
