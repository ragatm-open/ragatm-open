# backend/api/v1/core/views.py
"""Exposition HTTP de la façade RagATM_Core (PR 3).

Endpoints :
  GET  /api/v1/core/health/
  POST /api/v1/core/retrieve/
  GET  /api/v1/core/documents/<document_id>/status/?rag_space_id=...

Principes (cf. rapport d'évaluation, frontière App/Core) :
  - exposition PURE de la façade `apps.rag_core.core` : aucun retrieval, ingestion,
    embedding ou génération n'est réécrit ici ;
  - `require_ragspace_access` est appelé AVANT tout appel Core (le contrôle d'accès
    reste de la responsabilité de la couche App, pas de la façade) ;
  - /core/retrieve ne génère JAMAIS de réponse LLM : il renvoie passages, sources et
    métadonnées (la génération finale revient à RagATM App, puis à le serveur IA).
"""
from __future__ import annotations

import logging
import uuid

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.rag_core import core
from apps.workspaces.services.access import require_ragspace_access

from .serializers import CoreRetrieveSerializer

logger = logging.getLogger(__name__)


def _request_id(request) -> str:
    return (
        getattr(request, "request_id", None)
        or request.headers.get("x-request-id")
        or str(uuid.uuid4())
    )


class CoreHealthView(APIView):
    """GET /api/v1/core/health/ — santé du moteur (sans secret)."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(core.health().as_dict(), status=status.HTTP_200_OK)


class CoreRetrieveView(APIView):
    """POST /api/v1/core/retrieve/ — récupération de passages, SANS génération."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = CoreRetrieveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        rag_space_id = serializer.validated_data["rag_space_id"]
        # Audit périmètre AVANT toute recherche vectorielle/FTS/rerank/corpus.
        from apps.workspaces.services import scope_audit
        scope_audit.emit_ragspace_decision(
            user=request.user, rag_space_id=rag_space_id, permission="read",
            endpoint="core.retrieve", request=request,
        )
        # Contrôle d'accès AVANT l'appel Core (lève PermissionDenied -> 403).
        rag_space = require_ragspace_access(
            user=request.user,
            rag_space_id=rag_space_id,
            permission="read",
        )

        question = (serializer.validated_data.get("question") or "").strip()
        rid = _request_id(request)

        try:
            result = core.retrieve(
                rag_space_id=rag_space_id,
                question=question,
                user=request.user,
                rag_space=rag_space,
                request_id=rid,
            )
        except Exception:
            logger.exception(
                "core_retrieve_failed request_id=%s user_id=%s rag_space_id=%s",
                rid,
                getattr(request.user, "id", None),
                rag_space_id,
            )
            return Response(
                {"detail": "internal_error", "request_id": rid},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(result.as_dict(), status=status.HTTP_200_OK)


class CoreDocumentStatusView(APIView):
    """GET /api/v1/core/documents/<document_id>/status/?rag_space_id=..."""

    permission_classes = [IsAuthenticated]

    def get(self, request, document_id):
        rag_space_id = (request.query_params.get("rag_space_id") or "").strip()
        if not rag_space_id:
            return Response(
                {"detail": "rag_space_id_required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Audit périmètre AVANT la résolution Core du document.
        from apps.workspaces.services import scope_audit
        scope_audit.emit_ragspace_decision(
            user=request.user, rag_space_id=rag_space_id, permission="read",
            endpoint="core.document_status", request=request,
        )
        # Contrôle d'accès AVANT l'appel Core (lève PermissionDenied -> 403).
        require_ragspace_access(
            user=request.user,
            rag_space_id=rag_space_id,
            permission="read",
        )

        result = core.document_status(rag_space_id=rag_space_id, document_id=document_id)
        if not result.found:
            return Response({"detail": "not_found"}, status=status.HTTP_404_NOT_FOUND)

        return Response(result.as_dict(), status=status.HTTP_200_OK)
