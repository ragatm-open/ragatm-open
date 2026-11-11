"""Intégration coûts LLM (périmètre C+) : chat → RagQueryLog.cost, agrégations."""
import pytest
from django.contrib.auth import get_user_model

from apps.audit.aggregations import aggregate_costs
from apps.audit.models import RagQueryLog
from apps.rag_core.models import Chunk, Document, DocumentStatus, RagSpace
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db

#: Édition Open : aucune grille intégrée — provider facturé FICTIF et grille synthétique.
TABLE = {"modele-tarife": (0.0004, 0.0016)}


@pytest.fixture(autouse=True)
def _table(settings):
    settings.LLM_PRICING_PER_1K = dict(TABLE)


def _seed():
    ws = Workspace.objects.create(name="W")
    space = RagSpace.objects.create(name="S", workspace=ws)
    doc = Document.objects.create(rag_space=space, filename="d.pdf", status=DocumentStatus.READY)
    Chunk.objects.create(
        rag_space=space, document=doc, chunk_index=0,
        text="Paris est la capitale de la France.", page_number=1, meta={},
    )
    user = get_user_model().objects.create_user(username="u", password="pw")
    return ws, space, user


def test_chat_query_records_estimated_cost(monkeypatch):
    import apps.chat.services.chat_service as cs

    ws, space, user = _seed()

    def fake_generate_text(*, prompt, provider=None, model=None, timeout_s=None, **kwargs):
        return {
            "text": "Paris.",
            "provider": "metered",
            "model": "modele-tarife",
            "usage": {"input_tokens": 1000, "output_tokens": 500},
            "tokens": 1500,
            "cost": None,  # le router ne calcule plus le coût
        }

    monkeypatch.setattr(cs, "generate_text", fake_generate_text)

    out = cs.chat_query(
        rag_space_id=space.id, question="Quelle est la capitale de la France ?",
        user=user, rag_space=space,
    )
    assert out["status"] == "ok"
    assert out["llm_cost_estimated"] == pytest.approx(0.0012)

    log = RagQueryLog.objects.get(id=out["log_id"])
    assert log.llm_provider == "metered"
    assert log.llm_model == "modele-tarife"
    assert log.llm_prompt_tokens == 1000
    assert log.llm_completion_tokens == 500
    assert log.llm_total_tokens == 1500
    assert log.llm_cost_estimated == pytest.approx(0.0012)


def test_chat_query_unknown_model_cost_indeterminate(monkeypatch):
    import apps.chat.services.chat_service as cs

    ws, space, user = _seed()

    def fake_generate_text(*, prompt, provider=None, model=None, timeout_s=None, **kwargs):
        return {
            "text": "Paris.", "provider": "metered", "model": "mystery-x",
            "usage": {"input_tokens": 10, "output_tokens": 5}, "tokens": 15, "cost": None,
        }

    monkeypatch.setattr(cs, "generate_text", fake_generate_text)
    out = cs.chat_query(rag_space_id=space.id, question="capitale France ?", user=user, rag_space=space)

    log = RagQueryLog.objects.get(id=out["log_id"])
    # tokens connus, coût indéterminé (modèle non tarifé) -> None, jamais 0 inventé
    assert log.llm_total_tokens == 15
    assert log.llm_cost_estimated is None


def _mk_log(ws, space, user, *, provider, model, cost, total=100):
    return RagQueryLog.objects.create(
        user=user, workspace=ws, rag_space=space, question="q", answer="a",
        status=RagQueryLog.STATUS_OK, llm_provider=provider, llm_model=model,
        llm_total_tokens=total, llm_cost_estimated=cost,
    )


def test_aggregate_costs_groups_and_counts_unknown():
    ws, space, user = _seed()
    _mk_log(ws, space, user, provider="metered", model="modele-tarife", cost=0.001, total=100)
    _mk_log(ws, space, user, provider="metered", model="modele-tarife", cost=0.002, total=200)
    _mk_log(ws, space, user, provider="ollama", model="llama3.1:8b", cost=0.0, total=300)
    _mk_log(ws, space, user, provider="metered", model="mystery", cost=None, total=50)

    rows = aggregate_costs(workspace_id=ws.id, group_by=("llm_provider", "llm_model"))
    by_model = {(r["llm_provider"], r["llm_model"]): r for r in rows}

    mini = by_model[("metered", "modele-tarife")]
    assert mini["count"] == 2
    assert mini["total_tokens"] == 300
    assert mini["total_cost_usd"] == pytest.approx(0.003)
    assert mini["cost_unknown"] == 0

    mystery = by_model[("metered", "mystery")]
    assert mystery["total_cost_usd"] == 0.0  # somme de NULL = 0
    assert mystery["cost_unknown"] == 1  # mais signalé indéterminé


def test_llm_costs_command(capsys):
    from django.core.management import call_command

    ws, space, user = _seed()
    _mk_log(ws, space, user, provider="metered", model="modele-tarife", cost=0.005, total=100)
    _mk_log(ws, space, user, provider="metered", model="mystery", cost=None, total=50)

    call_command("llm_costs", "--days", "30", "--workspace", str(ws.id))
    out = capsys.readouterr().out
    assert "modele-tarife" in out
    assert "TOTAL" in out
    assert "indéterminé" in out  # le coût inconnu est signalé
