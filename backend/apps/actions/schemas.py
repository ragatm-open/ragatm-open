# backend/apps/actions/schemas.py
"""Schémas d'entrée stricts par action (audit §16).

Implémentés en **DRF serializers** (pydantic n'est pas une dépendance du projet).
Ils centralisent la validation aujourd'hui dispersée :
- bornage de ``question`` (corrige le ``CharField()`` sans ``max_length``) ;
- ``fts``/``lang``/``limit`` deviennent des champs officiels (plus de lecture
  brute depuis ``request.data``) ;
- clamps ``news`` (term/time_window_days/top_n_docs) rapatriés depuis
  ``intents/news.py``.

``validate_action_input(action, data)`` renvoie un dict validé ou lève
``ValidationError``. Aucune I/O, aucun accès DB : testable unitairement.
"""
from __future__ import annotations

from typing import Any

from rest_framework import serializers

# Longueur max de question (généreuse ; overridable via settings plus tard).
QUESTION_MAX_CHARS = 4000
TERM_MAX_CHARS = 200
NEWS_TERM_MAX_CHARS = 120


class SearchAnswerInput(serializers.Serializer):
    rag_space_id = serializers.UUIDField()
    question = serializers.CharField(min_length=1, max_length=QUESTION_MAX_CHARS, trim_whitespace=True)


class ListDocumentsInput(serializers.Serializer):
    rag_space_id = serializers.UUIDField()
    term = serializers.CharField(min_length=1, max_length=TERM_MAX_CHARS, trim_whitespace=True)
    fts = serializers.BooleanField(required=False, default=False)
    lang = serializers.ChoiceField(choices=["simple", "french", "english"], required=False, default="simple")
    limit = serializers.IntegerField(required=False, default=100, min_value=1, max_value=1000)


class NewsInput(serializers.Serializer):
    rag_space_id = serializers.UUIDField()
    term = serializers.CharField(min_length=1, max_length=NEWS_TERM_MAX_CHARS, trim_whitespace=True)
    time_window_days = serializers.IntegerField(required=False, default=120, min_value=7, max_value=365)
    top_n_docs = serializers.IntegerField(required=False, default=5, min_value=3, max_value=10)


class SummarizeInput(serializers.Serializer):
    rag_space_id = serializers.UUIDField()
    document_id = serializers.UUIDField(required=False, allow_null=True)
    document_reference = serializers.CharField(required=False, allow_blank=True, max_length=TERM_MAX_CHARS)
    scope = serializers.ChoiceField(choices=["document", "page"], required=False, default="document")
    page = serializers.IntegerField(required=False, allow_null=True, min_value=1)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        # Il faut au moins une façon de désigner le document.
        if not attrs.get("document_id") and not (attrs.get("document_reference") or "").strip():
            raise serializers.ValidationError(
                {"document": "document_id ou document_reference requis"}
            )
        if attrs.get("scope") == "page" and not attrs.get("page"):
            raise serializers.ValidationError({"page": "page requise pour scope=page"})
        return attrs


ACTION_SCHEMAS: dict[str, type[serializers.Serializer]] = {
    "search.answer": SearchAnswerInput,
    "search.list_documents": ListDocumentsInput,
    "search.news": NewsInput,
    "document.summarize": SummarizeInput,
}


def validate_action_input(action: str, data: dict[str, Any]) -> dict[str, Any]:
    """Valide ``data`` contre le schéma de ``action``. Lève ValidationError."""
    schema_cls = ACTION_SCHEMAS.get(action)
    if schema_cls is None:
        raise serializers.ValidationError({"action": f"schéma inconnu pour {action!r}"})
    ser = schema_cls(data=data)
    ser.is_valid(raise_exception=True)
    return dict(ser.validated_data)
