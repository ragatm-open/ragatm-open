"""Sanitation des métadonnées d'audit (§16) — aucune donnée sensible stockée."""
from __future__ import annotations

import pytest

from apps.audit.action_audit import sanitize_metadata

_REDACTED = "[redacted]"


def test_denied_keys_redacted_recursively():
    md = {
        "password": "hunter2",
        "provider_name": "ollama",
        "nested": {"api_key": "sk-xxx", "ok": 1},
        "list": [{"token": "abc"}, {"size_bytes": 10}],
    }
    out = sanitize_metadata(md)
    assert out["password"] == _REDACTED
    assert out["provider_name"] == "ollama"           # clé autorisée conservée
    assert out["nested"]["api_key"] == _REDACTED
    assert out["nested"]["ok"] == 1
    assert out["list"][0]["token"] == _REDACTED
    assert out["list"][1]["size_bytes"] == 10


@pytest.mark.parametrize(
    "key",
    ["prompt", "question", "answer", "content", "chunk", "embedding", "signed_url", "cookie", "traceback", "authorization"],
)
def test_all_forbidden_keys_redacted(key):
    out = sanitize_metadata({key: "SENSIBLE"})
    assert out[key] == _REDACTED
    assert "SENSIBLE" not in str(out)


def test_case_insensitive_and_substring():
    out = sanitize_metadata({"API_KEY": "x", "user_password_hash": "y", "Authorization": "z"})
    assert all(v == _REDACTED for v in out.values())


def test_long_string_truncated():
    out = sanitize_metadata({"note": "a" * 5000})
    assert out["note"].endswith("…") and len(out["note"]) <= 501


def test_depth_limited():
    deep = {"a": {"b": {"c": {"d": {"e": {"f": "x"}}}}}}
    out = sanitize_metadata(deep)
    assert "truncated:depth" in str(out)


def test_oversized_json_replaced():
    out = sanitize_metadata({f"provider_{i}": "v" * 400 for i in range(60)})
    # soit tronqué par nombre de clés, soit rejeté globalement pour taille
    assert out.get("[truncated:size]") is True or "[truncated:keys]" in out


def test_empty_metadata():
    assert sanitize_metadata(None) == {}
    assert sanitize_metadata({}) == {}
