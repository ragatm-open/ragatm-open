"""
P4 : la politique de mot de passe (AUTH_PASSWORD_VALIDATORS) est désormais
réellement configurée, donc validate_password impose des contraintes.
Test sans base de données.
"""

import pytest
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError


@pytest.mark.parametrize("weak", ["pw", "1234567890", "password", "azerty"])
def test_weak_passwords_are_rejected(weak):
    with pytest.raises(ValidationError):
        validate_password(weak)


def test_strong_password_is_accepted():
    # Suffisamment long, non numérique, non commun.
    validate_password("Tr0ub4dour-Plafond-92!")
