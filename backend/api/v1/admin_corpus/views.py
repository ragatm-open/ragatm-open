# backend/api/v1/admin_corpus/views.py
from __future__ import annotations

"""API admin globale des corpus métier (Phase 3 + ingestion Phase 5).

TOUTES les vues exigent ``IsRagATMOwner`` (propriétaire global ragATM = superuser).
Aucun rôle de workspace (owner/admin/member/viewer) ne peut atteindre ces endpoints.
Anti-énumération : un identifiant inexistant renvoie 404 APRÈS le contrôle de
permission (un non-owner reçoit toujours 403, jamais d'indice d'existence).
"""

import logging
import mimetypes

from django.conf import settings
from django.db.models import Count
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsRagATMOwner
from apps.corpus.models import (
    BusinessDomain,
    DomainCorpus,
    DomainCorpusAdmissionCalibration,
    DomainCorpusChunk,
    DomainCorpusDocument,
    WorkspaceDomainCorpusAccess,
)
from apps.corpus.domain.embedding_identity import (
    CorpusEmbeddingIdentityError,
    resolve_creation_identity,
)
from apps.corpus.services.embedding_consistency import identity_changed
from apps.corpus.models.document import CorpusDocumentStatus
from apps.corpus.services.document_counts import (
    corpus_document_counts,
    counts_from_annotated,
    document_count_annotations,
)
from apps.corpus.services.ingest import (
    ingest_corpus_document_from_upload,
    reindex_corpus,
    reindex_corpus_document,
)
from apps.llm_router.capabilities import ALLOWED_EMBEDDING_PROVIDERS
from apps.rag_core.services.ingest.extraction import (
    ExtractionContext,
    RejectedFile,
    UnsupportedFormat,
    validate_and_classify,
)
from apps.rag_core.services.ingest.limits import (
    UploadLimitExceeded,
    check_file_size,
    check_pdf_pages,
)
from apps.workspaces.models import Workspace

from .serializers import (
    BusinessDomainPatchSerializer,
    BusinessDomainSerializer,
    DomainCorpusPatchSerializer,
    DomainCorpusSerializer,
    WorkspaceCorpusAccessCreateSerializer,
)

logger = logging.getLogger(__name__)

OWNER_PERMS = [IsAuthenticated, IsRagATMOwner]


# ---------------------------------------------------------------------------
# Serialization helpers (dicts ; jamais de chemin disque interne exposé)
# ---------------------------------------------------------------------------
def _domain_dict(d: BusinessDomain, *, corpora_count: int | None = None) -> dict:
    out = {
        "id": str(d.id),
        "slug": d.slug,
        "name": d.name,
        "description": d.description,
        "is_active": d.is_active,
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "updated_at": d.updated_at.isoformat() if d.updated_at else None,
    }
    if corpora_count is not None:
        out["corpora_count"] = int(corpora_count)
    return out


def _embedding_state(c: DomainCorpus) -> dict:
    """État de cohérence CALCULÉ côté serveur. Jamais fourni par le client.

    Une valeur de cohérence envoyée par un client serait une affirmation invérifiable
    sur l'état de la base : tout est recalculé ici, à chaque lecture.
    """
    from apps.corpus.services.embedding_consistency import (
        active_calibration_state,
        consistency_report,
        embedding_dimension,
    )

    report = consistency_report(c)
    valid, error_code, version = active_calibration_state(c)
    out = report.to_dict()
    out.update({
        "embedding_dimension": embedding_dimension(),
        "embedding_identity_complete": bool(
            (c.embedding_provider or "").strip() and (c.embedding_model or "").strip()
        ),
        "embeddings_reindex_required": report.reindex_required > 0,
        "active_calibration_valid": bool(valid),
        "active_calibration_error_code": error_code,
        "active_calibration_version": version,
    })
    return out


def _corpus_dict(c: DomainCorpus, *, counts: dict | None = None) -> dict:
    out = {
        "id": str(c.id),
        "domain_id": str(c.domain_id),
        "domain_name": getattr(getattr(c, "domain", None), "name", None),
        "slug": c.slug,
        "name": c.name,
        "description": c.description,
        "is_active": c.is_active,
        "embedding_provider": c.embedding_provider,
        "embedding_model": c.embedding_model,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }
    # `documents_count` reste la clé historique ; les compteurs par statut s'y ajoutent
    # sans la remplacer (cf. apps.corpus.services.document_counts).
    if counts is not None:
        out.update({k: int(v) for k, v in counts.items()})
    out.update(_embedding_state(c))
    return out


def _document_dict(doc: DomainCorpusDocument, *, chunks_count: int | None = None) -> dict:
    out = {
        "id": str(doc.id),
        "corpus_id": str(doc.corpus_id),
        "title": doc.title,
        "original_filename": doc.original_filename,
        "mime_type": doc.mime_type,
        "size_bytes": doc.size_bytes,
        "status": doc.status,
        "error_message": doc.error_message,
        "source_url": doc.source_url,
        "source_reference": doc.source_reference,
        "version_label": doc.version_label,
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
        "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
    }
    if chunks_count is not None:
        out["chunks_count"] = int(chunks_count)
    return out


def _access_dict(a: WorkspaceDomainCorpusAccess) -> dict:
    return {
        "id": str(a.id),
        "workspace_id": a.workspace_id,
        "corpus_id": str(a.corpus_id),
        "corpus_name": getattr(getattr(a, "corpus", None), "name", None),
        "enabled": a.enabled,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


# ===========================================================================
# Business domains
# ===========================================================================
class BusinessDomainListCreateView(APIView):
    permission_classes = OWNER_PERMS

    def get(self, request):
        qs = BusinessDomain.objects.annotate(_cc=Count("corpora")).order_by("name")
        return Response([_domain_dict(d, corpora_count=d._cc) for d in qs])

    def post(self, request):
        ser = BusinessDomainSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        v = ser.validated_data
        if BusinessDomain.objects.filter(slug=v["slug"]).exists():
            return Response({"detail": "slug_already_exists"}, status=status.HTTP_409_CONFLICT)
        d = BusinessDomain.objects.create(
            slug=v["slug"], name=v["name"],
            description=v.get("description", ""), is_active=v.get("is_active", True),
        )
        return Response(_domain_dict(d), status=status.HTTP_201_CREATED)


class BusinessDomainDetailView(APIView):
    permission_classes = OWNER_PERMS

    def get(self, request, domain_id):
        d = get_object_or_404(BusinessDomain, id=domain_id)
        cc = DomainCorpus.objects.filter(domain_id=d.id).count()
        return Response(_domain_dict(d, corpora_count=cc))

    def patch(self, request, domain_id):
        d = get_object_or_404(BusinessDomain, id=domain_id)
        ser = BusinessDomainPatchSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        v = ser.validated_data
        if "slug" in v and v["slug"] != d.slug and BusinessDomain.objects.filter(slug=v["slug"]).exists():
            return Response({"detail": "slug_already_exists"}, status=status.HTTP_409_CONFLICT)
        for field in ("slug", "name", "description", "is_active"):
            if field in v:
                setattr(d, field, v[field])
        d.save()
        return Response(_domain_dict(d))

    def delete(self, request, domain_id):
        d = get_object_or_404(BusinessDomain, id=domain_id)
        d.delete()  # CASCADE -> corpora, documents, chunks, accès
        return Response(status=status.HTTP_204_NO_CONTENT)


# ===========================================================================
# Domain corpora
# ===========================================================================
class DomainCorpusListCreateView(APIView):
    permission_classes = OWNER_PERMS

    def get(self, request):
        qs = (
            DomainCorpus.objects.select_related("domain")
            .annotate(**document_count_annotations("documents"))
            .order_by("domain__name", "name")
        )
        domain_id = (request.query_params.get("domain_id") or "").strip()
        if domain_id:
            qs = qs.filter(domain_id=domain_id)
        return Response([_corpus_dict(c, counts=counts_from_annotated(c)) for c in qs])

    def post(self, request):
        ser = DomainCorpusSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        v = ser.validated_data
        domain = BusinessDomain.objects.filter(id=v["domain_id"]).first()
        if domain is None:
            return Response({"detail": "domain_not_found"}, status=status.HTTP_400_BAD_REQUEST)
        if DomainCorpus.objects.filter(domain=domain, slug=v["slug"]).exists():
            return Response({"detail": "slug_already_exists_in_domain"}, status=status.HTTP_409_CONFLICT)
        # Identité d'embeddings RÉSOLUE ici, une fois, et PERSISTÉE.
        #
        # L'ancien code écrivait `v.get("embedding_model", "")` : le serializer portant
        # `default=""`, la clé était toujours présente et la chaîne vide gagnait sur le
        # défaut de champ Django. Tout corpus créé par l'interface — qui n'envoie que
        # domaine, nom et slug — naissait donc sans modèle, avec un leg vectoriel refusé
        # et une admission impossible. C'est l'origine exacte de l'incident `refvc`.
        try:
            identity = resolve_creation_identity(
                provider=v.get("embedding_provider"),
                model=v.get("embedding_model"),
                dimension=int(getattr(settings, "RAG_EMBEDDING_DIM", 768)),
            )
        except CorpusEmbeddingIdentityError as exc:
            return Response({"detail": exc.code}, status=status.HTTP_400_BAD_REQUEST)
        c = DomainCorpus.objects.create(
            domain=domain,
            slug=v["slug"],
            name=v["name"],
            description=v.get("description", ""),
            is_active=v.get("is_active", True),
            embedding_provider=identity.provider,
            embedding_model=identity.model,
        )
        return Response(_corpus_dict(c), status=status.HTTP_201_CREATED)


class DomainCorpusDetailView(APIView):
    permission_classes = OWNER_PERMS

    def get(self, request, corpus_id):
        c = get_object_or_404(DomainCorpus.objects.select_related("domain"), id=corpus_id)
        return Response(_corpus_dict(c, counts=corpus_document_counts(c.id)))

    def patch(self, request, corpus_id):
        c = get_object_or_404(DomainCorpus.objects.select_related("domain"), id=corpus_id)
        ser = DomainCorpusPatchSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        v = ser.validated_data
        if "slug" in v and v["slug"] != c.slug and DomainCorpus.objects.filter(domain_id=c.domain_id, slug=v["slug"]).exists():
            return Response({"detail": "slug_already_exists_in_domain"}, status=status.HTTP_409_CONFLICT)

        # Changement d'IDENTITÉ d'embeddings : jamais silencieux.
        #
        # Les vecteurs stockés ne sont pas refaits par un PATCH. Sans garde, le
        # retrieval encoderait désormais la question avec le NOUVEAU modèle et la
        # comparerait aux ANCIENS vecteurs. Mesuré : cette comparaison ne lève aucune
        # erreur, rend une page pleine de candidats, des scores plausibles et un contenu
        # quasi aléatoire. On marque donc les documents et on invalide la calibration —
        # même politique que le PATCH d'un RagSpace.
        requested_provider = v.get("embedding_provider", c.embedding_provider)
        requested_model = v.get("embedding_model", c.embedding_model)
        identity_change = ("embedding_provider" in v or "embedding_model" in v) and identity_changed(
            c, provider=requested_provider, model=requested_model
        )
        if identity_change:
            try:
                resolve_creation_identity(
                    provider=requested_provider, model=requested_model,
                    dimension=int(getattr(settings, "RAG_EMBEDDING_DIM", 768)),
                )
            except CorpusEmbeddingIdentityError as exc:
                return Response({"detail": exc.code}, status=status.HTTP_400_BAD_REQUEST)

        for field in ("slug", "name", "description", "is_active", "embedding_provider", "embedding_model"):
            if field in v:
                setattr(c, field, v[field])
        c.save()

        # `DomainCorpus.save` a déjà marqué les documents et invalidé la calibration :
        # la vue MESURE l'effet pour le rapporter, elle ne le refait pas. Dupliquer
        # l'action ici la rendrait dépendante du chemin d'écriture — exactement ce que
        # la garde au niveau modèle évite.
        effects = {"affected_documents": 0, "calibrations_invalidated": 0}
        if identity_change:
            effects = {
                "affected_documents": DomainCorpusDocument.objects.filter(
                    corpus=c, reindex_required=True
                ).count(),
                "calibrations_invalidated": DomainCorpusAdmissionCalibration.objects.filter(
                    corpus=c, is_active=False
                ).count(),
            }
        payload = _corpus_dict(c, counts=corpus_document_counts(c.id))
        payload["embeddings_reindex_required"] = bool(identity_change)
        payload["affected_documents"] = effects["affected_documents"]
        payload["calibrations_invalidated"] = effects["calibrations_invalidated"]
        return Response(payload)

    def delete(self, request, corpus_id):
        c = get_object_or_404(DomainCorpus, id=corpus_id)
        c.delete()  # CASCADE -> documents, chunks, accès workspace
        return Response(status=status.HTTP_204_NO_CONTENT)


# ===========================================================================
# Corpus documents (listing / upload / detail / delete)
# ===========================================================================
def _mimetype_or_octet(filename: str) -> str:
    ctype, _ = mimetypes.guess_type(filename or "")
    return ctype or "application/octet-stream"


class DomainCorpusDocumentsView(APIView):
    """GET liste des documents d'un corpus / POST upload (multipart 'file')."""

    permission_classes = OWNER_PERMS

    def get(self, request, corpus_id):
        get_object_or_404(DomainCorpus, id=corpus_id)
        qs = (
            DomainCorpusDocument.objects.filter(corpus_id=corpus_id)
            .annotate(_cc=Count("chunks"))
            .order_by("-created_at")
        )
        return Response([_document_dict(d, chunks_count=d._cc) for d in qs])

    def post(self, request, corpus_id):
        corpus = get_object_or_404(DomainCorpus, id=corpus_id)

        upload_file = request.FILES.get("file")
        if not upload_file:
            return Response({"detail": "file required"}, status=status.HTTP_400_BAD_REQUEST)

        # --- Garde-fous d'ingestion réutilisés (mêmes règles que le privé) ---
        try:
            check_file_size(int(getattr(upload_file, "size", None) or 0))
        except UploadLimitExceeded as e:
            return Response({"detail": e.code, "error": e.detail}, status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)

        content = upload_file.read()
        upload_file.seek(0)
        filename = getattr(upload_file, "name", None) or "upload"
        content_type = getattr(upload_file, "content_type", None) or _mimetype_or_octet(filename)

        is_pdf = filename.lower().endswith(".pdf") or content_type == "application/pdf"
        if is_pdf:
            try:
                check_pdf_pages(content)
            except UploadLimitExceeded as e:
                return Response({"detail": e.code, "error": e.detail}, status=status.HTTP_400_BAD_REQUEST)

        # Validation pré-extraction : type RÉEL (allowlist) + refus exe/macro/archive.
        try:
            # allow_excel/allow_pptx=False : l'ingestion corpus est SYNCHRONE (aucune
            # tâche Celery). Un classeur ou une présentation y bloquerait la requête
            # HTTP -> refus net, avant tout Document.
            validate_and_classify(
                content,
                ExtractionContext(filename=filename, declared_content_type=content_type),
                allow_excel=False,
                allow_pptx=False,
            )
        except UnsupportedFormat as e:
            return Response({"detail": e.code, "error": e.detail}, status=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE)
        except RejectedFile as e:
            http = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE if e.code == "file_too_large" else status.HTTP_400_BAD_REQUEST
            return Response({"detail": e.code, "error": e.detail}, status=http)

        data = request.data if hasattr(request, "data") else {}
        result = ingest_corpus_document_from_upload(
            corpus=corpus,
            filename=filename,
            content_type=content_type,
            file_bytes=content,
            title=(data.get("title") or "").strip(),
            source_url=(data.get("source_url") or "").strip(),
            source_reference=(data.get("source_reference") or "").strip(),
            version_label=(data.get("version_label") or "").strip(),
        )

        doc = DomainCorpusDocument.objects.filter(id=result.document_id).first()
        body = {
            "ok": result.status != CorpusDocumentStatus.FAILED,
            "deduped": result.deduped,
            "document": _document_dict(doc, chunks_count=result.chunks_created) if doc else None,
        }
        http = status.HTTP_200_OK if result.deduped else status.HTTP_201_CREATED
        return Response(body, status=http)


class DomainCorpusDocumentDetailView(APIView):
    permission_classes = OWNER_PERMS

    def get(self, request, document_id):
        doc = get_object_or_404(DomainCorpusDocument, id=document_id)
        cc = DomainCorpusChunk.objects.filter(document_id=doc.id).count()
        return Response(_document_dict(doc, chunks_count=cc))

    def delete(self, request, document_id):
        doc = get_object_or_404(DomainCorpusDocument, id=document_id)
        sf = getattr(doc, "storage_file", None)
        if sf is not None and getattr(sf, "name", ""):
            try:
                sf.delete(save=False)
            except Exception:
                pass
        DomainCorpusChunk.objects.filter(document_id=doc.id).delete()
        doc.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class DomainCorpusDocumentReindexView(APIView):
    permission_classes = OWNER_PERMS

    def post(self, request, document_id):
        doc = get_object_or_404(DomainCorpusDocument, id=document_id)
        result = reindex_corpus_document(document=doc)
        doc.refresh_from_db()
        return Response(
            {
                "ok": result.status != CorpusDocumentStatus.FAILED,
                "document": _document_dict(doc, chunks_count=result.chunks_created),
            }
        )


class DomainCorpusReindexView(APIView):
    permission_classes = OWNER_PERMS

    def post(self, request, corpus_id):
        corpus = get_object_or_404(DomainCorpus, id=corpus_id)
        summary = reindex_corpus(corpus=corpus)
        return Response({"ok": True, "corpus_id": str(corpus.id), **summary})


# ===========================================================================
# Workspace <-> corpus associations
# ===========================================================================
class WorkspaceCorpusAccessView(APIView):
    """GET associations d'un workspace / POST associe un corpus au workspace."""

    permission_classes = OWNER_PERMS

    def get(self, request, workspace_id):
        get_object_or_404(Workspace, id=workspace_id)
        qs = (
            WorkspaceDomainCorpusAccess.objects.select_related("corpus")
            .filter(workspace_id=workspace_id)
            .order_by("corpus__name")
        )
        return Response([_access_dict(a) for a in qs])

    def post(self, request, workspace_id):
        ws = get_object_or_404(Workspace, id=workspace_id)
        ser = WorkspaceCorpusAccessCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        v = ser.validated_data
        corpus = DomainCorpus.objects.filter(id=v["corpus_id"]).first()
        if corpus is None:
            return Response({"detail": "corpus_not_found"}, status=status.HTTP_400_BAD_REQUEST)
        access, created = WorkspaceDomainCorpusAccess.objects.update_or_create(
            workspace=ws, corpus=corpus, defaults={"enabled": v.get("enabled", True)}
        )
        return Response(_access_dict(access), status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class WorkspaceCorpusAccessDeleteView(APIView):
    permission_classes = OWNER_PERMS

    def delete(self, request, workspace_id, corpus_id):
        get_object_or_404(Workspace, id=workspace_id)
        deleted, _ = WorkspaceDomainCorpusAccess.objects.filter(
            workspace_id=workspace_id, corpus_id=corpus_id
        ).delete()
        # idempotent : 204 même si l'association n'existait pas
        return Response(status=status.HTTP_204_NO_CONTENT)
