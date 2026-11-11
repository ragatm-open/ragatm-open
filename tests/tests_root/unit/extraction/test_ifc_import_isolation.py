"""ifcopenshell n'est JAMAIS importé au démarrage (import paresseux).

Preuve en sous-processus (le processus de test courant importe ifcopenshell via les
fixtures) : démarrer Django, importer le registre d'extraction et la validation, et
vérifier que `ifcopenshell` n'est pas dans `sys.modules`. L'import réel n'a lieu qu'à
l'extraction d'un IFC.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[4] / "backend"

PROBE = r"""
import os, sys
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
import django; django.setup()
import apps.rag_core.services.ingest.extraction as E
from apps.rag_core.services.ingest.extraction import validation, ifc_extractor
from apps.rag_core.services.ingest import async_policy, queues
from api.v1.documents import views_common
print("ifcopenshell_loaded=" + str("ifcopenshell" in sys.modules))
print("available=" + str(ifc_extractor.parser_available()))
"""


def test_startup_does_not_import_ifcopenshell():
    env = {**os.environ, "EMBEDDINGS_PROVIDER": "stub"}
    out = subprocess.run([sys.executable, "-c", PROBE], cwd=str(BACKEND), env=env,
                         capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stderr[-2000:]
    assert "ifcopenshell_loaded=False" in out.stdout, out.stdout
    assert "available=True" in out.stdout
