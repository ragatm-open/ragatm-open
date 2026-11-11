"""Revalidation d'accès À L'EXÉCUTION des tâches asynchrones (Lot 3).

Une tâche autorisée à l'enqueue peut devenir illégitime avant son exécution :
accès révoqué, membership retiré, workspace désactivé, RagSpace supprimé ou
transféré de périmètre. Ce module revalide l'ÉTAT COURANT au démarrage de la
tâche et renvoie une décision + une **raison stable**.

Reconstruit le contexte depuis les données persistées du job (`rag_space` +
`requested_by`/`user`) — aucun changement de signature de tâche requis. La
comparaison courante `effective_owner_scope(actor) == rag_space.owner_scope`
capture aussi bien un transfert du RagSpace qu'un transfert de l'acteur.

Flag-gated : `flags OFF ⇒ (proceed=True, "flags_off")` (comportement historique).
`OWNER_SCOPE_AUDIT_ONLY ⇒` journalise mais laisse toujours passer (observation).
`OWNER_SCOPE_ENABLED ⇒` annule (proceed=False) sur refus.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from django.conf import settings
from django.contrib.auth import get_user_model

from apps.rag_core.models import RagSpace, RagSpaceAccess
from apps.workspaces.models import Membership
from apps.workspaces.services.access import (
    effective_owner_scope_pk,
    effective_ragspace_decision,
    resolve_acl_mode,
    simulated_ragspace_decision,
)

logger = logging.getLogger("owner_scope.audit")

# Raisons stables (observables / journalisées).
OK = "ok"
FLAGS_OFF = "flags_off"
CONTEXT_MISSING = "context_missing"
ACTOR_INACTIVE = "actor_inactive"
MEMBERSHIP_MISSING = "membership_missing"
WORKSPACE_DISABLED = "workspace_disabled"
RAGSPACE_DELETED = "ragspace_deleted"
RAGSPACE_WITHOUT_SCOPE = "ragspace_without_scope"
OWNER_SCOPE_MISMATCH = "owner_scope_mismatch"
OWNER_SCOPE_TRANSFERRED = "owner_scope_transferred"
ACCESS_MISSING = "access_missing"
ACCESS_REVOKED = "access_revoked"
INSUFFICIENT_SPACE_ROLE = "insufficient_space_role"

# Raisons « neutres » qui ne doivent jamais annuler (historique / non-décidable).
_NON_BLOCKING = {OK, FLAGS_OFF, CONTEXT_MISSING}


@dataclass(frozen=True)
class TaskAccessResult:
    """Verdict d'une revalidation.

    `ok`/`reason` = décision EFFECTIVE (celle qui annule ou non). Les champs
    `simulated_*` portent ce que l'ACL AURAIT décidé : ils servent à observer, et
    ne doivent jamais être lus pour décider — les confondre est le défaut P0-1.
    """

    ok: bool
    reason: str
    acl_mode: str = "disabled"
    simulated_ok: bool | None = None
    simulated_reason: str | None = None


def _owner_scope_considered() -> bool:
    return bool(
        getattr(settings, "OWNER_SCOPE_ENABLED", False)
        or getattr(settings, "OWNER_SCOPE_AUDIT_ONLY", False)
    )


def revalidate_task_access(
    *, user_id, rag_space_id, required_permission: str = "read", enqueue_owner_scope_id=None,
    require_context: bool = False,
) -> TaskAccessResult:
    """Revalide l'état COURANT. Ne lève jamais. Retourne (ok, raison stable).

    `require_context=True` (tâches sensibles type ingestion) : un contexte absent
    (acteur non identifiable) devient un REFUS (`ok=False`) — sous enforcement,
    une telle tâche ne doit pas continuer silencieusement. Sinon (`False`, tâches
    reconstruisant leur contexte depuis un job), un contexte absent est neutre.
    """
    if not _owner_scope_considered():
        return TaskAccessResult(True, FLAGS_OFF)
    if user_id is None or rag_space_id is None:
        # Contexte absent : bloquant si require_context, neutre sinon.
        return TaskAccessResult(not require_context, CONTEXT_MISSING)

    user = get_user_model().objects.filter(id=user_id).first()
    if user is None or not user.is_active:
        return TaskAccessResult(False, ACTOR_INACTIVE)

    rs = RagSpace.objects.select_related("workspace").filter(id=rag_space_id).first()
    if rs is None or rs.deleted_at is not None:
        return TaskAccessResult(False, RAGSPACE_DELETED)
    if not rs.workspace.is_active:
        return TaskAccessResult(False, WORKSPACE_DISABLED)

    membership = Membership.objects.filter(workspace=rs.workspace, user=user).first()
    if membership is None:
        return TaskAccessResult(False, MEMBERSHIP_MISSING)
    if rs.owner_scope_id is None:
        return TaskAccessResult(False, RAGSPACE_WITHOUT_SCOPE)

    eff = effective_owner_scope_pk(membership)
    if eff != rs.owner_scope_id:
        # Distingue un transfert du RagSpace (l'acteur est resté dans le scope
        # d'enqueue, mais l'espace a bougé) d'un simple non-appariement.
        if enqueue_owner_scope_id is not None and eff == enqueue_owner_scope_id:
            return TaskAccessResult(False, OWNER_SCOPE_TRANSFERRED)
        return TaskAccessResult(False, OWNER_SCOPE_MISMATCH)

    # Décision EFFECTIVE — la même que celle du chemin HTTP. Auparavant, cette
    # ligne appelait `target_ragspace_decision`, c'est-à-dire la SIMULATION : en
    # `RAGSPACE_ACL_AUDIT_ONLY`, un refus simulé devenait une annulation réelle de
    # tâche. L'ingestion des non-owners était coupée par une ACL censée observer.
    ok, reason = effective_ragspace_decision(
        user=user, rag_space=rs, permission=required_permission, membership=membership
    )

    # Observation : ce que l'ACL aurait décidé. Calculée SÉPARÉMENT et n'influence
    # jamais `ok` — c'est toute la valeur du mode audit : voir sans subir.
    acl_mode = resolve_acl_mode()
    sim_ok, sim_reason = simulated_ragspace_decision(
        user=user, rag_space=rs, permission=required_permission, membership=membership
    )

    def _result(effective_ok: bool, effective_reason: str) -> TaskAccessResult:
        return TaskAccessResult(
            effective_ok, effective_reason,
            acl_mode=acl_mode.value, simulated_ok=sim_ok, simulated_reason=sim_reason,
        )

    if ok:
        return _result(True, OK)
    if reason == "access_missing":
        revoked = RagSpaceAccess.objects.filter(
            rag_space=rs, user=user, revoked_at__isnull=False
        ).exists()
        return _result(False, ACCESS_REVOKED if revoked else ACCESS_MISSING)
    if reason == "space_role_insufficient":
        return _result(False, INSUFFICIENT_SPACE_ROLE)
    return _result(False, reason)


def enforce_or_log(
    *, user_id, rag_space_id, required_permission: str = "read", job_label: str,
    enqueue_owner_scope_id=None, require_context: bool = False, request_id: str | None = None,
) -> tuple[bool, str]:
    """Revalide + journalise (1 événement) et décide si la tâche doit continuer.

    CONTRAT (testé, cf. tests/tests_root/http_api/test_acl_mode_matrix.py) :

    - `RAGSPACE_ACL_AUDIT_ONLY` **n'annule JAMAIS** une tâche. Un refus ACL y est
      seulement observé (`simulated_decision=deny`, `task_continued=true`).
    - Seul `RAGSPACE_ACL_ENABLED` sans audit-only produit un refus ACL effectif.
    - Les autres couches (périmètre owner, membership, workspace désactivé,
      espace supprimé) continuent de bloquer, quel que soit le mode ACL.
    - Le contrat est IDENTIQUE à celui du chemin HTTP `require_ragspace_access` :
      la revalidation asynchrone ne réinterprète aucun drapeau.

    `proceed=False` uniquement sous `OWNER_SCOPE_ENABLED` et refus EFFECTIF.
    `require_context=True` fait de l'absence de contexte un refus.
    """
    res = revalidate_task_access(
        user_id=user_id, rag_space_id=rag_space_id,
        required_permission=required_permission, enqueue_owner_scope_id=enqueue_owner_scope_id,
        require_context=require_context,
    )
    if _owner_scope_considered():
        logger.info(
            "owner_scope_audit %s",
            json.dumps(
                {
                    "event": "task_revalidation",
                    "job_label": job_label,
                    "request_id": request_id,
                    "user_id": user_id,
                    "rag_space_id": str(rag_space_id) if rag_space_id is not None else None,
                    "required_permission": required_permission,
                    # `decision`/`reason` = ce qui s'applique réellement. Les
                    # champs `simulated_*` disent ce que l'ACL aurait fait : sans
                    # cette séparation, un événement « deny » était émis alors que
                    # l'action était en fait autorisée.
                    "decision": "allow" if res.ok else "deny",
                    "reason": res.reason,
                    "acl_mode": res.acl_mode,
                    "simulated_decision": (
                        None if res.simulated_ok is None
                        else ("allow" if res.simulated_ok else "deny")
                    ),
                    "simulated_reason": res.simulated_reason,
                    "acl_would_have_denied": bool(
                        res.acl_mode == "audit_only" and res.simulated_ok is False and res.ok
                    ),
                    "task_continued": bool(
                        res.ok or not getattr(settings, "OWNER_SCOPE_ENABLED", False)
                    ),
                },
                default=str,
                sort_keys=True,
            ),
        )
    # `ok` vaut déjà True pour OK/FLAGS_OFF et pour un contexte absent NON requis :
    # seul un refus réel (ok=False) annule sous enforcement.
    if getattr(settings, "OWNER_SCOPE_ENABLED", False) and not res.ok:
        return (False, res.reason)  # ANNULER : ni retry, ni résultat partiel
    return (True, res.reason)
