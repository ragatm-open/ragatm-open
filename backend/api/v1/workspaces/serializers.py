from rest_framework import serializers
from apps.workspaces.models import Membership

# Rôles assignables par les endpoints de gestion (invitation, ajout, promotion).
# `owner` en est VOLONTAIREMENT exclu : le rôle propriétaire ne s'attribue jamais
# via ces flux (ni création, ni promotion, ni auto-promotion). Le transfert de
# propriété est une opération distincte, réservée à un owner (hors de ces vues).
ASSIGNABLE_ROLE_CHOICES = [
    c for c in Membership.ROLE_CHOICES if c[0] != Membership.ROLE_OWNER
]


class WorkspaceListItemSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    role = serializers.CharField()
    # Enrichissements navigation multi-workspace (lecture seule ; le backend reste
    # l'autorité). `is_current` est un concept CLIENT (état local) — non exposé ici.
    workspace_is_active = serializers.BooleanField(required=False, default=True)
    membership_id = serializers.IntegerField(required=False)
    effective_owner_scope_id = serializers.IntegerField(required=False, allow_null=True)


class WorkspaceMemberSerializer(serializers.Serializer):
    # id = id de Membership (utile pour patch/delete)
    id = serializers.IntegerField()
    user_id = serializers.IntegerField()
    email = serializers.EmailField()
    role = serializers.ChoiceField(choices=Membership.ROLE_CHOICES)
    created_at = serializers.DateTimeField()


class WorkspaceMemberAddSerializer(serializers.Serializer):
    """
    Invite / add member by email.
    - If user doesn't exist: create with unusable password (done in view/service).
    - Membership role defaults to 'member' if omitted.
    """
    email = serializers.EmailField()
    # `owner` est refusé au niveau du serializer (choix restreints) : un admin
    # ne peut jamais attribuer le rôle propriétaire, même en forgeant la requête.
    role = serializers.ChoiceField(choices=ASSIGNABLE_ROLE_CHOICES, required=False)

    def validate_email(self, v: str) -> str:
        return (v or "").strip().lower()

    def validate_role(self, v):
        if v == Membership.ROLE_OWNER:
            raise serializers.ValidationError("Le rôle owner ne peut pas être attribué.")
        return v

    def validate(self, attrs):
        # Default role
        if not attrs.get("role"):
            attrs["role"] = getattr(Membership, "ROLE_MEMBER", "member")
        return attrs


class WorkspaceMemberPatchSerializer(serializers.Serializer):
    # `owner` exclu : aucune promotion vers propriétaire via cet endpoint.
    role = serializers.ChoiceField(choices=ASSIGNABLE_ROLE_CHOICES)

    def validate_role(self, v):
        if v == Membership.ROLE_OWNER:
            raise serializers.ValidationError("Le rôle owner ne peut pas être attribué.")
        return v


class InvitationCreateSerializer(serializers.Serializer):
    """Création d'invitation : email + rôle (owner exclu)."""

    email = serializers.EmailField()
    role = serializers.ChoiceField(choices=ASSIGNABLE_ROLE_CHOICES, required=False)

    def validate_email(self, v: str) -> str:
        return (v or "").strip().lower()

    def validate_role(self, v):
        if v == Membership.ROLE_OWNER:
            raise serializers.ValidationError("Le rôle owner ne peut pas être attribué.")
        return v


class InvitationListItemSerializer(serializers.Serializer):
    """Sortie de liste : JAMAIS de token / token_hash / invitation_url."""

    id = serializers.UUIDField()
    email = serializers.EmailField()
    role = serializers.CharField()
    status = serializers.CharField()
    generation = serializers.IntegerField()
    created_at = serializers.DateTimeField()
    expires_at = serializers.DateTimeField()
    can_regenerate = serializers.BooleanField()
    can_revoke = serializers.BooleanField()


class WorkspaceRagSpaceCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200)
    # Optionnels : si absents, les défauts du modèle s'appliquent (OpenAI / OpenAI).
    llm_provider = serializers.CharField(max_length=32, required=False)
    llm_model = serializers.CharField(max_length=128, required=False, allow_blank=True)
    embedding_provider = serializers.CharField(max_length=32, required=False)
    embedding_model = serializers.CharField(max_length=128, required=False, allow_blank=True)

    def validate_name(self, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise serializers.ValidationError("name is required")
        return v
