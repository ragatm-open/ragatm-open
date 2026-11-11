"""Tests purs du parsing JSON tolérant des intents (safe_json_extract)."""
from apps.chat.services.intents.common import safe_json_extract


def test_pure_json():
    assert safe_json_extract('{"intent": "news", "term": "Nordexia"}') == {
        "intent": "news",
        "term": "Nordexia",
    }


def test_json_embedded_in_prose():
    raw = 'Voici le plan : {"intent": "news", "term": "Nordexia"} merci.'
    out = safe_json_extract(raw)
    assert out is not None
    assert out.get("intent") == "news"


def test_invalid_returns_none():
    assert safe_json_extract("pas du json du tout") is None


def test_empty_returns_none():
    assert safe_json_extract("") is None
