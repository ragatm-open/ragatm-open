"""enveloppe de conversion sécurisée + réutilisation du DxfExtractor.

Faux convertisseur (`_dwg_fixtures.make_fake_converter`) : prouve argv/tempdir/nettoyage/
délai/tailles/sorties/provenance — jamais que LibreDWG convertit un AC1032 (hors dépôt).
"""
from __future__ import annotations

import importlib
import json
import os
import tempfile
import time

import pytest
from apps.rag_core.services.ingest.extraction import ExtractionError, extract_document
from apps.rag_core.services.ingest.extraction.base import ExtractionContext
from django.test import override_settings

from . import _dwg_fixtures as F

pytestmark = pytest.mark.usefixtures("_dwg_on")
SEMANTIC_PATHS = ["Drawing", "Layout:Model/Layer:CVC", "Layout:Model/Layer:CVC/Block:EQUIP", "Layout:RDC/Layer:0"]


@pytest.fixture
def _dwg_on():
    with override_settings(DOCUMENT_DWG_ENABLED=True, DOCUMENT_DWG_CONVERSION_TIMEOUT_SECONDS=60):
        yield


@pytest.fixture
def fake(tmp_path):
    """Fabrique un faux convertisseur et pointe la configuration dessus."""
    def _make(mode="ok", payload=None, version="dwg2dxf 0.14", size=0):
        d = tmp_path / f"conv-{mode}"
        script = F.make_fake_converter(d, mode=mode, payload=payload, version=version, size=size)
        return d, script
    return _make


@pytest.fixture
def tempdirs(monkeypatch):
    """Enregistre tout répertoire temporaire créé pendant le test (preuve de nettoyage).

    L'extraction s'exécute dans un processus enfant (HARDENING-03) : une liste en
    mémoire y resterait confinée, et `assert not tempdirs` deviendrait une tautologie
    — vraie parce qu'on n'observe rien, pas parce que rien n'a été créé. Le témoin
    franchit la frontière et rend à ces assertions leur pouvoir de détection.
    """
    from tests_root.process_witness import TemoinPartage

    created = TemoinPartage(prefixe="dwg-tempdirs-")
    real = tempfile.mkdtemp

    def spy(*a, **k):
        p = real(*a, **k)
        # Le bac d'isolation de l'extraction (HARDENING-03) passe aussi par `mkdtemp`,
        # mais c'est de l'INFRASTRUCTURE, pas un temporaire d'extracteur : le compter
        # ferait échouer les cas où l'extracteur ne doit rien créer du tout.
        if not os.path.basename(p).startswith("ragatm-extraction-"):
            created.note(p)
        return p

    monkeypatch.setattr(tempfile, "mkdtemp", spy)
    return created


def _x(data: bytes, script, name="plan.dwg", ct="application/acad"):
    with override_settings(DOCUMENT_DWG_CONVERTER_PATH=str(script)):
        return extract_document(data, filename=name, content_type=ct)


def _err(data: bytes, script, name="plan.dwg"):
    with pytest.raises(ExtractionError) as ei:
        _x(data, script, name=name)
    return ei.value


def _mod():
    return importlib.import_module("apps.rag_core.services.ingest.extraction.dwg_extractor")


class TestConversionSuccess:
    def test_dwg_4_units_come_from_the_dxf_extractor(self, fake, tempdirs):
        d, script = fake("ok")
        res = _x(F.dwg_bytes(), script)
        assert res.text == "" and [u.meta["locator"]["path"] for u in res.units] == SEMANTIC_PATHS
        joined = "\n".join(u.text for u in res.units)
        assert "CTA-DWG-01" in joined and "Débit 15000 m3/h température 21 °" in joined
        assert "REPERE: CTA-DWG-01" in joined and "DEBIT: 15000 m3/h" in joined and "Dimension: 2500 mm" in joined
        assert "Tableau TGBT" in joined and "Declared XREF: PLAN_EXT -> " + F.XREF_PATH in res.units[0].text
        assert tempdirs and not any(os.path.exists(p) for p in tempdirs)  # DWG-45

    def test_dwg_46_argv_cwd_and_fixed_names_are_independent_of_the_user_filename(self, fake, monkeypatch):
        d, script = fake("ok")
        monkeypatch.setenv("DB_PASSWORD", "s3cret-db")
        monkeypatch.setenv("DJANGO_SECRET_KEY", "s3cret-key")
        evil = 'plan; rm -rf / $(id) `id` "a" \'b\' ../../etc/passwd.dwg'
        res = _x(F.dwg_bytes(), script, name=evil)
        assert len(res.units) == 4
        assert F.record(d, "argv.txt").split("\n")[:4] == ["-y", "-o", "output.dxf", "input.dwg"]
        assert F.record(d, "input_name.txt") == "input.dwg"
        cwd = F.record(d, "cwd.txt").strip()
        assert os.path.basename(cwd).startswith("ragatm-dwg-") and not os.path.exists(cwd)
        assert F.record(d, "cwd_mode.txt").strip() == "700" and F.record(d, "input_mode.txt").strip() == "600"
        env = F.record(d, "env.txt")
        assert "s3cret" not in env and "DB_PASSWORD" not in env and "DJANGO_SECRET_KEY" not in env
        assert f"TMPDIR={cwd}" in env and "PATH=" in env
        assert "plan; rm" not in F.record(d, "argv.txt") and "passwd" not in F.record(d, "argv.txt")

    def test_original_bytes_are_staged_verbatim(self, fake):
        import hashlib

        d, script = fake("ok")
        data = F.dwg_bytes(size=4000)
        _x(data, script)
        assert F.record(d, "input_size.txt").strip() == str(len(data))
        assert F.record(d, "input_sha.txt").strip() == hashlib.sha256(data).hexdigest()

    def test_dwg_21_22_23_24_provenance(self, fake):
        d, script = fake("ok")
        res = _x(F.dwg_bytes(), script)
        for u in res.units:
            loc = u.meta["locator"]
            assert loc["type"] == "dwg" and loc.get("identifier") is None  # handle DXF jamais exposé
            assert u.meta["structured_format"] == "dwg"
        blob = json.dumps({"units": [(u.text, u.meta) for u in res.units], "meta": res.metadata, "w": res.warnings})
        assert "/tmp" not in blob and "output.dxf" not in blob and "input.dwg" not in blob and "ragatm-dwg-" not in blob
        assert "handle:" not in blob

    def test_metadata_contract(self, fake):
        d, script = fake("ok")
        m = _x(F.dwg_bytes(), script).metadata
        assert m["format"] == "dwg" and m["converter"] == "libredwg" and m["converter_version"] == "0.14"
        assert m["source_dwg_version"] == "AC1032" and m["source_dwg_release"] == "R2018"
        assert m["intermediate_format"] == "dxf" and m["intermediate_dxf_version"] == "AC1032"
        assert m["parser"] == "ezdxf" and m["parser_version"]
        assert m["entity_count"] > 0 and "entity_counts" in m and m["semantic_unit_count"] == 4
        assert m["xrefs_count"] == 1 and m["xrefs_declared"][0]["path"] == F.XREF_PATH
        assert m["unknown_entity_count"] == 0 and m["unknown_entity_counts"] == {}
        assert m["converter_warning_count"] == 0 and m["intermediate_dxf_bytes"] > 0
        assert "dxf_binary" not in m and "mime" not in m or m.get("mime") in (None, "application/acad")

    def test_dwg_11_crlf_output_via_r1(self, fake):
        d, script = fake("ok", payload=F.to_crlf(F.semantic_dxf_lf()))
        a = _x(F.dwg_bytes(), script)
        d2, script2 = fake("ok")
        b = _x(F.dwg_bytes(), script2)
        assert [(u.text, u.meta) for u in a.units] == [(u.text, u.meta) for u in b.units]

    def test_dwg_12_exotic_entity_output_via_r2(self, fake):
        d, script = fake("ok", payload=F.semantic_dxf_with_exotic())
        res = _x(F.dwg_bytes(), script)
        assert [u.meta["locator"]["path"] for u in res.units] == SEMANTIC_PATHS
        assert res.metadata["unknown_entity_counts"] == {"SECTIONOBJECT": 1}
        assert "dxf_unknown_entities" in res.warnings

    def test_dwg_42_determinism(self, fake):
        d, script = fake("ok")
        a, b = _x(F.dwg_bytes(), script), _x(F.dwg_bytes(), script)
        assert [(u.text, u.meta) for u in a.units] == [(u.text, u.meta) for u in b.units]
        drop = {"conversion_seconds"}
        assert {k: v for k, v in a.metadata.items() if k not in drop} == {k: v for k, v in b.metadata.items() if k not in drop}
        assert a.warnings == b.warnings

    def test_converter_warnings_are_counted_not_stored(self, fake):
        d, script = fake("ok_warn")
        res = _x(F.dwg_bytes(), script)
        assert res.metadata["converter_warning_count"] == 1
        assert "dwg_converter_warnings" in res.warnings
        assert "HELIX" not in json.dumps(res.metadata)  # aucun stderr brut dans les métadonnées


class TestLongMtext:
    @pytest.mark.parametrize("style", ["0.14", "master"])
    def test_dwg_13_14_15_boundaries_unicode(self, fake, style):
        payload, expected = F.long_mtext_dxf(style)
        d, script = fake("ok", payload=payload)
        res = _x(F.dwg_bytes("AC1015"), script)
        joined = "\n".join(u.text for u in res.units)
        for key, txt in expected.items():
            assert txt in joined, key
        assert "m³/h" in joined and "°C" in joined and "Débit réglé" in joined
        assert "dxf_text_truncated" not in res.warnings


class TestControlledFailures:
    def test_dwg_3_dependency_absent(self, tempdirs):
        e = _err(F.dwg_bytes(), "/nonexistent/dwg2dxf")
        assert e.code == "dwg_dependency_unavailable" and not tempdirs

    def test_dwg_3b_converter_version_mismatch_is_environmental(self, fake, tempdirs):
        d, script = fake("ok", version="dwg2dxf 0.13.3")
        e = _err(F.dwg_bytes(), script)
        assert e.code == "dwg_dependency_unavailable" and "0.13.3" in json.dumps(e.meta or {})
        assert not tempdirs  # aucune conversion tentée

    def test_dwg_5_nonzero_exit(self, fake, tempdirs):
        d, script = fake("fail")
        e = _err(F.dwg_bytes(), script)
        assert e.code == "dwg_conversion_failed" and (e.meta or {}).get("converter_exit") == 3
        assert not any(os.path.exists(p) for p in tempdirs)

    def test_dwg_40_read_error_is_malformed(self, fake):
        d, script = fake("malformed")
        assert _err(F.dwg_bytes(), script).code == "dwg_malformed"

    def test_dwg_6_timeout_kills_the_process_group_and_cleans_up(self, fake, tempdirs):
        d, script = fake("sleep")
        t0 = time.monotonic()
        with override_settings(DOCUMENT_DWG_CONVERSION_TIMEOUT_SECONDS=1):
            e = _err(F.dwg_bytes(), script)
        assert e.code == "dwg_conversion_timeout" and time.monotonic() - t0 < 15
        assert not any(os.path.exists(p) for p in tempdirs)
        gc = F.record(d, "grandchild.txt").strip()
        assert gc, "le faux convertisseur n'a pas démarré son petit-fils"
        deadline = time.monotonic() + 3
        while F.pid_alive(int(gc)) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not F.pid_alive(int(gc)), "processus orphelin survivant au délai"

    def test_dwg_7_no_output(self, fake):
        d, script = fake("noout")
        e = _err(F.dwg_bytes(), script)
        assert e.code == "dwg_conversion_failed" and (e.meta or {}).get("reason") == "no_output"

    def test_dwg_8_empty_output(self, fake):
        d, script = fake("empty")
        e = _err(F.dwg_bytes(), script)
        assert e.code == "dwg_conversion_failed" and (e.meta or {}).get("reason") == "empty_output"

    def test_dwg_9_invalid_dxf_output_is_a_conversion_failure_not_recovered(self, fake):
        d, script = fake("invalid")
        e = _err(F.dwg_bytes(), script)
        assert e.code == "dwg_conversion_failed" and (e.meta or {}).get("reason") == "invalid_dxf"

    def test_dwg_10_oversized_intermediate(self, fake, tempdirs):
        d, script = fake("big", size=3 * 1024 * 1024)
        with override_settings(DOCUMENT_DWG_MAX_INTERMEDIATE_DXF_MB=2):
            e = _err(F.dwg_bytes(), script)
        assert e.code == "dwg_complexity_limit" and (e.meta or {}).get("intermediate_dxf_bytes") == 3 * 1024 * 1024
        assert not any(os.path.exists(p) for p in tempdirs)

    def test_dwg_47_noisy_stderr_is_bounded(self, fake, tempdirs):
        d, script = fake("noisy")
        res = _x(F.dwg_bytes(), script)
        assert len(res.units) == 4
        assert res.metadata["converter_warning_count"] >= 1000
        assert len(json.dumps(res.metadata)) < 8000
        assert not any(os.path.exists(p) for p in tempdirs)

    def test_converter_crash_is_controlled(self, fake, tempdirs):
        d, script = fake("crash")
        e = _err(F.dwg_bytes(), script)
        assert e.code == "dwg_conversion_failed" and (e.meta or {}).get("converter_exit", 0) < 0
        assert not any(os.path.exists(p) for p in tempdirs)

    def test_unsupported_version_is_refused_before_conversion(self, fake, tempdirs):
        d, script = fake("ok")
        e = _err(F.dwg_bytes("AC1014"), script)
        assert e.code == "dwg_unsupported_version" and not tempdirs

    def test_error_meta_never_contains_temp_paths(self, fake):
        d, script = fake("fail")
        e = _err(F.dwg_bytes(), script)
        assert e.code == "dwg_conversion_failed"
        blob = json.dumps(e.meta or {}) + str(e)
        assert "/tmp" not in blob and "ragatm-dwg-" not in blob and "input.dwg" not in blob


class TestConverterProbe:
    def test_converter_available_reads_the_version_once_per_binary(self, fake, monkeypatch):
        d, script = fake("ok")
        X = _mod()
        X._probe_cache_clear()
        with override_settings(DOCUMENT_DWG_CONVERTER_PATH=str(script)):
            calls = {"n": 0}
            real = X._run_version_probe

            def spy(path):
                calls["n"] += 1
                return real(path)

            monkeypatch.setattr(X, "_run_version_probe", spy)
            assert X.converter_available() is True
            _x(F.dwg_bytes(), script)
            _x(F.dwg_bytes(), script)
            assert calls["n"] == 1

    def test_no_shell_anywhere(self):
        """Analyse du CODE (AST), pas de la prose : aucun shell, aucune réparation ezdxf."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(_mod()))
        banned_calls, shell_true = [], []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = ast.unparse(node.func)
                if name in ("os.system", "os.popen", "subprocess.getoutput", "eval", "exec") or name.endswith(".recover"):
                    banned_calls.append(name)
                for kw in node.keywords:
                    if kw.arg == "shell" and not (isinstance(kw.value, ast.Constant) and kw.value.value is False):
                        shell_true.append(ast.unparse(node.func))
        assert banned_calls == [] and shell_true == []
        argv = [ast.unparse(n) for n in ast.walk(tree)
                if isinstance(n, ast.Call) and ast.unparse(n.func).startswith("subprocess.")]
        assert argv and all("shell=False" in c for c in argv), argv


def test_extraction_context_filename_is_kept_for_the_result(fake):
    d, script = fake("ok")
    with override_settings(DOCUMENT_DWG_CONVERTER_PATH=str(script)):
        from apps.rag_core.services.ingest.extraction import dwg_extractor as X

        res = X.DwgExtractor().extract(F.dwg_bytes(), ExtractionContext(filename="plan.dwg"))
    assert res.metadata["format"] == "dwg"
