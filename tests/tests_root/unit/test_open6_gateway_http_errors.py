"""projection HTTP des erreurs AI Gateway et classification des pannes de dépendance.

Verrouille le contrat d'erreur de l'édition Open : une panne nommée, un code HTTP
cohérent, `retryable` explicite, jamais de détail interne.
"""
from __future__ import annotations

import pytest

from apps.llm_router.gateway import exceptions as gx
from apps.llm_router.gateway.http_errors import (
    CODE_ERROR,
    CODE_UNAVAILABLE,
    RETRY_AFTER_SECONDS,
    classify_for_http,
    find_gateway_error,
)


@pytest.mark.parametrize(
    "exc",
    [
        gx.GatewayUnavailableError("no fallback"),
        gx.GatewayTemporaryUnavailable("connection refused"),
        gx.GatewayTimeoutError("timeout"),
        gx.GatewayRateLimited("429"),
        gx.GatewayStreamError("broken stream"),
    ],
)
def test_transient_failures_are_503_retryable(exc):
    p = classify_for_http(exc)
    assert (p.status, p.detail, p.retryable) == (503, CODE_UNAVAILABLE, True)
    assert p.headers() == {"Retry-After": str(RETRY_AFTER_SECONDS)}
    payload = p.payload(request_id="rid-1")
    assert payload == {"detail": CODE_UNAVAILABLE, "request_id": "rid-1", "retryable": True}
    # aucun détail interne : le message d'exception n'apparaît nulle part
    assert "connection" not in str(payload).lower() and "timeout" not in str(payload).lower()


@pytest.mark.parametrize(
    "exc",
    [
        gx.GatewayDisabledError("disabled"),
        gx.GatewayConfigError("AI_GATEWAY_BASE_URL manquante"),
        gx.GatewayAuthenticationError("401"),
        gx.GatewayContractError("schema"),
    ],
)
def test_deployment_defects_are_502_not_retryable(exc):
    p = classify_for_http(exc)
    assert (p.status, p.detail, p.retryable) == (502, CODE_ERROR, False)
    assert p.headers() == {}
    assert "AI_GATEWAY_BASE_URL" not in str(p.payload(request_id=None))


def test_unknown_gateway_subclass_defaults_to_transient():
    class Weird(gx.GatewayError):
        pass

    assert classify_for_http(Weird("x")).status == 503


def test_non_gateway_exception_is_not_classified():
    assert classify_for_http(RuntimeError("boom")) is None
    assert classify_for_http(ValueError("x")) is None


def test_find_gateway_error_walks_the_cause_chain():
    try:
        try:
            raise gx.GatewayTemporaryUnavailable("refused")
        except gx.GatewayError as inner:
            raise RuntimeError("retrieval leg failed") from inner
    except RuntimeError as outer:
        found = find_gateway_error(outer)
    assert isinstance(found, gx.GatewayTemporaryUnavailable)
    assert find_gateway_error(RuntimeError("plain")) is None


def test_dependency_classifier_names_redis_and_broker():
    import redis.exceptions as rx
    from kombu.exceptions import OperationalError

    from api.v1.exception_handler import classify_dependency

    assert classify_dependency(rx.ConnectionError("Error 111 connecting to redis:6379")) == "redis"
    assert classify_dependency(rx.TimeoutError("t")) == "redis"
    assert classify_dependency(OperationalError("broker down")) == "broker"
    try:
        try:
            raise rx.ConnectionError("inner")
        except rx.ConnectionError as e:
            raise RuntimeError("wrapped") from e
    except RuntimeError as wrapped:
        assert classify_dependency(wrapped) == "redis"
    assert classify_dependency(ValueError("x")) is None
    assert classify_dependency(KeyError("x")) is None
