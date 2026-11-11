"""Procédure opérateur : relever au défaut COURANT les quotas restés à un ancien défaut.

Le défaut de plateforme est copié dans ``WorkspaceQuota`` à la création et n'est
jamais rétroactif. Relever le défaut (1 000 → 5 000) laisse donc les workspaces
existants à l'ancienne valeur. Cette commande les rattrape — mais SEULEMENT ceux
dont l'origine « défaut standard » est prouvée, parce qu'un 1 000 peut aussi être un
choix délibéré d'un superuser.

Origine prouvée = les trois conditions à la fois :
  - ``max_document_count`` vaut exactement l'ancien défaut (``--from-count``) ;
  - la ligne n'a jamais été modifiée (``updated_at`` ≈ ``created_at``) ;
  - aucun événement ``workspace.quota.created`` ni ``workspace.quota.updated`` n'existe
    pour ce workspace. La création par la console plateforme et toute modification
    sont auditées ; seuls ``ensure_quota`` sans argument (création paresseuse,
    bootstrap) et le backfill de la migration créent un quota sans audit, et ils
    appliquent toujours le défaut du moment.

Tout autre cas est PRÉSERVÉ et signalé. Seul le nombre de documents change : la
limite en octets n'est jamais touchée. Chaque relèvement est audité
(``workspace.quota.updated``), comme une modification depuis la console.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.audit.models import ActionAuditEvent
from apps.workspaces.models import WorkspaceQuota
from apps.workspaces.services import quota as quota_service

#: Écart toléré entre ``created_at`` et ``updated_at`` d'une ligne jamais modifiée
#: (``auto_now_add`` et ``auto_now`` sont horodatés séparément à l'INSERT).
_UNTOUCHED_TOLERANCE = timedelta(seconds=1)

CANDIDATE = "candidat"
NOT_OLD_DEFAULT = "préservé:valeur_différente"
MODIFIED = "préservé:ligne_modifiée"
AUDITED = "préservé:origine_auditée"


def classify(quota: WorkspaceQuota, *, from_count: int, audited_ids: set[str]) -> str:
    if int(quota.max_document_count) != from_count:
        return NOT_OLD_DEFAULT
    if abs(quota.updated_at - quota.created_at) > _UNTOUCHED_TOLERANCE:
        return MODIFIED
    if str(quota.workspace_id) in audited_ids:
        return AUDITED
    return CANDIDATE


class Command(BaseCommand):
    help = "Relève au défaut courant les quotas documentaires restés à l'ancien défaut (origine prouvée)."

    def add_arguments(self, parser):
        parser.add_argument("--from-count", type=int, required=True, help="Ancien défaut (ex. 1000).")
        parser.add_argument("--workspace", type=int, action="append", default=[],
                            help="Restreindre à ce workspace (répétable).")
        parser.add_argument("--reason", required=True, help="Référence du lot / motif (audité).")
        parser.add_argument("--apply", action="store_true", help="Sans ce drapeau : inventaire seulement.")

    def handle(self, *args, **opts):
        from_count = int(opts["from_count"])
        to_count = quota_service.default_max_document_count()
        if to_count <= from_count:
            raise CommandError(f"défaut courant {to_count} ≤ ancien défaut {from_count} : rien à relever.")
        if to_count > quota_service.hard_max_document_count():
            raise CommandError(f"défaut courant {to_count} au-delà du plafond absolu.")

        qs = WorkspaceQuota.objects.select_related("workspace").order_by("workspace_id")
        if opts["workspace"]:
            qs = qs.filter(workspace_id__in=opts["workspace"])
        audited_ids = set(
            ActionAuditEvent.objects.filter(
                action_name__in=("workspace.quota.created", "workspace.quota.updated")
            ).values_list("workspace_id", flat=True)
        )

        rows = [(q, classify(q, from_count=from_count, audited_ids=audited_ids)) for q in qs]
        self.stdout.write(f"défaut courant={to_count} ancien={from_count} quotas={len(rows)}")
        for q, verdict in rows:
            self.stdout.write(
                f"  workspace={q.workspace_id} count={q.max_document_count} "
                f"bytes={q.max_document_bytes} -> {verdict}"
            )
        candidates = [q for q, v in rows if v == CANDIDATE]
        self.stdout.write(f"candidats={len(candidates)} préservés={len(rows) - len(candidates)}")
        if not opts["apply"]:
            self.stdout.write("Inventaire seulement — relancer avec --apply pour relever les candidats.")
            return

        upgraded = 0
        for q in candidates:
            with transaction.atomic():
                locked = WorkspaceQuota.objects.select_for_update().select_related("workspace").get(pk=q.pk)
                # Revérifié sous verrou : une modification concurrente préserve la ligne.
                if classify(locked, from_count=from_count, audited_ids=audited_ids) != CANDIDATE:
                    continue
                old_count = int(locked.max_document_count)
                locked.max_document_count = to_count
                locked.save(update_fields=["max_document_count", "updated_at"])
            usage = quota_service.get_usage(locked.workspace, quota=locked)
            quota_service.audit_quota_updated(
                actor=None, workspace=locked.workspace,
                old_count=old_count, new_count=to_count,
                old_bytes=int(locked.max_document_bytes), new_bytes=int(locked.max_document_bytes),
                reason=f"upgrade_default_workspace_quota: {opts['reason']}",
                is_exceeded=bool(usage.get("is_exceeded")),
            )
            upgraded += 1
        self.stdout.write(self.style.SUCCESS(f"{upgraded} quota(s) relevé(s) à {to_count}."))
