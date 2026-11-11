"""Domaine ``storage`` — base de données, médias et sauvegardes."""
from __future__ import annotations

from config.registry.definition import ConfigDefinition as D

DEFINITIONS: tuple[D, ...] = (
    D(
        name="DB_NAME",
        domain="storage",
        value_type="str",
        default="ragatm",
        description="Nom de la base PostgreSQL.",
        required_in=("staging", "prod"),
        consumers=("config.settings.base.DATABASES",),
    ),
    D(
        name="DB_USER",
        domain="storage",
        value_type="str",
        default="ragatm",
        description="Utilisateur PostgreSQL.",
        required_in=("staging", "prod"),
        consumers=("config.settings.base.DATABASES",),
    ),
    D(
        name="DB_PASSWORD",
        domain="storage",
        value_type="str",
        default="ragatm",
        secret=True,
        description="Mot de passe PostgreSQL. Jamais affiché, jamais versionné en clair.",
        required_in=("staging", "prod"),
        consumers=("config.settings.base.DATABASES",),
        notes="Le défaut `ragatm` est une valeur de DÉVELOPPEMENT : interdite en production.",
    ),
    D(
        name="DB_HOST",
        domain="storage",
        value_type="str",
        default="localhost",
        description="Hôte PostgreSQL.",
        required_in=("staging", "prod"),
        consumers=("config.settings.base.DATABASES",),
    ),
    D(
        name="DB_PORT",
        domain="storage",
        value_type="int",
        default=5432,
        minimum=1,
        maximum=65535,
        description="Port PostgreSQL.",
        consumers=("config.settings.base.DATABASES",),
    ),
    D(
        name="MEDIA_URL",
        domain="storage",
        value_type="str",
        default="/media/",
        description="Préfixe d'URL de service des fichiers documentaires.",
        source="backend/config/settings/prod.py",
        consumers=("config.settings.prod",),
    ),
    D(
        name="MEDIA_ROOT",
        domain="storage",
        value_type="path",
        default="",
        default_expr="= BASE_DIR/backend/media",
        description=(
            "Racine de stockage des fichiers sources. Doit correspondre au point de "
            "montage du volume `media`, sinon les documents sont perdus au redéploiement."
        ),
        source="backend/config/settings/prod.py",
        consumers=("config.settings.prod", "apps.documents"),
        default_is_dynamic=True,
    ),
    D(
        name="RAGATM_BACKUP_ROOT",
        domain="storage",
        value_type="path",
        default="",
        description=(
            "Racine des sauvegardes inspectée par le contrôle de santé d'exploitation. "
            "Vide => contrôle inactif."
        ),
        source="backend/apps/rag_core/services/ops_health.py",
        consumers=("apps.rag_core.services.ops_health",),
        default_is_dynamic=True,
    ),
)
