"""Lot 3 — identité de pipeline d'embeddings, client Gateway et filtrage gouverné.

Couvre :
  - §7  contrat et VALIDATIONS du client `/v1/embeddings` (toutes les gardes) ;
  - §6  routage par provider, sans détournement du chemin Ollama ;
  - §8  identités legacy vs gouvernées vs NULL ;
  - §11 filtrage SQL par famille de pipeline, et non-régression du chemin legacy ;
  - §14 budget de chunk réduit pour les espaces gouvernés uniquement.

Aucun réseau : on mocke `requests.post` à la frontière.
"""
from __future__ import annotations

import pytest

from apps.llm_router import embedding_pipeline as ep
from apps.llm_router import embeddings as emb
from apps.llm_router import embeddings_gateway as gw


# =========================================================================
# Réponse Gateway nominale — sert de base à toutes les mutations de test
# =========================================================================
# Identifiants OPAQUES au format du contrat Gateway v2 (`emb-v2:<empreinte>`) : la
# tâche n'y figure pas en clair, et ils ne se découpent pas. Les valeurs sont celles
# publiées par la Gateway staging le 2026-10-05 (contrôle de régression, pas source
# de vérité runtime : la référence est lue sur `/health/embeddings`).
DIGEST_FR_EN = "1a37926bf842899cbf90583e3932f3820548716d9d07661ba622199ffe95c552"
DIGEST_LEGACY = "0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f"
PIPELINE_IDS = {
    ("fr-en-retrieval", "query"): "emb-v2:da35d8329da5f8dc1b57d0b675fca563",
    ("fr-en-retrieval", "document"): "emb-v2:c8215aca68cbf785bf977a46ead4b23c",
    ("legacy-768", "query"): "emb-v2:3a49e14ec5df7088cebb7630fd1d27eb",
    ("legacy-768", "document"): "emb-v2:9e883f0fe1d326ef17f86027a1d4a5d8",
}
DIGESTS = {"fr-en-retrieval": DIGEST_FR_EN, "legacy-768": DIGEST_LEGACY}


def _registry_payload(models=("fr-en-retrieval", "legacy-768"), dim=8):
    """Réponse `/health/embeddings` telle que la Gateway la publie (extrait utile)."""
    return {
        "environment": "test",
        "models": [
            {
                "model_logical": m,
                "dimension": dim,
                "expected_digest": DIGESTS[m],
                "observed_digest": DIGESTS[m],
                "identity_verified": True,
                "pipeline_id_query": PIPELINE_IDS[(m, "query")],
                "pipeline_id_document": PIPELINE_IDS[(m, "document")],
                "state": "ready",
            }
            for m in models
        ],
    }


def _ok_payload(*, n=1, dim=8, task="document", model="fr-en-retrieval", digest=None):
    digest = digest or DIGESTS[model]
    return {
        "provider": "ai_gateway",
        "upstream_provider": "ollama",
        "model": "granite-embedding:278m",
        "dimension": dim,
        "embeddings": [[0.5] * dim for _ in range(n)],
        "usage": {"total_texts": n},
        "cost": 0.0,
        "governed": True,
        "model_logical": model,
        "model_digest": digest,
        "expected_digest": digest,
        "identity_verified": True,
        "pipeline_id": PIPELINE_IDS[(model, task)],
        "task": task,
        "normalized": True,
        "truncated": [False] * n,
        "upstream_endpoint": "/api/embed",
    }


class _Resp:
    def __init__(self, payload, status=200, text=""):
        self._payload, self.status_code, self.text = payload, status, text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


@pytest.fixture
def gateway_on(settings, monkeypatch):
    settings.AI_GATEWAY_EMBEDDINGS_ENABLED = True
    settings.AI_GATEWAY_EMBEDDINGS_BASE_URL = "http://gw.test:8200"
    settings.AI_GATEWAY_EMBEDDINGS_MODEL = "fr-en-retrieval"
    settings.AI_GATEWAY_EMBEDDINGS_TIMEOUT_SECONDS = 5.0
    # Hors escape hatch stub : sinon le stub court-circuite le chemin gouverné.
    settings.EMBEDDINGS_PROVIDER = "ollama"
    # Référence publiée par la Gateway : lue sur `/health/embeddings`, jamais codée
    # dans le client. Les tests qui veulent une référence différente la remplacent.
    _reference(monkeypatch, _registry_payload())
    return settings


def _post(monkeypatch, payload, status=200, capture=None, text=""):
    def _fake(url, headers=None, json=None, timeout=None):
        if capture is not None:
            capture["url"], capture["payload"], capture["timeout"] = url, json, timeout
        return _Resp(payload, status=status, text=text)

    monkeypatch.setattr(gw.requests, "post", _fake)


def _reference(monkeypatch, payload, status=200, capture=None):
    """Double de `GET /health/embeddings`. `requests` est un module PARTAGÉ : patcher
    `gw.requests.get` vise le même attribut que `embedding_reference.requests.get`."""
    def _fake(url, timeout=None, **kw):
        if capture is not None:
            capture.setdefault("urls", []).append(url)
        return _Resp(payload, status=status)

    monkeypatch.setattr(gw.requests, "get", _fake)


# =========================================================================
# §8 — Module PUR d'identité
# =========================================================================
class TestPipelineIdentity:
    def test_family_comes_from_attested_fields_not_from_the_id(self):
        """La famille est dérivée de (model_logical, model_digest) attestés — au format
        DÉJÀ persisté `modele@digest8` — jamais extraite de l'identifiant opaque."""
        assert ep.attested_pipeline_family(
            model_logical="fr-en-retrieval", model_digest=DIGEST_FR_EN
        ) == "fr-en-retrieval@1a37926b"
        assert ep.attested_pipeline_family(
            model_logical="legacy-768", model_digest=DIGEST_LEGACY
        ) == "legacy-768@0a109f42"
        # Un identifiant opaque ne livre AUCUNE famille : on ne devine pas.
        assert ep.pipeline_family(PIPELINE_IDS[("legacy-768", "document")]) == ""

    def test_query_and_document_share_the_same_family(self):
        """Le cœur du lot : deux identités de pipeline, une seule famille — donc comparables."""
        q, d = PIPELINE_IDS[("fr-en-retrieval", "query")], PIPELINE_IDS[("fr-en-retrieval", "document")]
        assert q != d
        fq = ep.attested_pipeline_family(model_logical="fr-en-retrieval", model_digest=DIGEST_FR_EN)
        fd = ep.attested_pipeline_family(model_logical="fr-en-retrieval", model_digest=DIGEST_FR_EN)
        assert ep.same_pipeline_family(fq, fd)

    def test_different_digest_is_not_comparable(self):
        a = ep.attested_pipeline_family(model_logical="fr-en-retrieval", model_digest=DIGEST_FR_EN)
        b = ep.attested_pipeline_family(model_logical="fr-en-retrieval", model_digest="deadbeef" * 8)
        assert not ep.same_pipeline_family(a, b)

    def test_missing_attestation_gives_no_family(self):
        assert ep.attested_pipeline_family(model_logical="", model_digest=DIGEST_FR_EN) == ""
        assert ep.attested_pipeline_family(model_logical="fr-en-retrieval", model_digest="") == ""
        assert ep.attested_pipeline_family(model_logical="fr-en-retrieval", model_digest="abc") == ""

    def test_absent_identity_is_never_comparable_with_itself(self):
        """NULL != NULL : une identité inconnue ne prouve pas une identité commune."""
        assert not ep.same_pipeline_family(None, None)
        assert not ep.same_pipeline_family("", "")

    def test_legacy_id_has_no_digest_and_is_its_own_family(self):
        lid = ep.legacy_pipeline_id(provider="ollama", model="nomic-embed-text", dimension=768)
        assert lid == "legacy:ollama:nomic-embed-text:768:legacy-v1"
        assert ep.is_legacy_pipeline_id(lid)
        assert not ep.is_governed_pipeline_id(lid)
        assert ep.pipeline_family(lid) == lid

    def test_governed_id_is_recognised_without_any_format_assumption(self):
        assert ep.is_governed_pipeline_id(PIPELINE_IDS[("fr-en-retrieval", "document")])
        # Un identifiant du format historique reste « gouverné » : non legacy, non vide.
        assert ep.is_governed_pipeline_id("fr-en-retrieval@1a37926b/document-v1")
        assert not ep.is_governed_pipeline_id("")


# =========================================================================
# §7 — Client Gateway : nominal
# =========================================================================
class TestGatewayClientNominal:
    def test_document_task_is_sent_and_identity_returned(self, gateway_on, monkeypatch):
        cap = {}
        _post(monkeypatch, _ok_payload(n=2, task="document"), capture=cap)
        batch = gw.embed_via_gateway(
            ["a", "b"], model="fr-en-retrieval", task="document", dim=8
        )
        assert cap["url"] == "http://gw.test:8200/v1/embeddings"
        assert cap["payload"] == {
            "model": "fr-en-retrieval", "task": "document", "texts": ["a", "b"]
        }
        # Contrat FERMÉ : `provider` n'appartient pas au schéma `/v1/embeddings`.
        assert "provider" not in cap["payload"]
        assert len(batch) == 2
        assert batch.pipeline_id == PIPELINE_IDS[("fr-en-retrieval", "document")]
        assert batch.family == "fr-en-retrieval@1a37926b"
        assert batch.model_digest == DIGEST_FR_EN
        assert batch.governed is True

    def test_query_task_is_sent(self, gateway_on, monkeypatch):
        cap = {}
        _post(monkeypatch, _ok_payload(task="query"), capture=cap)
        batch = gw.embed_via_gateway(["q"], model="fr-en-retrieval", task="query", dim=8)
        assert cap["payload"]["task"] == "query"
        assert set(cap["payload"]) == {"model", "task", "texts"}
        assert batch.pipeline_id == PIPELINE_IDS[("fr-en-retrieval", "query")]
        assert batch.pipeline_id != PIPELINE_IDS[("fr-en-retrieval", "document")]
        # Même famille que le document : c'est ce qui rend le filtre possible.
        assert batch.family == "fr-en-retrieval@1a37926b"

    def test_empty_input_makes_no_call(self, gateway_on, monkeypatch):
        def _boom(*a, **k):  # pragma: no cover
            raise AssertionError("aucun appel ne doit partir pour une liste vide")

        monkeypatch.setattr(gw.requests, "post", _boom)
        assert gw.embed_via_gateway([], model="m", task="query", dim=8).vectors == []


# =========================================================================
# §7 — Client Gateway : TOUTES les validations refusent
# =========================================================================
class TestGatewayClientValidations:
    @pytest.mark.parametrize(
        "mutate, expected_code",
        [
            (lambda p: p.update(governed=False), "not_governed"),
            (lambda p: p.update(model_logical="autre-modele"), "model_logical_mismatch"),
            (lambda p: p.update(dimension=1024), "dimension_mismatch"),
            (lambda p: p.update(normalized=False), "not_normalized"),
            (lambda p: p.update(task="query"), "task_mismatch"),
            (lambda p: p.pop("pipeline_id"), "pipeline_missing"),
            # Paire (task, pipeline_id) : l'identifiant déclaré pour `query` rendu
            # pour `task=document` — jugé par ÉGALITÉ avec la référence, pas par aspect.
            (lambda p: p.update(pipeline_id=PIPELINE_IDS[("fr-en-retrieval", "query")]),
             "pipeline_id_mismatch"),
            # Identifiant d'un AUTRE modèle logique, pourtant déclaré au registre.
            (lambda p: p.update(pipeline_id=PIPELINE_IDS[("legacy-768", "document")]),
             "pipeline_id_mismatch"),
            # Identifiant inconnu de la référence.
            (lambda p: p.update(pipeline_id="emb-v2:ffffffffffffffffffffffffffffffff"),
             "pipeline_id_mismatch"),
            (lambda p: p.update(identity_verified=False), "identity_not_verified"),
            (lambda p: p.pop("identity_verified"), "identity_not_verified"),
            (lambda p: p.pop("model_digest"), "digest_missing"),
            (lambda p: p.update(expected_digest="deadbeef" * 8), "digest_mismatch"),
            (lambda p: p.update(model_digest="deadbeef" * 8, expected_digest="deadbeef" * 8),
             "digest_mismatch"),
            (lambda p: p.update(embeddings=[[0.5] * 8, [0.5] * 8]), "vector_count_mismatch"),
            (lambda p: p.update(truncated=[True]), "truncated"),
            (lambda p: p.update(truncated=[False, False]), "truncated_shape_invalid"),
            (lambda p: p.pop("truncated"), "truncated_shape_invalid"),
            (lambda p: p.update(embeddings=[[0.0] * 8]), "vector_zero"),
            (lambda p: p.update(embeddings=[[0.5] * 4]), "vector_dimension_mismatch"),
            (lambda p: p.update(embeddings=[[float("nan")] * 8]), "vector_not_finite"),
            (lambda p: p.update(embeddings=[["x"] * 8]), "vector_not_numeric"),
        ],
    )
    def test_invalid_response_is_refused(self, gateway_on, monkeypatch, mutate, expected_code):
        payload = _ok_payload(n=1, task="document")
        mutate(payload)
        _post(monkeypatch, payload)
        with pytest.raises(gw.GatewayEmbeddingsError) as exc:
            gw.embed_via_gateway(["a"], model="fr-en-retrieval", task="document", dim=8)
        assert exc.value.code == expected_code

    @pytest.mark.parametrize("status", [400, 502])
    def test_http_error_is_refused(self, gateway_on, monkeypatch, status):
        _post(monkeypatch, None, status=status, text='{"detail":"nope"}')
        with pytest.raises(gw.GatewayEmbeddingsError) as exc:
            gw.embed_via_gateway(["a"], model="fr-en-retrieval", task="document", dim=8)
        assert exc.value.code == f"http_{status}"

    def test_timeout_is_refused(self, gateway_on, monkeypatch):
        def _timeout(*a, **k):
            raise gw.requests.Timeout("too slow")

        monkeypatch.setattr(gw.requests, "post", _timeout)
        with pytest.raises(gw.GatewayEmbeddingsError) as exc:
            gw.embed_via_gateway(["a"], model="fr-en-retrieval", task="document", dim=8)
        assert exc.value.code == "transport_error"

    def test_non_json_is_refused(self, gateway_on, monkeypatch):
        _post(monkeypatch, None, status=200)
        with pytest.raises(gw.GatewayEmbeddingsError) as exc:
            gw.embed_via_gateway(["a"], model="fr-en-retrieval", task="document", dim=8)
        assert exc.value.code == "bad_payload"

    def test_invalid_task_is_refused_before_any_call(self, gateway_on, monkeypatch):
        def _boom(*a, **k):  # pragma: no cover
            raise AssertionError("aucun appel avec une task invalide")

        monkeypatch.setattr(gw.requests, "post", _boom)
        with pytest.raises(gw.GatewayEmbeddingsError) as exc:
            gw.embed_via_gateway(["a"], model="m", task="bogus", dim=8)
        assert exc.value.code == "invalid_task"

    def test_disabled_flag_refuses_instead_of_falling_back(self, settings, monkeypatch):
        """Le point le plus important du lot : PAS de repli silencieux."""
        settings.EMBEDDINGS_PROVIDER = "ollama"
        settings.AI_GATEWAY_EMBEDDINGS_ENABLED = False
        settings.AI_GATEWAY_EMBEDDINGS_BASE_URL = "http://gw.test:8200"

        def _boom(*a, **k):  # pragma: no cover
            raise AssertionError("aucun appel ne doit partir quand le flag est OFF")

        monkeypatch.setattr(gw.requests, "post", _boom)
        with pytest.raises(gw.GatewayEmbeddingsError) as exc:
            gw.embed_via_gateway(["a"], model="fr-en-retrieval", task="document", dim=8)
        assert exc.value.code == "gateway_embeddings_disabled"

    def test_missing_base_url_is_refused(self, gateway_on):
        gateway_on.AI_GATEWAY_EMBEDDINGS_BASE_URL = ""
        with pytest.raises(gw.GatewayEmbeddingsError) as exc:
            gw.embed_via_gateway(["a"], model="fr-en-retrieval", task="document", dim=8)
        assert exc.value.code == "base_url_missing"


# =========================================================================
# §6 — Routage par provider
# =========================================================================
class TestProviderRouting:
    def test_ai_gateway_provider_uses_the_governed_client(self, gateway_on, monkeypatch):
        cap = {}
        _post(monkeypatch, _ok_payload(task="document"), capture=cap)
        batch = emb.embed_texts_with_identity(
            ["x"], dim=8, provider="ai_gateway", model="fr-en-retrieval", task="document"
        )
        assert cap["url"].endswith("/v1/embeddings")
        assert batch.governed is True


    def test_legacy_embed_texts_signature_unchanged(self, settings):
        """`embed_texts` rend toujours une simple liste de vecteurs."""
        settings.EMBEDDINGS_PROVIDER = "stub"
        vecs = emb.embed_texts(["a", "b"], dim=8)
        assert isinstance(vecs, list) and len(vecs) == 2
        assert all(isinstance(v, list) and len(v) == 8 for v in vecs)

    def test_stub_gives_the_same_family_to_query_and_document(self, settings):
        """Sinon la suite de tests d'un espace gouverné ne trouverait jamais rien."""
        settings.EMBEDDINGS_PROVIDER = "stub"
        q = emb.embed_texts_with_identity(["q"], dim=8, provider="ai_gateway",
                                          model="fr-en-retrieval", task="query")
        d = emb.embed_texts_with_identity(["d"], dim=8, provider="ai_gateway",
                                          model="fr-en-retrieval", task="document")
        assert ep.is_legacy_pipeline_id(q.pipeline_id)
        assert ep.same_pipeline_family(q.pipeline_id, d.pipeline_id)

    def test_unknown_provider_is_refused_not_silently_stubbed(self, settings):
        """Un provider INCONNU est refusé, il ne retombe plus sur le stub.

        Changement de contrat assumé par le lot EMBEDDINGS-EXTERNAL-EGRESS-GUARD.
        `is_external_provider` déclare depuis l'origine qu'un provider inconnu est
        traité comme EXTERNE — « un nom mal orthographié ne doit jamais être traité
        comme interne par défaut ». Tant que personne ne consultait la politique côté
        embeddings, cette règle restait lettre morte et un `EMBEDDINGS_PROVIDER` fautif
        produisait silencieusement des vecteurs de stub : indiscernables de vrais
        vecteurs, et donc pires qu'une erreur.

        Le stub reste atteignable par la voie prévue pour lui : `EMBEDDINGS_PROVIDER=stub`.
        """
        from apps.llm_router.egress import MissingEgressContext

        settings.EMBEDDINGS_PROVIDER = "ollama"
        with pytest.raises(MissingEgressContext):
            emb.embed_texts_with_identity(["a"], dim=8, provider="martian", model="m")

    def test_global_stub_escape_hatch_still_wins(self, settings):
        """`EMBEDDINGS_PROVIDER=stub` court-circuite tout, garde comprise (CI/dev)."""
        settings.EMBEDDINGS_PROVIDER = "stub"
        batch = emb.embed_texts_with_identity(["a"], dim=8, provider="martian", model="m")
        assert len(batch.vectors) == 1
        assert ep.is_legacy_pipeline_id(batch.pipeline_id)
        assert not batch.governed


# =========================================================================
# §11 — Filtrage SQL de la recherche vectorielle
# =========================================================================
@pytest.mark.django_db
class TestVectorSearchPipelineFilter:
    def _sql_of(self, monkeypatch, **kwargs):
        """Capture le SQL et les paramètres, sans toucher la base."""
        from apps.rag_core.repositories import chunk_repository as repo

        captured = {}

        class _Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params=None):
                # `transaction.atomic` + `SET LOCAL hnsw.ef_search` passent aussi par
                # ce curseur : on ne retient que la requête de recherche elle-même,
                # sinon un `RELEASE SAVEPOINT` écraserait la capture.
                if "FROM rag_chunk" not in str(sql):
                    return
                captured["sql"], captured["params"] = sql, params

            @staticmethod
            def fetchall():
                return []

        monkeypatch.setattr(repo.connection, "cursor", lambda: _Cur())
        repo.search_chunks_vector(
            rag_space_id="11111111-1111-1111-1111-111111111111",
            query_embedding=[0.1] * 8,
            top_k=5,
            **kwargs,
        )
        return captured

    def test_governed_search_filters_on_pipeline_family(self, monkeypatch):
        cap = self._sql_of(
            monkeypatch,
            embedding_provider="ai_gateway",
            embedding_model="fr-en-retrieval",
            pipeline_family="fr-en-retrieval@1a37926b",
        )
        assert "c.pipeline_family = %(pf)s" in cap["sql"]
        assert cap["params"]["pf"] == "fr-en-retrieval@1a37926b"
        # Le verrou d'espace reste intact — c'est la garde anti-fuite historique.
        assert "c.rag_space_id = %(space)s" in cap["sql"]
        assert "d.rag_space_id = %(space)s" in cap["sql"]

    def test_legacy_search_has_no_pipeline_clause(self, monkeypatch):
        cap = self._sql_of(
            monkeypatch, embedding_provider="ollama", embedding_model="nomic-embed-text"
        )
        assert "pipeline_family" not in cap["sql"]
        assert "pf" not in (cap["params"] or {})
        # La garde P1-D historique, elle, est toujours là.
        assert "d.embedding_model = %(em)s" in cap["sql"]

    def test_blank_family_is_treated_as_absent(self, monkeypatch):
        cap = self._sql_of(monkeypatch, pipeline_family="   ")
        assert "pipeline_family" not in cap["sql"]


# =========================================================================
# §9/§10 — Modèle et persistance
# =========================================================================
@pytest.mark.django_db
class TestChunkPipelineColumns:
    def test_pipeline_columns_are_nullable_for_historical_chunks(self):
        """Un chunk historique reste NULL : on ne l'étiquette jamais après coup."""
        from apps.rag_core.models import Chunk

        field = Chunk._meta.get_field("pipeline_id")
        assert field.null is True
        assert Chunk._meta.get_field("pipeline_family").null is True

    def test_composite_index_exists(self):
        from apps.rag_core.models import Chunk

        names = {i.name for i in Chunk._meta.indexes}
        assert "rag_chunk_space_pipeline_idx" in names


# =========================================================================
# §14 — Budget de chunk
# =========================================================================
@pytest.mark.django_db
class TestChunkBudget:
    def test_governed_space_gets_the_reduced_budget(self, settings):
        from apps.rag_core.services.ingest.utils import resolve_chunk_budget

        settings.RAG_CHUNK_SIZE_CHARS = 2500
        settings.RAG_CHUNK_OVERLAP_CHARS = 250
        settings.RAG_GOVERNED_CHUNK_SIZE_CHARS = 1200
        settings.RAG_GOVERNED_CHUNK_OVERLAP_CHARS = 180

        class _Space:
            embedding_provider = "ai_gateway"
            embedding_model = "fr-en-retrieval"

        assert resolve_chunk_budget(_Space()) == (1200, 180)

    def test_legacy_space_budget_is_unchanged(self, settings):
        from apps.rag_core.services.ingest.utils import resolve_chunk_budget

        settings.RAG_CHUNK_SIZE_CHARS = 2500
        settings.RAG_CHUNK_OVERLAP_CHARS = 250

        class _Space:
            embedding_provider = "ollama"
            embedding_model = "nomic-embed-text"

        assert resolve_chunk_budget(_Space()) == (2500, 250)
        assert resolve_chunk_budget(None) == (2500, 250)

    def test_overlap_can_never_exceed_chunk_size(self, settings):
        from apps.rag_core.services.ingest.utils import resolve_chunk_budget

        settings.RAG_GOVERNED_CHUNK_SIZE_CHARS = 300
        settings.RAG_GOVERNED_CHUNK_OVERLAP_CHARS = 900

        class _Space:
            embedding_provider = "ai_gateway"
            embedding_model = "fr-en-retrieval"

        size, overlap = resolve_chunk_budget(_Space())
        assert overlap < size
