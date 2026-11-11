"""Correctif FTS — une entité absente du texte ne doit plus condamner la recherche.

Le bug, découvert sur le PREMIER vrai classeur métier ingéré :

    question : « donne les shares outstanding pour Airworks »
    fichier  : AirWorks - 2026 Financing Pro Forma [Goodwin Draft 05.19.26].xlsx

L'heuristique anti-bruit privilégiait les entités (termes capitalisés) et **jetait
tous les autres mots-clés**. La requête FTS devenait `airworks:*` — qui ne matche
AUCUN chunk : « AirWorks » est dans le NOM DU FICHIER, jamais dans le contenu (qui ne
parle que de `Series A-3`, `Common`, `Warrants`). Pendant ce temps, `shares` et
`outstanding` — les seuls mots utiles — étaient supprimés.

Zéro résultat lexical, une réponse « Je ne sais pas », sur un document parfaitement
indexé. Le bug est **préexistant** : il ne concerne pas qu'Excel. Il ne se manifestait
simplement pas sur un corpus de presse, où l'entité est toujours dans le texte.
"""
import pytest

from apps.rag_core.repositories.chunk_repository import (
    _build_tsquery,
    build_tsquery_plan,
    search_chunks_hybrid,
)


class TestPlan:
    def test_entity_first_then_keywords(self):
        """LE cas : l'entité reste prioritaire, mais les mots-clés ne sont plus jetés."""
        plan = build_tsquery_plan("donne les shares outstanding pour Airworks")
        assert plan[0] == "airworks:*"                       # sélectif d'abord
        assert len(plan) == 2
        assert "shares:*" in plan[1] and "outstanding:*" in plan[1]

    def test_several_entities_are_still_anded(self):
        plan = build_tsquery_plan("Que disent ORVANIS et Nordexia du marché ?")
        assert plan[0] == "orvanis:* & nordexia:*"

    def test_without_entity_the_plan_has_a_single_broad_query(self):
        plan = build_tsquery_plan("quelle est la premoney valuation")
        assert plan == ["premoney:* | valuation:*"]

    def test_no_duplicate_when_the_two_queries_coincide(self):
        plan = build_tsquery_plan("ORVANIS")
        assert plan == ["orvanis:*"]

    def test_a_bare_number_is_a_constraint_not_a_name(self):
        """🔴 Régression attrapée par le benchmark RAG (`no_answer_accuracy` 1,0 -> 0,5).

        « Quel est le chiffre d'affaires **2030** ? » : `2030` est traité comme un terme
        capitalisé. S'il est absent du corpus, la réponse est VRAIMENT « je ne sais pas ».
        Élargir à ``chiffre | affaires | entreprise`` ne répondrait pas à la question —
        ça ramènerait du bruit et DÉTRUIRAIT une non-réponse correcte.

        Un NOM absent du texte, lui, a une explication banale : il est dans le nom du
        fichier. C'est là, et seulement là, que l'élargissement est légitime.
        """
        plan = build_tsquery_plan("Quel est le chiffre d'affaires 2030 de l'entreprise ?")
        assert plan == ["2030:*"], "aucun repli sur une contrainte numérique"

    def test_a_named_entity_alongside_a_number_still_falls_back(self):
        plan = build_tsquery_plan("chiffre d'affaires 2030 de Airworks")
        assert len(plan) == 2
        assert "airworks:*" in plan[1]

    def test_empty_question_has_no_plan(self):
        assert build_tsquery_plan("") == []
        assert build_tsquery_plan("le de la") == []

    def test_primary_query_is_unchanged(self):
        """Non-régression : l'ancien `_build_tsquery` renvoie toujours la primaire."""
        assert _build_tsquery("donne les shares outstanding pour Airworks") == "airworks:*"
        assert _build_tsquery("que dit-on de ORVANIS ?") == "orvanis:*"


@pytest.mark.django_db
class TestFallbackOnRealData:
    """Le repli ne se déclenche que sur ZÉRO résultat : il ne dilue jamais une
    recherche qui fonctionnait."""

    def _seed(self, rag_space):
        from apps.rag_core.models import Chunk, Document
        from apps.rag_core.models.document import DocumentStatus

        wb = Document.objects.create(
            rag_space=rag_space, filename="AirWorks - 2026 Financing Pro Forma.xlsx",
            content_type="", size_bytes=1, sha256="a", status=DocumentStatus.READY,
        )
        pdf = Document.objects.create(
            rag_space=rag_space, filename="Revue_de_presse.pdf",
            content_type="", size_bytes=1, sha256="b", status=DocumentStatus.READY,
        )
        # Le contenu du classeur ne contient JAMAIS « AirWorks » — comme dans la vraie vie.
        Chunk.objects.create(
            rag_space=rag_space, document=wb, chunk_index=0,
            text="Feuille : Summary Pro Forma (A2:S13)\n\n| Series A-3 | Shares Outstanding | "
                 "Fully Diluted Shares |\n| Common | 6234211 | 6234211 |",
            embedding=[0.0] * 768, meta={"kind": "excel_table", "sheet_name": "Summary Pro Forma"},
        )
        Chunk.objects.create(
            rag_space=rag_space, document=pdf, chunk_index=0,
            text="Le transporteur ORVANIS surveille le nombre de pays imposant des barrieres.",
            embedding=[0.0] * 768, meta={},
        )

    def test_the_reported_question_now_finds_the_workbook(self, rag_space):
        self._seed(rag_space)
        rows = search_chunks_hybrid(
            rag_space_id=rag_space.id,
            question="donne les shares outstanding pour Airworks",
            top_k=5, min_text_rank=0.0,
        )
        assert rows, "la question signalée ne doit plus renvoyer 0 résultat"
        assert rows[0]["source"]["filename"].endswith(".xlsx")

    def test_an_entity_present_in_the_text_does_not_trigger_the_fallback(self, rag_space):
        """« ORVANIS » est DANS le texte : la requête entité suffit, le repli reste inutilisé.

        C'est ce qui préserve l'heuristique anti-bruit sur un corpus de presse.
        """
        self._seed(rag_space)
        rows = search_chunks_hybrid(
            rag_space_id=rag_space.id, question="que dit-on de ORVANIS ?",
            top_k=5, min_text_rank=0.0,
        )
        assert rows
        assert all(r["source"]["filename"].endswith(".pdf") for r in rows)
        # Le classeur n'est PAS ramené : le repli large aurait pu le faire, il n'a pas eu lieu.
        assert not any(r["source"]["filename"].endswith(".xlsx") for r in rows)

    def test_an_unknown_entity_with_no_useful_keyword_still_returns_nothing(self, rag_space):
        """Le repli n'invente rien : sans mot-clé exploitable, on ne ramène rien."""
        self._seed(rag_space)
        rows = search_chunks_hybrid(
            rag_space_id=rag_space.id, question="Zorglub",
            top_k=5, min_text_rank=0.0,
        )
        assert rows == []
