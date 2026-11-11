from rest_framework import serializers


class RagSpaceListItemSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    workspace_id = serializers.IntegerField()
    # Chat / génération
    llm_provider = serializers.CharField()
    llm_model = serializers.CharField(allow_blank=True)
    # Embeddings / indexation
    embedding_provider = serializers.CharField()
    embedding_model = serializers.CharField(allow_blank=True)
    # Repli LLM cross-provider (coût externe) : opt-in explicite par espace.
    allow_external_fallback = serializers.BooleanField(required=False, default=False)
    # True si au moins un document de l'espace doit être réindexé (config
    # d'embeddings changée depuis son indexation).
    reindex_required = serializers.BooleanField(required=False, default=False)
    # Rôle EFFECTIF de l'appelant sur cet espace (lecture seule pour l'UI ;
    # calculé par la garde canonique = autorité). Flag-aware.
    permission_level = serializers.CharField(required=False, allow_null=True)  # read|write|manage|null
    space_role = serializers.CharField(required=False, allow_null=True)        # manager|editor|viewer|null
    is_owner_implicit = serializers.BooleanField(required=False, default=False)
    # Capacité d'ÉDITION de la config modèles (provider/modèle chat + embeddings).
    # Par utilisateur (superuser actif) ; le backend reste l'autorité. Read-only UI.
    can_edit_model_configuration = serializers.BooleanField(required=False, default=False)
    # dépôt audio ouvert pour cet espace (drapeau ET liste d'espaces).
    audio_upload_enabled = serializers.BooleanField(required=False, default=False)


class RagSpacePatchSerializer(serializers.Serializer):
    # Tous optionnels => PATCH partiel.
    # NB: la validation métier (provider autorisé, clé OpenAI présente, modèle sûr)
    #     est faite dans la vue pour renvoyer des codes applicatifs explicites.
    name = serializers.CharField(max_length=200, required=False)
    # Chat / génération
    llm_provider = serializers.CharField(max_length=32, required=False)
    llm_model = serializers.CharField(max_length=128, required=False, allow_blank=True)
    # Embeddings / indexation (indépendants du chat)
    embedding_provider = serializers.CharField(max_length=32, required=False)
    embedding_model = serializers.CharField(max_length=128, required=False, allow_blank=True)
    # Repli LLM cross-provider (coût externe) : opt-in explicite par espace.
    allow_external_fallback = serializers.BooleanField(required=False)
    # Échappatoire de gouvernance : autorise un modèle COMPATIBLE mais NON QUALIFIÉ.
    # Ne franchit jamais une incompatibilité (dimension, capacité, registre) — un
    # droit ne rend pas un modèle 1024 compatible avec un index 768. Superuser only
    # par construction : le bloc qui la lit est déjà sous `is_model_manager`.
    force = serializers.BooleanField(required=False)

    def validate(self, attrs):
        # `force` seul n'est pas une modification : il qualifie une modification. Sans
        # cette exclusion, un PATCH `{"force": true}` passerait le garde-fou de payload
        # vide et n'aurait aucun effet.
        if not {k: v for k, v in attrs.items() if k != "force"}:
            raise serializers.ValidationError("No fields to update.")
        return attrs
