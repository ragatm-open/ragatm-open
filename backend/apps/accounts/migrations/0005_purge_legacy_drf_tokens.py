"""Révoque tous les jetons `authtoken` historiques (MFA obligatoire).

Ces jetons ont été émis sur la seule foi du mot de passe. Depuis 0004, ils ne
sont plus acceptés par `CookieTokenAuthentication` (modèle `AuthToken`) ; les
supprimer ferme aussi la porte à toute réactivation accidentelle. Chaque
utilisateur se reconnecte et passe le second facteur.

Irréversible par nature : un jeton supprimé ne se recrée pas (no-op au retour).
"""
from django.db import migrations


def purge_legacy_tokens(apps, schema_editor):
    Token = apps.get_model("authtoken", "Token")
    Token.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0004_authtoken_mfachallenge"),
        ("authtoken", "0004_alter_tokenproxy_options"),
    ]

    operations = [
        migrations.RunPython(purge_legacy_tokens, migrations.RunPython.noop),
    ]
