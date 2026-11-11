"""Le reranking Gateway doit être racontable requête par requête.

Le défaut corrigé ici n'était pas une métrique manquante — c'était une trace qui
disait le CONTRAIRE de ce qui s'était passé. `query_rag` appelait
``trace.record_rerank_gateway(...)`` derrière un ``hasattr`` portant sur une méthode
qui n'a jamais existé : le garde était toujours faux, l'appel toujours sans effet.
Résultat du scénario de référence (données synthétiques) : 29 appels Gateway comptabilisés,
570 `RagQueryLog` inspectés, **zéro** portant la moindre trace de reranking — et le
leg historique affichant sereinement ``rerank: not_requested``.

Un audit qui affirme « non demandé » sur une requête rerankée est plus dangereux
qu'un audit muet : le premier ferme l'enquête, le second l'ouvre.
"""
from __future__ import annotations

import pytest

from apps.rag_core.services.query.trace import (
    LEG_RERANK,
    LEG_RERANK_GATEWAY,
    RERANK_GATEWAY_KEY,
    RerankGatewayStatus,
    RetrievalLegStatus,
    RetrievalTraceBuilder,
    read_gateway_rerank,
)


def _builder() -> RetrievalTraceBuilder:
    return RetrievalTraceBuilder(entrypoint="internal", request_id="req-1")


def _info(**surcharges):
    """`info` telle que `rerank_chunks` la rend, surchargeable par cas."""
    base = {
        "attempted": True, "success": True, "fallback": False,
        "documents": 40, "returned": 40, "duration_ms": 212.4, "pool_k": 40,
        "model_logical": "rerank-default", "model_revision": "953dc6f6",
        "policy": "fail_open_rrf", "error_kind": None,
        "movements": [
            {"chunk_id": "c-a", "document_id": "d-1", "before_rank": 12, "after_rank": 1, "delta": 11, "score": 0.87},
            {"chunk_id": "c-b", "document_id": "d-2", "before_rank": 1, "after_rank": 2, "delta": -1, "score": 0.41},
            {"chunk_id": "c-c", "document_id": "d-2", "before_rank": 3, "after_rank": 3, "delta": 0, "score": 0.22},
        ],
    }
    base.update(surcharges)
    return base


# ===========================================================================
# 1. La machine d'état
# ===========================================================================
@pytest.mark.parametrize(
    "info,attendu",
    [
        (_info(), RerankGatewayStatus.SUCCESS),
        (_info(attempted=False, success=False, movements=[]), RerankGatewayStatus.NOT_REQUESTED),
        (_info(success=False, fallback=True, error_kind="timeout", movements=[]), RerankGatewayStatus.TIMEOUT),
        (_info(success=False, fallback=True, error_kind="cardinalite 3/4", movements=[]), RerankGatewayStatus.INVALID_RESPONSE),
        (_info(success=False, fallback=True, error_kind="HTTP 503", movements=[]), RerankGatewayStatus.FAILURE),
    ],
)
def test_the_state_machine_covers_every_outcome(info, attendu):
    b = _builder()
    assert b.record_rerank_gateway(info).status is attendu


def test_a_timeout_is_not_a_generic_failure():
    """Les compteurs distinguaient déjà les deux ; la trace le devait aussi.

    Une Gateway lente et une réponse non conforme ne se corrigent pas de la même
    façon. Le lot précédent a observé en staging 18 replis, 1 dépassement de délai et
    4 échecs — sans pouvoir rattacher AUCUN de ces événements à une requête.
    """
    b = _builder()
    delai = b.record_rerank_gateway(_info(success=False, fallback=True, error_kind="timeout", movements=[]))
    autre = _builder().record_rerank_gateway(
        _info(success=False, fallback=True, error_kind="HTTP 502", movements=[])
    )
    assert delai.status is RerankGatewayStatus.TIMEOUT
    assert autre.status is RerankGatewayStatus.FAILURE
    assert delai.status is not autre.status


def test_fallback_is_a_separate_axis_from_the_outcome():
    """`success=true` ET `fallback=true` ne doivent jamais coexister.

    Le statut dit ce qui est arrivé à l'APPEL, `fallback` ce que le pipeline en a
    fait. Un état « FALLBACK » écraserait la cause — précisément ce qu'on cherche.
    """
    succes = _builder().record_rerank_gateway(_info())
    assert succes.status is RerankGatewayStatus.SUCCESS and succes.fallback is False

    repli = _builder().record_rerank_gateway(
        _info(success=False, fallback=True, error_kind="timeout", movements=[])
    )
    assert repli.status is RerankGatewayStatus.TIMEOUT and repli.fallback is True


# ===========================================================================
# 2. Les deux rerankers ne se confondent plus
# ===========================================================================
def test_the_two_rerankers_occupy_distinct_legs():
    """Le cas exact observé en production : Gateway actif, historique éteint."""
    b = _builder()
    b.record_leg(LEG_RERANK, RetrievalLegStatus.NOT_REQUESTED)   # historique, comme en prod
    b.record_rerank_gateway(_info())

    legs = b.build(final_candidate_count=120, source_count=6).summary()["legs"]

    assert legs[LEG_RERANK] == "not_requested"
    assert legs[LEG_RERANK_GATEWAY] == "succeeded"


def test_a_reranked_request_can_no_longer_read_as_not_requested():
    """L'invariant de sortie du lot, énoncé comme une interdiction.

    Il ne doit plus exister de trace où le seul mot « rerank » soit
    « not_requested » alors qu'un appel Gateway a eu lieu.
    """
    b = _builder()
    b.record_leg(LEG_RERANK, RetrievalLegStatus.NOT_REQUESTED)
    b.record_rerank_gateway(_info())

    resume = b.build(final_candidate_count=120, source_count=6).summary()
    etats_rerank = {k: v for k, v in resume["legs"].items() if "rerank" in k}

    assert etats_rerank != {LEG_RERANK: "not_requested"}
    assert resume[RERANK_GATEWAY_KEY]["status"] == "success"
    assert resume[RERANK_GATEWAY_KEY]["attempted"] is True


def test_a_fallback_is_marked_as_a_degradation():
    """Servir l'ordre RRF en croyant servir un classement reranké EST une dégradation.

    Le leg historique le fait déjà pour son propre repli : le leg Gateway suit la
    même doctrine plutôt que d'en inventer une seconde.
    """
    b = _builder()
    b.record_rerank_gateway(_info(success=False, fallback=True, error_kind="timeout", movements=[]))
    trace = b.build(final_candidate_count=120, source_count=6)

    assert trace.degraded is True
    assert "rerank_gateway_fallback" in trace.fallback_reasons


def test_a_success_does_not_degrade_the_retrieval():
    b = _builder()
    b.record_rerank_gateway(_info())
    assert b.build(final_candidate_count=120, source_count=6).degraded is False


# ===========================================================================
# 3. Mouvements de rang
# ===========================================================================
def test_movements_carry_the_ranks_and_the_delta():
    trace = _builder().record_rerank_gateway(_info())
    par_id = {m["chunk_id"]: m for m in trace.movements}

    assert par_id["c-a"]["before_rank"] == 12 and par_id["c-a"]["after_rank"] == 1
    assert par_id["c-a"]["delta"] == 11, "positif = remonté"
    assert par_id["c-b"]["delta"] == -1, "négatif = descendu"
    assert par_id["c-c"]["delta"] == 0


def test_aggregates_are_derived_not_stored():
    trace = _builder().record_rerank_gateway(_info())
    assert trace.moved_count == 2, "seuls deux candidats ont bougé"
    assert trace.top1_changed is True


def test_top1_unchanged_is_reported_as_such():
    """Un reranking qui ne déplace jamais le premier candidat coûte sans agir."""
    immobile = _info(movements=[
        {"chunk_id": "c-a", "before_rank": 1, "after_rank": 1, "delta": 0, "score": 0.9},
        {"chunk_id": "c-b", "before_rank": 2, "after_rank": 2, "delta": 0, "score": 0.5},
    ])
    trace = _builder().record_rerank_gateway(immobile)
    assert trace.top1_changed is False and trace.moved_count == 0


def test_a_missing_score_is_absent_never_zero():
    """`0` se lirait « jugé non pertinent ». L'absence doit rester une absence."""
    sans_score = _info(movements=[{"chunk_id": "c-a", "before_rank": 2, "after_rank": 1, "delta": 1}])
    trace = _builder().record_rerank_gateway(sans_score)
    assert "score" not in trace.movements[0]


# ===========================================================================
# 4. Survie au contexte final
# ===========================================================================
def test_final_context_survival_is_counted_after_selection():
    b = _builder()
    b.record_rerank_gateway(_info())
    b.record_rerank_gateway_survival(["c-a", "c-c", "autre-chunk"])

    assert b.rerank_gateway.survived_in_final_context == 2


def test_survival_is_unknown_until_it_is_recorded():
    """`None`, pas `0` : personne n'a encore regardé."""
    trace = _builder().record_rerank_gateway(_info())
    assert trace.survived_in_final_context is None


def test_survival_without_any_reranking_records_nothing():
    b = _builder()
    b.record_rerank_gateway_survival(["c-a"])
    assert b.rerank_gateway is None


# ===========================================================================
# 5. Compatibilité des traces antérieures
# ===========================================================================
def test_an_old_trace_reads_as_not_recorded_never_as_not_requested():
    """La distinction qui compte : « n'a pas eu lieu » ≠ « on ne sait pas ».

    Les 570 logs de production existants n'ont pas ce leg. Les lire comme
    `not_requested` reviendrait à répondre à une question que personne n'a posée.
    """
    ancienne = {"schema_version": 1, "legs": {"fts": "succeeded", "rerank": "not_requested"}}
    lu = read_gateway_rerank(ancienne)

    assert lu["status"] == RerankGatewayStatus.NOT_RECORDED.value
    assert lu["recorded"] is False
    assert lu["status"] != RerankGatewayStatus.NOT_REQUESTED.value


@pytest.mark.parametrize("entree", [None, {}, [], "", {"rerank_gateway": None}, {"rerank_gateway": {}}])
def test_reading_a_malformed_trace_never_raises(entree):
    """Un lecteur d'audit ne doit pas planter sur une trace ancienne ou tronquée."""
    assert read_gateway_rerank(entree)["recorded"] is False


def test_a_recorded_trace_reads_as_recorded():
    b = _builder()
    b.record_rerank_gateway(_info())
    lu = read_gateway_rerank(b.build(final_candidate_count=1, source_count=1).summary())
    assert lu["recorded"] is True and lu["status"] == "success"


def test_the_schema_version_is_unchanged_because_the_addition_is_additive():
    """Ajouter une clé ne casse aucun lecteur : la version ne bouge donc pas.

    L'incrémenter ferait croire à un changement INCOMPATIBLE et invaliderait la
    lecture des traces existantes, qui restent parfaitement valides.
    """
    from apps.rag_core.services.query.trace import RETRIEVAL_TRACE_SCHEMA_VERSION

    assert RETRIEVAL_TRACE_SCHEMA_VERSION == 1


# ===========================================================================
# 6. Confidentialité
# ===========================================================================
def test_no_business_content_reaches_the_trace():
    """Identifiants, rangs, scores, durées — jamais de texte.

    On injecte des contenus sentinelles dans l'`info` : aucun ne doit ressortir.
    """
    pollue = _info()
    pollue["question"] = "SENTINELLE-QUESTION"
    pollue["prompt"] = "SENTINELLE-PROMPT"
    pollue["movements"] = [
        {"chunk_id": "c-a", "before_rank": 2, "after_rank": 1, "delta": 1, "score": 0.9,
         "text": "SENTINELLE-TEXTE", "content": "SENTINELLE-CONTENU"},
    ]
    pollue["base_url"] = "http://gateway-privee.interne:8200"

    b = _builder()
    b.record_rerank_gateway(pollue)
    rendu = str(b.build(final_candidate_count=1, source_count=1).to_dict())

    for sentinelle in ("SENTINELLE-QUESTION", "SENTINELLE-PROMPT", "gateway-privee", "://"):
        assert sentinelle not in rendu, f"fuite : {sentinelle}"


def test_movement_fields_are_a_closed_list():
    """Le champ `movements` vient du client, mais la trace ne recopie que ce qu'elle
    déclare : un champ inattendu ne doit pas se retrouver persisté par accident."""
    from apps.rag_core.services import rerank_gateway as g

    head = [{"chunk_id": "a", "document_id": "d", "text": "SENTINELLE"},
            {"chunk_id": "b", "document_id": "d", "text": "SENTINELLE"}]
    reordonne = [
        {"chunk_id": "b", "document_id": "d", "text": "SENTINELLE", "rerank_rank": 1, "rerank_score": 0.9},
        {"chunk_id": "a", "document_id": "d", "text": "SENTINELLE", "rerank_rank": 2, "rerank_score": 0.1},
    ]
    mouvements = g._movements(head, reordonne)

    assert {k for m in mouvements for k in m} <= {
        "chunk_id", "document_id", "before_rank", "after_rank", "delta", "score",
    }
    assert "SENTINELLE" not in str(mouvements)


# ===========================================================================
# 7. Volumétrie bornée
# ===========================================================================
def test_the_movement_list_is_bounded_by_the_submitted_pool():
    """Le pool est plafonné par le contrat Gateway (64 au registre) : la trace hérite
    de cette borne. Aucune croissance libre."""
    from apps.rag_core.services import rerank_gateway as g

    head = [{"chunk_id": f"c{i}", "document_id": "d"} for i in range(64)]
    reordonne = [{**c, "rerank_rank": i + 1, "rerank_score": 1.0} for i, c in enumerate(head)]

    assert len(g._movements(head, reordonne)) == 64

    from config import registry
    assert registry.require("RAG_RERANKER_POOL_K").maximum == 64


# ===========================================================================
# 8. Le motif qui a rendu le défaut invisible ne doit pas revenir
# ===========================================================================
# `hasattr(trace, "record_rerank_gateway")` n'était pas une précaution : c'était un
# interrupteur qui, la méthode n'ayant jamais existé, a éteint l'observabilité du
# reranking pendant toute sa vie en production — sans un seul message d'erreur.
# Un garde de ce type autour d'une méthode CONTRACTUELLE transforme un défaut de code
# en silence, et c'est exactement ce qu'on ne veut plus.
def _appels_hasattr_sur_la_trace() -> list[str]:
    """Relu du source : `hasattr(<qqch>, "record_*")` dans les modules du pipeline."""
    import ast
    import pathlib

    racine = pathlib.Path(__file__).resolve().parents[3] / "backend/apps/rag_core/services/query"
    trouves: list[str] = []
    for chemin in sorted(racine.glob("*.py")):
        arbre = ast.parse(chemin.read_text(encoding="utf-8"))
        for noeud in ast.walk(arbre):
            if (
                isinstance(noeud, ast.Call)
                and isinstance(noeud.func, ast.Name)
                and noeud.func.id == "hasattr"
                and len(noeud.args) == 2
                and isinstance(noeud.args[1], ast.Constant)
                and isinstance(noeud.args[1].value, str)
                and noeud.args[1].value.startswith("record_")
            ):
                trouves.append(f"{chemin.name}:{noeud.lineno} -> {noeud.args[1].value}")
    return trouves


def test_no_hasattr_guard_around_a_contractual_trace_method():
    trouves = _appels_hasattr_sur_la_trace()
    assert not trouves, (
        "un `hasattr` autour d'une méthode `record_*` retransforme une perte "
        f"d'observabilité en silence : {trouves}"
    )


def test_the_recorder_is_contractual_on_the_builder():
    """Si la méthode disparaît, ce test rougit — au lieu du silence d'un `hasattr`."""
    for nom in ("record_rerank_gateway", "record_rerank_gateway_survival"):
        assert callable(getattr(RetrievalTraceBuilder, nom, None)), (
            f"{nom} fait partie du contrat de la trace"
        )


def test_the_orchestrator_calls_the_recorder_unconditionally():
    """Relu du source : l'appel ne doit plus dépendre d'une introspection."""
    import ast
    import pathlib

    chemin = (
        pathlib.Path(__file__).resolve().parents[3]
        / "backend/apps/rag_core/services/query/orchestrator.py"
    )
    source = chemin.read_text(encoding="utf-8")
    arbre = ast.parse(source)
    fonction = next(
        n for n in ast.walk(arbre)
        if isinstance(n, ast.FunctionDef) and n.name == "query_rag"
    )
    appels = [
        n.func.id for n in ast.walk(fonction)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    ]
    assert "_record_gateway_rerank" in appels
    assert "_record_gateway_rerank_survival" in appels


# ===========================================================================
# 9. Compteurs agrégés et trace par requête doivent raconter la même chose
# ===========================================================================
def test_the_counters_and_the_trace_agree_on_a_simulated_request(monkeypatch):
    """Les compteurs restent la vérité AGRÉGÉE, la trace la vérité PAR REQUÊTE.

    On ne réconcilie pas le passé — l'information n'existe pas. Mais sur une requête
    neuve, `attempted += 1` et `attempted=true` doivent être deux façons de dire le
    même fait, sans quoi les deux surfaces divergeraient dès la première panne.
    """
    from apps.rag_core.services import ops_counters, rerank_gateway as g

    vus: dict[str, int] = {}
    monkeypatch.setattr(ops_counters, "incr_many", lambda pending: vus.update(pending))
    monkeypatch.setattr(
        g, "_post_rerank",
        lambda question, docs: (
            {"results": [{"id": d["id"], "relevance_score": float(len(docs) - i)}
                         for i, d in enumerate(docs)]},
            12.0, 200,
        ),
    )
    chunks = [
        {"chunk_id": "a", "document_id": "d", "text": "x", "fusion_score": 0.03, "score": 0.5},
        {"chunk_id": "b", "document_id": "d", "text": "y", "fusion_score": 0.02, "score": 0.4},
    ]
    _, info = g.rerank_chunks(question="q", chunks=chunks)
    trace = _builder().record_rerank_gateway(info)

    assert vus.get("rag_reranker_attempted_total") == 1 and trace.attempted is True
    assert vus.get("rag_reranker_success_total") == 1 and trace.status is RerankGatewayStatus.SUCCESS
    assert vus.get("rag_reranker_fallback_total") is None and trace.fallback is False
    assert vus.get("rag_reranker_documents_total") == trace.documents_submitted


def test_a_fallback_agrees_on_both_surfaces(monkeypatch):
    from apps.rag_core.services import ops_counters, rerank_gateway as g

    vus: dict[str, int] = {}
    monkeypatch.setattr(ops_counters, "incr_many", lambda pending: vus.update(pending))

    def _boum(question, docs):
        raise g.RerankGatewayError("timeout")

    monkeypatch.setattr(g, "_post_rerank", _boum)
    chunks = [{"chunk_id": "a", "document_id": "d", "text": "x", "fusion_score": 0.03, "score": 0.5}]
    _, info = g.rerank_chunks(question="q", chunks=chunks)
    trace = _builder().record_rerank_gateway(info)

    assert vus.get("rag_reranker_timeout_total") == 1
    assert trace.status is RerankGatewayStatus.TIMEOUT
    assert vus.get("rag_reranker_fallback_total") == 1 and trace.fallback is True


# ===========================================================================
# 10. Deux surfaces, deux besoins
# ===========================================================================
def _trace_pleine(n=40):
    b = _builder()
    mouvements = [
        {"chunk_id": f"{i:032d}", "document_id": f"{i:032d}",
         "before_rank": i + 1, "after_rank": n - i, "delta": 2 * i - n, "score": 0.5}
        for i in range(n)
    ]
    b.record_rerank_gateway(_info(documents=n, returned=n, movements=mouvements))
    b.record_rerank_gateway_survival([m["chunk_id"] for m in mouvements[:6]])
    return b.build(final_candidate_count=120, source_count=6)


def test_the_movements_are_persisted_but_not_returned_to_the_caller():
    """Le détail vit dans l'audit, pas dans la charge utile du chat.

    `summary()` est ce que `RagQueryLog.retrieval` persiste — la seule surface où une
    campagne pourra relire les déplacements. `to_dict()` part dans la réponse de
    `query_rag` : y verser quarante mouvements coûterait ~7 ko par requête à un
    consommateur qui n'en a aucun usage.
    """
    trace = _trace_pleine()

    persiste = trace.summary()[RERANK_GATEWAY_KEY]
    rendu = trace.to_dict()[RERANK_GATEWAY_KEY]

    assert len(persiste["movements"]) == 40
    assert "movements" not in rendu
    # …mais l'état et les agrégats sont dans les DEUX : savoir ce qui s'est passé ne
    # doit jamais exiger d'aller lire la base.
    for champ in ("status", "attempted", "fallback", "moved_count", "top1_changed",
                  "duration_ms", "survived_in_final_context"):
        assert persiste[champ] == rendu[champ]


def test_the_persisted_trace_stays_within_a_few_kilobytes():
    """Borne de volumétrie, vérifiée plutôt que supposée.

    Le plafond du contrat Gateway (64 documents) borne mécaniquement la trace. Ce
    test fixe l'ordre de grandeur pour qu'une dérive future se voie.
    """
    import json

    persiste = len(json.dumps(_trace_pleine(40).summary(), ensure_ascii=False).encode())
    maximum = len(json.dumps(_trace_pleine(64).summary(), ensure_ascii=False).encode())
    rendu = len(json.dumps(_trace_pleine(40).to_dict(), ensure_ascii=False).encode())

    assert persiste < 12_000, f"ligne d'audit trop lourde : {persiste} o"
    assert maximum < 16_000, f"pire cas (pool 64) trop lourd : {maximum} o"
    assert rendu < 2_000, f"réponse API alourdie sans besoin : {rendu} o"
