"""Révocations d'accès déclenchées par le modèle, quel que soit le chemin d'écriture.

- Mot de passe changé (reset, Django admin, `changepassword`, console) : tous les
  jetons et challenges du compte tombent. Le second facteur, lui, est conservé —
  un reset de mot de passe ne désactive jamais l'obligation MFA.
- Facteur OTP supprimé : les jetons émis sur sa foi tombent aussi.
"""
from django.contrib.auth import get_user_model
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver
from django_otp.plugins.otp_static.models import StaticDevice
from django_otp.plugins.otp_totp.models import TOTPDevice

User = get_user_model()


@receiver(pre_save, sender=User)
def _flag_password_change(sender, instance, **kwargs):
    # `set_password` renseigne `_password` jusqu'au prochain `save()`.
    instance._ragatm_password_changed = bool(instance.pk) and getattr(instance, "_password", None) is not None


@receiver(post_save, sender=User)
def _revoke_on_password_change(sender, instance, created, **kwargs):
    if created or not getattr(instance, "_ragatm_password_changed", False):
        return
    from apps.accounts.mfa import revoke_auth_tokens, revoke_challenges

    revoke_auth_tokens(instance)
    revoke_challenges(instance)
    instance._ragatm_password_changed = False


@receiver(post_delete, sender=TOTPDevice)
@receiver(post_delete, sender=StaticDevice)
def _revoke_on_device_removed(sender, instance, **kwargs):
    if not instance.confirmed:
        return
    from apps.accounts.models import AuthToken

    # Par identifiant : en suppression en cascade du compte, l'objet `user`
    # peut déjà ne plus exister.
    AuthToken.objects.filter(user_id=instance.user_id).delete()
