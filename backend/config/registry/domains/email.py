"""Domaine ``email`` — messagerie sortante (invitations, réinitialisation)."""
from __future__ import annotations

from config.registry.definition import ConfigDefinition as D

DEFINITIONS: tuple[D, ...] = (
    D(
        name="EMAIL_BACKEND",
        domain="email",
        value_type="str",
        default="django.core.mail.backends.console.EmailBackend",
        description=(
            "Backend d'envoi Django. Le défaut console évite tout envoi réel sans "
            "configuration explicite."
        ),
        consumers=("django.core.mail",),
        notes=(
            "Non renseignée en staging : les emails d'invitation y partent donc dans "
            "la console. Constat de l'audit de configuration, pas une exigence du code."
        ),
    ),
    D(
        name="EMAIL_HOST",
        domain="email",
        value_type="str",
        default="localhost",
        description="Hôte SMTP.",
        consumers=("django.core.mail",),
    ),
    D(
        name="EMAIL_PORT",
        domain="email",
        value_type="int",
        default=25,
        minimum=1,
        maximum=65535,
        description="Port SMTP.",
        consumers=("django.core.mail",),
    ),
    D(
        name="EMAIL_HOST_USER",
        domain="email",
        value_type="str",
        default="",
        description="Utilisateur SMTP.",
        consumers=("django.core.mail",),
    ),
    D(
        name="EMAIL_HOST_PASSWORD",
        domain="email",
        value_type="str",
        default="",
        secret=True,
        description="Mot de passe SMTP. Jamais affiché, jamais versionné.",
        consumers=("django.core.mail",),
    ),
    D(
        name="EMAIL_USE_TLS",
        domain="email",
        value_type="bool",
        default=False,
        description="STARTTLS (port 587). Mutuellement exclusif avec EMAIL_USE_SSL ; TLS gagne.",
        consumers=("django.core.mail",),
    ),
    D(
        name="EMAIL_USE_SSL",
        domain="email",
        value_type="bool",
        default=False,
        description="SMTPS (port 465). Mutuellement exclusif avec EMAIL_USE_TLS.",
        consumers=("django.core.mail",),
    ),
    D(
        name="EMAIL_TIMEOUT",
        domain="email",
        value_type="int",
        default=0,
        default_expr="0 (aucun timeout : le setting Django vaut alors `None`)",
        minimum=0,
        description="Timeout SMTP en secondes. 0 => aucun timeout explicite.",
        consumers=("django.core.mail",),
        default_is_dynamic=True,
    ),
    D(
        name="DEFAULT_FROM_EMAIL",
        domain="email",
        value_type="str",
        default="ragatm@localhost",
        description="Expéditeur par défaut des emails applicatifs.",
        consumers=("django.core.mail",),
    ),
    D(
        name="SERVER_EMAIL",
        domain="email",
        value_type="str",
        default="ragatm@localhost",
        default_expr="= DEFAULT_FROM_EMAIL",
        description="Expéditeur des emails d'erreur serveur.",
        consumers=("django.core.mail",),
        default_is_dynamic=True,
    ),
    D(
        name="EMAIL_SUBJECT_PREFIX",
        domain="email",
        value_type="str",
        default="",
        description="Préfixe ajouté aux sujets (ex. « [STAGING] »).",
        consumers=("django.core.mail",),
    ),
)
