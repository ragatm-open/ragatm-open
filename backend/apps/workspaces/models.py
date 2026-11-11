import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

class Workspace(models.Model):
    name = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)

    # Désactivation douce (jamais de hard-delete d'un tenant). Un workspace
    # `is_active=False` est explicitement HORS périmètre d'enforcement (règle
    # documentée). Défaut True → aucun impact sur l'existant. L'enforcement réel
    # (blocage d'accès aux workspaces désactivés) est branché dans un lot ultérieur.
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "workspace"

    def __str__(self) -> str:
        return f"{self.name} ({self.id})"

class Membership(models.Model):
    ROLE_OWNER = "owner"
    ROLE_ADMIN = "admin"
    ROLE_MEMBER = "member"
    ROLE_VIEWER = "viewer"

    ROLE_CHOICES = [
        (ROLE_OWNER, "Owner"),
        (ROLE_ADMIN, "Admin"),
        (ROLE_MEMBER, "Member"),
        (ROLE_VIEWER, "Viewer"),
    ]

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default=ROLE_MEMBER)
    created_at = models.DateTimeField(auto_now_add=True)

    # --- Périmètre owner (owner_scope) -------------------------------------
    # Rattachement stable d'un membership NON-owner au membership `owner` qui
    # gouverne son périmètre à l'intérieur du workspace. Source de vérité de
    # l'autorisation (ne PAS déduire du seul created_by).
    #
    # Convention (documentée) :
    #   - membership `owner`               -> owner_scope = NULL. Un owner EST
    #     son propre périmètre ; ne pas pointer vers soi (évite l'écriture en
    #     deux temps et l'auto-référence). Cf. get_owner_scope_for_membership().
    #   - membership admin/member/viewer   -> owner_scope OBLIGATOIRE (quand
    #     OWNER_SCOPE_ENABLED), pointe un membership `owner` du MÊME workspace.
    #
    # Invariants (garantis au niveau service, cf. apps.workspaces.services.access) :
    #   owner_scope.workspace == self.workspace  ;  owner_scope.role == owner
    #   ;  owner_scope != self.
    # Nullable pour permettre le backfill progressif derrière feature flag.
    owner_scope = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="scoped_memberships",
    )

    class Meta:
        db_table = "workspace_membership"
        constraints = [
            models.UniqueConstraint(fields=["workspace", "user"], name="uniq_workspace_user_membership")
        ]

    def __str__(self) -> str:
        return f"{self.user_id} in {self.workspace_id} ({self.role})"

    @property
    def is_owner(self) -> bool:
        return self.role == self.ROLE_OWNER

    def clean(self) -> None:
        """Invariants de périmètre au niveau modèle (points de revue #2/#3).

        Défense en profondeur — indépendante de l'API, disponible via full_clean()
        (admin Django, forms, appels explicites service). Self-contained (pas
        d'import du service) pour éviter tout cycle. Ne bloque JAMAIS un ORM
        `.create()`/backfill de migration (clean() n'y est pas appelé).

        Toujours refusé : owner portant un owner_scope ; owner_scope visant un
        non-owner, un autre workspace, ou soi-même. « non-owner sans owner_scope »
        n'est refusé QUE sous OWNER_SCOPE_ENABLED (toléré avant le backfill).
        """
        from django.conf import settings as _settings
        from django.core.exceptions import ValidationError

        scope = self.owner_scope
        if self.role == self.ROLE_OWNER:
            if scope is not None:
                raise ValidationError({"owner_scope": "Un membership owner ne porte pas d'owner_scope (il EST son périmètre)."})
            return
        if scope is None:
            if getattr(_settings, "OWNER_SCOPE_ENABLED", False):
                raise ValidationError({"owner_scope": "Un membership non-owner doit avoir un owner_scope."})
            return
        if scope.pk == self.pk:
            raise ValidationError({"owner_scope": "owner_scope ne peut pas être le membership lui-même."})
        if scope.role != self.ROLE_OWNER:
            raise ValidationError({"owner_scope": "owner_scope doit référencer un membership de rôle owner."})
        if scope.workspace_id != self.workspace_id:
            raise ValidationError({"owner_scope": "owner_scope doit appartenir au même workspace."})


class WorkspaceInvitation(models.Model):
    """Invitation d'un email à rejoindre un workspace avec un rôle donné.

    Domaine DÉDIÉ, distinct du reset password. Le token brut n'est JAMAIS stocké :
    seule son empreinte SHA-256 (``token_hash``) l'est. Le membership n'est créé
    qu'à l'acceptation. Le statut est DÉRIVÉ (jamais fourni par le client) à partir
    de ``accepted_at`` / ``revoked_at`` / ``expires_at``.
    """

    STATUS_PENDING = "pending"
    STATUS_EXPIRED = "expired"
    STATUS_ACCEPTED = "accepted"
    STATUS_REVOKED = "revoked"

    # Rôles attribuables par invitation : owner est exclu par construction
    # (garanti aussi par une CheckConstraint DB).
    ROLE_CHOICES = [c for c in Membership.ROLE_CHOICES if c[0] != Membership.ROLE_OWNER]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        Workspace, on_delete=models.CASCADE, related_name="invitations"
    )
    email_normalized = models.EmailField()
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default=Membership.ROLE_MEMBER)
    # SHA-256 hexdigest = 64 caractères. Unique : deux invitations ne peuvent pas
    # partager un même token.
    token_hash = models.CharField(max_length=64, unique=True)
    generation = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_workspace_invitations",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="accepted_workspace_invitations",
    )
    revoked_at = models.DateTimeField(null=True, blank=True)
    last_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "workspace_invitation"
        constraints = [
            # Au plus UNE invitation en attente (ni acceptée ni révoquée) par
            # couple (workspace, email). Contrainte partielle Postgres.
            models.UniqueConstraint(
                fields=["workspace", "email_normalized"],
                condition=models.Q(accepted_at__isnull=True, revoked_at__isnull=True),
                name="uniq_pending_invitation_per_workspace_email",
            ),
            # Le rôle owner ne peut jamais être porté par une invitation.
            models.CheckConstraint(
                condition=~models.Q(role=Membership.ROLE_OWNER),
                name="invitation_role_not_owner",
            ),
            # accepted_at et revoked_at ne sont jamais renseignés simultanément.
            models.CheckConstraint(
                condition=~(
                    models.Q(accepted_at__isnull=False)
                    & models.Q(revoked_at__isnull=False)
                ),
                name="invitation_accepted_xor_revoked",
            ),
        ]
        indexes = [
            models.Index(fields=["workspace", "email_normalized"]),
        ]

    def status(self, *, now=None) -> str:
        """Statut dérivé : pending / expired / accepted / revoked."""
        if self.accepted_at is not None:
            return self.STATUS_ACCEPTED
        if self.revoked_at is not None:
            return self.STATUS_REVOKED
        now = now or timezone.now()
        if self.expires_at is not None and self.expires_at <= now:
            return self.STATUS_EXPIRED
        return self.STATUS_PENDING

    @property
    def is_pending(self) -> bool:
        return self.status() == self.STATUS_PENDING

    def __str__(self) -> str:
        return f"invite<{self.email_normalized} → {self.workspace_id} ({self.role})>"


class WorkspaceQuota(models.Model):
    """Quotas documentaires d'un Workspace : nombre de fichiers + octets cumulés.

    Les valeurs sont COPIÉES depuis les défauts de plateforme à la création du
    workspace ; une modification ultérieure des défauts n'a AUCUN effet rétroactif
    (chaque workspace porte ses propres limites). Les deux limites sont
    STRICTEMENT positives (CheckConstraint SQL) — pas de mode « illimité » dans ce
    lot, donc jamais de NULL ambigu.

    L'USAGE réel (nombre de documents, octets consommés) n'est PAS stocké ici : il
    est calculé à la demande par agrégat sur ``Document`` (cf.
    ``apps.workspaces.services.quota``), aucune table compteur à réconcilier. La
    ligne sert néanmoins de point de VERROU (``select_for_update``) pour sérialiser
    les uploads concurrents d'un même workspace, et prépare l'ajout ultérieur de
    réservations transactionnelles.
    """

    workspace = models.OneToOneField(
        Workspace, on_delete=models.CASCADE, related_name="quota"
    )
    max_document_count = models.PositiveIntegerField()
    max_document_bytes = models.BigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "workspace_quota"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(max_document_count__gt=0),
                name="workspace_quota_count_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(max_document_bytes__gt=0),
                name="workspace_quota_bytes_positive",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"quota<ws={self.workspace_id} "
            f"count={self.max_document_count} bytes={self.max_document_bytes}>"
        )
