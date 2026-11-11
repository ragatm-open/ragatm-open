"""A1 — journalisation des tokens LLM via maybe_update_log.

Régression : les chemins chat écrivaient `llm_tokens=`/`llm_cost=` (champs
inexistants), silencieusement filtrés -> tokens jamais persistés. On vérifie
désormais que `llm_total_tokens` est persisté et qu'un champ inconnu est rejeté
AVEC un warning.
"""

import logging

import pytest
from django.contrib.auth import get_user_model

from apps.audit.models import RagQueryLog
from apps.chat.services.chat_audit import maybe_update_log
from apps.rag_core.models import RagSpace
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db

User = get_user_model()


@pytest.fixture
def log():
    user = User.objects.create_user(username="qa", password="pw")
    ws = Workspace.objects.create(name="W")
    space = RagSpace.objects.create(name="S", workspace=ws)
    return RagQueryLog.objects.create(
        user=user,
        workspace=ws,
        rag_space=space,
        question="q?",
        status=RagQueryLog.STATUS_OK,
    )


def test_llm_total_tokens_is_persisted(log):
    maybe_update_log(str(log.id), llm_total_tokens=42)
    log.refresh_from_db()
    assert log.llm_total_tokens == 42


def test_unknown_field_is_rejected_and_warns(log, caplog):
    with caplog.at_level(logging.WARNING):
        maybe_update_log(str(log.id), llm_tokens=42)

    log.refresh_from_db()
    # llm_tokens n'existe pas -> rien persisté
    assert log.llm_total_tokens is None
    # ... et un warning a été émis (avant : filtrage silencieux)
    assert any("llm_tokens" in r.getMessage() for r in caplog.records)


def test_known_fields_still_applied_when_mixed_with_unknown(log, caplog):
    with caplog.at_level(logging.WARNING):
        maybe_update_log(
            str(log.id),
            llm_total_tokens=7,
            llm_provider="openai",
            llm_cost=0.01,  # inconnu -> ignoré + warning
        )

    log.refresh_from_db()
    assert log.llm_total_tokens == 7
    assert log.llm_provider == "openai"
    assert any("llm_cost" in r.getMessage() for r in caplog.records)
