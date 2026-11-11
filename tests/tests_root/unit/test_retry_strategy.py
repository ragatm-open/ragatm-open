"""Table de décision des stratégies de reprise — exhaustive et déterministe.

Cette table est la seule raison d'être du lot 3 : sans elle, reprendre un document
en erreur rejoue le pipeline qui vient d'échouer. Elle doit donc être vérifiée code
par code, y compris sur ce qu'elle REFUSE — proposer un OCR après un OCR sans texte
serait exactement la promesse creuse qu'on cherche à supprimer.

Tests unitaires purs : aucun accès base, aucun fichier, aucune tâche.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps.rag_core.models.document import DocumentStatus
from apps.rag_core.services.ingest import retry_strategy as rs


def doc(*, error_code: str = "", error_message: str = "", filename: str = "a.pdf",
        content_type: str = "application/pdf"):
    """Document MINIMAL : la résolution ne lit que ces champs, et rien d'autre."""
    meta = {"extraction": {"error_code": error_code}} if error_code else {}
    return SimpleNamespace(
        id="D1", status=DocumentStatus.FAILED, filename=filename,
        content_type=content_type, error_message=error_message, meta=meta,
    )


# --------------------------------------------------------------------------- #
# Lecture du code d'erreur
# --------------------------------------------------------------------------- #
class TestErrorCode:
    def test_lit_le_code_structure(self):
        assert rs.document_error_code(doc(error_code="extraction_timeout")) == "extraction_timeout"

    def test_le_code_structure_prime_sur_une_prose(self):
        # `GatewayEmbeddingsError`/exception technique : meta porte le code, le message
        # une phrase. Le préfixe d'une phrase n'est pas un code connu : meta est retenu.
        d = doc(error_code="extraction_timeout", error_message="le budget est dépassé, voir journal")
        assert rs.document_error_code(d) == "extraction_timeout"
        d = doc(error_code="pipeline_task_inconsistent",
                error_message="pipeline_id 'emb-v2:x' incohérent avec task='document'")
        assert rs.document_error_code(d) == "pipeline_task_inconsistent"

    def test_un_code_connu_porte_par_le_message_prime_sur_un_code_meta_different(self):
        # Lot STALE-ERROR-METADATA : `error_message` est réécrit à CHAQUE règlement,
        # `meta.extraction.error_code` seulement par ceux passés par l'indexation. Aucun
        # règlement n'écrit deux codes connus différents au même instant : s'ils
        # diffèrent, le code meta vient d'une tentative antérieure et le message prime.
        d = doc(error_code="extraction_timeout", error_message="ocr_no_text: …")
        assert rs.document_error_code(d) == "ocr_no_text"
        d = doc(error_code="pipeline_task_inconsistent", error_message="duplicate_content: contenu identique")
        assert rs.document_error_code(d) == "duplicate_content"
        d = doc(error_code="transient_ingest_error", error_message="unsupported_file_type: refusé")
        assert rs.document_error_code(d) == "unsupported_file_type"

    def test_un_prefixe_inconnu_ne_contredit_pas_le_code_meta(self):
        d = doc(error_code="corrupted_file", error_message="worker exploded")
        assert rs.document_error_code(d) == "corrupted_file"

    def test_codes_identiques_des_deux_cotes(self):
        d = doc(error_code="corrupted_file", error_message="corrupted_file: en-tête invalide")
        assert rs.document_error_code(d) == "corrupted_file"

    def test_sans_code_meta_le_prefixe_connu_du_message_est_lu(self):
        d = doc(error_message="source_file_missing")
        assert rs.document_error_code(d) == "source_file_missing"

    @pytest.mark.parametrize("meta", [
        None, {}, [], "str", {"extraction": None}, {"extraction": "x"}, {"extraction": [1]},
        {"extraction": {}}, {"extraction": {"error_code": None}}, {"extraction": {"error_code": 5}},
        {"extraction": {"error_code": "  "}},
    ])
    def test_meta_absente_ou_malformee_ne_leve_jamais(self, meta):
        d = doc(error_message="transient_ingest_error: X")
        d.meta = meta
        assert rs.document_error_code(d) == "transient_ingest_error"
        assert rs.classify_retryability(d)[0] == rs.RETRYABLE
        d.error_message = ""
        assert rs.document_error_code(d) == ""
        assert rs.resolve_retry_strategy(d, rs.STRATEGY_AUTO).is_actionable

    def test_le_vocabulaire_connu_est_l_union_des_tables(self):
        known = rs.known_error_codes()
        assert known == (frozenset(rs.STRATEGY_BY_ERROR_CODE) | rs.NON_RETRYABLE_ERROR_CODES
                         | rs.REPLACE_SOURCE_ERROR_CODES | rs.ENVIRONMENTAL_ERROR_CODES)
        assert "duplicate_content" in known and "boom" not in known

    @pytest.mark.parametrize("message, attendu", [
        ("pdf_scanned_requires_ocr: PDF graphique ou scanné…", "pdf_scanned_requires_ocr"),
        ("empty_document", "empty_document"),
        ("no_storage_file_for_reindex", "no_storage_file_for_reindex"),
        ("", ""),
        ("Une phrase en français sans code.", ""),
        ("500 Internal Server Error", ""),
    ])
    def test_repli_borne_sur_le_prefixe_du_message(self, message, attendu):
        # Repli pour les documents tombés en échec AVANT la persistance systématique
        # du code : il ne lit QUE le préfixe, jamais la prose.
        assert rs.document_error_code(doc(error_message=message)) == attendu


# --------------------------------------------------------------------------- #
# Table de décision (mode auto)
# --------------------------------------------------------------------------- #
class TestAutoResolution:
    @pytest.mark.parametrize("code, attendue", [
        ("pdf_scanned_requires_ocr", rs.STRATEGY_FORCE_OCR),
        ("pdf_text_unrecoverable", rs.STRATEGY_FORCE_OCR),
        ("extraction_timeout", rs.STRATEGY_EXTENDED_TIMEOUT),
        ("pdf_extraction_timeout", rs.STRATEGY_EXTENDED_TIMEOUT),
    ])
    def test_auto_choisit_une_methode_differente(self, code, attendue):
        d = rs.resolve_retry_strategy(doc(error_code=code), rs.STRATEGY_AUTO)
        assert d.effective == attendue
        assert d.retryability == rs.RETRYABLE_WITH_STRATEGY
        assert d.is_actionable
        assert d.auto_fallback is False

    @pytest.mark.parametrize("code", [
        "transient_ingest_error", "embedding_provider_unavailable", "embedding_timeout",
        "ocr_engine_missing", "reindex_enqueue_unavailable",
    ])
    def test_auto_rejoue_a_l_identique_une_panne_de_service(self, code):
        # Le document est sain : c'est le service qui manquait. Rien à changer.
        d = rs.resolve_retry_strategy(doc(error_code=code), rs.STRATEGY_AUTO)
        assert d.effective == rs.STRATEGY_STANDARD
        assert d.retryability == rs.RETRYABLE
        assert d.auto_fallback is False

    def test_code_inconnu_replie_sur_standard_et_le_dit(self):
        d = rs.resolve_retry_strategy(doc(error_code="tombe_du_ciel"), rs.STRATEGY_AUTO)
        assert d.effective == rs.STRATEGY_STANDARD
        assert d.auto_fallback is True, "un repli ne doit pas passer pour un choix éclairé"
        assert d.is_actionable

    def test_absence_de_code_replie_sur_standard(self):
        d = rs.resolve_retry_strategy(doc(), rs.STRATEGY_AUTO)
        assert d.effective == rs.STRATEGY_STANDARD
        assert d.auto_fallback is True

    def test_force_ocr_porte_le_drapeau_et_aucun_timeout(self):
        d = rs.resolve_retry_strategy(doc(error_code="pdf_scanned_requires_ocr"), rs.STRATEGY_AUTO)
        assert d.force_ocr is True
        assert d.timeout_seconds is None

    def test_delai_etendu_porte_le_budget_configure(self, settings):
        settings.DOCUMENT_EXTRACTION_EXTENDED_TIMEOUT_SECONDS = 420
        d = rs.resolve_retry_strategy(doc(error_code="extraction_timeout"), rs.STRATEGY_AUTO)
        assert d.force_ocr is False
        assert d.timeout_seconds == 420


# --------------------------------------------------------------------------- #
# Ce qu'on ne reprend pas
# --------------------------------------------------------------------------- #
class TestNonRetryable:
    def test_ocr_sans_texte_ne_repropose_pas_l_ocr(self):
        # LE cas qui motive tout le lot : l'OCR a déjà tourné et n'a rien produit.
        d = rs.resolve_retry_strategy(doc(error_code="ocr_no_text"), rs.STRATEGY_AUTO)
        assert d.retryability == rs.REPLACE_SOURCE_RECOMMENDED
        assert d.is_actionable is False
        assert d.skip_reason == rs.SKIP_REPLACE_SOURCE
        assert d.force_ocr is False

    def test_ocr_sans_texte_refuse_meme_un_ocr_demande_explicitement(self):
        d = rs.resolve_retry_strategy(doc(error_code="ocr_no_text"), rs.STRATEGY_FORCE_OCR)
        assert d.is_actionable is False

    @pytest.mark.parametrize("code", [
        "unsupported_file_type", "macro_enabled_document_refused", "encrypted_pptx",
        "docx_corrupted", "file_too_large", "no_storage_file_for_reindex",
        "excel_too_many_cells", "image_corrupted",
    ])
    def test_echecs_definitifs(self, code):
        d = rs.resolve_retry_strategy(doc(error_code=code), rs.STRATEGY_AUTO)
        assert d.retryability == rs.NON_RETRYABLE
        assert d.is_actionable is False
        assert d.skip_reason == rs.SKIP_NON_RETRYABLE

    @pytest.mark.parametrize("code", ["empty_document", "empty_text", "no_valid_chunks"])
    def test_documents_sans_texte_appellent_un_autre_fichier(self, code):
        d = rs.resolve_retry_strategy(doc(error_code=code), rs.STRATEGY_AUTO)
        assert d.retryability == rs.REPLACE_SOURCE_RECOMMENDED
        assert d.is_actionable is False

    def test_un_echec_definitif_prime_sur_toute_stratégie_demandee(self):
        for requested in (rs.STRATEGY_STANDARD, rs.STRATEGY_FORCE_OCR, rs.STRATEGY_EXTENDED_TIMEOUT):
            d = rs.resolve_retry_strategy(doc(error_code="unsupported_file_type"), requested)
            assert d.is_actionable is False


# --------------------------------------------------------------------------- #
# Éligibilité OCR
# --------------------------------------------------------------------------- #
class TestOcrEligibility:
    @pytest.mark.parametrize("ctype, name", [
        ("application/pdf", "a.pdf"),
        ("image/png", "a.png"),
        ("image/jpeg", "a.jpg"),
        ("image/tiff", "a.tif"),
        ("image/webp", "a.webp"),
    ])
    def test_formats_eligibles(self, ctype, name):
        assert rs.supports_ocr(content_type=ctype, filename=name) is True

    @pytest.mark.parametrize("ctype, name", [
        ("text/plain", "a.txt"),
        ("text/csv", "a.csv"),
        ("text/html", "a.html"),
        ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "a.docx"),
        ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "a.xlsx"),
    ])
    def test_formats_non_eligibles(self, ctype, name):
        assert rs.supports_ocr(content_type=ctype, filename=name) is False

    def test_extension_en_repli_quand_le_type_est_absent(self):
        assert rs.supports_ocr(content_type="", filename="scan.PDF") is True
        assert rs.supports_ocr(content_type="", filename="notes.txt") is False

    def test_un_type_connu_non_eligible_ne_se_fait_pas_contredire_par_l_extension(self):
        # Doctrine du dépôt : l'extension ne contredit jamais un content_type connu.
        assert rs.supports_ocr(content_type="text/plain", filename="piege.pdf") is False

    def test_ocr_demande_sur_un_format_incompatible_est_saute(self):
        d = rs.resolve_retry_strategy(
            doc(error_code="transient_ingest_error", filename="a.txt", content_type="text/plain"),
            rs.STRATEGY_FORCE_OCR,
        )
        assert d.is_actionable is False
        assert d.skip_reason == rs.SKIP_STRATEGY_NOT_SUPPORTED
        assert d.retryability == rs.RETRYABLE, "le document reste reprenable — autrement"


# --------------------------------------------------------------------------- #
# Stratégies explicites
# --------------------------------------------------------------------------- #
class TestExplicitStrategies:
    def test_standard_est_respecte_meme_quand_l_ocr_serait_recommande(self):
        # Ne pas bloquer un administrateur : l'interface avertit, elle n'interdit pas.
        d = rs.resolve_retry_strategy(doc(error_code="pdf_scanned_requires_ocr"), rs.STRATEGY_STANDARD)
        assert d.effective == rs.STRATEGY_STANDARD
        assert d.force_ocr is False
        assert d.is_actionable

    def test_ocr_force_sur_un_document_en_timeout(self):
        d = rs.resolve_retry_strategy(doc(error_code="extraction_timeout"), rs.STRATEGY_FORCE_OCR)
        assert d.effective == rs.STRATEGY_FORCE_OCR
        assert d.force_ocr is True

    def test_une_valeur_inconnue_retombe_sur_auto(self):
        d = rs.resolve_retry_strategy(doc(error_code="extraction_timeout"), "magique")
        assert d.requested == rs.STRATEGY_AUTO
        assert d.effective == rs.STRATEGY_EXTENDED_TIMEOUT

    def test_auto_n_est_jamais_une_stratégie_effective(self):
        for code in ("extraction_timeout", "pdf_scanned_requires_ocr", "inconnu", ""):
            d = rs.resolve_retry_strategy(doc(error_code=code), rs.STRATEGY_AUTO)
            assert d.effective in rs.EFFECTIVE_STRATEGIES


# --------------------------------------------------------------------------- #
# Options et traçabilité
# --------------------------------------------------------------------------- #
class TestOptionsAndMeta:
    def test_options_par_strategie(self, settings):
        settings.DOCUMENT_EXTRACTION_EXTENDED_TIMEOUT_SECONDS = 400
        assert rs.ReindexOptions.for_strategy(rs.STRATEGY_STANDARD) == rs.ReindexOptions()
        assert rs.ReindexOptions.for_strategy(rs.STRATEGY_FORCE_OCR).force_ocr is True
        assert rs.ReindexOptions.for_strategy(
            rs.STRATEGY_EXTENDED_TIMEOUT
        ).extraction_timeout_seconds == 400

    def test_meta_persistable_sans_contenu_documentaire(self, settings):
        settings.DOCUMENT_EXTRACTION_EXTENDED_TIMEOUT_SECONDS = 480
        d = rs.resolve_retry_strategy(doc(error_code="extraction_timeout"), rs.STRATEGY_AUTO)
        meta = d.as_meta()
        assert meta == {
            "requested_strategy": "auto",
            "error_code": "extraction_timeout",
            "retryability": rs.RETRYABLE_WITH_STRATEGY,
            "timeout_seconds": 480,
        }

    def test_meta_d_un_saut_porte_la_raison(self):
        d = rs.resolve_retry_strategy(doc(error_code="ocr_no_text"), rs.STRATEGY_AUTO)
        assert d.as_meta()["skip_reason"] == rs.SKIP_REPLACE_SOURCE

    def test_les_options_sont_immuables(self):
        with pytest.raises(Exception):
            rs.ReindexOptions().force_ocr = True  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Cohérence des tables
# --------------------------------------------------------------------------- #
def test_aucun_code_ne_figure_dans_deux_tables():
    """Un code classé à deux endroits rendrait la décision dépendante de l'ordre."""
    tables = {
        "strategy": set(rs.STRATEGY_BY_ERROR_CODE),
        "non_retryable": set(rs.NON_RETRYABLE_ERROR_CODES),
        "replace_source": set(rs.REPLACE_SOURCE_ERROR_CODES),
        "environmental": set(rs.ENVIRONMENTAL_ERROR_CODES),
    }
    noms = list(tables)
    for i, a in enumerate(noms):
        for b in noms[i + 1:]:
            assert not (tables[a] & tables[b]), f"{a} ∩ {b} = {tables[a] & tables[b]}"


def test_les_strategies_de_la_table_sont_toutes_effectives():
    assert set(rs.STRATEGY_BY_ERROR_CODE.values()) <= rs.EFFECTIVE_STRATEGIES


def test_auto_est_demandable_mais_pas_effective():
    assert rs.STRATEGY_AUTO in rs.REQUESTABLE_STRATEGIES
    assert rs.STRATEGY_AUTO not in rs.EFFECTIVE_STRATEGIES
