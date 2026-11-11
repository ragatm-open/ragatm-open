from rest_framework import serializers

class DocumentUploadSerializer(serializers.Serializer):
    rag_space_id = serializers.UUIDField()
    file = serializers.FileField()
