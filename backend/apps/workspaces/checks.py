"""System checks des flags de périmètre owner / ACL RagSpace (point de revue #6).

Empêche les combinaisons de flags silencieusement incohérentes : l'ACL par
RagSpace n'a de sens (et n'est appliquée) qu'à l'intérieur de l'isolation par
périmètre owner. Activer l'ACL sans le périmètre ne bloquerait RIEN — un faux
sentiment de sécurité. On échoue donc au démarrage (`manage.py check`).
"""
from __future__ import annotations

from django.conf import settings
from django.core.checks import Error, register
from django.core.checks import Warning  # noqa: A004 (nom Django, pas le builtin)


@register()
def owner_scope_flags_coherent(app_configs, **kwargs):
    errors = []
    owner_active = getattr(settings, "OWNER_SCOPE_ENABLED", False) or getattr(
        settings, "OWNER_SCOPE_AUDIT_ONLY", False
    )
    acl_active = getattr(settings, "RAGSPACE_ACL_ENABLED", False) or getattr(
        settings, "RAGSPACE_ACL_AUDIT_ONLY", False
    )
    if acl_active and not owner_active:
        errors.append(
            Error(
                "RAGSPACE_ACL_ENABLED/RAGSPACE_ACL_AUDIT_ONLY est activé sans "
                "OWNER_SCOPE_ENABLED/OWNER_SCOPE_AUDIT_ONLY.",
                hint=(
                    "L'ACL par RagSpace n'est évaluée qu'à l'intérieur de "
                    "l'isolation par périmètre owner. Activez d'abord "
                    "OWNER_SCOPE_ENABLED (ou OWNER_SCOPE_AUDIT_ONLY), sinon l'ACL "
                    "ne bloque rien."
                ),
                id="workspaces.E001",
            )
        )

    # Combinaison ambiguë : les deux drapeaux ACL à 1. Le contrat est DÉFINI
    # (`resolve_acl_mode` -> AUDIT_ONLY, comme pour le périmètre owner), donc on
    # n'échoue pas — mais l'exploitant croit probablement avoir activé
    # l'enforcement. Le lui dire vaut mieux que le laisser le découvrir.
    if getattr(settings, "RAGSPACE_ACL_ENABLED", False) and getattr(
        settings, "RAGSPACE_ACL_AUDIT_ONLY", False
    ):
        errors.append(
            Warning(
                "RAGSPACE_ACL_ENABLED=1 ET RAGSPACE_ACL_AUDIT_ONLY=1 : l'ACL "
                "OBSERVE sans bloquer (audit-only l'emporte).",
                hint=(
                    "Même convention que OWNER_SCOPE_* : poser le drapeau "
                    "d'observation est une déclaration d'intention, respectée. "
                    "Pour bloquer réellement, mettre RAGSPACE_ACL_AUDIT_ONLY=0."
                ),
                id="workspaces.W001",
            )
        )
    return errors
