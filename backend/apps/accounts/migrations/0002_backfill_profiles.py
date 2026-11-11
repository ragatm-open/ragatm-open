"""Rétro-remplit un Profile (locale = fr) pour chaque compte existant.

Doctrine : les comptes créés avant l'introduction de la préférence de langue
doivent explicitement porter le français (défaut). L'opération est idempotente
(`get_or_create`), ne touche AUCUNE autre donnée, et est réversible (le retour
supprime les profils créés). Aucun traitement coûteux : un simple parcours des
utilisateurs.
"""

from django.conf import settings
from django.db import migrations


def create_profiles(apps, schema_editor):
    User = apps.get_model(settings.AUTH_USER_MODEL.split(".")[0], settings.AUTH_USER_MODEL.split(".")[1])
    Profile = apps.get_model("accounts", "Profile")
    existing = set(Profile.objects.values_list("user_id", flat=True))
    to_create = [
        Profile(user_id=uid, preferred_locale="fr")
        for uid in User.objects.exclude(id__in=existing).values_list("id", flat=True)
    ]
    if to_create:
        Profile.objects.bulk_create(to_create, batch_size=500)


def remove_profiles(apps, schema_editor):
    Profile = apps.get_model("accounts", "Profile")
    Profile.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_profiles, remove_profiles),
    ]
