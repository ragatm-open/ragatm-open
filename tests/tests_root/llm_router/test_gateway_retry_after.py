"""Primitive partagée ``Retry-After`` — une définition pour V1 et V2."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from apps.llm_router.gateway import retry_after as RA
from apps.llm_router.gateway import vision as v1
from apps.llm_router.gateway import vision_v2 as v2

GATEWAY = Path(v1.__file__).resolve().parent


@pytest.mark.parametrize("headers,detail,expected", [
    ({"Retry-After": "10"}, None, 10.0),
    ({"Retry-After": " 12 "}, None, 12.0),
    ({"Retry-After": "0"}, None, 0.0),
    ({"Retry-After": "300"}, None, 300.0),
    ({"Retry-After": "301"}, None, None),
    ({"Retry-After": "-1"}, None, None),
    ({"Retry-After": "soon"}, None, None),
    ({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, None, None),
    ({}, {"retry_after_seconds": 7}, 7.0),
    (None, {"retry_after_seconds": 7}, 7.0),
    ({"Retry-After": "9"}, {"retry_after_seconds": 7}, 9.0),     # l'en-tête prime
    ({}, {"retry_after_seconds": True}, None),
    ({}, {"retry_after_seconds": 2.5}, None),
    ({}, {"retry_after_seconds": "4"}, 4.0),
    (None, None, None),
    ({}, "not a dict", None),
])
def test_parse_retry_after(headers, detail, expected):
    assert RA.parse_retry_after(headers, detail) == expected
    assert RA.RETRY_AFTER_MAX_S == 300


def test_v1_and_v2_use_the_same_primitive():
    assert v1.parse_retry_after is RA.parse_retry_after
    assert v2.parse_retry_after is RA.parse_retry_after


def test_v1_client_does_not_import_the_v2_module():
    tree = ast.parse((GATEWAY / "vision.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert not any("vision_v2" in m for m in imported), imported


def test_both_clients_expose_the_parsed_delay_unchanged():
    detail = {"status": "error", "provider": "ai_gateway", "message": "m", "code": "vision_overloaded",
              "retryable": True, "request_id": None, "retry_after_seconds": 10}
    e1 = v1.classify_vision_error(503, {"detail": detail})
    e2 = v2.classify_error(503, {"detail": detail}, None, request_id=None)
    assert e1.retry_after_s == e2.retry_after_s == 10.0
