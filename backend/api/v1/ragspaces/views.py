# backend/api/v1/ragspaces/views.py
import uuid

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.db.models import Exists, OuterRef
from rest_framework.exceptions import NotFound
from rest_framework.exceptions import PermissionDenied as DRFPermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.models import LlmSettingsChangeLog
from apps.llm_router.capabilities import (
    ALLOWED_CHAT_PROVIDERS,
    EMBEDDING_ERROR_MESSAGES,
    catalog_config_error,
    embedding_provider_error,
    governed_model_error,
    is_valid_chat_model,
    is_valid_model_name,
    provider_capabilities,
    space_embeddings_settings,
    test_provider_connection,
)
from apps.rag_core.models import Document, RagSpace
from apps.rag_core.models.document import DocumentStatus
from apps.rag_core.services.rag_space_deletion import (
    RagSpaceDeletionError,
    RagSpaceNotFound,
    delete_rag_space_permanently,
)
from apps.workspaces.models import Membership
from apps.workspaces.services.access import require_ragspace_access

from .serializers import RagSpaceListItemSerializer, RagSpacePatchSerializer


# Code de refus STABLE de la suppression définitive. Volontairement identique
# pour « pas le bon rôle » et « hors périmètre owner » : le client apprend qu'il
# n'a pas le droit, jamais à qui l'espace appartient.
RAG_SPACE_DELETE_FORBIDDEN = "rag_space_delete_forbidden"


def _is_owner_admin(*, rs: RagSpace, user) -> bool:
    m = Membership.objects.filter(workspace=rs.workspace, user=user).first()
    return bool(m and m.role in (Membership.ROLE_OWNER, Membership.ROLE_ADMIN))


def _serialize_rag_space(rs: RagSpace) -> dict:
    """Forme unique d'un item de listing RagSpace.

    Garantit que toutes les branches de listing renvoient EXACTEMENT les mêmes
    clés (id, name, workspace_id, llm_*, embedding_*). Les défauts viennent du
    modèle et sont identiques quel que soit le contexte.
    """
    return {
        "id": rs.id,
        "name": rs.name,
        "workspace_id": rs.workspace_id,
        "llm_provider": getattr(rs, "llm_provider", "ollama") or "ollama",
        "llm_model": getattr(rs, "llm_model", "") or "",
        "embedding_provider": getattr(rs, "embedding_provider", "ai_gateway") or "ai_gateway",
        "embedding_model": getattr(rs, "embedding_model", "") or "",
        "allow_external_fallback": bool(getattr(rs, "allow_external_fallback", False)),
        # Annoté sur le queryset (Exists) ; défaut False si non annoté.
        "reindex_required": bool(getattr(rs, "reindex_required_flag", False)),
    }


def _with_reindex_flag(qs):
    """Annote un queryset RagSpace avec ``reindex_required_flag`` = True si au moins
    un document de l'espace doit être réindexé (config d'embeddings changée). Évite
    tout N+1 dans les listings."""
    from apps.rag_core.models import Document

    return qs.annotate(
        reindex_required_flag=Exists(
            Document.objects.filter(rag_space_id=OuterRef("pk"), reindex_required=True)
        )
    )


class RagSpaceListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """
        GET /api/v1/rag-spaces/
          - optional: ?workspace_id=<int>
        Behavior:
          - if workspace_id provided: list ragspaces for that workspace if requester is member
          - else: list ragspaces across ALL workspaces where requester is a member
        Always excludes soft-deleted ragspaces.
        """
        from apps.workspaces.services import scope_audit
        from apps.workspaces.services.access import effective_ragspace_access, visible_ragspaces_for_user
        from apps.llm_router.model_policy import can_edit_model_configuration

        workspace_id = (request.query_params.get("workspace_id") or "").strip()
        # Capacité d'édition des MODÈLES : par UTILISATEUR (superuser actif), identique
        # pour tous les espaces. Le frontend ne la déduit jamais d'un rôle workspace/space.
        can_edit_models = can_edit_model_configuration(request.user)

        def _emit_and_serialize(wid: int, base_qs) -> list:
            # `visible_ragspaces_for_user` : flags OFF → base (tout le workspace,
            # identique au legacy) ; sous enforcement → périmètre owner de l'acteur.
            qs = _with_reindex_flag(base_qs).order_by("name")
            rows = list(qs)
            # Rôle effectif de l'appelant, ajouté à CHAQUE item (autorité backend).
            membership = Membership.objects.filter(workspace_id=wid, user=request.user).first()
            items = []
            for rs in rows:
                d = _serialize_rag_space(rs)
                d.update(effective_ragspace_access(user=request.user, rag_space=rs, membership=membership))
                d["can_edit_model_configuration"] = can_edit_models
                # dépôt audio ouvert pour CET espace (drapeau ET (mode
                # général OU liste)). Le droit d'écriture reste exigé au dépôt.
                from apps.rag_core.services.ingest.extraction import config as _acfg

                d["audio_upload_enabled"] = _acfg.audio_admitted_for(d.get("id"))
                items.append(d)
            scope_audit.emit_ragspace_list_decision(
                user=request.user, workspace_id=wid,
                legacy_ids=[it["id"] for it in items], endpoint="ragspaces.list", request=request,
            )
            return items

        # Case 1: workspace scoped list
        if workspace_id:
            wid = int(workspace_id)
            # Refus explicite d'un workspace_id arbitraire : l'appelant DOIT être
            # membre du workspace (défense en profondeur — le sélecteur ne propose
            # que des workspaces membres ; ici on refuse une requête forgée).
            if not Membership.objects.filter(workspace_id=wid, user=request.user).exists():
                return Response({"detail": "not_a_member"}, status=403)
            items = _emit_and_serialize(wid, visible_ragspaces_for_user(user=request.user, workspace_id=wid))
            return Response(RagSpaceListItemSerializer(items, many=True).data, status=200)

        # Case 2: list across all accessible workspaces (member of), scoped per workspace.
        # Workspaces ACTIFS seulement, comme `WorkspaceListView` : un workspace désactivé
        # est exclu de la navigation (NAVIGATION-SEARCH-1). Les règles d'accès au CONTENU
        # (`require_ragspace_access`) ne changent pas.
        ws_ids = sorted(Membership.objects.filter(user=request.user, workspace__is_active=True)
                        .values_list("workspace_id", flat=True))
        if not ws_ids:
            return Response([], status=200)
        items = []
        for wid in ws_ids:
            items.extend(_emit_and_serialize(wid, visible_ragspaces_for_user(user=request.user, workspace_id=wid)))
        return Response(RagSpaceListItemSerializer(items, many=True).data, status=200)


class RagSpaceDetailView(APIView):
    """
    PATCH   /api/v1/rag-spaces/<uuid>/
    DELETE  /api/v1/rag-spaces/<uuid>/   (suppression DÉFINITIVE — cf. `delete`)
    """
    permission_classes = [IsAuthenticated]

    def _require_owner_admin(self, *, rs: RagSpace, user) -> bool:
        return _is_owner_admin(rs=rs, user=user)

    def patch(self, request, rag_space_id):
        from apps.workspaces.services import scope_audit
        scope_audit.emit_ragspace_decision(
            user=request.user, rag_space_id=rag_space_id, permission="manage",
            endpoint="ragspaces.patch", request=request,
        )
        rs, elevated = _resolve_for_model_admin(request, rag_space_id)

        if getattr(rs, "deleted_at", None) is not None:
            return Response({"detail": "Not found"}, status=404)

        ser = RagSpacePatchSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        # Élévation PLATEFORME : l'acteur est superuser mais hors périmètre owner. Elle
        # ne couvre QUE la configuration IA — renommer un espace ou basculer sa politique
        # d'egress restent des actes métier, réservés à qui détient le périmètre.
        if elevated:
            from apps.llm_router.model_policy import (
                audit_model_config_elevated,
                platform_model_admin_denied_fields,
            )

            outside = platform_model_admin_denied_fields(ser.validated_data)
            if outside:
                return Response(
                    {"detail": "L'administration plateforme ne couvre que la configuration "
                               "modèle de cet espace.",
                     "code": "platform_admin_scope_exceeded", "fields": sorted(outside)},
                    status=403,
                )
            audit_model_config_elevated(
                request, rag_space_id=rs.id, workspace_id=rs.workspace_id,
                fields=set(ser.validated_data) - {"force"},
            )

        # Politique modèles : seul un superuser actif peut changer provider/modèle.
        # Un non-superuser qui tente de MODIFIER un de ces champs est REFUSÉ ; une
        # valeur identique à l'existante est ignorée (config historique jamais réécrite).
        from apps.llm_router.model_policy import (
            audit_model_config_changed,
            audit_model_config_denied,
            can_manage_ragspace_models,
            patch_denied_fields,
        )

        is_model_manager = can_manage_ragspace_models(request.user)
        denied = patch_denied_fields(request.user, rs, ser.validated_data)
        if denied:
            audit_model_config_denied(request, rag_space_id=rs.id, workspace_id=rs.workspace_id, fields=denied)
            return Response(
                {"detail": "La configuration des modèles ne peut être modifiée que par un "
                           "administrateur plateforme.",
                 "code": "ragspace_model_configuration_forbidden", "fields": sorted(denied)},
                status=403,
            )

        # État avant modification (pour audit chat + embeddings).
        old_provider = rs.llm_provider or ""
        old_model = rs.llm_model or ""
        old_emb_provider = rs.embedding_provider or ""
        old_emb_model = rs.embedding_model or ""

        update_fields = []

        if "name" in ser.validated_data:
            name = (ser.validated_data["name"] or "").strip()
            if not name:
                return Response({"detail": "name_required"}, status=400)
            rs.name = name
            update_fields.append("name")

        # --- Chat / génération (superuser uniquement) ---
        if is_model_manager and "llm_provider" in ser.validated_data:
            provider = (ser.validated_data["llm_provider"] or "").strip().lower()
            if provider not in ALLOWED_CHAT_PROVIDERS:
                return Response(
                    {"detail": "Provider LLM non supporté.", "code": "invalid_llm_provider"},
                    status=400,
                )
            rs.llm_provider = provider
            update_fields.append("llm_provider")

        if is_model_manager and "llm_model" in ser.validated_data:
            model = (ser.validated_data["llm_model"] or "").strip()
            if not is_valid_chat_model(model):
                return Response(
                    {"detail": "Modèle LLM invalide.", "code": "invalid_llm_model"},
                    status=400,
                )
            rs.llm_model = model
            update_fields.append("llm_model")

        # --- Embeddings / indexation (superuser uniquement) ---
        if is_model_manager and "embedding_provider" in ser.validated_data:
            emb_provider = (ser.validated_data["embedding_provider"] or "").strip().lower()
            code = embedding_provider_error(emb_provider)
            if code:
                return Response(
                    {"detail": EMBEDDING_ERROR_MESSAGES[code], "code": code}, status=400
                )
            rs.embedding_provider = emb_provider
            update_fields.append("embedding_provider")

        if is_model_manager and "embedding_model" in ser.validated_data:
            emb_model = (ser.validated_data["embedding_model"] or "").strip()
            if not is_valid_model_name(emb_model):
                return Response(
                    {"detail": "Modèle d'embeddings invalide.", "code": "invalid_embedding_model"},
                    status=400,
                )
            rs.embedding_model = emb_model
            update_fields.append("embedding_model")

        # Couple (provider, modèle) évalué sur l'état RÉSULTANT, pas sur le seul
        # payload : un PATCH qui ne change que le provider hérite du modèle existant,
        # et l'inverse est vrai aussi. Vérifier chaque champ isolément laisserait
        # passer un espace gouverné sans modèle logique exploitable.
        if is_model_manager:
            code = governed_model_error(rs.embedding_provider, rs.embedding_model)
            if code:
                return Response(
                    {"detail": EMBEDDING_ERROR_MESSAGES[code], "code": code}, status=400
                )

        # --- Gouvernance de catalogue -------------------------------------
        # DERNIÈRE barrière avant `rs.save()`, donc avant TOUT effet de bord : ni
        # l'espace ni ses documents n'ont encore été écrits. C'est ce qui fait qu'un
        # modèle refusé ne laisse rien derrière lui — le cas `bge-m3` marquait
        # auparavant tous les documents `reindex_required` avant d'échouer à la
        # première ingestion.
        #
        # `force` est superuser-only par construction (on est déjà dans
        # `is_model_manager`) et ne franchit que la qualification.
        if is_model_manager:
            forced = bool(ser.validated_data.get("force"))
            code, field = catalog_config_error(
                old={"llm_provider": old_provider, "llm_model": old_model,
                     "embedding_provider": old_emb_provider, "embedding_model": old_emb_model},
                new={"llm_provider": rs.llm_provider, "llm_model": rs.llm_model,
                     "embedding_provider": rs.embedding_provider,
                     "embedding_model": rs.embedding_model},
                force=forced,
            )
            if code:
                audit_model_config_denied(
                    request, rag_space_id=rs.id, workspace_id=rs.workspace_id, fields={field}
                )
                return Response(
                    {"detail": EMBEDDING_ERROR_MESSAGES[code], "code": code, "field": field},
                    status=400,
                )

        # --- Politique d'egress externe (opt-in explicite de CONFIDENTIALITÉ) ---
        # Ce n'est plus une simple option de coût : ce booléen décide si le contenu
        # documentaire de l'espace peut quitter l'infrastructure privée. Son
        # basculement est donc tracé séparément (cf. audit plus bas) — il ne l'était
        # nulle part avant le Lot 1 (constat P0-C #6).
        old_allow_external = bool(rs.allow_external_fallback)
        if "allow_external_fallback" in ser.validated_data:
            rs.allow_external_fallback = bool(ser.validated_data["allow_external_fallback"])
            update_fields.append("allow_external_fallback")


        if not update_fields:
            return Response({"detail": "No fields to update"}, status=400)

        rs.save(update_fields=update_fields)

        # --- Audit du changement de politique d'egress (append-only) ---
        # Journalisé AVANT tout autre audit et indépendamment de lui : autoriser la
        # sortie des données d'un espace est le geste le plus sensible de la
        # plateforme, il ne doit dépendre d'aucune autre condition.
        new_allow_external = bool(rs.allow_external_fallback)
        if new_allow_external != old_allow_external:
            from apps.llm_router.egress_audit import record_policy_change

            record_policy_change(
                actor=request.user,
                scope_type="ragspace",
                scope_ref=str(rs.id),
                workspace_id=str(getattr(rs, "workspace_id", "") or ""),
                field_name="allow_external_fallback",
                old_value=old_allow_external,
                new_value=new_allow_external,
                request_id=str(request.headers.get("X-Request-ID", ""))[:64],
            )

        # Audit : si chat ET/OU embeddings ont effectivement changé.
        new_provider = rs.llm_provider or ""
        new_model = rs.llm_model or ""
        new_emb_provider = rs.embedding_provider or ""
        new_emb_model = rs.embedding_model or ""
        chat_changed = new_provider != old_provider or new_model != old_model
        emb_changed = new_emb_provider != old_emb_provider or new_emb_model != old_emb_model

        # Audit dédié « configuration modèles » (superuser only — les non-superusers
        # ne peuvent jamais atteindre ce point avec un changement de modèle).
        if chat_changed or emb_changed:
            audit_model_config_changed(
                request, rag_space_id=rs.id, workspace_id=rs.workspace_id,
                before={"llm_provider": old_provider, "llm_model": old_model,
                        "embedding_provider": old_emb_provider, "embedding_model": old_emb_model},
                after={"llm_provider": new_provider, "llm_model": new_model,
                       "embedding_provider": new_emb_provider, "embedding_model": new_emb_model},
            )

        # P1-C : un changement d'embeddings rend les vecteurs existants incohérents
        # avec le modèle courant → on marque TOUS les documents non-FAILED de
        # l'espace comme "à réindexer". Effet backend réel (plus un simple flag de
        # réponse) : la recherche vectorielle exclura ces documents jusqu'à
        # réindexation (cf. search_chunks_vector), et l'UI peut proposer l'action.
        if emb_changed:
            Document.objects.filter(rag_space=rs).exclude(
                status=DocumentStatus.FAILED
            ).update(reindex_required=True)

        if chat_changed or emb_changed:
            LlmSettingsChangeLog.objects.create(
                user=request.user,
                workspace=rs.workspace,
                rag_space=rs,
                old_provider=old_provider,
                old_model=old_model,
                new_provider=new_provider,
                new_model=new_model,
                old_embedding_provider=old_emb_provider,
                old_embedding_model=old_emb_model,
                new_embedding_provider=new_emb_provider,
                new_embedding_model=new_emb_model,
            )

        return Response(
            {
                "id": str(rs.id),
                "name": rs.name,
                "llm_provider": rs.llm_provider,
                "llm_model": rs.llm_model or "",
                "embedding_provider": rs.embedding_provider,
                "embedding_model": rs.embedding_model or "",
                "allow_external_fallback": bool(rs.allow_external_fallback),
                "embeddings_reindex_required": emb_changed,
                "can_edit_model_configuration": is_model_manager,
            }
        )

    def delete(self, request, rag_space_id):
        """Suppression DÉFINITIVE (purge) — ce n'est PAS un soft delete.

        Supprime le RagSpace, toutes ses données dépendantes ET les fichiers
        physiques associés. Irréversible : c'est ce que promet l'interface.
        La logique vit dans le service ; cette vue ne fait que permissions +
        mapping HTTP.

        204 — purge terminée               403 — authentifié mais non autorisé
        404 — identifiant inexistant       500 — échec (RIEN n'a été supprimé)
                ou hors périmètre owner
        """
        rs = RagSpace.objects.select_related("workspace").filter(id=rag_space_id).first()
        if rs is None:
            # 404 (et non 204) : un identifiant inexistant est une erreur d'appel,
            # elle ne doit pas être présentée comme une suppression réussie.
            return Response({"detail": "RAG space not found"}, status=404)

        from apps.workspaces.services import scope_audit
        scope_audit.emit_ragspace_decision(
            user=request.user, rag_space_id=rag_space_id, permission="manage",
            endpoint="ragspaces.delete", request=request,
        )

        # --- Garde CANONIQUE (périmètre owner + ACL espace) ------------------
        # `emit_ragspace_decision` ci-dessus est fail-open : elle OBSERVE la
        # décision cible, elle ne l'applique pas. Sans l'appel ci-dessous, un
        # owner pouvait DÉTRUIRE un espace d'un autre périmètre alors que le
        # PATCH lui refusait de le RENOMMER — la garde manquait exactement là où
        # l'action est irréversible.
        #
        # `permission="manage"` n'est pas un choix nouveau : c'est déjà la
        # permission que cet endpoint déclare dans son propre audit (juste
        # au-dessus). L'enforcement rejoint la déclaration.
        try:
            require_ragspace_access(
                user=request.user, rag_space_id=rag_space_id, permission="manage"
            )
        except NotFound:
            # Hors périmètre owner : la ressource ne doit pas être révélée —
            # indiscernable d'un identifiant inexistant (anti-énumération).
            return Response({"detail": "RAG space not found"}, status=404)
        except (DjangoPermissionDenied, DRFPermissionDenied):
            # La garde canonique lève le `PermissionDenied` de DJANGO (garde
            # legacy) et le `NotFound` de DRF (refus de périmètre) — les deux
            # familles cohabitent dans `access.py`. On attrape les deux plutôt
            # que de parier sur celle qui remontera.
            return Response(
                {"detail": "No access to this RAG space", "code": RAG_SPACE_DELETE_FORBIDDEN},
                status=403,
            )

        # Rôle workspace : la garde canonique valide le PÉRIMÈTRE, celle-ci le
        # NIVEAU. Un membre/viewer dans le bon périmètre reste refusé ici.
        if not self._require_owner_admin(rs=rs, user=request.user):
            return Response(
                {"detail": "No access to this RAG space", "code": RAG_SPACE_DELETE_FORBIDDEN},
                status=403,
            )

        try:
            delete_rag_space_permanently(rag_space_id=rs.id, actor=request.user)
        except RagSpaceNotFound:
            # Course : un autre appel a purgé l'espace entre-temps.
            return Response({"detail": "RAG space not found"}, status=404)
        except RagSpaceDeletionError as e:
            # Un échec ne doit JAMAIS être présenté comme un succès. Aucun détail
            # de stockage n'est exposé au client (chemins internes).
            return Response(
                {
                    "detail": "La suppression définitive de l'espace a échoué.",
                    "code": "rag_space_deletion_failed",
                    "reason": e.code,
                },
                status=500,
            )
        return Response(status=204)


class RagSpaceStatsView(APIView):
    """
    GET /api/v1/rag-spaces/<uuid>/stats/
    - read access required
    Returns:
      - document counts by status
      - chunks_total
      - indexing in progress (pending+ingesting)
      - last_created_at
      - documents_watch: last N docs with status in pending/ingesting/failed
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, rag_space_id):
        from apps.workspaces.services import scope_audit
        scope_audit.emit_ragspace_decision(
            user=request.user, rag_space_id=rag_space_id, permission="read",
            endpoint="ragspaces.stats", request=request,
        )
        require_ragspace_access(user=request.user, rag_space_id=rag_space_id, permission="read")

        from apps.rag_core.domain.coverage import coverage_to_dict
        from apps.rag_core.models.document import Document, DocumentStatus
        from apps.rag_core.services.coverage_service import compute_rag_space_statistics

        # Tout le calcul (documents, contenu, ratios, empreinte, couverture) est
        # fait en SQL agrégé par le service : aucun LLM, aucun embedding, aucun
        # retrieval, aucune logique métier. Le périmètre est celui du RagSpace
        # dont l'accès vient d'être vérifié — il ne peut pas s'élargir ici.
        stats = compute_rag_space_statistics(rag_space_id)
        coverage = stats["coverage"]
        documents = stats["documents"]
        last_created_at = stats["last_created_at"]

        in_progress_count = documents["pending"] + documents["ingesting"]

        # Watchlist: docs pending/ingesting/failed (champ historique, inchangé).
        watch_statuses = [DocumentStatus.PENDING, DocumentStatus.INGESTING, DocumentStatus.FAILED]
        watch_limit = 25
        watch = (
            Document.objects.filter(rag_space_id=rag_space_id, status__in=watch_statuses)
            .order_by("status", "-created_at")
            .values("id", "filename", "status", "error_message", "created_at")[:watch_limit]
        )

        documents_watch = []
        for row in watch:
            created_at = row.get("created_at")
            documents_watch.append(
                {
                    "id": str(row.get("id")),
                    "filename": row.get("filename") or "",
                    "status": row.get("status") or "",
                    "error_message": row.get("error_message") or "",
                    "created_at": created_at.isoformat() if created_at else None,
                }
            )

        return Response(
            {
                # --- Champs historiques : forme strictement inchangée ---
                "rag_space_id": str(rag_space_id),
                "documents": documents,  # + with_chunks / without_chunks (additif)
                "chunks_total": stats["content"]["chunks_total"],
                "indexing": {
                    "in_progress": in_progress_count > 0,
                    "in_progress_count": in_progress_count,
                },
                "last_created_at": last_created_at.isoformat() if last_created_at else None,
                "documents_watch": documents_watch,
                "documents_watch_limit": watch_limit,
                # --- Nouveaux blocs (additifs) ---
                "content": stats["content"],
                "ratios": stats["ratios"],
                "scope": {
                    "generated_at": coverage.scope.generated_at.isoformat(),
                    "corpus_fingerprint": coverage.scope.corpus_fingerprint,
                },
                "coverage": coverage_to_dict(coverage),
            },
            status=200,
        )


def _resolve_for_model_admin(request, rag_space_id):
    """(space, elevated) — résout l'accès à la CONFIGURATION MODÈLE d'un espace.

    Chemin nominal d'abord : périmètre owner et rôle workspace. S'il refuse et que
    l'acteur est superuser actif, l'accès est accordé en mode ÉLEVÉ — strictement borné
    à la configuration IA (cf. `PlatformModelAdminScope`). Sans cela, un espace dont le
    périmètre appartient à un non-superuser n'est administrable que par shell, sans
    audit applicatif : la friction sans la garantie.
    """
    from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
    from rest_framework.exceptions import NotFound, PermissionDenied

    from apps.llm_router.model_policy import can_manage_ragspace_models

    # Les DEUX familles de refus doivent être captées : la garde legacy
    # (`workspaces.services.access`) lève le `PermissionDenied` de **Django**, la garde
    # owner-scope celui de **DRF**. N'en capter qu'une laissait l'élévation inopérante
    # sur le chemin le plus courant — un superuser non membre du workspace.
    _DENIALS = (DjangoPermissionDenied, PermissionDenied, NotFound)

    try:
        rs = require_ragspace_access(user=request.user, rag_space_id=rag_space_id, permission="write")
        if _is_owner_admin(rs=rs, user=request.user):
            return rs, False
        if not can_manage_ragspace_models(request.user):
            raise PermissionDenied("No access to this RAG space")
    except _DENIALS:
        if not can_manage_ragspace_models(request.user):
            raise
        rs = None
    if rs is None:
        rs = RagSpace.objects.filter(id=rag_space_id, deleted_at__isnull=True).first()
        if rs is None:
            raise NotFound("not_found")
    return rs, True


def _document_impact(rs) -> dict:
    """Ce qu'un changement de pipeline d'embeddings ferait, en nombres.

    Sert l'avertissement de réindexation de l'interface. Volontairement DEUX
    compteurs et non un total : `ready` sera marqué et réellement réindexé, `failed`
    ne le sera pas (le marquage exclut déjà `FAILED`) et relève d'un autre problème.
    """
    from django.db.models import Count, Q

    agg = Document.objects.filter(rag_space=rs).aggregate(
        total=Count("id"),
        ready=Count("id", filter=Q(status=DocumentStatus.READY)),
        failed=Count("id", filter=Q(status=DocumentStatus.FAILED)),
        reindex_required=Count("id", filter=Q(reindex_required=True)),
    )
    return {k: int(v or 0) for k, v in agg.items()}


class RagSpaceLLMSettingsView(APIView):
    """
    GET /api/v1/rag-spaces/<uuid>/llm-settings/
    - owner/admin uniquement
    - renvoie le réglage courant (chat ET embeddings) + capacités des providers
      (sans secret) ; les embeddings sont désormais configurables PAR espace.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, rag_space_id):
        from apps.workspaces.services import scope_audit
        scope_audit.emit_ragspace_decision(
            user=request.user, rag_space_id=rag_space_id, permission="manage",
            endpoint="ragspaces.llm_settings", request=request,
        )
        # Même résolution que le PATCH : un superuser doit pouvoir LIRE la configuration
        # qu'il a le droit de modifier, sinon l'écran ne peut rien afficher avant d'agir.
        # La réponse ne porte aucun contenu documentaire — seulement la config et des
        # compteurs.
        rs, _elevated = _resolve_for_model_admin(request, rag_space_id)

        from apps.llm_router.model_policy import can_edit_model_configuration

        return Response(
            {
                "llm_provider": rs.llm_provider or "ollama",
                "llm_model": rs.llm_model or "",
                "embedding_provider": rs.embedding_provider or "ai_gateway",
                "embedding_model": rs.embedding_model or "",
                "providers": provider_capabilities(),
                "embeddings": space_embeddings_settings(rs),
                "can_edit_model_configuration": can_edit_model_configuration(request.user),
                # Impact d'un changement de pipeline, calculé AVANT que l'utilisateur
                # ne le déclenche. `ready` et `failed` sont séparés parce qu'ils ne
                # décrivent pas le même problème : un document PRÊT sera réellement
                # réindexé, un document en ÉCHEC ne le sera pas — il n'a jamais été
                # indexé, et aucune réindexation ne le réparera. Les additionner
                # produit le compteur trompeur observé sur l'espace AEC
                # (« 46 documents à réindexer », dont 44 qui n'ont aucun chunk).
                "documents": _document_impact(rs),
            },
            status=200,
        )


class RagSpaceReindexView(APIView):
    """
    POST /api/v1/rag-spaces/<uuid>/reindex/   (owner/admin uniquement)

    Étape 2 — réindexation globale d'un espace (ex. après changement du provider/
    modèle d'embeddings). Réindexe tous les documents de l'espace possédant un
    fichier source ; ignore ceux déjà en cours (PENDING/INGESTING) et ceux sans
    storage_file (anciens imports non réindexables).

    Réponse **asynchrone dans tous les cas** : la requête crée un job durable et
    rend la main aussitôt. Il n'existe plus de branche d'exécution « inline » —
    `CELERY_TASK_ALWAYS_EAGER` ne change PAS le code de retour.

    Codes :
      - 202 {status: "accepted", job_id, total_documents, queued_documents,
        already_running: false} — job créé, travail semé dans Celery (queue
        `reindex`), un item par document ;
      - 409 {status: "already_running", job_id, job} — un job est déjà actif sur
        l'espace ; on retourne l'existant plutôt que d'en créer un second ;
      - 403 non owner/admin ; 404 espace supprimé ;
      - 503 garde EAGER : trop de documents pour un traitement sans broker.

    Suivi : GET /api/v1/reindex-jobs/<job_id>/ et
    GET /api/v1/rag-spaces/<id>/active-reindex-job/.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, rag_space_id):
        """Crée un JOB de réindexation durable et répond **202** immédiatement.

        Aucune extraction/embedding n'a lieu dans la requête HTTP : le travail est
        semé dans Celery (queue `reindex`), item par document. Concurrence : un seul
        job actif par espace → sinon on retourne le job existant (`already_running`).
        """
        from apps.audit.action_audit import emit_action_event
        from apps.rag_core.services.ingest.reindex_jobs import (
            ReindexJobError,
            create_reindex_job,
            serialize_job,
        )

        from apps.workspaces.services import scope_audit
        scope_audit.emit_ragspace_decision(
            user=request.user, rag_space_id=rag_space_id, permission="manage",
            endpoint="ragspaces.reindex", request=request,
        )
        rs = require_ragspace_access(user=request.user, rag_space_id=rag_space_id, permission="write")
        if getattr(rs, "deleted_at", None) is not None:
            return Response({"detail": "Not found"}, status=404)
        if not _is_owner_admin(rs=rs, user=request.user):
            emit_action_event(
                event_name="action.denied", action_name="ragspace.reindex", status="denied",
                actor=request.user, rag_space_id=str(rs.id), resource_type="rag_space",
                resource_id=str(rs.id),
            )
            return Response({"detail": "No access to this RAG space"}, status=403)

        try:
            outcome = create_reindex_job(rag_space=rs, user=request.user)
        except ReindexJobError as e:
            emit_action_event(
                event_name="action.failed", action_name="ragspace.reindex", status="failed",
                actor=request.user, rag_space_id=str(rs.id), resource_type="rag_space",
                resource_id=str(rs.id), error_code=e.code,
            )
            return Response({"ok": False, "detail": e.code}, status=e.http_status)

        job = outcome.job
        payload = {
            "status": "already_running" if outcome.already_running else "accepted",
            "job_id": str(job.id),
            "rag_space_id": str(rs.id),
            "total_documents": job.total_count,
            "queued_documents": job.total_count,
            "already_running": outcome.already_running,
        }
        if outcome.already_running:
            # Idempotent côté UI : on renvoie le job actif (409 = ne pas en relancer un second).
            payload["job"] = serialize_job(job)
            return Response(payload, status=409)

        emit_action_event(
            event_name="action.queued", action_name="ragspace.reindex", status="queued",
            actor=request.user, rag_space_id=str(rs.id), resource_type="reindex_job",
            resource_id=str(job.id), metadata={"total": job.total_count},
        )
        return Response(payload, status=202)


def _parse_strategy(data, *, field: str = "strategy", default: str | None = None) -> str:
    """Stratégie de reprise demandée, validée contre l'ALLOWLIST.

    Le défaut est `auto` : c'est la seule valeur qui tient compte de la cause de
    l'échec. Une chaîne inconnue est REFUSÉE plutôt que silencieusement ramenée à
    `standard` — sinon une faute de frappe produirait une campagne qui ne fait rien
    de ce qu'on croyait demander.
    """
    from apps.rag_core.services.ingest.retry_strategy import (
        REQUESTABLE_STRATEGIES,
        STRATEGY_AUTO,
    )

    if not isinstance(data, dict):
        return default or STRATEGY_AUTO
    raw = data.get(field)
    if raw is None:
        return default or STRATEGY_AUTO
    value = str(raw).strip().lower()
    if value not in REQUESTABLE_STRATEGIES:
        raise ValueError(f"{field} must be one of {sorted(REQUESTABLE_STRATEGIES)}")
    return value


class RagSpaceRetryFailedDocumentsView(APIView):
    """
    POST /api/v1/rag-spaces/<uuid>/retry-failed-documents/   (owner/admin uniquement)

    Reprise en masse des documents en **erreur** d'un espace. Crée un job durable du
    même type que la réindexation globale (même modèle, même worker, même suivi),
    mais dont le snapshot est **restreint** à une sélection.

    Ce n'est ni un upload ni une réindexation globale : aucun `Document` n'est créé,
    aucun fichier n'est renvoyé, aucun quota n'est consommé. Les documents traités
    conservent leur identifiant et leur fichier source.

    Corps — sélection EXPLICITE :
        {"selection": {"mode": "explicit", "document_ids": ["<uuid>", ...]}}

    Corps — sélection PAR FILTRE (tous les documents en erreur, moins des exclusions) :
        {"selection": {"mode": "filter", "search": "rapport",
                       "excluded_document_ids": ["<uuid>", ...]}}

    Le mode `filter` évite de faire transiter des centaines d'identifiants : le
    serveur reconstruit le périmètre avec **la même sémantique de recherche et de
    statut que la liste documentaire** (`api.v1.documents.listing`), sans pagination
    ni tri — une sélection « tous les résultats » ne dépend pas de la page affichée.

    Codes :
      - 202 {status: "accepted", job_id, kind, total_documents, skipped_documents} ;
      - 409 {status: "already_running", job_id, job} — un job est déjà actif sur
        l'espace (globale ou ciblée : l'exclusivité porte sur l'espace) ;
      - 409 {detail: "no_eligible_documents"} — plus rien à reprendre ;
      - 409 {detail: "selection_too_large"} ;
      - 400 {detail: "invalid_selection"} ; 403 non owner/admin ; 404 espace supprimé ;
      - 503 garde EAGER.
    """

    permission_classes = [IsAuthenticated]

    #: Borne de la liste d'identifiants acceptée dans un corps de requête. Au-delà,
    #: le mode `filter` est le bon outil — pas une requête HTTP démesurée.
    MAX_EXPLICIT_IDS = 2000

    def post(self, request, rag_space_id):
        from apps.audit.action_audit import emit_action_event
        from apps.rag_core.services.ingest.reindex_jobs import (
            ReindexJobError,
            create_failed_documents_retry_job,
            serialize_job,
        )
        from apps.rag_core.services.ingest.retry_strategy import extended_timeout_seconds

        from apps.workspaces.services import scope_audit
        scope_audit.emit_ragspace_decision(
            user=request.user, rag_space_id=rag_space_id, permission="manage",
            endpoint="ragspaces.retry_failed_documents", request=request,
        )
        rs = require_ragspace_access(user=request.user, rag_space_id=rag_space_id, permission="write")
        if getattr(rs, "deleted_at", None) is not None:
            return Response({"detail": "Not found"}, status=404)
        # Aligné sur la réindexation globale : une campagne de masse engage la charge
        # de tout l'espace. Le refus ne dit RIEN du contenu (ni compteurs, ni ids).
        if not _is_owner_admin(rs=rs, user=request.user):
            emit_action_event(
                event_name="action.denied", action_name="ragspace.retry_failed_documents",
                status="denied", actor=request.user, rag_space_id=str(rs.id),
                resource_type="rag_space", resource_id=str(rs.id),
            )
            return Response({"detail": "No access to this RAG space"}, status=403)

        try:
            selection = self._parse_selection(request.data)
            strategy = _parse_strategy(request.data)
        except ValueError as e:
            return Response({"detail": "invalid_selection", "error": str(e)}, status=400)

        try:
            outcome = create_failed_documents_retry_job(
                rag_space=rs,
                user=request.user,
                selection_mode=selection["mode"],
                extra_filter=selection["extra_filter"],
                document_ids=selection["document_ids"],
                excluded_document_ids=selection["excluded_document_ids"],
                requested_count=selection["requested_count"],
                selection_meta=selection["meta"],
                requested_strategy=strategy,
            )
        except ReindexJobError as e:
            emit_action_event(
                event_name="action.failed", action_name="ragspace.retry_failed_documents",
                status="failed", actor=request.user, rag_space_id=str(rs.id),
                resource_type="rag_space", resource_id=str(rs.id), error_code=e.code,
            )
            return Response({"ok": False, "detail": e.code}, status=e.http_status)

        job = outcome.job
        requested = selection["requested_count"]
        job_meta = job.selection_meta or {}
        payload = {
            "status": "already_running" if outcome.already_running else "accepted",
            "job_id": str(job.id),
            "rag_space_id": str(rs.id),
            "kind": job.kind,
            "requested_strategy": job.requested_strategy,
            "strategy_breakdown": job_meta.get("strategy_breakdown") or {},
            "skip_reasons": job_meta.get("skip_reasons") or {},
            "total_documents": job.total_count,
            # Écart entre le demandé et le retenu : documents devenus prêts, supprimés,
            # sans fichier source, ou déjà pris par un autre traitement depuis la
            # sélection. On le dit sans jamais nommer les documents concernés.
            "skipped_documents": max(0, (requested or 0) - job.total_count)
            if requested is not None and not outcome.already_running else 0,
            "already_running": outcome.already_running,
        }
        if outcome.already_running:
            payload["job"] = serialize_job(job)
            return Response(payload, status=409)

        emit_action_event(
            event_name="action.queued", action_name="ragspace.retry_failed_documents",
            status="queued", actor=request.user, workspace_id=str(rs.workspace_id),
            rag_space_id=str(rs.id), resource_type="reindex_job", resource_id=str(job.id),
            metadata={
                "kind": job.kind,
                "selection_mode": selection["mode"],
                "requested_count": requested,
                "eligible_count": job.total_count,
                "skipped_count": payload["skipped_documents"],
                # De quoi répondre, plus tard, à « pourquoi CE document a-t-il été
                # repris avec OCR ? » et « quel délai lui a été accordé ? ».
                "requested_strategy": job.requested_strategy,
                "effective_strategy_breakdown": payload["strategy_breakdown"],
                "retryability_breakdown": payload["skip_reasons"],
                "ocr_forced_count": int(payload["strategy_breakdown"].get("force_ocr", 0)),
                "non_retryable_count": job.skipped_count,
                "extended_timeout_seconds": (
                    extended_timeout_seconds()
                    if payload["strategy_breakdown"].get("extended_timeout") else None
                ),
            },
        )
        return Response(payload, status=202)

    def _parse_selection(self, data) -> dict:
        """Valide le corps et traduit la sélection en filtres. Lève `ValueError`.

        La sémantique de `search` et du statut « erreur » n'est PAS réécrite ici :
        elle est empruntée à `api.v1.documents.listing`, celle-là même qui alimente
        la liste. Deux implémentations finiraient par sélectionner autre chose que
        ce que l'écran affiche.
        """
        from api.v1.documents import listing

        if not isinstance(data, dict):
            raise ValueError("body must be an object")
        selection = data.get("selection")
        if not isinstance(selection, dict):
            raise ValueError("selection required")

        mode = str(selection.get("mode") or "").strip().lower()
        if mode not in ("explicit", "filter"):
            raise ValueError("mode must be 'explicit' or 'filter'")

        # `status` est facultatif ; s'il est fourni il ne peut valoir qu'« erreur ».
        # Cet endpoint ne reprend QUE des documents en échec : accepter une autre
        # valeur laisserait croire qu'il sait faire autre chose.
        status_key = str(selection.get("status") or listing.UI_ERROR).strip().lower()
        if status_key != listing.UI_ERROR:
            raise ValueError("status must be 'error'")

        excluded = self._parse_uuid_list(selection.get("excluded_document_ids"), "excluded_document_ids")

        if mode == "explicit":
            ids = self._parse_uuid_list(selection.get("document_ids"), "document_ids")
            if not ids:
                raise ValueError("document_ids required")
            return {
                "mode": mode,
                "extra_filter": None,
                "document_ids": ids,
                "excluded_document_ids": excluded,
                "requested_count": len(ids),
                "meta": {},
            }

        search = str(selection.get("search") or "").strip()[: listing.MAX_SEARCH_LENGTH]
        return {
            "mode": mode,
            "extra_filter": listing.search_filter(search),
            "document_ids": None,
            "excluded_document_ids": excluded,
            # En mode filtre, le « demandé » n'est connu qu'après évaluation : le
            # service posera `requested = eligible`, et `skipped` vaudra 0.
            "requested_count": None,
            "meta": {"search": search, "excluded_count": len(excluded)},
        }

    def _parse_uuid_list(self, raw, field: str) -> list:
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise ValueError(f"{field} must be a list")
        if len(raw) > self.MAX_EXPLICIT_IDS:
            raise ValueError(f"{field} too large")
        out = []
        for v in raw:
            try:
                out.append(uuid.UUID(str(v)))
            except (TypeError, ValueError):
                raise ValueError(f"{field} contains an invalid uuid") from None
        return out


class RagSpaceRetryFailedDocumentsPreviewView(RagSpaceRetryFailedDocumentsView):
    """
    POST /api/v1/rag-spaces/<uuid>/retry-failed-documents/preview/  (owner/admin)

    Répond « voici ce qui se passerait » sans que rien ne se passe : aucun job créé,
    aucune tâche mise en file, aucun fichier lu. Permet d'annoncer, avant
    confirmation, combien de documents relèvent de l'OCR, du délai étendu, ou
    d'aucune reprise — plutôt que de lancer une campagne pour le découvrir.

    Hérite de la vue de création pour partager EXACTEMENT ses permissions et son
    analyse de sélection ; la répartition vient de la même fonction `plan_retry` que
    la création. Deux chemins distincts finiraient par annoncer une chose et en
    exécuter une autre.
    """

    def post(self, request, rag_space_id):
        from apps.rag_core.services.ingest.reindex_jobs import plan_retry
        from apps.rag_core.services.ingest.retry_strategy import extended_timeout_seconds

        from apps.workspaces.services import scope_audit
        scope_audit.emit_ragspace_decision(
            user=request.user, rag_space_id=rag_space_id, permission="manage",
            endpoint="ragspaces.retry_failed_documents_preview", request=request,
        )
        rs = require_ragspace_access(user=request.user, rag_space_id=rag_space_id, permission="write")
        if getattr(rs, "deleted_at", None) is not None:
            return Response({"detail": "Not found"}, status=404)
        if not _is_owner_admin(rs=rs, user=request.user):
            return Response({"detail": "No access to this RAG space"}, status=403)

        try:
            selection = self._parse_selection(request.data)
            strategy = _parse_strategy(request.data)
        except ValueError as e:
            return Response({"detail": "invalid_selection", "error": str(e)}, status=400)

        plan = plan_retry(
            rag_space_id=rs.id,
            requested_strategy=strategy,
            extra_filter=selection["extra_filter"],
            document_ids=selection["document_ids"],
            excluded_document_ids=selection["excluded_document_ids"],
        )
        payload = plan.as_preview()
        payload["rag_space_id"] = str(rs.id)
        payload["extended_timeout_seconds"] = (
            extended_timeout_seconds()
            if payload["strategy_breakdown"].get("extended_timeout") else None
        )
        return Response(payload, status=200)


def _load_job_for_user(*, user, job_id):
    """Charge un job dont l'utilisateur a le droit d'écriture sur l'espace (ou None)."""
    from rest_framework.exceptions import PermissionDenied

    from apps.rag_core.models.reindex_job import RagSpaceReindexJob

    job = (
        RagSpaceReindexJob.objects.select_related("rag_space")
        .filter(id=job_id).first()
    )
    if job is None:
        return None
    from apps.workspaces.services import scope_audit
    scope_audit.emit_ragspace_decision(
        user=user, rag_space_id=str(job.rag_space_id), permission="manage",
        endpoint="ragspaces.reindex_job",
    )
    try:
        rs = require_ragspace_access(user=user, rag_space_id=str(job.rag_space_id), permission="write")
    except PermissionDenied:
        return None
    if getattr(rs, "deleted_at", None) is not None or not _is_owner_admin(rs=rs, user=user):
        return None
    return job


class RagSpaceReindexJobDetailView(APIView):
    """GET /api/v1/reindex-jobs/<job_id>/ — état/progression (polling frontend)."""

    permission_classes = [IsAuthenticated]

    def get(self, request, job_id):
        from apps.rag_core.services.ingest.reindex_jobs import serialize_job

        job = _load_job_for_user(user=request.user, job_id=job_id)
        if job is None:
            return Response({"detail": "Not found"}, status=404)
        return Response(serialize_job(job, with_errors=True), status=200)


class RagSpaceActiveReindexJobView(APIView):
    """GET /api/v1/rag-spaces/<id>/active-reindex-job/ — reprise du suivi après refresh."""

    permission_classes = [IsAuthenticated]

    def get(self, request, rag_space_id):
        from apps.rag_core.models.reindex_job import JOB_ACTIVE_STATUSES, RagSpaceReindexJob
        from apps.rag_core.services.ingest.reindex_jobs import serialize_job

        from apps.workspaces.services import scope_audit
        scope_audit.emit_ragspace_decision(
            user=request.user, rag_space_id=rag_space_id, permission="manage",
            endpoint="ragspaces.active_reindex_job", request=request,
        )
        rs = require_ragspace_access(user=request.user, rag_space_id=rag_space_id, permission="write")
        if getattr(rs, "deleted_at", None) is not None or not _is_owner_admin(rs=rs, user=request.user):
            return Response({"detail": "No access to this RAG space"}, status=403)
        job = (
            RagSpaceReindexJob.objects
            .filter(rag_space=rs, status__in=list(JOB_ACTIVE_STATUSES))
            .order_by("-created_at").first()
        )
        if job is None:
            return Response({"active": False, "job": None}, status=200)
        return Response({"active": True, "job": serialize_job(job)}, status=200)


class ReindexJobRetryFailedView(APIView):
    """POST /api/v1/reindex-jobs/<job_id>/retry-failed/ — nouveau job sur les échecs.

    Corps optionnel `{"strategy": "auto"}` : **sans** lui, la stratégie du job parent
    est CONSERVÉE (rejouer à l'identique) ; avec `auto`, elle est recalculée sur les
    codes d'erreur à jour — ceux que la tentative précédente vient d'écrire. Les deux
    gestes sont légitimes et l'API ne tranche pas à la place de l'utilisateur.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, job_id):
        from apps.audit.action_audit import emit_action_event
        from apps.rag_core.services.ingest.reindex_jobs import (
            ReindexJobError,
            retry_failed,
            serialize_job,
        )

        job = _load_job_for_user(user=request.user, job_id=job_id)
        if job is None:
            return Response({"detail": "Not found"}, status=404)

        try:
            # `default=None` : l'absence de champ signifie « garde celle du parent »,
            # pas « auto ». Un défaut différent changerait le comportement d'appels
            # existants sans qu'ils aient rien demandé.
            strategy = _parse_strategy(request.data, default=None) if request.data else None
        except ValueError as e:
            return Response({"detail": "invalid_strategy", "error": str(e)}, status=400)

        try:
            outcome = retry_failed(job=job, user=request.user, requested_strategy=strategy)
        except ReindexJobError as e:
            return Response({"ok": False, "detail": e.code}, status=e.http_status)

        new_job = outcome.job
        code = 409 if outcome.already_running else 202
        emit_action_event(
            event_name="action.queued", action_name="ragspace.reindex_retry", status="queued",
            actor=request.user, rag_space_id=str(new_job.rag_space_id),
            resource_type="reindex_job", resource_id=str(new_job.id),
            metadata={
                "parent_job_id": str(job.id),
                "requested_strategy": new_job.requested_strategy,
                "effective_strategy_breakdown":
                    (new_job.selection_meta or {}).get("strategy_breakdown") or {},
            },
        )
        return Response(
            {
                "status": "already_running" if outcome.already_running else "accepted",
                "job_id": str(new_job.id),
                "parent_job_id": str(job.id),
                "requested_strategy": new_job.requested_strategy,
                "already_running": outcome.already_running,
                "job": serialize_job(new_job),
            },
            status=code,
        )


class RagSpaceLLMSettingsTestView(APIView):
    """
    POST /api/v1/rag-spaces/<uuid>/llm-settings/test/
    Body: {"provider": "ollama", "model": "<optionnel>"}
    - owner/admin uniquement
    - test de connexion léger (pas de génération)
    - ne renvoie jamais de secret / payload sensible
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, rag_space_id):
        from apps.workspaces.services import scope_audit
        scope_audit.emit_ragspace_decision(
            user=request.user, rag_space_id=rag_space_id, permission="manage",
            endpoint="ragspaces.llm_settings_test", request=request,
        )
        rs = require_ragspace_access(user=request.user, rag_space_id=rag_space_id, permission="write")
        if not _is_owner_admin(rs=rs, user=request.user):
            return Response({"detail": "No access to this RAG space"}, status=403)

        data = request.data if isinstance(request.data, dict) else {}
        provider = str(data.get("provider") or rs.llm_provider or "ollama").strip().lower()
        model = data.get("model")
        if model is not None:
            model = str(model).strip()

        result = test_provider_connection(provider, model)
        return Response(result, status=200)

