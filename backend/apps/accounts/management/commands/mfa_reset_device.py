"""Procédure opérateur : retirer le second facteur d'un compte qui l'a perdu.

Volontairement MANUELLE (repris d'un projet interne) :
aucun parcours de récupération en libre-service n'existe — il deviendrait le
maillon faible de toute la chaîne MFA. L'identité du demandeur est vérifiée par
un canal indépendant AVANT d'exécuter cette commande (voir
docs/07-operations/mfa-recuperation.md).

Effet : supprime les appareils TOTP et jetons de secours, révoque jetons d'accès
et challenges. Le compte se réenrôle lui-même à sa prochaine connexion : le
secret ne transite par personne d'autre que son détenteur. Le MFA reste
obligatoire — il n'existe pas d'option pour l'exempter.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.accounts.mfa import reset_devices


class Command(BaseCommand):
    help = "Retire les facteurs OTP d'un compte (réenrôlement obligatoire à la prochaine connexion)."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)
        parser.add_argument("--reason", required=True, help="Référence du ticket / motif.")
        parser.add_argument("--verified-by", required=True, help="Canal de vérification d'identité utilisé.")
        parser.add_argument("--apply", action="store_true", help="Sans ce drapeau : aperçu seulement.")

    def handle(self, *args, **opts):
        User = get_user_model()
        users = list(User.objects.filter(email__iexact=opts["email"].strip()))
        if len(users) != 1:
            raise CommandError(f"Compte introuvable ou ambigu ({len(users)} correspondance(s)).")
        user = users[0]

        from django_otp import devices_for_user

        devices = list(devices_for_user(user, confirmed=None))
        self.stdout.write(f"compte id={user.pk} actif={user.is_active} appareils={len(devices)}")
        if not opts["apply"]:
            self.stdout.write("Aperçu seulement — relancer avec --apply pour retirer les facteurs.")
            return

        removed = reset_devices(user)
        try:
            from apps.audit.action_audit import emit_action_event

            emit_action_event(
                event_name="auth.mfa_device_reset",
                action_name="mfa_device_reset",
                status="completed",
                resource_type="user",
                detection_method="management_command",
                metadata={
                    "target_user_id": user.pk,
                    "devices_removed": removed,
                    "reason": opts["reason"],
                    "verified_by": opts["verified_by"],
                },
            )
        except Exception as exc:  # noqa: BLE001 — l'audit ne bloque pas la récupération
            self.stderr.write(f"audit non écrit : {exc.__class__.__name__}")
        self.stdout.write(self.style.SUCCESS(f"{removed} appareil(s) retiré(s) ; accès révoqués."))
