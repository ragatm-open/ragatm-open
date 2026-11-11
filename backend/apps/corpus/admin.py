from django.contrib import admin

from .models import (
    BusinessDomain,
    DomainCorpus,
    DomainCorpusChunk,
    DomainCorpusDocument,
    WorkspaceDomainCorpusAccess,
)

# L'admin Django n'est accessible qu'aux ``is_staff``/``is_superuser`` : il
# constitue donc une surface réservée de fait au propriétaire global ragATM
# (cf. apps.accounts.permissions.is_ragatm_owner). Les corpus métier y sont
# inspectables/administrables sans jamais exposer la couche privée des workspaces.


@admin.register(BusinessDomain)
class BusinessDomainAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "slug", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "slug", "id")
    readonly_fields = ("created_at", "updated_at")


@admin.register(DomainCorpus)
class DomainCorpusAdmin(admin.ModelAdmin):
    list_display = (
        "id", "name", "slug", "domain", "is_active",
        "embedding_provider", "embedding_model", "embedding_consistency", "created_at",
    )
    list_filter = ("is_active", "domain", "embedding_provider")
    search_fields = ("name", "slug", "id", "domain__name")
    autocomplete_fields = ("domain",)
    readonly_fields = ("created_at", "updated_at", "embedding_consistency")

    @admin.display(description="Cohérence embeddings")
    def embedding_consistency(self, obj) -> str:
        """Verdict lisible : statut, décompte documentaire et validité de calibration.

        L'admin Django permet de modifier provider et modèle sans passer par l'API :
        sans cette colonne, un superuser pourrait rendre un corpus incohérent sans que
        rien ne le lui dise. La garde de réindexation, elle, s'applique au modèle (cf.
        `DomainCorpus.save`).
        """
        from apps.corpus.services.embedding_consistency import (
            active_calibration_state,
            consistency_report,
        )

        report = consistency_report(obj)
        valid, code, version = active_calibration_state(obj)
        calib = f"calibration v{version} valide" if valid else f"calibration {code or 'absente'}"
        return (
            f"{report.status} — {report.compatible}/{report.ready} cohérents, "
            f"{report.reindex_required} à réindexer, {report.unknown} inconnus · {calib}"
        )


@admin.register(DomainCorpusDocument)
class DomainCorpusDocumentAdmin(admin.ModelAdmin):
    list_display = ("id", "display_title", "corpus", "status", "size_bytes", "created_at")
    list_filter = ("status", "corpus")
    search_fields = ("title", "original_filename", "id", "sha256")
    autocomplete_fields = ("corpus",)
    readonly_fields = ("created_at", "updated_at", "sha256", "size_bytes")


@admin.register(DomainCorpusChunk)
class DomainCorpusChunkAdmin(admin.ModelAdmin):
    list_display = ("id", "corpus", "document", "chunk_index", "page_number", "created_at")
    list_filter = ("corpus",)
    search_fields = ("id", "document__original_filename")
    autocomplete_fields = ("corpus", "document")
    readonly_fields = ("created_at",)


@admin.register(WorkspaceDomainCorpusAccess)
class WorkspaceDomainCorpusAccessAdmin(admin.ModelAdmin):
    list_display = ("id", "workspace", "corpus", "enabled", "created_at")
    list_filter = ("enabled", "corpus")
    search_fields = ("id", "workspace__name", "corpus__name")
    autocomplete_fields = ("workspace", "corpus")
    readonly_fields = ("created_at", "updated_at")
