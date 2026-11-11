from __future__ import annotations

from rest_framework import serializers

from api.v1.limits import MAX_QUESTION_CHARS
from apps.chat.services.corpus_mode_service import CHOICES


class ChatRequestSerializer(serializers.Serializer):
    rag_space_id = serializers.UUIDField()
    #: Borne du CONTRAT, pas de l'interface : le classifieur d'intention tourne
    #: sur chaque requête AVANT tout retrieval, et son coût est quadratique sur
    #: certaines entrées. Refus explicite (400), jamais de troncature — une
    #: question coupée produirait une réponse à une autre question.
    question = serializers.CharField(allow_blank=True, max_length=MAX_QUESTION_CHARS)
    thread_id = serializers.UUIDField(required=False, allow_null=True)
    #: Lot 3 — choix de mode envoyé par un BOUTON, jamais tapé. Identifiant stable :
    #: le libellé affiché peut changer sans casser le contrat. Quand il est présent,
    #: c'est lui qui fait autorité, et `question` n'est plus interprétée du tout.
    corpus_choice = serializers.ChoiceField(
        choices=CHOICES, required=False, allow_null=True
    )
    #: Préférence de LANGUE DE RÉPONSE IA (distincte de la locale d'interface).
    #: OPTIONNEL, défaut ``auto`` (langue de la question). N'a d'effet que si le flag
    #: plateforme ``RAG_ANSWER_LANGUAGE_POLICY_ENABLED`` est actif ; sinon ignoré.
    answer_language = serializers.ChoiceField(
        choices=["auto", "fr", "en"], required=False, default="auto"
    )

    def validate(self, attrs):
        # `question` peut être vide UNIQUEMENT si un choix structuré la remplace :
        # c'est le serveur qui écrira alors le libellé persisté.
        if not (attrs.get("question") or "").strip() and not attrs.get("corpus_choice"):
            raise serializers.ValidationError({"question": "This field may not be blank."})
        return attrs


class ChatThreadCreateSerializer(serializers.Serializer):
    rag_space_id = serializers.UUIDField()
    title = serializers.CharField(required=False, allow_blank=True, allow_null=True)


class ChatThreadSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    rag_space_id = serializers.UUIDField()
    title = serializers.CharField(allow_blank=True, allow_null=True)
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()


class ChatMessageSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    thread_id = serializers.UUIDField()
    role = serializers.ChoiceField(choices=["user", "assistant", "system"])
    content = serializers.CharField()
    sources = serializers.JSONField(required=False, allow_null=True)
    request_id = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    rag_query_log_id = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    # Permet au frontend de replacer la carte d'analyse sous son message après un
    # rechargement du fil.
    analysis_id = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    # Résumé de document persisté dans le fil. Tous facultatifs : les messages
    # antérieurs à la migration 0007 n'en portent aucun et restent sérialisables.
    # ``kind`` vide = message standard ; ``status`` vide = sans cycle de vie.
    kind = serializers.CharField(required=False, allow_blank=True)
    status = serializers.CharField(required=False, allow_blank=True)
    document_id = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    summary_id = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    # Lot 3 — ce message attend-il ENCORE un choix de mode ? Calculé par le serveur
    # à la lecture (jamais stocké, jamais déduit du Markdown) : c'est ce qui fait
    # réapparaître les boutons après un rechargement, et ce qui les fait disparaître
    # dès que le choix a été consommé.
    awaiting_corpus_choice = serializers.BooleanField(required=False)
    global_analysis_available = serializers.BooleanField(required=False)
    created_at = serializers.DateTimeField()
