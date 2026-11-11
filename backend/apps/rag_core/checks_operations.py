"""System checks d'exploitation — ordonnancement et rétention (Lot 4-E).

Ces contrôles s'exécutent à chaque ``manage.py check`` (donc à chaque démarrage,
et en CI). Une configuration d'exploitation incohérente **bloque le démarrage**
plutôt que de produire un service qui semble fonctionner mais n'ordonnance rien
ou purge trop tôt.

CE QUE CES CHECKS NE FONT PAS
-----------------------------
Ils ne vérifient **pas** qu'un processus Beat tourne. Un system check Django
s'exécute dans le processus web : il n'a aucun moyen honnête de savoir si un
conteneur `celery_beat` existe ailleurs. Prétendre le contrôler ici donnerait une
fausse assurance. Cette vérification appartient au **healthcheck runtime**, qui
lit le heartbeat (cf. ``apps.rag_core.services.beat_heartbeat``).

IDENTIFIANTS
------------
=============  ==========  =============================================
ID             Niveau      Condition
=============  ==========  =============================================
ragatm.E010    Error       Rétention invalide (négative, nulle, sous le
                           plancher de sécurité, bornes incohérentes)
ragatm.E011    Error       Schedule Beat incohérent (tâche non importable,
                           fréquence invalide, queue inconnue)
ragatm.E012    Error       Broker Celery absent alors qu'un schedule est
                           déclaré hors tests
ragatm.W010    Warning     Modèle d'audit sans règle de rétention
ragatm.W011    Warning     Purge de rétention désactivée
=============  ==========  =============================================
"""
from __future__ import annotations

from django.core.checks import Error, Warning as DjangoWarning

#: Queues effectivement consommées par un worker déclaré dans les composes
#: officiels. Router une tâche vers une queue que personne ne consomme la rend
#: invisible : elle s'empile dans Redis sans jamais s'exécuter, et rien ne le dit.
KNOWN_QUEUES = frozenset({"celery", "analytics", "reindex", "maintenance"})


def check_audit_retention(app_configs, **kwargs):
    """ragatm.E010 — la politique de rétention doit être exploitable."""
    from django.conf import settings

    from apps.audit import retention

    problems = []
    for msg in retention.validate_settings():
        problems.append(
            Error(
                f"Rétention d'audit invalide : {msg}",
                hint=(
                    "Voir docs/07-operations/retention-audits.md. Les audits de "
                    "SÉCURITÉ ne peuvent pas descendre sous "
                    f"{retention.SECURITY_AUDIT_MIN_DAYS} jours."
                ),
                id="ragatm.E010",
            )
        )

    if not bool(getattr(settings, "AUDIT_PURGE_ENABLED", True)):
        problems.append(
            DjangoWarning(
                "AUDIT_PURGE_ENABLED=0 : la purge de rétention est SUSPENDUE.",
                hint=(
                    "Les journaux continuent de croître et la politique de "
                    "rétention n'est pas appliquée. À n'utiliser qu'en incident, "
                    "et à rétablir ensuite."
                ),
                id="ragatm.W011",
            )
        )
    return problems


def check_beat_schedule(app_configs, **kwargs):
    """ragatm.E011 / E012 — le schedule déclaré doit être exécutable."""
    import importlib

    from django.conf import settings

    problems: list = []
    schedule = getattr(settings, "CELERY_BEAT_SCHEDULE", {}) or {}

    if not isinstance(schedule, dict):
        return [
            Error(
                f"CELERY_BEAT_SCHEDULE doit être un dict, reçu {type(schedule).__name__}.",
                id="ragatm.E011",
            )
        ]

    for name, entry in schedule.items():
        if not isinstance(entry, dict):
            problems.append(
                Error(f"Entrée de schedule '{name}' invalide (dict attendu).", id="ragatm.E011")
            )
            continue

        # 1) La tâche doit être IMPORTABLE. Une entrée qui référence un chemin
        #    obsolète échoue silencieusement à chaque cycle, côté Beat.
        path = str(entry.get("task", "") or "")
        if not path:
            problems.append(
                Error(f"Entrée de schedule '{name}' sans clé 'task'.", id="ragatm.E011")
            )
        else:
            module_path, _, func = path.rpartition(".")
            try:
                module = importlib.import_module(module_path)
                if not hasattr(module, func):
                    problems.append(
                        Error(
                            f"Tâche planifiée introuvable : '{path}' "
                            f"(schedule '{name}'). Elle ne s'exécutera JAMAIS.",
                            hint="Vérifier le chemin dans settings.CELERY_BEAT_SCHEDULE.",
                            id="ragatm.E011",
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                problems.append(
                    Error(
                        f"Module de la tâche planifiée '{path}' non importable "
                        f"({type(exc).__name__}) — schedule '{name}'.",
                        id="ragatm.E011",
                    )
                )

        # 2) Fréquence strictement positive.
        sched = entry.get("schedule")
        if isinstance(sched, (int, float)):
            if sched <= 0:
                problems.append(
                    Error(
                        f"Fréquence invalide pour '{name}' : {sched} (doit être > 0).",
                        id="ragatm.E011",
                    )
                )
        elif sched is None:
            problems.append(
                Error(f"Entrée de schedule '{name}' sans fréquence.", id="ragatm.E011")
            )

        # 3) Queue réellement consommée.
        queue = str((entry.get("options") or {}).get("queue", "") or "")
        if queue and queue not in KNOWN_QUEUES:
            problems.append(
                Error(
                    f"Schedule '{name}' route vers la queue '{queue}', qu'aucun "
                    "worker déclaré ne consomme : les messages s'empileraient "
                    "sans jamais être exécutés.",
                    hint=f"Queues connues : {', '.join(sorted(KNOWN_QUEUES))}.",
                    id="ragatm.E011",
                )
            )

    # 4) Un schedule sans broker ne s'exécutera pas. Toléré quand les tâches sont
    #    exécutées en direct (tests) — sinon c'est une erreur de déploiement.
    if schedule and not bool(getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False)):
        broker = str(getattr(settings, "CELERY_BROKER_URL", "") or "")
        if not broker:
            problems.append(
                Error(
                    "CELERY_BEAT_SCHEDULE est déclaré mais CELERY_BROKER_URL est vide : "
                    "aucune tâche périodique ne pourra être distribuée.",
                    id="ragatm.E012",
                )
            )
    return problems


def check_audit_models_have_retention(app_configs, **kwargs):
    """ragatm.W010 — un modèle d'audit hors politique croît indéfiniment."""
    from apps.audit import retention

    covered = {r.key for r in retention.RULES}
    expected = {
        "rag_query_log",
        "action_audit",
        "llm_egress_audit",
        "egress_policy_change",
    }
    missing = expected - covered
    if missing:
        return [
            DjangoWarning(
                "Modèles d'audit sans règle de rétention : " + ", ".join(sorted(missing)),
                hint="Les inscrire dans apps.audit.retention.RULES.",
                id="ragatm.W010",
            )
        ]
    return []
