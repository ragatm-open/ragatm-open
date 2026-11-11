# -*- coding: utf-8 -*-
"""Fragmentation bornée des tableaux structurés (TABLE-CHUNK-01).

Ce que ces tests protègent tient en une phrase : un tableau ne doit plus pouvoir
faire échouer le document qui le contient. Le défaut corrigé était double —
troncature silencieuse à 12 000 caractères (47 % d'un tableau réel perdus), et
chunk émis au-delà de ce que le modèle d'embeddings gouverné accepte, ce qui
faisait refuser le LOT entier par la passerelle et basculer tout le document en
`FAILED`.
"""
from __future__ import annotations

import pytest

from apps.rag_core.services.ingest.extraction.table_chunking import (
    fragmenter_tableau_markdown,
)




def _table(n_lignes: int, largeur_cellule: int = 20, colonnes: int = 4) -> str:
    entete = "| " + " | ".join(f"Col{c}" for c in range(colonnes)) + " |"
    sep = "|" + "|".join([" --- "] * colonnes) + "|"
    lignes = [entete, sep]
    for i in range(n_lignes):
        lignes.append("| " + " | ".join(f"v{i}c{c}".ljust(largeur_cellule)
                                        for c in range(colonnes)) + " |")
    return "\n".join(lignes)


def _entete_de(markdown: str) -> str:
    return markdown.splitlines()[0]


# ------------------------------------------------------------------ §21 petit
def test_un_tableau_dans_le_budget_sort_inchange():
    """Le cas courant, et le seul qui gouverne la non-régression.

    Tant que le tableau tient, la sortie doit être IDENTIQUE au caractère : c'est
    ce qui garantit qu'aucun document déjà indexé ne change de contenu.
    """
    md = _table(3)
    r = fragmenter_tableau_markdown(md, budget=10_000, max_fragments=40)
    assert r.fragments == [md]
    assert r.complet is True
    assert r.tronque is False


def test_un_tableau_vide_ne_produit_rien():
    assert fragmenter_tableau_markdown("", budget=100, max_fragments=10).fragments == []
    assert fragmenter_tableau_markdown("   \n ", budget=100, max_fragments=10).fragments == []


# ------------------------------------------------------------------ §22 grand
def test_un_grand_tableau_donne_plusieurs_fragments_bornes():
    md = _table(120)
    budget = 900
    r = fragmenter_tableau_markdown(md, budget=budget, max_fragments=200)
    assert len(r.fragments) > 1
    assert all(len(f) <= budget for f in r.fragments), \
        [len(f) for f in r.fragments if len(f) > budget]


def test_aucune_ligne_de_donnees_n_est_perdue_ni_reordonnee():
    """Le contenu doit être intégralement retrouvable, DANS L'ORDRE.

    C'est la propriété que la troncature violait : elle rendait un tableau
    d'apparence normale dont la seconde moitié n'existait plus.
    """
    md = _table(120)
    r = fragmenter_tableau_markdown(md, budget=900, max_fragments=200)
    entete = _entete_de(md)
    vues = []
    for f in r.fragments:
        for ligne in f.splitlines():
            if ligne == entete or set(ligne) <= set("| -"):
                continue
            vues.append(ligne)
    attendues = [l for l in md.splitlines()[2:]]
    assert vues == attendues
    assert r.complet is True


# ------------------------------------------------- §23 répétition de l'en-tête
def test_chaque_fragment_porte_l_entete():
    """Un fragment de valeurs sans ses colonnes ne veut plus rien dire.

    C'est le point qui distingue ce découpage d'une coupe tous les N caractères :
    les valeurs restent interprétables fragment par fragment.
    """
    md = _table(120)
    r = fragmenter_tableau_markdown(md, budget=900, max_fragments=200)
    entete = _entete_de(md)
    assert len(r.fragments) > 1
    for i, f in enumerate(r.fragments):
        assert f.splitlines()[0] == entete, f"fragment {i} sans en-tete"


def test_la_ligne_de_separation_suit_l_entete():
    """La séparation Markdown appartient à l'en-tête : la détacher casse la relecture."""
    md = _table(120)
    r = fragmenter_tableau_markdown(md, budget=900, max_fragments=200)
    for f in r.fragments:
        lignes = f.splitlines()
        assert set(lignes[1]) <= set("| -"), lignes[1]


def test_la_repetition_de_l_entete_n_est_pas_une_duplication():
    """§20 : l'en-tête se répète, les fragments COMPLETS ne se répètent jamais."""
    md = _table(120)
    r = fragmenter_tableau_markdown(md, budget=900, max_fragments=200)
    assert len(set(r.fragments)) == len(r.fragments)


# -------------------------------------------------------- §12 en-tête énorme
def test_un_entete_plus_grand_que_le_budget_ne_produit_pas_de_chunk_hors_borne():
    """Cas limite du §12 : aucune frontière idéale n'existe.

    Le document ne doit pas échouer pour autant — on renonce à répéter l'en-tête
    plutôt qu'à respecter le budget.
    """
    entete = "| " + " | ".join("Colonne très longue" * 6 for _ in range(6)) + " |"
    sep = "|" + "|".join([" --- "] * 6) + "|"
    corps = ["| " + " | ".join(f"v{i}c{c}" for c in range(6)) + " |" for i in range(40)]
    md = "\n".join([entete, sep] + corps)
    budget = 300
    r = fragmenter_tableau_markdown(md, budget=budget, max_fragments=200)
    assert r.fragments
    assert all(len(f) <= budget for f in r.fragments), \
        [len(f) for f in r.fragments if len(f) > budget]


# --------------------------------------------------------- §13/§24 ligne énorme
def test_une_ligne_plus_grande_que_le_budget_est_coupee_sans_depassement():
    entete = "| A | B |"
    sep = "| --- | --- |"
    enorme = "| " + "x" * 5000 + " | y |"
    md = "\n".join([entete, sep, "| a | b |", enorme, "| c | d |"])
    budget = 400
    r = fragmenter_tableau_markdown(md, budget=budget, max_fragments=200)
    assert r.fragments
    assert all(len(f) <= budget for f in r.fragments), \
        [len(f) for f in r.fragments if len(f) > budget]


def test_une_ligne_enorme_ne_fait_pas_disparaitre_les_lignes_voisines():
    """Aucune perte silencieuse (§24) : ce qui entoure la ligne difficile survit."""
    entete = "| A | B |"
    sep = "| --- | --- |"
    md = "\n".join([entete, sep, "| avant | 1 |", "| " + "x" * 3000 + " | y |",
                    "| apres | 2 |"])
    r = fragmenter_tableau_markdown(md, budget=400, max_fragments=200)
    joint = "\n".join(r.fragments)
    assert "avant" in joint and "apres" in joint


# ---------------------------------------------------- §25/§26 budget gouverné
@pytest.mark.parametrize("budget", [1800, 1200, 600, 250])
def test_aucun_fragment_ne_depasse_jamais_le_budget_recu(budget):
    """Le budget est REÇU, jamais recalculé ici.

    §25 (modèle plus restrictif que la taille configurée) et §26 (taille
    configurée plus restrictive que le modèle) sont le même contrôle vu des deux
    côtés : la fonction n'a pas à savoir laquelle des deux bornes a gagné.
    """
    md = _table(300)
    r = fragmenter_tableau_markdown(md, budget=budget, max_fragments=1000)
    assert r.fragments
    assert max(len(f) for f in r.fragments) <= budget


def test_un_budget_absurde_ne_leve_pas():
    for b in (0, -5, 1):
        r = fragmenter_tableau_markdown(_table(10), budget=b, max_fragments=10)
        assert all(len(f) <= max(1, b) for f in r.fragments)


# ----------------------------------------------------- borne anti-explosion
def test_le_nombre_de_fragments_est_borne_et_la_troncature_est_dite():
    """Une borne est nécessaire, mais elle ne doit pas mentir sur la couverture."""
    md = _table(400)
    r = fragmenter_tableau_markdown(md, budget=300, max_fragments=5)
    assert len(r.fragments) == 5
    assert r.tronque is True
    assert r.raison and "capped" in r.raison
    assert r.complet is False


# ============================================================ câblage indexation
# Les tests ci-dessus portent sur l'algorithme. Ceux-ci portent sur son BRANCHEMENT :
# c'est là que le défaut vivait réellement — la fonction de découpage n'existait pas,
# mais le point qui aurait dû l'appeler, si.
class _Res:
    def __init__(self, tables):
        self.tables = tables


def _tables(markdown, **meta):
    base = {"page_number": 1, "source_engine": "mineru", "bbox": [0.1, 0.2, 0.9, 0.8],
            "rows": 50, "columns": 4, "provenance": {"engine": "mineru"}}
    base.update(meta)
    return [{"markdown": markdown, "meta": base}]


def _specs(markdown, *, chunk_size, **meta):
    from apps.rag_core.services.ingest import indexing
    return indexing._build_table_specs(result=_Res(_tables(markdown, **meta)),
                                       filename="doc.pdf", start_chunk_index=7,
                                       chunk_size=chunk_size)


def test_le_cablage_respecte_le_budget_de_l_espace():
    """Le budget vient du DEHORS : c'est `resolve_chunk_budget` qui l'a décidé."""
    specs = _specs(_table(200), chunk_size=1000)
    assert len(specs) > 1
    assert all(len(s.text) <= 1000 for s in specs)


def test_le_cablage_laisse_intact_un_petit_tableau():
    """§27 : rien ne bouge pour un tableau qui tenait déjà."""
    md = _table(3)
    specs = _specs(md, chunk_size=10_000)
    assert len(specs) == 1
    assert specs[0].text == md
    assert specs[0].chunk_index == 7


def test_les_index_de_chunk_restent_contigus():
    specs = _specs(_table(200), chunk_size=1000)
    assert [s.chunk_index for s in specs] == list(range(7, 7 + len(specs)))


def test_la_provenance_et_la_geometrie_survivent_a_la_fragmentation():
    """§17/§19 : chaque fragment garde d'où il vient, et n'invente pas de bbox."""
    specs = _specs(_table(200), chunk_size=1000)
    assert len(specs) > 1
    for s in specs:
        t = s.meta["table"]
        assert t["source_engine"] == "mineru"
        assert t["bbox"] == [0.1, 0.2, 0.9, 0.8], "bbox du tableau, héritée telle quelle"
        assert t["provenance"] == {"engine": "mineru"}
        assert s.page_number == 1
        assert s.meta["kind"] == "table"


def test_les_fragments_d_un_meme_tableau_partagent_une_identite():
    """§18 : identité commune + rang + total, dans la meta JSON, sans migration."""
    specs = _specs(_table(200), chunk_size=1000)
    ids = {s.meta["table_id"] for s in specs}
    assert len(ids) == 1
    assert [s.meta["table_fragment_index"] for s in specs] == list(range(len(specs)))
    assert all(s.meta["table_fragment_count"] == len(specs) for s in specs)


def test_deux_tableaux_du_meme_document_ont_des_identites_distinctes():
    from apps.rag_core.services.ingest import indexing
    t = _tables(_table(200))[0]
    res = _Res([t, dict(t)])
    specs = indexing._build_table_specs(result=res, filename="doc.pdf",
                                        start_chunk_index=0, chunk_size=1000)
    assert len({s.meta["table_id"] for s in specs}) == 2


def test_un_tableau_ecourte_par_le_volume_le_signale():
    """Une troncature de volume doit se lire dans la meta, pas se deviner."""
    from apps.rag_core.services.ingest.extraction import config as _cfg
    enorme = _table(4000)
    assert len(enorme) > _cfg.table_max_chars()
    specs = _specs(enorme, chunk_size=1000)
    assert specs
    assert all(s.meta.get("table_truncated") for s in specs)
    assert specs[0].meta.get("table_truncation_reason")


# ---------------------------------------------------- §14 garde de dernier recours
def test_la_garde_ecrete_ce_qui_depasse_et_le_dit():
    """Dernière barrière : un seul chunk hors borne ferait échouer TOUT le document.

    On préfère perdre la fin de ce chunk-là — en le signalant — que le document.
    """
    from apps.rag_core.services.chunking import ChunkSpec
    from apps.rag_core.services.ingest import indexing
    specs = [ChunkSpec(chunk_index=0, text="a" * 50, page_number=1, char_start=0,
                       char_end=50, source="d.pdf", meta={}),
             ChunkSpec(chunk_index=1, text="b" * 5000, page_number=1, char_start=0,
                       char_end=5000, source="d.pdf", meta={})]
    out = indexing._enforce_chunk_budget(specs, budget=100, doc_id=None)
    assert [len(s.text) for s in out] == [50, 100]
    assert out[1].meta["chunk_budget_enforced"] is True
    assert out[1].meta["chunk_original_chars"] == 5000
    assert out[0].meta == {}, "un chunk conforme n'est pas touché"


def test_la_garde_borne_sur_le_plafond_du_modele_et_non_sur_la_granularite():
    """Régression mesurée en R4 : la confusion entre les deux bornes abîme l'Excel.

    L'extracteur Excel fenêtre lui-même ses chunks, jusqu'à 3 819 caractères. Ils
    sont au-dessus de la granularité produit (2 500) et bien en dessous de ce que
    le modèle gouverné accepte (6 000) — donc parfaitement valides. Les avoir
    écrêtés a coûté 8 517 caractères à trois classeurs qui n'avaient aucun
    problème. Une garde ne doit pas abîmer ce qu'elle protège.
    """
    from apps.rag_core.services.chunking import ChunkSpec
    from apps.rag_core.services.ingest import indexing
    excel = ChunkSpec(chunk_index=0, text="x" * 3819, page_number=None, char_start=0,
                      char_end=3819, source="c.xlsx", meta={"kind": "excel_table"})
    table = ChunkSpec(chunk_index=1, text="y" * 12000, page_number=1, char_start=0,
                      char_end=12000, source="d.pdf", meta={"kind": "table"})
    out = indexing._enforce_chunk_budget([excel, table], budget=6000, doc_id=None)
    assert len(out[0].text) == 3819, "un chunk Excel valide doit rester intact"
    assert "chunk_budget_enforced" not in (out[0].meta or {})
    assert len(out[1].text) == 6000, "seul ce qui menace le document est ecrete"
    assert out[1].meta["chunk_budget_enforced"] is True


def test_un_espace_non_gouverne_n_est_pas_ecrete():
    """Plafond inconnu : on ne coupe pas. Le contrat historique est préservé."""
    from apps.rag_core.services.chunking import ChunkSpec
    from apps.rag_core.services.ingest import indexing
    s = ChunkSpec(chunk_index=0, text="z" * 9000, page_number=1, char_start=0,
                  char_end=9000, source="d.pdf", meta={})
    for budget in (None, 0):
        out = indexing._enforce_chunk_budget([s], budget=budget, doc_id=None)
        assert out[0].text == s.text


def test_la_garde_ne_touche_rien_quand_tout_est_conforme():
    from apps.rag_core.services.chunking import ChunkSpec
    from apps.rag_core.services.ingest import indexing
    specs = [ChunkSpec(chunk_index=i, text="x" * 10, page_number=1, char_start=0,
                       char_end=10, source="d.pdf", meta={"kind": "table"})
             for i in range(3)]
    out = indexing._enforce_chunk_budget(specs, budget=2500, doc_id=None)
    assert out == specs
    assert all("chunk_budget_enforced" not in (s.meta or {}) for s in out)


# ------------------------------------------- §28 le rail générique reste éteint
def test_le_rail_generique_de_tableaux_pdf_reste_eteint_par_defaut():
    """Réutiliser une idée n'est pas activer une fonctionnalité.

    Le rail générique avait été mesuré NUISIBLE au retrieval ; ce lot emprunte son
    principe (fragmenter par ligne en répétant l'en-tête) sans le rallumer.
    """
    from apps.rag_core.services.ingest.extraction import config as _cfg
    assert _cfg.pdf_generic_tables_enabled() is False


# ------------------------------------------------- §20 doublons et charpente vide
def test_deux_fragments_identiques_ne_sont_jamais_emis():
    """Mesuré sur un bilan réel : MinerU y répétait sa ligne d'en-tête fusionnée.

    Deux lignes de 4 919 et 4 927 caractères, quasi identiques. Découpées au
    budget, leurs premières tranches l'étaient exactement. Deux chunks identiques
    portent le même vecteur : le second n'apporte rien et occupe une place au
    classement.
    """
    ligne = "| " + "en-tete tres long " * 200 + " |"
    md = "\n".join([ligne, "| --- | --- |", ligne, "| a | b |"])
    r = fragmenter_tableau_markdown(md, budget=600, max_fragments=100)
    assert len(set(r.fragments)) == len(r.fragments)


def test_un_fragment_de_pure_charpente_n_est_pas_indexe():
    """Une ligne de séparation restée seule n'est pas une donnée."""
    from apps.rag_core.services.ingest.extraction.table_chunking import _porte_du_contenu
    assert _porte_du_contenu("| --- | --- | --- |") is False
    assert _porte_du_contenu("|   |   |") is False
    assert _porte_du_contenu("| a | b |") is True

    ligne = "| " + "x" * 3000 + " |"
    md = "\n".join([ligne, "| --- | --- | --- | --- | --- | --- | --- | --- |",
                    "| valeur | utile |"])
    r = fragmenter_tableau_markdown(md, budget=700, max_fragments=100)
    assert all(_porte_du_contenu(f) for f in r.fragments)
    assert any("valeur" in f for f in r.fragments)


def test_le_nettoyage_ne_supprime_pas_des_lignes_de_donnees_distinctes():
    """Garde-fou inverse : deux lignes différentes restent deux fragments."""
    from apps.rag_core.services.ingest.extraction.table_chunking import _porte_du_contenu
    md = _table(120)
    r = fragmenter_tableau_markdown(md, budget=900, max_fragments=200)
    entete = _entete_de(md)
    corps = []
    for f in r.fragments:
        corps += [l for l in f.splitlines() if l != entete and _porte_du_contenu(l)]
    attendues = md.splitlines()[2:]
    assert corps == attendues
