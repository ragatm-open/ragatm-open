"""Streaming natif de l'AI Gateway (`/v1/generate/stream`).

Ce que ces tests verrouillent, dans l'ordre des risques du lot :

1. **Parité de payload** — le corps envoyé en mode flux est celui du mode
   non-flux, à l'octet près. Un ``num_ctx`` ou un modèle qui divergerait ferait
   recharger le runner Ollama (plusieurs secondes) à chaque bascule de régime.
2. **Contrat NDJSON** — 0..N ``delta`` puis exactement un ``done`` ; une fin de
   flux sans ``done`` est une erreur, jamais une réponse complète silencieuse.
3. **Budget TOTAL** — le timeout borne la génération entière ; il n'est pas
   renouvelé à chaque fragment.
4. **Annulation** — fermer le générateur ferme la socket, ce qui est le seul
   moyen pour la Gateway de constater la déconnexion et d'arrêter Ollama.
5. **Repli** — licite avant le premier fragment, INTERDIT après.

Aucun réseau : ``requests.post`` est remplacé par un double.
"""
from __future__ import annotations

import json

import pytest

from apps.llm_router import router
from apps.llm_router.egress import LlmExecutionContext
from apps.llm_router.gateway import (
    AIGatewayClient,
    GatewayGenerationRequest,
    GatewayStreamError,
    GatewayTimeoutError,
)
from apps.llm_router.gateway import client as client_mod

CANARY = "CONFIDENTIEL_PROMPT_CANARI_STREAM"


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------
class _FakeRaw:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeResponse:
    """Réponse en flux : expose ``iter_lines`` et trace les fermetures."""

    def __init__(self, lines: list[bytes], *, status_code: int = 200, on_line=None) -> None:
        self._lines = lines
        self.status_code = status_code
        self.raw = _FakeRaw()
        self.closed = False
        self._on_line = on_line
        self.lines_consumed = 0

    def iter_lines(self, decode_unicode: bool = False):
        for line in self._lines:
            self.lines_consumed += 1
            if self._on_line is not None:
                self._on_line()
            yield line

    def close(self) -> None:
        self.closed = True


def _ndjson(*events: dict) -> list[bytes]:
    return [json.dumps(e).encode("utf-8") for e in events]


DELTAS_THEN_DONE = _ndjson(
    {"type": "delta", "delta": "Bonjour", "done": False},
    {"type": "delta", "delta": " le", "done": False},
    {"type": "delta", "delta": " monde", "done": False},
    {
        "type": "done",
        "delta": "",
        "done": True,
        "model": "qwen3:30b-instruct",
        "tokens": {"prompt": 12, "completion": 3, "total": 15},
        "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
    },
)


def _install(monkeypatch, response: _FakeResponse) -> dict:
    """Remplace ``requests.post`` et capture l'appel."""
    captured: dict = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return response

    monkeypatch.setattr(client_mod.requests, "post", fake_post)
    return captured


def _client(timeout_s: float = 30.0) -> AIGatewayClient:
    return AIGatewayClient(base_url="http://gateway.invalid:8100", timeout_s=timeout_s, enabled=True)


def _req(**kw) -> GatewayGenerationRequest:
    base = dict(
        prompt=CANARY,
        provider="ollama",
        model="qwen3:30b-instruct",
        options={"temperature": 0.1, "num_ctx": 8192, "num_predict": 768},
        think=False,
    )
    base.update(kw)
    return GatewayGenerationRequest(**base)


# ---------------------------------------------------------------------------
# 1. Parité de payload — la garde centrale du lot
# ---------------------------------------------------------------------------
def test_payload_identique_entre_generate_et_generate_stream(monkeypatch):
    """Même requête => même corps JSON. Seul le chemin d'URL diffère."""
    req = _req()

    sync_resp = _FakeResponse([])
    sync_resp.json = lambda: {  # type: ignore[assignment]
        "text": "x", "provider": "ai_gateway", "model": "qwen3:30b-instruct",
        "tokens": {"total": 1}, "usage": {"total_tokens": 1},
    }
    captured_sync = _install(monkeypatch, sync_resp)
    _client().generate(req)

    captured_stream = _install(monkeypatch, _FakeResponse(DELTAS_THEN_DONE))
    list(_client().generate_stream(req))

    assert captured_sync["json"] == captured_stream["json"]
    assert captured_sync["url"].endswith("/v1/generate")
    assert captured_stream["url"].endswith("/v1/generate/stream")
    # Les réglages qui coûtent un rechargement de runner sont bien présents.
    assert captured_stream["json"]["options"]["num_ctx"] == 8192
    assert captured_stream["json"]["model"] == "qwen3:30b-instruct"


def test_le_mode_flux_annonce_ndjson_et_ouvre_un_vrai_flux(monkeypatch):
    captured = _install(monkeypatch, _FakeResponse(DELTAS_THEN_DONE))
    list(_client().generate_stream(_req()))
    assert captured["headers"]["Accept"] == "application/x-ndjson"
    assert captured["stream"] is True
    # Budget de connexion plafonné, budget de lecture = budget total.
    assert captured["timeout"] == (10.0, 30.0)


def test_parite_du_constructeur_de_requete_entre_les_deux_modes(monkeypatch):
    """Un seul point de construction : `_build_gateway_call` sert les deux modes."""
    monkeypatch.setattr(router, "_gateway_enabled", lambda: True)
    args = dict(
        prompt=CANARY,
        provider="ollama",
        model="qwen3:30b-instruct",
        options={"temperature": 0.1, "num_ctx": 8192, "num_predict": 768},
        think=False,
        timeout_s=42.0,
    )
    _, req = router._build_gateway_call(**args)
    assert req.to_payload()["options"]["num_ctx"] == 8192
    assert req.to_payload()["model"] == "qwen3:30b-instruct"
    assert req.to_payload()["think"] is False


# ---------------------------------------------------------------------------
# 2. Contrat NDJSON
# ---------------------------------------------------------------------------
def test_deltas_puis_resultat(monkeypatch):
    _install(monkeypatch, _FakeResponse(DELTAS_THEN_DONE))
    events = list(_client().generate_stream(_req()))

    assert [e["type"] for e in events] == ["delta", "delta", "delta", "result"]
    assert "".join(e["delta"] for e in events if e["type"] == "delta") == "Bonjour le monde"
    result = events[-1]
    assert result["model"] == "qwen3:30b-instruct"
    assert result["tokens"] == 15
    assert result["usage"]["total_tokens"] == 15
    # Le `done` ne porte pas de provider : on reprend celui que `/v1/generate`
    # renvoie dans sa réponse, sinon le même échange serait journalisé sous deux
    # fournisseurs différents selon le régime.
    assert result["provider"] == "ai_gateway"


def test_fin_de_flux_sans_done_est_une_erreur(monkeypatch):
    """Sans `done`, la réponse est tronquée : elle ne doit pas passer pour complète."""
    _install(monkeypatch, _FakeResponse(DELTAS_THEN_DONE[:2]))
    gen = _client().generate_stream(_req())
    assert next(gen)["delta"] == "Bonjour"
    assert next(gen)["delta"] == " le"
    with pytest.raises(GatewayStreamError):
        next(gen)


def test_evenement_error_de_la_gateway(monkeypatch):
    lines = DELTAS_THEN_DONE[:1] + _ndjson({"type": "error", "code": "upstream_down", "message": CANARY})
    _install(monkeypatch, _FakeResponse(lines))
    gen = _client().generate_stream(_req())
    next(gen)
    with pytest.raises(GatewayStreamError) as exc:
        next(gen)
    # Le message de la Gateway peut ré-écho l'entrée : seul le code est repris.
    assert CANARY not in str(exc.value)
    assert "upstream_down" in str(exc.value)


def test_lignes_vides_et_types_inconnus_sont_tolerees(monkeypatch):
    lines = [b""] + DELTAS_THEN_DONE[:1] + _ndjson({"type": "ping"}) + DELTAS_THEN_DONE[3:]
    _install(monkeypatch, _FakeResponse(lines))
    events = list(_client().generate_stream(_req()))
    assert [e["type"] for e in events] == ["delta", "result"]


def test_ligne_non_json_est_une_erreur_de_contrat_sans_echo(monkeypatch):
    from apps.llm_router.gateway import GatewayContractError

    _install(monkeypatch, _FakeResponse([CANARY.encode("utf-8")]))
    with pytest.raises(GatewayContractError) as exc:
        list(_client().generate_stream(_req()))
    assert CANARY not in str(exc.value)


def test_statut_http_en_erreur_est_classe_avant_toute_lecture(monkeypatch):
    from apps.llm_router.gateway import GatewayTemporaryUnavailable

    resp = _FakeResponse(DELTAS_THEN_DONE, status_code=503)
    _install(monkeypatch, resp)
    with pytest.raises(GatewayTemporaryUnavailable):
        list(_client().generate_stream(_req()))
    assert resp.lines_consumed == 0
    assert resp.raw.closed is True


def test_aucun_appel_reseau_si_la_gateway_est_desactivee(monkeypatch):
    from apps.llm_router.gateway import GatewayDisabledError

    def explode(*a, **k):
        raise AssertionError("aucune requête ne doit partir")

    monkeypatch.setattr(client_mod.requests, "post", explode)
    disabled = AIGatewayClient(base_url="http://gateway.invalid:8100", timeout_s=30.0, enabled=False)
    with pytest.raises(GatewayDisabledError):
        list(disabled.generate_stream(_req()))


# ---------------------------------------------------------------------------
# 3. Budget TOTAL (non renouvelé par fragment)
# ---------------------------------------------------------------------------
class _FakeClock:
    """Horloge pilotée : chaque lecture avance de ``step`` secondes."""

    def __init__(self, step: float) -> None:
        self.t = 0.0
        self.step = step

    def monotonic(self) -> float:
        self.t += self.step
        return self.t


def test_le_budget_est_total_et_non_par_fragment(monkeypatch):
    """Un flux de fragments individuellement rapides mais long au total expire."""
    monkeypatch.setattr(client_mod, "time", _FakeClock(step=4.0))
    # 10 s de budget, l'horloge avance de 4 s par lecture => coupure avant la fin.
    _install(monkeypatch, _FakeResponse(DELTAS_THEN_DONE))
    with pytest.raises(GatewayTimeoutError):
        list(_client(timeout_s=10.0).generate_stream(_req()))


def test_un_flux_court_reste_dans_le_budget(monkeypatch):
    monkeypatch.setattr(client_mod, "time", _FakeClock(step=0.1))
    _install(monkeypatch, _FakeResponse(DELTAS_THEN_DONE))
    events = list(_client(timeout_s=10.0).generate_stream(_req()))
    assert events[-1]["type"] == "result"


# ---------------------------------------------------------------------------
# 4. Annulation
# ---------------------------------------------------------------------------
def test_fermer_le_generateur_ferme_la_socket(monkeypatch):
    """C'est la fermeture de socket qui fait constater la déconnexion à la Gateway."""
    resp = _FakeResponse(DELTAS_THEN_DONE)
    _install(monkeypatch, resp)
    gen = _client().generate_stream(_req())
    assert next(gen)["delta"] == "Bonjour"
    assert resp.raw.closed is False
    gen.close()  # équivaut à l'abandon du consommateur (déconnexion cliente)
    assert resp.raw.closed is True
    assert resp.closed is True


def test_la_socket_est_fermee_meme_en_cas_derreur(monkeypatch):
    resp = _FakeResponse(DELTAS_THEN_DONE[:1])
    _install(monkeypatch, resp)
    with pytest.raises(GatewayStreamError):
        list(_client().generate_stream(_req()))
    assert resp.raw.closed is True


# ---------------------------------------------------------------------------
# 5. Régimes du routeur
# ---------------------------------------------------------------------------
def _ctx() -> LlmExecutionContext:
    from apps.llm_router.egress import policy_from_legacy_flags

    return LlmExecutionContext(
        operation="test.stream",
        policy=policy_from_legacy_flags(space_provider="ollama", allow_external_fallback=False),
    )


@pytest.mark.django_db
def test_regime_gateway_sans_flag_refuse_le_stream(monkeypatch):
    """Interdiction historique conservée : jamais de contournement du serveur IA."""
    monkeypatch.setattr(router, "_gateway_enabled", lambda: True)
    monkeypatch.setattr(router, "_gateway_streaming_enabled", lambda: False)
    monkeypatch.setattr(
        router, "_ollama_stream",
        lambda **k: (_ for _ in ()).throw(AssertionError("bypass Gateway interdit")),
    )
    with pytest.raises(router.LLMError) as exc:
        list(router.generate_text_stream(prompt=CANARY, context=_ctx()))
    assert "AI_GATEWAY_STREAMING_ENABLED=0" in str(exc.value)


@pytest.mark.django_db
def test_regime_gateway_avec_flag_streame(monkeypatch):
    monkeypatch.setattr(router, "_gateway_enabled", lambda: True)
    monkeypatch.setattr(router, "_gateway_streaming_enabled", lambda: True)
    _install(monkeypatch, _FakeResponse(DELTAS_THEN_DONE))
    monkeypatch.setattr(
        router, "_build_gateway_call",
        lambda **k: (_client(), _req(prompt=k["prompt"])),
    )
    events = list(router.generate_text_stream(prompt=CANARY, context=_ctx()))
    assert [e["type"] for e in events] == ["delta", "delta", "delta", "result"]


@pytest.mark.django_db
def test_un_seul_evenement_daudit_pour_tout_le_flux(monkeypatch):
    """L'audit suit la génération, pas les fragments."""
    from apps.audit.models import LlmEgressAuditEvent

    monkeypatch.setattr(router, "_gateway_enabled", lambda: True)
    monkeypatch.setattr(router, "_gateway_streaming_enabled", lambda: True)
    _install(monkeypatch, _FakeResponse(DELTAS_THEN_DONE))
    monkeypatch.setattr(router, "_build_gateway_call", lambda **k: (_client(), _req()))

    before = LlmEgressAuditEvent.objects.count()
    list(router.generate_text_stream(prompt=CANARY, context=_ctx()))
    events = LlmEgressAuditEvent.objects.all()[before:]
    assert LlmEgressAuditEvent.objects.count() - before == 1
    ev = LlmEgressAuditEvent.objects.order_by("-created_at").first()
    assert ev.gateway_attempted is True
    assert ev.status == "ok"
    # Invariant de confidentialité : aucun champ ne porte le prompt.
    assert CANARY not in json.dumps(
        {f.name: str(getattr(ev, f.name)) for f in ev._meta.fields}, ensure_ascii=False
    )
    assert len(events) >= 0  # (l'assertion utile est le compte ci-dessus)


@pytest.mark.django_db
def test_annulation_tracee_comme_telle_dans_laudit(monkeypatch):
    from apps.audit.models import LlmEgressAuditEvent

    monkeypatch.setattr(router, "_gateway_enabled", lambda: True)
    monkeypatch.setattr(router, "_gateway_streaming_enabled", lambda: True)
    _install(monkeypatch, _FakeResponse(DELTAS_THEN_DONE))
    monkeypatch.setattr(router, "_build_gateway_call", lambda **k: (_client(), _req()))

    gen = router.generate_text_stream(prompt=CANARY, context=_ctx())
    next(gen)
    gen.close()

    ev = LlmEgressAuditEvent.objects.order_by("-created_at").first()
    assert ev.status == "cancelled"


@pytest.mark.django_db
def test_repli_autorise_avant_le_premier_fragment(monkeypatch):
    """Panne transitoire AVANT tout fragment : repli contrôlé, flux cohérent."""
    monkeypatch.setattr(router, "_gateway_enabled", lambda: True)
    monkeypatch.setattr(router, "_gateway_streaming_enabled", lambda: True)

    class _Boom:
        def generate_stream(self, req):
            raise GatewayTimeoutError("timeout")
            yield  # pragma: no cover

    monkeypatch.setattr(router, "_build_gateway_call", lambda **k: (_Boom(), _req()))
    monkeypatch.setattr(
        router, "_generate_gateway_fallback",
        lambda **k: {"text": "réponse de repli", "provider": "openai",
                     "model": "gpt-4o-mini", "usage": None, "tokens": 3},
    )

    events = list(router.generate_text_stream(prompt=CANARY, context=_ctx()))
    assert [e["type"] for e in events] == ["delta", "result"]
    assert events[0]["delta"] == "réponse de repli"
    assert events[1]["fallback_used"] is True
    assert events[1]["provider"] == "openai"


@pytest.mark.django_db
def test_aucun_repli_apres_le_premier_fragment(monkeypatch):
    """INTERDIT : recoller la fin d'un modèle B au début d'un modèle A."""
    monkeypatch.setattr(router, "_gateway_enabled", lambda: True)
    monkeypatch.setattr(router, "_gateway_streaming_enabled", lambda: True)

    class _HalfWay:
        def generate_stream(self, req):
            yield {"type": "delta", "delta": "début de "}
            raise GatewayStreamError("coupure")

    monkeypatch.setattr(router, "_build_gateway_call", lambda **k: (_HalfWay(), _req()))

    def _forbidden(**k):
        raise AssertionError("aucun repli ne doit avoir lieu après un fragment")

    monkeypatch.setattr(router, "_generate_gateway_fallback", _forbidden)

    gen = router.generate_text_stream(prompt=CANARY, context=_ctx())
    assert next(gen)["delta"] == "début de "
    with pytest.raises(GatewayStreamError):
        next(gen)


@pytest.mark.django_db
def test_le_journal_de_synthese_ne_porte_aucun_contenu(monkeypatch, caplog):
    import logging

    monkeypatch.setattr(router, "_gateway_enabled", lambda: True)
    monkeypatch.setattr(router, "_gateway_streaming_enabled", lambda: True)
    _install(monkeypatch, _FakeResponse(DELTAS_THEN_DONE))
    monkeypatch.setattr(router, "_build_gateway_call", lambda **k: (_client(), _req()))

    with caplog.at_level(logging.INFO, logger="apps.llm_router.router"):
        list(router.generate_text_stream(prompt=CANARY, context=_ctx()))

    lines = [r.getMessage() for r in caplog.records if "gateway_stream_end" in r.getMessage()]
    assert len(lines) == 1, "une seule ligne de synthèse par génération"
    line = lines[0]
    assert CANARY not in line
    assert "Bonjour le monde" not in line
    for champ in ("delta_count=3", "streamed_chars=16", "termination=done", "gateway_stream=true"):
        assert champ in line
    assert "first_delta_ms=" in line and "total_generation_ms=" in line
