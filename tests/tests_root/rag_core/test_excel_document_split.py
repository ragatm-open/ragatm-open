"""Split STRICTEMENT documentaire (par content_sha256) + contrôles de fuite.
Fixtures synthétiques, aucun DB."""
from __future__ import annotations

import subprocess
import sys
import textwrap

from apps.rag_core.benchmark import split_validation as sv


def _case(sha, *, sheet="S", rng="A2:B2", case_id="xlsx-1", question="q?",
          fact_group="fg", difficulty="easy", evidence="e" * 64):
    return {
        "case_id": case_id, "question": question, "difficulty": difficulty,
        "fact_group_id": fact_group,
        "ground_truth": {
            "document_ref": {"content_sha256": sha},
            "locator": {"sheet_name": sheet, "cell_range": rng},
            "evidence": {"region_text_sha256": evidence},
        },
    }


def _docset(n_docs, per_doc=2):
    cases = []
    for d in range(n_docs):
        for k in range(per_doc):
            cases.append(_case(f"{d:064d}", sheet=f"S{d}", rng=f"A{2+k}:B{2+k}",
                               case_id=f"xlsx-{d}-{k}", question=f"q{d}-{k}",
                               fact_group=f"fg-{d}-{k}", evidence=f"{d}{k}".ljust(64, "0")))
    return cases


# --------------------------------------------------------------------------- #
# Split : un document jamais partagé, déterministe
# --------------------------------------------------------------------------- #
def test_split_document_never_shared():
    cal, hold = sv.split_by_document(_docset(6), calibration_ratio=0.70, seed=1789)
    cal_docs = {c["ground_truth"]["document_ref"]["content_sha256"] for c in cal}
    hold_docs = {c["ground_truth"]["document_ref"]["content_sha256"] for c in hold}
    assert cal_docs and hold_docs
    assert cal_docs.isdisjoint(hold_docs)               # 0 document partagé
    # Toutes les lignes d'un classeur du même côté.
    assert sv.find_leaks(cal, hold) == {}


def test_split_deterministic_same_seed():
    a = sv.split_by_document(_docset(6), seed=1789)
    b = sv.split_by_document(list(reversed(_docset(6))), seed=1789)   # ordre d'entrée différent
    assert [c["case_id"] for c in a[0]] == [c["case_id"] for c in b[0]]
    assert [c["case_id"] for c in a[1]] == [c["case_id"] for c in b[1]]


def test_split_independent_of_pythonhashseed():
    script = textwrap.dedent("""
        import os, django
        os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings.dev'); django.setup()
        from apps.rag_core.benchmark import split_validation as sv
        cases=[{'case_id':f'c{d}-{k}','question':f'q{d}','difficulty':'easy','fact_group_id':f'fg{d}{k}',
                'ground_truth':{'document_ref':{'content_sha256':f'{d:064d}'},
                'locator':{'sheet_name':f'S{d}','cell_range':f'A{2+k}:B{2+k}'},
                'evidence':{'region_text_sha256':f'{d}{k}'.ljust(64,'0')}}} for d in range(6) for k in range(2)]
        cal,hold=sv.split_by_document(cases, seed=1789)
        print(','.join(c['case_id'] for c in cal) + '|' + ','.join(c['case_id'] for c in hold))
    """)
    outs = []
    for hsd in ("0", "1", "random"):
        env = {**__import__("os").environ, "PYTHONHASHSEED": hsd}
        r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env)
        assert r.returncode == 0, r.stderr[-400:]
        outs.append(r.stdout.strip().splitlines()[-1])
    assert len(set(outs)) == 1, outs


# --------------------------------------------------------------------------- #
# Fuites : chaque type bloque
# --------------------------------------------------------------------------- #
def test_leak_document():
    shared = _case("d" * 64, case_id="a")
    leaks = sv.find_leaks([shared], [_case("d" * 64, case_id="b")])
    assert sv.LEAK_DOCUMENT in leaks


def test_leak_case_id():
    leaks = sv.find_leaks([_case("1" * 64, case_id="same")], [_case("2" * 64, case_id="same")])
    assert sv.LEAK_CASE_ID in leaks


def test_leak_question_and_factgroup_and_locator_and_evidence():
    a = _case("1" * 64, case_id="a", question="Même question", fact_group="fg",
              sheet="S", rng="A2:B2", evidence="ff" + "0" * 62)
    b = _case("2" * 64, case_id="b", question="Même question", fact_group="fg",
              sheet="S", rng="A2:B2", evidence="ff" + "0" * 62)
    leaks = sv.find_leaks([a], [b])
    assert sv.LEAK_QUESTION in leaks
    assert sv.LEAK_FACT_GROUP in leaks
    # locator/evidence partagent le doc_sha préfixe -> différent ici (docs distincts) donc PAS de fuite locator.
    assert sv.LEAK_LOCATOR not in leaks


# --------------------------------------------------------------------------- #
# Diversité / usabilité officielle
# --------------------------------------------------------------------------- #
def test_diversity_and_usability_low_volume():
    cal, hold = sv.split_by_document(_docset(3, per_doc=2), seed=1789)   # 6 cas, 3 docs
    div = sv.diversity(cal, hold)
    assert div.calibration_documents + div.holdout_documents == 3
    # Volume faible -> holdout probablement non officiellement exploitable.
    assert not sv.holdout_is_officially_usable(div)


def test_usability_sufficient_volume():
    cal, hold = sv.split_by_document(_docset(10, per_doc=3), seed=1789)  # 30 cas, 10 docs
    assert sv.holdout_is_officially_usable(sv.diversity(cal, hold))
