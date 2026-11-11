"""Business Corpus dans le retrieval du chat — spécification.

Ce fichier est né comme une CARACTÉRISATION du signalement production (« references vc »
/ Venture Capital) : un corpus métier associé, actif et indexé ne contribuait aucune
source, tandis qu'un document privé du workspace était cité seul.

Défaut D1, corrigé ici. Le leg lexical corpus n'exécutait que
``build_tsquery_plan(question)[0]``, sans repli. « **W**hat is venture capital ? »
produit ``plan[0] = what:*`` : zéro candidat corpus, sans erreur ni trace, tandis que le
leg privé retombait sur la requête large et répondait. La stratégie d'exécution du plan
est désormais PARTAGÉE (``rag_core.domain.fts_plan.execute_fts_plan``) : la divergence
n'est plus exprimable.

Défaut D2, corrigé derrière un flag. Les candidats corpus, injectés APRÈS
``merge_hybrid``, n'avaient pas de ``fusion_score``. La diversification classant sur
``(fusion_score, score)``, ils passaient derrière TOUT candidat privé, quelle que soit
leur pertinence. La fusion unifiée
(``RAG_BUSINESS_CORPUS_UNIFIED_FUSION_ENABLED``) les fait voter dans la même fusion de
rangs ; flag OFF, le comportement historique — défaut compris — reste testé tel quel.

Les scénarios qui ont démontré les défauts sont CONSERVÉS : ils vérifient désormais le
comportement attendu.

Jeu de données synthétique (aucune donnée réelle) :
  workspace  `vc-repro-workspace` / rag space `vc-repro-space`
  corpus     `VC Reference Corpus` (slug `vc-reference-corpus`)
  marqueurs  PRIVATE_WORKSPACE_MARKER_7F31 / BUSINESS_CORPUS_MARKER_9A42
"""
from __future__ import annotations

import json
import os

import pytest
from django.test import override_settings

from apps.corpus.models import (
    BusinessDomain,
    DomainCorpus,
    DomainCorpusChunk,
    DomainCorpusDocument,
    WorkspaceDomainCorpusAccess,
)
from apps.corpus.services.ingest import ingest_corpus_document_from_upload
from apps.corpus.services.retrieval import (
    allowed_corpus_ids_for_workspace,
    search_corpus_chunks_hybrid,
)
from apps.rag_core.domain.fts_plan import execute_fts_plan
from apps.rag_core.models import RagSpace
from apps.rag_core.repositories.chunk_repository import build_tsquery_plan, search_chunks_hybrid
from apps.rag_core.services.query import query_rag
from apps.workspaces.models import Membership, Workspace

pytestmark = pytest.mark.django_db

PRIVATE_MARKER = "PRIVATE_WORKSPACE_MARKER_7F31"
CORPUS_MARKER = "BUSINESS_CORPUS_MARKER_9A42"

#: Question telle qu'elle est saisie dans le chat (majuscule initiale, comme toute
#: phrase). C'est la forme du signalement production, et celle qui déclenchait D1.
QUESTION_CAPITALIZED = "What is venture capital?"
#: Même question sans majuscule : `plan[0]` est déjà la requête large. Sert à prouver
#: que les deux formes convergent maintenant.
QUESTION_LOWERCASE = "what is venture capital?"

DISCRIMINANT_QUESTIONS = [
    "What is the role of general partners and limited partners in venture capital?",
    "How do venture capital investors usually realise their return?",
    "What distinguishes venture capital from a conventional bank loan?",
]

UNIFIED_ON = override_settings(RAG_BUSINESS_CORPUS_UNIFIED_FUSION_ENABLED=True)
UNIFIED_OFF = override_settings(RAG_BUSINESS_CORPUS_UNIFIED_FUSION_ENABLED=False)

# --- Documents PRIVÉS du RagSpace : notes internes GÉNÉRIQUES ------------------
# Le privé mentionne le sujet EN PASSANT (une occurrence par paragraphe) : sa
# pertinence lexicale est délibérément FAIBLE devant celle du corpus.
_PRIVATE_PARAGRAPH = (
    "{marker} The internal note number {n} reviews the quarterly meeting minutes, "
    "the travel policy, the office relocation schedule, the supplier contract "
    "renewals and the annual training plan. It mentions capital once, in passing, "
    "without any definition. The remainder of the note concerns unrelated "
    "operational matters such as badge access, parking allocation, cafeteria "
    "opening hours, the printer maintenance window, the fire drill date, the "
    "archive room inventory, the stationery order, the visitor badge procedure and "
    "the annual furniture audit. "
)
PRIVATE_TEXT = "\n\n".join(
    _PRIVATE_PARAGRAPH.format(marker=PRIVATE_MARKER, n=i) for i in range(1, 16)
)

# Le workspace de production compte plusieurs documents privés, et le NOMBRE compte :
# `diversify_ranked_results` plafonne à RAG_RETRIEVAL_MAX_PER_GROUP (2) chunks par
# DOCUMENT en passe 1. Avec un seul document privé, la passe 1 ne peut retenir que
# 2 chunks privés. Dès que le pool privé couvre RAG_TOP_K / MAX_PER_GROUP = 4 documents
# distincts, la passe 1 est saturée par le privé. On en met 10, courts : c'est la
# situation banale d'un workspace réel, pas un cas limite construit.
PRIVATE_DOCUMENT_COUNT = 10

# --- Document du BUSINESS CORPUS : définition précise et distinctive ----------
# Aucun mot commençant par « what » ou « how » : c'est ce qui rendait D1 observable —
# plan[0] (`what:*` / `how:*`) ne peut pas matcher son contenu.
CORPUS_TEXT = (
    f"{CORPUS_MARKER}\n"
    "Venture capital is a form of equity capital provided to unlisted, privately "
    "held companies with high growth potential and a high risk of failure. "
    "The venture capital investor takes an equity participation in the company "
    "capital rather than lending money. Venture capital funds invest capital in "
    "unlisted companies; venture capital investors provide capital and take equity. "
    "A venture capital fund is structured around general partners, who manage the "
    "venture capital fund, and limited partners, who commit the capital and remain "
    "passive. Venture capital investors realise their return over an exit horizon "
    "of typically five to ten years, through a trade sale, a secondary sale or an "
    "initial public offering, not through interest payments. Venture capital is "
    "risk capital: venture capital investments may fail. Venture capital differs "
    "from a conventional bank loan because venture capital carries no collateral, "
    "no repayment schedule and no interest; venture capital participations remain "
    "illiquid until the venture capital exit horizon is reached."
).encode("utf-8")


# ---------------------------------------------------------------------------
# Instrumentation locale (test-only) — aucune modification du code produit
# ---------------------------------------------------------------------------
class Trace:
    """Collecte l'état du pipeline pour un appel `query_rag`."""

    def __init__(self) -> None:
        self.corpus_legs = None
        self.diversify_pool: list[dict] = []
        self.diversify_out: list[dict] = []
        self.diversify_called = False
        self.result: dict | None = None

    @staticmethod
    def provenance(c: dict) -> str:
        src = c.get("source") if isinstance(c.get("source"), dict) else {}
        return c.get("source_type") or src.get("source_type") or "unknown"

    @classmethod
    def _row(cls, c: dict) -> dict:
        src = c.get("source") if isinstance(c.get("source"), dict) else {}
        return {
            "chunk_id": str(c.get("chunk_id"))[:8],
            "provenance": cls.provenance(c),
            "doc": str(src.get("filename") or "")[:32],
            "score": round(float(c.get("score") or 0.0), 6),
            "fusion_score": round(float(c["fusion_score"]), 6) if "fusion_score" in c else None,
        }

    def rows(self, key: str) -> list[dict]:
        return [self._row(c) for c in getattr(self, key)]

    def counts(self, key: str) -> dict[str, int]:
        out = {"private": 0, "business_corpus": 0}
        for c in getattr(self, key):
            out["business_corpus" if self.provenance(c) == "domain_corpus" else "private"] += 1
        return out

    @property
    def corpus_candidates(self) -> list[dict]:
        return list(getattr(self.corpus_legs, "merged", []) or [])


@pytest.fixture
def trace(monkeypatch):
    tr = Trace()

    from apps.corpus.services import retrieval as corpus_retrieval
    from apps.rag_core.services.query import orchestrator

    real_corpus = corpus_retrieval.retrieve_corpus_candidate_legs
    real_div = orchestrator.diversify_ranked_results

    def spy_corpus(**kwargs):
        out = real_corpus(**kwargs)
        tr.corpus_legs = out
        return out

    def spy_div(results, **kwargs):
        tr.diversify_called = True
        tr.diversify_pool = list(results)
        out = real_div(results, **kwargs)
        tr.diversify_out = list(out)
        return out

    monkeypatch.setattr(corpus_retrieval, "retrieve_corpus_candidate_legs", spy_corpus)
    monkeypatch.setattr(orchestrator, "diversify_ranked_results", spy_div)
    return tr


# ---------------------------------------------------------------------------
# Jeu de données
# ---------------------------------------------------------------------------
@pytest.fixture
def vc_setup(db, ingest_text):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    user = User.objects.create_user(
        username="vc-repro", password="pw", email="vc-repro@example.test"
    )
    ws = Workspace.objects.create(name="vc-repro-workspace")
    Membership.objects.create(workspace=ws, user=user, role=Membership.ROLE_MEMBER)
    rs = RagSpace.objects.create(name="vc-repro-space", workspace=ws)

    domain = BusinessDomain.objects.create(slug="venture-capital", name="Venture Capital")
    corpus = DomainCorpus.objects.create(
        domain=domain, slug="vc-reference-corpus", name="VC Reference Corpus", is_active=True
    )
    ingest_corpus_document_from_upload(
        corpus=corpus,
        filename="vc-reference.txt",
        content_type="text/plain",
        file_bytes=CORPUS_TEXT,
    )
    return {
        "user": user,
        "workspace": ws,
        "rag_space": rs,
        "domain": domain,
        "corpus": corpus,
        "ingest_private": lambda: [
            ingest_text(
                rs,
                f"Private note {i}.\n\n{PRIVATE_TEXT}",
                filename=f"generic-vc-note-{i}.txt",
            )
            for i in range(1, PRIVATE_DOCUMENT_COUNT + 1)
        ],
    }


def _associate(vc, *, enabled=True):
    return WorkspaceDomainCorpusAccess.objects.create(
        workspace=vc["workspace"], corpus=vc["corpus"], enabled=enabled
    )


def _run(vc, tr, question):
    res = query_rag(
        rag_space_id=vc["rag_space"].id,
        rag_space=vc["rag_space"],
        user=vc["user"],
        question=question,
    )
    tr.result = res
    return res


def _source_types(res) -> list[str]:
    return [s.get("source_type") for s in (res.get("sources") or [])]


def _dump(label: str, vc, tr, res) -> None:
    """Trace lisible — données SYNTHÉTIQUES uniquement."""
    if not os.getenv("VC_REPRO_VERBOSE"):
        return
    payload = {
        "case": label,
        "workspace_id": vc["workspace"].id,
        "active_allowed_corpus_ids": allowed_corpus_ids_for_workspace(vc["workspace"].id),
        "eligible_corpus_documents": DomainCorpusDocument.objects.filter(
            corpus=vc["corpus"], status="READY"
        ).count(),
        "corpus_chunks": DomainCorpusChunk.objects.filter(corpus=vc["corpus"]).count(),
        "corpus_leg_candidate_count": len(tr.corpus_candidates),
        "diversify_called": tr.diversify_called,
        "pool_counts": tr.counts("diversify_pool"),
        "selected_counts": tr.counts("diversify_out"),
        "api_source_types": _source_types(res),
        "status": res.get("status"),
    }
    print("\n" + json.dumps(payload, indent=2, ensure_ascii=False))


# ===========================================================================
# D1 — le leg corpus applique le plan FTS complet, avec repli
# ===========================================================================
def test_d1_corpus_and_private_legs_share_the_same_plan_strategy(vc_setup, ingest_text):
    """Les deux legs exécutent le MÊME plan avec le MÊME repli."""
    vc = vc_setup
    ingest_text(vc["rag_space"], PRIVATE_TEXT, filename="generic-vc-note.txt")
    cid = str(vc["corpus"].id)

    plan = build_tsquery_plan(QUESTION_CAPITALIZED)
    assert plan[0] == "what:*", "plan[0] reste la requête ENTITÉ (mot capitalisé)"
    assert len(plan) > 1, "un repli large existe dans le plan"

    private_hits = search_chunks_hybrid(
        rag_space_id=vc["rag_space"].id, question=QUESTION_CAPITALIZED, top_k=30, min_text_rank=0.0
    )
    corpus_hits = search_corpus_chunks_hybrid(
        corpus_ids=[cid], question=QUESTION_CAPITALIZED, top_k=30, min_text_rank=0.0
    )
    # C'était ici l'asymétrie : privé non vide, corpus vide.
    assert private_hits
    assert corpus_hits
    assert CORPUS_MARKER in corpus_hits[0]["text"]


def test_d1_plan_index_reports_the_fallback(vc_setup):
    """La télémétrie dit QUELLE requête du plan a répondu — sans exposer la requête."""
    executions = []
    hits = search_corpus_chunks_hybrid(
        corpus_ids=[str(vc_setup["corpus"].id)],
        question=QUESTION_CAPITALIZED,
        top_k=30,
        min_text_rank=0.0,
        execution_sink=executions.append,
    )
    assert hits
    (execution,) = executions
    assert execution.plan_index == 1, "c'est le repli, pas la requête la plus sélective"
    assert execution.fallback_used is True
    assert execution.attempt_row_counts[0] == 0
    payload = execution.to_dict()
    # Jeu de clés VERROUILLÉ : aucun champ ne doit s'ajouter sans revue. Les cinq
    # dernières viennent de l'observabilité de la normalisation V2 — ce sont des
    # COMPTEURS et un mode, jamais des jetons.
    assert set(payload) == {
        "plan_size", "attempt_count", "plan_index", "attempt_row_counts",
        "truncated", "fallback_used",
        "query_mode", "raw_term_count", "normalized_term_count",
        "result_count", "duration_ms",
    }
    assert payload["query_mode"] == "v1"
    # Aucune tsquery, aucun texte : la télémétrie est faite de compteurs.
    assert "what" not in json.dumps(payload)


def test_d1_first_plan_wins_when_it_matches(vc_setup):
    """Le plan suivant n'est PAS exécuté quand le premier ramène des candidats."""
    executions = []
    # « Venture » est capitalisé ET présent dans le corpus : plan[0] doit suffire.
    hits = search_corpus_chunks_hybrid(
        corpus_ids=[str(vc_setup["corpus"].id)],
        question="Venture capital definition",
        top_k=30,
        min_text_rank=0.0,
        execution_sink=executions.append,
    )
    assert hits
    (execution,) = executions
    assert execution.plan_index == 0
    assert execution.attempt_count == 1, "une seule requête SQL émise"
    assert execution.fallback_used is False


def test_d1_plan_execution_is_bounded_and_deduplicated():
    """La stratégie partagée borne les tentatives et ne produit aucun doublon."""
    calls: list[str] = []

    def runner(tsq: str) -> list[dict]:
        calls.append(tsq)
        # Deux fois le même chunk : la déduplication est un invariant du helper.
        return [{"chunk_id": "c1"}, {"chunk_id": "c1"}] if tsq == "q3" else []

    rows, execution = execute_fts_plan(
        plan=["q1", "q2", "q3", "q4"], runner=runner, max_attempts=2
    )
    assert calls == ["q1", "q2"], "la borne est respectée"
    assert rows == []
    assert execution.truncated is True
    assert execution.plan_index is None

    calls.clear()
    rows, execution = execute_fts_plan(plan=["q1", "q3"], runner=runner)
    assert rows == [{"chunk_id": "c1"}], "aucun doublon"
    assert execution.plan_index == 1


def test_d1_empty_plan_is_a_controlled_empty_result(vc_setup):
    """Une question sans terme exploitable ne lève pas : elle ne trouve rien."""
    assert build_tsquery_plan("?? !!") == []
    assert search_corpus_chunks_hybrid(
        corpus_ids=[str(vc_setup["corpus"].id)], question="?? !!", top_k=10, min_text_rank=0.0
    ) == []


@pytest.mark.parametrize("question", DISCRIMINANT_QUESTIONS)
def test_d1_discriminant_questions_find_the_corpus(vc_setup, question):
    """Les questions discriminantes — toutes capitalisées — atteignent le corpus."""
    hits = search_corpus_chunks_hybrid(
        corpus_ids=[str(vc_setup["corpus"].id)], question=question, top_k=30, min_text_rank=0.0
    )
    assert hits and CORPUS_MARKER in hits[0]["text"]


def test_d1_ranking_is_unchanged_when_the_first_plan_succeeds(vc_setup, ingest_text):
    """Quand `plan[0]` matche, le repli n'existe pas : même requête, même classement.

    Garde-fou de non-régression du commit : le partage de la stratégie ne doit RIEN
    changer au cas nominal, seulement ajouter un comportement là où il n'y en avait pas.
    """
    from apps.corpus.services import retrieval as corpus_retrieval

    ingest_text(vc_setup["rag_space"], PRIVATE_TEXT, filename="note.txt")
    cid = str(vc_setup["corpus"].id)
    question = "Venture capital definition"
    plan = build_tsquery_plan(question)

    via_plan = search_corpus_chunks_hybrid(
        corpus_ids=[cid], question=question, top_k=30, min_text_rank=0.0
    )
    direct = corpus_retrieval._search_corpus_chunks_fts(
        corpus_ids=[cid], tsq=plan[0], top_k=30, min_text_rank=0.0
    )
    assert [c["chunk_id"] for c in via_plan] == [c["chunk_id"] for c in direct]
    assert [c["score"] for c in via_plan] == [c["score"] for c in direct]


# ===========================================================================
# Périmètre d'autorisation — INCHANGÉ par le correctif
# ===========================================================================
def test_unassociated_corpus_is_still_excluded(vc_setup, trace):
    vc_setup["ingest_private"]()
    res = _run(vc_setup, trace, QUESTION_CAPITALIZED)
    _dump("non-associé", vc_setup, trace, res)

    assert allowed_corpus_ids_for_workspace(vc_setup["workspace"].id) == []
    assert trace.corpus_candidates == []
    assert "domain_corpus" not in _source_types(res)
    assert "workspace_document" in _source_types(res)


def test_disabled_corpus_is_still_excluded(vc_setup, trace):
    vc_setup["ingest_private"]()
    _associate(vc_setup, enabled=False)
    res = _run(vc_setup, trace, QUESTION_CAPITALIZED)
    _dump("désactivé", vc_setup, trace, res)

    assert allowed_corpus_ids_for_workspace(vc_setup["workspace"].id) == []
    assert trace.corpus_candidates == []
    assert "domain_corpus" not in _source_types(res)


def test_non_ready_corpus_document_is_still_excluded(vc_setup, trace):
    """Un document non READY reste exclu par la clause d'éligibilité SQL."""
    _associate(vc_setup)
    DomainCorpusDocument.objects.filter(corpus=vc_setup["corpus"]).update(status="FAILED")

    assert search_corpus_chunks_hybrid(
        corpus_ids=[str(vc_setup["corpus"].id)],
        question=QUESTION_CAPITALIZED,
        top_k=30,
        min_text_rank=0.0,
    ) == []

    res = _run(vc_setup, trace, QUESTION_CAPITALIZED)
    assert "domain_corpus" not in _source_types(res)


# ===========================================================================
# D1 de bout en bout — le corpus participe et est cité
# ===========================================================================
def test_capitalized_question_now_uses_the_corpus(vc_setup, trace):
    """Le cas du signalement production : le corpus est retrouvé ET cité."""
    _associate(vc_setup)
    res = _run(vc_setup, trace, QUESTION_CAPITALIZED)
    _dump("D1/capitalisée", vc_setup, trace, res)

    assert res.get("status") == "ok"
    assert trace.corpus_candidates, "le leg corpus produit des candidats"
    assert "domain_corpus" in _source_types(res)
    corpus_sources = [s for s in res["sources"] if s.get("source_type") == "domain_corpus"]
    assert corpus_sources[0]["source"]["corpus_name"] == "VC Reference Corpus"
    assert CORPUS_MARKER in str(res["sources"])


def test_capitalized_and_lowercase_questions_converge(vc_setup, trace):
    """Les deux formes de la même question retrouvent le même document corpus."""
    _associate(vc_setup)
    up = _run(vc_setup, trace, QUESTION_CAPITALIZED)
    low = _run(vc_setup, trace, QUESTION_LOWERCASE)

    def corpus_docs(res):
        return {
            (s.get("source") or {}).get("document_id")
            for s in res["sources"]
            if s.get("source_type") == "domain_corpus"
        }

    assert corpus_docs(up)
    assert corpus_docs(up) == corpus_docs(low)


def test_corpus_sources_survive_the_core_facade(vc_setup):
    """La façade Core ne perd ni ne réordonne les sources de corpus métier.

    Ce test comparait autrefois les deux branches de `RAG_USE_CORE_RETRIEVE`. Le
    flag a été retiré ; ce qui reste à prouver est que le chemin UNIQUE conserve
    bien les sources de corpus — l'anomalie que la parité surveillait.
    """
    from apps.chat.services import chat_service

    _associate(vc_setup)
    result = chat_service._dispatch_retrieval(
        rag_space_id=vc_setup["rag_space"].id,
        rag_space=vc_setup["rag_space"],
        user=vc_setup["user"],
        question=QUESTION_CAPITALIZED,
        request_id=None,
        conversation_context=None,
    )
    assert result["sources"], "le retrieval ne doit pas revenir vide"
    assert "domain_corpus" in _source_types(result)


# ===========================================================================
# Coût du repli — le rappel gagné doit rester une dépense bornée
# ===========================================================================
def test_fts_fallback_costs_one_extra_query_per_pass(vc_setup):
    """Le repli n'émet qu'UNE requête par tentative, et s'arrête au premier succès.

    Mesuré : 1 requête quand `plan[0]` matche, 2 quand le repli sert. La borne dure est
    `RAG_FTS_PLAN_MAX_ATTEMPTS` — un plan qui grossirait ne pourrait pas coûter N
    requêtes en silence.
    """
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    cid = str(vc_setup["corpus"].id)

    def corpus_fts_queries(question: str) -> int:
        with CaptureQueriesContext(connection) as captured:
            search_corpus_chunks_hybrid(
                corpus_ids=[cid], question=question, top_k=10, min_text_rank=0.0
            )
        return sum(
            1 for q in captured.captured_queries
            if "domain_corpus_chunk" in q["sql"] and "to_tsquery" in q["sql"]
        )

    assert corpus_fts_queries("Venture capital definition") == 1, "plan[0] matche"
    assert corpus_fts_queries(QUESTION_CAPITALIZED) == 2, "repli sur plan[1]"


# ===========================================================================
# Compteurs de documents exploitables
# ===========================================================================
def test_document_counts_separate_total_from_ready(vc_setup):
    from apps.corpus.services.document_counts import corpus_document_counts

    corpus = vc_setup["corpus"]
    DomainCorpusDocument.objects.create(
        corpus=corpus, original_filename="failed.txt", status="FAILED"
    )
    DomainCorpusDocument.objects.create(
        corpus=corpus, original_filename="pending.txt", status="PENDING"
    )
    DomainCorpusDocument.objects.create(
        corpus=corpus, original_filename="ingesting.txt", status="INGESTING"
    )

    counts = corpus_document_counts(corpus.id)
    assert counts == {
        "documents_count": 4,
        "ready_documents_count": 1,
        "processing_documents_count": 2,
        "failed_documents_count": 1,
    }


def test_workspace_corpora_endpoint_exposes_ready_count(vc_setup):
    from rest_framework.test import APIClient

    _associate(vc_setup)
    DomainCorpusDocument.objects.create(
        corpus=vc_setup["corpus"], original_filename="failed.txt", status="FAILED"
    )
    client = APIClient()
    client.force_authenticate(user=vc_setup["user"])
    resp = client.get(f"/api/v1/workspaces/{vc_setup['workspace'].id}/domain-corpora/")
    assert resp.status_code == 200
    (row,) = resp.json()
    # `documents_count` est CONSERVÉ (compatibilité) et ne suffit plus à conclure.
    assert row["documents_count"] == 2
    assert row["ready_documents_count"] == 1
    assert row["failed_documents_count"] == 1
    assert row["processing_documents_count"] == 0


def test_ready_count_matches_retrieval_eligibility(vc_setup):
    """Le compteur READY et le filtre SQL du retrieval partagent la même définition."""
    from apps.corpus.services.document_counts import corpus_document_counts
    from apps.rag_core.domain.eligibility import ELIGIBLE_DOCUMENT_STATUS

    _associate(vc_setup)
    assert corpus_document_counts(vc_setup["corpus"].id)["ready_documents_count"] == 1
    DomainCorpusDocument.objects.filter(corpus=vc_setup["corpus"]).update(status="INGESTING")
    assert corpus_document_counts(vc_setup["corpus"].id)["ready_documents_count"] == 0
    assert search_corpus_chunks_hybrid(
        corpus_ids=[str(vc_setup["corpus"].id)],
        question=QUESTION_LOWERCASE,
        top_k=10,
        min_text_rank=0.0,
    ) == []
    assert ELIGIBLE_DOCUMENT_STATUS == "READY"


# ===========================================================================
# Observabilité — la trace dit à quelle étape le corpus disparaît
# ===========================================================================
def _corpus_trace(res) -> dict:
    return (res.get("retrieval_trace") or {}).get("business_corpus") or {}


def test_trace_reports_not_associated(vc_setup, trace):
    vc_setup["ingest_private"]()
    res = _run(vc_setup, trace, QUESTION_CAPITALIZED)
    ct = _corpus_trace(res)
    assert ct["associated_corpus_count"] == 0
    assert ct["active_corpus_count"] == 0
    assert ct["corpus_drop_stage"] == "not_associated"


def test_trace_distinguishes_disabled_from_not_associated(vc_setup, trace):
    """Deux situations, deux actions opposées de l'exploitant : deux états distincts."""
    vc_setup["ingest_private"]()
    _associate(vc_setup, enabled=False)
    res = _run(vc_setup, trace, QUESTION_CAPITALIZED)
    ct = _corpus_trace(res)
    assert ct["associated_corpus_count"] == 1
    assert ct["active_corpus_count"] == 0
    assert ct["corpus_drop_stage"] == "disabled"


def test_trace_reports_no_ready_documents(vc_setup, trace):
    _associate(vc_setup)
    DomainCorpusDocument.objects.filter(corpus=vc_setup["corpus"]).update(status="FAILED")
    res = _run(vc_setup, trace, QUESTION_CAPITALIZED)
    ct = _corpus_trace(res)
    assert ct["ready_corpus_document_count"] == 0
    assert ct["corpus_drop_stage"] == "no_ready_documents"


def test_trace_reports_scope_execution_and_selection(vc_setup, trace):
    _associate(vc_setup)
    res = _run(vc_setup, trace, QUESTION_CAPITALIZED)

    ct = _corpus_trace(res)
    assert ct["associated_corpus_count"] == 1
    assert ct["active_corpus_count"] == 1
    assert ct["ready_corpus_document_count"] == 1
    assert ct["corpus_lexical_status"] == "ok"
    assert ct["corpus_vector_status"] in {"ok", "empty", "not_run"}
    assert ct["corpus_fts_plan_index"] == 1, "le repli a servi"
    assert ct["corpus_fts_attempt_count"] >= 2
    assert ct["corpus_fts_fallback_used"] is True
    assert ct["corpus_candidate_count"] >= 1
    assert ct["corpus_selected_count"] >= 1
    assert ct["corpus_drop_stage"] == "selected"
    assert ct["per_corpus"] == [
        {
            "corpus_id": str(vc_setup["corpus"].id),
            "candidates": ct["corpus_pool_count"],
            "selected": ct["corpus_selected_count"],
        }
    ]


def test_trace_never_claims_reranking_when_none_ran(vc_setup, trace):
    """`corpus_drop_stage` ne doit jamais désigner une étape qui n'a pas tourné."""
    vc_setup["ingest_private"]()
    _associate(vc_setup)
    res = _run(vc_setup, trace, QUESTION_CAPITALIZED)
    ct = _corpus_trace(res)
    assert ct["rerank_applied"] is False, "rerank désactivé par défaut"
    assert ct["corpus_drop_stage"] != "reranking"


def test_trace_contains_no_sensitive_payload(vc_setup, trace):
    """Ni question, ni tsquery, ni extrait, ni nom de fichier, ni email dans la trace."""
    _associate(vc_setup)
    vc_setup["ingest_private"]()
    res = _run(vc_setup, trace, QUESTION_CAPITALIZED)

    blob = json.dumps(res.get("retrieval_trace") or {})
    for forbidden in (
        "venture", "capital", "what", CORPUS_MARKER, PRIVATE_MARKER,
        "vc-reference.txt", "generic-vc-note", vc_setup["user"].email,
    ):
        assert forbidden not in blob, forbidden


def test_corpus_leg_has_no_per_document_query(vc_setup):
    """Aucun N+1 : le nombre de requêtes ne dépend pas du nombre de documents corpus."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from apps.corpus.services.retrieval import retrieve_corpus_candidate_legs

    _associate(vc_setup)

    def leg_queries() -> int:
        with CaptureQueriesContext(connection) as captured:
            retrieve_corpus_candidate_legs(
                workspace_id=vc_setup["workspace"].id,
                queries=[QUESTION_CAPITALIZED],
                top_k=8,
                vector_top_k=8,
                hybrid_enabled=False,
                min_text_rank=0.0,
            )
        return len(captured.captured_queries)

    before = leg_queries()
    for i in range(1, 6):
        ingest_corpus_document_from_upload(
            corpus=vc_setup["corpus"],
            filename=f"extra-{i}.txt",
            content_type="text/plain",
            file_bytes=(f"EXTRA_{i} ".encode() + CORPUS_TEXT),
        )
    assert leg_queries() == before


# ===========================================================================
# D2 — fusion bi-source unifiée (flag)
# ===========================================================================
#: Document corpus DÉLIBÉRÉMENT hors sujet, décliné en plusieurs exemplaires : sert à
#: vérifier qu'aucun quota de provenance n'a été introduit.
IRRELEVANT_CORPUS_TEXT = (
    "This reference note describes the cafeteria opening hours, the parking allocation "
    "policy and the printer maintenance window. It mentions capital once and nothing "
    "else of substance."
).encode("utf-8")


@UNIFIED_OFF
def test_d2_legacy_path_keeps_historic_behaviour(vc_setup, trace):
    """Flag OFF : comportement HISTORIQUE conservé, y compris son défaut.

    C'est ce qui rend le rollback réel — et ce qui documente ce que le flag change.
    """
    vc_setup["ingest_private"]()
    _associate(vc_setup)
    res = _run(vc_setup, trace, QUESTION_CAPITALIZED)
    _dump("D2/flag off", vc_setup, trace, res)

    assert trace.corpus_candidates, "D1 corrigé : le corpus participe même flag OFF"
    assert trace.diversify_called
    corpus_pool = [c for c in trace.diversify_pool if Trace.provenance(c) == "domain_corpus"]
    assert corpus_pool, "les candidats corpus atteignent le pool"
    assert all("fusion_score" not in c for c in corpus_pool), "sans fusion_score (historique)"
    assert trace.counts("diversify_out")["business_corpus"] == 0
    assert "domain_corpus" not in _source_types(res)
    assert _corpus_trace(res)["corpus_drop_stage"] == "diversification"
    assert _corpus_trace(res)["unified_fusion"] is False


@UNIFIED_ON
def test_d2_unified_fusion_gives_every_candidate_a_comparable_key(vc_setup, trace):
    """Flag ON : TOUS les candidats entrant dans la diversification ont un rang fusionné."""
    vc_setup["ingest_private"]()
    _associate(vc_setup)
    res = _run(vc_setup, trace, QUESTION_CAPITALIZED)
    _dump("D2/flag on", vc_setup, trace, res)

    assert trace.diversify_called
    pool = trace.diversify_pool
    corpus_pool = [c for c in pool if Trace.provenance(c) == "domain_corpus"]
    private_pool = [c for c in pool if Trace.provenance(c) != "domain_corpus"]
    assert corpus_pool and private_pool

    # 1) Clé de classement définie pour tout le monde, corpus compris.
    assert all(float(c.get("fusion_score") or 0.0) > 0.0 for c in pool)
    # 2) Obtenue par la MÊME méthode : un vote de rang 1/(K+rang), donc la valeur d'un
    #    candidat de rang 1 est identique quelle que soit sa provenance.
    from apps.rag_core.services.query.merge import _rrf_k

    best_corpus = max(corpus_pool, key=lambda c: c["fusion_score"])
    assert best_corpus["fusion_score"] == pytest.approx(1.0 / (_rrf_k() + 1))
    # 3) Le corpus, plus pertinent ici, est sélectionné.
    assert trace.counts("diversify_out")["business_corpus"] > 0
    assert "domain_corpus" in _source_types(res)
    ct = _corpus_trace(res)
    assert ct["unified_fusion"] is True
    assert ct["corpus_drop_stage"] == "selected"


@UNIFIED_ON
def test_d2_no_provenance_quota_is_applied(vc_setup, trace):
    """Aucune place n'est RÉSERVÉE : le corpus n'occupe pas le top-k par principe."""
    vc_setup["ingest_private"]()
    _associate(vc_setup)
    res = _run(vc_setup, trace, QUESTION_CAPITALIZED)

    selected = trace.counts("diversify_out")
    assert selected["private"] > 0, "les documents privés pertinents restent présents"
    assert selected["business_corpus"] < len(trace.diversify_out), "pas de monopole corpus"
    assert len(res["sources"]) > selected["business_corpus"]


@UNIFIED_ON
def test_d2_weaker_corpus_candidates_stay_excluded(vc_setup, trace):
    """C'est le RANG qui décide, pas la provenance — dans les deux sens.

    Contre-épreuve du test précédent : sans elle, « le corpus remonte » pourrait aussi
    bien décrire un biais de provenance qu'une fusion correcte. On ajoute donc au corpus
    quatre documents faiblement pertinents ; ils ne doivent pas entrer en bloc, et les
    documents privés doivent rester présents.
    """
    for i in range(1, 5):
        ingest_corpus_document_from_upload(
            corpus=vc_setup["corpus"],
            filename=f"weak-{i}.txt",
            content_type="text/plain",
            file_bytes=(f"WEAK_CORPUS_MARKER_{i} ".encode() + IRRELEVANT_CORPUS_TEXT),
        )
    _associate(vc_setup)
    vc_setup["ingest_private"]()

    res = _run(vc_setup, trace, QUESTION_CAPITALIZED)
    _dump("D2/corpus faibles", vc_setup, trace, res)

    pool = trace.counts("diversify_pool")
    selected = trace.counts("diversify_out")
    assert pool["business_corpus"] >= 5, "les cinq documents corpus sont candidats"
    # Tous les candidats corpus ne passent pas : la sélection les CLASSE, elle ne les
    # accueille pas en bloc.
    assert selected["business_corpus"] < pool["business_corpus"]
    # Les documents privés pertinents restent présents : aucune place n'est réservée à
    # une provenance, dans un sens comme dans l'autre.
    assert selected["private"] > 0
    # Les documents corpus les plus faibles restent dehors.
    blob = str(res["sources"])
    assert sum(f"WEAK_CORPUS_MARKER_{i}" in blob for i in range(1, 5)) < 4
    # Propriété ASSUMÉE du RRF, à connaître avant d'activer le flag : à poids égal, le
    # meilleur candidat de chaque leg obtient la MÊME contribution `1/(K+1)`. Deux legs
    # lexicaux (privé et corpus) s'entrelacent donc rang par rang. Ce n'est pas un biais
    # de provenance — c'est la sémantique du vote de rang, la même qui gouverne déjà
    # lexical vs vectoriel. C'est aussi pourquoi ce flag est OFF par défaut : l'effet sur
    # la baseline se mesure au benchmark, il ne se devine pas.
    assert selected["business_corpus"] >= 1


@UNIFIED_ON
def test_d2_unified_fusion_preserves_private_only_behaviour(vc_setup, trace):
    """Sans corpus associé, la fusion unifiée rend le MÊME résultat que l'historique.

    Verrouille le périmètre du flag : il ne doit rien changer aux workspaces qui n'ont
    aucun corpus métier, c'est-à-dire à la majorité.
    """
    vc_setup["ingest_private"]()
    with UNIFIED_OFF:
        legacy = _run(vc_setup, trace, QUESTION_CAPITALIZED)
    unified = _run(vc_setup, trace, QUESTION_CAPITALIZED)

    assert [s["chunk_id"] for s in unified["sources"]] == [s["chunk_id"] for s in legacy["sources"]]


def test_d2_flag_is_off_by_default(vc_setup):
    """Le défaut de déploiement est le comportement historique — rollback par omission."""
    from apps.rag_core.services.query.candidate_retriever import unified_fusion_enabled

    assert unified_fusion_enabled() is False
