"""Le budget de prompt ne peut plus vider le prompt de ses sources.

Comportement INTERDIT, reproduit sur un scénario synthétique :

    candidats retenus > 0
    sources dans le prompt = 0
    answer_kind != no_answer
    aucune erreur explicite

Le modèle recevait alors les consignes et la question, sans aucune matière, et aucune
citation n'était possible. La cause n'était pas le retrieval : la boucle de réduction
s'arrêtait quand la liste de sources était VIDE, pas quand elle était à un élément.

Ce fichier verrouille l'invariant inverse — un candidat retenu implique au moins une
source dans le prompt — et la garde qui empêche la récidive : le coût FIXE des consignes
est borné, parce qu'il se paie sur les sources.
"""
from __future__ import annotations

import pytest
from django.conf import settings
from django.test import override_settings

from apps.rag_core.domain.retrieval import FORMAT_RULES_EN, FORMAT_RULES_FR, build_prompt
from apps.rag_core.services.query.prompt_builder import (
    _build_prompt_with_budget,
    _min_final_source_chars,
)

QUESTION = "Quelles sont les clauses principales ?"


def _chunk(i: int, size: int, *, tabular: bool = False) -> dict:
    src = {"document_id": f"d{i}", "filename": f"f{i}.pdf", "page_number": 1,
           "label": f"[{i}] f{i}.pdf p.1"}
    if tabular:
        src["meta"] = {"kind": "table", "table_type": "synthese"}
        text = "\n".join("| colonne A | colonne B |" for _ in range(size // 26 + 1))
    else:
        text = "x" * size
    return {"chunk_id": f"c{i}", "text": text, "score": 1.0 - i / 100, "source": src}


def _run(selected, *, budget: int, snippet: int = 800, table: int = 4000):
    return _build_prompt_with_budget(
        question_for_prompt=QUESTION,
        selected=selected,
        template_content=None,
        max_prompt_chars=budget,
        snippet_max_chars=snippet,
        table_max_chars=table,
    )


def _overhead() -> int:
    return len(build_prompt(QUESTION, [], template_content=None, answer_language=None))


# ===========================================================================
# 1-6. L'invariant
# ===========================================================================
def test_no_candidate_yields_no_source():
    """Aucun candidat : zéro source est la seule réponse honnête."""
    prompt, kept, info = _run([], budget=8300)
    assert kept == []
    assert info["sources_before_budget"] == 0
    assert prompt, "le prompt existe même sans source (consignes + question)"


def test_short_candidate_is_kept_untouched():
    prompt, kept, info = _run([_chunk(1, 200)], budget=8300)
    assert len(kept) == 1
    assert info["final_source_truncated"] is False
    assert info["sources_dropped_by_budget"] == 0
    assert "x" * 200 in prompt


def test_prompt_under_budget_is_unchanged():
    """Sous le budget : même prompt, mêmes sources, même ordre — aucune régression."""
    selected = [_chunk(i, 300) for i in range(1, 5)]
    prompt, kept, info = _run(selected, budget=8300)
    assert [c["chunk_id"] for c in kept] == ["c1", "c2", "c3", "c4"]
    assert info["prompt_budget_exhausted"] is False
    assert info["final_source_truncated"] is False
    assert len(prompt) <= 8300


def test_single_oversized_candidate_is_truncated_not_dropped():
    """Le cas mesuré : une seule source, trop grosse. Elle est TRONQUÉE, pas supprimée."""
    prompt, kept, info = _run([_chunk(1, 6000)], budget=2500, snippet=6000)
    assert len(kept) == 1, "la dernière source ne doit jamais disparaître"
    assert info["final_source_truncated"] is True
    assert info["final_source_original_chars"] == 6000
    assert 0 < info["final_source_retained_chars"] < 6000
    assert len(prompt) <= 2500


def test_lowest_priority_sources_are_dropped_first():
    selected = [_chunk(i, 800) for i in range(1, 9)]
    _prompt, kept, info = _run(selected, budget=3000)
    assert [c["chunk_id"] for c in kept] == ["c1", "c2"][: len(kept)]
    assert info["sources_dropped_by_budget"] == 8 - len(kept)
    assert len(kept) >= 1


def test_the_loop_never_goes_from_one_to_zero():
    """Quel que soit le budget, une source retenue reste une source retenue."""
    for budget in (100, 500, 1000, 1500, 2000):
        _prompt, kept, _info = _run([_chunk(1, 4000)], budget=budget, snippet=4000)
        assert len(kept) == 1, f"budget={budget} a vidé le prompt de sa source"


# ===========================================================================
# 7-9. Ce que la troncature préserve
# ===========================================================================
def test_truncated_source_keeps_its_label_and_identity():
    prompt, kept, _info = _run([_chunk(1, 6000)], budget=2500, snippet=6000)
    assert kept[0]["chunk_id"] == "c1"
    assert kept[0]["source"]["document_id"] == "d1"
    assert kept[0]["source"]["label"] == "[1] f1.pdf p.1"
    assert "[1] f1.pdf" in prompt, "la source reste citable"


def test_truncated_source_keeps_a_non_empty_excerpt():
    _prompt, kept, info = _run([_chunk(1, 6000)], budget=2500, snippet=6000)
    assert kept[0]["text"].strip(), "une source sans texte est une citation sans preuve"
    assert info["final_source_retained_chars"] >= 1


def test_tabular_source_is_truncated_on_a_line_boundary():
    """Un tableau coupé au caractère produit une demi-ligne, lue comme une donnée."""
    _prompt, kept, _info = _run([_chunk(1, 6000, tabular=True)], budget=2600, snippet=6000)
    assert len(kept) == 1
    text = kept[0]["text"]
    assert "extrait tronqué" in text
    assert not text.endswith("| colonne"), "aucune ligne coupée en son milieu"


def test_prompt_never_exceeds_budget_when_a_minimal_excerpt_fits():
    _prompt, _kept, info = _run([_chunk(1, 6000)], budget=2500, snippet=6000)
    assert info["prompt_budget_unable_to_fit_minimum_source"] is False
    assert len(_prompt) <= 2500


def test_source_survives_even_when_the_minimum_excerpt_does_not_fit():
    """Budget absurde : on garde la source ET on le DIT. Jamais de prompt muet."""
    prompt, kept, info = _run([_chunk(1, 6000)], budget=900, snippet=6000)
    assert len(kept) == 1
    assert info["prompt_budget_unable_to_fit_minimum_source"] is True
    assert kept[0]["text"].strip()
    assert len(prompt) > 900, "le dépassement est assumé, pas masqué"


# ===========================================================================
# 10-12. La comptabilité du budget
# ===========================================================================
def test_overhead_is_measured_on_the_real_prompt():
    _prompt, _kept, info = _run([_chunk(1, 300)], budget=8300)
    assert info["prompt_overhead_chars"] == _overhead()
    assert info["prompt_overhead_chars"] > 0


def test_source_budget_is_the_remainder():
    _prompt, _kept, info = _run([_chunk(1, 300)], budget=8300)
    assert info["source_budget_chars"] == 8300 - info["prompt_overhead_chars"]
    assert info["selected_source_chars"] > 0


def test_accounting_reports_drops_and_truncation():
    selected = [_chunk(i, 800) for i in range(1, 9)]
    _prompt, kept, info = _run(selected, budget=3000)
    assert info["sources_before_budget"] == 8
    assert info["sources_after_budget"] == len(kept)
    assert info["sources_dropped_by_budget"] == 8 - len(kept)
    assert info["prompt_budget_exhausted"] is True


def test_accounting_contains_no_text():
    _prompt, _kept, info = _run([_chunk(1, 6000)], budget=2500, snippet=6000)
    for value in info.values():
        assert not isinstance(value, str), f"la comptabilité ne porte que des nombres : {value!r}"


# ===========================================================================
# Garde : le coût fixe des consignes est BORNÉ
# ===========================================================================
#: Part maximale du budget que le gabarit fixe peut consommer. Au-delà, chaque
#: élargissement de consigne se paie en sources sans que personne ne le voie — c'est
#: exactement ce qui s'est produit avec un bloc de format passé de 115 à 688 caractères.
MAX_OVERHEAD_RATIO = 0.18
#: Seuil d'alerte : au-delà, la marge devient trop faible pour absorber une question
#: longue ou un libellé de source inhabituel.
WARN_OVERHEAD_RATIO = 0.15


def test_fixed_template_stays_within_its_share_of_the_budget():
    ratio = _overhead() / int(settings.RAG_MAX_PROMPT_CHARS)
    assert ratio <= MAX_OVERHEAD_RATIO, (
        f"le gabarit fixe consomme {ratio:.1%} du budget (limite {MAX_OVERHEAD_RATIO:.0%}) : "
        "toute consigne ajoutée se paie désormais en sources"
    )


def test_fixed_template_warning_threshold_is_documented():
    """Sous 15 %, la marge absorbe une question longue ; au-delà, elle ne le fait plus."""
    ratio = _overhead() / int(settings.RAG_MAX_PROMPT_CHARS)
    if ratio > WARN_OVERHEAD_RATIO:
        pytest.fail(
            f"gabarit à {ratio:.1%} du budget : au-delà de {WARN_OVERHEAD_RATIO:.0%} la marge "
            "ne suffit plus à absorber une question longue — condenser ou relever le budget"
        )


def test_format_block_stays_condensed():
    """Le bloc de format est la partie variable du gabarit : c'est elle qu'on surveille."""
    assert len(FORMAT_RULES_FR) <= 500, f"bloc FR à {len(FORMAT_RULES_FR)} c"
    assert len(FORMAT_RULES_EN) <= 500, f"bloc EN à {len(FORMAT_RULES_EN)} c"


@pytest.mark.parametrize("rules", [FORMAT_RULES_FR, FORMAT_RULES_EN])
def test_condensation_kept_the_anti_hallucination_guards(rules):
    """Condenser ne doit jamais retirer une garde qui n'existe nulle part ailleurs."""
    lowered = rules.lower()
    assert ("ne complète jamais" in lowered) or ("never fill gaps" in lowered)
    assert ("ce qui manque" in lowered) or ("what is missing" in lowered)


# ===========================================================================
# Les deux cas mesurés en production
# ===========================================================================
def test_single_large_source_survives_every_budget():
    """Le motif des cas mesurés en production (`IN01`, `IN03`).

    Une seule source, volumineuse, et un budget saturé par le gabarit : le prompt tombait
    à 1415 caractères SANS aucune source. On vérifie ici le motif, pas des tailles
    d'octets relevées ailleurs — la taille réellement occupée par une source dépend de
    son type (un extrait tabulaire a son propre plafond) et des règles conditionnelles
    que sa présence déclenche. La reproduction fidèle de ces deux questions appartient à
    la requalification en environnement, pas à un test unitaire.
    """
    for budget in (2000, 3000, 5000, 7000, 8300):
        prompt, kept, info = _run([_chunk(1, 6000)], budget=budget, snippet=6000)
        assert len(kept) == 1, f"budget={budget} : la source a disparu"
        assert kept[0]["text"].strip(), f"budget={budget} : extrait vide"
        assert info["sources_dropped_by_budget"] == 0
        if info["final_source_truncated"]:
            assert info["final_source_retained_chars"] >= 1
            assert info["final_source_retained_chars"] < info["final_source_original_chars"]


def test_minimum_final_source_chars_is_configurable_and_sane():
    assert _min_final_source_chars() == int(settings.RAG_MIN_FINAL_SOURCE_CHARS)
    with override_settings(RAG_MIN_FINAL_SOURCE_CHARS=250):
        assert _min_final_source_chars() == 250
