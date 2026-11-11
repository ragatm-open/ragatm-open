"""Tests de non-régression — PR 1 RagATM_Core (métadonnées de scoping additives).

Vérifie que l'ajout des champs de scoping (RagSpace.project/environment ;
Document.collection/source_app/confidentiality/document_status/version) :
- applique bien les valeurs de compatibilité par défaut aux objets ;
- garde `document_status` (métier) DISTINCT de `status` (technique d'ingestion) ;
- ne touche pas la dimension d'embedding (gelée à 768) ;
- n'expose aucun endpoint /core/* ;
- n'expose pas les nouveaux champs via les serializers publics existants.

Les régressions plus larges (endpoints, upload, retrieval, permissions) sont
couvertes par les suites existantes tests/tests_root/http_api et tests/tests_root/integration.
"""
import pytest


@pytest.mark.django_db
def test_ragspace_defaults_match_compat_values(rag_space):
    # RagSpace créé sans préciser project/environment => défauts de compatibilité.
    assert rag_space.project == "ragatm"
    assert rag_space.environment == "prod"


@pytest.mark.django_db
def test_existing_ragspaces_are_backfilled_by_default(rag_space):
    # Relecture depuis la base : les colonnes existent et portent bien le défaut
    # (AddField avec `default` => PostgreSQL backfille les lignes existantes).
    from apps.rag_core.models import RagSpace

    reloaded = RagSpace.objects.get(pk=rag_space.pk)
    assert reloaded.project == "ragatm"
    assert reloaded.environment == "prod"


@pytest.mark.django_db
def test_document_defaults_via_ingest(rag_space, ingest_text):
    # L'ingestion documentaire fonctionne toujours et le Document produit porte
    # les défauts de scoping de compatibilité.
    from apps.rag_core.models import Document
    from apps.rag_core.models.document import DocumentStatus

    doc_id = ingest_text(rag_space, "Texte de référence pour scoping.", filename="ref.txt")
    doc = Document.objects.get(pk=doc_id)

    assert doc.collection == "legacy"
    assert doc.source_app == "ragatm"
    assert doc.confidentiality == "internal"
    assert doc.document_status == "active"
    assert doc.version == ""
    # Le statut TECHNIQUE d'ingestion n'est pas affecté.
    assert doc.status == DocumentStatus.READY


@pytest.mark.django_db
def test_document_status_is_distinct_from_technical_status(rag_space, ingest_text):
    # Garde-fou : `document_status` (métier) et `status` (technique) sont deux
    # attributs séparés et ne doivent jamais être confondus.
    from apps.rag_core.models import Document

    doc_id = ingest_text(rag_space, "Contenu.", filename="d.txt")
    doc = Document.objects.get(pk=doc_id)

    field_names = {f.name for f in Document._meta.get_fields()}
    assert "status" in field_names
    assert "document_status" in field_names
    # Valeurs indépendantes : technique=READY, métier=active.
    assert doc.status != doc.document_status


def test_embedding_dimension_is_unchanged():
    # La dimension d'embedding reste gelée à 768 (aucun changement dans cette PR).
    from django.conf import settings

    from apps.rag_core.models.chunk import EMBEDDING_VECTOR_DIM, Chunk

    assert EMBEDDING_VECTOR_DIM == 768
    assert int(settings.RAG_EMBEDDING_DIM) == 768
    assert Chunk._meta.get_field("embedding").dimensions == 768


@pytest.mark.django_db
def test_core_endpoints_are_registered_and_auth_protected(client):
    # Depuis PR 3, les endpoints /api/v1/core/* existent (exposition de la façade).
    # Un client NON authentifié ne doit pas y accéder (401/403, jamais 200).
    # NB: APPEND_SLASH=False => slash final requis.
    assert client.get("/api/v1/core/health/").status_code in (401, 403)
    assert client.post("/api/v1/core/retrieve/").status_code in (401, 403)


def test_public_ragspace_serializer_does_not_leak_scoping_fields():
    # Les serializers publics sont des Serializer explicites : les nouveaux champs
    # modèles ne doivent pas apparaître dans la réponse API (aucun changement API).
    from api.v1.ragspaces.serializers import RagSpaceListItemSerializer

    exposed = set(RagSpaceListItemSerializer().fields.keys())
    assert "project" not in exposed
    assert "environment" not in exposed
