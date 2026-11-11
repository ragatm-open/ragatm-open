"""Initialisation IDEMPOTENTE du premier superuser — pilotée par l'environnement.

    python manage.py ensure_superuser

Pourquoi cette commande plutôt que `createsuperuser --noinput` :

- `createsuperuser` **échoue** si le compte existe déjà (``username is already taken``).
  Un déploiement qui la lance devient donc non rejouable : soit on l'exclut du runbook
  après le premier passage — et plus rien ne garantit les privilèges — soit on ignore
  son code de sortie, et on n'apprend plus rien de son échec.
- `createsuperuser` ne **vérifie pas** les privilèges d'un compte existant. Un superuser
  rétrogradé (``is_superuser`` retiré à la main, compte désactivé) resterait cassé sans
  qu'aucune commande de déploiement ne le signale.
- `createsuperuser` n'applique **aucun validateur de mot de passe** en mode `--noinput` :
  ``DJANGO_SUPERUSER_PASSWORD=admin`` passerait sans broncher.

Contrat de sortie (une seule ligne, jamais de secret) :

    Superuser created.
    Superuser already exists; privileges verified.
    Superuser updated.

Le mot de passe n'est JAMAIS accepté en argument de ligne de commande (il finirait dans
l'historique shell et dans `ps`), JAMAIS affiché, JAMAIS journalisé — y compris dans les
messages d'erreur, qui décrivent le motif du rejet sans citer la valeur.

Variables lues :

===================================  ==========================================
``DJANGO_SUPERUSER_USERNAME``        obligatoire
``DJANGO_SUPERUSER_EMAIL``           obligatoire (voie de reset du mot de passe)
``DJANGO_SUPERUSER_PASSWORD``        obligatoire à la CRÉATION ; sinon ignoré sauf
                                     opt-in de mise à jour
``DJANGO_SUPERUSER_UPDATE_PASSWORD`` ``0`` par défaut — à ``1``, réécrit le mot de
                                     passe d'un compte EXISTANT
===================================  ==========================================

Le modèle utilisateur est résolu par ``get_user_model()`` et le nom de compte par
``UserModel.USERNAME_FIELD`` : aucune hypothèse sur ``django.contrib.auth.models.User``.

Référence : docs/07-operations/runbook-premiere-production-le serveur applicatif.md
"""
from __future__ import annotations

import os

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from django.db import transaction

#: Mots de passe d'EXEMPLE refusés quelle que soit leur force apparente. Ces valeurs
#: circulent dans les `.env.example`, les runbooks et les copier-coller : les laisser
#: passer, c'est déployer une production dont le mot de passe est public. La
#: comparaison est insensible à la casse et ignore les espaces de bordure.
FORBIDDEN_PASSWORDS: frozenset[str] = frozenset(
    {
        "change-me",
        "change_me",
        "changeme",
        "changeme123",
        "<generate_and_inject>",
        "<inject>",
        "<secret>",
        "admin",
        "administrator",
        "password",
        "passw0rd",
        "motdepasse",
        "secret",
        "dev-secret-key",
        "ragatm",
        "ragatm123",
        "superuser",
        "test",
        "toto",
        "example",
        "placeholder",
    }
)

#: Valeur exacte attendue pour autoriser la réécriture du mot de passe d'un compte
#: existant. Tout le reste (``true``, ``yes``, vide) vaut NON : sur une variable dont
#: le seul effet est destructif, on n'interprète pas, on exige la valeur canonique.
_UPDATE_PASSWORD_YES = "1"

CREATED = "Superuser created."
VERIFIED = "Superuser already exists; privileges verified."
UPDATED = "Superuser updated."


def _env(name: str) -> str:
    """Valeur d'environnement nettoyée de ses espaces de bordure (jamais ``None``)."""
    return (os.environ.get(name) or "").strip()


def _raw_env(name: str) -> str:
    """Valeur d'environnement BRUTE — pour le mot de passe, qu'on ne retouche pas.

    Un ``.strip()`` sur un mot de passe changerait silencieusement le secret posé par
    l'exploitant : le compte serait créé avec une valeur qu'il ne connaît pas.
    """
    return os.environ.get(name) or ""


class Command(BaseCommand):
    help = (
        "Crée ou met à niveau le superuser d'amorçage à partir des variables "
        "DJANGO_SUPERUSER_* (idempotent, aucun secret affiché)."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--update-password",
            action="store_true",
            help=(
                "Réécrit le mot de passe d'un compte EXISTANT (équivalent à "
                "DJANGO_SUPERUSER_UPDATE_PASSWORD=1). Sans opt-in, le mot de passe "
                "d'un compte existant n'est jamais touché."
            ),
        )

    # -- exécution ---------------------------------------------------------------
    def handle(self, *args, **options) -> None:
        UserModel = get_user_model()
        username_field = UserModel.USERNAME_FIELD

        username = _env("DJANGO_SUPERUSER_USERNAME")
        email = _env("DJANGO_SUPERUSER_EMAIL")
        password = _raw_env("DJANGO_SUPERUSER_PASSWORD")
        update_password = bool(options.get("update_password")) or (
            _env("DJANGO_SUPERUSER_UPDATE_PASSWORD") == _UPDATE_PASSWORD_YES
        )

        if not username:
            raise CommandError(
                "DJANGO_SUPERUSER_USERNAME is required (empty or unset)."
            )
        if not email:
            raise CommandError("DJANGO_SUPERUSER_EMAIL is required (empty or unset).")
        try:
            validate_email(email)
        except ValidationError:
            raise CommandError(
                "DJANGO_SUPERUSER_EMAIL is not a valid email address."
            ) from None

        # `transaction.atomic` couvre TOUT le chemin : si la validation du mot de passe
        # échoue après la création (elle a besoin de l'instance pour la similarité
        # attribut/mot de passe), aucun compte partiel ne subsiste.
        with transaction.atomic():
            existing = UserModel._default_manager.filter(
                **{username_field: username}
            ).first()

            if existing is None:
                outcome = self._create(
                    UserModel, username_field, username, email, password
                )
            else:
                outcome = self._update(existing, email, password, update_password)

        self.stdout.write(outcome)

    # -- création ----------------------------------------------------------------
    def _create(self, UserModel, username_field, username, email, password) -> str:
        if not password:
            raise CommandError(
                "DJANGO_SUPERUSER_PASSWORD is required to create the superuser."
            )

        # Instance NON sauvegardée : elle sert de contexte aux validateurs (le
        # UserAttributeSimilarityValidator compare le mot de passe au username et à
        # l'email). Valider avant l'écriture évite de créer un compte qu'on refuserait.
        candidate = UserModel(**{username_field: username})
        if hasattr(candidate, "email"):
            candidate.email = email
        self._validate_password(password, candidate)

        user = UserModel._default_manager.create_user(
            **{username_field: username},
            email=email,
            password=password,
        )
        # `create_user` ne pose ni is_staff ni is_superuser : on les DÉCLARE, on ne
        # compte pas sur un manager qui les devinerait.
        user.is_staff = True
        user.is_superuser = True
        user.is_active = True
        user.save(update_fields=["is_staff", "is_superuser", "is_active"])
        return CREATED

    # -- mise à niveau -----------------------------------------------------------
    def _update(self, user, email, password, update_password) -> str:
        changed_fields: list[str] = []

        # Privilèges : on les GARANTIT à chaque passage. C'est la raison d'être de la
        # commande — un compte rétrogradé ou désactivé se répare par un redéploiement.
        for field in ("is_staff", "is_superuser", "is_active"):
            if getattr(user, field, False) is not True:
                setattr(user, field, True)
                changed_fields.append(field)

        if hasattr(user, "email") and (user.email or "") != email:
            user.email = email
            changed_fields.append("email")

        if update_password:
            if not password:
                raise CommandError(
                    "DJANGO_SUPERUSER_UPDATE_PASSWORD=1 but "
                    "DJANGO_SUPERUSER_PASSWORD is empty."
                )
            self._validate_password(password, user)
            user.set_password(password)
            changed_fields.append("password")

        if not changed_fields:
            return VERIFIED

        user.save(update_fields=changed_fields)
        return UPDATED

    # -- validation du secret ----------------------------------------------------
    @staticmethod
    def _validate_password(password: str, user) -> None:
        """Applique la politique Django + la liste des valeurs d'exemple.

        Ne cite JAMAIS la valeur : les messages décrivent le motif, pas le secret.
        """
        if password.strip().lower() in FORBIDDEN_PASSWORDS:
            raise CommandError(
                "DJANGO_SUPERUSER_PASSWORD matches a well-known example/placeholder "
                "value; pick a real secret."
            )
        try:
            validate_password(password, user=user)
        except ValidationError as exc:
            # `exc.messages` décrit les règles violées (longueur, similarité, mot
            # courant) sans reproduire le mot de passe.
            raise CommandError(
                "DJANGO_SUPERUSER_PASSWORD rejected by Django password validators: "
                + " ".join(exc.messages)
            ) from None
