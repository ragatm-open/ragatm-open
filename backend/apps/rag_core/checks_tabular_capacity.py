"""System check de la capacité tabulaire — bloquant en production.

Django exécute les system checks avant de servir la première requête et **refuse de
démarrer** sur une `Error`. C'est le mécanisme idiomatique de garde de démarrage : il
n'exige aucun script d'entrée particulier, il fonctionne pour gunicorn comme pour
`runserver`, et `manage.py check` le rejoue en CI.

La sévérité dépend de l'environnement, et c'est délibéré :

- **production** — une capacité inconnue ou incohérente est une `Error` : le service
  ne démarre pas. Déployer avec une capacité non calculable revient à déployer sans
  savoir combien de requêtes lourdes peuvent s'exécuter, ce qui est précisément le
  risque que ce lot doit supprimer ;
- **staging** — `Warning` : la valeur reste visible, le service démarre ;
- **développement** — silencieux tant que le moteur est désactivé.

Le check ne s'applique **que si `TABULAR_QUERY_ENABLED=1`** : inutile d'imposer une
configuration de capacité à un déploiement qui n'utilise pas le moteur.
"""
from __future__ import annotations

from django.conf import settings
from django.core.checks import Error
from django.core.checks import Warning as CheckWarning

#: Identifiants stables : ils apparaissent dans les journaux de déploiement et dans
#: `manage.py check`, et servent de référence au runbook.
ID_PREFIX = "rag_core.tabular.capacity"


def _is_production() -> bool:
    module = getattr(settings, "SETTINGS_MODULE", "") or ""
    env = (getattr(settings, "DJANGO_ENV", "") or "").lower()
    return env == "prod" or module.endswith(".prod")


def check_tabular_capacity(app_configs, **kwargs):
    """Vérifie que la capacité tabulaire est calculable et cohérente."""
    from apps.rag_core.tabular import deployment as DEP

    if not getattr(settings, "TABULAR_QUERY_ENABLED", False):
        return []

    production = _is_production()
    etat = DEP.describe(strict=production)

    if etat["configuration_valid"]:
        return []

    gravite = Error if production else CheckWarning
    messages = []
    for index, probleme in enumerate(etat["problems"]):
        messages.append(gravite(
            f"Capacité tabulaire : {probleme['detail']}",
            hint=(
                "Déclarez GUNICORN_WORKERS, TABULAR_MAX_CONCURRENT_QUERIES et "
                "TABULAR_DEPLOYMENT_REPLICAS. La capacité de déploiement vaut "
                "workers × limite × réplicas ; tant qu'un facteur est inconnu, elle "
                "n'est pas calculable. Voir docs/07-operations/tabular-query-rollout.md "
                "§ Runbook."
            ),
            id=f"{ID_PREFIX}.{probleme['code']}",
            obj=f"problème {index + 1}/{len(etat['problems'])}",
        ))
    return messages
