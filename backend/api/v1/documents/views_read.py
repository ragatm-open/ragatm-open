# backend/api/v1/documents/views_read.py
from __future__ import annotations

import re

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import listing
from .views_common import (
    FileResponse,
    _serialize_document,
    count_chunks_for_document,
    load_accessible_document,
    logger,
    require_rag_space_access,
    uuid,
)


def _safe_int(v, default: int) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _boolish(v) -> bool:
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    return s in ("1", "true", "t", "yes", "y", "on")


# --- Sécurité du téléchargement des fichiers originaux ---------------------
#
# Les fichiers uploadés sont du contenu non fiable. Servir un HTML/SVG/XML
# uploadé en `inline`, same-origin, avec le Content-Type déclaré par le client
# permet un XSS stocké (le script s'exécute dans l'origine de l'app et peut
# appeler les API authentifiées via les cookies de session). On force donc :
#   - un Content-Disposition `attachment` pour tout type actif (et par défaut) ;
#   - un Content-Type neutralisé (octet-stream) pour les types actifs ;
#   - `X-Content-Type-Options: nosniff` et une CSP `sandbox` sur la réponse.
# Seuls quelques types explicitement sûrs restent prévisualisables en `inline`.

# Types que le navigateur peut interpréter activement (exécution de script).
_ACTIVE_MIME_TYPES = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "image/svg+xml",
        "application/xml",
        "text/xml",
        "application/javascript",
        "text/javascript",
        "application/x-javascript",
        "application/ecmascript",
        "text/ecmascript",
        "application/xhtml",
        "text/html-sandboxed",
    }
)

# Types autorisés à s'afficher en `inline` (prévisualisation sûre).
_INLINE_SAFE_MIME_TYPES = frozenset(
    {
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/bmp",
        "image/tiff",
        "text/plain",
        "text/csv",
    }
)

_FILENAME_UNSAFE = re.compile(r'[\r\n"\\]')


def _safe_filename(name) -> str:
    """Empêche l'injection d'en-tête via le nom de fichier."""
    name = str(name or "document").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    name = _FILENAME_UNSAFE.sub("_", name).strip()
    return name or "document"


def _is_active_mime(ctype_norm: str) -> bool:
    return ctype_norm in _ACTIVE_MIME_TYPES or ctype_norm.endswith("+xml")


def _apply_download_security(resp, *, filename, ctype, as_download: bool):
    """
    Pose un Content-Disposition / Content-Type sûrs et les en-têtes de sécurité
    sur une réponse de téléchargement de fichier original.
    """
    ctype_norm = (ctype or "").split(";", 1)[0].strip().lower()
    is_active = _is_active_mime(ctype_norm)

    # `inline` réservé aux types explicitement sûrs et jamais à un type actif.
    allow_inline = (not as_download) and (ctype_norm in _INLINE_SAFE_MIME_TYPES) and not is_active
    disposition = "inline" if allow_inline else "attachment"

    # Pour un type actif on neutralise le Content-Type : le navigateur ne doit
    # pas le rendre comme du HTML/SVG exécutable.
    safe_ctype = "application/octet-stream" if is_active else (ctype or "application/octet-stream")

    resp["Content-Type"] = safe_ctype
    resp["Content-Disposition"] = f'{disposition}; filename="{_safe_filename(filename)}"'
    resp["X-Content-Type-Options"] = "nosniff"
    resp["X-Download-Options"] = "noopen"
    # CSP de défense en profondeur : isole tout contenu qui serait quand même
    # rendu (attachment ouvert dans l'onglet, ou type actif). On laisse les
    # types sûrs inline (PDF/image) sans sandbox pour préserver l'aperçu.
    if disposition == "attachment" or is_active:
        resp["Content-Security-Policy"] = "sandbox; default-src 'none'"
    return resp


def _count_chunks_safe(document_id):
    """
    Keep compatibility if count_chunks_for_document signature differs across versions.
    Prefer keyword arg if supported.
    """
    try:
        return count_chunks_for_document(document_id=document_id)
    except TypeError:
        return count_chunks_for_document(document_id)


def _coerce_uuid(v):
    """
    Django url converter <uuid:...> already passes a uuid.UUID.
    But some older routes could still pass str. Handle both.
    """
    try:
        return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))
    except Exception:
        return None


class DocumentListView(APIView):
    """
    GET /api/v1/documents/?rag_space_id=<uuid>&limit=50&offset=0
        &status=all|ready|analyzing|queued|error
        &search=<texte>
        &ordering=filename|file_type|status|created_at (préfixe `-` = décroissant)

    Response:
    {
      "items": [...],
      "total": <int>,          # après recherche ET filtre de statut
      "limit": <int>,
      "offset": <int>,
      "has_next": <bool>,
      "has_previous": <bool>,
      "ordering": "<str>",
      "counts": {"all": .., "ready": .., "analyzing": .., "queued": .., "error": ..}
    }

    `counts` est calculé APRÈS la recherche et AVANT le filtre de statut : les
    onglets doivent rester lisibles quand l'un d'eux est actif, et refléter ce que
    l'utilisateur peut effectivement atteindre quand il cherche. Ils portent sur
    **tout le RagSpace**, jamais sur la page courante — c'est le défaut corrigé.

    Enveloppe `{items,total,limit,offset}` conservée à l'identique : les clés
    ajoutées le sont de façon strictement additive.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        rag_space_id = (request.query_params.get("rag_space_id") or "").strip()
        if not rag_space_id:
            return Response({"detail": "rag_space_id required"}, status=status.HTTP_400_BAD_REQUEST)

        rag_space_uuid = _coerce_uuid(rag_space_id)
        if not rag_space_uuid:
            return Response({"detail": "invalid rag_space_id"}, status=status.HTTP_400_BAD_REQUEST)

        # Wrapper in views_common handles keyword-only signature safely
        from apps.workspaces.services import scope_audit
        scope_audit.emit_ragspace_decision(
            user=request.user, rag_space_id=rag_space_uuid, permission="read",
            endpoint="documents.list", request=request,
        )
        require_rag_space_access(request.user, rag_space_uuid, write=False)

        limit, offset = listing.parse_pagination(
            request.query_params.get("limit"), request.query_params.get("offset")
        )

        status_key = (request.query_params.get("status") or listing.UI_ALL).strip().lower()
        if status_key not in listing.STATUS_CHOICES:
            return Response(
                {"detail": "invalid_status", "allowed": list(listing.STATUS_CHOICES)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        parsed_ordering = listing.parse_ordering(request.query_params.get("ordering"))
        if parsed_ordering is None:
            return Response(
                {"detail": "invalid_ordering", "allowed": list(listing.ORDERING_FIELDS)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        order_field, order_desc = parsed_ordering

        from django.db.models import Count

        from apps.rag_core.models.document import Document

        # Avoid `.only(...)` here: field renames across migrations previously caused 500s.
        qs = Document.objects.filter(rag_space_id=rag_space_uuid)

        search = (request.query_params.get("search") or "").strip()[: listing.MAX_SEARCH_LENGTH]
        search_q = listing.search_filter(search)
        if search_q is not None:
            qs = qs.filter(search_q)

        # Agrégats : UNE requête, sur le périmètre « RagSpace + recherche ».
        counts = listing.status_counts(qs)

        status_q = listing.status_filter(status_key)
        if status_q is not None:
            qs = qs.filter(status_q)

        # Déduit des agrégats : évite un second COUNT(*) pour la même information.
        total = counts["all"] if status_key == listing.UI_ALL else counts[status_key]

        # Le nombre d'extraits était compté document par document (une requête par
        # ligne, soit 200 requêtes par page). Une annotation le ramène à zéro requête
        # supplémentaire.
        qs = qs.annotate(chunks_count=Count("chunks"))
        qs = listing.apply_ordering(qs, order_field, order_desc)

        docs = list(qs[offset : offset + limit])
        items = [
            _serialize_document(d, chunks_count=getattr(d, "chunks_count", None))
            for d in docs
        ]

        return Response({
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_next": offset + len(docs) < total,
            "has_previous": offset > 0,
            "ordering": ("-" if order_desc else "") + order_field,
            "counts": counts,
        })


class DocumentsSearchView(APIView):
    """
    GET /api/v1/documents/search/?rag_space_id=<uuid>&q=Nordexia&limit=200&fts=1

    Returns a "listing" of documents that mention a term (for queries like:
    "liste des documents/articles qui parlent de X").

    Response:
    {
      "query": "Nordexia",
      "fts": true,
      "limit": 200,
      "results": [
        {"document_id": "...", "filename": "...", "hits": 12, "pages": [3, 5, 18]},
        ...
      ]
    }
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        rag_space_id = (request.query_params.get("rag_space_id") or "").strip()
        if not rag_space_id:
            return Response({"detail": "rag_space_id required"}, status=status.HTTP_400_BAD_REQUEST)

        rag_space_uuid = _coerce_uuid(rag_space_id)
        if not rag_space_uuid:
            return Response({"detail": "invalid rag_space_id"}, status=status.HTTP_400_BAD_REQUEST)

        from apps.workspaces.services import scope_audit
        scope_audit.emit_ragspace_decision(
            user=request.user, rag_space_id=rag_space_uuid, permission="read",
            endpoint="documents.search", request=request,
        )
        require_rag_space_access(request.user, rag_space_uuid, write=False)

        q = (request.query_params.get("q") or "").strip()
        if not q:
            return Response({"detail": "q required"}, status=status.HTTP_400_BAD_REQUEST)

        limit = _safe_int(request.query_params.get("limit"), 200)
        limit = max(1, min(limit, 1000))

        use_fts = _boolish((request.query_params.get("fts") or "").strip())
        language = (request.query_params.get("lang") or "simple").strip() or "simple"

        try:
            # You said document_repository.py has been implemented.
            # Keep imports local to avoid import-time issues during deploys.
            from apps.rag_core.repositories.document_repository import (
                list_documents_mentioning_term,
                list_documents_mentioning_term_fts,
            )

            if use_fts:
                hits = list_documents_mentioning_term_fts(
                    space_id=rag_space_uuid,
                    term=q,
                    limit=limit,
                    language=language,
                )
            else:
                hits = list_documents_mentioning_term(
                    space_id=rag_space_uuid,
                    term=q,
                    limit=limit,
                )

            results = [
                {
                    "document_id": str(h.document_id),
                    "filename": h.filename,
                    "hits": int(h.hits),
                    "pages": list(h.pages or []),
                }
                for h in hits
            ]

            return Response(
                {
                    "query": q,
                    "fts": bool(use_fts),
                    "lang": language if use_fts else None,
                    "limit": limit,
                    "results": results,
                }
            )
        except Exception as e:
            logger.exception("documents search failed: %s", e)
            return Response({"detail": "search_failed"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class DocumentDownloadView(APIView):
    """
    GET /api/v1/documents/<document_id>/download/?download=1
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, document_id):
        doc_uuid = _coerce_uuid(document_id)
        if not doc_uuid:
            return Response({"detail": "invalid document_id"}, status=status.HTTP_400_BAD_REQUEST)

        doc = load_accessible_document(request.user, doc_uuid, write=False, request=request)

        # Prefer storage_file (FileField)
        storage_file = getattr(doc, "storage_file", None)
        if storage_file is not None and getattr(storage_file, "name", ""):
            try:
                f = storage_file.open("rb")

                filename = (
                    getattr(doc, "filename", None)
                    or getattr(doc, "original_filename", None)
                    or getattr(doc, "name", None)
                    or storage_file.name.split("/")[-1]
                )

                ctype = (
                    getattr(doc, "content_type", None)
                    or getattr(doc, "mime_type", None)
                    or "application/octet-stream"
                )

                resp = FileResponse(f, content_type=ctype)

                as_download = _boolish((request.query_params.get("download") or "").strip())
                _apply_download_security(
                    resp, filename=filename, ctype=ctype, as_download=as_download
                )
                return resp
            except Exception as e:
                logger.exception("download storage_file failed: %s", e)

        # No on-disk fallback: Document only persists files through `storage_file`
        # (Django storage backend). A raw `open(path, "rb")` on a DB-provided value
        # would be a path-traversal / arbitrary-file-read vector, so it is removed.
        return Response({"detail": "no_file_available"}, status=status.HTTP_404_NOT_FOUND)


class DocumentDetailView(APIView):
    """
    GET /api/v1/documents/<document_id>/
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, document_id):
        doc_uuid = _coerce_uuid(document_id)
        if not doc_uuid:
            return Response({"detail": "invalid document_id"}, status=status.HTTP_400_BAD_REQUEST)

        doc = load_accessible_document(request.user, doc_uuid, write=False, request=request)

        chunks = None
        try:
            chunks = _count_chunks_safe(doc.id)
        except Exception:
            chunks = None

        return Response(_serialize_document(doc, chunks_count=chunks))
