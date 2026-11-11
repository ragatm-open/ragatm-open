"""
Sécurité du téléchargement des fichiers originaux uploadés.

On vérifie que la couche de durcissement (`_apply_download_security`) neutralise
le scénario d'XSS stocké : un HTML/SVG/XML uploadé ne doit jamais être servi
`inline` avec son Content-Type actif, et la réponse doit porter les en-têtes de
sécurité (`nosniff`, CSP sandbox).
"""

import pytest
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.http import HttpResponse
from rest_framework.test import APIClient

from api.v1.documents.views_read import _apply_download_security
from apps.rag_core.models import Document, RagSpace
from apps.workspaces.models import Membership, Workspace


def _build(ctype, *, filename="f", as_download=False):
    resp = HttpResponse(b"x", content_type=ctype)
    _apply_download_security(resp, filename=filename, ctype=ctype, as_download=as_download)
    return resp


@pytest.mark.parametrize(
    "ctype",
    [
        "text/html",
        "text/html; charset=utf-8",
        "application/xhtml+xml",
        "image/svg+xml",
        "application/xml",
        "text/xml",
        "application/javascript",
        "application/x-custom+xml",
    ],
)
def test_active_types_forced_attachment_and_neutralized(ctype):
    resp = _build(ctype)  # même sans ?download=1
    assert resp["Content-Disposition"].startswith("attachment;")
    assert resp["Content-Type"] == "application/octet-stream"
    assert resp["X-Content-Type-Options"] == "nosniff"
    assert "sandbox" in resp["Content-Security-Policy"]


@pytest.mark.parametrize("ctype", ["application/pdf", "image/png", "text/plain", "text/csv"])
def test_safe_types_previewable_inline(ctype):
    resp = _build(ctype)
    assert resp["Content-Disposition"].startswith("inline;")
    assert resp["Content-Type"] == ctype
    assert resp["X-Content-Type-Options"] == "nosniff"
    # Pas de sandbox sur l'aperçu sûr (PDF/image), pour ne pas casser le viewer.
    assert "Content-Security-Policy" not in resp


def test_download_flag_forces_attachment_even_for_safe_types():
    resp = _build("application/pdf", as_download=True)
    assert resp["Content-Disposition"].startswith("attachment;")
    assert "sandbox" in resp["Content-Security-Policy"]


def test_unknown_type_defaults_to_attachment():
    resp = _build("application/vnd.whatever")
    assert resp["Content-Disposition"].startswith("attachment;")
    assert resp["X-Content-Type-Options"] == "nosniff"


def test_filename_header_injection_is_stripped():
    resp = _build("text/html", filename='evil";\r\nSet-Cookie: x=1')
    disp = resp["Content-Disposition"]
    assert "\r" not in disp and "\n" not in disp
    assert "Set-Cookie" not in resp  # pas d'injection d'en-tête


# --- Tests d'intégration de la vue de téléchargement (A5) -------------------


@pytest.fixture
def _download_ctx(db):
    User = get_user_model()
    user = User.objects.create_user(username="dl", password="pw")
    ws = Workspace.objects.create(name="WS-DL")
    Membership.objects.create(workspace=ws, user=user, role=Membership.ROLE_MEMBER)
    space = RagSpace.objects.create(name="space-dl", workspace=ws)
    client = APIClient()
    client.force_authenticate(user=user)
    return client, space


def test_download_serves_storage_file_bytes(_download_ctx):
    client, space = _download_ctx
    doc = Document.objects.create(
        rag_space=space,
        filename="hello.txt",
        content_type="text/plain",
    )
    doc.storage_file.save("hello.txt", ContentFile(b"hello-world"), save=True)

    res = client.get(f"/api/v1/documents/{doc.id}/download/")
    assert res.status_code == 200
    assert b"".join(res.streaming_content) == b"hello-world"
    assert res["X-Content-Type-Options"] == "nosniff"


def test_download_active_type_keeps_security_headers(_download_ctx):
    client, space = _download_ctx
    doc = Document.objects.create(
        rag_space=space,
        filename="x.html",
        content_type="text/html",
    )
    doc.storage_file.save("x.html", ContentFile(b"<script>alert(1)</script>"), save=True)

    res = client.get(f"/api/v1/documents/{doc.id}/download/")
    assert res.status_code == 200
    # type actif neutralisé même sans ?download=1
    assert res["Content-Disposition"].startswith("attachment;")
    assert res["Content-Type"] == "application/octet-stream"
    assert res["X-Content-Type-Options"] == "nosniff"
    assert "sandbox" in res["Content-Security-Policy"]


def test_download_without_storage_file_returns_404(_download_ctx):
    client, space = _download_ctx
    doc = Document.objects.create(
        rag_space=space,
        filename="missing.txt",
        content_type="text/plain",
    )
    # Aucun storage_file enregistré -> 404 explicite, pas de fallback disque.
    res = client.get(f"/api/v1/documents/{doc.id}/download/")
    assert res.status_code == 404
    assert res.data["detail"] == "no_file_available"


def test_download_legacy_path_attribute_is_never_read(_download_ctx, monkeypatch):
    """Même si une valeur `path`/`file_path` réapparaît sur l'instance, la vue ne
    doit jamais l'ouvrir (anti path-traversal / LFI). On l'injecte dynamiquement et
    on vérifie qu'on obtient 404 et non le contenu d'un fichier hors storage."""
    client, space = _download_ctx
    doc = Document.objects.create(
        rag_space=space,
        filename="missing.txt",
        content_type="text/plain",
    )

    # On simule un Document compromis exposant un chemin arbitraire.
    from apps.rag_core.models.document import Document as DocModel

    monkeypatch.setattr(DocModel, "file_path", "/etc/passwd", raising=False)
    monkeypatch.setattr(DocModel, "path", "/etc/passwd", raising=False)

    res = client.get(f"/api/v1/documents/{doc.id}/download/")
    assert res.status_code == 404
    assert res.data["detail"] == "no_file_available"
