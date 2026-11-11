"""API des analyses globales de corpus.

Règle absolue : la possession de l'UUID d'un job ne suffit JAMAIS. L'accès au
RagSpace est revérifié à CHAQUE opération — création, consultation, annulation.
Un utilisateur qui perd l'accès à l'espace perd l'accès aux résultats déjà
produits.
"""
from __future__ import annotations

import logging

from django.core.exceptions import PermissionDenied
from rest_framework.exceptions import NotFound
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.rag_core.analysis.concurrency import ConcurrencyLimitReached
from apps.rag_core.analysis.global_search import GlobalSearchError
from apps.rag_core.analysis.progress import request_cancel
from apps.rag_core.analysis.service import (
    PreflightLimitExceeded,
    UnknownAnalysisType,
    create_analysis_job,
)
from apps.rag_core.models.corpus_analysis_job import CorpusAnalysisJob
from apps.workspaces.models import Membership
from apps.workspaces.services.access import require_ragspace_access

from .serializers import AnalysisCreateSerializer, serialize_job

logger = logging.getLogger(__name__)

#: Corps de réponse d'un job introuvable OU inaccessible. Identique dans les deux
#: cas : un job d'un espace auquel on n'a pas accès doit être indistinguable d'un
#: job inexistant (anti-énumération, convention du dépôt).
NOT_FOUND = {"detail": "Not found"}


def _load_accessible_job(*, user, analysis_id) -> CorpusAnalysisJob | None:
    """Charge un job SI l'utilisateur a toujours accès à son RagSpace.

    Renvoie ``None`` aussi bien pour un job inexistant que pour un job
    inaccessible : aucun oracle d'existence.
    """
    job = (
        CorpusAnalysisJob.objects.select_related("rag_space")
        .filter(pk=analysis_id)
        .first()
    )
    if job is None:
        return None
    from apps.workspaces.services import scope_audit
    scope_audit.emit_ragspace_decision(
        user=user, rag_space_id=job.rag_space_id, permission="read",
        endpoint="analyses.access",
    )
    try:
        # Revérifié À CHAQUE ACCÈS, jamais seulement à la création.
        require_ragspace_access(
            user=user, rag_space_id=job.rag_space_id, permission="read"
        )
    except (PermissionDenied, NotFound):
        return None
    return job


class RagSpaceAnalysisCreateView(APIView):
    """POST /api/v1/rag-spaces/<uuid>/analyses/"""

    permission_classes = [IsAuthenticated]

    def post(self, request, rag_space_id):
        from apps.workspaces.services import scope_audit
        scope_audit.emit_ragspace_decision(
            user=request.user, rag_space_id=rag_space_id, permission="read",
            endpoint="analyses.create", request=request,
        )
        rag_space = require_ragspace_access(
            user=request.user, rag_space_id=rag_space_id, permission="read"
        )

        serializer = AnalysisCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"detail": "invalid_request", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            outcome = create_analysis_job(
                user=request.user,
                rag_space=rag_space,
                analysis_type=serializer.validated_data["analysis_type"],
                parameters=serializer.validated_data.get("parameters") or {},
                request_id=serializer.validated_data.get("request_id") or "",
            )
        except UnknownAnalysisType:
            return Response(
                {"detail": "unknown_analysis_type"}, status=status.HTTP_400_BAD_REQUEST
            )
        except PreflightLimitExceeded as e:
            return Response(
                {"detail": "preflight_limit_exceeded", "preflight": e.preflight},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except ConcurrencyLimitReached as e:
            # On ne crée PAS un job `pending` qu'on ne pourra pas honorer.
            return Response(
                {
                    "detail": "analytics_concurrency_limit",
                    "active": e.active,
                    "limit": e.limit,
                },
                status=status.HTTP_409_CONFLICT,
            )
        except GlobalSearchError as e:
            # Sujet requis, etc. — code stable, message sûr.
            return Response({"detail": e.code}, status=status.HTTP_400_BAD_REQUEST)

        payload = serialize_job(outcome.job)
        payload["reused"] = outcome.reused
        payload["joined_active"] = outcome.joined_active

        # 202 : un traitement a été mis en file. 200 : rien à faire (résultat déjà
        # disponible, ou analyse identique déjà en cours / déjà demandée).
        code = status.HTTP_202_ACCEPTED if outcome.created else status.HTTP_200_OK
        return Response(payload, status=code)


class AnalysisDetailView(APIView):
    """GET /api/v1/analyses/<uuid>/"""

    permission_classes = [IsAuthenticated]

    def get(self, request, analysis_id):
        job = _load_accessible_job(user=request.user, analysis_id=analysis_id)
        if job is None:
            return Response(NOT_FOUND, status=status.HTTP_404_NOT_FOUND)
        return Response(serialize_job(job), status=status.HTTP_200_OK)


class AnalysisCancelView(APIView):
    """POST /api/v1/analyses/<uuid>/cancel/

    Autorisé au **demandeur** du job, ainsi qu'aux owner/admin du workspace
    propriétaire de l'espace (ils portent la responsabilité du coût), et au
    superuser. Un simple membre lecteur ne peut pas annuler l'analyse d'un autre.
    """

    permission_classes = [IsAuthenticated]

    def _may_cancel(self, *, user, job) -> bool:
        if job.requested_by_id == user.id:
            return True
        if getattr(user, "is_superuser", False):
            return True
        membership = Membership.objects.filter(
            workspace_id=job.rag_space.workspace_id, user=user
        ).first()
        return bool(
            membership
            and membership.role in (Membership.ROLE_OWNER, Membership.ROLE_ADMIN)
        )

    def post(self, request, analysis_id):
        job = _load_accessible_job(user=request.user, analysis_id=analysis_id)
        if job is None:
            return Response(NOT_FOUND, status=status.HTTP_404_NOT_FOUND)

        if not self._may_cancel(user=request.user, job=job):
            return Response(
                {"detail": "Not allowed to cancel this analysis"},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Idempotent : un job terminal n'est jamais modifié.
        request_cancel(job)
        job.refresh_from_db()
        return Response(serialize_job(job), status=status.HTTP_202_ACCEPTED)


class ThreadAnalysesView(APIView):
    """GET /api/v1/chat/threads/<uuid>/analyses/

    Reprise après rechargement : le frontend retrouve ici les analyses lancées
    depuis ce fil. Le lien est DURABLE (``CorpusAnalysisJob.chat_thread``) — jamais
    un UUID parsé dans du Markdown, jamais « le dernier job de l'utilisateur ».
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, thread_id):
        from apps.chat.models import ChatThread

        thread = ChatThread.objects.filter(id=thread_id, user=request.user).first()
        if thread is None:
            return Response(NOT_FOUND, status=status.HTTP_404_NOT_FOUND)

        # Permission revérifiée sur l'espace, pas seulement sur le fil.
        from apps.workspaces.services import scope_audit
        scope_audit.emit_ragspace_decision(
            user=request.user, rag_space_id=thread.rag_space_id, permission="read",
            endpoint="analyses.thread", request=request,
        )
        try:
            require_ragspace_access(
                user=request.user, rag_space_id=thread.rag_space_id, permission="read"
            )
        except (PermissionDenied, NotFound):
            return Response(NOT_FOUND, status=status.HTTP_404_NOT_FOUND)

        # Deux chemins mènent au même fil, et il faut les deux :
        #  - `chat_thread` : le fil où le job a été CRÉÉ ;
        #  - les `analysis_id` des messages : un job réutilisé par idempotence reste
        #    rattaché à son fil d'origine, mais il est bien cité ici. Sans cette
        #    union, rejouer la même question dans un autre fil y perdrait la carte
        #    au rechargement.
        from django.db.models import Q

        from apps.chat.models import ChatMessage

        cited = (
            ChatMessage.objects.filter(thread=thread, analysis_id__isnull=False)
            .values_list("analysis_id", flat=True)
        )
        jobs = (
            CorpusAnalysisJob.objects.filter(
                Q(chat_thread=thread) | Q(id__in=list(cited)),
                requested_by=request.user,
            )
            .select_related("rag_space")
            .order_by("-created_at")[:20]
        )
        return Response(
            {"analyses": [serialize_job(j) for j in jobs]}, status=status.HTTP_200_OK
        )
