from django.contrib import admin

from .models import RagSpace, Document, Chunk
from apps.chat.models import PromptTemplate


@admin.register(PromptTemplate)
class PromptTemplateAdmin(admin.ModelAdmin):
    list_display = ("id", "workspace", "name", "version", "is_active", "created_at")
    list_filter = ("workspace", "is_active")
    search_fields = ("name", "content", "workspace__name")
    autocomplete_fields = ("workspace",)
    readonly_fields = ("created_at",)


@admin.register(RagSpace)
class RagSpaceAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "workspace", "created_at")
    list_filter = ("workspace",)
    search_fields = ("name", "workspace__name", "id")
    autocomplete_fields = ("workspace", "prompt_template")
    readonly_fields = ("created_at",)


# Optionnel : utile pour debug prod
@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ("id", "filename", "rag_space", "status", "created_at")
    list_filter = ("status", "rag_space")
    search_fields = ("filename", "id", "rag_space__name")
    autocomplete_fields = ("rag_space",)
    readonly_fields = ("created_at",)


@admin.register(Chunk)
class ChunkAdmin(admin.ModelAdmin):
    list_display = ("id", "rag_space", "document", "page_number", "created_at")
    list_filter = ("rag_space", "document")
    search_fields = ("id", "document__filename")
    autocomplete_fields = ("rag_space", "document")
    readonly_fields = ("created_at",)
