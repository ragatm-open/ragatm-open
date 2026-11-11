"""Politique de rétention des journaux d'audit — source unique (Lot 4-D).

QUATRE CATÉGORIES, QUATRE HORIZONS
----------------------------------
Toutes les traces n'ont ni la même sensibilité ni la même utilité dans le temps.
Les confondre sous une durée unique conduit soit à garder trop longtemps du
contenu conversationnel, soit à détruire trop tôt une preuve de sécurité.

===========================  ==========================================  =========
Catégorie                    Contenu                                     Défaut
===========================  ==========================================  =========
``conversational``           question, réponse, extraits de sources      90 j
``functional_audit``         action documentaire, acteur, ressource      365 j
``security_audit``           egress externe, changement de politique     365 j
``technical``                (aucun modèle dédié à ce jour)              30 j
===========================  ==========================================  =========

``RagQueryLog`` est classé **conversationnel** et non « audit » : il porte la
question et la réponse en clair. C'est la donnée la plus sensible de la
plateforme, et elle ne mérite pas l'horizon long d'un journal d'audit.

PLANCHER SUR L'AUDIT DE SÉCURITÉ
--------------------------------
``security_audit`` ne peut pas descendre sous :data:`SECURITY_AUDIT_MIN_DAYS`.
Un opérateur qui met cette rétention à 7 jours — par inadvertance ou pour faire
de la place — effacerait la trace des egress externes et des ouvertures de
politique, c'est-à-dire exactement ce qu'on voudrait relire après un incident.
Une valeur inférieure est refusée par un system check **bloquant**.

BORNES D'EXÉCUTION
------------------
La purge est **bornée par lots** : une suppression non bornée qui rencontre un an
d'arriéré tiendrait une transaction ouverte plusieurs minutes et bloquerait les
écritures concurrentes. Une exécution interrompue reprend proprement au passage
suivant — la purge est idempotente par construction (elle ne supprime que ce qui
est déjà expiré).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable

from django.conf import settings
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

#: Plancher NON NÉGOCIABLE de rétention des audits de sécurité (jours).
SECURITY_AUDIT_MIN_DAYS = 90

CATEGORY_CONVERSATIONAL = "conversational"
CATEGORY_FUNCTIONAL_AUDIT = "functional_audit"
CATEGORY_SECURITY_AUDIT = "security_audit"
CATEGORY_TECHNICAL = "technical"


@dataclass(frozen=True)
class RetentionRule:
    """Une règle de rétention applicable à un modèle."""

    key: str
    category: str
    setting_name: str
    default_days: int
    #: Fabrique du modèle — importée tardivement (les modèles ne sont pas prêts
    #: au chargement du module).
    model_loader: Callable[[], Any]
    timestamp_field: str = "created_at"
    label: str = ""

    def days(self) -> int:
        return int(getattr(settings, self.setting_name, self.default_days))

    def model(self) -> Any:
        return self.model_loader()


def _m_action_audit():
    from apps.audit.models import ActionAuditEvent

    return ActionAuditEvent


def _m_rag_query_log():
    from apps.audit.models import RagQueryLog

    return RagQueryLog


def _m_llm_egress():
    from apps.audit.models import LlmEgressAuditEvent

    return LlmEgressAuditEvent


def _m_egress_policy():
    from apps.audit.models import EgressPolicyChangeLog

    return EgressPolicyChangeLog


#: Les règles applicables. Ajouter un modèle d'audit sans l'inscrire ici revient à
#: le conserver indéfiniment — le system check `ragatm.W010` le signale.
RULES: tuple[RetentionRule, ...] = (
    RetentionRule(
        key="rag_query_log",
        category=CATEGORY_CONVERSATIONAL,
        setting_name="RAG_QUERY_LOG_RETENTION_DAYS",
        default_days=90,
        model_loader=_m_rag_query_log,
        label="RagQueryLog (question + réponse en clair)",
    ),
    RetentionRule(
        key="action_audit",
        category=CATEGORY_FUNCTIONAL_AUDIT,
        setting_name="ACTION_AUDIT_RETENTION_DAYS",
        default_days=365,
        model_loader=_m_action_audit,
        label="ActionAuditEvent (actions documentaires)",
    ),
    RetentionRule(
        key="llm_egress_audit",
        category=CATEGORY_SECURITY_AUDIT,
        setting_name="LLM_EGRESS_AUDIT_RETENTION_DAYS",
        default_days=365,
        model_loader=_m_llm_egress,
        label="LlmEgressAuditEvent (sorties vers un tiers)",
    ),
    RetentionRule(
        key="egress_policy_change",
        category=CATEGORY_SECURITY_AUDIT,
        setting_name="EGRESS_POLICY_CHANGE_RETENTION_DAYS",
        default_days=1095,
        model_loader=_m_egress_policy,
        label="EgressPolicyChangeLog (ouvertures de politique)",
    ),
)


def rule_by_key(key: str) -> RetentionRule | None:
    for r in RULES:
        if r.key == key:
            return r
    return None


def purge_rule(
    rule: RetentionRule,
    *,
    apply: bool = False,
    now=None,
    batch_size: int | None = None,
    max_rows: int | None = None,
) -> dict[str, Any]:
    """Purge une règle, par lots bornés. Ne supprime rien si ``apply=False``.

    Retourne un dict de compteurs : ``eligible``, ``deleted``, ``batches``,
    ``truncated`` (plafond atteint — il reste du travail pour le prochain run).
    """
    now = now or timezone.now()
    days = rule.days()
    batch = int(
        batch_size
        if batch_size is not None
        else getattr(settings, "AUDIT_PURGE_BATCH_SIZE", 1000)
    )
    cap = int(
        max_rows
        if max_rows is not None
        else getattr(settings, "AUDIT_PURGE_MAX_ROWS_PER_RUN", 50000)
    )

    model = rule.model()
    cutoff = now - timedelta(days=days)
    flt = {f"{rule.timestamp_field}__lt": cutoff}
    eligible = model.objects.filter(**flt).count()

    result: dict[str, Any] = {
        "key": rule.key,
        "category": rule.category,
        "days": days,
        "cutoff": cutoff.isoformat(),
        "eligible": eligible,
        "deleted": 0,
        "batches": 0,
        "truncated": False,
    }

    # Anomalie de données : des lignes horodatées dans le FUTUR ne seront jamais
    # purgées et faussent tout raisonnement de rétention. On les signale sans
    # jamais les supprimer — leur origine (horloge décalée, import) doit être
    # comprise avant d'y toucher.
    future = model.objects.filter(**{f"{rule.timestamp_field}__gt": now}).count()
    if future:
        result["future_dated"] = future
        logger.warning(
            "retention.future_dated_rows key=%s count=%d (jamais purgées)",
            rule.key, future,
        )

    if not apply or eligible == 0:
        return result

    deleted_total = 0
    while deleted_total < min(eligible, cap):
        ids = list(
            model.objects.filter(**flt)
            .order_by(rule.timestamp_field)
            .values_list("pk", flat=True)[:batch]
        )
        if not ids:
            break
        # Transaction PAR LOT : une interruption laisse une base cohérente, et
        # aucun verrou n'est tenu plus longtemps que la suppression d'un lot.
        with transaction.atomic():
            n = model.objects.filter(pk__in=ids).delete()[0]
        deleted_total += n
        result["batches"] += 1
        if n == 0:
            break

    result["deleted"] = deleted_total
    result["truncated"] = deleted_total >= cap and eligible > cap
    if result["truncated"]:
        logger.warning(
            "retention.truncated key=%s deleted=%d eligible=%d "
            "(plafond AUDIT_PURGE_MAX_ROWS_PER_RUN atteint ; reprise au prochain run)",
            rule.key, deleted_total, eligible,
        )
    return result


def purge_all(
    *, apply: bool = False, now=None, only: str | None = None, **kw: Any
) -> dict[str, Any]:
    """Applique toutes les règles. ``only`` restreint à une clé."""
    rules = RULES if not only else tuple(r for r in RULES if r.key == only)
    results = [purge_rule(r, apply=apply, now=now, **kw) for r in rules]
    return {
        "apply": apply,
        "results": results,
        "deleted_total": sum(r["deleted"] for r in results),
        "eligible_total": sum(r["eligible"] for r in results),
    }


def validate_settings() -> list[str]:
    """Retourne la liste des erreurs de configuration de rétention.

    Utilisé par le system check ``ragatm.E010`` — donc **bloquant** au démarrage.
    """
    errors: list[str] = []
    for rule in RULES:
        raw = getattr(settings, rule.setting_name, rule.default_days)
        try:
            days = int(raw)
        except (TypeError, ValueError):
            errors.append(f"{rule.setting_name}={raw!r} n'est pas un entier.")
            continue
        if days <= 0:
            errors.append(
                f"{rule.setting_name}={days} : une rétention nulle ou négative "
                f"purgerait immédiatement {rule.label}."
            )
        elif rule.category == CATEGORY_SECURITY_AUDIT and days < SECURITY_AUDIT_MIN_DAYS:
            errors.append(
                f"{rule.setting_name}={days} est sous le plancher de "
                f"{SECURITY_AUDIT_MIN_DAYS} jours imposé aux audits de SÉCURITÉ "
                f"({rule.label})."
            )

    batch = getattr(settings, "AUDIT_PURGE_BATCH_SIZE", 1000)
    cap = getattr(settings, "AUDIT_PURGE_MAX_ROWS_PER_RUN", 50000)
    try:
        if int(batch) <= 0:
            errors.append(f"AUDIT_PURGE_BATCH_SIZE={batch} doit être > 0.")
        if int(cap) <= 0:
            errors.append(f"AUDIT_PURGE_MAX_ROWS_PER_RUN={cap} doit être > 0.")
        elif int(cap) < int(batch):
            errors.append(
                f"AUDIT_PURGE_MAX_ROWS_PER_RUN={cap} < AUDIT_PURGE_BATCH_SIZE={batch} : "
                "le plafond serait atteint dès le premier lot."
            )
    except (TypeError, ValueError):
        errors.append("AUDIT_PURGE_BATCH_SIZE / AUDIT_PURGE_MAX_ROWS_PER_RUN non entiers.")
    return errors
