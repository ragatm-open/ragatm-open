from __future__ import annotations

from django.db import transaction
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.workspaces.models import Membership, Workspace
from apps.rag_core.models import RagSpace
from apps.workspaces.services.ragspace_creation import (
    RagSpaceCreationError,
    create_ragspace,
    resolve_owner_scope_for_ragspace,
)
from apps.accounts.models import get_preferred_locale, get_preferred_answer_language

from .serializers import BootstrapResponseSerializer


class BootstrapView(APIView):
    """Create a minimal working environment for a new user.

    Idempotent:
      - If the user already has at least one workspace membership, we return the
        earliest (by workspace.created_at) workspace and its earliest ragspace.
      - If that workspace has no ragspace yet, we create a default one.

    Response shape is stable and meant to be called automatically after login.
    """
    permission_classes = [IsAuthenticated]

    DEFAULT_WORKSPACE_NAME = "Initial workspace"
    DEFAULT_RAGSPACE_NAME = "Default"

    def post(self, request):
        user = request.user
        created_any = False

        with transaction.atomic():
            # `workspace__is_active=True` : un workspace DÉSACTIVÉ n'est pas un
            # « environnement de travail minimal ». Le garde d'exécution des tâches
            # y refuse tout traitement (`task_guard.WORKSPACE_DISABLED`) : y créer un
            # espace produirait un objet mort.
            candidates = list(
                Membership.objects.select_related("workspace")
                .filter(user=user, workspace__is_active=True)
                .order_by("workspace__created_at", "id")
            )

            # 1) Un espace existe déjà quelque part -> on le rend, sans rien créer.
            #    Aucun périmètre n'est requis dans ce cas : le contrat historique de
            #    l'endpoint est préservé à l'identique.
            membership = None
            rs = None
            for m in candidates:
                existing = (
                    RagSpace.objects.filter(workspace=m.workspace)
                    .order_by("created_at", "id")
                    .first()
                )
                if existing is not None:
                    membership, rs = m, existing
                    break

            # 2) Rien à rendre : il faut CRÉER, donc disposer d'un périmètre owner
            #    déterminable. On retient le premier workspace où c'est le cas.
            #    Un workspace sans owner ne peut pas en fournir : y créer un espace
            #    donnerait un objet que personne ne pourrait ouvrir sous enforcement.
            #    Plutôt que d'élargir des droits (promouvoir l'utilisateur owner),
            #    on lui donne un workspace neuf dont il est légitimement owner —
            #    c'est précisément la raison d'être de cet endpoint.
            if rs is None:
                for m in candidates:
                    try:
                        resolve_owner_scope_for_ragspace(
                            workspace=m.workspace, actor=user, actor_membership=m
                        )
                    except RagSpaceCreationError:
                        continue
                    membership = m
                    break

                if membership is None:
                    ws = Workspace.objects.create(name=self.DEFAULT_WORKSPACE_NAME)
                    membership = Membership.objects.create(
                        workspace=ws, user=user, role=Membership.ROLE_OWNER
                    )
                # Périmètre écrit dans CETTE transaction : à aucun instant l'espace
                # n'existe sans lui.
                rs = create_ragspace(
                    workspace=membership.workspace,
                    name=self.DEFAULT_RAGSPACE_NAME,
                    actor=user,
                    actor_membership=membership,
                )
                created_any = True

            ws = membership.workspace
            role = membership.role

        payload = {
            "created": created_any,
            "workspace": {"id": ws.id, "name": ws.name, "role": role},
            "ragspace": {"id": rs.id, "name": rs.name},
            "preferred_locale": get_preferred_locale(user),
            "preferred_answer_language": get_preferred_answer_language(user),
        }
        return Response(BootstrapResponseSerializer(payload).data)
