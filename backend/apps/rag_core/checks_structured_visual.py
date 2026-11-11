"""System check — extraction structurée chart/table (expérimentale).

L'extraction structurée est ÉTEINTE par défaut. En production (`DJANGO_ENV` =
`prod`/`production`), le code la refuse tant que STRUCTURED_VISUAL_PRODUCTION_ALLOWED est
faux, plafonne ses tâches à `table` et refuse toujours le shadow des courbes ; un
`DJANGO_ENV` inconnu la refuse. Ces refus sont SILENCIEUX — un opérateur qui croit l'avoir
allumée ne voit rien. Ces avertissements rendent visible, au démarrage, une configuration
sans effet.

Jamais bloquant : le comportement effectif est déjà sûr (refus), seul le constat manque.

IDENTIFIANTS
------------
=============  ==========  =============================================
ID             Niveau      Condition
=============  ==========  =============================================
rag_core.W130  Warning     Réglage structuré allumé en production sans
                           dérogation, ou shadow des courbes configuré en
                           production : ignoré par le code
rag_core.W131  Warning     Drapeau allumé, allowlist d'espaces vide :
                           aucun espace concerné
rag_core.W132  Warning     Réglage dépendant posé sans
                           STRUCTURED_VISUAL_ENABLED : inerte
rag_core.W133  Warning     Shadow des courbes allumé, allowlist shadow
                           vide : aucun espace concerné
rag_core.W134  Warning     Drapeau allumé, `DJANGO_ENV` inconnu : refusé
rag_core.W135  Warning     STRUCTURED_VISUAL_ALLOWED_TASKS : valeur
                           inconnue, ou tâche écartée par le plafond de
                           production
=============  ==========  =============================================
"""
from __future__ import annotations

from django.conf import settings
from django.core.checks import Warning as CheckWarning

ID_PRODUCTION_IGNORED = "rag_core.W130"
ID_EMPTY_ALLOWLIST = "rag_core.W131"
ID_DEPENDENT_INERT = "rag_core.W132"
ID_SHADOW_EMPTY_ALLOWLIST = "rag_core.W133"
ID_UNKNOWN_ENVIRONMENT = "rag_core.W134"
ID_TASKS_IGNORED = "rag_core.W135"

#: Réglages qui n'agissent qu'avec STRUCTURED_VISUAL_ENABLED, l'allowlist et hors production.
DEPENDENT_SETTINGS = (
    "STRUCTURED_VISUAL_AUTO_ENABLED",
    "STRUCTURED_VISUAL_PDF_MAX_ASSETS",
    "STRUCTURED_VISUAL_PDF_MAX_PER_PAGE",
    "STRUCTURED_VISUAL_CURVE_SHADOW_ENABLED",
    "STRUCTURED_VISUAL_PRODUCTION_ALLOWED",
)
SHADOW_SETTINGS = ("STRUCTURED_VISUAL_CURVE_SHADOW_ENABLED",)


def _is_set(name: str) -> bool:
    value = getattr(settings, name, None)
    if isinstance(value, bool):
        return value
    try:
        return int(value or 0) > 0
    except (TypeError, ValueError):
        return False


def check_structured_visual_scope(app_configs, **kwargs):
    """Signale un réglage d'extraction structurée qui ne peut avoir aucun effet."""
    from apps.rag_core.services.ingest.visual.structured import (
        NON_PRODUCTION_ENVS,
        PRODUCTION_ENVS,
        TASKS,
        _allowlist,
        allowed_tasks,
    )

    enabled = bool(getattr(settings, "STRUCTURED_VISUAL_ENABLED", False))
    dependents = [name for name in DEPENDENT_SETTINGS if _is_set(name)]
    env = str(getattr(settings, "DJANGO_ENV", "") or "").strip().lower()
    production = env in PRODUCTION_ENVS
    production_allowed = getattr(settings, "STRUCTURED_VISUAL_PRODUCTION_ALLOWED", False) is True
    issues: list = []

    if production:
        # Shadow des courbes : refusé en production quoi qu'il arrive ; le reste, sans dérogation.
        ignored = [name for name in SHADOW_SETTINGS if _is_set(name)]
        if not production_allowed:
            ignored = (["STRUCTURED_VISUAL_ENABLED"] if enabled else []) + dependents
        if ignored:
            issues.append(
                CheckWarning(
                    f"Extraction structurée configurée en production ({', '.join(ignored)}) : ignorée.",
                    hint=(
                        "Sans STRUCTURED_VISUAL_PRODUCTION_ALLOWED, l'extraction structurée est refusée "
                        "en production (aucun appel) ; le shadow des courbes l'est toujours, dérogation "
                        "comprise. Retirez ces réglages, ou posez la dérogation pour un canary limité à "
                        "l'allowlist."
                    ),
                    id=ID_PRODUCTION_IGNORED,
                )
            )
        if not production_allowed:
            return issues
    elif enabled and env not in NON_PRODUCTION_ENVS:
        issues.append(
            CheckWarning(
                f"STRUCTURED_VISUAL_ENABLED est allumé mais DJANGO_ENV={env!r} est inconnu : refusé.",
                hint=f"Environnements admis : {', '.join(sorted(NON_PRODUCTION_ENVS | PRODUCTION_ENVS))}.",
                id=ID_UNKNOWN_ENVIRONMENT,
            )
        )
        return issues

    raw_tasks = getattr(settings, "STRUCTURED_VISUAL_ALLOWED_TASKS", None) or []
    if isinstance(raw_tasks, str):
        raw_tasks = raw_tasks.split(",")
    configured = {str(v).strip().lower() for v in raw_tasks if str(v).strip()}
    ignored = sorted((configured - set(TASKS)) | (configured & set(TASKS)) - set(allowed_tasks()))
    if enabled and ignored:
        issues.append(
            CheckWarning(
                f"STRUCTURED_VISUAL_ALLOWED_TASKS : {', '.join(ignored)} ignoré(s).",
                hint=("Tâches connues : chart, table ; en production, seule `table` est admise "
                      f"(tâches effectives : {', '.join(sorted(allowed_tasks())) or 'aucune'})."),
                id=ID_TASKS_IGNORED,
            )
        )
    if enabled and not _allowlist():
        issues.append(
            CheckWarning(
                "STRUCTURED_VISUAL_ENABLED est allumé mais STRUCTURED_VISUAL_RAGSPACE_ALLOWLIST "
                "est vide : aucun espace n'est concerné.",
                hint="Listez les identifiants des RagSpace d'expérimentation, ou éteignez le drapeau.",
                id=ID_EMPTY_ALLOWLIST,
            )
        )
    if enabled and _is_set("STRUCTURED_VISUAL_CURVE_SHADOW_ENABLED"):
        from apps.rag_core.services.ingest.visual.curve_shadow import _list_setting

        if not _list_setting("STRUCTURED_VISUAL_CURVE_SHADOW_RAGSPACE_ALLOWLIST"):
            issues.append(
                CheckWarning(
                    "STRUCTURED_VISUAL_CURVE_SHADOW_ENABLED est allumé mais "
                    "STRUCTURED_VISUAL_CURVE_SHADOW_RAGSPACE_ALLOWLIST est vide : aucun espace "
                    "n'est concerné.",
                    hint="Listez l'espace pilote du shadow, ou éteignez le drapeau.",
                    id=ID_SHADOW_EMPTY_ALLOWLIST,
                )
            )
    if dependents and not enabled:
        issues.append(
            CheckWarning(
                f"{', '.join(dependents)} posé(s) sans STRUCTURED_VISUAL_ENABLED : sans effet.",
                hint="Ces réglages n'agissent qu'avec le drapeau principal et l'allowlist d'espaces.",
                id=ID_DEPENDENT_INERT,
            )
        )
    return issues
