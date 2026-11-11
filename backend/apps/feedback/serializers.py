"""Sérialiseurs du feedback.

Règle : la sortie API ne renvoie JAMAIS de contenu (question, réponse, extrait,
prompt). Elle renvoie des hash, des identifiants et des métadonnées. Un client
qui a besoin du contenu passe par le snapshot, qui a ses propres droits.
"""
from __future__ import annotations

from rest_framework import serializers

from apps.feedback.models import COMMENT_MAX_CHARS, AnswerFeedback


class FeedbackCreateSerializer(serializers.Serializer):
    """Payload d'un clic sur le pouce.

    Compatibilité ascendante assumée : ``reason`` (ancien nom) reste accepté et
    traduit, et les doublons historiques ``feedback_*`` envoyés par le frontend
    sont ignorés silencieusement plutôt que rejetés — un client non déployé ne
    doit pas voir ses signalements refusés.
    """

    log_id = serializers.UUIDField()
    score = serializers.IntegerField()
    kind = serializers.CharField(required=False, allow_blank=True, max_length=32)
    reason = serializers.CharField(required=False, allow_blank=True, max_length=64)
    comment = serializers.CharField(
        required=False, allow_blank=True, max_length=COMMENT_MAX_CHARS
    )
    expected_document = serializers.CharField(
        required=False, allow_blank=True, max_length=512
    )
    expected_term = serializers.CharField(required=False, allow_blank=True, max_length=512)
    message_id = serializers.UUIDField(required=False, allow_null=True)
    thread_id = serializers.UUIDField(required=False, allow_null=True)

    def validate_score(self, value):
        if int(value) not in (AnswerFeedback.RATING_UP, AnswerFeedback.RATING_DOWN):
            raise serializers.ValidationError("score must be 1 or -1")
        return int(value)


class FeedbackUpdateSerializer(serializers.Serializer):
    """PATCH — l'utilisateur corrige son signalement, il ne le réécrit pas.

    ``score`` est volontairement absent : changer d'avis sur la note produit un
    autre cas, pas une réécriture du précédent.
    """

    kind = serializers.CharField(required=False, allow_blank=True, max_length=32)
    comment = serializers.CharField(
        required=False, allow_blank=True, max_length=COMMENT_MAX_CHARS
    )
    expected_document = serializers.CharField(
        required=False, allow_blank=True, max_length=512
    )
    expected_term = serializers.CharField(required=False, allow_blank=True, max_length=512)


class FeedbackAckSerializer(serializers.ModelSerializer):
    """Accusé de réception rendu à l'utilisateur : aucune donnée sensible."""

    has_snapshot = serializers.SerializerMethodField()

    class Meta:
        model = AnswerFeedback
        fields = (
            "id",
            "rating",
            "kind",
            "status",
            "priority_score",
            "created_at",
            "has_snapshot",
        )
        read_only_fields = fields

    def get_has_snapshot(self, obj) -> bool:
        return obj.has_snapshot
