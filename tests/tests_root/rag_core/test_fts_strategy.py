"""L4.1 — stratégie FTS : planificateur adaptatif, vérité terrain, retry.

Le préfiltre est mesuré contre une vérité terrain ANNOTÉE À LA MAIN : précision et
rappel du filtre LEXICAL, indépendamment de la qualité du LLM.
"""
import pytest

from apps.rag_core.analysis import global_search as gs
from apps.rag_core.analysis.fts_query import (
    QueryTooBroad,
    build_global_fts_query,
    build_tsquery_expression,
    extract_quoted_phrases,
    plan_global_search_query,
)
from apps.rag_core.models import Chunk, Document, RagSpace
from apps.rag_core.models.document import DocumentStatus
from apps.rag_core.repositories import global_search_repository as repo

from .fixtures_global_search_truth import BENCHMARK_QUERIES, DOCUMENTS

pytestmark = pytest.mark.django_db

CFG = "french"
#: 20 lots de 20 extraits = ce qu'un budget d'analyse peut absorber.
MAX_CANDIDATES = 400


@pytest.fixture
def truth(rag_space, workspace):
    """Monte le corpus de vérité terrain. Renvoie (space, other, key->doc_id)."""
    other = RagSpace.objects.create(name="other", workspace=workspace)
    key2doc: dict[str, str] = {}

    for i, d in enumerate(DOCUMENTS):
        space = other if d.get("space") == "other" else rag_space
        status = (
            DocumentStatus.FAILED if d.get("status") == "FAILED" else DocumentStatus.READY
        )
        doc = Document.objects.create(
            rag_space=space, filename=f"{d['key']}.txt", status=status, sha256=f"s{i}"
        )
        key2doc[d["key"]] = str(doc.id)
        for j, text in enumerate(d["chunks"]):
            Chunk.objects.create(
                rag_space=space, document=doc, chunk_index=j, text=text, page_number=1
            )

    return rag_space, other, key2doc


def _measure(space, key2doc, query: str, *, max_candidates=MAX_CANDIDATES):
    """Exécute le préfiltre planifié et renvoie (clés de documents, plan)."""
    doc2key = {v: k for k, v in key2doc.items()}
    scanned = repo.scan_scope(rag_space_id=space.id)["chunks_scanned"]

    gq = build_global_fts_query(
        query,
        configuration=CFG,
        df_provider=lambda terms: repo.term_document_frequencies(
            rag_space_id=space.id, terms=terms, configuration=CFG
        ),
        scanned_chunks=scanned,
    )
    plan = plan_global_search_query(
        gq,
        counter=lambda e: repo.count_global_fts_candidates(
            rag_space_id=space.id, query=e, configuration=CFG, mode="boolean"
        ),
        max_candidate_chunks=max_candidates,
    )
    keys = {
        doc2key.get(c["document_id"])
        for c in repo.iterate_global_fts_candidates(
            rag_space_id=space.id, query=plan.expression, configuration=CFG,
            mode="boolean",
        )
    }
    keys.discard(None)
    return keys, plan


# --------------------------------------------------------------------------- #
# Vérité terrain : précision et rappel du PRÉFILTRE
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("case", BENCHMARK_QUERIES, ids=lambda c: c["id"])
def test_no_irrelevant_document_is_retrieved(truth, case):
    """Précision — aucun document explicitement hors sujet ne doit remonter."""
    space, _other, key2doc = truth
    got, _plan = _measure(space, key2doc, case["query"])

    traps = got & set(case["expected_irrelevant"])
    assert not traps, f"documents hors sujet retrouvés : {sorted(traps)}"


@pytest.mark.parametrize("case", BENCHMARK_QUERIES, ids=lambda c: c["id"])
def test_lexically_reachable_documents_are_retrieved(truth, case):
    """Rappel — tout ce qui est LEXICALEMENT atteignable doit être retrouvé.

    Les documents listés dans ``lexically_unreachable`` (synonymes, paraphrases,
    fautes de frappe, accents manquants, autre langue) ne sont PAS attendus : un
    préfiltre lexical ne peut pas les atteindre. C'est exactement la frontière
    entre couverture B (garantie) et C (probabiliste) — elle est mesurée, pas
    masquée.
    """
    space, _other, key2doc = truth
    got, _plan = _measure(space, key2doc, case["query"])

    expected = set(case["expected_relevant"])
    missed = expected - got
    assert not missed, f"documents atteignables manqués : {sorted(missed)}"


def test_ground_truth_precision_and_recall(truth, capsys):
    """Mesure agrégée, PUBLIÉE.

    Le rappel doit être PARFAIT sur ce que le préfiltre lexical peut atteindre
    (c'est la garantie de niveau B). La précision, elle, est bornée par des limites
    réelles du FTS — collisions de radicaux notamment — qui sont mesurées ici, pas
    embellies.
    """
    space, _other, key2doc = truth
    tp = fp = fn = 0
    collisions = 0

    for case in BENCHMARK_QUERIES:
        got, _ = _measure(space, key2doc, case["query"])
        expected = set(case["expected_relevant"])
        tp += len(got & expected)
        fn += len(expected - got)
        fp += len(got & set(case["expected_irrelevant"]))
        collisions += len(got & set(case.get("stem_collisions", [])))

    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0

    with capsys.disabled():
        print(f"\n  PRÉFILTRE — précision {precision:.2f} | rappel {recall:.2f} "
              f"| collisions de radical : {collisions}")

    # Rappel PARFAIT sur l'atteignable : c'est la promesse du niveau B.
    assert recall == 1.0, f"rappel {recall:.2f} — un document atteignable manque"
    # Aucun document franchement hors sujet ne remonte.
    assert precision == 1.0, f"précision {precision:.2f} — un piège est remonté"


def test_french_stemming_collides_and_we_say_so(truth):
    """LIMITE RÉELLE — « équipements » et « équipe » ont le MÊME radical (« équip »).

    Un préfiltre lexical ramènera donc des documents sur l'« équipe » quand on
    cherche des « équipements ». Ce n'est pas un bug : c'est la frontière entre
    couverture B (lexicale, garantie) et C (sémantique, jamais garantie). Le
    traitement par lots doit écarter ces faux positifs — le préfiltre, lui, ne le
    peut pas.
    """
    space, _other, key2doc = truth
    got, _ = _measure(
        space, key2doc,
        "analyse tout le corpus concernant le transport des équipements",
    )
    assert "d13" in got                      # le vrai sujet
    assert got & {"d23", "d24"}, "la collision de radical devrait se produire"


def test_accent_folding_is_inconsistent_and_we_say_so(truth):
    """LIMITE RÉELLE — le repliement d'accent du stemmer français est INCOHÉRENT.

    Mesuré en base :
      « délais » -> 'del'   et « delai »   -> 'del'    => ILS MATCHENT
      « sécurité » -> 'sécur' et « securite » -> 'securit' => ILS NE MATCHENT PAS

    Il est donc FAUX de dire « les accents ne sont pas dépliés » (trop général) ET
    faux de dire « les accents sont dépliés ». La règle exacte : **on ne peut pas
    compter sur les variantes accentuées**. C'est une limite du niveau C, mesurée
    plutôt que masquée.
    """
    space, _other, key2doc = truth

    # Cas où le repliement N'A PAS lieu : « securite » n'est pas atteint.
    got_sec, _ = _measure(
        space, key2doc,
        "recherche dans toute la base les informations sur la sécurité",
    )
    assert "d05" in got_sec        # avec accent : trouvé
    assert "d08" not in got_sec    # sans accent : PAS trouvé

    # Cas où le repliement A LIEU : « delai » EST atteint depuis « délais ».
    got_del, _ = _measure(space, key2doc, "analyse tout le corpus sur les délais")
    assert "d04" in got_del        # « sans delai » (sans accent) : trouvé quand même


def test_documents_of_another_space_never_leak(truth):
    """Le document d27 parle du sujet, mais vit dans un AUTRE RagSpace."""
    space, _other, key2doc = truth
    for case in BENCHMARK_QUERIES:
        got, _ = _measure(space, key2doc, case["query"])
        assert "d27" not in got, f"fuite inter-espace sur {case['id']}"


def test_non_exploitable_documents_are_never_candidates(truth):
    """d25 (READY sans extrait) et d26 (FAILED) ne peuvent pas être candidats."""
    space, _other, key2doc = truth
    for case in BENCHMARK_QUERIES:
        got, _ = _measure(space, key2doc, case["query"])
        assert "d25" not in got and "d26" not in got


# --------------------------------------------------------------------------- #
# Planificateur adaptatif
# --------------------------------------------------------------------------- #

def test_or_is_kept_when_affordable(truth):
    """Le OU donne le meilleur rappel : on le garde tant qu'il tient."""
    space, _other, key2doc = truth
    _got, plan = _measure(space, key2doc, BENCHMARK_QUERIES[3]["query"])  # medium-1
    assert plan.strategy == "or"


def test_it_tightens_to_and_rather_than_truncating(truth):
    """Quand le OU explose, on RESSERRE (ET). On ne prend jamais « les N premiers »."""
    space, _other, key2doc = truth

    # Plafond très bas : le OU ne tient plus, le ET doit prendre le relais.
    got, plan = _measure(
        space, key2doc,
        "recherche dans toute la base les informations sur les délais de livraison",
        max_candidates=3,
    )
    assert plan.strategy == "and"
    assert plan.candidate_chunks <= 3
    # Les tentatives sont AUDITABLES : on voit ce qui a été essayé et rejeté.
    assert [a["strategy"] for a in plan.attempts] == ["or", "and"]


def test_it_refuses_when_nothing_fits(truth):
    """Aucune formulation ne tient -> refus. Jamais de troncature silencieuse."""
    space, _other, key2doc = truth
    with pytest.raises(QueryTooBroad):
        _measure(
            space, key2doc,
            "recherche dans toute la base les informations sur la sécurité",
            max_candidates=1,
        )


def test_a_single_very_common_term_is_refused(truth):
    """Un mot unique qui touche tout le corpus n'est pas un sujet recherchable."""
    space, _other, key2doc = truth
    with pytest.raises(QueryTooBroad):
        _measure(space, key2doc,
                 "analyse tout le corpus concernant la sécurité", max_candidates=1)


# --------------------------------------------------------------------------- #
# Construction de l'expression (fonctions pures)
# --------------------------------------------------------------------------- #

def test_quoted_phrases_are_preserved():
    phrases, rest = extract_quoted_phrases(
        'recherche dans toute la base "protection des données" et la sécurité'
    )
    assert phrases == ["protection des données"]
    assert "protection" not in rest


def test_phrase_becomes_an_adjacency_expression():
    expr = build_tsquery_expression(
        required=(), optional=(), phrases=("protection des données",), strategy="hybrid"
    )
    assert expr == "protection <-> des <-> données"


@pytest.mark.parametrize("strategy,expected", [
    ("or", "sécurité | incendie"),
    ("and", "sécurité & incendie"),
])
def test_strategies_build_the_right_operator(strategy, expected):
    expr = build_tsquery_expression(
        required=(), optional=("sécurité", "incendie"), strategy=strategy
    )
    assert expr == expected


def test_user_input_can_never_inject_an_operator():
    """Les JETONS sont assainis : aucun opérateur ne peut venir de l'utilisateur.

    Les opérateurs présents dans l'expression finale sont les NÔTRES (`|`, `&`,
    `<->`) : c'est nous qui construisons la formule booléenne, à partir de jetons
    déjà nettoyés.
    """
    expr = build_tsquery_expression(
        required=(), optional=("sécu&rité", "a|b", "x\'; DROP--", "c:*"),
        strategy="or",
    )
    # Les jetons, une fois isolés de nos séparateurs, ne contiennent AUCUN opérateur.
    tokens = [t.strip() for t in expr.split("|")]
    for token in tokens:
        for op in ("&", "|", "!", ":", "*", ";", "\'", "--", "<->"):
            assert op not in token, f"opérateur {op!r} injecté dans le jeton {token!r}"


def test_accents_are_preserved_in_terms():
    """PostgreSQL ne déplie pas les accents : les désaccentuer viderait la
    recherche sur un corpus français, silencieusement."""
    gq = build_global_fts_query(
        "recherche dans toute la base les informations sur la cybersécurité",
        configuration=CFG,
    )
    assert "cybersécurité" in gq.terms


def test_the_planner_is_auditable(truth):
    """Le choix de formulation doit être traçable dans le résultat."""
    space, _other, key2doc = truth
    _got, plan = _measure(space, key2doc, BENCHMARK_QUERIES[0]["query"])
    assert plan.strategy in ("or", "and")
    assert plan.expression
    assert all("candidate_chunks" in a for a in plan.attempts)


# --------------------------------------------------------------------------- #
# Retry transitoire borné
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("exc", [
    TimeoutError("read timed out"),
    ConnectionError("connection refused"),
    RuntimeError("HTTP 503 Service Unavailable"),
    RuntimeError("502 bad gateway"),
    RuntimeError("429 too many requests"),
])
def test_transient_errors_are_retryable(exc):
    assert gs.is_transient_error(exc) is True


@pytest.mark.parametrize("exc", [
    ValueError("batch_invalid_json"),
    gs.GlobalSearchError("invalid_unsourced_result"),
    RuntimeError("model produced garbage"),
])
def test_non_transient_errors_are_never_retried(exc):
    """Rejouer un JSON invalide ou une source inventée ne changerait rien et
    coûterait deux fois."""
    assert gs.is_transient_error(exc) is False


def test_retry_recovers_from_a_transient_failure(monkeypatch, settings):
    settings.CORPUS_GLOBAL_SEARCH_RETRY_BACKOFF_S = [0, 0]
    calls = []

    def flaky(**kw):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("HTTP 503 Service Unavailable")
        return {"text": '{"items": []}', "provider": "s", "model": "m", "usage": {}}

    monkeypatch.setattr(gs, "_generate", flaky)
    monkeypatch.setattr(gs, "raise_if_cancel_requested", lambda _id: None)

    out = gs._generate_with_retry(prompt="p", rag_space=None, job_id="j")

    assert out["text"] == '{"items": []}'
    assert len(calls) == 2   # un échec, un succès


def test_retries_are_bounded(monkeypatch, settings):
    settings.CORPUS_GLOBAL_SEARCH_MAX_RETRIES = 2
    settings.CORPUS_GLOBAL_SEARCH_RETRY_BACKOFF_S = [0, 0]
    calls = []

    def always_down(**kw):
        calls.append(1)
        raise RuntimeError("HTTP 503 Service Unavailable")

    monkeypatch.setattr(gs, "_generate", always_down)
    monkeypatch.setattr(gs, "raise_if_cancel_requested", lambda _id: None)

    with pytest.raises(RuntimeError):
        gs._generate_with_retry(prompt="p", rag_space=None, job_id="j")

    assert len(calls) == 3   # 1 tentative + 2 retries, pas davantage


def test_cancellation_stops_the_retry(monkeypatch, settings):
    """Un job annulé ne doit pas continuer à dépenser en retries."""
    settings.CORPUS_GLOBAL_SEARCH_RETRY_BACKOFF_S = [0, 0]

    def always_down(**kw):
        raise RuntimeError("HTTP 503 Service Unavailable")

    def cancelled(_id):
        raise gs.AnalysisCancelled("j")

    monkeypatch.setattr(gs, "_generate", always_down)
    monkeypatch.setattr(gs, "raise_if_cancel_requested", cancelled)

    with pytest.raises(gs.AnalysisCancelled):
        gs._generate_with_retry(prompt="p", rag_space=None, job_id="j")


def test_retry_never_logs_batch_content(monkeypatch, settings, caplog):
    settings.CORPUS_GLOBAL_SEARCH_RETRY_BACKOFF_S = [0, 0]
    calls = []

    def flaky(**kw):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("HTTP 503")
        return {"text": '{"items": []}', "provider": "s", "model": "m", "usage": {}}

    monkeypatch.setattr(gs, "_generate", flaky)
    monkeypatch.setattr(gs, "raise_if_cancel_requested", lambda _id: None)

    with caplog.at_level("WARNING"):
        gs._generate_with_retry(
            prompt="TEXTE_CONFIDENTIEL_DU_LOT", rag_space=None, job_id="j"
        )

    assert "TEXTE_CONFIDENTIEL_DU_LOT" not in caplog.text
