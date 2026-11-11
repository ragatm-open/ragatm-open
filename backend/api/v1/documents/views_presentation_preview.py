# backend/api/v1/documents/views_presentation_preview.py
from __future__ import annotations

"""`GET /api/v1/documents/<id>/presentation-preview/` — aperçu d'une présentation.

Endpoint DÉDIÉ, distinct de `tabular-preview` et de `document-preview` : une
présentation est une SUITE DE DIAPOSITIVES, chacune portant ses propres blocs.
Un contrat générique devrait unir grille, blocs et diapositives, et le client
devrait deviner laquelle il reçoit. Ce qui est partagé — permissions, bornes,
cache, codes de refus — l'est au niveau du **service**, pas de la sortie.

Journalisation : identifiants, diapositive, bornes, durée, nombre de blocs.
**Jamais** le texte d'une diapositive, jamais celui d'un chunk.
"""

import logging
import time

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.rag_core.services.preview import errors as E
from apps.rag_core.services.preview import presentation_service

from .views_common import load_accessible_document

logger = logging.getLogger(__name__)


def _coerce_uuid(v):
    from uuid import UUID

    try:
        return v if hasattr(v, "hex") else UUID(str(v))
    except Exception:
        return None


def _coerce_int(v, *, code=E.ERR_CONTEXT_LIMIT_EXCEEDED):
    if v in (None, ""):
        return None
    try:
        return int(str(v).strip())
    except Exception:
        raise E.PreviewInvalidRequest(code, "paramètre numérique invalide")


#: Un code de refus, un statut. `notes_configuration_mismatch` et `chunk_stale`
#: méritent leur message côté client : afficher une diapositive approchante
#: serait pire que ne rien afficher.
_STATUS_BY_CODE = {
    E.ERR_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    E.ERR_NO_FILE: status.HTTP_404_NOT_FOUND,
    E.ERR_CHUNK_DOCUMENT_MISMATCH: status.HTTP_404_NOT_FOUND,
    E.ERR_SLIDE_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    E.ERR_CHUNK_STALE: status.HTTP_409_CONFLICT,
    E.ERR_DOCUMENT_VERSION_MISMATCH: status.HTTP_409_CONFLICT,
    E.ERR_NOTES_CONFIGURATION_MISMATCH: status.HTTP_409_CONFLICT,
    E.ERR_UNSUPPORTED_FORMAT: status.HTTP_422_UNPROCESSABLE_ENTITY,
    E.ERR_SLIDE_NOT_PREVIEWABLE: status.HTTP_422_UNPROCESSABLE_ENTITY,
    E.ERR_PRESENTATION_UNREADABLE: status.HTTP_422_UNPROCESSABLE_ENTITY,
    E.ERR_ENCRYPTED_PRESENTATION: status.HTTP_422_UNPROCESSABLE_ENTITY,
    E.ERR_PREVIEW_UNAVAILABLE: status.HTTP_422_UNPROCESSABLE_ENTITY,
    E.ERR_INVALID_CHUNK_OFFSETS: status.HTTP_422_UNPROCESSABLE_ENTITY,
    E.ERR_PRESENTATION_TOO_LARGE: status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
    E.ERR_INVALID_SLIDE_RANGE: status.HTTP_400_BAD_REQUEST,
    E.ERR_CONTEXT_LIMIT_EXCEEDED: status.HTTP_400_BAD_REQUEST,
    E.ERR_PREVIEW_TIMEOUT: status.HTTP_504_GATEWAY_TIMEOUT,
}


class DocumentPresentationPreviewView(APIView):
    """GET /api/v1/documents/<document_id>/presentation-preview/"""

    permission_classes = [IsAuthenticated]

    def get(self, request, document_id):
        started = time.perf_counter()

        doc_uuid = _coerce_uuid(document_id)
        if not doc_uuid:
            return Response(
                {"detail": "invalid document_id", "reason_code": E.ERR_NOT_FOUND},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # ⚠️ Permission D'ABORD : avant lecture de fichier, de cache, de chunk.
        doc = load_accessible_document(request.user, doc_uuid, write=False, request=request)

        q = request.query_params
        try:
            payload = presentation_service.build_presentation_preview_cached(
                document=doc,
                chunk_id=(q.get("chunk_id") or "").strip() or None,
                slide_number=_coerce_int(
                    q.get("slide_number"), code=E.ERR_INVALID_SLIDE_RANGE
                ),
                context_before=_coerce_int(q.get("context_before")),
                context_after=_coerce_int(q.get("context_after")),
                expected_sha256=(q.get("sha256") or "").strip() or None,
            )
        except E.PreviewError as exc:
            http = _STATUS_BY_CODE.get(exc.code, status.HTTP_400_BAD_REQUEST)
            logger.info(
                "presentation_preview refus document=%s code=%s duree_ms=%d",
                doc.id, exc.code, int((time.perf_counter() - started) * 1000),
            )
            return Response({"reason_code": exc.code, "detail": exc.message}, status=http)
        except Exception:
            logger.exception("presentation_preview: défaut inattendu document=%s", doc.id)
            return Response(
                {"reason_code": "internal_error", "detail": "erreur interne"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        n_blocks = sum(
            len(s["blocks"]) + len(s["speaker_notes"]) for s in payload["slides"]
        )
        logger.info(
            "presentation_preview ok document=%s sha256=%s slide=%s "
            "fenetre=%s-%s blocs=%d tronque=%s duree_ms=%d",
            doc.id,
            (payload["document"]["sha256"] or "")[:12],
            payload["viewport"]["active_slide"],
            payload["viewport"]["first_slide"],
            payload["viewport"]["last_slide"],
            n_blocks,
            payload["truncation"]["truncated"],
            int((time.perf_counter() - started) * 1000),
        )
        return Response(payload)
