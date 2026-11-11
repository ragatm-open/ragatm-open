"""Observabilité du moteur tabulaire — compteurs, et surtout ce qui NE fuit PAS.

Le test central de ce fichier n'est pas « les compteurs s'incrémentent » : c'est
« rien de ce qui est journalisé ne permet de reconstituer le contenu d'un document ».
Un compteur faux est un inconvénient ; une valeur de cellule dans un log est une fuite
de données, et elle survit dans la rétention des journaux bien après le correctif.
"""
from __future__ import annotations

import io
import logging
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.urls import reverse
from openpyxl import Workbook
from rest_framework.test import APIClient

from apps.rag_core.models import Document, RagSpace
from apps.rag_core.tabular import observability as OBS
from apps.workspaces.models import Membership, Workspace

User = get_user_model()

#: Valeurs SENSIBLES plantées dans le classeur. Aucune ne doit apparaître en journal.
SECRET_CLIENT = "ClientUltraConfidentielSA"
SECRET_MONTANT = "987654321.99"


@pytest.fixture
def rag_space(db):
    workspace = Workspace.objects.create(name="W-obs")
    return RagSpace.objects.create(name="S-obs", workspace=workspace)


@pytest.fixture
def user(db, rag_space):
    u = User.objects.create_user(username="obs-user", password="pw")
    Membership.objects.create(workspace=rag_space.workspace, user=u,
                              role=Membership.ROLE_MEMBER)
    return u


@pytest.fixture
def document(rag_space):
    buf = io.BytesIO()
    wb = Workbook()
    ws = wb.active
    ws.title = "Ventes"
    ws.append(["Client", "Montant"])
    ws.append([SECRET_CLIENT, float(SECRET_MONTANT)])
    ws.append(["AutreClient", 100.0])
    wb.save(buf)

    doc = Document.objects.create(
        rag_space=rag_space, filename="confidentiel.xlsx",
        sha256="c" * 64, status="READY")
    doc.storage_file.save("confidentiel.xlsx", ContentFile(buf.getvalue()), save=True)
    return doc


@pytest.fixture
def api(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture(autouse=True)
def _compteurs_propres():
    OBS.reset()
    yield
    OBS.reset()


@pytest.fixture
def journaux():
    """Capture TOUT ce que le backend journalise pendant la requête."""
    flux = io.StringIO()
    handler = logging.StreamHandler(flux)
    handler.setLevel(logging.DEBUG)
    racine = logging.getLogger()
    ancien = racine.level
    racine.addHandler(handler)
    racine.setLevel(logging.DEBUG)
    try:
        yield flux
    finally:
        racine.removeHandler(handler)
        racine.setLevel(ancien)


# --------------------------------------------------------------------------- #
# Compteurs
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_une_requete_reussie_incremente_ok(api, rag_space, document, settings):
    settings.TABULAR_QUERY_ENABLED = True
    r = api.post(reverse("tabular-query"), {
        "rag_space_id": str(rag_space.id),
        "question": "Quelle est la somme du Montant ?",
        "document_ids": [str(document.id)],
    }, format="json")
    assert r.status_code == 200, r.data

    compteurs = OBS.snapshot()
    assert compteurs[OBS.QUERIES_TOTAL] == 1
    assert compteurs[OBS.QUERIES_OK] == 1
    assert compteurs.get(OBS.QUERIES_ERROR, 0) == 0
    assert compteurs[OBS.DURATION_QUERY] > 0


@pytest.mark.django_db
def test_le_flag_desactive_compte_un_unsupported(api, rag_space, document, settings):
    settings.TABULAR_QUERY_ENABLED = False
    api.post(reverse("tabular-query"), {
        "rag_space_id": str(rag_space.id),
        "question": "Quelle est la somme du Montant ?",
    }, format="json")

    compteurs = OBS.snapshot()
    assert compteurs[OBS.QUERIES_TOTAL] == 1
    assert compteurs[OBS.QUERIES_UNSUPPORTED] == 1


@pytest.mark.django_db
def test_un_refus_de_permission_est_compte(api, rag_space, document, settings):
    settings.TABULAR_QUERY_ENABLED = True
    intrus = User.objects.create_user(username="intrus-obs", password="pw")
    client = APIClient()
    client.force_authenticate(user=intrus)
    client.post(reverse("tabular-query"), {
        "rag_space_id": str(rag_space.id),
        "question": "Quelle est la somme du Montant ?",
    }, format="json")

    compteurs = OBS.snapshot()
    assert compteurs[OBS.PERMISSION_DENIALS] == 1
    assert compteurs[OBS.QUERIES_ERROR] == 1


# --------------------------------------------------------------------------- #
# Absence de fuite — le test qui compte
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_les_journaux_ne_contiennent_aucune_valeur_de_cellule(
        api, rag_space, document, settings, journaux):
    settings.TABULAR_QUERY_ENABLED = True
    r = api.post(reverse("tabular-query"), {
        "rag_space_id": str(rag_space.id),
        "question": "Quelle est la somme du Montant ?",
        "document_ids": [str(document.id)],
    }, format="json")
    assert r.status_code == 200, r.data

    sortie = journaux.getvalue()
    assert SECRET_CLIENT not in sortie, "une valeur de cellule a fuité en journal"
    assert SECRET_MONTANT not in sortie, "un montant a fuité en journal"


@pytest.mark.django_db
def test_les_journaux_ne_contiennent_ni_sql_ni_chemin_disque(
        api, rag_space, document, settings, journaux):
    """Le SQL porte ses PARAMÈTRES, et les paramètres sont les valeurs de filtre.

    Journaliser la requête compilée reviendrait donc à journaliser le contenu filtré.
    Le chemin disque, lui, révèle l'arborescence de stockage et l'UUID de l'espace.
    """
    settings.TABULAR_QUERY_ENABLED = True
    api.post(reverse("tabular-query"), {
        "rag_space_id": str(rag_space.id),
        "question": "Quelle est la somme du Montant où le Client vaut AutreClient ?",
        "document_ids": [str(document.id)],
    }, format="json")

    sortie = journaux.getvalue()
    assert "SELECT " not in sortie
    assert "FROM t" not in sortie
    assert "rag_docs/" not in sortie
    assert "/media/" not in sortie


@pytest.mark.django_db
def test_la_question_utilisateur_nest_pas_journalisee_en_clair(
        api, rag_space, document, settings, journaux):
    """Une question porte souvent le nom d'un client ou d'un dossier."""
    settings.TABULAR_QUERY_ENABLED = True
    question = f"Quelle est la somme du Montant pour {SECRET_CLIENT} ?"
    api.post(reverse("tabular-query"), {
        "rag_space_id": str(rag_space.id), "question": question,
        "document_ids": [str(document.id)],
    }, format="json")

    assert question not in journaux.getvalue()


@pytest.mark.django_db
def test_le_journal_porte_bien_les_identifiants_techniques(
        api, rag_space, document, settings, journaux):
    """L'inverse du test précédent : sans identifiant, un incident est indiagnosticable.

    Hashes et UUID sont journalisables — ils relient un incident à une exécution
    tracée en base sans rien révéler du contenu.
    """
    settings.TABULAR_QUERY_ENABLED = True
    api.post(reverse("tabular-query"), {
        "rag_space_id": str(rag_space.id),
        "question": "Quelle est la somme du Montant ?",
        "document_ids": [str(document.id)],
    }, format="json")

    sortie = journaux.getvalue()
    assert "tabular.query" in sortie
    assert "status=ok" in sortie
    assert "operation=sum" in sortie
    assert str(rag_space.id) in sortie


def test_les_noms_de_compteurs_sont_stables():
    """Ils sont destinés à être scrapés : un renommage casse les tableaux de bord."""
    assert OBS.QUERIES_TOTAL == "tabular_queries_total"
    assert OBS.QUERIES_OK == "tabular_queries_ok_total"
    assert OBS.QUERIES_CLARIFICATION == "tabular_queries_clarification_total"
    assert OBS.QUERIES_UNSUPPORTED == "tabular_queries_unsupported_total"
    assert OBS.QUERIES_ERROR == "tabular_queries_error_total"
    assert OBS.MEMORY_LIMIT_ERRORS == "tabular_memory_limit_errors_total"
    assert OBS.TIMEOUT_ERRORS == "tabular_timeout_errors_total"
    assert OBS.PERMISSION_DENIALS == "tabular_permission_denials_total"


def test_les_compteurs_supportent_la_concurrence():
    """Un `+=` sur un dict n'est pas atomique ; le moteur est appelé depuis des
    threads de requête."""
    import threading

    OBS.reset()

    def bombarde():
        for _ in range(500):
            OBS.incr(OBS.QUERIES_TOTAL)

    fils = [threading.Thread(target=bombarde) for _ in range(8)]
    for f in fils:
        f.start()
    for f in fils:
        f.join()

    assert OBS.snapshot()[OBS.QUERIES_TOTAL] == 8 * 500
