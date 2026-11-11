"""une ligne désignée par la question et RÉCUPÉRÉE ne doit pas être perdue
avant génération ; une absence affirmée ne passe pas le contrôle quand le fragment récupéré la contredit.

Constat réel (REQUALIFICATION-10, R05) : fragment récupéré de 1 168 c. portant « 3 Local ménage 4 3,85
15,38 », extrait centré transmis de 874 c. qui l'omet ; le modèle affirme l'absence de la ligne et le contrôle,
qui ne voit que l'extrait, la laisse passer. Données SYNTHÉTIQUES, structure du cas réel.
"""
from __future__ import annotations

import pytest

from apps.rag_core.services.query import numeric_evidence as NE
from apps.rag_core.services.query.prompt_builder import (
    SNIPPET_STRATEGY_CENTERED,
    SNIPPET_STRATEGY_HEAD,
    _build_centered_snippet,
    _prepare_selected_for_prompt,
)

BUDGET = 800
ROW = "3 Local entretien 4 3,85 15,38"
Q = ("Dans le document « Synth.pdf », quelle est la valeur de « Surface utiles par catégorie » "
     "pour la ligne « Local entretien » ?")


def _page_text(row_position="top"):
    """Page de tableau APLATI (texte natif, aucune colonne prouvée), structure du cas réel : une ligne COURTE et
    dense en termes de la question (« Surfaces utiles par catégorie ») attire la fenêtre centrée, loin de la
    ligne désignée (deux termes seulement)."""
    rows = ["Salle de réunion 1 12,40 12,40", "Local archives 2 6,10 12,20", ROW, "Local reprographie 1 7,70 7,70"]
    others = [f"Bureau type {i} {i} {10 + i},50 {(10 + i) * i},50" for i in range(1, 31)]
    others += ["Local technique Nord 1 9,03 9,03", "Local technique Sud 1 38,74 38,74"]
    dense = ["Surfaces utiles par catégorie", "Niveau RDC et étages"]
    if row_position == "top":
        body = ["Tableau BCC niveau courant"] + rows + others + dense
    elif row_position == "bottom":
        body = dense + others + rows
    else:
        body = others[:16] + rows + others[16:] + dense
    return "\n".join(body)


def _chunk(text, *, doc="d-synth", page=3, filename="Synth.pdf", chunk_id="c-synth-3", **meta):
    return {"chunk_id": chunk_id, "text": text,
            "source": {"document_id": doc, "page_number": page, "filename": filename, "label": filename,
                       "meta": {"source_is_ocr": False, **meta}}}


def _prepared(text, question=Q, strategy=SNIPPET_STRATEGY_CENTERED, **kw):
    return _prepare_selected_for_prompt([_chunk(text, **kw)], snippet_max_chars=BUDGET, question=question,
                                        snippet_strategy=strategy)[0]


class TestQueryAnchoredContext:
    def test_baseline_centered_window_loses_the_designated_row(self):
        """Reproduction du défaut : la fenêtre centrée seule omet la ligne désignée."""
        text = _page_text()
        assert ROW in text and len(text) > BUDGET
        assert ROW not in _build_centered_snippet(text, Q, BUDGET)

    @pytest.mark.parametrize("position", ["top", "middle", "bottom"])
    def test_designated_row_is_in_the_transmitted_excerpt_at_constant_budget(self, position):
        text = _page_text(position)
        out = _prepared(text)
        assert ROW in out["text"] and len(out["text"]) <= BUDGET
        assert out["chunk_id"] == "c-synth-3" and out["source"]["filename"] == "Synth.pdf" and out["source"]["page_number"] == 3

    def test_values_keep_their_order_and_the_label_is_never_cut(self):
        out = _prepared(_page_text())["text"]
        assert ROW in out and "Local entretien 4 3,85 15,38" in out

    def test_head_strategy_is_unchanged(self):
        text = _page_text()
        out = _prepared(text, strategy=SNIPPET_STRATEGY_HEAD)
        assert out["text"] == text[:BUDGET - 1].rstrip() + "…"

    @pytest.mark.parametrize("question", [
        pytest.param("Quelle est la surface utile par catégorie des locaux ?", id="no-designated-row"),
        pytest.param("Quelle est la valeur pour la ligne « Local ent » ?", id="partial-label"),
        pytest.param("Quelle est la valeur pour la ligne « Local » ?", id="generic-word"),
        pytest.param("Quelle est la valeur pour la ligne « Local entretien Est » ?", id="absent-label"),
    ])
    def test_no_reliable_row_keeps_the_existing_window(self, question):
        text = _page_text()
        assert _prepared(text, question=question)["text"] == _build_centered_snippet(text, question, BUDGET)

    def test_close_labels_are_not_interchangeable(self):
        """« Local technique Nord » désigné : jamais la ligne « Local technique Sud »."""
        text = _page_text("top")
        q = "Quelle est la valeur pour la ligne « Local technique Nord » ?"
        out = _prepared(text, question=q)["text"]
        assert "Local technique Nord 1 9,03 9,03" in out

    def test_row_already_visible_keeps_the_existing_window(self):
        text = _page_text("bottom")
        q = "Quelle est la valeur pour la ligne « Local technique Sud » ?"
        base = _build_centered_snippet(text, q, BUDGET)
        if "Local technique Sud 1 38,74 38,74" in base:
            assert _prepared(text, question=q)["text"] == base

    def test_short_fragment_is_returned_whole(self):
        assert _prepared(ROW)["text"] == ROW

    def test_tabular_and_structured_contracts_are_unchanged(self):
        """Fragment du rail tabulaire : coupe sur frontière de ligne inchangée (aucun ancrage)."""
        text = _page_text()
        a = _prepare_selected_for_prompt([_chunk(text, kind="table", table_type="generic", table={})],
                                         snippet_max_chars=BUDGET, question=Q, snippet_strategy=SNIPPET_STRATEGY_CENTERED)[0]
        b = _prepare_selected_for_prompt([_chunk(text, kind="table", table_type="generic", table={})],
                                         snippet_max_chars=BUDGET, question="Question sans ligne désignée ?",
                                         snippet_strategy=SNIPPET_STRATEGY_CENTERED)[0]
        assert a["text"] == b["text"]

    def test_making_the_row_visible_never_certifies_its_numbers(self):
        """La ligne visible porte deux décimales (3,85 ; 15,38) : colonne indéterminable, aucune n'est prouvée."""
        out = _prepared(_page_text())
        src = NE.annotate_numeric_evidence([out, _chunk("vignette", page=1, chunk_id="c-ocr", source_is_ocr=True,
                                                        requires_visual_check=False, page_type="text")], table_pages_by_doc={})
        for value in ("15,38", "3,85"):
            d = NE.guard_answer(f"La valeur pour « Local entretien » est de {value} m² [1].", question=Q, sources=src)
            assert d.verdict != NE.VERDICT_PASS and value not in d.answer

    def test_single_decimal_flattened_row_is_still_proven(self):
        """Non-régression (forme R06) : « Local technique Nord 1 9,03 9,03 » prouve 9,03 pour cette ligne."""
        q = "Quelle est la valeur de « Surface utiles par catégorie » pour la ligne « Local technique Nord » ?"
        out = _prepared(_page_text(), question=q)
        src = NE.annotate_numeric_evidence([out, _chunk("vignette", page=1, chunk_id="c-ocr", source_is_ocr=True,
                                                        requires_visual_check=False, page_type="text")], table_pages_by_doc={})
        answer = "La valeur de « Surface utiles par catégorie » pour « Local technique Nord » est de 9,03 [1]."
        d = NE.guard_answer(answer, question=q, sources=src)
        assert (d.verdict, d.answer) == (NE.VERDICT_PASS, answer)


# --------------------------------------------------------------------------- #
# Bloc B — contrôle d'absence sur le fragment RÉCUPÉRÉ entier de la source citée.

from apps.chat.services.chat_sources import normalize_source_for_api  # noqa: E402
from apps.rag_core.services.query.audit import normalize_sources_for_audit  # noqa: E402
from apps.rag_core.services.query.prompt_builder import RETRIEVED_TEXT_KEY, _build_prompt_with_budget  # noqa: E402

FALSE_ABSENCE = "Je ne trouve pas de mention de « Local entretien » dans le document « Synth.pdf » [1]."
KEEP = "Je ne peux donc pas fournir la valeur demandée."


def _ocr_vignette():
    return _chunk("Tableau détaillé des surfaces", page=1, chunk_id="c-ocr", source_is_ocr=True,
                  requires_visual_check=False, page_type="text")


def _sources_head_excerpt(full=None):
    """Extrait en TÊTE (la ligne désignée, en bas du fragment, n'y est pas) + fragment entier attaché."""
    text = _page_text("bottom")
    prepared = _prepared(text, strategy=SNIPPET_STRATEGY_HEAD)
    assert ROW not in prepared["text"] and prepared[RETRIEVED_TEXT_KEY] == text
    if full is not None:
        prepared[RETRIEVED_TEXT_KEY] = full
    return NE.annotate_numeric_evidence([prepared, _ocr_vignette()], table_pages_by_doc={})


class TestFullFragmentAbsence:
    def test_excerpt_only_reproduces_the_real_defect(self):
        """Sans fragment entier (chemin de e948020) : l'absence fausse passe le contrôle."""
        sources = _sources_head_excerpt()
        for s in sources:
            s.pop(RETRIEVED_TEXT_KEY, None)
        d = NE.guard_answer(f"{FALSE_ABSENCE} {KEEP}", question=Q, sources=sources)
        assert FALSE_ABSENCE in d.answer and d.absences_removed == 0

    def test_full_fragment_of_the_cited_source_contradicts_the_absence(self):
        d = NE.guard_answer(f"{FALSE_ABSENCE} {KEEP}", question=Q, sources=_sources_head_excerpt())
        assert FALSE_ABSENCE not in d.answer and KEEP in d.answer and d.absences_removed == 1

    def test_inconsistent_full_fragment_is_never_used(self):
        """Un fragment entier qui ne contient pas l'extrait transmis (autre source, autre version) n'est pas une preuve."""
        other = "Fragment d'un autre document\n" + ROW + "\nAucun rapport avec l'extrait"
        d = NE.guard_answer(FALSE_ABSENCE, question=Q, sources=_sources_head_excerpt(full=other))
        assert FALSE_ABSENCE in d.answer and d.absences_removed == 0

    def test_absence_scoped_to_another_source_is_kept(self):
        """« La source [2] (vignette p.1) ne contient pas… » : le fragment entier de [1] ne la contredit pas."""
        answer = "La source [2] ne contient pas de ligne « Local entretien »."
        d = NE.guard_answer(answer, question=Q, sources=_sources_head_excerpt())
        assert answer in d.answer and d.absences_removed == 0

    @pytest.mark.parametrize("answer", [
        "La ligne existe, mais sa colonne n'est pas prouvée [1].",
        "Je ne peux pas déterminer la surface demandée.",
        "Cette valeur ne peut pas être certifiée.",
        "Il manque une preuve géométrique suffisante pour la ligne « Local entretien » [1].",
        "La valeur de « Surface utiles par catégorie » pour « Local entretien » n'est pas présente dans le document [1].",
    ])
    def test_legitimate_negations_are_kept_with_the_full_fragment(self, answer):
        d = NE.guard_answer(answer, question=Q, sources=_sources_head_excerpt())
        assert answer in d.answer and d.absences_removed == 0

    def test_numbers_are_never_proven_by_the_full_fragment(self):
        """Anti-substitution : un nombre présent seulement dans le fragment entier (pas dans l'extrait vu par le
        modèle) n'est jamais prouvé — le contrôle numérique lit `text`, pas le fragment entier."""
        q = "Quelle est la valeur de « Surface utiles par catégorie » pour la ligne « Local technique Nord » ?"
        sources = _sources_head_excerpt()
        assert "9,03" not in sources[0]["text"] and "9,03" in sources[0][RETRIEVED_TEXT_KEY]
        d = NE.guard_answer("La valeur pour « Local technique Nord » est de 9,03 [1].", question=q, sources=sources)
        assert "9,03" not in d.answer

    def test_budgeted_prompt_sources_carry_the_full_fragment_but_the_prompt_does_not(self):
        text = _page_text("bottom")
        prompt, selected, _info = _build_prompt_with_budget(
            question_for_prompt=Q, selected=[_chunk(text)], template_content=None, max_prompt_chars=18_000,
            snippet_max_chars=BUDGET, question=Q, snippet_strategy=SNIPPET_STRATEGY_HEAD)
        assert selected[0][RETRIEVED_TEXT_KEY] == text and ROW not in selected[0]["text"]
        assert ROW not in prompt and selected[0]["chunk_id"] == "c-synth-3"

    def test_full_fragment_never_reaches_the_api_nor_the_audit(self):
        prepared = _prepared(_page_text("bottom"), strategy=SNIPPET_STRATEGY_HEAD)
        api = normalize_source_for_api(prepared, 1)
        audit = normalize_sources_for_audit([prepared])[0]
        for projected in (api, audit):
            assert RETRIEVED_TEXT_KEY not in projected and ROW not in str(projected)


class TestAbsenceFormsObservedInRequalification10:
    """Formes RÉELLES restées publiées dans REQUALIFICATION-10 (R05) — libellés synthétiques."""

    def test_aucune_ligne_correspondante_a_is_an_absence_claim(self):
        answer = "Dans le tableau fourni dans la source [1], aucune ligne correspondante à « Local entretien » n'est présente."
        d = NE.guard_answer(answer, question=Q, sources=_sources_head_excerpt())
        assert answer not in d.answer and d.absences_removed == 1

    def test_aucune_ne_correspond_follows_a_demonstrated_contradiction(self):
        answer = (f"{FALSE_ABSENCE} Les lignes présentes concernent des catégories comme « Salle de réunion », "
                  "mais aucune ne correspond à « Local entretien ».")
        d = NE.guard_answer(answer, question=Q, sources=_sources_head_excerpt())
        assert "aucune ne correspond" not in d.answer and d.absences_removed == 2

    def test_seules_references_mentionnant_is_an_exclusivity_claim(self):
        """Forme réelle : la citation désigne une source d'un AUTRE document ([3]) ; [1] porte la ligne."""
        other = _chunk("C 1.1 Local entretien NC NC", doc="d-other", page=1, filename="Autre.pdf", chunk_id="c-other",
                       kind="table", table_type="generic", table={})
        sources = _sources_head_excerpt() + NE.annotate_numeric_evidence([other], table_pages_by_doc={})
        answer = "Les seules références mentionnant « Local entretien » se trouvent dans d'autres documents (ex. [3])."
        d = NE.guard_answer(answer, question=Q, sources=sources)
        assert answer not in d.answer and d.absences_removed == 1

    @pytest.mark.parametrize("answer", [
        "Aucune des sources ne fournit une valeur de surface utile pour « Local entretien » [1].",
        "Je ne trouve pas la valeur de « Surface utiles par catégorie » pour la ligne « Local entretien » [1].",
        "Aucune valeur ne correspond à « Local entretien » dans [1].",
    ])
    def test_value_negations_stay_out_of_scope(self, answer):
        d = NE.guard_answer(answer, question=Q, sources=_sources_head_excerpt())
        assert answer in d.answer and d.absences_removed == 0


# --------------------------------------------------------------------------- #
# Bloc C — contrat de capture : ce que le contrôle voit = ce que le modèle a reçu.

class TestFaithfulReplayContract:
    def test_each_budgeted_source_excerpt_is_exactly_its_prompt_block(self):
        """Le `text` d'une source budgétée est, au caractère près, le corps de son bloc [n] dans le prompt :
        un rejeu reconstruit depuis le prompt capturé est donc fidèle au contrôle, et un rejeu depuis le
        fragment entier ne l'est pas."""
        texts = [_page_text("bottom"), _page_text("top")]
        chunks = [_chunk(t, chunk_id=f"c-{i}", page=3 + i) for i, t in enumerate(texts)]
        prompt, selected, _info = _build_prompt_with_budget(
            question_for_prompt=Q, selected=chunks, template_content=None, max_prompt_chars=18_000,
            snippet_max_chars=BUDGET, question=Q, snippet_strategy=SNIPPET_STRATEGY_CENTERED)
        body = prompt.split("\nSOURCES:\n", 1)[1].split("\nQUESTION:", 1)[0]
        for i, src in enumerate(selected, start=1):
            assert f"[{i}] Synth.pdf" in body and src["text"] in body
            assert src[RETRIEVED_TEXT_KEY] != src["text"] and src[RETRIEVED_TEXT_KEY] not in body

    def test_substituting_the_full_fragment_for_the_excerpt_changes_the_decision(self):
        """Garde-fou méthodologique : remplacer l'extrait par le fragment entier n'est PAS neutre (c'est ce qui
        rendait le rejeu de RECOVERY-9 trop favorable) ; le contrôle candidat sur les entrées fidèles décide
        par le fragment ATTACHÉ, jamais par un `text` substitué."""
        faithful = _sources_head_excerpt()
        substituted = [dict(s, text=s.get(RETRIEVED_TEXT_KEY) or s["text"]) for s in faithful]
        for s in substituted:
            s.pop(RETRIEVED_TEXT_KEY, None)
        excerpt_only = [{k: v for k, v in s.items() if k != RETRIEVED_TEXT_KEY} for s in faithful]
        q = "Quelle est la valeur de « Surface utiles par catégorie » pour la ligne « Local technique Nord » ?"
        answer = "La valeur pour « Local technique Nord » est de 9,03 [1]."
        assert "9,03" in NE.guard_answer(answer, question=q, sources=substituted).answer   # faux GO de rejeu
        assert "9,03" not in NE.guard_answer(answer, question=q, sources=excerpt_only).answer
        assert "9,03" not in NE.guard_answer(answer, question=q, sources=faithful).answer
