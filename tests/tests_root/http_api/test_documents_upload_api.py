"""Tests de l'endpoint d'upload multipart (/documents/upload/).

Régression : le frontend envoie `rag_space_id` dans le CORPS multipart (pas en
query param). Le backend doit l'accepter dans les deux cas.
"""
import io

import pytest

from apps.rag_core.models import Chunk, Document

pytestmark = pytest.mark.django_db


def _txt(content=b"Paris est la capitale de la France. Contenu suffisant."):
    f = io.BytesIO(content)
    f.name = "note.txt"
    return f


def test_upload_accepts_rag_space_id_in_form_body(api_client, rag_space):
    # rag_space_id UNIQUEMENT dans le corps du formulaire (cas réel du front)
    res = api_client.post(
        "/api/v1/documents/upload/",
        data={"rag_space_id": str(rag_space.id), "file": _txt()},
        format="multipart",
    )
    assert res.status_code == 200, res.data
    assert res.data["ok"] is True
    doc_id = res.data["document"]["id"]
    assert Document.objects.get(id=doc_id).status == "READY"
    assert Chunk.objects.filter(document_id=doc_id).count() >= 1


def test_upload_accepts_rag_space_id_in_query(api_client, rag_space):
    res = api_client.post(
        f"/api/v1/documents/upload/?rag_space_id={rag_space.id}",
        data={"file": _txt()},
        format="multipart",
    )
    assert res.status_code == 200, res.data


def test_upload_missing_rag_space_id_returns_400(api_client, rag_space):
    res = api_client.post(
        "/api/v1/documents/upload/",
        data={"file": _txt()},
        format="multipart",
    )
    assert res.status_code == 400
    assert res.data["detail"] == "rag_space_id required"


def test_upload_unsupported_type_is_rejected(api_client, rag_space):
    exe = io.BytesIO(b"MZ\x90\x00" + b"\x00" * 80)
    exe.name = "evil.exe"
    res = api_client.post(
        "/api/v1/documents/upload/",
        data={"rag_space_id": str(rag_space.id), "file": exe},
        format="multipart",
    )
    # rejeté proprement (415 unsupported ou 400 selon le code), pas de 500
    assert res.status_code in (400, 415)


# --------------------------------------------------------------------------- #
# Import de dossier : métadonnées relative_path / source_folder
# --------------------------------------------------------------------------- #

def _named_txt(name: str, content=b"Contenu de test suffisant pour l'indexation."):
    f = io.BytesIO(content)
    f.name = name
    return f


def test_upload_persists_relative_path_metadata(api_client, rag_space):
    res = api_client.post(
        "/api/v1/documents/upload/",
        data={
            "rag_space_id": str(rag_space.id),
            "file": _named_txt("conges.txt"),
            "source_folder": "Ressources humaines",
            "relative_path": "procédures/conges.txt",
        },
        format="multipart",
    )
    assert res.status_code == 200, res.data
    doc = Document.objects.get(id=res.data["document"]["id"])
    # Un document DISTINCT par fichier ; provenance conservée en meta.import.
    assert doc.meta.get("import") == {
        "source_folder": "Ressources humaines",
        "relative_path": "procédures/conges.txt",
    }
    # L'ingestion normale a bien tourné (pipeline inchangé).
    assert doc.status == "READY"
    # Le storage_file n'utilise JAMAIS relative_path (basename only, anti-traversal).
    assert doc.storage_file and "procédures" not in doc.storage_file.name


def test_upload_normalizes_backslashes_in_relative_path(api_client, rag_space):
    res = api_client.post(
        "/api/v1/documents/upload/",
        data={
            "rag_space_id": str(rag_space.id),
            "file": _named_txt("a.txt"),
            "source_folder": "Docs",
            "relative_path": "sub\\a.txt",  # séparateur Windows
        },
        format="multipart",
    )
    assert res.status_code == 200, res.data
    doc = Document.objects.get(id=res.data["document"]["id"])
    assert doc.meta["import"]["relative_path"] == "sub/a.txt"


def test_upload_rejects_traversal_relative_path(api_client, rag_space):
    res = api_client.post(
        "/api/v1/documents/upload/",
        data={
            "rag_space_id": str(rag_space.id),
            "file": _named_txt("secret.txt"),
            "relative_path": "../secret.txt",
        },
        format="multipart",
    )
    assert res.status_code == 400
    assert res.data["detail"] == "import_path_traversal"
    # Aucun document créé pour un chemin dangereux.
    assert not Document.objects.filter(rag_space=rag_space, filename="secret.txt").exists()


def test_upload_rejects_absolute_relative_path(api_client, rag_space):
    res = api_client.post(
        "/api/v1/documents/upload/",
        data={
            "rag_space_id": str(rag_space.id),
            "file": _named_txt("a.txt"),
            "relative_path": "/etc/passwd",
        },
        format="multipart",
    )
    assert res.status_code == 400
    assert res.data["detail"] == "import_path_absolute"


def test_upload_without_import_metadata_still_works(api_client, rag_space):
    # Non-régression : l'upload unitaire (sans relative_path) reste inchangé.
    res = api_client.post(
        "/api/v1/documents/upload/",
        data={"rag_space_id": str(rag_space.id), "file": _named_txt("plain.txt")},
        format="multipart",
    )
    assert res.status_code == 200, res.data
    doc = Document.objects.get(id=res.data["document"]["id"])
    assert "import" not in (doc.meta or {})
