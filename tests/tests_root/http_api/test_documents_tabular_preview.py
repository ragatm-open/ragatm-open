"""`GET /api/v1/documents/<id>/tabular-preview/` — contrat, permissions, sécurité.

Le fil directeur de ces tests : **aucun refus ne doit ressembler à un succès**.
Ni tableau vide, ni repli sur la première feuille, ni plage voisine. Un aperçu
faux est pire qu'un aperçu absent, parce qu'il est vérifiable en apparence.
"""
import io
import sys
from pathlib import Path

import pytest
from django.core.files.base import ContentFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit" / "extraction"))

from _builders import build_xlsx  # noqa: E402

from apps.rag_core.models.chunk import Chunk  # noqa: E402
from apps.rag_core.models.document import Document  # noqa: E402
from apps.rag_core.services.ingest.extraction import mime as M  # noqa: E402

pytestmark = pytest.mark.django_db


SHEETS = {
    "Accueil": [["Bienvenue"], ["Sommaire"]],
    "Proposition liste données princ": [
        ["Code", "Libelle", "Montant", "Taux"],
        *[[f"CRE {i}", f"Ligne {i}", i * 1000, (0.1, "0%")] for i in range(1, 60)],
    ],
    "Feuille masquée": [["Secret"], ["caché mais indexé"]],
    "Très masquée": [["Invisible"], ["non indexé"]],
}


def _make_document(rag_space, *, filename="REFERENTIEL.xlsx", data=None, sha256=None):
    data = data if data is not None else build_xlsx(
        SHEETS, hidden=("Feuille masquée",), very_hidden=("Très masquée",)
    )
    # Une contrainte d'unicité partielle interdit deux documents actifs de même
    # sha256 dans un RagSpace : chaque document de test doit avoir le sien.
    import hashlib

    digest = sha256 or hashlib.sha256(filename.encode() + data[:64]).hexdigest()
    doc = Document.objects.create(
        rag_space=rag_space,
        filename=filename,
        content_type=M.XLSX,
        sha256=digest,
        status="READY",
    )
    doc.storage_file.save(filename, ContentFile(data), save=True)
    return doc


def _make_chunk(doc, *, sheet_index=2, sheet_name="Proposition liste données princ",
                cell_range="A10:D14", extra=None):
    meta = {
        "kind": "excel_table",
        "sheet_name": sheet_name,
        "sheet_index": sheet_index,
        "sheet_state": "visible",
        "cell_range": cell_range,
        "data_range": cell_range,
        "header_range": "A1:D1",
        "row_start": 10,
        "row_end": 14,
        "column_start": "A",
        "column_end": "D",
        "headers": ["Code", "Libelle", "Montant", "Taux"],
    }
    meta.update(extra or {})
    return Chunk.objects.create(
        rag_space=doc.rag_space, document=doc, chunk_index=0, text="…", meta=meta
    )


def _url(doc, **params):
    from urllib.parse import urlencode

    qs = urlencode({k: v for k, v in params.items() if v is not None})
    return f"/api/v1/documents/{doc.id}/tabular-preview/" + (f"?{qs}" if qs else "")


# --------------------------------------------------------------------------- #
# Permissions et IDOR
# --------------------------------------------------------------------------- #
class TestPermissions:
    def test_utilisateur_autorise_obtient_l_apercu(self, api_client, rag_space):
        doc = _make_document(rag_space)
        chunk = _make_chunk(doc)
        res = api_client.get(_url(doc, chunk_id=str(chunk.id)))
        assert res.status_code == 200, res.data
        assert res.data["viewport"]["sheet_index"] == 2

    def test_utilisateur_non_authentifie_refuse(self, rag_space):
        from rest_framework.test import APIClient

        doc = _make_document(rag_space)
        res = APIClient().get(_url(doc))
        assert res.status_code in (401, 403)

    def test_utilisateur_d_un_autre_workspace_recoit_404(self, rag_space, foreign_setup):
        # 404 et non 403 : l'API ne doit pas servir d'oracle d'existence.
        doc = _make_document(rag_space)
        chunk = _make_chunk(doc)
        res = foreign_setup["client"].get(_url(doc, chunk_id=str(chunk.id)))
        assert res.status_code == 404

    def test_document_inexistant_indiscernable(self, api_client):
        import uuid

        res = api_client.get(f"/api/v1/documents/{uuid.uuid4()}/tabular-preview/")
        assert res.status_code == 404


class TestIDOR:
    def test_chunk_d_un_autre_document_refuse(self, api_client, rag_space):
        doc_a = _make_document(rag_space, filename="a.xlsx")
        doc_b = _make_document(rag_space, filename="b.xlsx")
        chunk_b = _make_chunk(doc_b)

        res = api_client.get(_url(doc_a, chunk_id=str(chunk_b.id)))
        assert res.status_code == 404
        assert res.data["reason_code"] == "chunk_document_mismatch"

    def test_chunk_d_un_autre_ragspace_refuse(self, api_client, rag_space, foreign_setup):
        doc = _make_document(rag_space)
        foreign_doc = _make_document(foreign_setup["rag_space"], filename="f.xlsx")
        foreign_chunk = _make_chunk(foreign_doc)

        res = api_client.get(_url(doc, chunk_id=str(foreign_chunk.id)))
        assert res.status_code == 404
        assert res.data["reason_code"] == "chunk_document_mismatch"

    def test_chunk_supprime_produit_chunk_stale(self, api_client, rag_space):
        # Les chunks sont supprimés puis recréés à chaque réindexation : un id
        # inconnu doit se DIRE, jamais retomber sur la première feuille.
        import uuid

        doc = _make_document(rag_space)
        res = api_client.get(_url(doc, chunk_id=str(uuid.uuid4())))
        assert res.status_code == 409
        assert res.data["reason_code"] == "chunk_stale"


# --------------------------------------------------------------------------- #
# Feuilles
# --------------------------------------------------------------------------- #
class TestFeuilles:
    def test_index_1_based_et_feuille_citee_ouverte(self, api_client, rag_space):
        doc = _make_document(rag_space)
        chunk = _make_chunk(doc, sheet_index=2)
        res = api_client.get(_url(doc, chunk_id=str(chunk.id)))
        assert res.status_code == 200
        vp = res.data["viewport"]
        assert vp["sheet_index"] == 2
        assert vp["sheet_name"] == "Proposition liste données princ"

    def test_tres_masquee_absente_de_l_inventaire(self, api_client, rag_space):
        # `veryHidden` n'est pas indexée par la politique par défaut : l'exposer
        # ferait apparaître un contenu que le RAG a délibérément écarté.
        doc = _make_document(rag_space)
        res = api_client.get(_url(doc, sheet_index=1))
        assert res.status_code == 200
        names = [s["name"] for s in res.data["workbook"]["sheets"]]
        assert "Très masquée" not in names
        assert "Feuille masquée" in names       # `hidden` reste consultable
        assert "Accueil" in names

    def test_feuille_tres_masquee_demandee_explicitement_refusee(self, api_client, rag_space):
        doc = _make_document(rag_space)
        res = api_client.get(_url(doc, sheet_index=4))
        assert res.status_code == 422
        assert res.data["reason_code"] == "sheet_not_previewable"

    def test_feuille_masquee_porte_son_etat(self, api_client, rag_space):
        doc = _make_document(rag_space)
        res = api_client.get(_url(doc, sheet_index=3))
        assert res.status_code == 200
        assert res.data["viewport"]["sheet_state"] == "hidden"

    def test_feuille_hors_classeur(self, api_client, rag_space):
        doc = _make_document(rag_space)
        res = api_client.get(_url(doc, sheet_index=99))
        assert res.status_code == 404
        assert res.data["reason_code"] == "sheet_not_found"

    def test_nom_accentue_et_espaces_traverse_l_url(self, api_client, rag_space):
        doc = _make_document(rag_space)
        chunk = _make_chunk(doc)
        res = api_client.get(
            _url(doc, chunk_id=str(chunk.id), sheet_name="Proposition liste données princ")
        )
        assert res.status_code == 200

    def test_contradiction_index_nom_refusee(self, api_client, rag_space):
        # Arbitrer silencieusement afficherait une feuille non demandée tout en
        # prétendant montrer la source citée.
        doc = _make_document(rag_space)
        chunk = _make_chunk(doc, sheet_index=2)
        res = api_client.get(_url(doc, chunk_id=str(chunk.id), sheet_index=1))
        assert res.status_code == 400
        assert res.data["reason_code"] == "sheet_reference_conflict"

    def test_contradiction_nom_refusee(self, api_client, rag_space):
        doc = _make_document(rag_space)
        chunk = _make_chunk(doc)
        res = api_client.get(_url(doc, chunk_id=str(chunk.id), sheet_name="Accueil"))
        assert res.status_code == 400
        assert res.data["reason_code"] == "sheet_reference_conflict"


# --------------------------------------------------------------------------- #
# Plages et contexte
# --------------------------------------------------------------------------- #
class TestPlages:
    def test_surlignage_de_la_plage_citee(self, api_client, rag_space):
        doc = _make_document(rag_space)
        chunk = _make_chunk(doc, cell_range="A10:D14")
        res = api_client.get(_url(doc, chunk_id=str(chunk.id)))
        assert res.status_code == 200
        assert res.data["viewport"]["requested_ranges"] == ["A10:D14"]

        marked = {
            (r["row_number"], c["column_index"])
            for r in res.data["rows"]
            for c in r["cells"]
            if c["highlighted"]
        }
        assert marked == {(row, col) for row in range(10, 15) for col in range(1, 5)}

    def test_plages_multiples_chunk_decoupe_par_colonnes(self, api_client, rag_space):
        # Un chunk découpé par colonnes couvre une zone NON CONTIGUË : colonne
        # d'identification + fenêtre. Surligner `cell_range` seul omettrait l'ID.
        doc = _make_document(rag_space)
        chunk = _make_chunk(
            doc,
            cell_range="C10:D10",
            extra={
                "chunk_axis": "columns",
                "identity_columns": ["A"],
                "identity_range": "A10:A10",
                "data_ranges": ["A10:A10", "C10:D10"],
                "row_start": 10, "row_end": 10,
                "column_start": "C", "column_end": "D",
            },
        )
        res = api_client.get(_url(doc, chunk_id=str(chunk.id)))
        assert res.status_code == 200
        assert res.data["viewport"]["requested_ranges"] == ["A10:A10", "C10:D10"]

        marked = {
            (r["row_number"], c["column_index"])
            for r in res.data["rows"]
            for c in r["cells"]
            if c["highlighted"]
        }
        # La colonne B (intermédiaire) n'est PAS citée : la surligner marquerait
        # une donnée qui ne fait pas partie de la source.
        assert marked == {(10, 1), (10, 3), (10, 4)}

    def test_contexte_borne_autour_de_la_plage(self, api_client, rag_space):
        doc = _make_document(rag_space)
        chunk = _make_chunk(doc, cell_range="A20:D24")
        res = api_client.get(_url(doc, chunk_id=str(chunk.id), context_rows=3, context_columns=1))
        vp = res.data["viewport"]
        assert vp["row_start"] == 17 and vp["row_end"] == 27
        assert vp["column_start"] == "A"        # borné à 1, pas de colonne 0

    def test_plage_invalide(self, api_client, rag_space):
        doc = _make_document(rag_space)
        res = api_client.get(_url(doc, sheet_index=2, range="PAS-UNE-PLAGE"))
        assert res.status_code == 400
        assert res.data["reason_code"] == "invalid_cell_range"

    def test_plage_hors_dimensions(self, api_client, rag_space):
        doc = _make_document(rag_space)
        res = api_client.get(_url(doc, sheet_index=2, range="A9000:D9010"))
        assert res.status_code == 400
        assert res.data["reason_code"] == "range_out_of_bounds"

    @pytest.mark.parametrize(
        "params",
        [
            {"context_rows": 10_000},
            {"context_columns": 500},
            {"context_rows": -1},
        ],
    )
    def test_parametres_de_contexte_excessifs(self, api_client, rag_space, params):
        doc = _make_document(rag_space)
        res = api_client.get(_url(doc, sheet_index=2, **params))
        assert res.status_code == 400
        assert res.data["reason_code"] == "context_limit_exceeded"


# --------------------------------------------------------------------------- #
# Format et version
# --------------------------------------------------------------------------- #
class TestFormatEtVersion:
    def test_csv_refuse_explicitement(self, api_client, rag_space):
        doc = Document.objects.create(
            rag_space=rag_space, filename="export.csv",
            content_type=M.CSV, status="READY",
        )
        doc.storage_file.save("export.csv", ContentFile(b"a,b\n1,2\n"), save=True)
        res = api_client.get(_url(doc))
        assert res.status_code == 422
        assert res.data["reason_code"] == "unsupported_format"

    def test_pdf_refuse_explicitement(self, api_client, rag_space):
        doc = Document.objects.create(
            rag_space=rag_space, filename="r.pdf", content_type=M.PDF, status="READY"
        )
        doc.storage_file.save("r.pdf", ContentFile(b"%PDF-1.4"), save=True)
        res = api_client.get(_url(doc))
        assert res.status_code == 422
        assert res.data["reason_code"] == "unsupported_format"

    def test_classeur_corrompu_ne_donne_jamais_un_500(self, api_client, rag_space):
        doc = _make_document(rag_space, data=b"PK\x03\x04 ceci n'est pas un classeur")
        res = api_client.get(_url(doc, sheet_index=1))
        assert res.status_code == 422
        assert res.data["reason_code"] == "workbook_unreadable"

    def test_fichier_absent_du_stockage(self, api_client, rag_space):
        doc = Document.objects.create(
            rag_space=rag_space, filename="vide.xlsx",
            content_type=M.XLSX, status="READY",
        )
        res = api_client.get(_url(doc, sheet_index=1))
        assert res.status_code == 404
        assert res.data["reason_code"] == "no_file_available"

    def test_version_attendue_differente(self, api_client, rag_space):
        doc = _make_document(rag_space)
        res = api_client.get(_url(doc, sheet_index=1, sha256="f" * 64))
        assert res.status_code == 409
        assert res.data["reason_code"] == "document_version_mismatch"

    def test_chunk_non_tabulaire(self, api_client, rag_space):
        doc = _make_document(rag_space)
        chunk = Chunk.objects.create(
            rag_space=rag_space, document=doc, chunk_index=0, text="…",
            meta={"kind": "pptx_slide", "slide_number": 4},
        )
        res = api_client.get(_url(doc, chunk_id=str(chunk.id)))
        assert res.status_code == 422
        assert res.data["reason_code"] == "chunk_not_tabular"

    def test_feuille_renommee_depuis_l_indexation(self, api_client, rag_space):
        # Le chunk pointe une feuille dont le nom ne correspond plus : la position
        # ne veut plus rien dire. On refuse plutôt que d'afficher la feuille 2.
        doc = _make_document(rag_space)
        chunk = _make_chunk(doc, sheet_index=2, sheet_name="Ancien nom")
        res = api_client.get(_url(doc, chunk_id=str(chunk.id)))
        assert res.status_code == 409
        assert res.data["reason_code"] == "document_version_mismatch"


# --------------------------------------------------------------------------- #
# Contrat de réponse
# --------------------------------------------------------------------------- #
class TestContrat:
    def test_forme_de_la_reponse(self, api_client, rag_space):
        doc = _make_document(rag_space)
        chunk = _make_chunk(doc)
        res = api_client.get(_url(doc, chunk_id=str(chunk.id)))
        d = res.data

        assert d["schema_version"] == 1
        assert d["document"]["id"] == str(doc.id)
        assert d["document"]["sha256"] == doc.sha256
        assert d["workbook"]["sheet_count"] == 3          # veryHidden exclue
        assert {"index", "name", "state", "has_citation"} <= set(
            d["workbook"]["sheets"][0]
        )
        assert d["fidelity"]["simplified"] is True
        assert "styles" in d["fidelity"]["omitted"]
        assert d["truncation"]["truncated"] is False

    def test_onglet_cite_signale(self, api_client, rag_space):
        doc = _make_document(rag_space)
        chunk = _make_chunk(doc, sheet_index=2)
        res = api_client.get(_url(doc, chunk_id=str(chunk.id)))
        cited = [s for s in res.data["workbook"]["sheets"] if s["has_citation"]]
        assert [s["index"] for s in cited] == [2]

    def test_colonnes_portent_lettre_et_entete(self, api_client, rag_space):
        doc = _make_document(rag_space)
        chunk = _make_chunk(doc)
        res = api_client.get(_url(doc, chunk_id=str(chunk.id)))
        cols = res.data["columns"]
        assert [c["letter"] for c in cols][:4] == ["A", "B", "C", "D"]
        assert cols[0]["header"] == "Code"     # lu au vol pendant le passage unique

    def test_lignes_portent_leur_numero_excel(self, api_client, rag_space):
        doc = _make_document(rag_space)
        chunk = _make_chunk(doc, cell_range="A10:D14")
        res = api_client.get(_url(doc, chunk_id=str(chunk.id)))
        numbers = [r["row_number"] for r in res.data["rows"]]
        assert 10 in numbers and 14 in numbers
        assert numbers == sorted(numbers)

    def test_navigation_libre_sans_chunk(self, api_client, rag_space):
        doc = _make_document(rag_space)
        res = api_client.get(_url(doc, sheet_index=1))
        assert res.status_code == 200
        assert res.data["viewport"]["requested_ranges"] == []
        assert not any(
            c["highlighted"] for r in res.data["rows"] for c in r["cells"]
        )

    def test_sans_chunk_ni_feuille(self, api_client, rag_space):
        doc = _make_document(rag_space)
        res = api_client.get(_url(doc))
        assert res.status_code == 400


# --------------------------------------------------------------------------- #
# Volumétrie
# --------------------------------------------------------------------------- #
class TestVolumetrie:
    def test_troncature_declaree_jamais_silencieuse(self, api_client, rag_space, settings):
        settings.PREVIEW_MAX_ROWS = 5
        doc = _make_document(rag_space)
        res = api_client.get(_url(doc, sheet_index=2, range="A1:D1", context_rows=50))
        assert res.status_code == 200
        assert res.data["truncation"]["truncated"] is True
        assert res.data["truncation"]["reason"] == "max_rows"
        assert len(res.data["rows"]) <= 5

    def test_borne_cardinale_de_cellules(self, api_client, rag_space, settings):
        settings.PREVIEW_MAX_CELLS = 20
        doc = _make_document(rag_space)
        res = api_client.get(_url(doc, sheet_index=2, range="A1:D1", context_rows=50))
        assert res.status_code == 200
        n_cells = sum(len(r["cells"]) for r in res.data["rows"])
        assert n_cells <= 20
        assert res.data["truncation"]["truncated"] is True

    def test_reponse_bornee_par_defaut(self, api_client, rag_space):
        doc = _make_document(rag_space)
        chunk = _make_chunk(doc)
        res = api_client.get(_url(doc, chunk_id=str(chunk.id)))
        n_cells = sum(len(r["cells"]) for r in res.data["rows"])
        assert n_cells <= 10_000


# --------------------------------------------------------------------------- #
# Sécurité du contenu
# --------------------------------------------------------------------------- #
class TestSecuriteContenu:
    def test_html_et_formules_dangereuses_rendus_inertes(self, api_client, rag_space):
        data = build_xlsx({
            "S": [
                ["Titre", "Charge"],
                ["<script>alert(1)</script>", "<img src=x onerror=alert(1)>"],
                ["javascript:alert(1)", "=cmd|'/c calc'!A1"],
                ["+1+1", "-2-2"],
                ["@SUM(1)", "normal"],
            ]
        })
        doc = _make_document(rag_space, data=data)
        res = api_client.get(_url(doc, sheet_index=1, range="A1:B5", context_rows=0))
        assert res.status_code == 200

        values = {
            (r["row_number"], c["column_index"]): c["value"]
            for r in res.data["rows"] for c in r["cells"]
        }
        # Le HTML est transporté comme TEXTE, jamais interprété ni échappé en dur :
        # c'est le rendu React qui l'échappe (aucun dangerouslySetInnerHTML).
        assert values[(2, 1)] == "<script>alert(1)</script>"

        # Les amorces de formule dans du TEXTE sont neutralisées, comme à
        # l'ingestion : sans cela, un copier-coller vers Excel exécuterait la
        # cellule. (`+`, `-`, `@` restent du texte pour Excel ; `=` en fait une
        # vraie formule, traitée juste en dessous.)
        for ref in [(4, 1), (4, 2), (5, 1)]:
            assert values[ref].startswith("'"), f"{ref} non neutralisée : {values[ref]!r}"

        # `=cmd|'/c calc'!A1` est stocké comme une FORMULE par le classeur. Lu en
        # `data_only=True`, il ne rend aucune valeur — et surtout jamais son texte,
        # qui est le vecteur DDE historique. L'aperçu n'affiche pas les formules.
        assert values[(3, 2)] == ""
        assert "cmd|" not in "".join(values.values())

    def test_aucune_valeur_de_cellule_dans_les_logs(self, api_client, rag_space, caplog):
        import logging

        caplog.set_level(logging.INFO)
        data = build_xlsx({"S": [["Entete"], ["SECRET-CANARI-42"]]})
        doc = _make_document(rag_space, data=data)
        res = api_client.get(_url(doc, sheet_index=1, range="A1:A2", context_rows=0))
        assert res.status_code == 200

        assert any("SECRET-CANARI-42" in r["cells"][0]["value"] for r in res.data["rows"])
        assert "SECRET-CANARI-42" not in caplog.text
