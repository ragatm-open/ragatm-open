"""Runner sur cas v2 : recall sensible à match_policy (any/all), MRR, sans service externe.

Le runner consomme l'objet interne résolu (``BenchmarkCase`` v2 : expected_chunk_ids =
UUID courants). La façade retrieval est mockée : aucun appel externe.
"""
from __future__ import annotations

from apps.rag_core.benchmark import metrics
from apps.rag_core.benchmark.dataset import BenchmarkCase
from apps.rag_core.benchmark.runner import run_case


def _case(expected_chunks, match_policy="any"):
    return BenchmarkCase(
        id="xlsx-1", rag_space="s", question="q?", should_answer=True,
        expected_documents=(), expected_chunk_ids=tuple(expected_chunks),
        format="xlsx", source_schema_version=2, match_policy=match_policy,
    )


def _query_returning(chunk_ids):
    def qf(space_id, question):
        return {"status": "ok", "sources": [{"chunk_id": c, "score": 0.9 - i * 0.1}
                                             for i, c in enumerate(chunk_ids)]}
    return qf


def _run(case, retrieved_chunk_ids, ks=(1, 3, 5)):
    return run_case(case, query_fn=_query_returning(retrieved_chunk_ids),
                    space_resolver=lambda name: "space-id", ks=ks)


# --- metric unit ---------------------------------------------------------- #
def test_recall_policy_any_vs_all():
    exp = ["c1", "c2"]
    ret = ["c1", "x", "y"]  # un seul des deux trouvés
    assert metrics.recall_at_k_policy(ret, exp, 3, "any") == 1.0
    assert metrics.recall_at_k_policy(ret, exp, 3, "all") == 0.5
    assert metrics.recall_at_k_policy(["z"], exp, 3, "any") == 0.0


# --- runner --------------------------------------------------------------- #
def test_runner_any_single_chunk_top1():
    r = _run(_case(["c1"], "any"), ["c1", "c2", "c3"])
    assert r.recall[1] == 1.0 and r.recall[3] == 1.0
    assert r.reciprocal_rank == 1.0 and r.first_relevant_rank == 1


def test_runner_any_multi_chunk_one_present():
    # Région découpée en 2 chunks équivalents ; un seul remonté -> any => 1.0.
    r = _run(_case(["c1", "c2"], "any"), ["x", "c2", "y"])
    assert r.recall[3] == 1.0
    assert r.recall[1] == 0.0            # c2 est au rang 2, pas dans top-1
    assert r.first_relevant_rank == 2


def test_runner_all_requires_every_chunk():
    r = _run(_case(["c1", "c2"], "all"), ["c1", "z", "w"])
    assert r.recall[3] == 0.5           # 1 des 2
    r2 = _run(_case(["c1", "c2"], "all"), ["c1", "c2", "w"])
    assert r2.recall[3] == 1.0          # les 2 présents


def test_runner_miss():
    r = _run(_case(["c1"], "any"), ["a", "b", "c"])
    assert r.recall[3] == 0.0 and r.reciprocal_rank == 0.0 and r.first_relevant_rank is None
