# backend/api/v1/admin_corpus/serializers.py
"""Serializers de l'API admin globale des corpus métier (Phase 3).

Tous ces endpoints sont réservés au propriétaire global ragATM (IsRagATMOwner).
Les serializers de sortie n'exposent JAMAIS de chemin disque interne
(``storage_file``) ni le texte indexé.
"""
from rest_framework import serializers

from apps.corpus.domain.embedding_identity import ALLOWED_CORPUS_EMBEDDING_PROVIDERS


# ---------------------------------------------------------------------------
# Business domains
# ---------------------------------------------------------------------------
class BusinessDomainSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True)
    slug = serializers.SlugField(max_length=80)
    name = serializers.CharField(max_length=200)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    is_active = serializers.BooleanField(required=False, default=True)
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)
    corpora_count = serializers.IntegerField(read_only=True, required=False)


class BusinessDomainPatchSerializer(serializers.Serializer):
    slug = serializers.SlugField(max_length=80, required=False)
    name = serializers.CharField(max_length=200, required=False)
    description = serializers.CharField(required=False, allow_blank=True)
    is_active = serializers.BooleanField(required=False)


# ---------------------------------------------------------------------------
# Domain corpora
# ---------------------------------------------------------------------------
class DomainCorpusSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True)
    domain_id = serializers.UUIDField()
    domain_name = serializers.CharField(read_only=True, required=False)
    slug = serializers.SlugField(max_length=80)
    name = serializers.CharField(max_length=200)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    is_active = serializers.BooleanField(required=False, default=True)
    # Pas de `default` ici : le défaut est un choix MÉTIER, résolu une seule fois par
    # `resolve_creation_identity`. Un `default=""` réintroduirait exactement le défaut
    # corrigé — la vue recevait alors toujours la clé, et écrivait la chaîne vide.
    embedding_provider = serializers.ChoiceField(
        choices=sorted(ALLOWED_CORPUS_EMBEDDING_PROVIDERS), required=False
    )
    embedding_model = serializers.CharField(max_length=128, required=False, allow_blank=True)
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)
    # `documents_count` reste le TOTAL (compatibilité). Les trois compteurs suivants
    # disent ce que le total ne dit pas : combien de documents sont réellement
    # interrogeables par le chat. Cf. apps.corpus.services.document_counts.
    documents_count = serializers.IntegerField(read_only=True, required=False)
    ready_documents_count = serializers.IntegerField(read_only=True, required=False)
    processing_documents_count = serializers.IntegerField(read_only=True, required=False)
    failed_documents_count = serializers.IntegerField(read_only=True, required=False)


class DomainCorpusPatchSerializer(serializers.Serializer):
    slug = serializers.SlugField(max_length=80, required=False)
    name = serializers.CharField(max_length=200, required=False)
    description = serializers.CharField(required=False, allow_blank=True)
    is_active = serializers.BooleanField(required=False)
    embedding_provider = serializers.ChoiceField(
        choices=sorted(ALLOWED_CORPUS_EMBEDDING_PROVIDERS), required=False
    )
    embedding_model = serializers.CharField(max_length=128, required=False, allow_blank=True)


# ---------------------------------------------------------------------------
# Corpus documents (lecture seule côté API ; jamais de storage_file exposé)
# ---------------------------------------------------------------------------
class DomainCorpusDocumentSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True)
    corpus_id = serializers.UUIDField(read_only=True)
    title = serializers.CharField(read_only=True)
    original_filename = serializers.CharField(read_only=True)
    mime_type = serializers.CharField(read_only=True)
    size_bytes = serializers.IntegerField(read_only=True)
    status = serializers.CharField(read_only=True)
    error_message = serializers.CharField(read_only=True)
    source_url = serializers.CharField(read_only=True)
    source_reference = serializers.CharField(read_only=True)
    version_label = serializers.CharField(read_only=True)
    chunks_count = serializers.IntegerField(read_only=True, required=False)
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)


# ---------------------------------------------------------------------------
# Workspace <-> corpus associations
# ---------------------------------------------------------------------------
class WorkspaceCorpusAccessSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True)
    workspace_id = serializers.IntegerField(read_only=True)
    corpus_id = serializers.UUIDField(read_only=True)
    corpus_name = serializers.CharField(read_only=True, required=False)
    enabled = serializers.BooleanField(read_only=True)
    created_at = serializers.DateTimeField(read_only=True)


class WorkspaceCorpusAccessCreateSerializer(serializers.Serializer):
    corpus_id = serializers.UUIDField()
    enabled = serializers.BooleanField(required=False, default=True)
