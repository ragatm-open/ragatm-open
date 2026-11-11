"""System checks du dispositif de feedback.

Deux angles morts d'exploitation, tous deux silencieux si on ne les signale pas :

1. **`APP_GIT_SHA` absent** — le repli ``git rev-parse`` fonctionne en
   développement, mais une release produite par ``git archive`` ou une image
   Docker sans ``.git`` n'a pas de dépôt à interroger. Les incidents porteraient
   alors une révision VIDE, et « corrigé depuis quelle version ? » resterait
   sans réponse. C'est précisément l'information qu'on ne peut pas reconstituer
   après coup.

2. **`FEEDBACK_PSEUDONYM_SALT` absent** — le repli sur ``SECRET_KEY`` évite tout
   état non salé, mais il COUPLE deux usages sans rapport : une rotation de la
   clé Django changerait tous les pseudonymes, et le regroupement historique
   « plusieurs utilisateurs signalent le même cas » cesserait de fonctionner sur
   les incidents antérieurs — sans erreur, sans alerte, sans trace.

Ce sont des **avertissements**, jamais des erreurs bloquantes : ni l'un ni
l'autre ne compromet une réponse utilisateur. Ils dégradent la capacité
d'analyse plus tard, ce qui est exactement ce qui passe inaperçu.
"""
from __future__ import annotations

from django.conf import settings
from django.core.checks import Warning as CheckWarning

ID_GIT_SHA_MISSING = "feedback.W001"
ID_SALT_FALLBACK = "feedback.W002"

#: Environnements où l'absence de ces réglages est réellement problématique.
#: En dev, le repli est le comportement voulu et n'a pas à faire de bruit.
_DEPLOYED = ("staging", "prod", "production")


def _environment() -> str:
    for name in ("RAGATM_ENVIRONMENT", "DJANGO_ENV", "ENVIRONMENT"):
        value = str(getattr(settings, name, "") or "").strip().lower()
        if value:
            return value
    # Repli sur le module de settings chargé (config.settings.prod -> "prod").
    module = str(getattr(settings, "SETTINGS_MODULE", "") or "")
    return module.rsplit(".", 1)[-1].lower() if module else ""


def _is_deployed() -> bool:
    return _environment() in _DEPLOYED


def check_feedback_provenance(app_configs, **kwargs):
    """Signale une provenance d'incident non déterminable en environnement déployé."""
    issues: list = []
    if not _is_deployed():
        return issues

    if not str(getattr(settings, "APP_GIT_SHA", "") or "").strip():
        issues.append(
            CheckWarning(
                "APP_GIT_SHA n'est pas défini : les incidents de feedback seront "
                "estampillés d'une révision vide si le dépôt git est absent de "
                "l'image (release `git archive`, build Docker sans `.git`).",
                hint=(
                    "Injectez la révision au build ou au déploiement : "
                    "APP_GIT_SHA=$(git rev-parse HEAD). La révision d'un incident "
                    "ne peut pas être reconstituée après coup."
                ),
                id=ID_GIT_SHA_MISSING,
            )
        )

    if not str(getattr(settings, "FEEDBACK_PSEUDONYM_SALT", "") or "").strip():
        issues.append(
            CheckWarning(
                "FEEDBACK_PSEUDONYM_SALT n'est pas défini : les pseudonymes "
                "d'auteurs sont dérivés de DJANGO_SECRET_KEY. Une rotation de "
                "cette clé changerait tous les pseudonymes et casserait "
                "silencieusement le regroupement multi-signalants des incidents "
                "antérieurs.",
                hint=(
                    "Posez un secret DÉDIÉ (32 octets aléatoires suffisent). Sa "
                    "rotation devient alors une décision distincte, et elle doit "
                    "être documentée : elle rend les anciens pseudonymes "
                    "incomparables aux nouveaux."
                ),
                id=ID_SALT_FALLBACK,
            )
        )

    return issues
