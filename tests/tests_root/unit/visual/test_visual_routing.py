"""drapeaux, allowlist de sources, asynchrone forcé et file dédiée."""
from __future__ import annotations

import pytest

from apps.rag_core.services.ingest import async_policy, queues
from apps.rag_core.services.ingest.extraction import config
from apps.rag_core.services.ingest.extraction.validation import (
    FMT_DOCX,
    FMT_IMAGE,
    FMT_PDF,
    FMT_PPTX,
)


@pytest.fixture
def visual_on(settings):
    settings.DOCUMENT_VISUAL_ANALYSIS_ENABLED = True
    settings.DOCUMENT_VISUAL_ANALYSIS_SOURCES = "image"
    settings.DOCUMENT_OCR_ENABLED = False
    settings.RAG_INGEST_ASYNC_DEFAULT = False
    return settings


class TestFlags:
    def test_defaults_are_inert(self):
        from config import registry

        enabled = registry.get("DOCUMENT_VISUAL_ANALYSIS_ENABLED")
        sources = registry.get("DOCUMENT_VISUAL_ANALYSIS_SOURCES")
        assert enabled.default is False
        assert list(sources.default) == ["image"]

    def test_off_means_no_active_source(self, settings):
        settings.DOCUMENT_VISUAL_ANALYSIS_ENABLED = False
        settings.DOCUMENT_VISUAL_ANALYSIS_SOURCES = "image"
        assert config.visual_analysis_sources() == frozenset()

    def test_on_with_image(self, visual_on):
        assert config.visual_analysis_sources() == frozenset({"image"})

    @pytest.mark.parametrize("raw", ["email", "", "  "])
    def test_future_or_empty_sources_are_not_activated(self, visual_on, raw):
        """Une source sans lot reste inerte, même écrite dans la configuration.

        `docx` était ici auparavant : elle est maintenant OPÉRATIONNELLE
        (cf. `test_docx_is_operational_since_1d`). `email` prend sa place de témoin.
        """
        visual_on.DOCUMENT_VISUAL_ANALYSIS_SOURCES = raw
        assert config.visual_analysis_sources() == frozenset()

    def test_pdf_is_operational_since_1c(self, visual_on):
        visual_on.DOCUMENT_VISUAL_ANALYSIS_SOURCES = "pdf"
        assert config.visual_analysis_sources() == frozenset({"pdf"})
        assert config.visual_pdf_analysis_active() is True
        assert config.visual_image_analysis_active() is False

    def test_docx_is_operational_since_1d(self, visual_on):
        """`docx` a désormais son lot, sa tâche et son adaptateur."""
        visual_on.DOCUMENT_VISUAL_ANALYSIS_SOURCES = "docx"
        assert config.visual_analysis_sources() == frozenset({"docx"})
        assert config.visual_docx_analysis_active() is True
        assert config.visual_image_analysis_active() is False
        assert config.visual_pdf_analysis_active() is False

    def test_list_setting_is_accepted(self, visual_on):
        visual_on.DOCUMENT_VISUAL_ANALYSIS_SOURCES = ["IMAGE", "pdf", "docx"]
        assert config.visual_analysis_sources() == frozenset({"image", "pdf", "docx"})

    def test_pptx_is_operational_since_pptx_1b(self, visual_on):
        """`pptx` a son lot, sa tâche et son adaptateur."""
        visual_on.DOCUMENT_VISUAL_ANALYSIS_SOURCES = "pptx"
        assert config.visual_analysis_sources() == frozenset({"pptx"})
        assert config.visual_pptx_analysis_active() is True
        assert config.visual_docx_analysis_active() is False
        assert config.visual_image_analysis_active() is False

    def test_pptx_closed_by_default_and_outside_allowlist(self, visual_on, settings):
        assert config.visual_pptx_analysis_active() is False, "défaut : `image` seule"
        visual_on.DOCUMENT_VISUAL_ANALYSIS_SOURCES = "image,pdf,docx"
        assert config.visual_pptx_analysis_active() is False
        settings.DOCUMENT_VISUAL_ANALYSIS_ENABLED = False
        settings.DOCUMENT_VISUAL_ANALYSIS_SOURCES = "pptx"
        assert config.visual_pptx_analysis_active() is False

    def test_pptx_format_gate_closes_visual_gate(self, visual_on):
        """Le format refusé n'a rien à faire sur la file visuelle."""
        visual_on.DOCUMENT_VISUAL_ANALYSIS_SOURCES = "pptx"
        visual_on.DOCUMENT_PPTX_ENABLED = False
        assert config.visual_pptx_analysis_active() is False

    def test_no_dedicated_pptx_visual_flag(self):
        """Une seule porte visuelle générique : aucun drapeau propre au PPTX."""
        from config import registry

        noms = {d.name for d in registry.all_definitions()}
        assert "DOCUMENT_VISUAL_ANALYSIS_SOURCES" in noms, "le registre est bien lu"
        assert not any("PPTX_VISUAL" in n for n in noms)
        assert not hasattr(config, "pptx_visual_enabled")


class TestAsync:
    def test_visual_image_is_forced_async(self, visual_on):
        assert async_policy.resolve_async(explicit="false", format_key=FMT_IMAGE) is True

    def test_feature_off_keeps_baseline(self, settings):
        settings.DOCUMENT_VISUAL_ANALYSIS_ENABLED = False
        settings.DOCUMENT_OCR_ENABLED = False
        settings.RAG_INGEST_ASYNC_DEFAULT = False
        assert async_policy.resolve_async(explicit="false", format_key=FMT_IMAGE) is False
        assert async_policy.resolve_async(explicit=None, format_key=FMT_IMAGE) is False

    def test_source_not_allowed_keeps_baseline(self, visual_on):
        """Source `pdf` seule : l'image redevient une ingestion ordinaire."""
        visual_on.DOCUMENT_VISUAL_ANALYSIS_SOURCES = "pdf"
        assert async_policy.resolve_async(explicit="false", format_key=FMT_IMAGE) is False

    def test_visual_pdf_is_forced_async(self, visual_on):
        visual_on.DOCUMENT_VISUAL_ANALYSIS_SOURCES = "image,pdf"
        assert async_policy.resolve_async(explicit="false", format_key=FMT_PDF) is True

    def test_pdf_unchanged_while_source_closed(self, visual_on):
        """Source `pdf` hors allowlist : politique PDF strictement inchangée."""
        visual_on.DOCUMENT_VISUAL_ANALYSIS_SOURCES = "image"
        assert async_policy.resolve_async(explicit="false", format_key=FMT_PDF) is False

    def test_visual_docx_is_forced_async(self, visual_on):
        """un appel Gateway n'a pas sa place dans une requête HTTP.

        Prouvé INDÉPENDAMMENT de l'OCR : la porte OCR est fermée par la fixture, et
        l'override explicite `false` est ignoré. C'est donc bien la source visuelle
        `docx` qui force l'asynchrone, pas un effet de bord d'une autre politique.
        """
        visual_on.DOCUMENT_VISUAL_ANALYSIS_SOURCES = "image,docx"
        assert visual_on.DOCUMENT_OCR_ENABLED is False
        assert async_policy.resolve_async(explicit="false", format_key=FMT_DOCX) is True

    def test_docx_unchanged_while_source_closed(self, visual_on):
        """Source `docx` hors allowlist : politique DOCX strictement inchangée."""
        visual_on.DOCUMENT_VISUAL_ANALYSIS_SOURCES = "image,pdf"
        assert async_policy.resolve_async(explicit="false", format_key=FMT_DOCX) is False

    def test_visual_pptx_is_forced_async(self, visual_on):
        """couvert par la politique VISUELLE elle-même."""
        visual_on.DOCUMENT_VISUAL_ANALYSIS_SOURCES = "pptx"
        assert async_policy.visual_forces_async(FMT_PPTX) is True
        assert async_policy.resolve_async(explicit="false", format_key=FMT_PPTX) is True

    @pytest.mark.parametrize("sources,enabled", [("image,pdf,docx", True), ("pptx", False)])
    def test_pptx_stays_async_outside_visual_policy(self, visual_on, sources, enabled):
        """PPTX reste TOUJOURS asynchrone, vision éteinte ou source fermée."""
        visual_on.DOCUMENT_VISUAL_ANALYSIS_ENABLED = enabled
        visual_on.DOCUMENT_VISUAL_ANALYSIS_SOURCES = sources
        assert async_policy.visual_forces_async(FMT_PPTX) is False
        assert async_policy.pptx_forces_async(FMT_PPTX) is True
        assert async_policy.resolve_async(explicit="false", format_key=FMT_PPTX) is True

    @pytest.mark.parametrize("fmt", ["docx", "txt"])
    def test_other_formats_unchanged(self, visual_on, fmt):
        assert async_policy.resolve_async(explicit="false", format_key=fmt) is False


class TestQueue:
    @pytest.mark.parametrize("name", ["scan.PNG", "photo.jpg", "photo.jpeg", "plan.tif",
                                      "plan.tiff", "pic.webp"])
    def test_image_routed_to_visual_queue_when_active(self, visual_on, name):
        assert queues.ingest_queue_for(filename=name) == queues.INGEST_VISUAL_QUEUE == "ingest_visual"

    def test_image_not_routed_when_off(self, settings):
        settings.DOCUMENT_VISUAL_ANALYSIS_ENABLED = False
        assert queues.ingest_queue_for(filename="photo.png") is None

    def test_pdf_primary_pass_never_waits_on_the_visual_queue(self, visual_on):
        """VISUAL-ENRICHMENT-DEFERRED : la passe PRIMAIRE d'un PDF part sur la file par
        défaut, source ouverte ou non — le document devient READY sans attendre la vision
        (auparavant : `ingest_visual` dès l'ouverture de la source)."""
        visual_on.DOCUMENT_VISUAL_ANALYSIS_SOURCES = "image,pdf"
        assert queues.ingest_queue_for(filename="rapport.PDF") is None

    def test_pdf_visual_primary_resumption_goes_to_the_visual_queue(self, visual_on):
        """Seule la reprise PRIMAIRE (aucune évidence hors visuels) va sur `ingest_visual`."""
        visual_on.DOCUMENT_VISUAL_ANALYSIS_SOURCES = "image,pdf"
        assert queues.ingest_queue_for(filename="rapport.PDF", visual_primary=True) == queues.INGEST_VISUAL_QUEUE

    def test_pdf_not_routed_when_source_closed(self, visual_on):
        visual_on.DOCUMENT_VISUAL_ANALYSIS_SOURCES = "image"
        assert queues.ingest_queue_for(filename="report.pdf") is None

    def test_docx_primary_pass_never_waits_on_the_visual_queue(self, visual_on):
        """VISUAL-ENRICHMENT-DEFERRED : la passe PRIMAIRE d'un DOCX part sur la file par
        défaut, source ouverte ou non — le document devient READY sans attendre la vision
        (auparavant : `ingest_visual` dès l'ouverture de la source)."""
        visual_on.DOCUMENT_VISUAL_ANALYSIS_SOURCES = "image,pdf,docx"
        assert queues.ingest_queue_for(filename="note.DOCX") is None

    def test_docx_visual_primary_resumption_goes_to_the_visual_queue(self, visual_on):
        """Seule la reprise PRIMAIRE (aucune évidence hors visuels) va sur `ingest_visual`."""
        visual_on.DOCUMENT_VISUAL_ANALYSIS_SOURCES = "image,pdf,docx"
        assert queues.ingest_queue_for(filename="note.DOCX", visual_primary=True) == queues.INGEST_VISUAL_QUEUE

    def test_docx_not_routed_when_source_closed(self, visual_on):
        """Porte `docx` fermée : routage DOCX strictement inchangé, même `image` ouverte."""
        visual_on.DOCUMENT_VISUAL_ANALYSIS_SOURCES = "image,pdf"
        assert queues.ingest_queue_for(filename="note.docx") is None

    def test_pptx_primary_pass_never_waits_on_the_visual_queue(self, visual_on):
        """VISUAL-ENRICHMENT-DEFERRED : la passe PRIMAIRE d'un PPTX part sur la file par
        défaut, source ouverte ou non — le document devient READY sans attendre la vision
        (auparavant : `ingest_visual` dès l'ouverture de la source)."""
        visual_on.DOCUMENT_VISUAL_ANALYSIS_SOURCES = "image,pdf,docx,pptx"
        assert queues.ingest_queue_for(filename="deck.PPTX") is None

    def test_pptx_visual_primary_resumption_goes_to_the_visual_queue(self, visual_on):
        """Seule la reprise PRIMAIRE (aucune évidence hors visuels) va sur `ingest_visual`."""
        visual_on.DOCUMENT_VISUAL_ANALYSIS_SOURCES = "image,pdf,docx,pptx"
        assert queues.ingest_queue_for(filename="deck.PPTX", visual_primary=True) == queues.INGEST_VISUAL_QUEUE

    def test_pptx_not_routed_when_off(self, settings):
        settings.DOCUMENT_VISUAL_ANALYSIS_ENABLED = False
        settings.DOCUMENT_VISUAL_ANALYSIS_SOURCES = "pptx"
        assert queues.ingest_queue_for(filename="deck.pptx") is None

    def test_pptx_not_routed_when_source_closed(self, visual_on):
        """Porte `pptx` fermée : file historique, même toutes les autres sources ouvertes."""
        visual_on.DOCUMENT_VISUAL_ANALYSIS_SOURCES = "image,pdf,docx"
        assert queues.ingest_queue_for(filename="deck.pptx") is None

    @pytest.mark.parametrize("name", ["data.xlsx", "a.txt", "maquette.ifc.txt", "macro.pptm"])
    def test_other_formats_never_routed_to_visual_queue(self, visual_on, name):
        visual_on.DOCUMENT_VISUAL_ANALYSIS_SOURCES = "image,pdf,docx,pptx"
        assert queues.ingest_queue_for(filename=name) is None
