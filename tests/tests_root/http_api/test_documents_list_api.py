"""Liste des documents : pagination serveur, compteurs, recherche, tri.

Le défaut corrigé : l'écran chargeait 200 documents et présentait `items.length`
comme le total du RagSpace. Ces tests fixent le contrat inverse — les compteurs et
le total portent sur **tout** l'espace, jamais sur la page rendue — et vérifient
qu'aucun document ne devient inatteignable en paginant.
"""
from datetime import timedelta

import pytest
from django.utils import timezone

from api.v1.documents import listing
from apps.rag_core.models import Chunk, Document
from apps.rag_core.models.document import DocumentStatus

pytestmark = pytest.mark.django_db

URL = "/api/v1/documents/"


def _doc(space, *, name, status=DocumentStatus.READY, ctype="application/pdf", created=None):
    """Document nu (sans fichier ni ingestion) : ces tests portent sur la LISTE."""
    d = Document.objects.create(
        rag_space=space,
        filename=name,
        content_type=ctype,
        size_bytes=10,
        sha256=f"{abs(hash(name)):064x}"[:64],
        status=status,
    )
    if created is not None:
        # `created_at` est en auto_now_add : seul un UPDATE peut le fixer.
        Document.objects.filter(id=d.id).update(created_at=created)
        d.refresh_from_db()
    return d


def _bulk(space, n, *, prefix="doc", status=DocumentStatus.READY):
    base = timezone.now() - timedelta(days=n)
    return [
        _doc(space, name=f"{prefix}-{i:04d}.pdf", status=status, created=base + timedelta(minutes=i))
        for i in range(n)
    ]


def _ids(payload):
    return [it["id"] for it in payload["items"]]


# --- Pagination -------------------------------------------------------------

class TestPagination:
    def test_total_est_le_vrai_total_pas_la_taille_de_page(self, api_client, rag_space):
        _bulk(rag_space, 205)
        res = api_client.get(f"{URL}?rag_space_id={rag_space.id}&limit=50")
        assert res.status_code == 200
        assert res.data["total"] == 205, "le total doit ignorer la pagination"
        assert len(res.data["items"]) == 50
        assert res.data["has_next"] is True
        assert res.data["has_previous"] is False

    def test_limite_bornee_au_maximum(self, api_client, rag_space):
        _bulk(rag_space, 205)
        res = api_client.get(f"{URL}?rag_space_id={rag_space.id}&limit=9999")
        assert res.data["limit"] == listing.MAX_LIMIT
        assert len(res.data["items"]) == listing.MAX_LIMIT

    def test_limite_par_defaut_et_valeurs_invalides(self, api_client, rag_space):
        _bulk(rag_space, 60)
        assert api_client.get(f"{URL}?rag_space_id={rag_space.id}").data["limit"] == 50
        assert api_client.get(f"{URL}?rag_space_id={rag_space.id}&limit=abc").data["limit"] == 50
        assert api_client.get(f"{URL}?rag_space_id={rag_space.id}&limit=0").data["limit"] == 1
        r = api_client.get(f"{URL}?rag_space_id={rag_space.id}&offset=-5")
        assert r.status_code == 200 and r.data["offset"] == 0

    def test_toutes_les_pages_couvrent_le_corpus_sans_doublon(self, api_client, rag_space):
        created = {str(d.id) for d in _bulk(rag_space, 205)}
        seen, offset = [], 0
        while True:
            res = api_client.get(f"{URL}?rag_space_id={rag_space.id}&limit=50&offset={offset}")
            seen.extend(_ids(res.data))
            if not res.data["has_next"]:
                break
            offset += 50
        assert len(seen) == len(set(seen)), "aucun document ne doit apparaître deux fois"
        assert set(seen) == created, "aucun document ne doit rester inatteignable"

    def test_offset_au_dela_du_total_reste_deterministe(self, api_client, rag_space):
        _bulk(rag_space, 10)
        res = api_client.get(f"{URL}?rag_space_id={rag_space.id}&offset=500")
        assert res.status_code == 200
        assert res.data["items"] == []
        assert res.data["total"] == 10
        assert res.data["has_next"] is False
        assert res.data["has_previous"] is True

    def test_ordre_stable_a_date_egale(self, api_client, rag_space):
        same = timezone.now()
        for i in range(12):
            _doc(rag_space, name=f"ex-aequo-{i}.pdf", created=same)
        first = _ids(api_client.get(f"{URL}?rag_space_id={rag_space.id}&limit=6").data)
        second = _ids(api_client.get(f"{URL}?rag_space_id={rag_space.id}&limit=6&offset=6").data)
        assert not set(first) & set(second), "un départage stable interdit tout chevauchement"
        # Rejouer la même page doit rendre exactement la même tranche.
        assert _ids(api_client.get(f"{URL}?rag_space_id={rag_space.id}&limit=6").data) == first


# --- Compteurs --------------------------------------------------------------

class TestCounts:
    @pytest.fixture
    def space_with_statuses(self, rag_space):
        _bulk(rag_space, 7, prefix="ready", status=DocumentStatus.READY)
        _bulk(rag_space, 3, prefix="ing", status=DocumentStatus.INGESTING)
        _bulk(rag_space, 2, prefix="pend", status=DocumentStatus.PENDING)
        _bulk(rag_space, 5, prefix="fail", status=DocumentStatus.FAILED)
        return rag_space

    def test_compte_exact_par_statut(self, api_client, space_with_statuses):
        c = api_client.get(f"{URL}?rag_space_id={space_with_statuses.id}").data["counts"]
        assert c == {"all": 17, "ready": 7, "analyzing": 3, "queued": 2, "error": 5}

    def test_somme_des_statuts_egale_le_total(self, api_client, space_with_statuses):
        c = api_client.get(f"{URL}?rag_space_id={space_with_statuses.id}").data["counts"]
        assert c["ready"] + c["analyzing"] + c["queued"] + c["error"] == c["all"]

    def test_compteurs_independants_de_la_page(self, api_client, space_with_statuses):
        page = api_client.get(f"{URL}?rag_space_id={space_with_statuses.id}&limit=2&offset=8").data
        assert len(page["items"]) == 2
        assert page["counts"]["all"] == 17, "les onglets ne comptent pas la page rendue"

    def test_filtre_actif_n_annule_pas_les_autres_compteurs(self, api_client, space_with_statuses):
        res = api_client.get(f"{URL}?rag_space_id={space_with_statuses.id}&status=error").data
        assert res["total"] == 5, "le total suit le filtre (plage « 1–5 sur 5 »)"
        assert res["counts"] == {"all": 17, "ready": 7, "analyzing": 3, "queued": 2, "error": 5}
        assert all(i["status"] == DocumentStatus.FAILED for i in res["items"])

    def test_statut_inattendu_compte_comme_en_attente(self, api_client, rag_space):
        # Miroir du repli de `toDocStatus` côté frontend : rien ne doit disparaître
        # des compteurs, sinon la somme cesse de valoir le total.
        d = _doc(rag_space, name="bizarre.pdf")
        Document.objects.filter(id=d.id).update(status="UNKNOWN_STATE")
        c = api_client.get(f"{URL}?rag_space_id={rag_space.id}").data["counts"]
        assert c == {"all": 1, "ready": 0, "analyzing": 0, "queued": 1, "error": 0}
        filtered = api_client.get(f"{URL}?rag_space_id={rag_space.id}&status=queued").data
        assert filtered["total"] == 1 and len(filtered["items"]) == 1

    def test_autre_ragspace_jamais_compte(self, api_client, rag_space, foreign_setup):
        _bulk(rag_space, 4)
        _bulk(foreign_setup["rag_space"], 9)
        res = api_client.get(f"{URL}?rag_space_id={rag_space.id}").data
        assert res["total"] == 4 and res["counts"]["all"] == 4

    def test_statut_inconnu_rejete(self, api_client, rag_space):
        res = api_client.get(f"{URL}?rag_space_id={rag_space.id}&status=zombie")
        assert res.status_code == 400
        assert res.data["detail"] == "invalid_status"


# --- Recherche --------------------------------------------------------------

class TestSearch:
    @pytest.fixture
    def corpus(self, rag_space):
        _doc(rag_space, name="Rapport annuel.pdf")
        _doc(rag_space, name="rapport-mensuel.pdf", status=DocumentStatus.FAILED)
        _doc(rag_space, name="Synthèse.docx")
        _doc(rag_space, name="budget.xlsx")
        return rag_space

    def test_recherche_par_nom(self, api_client, corpus):
        res = api_client.get(f"{URL}?rag_space_id={corpus.id}&search=rapport").data
        assert res["total"] == 2
        assert {i["filename"] for i in res["items"]} == {"Rapport annuel.pdf", "rapport-mensuel.pdf"}

    def test_recherche_insensible_a_la_casse(self, api_client, corpus):
        assert api_client.get(f"{URL}?rag_space_id={corpus.id}&search=RAPPORT").data["total"] == 2

    def test_recherche_accents(self, api_client, corpus):
        assert api_client.get(f"{URL}?rag_space_id={corpus.id}&search=synthèse").data["total"] == 1

    def test_recherche_sans_resultat(self, api_client, corpus):
        res = api_client.get(f"{URL}?rag_space_id={corpus.id}&search=zzzz").data
        assert res["total"] == 0 and res["items"] == [] and res["counts"]["all"] == 0

    def test_compteurs_recalcules_sur_la_recherche(self, api_client, corpus):
        c = api_client.get(f"{URL}?rag_space_id={corpus.id}&search=rapport").data["counts"]
        assert c == {"all": 2, "ready": 1, "analyzing": 0, "queued": 0, "error": 1}

    def test_recherche_combinee_avec_statut(self, api_client, corpus):
        res = api_client.get(f"{URL}?rag_space_id={corpus.id}&search=rapport&status=error").data
        assert res["total"] == 1
        assert res["items"][0]["filename"] == "rapport-mensuel.pdf"

    def test_recherche_combinee_avec_pagination(self, api_client, rag_space):
        _bulk(rag_space, 30, prefix="contrat")
        _bulk(rag_space, 5, prefix="autre")
        page = api_client.get(f"{URL}?rag_space_id={rag_space.id}&search=contrat&limit=10&offset=20").data
        assert page["total"] == 30
        assert len(page["items"]) == 10
        assert page["has_next"] is False

    def test_recherche_ne_voit_pas_les_autres_espaces(self, api_client, rag_space, foreign_setup):
        _doc(foreign_setup["rag_space"], name="rapport-secret.pdf")
        assert api_client.get(f"{URL}?rag_space_id={rag_space.id}&search=rapport").data["total"] == 0

    def test_recherche_tres_longue_bornee_sans_erreur(self, api_client, corpus):
        res = api_client.get(f"{URL}?rag_space_id={corpus.id}&search={'x' * 5000}")
        assert res.status_code == 200 and res.data["total"] == 0


# --- Tri --------------------------------------------------------------------

class TestOrdering:
    def test_tri_par_nom_asc_et_desc(self, api_client, rag_space):
        for n in ("banane.pdf", "Abricot.pdf", "cerise.pdf"):
            _doc(rag_space, name=n)
        asc = api_client.get(f"{URL}?rag_space_id={rag_space.id}&ordering=filename").data
        assert [i["filename"] for i in asc["items"]] == ["Abricot.pdf", "banane.pdf", "cerise.pdf"]
        desc = api_client.get(f"{URL}?rag_space_id={rag_space.id}&ordering=-filename").data
        assert [i["filename"] for i in desc["items"]] == ["cerise.pdf", "banane.pdf", "Abricot.pdf"]

    def test_tri_par_type_asc_et_desc(self, api_client, rag_space):
        _doc(rag_space, name="a.pdf", ctype="application/pdf")
        _doc(rag_space, name="b.csv", ctype="text/csv")
        _doc(rag_space, name="c.png", ctype="image/png")
        asc = api_client.get(f"{URL}?rag_space_id={rag_space.id}&ordering=file_type").data
        assert [i["file_type"] for i in asc["items"]] == ["CSV", "PDF", "PNG"]
        desc = api_client.get(f"{URL}?rag_space_id={rag_space.id}&ordering=-file_type").data
        assert [i["file_type"] for i in desc["items"]] == ["PNG", "PDF", "CSV"]

    def test_type_trie_en_base_egale_le_type_serialise(self, api_client, rag_space):
        # Le tri SQL et le libellé Python sortent de la même table : ils ne peuvent
        # pas diverger. Ce test le vérifie sur des cas volontairement pénibles.
        cases = [
            ("scan.JPG", "", "JPG"),                                   # repli extension
            ("photo.jpeg", "image/jpeg", "JPEG"),                      # MIME prioritaire
            ("photo.bin", "image/jpeg", "JPEG"),                       # extension contredite
            ("notes", "", ""),                                         # ni MIME ni extension
            ("archive.tar.gz", "", "GZ"),                              # dernière extension
            ("rapport.pdf", "application/pdf; charset=binary", "PDF"),  # paramètre MIME
        ]
        for name, ctype, _ in cases:
            _doc(rag_space, name=name, ctype=ctype)
        expected = {name: label for name, _, label in cases}

        res = api_client.get(f"{URL}?rag_space_id={rag_space.id}&ordering=file_type").data
        assert {i["filename"]: i["file_type"] for i in res["items"]} == expected

        annotated = (
            Document.objects.filter(rag_space=rag_space)
            .annotate(sql_type=listing.file_type_expression())
            .values_list("filename", "sql_type")
        )
        assert dict(annotated) == expected, "le tri SQL doit rendre le libellé affiché"

    def test_tri_par_statut_suit_l_ordre_metier(self, api_client, rag_space):
        for st in (DocumentStatus.FAILED, DocumentStatus.READY,
                   DocumentStatus.PENDING, DocumentStatus.INGESTING):
            _doc(rag_space, name=f"{st}.pdf", status=st)
        asc = api_client.get(f"{URL}?rag_space_id={rag_space.id}&ordering=status").data
        assert [i["status"] for i in asc["items"]] == [
            DocumentStatus.PENDING, DocumentStatus.INGESTING,
            DocumentStatus.READY, DocumentStatus.FAILED,
        ], "en attente -> en analyse -> prêt -> erreur (pas l'ordre alphabétique)"
        desc = api_client.get(f"{URL}?rag_space_id={rag_space.id}&ordering=-status").data
        assert [i["status"] for i in desc["items"]] == [
            DocumentStatus.FAILED, DocumentStatus.READY,
            DocumentStatus.INGESTING, DocumentStatus.PENDING,
        ]

    def test_tri_par_date_asc_et_desc(self, api_client, rag_space):
        now = timezone.now()
        _doc(rag_space, name="vieux.pdf", created=now - timedelta(days=3))
        _doc(rag_space, name="recent.pdf", created=now - timedelta(minutes=1))
        _doc(rag_space, name="milieu.pdf", created=now - timedelta(days=1))
        asc = api_client.get(f"{URL}?rag_space_id={rag_space.id}&ordering=created_at").data
        assert [i["filename"] for i in asc["items"]] == ["vieux.pdf", "milieu.pdf", "recent.pdf"]
        desc = api_client.get(f"{URL}?rag_space_id={rag_space.id}&ordering=-created_at").data
        assert [i["filename"] for i in desc["items"]] == ["recent.pdf", "milieu.pdf", "vieux.pdf"]

    def test_ordre_par_defaut_le_plus_recent_d_abord(self, api_client, rag_space):
        now = timezone.now()
        _doc(rag_space, name="ancien.pdf", created=now - timedelta(days=2))
        _doc(rag_space, name="nouveau.pdf", created=now)
        res = api_client.get(f"{URL}?rag_space_id={rag_space.id}").data
        assert res["items"][0]["filename"] == "nouveau.pdf"
        assert res["ordering"] == listing.DEFAULT_ORDERING

    def test_tri_stable_a_valeur_egale(self, api_client, rag_space):
        same = timezone.now()
        for i in range(10):
            _doc(rag_space, name="identique.pdf".replace(".pdf", f"-{i}.pdf"), created=same)
        a = _ids(api_client.get(f"{URL}?rag_space_id={rag_space.id}&ordering=file_type&limit=5").data)
        b = _ids(api_client.get(f"{URL}?rag_space_id={rag_space.id}&ordering=file_type&limit=5&offset=5").data)
        assert not set(a) & set(b)

    def test_ordering_inconnu_rejete(self, api_client, rag_space):
        res = api_client.get(f"{URL}?rag_space_id={rag_space.id}&ordering=size_bytes")
        assert res.status_code == 400
        assert res.data["detail"] == "invalid_ordering"
        assert api_client.get(f"{URL}?rag_space_id={rag_space.id}&ordering=--filename").status_code == 400

    def test_tri_compatible_avec_recherche_et_filtre(self, api_client, rag_space):
        _doc(rag_space, name="beta.pdf", status=DocumentStatus.FAILED)
        _doc(rag_space, name="alpha.pdf", status=DocumentStatus.FAILED)
        _doc(rag_space, name="gamma.pdf", status=DocumentStatus.READY)
        res = api_client.get(
            f"{URL}?rag_space_id={rag_space.id}&status=error&search=a&ordering=filename"
        ).data
        assert [i["filename"] for i in res["items"]] == ["alpha.pdf", "beta.pdf"]


# --- Contenu de la réponse & permissions ------------------------------------

class TestPayloadAndAccess:
    def test_chunks_count_sans_requete_par_document(self, api_client, rag_space,
                                                    django_assert_max_num_queries):
        docs = _bulk(rag_space, 25)
        Chunk.objects.bulk_create([
            Chunk(rag_space=rag_space, document=docs[0], chunk_index=i, text=f"t{i}",
                  embedding=[0.0] * 768, page_number=1, meta={})
            for i in range(3)
        ])
        # Budget mesuré : 5 requêtes (accès/audit + agrégats + page), indépendant du
        # nombre de lignes. La borne est large à dessein — elle n'est PAS là pour
        # figer le budget exact mais pour interdire le retour du N+1 : compter les
        # extraits document par document donnerait ici 25 requêtes de plus.
        with django_assert_max_num_queries(10):
            res = api_client.get(f"{URL}?rag_space_id={rag_space.id}&limit=25")
        by_id = {i["id"]: i for i in res.data["items"]}
        assert by_id[str(docs[0].id)]["chunks_count"] == 3
        assert all(by_id[str(d.id)]["chunks_count"] == 0 for d in docs[1:])

    def test_espace_non_autorise_ne_fuit_ni_items_ni_compteurs(self, api_client, foreign_setup):
        _bulk(foreign_setup["rag_space"], 3)
        res = api_client.get(f"{URL}?rag_space_id={foreign_setup['rag_space'].id}")
        assert res.status_code == 403
        assert "counts" not in (res.data or {})

    def test_enveloppe_historique_preservee(self, api_client, rag_space):
        _doc(rag_space, name="a.pdf")
        data = api_client.get(f"{URL}?rag_space_id={rag_space.id}").data
        for key in ("items", "total", "limit", "offset"):
            assert key in data, "l'enveloppe existante doit rester intacte"
        for key in ("has_next", "has_previous", "counts", "ordering"):
            assert key in data
