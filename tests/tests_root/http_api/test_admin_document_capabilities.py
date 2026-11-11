"""GET /api/v1/admin/core-config/document-capabilities/.

Superuser actif seulement, lecture seule, états calculés depuis les accesseurs et résolveurs
existants, parité par consommateur, aucune ingestion ni inférence. Gateway et Redis SIMULÉS.
"""
from __future__ import annotations

import time

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from apps.rag_core.models import RagSpace
from apps.rag_core.services.capabilities import gateway_health, resolver, runtime
from apps.workspaces.models import Membership

URL = "/api/v1/admin/core-config/document-capabilities/"
pytestmark = pytest.mark.django_db

GW_OK = {
    "vision": {"service": "vision", "status": "AVAILABLE", "model_logical": "vision-large", "model_concrete": "m24b",
               "tasks": ["describe"], "describe_available": True, "semantic_enabled": False},
    "structured_extract": {"service": "structured_extract", "status": "AVAILABLE", "experimental": True,
                           "qualified_for_production": False, "task_qualification": {"chart": "NOT_QUALIFIED", "table": "PARTIAL"}},
    "asr": {"service": "asr", "status": "AVAILABLE", "model_logical": "asr-whisper-turbo"},
    "embeddings": {"service": "embeddings", "status": "AVAILABLE"},
    "rerank": {"service": "rerank", "status": "AVAILABLE"},
}


@pytest.fixture(autouse=True)
def _simulated_runtime(monkeypatch):
    state = {"gw": {k: dict(v) for k, v in GW_OK.items()}, "fps": []}
    monkeypatch.setattr(gateway_health, "snapshot", lambda refresh=False: {"services": state["gw"], "probed_at": time.time(), "cached": False})
    monkeypatch.setattr(runtime, "read_all", lambda: state["fps"])
    return state


@pytest.fixture
def root(db):
    return get_user_model().objects.create_superuser(username="root30b", password="pw", email="r@qa.invalid")


def _client(user):
    c = APIClient()
    c.force_authenticate(user=user)
    return c


def _get(client, **params):
    resp = client.get(URL, params)
    assert resp.status_code == 200, getattr(resp, "data", resp)
    return resp.json()


def _cap(body, cap_id):
    return next(c for c in body["capabilities"] if c["id"] == cap_id)


# ----------------------------------------------------------------------------- sécurité
def test_superuser_actif_200(root):
    body = _get(_client(root))
    assert body["read_only"] is True and body["environment"] and "capabilities" in body


@pytest.mark.parametrize("role", ["owner", "admin", "member", "viewer"])
def test_roles_de_workspace_refuses(workspace, role):
    u = get_user_model().objects.create_user(username=f"w-{role}", password="pw")
    Membership.objects.create(workspace=workspace, user=u, role=role)
    assert _client(u).get(URL).status_code == 403


def test_superuser_inactif_refuse(root):
    root.is_active = False
    root.save(update_fields=["is_active"])
    assert _client(root).get(URL).status_code in (401, 403)


def test_staff_non_superuser_refuse(db):
    u = get_user_model().objects.create_user(username="staff", password="pw", is_staff=True)
    assert _client(u).get(URL).status_code == 403


def test_anonyme_refuse():
    assert APIClient().get(URL).status_code in (401, 403)


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_lecture_seule(root, method):
    assert getattr(_client(root), method)(URL, {}).status_code == 405


def test_aucun_secret_ni_url_interne(root, settings):
    settings.AI_GATEWAY_BASE_URL = "http://secret-gw.internal:8100"
    settings.DOCUMENT_AUDIO_RAGSPACE_IDS = ["22222222-2222-2222-2222-222222222222"]
    raw = str(_client(root).get(URL).content)
    assert "secret-gw.internal" not in raw
    assert "22222222-2222" not in raw                     # aucun UUID de liste en vue globale


# ----------------------------------------------------------------------------- aucune mutation, aucune inférence
def test_consultation_sans_ecriture_ni_inference(root, rag_space, monkeypatch):
    from apps.llm_router.gateway import vision as vision_client
    from apps.rag_core.services.ingest import extraction
    from apps.rag_core.services.ingest.audio import gateway_asr

    def _boom(*a, **k):
        raise AssertionError("traitement métier déclenché par la consultation")

    monkeypatch.setattr(extraction, "extract_document", _boom)
    monkeypatch.setattr(gateway_asr, "submit", _boom)
    monkeypatch.setattr(vision_client.VisionGatewayClient, "analyze", _boom)
    c = _client(root)
    with CaptureQueriesContext(connection) as ctx:
        c.get(URL)
        c.get(URL, {"ragspace_id": str(rag_space.id)})
    writes = [q["sql"] for q in ctx.captured_queries
              if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) and "django_session" not in q["sql"]]
    assert writes == []


# ----------------------------------------------------------------------------- résolution
def test_global_conditionnel_vision_pdf_docx_pptx(root, settings):
    """PDF/DOCX/PPTX_INLINE_IMAGE_VISION : globalement activé, exécuté si une image est présente."""
    settings.DOCUMENT_VISUAL_ANALYSIS_ENABLED = True
    settings.DOCUMENT_VISUAL_ANALYSIS_SOURCES = ["image", "pdf", "docx", "pptx"]
    settings.DOCUMENT_PPTX_ENABLED = True
    body = _get(_client(root))
    for fmt in ("pdf", "docx", "pptx"):
        cap = _cap(body, f"{fmt}.inline_image.vision")
        assert cap["state"] == "ACTIVE_CONDITIONAL" and cap["activation_scope"] == "GLOBAL"
        assert cap["gateway_dependency"]["gateway_service"] == "vision"
        assert cap["output_indexed"] == "INDEXED" and cap["output_searchable"] is True
        assert cap["numeric_evidence_reliable"] is False
        assert _cap(body, f"{fmt}.inline_image.ocr")["state"] == "UNSUPPORTED"


def test_vision_desactivee(root, settings):
    settings.DOCUMENT_VISUAL_ANALYSIS_ENABLED = False
    body = _get(_client(root))
    assert _cap(body, "pdf.inline_image.vision")["state"] == "DISABLED"
    assert _cap(body, "pdf.inline_image.vision")["output_searchable"] is False


def test_images_de_courriel_ocr_local_jamais_vision(root, settings):
    """EMAIL_INLINE_IMAGE_LOCAL_OCR."""
    settings.DOCUMENT_EML_ENABLED = True
    settings.DOCUMENT_EMAIL_GLOBAL_ENABLED = True
    settings.DOCUMENT_EMAIL_ATTACHMENT_INGEST_ENABLED = True
    body = _get(_client(root))
    ocr = _cap(body, "eml.inline_image.ocr")
    assert ocr["state"] == "ACTIVE_CONDITIONAL" and ocr["gateway_dependency"]["uses_ai_gateway"] is False
    assert _cap(body, "eml.inline_image.vision")["state"] == "UNSUPPORTED"


def test_excel_images_et_graphiques_non_supportes(root, settings):
    """XLSX_IMAGES_UNSUPPORTED / XLSX_CHARTS_UNSUPPORTED, même avec les graphiques Office activés."""
    settings.DOCUMENT_OFFICE_CHART_DATA_ENABLED = True
    body = _get(_client(root))
    assert _cap(body, "xlsx.images")["state"] == "UNSUPPORTED"
    assert _cap(body, "xlsx.charts")["state"] == "UNSUPPORTED"
    assert _cap(body, "docx.office_charts")["state"] == "ACTIVE_CONDITIONAL"


def test_audio_global_et_non_probant(root, settings, rag_space):
    """AUDIO_GLOBAL_ASR + ASR_NON_PROBATIVE ; un espace créé ensuite est admis sans liste."""
    settings.DOCUMENT_AUDIO_INGEST_ENABLED = True
    settings.DOCUMENT_AUDIO_GLOBAL_ENABLED = True
    settings.DOCUMENT_AUDIO_RAGSPACE_IDS = []
    body = _get(_client(root), ragspace_id=str(rag_space.id))
    asr = _cap(body, "audio.asr")
    assert asr["state"] == "ACTIVE_GLOBAL" and asr["activation_scope"] == "GLOBAL"
    assert asr["gateway_dependency"]["gateway_service"] == "asr"
    assert asr["gateway_dependency"]["model_logical"] == settings.AUDIO_ASR_MODEL_LOGICAL
    assert asr["numeric_evidence_class"] == "asr_transcript" and asr["numeric_evidence_reliable"] is False
    assert asr["space_applicable"] is True
    fmt = next(f for f in body["formats"] if f["format_id"] == "audio")
    assert fmt["max_upload_bytes"] == 600_000_000


def test_audio_par_liste(root, settings, rag_space):
    settings.DOCUMENT_AUDIO_INGEST_ENABLED = True
    settings.DOCUMENT_AUDIO_GLOBAL_ENABLED = False
    settings.DOCUMENT_AUDIO_RAGSPACE_IDS = [str(rag_space.id)]
    body = _get(_client(root))
    asr = _cap(body, "audio.asr")
    assert asr["state"] == "ACTIVE_SCOPED" and asr["scope"]["admitted_spaces"] == 1


def test_liste_vide_inoperante(root, settings):
    """PDF_VECTOR_FIGURE_EMPTY_SCOPE : flag allumé, aucun espace → inopérant, jamais « actif »."""
    settings.DOCUMENT_VISUAL_ANALYSIS_ENABLED = True
    settings.DOCUMENT_VISUAL_ANALYSIS_SOURCES = ["pdf"]
    settings.DOCUMENT_PDF_VECTOR_REGION_RENDER_ENABLED = True
    settings.DOCUMENT_PDF_VECTOR_REGION_RAGSPACE_IDS = []
    body = _get(_client(root))
    cap = _cap(body, "pdf.vector_figures")
    assert cap["state"] == "INCONSISTENT" and cap["scope"]["admitted_spaces"] == 0
    assert any(w["code"] == "SCOPE_EMPTY" and w["capability_id"] == "pdf.vector_figures" for w in body["warnings"])


def test_entree_de_liste_vers_espace_supprime(root, settings):
    settings.DOCUMENT_EMAIL_RAGSPACE_IDS = ["33333333-3333-3333-3333-333333333333"]
    body = _get(_client(root))
    assert any(w["code"] == "SCOPE_STALE_ENTRY" and w["scope"] == "email" for w in body["warnings"])


def test_structx_restreint_et_non_qualifie(root, settings, rag_space):
    """STRUCTX_SCOPED : service disponible ≠ capacité globale ; graphiques non qualifiés."""
    settings.DOCUMENT_VISUAL_ANALYSIS_ENABLED = True
    settings.STRUCTURED_VISUAL_ENABLED = True
    settings.STRUCTURED_VISUAL_ALLOWED_TASKS = []
    settings.STRUCTURED_VISUAL_RAGSPACE_ALLOWLIST = [str(rag_space.id)]
    body = _get(_client(root))
    table = _cap(body, "visual.structx.table")
    assert table["state"] == "ACTIVE_SCOPED" and table["scope"]["admitted_spaces"] == 1
    chart = _cap(body, "visual.structx.chart")
    assert chart["qualification_status"] == "NOT_QUALIFIED"
    sx = next(s for s in body["gateway_services"] if s["service"] == "structured_extract")
    assert sx["task_qualification"]["chart"] == "NOT_QUALIFIED"


def test_structx_plafond_de_production(root, settings, rag_space):
    settings.DJANGO_ENV = "prod"
    settings.DOCUMENT_VISUAL_ANALYSIS_ENABLED = True
    settings.STRUCTURED_VISUAL_ENABLED = True
    settings.STRUCTURED_VISUAL_PRODUCTION_ALLOWED = True
    settings.STRUCTURED_VISUAL_ALLOWED_TASKS = []
    settings.STRUCTURED_VISUAL_RAGSPACE_ALLOWLIST = [str(rag_space.id)]
    body = _get(_client(root))
    assert _cap(body, "visual.structx.table")["state"] == "ACTIVE_SCOPED"
    assert _cap(body, "visual.structx.chart")["state"] == "DISABLED"


def test_vision_v2_interne_jamais_recherchable(root, settings):
    settings.VISUAL_V2_ORCHESTRATION_ENABLED = True
    body = _get(_client(root))
    v2 = _cap(body, "visual.vision_v2")
    assert v2["output_indexed"] == "INTERNAL_ONLY" and v2["output_searchable"] is False


def test_gateway_indisponible(root, settings, _simulated_runtime):
    """UNAVAILABLE_GATEWAY : capacité autorisée mais service KO ; le reste reste consultable."""
    settings.DOCUMENT_AUDIO_INGEST_ENABLED = True
    settings.DOCUMENT_AUDIO_GLOBAL_ENABLED = True
    _simulated_runtime["gw"]["asr"] = {"service": "asr", "status": "UNAVAILABLE", "reason": "timeout"}
    body = _get(_client(root))
    assert _cap(body, "audio.asr")["state"] == "UNAVAILABLE"
    assert _cap(body, "pdf.text")["state"] == "ACTIVE_GLOBAL"
    assert any(w["code"] == "SERVICE_UNAVAILABLE" and w["service"] == "asr" for w in body["warnings"])


def test_porte_en_erreur_donne_unknown(root, monkeypatch):
    """INVALID_CONFIGURATION / MISSING_FLAG : jamais ACTIVE ni DISABLED par déduction."""
    def _raise():
        raise ValueError("config illisible")

    monkeypatch.setitem(resolver.GATES, "ocr", _raise)
    cap = _cap(_get(_client(root)), "pdf.ocr")
    assert cap["state"] == "UNKNOWN" and cap["configuration_error"] == "ValueError"


def test_flag_absent_des_settings_retombe_sur_le_defaut_ferme(root, settings):
    del settings.DOCUMENT_TABLE_OCR_ENABLED
    assert _cap(_get(_client(root)), "pdf.tables.ocr")["state"] == "DISABLED"


def test_divergence_reelle_marque_incoherent(root, settings, _simulated_runtime):
    """CONFIGURATION_MISMATCH : le worker_audio n'a pas chargé la même valeur."""
    settings.DOCUMENT_AUDIO_INGEST_ENABLED = True
    settings.DOCUMENT_AUDIO_GLOBAL_ENABLED = True
    digests = runtime.local_digests()
    other = {**digests, "DOCUMENT_AUDIO_GLOBAL_ENABLED": runtime.value_digest(False)}
    _simulated_runtime["fps"] = [{"schema_version": 1, "service_role": "worker_audio", "process_identity": "a:1",
                                  "deployment_sha": runtime.deployment_sha(), "digests": other, "observed_at": time.time()}]
    body = _get(_client(root))
    assert _cap(body, "audio.asr")["state"] == "INCONSISTENT"
    assert body["runtime_consistency"]["status"] == "MISMATCH"


def test_non_consommateur_ne_cree_pas_de_divergence(root, settings, _simulated_runtime):
    settings.TABULAR_QUERY_ENABLED = True
    other = {**runtime.local_digests(), "TABULAR_QUERY_ENABLED": runtime.value_digest(False)}
    _simulated_runtime["fps"] = [{"schema_version": 1, "service_role": "worker", "process_identity": "w:1",
                                  "deployment_sha": runtime.deployment_sha(), "digests": other, "observed_at": time.time()}]
    body = _get(_client(root))
    assert body["runtime_consistency"]["mismatches"] == []
    assert _cap(body, "xlsx.tabular_query")["state"] == "ACTIVE_CONDITIONAL"


def test_worker_perime_signale(root, _simulated_runtime):
    _simulated_runtime["fps"] = [{"schema_version": 1, "service_role": "worker_visual", "process_identity": "v:1",
                                  "deployment_sha": runtime.deployment_sha(), "digests": runtime.local_digests(),
                                  "observed_at": time.time() - 999}]
    body = _get(_client(root))
    assert any(w["code"] == "WORKER_STALE" and w["service"] == "worker_visual" for w in body["warnings"])


def test_fournisseur_d_embeddings_par_espace(root, workspace):
    """EMBEDDING_PROVIDER_PER_RAGSPACE : calculé depuis les RagSpace, pas depuis le défaut global."""
    RagSpace.objects.create(name="gw", workspace=workspace, embedding_provider="ai_gateway", embedding_model="legacy-768")
    direct = RagSpace.objects.create(name="ol", workspace=workspace, embedding_provider="ollama", embedding_model="nomic-embed-text")
    body = _get(_client(root), ragspace_id=str(direct.id))
    emb = body["embeddings"]
    assert emb["ai_gateway_spaces"] >= 1 and emb["ollama_direct_spaces"] >= 1
    assert emb["selected_space"] == {"provider": "ollama", "model": "nomic-embed-text", "via_ai_gateway": False}
    assert body["ragspace_view"]["id"] == str(direct.id)


def test_ragspace_invalide_ou_inconnu(root):
    c = _client(root)
    assert c.get(URL, {"ragspace_id": "pas-un-uuid"}).status_code == 400
    assert c.get(URL, {"ragspace_id": "44444444-4444-4444-4444-444444444444"}).status_code == 404


def test_matrice_et_formats(root):
    body = _get(_client(root))
    assert body["summary"]["extensions"] == 53  # édition Open : plus de `.msg`
    rows = {r["format_id"]: r for r in body["matrix"]}
    assert {"pdf", "docx", "pptx", "xlsx", "eml", "image", "audio"} <= set(rows)
    assert "msg" not in rows
    assert set(rows["pdf"]["cells"]) == {"text", "ocr", "images", "vision", "tables", "charts", "audio", "gateway"}
    refused = next(f for f in body["formats"] if f["format_id"] == "macro")
    assert refused["status"] == "UNSUPPORTED" and refused["upload_enabled"] is False
    assert next(f for f in body["formats"] if f["format_id"] == "text_fallback")["status"] == "TEXT_FALLBACK"


def test_actualisation_bornee(root, monkeypatch):
    calls = []
    monkeypatch.setattr(gateway_health, "snapshot", lambda refresh=False: calls.append(refresh) or {"services": GW_OK, "probed_at": 0})
    c = _client(root)
    c.get(URL, {"refresh": "1"})
    c.get(URL, {"refresh": "1"})                     # dans la fenêtre : cache conservé
    assert calls == [True, False]
