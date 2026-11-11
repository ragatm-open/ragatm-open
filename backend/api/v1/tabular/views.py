"""API du moteur tabulaire déterministe.

Une seule route, `POST /api/v1/tabular/query/`. Elle rend TOUJOURS un statut explicite :
`ok`, `clarification_required`, `unsupported`, `forbidden`, `limit_exceeded` ou `error`.
Il n'existe aucun chemin par lequel une question analytique se termine en réponse
vaguement plausible — c'est la propriété que cette API existe pour garantir.

Quand `TABULAR_QUERY_ENABLED=0` (défaut), la route répond `unsupported` avec le code
`tabular_query_disabled` : le comportement historique de refus est conservé tel quel.
"""
from __future__ import annotations

import logging
import time

from rest_framework import status
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.rag_core.tabular import capacity as CAP
from apps.rag_core.tabular import config as CFG
from apps.rag_core.tabular import errors as E
from apps.rag_core.tabular import observability as OBS
from apps.rag_core.tabular import service as TSVC

logger = logging.getLogger(__name__)

#: Statut tabulaire -> code HTTP. Une clarification n'est PAS une erreur du client :
#: la question était légitime, il manque une information. D'où 200.
#: `tabular_capacity_exceeded` sort en 503 avec `Retry-After` : c'est une saturation
#: TEMPORAIRE, pas une erreur du client. Un 4xx inviterait à ne pas réessayer.
CAPACITY_RETRY_AFTER_SECONDS = 2

_HTTP_BY_STATUS = {
    "ok": status.HTTP_200_OK,
    "clarification_required": status.HTTP_200_OK,
    "unsupported": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "limit_exceeded": status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
    "forbidden": status.HTTP_403_FORBIDDEN,
    "error": status.HTTP_400_BAD_REQUEST,
}


class TabularQueryView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        payload = request.data or {}
        rag_space_id = payload.get("rag_space_id")
        question = (payload.get("question") or "").strip()
        document_ids = payload.get("document_ids") or []
        plan = payload.get("plan")

        if not rag_space_id:
            return Response({"status": "error", "code": "missing_rag_space_id",
                             "message": "`rag_space_id` est requis."},
                            status=status.HTTP_400_BAD_REQUEST)
        if not question and not plan:
            return Response({"status": "error", "code": "missing_question",
                             "message": "`question` ou `plan` est requis."},
                            status=status.HTTP_400_BAD_REQUEST)

        started = time.perf_counter()
        try:
            if plan is not None:
                result = TSVC.execute_validated_plan(
                    user=request.user, rag_space_id=rag_space_id, plan_payload=plan)
            else:
                result = TSVC.answer_question(
                    user=request.user, rag_space_id=rag_space_id,
                    question=question,
                    document_ids=[d for d in document_ids] or None)
            OBS.record_query(
                status=result.status, operation=result.operation,
                duration_s=time.perf_counter() - started,
                rows_scanned=result.evidence.get("table_row_count", 0),
                result_rows=result.row_count,
                plan_hash=result.evidence.get("plan_hash", ""),
                document_id=str(result.evidence.get("document_id", "")),
                rag_space_id=str(rag_space_id),
            )
            return Response(result.to_dict(), status=status.HTTP_200_OK)

        except E.TabularError as exc:
            # Toute erreur du moteur porte un code stable et sort telle quelle. Aucune
            # n'est convertie en résultat vide : « 0 » et « je ne sais pas » sont deux
            # réponses différentes, et les confondre est le défaut le plus grave ici.
            body = exc.to_dict()
            http = _HTTP_BY_STATUS.get(exc.status, status.HTTP_400_BAD_REQUEST)
            entetes = {}
            if exc.code == E.ERR_TABULAR_CAPACITY_EXCEEDED:
                http = status.HTTP_503_SERVICE_UNAVAILABLE
                entetes["Retry-After"] = str(CAPACITY_RETRY_AFTER_SECONDS)
            OBS.record_query(
                status=exc.status, code=exc.code,
                duration_s=time.perf_counter() - started,
                rag_space_id=str(rag_space_id),
            )
            return Response(body, status=http, headers=entetes or None)

        except Exception:
            # Un défaut inattendu ne doit jamais ressembler à une réponse. On journalise
            # avec la trace et on rend une erreur explicite, sans détail interne.
            logger.exception("tabular: échec inattendu")
            OBS.record_query(status="error", code="internal_error",
                             duration_s=time.perf_counter() - started,
                             rag_space_id=str(rag_space_id))
            return Response(
                {"status": "error", "code": "internal_error",
                 "message": "Le moteur tabulaire n'a pas pu traiter cette demande."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class TabularDeploymentView(APIView):
    """Diagnostic de capacité de DÉPLOIEMENT — réservé au personnel.

    Restreint à `IsAdminUser` : la capacité, le nombre de workers et la mémoire
    disponible renseignent sur le dimensionnement de l'infrastructure. Ce n'est pas un
    secret, mais ce n'est pas une information d'utilisateur final non plus.
    """

    permission_classes = [IsAdminUser]

    def get(self, request):
        from apps.rag_core.tabular import deployment as DEP

        etat = DEP.describe()
        # 200 même si la configuration est invalide : c'est un DIAGNOSTIC. Renvoyer
        # une erreur HTTP empêcherait justement de lire ce qui ne va pas.
        return Response(etat, status=status.HTTP_200_OK)


class TabularCapabilitiesView(APIView):
    """Ce que le moteur sait faire — et ce qu'il refuse. Utile au front et aux tests."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.rag_core.tabular import plan_schema as PS

        return Response({
            "enabled": CFG.query_enabled(),
            "llm_planner_enabled": CFG.llm_planner_enabled(),
            "narration_enabled": CFG.narration_enabled(),
            "plan_schema_version": PS.PLAN_SCHEMA_VERSION,
            "operations": sorted(PS.Operation.__args__),
            "deferred_operations": sorted(PS.DEFERRED_OPERATIONS),
            "filter_operators": sorted(PS.FilterOperator.__args__),
            "formats": ["xlsx", "csv"],
            # Portée explicite : la limite est PAR PROCESSUS. Exposer la limite sans
            # sa portée laisserait croire à une borne globale.
            "capacity": CAP.effective_capacity(),
        }, status=status.HTTP_200_OK)
