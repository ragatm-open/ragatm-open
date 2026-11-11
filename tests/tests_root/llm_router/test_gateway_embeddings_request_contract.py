"""contrat de requête ``POST /v1/embeddings``.

Le schéma ``EmbeddingsRequest`` de la Gateway v2 est FERMÉ
(``additionalProperties: false``) et n'accepte que ``texts``, ``model``, ``task`` et
``keep_alive``. ``provider`` n'en a **jamais** fait partie : un build ancien l'ignorait
en silence, un build récent le rejette en 400 ``extra_forbidden`` — et l'ingestion
échouait après que tout le reste (extraction, OCR, vision) eut réussi.

Ces tests verrouillent deux choses de nature différente :

1. la RÉPARATION — un seul corps canonique, tâche explicite, jamais de ``provider`` ;
2. ce que la réparation ne doit PAS emporter — l'espace historique garde son modèle
   CONCRET et son identité de pipeline LEGACY. **Aligner le fil n'est pas migrer le
   pipeline.**

Aucun réseau : ``requests.post`` est remplacé à la frontière.
"""
from __future__ import annotations

import copy
import pathlib
import re

import pytest

from apps.llm_router import embedding_pipeline as ep
from apps.llm_router import embeddings as emb
from apps.llm_router import embeddings_gateway as gw
from apps.llm_router.embeddings_gateway import build_gateway_embeddings_request

#: Dimension réelle de la colonne pgvector : les tests qui écrivent en base doivent
#: la respecter, sous peine de tester autre chose que le chemin de production.
DIM = 768

#: Le contrat FERMÉ, tel que publié par l'OpenAPI de la Gateway.
CLOSED_SCHEMA_KEYS = {"texts", "model", "task", "keep_alive"}


# --------------------------------------------------------------------------- #
# Doublures
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, payload, status=200, text=""):
        self._payload, self.status_code, self.text = payload, status, text

    def json(self):
        return copy.deepcopy(self._payload)


class _StrictGateway:
    """Transport factice qui APPLIQUE le schéma fermé.

    C'est le cœur de la non-régression : une doublure permissive laisserait repasser
    le défaut sans que rien n'échoue. Celle-ci refuse tout champ hors contrat,
    exactement comme le build qui a fait tomber l'ingestion en conditions réelles.
    """

    def __init__(self, *, dim=DIM):
        self.dim = dim
        self.payloads: list[dict] = []

    def __call__(self, url, headers=None, json=None, timeout=None):
        self.payloads.append(copy.deepcopy(json))
        extras = sorted(set(json) - CLOSED_SCHEMA_KEYS)
        if extras:
            return _Resp(
                {
                    "detail": {
                        "errors": [
                            {
                                "type": "extra_forbidden",
                                "loc": ["body", nom],
                                "msg": "Extra inputs are not permitted",
                            }
                            for nom in extras
                        ]
                    }
                },
                status=400,
                text=f"extra_forbidden: {extras}",
            )
        return _Resp({"embeddings": [[0.05] * self.dim for _ in json["texts"]]})

    @property
    def only(self) -> dict:
        assert len(self.payloads) == 1, f"attendu 1 requête, vu {len(self.payloads)}"
        return self.payloads[0]


@pytest.fixture
def strict(monkeypatch):
    transport = _StrictGateway()
    monkeypatch.setattr(emb.requests, "post", transport)
    return transport


# =========================================================================== #
# §12/§13 — Le constructeur canonique
# =========================================================================== #
class TestCanonicalBuilder:
    def test_keys_are_exactly_the_contract_without_keep_alive(self):
        corps = build_gateway_embeddings_request(
            texts=["a"], model="nomic-embed-text", task="document"
        )
        assert set(corps) == {"texts", "model", "task"}
        assert corps == {"texts": ["a"], "model": "nomic-embed-text", "task": "document"}

    def test_keep_alive_is_emitted_only_when_supplied(self):
        """Émettre `keep_alive: null` par défaut ajouterait un champ sans intention."""
        sans = build_gateway_embeddings_request(texts=["a"], model="m", task="query")
        assert "keep_alive" not in sans

        avec = build_gateway_embeddings_request(
            texts=["a"], model="m", task="query", keep_alive="5m"
        )
        assert set(avec) == {"texts", "model", "task", "keep_alive"}
        assert avec["keep_alive"] == "5m"

    def test_never_emits_provider(self):
        corps = build_gateway_embeddings_request(texts=["a"], model="m", task="document")
        assert "provider" not in corps

    def test_output_is_always_within_the_closed_schema(self):
        for task in ("document", "query"):
            for keep_alive in (None, "10m"):
                corps = build_gateway_embeddings_request(
                    texts=["a"], model="m", task=task, keep_alive=keep_alive
                )
                assert set(corps) <= CLOSED_SCHEMA_KEYS

    def test_invalid_task_is_refused_before_any_byte(self):
        with pytest.raises(gw.GatewayEmbeddingsError) as exc:
            build_gateway_embeddings_request(texts=["a"], model="m", task="describe")
        assert exc.value.code == "invalid_task"

    def test_empty_model_is_refused(self):
        with pytest.raises(gw.GatewayEmbeddingsError) as exc:
            build_gateway_embeddings_request(texts=["a"], model="  ", task="document")
        assert exc.value.code == "model_missing"


# =========================================================================== #
# §43 — Régression DIRECTE de l'ancien défaut
# =========================================================================== #
class TestOldDefectRegression:
    def test_the_strict_double_really_rejects_the_old_shape(self, strict):
        """Garde de la garde : si la doublure était permissive, tout ce fichier mentirait."""
        reponse = strict(
            "http://gw.test:8100/v1/embeddings",
            json={"provider": "ollama", "model": "nomic-embed-text", "texts": ["x"]},
        )
        assert reponse.status_code == 400
        assert "extra_forbidden" in reponse.text


# =========================================================================== #
# §42 — Unicité du constructeur dans le code de production
# =========================================================================== #
class TestSingleRequestBuilder:
    #: Seule exception admise : la sonde de charge d'EXPLOITATION, qui n'indexe rien
    #: et ne persiste aucun vecteur. Elle est listée ici pour qu'une troisième forme
    #: de requête ne puisse pas apparaître sans qu'un test le dise.
    SONDE_DIAGNOSTIC = "gateway_load.py"

    def _modules_parlant_embeddings(self) -> list[pathlib.Path]:
        racine = pathlib.Path(__file__).resolve().parents[3] / "backend" / "apps"
        return [
            chemin
            for chemin in racine.rglob("*.py")
            if "/v1/embeddings" in chemin.read_text(encoding="utf-8")
        ]

    def test_no_production_module_serialises_provider_into_embeddings(self):
        for chemin in self._modules_parlant_embeddings():
            texte = chemin.read_text(encoding="utf-8")
            corps = re.findall(r"^\s*(?:payload|corps)\s*(?::[^=]+)?=\s*.+$", texte, re.M)
            for ligne in corps:
                assert '"provider"' not in ligne, f"{chemin.name}: {ligne.strip()}"


    def test_no_third_request_shape_appears(self):
        constructeurs = []
        for chemin in self._modules_parlant_embeddings():
            texte = chemin.read_text(encoding="utf-8")
            if chemin.name == self.SONDE_DIAGNOSTIC:
                continue
            if re.search(r'\{\s*"(?:model|provider|texts)"\s*:', texte):
                if "build_gateway_embeddings_request" not in texte:
                    constructeurs.append(chemin.name)
        assert constructeurs == [], f"forme de requête non canonique: {constructeurs}"


