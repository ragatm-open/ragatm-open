"""Tests du collecteur d'observation owner_scope (agrégation offline)."""
import io
import json

import pytest
from django.core.management import call_command

from apps.workspaces.management.commands.owner_scope_observation import aggregate


def _rs(endpoint, audit, *, rid, role="owner", reason=None, perm="read", scope=1, extra=None):
    p = {"event": "ragspace_decision", "endpoint": endpoint, "request_id": rid,
         "workspace_role": role, "owner_scope_id": scope, "permission": perm,
         "audit": audit, "reason": reason, "rag_space_id": "rs-x"}
    if extra:
        p.update(extra)
    return "INFO owner_scope_audit " + json.dumps(p)


def _task(job, decision, *, rid, reason=None):
    p = {"event": "task_revalidation", "job_label": job, "request_id": rid,
         "decision": decision, "reason": reason, "rag_space_id": "rs-x"}
    return json.dumps({"logger": "owner_scope.audit", "message": "owner_scope_audit " + json.dumps(p)})


@pytest.fixture
def lines():
    return [
        _rs("ragspaces.stats", "matched_allow", rid="r1"),
        _rs("documents.access", "would_deny", rid="r2", reason="owner_scope_mismatch"),
        _rs("documents.access", "would_deny", rid="r2", reason="owner_scope_mismatch"),  # doublon
        _rs("ragspaces.stats", "would_allow", rid="r3", role="viewer", perm="write",
            extra={"question": "SECRET-CONTENT", "filename": "confidentiel.pdf"}),
        _rs("chat.threads.list", "matched_deny", rid=None, role=None),
        '{"event":"ragspace_list_decision","endpoint":"ragspaces.list","request_id":"r5",'
        '"legacy_count":2,"target_count":1,"would_hide_count":1}',
        _task("document_summary", "deny", rid="r6", reason="membership_missing"),
        _task("ingest_document_task", "deny", rid=None, reason="context_missing"),
        _task("ragspace_reindex", "allow", rid="r7"),
        "ceci n'est pas du json {{{",   # ligne invalide
        "",                              # ligne vide (ignorée, pas une erreur)
    ]


def test_aggregates_four_classifications(lines):
    r = aggregate(lines)
    assert r["totals"]["matched_allow"] == 1
    assert r["totals"]["matched_deny"] == 1
    assert r["totals"]["would_allow"] == 1
    assert r["totals"]["would_deny"] == 1  # le doublon r2 est dédupliqué


def test_deduplicates(lines):
    assert aggregate(lines)["dedup_removed"] == 1


def test_separates_http_and_tasks(lines):
    hv = aggregate(lines)["http_vs_task"]
    assert hv["http"] == 4 and hv["task"] == 3 and hv["list"] == 1


def test_detects_context_missing(lines):
    r = aggregate(lines)
    assert len(r["context_missing"]) == 1
    assert r["context_missing"][0]["job_label"] == "ingest_document_task"


def test_detects_would_allow(lines):
    r = aggregate(lines)
    assert r["totals"]["would_allow"] == 1
    assert any(f["kind"] == "would_allow" for f in r["findings"])


def test_detects_silent_surfaces(lines):
    # beaucoup de surfaces attendues ne sont pas dans le jeu synthétique
    assert "core.retrieve" in aggregate(lines)["silent_surfaces"]


def test_request_id_coverage(lines):
    cov = aggregate(lines)["request_id_coverage"]
    # 9 événements retenus (10e dédupliqué), 2 sans request_id
    assert cov["without"] == 2 and 0 < cov["rate"] < 1


def test_ignores_sensitive_fields(lines):
    blob = json.dumps(aggregate(lines))
    assert "SECRET-CONTENT" not in blob and "confidentiel.pdf" not in blob


def test_tolerates_invalid_line(lines):
    assert aggregate(lines)["parse_errors"] == 1


def test_deterministic(lines):
    assert json.dumps(aggregate(lines), sort_keys=True) == json.dumps(aggregate(lines), sort_keys=True)


def test_would_deny_breakdown(lines):
    wdb = aggregate(lines)["would_deny_breakdown"]
    assert wdb["expected"].get("owner_scope_mismatch") == 1


def test_gate_exit_code(tmp_path, lines):
    p = tmp_path / "logs.jsonl"
    p.write_text("\n".join(lines))
    with pytest.raises(SystemExit) as exc:
        call_command("owner_scope_observation", "--input", str(p), "--fail-on-would-allow", stdout=io.StringIO())
    assert exc.value.code == 1
    # sans le gate would_allow, pas d'échec (would_deny seul n'échoue pas par défaut)
    out = io.StringIO()
    call_command("owner_scope_observation", "--input", str(p), stdout=out)
    assert json.loads(out.getvalue())["totals"]["would_allow"] == 1
