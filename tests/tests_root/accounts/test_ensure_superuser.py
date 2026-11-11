"""Garde-fous de `manage.py ensure_superuser` — initialisation du premier superuser.

Cette commande s'exécute sur la PRODUCTION, avec un secret dans l'environnement.
Deux familles d'invariants sont donc verrouillées ici :

1. **Idempotence et privilèges.** Rejouer le déploiement ne doit ni dupliquer le
   compte, ni réécrire silencieusement son mot de passe, ni laisser passer un
   compte rétrogradé (``is_superuser`` retiré, compte désactivé).

2. **Non-divulgation.** Le mot de passe ne doit apparaître NI sur stdout, NI sur
   stderr, NI dans le message d'une exception — y compris sur les chemins d'échec,
   qui sont justement ceux où l'on est tenté d'afficher la valeur pour aider au
   diagnostic.

Le modèle utilisateur est obtenu par ``get_user_model()`` : les tests ne
présupposent pas ``django.contrib.auth.models.User``.
"""

from __future__ import annotations

import io

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

#: Hasher de PRODUCTION, restauré pour les seuls tests dont le hachage est le sujet.
#: La suite impose MD5 par défaut (vitesse) ; ici on veut le vrai comportement.
PRODUCTION_PASSWORD_HASHERS = ["django.contrib.auth.hashers.PBKDF2PasswordHasher"]

from apps.accounts.management.commands.ensure_superuser import (
    CREATED,
    UPDATED,
    VERIFIED,
)

UserModel = get_user_model()
USERNAME_FIELD = UserModel.USERNAME_FIELD

# Mot de passe de test : long, non commun, sans similarité avec le username ni
# l'email — il doit passer les quatre validateurs Django configurés.
STRONG = "Brouillard-Vermeil-7712"
STRONG_2 = "Cascade-Turquoise-4488"

BASE_ENV = {
    "DJANGO_SUPERUSER_USERNAME": "prod_admin",
    "DJANGO_SUPERUSER_EMAIL": "prod-admin@example.org",
    "DJANGO_SUPERUSER_PASSWORD": STRONG,
}


@pytest.fixture
def env(monkeypatch):
    """Environnement propre : aucune variable DJANGO_SUPERUSER_* héritée du poste."""
    for key in (
        "DJANGO_SUPERUSER_USERNAME",
        "DJANGO_SUPERUSER_EMAIL",
        "DJANGO_SUPERUSER_PASSWORD",
        "DJANGO_SUPERUSER_UPDATE_PASSWORD",
    ):
        monkeypatch.delenv(key, raising=False)

    def _set(**overrides):
        values = {**BASE_ENV, **overrides}
        for key, value in values.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
        return values

    return _set


def run(*args) -> str:
    """Exécute la commande et retourne sa sortie standard."""
    out = io.StringIO()
    call_command("ensure_superuser", *args, stdout=out)
    return out.getvalue()


def get_user(username: str = "prod_admin"):
    return UserModel._default_manager.get(**{USERNAME_FIELD: username})


# ===========================================================================
# 1. Création
# ===========================================================================
@pytest.mark.django_db
def test_creates_the_account_when_absent(env):
    env()
    assert UserModel._default_manager.count() == 0

    assert run().strip() == CREATED

    user = get_user()
    assert user.is_staff is True
    assert user.is_superuser is True
    assert user.is_active is True
    assert user.email == "prod-admin@example.org"
    assert user.check_password(STRONG)


@pytest.mark.django_db
@override_settings(PASSWORD_HASHERS=PRODUCTION_PASSWORD_HASHERS)
def test_password_is_hashed_not_stored_in_clear(env):
    """Contrôle explicite : la base ne contient jamais le secret en clair.

    Ce test a pour SUJET le hachage lui-même : il doit donc s'exécuter avec le
    hasher de production, pas avec le MD5 que la suite impose par défaut pour sa
    vitesse (cf. `tests/tests_root/conftest.py`). L'`override_settings` restaure
    la configuration réelle le temps de ce seul test — l'assertion, elle, n'est
    pas assouplie : elle continue d'exiger un préfixe de hasher fort.
    """
    env()
    run()
    user = get_user()
    assert user.password != STRONG
    assert user.password.startswith(("pbkdf2_", "argon2", "bcrypt"))


# ===========================================================================
# 2. Idempotence
# ===========================================================================
@pytest.mark.django_db
def test_second_run_does_not_duplicate_and_reports_verified(env):
    env()
    run()
    assert run().strip() == VERIFIED
    assert UserModel._default_manager.count() == 1


@pytest.mark.django_db
def test_password_is_not_touched_by_default(env):
    """Le cas qui compte : un redéploiement ne doit pas réinitialiser le compte.

    Sans cet invariant, un `.env` de déploiement qui traîne avec l'ancien mot de
    passe le réimposerait à chaque `up`, annulant toute rotation faite depuis.
    """
    env()
    run()
    user = get_user()
    user.set_password(STRONG_2)  # rotation faite hors déploiement
    user.save(update_fields=["password"])

    assert run().strip() == VERIFIED

    user.refresh_from_db()
    assert user.check_password(STRONG_2)
    assert not user.check_password(STRONG)


@pytest.mark.django_db
@pytest.mark.parametrize("opt_in", ["env", "flag"])
def test_password_updated_only_with_explicit_opt_in(env, monkeypatch, opt_in):
    env()
    run()

    if opt_in == "env":
        monkeypatch.setenv("DJANGO_SUPERUSER_UPDATE_PASSWORD", "1")
        monkeypatch.setenv("DJANGO_SUPERUSER_PASSWORD", STRONG_2)
        out = run()
    else:
        monkeypatch.setenv("DJANGO_SUPERUSER_PASSWORD", STRONG_2)
        out = run("--update-password")

    assert out.strip() == UPDATED
    assert get_user().check_password(STRONG_2)


@pytest.mark.django_db
@pytest.mark.parametrize("value", ["0", "true", "yes", "on", "", "TRUE", "2"])
def test_only_the_exact_value_1_authorises_a_password_rewrite(env, monkeypatch, value):
    """Sur une variable au seul effet destructeur, on n'interprète pas : 1 ou rien.

    Accepter ``true`` reviendrait à réécrire un mot de passe de production parce
    que quelqu'un a écrit le booléen dans l'autre convention.
    """
    env()
    run()
    monkeypatch.setenv("DJANGO_SUPERUSER_UPDATE_PASSWORD", value)
    monkeypatch.setenv("DJANGO_SUPERUSER_PASSWORD", STRONG_2)

    assert run().strip() == VERIFIED
    assert get_user().check_password(STRONG)


# ===========================================================================
# 3. Mise à niveau d'un compte existant
# ===========================================================================
@pytest.mark.django_db
def test_promotes_an_existing_non_superuser(env):
    """Un compte homonyme non privilégié est PROMU, pas dupliqué ni ignoré."""
    env()
    UserModel._default_manager.create_user(
        **{USERNAME_FIELD: "prod_admin"},
        email="prod-admin@example.org",
        password=STRONG,
    )
    user = get_user()
    assert user.is_superuser is False

    assert run().strip() == UPDATED

    user.refresh_from_db()
    assert user.is_staff is True
    assert user.is_superuser is True
    assert user.is_active is True
    # Le mot de passe existant n'est pas touché : la promotion n'est pas une
    # réinitialisation de compte.
    assert user.check_password(STRONG)


@pytest.mark.django_db
def test_reactivates_a_disabled_superuser(env):
    env()
    run()
    user = get_user()
    user.is_active = False
    user.is_staff = False
    user.save(update_fields=["is_active", "is_staff"])

    assert run().strip() == UPDATED

    user.refresh_from_db()
    assert user.is_active is True
    assert user.is_staff is True
    assert user.is_superuser is True


@pytest.mark.django_db
def test_updates_the_email_when_it_changed(env, monkeypatch):
    env()
    run()
    monkeypatch.setenv("DJANGO_SUPERUSER_EMAIL", "ops@example.org")

    assert run().strip() == UPDATED
    assert get_user().email == "ops@example.org"


# ===========================================================================
# 4. Validation des entrées
# ===========================================================================
@pytest.mark.django_db
@pytest.mark.parametrize(
    "missing", ["DJANGO_SUPERUSER_USERNAME", "DJANGO_SUPERUSER_EMAIL"]
)
def test_required_variables_are_enforced(env, missing):
    env(**{missing: None})
    with pytest.raises(CommandError, match=missing):
        run()
    assert UserModel._default_manager.count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_username_is_refused(env, blank):
    env(DJANGO_SUPERUSER_USERNAME=blank)
    with pytest.raises(CommandError, match="USERNAME"):
        run()


@pytest.mark.django_db
@pytest.mark.parametrize("bad", ["not-an-email", "a@", "@example.org", "a b@c.org"])
def test_invalid_email_is_refused(env, bad):
    env(DJANGO_SUPERUSER_EMAIL=bad)
    with pytest.raises(CommandError, match="valid email"):
        run()
    assert UserModel._default_manager.count() == 0


@pytest.mark.django_db
def test_missing_password_on_creation_is_refused(env):
    env(DJANGO_SUPERUSER_PASSWORD=None)
    with pytest.raises(CommandError, match="PASSWORD is required"):
        run()
    assert UserModel._default_manager.count() == 0


@pytest.mark.django_db
def test_opt_in_without_password_is_refused(env, monkeypatch):
    env()
    run()
    monkeypatch.delenv("DJANGO_SUPERUSER_PASSWORD", raising=False)
    monkeypatch.setenv("DJANGO_SUPERUSER_UPDATE_PASSWORD", "1")
    with pytest.raises(CommandError, match="PASSWORD is empty"):
        run()


@pytest.mark.django_db
@pytest.mark.parametrize("weak", ["short", "12345678901", "password", "azerty"])
def test_weak_passwords_are_refused_by_django_validators(env, weak):
    env(DJANGO_SUPERUSER_PASSWORD=weak)
    with pytest.raises(CommandError):
        run()
    assert UserModel._default_manager.count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize("example", ["change-me", "ChangeMe", "  changeme  ", "<INJECT>"])
def test_example_passwords_are_refused_even_if_they_pass_validators(env, example):
    """Une valeur d'exemple est publique : sa force apparente n'y change rien."""
    env(DJANGO_SUPERUSER_PASSWORD=example)
    with pytest.raises(CommandError, match="example/placeholder"):
        run()
    assert UserModel._default_manager.count() == 0


@pytest.mark.django_db
def test_password_too_similar_to_the_username_is_refused(env):
    env(
        DJANGO_SUPERUSER_USERNAME="mireille.dupont",
        DJANGO_SUPERUSER_PASSWORD="mireille.dupont",
    )
    with pytest.raises(CommandError):
        run()
    assert UserModel._default_manager.count() == 0


# ===========================================================================
# 5. Atomicité
# ===========================================================================
@pytest.mark.django_db(transaction=True)
def test_no_partial_account_survives_a_rejected_password(env):
    """La validation a besoin de l'instance ; l'échec ne doit rien laisser derrière."""
    env(DJANGO_SUPERUSER_PASSWORD="short")
    with pytest.raises(CommandError):
        run()
    assert not UserModel._default_manager.filter(
        **{USERNAME_FIELD: "prod_admin"}
    ).exists()


@pytest.mark.django_db
def test_a_rejected_update_leaves_the_account_untouched(env, monkeypatch):
    env()
    run()
    before = get_user()
    monkeypatch.setenv("DJANGO_SUPERUSER_UPDATE_PASSWORD", "1")
    monkeypatch.setenv("DJANGO_SUPERUSER_PASSWORD", "short")
    monkeypatch.setenv("DJANGO_SUPERUSER_EMAIL", "nouveau@example.org")

    with pytest.raises(CommandError):
        run()

    after = get_user()
    assert after.check_password(STRONG)
    # L'email non plus n'a pas bougé : la transaction couvre TOUT le chemin.
    assert after.email == before.email == "prod-admin@example.org"


# ===========================================================================
# 6. Non-divulgation du secret
# ===========================================================================
@pytest.mark.django_db
@pytest.mark.parametrize(
    "scenario",
    ["create", "verify", "update"],
)
@override_settings(PASSWORD_HASHERS=PRODUCTION_PASSWORD_HASHERS)
def test_the_password_never_appears_on_stdout_or_stderr(env, monkeypatch, scenario):
    """Sous MD5, l'assertion finale (`"pbkdf2" not in combined`) serait vraie sans
    rien prouver : un hash divulgué commencerait par `md5$`. Le hasher de
    production est donc restauré pour que ce contrôle garde sa force."""
    env()
    out, err = io.StringIO(), io.StringIO()

    if scenario == "create":
        call_command("ensure_superuser", stdout=out, stderr=err)
    elif scenario == "verify":
        run()
        call_command("ensure_superuser", stdout=out, stderr=err)
    else:
        run()
        monkeypatch.setenv("DJANGO_SUPERUSER_PASSWORD", STRONG_2)
        call_command("ensure_superuser", "--update-password", stdout=out, stderr=err)

    combined = out.getvalue() + err.getvalue()
    assert STRONG not in combined
    assert STRONG_2 not in combined
    # Ni le hash : une sortie de déploiement peut finir dans un journal partagé.
    assert "pbkdf2" not in combined


@pytest.mark.django_db
@pytest.mark.parametrize("bad_password", ["short", "change-me", "prod_admin"])
def test_the_password_never_appears_in_an_error_message(env, bad_password):
    """Le chemin d'échec est celui où l'on est tenté de citer la valeur fautive."""
    env(DJANGO_SUPERUSER_PASSWORD=bad_password)
    with pytest.raises(CommandError) as excinfo:
        run()
    assert bad_password not in str(excinfo.value)


@pytest.mark.django_db
def test_the_output_is_one_of_the_three_contract_lines(env):
    env()
    assert run().strip() in {CREATED, VERIFIED, UPDATED}
    assert run().strip() in {CREATED, VERIFIED, UPDATED}
