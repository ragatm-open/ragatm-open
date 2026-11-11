"""Test du harnais de rejeu owner_scope (Lot 2 — observation).

Vérifie que le harnais : exerce toutes les surfaces attendues (aucune silencieuse),
ne change aucune réponse HTTP, détecte le masquage cible (would_deny) et n'écrit
rien en base (rollback intégral).
"""
import io
import json

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command

from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


def test_replay_harness_report_and_rollback():
    out = io.StringIO()
    call_command("owner_scope_replay", "--simulate-acl", stdout=out)
    report = json.loads(out.getvalue())

    # Couverture : aucune surface attendue silencieuse.
    assert report["endpoints_silent"] == []
    assert "ragspaces.list" in report["endpoints_exercised"]
    # Invariant audit-only : réponses HTTP inchangées.
    assert report["http_unchanged"] is True
    assert report["http_changed"] == []
    # Signal : masquage cible détecté (cross-owner) + raisons peuplées.
    assert report["would_deny"]
    assert "owner_scope_mismatch" in report["by_reason"]
    assert report["list_findings"]  # ragspaces.list masquerait des espaces
    # Rollback intégral : aucune donnée synthétique persistée.
    assert not Workspace.objects.filter(name__startswith="ZZ_REPLAY").exists()
    assert not get_user_model().objects.filter(username__startswith="zzr_").exists()
