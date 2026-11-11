"""Verrous temporaires Office (`~$rapport.docx`) — défense backend.

Le frontend les filtre avant envoi (ergonomie : éviter 18 allers-retours pour rien),
mais le frontend n'est PAS une barrière : un client tiers, un onglet resté ouvert sur une
version antérieure, ou un appel direct à l'API peuvent encore en poster un.

Ces fichiers étaient déjà refusés par le contenu — ni OOXML, ni OLE2, donc `OCTET` donc
`unsupported_file_type`. Le refus était correct ; c'est son MESSAGE qui était trompeur
(« Type de fichier non reconnu ou non supporté »), et c'est précisément la confusion que
l'utilisateur a signalée en voyant 18 lignes rouges pour des fichiers qu'il n'avait jamais
eu l'intention d'importer.
"""
import pytest

from apps.rag_core.services.ingest.extraction.base import (
    ExtractionContext,
    RejectedFile,
    UnsupportedFormat,
)
from apps.rag_core.services.ingest.extraction.validation import validate_and_classify

from ._builders import build_pptx, make_docx, make_xlsx_zip

# Contenu RÉEL d'un verrou Word : ~160 octets, nom du propriétaire en UTF-16.
# Ni une archive OOXML, ni un conteneur OLE2 — juste un en-tête et du remplissage.
OFFICE_LOCK = bytes([0x60, 0x00]) + "<auteur>".encode("utf-16-le") + b"\x00" * 150


def _ctx(name):
    return ExtractionContext(filename=name)


class TestOfficeLocksAreNamed:
    @pytest.mark.parametrize("name", [
        "~$rapport.docx",
        "~$budget.xlsx",
        "~$presentation.pptx",
        "~$Document sans titre.docx",
        "~$rapport.pdf",
    ])
    def test_refused_with_a_dedicated_code(self, name):
        with pytest.raises(RejectedFile) as ei:
            validate_and_classify(OFFICE_LOCK, _ctx(name))
        assert ei.value.code == "office_temporary_file"

    def test_the_message_says_what_it_is(self):
        """Régression visée : « Type de fichier non reconnu ou non supporté », qui
        laissait croire à un format légitime que la plateforme ne gère pas."""
        with pytest.raises(RejectedFile) as ei:
            validate_and_classify(OFFICE_LOCK, _ctx("~$rapport.docx"))
        detail = ei.value.detail.lower()
        assert "temporaire" in detail
        assert "verrouillage" in detail
        assert "non reconnu" not in detail

    def test_is_a_400_not_a_415(self):
        """Convention du dépôt : seul `UnsupportedFormat` produit un 415."""
        with pytest.raises(RejectedFile) as ei:
            validate_and_classify(OFFICE_LOCK, _ctx("~$a.docx"))
        assert not isinstance(ei.value, UnsupportedFormat)

    def test_case_of_the_extension_does_not_matter(self):
        with pytest.raises(RejectedFile) as ei:
            validate_and_classify(OFFICE_LOCK, _ctx("~$RAPPORT.DOCX"))
        assert ei.value.code == "office_temporary_file"

    def test_unicode_name(self):
        with pytest.raises(RejectedFile) as ei:
            validate_and_classify(OFFICE_LOCK, _ctx("~$présentation.pptx"))
        assert ei.value.code == "office_temporary_file"

    @pytest.mark.parametrize("name", [
        "sous-dossier/~$annexe.xlsx",
        "a/b/c/~$profond.docx",
        "sous-dossier\\~$annexe.xlsx",
    ])
    def test_detection_is_on_the_basename_not_the_path(self, name):
        with pytest.raises(RejectedFile) as ei:
            validate_and_classify(OFFICE_LOCK, _ctx(name))
        assert ei.value.code == "office_temporary_file"

    def test_a_folder_named_tilde_dollar_does_not_condemn_its_files(self):
        """Le marqueur est dans le CHEMIN, pas dans le basename : c'est un vrai document."""
        _, fmt = validate_and_classify(make_docx(), _ctx("~$dossier/rapport.docx"))
        assert fmt == "docx"


class TestNoFalsePositive:
    """Le marqueur est un PRÉFIXE du basename. Rien d'autre ne doit être écarté."""

    @pytest.mark.parametrize("name", [
        "rapport~$.docx",       # ~$ au milieu
        "rapport~$suffixe.docx",
        "~rapport.docx",        # tilde seul
        "$rapport.docx",        # dollar seul
        "$~rapport.docx",       # ordre inversé
        "rapport.docx",
    ])
    def test_real_documents_still_pass(self, name):
        _, fmt = validate_and_classify(make_docx(), _ctx(name))
        assert fmt == "docx"

    def test_a_real_xlsx_still_passes(self):
        _, fmt = validate_and_classify(make_xlsx_zip(), _ctx("budget.xlsx"))
        assert fmt == "excel"

    def test_a_real_pptx_still_passes(self):
        _, fmt = validate_and_classify(
            build_pptx([{"title": "T", "bullets": ["a"]}]), _ctx("deck.pptx")
        )
        assert fmt == "pptx"

    def test_an_empty_name_does_not_crash(self):
        """Le nom peut être vide : le refus doit venir du CONTENU, pas d'une exception."""
        with pytest.raises((RejectedFile, UnsupportedFormat)):
            validate_and_classify(OFFICE_LOCK, _ctx(""))


class TestPrecedence:
    def test_the_lock_is_named_before_the_generic_refusal(self):
        """Sans la garde, ce fichier retombait sur `unsupported_file_type` — correct
        mais muet sur la vraie nature du fichier."""
        with pytest.raises(RejectedFile) as ei:
            validate_and_classify(OFFICE_LOCK, _ctx("~$a.docx"))
        assert ei.value.code != "unsupported_file_type"

    def test_a_lock_named_lock_is_refused_as_a_lock_not_as_too_large(self):
        """Le nom prime sur la taille : le motif doit être le bon."""
        from django.test import override_settings

        with override_settings(DOCUMENT_MAX_UPLOAD_SIZE_MB=0.00001):
            with pytest.raises(RejectedFile) as ei:
                validate_and_classify(OFFICE_LOCK, _ctx("~$a.docx"))
        assert ei.value.code == "office_temporary_file"

    def test_an_empty_lock_is_refused_as_a_lock_not_as_empty(self):
        with pytest.raises(RejectedFile) as ei:
            validate_and_classify(b"", _ctx("~$a.docx"))
        assert ei.value.code == "office_temporary_file"

    def test_refusing_on_the_name_never_accepts_anything(self):
        """La doctrine « ne jamais faire confiance à l'extension » interdit d'ACCEPTER
        sur le nom. Refuser ne peut que rejeter davantage — un exécutable renommé
        `~$a.docx` reste refusé, peu importe lequel des deux motifs s'applique."""
        with pytest.raises(RejectedFile):
            validate_and_classify(b"MZ\x90\x00" + b"\x00" * 80, _ctx("~$virus.docx"))


class TestNoDocumentIsEverCreated:
    """Le refus tombe AVANT toute écriture : c'est ce qui compte le plus."""

    def test_the_refusal_happens_in_validation_before_any_persistence(self):
        # `validate_and_classify` est appelée par la vue AVANT `create_or_get_by_sha`
        # (cf. api/v1/documents/views_write.py) : lever ici garantit qu'aucun Document,
        # aucun fichier stocké et aucune tâche d'ingestion n'existent.
        with pytest.raises(RejectedFile) as ei:
            validate_and_classify(OFFICE_LOCK, _ctx("~$a.docx"))
        assert ei.value.code == "office_temporary_file"
