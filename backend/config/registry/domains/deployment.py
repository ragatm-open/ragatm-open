"""Domaine ``deployment`` — variables d'infrastructure génériques (compose, image, sondes).

RAGatm Open : ce domaine ne décrit AUCUNE topologie particulière. Il recense les variables
qui vivent dans le même fichier d'environnement que les réglages applicatifs sans être
lues par Django (publication de ports, options de construction d'image, concurrence des
workers), afin qu'elles soient auditées avec les autres (`manage.py config_audit`).

Les valeurs par défaut sont celles du profil Compact (une machine, un compose) ; elles ne
désignent jamais un hôte, une adresse ou un port d'une infrastructure existante.
"""
from __future__ import annotations

from config.registry.definition import ConfigDefinition as D

DEFINITIONS: tuple[D, ...] = (
    D(
        name="DEBUG",
        domain="deployment",
        value_type="bool",
        default=False,
        description=(
            "Mode debug Django. INERTE côté backend : `settings/base.py` force "
            "`DEBUG = False` et `prod.py` le confirme."
        ),
        deprecated=True,
        replacement=None,
        default_expr="False (True uniquement via `config.settings.dev`)",
        default_is_dynamic=True,
        source="docker-compose / .env",
        consumers=("docker-compose",),
        notes=(
            "Conservée pour la lisibilité des fichiers d'environnement ; une valeur `True` "
            "n'active PAS le mode debug."
        ),
    ),
    D(
        name="APP_HOST_PORT",
        domain="deployment",
        value_type="int",
        default=8000,
        minimum=1,
        maximum=65535,
        description="Port publié par le conteneur backend sur l'hôte (profil Compact).",
        source="docker-compose",
        consumers=("docker-compose",),
        default_is_dynamic=True,
    ),
    D(
        name="DEV_REDIS_PORT",
        domain="deployment",
        value_type="int",
        default=63790,
        minimum=1,
        maximum=65535,
        description="Port Redis publié en développement (loopback uniquement).",
        environments=("dev", "test"),
        source="docker-compose",
        consumers=("docker-compose",),
        default_is_dynamic=True,
    ),
    D(
        name="PUBLIC_BASE_URL",
        domain="deployment",
        value_type="url",
        default="",
        description="URL publique du service (reverse proxy de l'intégrateur, sondes externes).",
        source="docker-compose / deploy",
        consumers=("deploy",),
        default_is_dynamic=True,
    ),
    D(
        name="BACKEND_HEALTHCHECK_HOST",
        domain="deployment",
        value_type="str",
        default="",
        description="En-tête `Host` envoyé par la sonde de santé du backend (vide = hôte local).",
        source="docker-compose",
        consumers=("docker-compose",),
        default_is_dynamic=True,
    ),
    D(
        name="WITH_OCR",
        domain="deployment",
        value_type="bool",
        default=False,
        description=(
            "Construction de l'image AVEC Tesseract et l'extra Python `[ocr]`. Compact : "
            "désactivé par défaut. Distinct de DOCUMENT_OCR_ENABLED, qui active l'OCR à l'exécution."
        ),
        source="Dockerfile / docker-compose",
        consumers=("docker-compose", "Dockerfile"),
    ),
    D(
        name="CELERYD_CONCURRENCY",
        domain="deployment",
        value_type="int",
        default=2,
        minimum=1,
        maximum=64,
        description="Concurrence du worker Celery par défaut (ingestion, chat).",
        source="docker-compose",
        consumers=("docker-compose",),
        default_is_dynamic=True,
    ),
    D(
        name="CELERY_ANALYTICS_CONCURRENCY",
        domain="deployment",
        value_type="int",
        default=2,
        minimum=1,
        maximum=64,
        description="Concurrence du worker Celery dédié aux analyses globales.",
        source="docker-compose",
        consumers=("docker-compose",),
        default_is_dynamic=True,
    ),
)
