"""la tâche vision opérationnelle est une GOUVERNANCE, pas un réglage.

Fait consommé (AI-GATEWAY-VISION-1B) : seule `describe` est qualifiée côté Gateway ; les
autres tâches V1 sont refusées en échec fermé (400 `vision_task_not_qualified`). Le
vocabulaire V1 et les normaliseurs restent complets : ce sont les capacités futures.

Ces tests existent pour qu'un changement de tâche d'une source soit DÉLIBÉRÉ et visible.
"""
from __future__ import annotations

import pathlib

import pytest

from apps.llm_router.gateway import vision_contract as V1
from apps.rag_core.services.ingest.visual import capability as CAP
from apps.rag_core.services.ingest.visual.contracts import VisualAsset, sha256_hex

BACKEND = pathlib.Path(__file__).resolve().parents[4] / "backend"
IMAGE_EXTRACTOR = BACKEND / "apps/rag_core/services/ingest/extraction/image_extractor.py"
VISUAL_PACKAGE = BACKEND / "apps/rag_core/services/ingest/visual"


class TestQualifiedCapability:
    def test_standalone_image_uses_describe(self):
        assert CAP.visual_task_for_source("image") == "describe"
        assert CAP.STANDALONE_IMAGE_VISUAL_TASK == "describe"

    def test_describe_is_the_only_qualified_task(self):
        assert CAP.QUALIFIED_TASKS == frozenset({"describe"})

    def test_unqualified_tasks_remain_valid_v1_vocabulary(self):
        """Le contrat V1 n'est pas amputé : ces tâches existent, elles ne sont pas servies."""
        assert CAP.UNQUALIFIED_TASKS == frozenset({"auto", "table", "diagram", "chart",
                                                   "technical_drawing"})
        assert CAP.UNQUALIFIED_TASKS < V1.TASKS
        assert CAP.QUALIFIED_TASKS | CAP.UNQUALIFIED_TASKS == V1.TASKS
        assert not (CAP.QUALIFIED_TASKS & CAP.UNQUALIFIED_TASKS)

    def test_pdf_visuals_use_describe(self):
        """la source `pdf` est DÉCLARÉE, et sur la même tâche.

        Un visuel intégré peut bien être un graphique ou un tableau à l'œil : tant que
        `chart`/`table` ne sont pas qualifiées, il est décrit en texte libre, rien de plus.
        """
        assert CAP.visual_task_for_source("pdf") == "describe"
        assert CAP.PDF_VISUAL_TASK == "describe"

    def test_docx_visuals_use_describe(self):
        """la source `docx` est DÉCLARÉE, et sur la même tâche.

        Un visuel intégré à un DOCX est souvent un organigramme ou un graphique collé
        depuis un tableur : tant que `diagram`/`chart` ne sont pas qualifiées, il est
        décrit en texte libre, rien de plus.
        """
        assert CAP.visual_task_for_source("docx") == "describe"
        assert CAP.DOCX_VISUAL_TASK == "describe"

    def test_pptx_visuals_use_describe(self):
        """la source `pptx` est DÉCLARÉE, et sur la même tâche.

        Une image de diapositive peut représenter un tableau ou un graphique : tant que
        `table`/`chart` ne sont pas qualifiées, elle est décrite en texte libre.
        """
        assert CAP.visual_task_for_source("pptx") == "describe"
        assert CAP.PPTX_VISUAL_TASK == "describe"

    def test_declared_sources_are_exactly_the_operational_ones(self):
        """Aucune dérive : les quatre sources, toutes sur `describe`, et rien d'autre."""
        from apps.rag_core.services.ingest.extraction import config

        declared = {s: CAP.visual_task_for_source(s) for s in ("image", "pdf", "docx", "pptx")}
        assert declared == {"image": "describe", "pdf": "describe",
                            "docx": "describe", "pptx": "describe"}
        assert set(declared) == set(config.VISUAL_OPERATIONAL_SOURCES)

    @pytest.mark.parametrize("source", ["email", "xlsx", "pptm", ""])
    def test_undeclared_source_fails_closed(self, source):
        """Une source future doit déclarer sa tâche EXPLICITEMENT.

        `docx` figurait ici jusqu'à, `pptx` jusqu'à,
        qui les ont déclarées. Le principe, lui, ne bouge pas : aucune source ne reçoit de
        tâche par défaut.
        """
        with pytest.raises(KeyError):
            CAP.visual_task_for_source(source)


class TestNotConfigurable:
    def test_no_runtime_task_setting_exists(self, settings):
        """Aucun réglage d'exploitation ne choisit la tâche : un réglage inventé est ignoré."""
        settings.DOCUMENT_VISUAL_TASK = "auto"
        assert CAP.visual_task_for_source("image") == "describe"

    def test_no_task_setting_is_read_anywhere(self):
        sources = list(VISUAL_PACKAGE.glob("*.py")) + [IMAGE_EXTRACTOR]
        for path in sources:
            src = path.read_text(encoding="utf-8")
            assert "DOCUMENT_VISUAL_TASK" not in src, path.name


class TestSourceAdapter:
    def test_standalone_adapter_does_not_hardcode_a_task(self):
        src = IMAGE_EXTRACTOR.read_text(encoding="utf-8")
        assert 'task="auto"' not in src and "task='auto'" not in src
        assert 'visual_task_for_source("image")' in src

    def test_generic_contract_default_stays_auto(self):
        """Le DÉFAUT générique du contrat reste `auto` : contrat != stratégie qualifiée."""
        asset = VisualAsset(content=b"x", mime_type="image/png", width=1, height=1,
                            source_kind="image", content_hash=sha256_hex(b"x"), ordinal=0)
        assert asset.task == "auto"

    def test_no_automatic_fallback_between_tasks(self):
        """Aucun repli `auto` -> `describe` : une seule requête gouvernée est émise."""
        for path in list(VISUAL_PACKAGE.glob("*.py")) + [IMAGE_EXTRACTOR]:
            src = path.read_text(encoding="utf-8")
            assert "vision_task_not_qualified" not in src or path.name == "capability.py", path.name

    def test_no_capability_probe_per_document(self):
        for path in list(VISUAL_PACKAGE.glob("*.py")) + [IMAGE_EXTRACTOR]:
            src = path.read_text(encoding="utf-8")
            for probe in ("/v1/models", "/health/vision", "capabilities"):
                assert probe not in src, f"{path.name}: {probe}"
