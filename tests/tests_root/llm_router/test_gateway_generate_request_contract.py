"""Rétroportage canonique — contrat de requête ``/v1/generate`` (lignée de production).

Le schéma ``GenerateRequest`` de la passerelle candidate est FERMÉ
(``extra="forbid"``). Il n'accepte que ``prompt``, ``model``, ``options``,
``system``, ``think``, ``format``, ``temperature`` et ``keep_alive``.

Cette lignée sérialisait en plus ``stream``, ``provider`` et ``request_id`` — trois
champs qui n'ont jamais appartenu à ce contrat. La passerelle en service les ignore
en silence ; la candidate les refuse en 400 ``extra_forbidden``, ce qui ferait
échouer toute génération dès sa promotion.

Ces tests verrouillent deux choses de nature différente :

1. la RÉPARATION — un seul corps canonique, identique en unaire et en flux ;
2. ce qu'elle ne doit PAS emporter — l'état interne du DTO reste riche.

Aucun réseau : ``requests.post`` est remplacé à la frontière du client.
"""
import copy
import json

import pytest

import apps.llm_router.gateway.client as gw_client
from apps.llm_router.gateway import AIGatewayClient, GatewayGenerationRequest

#: Contrat FERMÉ de la passerelle candidate. Cette lignée n'expose ni `format`,
#: ni `temperature`, ni `keep_alive` : l'assertion est une BORNE SUPÉRIEURE.
CANONICAL_KEYS = {"prompt", "model", "options", "system", "think", "format",
                  "temperature", "keep_alive"}

#: Champs internes de routage/corrélation qui ne doivent jamais descendre.
FORBIDDEN_WIRE_KEYS = {"stream", "provider", "request_id"}

_DONE = [json.dumps({"type": "delta", "delta": "x"}).encode(),
         json.dumps({"type": "done", "model": "m", "tokens": {"total": 1},
                     "usage": {"total_tokens": 1}}).encode()]


class _Resp:
    def __init__(self, payload=None, *, status=200, lines=None, text=""):
        self._payload = payload if payload is not None else {}
        self.status_code = status
        self.text = text
        self._lines = lines or []
        self.raw = None

    def json(self):
        return copy.deepcopy(self._payload)

    def iter_lines(self, decode_unicode=False):
        yield from self._lines

    def close(self):
        pass


class _StrictGateway:
    """Transport factice qui APPLIQUE le schéma fermé.

    Une doublure permissive laisserait repasser le défaut sans que rien n'échoue :
    celle-ci refuse tout champ hors contrat, comme la passerelle candidate.
    """

    def __init__(self, lines=None):
        self.calls = []
        self._lines = lines

    def __call__(self, url, headers=None, json=None, timeout=None, stream=False):
        self.calls.append({"url": url, "json": copy.deepcopy(json), "stream": stream})
        extras = sorted(set(json) - CANONICAL_KEYS)
        if extras:
            return _Resp({"detail": {"errors": [
                {"type": "extra_forbidden", "loc": ["body", nom],
                 "msg": "Extra inputs are not permitted"} for nom in extras]}},
                status=400, text="extra_forbidden: %s" % extras)
        if self._lines is not None:
            return _Resp(lines=self._lines)
        return _Resp({"text": "ok", "provider": "ai_gateway", "model": "m",
                      "tokens": {"total": 1}, "usage": {"total_tokens": 1}})

    @property
    def only(self):
        assert len(self.calls) == 1, "attendu 1 requête, vu %d" % len(self.calls)
        return self.calls[0]


def _client():
    return AIGatewayClient(base_url="http://gw.test:8200", timeout_s=30.0, enabled=True)


def _req(**kw):
    base = dict(prompt="Question ?", provider="ollama", model="qwen3:14b",
                options={"num_ctx": 8192, "temperature": 0.1}, think=False)
    base.update(kw)
    return GatewayGenerationRequest(**base)


# --- Sérialiseur canonique -------------------------------------------------

def test_le_corps_ne_porte_plus_les_champs_internes():
    p = _req(request_id="rid-42", stream=True).to_payload()
    assert p["prompt"] == "Question ?" and p["model"] == "qwen3:14b"
    for interdit in sorted(FORBIDDEN_WIRE_KEYS):
        assert interdit not in p


def test_prompt_seul():
    assert GatewayGenerationRequest(prompt="p").to_payload() == {"prompt": "p"}


@pytest.mark.parametrize("variante", [
    {}, {"options": {}}, {"system": "sys"}, {"think": True},
    {"request_id": "rid"}, {"stream": True}, {"provider": "openai"},
])
def test_allowset_respecte(variante):
    payload = _req(**variante).to_payload()
    assert set(payload) <= CANONICAL_KEYS, "hors contrat : %s" % (set(payload) - CANONICAL_KEYS)


def test_champs_canoniques_preserves():
    p = _req(system="sys", think=True).to_payload()
    assert p["options"] == {"num_ctx": 8192, "temperature": 0.1}
    assert p["system"] == "sys" and p["think"] is True


def test_think_false_reste_emis():
    """`think=False` est une VALEUR, pas une absence : elle doit descendre."""
    assert _req(think=False).to_payload()["think"] is False


# --- Routes : le flux est porté par l'URL, jamais par le corps -------------

def test_meme_corps_routes_differentes(monkeypatch):
    req = _req()

    unaire = _StrictGateway()
    monkeypatch.setattr(gw_client.requests, "post", unaire)
    _client().generate(req)

    flux = _StrictGateway(lines=_DONE)
    monkeypatch.setattr(gw_client.requests, "post", flux)
    list(_client().generate_stream(req))

    assert unaire.only["json"] == flux.only["json"]
    assert unaire.only["url"].endswith("/v1/generate")
    assert flux.only["url"].endswith("/v1/generate/stream")
    assert "stream" not in unaire.only["json"] and "stream" not in flux.only["json"]


def test_le_transport_en_flux_reste_actif(monkeypatch):
    flux = _StrictGateway(lines=_DONE)
    monkeypatch.setattr(gw_client.requests, "post", flux)
    evenements = list(_client().generate_stream(_req()))
    assert flux.only["stream"] is True, "le flux HTTP reste demandé à requests"
    assert "stream" not in flux.only["json"], "mais jamais annoncé dans le corps"
    assert [e["type"] for e in evenements] == ["delta", "result"]


# --- Passerelle stricte / passerelle en service ---------------------------

def test_candidate_stricte_accepte_lunaire(monkeypatch):
    monkeypatch.setattr(gw_client.requests, "post", _StrictGateway())
    assert _client().generate(_req()).text == "ok"


def test_candidate_stricte_accepte_le_flux(monkeypatch):
    monkeypatch.setattr(gw_client.requests, "post", _StrictGateway(lines=_DONE))
    assert list(_client().generate_stream(_req()))[-1]["type"] == "result"


@pytest.mark.parametrize("interdit", sorted(FORBIDDEN_WIRE_KEYS))
def test_regression_lincident_reel(interdit):
    """Documente la panne : ces champs faisaient rejeter la requête en 400.

    On soumet à la MÊME doublure le corps canonique augmenté du champ fautif :
    elle doit refuser. C'est la preuve que la doublure mord réellement, donc que
    les tests ci-dessus ne passent pas par permissivité.
    """
    corps = dict(_req().to_payload())
    corps[interdit] = "peu importe"
    reponse = _StrictGateway()("http://gw.test:8200/v1/generate", json=corps)
    assert reponse.status_code == 400
    assert "extra_forbidden" in reponse.text and interdit in reponse.text


def test_passerelle_en_service_accepte_le_meme_corps(monkeypatch):
    """Le schéma en service est un SUR-ENSEMBLE : il accepte les champs canoniques.

    Le rétroportage ne retire aucun champ que la passerelle actuelle exigeait —
    seulement des champs qu'elle ignorait. Le corps canonique convient donc aux
    deux builds : c'est le pont de compatibilité recherché.
    """
    class _Permissive(_StrictGateway):
        def __call__(self, url, headers=None, json=None, timeout=None, stream=False):
            self.calls.append({"url": url, "json": copy.deepcopy(json), "stream": stream})
            return _Resp({"text": "ok", "provider": "ai_gateway", "model": "m",
                          "tokens": {"total": 1}})

    permissive = _Permissive()
    monkeypatch.setattr(gw_client.requests, "post", permissive)
    assert _client().generate(_req()).text == "ok"
    assert set(permissive.only["json"]) <= CANONICAL_KEYS


# --- Non-régression de l'état interne -------------------------------------

def test_le_dto_conserve_ses_champs_internes():
    req = _req(request_id="rid-7", stream=True)
    assert req.provider == "ollama" and req.request_id == "rid-7" and req.stream is True
