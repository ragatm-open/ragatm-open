"""Le préflight doit être incapable d'écrire, pas seulement s'en abstenir.

Un `--dry-run` sur une commande qui sait écrire laisse toujours la possibilité d'un
drapeau oublié. La garantie recherchée est structurelle : la commande ne connaît que
les fonctions de lecture.
"""
import ast
import inspect
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.rag_core.models import Chunk, Document, RagSpace
from apps.rag_core.models.document import DocumentStatus
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


@pytest.fixture
def espace_peuple(db):
    ws = Workspace.objects.create(name="WS-PF")
    space = RagSpace.objects.create(
        workspace=ws, name="PF", embedding_provider="ollama",
        embedding_model="nomic-embed-text",
    )
    doc = Document.objects.create(
        rag_space=space, filename="ok.pdf", status=DocumentStatus.READY,
        embedding_provider="ollama", embedding_model="nomic-embed-text",
    )
    for i in range(3):
        Chunk.objects.create(rag_space=space, document=doc, chunk_index=i,
                             text=f"fragment {i}", page_number=1, meta={})
    Document.objects.create(
        rag_space=space, filename="casse.pdf", status=DocumentStatus.FAILED,
        embedding_provider="", embedding_model="", reindex_required=True,
    )
    return space


def test_la_commande_ne_peut_pas_ecrire():
    """Structurel : elle n'importe ni n'appelle la primitive d'écriture."""
    from apps.rag_core.management.commands import reembed_preflight as cmd

    arbre = ast.parse(inspect.getsource(cmd))
    interdits = {"reembed_document", "bulk_update", "save", "update", "create", "delete"}
    vus = set()
    for n in ast.walk(arbre):
        if isinstance(n, ast.Call):
            nom = getattr(n.func, "id", None) or getattr(n.func, "attr", None)
            if nom in interdits:
                vus.add(nom)
        elif isinstance(n, ast.ImportFrom):
            vus |= {a.name for a in n.names} & interdits
    assert not vus, f"le préflight peut écrire : {sorted(vus)}"


def test_le_preflight_chiffre_le_travail(espace_peuple):
    out = StringIO()
    call_command("reembed_preflight", "--rag-space", str(espace_peuple.id),
                 "--provider", "ai_gateway", "--model", "legacy-768", stdout=out)
    texte = out.getvalue()
    assert "documents            : 2" in texte
    assert "éligibles          : 1" in texte
    assert "fragments à recalculer : 3" in texte
    assert "not_ready" in texte


def test_le_preflight_avertit_quand_l_espace_ne_declare_pas_la_cible(espace_peuple):
    """L'avertissement n'est pas décoratif : sans lui, une migration rendrait les
    documents invisibles au retrieval sans que personne ne l'ait prévu."""
    out = StringIO()
    call_command("reembed_preflight", "--rag-space", str(espace_peuple.id),
                 "--provider", "ai_gateway", "--model", "legacy-768", stdout=out)
    assert "ATTENTION" in out.getvalue()
    assert "INVISIBLES" in out.getvalue()


def test_aucun_avertissement_quand_l_espace_declare_deja_la_cible(espace_peuple):
    out = StringIO()
    call_command("reembed_preflight", "--rag-space", str(espace_peuple.id),
                 "--provider", "ollama", "--model", "nomic-embed-text", stdout=out)
    assert "ATTENTION" not in out.getvalue()


def test_espace_introuvable(espace_peuple):
    with pytest.raises(CommandError, match="introuvable"):
        call_command("reembed_preflight", "--rag-space",
                     "00000000-0000-0000-0000-000000000000", stdout=StringIO())


def test_le_preflight_ne_modifie_rien(espace_peuple):
    avant = sorted(
        (str(c.id), c.text, c.pipeline_family, bool(c.embedding))
        for c in Chunk.objects.filter(rag_space=espace_peuple)
    )
    docs_avant = sorted(
        (str(d.id), d.embedding_provider, d.embedding_model, d.reindex_required, d.status)
        for d in Document.objects.filter(rag_space=espace_peuple)
    )
    call_command("reembed_preflight", "--rag-space", str(espace_peuple.id),
                 "--provider", "ai_gateway", "--model", "legacy-768",
                 "--details", stdout=StringIO())
    apres = sorted(
        (str(c.id), c.text, c.pipeline_family, bool(c.embedding))
        for c in Chunk.objects.filter(rag_space=espace_peuple)
    )
    docs_apres = sorted(
        (str(d.id), d.embedding_provider, d.embedding_model, d.reindex_required, d.status)
        for d in Document.objects.filter(rag_space=espace_peuple)
    )
    assert apres == avant and docs_apres == docs_avant
