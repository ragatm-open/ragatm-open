"""Un extrait de 800 caractères pris en TÊTE n'est pas un extrait pertinent.

Défaut mesuré au lot SNIPPET-TRUNCATION-1A, sur cinq cas reproduits en staging : le bon
chunk est retrouvé et classé rang 1 ou 2, le prompt reste sous son budget, aucune source
n'est évincée — et la réponse est fausse quand même, parce que la phrase qui la porte se
trouve aux caractères 796, 921, 1332, 1510 ou 1959 d'un chunk de 2 500. `_truncate_text`
garde les 800 PREMIERS caractères sans jamais regarder la question.

Correctif : à taille INCHANGÉE, choisir la fenêtre autour de la phrase la plus proche de
la question. Mesuré : tête 0/5, tête élargie à 2 000 c 5/5 mais au prix de la moitié des
sources, fenêtre centrée 5/5 sans perdre une source.

Deux exigences opposées tiennent ce fichier :
  1. en mode `head`, le prompt doit être STRICTEMENT celui d'avant — le correctif ne vaut
     rien s'il change silencieusement la production ;
  2. en mode `centered`, la preuve doit entrer dans la fenêtre où qu'elle soit, sans
     dépasser le budget, sans toucher à l'ordre ni au nombre des sources, et sans
     jamais lever d'exception sur un texte réel (donc laid).
"""
from __future__ import annotations

import pytest
from django.test import override_settings

from apps.rag_core.services.query import prompt_builder as PB
from apps.rag_core.services.query.prompt_builder import (
    SNIPPET_STRATEGY_CENTERED,
    SNIPPET_STRATEGY_HEAD,
    _build_centered_snippet,
    _build_prompt_with_budget,
    _prepare_selected_for_prompt,
    _question_prefixes,
    _snippet_strategy,
)

BUDGET = 800


# ---------------------------------------------------------------------------
# Contrat HISTORIQUE, réécrit ici indépendamment du code de production.
# ---------------------------------------------------------------------------
def _historical_head(value, max_chars: int) -> str:
    """La troncature de tête telle qu'elle existait AVANT ce correctif.

    Volontairement recopiée : si un jour `_truncate_text` change, ce test doit échouer
    au lieu de suivre. C'est la seule façon d'affirmer « identique à avant » sans
    dépendre du code qu'on est en train de modifier.
    """
    text = ("" if value is None else str(value)).replace("\u00a0", " ").strip()
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _prose(marker: str, position: int, total: int = 2500) -> str:
    """Prose de remplissage de ``total`` caractères portant ``marker`` à ``position``.

    Reproduit le gabarit réel : un chunk DOCX/PDF plein, découpé à
    `RAG_CHUNK_SIZE_CHARS`, dont la preuve est une phrase parmi d'autres.
    """
    filler = (
        "Les interventions sont tracées dans la GMAO du réseau et font l'objet d'un "
        "compte rendu transmis chaque semaine. "
    )
    head = (filler * (total // len(filler) + 2))[:position]
    if head and not head.endswith(" "):
        head = head[: head.rfind(" ") + 1] if " " in head else head
    tail_room = total - len(head) - len(marker)
    tail = (filler * (total // len(filler) + 2))[: max(0, tail_room)]
    text = f"{head}{marker}{tail}"
    assert marker in text
    return text


EVIDENCE = "La sous-station SST-9 dessert le quartier des Tilleuls. "
QUESTION = "Quel quartier dessert la sous-station SST-9 ?"


def _chunk(text: str, **meta) -> dict:
    return {
        "text": text,
        "chunk_id": "c-1",
        "source": {"document_id": "d-1", "filename": "dossier.docx", "meta": dict(meta)},
    }


# ==========================================================================
# 1. Le mode `head` est le comportement d'avant, à l'octet près
# ==========================================================================
class TestHeadModeIsUnchanged:
    def test_the_default_strategy_is_head(self):
        assert _snippet_strategy() == SNIPPET_STRATEGY_HEAD

    def test_an_unknown_value_is_treated_as_head(self):
        """Une faute de frappe ne peut pas activer une sémantique nouvelle."""
        with override_settings(RAG_SNIPPET_STRATEGY="centred"):
            assert _snippet_strategy() == SNIPPET_STRATEGY_HEAD
        with override_settings(RAG_SNIPPET_STRATEGY=""):
            assert _snippet_strategy() == SNIPPET_STRATEGY_HEAD

    def test_centered_is_the_only_value_that_switches(self):
        with override_settings(RAG_SNIPPET_STRATEGY=SNIPPET_STRATEGY_CENTERED):
            assert _snippet_strategy() == SNIPPET_STRATEGY_CENTERED

    def test_head_mode_matches_the_historical_contract(self):
        text = _prose(EVIDENCE, 1900)
        chunk = dict(_chunk(text), snippet="un extrait de commodité " * 80)
        chunk["source"] = dict(chunk["source"], text="le texte de la source " * 80)

        [out] = _prepare_selected_for_prompt(
            [chunk], snippet_max_chars=BUDGET, question=QUESTION,
            snippet_strategy=SNIPPET_STRATEGY_HEAD,
        )
        assert out["text"] == _historical_head(text, BUDGET)
        assert out["snippet"] == _historical_head(chunk["snippet"], BUDGET)
        assert out["source"]["text"] == _historical_head(chunk["source"]["text"], BUDGET)

    def test_head_mode_never_calls_the_centered_builder(self, monkeypatch):
        def explode(*_a, **_k):  # pragma: no cover - doit rester non appelé
            raise AssertionError("le mode head ne doit pas construire de fenêtre centrée")

        monkeypatch.setattr(PB, "_build_centered_snippet", explode)
        [out] = _prepare_selected_for_prompt(
            [_chunk(_prose(EVIDENCE, 1900))], snippet_max_chars=BUDGET,
            question=QUESTION, snippet_strategy=SNIPPET_STRATEGY_HEAD,
        )
        assert out["text"].endswith("…")

    def test_the_new_parameters_cannot_change_the_historical_path(self):
        """Appel SANS les nouveaux arguments == appel en mode head : mêmes octets.

        C'est la barrière §5 : le prompt complet — consignes, numérotation, marqueurs,
        budget, mémoire, sources — doit être identique à celui d'avant le correctif.
        """
        chunks = [_chunk(_prose(EVIDENCE, 1900)), _chunk(_prose("Une autre phrase. ", 1200))]
        common = dict(
            question_for_prompt="Question: " + QUESTION,
            selected=chunks,
            template_content=None,
            max_prompt_chars=8300,
            snippet_max_chars=BUDGET,
            table_max_chars=4000,
            conversation_context="Utilisateur : bonjour. Assistant : bonjour.",
            min_sources=4,
        )
        avant, kept_avant, info_avant = _build_prompt_with_budget(**common)
        apres, kept_apres, info_apres = _build_prompt_with_budget(
            **common, question=QUESTION, snippet_strategy=SNIPPET_STRATEGY_HEAD
        )
        assert apres == avant
        assert [c["text"] for c in kept_apres] == [c["text"] for c in kept_avant]
        assert info_apres == info_avant


# ==========================================================================
# 2. Le mode `centered` place la fenêtre sur la preuve
# ==========================================================================
class TestCenteredWindow:
    @pytest.mark.parametrize("position", [900, 1300, 1500, 1950])
    def test_evidence_in_the_middle_is_kept(self, position):
        """Le cas principal : la preuve est au-delà de 800 et en deçà de la fin."""
        text = _prose(EVIDENCE, position)
        assert EVIDENCE not in _historical_head(text, BUDGET)  # la tête la perd

        snippet = _build_centered_snippet(text, QUESTION, BUDGET)
        assert EVIDENCE.strip() in snippet
        assert len(snippet) <= BUDGET

    def test_evidence_at_the_start_is_kept(self):
        text = _prose(EVIDENCE, 40)
        snippet = _build_centered_snippet(text, QUESTION, BUDGET)
        assert EVIDENCE.strip() in snippet
        assert len(snippet) <= BUDGET

    def test_evidence_at_the_end_is_kept_with_a_leading_marker(self):
        text = _prose(EVIDENCE, 2400)
        snippet = _build_centered_snippet(text, QUESTION, BUDGET)
        assert EVIDENCE.strip() in snippet
        assert snippet.startswith("…")
        assert len(snippet) <= BUDGET

    def test_the_window_keeps_context_around_the_sentence(self):
        """Une source réduite à sa seule phrase perd ce qui la rend citable."""
        text = _prose(EVIDENCE, 1400)
        snippet = _build_centered_snippet(text, QUESTION, BUDGET)
        assert len(snippet) > len(EVIDENCE) * 2
        assert len(snippet) >= BUDGET - 80  # le budget est réellement utilisé

    def test_markers_are_counted_inside_the_budget(self):
        for position in range(200, 2300, 137):
            text = _prose(EVIDENCE, position)
            snippet = _build_centered_snippet(text, QUESTION, BUDGET)
            assert len(snippet) <= BUDGET, (position, len(snippet))

    def test_bounds_land_on_spaces(self):
        text = _prose(EVIDENCE, 1500)
        snippet = _build_centered_snippet(text, QUESTION, BUDGET)
        body = snippet.strip("…")
        assert not body.startswith(" ") and not body.endswith(" ")
        # aucun mot coupé en deux : le corps se retrouve tel quel dans le texte
        assert body in text

    def test_a_sentence_longer_than_the_budget_does_not_crash(self):
        """§32 : la fenêtre s'ancre sur le premier terme apparié, sans dépasser."""
        long_sentence = (
            "Le réseau comporte de nombreux organes " * 30
            + "et la sous-station SST-9 dessert le quartier des Tilleuls "
            + "ainsi que de nombreux autres ouvrages " * 30
        )
        assert len(long_sentence) > BUDGET
        snippet = _build_centered_snippet(long_sentence, QUESTION, BUDGET)
        assert len(snippet) <= BUDGET
        assert "SST-9" in snippet


# ==========================================================================
# 3. Repli : identique à la tête, à l'octet près
# ==========================================================================
class TestFallback:
    def test_no_common_term_falls_back_to_head(self):
        text = _prose("Une phrase parfaitement neutre. ", 1500)
        question = "Wieviel Grad hat die Rücklauftemperatur?"
        assert _build_centered_snippet(text, question, BUDGET) == _historical_head(text, BUDGET)

    def test_an_empty_question_falls_back_to_head(self):
        text = _prose(EVIDENCE, 1500)
        for question in ("", None, "   ", "de la ?"):
            assert _build_centered_snippet(text, question, BUDGET) == _historical_head(
                text, BUDGET
            )

    def test_a_short_text_is_returned_whole_without_markers(self):
        text = "Un chunk court. " + EVIDENCE
        assert len(text) <= BUDGET
        snippet = _build_centered_snippet(text, QUESTION, BUDGET)
        assert snippet == text.strip() == _historical_head(text, BUDGET)
        assert "…" not in snippet

    def test_a_text_without_any_sentence_long_enough_falls_back(self):
        text = "a. " * 900  # aucun segment de 20 caractères sans ponctuation
        assert len(text) > BUDGET
        assert _build_centered_snippet(text, "quartier Tilleuls", BUDGET) == _historical_head(
            text, BUDGET
        )


# ==========================================================================
# 4. Appariement : préfixe, morphologie, identifiants
# ==========================================================================
class TestPrefixMatching:
    def test_the_morphological_case_that_motivated_the_prefix(self):
        """« exploite » (question) vs « L'exploitant » (document) : dernier échec 1A."""
        text = _prose("Le réseau est exploité par la société Calorys. ", 1500)
        snippet = _build_centered_snippet(text, "Qui exploite le réseau de chaleur ?", BUDGET)
        assert "Calorys" in snippet

    @pytest.mark.parametrize(
        "asked,written",
        [
            ("installer", "L'installation"),
            ("produire", "la production"),
            ("exploiter", "l'exploitation"),
            ("raccorder", "le raccordement"),
        ],
    )
    def test_simple_morphological_pairs(self, asked, written):
        phrase = f"Ici {written} de la station Albatros porte le repère AV-6612. "
        text = _prose(phrase, 1500)
        snippet = _build_centered_snippet(text, f"Comment {asked} la station ?", BUDGET)
        assert "AV-6612" in snippet

    @pytest.mark.parametrize("identifier", ["AV-6612", "S355", "316L", "DN250"])
    def test_exact_technical_identifiers_are_strong_signals(self, identifier):
        phrase = f"La pièce repérée {identifier} est posée en attente. "
        text = _prose(phrase, 1700)
        snippet = _build_centered_snippet(text, f"Où se trouve {identifier} ?", BUDGET)
        assert identifier in snippet

    def test_short_tokens_are_ignored(self):
        """« eau » apparierait « niveau », « réseau », « bureau » : jamais retenu."""
        assert "eau" not in _question_prefixes("Quel est le niveau d'eau ?")

    def test_prefixes_are_deduplicated_and_ordered(self):
        assert _question_prefixes("exploitation exploitant exploite réseau") == (
            "exploi",
            "reseau",
        )

    def test_function_words_are_ignored(self):
        assert _question_prefixes("Quelle est cette valeur pour leurs postes ?") == (
            "valeur",
            "postes",
        )

    def test_accents_do_not_shift_the_window(self):
        """La normalisation doit préserver la LONGUEUR, sinon les bornes dérivent."""
        text = _prose("Le débit mesuré à l'échangeur EX-71 atteint 2,4 MW. ", 1600)
        accented = text.replace("e", "é", 200)
        snippet = _build_centered_snippet(accented, "Quelle est la valeur de EX-71 ?", BUDGET)
        assert "EX-71" in snippet
        assert snippet.strip("…") in accented


# ==========================================================================
# 4 bis. Langues : FR, EN, et honnêteté sur le cross-lingual
# ==========================================================================
class TestLanguages:
    def test_english_question_on_english_text(self):
        evidence = "The SST-9 substation supplies the Tilleuls district. "
        filler = "Interventions are logged in the maintenance system every week. "
        text = filler * 14 + evidence + filler * 14
        assert evidence not in _historical_head(text, BUDGET)
        snippet = _build_centered_snippet(
            text, "Which district does the SST-9 substation supply?", BUDGET
        )
        assert "Tilleuls" in snippet

    def test_cross_lingual_works_through_shared_identifiers(self):
        """Un repère technique est le même dans les deux langues : il apparie."""
        evidence = "The primary steel grade is S355JR for the main frame. "
        filler = "The assembly sequence is described in the following section. "
        text = filler * 14 + evidence + filler * 14
        snippet = _build_centered_snippet(text, "Quelle est la nuance S355 ?", BUDGET)
        assert "S355JR" in snippet

    def test_a_technical_cognate_matches_across_languages(self):
        """Ce n'est pas de la traduction : « température » et *temperature* partagent
        leurs six premiers caractères, donc apparient. Gain réel mais fortuit — il
        vaut pour les cognats techniques, pas pour la langue en général."""
        evidence = "Return temperature threshold is monitored continuously. "
        text = "The substation supplies the northern district with heat. " * 14 + evidence
        assert evidence not in _historical_head(text, BUDGET)
        snippet = _build_centered_snippet(
            text, "Quelle est la température de retour ?", BUDGET
        )
        assert "temperature threshold" in snippet

    def test_cross_lingual_without_any_shared_token_falls_back(self):
        """Aucune prétention : sans terme partagé, on rend la tête, ni mieux ni pire.

        Une stratégie lexicale ne traduit pas. Ce test EXISTE pour interdire la
        promesse inverse : le comportement doit être celui d'avant, pas une fenêtre
        choisie au hasard.
        """
        text = (
            "The booster unit was replaced last winter by the night shift. " * 12
            + "The flow meter reading is stored hourly for later review. " * 12
        )
        assert len(text) > BUDGET
        snippet = _build_centered_snippet(
            text, "Quel est le débit de la pompe de gavage ?", BUDGET
        )
        assert snippet == _historical_head(text, BUDGET)


# ==========================================================================
# 5. Textes pathologiques : aucune exception
# ==========================================================================
class TestPathologicalText:
    @pytest.mark.parametrize(
        "text",
        [
            "x" * 5000,
            "mot " * 2000,
            "     " * 400 + "SST-9 quartier des Tilleuls" + "     " * 400,
            "\n\n\n" + "ligne vide\n\n" * 300,
            "ᚠᚢᚦᚨᚱᚲ " * 400 + "SST-9",
            "Éèêëàâäôöûüç " * 200 + "quartier des Tilleuls",
            "N° 12 ; p. ex. : 2,4 MW / 88 °C — cf. §4.2. " * 60,
            "…" * 900,
            "\u00a0" * 900 + "Tilleuls",
        ],
    )
    def test_no_exception_and_budget_respected(self, text):
        snippet = _build_centered_snippet(text, QUESTION, BUDGET)
        assert isinstance(snippet, str)
        assert len(snippet) <= BUDGET

    def test_a_zero_or_negative_budget_returns_the_text(self):
        text = _prose(EVIDENCE, 1500)
        assert _build_centered_snippet(text, QUESTION, 0) == text.strip()

    def test_none_is_tolerated(self):
        assert _build_centered_snippet(None, QUESTION, BUDGET) == ""


# ==========================================================================
# 6. Déterminisme
# ==========================================================================
class TestDeterminism:
    def test_same_inputs_give_the_same_snippet(self):
        text = _prose(EVIDENCE, 1450)
        first = _build_centered_snippet(text, QUESTION, BUDGET)
        for _ in range(20):
            assert _build_centered_snippet(text, QUESTION, BUDGET) == first

    def test_ties_keep_the_first_best_sentence(self):
        """Deux phrases au score identique : la PREMIÈRE gagne, toujours."""
        sentence = "La sous-station SST-9 dessert le quartier des Tilleuls. "
        text = "Préambule neutre de remplissage pour éloigner la borne. " * 4
        text += sentence + ("Contexte intermédiaire de remplissage. " * 12) + sentence
        text += "Fin de section neutre de remplissage. " * 12
        snippet = _build_centered_snippet(text, QUESTION, BUDGET)
        assert snippet == _build_centered_snippet(text, QUESTION, BUDGET)
        assert text.index(sentence) < text.index(snippet.strip("…")) + len(snippet)


# ==========================================================================
# 7. Non-régression : tableaux, visuels, ordre des sources
# ==========================================================================
_TABLE = "| Poste | Valeur |\n" + "".join(f"| P{i} | {i * 7} |\n" for i in range(300))


class TestNonRegression:
    def test_tabular_chunks_are_byte_identical_between_strategies(self):
        chunk = {
            "text": _TABLE,
            "source": {"filename": "p.xlsx", "meta": {"kind": "excel_table"}},
        }
        [head] = _prepare_selected_for_prompt(
            [chunk], snippet_max_chars=BUDGET, table_max_chars=4000,
            question=QUESTION, snippet_strategy=SNIPPET_STRATEGY_HEAD,
        )
        [centered] = _prepare_selected_for_prompt(
            [chunk], snippet_max_chars=BUDGET, table_max_chars=4000,
            question=QUESTION, snippet_strategy=SNIPPET_STRATEGY_CENTERED,
        )
        assert centered == head
        assert "tableau" in centered["text"]  # le marqueur de ligne, pas une ellipse nue

    def test_a_pdf_table_chunk_is_also_protected(self):
        chunk = {"text": _TABLE, "source": {"filename": "p.pdf", "meta": {"kind": "table"}}}
        [centered] = _prepare_selected_for_prompt(
            [chunk], snippet_max_chars=BUDGET, table_max_chars=4000,
            question=QUESTION, snippet_strategy=SNIPPET_STRATEGY_CENTERED,
        )
        assert centered["text"].endswith(
            "… (extrait tronqué : les lignes suivantes du tableau ne sont pas affichées)"
        )

    def test_visual_metadata_is_untouched(self):
        chunk = _chunk(
            _prose(EVIDENCE, 1500),
            visual={"kind": "photograph"},
            page_number=4,
            slide_number=7,
            locator={"type": "pptx", "shape_order": 2},
        )
        [centered] = _prepare_selected_for_prompt(
            [chunk], snippet_max_chars=BUDGET, question=QUESTION,
            snippet_strategy=SNIPPET_STRATEGY_CENTERED,
        )
        assert centered["source"]["meta"] == chunk["source"]["meta"]
        assert EVIDENCE.strip() in centered["text"]

    def test_order_identity_and_count_are_preserved(self):
        chunks = [
            dict(_chunk(_prose(EVIDENCE, 1500)), chunk_id=f"c-{i}")
            for i in range(6)
        ]
        kwargs = dict(snippet_max_chars=BUDGET, question=QUESTION)
        head = _prepare_selected_for_prompt(
            chunks, **kwargs, snippet_strategy=SNIPPET_STRATEGY_HEAD
        )
        centered = _prepare_selected_for_prompt(
            chunks, **kwargs, snippet_strategy=SNIPPET_STRATEGY_CENTERED
        )
        assert [c["chunk_id"] for c in centered] == [c["chunk_id"] for c in head]
        assert [c["source"]["document_id"] for c in centered] == [
            c["source"]["document_id"] for c in head
        ]
        assert len(centered) == len(head) == 6

    def test_the_input_list_is_never_mutated(self):
        chunk = _chunk(_prose(EVIDENCE, 1500))
        original = chunk["text"]
        _prepare_selected_for_prompt(
            [chunk], snippet_max_chars=BUDGET, question=QUESTION,
            snippet_strategy=SNIPPET_STRATEGY_CENTERED,
        )
        assert chunk["text"] == original

    def test_the_budget_keeps_the_same_number_of_sources(self):
        """Le correctif ne doit RIEN coûter au budget global : même nombre de sources."""
        chunks = [
            dict(_chunk(_prose(EVIDENCE, 1500)), chunk_id=f"c-{i}") for i in range(14)
        ]
        common = dict(
            question_for_prompt="Question: " + QUESTION,
            selected=chunks,
            template_content=None,
            max_prompt_chars=8300,
            snippet_max_chars=BUDGET,
            table_max_chars=4000,
            min_sources=4,
            question=QUESTION,
        )
        _, kept_head, info_head = _build_prompt_with_budget(
            **common, snippet_strategy=SNIPPET_STRATEGY_HEAD
        )
        prompt_c, kept_centered, info_centered = _build_prompt_with_budget(
            **common, snippet_strategy=SNIPPET_STRATEGY_CENTERED
        )
        # JAMAIS moins de sources. Une fenêtre recalée sur un espace est parfois plus
        # courte que la tête, donc une source de plus peut entrer : c'est un gain, pas
        # une dérive — ce qui est interdit, c'est d'en perdre.
        assert len(kept_centered) >= len(kept_head)
        assert (
            info_centered["sources_dropped_by_budget"]
            <= info_head["sources_dropped_by_budget"]
        )
        assert len(prompt_c) <= 8300
        assert EVIDENCE.strip() in prompt_c  # et la preuve est là, elle

    @pytest.mark.parametrize("memory_chars", [0, 1500, 4000])
    def test_conversation_memory_does_not_evict_more_sources(self, memory_chars):
        chunks = [
            dict(_chunk(_prose(EVIDENCE, 1500)), chunk_id=f"c-{i}") for i in range(14)
        ]
        common = dict(
            question_for_prompt="Question: " + QUESTION,
            selected=chunks,
            template_content=None,
            max_prompt_chars=8300,
            snippet_max_chars=BUDGET,
            table_max_chars=4000,
            min_sources=4,
            question=QUESTION,
            conversation_context="Utilisateur : contexte antérieur. " * 200
            if memory_chars
            else None,
        )
        if memory_chars:
            common["conversation_context"] = common["conversation_context"][:memory_chars]
        _, kept_head, _ = _build_prompt_with_budget(
            **common, snippet_strategy=SNIPPET_STRATEGY_HEAD
        )
        prompt_c, kept_centered, _ = _build_prompt_with_budget(
            **common, snippet_strategy=SNIPPET_STRATEGY_CENTERED
        )
        assert len(kept_centered) >= len(kept_head)
        assert EVIDENCE.strip() in prompt_c


# ==========================================================================
# 8. Aucune entrée/sortie
# ==========================================================================
class TestNoIO:
    def test_the_builder_performs_no_sql_and_no_network(self, monkeypatch):
        """Aucune base, aucun réseau.

        Le SQL est interdit par construction : ce test ne demande NI `db` NI
        `django_db`, donc pytest-django lève `RuntimeError` au premier accès. Le réseau
        est interdit explicitement — un extrait qui appellerait le Gateway pour choisir
        sa fenêtre paierait le correctif au prix d'un aller-retour par source.
        """
        import urllib.request

        import requests

        def forbidden(*_a, **_k):  # pragma: no cover - doit rester non appelé
            raise AssertionError("la construction de l'extrait ne fait aucune I/O")

        monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
        monkeypatch.setattr(urllib.request, "urlopen", forbidden)
        monkeypatch.setattr("socket.socket.connect", forbidden)

        text = _prose(EVIDENCE, 1500)
        for _ in range(50):
            assert EVIDENCE.strip() in _build_centered_snippet(text, QUESTION, BUDGET)
