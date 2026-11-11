"""Lot 4 v2 — indépendance du découpage et déduplication.

La v1 découpait par empreinte de `case_id`. Deux questions dérivées de deux chunks
du MÊME document pouvaient donc tomber de part et d'autre : le jeu de test contenait
des documents déjà vus, et sa mesure était optimiste.

Une fuite ne se voit pas dans les métriques — elle les améliore. D'où ces tests.
"""
from __future__ import annotations

import pytest

from apps.rag_core.benchmark.admission_dataset import (
    NEAR_DUPLICATE_JACCARD,
    assign_splits,
    deduplicate,
    group_key,
    jaccard,
    normalize_question,
    split_leakage_report,
)


def pos(cid: str, doc: str, q: str = "question") -> dict:
    return {"case_id": cid, "label": "positive", "expected_document_id": doc, "question": q}


def neg(cid: str, q: str) -> dict:
    return {"case_id": cid, "label": "negative", "expected_document_id": None, "question": q}


class TestNormalization:
    def test_accents_case_and_punctuation_are_folded(self):
        assert normalize_question("Quels SONT les chiffres ?") == \
               normalize_question("quels sont les chiffres")

    def test_distinct_questions_stay_distinct(self):
        assert normalize_question("le budget de la ville") != \
               normalize_question("le budget du logement")


class TestDeduplication:
    def test_exact_duplicates_are_removed(self):
        kept, dropped = deduplicate([
            neg("a", "Quel est le budget ?"),
            neg("b", "quel est le budget"),
        ])
        assert [c["case_id"] for c in kept] == ["a"]
        assert dropped[0]["dropped_reason"] == "exact_duplicate"
        assert dropped[0]["duplicate_of"] == "a"

    def test_near_duplicates_are_removed_with_their_score(self):
        kept, dropped = deduplicate([
            neg("a", "Quelles sont les principales conclusions du rapport annuel ?"),
            neg("b", "Quelles sont les principales conclusions de ce rapport annuel ?"),
        ])
        assert [c["case_id"] for c in kept] == ["a"]
        assert dropped[0]["dropped_reason"] == "near_duplicate"
        assert dropped[0]["jaccard"] >= NEAR_DUPLICATE_JACCARD

    def test_different_questions_are_kept(self):
        kept, dropped = deduplicate([
            neg("a", "Quel est le budget de la rénovation urbaine ?"),
            neg("b", "Comment le logement social est-il financé en Europe ?"),
        ])
        assert len(kept) == 2 and not dropped

    def test_first_occurrence_is_the_one_kept(self):
        kept, _ = deduplicate([neg("first", "le budget"), neg("second", "le budget")])
        assert kept[0]["case_id"] == "first"

    def test_jaccard_is_symmetric_and_bounded(self):
        a, b = "le budget de la ville", "le budget du quartier"
        assert 0.0 <= jaccard(a, b) <= 1.0
        assert jaccard(a, b) == pytest.approx(jaccard(b, a))


class TestGroupKey:
    def test_positives_group_by_document(self):
        assert group_key(pos("c1", "doc-A")) == group_key(pos("c2", "doc-A"))
        assert group_key(pos("c1", "doc-A")) != group_key(pos("c3", "doc-B"))

    def test_negatives_are_their_own_group(self):
        assert group_key(neg("n1", "q")) != group_key(neg("n2", "q"))


class TestSplitIndependence:
    def test_all_cases_of_a_document_land_in_the_same_split(self):
        """L'invariant central : un document ne peut pas être des deux côtés."""
        cases = [pos(f"c{i}", f"doc-{i // 4}", f"question numero {i}") for i in range(40)]
        splits = assign_splits(cases)
        for c in cases:
            c["split"] = splits[c["case_id"]]
        by_doc: dict[str, set[str]] = {}
        for c in cases:
            by_doc.setdefault(c["expected_document_id"], set()).add(c["split"])
        assert all(len(v) == 1 for v in by_doc.values())

    def test_leakage_report_confirms_independence(self):
        cases = [pos(f"c{i}", f"doc-{i // 3}", f"question {i}") for i in range(30)]
        splits = assign_splits(cases)
        for c in cases:
            c["split"] = splits[c["case_id"]]
        rep = split_leakage_report(cases)
        assert rep["independent"] is True
        assert rep["documents_in_both_splits"] == []
        assert rep["groups_straddling_splits"] == []

    def test_leakage_report_detects_a_straddling_document(self):
        """Le rapport doit ATTRAPER une fuite, pas seulement en constater l'absence."""
        cases = [pos("c1", "doc-A"), pos("c2", "doc-A")]
        cases[0]["split"] = "calibration"
        cases[1]["split"] = "holdout"
        rep = split_leakage_report(cases)
        assert rep["independent"] is False
        assert "doc-A" in rep["documents_in_both_splits"]

    def test_split_is_deterministic(self):
        cases = [pos(f"c{i}", f"doc-{i // 3}", f"q{i}") for i in range(30)]
        assert assign_splits(cases) == assign_splits(list(reversed(cases)))

    def test_both_splits_are_populated(self):
        cases = [pos(f"c{i}", f"doc-{i // 2}", f"q{i}") for i in range(40)]
        splits = assign_splits(cases)
        values = set(splits.values())
        assert values == {"calibration", "holdout"}

    def test_a_group_is_never_cut_to_reach_the_ratio(self):
        """L'indépendance prime sur l'équilibre : mieux vaut 55/45 qu'un groupe coupé."""
        cases = [pos(f"c{i}", "doc-unique", f"q{i}") for i in range(20)]
        splits = assign_splits(cases)
        assert len(set(splits.values())) == 1


# --- politique de difficulté par catégorie (module pur) ------------------------

from apps.rag_core.benchmark.admission_dataset import (  # noqa: E402
    NEGATIVE_CATEGORIES,
    NEGATIVE_CATEGORY_POLICIES,
    NEGATIVE_CATEGORY_SPECS,
    POLICY_ABSENCE_REVIEW,
    POLICY_MIN_SCORE,
    SCORE_EXEMPT_CATEGORIES,
    SCORE_FILTERED_CATEGORIES,
    category_policy,
    is_score_exempt,
    score_stats,
)


class TestCategoryPolicies:
    def test_every_category_declares_a_known_policy(self):
        """Ajouter une catégorie doit OBLIGER à choisir sa politique."""
        for name, (_just, policy) in NEGATIVE_CATEGORY_SPECS.items():
            assert policy in NEGATIVE_CATEGORY_POLICIES, name

    def test_out_of_corpus_is_the_only_exempt_category(self):
        assert SCORE_EXEMPT_CATEGORIES == ("out_of_corpus",)
        assert is_score_exempt("out_of_corpus") is True
        assert category_policy("out_of_corpus") == POLICY_ABSENCE_REVIEW

    def test_the_seven_others_require_a_minimum_score(self):
        assert len(SCORE_FILTERED_CATEGORIES) == 7
        for cat in SCORE_FILTERED_CATEGORIES:
            assert category_policy(cat) == POLICY_MIN_SCORE
            assert is_score_exempt(cat) is False

    def test_the_two_families_partition_the_categories(self):
        assert set(SCORE_FILTERED_CATEGORIES) | set(SCORE_EXEMPT_CATEGORIES) \
               == set(NEGATIVE_CATEGORIES)
        assert not set(SCORE_FILTERED_CATEGORIES) & set(SCORE_EXEMPT_CATEGORIES)

    def test_an_unknown_category_is_refused(self):
        with pytest.raises(ValueError, match="catégorie de négatif inconnue"):
            category_policy("inventee")

    def test_the_historical_mapping_is_preserved(self):
        """L'en-tête des datasets sérialise `{nom: justification}` — format conservé."""
        assert set(NEGATIVE_CATEGORIES) == set(NEGATIVE_CATEGORY_SPECS)
        assert all(isinstance(v, str) and v for v in NEGATIVE_CATEGORIES.values())


class TestScoreStats:
    def test_an_empty_series_has_no_statistics(self):
        assert score_stats([]) == {"count": 0, "min": None, "median": None,
                                   "p95": None, "max": None}

    def test_odd_length_takes_the_middle_value(self):
        assert score_stats([0.1, 0.5, 0.9])["median"] == 0.5

    def test_even_length_averages_the_two_middles(self):
        assert score_stats([0.80, 0.82, 0.86, 0.90])["median"] == pytest.approx(0.84)

    def test_p95_is_an_observed_value_never_interpolated(self):
        s = [i / 100 for i in range(1, 101)]
        assert score_stats(s)["p95"] in s

    def test_order_does_not_matter(self):
        assert score_stats([0.9, 0.1, 0.5]) == score_stats([0.1, 0.5, 0.9])
