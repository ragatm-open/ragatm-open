"""Frontière EXACTE de la limite d'upload par fichier — 50 Mio.

Le contrat est `size <= limite` : la limite elle-même est ACCEPTÉE. Ce fichier
existe parce que le dépôt ne testait la limite qu'en l'abaissant à 0,001 Mo pour
provoquer un refus — ce qui prouve qu'un refus arrive, jamais OÙ il arrive.

Unités : le calcul est BINAIRE de bout en bout (`× 1024 × 1024`), y compris côté
frontend (`MAX_SIZE_MB`) et côté proxy (nginx `m`). L'UI affiche « Mo » ; c'est un
libellé produit, pas une conversion décimale.
"""
from __future__ import annotations

import pytest
from django.conf import settings
from django.test import override_settings

from apps.rag_core.services.ingest import limits

#: 50 Mio, la valeur du contrat. Écrite en clair : une constante dérivée d'un
#: réglage ne prouverait rien sur la valeur attendue.
LIMIT_BYTES = 52_428_800


def test_cinquante_mio_vaut_bien_52_428_800_octets():
    assert 50 * 1024 * 1024 == LIMIT_BYTES


def test_le_defaut_du_socle_est_cinquante_mio():
    assert float(settings.UPLOAD_MAX_FILE_SIZE_MB) == 50.0
    assert int(settings.UPLOAD_MAX_FILE_SIZE_BYTES) == LIMIT_BYTES
    assert limits.max_file_size_bytes() == LIMIT_BYTES


def test_le_seuil_de_bufferisation_django_suit_la_limite():
    # Dérivé, jamais dupliqué : une valeur figée dériverait en silence.
    assert settings.DATA_UPLOAD_MAX_MEMORY_SIZE == settings.UPLOAD_MAX_FILE_SIZE_BYTES


def test_le_socle_d_extraction_partage_la_meme_limite():
    assert float(settings.DOCUMENT_MAX_UPLOAD_SIZE_MB) == float(settings.UPLOAD_MAX_FILE_SIZE_MB)


@pytest.mark.parametrize("taille", [0, 1, LIMIT_BYTES - 1, LIMIT_BYTES])
def test_jusqu_a_la_limite_incluse_l_upload_passe(taille):
    limits.check_file_size(taille)  # ne lève pas


def test_un_octet_de_trop_est_refuse():
    with pytest.raises(limits.UploadLimitExceeded) as exc:
        limits.check_file_size(LIMIT_BYTES + 1)
    assert exc.value.code == "file_too_large"


def test_la_borne_suit_une_surcharge_de_configuration():
    """La frontière n'est pas codée en dur dans le contrôle : elle suit le réglage."""
    with override_settings(UPLOAD_MAX_FILE_SIZE_MB=25.0):
        assert limits.max_file_size_bytes() == 26_214_400
        limits.check_file_size(26_214_400)
        with pytest.raises(limits.UploadLimitExceeded):
            limits.check_file_size(26_214_401)
