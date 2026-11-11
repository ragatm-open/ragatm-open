"""Préférence de langue utilisateur (preferred_locale) — endpoint + intégration.

Couvre le contrat de la mission i18n côté backend :
  - défaut fr (profil paresseux + rétro-remplissage) ;
  - mise à jour fr ⇄ en ;
  - rejet strict de toute locale hors {fr, en} ;
  - authentification obligatoire ;
  - isolation stricte entre comptes ;
  - présence de preferred_locale dans /auth/me/ et /bootstrap/.
"""
import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db


def _auth_client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def test_default_locale_is_fr(user):
    """Un compte sans profil renvoie fr (défaut), profil créé paresseusement."""
    res = _auth_client(user).get("/api/v1/me/preferences/")
    assert res.status_code == 200
    assert res.data == {"preferred_locale": "fr", "preferred_answer_language": "auto"}


def test_update_to_en_then_fr(user):
    client = _auth_client(user)

    res = client.patch("/api/v1/me/preferences/", {"preferred_locale": "en"}, format="json")
    assert res.status_code == 200
    assert res.data["preferred_locale"] == "en"
    # Persistance vérifiée par relecture.
    assert client.get("/api/v1/me/preferences/").data["preferred_locale"] == "en"

    res = client.patch("/api/v1/me/preferences/", {"preferred_locale": "fr"}, format="json")
    assert res.status_code == 200
    assert res.data["preferred_locale"] == "fr"


def test_rejects_unsupported_locale(user):
    res = _auth_client(user).patch(
        "/api/v1/me/preferences/", {"preferred_locale": "de"}, format="json"
    )
    assert res.status_code == 400
    assert "preferred_locale" in res.data  # erreur 400 structurée (champ nommé)


def test_requires_authentication(db):
    client = APIClient()
    assert client.get("/api/v1/me/preferences/").status_code == 401
    assert (
        client.patch(
            "/api/v1/me/preferences/", {"preferred_locale": "en"}, format="json"
        ).status_code
        == 401
    )


def test_isolation_between_users(user):
    """La modification d'un compte n'affecte jamais un autre compte."""
    other = get_user_model().objects.create_user(username="u2", password="pw")

    _auth_client(user).patch(
        "/api/v1/me/preferences/", {"preferred_locale": "en"}, format="json"
    )

    # L'autre utilisateur conserve son défaut fr.
    res_other = _auth_client(other).get("/api/v1/me/preferences/")
    assert res_other.data["preferred_locale"] == "fr"


def test_me_endpoint_exposes_preferred_locale(user):
    res = _auth_client(user).get("/api/v1/auth/me/")
    assert res.status_code == 200
    assert res.data["preferred_locale"] == "fr"

    _auth_client(user).patch(
        "/api/v1/me/preferences/", {"preferred_locale": "en"}, format="json"
    )
    assert _auth_client(user).get("/api/v1/auth/me/").data["preferred_locale"] == "en"


def test_bootstrap_exposes_preferred_locale(user):
    res = _auth_client(user).post("/api/v1/bootstrap/", {}, format="json")
    assert res.status_code == 200
    assert res.data["preferred_locale"] == "fr"


def test_backfill_migration_defaults_existing_accounts_to_fr(user):
    """get_preferred_locale renvoie fr pour un compte préexistant sans profil."""
    from apps.accounts.models import Profile, get_preferred_locale

    Profile.objects.filter(user=user).delete()  # simule un compte antérieur à la feature
    assert get_preferred_locale(user) == "fr"
