"""A2 — normalisation des statuts Document lowercase -> uppercase.

On exerce directement la fonction de transformation de données de la migration
`0022_normalize_document_status_uppercase` (django_test_migrations n'est pas
disponible dans l'environnement). Les lignes lowercase sont créées via l'ORM
(`.create()` ne valide pas les `choices`), ce qui reproduit l'état d'une base
déployée avant le passage uppercase de 0017.
"""

import importlib

import pytest
from django.apps import apps as django_apps

from apps.rag_core.models import Document, RagSpace
from apps.workspaces.models import Workspace

# Le module de migration commence par un chiffre -> import dynamique.
_mig = importlib.import_module(
    "apps.rag_core.migrations.0022_normalize_document_status_uppercase"
)
uppercase_status = _mig.uppercase_status

pytestmark = pytest.mark.django_db


def _space():
    ws = Workspace.objects.create(name="WS-mig")
    return RagSpace.objects.create(name="S-mig", workspace=ws)


def test_lowercase_status_is_normalized_to_uppercase():
    space = _space()
    d_ready = Document.objects.create(rag_space=space, filename="r.pdf", status="ready")
    d_failed = Document.objects.create(rag_space=space, filename="f.pdf", status="failed")
    d_pending = Document.objects.create(rag_space=space, filename="p.pdf", status="pending")
    d_already = Document.objects.create(rag_space=space, filename="ok.pdf", status="READY")

    uppercase_status(django_apps, None)

    for d, expected in (
        (d_ready, "READY"),
        (d_failed, "FAILED"),
        (d_pending, "PENDING"),
        (d_already, "READY"),
    ):
        d.refresh_from_db()
        assert d.status == expected


def test_migration_is_idempotent():
    space = _space()
    Document.objects.create(rag_space=space, filename="r.pdf", status="ready")

    uppercase_status(django_apps, None)
    uppercase_status(django_apps, None)  # 2e passage : aucun effet de bord

    assert list(Document.objects.values_list("status", flat=True)) == ["READY"]
