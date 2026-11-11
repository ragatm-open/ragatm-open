# backend/api/v1/documents/views_preview.py
from __future__ import annotations

"""`GET /api/v1/documents/<id>/tabular-preview/` — aperçu borné d'un classeur.

Endpoint DÉDIÉ, et non une extension de `/download/` : celui-ci sert des octets
bruts avec une sécurisation qui lui est propre (Content-Disposition, CSP sandbox,
neutralisation des types actifs). Y greffer un rendu JSON structuré mêlerait deux
responsabilités et deux modèles de menace.

Journalisation : identifiants, plages, durée, nombre de cellules — **jamais** une
valeur de cellule, jamais le texte d'un chunk.
"""

import logging
import time

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.rag_core.services.preview import errors as E
from apps.rag_core.services.preview import service as preview_service

from .views_common import load_accessible_document

logger = logging.getLogger(__name__)


def _coerce_uuid(v):
    from uuid import UUID

    try:
        return v if hasattr(v, "hex") else UUID(str(v))
    except Exception:
        return None


def _coerce_int(v):
    if v in (None, ""):
        return None
    try:
        return int(str(v).strip())
    except Exception:
        raise E.PreviewInvalidRequest(
            E.ERR_CONTEXT_LIMIT_EXCEEDED, "paramètre numérique invalide"
        )


#: Chaque code de refus a un statut HTTP, et un seul. Les codes voyagent jusqu'au
#: frontend, qui rend un message dédié — en particulier pour `chunk_stale`, où
#: retomber silencieusement sur la première feuille serait le pire comportement.
_STATUS_BY_CODE = {
    E.ERR_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    E.ERR_NO_FILE: status.HTTP_404_NOT_FOUND,
    E.ERR_CHUNK_DOCUMENT_MISMATCH: status.HTTP_404_NOT_FOUND,
    E.ERR_SHEET_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    E.ERR_CHUNK_STALE: status.HTTP_409_CONFLICT,
    E.ERR_DOCUMENT_VERSION_MISMATCH: status.HTTP_409_CONFLICT,
    E.ERR_UNSUPPORTED_FORMAT: status.HTTP_422_UNPROCESSABLE_ENTITY,
    E.ERR_CHUNK_NOT_TABULAR: status.HTTP_422_UNPROCESSABLE_ENTITY,
    E.ERR_SHEET_NOT_PREVIEWABLE: status.HTTP_422_UNPROCESSABLE_ENTITY,
    E.ERR_WORKBOOK_UNREADABLE: status.HTTP_422_UNPROCESSABLE_ENTITY,
    E.ERR_CHUNK_LOCATION_INVALID: status.HTTP_400_BAD_REQUEST,
    E.ERR_SHEET_REFERENCE_CONFLICT: status.HTTP_400_BAD_REQUEST,
    E.ERR_INVALID_CELL_RANGE: status.HTTP_400_BAD_REQUEST,
    E.ERR_RANGE_OUT_OF_BOUNDS: status.HTTP_400_BAD_REQUEST,
    E.ERR_CONTEXT_LIMIT_EXCEEDED: status.HTTP_400_BAD_REQUEST,
    E.ERR_PREVIEW_TIMEOUT: status.HTTP_504_GATEWAY_TIMEOUT,
}


class DocumentTabularPreviewView(APIView):
    """GET /api/v1/documents/<document_id>/tabular-preview/"""

    permission_classes = [IsAuthenticated]

    def get(self, request, document_id):
        started = time.perf_counter()

        doc_uuid = _coerce_uuid(document_id)
        if not doc_uuid:
            return Response(
                {"detail": "invalid document_id", "reason_code": E.ERR_NOT_FOUND},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # ⚠️ Permission D'ABORD : avant toute ouverture de fichier, toute lecture de
        # cache, tout listage de feuilles, toute résolution de chunk. Lève NotFound
        # (404 anti-énumération) ou PermissionDenied — gérées par le handler DRF.
        doc = load_accessible_document(request.user, doc_uuid, write=False, request=request)

        q = request.query_params
        try:
            payload = preview_service.build_preview_cached(
                document=doc,
                chunk_id=(q.get("chunk_id") or "").strip() or None,
                sheet_index=_coerce_int(q.get("sheet_index")),
                sheet_name=(q.get("sheet_name") or "").strip() or None,
                cell_range=(q.get("range") or "").strip() or None,
                context_rows=_coerce_int(q.get("context_rows")),
                context_columns=_coerce_int(q.get("context_columns")),
                expected_sha256=(q.get("sha256") or "").strip() or None,
            )
        except E.PreviewError as exc:
            http = _STATUS_BY_CODE.get(exc.code, status.HTTP_400_BAD_REQUEST)
            logger.info(
                "tabular_preview refus document=%s code=%s duree_ms=%d",
                doc.id, exc.code, int((time.perf_counter() - started) * 1000),
            )
            return Response(
                {"reason_code": exc.code, "detail": exc.message}, status=http
            )
        except Exception:
            # Un défaut inattendu ne doit pas fuiter d'interne — et surtout pas
            # laisser le frontend sur une zone blanche sans code exploitable.
            logger.exception("tabular_preview: défaut inattendu document=%s", doc.id)
            return Response(
                {"reason_code": "internal_error", "detail": "erreur interne"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        n_cells = sum(len(r["cells"]) for r in payload["rows"])
        logger.info(
            "tabular_preview ok document=%s sha256=%s feuille=%s plage=%s "
            "cellules=%d tronque=%s duree_ms=%d",
            doc.id,
            (payload["document"]["sha256"] or "")[:12],
            payload["viewport"]["sheet_index"],
            payload["viewport"]["display_range"],
            n_cells,
            payload["truncation"]["truncated"],
            int((time.perf_counter() - started) * 1000),
        )
        return Response(payload)
