"""Tests de l'API audit (owner/admin uniquement)."""
import pytest

from apps.audit.models import RagQueryLog

pytestmark = pytest.mark.django_db


def test_audit_requires_workspace_id(admin_client):
    res = admin_client.get("/api/v1/audit/rag-queries/")
    assert res.status_code == 400


def test_audit_member_forbidden(api_client, workspace):
    # api_client = member -> 403 (owner/admin requis)
    res = api_client.get(f"/api/v1/audit/rag-queries/?workspace_id={workspace.id}")
    assert res.status_code == 403


def test_audit_list_as_admin(admin_client, workspace, rag_space, user):
    RagQueryLog.objects.create(
        user=user, workspace=workspace, rag_space=rag_space,
        question="q", answer="a", status="ok",
    )
    res = admin_client.get(f"/api/v1/audit/rag-queries/?workspace_id={workspace.id}")
    assert res.status_code == 200
    assert len(res.data) >= 1
    assert res.data[0]["question"] == "q"


def test_audit_detail_not_found(admin_client):
    res = admin_client.get("/api/v1/audit/rag-queries/00000000-0000-0000-0000-000000000000/")
    assert res.status_code == 404


def test_audit_detail_as_admin(admin_client, workspace, rag_space, user):
    log = RagQueryLog.objects.create(
        user=user, workspace=workspace, rag_space=rag_space,
        question="détail", answer="a", status="ok",
    )
    res = admin_client.get(f"/api/v1/audit/rag-queries/{log.id}/")
    assert res.status_code == 200
    assert res.data["question"] == "détail"


# --------------------------------------------------------------------------
# Agrégation coûts LLM (périmètre C+)
# --------------------------------------------------------------------------
def test_llm_costs_requires_workspace_id(admin_client):
    res = admin_client.get("/api/v1/audit/llm-costs/")
    assert res.status_code == 400


def test_llm_costs_member_forbidden(api_client, workspace):
    res = api_client.get(f"/api/v1/audit/llm-costs/?workspace_id={workspace.id}")
    assert res.status_code == 403


def test_llm_costs_aggregates_as_admin(admin_client, workspace, rag_space, user):
    RagQueryLog.objects.create(
        user=user, workspace=workspace, rag_space=rag_space, question="q", answer="a",
        status="ok", llm_provider="openai", llm_model="gpt-4.1-mini",
        llm_total_tokens=100, llm_cost_estimated=0.001,
    )
    RagQueryLog.objects.create(
        user=user, workspace=workspace, rag_space=rag_space, question="q", answer="a",
        status="ok", llm_provider="openai", llm_model="mystery",
        llm_total_tokens=50, llm_cost_estimated=None,
    )
    res = admin_client.get(f"/api/v1/audit/llm-costs/?workspace_id={workspace.id}")
    assert res.status_code == 200
    assert res.data["totals"]["total_cost_usd"] == pytest.approx(0.001)
    assert res.data["totals"]["cost_unknown"] == 1
    models = {r["llm_model"] for r in res.data["rows"]}
    assert {"gpt-4.1-mini", "mystery"} <= models
