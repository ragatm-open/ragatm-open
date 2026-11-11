from __future__ import annotations

import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.core.mail import send_mail
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.workspaces.models import Membership
from apps.rag_core.models import RagSpace
from apps.workspaces.services.access import require_workspace_access, require_workspace_admin
from .serializers import (
    WorkspaceListItemSerializer,
    WorkspaceMemberSerializer,
    WorkspaceMemberAddSerializer,
    WorkspaceMemberPatchSerializer,
    WorkspaceRagSpaceCreateSerializer,
)

logger = logging.getLogger(__name__)
token_generator = PasswordResetTokenGenerator()


def _frontend_origin(request) -> str:
    """
    Origine frontend utilisée pour fabriquer un reset_link.
    Priorité:
      1) settings.FRONTEND_APP_ORIGIN
      2) Header Origin
      3) Host courant (https si request.is_secure)
    """
    origin = getattr(settings, "FRONTEND_APP_ORIGIN", "") or request.headers.get("origin", "")
    if origin:
        return origin.rstrip("/")
    scheme = "https" if request.is_secure() else "http"
    return f"{scheme}://{request.get_host()}"


def _make_reset_link(request, user) -> str:
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = token_generator.make_token(user)
    return f"{_frontend_origin(request)}/reset-password?uid={uid}&token={token}"


def _count_owners(workspace_id: int) -> int:
    return Membership.objects.filter(workspace_id=workspace_id, role=Membership.ROLE_OWNER).count()


def _get_requester_membership(user, workspace_id: int):
    return Membership.objects.filter(user=user, workspace_id=workspace_id).first()


def _send_invite_email(
    *,
    to_email: str,
    workspace_name: str,
    invited_by_email: str,
    reset_link: str,
) -> tuple[bool, str | None]:
    """
    Envoi best-effort : ne doit jamais faire échouer l'API.
    MAIS: on ne "fail_silently" plus => on capture l'erreur et on la remonte en payload (utile en dev).
    """
    to_email = (to_email or "").strip()
    if not to_email:
        return False, "missing_to_email"

    email_backend = getattr(settings, "EMAIL_BACKEND", "") or ""
    host_user = getattr(settings, "EMAIL_HOST_USER", "") or ""
    host_password = getattr(settings, "EMAIL_HOST_PASSWORD", "") or ""

    # Pré-check typique IONOS : si SMTP configuré mais pas de password, ça ne partira pas.
    if "smtp" in email_backend.lower() and host_user and not host_password:
        return False, "missing_EMAIL_HOST_PASSWORD"

    subject_prefix = getattr(settings, "EMAIL_SUBJECT_PREFIX", "") or ""
    subject = f"{subject_prefix}Invitation à rejoindre {workspace_name}".strip()

    body = (
        "Bonjour,\n\n"
        f"Vous avez été invité à rejoindre l’espace de travail « {workspace_name} » sur RAG-ATM.\n\n"
        "Pour activer votre accès et définir votre mot de passe, cliquez sur ce lien :\n"
        f"{reset_link}\n\n"
        f"Invité par : {invited_by_email}\n\n"
        "Si vous n’êtes pas concerné, vous pouvez ignorer cet email.\n"
    )

    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", None) or getattr(settings, "SERVER_EMAIL", None)
    if not from_email:
        from_email = "no-reply@localhost"

    try:
        # fail_silently=False => on voit les vraies erreurs (auth, connexion, TLS, etc.)
        send_mail(
            subject=subject,
            message=body,
            from_email=from_email,
            recipient_list=[to_email],
            fail_silently=False,
        )
        return True, None
    except Exception as e:
        logger.exception("Invite email failed to=%s workspace=%s", to_email, workspace_name)
        return False, f"{type(e).__name__}: {e}"


class WorkspaceListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # Sélecteur MÉTIER : workspaces ACTIFS dont l'utilisateur est membre. Un
        # workspace désactivé (is_active=False) est exclu de la navigation métier
        # (il reste géré via la console plateforme). `?include_inactive=1` lève le
        # filtre (usage admin). Aucun filtre sur is_superuser : membership requis.
        from apps.workspaces.services.access import effective_owner_scope_pk

        include_inactive = str(request.query_params.get("include_inactive") or "").lower() in ("1", "true", "yes")
        memberships = (
            Membership.objects.select_related("workspace")
            .filter(user=request.user)
            .order_by("workspace__name", "workspace_id")
        )
        if not include_inactive:
            memberships = memberships.filter(workspace__is_active=True)
        items = [
            {
                "id": m.workspace.id,
                "name": m.workspace.name,
                "role": m.role,
                "workspace_is_active": m.workspace.is_active,
                "membership_id": m.id,
                "effective_owner_scope_id": effective_owner_scope_pk(m),
            }
            for m in memberships
        ]
        return Response(WorkspaceListItemSerializer(items, many=True).data)


class WorkspaceQuotaView(APIView):
    """
    GET /api/v1/workspaces/<id>/quota/  (OWNER/ADMIN)

    Consultation LECTURE SEULE des quotas documentaires du workspace et de leur
    consommation. Réservée aux owners/admins (``require_workspace_admin``) : un
    membre/viewer n'obtient pas plus que ce que permet déjà le modèle
    d'autorisation. La MODIFICATION est exclusivement superuser (console
    plateforme, PATCH /api/v1/platform/workspaces/<id>/quota/) — jamais exposée ici.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id: int):
        ws = require_workspace_admin(user=request.user, workspace_id=workspace_id)
        from apps.workspaces.services import quota as quota_service

        return Response(quota_service.get_usage(ws), status=200)


class WorkspaceMembersView(APIView):
    """
    GET  /api/v1/workspaces/<id>/members/   (OWNER/ADMIN)
    POST /api/v1/workspaces/<id>/members/   (OWNER/ADMIN) invite/add by email (create user if missing)
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id: int):
        require_workspace_admin(user=request.user, workspace_id=workspace_id)

        qs = (
            Membership.objects.select_related("user")
            .filter(workspace_id=workspace_id)
            .order_by("user__email")
        )

        items = [
            {
                "id": m.id,
                "user_id": m.user_id,
                "email": getattr(m.user, "email", "") or "",
                "role": m.role,
                "created_at": m.created_at,
            }
            for m in qs
        ]
        return Response(WorkspaceMemberSerializer(items, many=True).data)

    def post(self, request, workspace_id: int):
        """
        [DÉPRÉCIÉ] Ancien flux d'invitation par reset-link.

        Sous ``WORKSPACE_INVITATIONS_V2_ENABLED`` (défaut), ce chemin est neutralisé :
        aucun compte n'est créé, aucun membership n'est ajouté, aucun reset_link
        n'est produit. Utiliser le domaine dédié POST
        ``/api/v1/workspaces/<id>/invitations/`` (lien one-shot, acceptation).

        Le corps ci-dessous ne s'exécute qu'en rollback (flag à 0) et conserve le
        gating historique de ``INVITE_RETURN_RESET_LINK`` (jamais exposé en prod).
        """
        require_workspace_admin(user=request.user, workspace_id=workspace_id)

        if getattr(settings, "WORKSPACE_INVITATIONS_V2_ENABLED", True):
            return Response(
                {
                    "detail": (
                        "Endpoint déprécié : utilisez POST "
                        f"/api/v1/workspaces/{workspace_id}/invitations/."
                    ),
                    "code": "use_invitations_endpoint",
                },
                status=409,
            )

        ser = WorkspaceMemberAddSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        email = ser.validated_data["email"]
        role = ser.validated_data.get("role") or Membership.ROLE_MEMBER
        # Garde de défense en profondeur : le rôle owner ne s'attribue jamais ici.
        if role == Membership.ROLE_OWNER:
            return Response({"detail": "Le rôle owner ne peut pas être attribué."}, status=403)

        User = get_user_model()

        user = User.objects.filter(email=email).first()
        user_created = False
        if not user:
            defaults = {"email": email, "is_active": True}
            if hasattr(User, "USERNAME_FIELD") and User.USERNAME_FIELD != "email" and hasattr(User, "username"):
                defaults["username"] = email

            user = User.objects.create(**defaults)
            user.set_unusable_password()
            user.save(update_fields=["password"])
            user_created = True

        m, membership_created = Membership.objects.get_or_create(
            workspace_id=workspace_id,
            user=user,
            defaults={"role": role},
        )
        if not membership_created and m.role != role:
            if m.role == Membership.ROLE_OWNER and role != Membership.ROLE_OWNER and _count_owners(workspace_id) <= 1:
                return Response({"detail": "Impossible: dernier owner du workspace."}, status=400)
            m.role = role
            m.save(update_fields=["role"])

        reset_link = _make_reset_link(request, user)

        # Workspace name (best-effort)
        workspace_name = "Workspace"
        try:
            if hasattr(m, "workspace") and getattr(m.workspace, "name", ""):
                workspace_name = m.workspace.name
        except Exception:
            workspace_name = "Workspace"

        invited_by = (getattr(request.user, "email", "") or "").strip() or "admin"

        email_sent, email_error = _send_invite_email(
            to_email=(getattr(user, "email", "") or email),
            workspace_name=workspace_name,
            invited_by_email=invited_by,
            reset_link=reset_link,
        )

        payload = {
            "member": WorkspaceMemberSerializer(
                {
                    "id": m.id,
                    "user_id": m.user_id,
                    "email": getattr(user, "email", "") or email,
                    "role": m.role,
                    "created_at": m.created_at,
                }
            ).data,
            "user_created": user_created,
            "membership_created": membership_created,
            "email_sent": email_sent,
        }

        # Le reset_link (token de prise de contrôle du compte) et l'erreur SMTP brute
        # ne sont exposés que si INVITE_RETURN_RESET_LINK est explicitement activé
        # (DEV / support). Désactivé par défaut, forcé à False en prod. Le token
        # doit normalement transiter uniquement par email.
        if getattr(settings, "INVITE_RETURN_RESET_LINK", False):
            payload["reset_link"] = reset_link
            payload["email_error"] = email_error

        return Response(payload, status=201 if (user_created or membership_created) else 200)


class WorkspaceMemberDetailView(APIView):
    """
    PATCH  /api/v1/workspaces/<id>/members/<membership_id>/
    DELETE /api/v1/workspaces/<id>/members/<membership_id>/
    """
    permission_classes = [IsAuthenticated]

    def patch(self, request, workspace_id: int, membership_id: int):
        require_workspace_admin(user=request.user, workspace_id=workspace_id)

        m = (
            Membership.objects.select_related("user")
            .filter(id=membership_id, workspace_id=workspace_id)
            .first()
        )
        if not m:
            return Response({"detail": "Membership introuvable."}, status=404)

        ser = WorkspaceMemberPatchSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        new_role = ser.validated_data["role"]
        # Garde de défense en profondeur : aucune promotion vers owner (ni d'un
        # autre membre, ni de soi-même) via cet endpoint.
        if new_role == Membership.ROLE_OWNER:
            return Response({"detail": "Le rôle owner ne peut pas être attribué."}, status=403)

        if m.role == Membership.ROLE_OWNER and new_role != Membership.ROLE_OWNER and _count_owners(workspace_id) <= 1:
            return Response({"detail": "Impossible: dernier owner du workspace."}, status=400)

        m.role = new_role
        m.save(update_fields=["role"])

        return Response(
            WorkspaceMemberSerializer(
                {
                    "id": m.id,
                    "user_id": m.user_id,
                    "email": getattr(m.user, "email", "") or "",
                    "role": m.role,
                    "created_at": m.created_at,
                }
            ).data
        )

    def delete(self, request, workspace_id: int, membership_id: int):
        require_workspace_admin(user=request.user, workspace_id=workspace_id)

        requester = _get_requester_membership(request.user, workspace_id)
        if not requester:
            return Response({"detail": "No access."}, status=403)

        m = Membership.objects.select_related("user").filter(id=membership_id, workspace_id=workspace_id).first()
        if not m:
            return Response(status=204)

        if m.role == Membership.ROLE_OWNER and _count_owners(workspace_id) <= 1:
            return Response({"detail": "Impossible: dernier owner du workspace."}, status=400)

        if requester.role == Membership.ROLE_ADMIN and m.role == Membership.ROLE_OWNER:
            return Response({"detail": "Seul un owner peut retirer un owner."}, status=403)

        m.delete()
        return Response(status=204)


class WorkspaceRagSpaceListCreateView(APIView):
    """
    GET  /api/v1/workspaces/<id>/ragspaces/  (OWNER/ADMIN)  -> list non deleted
    POST /api/v1/workspaces/<id>/ragspaces/  (OWNER/ADMIN)  -> create
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id: int):
        require_workspace_admin(user=request.user, workspace_id=workspace_id)

        qs = (
            RagSpace.objects.filter(workspace_id=workspace_id, deleted_at__isnull=True)
            .order_by("name")
            .values("id", "name")
        )
        items = [{"id": str(row["id"]), "name": row["name"]} for row in qs]
        return Response(items, status=200)

    def post(self, request, workspace_id: int):
        # Le retour porte le Workspace : on le réutilise plutôt que de le relire
        # (et il garantit que l'appelant en est owner/admin).
        workspace = require_workspace_admin(user=request.user, workspace_id=workspace_id)

        from apps.llm_router.capabilities import (
            ALLOWED_CHAT_PROVIDERS,
            EMBEDDING_ERROR_MESSAGES,
            catalog_config_error,
            default_chat_model_error,
            embedding_provider_error,
            governed_model_error,
            is_valid_model_name,
        )
        from apps.llm_router.model_defaults import fill_missing_model_defaults
        from apps.llm_router.model_policy import (
            audit_model_config_denied,
            can_manage_ragspace_models,
            canonical_model_config,
            create_denied_fields,
        )
        from apps.workspaces.services.ragspace_creation import (
            RagSpaceCreationError,
            create_ragspace,
        )

        def _refused(exc: RagSpaceCreationError):
            """Refus de création -> réponse HTTP.

            409 et non 404 : la garde `require_workspace_admin` a DÉJÀ prouvé que
            l'appelant administre ce workspace. Lui dire pourquoi la création est
            refusée ne révèle donc rien qu'il ne puisse déjà voir — l'anti-énumération
            n'est pas concernée et reste intacte partout ailleurs.
            """
            return Response(
                {"detail": str(exc), "code": exc.code, **exc.detail}, status=409
            )

        def _create(kwargs: dict):
            """Unique point de création des DEUX parcours (superuser et standard).

            Le périmètre owner n'est PAS un paramètre : le service le dérive du
            membership de l'acteur. Sans cela, un RagSpace naissait avec
            `owner_scope=None` et devenait inutilisable sous enforcement — visible
            dans la liste, mais 404 à l'ouverture et à l'upload.
            """
            kwargs.pop("workspace_id", None)
            return create_ragspace(
                workspace=workspace,
                name=kwargs.pop("name"),
                actor=request.user,
                request_id=request.headers.get("x-request-id") or "",
                **kwargs,
            )

        ser = WorkspaceRagSpaceCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        name = ser.validated_data["name"]
        data = ser.validated_data

        # Politique modèles : seul un superuser actif choisit provider/modèle. Un
        # non-superuser qui tente une valeur ≠ canonique est REFUSÉ (jamais appliqué
        # silencieusement) ; sinon les valeurs canoniques sont imposées.
        is_model_manager = can_manage_ragspace_models(request.user)
        denied = create_denied_fields(request.user, data)
        if denied:
            audit_model_config_denied(request, rag_space_id=None, workspace_id=workspace_id, fields=denied)
            return Response(
                {"detail": "La configuration des modèles ne peut être modifiée que par un "
                           "administrateur plateforme.",
                 "code": "ragspace_model_configuration_forbidden", "fields": sorted(denied)},
                status=403,
            )

        create_kwargs: dict = {"workspace_id": workspace_id, "name": name}

        if not is_model_manager:
            # Valeurs canoniques imposées. Les champs modèle du payload sont ignorés.
            # Seule garde : un défaut PROUVÉ inutilisable en génération (retiré de la
            # passerelle, non servi par /v1/generate) est refusé avec un motif
            # exploitable plutôt que de créer un espace dont le chat échouera.
            default_code = default_chat_model_error()
            if default_code:
                return Response(
                    {"detail": EMBEDDING_ERROR_MESSAGES[default_code], "code": default_code,
                     "field": "llm_model"},
                    status=400,
                )
            create_kwargs.update(canonical_model_config())
            try:
                rs = _create(create_kwargs)
            except RagSpaceCreationError as exc:
                return _refused(exc)
            return Response(
                {"id": str(rs.id), "name": rs.name, "llm_provider": rs.llm_provider,
                 "llm_model": rs.llm_model or "", "embedding_provider": rs.embedding_provider,
                 "embedding_model": rs.embedding_model or "",
                 "can_edit_model_configuration": False},
                status=201,
            )

        def _check_provider(value, allowed, *, code):
            p = (value or "").strip().lower()
            if p not in allowed:
                return None, Response({"detail": "Provider non supporté.", "code": code}, status=400)
            return p, None

        if "llm_provider" in data:
            p, err = _check_provider(data["llm_provider"], ALLOWED_CHAT_PROVIDERS, code="invalid_llm_provider")
            if err:
                return err
            create_kwargs["llm_provider"] = p
        if "llm_model" in data:
            m = (data["llm_model"] or "").strip()
            if not is_valid_model_name(m):
                return Response({"detail": "Modèle LLM invalide.", "code": "invalid_llm_model"}, status=400)
            create_kwargs["llm_model"] = m
        if "embedding_provider" in data:
            # Validation DÉDIÉE aux embeddings d'un RagSpace : elle admet le pipeline
            # gouverné `ai_gateway`, mais seulement si l'environnement l'expose
            # réellement. `_check_provider` (chat) ne connaît pas cette condition.
            ep = (data["embedding_provider"] or "").strip().lower()
            code = embedding_provider_error(ep)
            if code:
                return Response(
                    {"detail": EMBEDDING_ERROR_MESSAGES[code], "code": code}, status=400
                )
            create_kwargs["embedding_provider"] = ep
        if "embedding_model" in data:
            m = (data["embedding_model"] or "").strip()
            if not is_valid_model_name(m):
                return Response({"detail": "Modèle d'embeddings invalide.", "code": "invalid_embedding_model"}, status=400)
            create_kwargs["embedding_model"] = m

        # Champs modèle ABSENTS du payload -> défauts de l'environnement, EXPLICITEMENT.
        #
        # Sans cette ligne, le superuser retombait sur le défaut du champ Django
        # tandis qu'un non-superuser recevait `canonical_model_config()` : deux
        # parcours, deux résultats, pour la même absence de choix. Les défauts de
        # champ sont désormais dynamiques eux aussi, mais on ne s'en REMET pas à eux
        # ici — la réponse HTTP doit porter la valeur persistée, et le contrat de
        # l'API ne doit pas dépendre d'un défaut déclaré ailleurs.
        fill_missing_model_defaults(create_kwargs)

        # Couple (provider, modèle) évalué sur l'état RÉSULTANT — défauts compris.
        # Un espace gouverné sans modèle logique exploitable échouerait à la première
        # ingestion ; il ne doit pas pouvoir naître.
        code = governed_model_error(
            create_kwargs.get("embedding_provider", ""), create_kwargs.get("embedding_model", "")
        )
        if code:
            return Response({"detail": EMBEDDING_ERROR_MESSAGES[code], "code": code}, status=400)

        # --- Gouvernance de catalogue -------------------------------------
        # AVANT `_create` : un espace refusé ne doit laisser aucune trace. À la
        # création, tout est « changé » — l'ancien état est vide, donc les quatre
        # champs résultants sont validés.
        cat_code, cat_field = catalog_config_error(
            old={},
            new={k: create_kwargs.get(k, "") for k in
                 ("llm_provider", "llm_model", "embedding_provider", "embedding_model")},
            force=bool(data.get("force")),
        )
        if cat_code:
            audit_model_config_denied(
                request, rag_space_id=None, workspace_id=workspace_id, fields={cat_field}
            )
            return Response(
                {"detail": EMBEDDING_ERROR_MESSAGES[cat_code], "code": cat_code,
                 "field": cat_field},
                status=400,
            )

        try:
            rs = _create(create_kwargs)
        except RagSpaceCreationError as exc:
            return _refused(exc)
        return Response(
            {
                "id": str(rs.id),
                "name": rs.name,
                "llm_provider": rs.llm_provider,
                "llm_model": rs.llm_model or "",
                "embedding_provider": rs.embedding_provider,
                "embedding_model": rs.embedding_model or "",
                # Chemin atteint uniquement par un superuser (gérant des modèles).
                "can_edit_model_configuration": True,
            },
            status=201,
        )


class WorkspaceDomainCorpusListView(APIView):
    """GET /api/v1/workspaces/<id>/domain-corpora/ — lecture seule (Phase 4).

    Liste les corpus métier ASSOCIÉS et ACTIFS du workspace. Accessible à tout
    membre (require_workspace_access "read") ; AUCUNE écriture n'est exposée ici.
    Étanchéité : ne révèle que les corpus explicitement associés et activés
    (jointure ``WorkspaceDomainCorpusAccess`` enabled + corpus actif). Un workspace
    sans corpus reçoit ``[]``. Aucun champ sensible (chemins, texte) n'est exposé.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id: int):
        # 403 anti-énumération si non-membre (PermissionDenied -> 403).
        require_workspace_access(user=request.user, workspace_id=workspace_id, permission="read")

        from apps.corpus.models import WorkspaceDomainCorpusAccess
        from apps.corpus.services.document_counts import (
            counts_from_annotated,
            document_count_annotations,
        )

        # Compteurs par STATUT : « 12 documents » ne dit pas combien sont réellement
        # interrogeables. `ready_documents_count` applique la définition d'éligibilité
        # du retrieval, pas une approximation. Agrégats filtrés = une seule requête.
        qs = (
            WorkspaceDomainCorpusAccess.objects.select_related("corpus", "corpus__domain")
            .filter(workspace_id=workspace_id, enabled=True, corpus__is_active=True)
            .annotate(**document_count_annotations("corpus__documents"))
            .order_by("corpus__domain__name", "corpus__name")
        )

        items = [
            {
                "corpus_id": str(a.corpus_id),
                "corpus_name": a.corpus.name,
                "corpus_slug": a.corpus.slug,
                "description": a.corpus.description,
                "business_domain_id": str(a.corpus.domain_id),
                "business_domain_name": getattr(a.corpus.domain, "name", None),
                "is_active": a.corpus.is_active,
                "read_only": True,
                "managed_by": "ragatm",
                # `documents_count` est CONSERVÉ (compatibilité des clients existants).
                **counts_from_annotated(a),
                "updated_at": a.corpus.updated_at.isoformat() if a.corpus.updated_at else None,
            }
            for a in qs
        ]
        return Response(items)
