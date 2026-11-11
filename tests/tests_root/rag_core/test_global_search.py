"""L4 — moteur de recherche globale : FTS, lots, sources, couverture, concurrence."""
import json
import re

import pytest

from apps.rag_core.analysis import global_search as gs
from apps.rag_core.analysis.concurrency import ConcurrencyLimitReached
from apps.rag_core.analysis.registry import COVERAGE_SNAPSHOT, GLOBAL_SEARCH, get_analysis_type
from apps.rag_core.analysis.runner import execute_job
from apps.rag_core.analysis.service import create_analysis_job
from apps.rag_core.models import Chunk, Document, RagSpace
from apps.rag_core.models.corpus_analysis_job import AnalysisStatus, CorpusAnalysisJob
from apps.rag_core.models.document import DocumentStatus
from apps.rag_core.repositories import global_search_repository as repo

pytestmark = pytest.mark.django_db

QUERY = "recherche dans toute la base les informations sur la cybersécurité"
RELEVANT = [
    "La cybersécurité du système est renforcée par un pare-feu.",
    "Le plan de cybersécurité prévoit un audit annuel.",
    "La cybersécurité reste une priorité budgétaire.",
]
IRRELEVANT = ["Le catalogue produit contient douze références.", "La cantine ouvre à midi."]


def _doc(space, *, status=DocumentStatus.READY, name="d.txt", sha="a"):
    return Document.objects.create(rag_space=space, filename=name, status=status, sha256=sha)


def _chunk(space, doc, text, *, idx=0, page=1):
    return Chunk.objects.create(
        rag_space=space, document=doc, chunk_index=idx, text=text, page_number=page
    )


@pytest.fixture
def corpus(rag_space):
    for i, t in enumerate(RELEVANT + IRRELEVANT):
        d = _doc(rag_space, name=f"d{i}.txt", sha=f"s{i}")
        _chunk(rag_space, d, t, idx=i)
    _doc(rag_space, name="ko.txt", status=DocumentStatus.FAILED, sha="")
    return rag_space


@pytest.fixture
def fake_llm(monkeypatch):
    """LLM factice : cite un vrai chunk, en invente un, et en produit un sans source."""
    calls = []

    def fake(**kw):
        ids = re.findall(r"\[([0-9a-f\-]{36})\]", kw["prompt"])
        calls.append(ids)
        items = [
            {"statement": "La cybersécurité fait l’objet d’un audit.",
             "source_chunk_ids": ids[:1], "confidence": 0.9},
            {"statement": "Fait halluciné.",
             "source_chunk_ids": ["00000000-0000-0000-0000-000000000000"], "confidence": 0.9},
            {"statement": "Sans source.", "source_chunk_ids": [], "confidence": 0.9},
        ] if ids else []
        return {"text": json.dumps({"items": items}), "provider": "stub", "model": "m",
                "usage": {"prompt_tokens": 100, "completion_tokens": 20}}

    monkeypatch.setattr(gs, "_generate", lambda **kw: fake(**kw))
    return calls


def _run(user, space, query=QUERY, request_id="rid"):
    out = create_analysis_job(
        user=user, rag_space=space, analysis_type=GLOBAL_SEARCH,
        parameters={"query": query, "language": "auto"}, request_id=request_id,
    )
    execute_job(out.job.id)
    return CorpusAnalysisJob.objects.get(pk=out.job.id)


# --------------------------------------------------------------------------- #
# FTS1..FTS10
# --------------------------------------------------------------------------- #

def test_scan_covers_the_whole_scope_not_only_candidates(corpus):
    """FTS1 — chunks_scanned = TOUS les chunks READY, pas seulement les candidats."""
    scope = repo.scan_scope(rag_space_id=corpus.id)
    assert scope["chunks_scanned"] == 5          # 3 pertinents + 2 non pertinents
    assert scope["documents_text_scanned"] == 5

    counts = repo.count_global_fts_candidates(
        rag_space_id=corpus.id, query="cybersécurité", configuration="french"
    )
    assert counts["candidate_chunks"] == 3       # strictement moins que le scan


def test_rag_space_isolation(corpus, workspace):
    """FTS2 — un chunk identique dans un autre espace n'est ni compté ni retourné."""
    other = RagSpace.objects.create(name="autre", workspace=workspace)
    d = _doc(other, name="o.txt", sha="o")
    _chunk(other, d, "La cybersécurité est essentielle.")

    scope = repo.scan_scope(rag_space_id=corpus.id)
    counts = repo.count_global_fts_candidates(
        rag_space_id=corpus.id, query="cybersécurité", configuration="french"
    )
    assert scope["chunks_scanned"] == 5          # pas 6
    assert counts["candidate_chunks"] == 3       # pas 4

    ids = [c["chunk_id"] for c in repo.iterate_global_fts_candidates(
        rag_space_id=corpus.id, query="cybersécurité", configuration="french")]
    foreign = {str(c.id) for c in Chunk.objects.filter(rag_space=other)}
    assert not (set(ids) & foreign)


def test_non_ready_documents_are_never_candidates(rag_space):
    """FTS3 — les chunks d'un document non READY ne deviennent pas candidats."""
    d = _doc(rag_space, name="ing.txt", status=DocumentStatus.INGESTING, sha="i")
    _chunk(rag_space, d, "La cybersécurité est un sujet clé.")

    assert repo.scan_scope(rag_space_id=rag_space.id)["chunks_scanned"] == 0
    assert repo.count_global_fts_candidates(
        rag_space_id=rag_space.id, query="cybersécurité", configuration="french"
    )["candidate_chunks"] == 0


def test_zero_candidates_calls_no_llm(rag_space, user, fake_llm):
    """FTS4 — aucun candidat : aucun appel LLM, job succeeded, message honnête."""
    d = _doc(rag_space, sha="a")
    _chunk(rag_space, d, "Le catalogue produit contient douze références.")

    job = _run(user, rag_space)

    assert job.status == AnalysisStatus.SUCCEEDED
    assert fake_llm == []                    # LLM jamais appelé
    assert job.tokens_in == 0
    assert job.result["items"] == []
    assert job.result["search"]["candidate_chunks"] == 0
    assert job.result["search"]["chunks_scanned"] == 1  # le scan, lui, a bien eu lieu

    md = job.result["summary_markdown"]
    assert "Aucune correspondance lexicale" in md
    # Ne jamais conclure que le sujet est ABSENT.
    assert "ne prouve pas l’absence du sujet" in md


@pytest.mark.parametrize("page_size", [1, 2, 3, 7, 200])
def test_pagination_loses_and_duplicates_nothing(corpus, page_size):
    """FTS6 — keyset : quelle que soit la taille de page, ni perte ni doublon."""
    total = repo.count_global_fts_candidates(
        rag_space_id=corpus.id, query="cybersécurité", configuration="french"
    )["candidate_chunks"]

    ids = [c["chunk_id"] for c in repo.iterate_global_fts_candidates(
        rag_space_id=corpus.id, query="cybersécurité",
        configuration="french", page_size=page_size)]

    assert len(ids) == total          # aucune perte
    assert len(set(ids)) == len(ids)  # aucun doublon


@pytest.mark.parametrize("hostile", [
    "'; DROP TABLE rag_chunk; --",
    'a" OR "1"="1',
    "x & | ! ( ) : * <->",
    "l'apostrophe",
    "\\\\; SELECT pg_sleep(10)",
])
def test_hostile_queries_never_inject(corpus, hostile):
    """FTS7 — la requête n'est jamais concaténée dans le SQL."""
    repo.count_global_fts_candidates(
        rag_space_id=corpus.id, query=hostile, configuration="french"
    )
    list(repo.iterate_global_fts_candidates(
        rag_space_id=corpus.id, query=hostile, configuration="french"))
    # Les tables sont intactes.
    assert Chunk.objects.count() >= 5
    assert Document.objects.count() >= 6


def test_fts_configuration_strategy():
    """FTS8 — seules les configurations RÉELLEMENT indexées sont acceptées."""
    assert repo.resolve_fts_configuration(language="french") == "french"
    assert repo.resolve_fts_configuration(language="simple") == "simple"
    # Une langue sans index GIN retomberait en seq scan : refusée -> 'simple'.
    assert repo.resolve_fts_configuration(language="english") == "simple"
    assert repo.resolve_fts_configuration(language=None) == "simple"
    assert repo.resolve_fts_configuration(language="'; DROP--") == "simple"


def test_topic_is_required(rag_space, user):
    """FTS9 — une demande globale SANS sujet ne peut pas être recherchée."""
    for no_topic in ("analyse tous les documents", "résume toute la base",
                     "parcours tout le corpus", ""):
        assert not gs.has_topic(no_topic), no_topic
        with pytest.raises(gs.TopicRequired):
            gs.validate_query(no_topic)

    for with_topic in (QUERY, "analyse dans tous les documents les retards de livraison"):
        assert gs.has_topic(with_topic)


def test_fts_query_is_built_on_topic_terms_with_accents():
    """La requête lexicale porte sur le SUJET, accents conservés.

    Passer la question entière ferait un ET de tous ses mots (« recherche » ET
    « base » ET…) : zéro candidat. Désaccentuer produirait aussi zéro candidat,
    car PostgreSQL ne déplie pas les accents.
    """
    assert gs.build_fts_query(QUERY) == "cybersécurité"
    assert "é" in gs.build_fts_query(QUERY)
    assert gs.build_fts_query("analyse tous les documents") == ""


def test_sql_budget_does_not_grow_per_chunk(corpus, django_assert_max_num_queries):
    """FTS10 — pas de N+1 : le scan et le comptage sont deux requêtes agrégées."""
    with django_assert_max_num_queries(2):
        repo.scan_scope(rag_space_id=corpus.id)
        repo.count_global_fts_candidates(
            rag_space_id=corpus.id, query="cybersécurité", configuration="french")


def test_no_conversational_top_k_in_the_engine():
    """Le moteur n'utilise JAMAIS le retrieval conversationnel (LIMIT top_k=8).

    Vérifié sur le CODE, commentaires exclus : le repository documente justement
    pourquoi il n'utilise pas `search_chunks_hybrid`.
    """
    import ast

    banned = {"search_chunks_hybrid", "RAG_TOP_K", "RAG_VECTOR_TOP_K", "top_k"}

    for module in (gs, repo):
        tree = ast.parse(open(module.__file__).read())
        names = {
            n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
        } | {
            n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
        } | {
            kw.arg for n in ast.walk(tree)
            if isinstance(n, ast.Call) for kw in n.keywords if kw.arg
        } | {
            n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        }
        leaked = banned & names
        assert not leaked, f"{module.__name__} : retrieval conversationnel utilisé {leaked}"


def test_partial_failure_never_claims_zero_matches(corpus, user, monkeypatch):
    """Le rendu ne doit JAMAIS dire « aucune correspondance » après une panne de
    traitement : le préfiltre AVAIT trouvé des candidats. Ce serait un mensonge."""
    monkeypatch.setattr(gs, "_generate",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("down")))

    job = _run(user, corpus)
    md = job.result["summary_markdown"]

    assert "Aucune correspondance lexicale" not in md
    assert "Analyse partielle" in md
    assert "3 extraits candidats" in md
    assert "aucune conclusion ne peut être tirée" in md.lower()


# --------------------------------------------------------------------------- #
# LLM1..LLM10
# --------------------------------------------------------------------------- #

def test_valid_structure_is_accepted():
    allowed = {"a-1", "b-2"}
    out = gs.parse_batch_output(
        json.dumps({"items": [
            {"statement": "Fait A.", "source_chunk_ids": ["a-1"], "confidence": 0.8}]}),
        allowed_chunk_ids=allowed, max_items=10)
    assert len(out) == 1 and out[0]["source_chunk_ids"] == ["a-1"]


@pytest.mark.parametrize("bad", ["pas du json", "", "{items: [}", "[1,2,3]"])
def test_invalid_json_raises(bad):
    """LLM2 — un JSON inexploitable lève : le lot est compté en échec, jamais ignoré."""
    with pytest.raises(ValueError):
        gs.parse_batch_output(bad, allowed_chunk_ids={"a"}, max_items=10)


def test_invented_source_is_rejected():
    """LLM3 — un chunk_id non transmis est une source INVENTÉE."""
    out = gs.parse_batch_output(
        json.dumps({"items": [
            {"statement": "Halluciné.", "source_chunk_ids": ["jamais-transmis"]},
            {"statement": "Vrai.", "source_chunk_ids": ["a-1", "jamais-transmis"]},
        ]}),
        allowed_chunk_ids={"a-1"}, max_items=10)

    assert len(out) == 1                          # l'item 100 % halluciné est écarté
    assert out[0]["source_chunk_ids"] == ["a-1"]  # la source inventée est retirée


def test_item_without_source_is_rejected():
    out = gs.parse_batch_output(
        json.dumps({"items": [{"statement": "Sans source.", "source_chunk_ids": []}]}),
        allowed_chunk_ids={"a-1"}, max_items=10)
    assert out == []


def test_too_many_items_are_bounded():
    items = [{"statement": f"Fait {i}.", "source_chunk_ids": ["a-1"]} for i in range(50)]
    out = gs.parse_batch_output(json.dumps({"items": items}),
                                allowed_chunk_ids={"a-1"}, max_items=5)
    assert len(out) == 5


def test_external_fallback_is_off_by_default(corpus, user, monkeypatch, settings):
    """LLM6 — même si l'espace autorise un repli externe, L4 le coupe.

    Une analyse globale enverrait TOUT le corpus candidat chez un tiers : ce n'est
    pas le même contrat de confidentialité qu'une question isolée.
    """
    captured = {}
    corpus.allow_external_fallback = True
    corpus.save(update_fields=["allow_external_fallback"])

    from apps.llm_router import router

    def spy(**kw):
        captured.update(kw)
        return {"text": '{"items": []}', "provider": "stub", "model": "m", "usage": {}}

    monkeypatch.setattr(router, "generate_text", spy)

    assert settings.CORPUS_GLOBAL_SEARCH_ALLOW_EXTERNAL_FALLBACK is False
    _run(user, corpus)

    # Lot 1 : le refus est porté par le contexte d'egress, et il PRIME sur
    # l'opt-in de l'espace (positionné à True ci-dessus) — une analyse globale
    # n'a pas le même contrat de confidentialité qu'une question isolée.
    assert captured["context"].external_egress_allowed is False
    assert captured["context"].policy.policy_source == "system-local-only"


def test_gateway_unavailable_yields_partial_not_succeeded(corpus, user, monkeypatch):
    """LLM7 — un lot en échec produit `partial`, JAMAIS `succeeded`."""
    def boom(**kw):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(gs, "_generate", boom)

    job = _run(user, corpus)

    assert job.status == AnalysisStatus.PARTIAL
    assert job.result["search"]["batches_failed"] == 1
    assert job.coverage["coverage_complete"] is False
    assert "Analyse partielle" in job.result["summary_markdown"]


def test_cancellation_stops_the_next_batch(corpus, user, monkeypatch):
    """LLM8 — annulation entre lots : aucun lot suivant n'est lancé."""
    from django.utils import timezone

    calls = []

    def fake(**kw):
        calls.append(1)
        # On annule pendant le 1er lot.
        CorpusAnalysisJob.objects.filter(analysis_type=GLOBAL_SEARCH).update(
            cancel_requested_at=timezone.now())
        return {"text": '{"items": []}', "provider": "s", "model": "m", "usage": {}}

    monkeypatch.setattr(gs, "_generate", lambda **kw: fake(**kw))
    monkeypatch.setattr("django.conf.settings.CORPUS_GLOBAL_SEARCH_BATCH_CHUNKS", 1)

    job = _run(user, corpus)

    assert job.status == AnalysisStatus.CANCELLED
    assert len(calls) == 1          # 3 candidats -> 3 lots, mais un seul exécuté
    assert job.result is None       # aucune synthèse partielle publiée


def test_tokens_and_cost_are_persisted(corpus, user, fake_llm):
    """LLM9 — les tokens sont cumulés et persistés."""
    job = _run(user, corpus)
    assert job.tokens_in == 100 and job.tokens_out == 20


def test_engine_computes_no_embedding(corpus, user, monkeypatch, fake_llm):
    """LLM10 — le préfiltre est purement LEXICAL : aucun embedding."""
    from apps.llm_router import embeddings

    def boom(*a, **kw):
        raise AssertionError("FORBIDDEN_EMBEDDING")

    monkeypatch.setattr(embeddings, "embed_texts", boom, raising=True)

    job = _run(user, corpus)
    assert job.status == AnalysisStatus.SUCCEEDED

    # Preuve STRUCTURELLE : le graphe d'imports du moteur ne contient aucun
    # embedding, aucun retrieval vectoriel.
    src = open(gs.__file__).read() + open(repo.__file__).read()
    for banned in ("embed_texts", "embedding", "search_chunks_vector", "hnsw"):
        assert banned not in src, banned


def test_control_a_real_embeddings_path_would_raise(monkeypatch):
    """Contrôle négatif du patch ci-dessus."""
    from apps.llm_router import embeddings

    monkeypatch.setattr(
        embeddings, "embed_texts",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("FORBIDDEN_EMBEDDING")),
        raising=True)
    with pytest.raises(AssertionError, match="FORBIDDEN_EMBEDDING"):
        embeddings.embed_texts(["x"])


# --------------------------------------------------------------------------- #
# COV1..COV8
# --------------------------------------------------------------------------- #

def test_coverage_complete_when_everything_is_exploitable(rag_space, user, fake_llm):
    """COV1 — aucun exclu, tous les candidats traités -> couverture complète…"""
    for i, t in enumerate(RELEVANT):
        d = _doc(rag_space, name=f"d{i}.txt", sha=f"s{i}")
        _chunk(rag_space, d, t, idx=i)

    job = _run(user, rag_space)

    assert job.status == AnalysisStatus.SUCCEEDED
    assert job.coverage["coverage_complete"] is True
    assert job.coverage["documents_excluded"] == []

    # …mais le rendu ne promet JAMAIS l'exhaustivité sémantique.
    md = job.result["summary_markdown"].lower()
    assert "peut ne pas reconnaître toutes les formulations" in md
    for banned in ("toutes les informations ont été trouvées",
                   "aucune information n’a été manquée",
                   "exhaustivité sémantique"):
        assert banned not in md


def test_failed_document_makes_coverage_incomplete(corpus, user, fake_llm):
    """COV2 — un document en échec rend la couverture incomplète."""
    job = _run(user, corpus)
    assert job.coverage["coverage_complete"] is False
    assert any(e["reason"] == "extraction_failed"
               for e in job.coverage["documents_excluded"])


def test_too_broad_query_refuses_rather_than_truncating(corpus, user, settings, fake_llm):
    """Troncature INTERDITE — on REFUSE, on ne prend pas « les N premiers ».

    Le plafond réel est le nombre de LOTS : c'est lui qui fait le coût.
    """
    settings.CORPUS_GLOBAL_SEARCH_MAX_BATCHES = 1
    settings.CORPUS_GLOBAL_SEARCH_BATCH_CHUNKS = 1   # -> plafond = 1 candidat

    job = _run(user, corpus)                          # 3 candidats

    assert job.status == AnalysisStatus.FAILED
    assert job.error_code == "query_too_broad"
    assert job.result is None            # aucun résultat tronqué publié
    assert fake_llm == []                # ZÉRO appel LLM, zéro token
    assert job.tokens_in == 0
    assert "trop large" in job.error_message
    assert "Précisez le sujet" in job.error_message


def test_coverage_metrics_are_distinct(corpus, user, fake_llm):
    """COV6 — scan, candidats et traités sont TROIS chiffres différents."""
    job = _run(user, corpus)
    s = job.result["search"]

    assert s["chunks_scanned"] == 5      # tout le périmètre textuel
    assert s["candidate_chunks"] == 3    # retenus par le préfiltre
    assert s["processed_chunks"] == 3    # soumis au traitement
    assert s["chunks_scanned"] > s["candidate_chunks"]
    assert job.coverage["chunks_analyzed"] == 5  # le SCAN, pas les candidats


def test_every_item_has_a_real_source(corpus, user, fake_llm):
    """COV5 / citations — items_without_source vaut toujours 0."""
    job = _run(user, corpus)

    assert job.coverage["items_without_source"] == 0
    assert job.result["items"]
    real_ids = {str(c.id) for c in Chunk.objects.filter(rag_space=corpus)}
    for it in job.result["items"]:
        assert it["sources"]
        for s in it["sources"]:
            assert s["chunk_id"] in real_ids      # source RÉELLE, du bon espace
            assert "document_id" in s


def test_result_exposes_no_internals(corpus, user, fake_llm):
    job = _run(user, corpus)
    blob = json.dumps(job.result).lower()
    for banned in ("select ", "rag_chunk", "tsvector", "websearch", "postgres",
                   "ollama", "openai", "gateway", "queue", "worker", "prompt"):
        assert banned not in blob, banned


# --------------------------------------------------------------------------- #
# CONC1..CONC4
# --------------------------------------------------------------------------- #

def test_concurrency_limit_is_enforced(corpus, user, settings):
    """CONC1 — au-delà de la limite : aucun nouveau job coûteux."""
    settings.CORPUS_GLOBAL_SEARCH_MAX_CONCURRENT_PER_WORKSPACE = 1

    create_analysis_job(user=user, rag_space=corpus, analysis_type=GLOBAL_SEARCH,
                        parameters={"query": QUERY}, request_id="r1")

    with pytest.raises(ConcurrencyLimitReached):
        create_analysis_job(user=user, rag_space=corpus, analysis_type=GLOBAL_SEARCH,
                            parameters={"query": "les délais de livraison"},
                            request_id="r2")

    # Aucun job `pending` impossible à honorer n'a été créé.
    assert CorpusAnalysisJob.objects.filter(status="pending").count() == 1


def test_snapshot_is_exempt_from_the_quota(corpus, user, settings):
    """CONC3 — l'instantané SQL ne consomme pas le quota des analyses coûteuses."""
    settings.CORPUS_GLOBAL_SEARCH_MAX_CONCURRENT_PER_WORKSPACE = 1
    create_analysis_job(user=user, rag_space=corpus, analysis_type=GLOBAL_SEARCH,
                        parameters={"query": QUERY}, request_id="r1")

    out = create_analysis_job(user=user, rag_space=corpus,
                              analysis_type=COVERAGE_SNAPSHOT,
                              parameters={}, request_id="r2")
    assert out.created is True

    assert get_analysis_type(COVERAGE_SNAPSHOT).counts_against_concurrency is False
    assert get_analysis_type(GLOBAL_SEARCH).counts_against_concurrency is True


def test_a_slot_is_freed_when_a_job_terminates(corpus, user, settings, fake_llm):
    """CONC4 — une place se libère après passage à un statut terminal."""
    settings.CORPUS_GLOBAL_SEARCH_MAX_CONCURRENT_PER_WORKSPACE = 1

    first = create_analysis_job(user=user, rag_space=corpus, analysis_type=GLOBAL_SEARCH,
                                parameters={"query": QUERY}, request_id="r1")
    execute_job(first.job.id)  # -> succeeded

    out = create_analysis_job(user=user, rag_space=corpus, analysis_type=GLOBAL_SEARCH,
                              parameters={"query": "les délais de livraison"},
                              request_id="r2")
    assert out.created is True


def test_concurrency_uses_an_atomic_lock():
    """Le quota n'est pas un COUNT puis CREATE : il est pris sous advisory lock."""
    from apps.rag_core.analysis import concurrency

    src = open(concurrency.__file__).read()
    assert "pg_advisory_xact_lock" in src


# --------------------------------------------------------------------------- #
# Registre
# --------------------------------------------------------------------------- #

def test_global_search_is_registered_with_the_right_properties():
    d = get_analysis_type(GLOBAL_SEARCH)
    assert d.requires_llm is True
    assert d.requires_embeddings is False   # préfiltre purement lexical
    assert d.supports_cancel is True
    assert d.result_version == "1"


# --------------------------------------------------------------------------- #
# Sources auto-descriptives — le résultat est PERSISTÉ et relu plus tard.
# --------------------------------------------------------------------------- #

def test_sources_carry_the_document_name(corpus, user, fake_llm):
    """Sans `filename`, une source relue après coup n'est qu'un UUID : illisible.

    Le nom est FIGÉ dans le résultat, comme le font déjà les sources du chat.
    """
    job = _run(user, corpus)

    sources = [s for it in job.result["items"] for s in it["sources"]]
    assert sources, "le test n'a aucun intérêt si l'analyse ne cite rien"

    names = {Document.objects.get(pk=s["document_id"]).filename for s in sources}
    for s in sources:
        # Additif : les champs d'origine sont intacts.
        assert {"chunk_id", "document_id", "page", "filename"} <= set(s)
        assert s["filename"] in names
        assert s["filename"].endswith(".txt")


def test_document_name_survives_the_deletion_of_the_document(corpus, user, fake_llm):
    """Le nom est un instantané : supprimer le document ne le fait pas disparaître
    du résultat déjà produit — et ne casse pas la relecture."""
    job = _run(user, corpus)
    cited = {s["document_id"] for it in job.result["items"] for s in it["sources"]}
    assert cited

    Document.objects.filter(pk__in=cited).delete()

    job.refresh_from_db()
    sources = [s for it in job.result["items"] for s in it["sources"]]
    assert all(s["filename"] for s in sources)


def test_attach_document_names_tolerates_an_unknown_document():
    """Un document introuvable donne `None`, jamais une exception."""
    items = [{
        "id": "i-1", "statement": "x", "confidence": None,
        "sources": [{
            "chunk_id": "c-1",
            "document_id": "00000000-0000-0000-0000-000000000000",
            "page": 1,
        }],
    }]

    gs.attach_document_names(items)

    assert items[0]["sources"][0]["filename"] is None

