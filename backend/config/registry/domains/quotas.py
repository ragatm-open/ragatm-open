"""Domaine ``quotas`` — quotas documentaires par workspace.

Ordre de résolution EFFECTIF (cf. `apps.workspaces.services.quota`) :
override CoreConfig scope=global > settings (env) > défaut de code.
Les variables d'environnement ne remplacent PAS l'administration superuser :
elles servent de fallback et d'initialisation.
"""
from __future__ import annotations

from config.registry.definition import ConfigDefinition as D

_TIB = 1024 ** 4
_GIB = 1024 ** 3

DEFINITIONS: tuple[D, ...] = (
    D(
        name="WORKSPACE_DEFAULT_MAX_DOCUMENT_COUNT",
        domain="quotas",
        value_type="int",
        default=5000,
        minimum=1,
        maximum=100000,
        description=(
            "Défaut plateforme : nombre maximal de documents par workspace. Copié dans "
            "WorkspaceQuota à la création (jamais rétroactif)."
        ),
        runtime_editable=True,
        max_scope="global",
        restart_required=False,
        consumers=("apps.workspaces.services.quota",),
        notes="Les bornes répliquent les plafonds absolus par défaut (garde-fou registre).",
    ),
    D(
        name="WORKSPACE_DEFAULT_MAX_DOCUMENT_BYTES",
        domain="quotas",
        value_type="int",
        default=10 * _GIB,
        minimum=1,
        maximum=_TIB,
        description=(
            "Défaut plateforme : taille cumulée maximale des fichiers sources d'un "
            "workspace (octets). 10 Gio."
        ),
        runtime_editable=True,
        max_scope="global",
        restart_required=False,
        consumers=("apps.workspaces.services.quota",),
    ),
    D(
        name="WORKSPACE_HARD_MAX_DOCUMENT_COUNT",
        domain="quotas",
        value_type="int",
        default=100000,
        minimum=1,
        description=(
            "Plafond ABSOLU serveur : un superuser ne peut pas fixer un quota au-delà "
            "sans changer la configuration serveur. Non éditable à chaud."
        ),
        consumers=("apps.workspaces.services.quota",),
    ),
    D(
        name="WORKSPACE_HARD_MAX_DOCUMENT_BYTES",
        domain="quotas",
        value_type="int",
        default=_TIB,
        minimum=1,
        description="Plafond ABSOLU serveur de la taille cumulée (1 Tio). Non éditable à chaud.",
        consumers=("apps.workspaces.services.quota",),
    ),
    D(
        name="WORKSPACE_QUOTAS_ENFORCED",
        domain="quotas",
        value_type="bool",
        default=True,
        description=(
            "Blocage effectif des uploads dépassant le quota. À 0, les quotas restent "
            "CALCULÉS et AFFICHÉS mais aucun upload n'est bloqué (rollout progressif)."
        ),
        consumers=("apps.workspaces.services.quota", "api.v1.documents"),
    ),
)
