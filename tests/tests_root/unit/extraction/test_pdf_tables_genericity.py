"""garde de GÉNÉRICITÉ du moteur de tableaux.

Le corpus de qualification est un ORACLE, pas une configuration. Un moteur « qui passe sur le corpus » ne doit pas être un moteur taillé POUR le
corpus : les fixtures sont synthétiques et neutres, et aucun cas particulier de mise
en page n'est admis dans le code de production.

La garde couvre aussi le motif de spécialisation le plus tentant : câbler un numéro
de page. Le contrôle du vocabulaire du corpus de qualification est exécuté hors
du dépôt, sur les artefacts de release.
"""
from __future__ import annotations

import pathlib
import re

import pytest

_BACKEND = pathlib.Path(__file__).resolve().parents[4] / "backend"
_EXTRACTION = _BACKEND / "apps" / "rag_core" / "services" / "ingest" / "extraction"
_INDEXING = _BACKEND / "apps" / "rag_core" / "services" / "ingest" / "indexing.py"

#: Motifs de spécialisation structurelle : câbler un numéro de page connu.
FORBIDDEN_PATTERNS = (
    (r"\bpage\s*==\s*\d+", "cas particulier sur un numéro de page"),
    (r"page_number\s*==\s*\d+", "cas particulier sur un numéro de page"),
)


def _production_files():
    files = sorted(p for p in _EXTRACTION.glob("*.py") if p.name != "__init__.py")
    files.append(_INDEXING)
    return files


@pytest.mark.parametrize("path", _production_files(), ids=lambda p: p.name)
def test_no_corpus_shaped_specialization(path: pathlib.Path):
    src = path.read_text(encoding="utf-8")
    for pattern, label in FORBIDDEN_PATTERNS:
        assert not re.search(pattern, src), f"{path.name} : {label} ({pattern})"


def test_generic_engine_declares_no_fixed_column_count():
    """Un moteur qui exigerait N colonnes serait spécialisé pour une mise en page."""
    src = (_EXTRACTION / "pdf_tables.py").read_text(encoding="utf-8")
    assert not re.search(r"len\(\s*\w+\s*\)\s*==\s*4", src), "comptage de colonnes figé"
    assert not re.search(r"n_physical_cols\s*==\s*\d", src), "comptage de colonnes figé"


def test_continuation_is_not_decided_by_blank_anchor_alone():
    """Garde de conception : la décision de continuation doit croiser plusieurs
    signaux. Le lot interdit explicitement « première cellule vide = continuation »."""
    from apps.rag_core.services.ingest.extraction import pdf_tables

    import inspect

    src = inspect.getsource(pdf_tables._continuation_decision)
    for signal in ("fragment_is_chained", "is_first_body_row", "anchor_empty",
                   "open_row", "previous_columns"):
        assert signal in src, f"signal de continuation absent : {signal}"
