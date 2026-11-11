"""PIPELINE-ID-REPAIR — identités de pipeline OPAQUES.

Depuis le contrat Gateway v2, `pipeline_id` vaut `emb-v2:<empreinte>` : il ne porte ni
la tâche, ni le modèle, ni le digest. Le client RagATM :

- valide la paire (task, pipeline_id) par ÉGALITÉ avec la référence publiée sur
  `/health/embeddings` (fail-closed si la référence est indisponible, incomplète,
  ambiguë, ou si l'identifiant ne lui correspond pas) ;
- dérive la FAMILLE de comparabilité des champs attestés (`model_logical`,
  `model_digest` vérifié), au format déjà persisté `modele@digest8` ;
- ne découpe JAMAIS l'identifiant.

Aucun réseau : `requests.post` et `requests.get` sont doublés à la frontière.
"""
from __future__ import annotations

import re

import pytest

from apps.llm_router import embedding_pipeline as ep
from apps.llm_router import embedding_reference as ref
from apps.llm_router import embeddings as emb
from apps.llm_router import embeddings_gateway as gw
from apps.llm_router.embedding_errors import GatewayEmbeddingsError
from apps.rag_core.services.ingest.types import is_retryable_error

from _opaque_gateway_fixtures import (  # noqa: E402 — dossier de tests inséré dans sys.path par pytest
    BASE_URL,
    DIGESTS,
    DIM,
    PERSISTED_FAMILIES,
    PIPELINE_IDS,
    UNKNOWN_PIPELINE_ID,
    FakeGateway,
    Resp,
    embeddings_payload,
    governed_settings,
    registry_entry,
    registry_payload,
)

MODELS = ("legacy-768", "fr-en-retrieval")
TASKS = ("query", "document")


@pytest.fixture
def governed(settings):
    return governed_settings(settings)


def _embed(model, task, payload=None, *, gateway=None, monkeypatch=None, n=1):
    g = gateway or FakeGateway(embeddings=payload or embeddings_payload(model=model, task=task, n=n))
    if monkeypatch is not None:
        g.install(monkeypatch)
    batch = gw.embed_via_gateway(["x"] * n, model=model, task=task, dim=DIM)
    return batch, g


def _reject(model, task, payload, monkeypatch, *, gateway=None):
    g = gateway or FakeGateway(embeddings=payload)
    g.install(monkeypatch)
    with pytest.raises(GatewayEmbeddingsError) as exc:
        gw.embed_via_gateway(["x"], model=model, task=task, dim=DIM)
    return exc.value, g


# =========================================================================
# §20, §21 — identités courantes acceptées, pour les DEUX profils
# =========================================================================
class TestAcceptedPairs:
    @pytest.mark.parametrize("model", MODELS)
    @pytest.mark.parametrize("task", TASKS)
    def test_declared_pair_is_accepted(self, governed, monkeypatch, model, task):
        batch, g = _embed(model, task, monkeypatch=monkeypatch)
        assert batch.pipeline_id == PIPELINE_IDS[(model, task)]
        assert batch.task == task
        assert batch.governed is True
        assert batch.family == PERSISTED_FAMILIES[model]
        # La référence a été lue UNE fois, sur l'URL de la Gateway d'embeddings.
        assert g.gets == [f"{BASE_URL}/health/embeddings"]

    def test_legacy768_document_the_incident_pair(self, governed, monkeypatch):
        """La paire exacte refusée en staging le 2026-10-05 (177 documents)."""
        batch, _ = _embed("legacy-768", "document", monkeypatch=monkeypatch)
        assert batch.pipeline_id == "emb-v2:9e883f0fe1d326ef17f86027a1d4a5d8"

    def test_legacy768_query_the_retrieval_pair(self, governed, monkeypatch):
        """La paire refusée côté requête depuis le 2026-10-01 (leg vectoriel dégradé)."""
        batch, _ = _embed("legacy-768", "query", monkeypatch=monkeypatch)
        assert batch.pipeline_id == "emb-v2:3a49e14ec5df7088cebb7630fd1d27eb"


# =========================================================================
# §20 mismatch A/B, §23, §24, §25 — rejets
# =========================================================================
class TestRejectedPairs:
    @pytest.mark.parametrize("model", MODELS)
    def test_document_pipeline_with_query_task_is_rejected(self, governed, monkeypatch, model):
        payload = embeddings_payload(model=model, task="query",
                                     pipeline_id=PIPELINE_IDS[(model, "document")])
        err, _ = _reject(model, "query", payload, monkeypatch)
        assert err.code == "pipeline_id_mismatch"

    @pytest.mark.parametrize("model", MODELS)
    def test_query_pipeline_with_document_task_is_rejected(self, governed, monkeypatch, model):
        payload = embeddings_payload(model=model, task="document",
                                     pipeline_id=PIPELINE_IDS[(model, "query")])
        err, _ = _reject(model, "document", payload, monkeypatch)
        assert err.code == "pipeline_id_mismatch"

    def test_returned_task_contradicting_expected_task_is_rejected(self, governed, monkeypatch):
        """Même un identifiant déclaré ne sauve pas une tâche contradictoire (§24)."""
        payload = embeddings_payload(model="legacy-768", task="query")  # id ET task = query
        err, _ = _reject("legacy-768", "document", payload, monkeypatch)
        assert err.code == "task_mismatch"

    def test_unknown_pipeline_is_rejected(self, governed, monkeypatch):
        payload = embeddings_payload(model="legacy-768", task="document", pipeline_id=UNKNOWN_PIPELINE_ID)
        err, g = _reject("legacy-768", "document", payload, monkeypatch)
        assert err.code == "pipeline_id_mismatch"
        # Un désaccord provoque UNE relecture de la référence, puis le refus tient.
        assert len(g.gets) == 2

    def test_pipeline_of_another_model_is_rejected(self, governed, monkeypatch):
        """legacy-768/document avec l'identifiant fr-en-retrieval/document (§25)."""
        payload = embeddings_payload(model="legacy-768", task="document",
                                     pipeline_id=PIPELINE_IDS[("fr-en-retrieval", "document")])
        err, _ = _reject("legacy-768", "document", payload, monkeypatch)
        assert err.code == "pipeline_id_mismatch"

    def test_error_message_names_the_reference_not_a_substring(self, governed, monkeypatch):
        payload = embeddings_payload(model="legacy-768", task="document",
                                     pipeline_id=PIPELINE_IDS[("legacy-768", "query")])
        err, _ = _reject("legacy-768", "document", payload, monkeypatch)
        msg = str(err)
        assert "model_logical='legacy-768'" in msg and "task='document'" in msg
        assert "référence Gateway" in msg and PIPELINE_IDS[("legacy-768", "document")] in msg
        assert "incohérent avec task" not in msg

    def test_missing_pipeline_id_keeps_its_explicit_code(self, governed, monkeypatch):
        payload = embeddings_payload(model="legacy-768", task="document")
        payload.pop("pipeline_id")
        err, _ = _reject("legacy-768", "document", payload, monkeypatch)
        assert err.code == "pipeline_missing"

    def test_wrong_dimension_is_still_rejected_before_the_reference(self, governed, monkeypatch):
        payload = embeddings_payload(model="legacy-768", task="document", dimension=1024)
        err, g = _reject("legacy-768", "document", payload, monkeypatch)
        assert err.code == "dimension_mismatch"
        assert g.gets == []  # aucune lecture de référence pour une réponse déjà invalide


# =========================================================================
# §22 — l'identifiant est OPAQUE : rien n'est parsé
# =========================================================================
class TestOpaqueIdentifier:
    OPAQUE = "opq_7f3a9c1e2b4d6f8a0c2e4a6b8d0f1a3c"  # ni `query`, ni `document`, ni `/`

    def test_fully_opaque_identifier_is_accepted_when_the_reference_attests_it(
        self, governed, monkeypatch
    ):
        assert "query" not in self.OPAQUE and "document" not in self.OPAQUE and "/" not in self.OPAQUE
        registry = registry_payload(
            registry_entry("legacy-768", pipeline_id_document=self.OPAQUE),
            registry_entry("fr-en-retrieval"),
        )
        payload = embeddings_payload(model="legacy-768", task="document", pipeline_id=self.OPAQUE)
        batch, _ = _embed("legacy-768", "document", gateway=FakeGateway(embeddings=payload, registry=registry),
                          monkeypatch=monkeypatch)
        assert batch.pipeline_id == self.OPAQUE
        assert batch.family == PERSISTED_FAMILIES["legacy-768"]

    def test_historical_v1_identifier_is_judged_by_the_reference_too(self, governed, monkeypatch):
        """Un identifiant au format v1 n'est ni privilégié ni découpé : s'il n'est pas
        déclaré par la référence, il est refusé comme n'importe quel inconnu."""
        payload = embeddings_payload(model="legacy-768", task="document",
                                     pipeline_id="legacy-768@0a109f42/document-v1")
        err, _ = _reject("legacy-768", "document", payload, monkeypatch)
        assert err.code == "pipeline_id_mismatch"

    @pytest.mark.parametrize("module", [gw, ep, ref])
    def test_no_format_assumption_survives_in_the_source(self, module):
        """Verrou statique : aucune lecture sémantique de `pipeline_id` dans le code."""
        import inspect

        src = inspect.getsource(module)
        code_lines = [
            line for line in src.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        code = "\n".join(code_lines)
        forbidden = [
            "task in pipeline_id", 'pipeline_id.split("/")', "rpartition(", "_TASK_SEPARATOR",
            'endswith("/document', 'endswith("/query', '"/document-v', '"/query-v',
        ]
        for pattern in forbidden:
            assert pattern not in code, f"{module.__name__} : motif hérité {pattern!r}"


# =========================================================================
# §26, §27 — famille : parité query/document et compatibilité des fragments existants
# =========================================================================
class TestVectorFamily:
    @pytest.mark.parametrize("model", MODELS)
    def test_query_and_document_share_one_family_with_distinct_pipeline_ids(
        self, governed, monkeypatch, model
    ):
        q, _ = _embed(model, "query", monkeypatch=monkeypatch)
        d, _ = _embed(model, "document", monkeypatch=monkeypatch)
        assert q.pipeline_id != d.pipeline_id
        assert q.family == d.family == PERSISTED_FAMILIES[model]
        assert ep.same_pipeline_family(q.family, d.family)

    @pytest.mark.parametrize("model", MODELS)
    def test_family_has_the_historical_format(self, governed, monkeypatch, model):
        batch, _ = _embed(model, "document", monkeypatch=monkeypatch)
        assert re.fullmatch(r"[a-z0-9][a-z0-9-]*@[0-9a-f]{8}", batch.family), batch.family

    @pytest.mark.parametrize("model", MODELS)
    def test_existing_vector_metadata_compatibility(self, model):
        """Fixture = métadonnée vectorielle DÉJÀ persistée (inventaire staging). La
        dérivation attestée rend exactement la même famille : aucune migration."""
        persisted = PERSISTED_FAMILIES[model]
        derived = ep.attested_pipeline_family(model_logical=model, model_digest=DIGESTS[model])
        assert derived == persisted

    def test_family_is_derived_from_attested_fields_not_from_model_alias(self, governed, monkeypatch):
        """Le nom Ollama physique (`nomic-embed-text`, `:latest`) n'entre pas dans la
        famille : c'est le modèle LOGIQUE du registre, comme dans les fragments existants."""
        payload = embeddings_payload(model="legacy-768", task="document")
        payload["model"] = "nomic-embed-text:latest"  # nom Ollama physique, avec alias
        batch, _ = _embed("legacy-768", "document", payload, monkeypatch=monkeypatch)
        assert batch.family == "legacy-768@0a109f42"

    def test_different_digest_gives_a_different_family(self, governed, monkeypatch):
        other = "deadbeef" * 8
        registry = registry_payload(registry_entry("legacy-768", expected_digest=other, observed_digest=other),
                                    registry_entry("fr-en-retrieval"))
        payload = embeddings_payload(model="legacy-768", task="document", model_digest=other, expected_digest=other)
        batch, _ = _embed("legacy-768", "document", gateway=FakeGateway(embeddings=payload, registry=registry),
                          monkeypatch=monkeypatch)
        assert batch.family == "legacy-768@deadbeef"
        assert not ep.same_pipeline_family(batch.family, PERSISTED_FAMILIES["legacy-768"])


# =========================================================================
# §29 — référence Gateway : chaque défaillance a un comportement déterministe
# =========================================================================
class TestGatewayReference:
    def test_nominal_registry_is_parsed(self):
        models = ref.parse_pipeline_reference(registry_payload())
        assert set(models) == set(MODELS)
        r = models["legacy-768"]
        assert r.pipeline_id_for("query") == PIPELINE_IDS[("legacy-768", "query")]
        assert r.pipeline_id_for("document") == PIPELINE_IDS[("legacy-768", "document")]
        assert r.expected_digest == DIGESTS["legacy-768"] and r.dimension == DIM and r.identity_verified

    def test_model_absent_from_reference(self, governed, monkeypatch):
        g = FakeGateway(embeddings=embeddings_payload(model="legacy-768", task="document"),
                        registry=registry_payload(registry_entry("fr-en-retrieval")))
        err, _ = _reject("legacy-768", "document", None, monkeypatch, gateway=g)
        assert err.code == "pipeline_reference_missing_model"

    @pytest.mark.parametrize("task,missing_key", [("query", "pipeline_id_query"), ("document", "pipeline_id_document")])
    def test_task_absent_from_reference(self, governed, monkeypatch, task, missing_key):
        entry = registry_entry("legacy-768")
        entry.pop(missing_key)
        g = FakeGateway(embeddings=embeddings_payload(model="legacy-768", task=task),
                        registry=registry_payload(entry, registry_entry("fr-en-retrieval")))
        err, _ = _reject("legacy-768", task, None, monkeypatch, gateway=g)
        assert err.code == "pipeline_reference_missing_task"

    def test_other_task_still_works_when_one_is_undeclared(self, governed, monkeypatch):
        entry = registry_entry("legacy-768")
        entry.pop("pipeline_id_query")
        g = FakeGateway(embeddings=embeddings_payload(model="legacy-768", task="document"),
                        registry=registry_payload(entry, registry_entry("fr-en-retrieval")))
        batch, _ = _embed("legacy-768", "document", gateway=g, monkeypatch=monkeypatch)
        assert batch.pipeline_id == PIPELINE_IDS[("legacy-768", "document")]

    @pytest.mark.parametrize("malformed", ["", "   ", 42, None, ["emb-v2:x"]])
    def test_malformed_declared_pipeline_id_counts_as_undeclared(self, governed, monkeypatch, malformed):
        entry = registry_entry("legacy-768", pipeline_id_document=malformed)
        g = FakeGateway(embeddings=embeddings_payload(model="legacy-768", task="document"),
                        registry=registry_payload(entry, registry_entry("fr-en-retrieval")))
        err, _ = _reject("legacy-768", "document", None, monkeypatch, gateway=g)
        assert err.code == "pipeline_reference_missing_task"

    def test_duplicate_model_identity_is_ambiguous_and_refused(self, governed, monkeypatch):
        g = FakeGateway(embeddings=embeddings_payload(model="legacy-768", task="document"),
                        registry=registry_payload(registry_entry("legacy-768"), registry_entry("legacy-768")))
        err, _ = _reject("legacy-768", "document", None, monkeypatch, gateway=g)
        assert err.code == "pipeline_reference_invalid"

    @pytest.mark.parametrize("bad", [None, [], {"models": {}}, {"models": [1]}, {"models": [{"model_logical": ""}]}])
    def test_malformed_registry_is_refused(self, bad):
        with pytest.raises(GatewayEmbeddingsError) as exc:
            ref.parse_pipeline_reference(bad)
        assert exc.value.code == "pipeline_reference_invalid"

    def test_timeout_is_unavailable_and_transient(self, governed, monkeypatch):
        import requests

        g = FakeGateway(embeddings=embeddings_payload(model="legacy-768", task="document"))
        g.registry_error = requests.Timeout("slow")
        err, _ = _reject("legacy-768", "document", None, monkeypatch, gateway=g)
        assert err.code == "pipeline_reference_unavailable"
        # L'ingestion la classe TRANSITOIRE : retry Celery, pas de FAILED définitif.
        assert is_retryable_error(err)

    def test_http_503_is_unavailable_and_transient(self, governed, monkeypatch):
        g = FakeGateway(embeddings=embeddings_payload(model="legacy-768", task="document"), registry_status=503)
        err, _ = _reject("legacy-768", "document", None, monkeypatch, gateway=g)
        assert err.code == "pipeline_reference_unavailable"
        assert is_retryable_error(err)

    def test_http_404_is_unavailable_but_not_transient(self, governed, monkeypatch):
        g = FakeGateway(embeddings=embeddings_payload(model="legacy-768", task="document"), registry_status=404)
        err, _ = _reject("legacy-768", "document", None, monkeypatch, gateway=g)
        assert err.code == "pipeline_reference_unavailable"
        assert not is_retryable_error(err)

    def test_invalid_json_is_unavailable(self, governed, monkeypatch):
        g = FakeGateway(embeddings=embeddings_payload(model="legacy-768", task="document"))
        g.registry = None  # `Resp(None).json()` lève ValueError : corps non JSON
        err, _ = _reject("legacy-768", "document", None, monkeypatch, gateway=g)
        assert err.code == "pipeline_reference_unavailable"

    def test_reference_failure_never_accepts_the_vector(self, governed, monkeypatch):
        """Fail-closed : aucune des défaillances ci-dessus ne rend un lot."""
        g = FakeGateway(embeddings=embeddings_payload(model="legacy-768", task="document"), registry_status=500)
        g.install(monkeypatch)
        with pytest.raises(GatewayEmbeddingsError):
            gw.embed_via_gateway(["x"], model="legacy-768", task="document", dim=DIM)
        assert gw.counters_snapshot()["embedding_gateway_errors_total"] >= 1


# =========================================================================
# §30 — cycle de vie du cache de référence
# =========================================================================
class TestReferenceCache:
    def test_cache_hit_makes_no_second_network_call(self, governed, monkeypatch):
        g = FakeGateway(embeddings=embeddings_payload(model="legacy-768", task="document")).install(monkeypatch)
        gw.embed_via_gateway(["x"], model="legacy-768", task="document", dim=DIM)
        gw.embed_via_gateway(["y"], model="legacy-768", task="document", dim=DIM)
        g.embeddings = embeddings_payload(model="legacy-768", task="query")
        gw.embed_via_gateway(["q"], model="legacy-768", task="query", dim=DIM)
        assert len(g.posts) == 3 and len(g.gets) == 1
        assert ref.counters_snapshot()["embedding_pipeline_reference_cache_hits_total"] == 2

    def test_cache_miss_after_reset(self, governed, monkeypatch):
        g = FakeGateway(embeddings=embeddings_payload(model="legacy-768", task="document")).install(monkeypatch)
        gw.embed_via_gateway(["x"], model="legacy-768", task="document", dim=DIM)
        ref.reset_reference_cache()
        gw.embed_via_gateway(["x"], model="legacy-768", task="document", dim=DIM)
        assert len(g.gets) == 2

    def test_expiry_triggers_a_fresh_read(self, governed, monkeypatch):
        clock = {"t": 1000.0}
        monkeypatch.setattr(ref, "_now", lambda: clock["t"])
        g = FakeGateway(embeddings=embeddings_payload(model="legacy-768", task="document")).install(monkeypatch)
        gw.embed_via_gateway(["x"], model="legacy-768", task="document", dim=DIM)
        clock["t"] += ref.REFERENCE_TTL_SECONDS - 1
        gw.embed_via_gateway(["x"], model="legacy-768", task="document", dim=DIM)
        assert len(g.gets) == 1  # encore valide
        clock["t"] += 2
        gw.embed_via_gateway(["x"], model="legacy-768", task="document", dim=DIM)
        assert len(g.gets) == 2  # expirée : relue

    def test_reference_change_is_absorbed_by_one_forced_refresh(self, governed, monkeypatch):
        """Gateway redéployée : nouvel identifiant déclaré ET rendu → accepté après UNE
        relecture, sans faux rejet et sans accepter l'ancien."""
        g = FakeGateway(embeddings=embeddings_payload(model="legacy-768", task="document")).install(monkeypatch)
        gw.embed_via_gateway(["x"], model="legacy-768", task="document", dim=DIM)
        new_id = "emb-v3:0123456789abcdef0123456789abcdef"
        g.registry = registry_payload(registry_entry("legacy-768", pipeline_id_document=new_id),
                                      registry_entry("fr-en-retrieval"))
        g.embeddings = embeddings_payload(model="legacy-768", task="document", pipeline_id=new_id)
        batch = gw.embed_via_gateway(["x"], model="legacy-768", task="document", dim=DIM)
        assert batch.pipeline_id == new_id and len(g.gets) == 2
        # L'ANCIEN identifiant n'est plus déclaré : refusé désormais.
        g.embeddings = embeddings_payload(model="legacy-768", task="document")
        with pytest.raises(GatewayEmbeddingsError) as exc:
            gw.embed_via_gateway(["x"], model="legacy-768", task="document", dim=DIM)
        assert exc.value.code == "pipeline_id_mismatch"

    def test_expired_reference_is_never_served_stale_when_gateway_is_down(self, governed, monkeypatch):
        import requests

        clock = {"t": 1000.0}
        monkeypatch.setattr(ref, "_now", lambda: clock["t"])
        g = FakeGateway(embeddings=embeddings_payload(model="legacy-768", task="document")).install(monkeypatch)
        gw.embed_via_gateway(["x"], model="legacy-768", task="document", dim=DIM)
        clock["t"] += ref.REFERENCE_TTL_SECONDS + 1
        g.registry_error = requests.ConnectionError("down")
        with pytest.raises(GatewayEmbeddingsError) as exc:
            gw.embed_via_gateway(["x"], model="legacy-768", task="document", dim=DIM)
        assert exc.value.code == "pipeline_reference_unavailable"

    def test_valid_cached_reference_survives_a_transient_gateway_health_outage(self, governed, monkeypatch):
        """Dans le TTL, la référence lue reste valable : la santé du registre n'est pas
        réinterrogée à chaque lot (pas d'appel health par batch)."""
        import requests

        g = FakeGateway(embeddings=embeddings_payload(model="legacy-768", task="document")).install(monkeypatch)
        gw.embed_via_gateway(["x"], model="legacy-768", task="document", dim=DIM)
        g.registry_error = requests.ConnectionError("down")
        batch = gw.embed_via_gateway(["x"], model="legacy-768", task="document", dim=DIM)
        assert batch.pipeline_id == PIPELINE_IDS[("legacy-768", "document")]
        assert len(g.gets) == 1

    def test_mismatch_refresh_failure_is_fail_closed(self, governed, monkeypatch):
        import requests

        g = FakeGateway(embeddings=embeddings_payload(model="legacy-768", task="document")).install(monkeypatch)
        gw.embed_via_gateway(["x"], model="legacy-768", task="document", dim=DIM)
        g.embeddings = embeddings_payload(model="legacy-768", task="document", pipeline_id=UNKNOWN_PIPELINE_ID)
        g.registry_error = requests.ConnectionError("down")
        with pytest.raises(GatewayEmbeddingsError) as exc:
            gw.embed_via_gateway(["x"], model="legacy-768", task="document", dim=DIM)
        assert exc.value.code == "pipeline_reference_unavailable"


# =========================================================================
# §31 — identité vérifiée et digests
# =========================================================================
class TestIdentityVerified:
    def test_identity_verified_true_is_required(self, governed, monkeypatch):
        batch, _ = _embed("legacy-768", "document", monkeypatch=monkeypatch)
        assert batch.model_digest == DIGESTS["legacy-768"]

    @pytest.mark.parametrize("value", [False, None, "true", 1])
    def test_identity_not_verified_is_rejected(self, governed, monkeypatch, value):
        payload = embeddings_payload(model="legacy-768", task="document", identity_verified=value)
        err, g = _reject("legacy-768", "document", payload, monkeypatch)
        assert err.code == "identity_not_verified"
        assert g.gets == []  # refus AVANT toute lecture de référence

    def test_response_digest_mismatch_is_rejected(self, governed, monkeypatch):
        payload = embeddings_payload(model="legacy-768", task="document", model_digest="deadbeef" * 8)
        err, _ = _reject("legacy-768", "document", payload, monkeypatch)
        assert err.code == "digest_mismatch"

    def test_registry_digest_mismatch_is_rejected(self, governed, monkeypatch):
        other = "deadbeef" * 8
        g = FakeGateway(embeddings=embeddings_payload(model="legacy-768", task="document"),
                        registry=registry_payload(registry_entry("legacy-768", expected_digest=other),
                                                  registry_entry("fr-en-retrieval")))
        err, _ = _reject("legacy-768", "document", None, monkeypatch, gateway=g)
        assert err.code == "digest_mismatch"

    def test_expected_digest_absent_from_response_falls_back_to_the_registry(self, governed, monkeypatch):
        payload = embeddings_payload(model="legacy-768", task="document")
        payload.pop("expected_digest")
        batch, _ = _embed("legacy-768", "document", payload, monkeypatch=monkeypatch)
        assert batch.family == PERSISTED_FAMILIES["legacy-768"]

    def test_model_digest_absent_is_rejected(self, governed, monkeypatch):
        payload = embeddings_payload(model="legacy-768", task="document")
        payload.pop("model_digest")
        err, _ = _reject("legacy-768", "document", payload, monkeypatch)
        assert err.code == "digest_missing"


# =========================================================================
# §32 — aucun repli vers un provider direct pour « réussir »
# =========================================================================
class TestNoSilentFallback:
    @pytest.mark.parametrize("failure", ["reference_down", "mismatch", "identity"])
    def test_governed_failure_never_reaches_a_direct_provider(self, governed, monkeypatch, failure):
        import requests

        for name in ("_stub_embed",):  # édition Open : aucun autre chemin d'encodage n'existe
            monkeypatch.setattr(emb, name, lambda *a, **k: (_ for _ in ()).throw(AssertionError(f"{name} appelé")))
        payload = embeddings_payload(model="legacy-768", task="document")
        g = FakeGateway(embeddings=payload)
        if failure == "reference_down":
            g.registry_error = requests.ConnectionError("down")
        elif failure == "mismatch":
            g.embeddings = embeddings_payload(model="legacy-768", task="document", pipeline_id=UNKNOWN_PIPELINE_ID)
        else:
            g.embeddings = embeddings_payload(model="legacy-768", task="document", identity_verified=False)
        g.install(monkeypatch)
        with pytest.raises(GatewayEmbeddingsError):
            emb.embed_texts_with_identity(["x"], dim=DIM, provider="ai_gateway", model="legacy-768",
                                          task=ep.TASK_DOCUMENT)



# =========================================================================
# §33, §34 — chemin de requête : famille compatible avec les fragments existants,
# leg vectoriel réellement exécuté (pas de repli lexical)
# =========================================================================
@pytest.mark.django_db
class TestQueryPathAgainstExistingVectors:
    HISTORICAL_PIPELINE_ID = "legacy-768@0a109f42/document-v1"  # verbatim, tel que persisté

    @pytest.fixture
    def space(self, db):
        from apps.rag_core.models import RagSpace
        from apps.workspaces.models import Workspace

        ws = Workspace.objects.create(name="OPQ-WS")
        return RagSpace.objects.create(workspace=ws, name="OPQ-GOV",
                                       embedding_provider="ai_gateway", embedding_model="legacy-768")

    @pytest.fixture
    def existing_chunk(self, space):
        from apps.rag_core.models import Chunk, Document
        from apps.rag_core.models.document import DocumentStatus

        doc = Document.objects.create(
            rag_space=space, filename="historique.pdf", status=DocumentStatus.READY,
            embedding_provider="ai_gateway", embedding_model="legacy-768",
        )
        return Chunk.objects.create(
            rag_space=space, document=doc, chunk_index=0, text="obligations et delais du contrat",
            page_number=1, meta={}, embedding=[0.1] * DIM,
            pipeline_id=self.HISTORICAL_PIPELINE_ID,
            pipeline_family=PERSISTED_FAMILIES["legacy-768"],
        )

    @pytest.fixture(autouse=True)
    def _no_corpus(self, monkeypatch):
        import apps.corpus.services.retrieval as cr

        monkeypatch.setattr(cr, "allowed_corpus_ids_for_workspace", lambda ws: [])

    def test_query_family_matches_historical_fragments(self, governed, monkeypatch, space, existing_chunk):
        from apps.rag_core.repositories.chunk_repository import search_chunks_vector

        FakeGateway(embeddings=embeddings_payload(model="legacy-768", task="query", vector=[0.1] * DIM)).install(monkeypatch)
        lot = emb.embed_texts_with_identity(["obligations"], dim=DIM, provider="ai_gateway",
                                            model="legacy-768", task=ep.TASK_QUERY)
        assert lot.pipeline_id == PIPELINE_IDS[("legacy-768", "query")]
        assert lot.pipeline_id != existing_chunk.pipeline_id
        assert lot.family == existing_chunk.pipeline_family
        hits = search_chunks_vector(
            rag_space_id=space.id, query_embedding=lot.vectors[0], top_k=10,
            embedding_provider="ai_gateway", embedding_model="legacy-768", pipeline_family=lot.family,
        )
        assert [h["chunk_id"] for h in hits] == [str(existing_chunk.id)]

    def _ctx(self, space):
        from apps.rag_core.services.query.contracts import (
            PreparedQuery, QueryExecutionContext, QueryRequest, RetrievalPolicy,
        )
        from apps.rag_core.services.query.trace import RetrievalTraceBuilder

        q = "obligations contractuelles"
        req = QueryRequest.from_legacy_args(rag_space_id=space.id, question=q, rag_space=space)
        ctx = QueryExecutionContext.create(
            req, started_at=0.0, trace=RetrievalTraceBuilder(entrypoint="internal", request_id="rid")
        )
        ctx.prepared = PreparedQuery.from_resolved(
            original_question=q, queries_for_search=[q], primary_retrieval_query=q,
            inventory=False, explanatory=False, news=False, has_temporal_intent=False, comparative=False,
        )
        ctx.policy = RetrievalPolicy(
            fts_required=True, hybrid_enabled=True, vector_available=True, temporal_enabled=False,
            rerank_enabled=False, diversity_enabled=False, min_sources=1, min_text_rank=0.0,
            min_score=0.0, top_k=4, vector_top_k=20, candidate_k=4, lexical_weight=1.0,
            rerank_trigger_min_candidates=1, rerank_top_k=4,
        )
        return ctx

    def _run(self, space):
        from apps.rag_core.repositories.chunk_repository import search_chunks_vector
        from apps.rag_core.services.query.candidate_retriever import LEG_VECTOR, retrieve_candidates

        ctx = self._ctx(space)
        candidates = retrieve_candidates(ctx, fts_search=lambda **k: [], vector_search=search_chunks_vector)
        trace = ctx.trace.build(final_candidate_count=len(candidates), source_count=len(candidates))
        legs = {leg.name: leg for leg in trace.legs}
        return candidates, legs[LEG_VECTOR], trace

    def test_vector_leg_succeeds_without_lexical_fallback(self, governed, monkeypatch, space, existing_chunk):
        """VECTOR_PATH_SUCCEEDED, et non VECTOR_FAILED_AND_LEXICAL_FALLBACK_USED : la
        distinction est lue dans la TRACE, jamais déduite du seul résultat."""
        from apps.rag_core.services.query.trace import RetrievalLegStatus

        FakeGateway(embeddings=embeddings_payload(model="legacy-768", task="query", vector=[0.1] * DIM)).install(monkeypatch)
        candidates, vector_leg, trace = self._run(space)
        assert vector_leg.status == RetrievalLegStatus.SUCCEEDED
        assert vector_leg.candidate_count >= 1 and vector_leg.error_kind is None
        assert "vector_error" not in trace.fallback_reasons
        assert str(existing_chunk.id) in {c["chunk_id"] for c in candidates}

    def test_control_a_rejected_pair_still_degrades_to_lexical_and_is_visible(
        self, governed, monkeypatch, space, existing_chunk
    ):
        """Témoin : le test précédent DISTINGUE bien les deux issues — avec l'ancienne
        paire incohérente (identifiant query ≠ référence), le leg vectoriel est dégradé
        et la raison de repli est tracée."""
        from apps.rag_core.services.query.trace import RetrievalLegStatus

        FakeGateway(embeddings=embeddings_payload(model="legacy-768", task="query", vector=[0.1] * DIM,
                                                  pipeline_id=UNKNOWN_PIPELINE_ID)).install(monkeypatch)
        candidates, vector_leg, trace = self._run(space)
        assert vector_leg.status == RetrievalLegStatus.DEGRADED
        assert "vector_error" in trace.fallback_reasons
        assert candidates == [] or str(existing_chunk.id) not in {c["chunk_id"] for c in candidates}


# =========================================================================
# Chemin document : l'indexation persiste l'identifiant opaque ET la famille attestée
# =========================================================================
@pytest.mark.django_db
class TestDocumentPathPersistence:
    def test_indexing_persists_opaque_pipeline_id_and_attested_family(self, governed, monkeypatch):
        from apps.rag_core.models import Chunk, Document, RagSpace
        from apps.rag_core.models.document import DocumentStatus
        from apps.rag_core.services.ingest.extraction import ExtractionResult, ExtractionStatus
        from apps.rag_core.services.ingest.indexing import index_extraction_result
        from apps.workspaces.models import Workspace

        ws = Workspace.objects.create(name="OPQ-DOC-WS")
        space = RagSpace.objects.create(workspace=ws, name="OPQ-DOC", embedding_provider="ai_gateway",
                                        embedding_model="legacy-768")
        doc = Document.objects.create(rag_space=space, filename="d.txt", status=DocumentStatus.PENDING,
                                      embedding_provider="ai_gateway", embedding_model="legacy-768")
        g = FakeGateway(embeddings=None).install(monkeypatch)

        def _post(url, headers=None, json=None, timeout=None):
            # Autant de vecteurs que de textes du lot : la Gateway répond au lot reçu.
            g.posts.append({"url": url, "payload": json})
            return Resp(embeddings_payload(model="legacy-768", task="document", n=len(json["texts"])))

        monkeypatch.setattr(gw.requests, "post", _post)
        result = ExtractionResult(text="phrase de test. " * 60, status=ExtractionStatus.SUCCESS,
                                  method="test", detected_mime="text/plain")
        res = index_extraction_result(doc=doc, space=space, result=result, filename="d.txt",
                                      chunk_size=300, overlap=50, dim=DIM)
        assert res.status == DocumentStatus.READY, Document.objects.get(id=doc.id).error_message
        chunks = list(Chunk.objects.filter(document=doc))
        assert chunks
        assert {c.pipeline_id for c in chunks} == {PIPELINE_IDS[("legacy-768", "document")]}
        assert {c.pipeline_family for c in chunks} == {PERSISTED_FAMILIES["legacy-768"]}
        assert all(p["payload"]["task"] == "document" for p in g.posts)

    def test_indexing_refuses_a_governed_batch_without_family(self, governed, monkeypatch):
        """Garde d'invariant : un lot gouverné sans famille attestée n'est jamais publié."""
        from apps.rag_core.models import Chunk, Document, RagSpace
        from apps.rag_core.models.document import DocumentStatus
        from apps.rag_core.services.ingest import indexing
        from apps.rag_core.services.ingest.extraction import ExtractionResult, ExtractionStatus
        from apps.workspaces.models import Workspace

        ws = Workspace.objects.create(name="OPQ-NOFAM-WS")
        space = RagSpace.objects.create(workspace=ws, name="OPQ-NOFAM", embedding_provider="ai_gateway",
                                        embedding_model="legacy-768")
        doc = Document.objects.create(rag_space=space, filename="d.txt", status=DocumentStatus.PENDING,
                                      embedding_provider="ai_gateway", embedding_model="legacy-768")

        def _no_family(texts, dim=None, **kw):
            return gw.EmbeddingBatch(vectors=[[0.1] * DIM for _ in texts], pipeline_id=PIPELINE_IDS[("legacy-768", "document")],
                                     dimension=DIM, task="document", governed=True)

        monkeypatch.setattr(indexing, "embed_texts_with_identity", _no_family)
        result = ExtractionResult(text="phrase de test. " * 30, status=ExtractionStatus.SUCCESS,
                                  method="test", detected_mime="text/plain")
        res = indexing.index_extraction_result(doc=doc, space=space, result=result, filename="d.txt",
                                               chunk_size=300, overlap=50, dim=DIM)
        assert res.status == DocumentStatus.FAILED
        assert Document.objects.get(id=doc.id).error_message == "pipeline_family_unresolved"
        assert Chunk.objects.filter(document=doc).count() == 0
