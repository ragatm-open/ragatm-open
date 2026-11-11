from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth import get_user_model
from django.db import transaction

from apps.workspaces.models import Workspace, Membership
from apps.workspaces.services.ragspace_creation import create_ragspace
from apps.rag_core.models import RagSpace


class Command(BaseCommand):
    help = "Bootstrap a Workspace + OWNER membership + one or more RagSpaces."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True, help="Owner user email (must exist).")
        parser.add_argument("--workspace", required=True, help="Workspace name to create.")
        parser.add_argument(
            "--ragspace",
            action="append",
            default=[],
            help="RagSpace name to create (repeatable). Example: --ragspace 'Initial RAG'",
        )
        parser.add_argument(
            "--default-ragspace",
            default="Initial RAG",
            help="Used when no --ragspace is provided.",
        )

    @transaction.atomic
    def handle(self, *args, **opts):
        email: str = opts["email"].strip().lower()
        ws_name: str = opts["workspace"].strip()
        ragspaces: list[str] = [s.strip() for s in (opts["ragspace"] or []) if s.strip()]
        if not ragspaces:
            ragspaces = [opts["default_ragspace"].strip()]

        User = get_user_model()
        user = User.objects.filter(email=email).first()
        if not user:
            raise CommandError(f"User not found: {email} (create it first as superadmin)")

        # Workspace + membership owner + espaces : TOUT ou RIEN. Sans transaction,
        # une erreur au milieu laissait un workspace sans owner, donc un périmètre
        # indéterminable pour tout espace créé ensuite.
        with transaction.atomic():
            ws = Workspace.objects.create(name=ws_name)

            owner = Membership.objects.create(
                workspace=ws,
                user=user,
                role=Membership.ROLE_OWNER,
            )

            # `create_ragspace` dérive le périmètre du membership owner qu'on vient
            # de créer et l'écrit dans la même transaction. Un `RagSpace.objects
            # .create()` direct laissait `owner_scope=None` : l'espace apparaissait
            # dans la liste mais répondait 404 sous enforcement owner-scope.
            created_rs: list[RagSpace] = [
                create_ragspace(
                    workspace=ws, name=name, actor=user, actor_membership=owner,
                )
                for name in ragspaces
            ]

        self.stdout.write(self.style.SUCCESS("Bootstrap complete"))
        self.stdout.write(f"Workspace: {ws.id}  name={ws.name}")
        for rs in created_rs:
            self.stdout.write(f"RagSpace:  {rs.id}  name={rs.name}")
