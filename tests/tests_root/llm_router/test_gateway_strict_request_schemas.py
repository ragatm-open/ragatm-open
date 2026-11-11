"""corps Gateway valides sur DEUX builds.

La promotion de production suit un ordre imposé : RAGatm d'abord, sur la Gateway en
service (build antérieur, schémas OUVERTS), puis la Gateway stricte (build strict, schémas FERMÉS,
400 ``extra_forbidden``). Tout corps émis par RAGatm doit donc être accepté par les deux.

Les schémas viennent d'un relevé MINIMAL de ``/openapi.json`` des deux builds
(``_gateway_request_schemas.json`` : seulement les corps de ``/v1/generate``,
``/v1/generate/stream``, ``/v1/embeddings`` et ``/v1/rerank``). Aucun réseau.

Trois familles de tests :

1. le relevé lui-même — il fige la raison du chantier (build antérieur ouvert, build strict fermé) ;
2. chaque constructeur RÉEL, capturé à sa frontière de transport, validé sur les deux
   builds — y compris la commande de calibration ``verify_negative_absence``, dernière
   à sérialiser ``provider`` ;
3. une garde statique : dans une fonction qui adresse l'une de ces routes, aucun corps
   ne porte ``provider``, ``stream`` ou ``request_id``. Ces noms restent légitimes
   partout ailleurs (routage, DTO interne, audit, vision).
"""
from __future__ import annotations

import ast
import copy
import json
import pathlib

import pytest

from apps.llm_router import embeddings as emb
from apps.llm_router import embeddings_gateway as emb_gw
from apps.llm_router.embedding_errors import GatewayEmbeddingsError
from apps.llm_router.gateway import client as gw_client
from apps.llm_router.gateway.client import AIGatewayClient
from apps.llm_router.gateway.dto import GatewayGenerationRequest
from apps.rag_core.services import rerank_gateway as RG

_REPO = pathlib.Path(__file__).resolve().parents[3]
_SNAPSHOT = json.loads(
    pathlib.Path(__file__).with_name("_gateway_request_schemas.json").read_text(encoding="utf-8")
)
PROD, STRICT = "0a0a0a0a", "5b5b5b5b"
BUILDS = (PROD, STRICT)

#: Champs internes de RAGatm qui n'appartiennent à AUCUN de ces quatre contrats.
FORBIDDEN_WIRE_KEYS = frozenset({"provider", "stream", "request_id"})
#: Routes couvertes. `/v1/vision/analyze` en est exclue : son schéma admet `request_id`.
GATEWAY_ROUTES = ("/v1/generate", "/v1/embeddings", "/v1/rerank")


# --------------------------------------------------------------------------- #
# Validation contre le relevé
# --------------------------------------------------------------------------- #
_TYPES = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "null": lambda v: v is None,
}


def _value_errors(build: dict, spec: dict, value, where: str) -> list[str]:
    if "$ref" in spec:
        return _object_errors(build, spec["$ref"].rsplit("/", 1)[-1], value, where)
    options = spec.get("anyOf") or [spec]
    if len(options) > 1:
        ok = any(not _value_errors(build, o, value, where) for o in options)
        return [] if ok else [f"type:{where}"]
    o = options[0]
    if "type" in o and not _TYPES[o["type"]](value):
        return [f"type:{where}"]
    if "enum" in o and value not in o["enum"]:
        return [f"enum:{where}"]
    if "const" in o and value != o["const"]:
        return [f"const:{where}"]
    if o.get("type") == "array":
        errs = [] if len(value) >= o.get("minItems", 0) else [f"min_items:{where}"]
        for i, item in enumerate(value):
            errs += _value_errors(build, o.get("items", {}), item, f"{where}[{i}]")
        return errs
    if o.get("type") == "string" and len(value) < o.get("minLength", 0):
        return [f"min_length:{where}"]
    return []


def _object_errors(build: dict, name: str, body, where: str) -> list[str]:
    schema = build["schemas"][name]
    if not isinstance(body, dict):
        return [f"type:{where}"]
    errs = [f"missing:{where}.{k}" for k in schema["required"] if k not in body]
    for key, value in body.items():
        spec = schema["properties"].get(key)
        if spec is None:
            if schema["additionalProperties"] is False:
                errs.append(f"extra_forbidden:{where}.{key}")
            continue
        errs += _value_errors(build, spec, value, f"{where}.{key}")
    return errs


def violations(build_id: str, route: str, body: dict) -> list[str]:
    """Écarts d'un corps au schéma de ``route`` pour ce build (vide = ACCEPTED)."""
    build = _SNAPSHOT["builds"][build_id]
    return _object_errors(build, build["routes"][route], body, "body")


def assert_accepted_everywhere(route: str, body: dict) -> None:
    assert not (set(body) & FORBIDDEN_WIRE_KEYS), f"champ interne sur le fil : {sorted(body)}"
    for build_id in BUILDS:
        assert violations(build_id, route, body) == [], (build_id, route, sorted(body))


# --------------------------------------------------------------------------- #
# Doublures de transport
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, payload=None, *, status=200, lines=None):
        self._payload = payload if payload is not None else {}
        self.status_code = status
        self.text = ""
        self._lines = lines or []
        self.raw = None

    def json(self):
        return copy.deepcopy(self._payload)

    def iter_lines(self, decode_unicode=False):
        yield from self._lines

    def close(self):
        pass


class _Capture:
    """Relève (url, corps) puis répond ce qu'on lui a dit de répondre."""

    def __init__(self, response):
        self.calls: list[tuple[str, dict]] = []
        self._response = response

    def __call__(self, url, headers=None, json=None, timeout=None, stream=False):
        self.calls.append((url, copy.deepcopy(json)))
        return self._response

    @property
    def only(self) -> tuple[str, dict]:
        assert len(self.calls) == 1, f"attendu 1 requête, vu {len(self.calls)}"
        return self.calls[0]


class _UrlopenResp:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._raw

    def getcode(self):
        return 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


_STREAM_LINES = [
    json.dumps({"type": "delta", "delta": "x"}).encode(),
    json.dumps({"type": "done", "model": "m", "tokens": {"total": 1},
                "usage": {"total_tokens": 1}}).encode(),
]
_UNARY_OK = {"text": "ok", "provider": "ai_gateway", "model": "m",
             "tokens": {"total": 1}, "usage": {"total_tokens": 1}}


def _generation_requests():
    """DTO riche : l'état interne porte `provider`, `stream`, `request_id`."""
    base = dict(prompt="Question ?", provider="ollama", model="qwen3:30b-instruct",
                options={"num_ctx": 8192, "temperature": 0.1}, request_id="rid-1",
                system="Tu es un assistant.", think=False)
    variantes = [base]
    champs = GatewayGenerationRequest.__dataclass_fields__
    # `format`/`temperature` n'existent que sur les lignées qui les portent.
    if "format" in champs and "temperature" in champs:
        variantes.append(dict(base, format={"type": "object"}, temperature=0.0))
    return variantes


# =========================================================================== #
# 1. Le relevé fige la raison du chantier
# =========================================================================== #
class TestSnapshot:
    def test_both_builds_are_recorded_for_the_four_routes(self):
        for build_id in BUILDS:
            routes = _SNAPSHOT["builds"][build_id]["routes"]
            assert routes == {
                "/v1/generate": "GenerateRequest",
                "/v1/generate/stream": "GenerateRequest",
                "/v1/embeddings": "EmbeddingsRequest",
                "/v1/rerank": "RerankRequest",
            }

    def test_production_build_is_open_and_strict_build_is_closed(self):
        for name, schema in _SNAPSHOT["builds"][PROD]["schemas"].items():
            assert schema["additionalProperties"] is not False, name
        for name, schema in _SNAPSHOT["builds"][STRICT]["schemas"].items():
            assert schema["additionalProperties"] is False, name

    def test_no_forbidden_key_belongs_to_any_contract(self):
        for build_id in BUILDS:
            for name, schema in _SNAPSHOT["builds"][build_id]["schemas"].items():
                assert not (set(schema["properties"]) & FORBIDDEN_WIRE_KEYS), (build_id, name)


# =========================================================================== #
# 2. Régression directe : les formes historiques
# =========================================================================== #
class TestHistoricalShapes:
    def test_historical_embeddings_body_is_rejected_by_the_strict_build_only(self):
        corps = {"provider": "ollama", "model": "nomic-embed-text", "texts": ["t"]}
        assert violations(PROD, "/v1/embeddings", corps) == []
        assert violations(STRICT, "/v1/embeddings", corps) == ["extra_forbidden:body.provider"]

    def test_historical_generation_body_is_rejected_by_the_strict_build_only(self):
        corps = {"prompt": "P", "stream": True, "provider": "ollama", "model": "m",
                 "request_id": "rid"}
        for route in ("/v1/generate", "/v1/generate/stream"):
            assert violations(PROD, route, corps) == []
            assert sorted(violations(STRICT, route, corps)) == [
                "extra_forbidden:body.provider",
                "extra_forbidden:body.request_id",
                "extra_forbidden:body.stream",
            ]

    def test_historical_calibration_body_is_rejected_by_the_strict_build_only(self):
        corps = {"provider": "ollama", "model": "qwen3:14b", "prompt": "P",
                 "options": {"num_predict": 120, "temperature": 0.0}}
        assert violations(PROD, "/v1/generate", corps) == []
        assert violations(STRICT, "/v1/generate", corps) == ["extra_forbidden:body.provider"]

    def test_the_validator_is_not_vacuous(self):
        assert violations(STRICT, "/v1/rerank", {"query": "q", "documents": []}) == [
            "min_items:body.documents"]
        assert violations(STRICT, "/v1/embeddings", {"texts": ["t"], "task": "describe"}) == [
            "type:body.task"]
        assert violations(STRICT, "/v1/generate", {"model": "m"}) == ["missing:body.prompt"]


# =========================================================================== #
# 3. Constructeurs RÉELS, capturés à leur frontière de transport
# =========================================================================== #
class TestEmbeddingsConstructors:

    @pytest.mark.parametrize("task", ["document", "query"])
    def test_governed_path(self, monkeypatch, settings, task):
        settings.AI_GATEWAY_EMBEDDINGS_ENABLED = True
        settings.AI_GATEWAY_EMBEDDINGS_BASE_URL = "http://gw.test:8200"
        transport = _Capture(_Resp({}, status=503))
        monkeypatch.setattr(emb_gw.requests, "post", transport)

        # Seul le corps émis compte ici : la réponse factice est refusée en aval.
        with pytest.raises(GatewayEmbeddingsError):
            emb_gw.embed_via_gateway(["passage"], model="legacy-768", task=task, dim=768)

        url, corps = transport.only
        assert url.endswith("/v1/embeddings")
        assert corps == {"model": "legacy-768", "task": task, "texts": ["passage"]}
        assert_accepted_everywhere("/v1/embeddings", corps)


class TestGenerationConstructors:
    @pytest.mark.parametrize("kwargs", _generation_requests())
    def test_unary(self, monkeypatch, kwargs):
        transport = _Capture(_Resp(_UNARY_OK))
        monkeypatch.setattr(gw_client.requests, "post", transport)

        AIGatewayClient(base_url="http://gw.test", timeout_s=5.0, enabled=True).generate(
            GatewayGenerationRequest(**kwargs))

        url, corps = transport.only
        assert url.endswith("/v1/generate")
        assert_accepted_everywhere("/v1/generate", corps)

    @pytest.mark.parametrize("kwargs", _generation_requests())
    def test_stream(self, monkeypatch, kwargs):
        transport = _Capture(_Resp(lines=_STREAM_LINES))
        monkeypatch.setattr(gw_client.requests, "post", transport)

        req = GatewayGenerationRequest(**dict(kwargs, stream=True))
        list(AIGatewayClient(base_url="http://gw.test", timeout_s=5.0,
                             enabled=True).generate_stream(req))

        url, corps = transport.only
        assert url.endswith("/v1/generate/stream")
        assert_accepted_everywhere("/v1/generate/stream", corps)


class TestRerankConstructor:
    def test_body(self, monkeypatch, settings):
        settings.RAG_RERANKER_BASE_URL = "http://gw.test"
        captured = {}

        def _urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _UrlopenResp({"results": []})

        monkeypatch.setattr(RG.urllib.request, "urlopen", _urlopen)
        RG._post_rerank("question ?", [{"id": "c1", "text": "passage"}])

        assert captured["url"].endswith("/v1/rerank")
        assert_accepted_everywhere("/v1/rerank", captured["body"])



# =========================================================================== #
# 4. Garde statique, limitée aux fonctions qui adressent ces routes
# =========================================================================== #
def _docstrings(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                ids.add(id(first.value))
    return ids


def _own_nodes(fn: ast.AST):
    """Nœuds de ``fn`` sans ceux des fonctions ou classes imbriquées."""
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def forbidden_body_keys(source: str) -> list[tuple[str, int, str]]:
    """(fonction, ligne, clé) pour chaque clé interdite d'un corps adressé à la Gateway.

    Une fonction est concernée si elle contient, hors docstring, une chaîne nommant une
    des routes. Sont inspectés : dictionnaires littéraux, ``dict(...)`` et affectations
    ``corps["clé"] = …``.
    """
    tree = ast.parse(source)
    docs = _docstrings(tree)
    hits = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        nodes = list(_own_nodes(fn))
        if not any(isinstance(n, ast.Constant) and isinstance(n.value, str)
                   and id(n) not in docs and any(r in n.value for r in GATEWAY_ROUTES)
                   for n in nodes):
            continue
        for n in nodes:
            if isinstance(n, ast.Dict):
                keys = [k.value for k in n.keys if isinstance(k, ast.Constant)]
            elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "dict":
                keys = [k.arg for k in n.keywords]
            elif (isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Store)
                  and isinstance(n.slice, ast.Constant)):
                keys = [n.slice.value]
            else:
                continue
            hits += [(fn.name, n.lineno, k) for k in keys if k in FORBIDDEN_WIRE_KEYS]
    return hits


class TestStaticGuard:
    def _production_sources(self):
        backend = _REPO / "backend"
        for chemin in sorted([*backend.joinpath("apps").rglob("*.py"),
                              *backend.joinpath("api").rglob("*.py")]):
            if "migrations" not in chemin.parts:
                yield chemin

    def test_no_gateway_body_carries_an_internal_field(self):
        fautes = []
        for chemin in self._production_sources():
            for fn, ligne, cle in forbidden_body_keys(chemin.read_text(encoding="utf-8")):
                fautes.append(f"{chemin.relative_to(_REPO)}:{ligne} {fn}() -> {cle!r}")
        assert fautes == []

    @pytest.mark.parametrize("source, attendu", [
        # verify_negative_absence avant BACKPORT-1B
        ('def _judge(base):\n'
         '    body = json.dumps({"provider": "ollama", "model": "m", "prompt": "p"})\n'
         '    return urllib.request.Request(f"{base}/v1/generate", data=body)\n',
         [("_judge", 2, "provider")]),
        # _gateway_embed avant 5e79c50
        ('def _gateway_embed(texts, base_url):\n'
         '    url = f"{base_url}/v1/embeddings"\n'
         '    payload = {"provider": "ollama", "model": "m", "texts": texts}\n',
         [("_gateway_embed", 3, "provider")]),
        # corps de génération historique, construit par affectations
        ('def send(base):\n'
         '    payload = dict(prompt="p", stream=True)\n'
         '    payload["request_id"] = "r"\n'
         '    return post(base + "/v1/generate/stream", json=payload)\n',
         [("send", 2, "stream"), ("send", 3, "request_id")]),
    ])
    def test_the_guard_catches_the_historical_shapes(self, source, attendu):
        assert sorted(forbidden_body_keys(source)) == sorted(attendu)

    @pytest.mark.parametrize("source", [
        # un docstring qui cite la route ne rend pas la fonction émettrice
        'def route_info():\n    """Appelle /v1/generate."""\n    return {"provider": "ai_gateway"}\n',
        # la vision admet request_id
        'def analyze(base):\n    return post(base + "/v1/vision/analyze", json={"request_id": "r"})\n',
        # un résultat applicatif hors de toute fonction émettrice
        'def result():\n    return {"provider": "ai_gateway", "stream": False}\n',
        # une fonction imbriquée n'hérite pas de la route de sa parente
        'def outer(base):\n    url = base + "/v1/embeddings"\n'
        '    def log():\n        return {"provider": "x"}\n    return url\n',
    ])
    def test_the_guard_ignores_legitimate_uses(self, source):
        assert forbidden_body_keys(source) == []
