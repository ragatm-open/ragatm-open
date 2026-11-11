from __future__ import annotations

import pytest

from apps.actions.normalize import normalize, strip_accents


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Résume LE Document", "resume le document"),
        ("  quoi   de\tneuf  ", "quoi de neuf"),
        ("À propos de l’Orvanis", "a propos de l'orvanis"),  # apostrophe typo -> '
        ("ACTUALITÉS", "actualites"),
        ("mise à jour", "mise a jour"),
        ("«Rapport»", '"rapport"'),
        ("", ""),
    ],
)
def test_normalize_cases(raw, expected):
    assert normalize(raw) == expected


def test_normalize_idempotent():
    for s in ["Résumé « clé »", "Liste  DES   Articles", "Quoi de Neuf ?"]:
        once = normalize(s)
        assert normalize(once) == once


def test_strip_accents():
    assert strip_accents("éàçüîô") == "eacuio"
