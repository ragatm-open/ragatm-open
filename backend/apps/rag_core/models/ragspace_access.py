import uuid

from django.conf import settings
from django.db import models


class RagSpaceAccess(models.Model):
    """Accès explicite d'un utilisateur à un RagSpace, avec rôle d'espace.

    Sépare l'axe workspace (Membership.role) de l'axe espace (space_role). Un
    non-owner ne voit/ouvre un RagSpace que s'il possède une ligne ACTIVE ici
    (revoked_at IS NULL). L'owner du périmètre n'a PAS besoin de ligne : son
    accès découle de l'ownership (rag_space.owner_scope == son membership).

    Invariants (garantis au niveau service, cf. apps.workspaces.services.access) :
      - user membre du même workspace que rag_space ;
      - user.owner_scope == rag_space.owner_scope (même périmètre owner) ;
      - assigned_by appartient au même owner_scope ;
      - space_role plafonné par le rôle workspace (doctrine D1).
    """

    ROLE_MANAGER = "manager"
    ROLE_EDITOR = "editor"
    ROLE_VIEWER = "viewer"
    SPACE_ROLE_CHOICES = [
        (ROLE_MANAGER, "Manager"),
        (ROLE_EDITOR, "Editor"),
        (ROLE_VIEWER, "Viewer"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    rag_space = models.ForeignKey(
        "rag_core.RagSpace",
        on_delete=models.CASCADE,
        related_name="accesses",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="rag_space_accesses",
    )
    space_role = models.CharField(max_length=16, choices=SPACE_ROLE_CHOICES)

    # Qui a accordé l'accès (audit + invariant de périmètre). SET_NULL pour
    # survivre à la suppression de l'acteur.
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="granted_rag_space_accesses",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    # Retrait traçable et immédiat (préféré à un DELETE dur, pour l'audit).
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "rag_space_access"
        constraints = [
            # Une seule ligne ACTIVE par (rag_space, user). L'historique révoqué
            # (revoked_at renseigné) reste conservé et n'empêche pas un ré-octroi.
            models.UniqueConstraint(
                fields=["rag_space", "user"],
                condition=models.Q(revoked_at__isnull=True),
                name="uniq_active_ragspace_access_per_user",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "revoked_at"]),
            models.Index(fields=["rag_space", "revoked_at"]),
        ]

    def __str__(self) -> str:
        state = "revoked" if self.revoked_at else self.space_role
        return f"{self.user_id}@{self.rag_space_id} ({state})"

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None

    def clean(self) -> None:
        """Cohérence utilisateur↔périmètre↔RagSpace + plafond D1 (point de revue #5).

        Défense en profondeur au niveau modèle (via full_clean()). L'enforcement
        canonique reste le service, appelé dans la transaction de création/
        réactivation. N'est pas déclenché par un simple `.create()`.
        """
        from django.core.exceptions import ValidationError

        from apps.workspaces.services.access import validate_ragspace_access_invariants

        # Une ligne révoquée est un historique : ne pas re-valider les invariants.
        if self.revoked_at is not None:
            return
        try:
            validate_ragspace_access_invariants(
                user=self.user, rag_space=self.rag_space, space_role=self.space_role
            )
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
