# backend/apps/rag_core/benchmark/bi_source_weights.py
from __future__ import annotations

"""Qualification de la PONDÉRATION des legs RRF, évaluée APRÈS budget de prompt.

Ce que ce module ajoute au benchmark bi-source existant
------------------------------------------------------
``bi_source.py`` mesure le classement. Il ne suffit pas : la production a montré que
40 sources sont sélectionnées et qu'environ 8 seulement atteignent le contexte du
modèle. Un classement excellent dont les bonnes sources tombent au budget de prompt
produit exactement la même réponse qu'un mauvais classement — et aucun compteur de
retrieval ne le dit.

La métrique décisive de ce module est donc **la présence de la source attendue dans le
prompt**, pas son rang dans le pool fusionné.

Ce qui est mesuré, poids par poids
----------------------------------
Pour chaque question et chaque α : présence et rang de la source attendue après
sélection (top_k) PUIS après budget de prompt, rétention par provenance entre les deux,
documents distincts, concentration par document, doublons, latence, requêtes SQL.

L'équilibre 50/50 n'est PAS une cible. La cible est que la source attendue survive.

Aucune génération LLM : l'évaluation porte sur le retrieval et le contexte assemblé.
"""

import time
from dataclasses import dataclass, field
from statistics import median
from typing import Any, Sequence

from django.db import connection
from django.test.utils import CaptureQueriesContext

DATASET_ID = "bi-source-weights"
DATASET_VERSION = "v1"

WORKSPACE_NAME = "Bench Weights"
SPACE_NAME = "bench-weights-space"
DOMAIN_SLUG = "bench-weights-domain"
CORPUS_SLUG = "bench-weights-corpus"
CORPUS_NAME = "Bench Weights Reference"

DEFAULT_WEIGHTS: tuple[float, ...] = (1.0, 0.75, 0.5, 0.35, 0.25)

# ---------------------------------------------------------------------------
# Documents PRIVÉS — notes internes d'un espace « bâti / ingénierie ».
# Chaque note porte UN sujet propre, pour qu'une question privée ait une réponse
# réellement située dans un document identifiable.
# ---------------------------------------------------------------------------
PRIVATE_DOCUMENTS: dict[str, str] = {
    "note-badge-access.txt": (
        "Internal note — badge access policy. Site badges are issued by the facility "
        "manager and revoked on the last working day. Visitors receive a temporary "
        "badge valid for one day and must be escorted at all times. Lost badges are "
        "deactivated within one hour of being reported."
    ),
    "note-parking.txt": (
        "Internal note — parking allocation. Parking spaces are allocated quarterly by "
        "seniority and commuting distance. Electric vehicle bays are reserved and "
        "cannot be occupied by combustion vehicles. Visitors use the north gate bays."
    ),
    "note-hvac-spec.txt": (
        "Internal note — HVAC specification for the east wing. Air handling units must "
        "sustain six air changes per hour at design occupancy. Filters are class F7 and "
        "replaced every six months. Acoustic attenuation targets 35 dBA in open offices."
    ),
    "note-bim-standard.txt": (
        "Internal note — BIM standard. Models are delivered at level of development 350 "
        "for appel d'offres and 500 as built. Naming follows the project prefix, discipline "
        "code and zone. Clash detection runs weekly and blocking clashes stop issuance."
    ),
    "note-site-safety.txt": (
        "Internal note — site safety induction. Every person entering the site follows "
        "a two hour induction covering fall protection, lifting operations and permit "
        "to work. Harnesses are inspected monthly and tagged."
    ),
    "note-procurement.txt": (
        "Internal note — procurement thresholds. Purchases below five thousand euros "
        "are approved by the project lead, above that by the steering committee. Three "
        "quotes are mandatory above twenty thousand euros. Framework suppliers are "
        "reviewed annually."
    ),
    "note-timesheet.txt": (
        "Internal note — timesheet rules. Hours are booked daily against a work package "
        "code and locked on the fifth of the following month. Overtime requires prior "
        "written approval from the project lead."
    ),
    "note-training-plan.txt": (
        "Internal note — annual training plan. Each engineer receives five training "
        "days per year, at least two of them technical. Requests are consolidated in "
        "September and arbitrated by the discipline lead."
    ),
    "note-travel-policy.txt": (
        "Internal note — travel policy. Rail is mandatory below four hours door to "
        "door. Hotel caps are set by city band and revised each January. Expense claims "
        "are filed within thirty days with receipts."
    ),
    "note-archive-room.txt": (
        "Internal note — archive room. Physical drawings are retained for ten years "
        "after handover, then destroyed with a witnessed certificate. The archive room "
        "is kept below sixty percent relative humidity."
    ),
}

# ---------------------------------------------------------------------------
# Documents du CORPUS MÉTIER — socle de référence, un concept par document.
# ---------------------------------------------------------------------------
CORPUS_DOCUMENTS: dict[str, str] = {
    "ref-definition.txt": (
        "Venture capital is a form of equity financing provided to unlisted, privately "
        "held companies with high growth potential and a high risk of failure. The "
        "investor takes an equity participation rather than lending money."
    ),
    "ref-partners.txt": (
        "A venture capital fund is structured around general partners, who manage the "
        "fund and select investments, and limited partners, who commit the capital and "
        "remain passive. General partners earn a management fee and carried interest."
    ),
    "ref-exit.txt": (
        "Venture capital investors realise their return through an exit over a horizon "
        "of five to ten years: a trade sale, a secondary sale or an initial public "
        "offering. No interest is paid along the way."
    ),
    "ref-bank-loan.txt": (
        "Unlike a conventional bank loan, venture capital carries no collateral, no "
        "repayment schedule and no interest. The lender is repaid regardless of "
        "performance; the equity investor only through an exit."
    ),
    "ref-dilution.txt": (
        "Dilution occurs when new shares are issued in a subsequent financing round, "
        "reducing the ownership percentage of existing shareholders. Anti dilution "
        "clauses protect earlier investors against a down round."
    ),
    "ref-term-sheet.txt": (
        "A term sheet summarises the economics and the control of a proposed "
        "investment: valuation, liquidation preference, board composition, vesting and "
        "information rights. It is largely non binding except for confidentiality."
    ),
    "ref-due-diligence.txt": (
        "Due diligence reviews the legal, financial, technical and commercial standing "
        "of a target before closing. Findings feed the representations and warranties "
        "and may adjust the agreed valuation."
    ),
    "ref-valuation.txt": (
        "Pre money valuation is agreed before the new investment; post money valuation "
        "adds the amount invested. The ratio between them determines the ownership "
        "percentage acquired by the new investor."
    ),
    "ref-board.txt": (
        "Board composition balances founders, investors and independent directors. "
        "Reserved matters list the decisions that require investor consent, such as "
        "issuing new shares or approving the annual budget."
    ),
    "ref-follow-on.txt": (
        "A follow on investment is a further contribution to an existing portfolio "
        "company, often to maintain the ownership percentage through a later round. "
        "Reserves are set aside at fund inception for that purpose."
    ),
}


# ---------------------------------------------------------------------------
# VOLUME — la condition qui rend la mesure discriminante
# ---------------------------------------------------------------------------
# Sans volume, les 20 documents porteurs de réponse tiennent tous dans le contexte :
# chaque métrique vaut 1.0 et le poids n'a aucun effet observable. Ce n'est pas un
# résultat, c'est une absence de contrainte.
#
# La production met en compétition ~120 candidats privés contre ~120 candidats corpus
# pour 40 places, dont ~8 seulement atteignent le prompt. On reproduit CETTE rareté :
# des documents de remplissage, thématiquement plausibles mais sans réponse attendue,
# des deux côtés, en quantité comparable.
FILLER_DOCUMENTS_PER_SIDE = 40

_PRIVATE_FILLER_TOPICS = (
    "meeting minutes", "supplier review", "site diary", "quality checklist",
    "handover log", "inspection report", "coordination report", "risk register",
)
_CORPUS_FILLER_TOPICS = (
    "fund reporting", "portfolio review", "capital call", "investor letter",
    "market commentary", "sector note", "syndication practice", "governance memo",
)


def _filler_text(kind: str, topic: str, index: int) -> str:
    """Texte de remplissage long (≈3 chunks), sans réponse à aucune question du jeu."""
    body = (
        f"This {kind} document number {index} covers {topic}. It records routine "
        "observations, references the applicable internal procedure, lists the "
        "participants by role and notes the follow up actions with their owners and "
        "target dates. It contains no definition, no threshold and no rule that any "
        "evaluation question asks about. "
    )
    return (body * 12).strip()


def filler_documents(kind: str) -> dict[str, str]:
    topics = _PRIVATE_FILLER_TOPICS if kind == "private" else _CORPUS_FILLER_TOPICS
    prefix = "filler-private" if kind == "private" else "filler-corpus"
    return {
        f"{prefix}-{i:03d}.txt": _filler_text(kind, topics[i % len(topics)], i)
        for i in range(1, FILLER_DOCUMENTS_PER_SIDE + 1)
    }


@dataclass(frozen=True)
class Case:
    """Une question annotée. ``expected`` référence des NOMS de fichiers du jeu."""

    id: str
    category: str  # private | corpus | mixed | off_topic
    question: str
    expected_origin: str  # workspace | corpus | mixed | none
    expected_private: tuple[str, ...] = ()
    expected_corpus: tuple[str, ...] = ()
    notes: str = ""


CASES: tuple[Case, ...] = (
    # --- A. principalement PRIVÉ (10) --------------------------------------
    Case("p01", "private", "Who issues site badges and when are they revoked?",
         "workspace", ("note-badge-access.txt",)),
    Case("p02", "private", "How are parking spaces allocated?",
         "workspace", ("note-parking.txt",)),
    Case("p03", "private", "What air change rate must the east wing air handling units sustain?",
         "workspace", ("note-hvac-spec.txt",)),
    Case("p04", "private", "At which level of development are BIM models delivered as built?",
         "workspace", ("note-bim-standard.txt",)),
    Case("p05", "private", "How long is the site safety induction?",
         "workspace", ("note-site-safety.txt",)),
    Case("p06", "private", "Above which amount are three quotes mandatory?",
         "workspace", ("note-procurement.txt",)),
    Case("p07", "private", "When are timesheets locked?",
         "workspace", ("note-timesheet.txt",)),
    Case("p08", "private", "How many training days does each engineer receive?",
         "workspace", ("note-training-plan.txt",)),
    Case("p09", "private", "When is rail travel mandatory?",
         "workspace", ("note-travel-policy.txt",)),
    Case("p10", "private", "How long are physical drawings retained after handover?",
         "workspace", ("note-archive-room.txt",)),
    # --- B. principalement CORPUS (10) -------------------------------------
    Case("c01", "corpus", "What is venture capital?",
         "corpus", (), ("ref-definition.txt",)),
    Case("c02", "corpus", "What is the role of general partners and limited partners?",
         "corpus", (), ("ref-partners.txt",)),
    Case("c03", "corpus", "How do venture capital investors realise their return?",
         "corpus", (), ("ref-exit.txt",)),
    Case("c04", "corpus", "How is venture capital different from a bank loan?",
         "corpus", (), ("ref-bank-loan.txt",)),
    Case("c05", "corpus", "What is dilution in a financing round?",
         "corpus", (), ("ref-dilution.txt",)),
    Case("c06", "corpus", "What does a term sheet summarise?",
         "corpus", (), ("ref-term-sheet.txt",)),
    Case("c07", "corpus", "What does due diligence review before closing?",
         "corpus", (), ("ref-due-diligence.txt",)),
    Case("c08", "corpus", "What is the difference between pre money and post money valuation?",
         "corpus", (), ("ref-valuation.txt",)),
    Case("c09", "corpus", "Which decisions require investor consent on the board?",
         "corpus", (), ("ref-board.txt",)),
    Case("c10", "corpus", "What is a follow on investment?",
         "corpus", (), ("ref-follow-on.txt",)),
    # --- C. MIXTE (10) : les deux origines apportent un élément -------------
    Case("m01", "mixed", "What approval is needed for a purchase and what is a reserved matter?",
         "mixed", ("note-procurement.txt",), ("ref-board.txt",)),
    Case("m02", "mixed", "How is overtime approved and how are general partners remunerated?",
         "mixed", ("note-timesheet.txt",), ("ref-partners.txt",)),
    Case("m03", "mixed", "How long are drawings retained and what is the exit horizon?",
         "mixed", ("note-archive-room.txt",), ("ref-exit.txt",)),
    Case("m04", "mixed", "What is the training budget and what is a management fee?",
         "mixed", ("note-training-plan.txt",), ("ref-partners.txt",)),
    Case("m05", "mixed", "What are the hotel caps and what is a liquidation preference?",
         "mixed", ("note-travel-policy.txt",), ("ref-term-sheet.txt",)),
    Case("m06", "mixed", "Who inspects harnesses and what is due diligence?",
         "mixed", ("note-site-safety.txt",), ("ref-due-diligence.txt",)),
    Case("m07", "mixed", "What is the filter class and what is an anti dilution clause?",
         "mixed", ("note-hvac-spec.txt",), ("ref-dilution.txt",)),
    Case("m08", "mixed", "How often does clash detection run and what is post money valuation?",
         "mixed", ("note-bim-standard.txt",), ("ref-valuation.txt",)),
    Case("m09", "mixed", "Who escorts visitors and who commits the capital of a fund?",
         "mixed", ("note-badge-access.txt",), ("ref-partners.txt",)),
    Case("m10", "mixed", "Which bays are reserved and what is a follow on investment?",
         "mixed", ("note-parking.txt",), ("ref-follow-on.txt",)),
    # --- D. HORS SUJET (5) -------------------------------------------------
    Case("o01", "off_topic", "Quelle est la recette de la tarte tatin ?", "none"),
    Case("o02", "off_topic", "Quelle est la hauteur du mont Fuji ?", "none"),
    Case("o03", "off_topic", "Comment tailler un rosier en hiver ?", "none"),
    Case("o04", "off_topic", "Qui a composé la Symphonie du Nouveau Monde ?", "none"),
    Case("o05", "off_topic", "Quelle est la durée de vie d'une tortue d'Hermann ?", "none"),
)

CATEGORIES: tuple[str, ...] = ("private", "corpus", "mixed", "off_topic")


# ---------------------------------------------------------------------------
# Mesure
# ---------------------------------------------------------------------------
def _origin(source: dict) -> str:
    src = source.get("source") if isinstance(source.get("source"), dict) else {}
    return (
        "corpus"
        if (source.get("source_type") or src.get("source_type")) == "domain_corpus"
        else "workspace"
    )


def _doc_id(source: dict) -> str | None:
    src = source.get("source") if isinstance(source.get("source"), dict) else {}
    v = src.get("document_id") or source.get("document_id")
    return str(v) if v else None


def _first_rank(sources: Sequence[dict], wanted: set[str]) -> int | None:
    for i, s in enumerate(sources, start=1):
        if _doc_id(s) in wanted:
            return i
    return None


@dataclass
class CaseResult:
    """Résultat d'UNE question sous UN poids."""

    case_id: str
    category: str
    weight: float
    status: str = ""
    latency_ms: float = 0.0
    sql_queries: int = 0
    # sélection (top_k) puis contexte réellement transmis (après budget de prompt)
    top_k_workspace: int = 0
    top_k_corpus: int = 0
    prompt_workspace: int = 0
    prompt_corpus: int = 0
    expected_rank_top_k: int | None = None
    expected_rank_prompt: int | None = None
    expected_private_in_prompt: bool = False
    expected_corpus_in_prompt: bool = False
    duplicates: int = 0
    distinct_documents_prompt: int = 0
    max_chunks_same_document_prompt: int = 0
    prompt_chars: int = 0

    @property
    def workspace_retention(self) -> float:
        return self.prompt_workspace / max(self.top_k_workspace, 1)

    @property
    def corpus_retention(self) -> float:
        return self.prompt_corpus / max(self.top_k_corpus, 1)

    def as_row(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "category": self.category,
            "weight": self.weight,
            "status": self.status,
            "latency_ms": round(self.latency_ms, 1),
            "sql_queries": self.sql_queries,
            "workspace_count_top_k": self.top_k_workspace,
            "corpus_count_top_k": self.top_k_corpus,
            "workspace_count_prompt": self.prompt_workspace,
            "corpus_count_prompt": self.prompt_corpus,
            "workspace_retention_prompt": round(self.workspace_retention, 4),
            "corpus_retention_prompt": round(self.corpus_retention, 4),
            "expected_rank_top_k": self.expected_rank_top_k,
            "expected_rank_prompt": self.expected_rank_prompt,
            "expected_private_in_prompt": self.expected_private_in_prompt,
            "expected_corpus_in_prompt": self.expected_corpus_in_prompt,
            "duplicates": self.duplicates,
            "distinct_documents_prompt": self.distinct_documents_prompt,
            "max_chunks_same_document_prompt": self.max_chunks_same_document_prompt,
            "prompt_chars": self.prompt_chars,
        }


def run_case(*, rag_space, user, case: Case, weight: float, doc_ids: dict[str, str]) -> CaseResult:
    """Exécute UNE question et mesure la survie jusqu'au contexte du modèle."""
    from apps.rag_core.services.query import query_rag

    wanted_private = {doc_ids[f] for f in case.expected_private if f in doc_ids}
    wanted_corpus = {doc_ids[f] for f in case.expected_corpus if f in doc_ids}
    wanted = wanted_private | wanted_corpus

    started = time.perf_counter()
    with CaptureQueriesContext(connection) as captured:
        # `query_rag` s'arrête au prompt assemblé : aucune génération n'est déclenchée.
        res = query_rag(
            rag_space_id=rag_space.id, rag_space=rag_space, user=user, question=case.question
        )
    latency = (time.perf_counter() - started) * 1000.0

    prompt_sources = res.get("sources") or []
    trace = (res.get("retrieval_trace") or {}).get("business_corpus") or {}
    stages = trace.get("stage_counts") or {}
    after_rerank = stages.get("after_reranking") or {}

    doc_counts: dict[str, int] = {}
    for s in prompt_sources:
        d = _doc_id(s)
        if d:
            doc_counts[d] = doc_counts.get(d, 0) + 1
    ids = [s.get("chunk_id") for s in prompt_sources if s.get("chunk_id")]

    return CaseResult(
        case_id=case.id,
        category=case.category,
        weight=weight,
        status=str(res.get("status") or ""),
        latency_ms=latency,
        sql_queries=len(captured.captured_queries),
        top_k_workspace=int(after_rerank.get("workspace") or 0),
        top_k_corpus=int(after_rerank.get("corpus") or 0),
        prompt_workspace=sum(1 for s in prompt_sources if _origin(s) == "workspace"),
        prompt_corpus=sum(1 for s in prompt_sources if _origin(s) == "corpus"),
        expected_rank_prompt=_first_rank(prompt_sources, wanted) if wanted else None,
        expected_private_in_prompt=bool(
            wanted_private and any(_doc_id(s) in wanted_private for s in prompt_sources)
        ),
        expected_corpus_in_prompt=bool(
            wanted_corpus and any(_doc_id(s) in wanted_corpus for s in prompt_sources)
        ),
        duplicates=len(ids) - len(set(ids)),
        distinct_documents_prompt=len(doc_counts),
        max_chunks_same_document_prompt=max(doc_counts.values(), default=0),
        prompt_chars=len(res.get("prompt") or ""),
    )


@dataclass
class WeightSummary:
    """Agrégat d'un poids, par catégorie et global."""

    weight: float
    results: list[CaseResult] = field(default_factory=list)

    def _by(self, category: str) -> list[CaseResult]:
        return [r for r in self.results if r.category == category]

    @staticmethod
    def _rate(rows: list[CaseResult], predicate) -> float:
        return round(sum(1 for r in rows if predicate(r)) / len(rows), 4) if rows else 0.0

    def as_dict(self) -> dict[str, Any]:
        private, corpus = self._by("private"), self._by("corpus")
        mixed, off = self._by("mixed"), self._by("off_topic")
        lat = sorted(r.latency_ms for r in self.results)
        return {
            "weight": self.weight,
            "cases": len(self.results),
            # A. la source attendue survit-elle jusqu'au PROMPT ?
            "private_expected_in_prompt": self._rate(private, lambda r: r.expected_private_in_prompt),
            "corpus_expected_in_prompt": self._rate(corpus, lambda r: r.expected_corpus_in_prompt),
            "mixed_both_in_prompt": self._rate(
                mixed, lambda r: r.expected_private_in_prompt and r.expected_corpus_in_prompt
            ),
            "mixed_private_in_prompt": self._rate(mixed, lambda r: r.expected_private_in_prompt),
            "mixed_corpus_in_prompt": self._rate(mixed, lambda r: r.expected_corpus_in_prompt),
            # C. faux positifs corpus sur les questions sans réponse attendue
            "off_topic_free_of_corpus": self._rate(off, lambda r: r.prompt_corpus == 0),
            "off_topic_no_answer": self._rate(off, lambda r: r.status != "ok"),
            # rétention par provenance entre sélection et contexte
            "workspace_retention_prompt": round(
                sum(r.workspace_retention for r in self.results) / max(len(self.results), 1), 4
            ),
            "corpus_retention_prompt": round(
                sum(r.corpus_retention for r in self.results) / max(len(self.results), 1), 4
            ),
            "prompt_workspace_mean": round(
                sum(r.prompt_workspace for r in self.results) / max(len(self.results), 1), 2
            ),
            "prompt_corpus_mean": round(
                sum(r.prompt_corpus for r in self.results) / max(len(self.results), 1), 2
            ),
            "distinct_documents_prompt_mean": round(
                sum(r.distinct_documents_prompt for r in self.results) / max(len(self.results), 1), 2
            ),
            "max_chunks_same_document_prompt": max(
                (r.max_chunks_same_document_prompt for r in self.results), default=0
            ),
            "duplicates_total": sum(r.duplicates for r in self.results),
            "no_answer_rate": self._rate(self.results, lambda r: r.status != "ok"),
            "latency_p50_ms": round(median(lat), 1) if lat else 0.0,
            "latency_p95_ms": round(lat[int(len(lat) * 0.95) - 1], 1) if len(lat) >= 2 else 0.0,
            "sql_queries_mean": round(
                sum(r.sql_queries for r in self.results) / max(len(self.results), 1), 1
            ),
            "prompt_chars_mean": round(
                sum(r.prompt_chars for r in self.results) / max(len(self.results), 1)
            ),
        }
