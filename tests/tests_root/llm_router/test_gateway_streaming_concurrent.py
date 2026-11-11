"""Flux Gateway simultanés : isolation d'état et fermeture individuelle.

Ces tests ne remplacent pas les mesures staging (elles seules exercent Nginx,
Next.js, Gunicorn et le GPU) : ils verrouillent ce qu'une mesure ne peut pas
garantir dans la durée — qu'aucun état ne soit partagé entre deux générations
concurrentes, et que fermer l'une n'affecte pas l'autre.

Le risque visé est concret : un compteur, un tampon ou une socket hissés au
niveau du module rendraient les flux dépendants les uns des autres, ce qui ne se
verrait qu'en production, sous forme de réponses mélangées.
"""
from __future__ import annotations

import json
import threading

import pytest

from apps.llm_router.gateway import AIGatewayClient, GatewayGenerationRequest
from apps.llm_router.gateway import client as client_mod


class _Raw:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Reponse:
    """Réponse en flux dont chaque ligne est libérée à la demande du test."""

    def __init__(self, lignes: list[bytes]) -> None:
        self._lignes = lignes
        self.status_code = 200
        self.raw = _Raw()
        self.closed = False
        self.porte = threading.Event()
        self.lignes_lues = 0

    def iter_lines(self, decode_unicode: bool = False):
        for i, ligne in enumerate(self._lignes):
            if i == 1:
                self.porte.wait(timeout=5)  # synchronise les flux entre eux
            self.lignes_lues += 1
            yield ligne

    def close(self) -> None:
        self.closed = True


def _flux(marqueur: str, n: int = 4) -> list[bytes]:
    evts = [{"type": "delta", "delta": f"{marqueur}{i}", "done": False} for i in range(n)]
    evts.append({
        "type": "done", "delta": "", "done": True, "model": f"modele-{marqueur}",
        "tokens": {"total": n}, "usage": {"total_tokens": n},
    })
    return [json.dumps(e).encode("utf-8") for e in evts]


def _client() -> AIGatewayClient:
    return AIGatewayClient(base_url="http://gateway.invalid:8100", timeout_s=30.0, enabled=True)


def _req(marqueur: str) -> GatewayGenerationRequest:
    return GatewayGenerationRequest(prompt=f"question {marqueur}", provider="ollama",
                                    model=f"modele-{marqueur}", options={"num_ctx": 8192})


def _installer(monkeypatch, reponses: dict[str, _Reponse]) -> None:
    """Chaque requête reçoit LA réponse correspondant à son propre marqueur."""

    def faux_post(url, **kwargs):
        marqueur = kwargs["json"]["prompt"].split()[-1]
        return reponses[marqueur]

    monkeypatch.setattr(client_mod.requests, "post", faux_post)


def test_trois_flux_simultanes_ne_se_melangent_pas(monkeypatch):
    """Chaque générateur ne voit QUE ses propres fragments."""
    marqueurs = ["A", "B", "C"]
    reponses = {m: _Reponse(_flux(m)) for m in marqueurs}
    _installer(monkeypatch, reponses)

    recu: dict[str, list[str]] = {m: [] for m in marqueurs}
    resultats: dict[str, dict] = {}
    erreurs: list[Exception] = []

    def consommer(m: str) -> None:
        try:
            for evt in _client().generate_stream(_req(m)):
                if evt["type"] == "delta":
                    recu[m].append(evt["delta"])
                else:
                    resultats[m] = evt
        except Exception as e:  # noqa: BLE001
            erreurs.append(e)

    fils = [threading.Thread(target=consommer, args=(m,)) for m in marqueurs]
    for f in fils:
        f.start()
    for r in reponses.values():   # libère les trois flux en même temps
        r.porte.set()
    for f in fils:
        f.join(timeout=10)

    assert not erreurs
    for m in marqueurs:
        assert recu[m] == [f"{m}{i}" for i in range(4)]
        autres = [x for k in marqueurs if k != m for x in recu[k]]
        assert not any(frag in autres for frag in recu[m])
        assert resultats[m]["model"] == f"modele-{m}"


def test_fermer_un_flux_n_affecte_pas_les_autres(monkeypatch):
    """B est abandonné en cours de route ; A et C vont au bout."""
    marqueurs = ["A", "B", "C"]
    reponses = {m: _Reponse(_flux(m, n=6)) for m in marqueurs}
    _installer(monkeypatch, reponses)
    for r in reponses.values():
        r.porte.set()

    gens = {m: _client().generate_stream(_req(m)) for m in marqueurs}
    for m in marqueurs:                      # un fragment chacun
        assert next(gens[m])["delta"] == f"{m}0"

    gens["B"].close()                        # déconnexion cliente de B
    assert reponses["B"].raw.closed is True
    assert reponses["A"].raw.closed is False
    assert reponses["C"].raw.closed is False

    for m in ("A", "C"):
        evts = list(gens[m])
        assert [e["delta"] for e in evts if e["type"] == "delta"] == [f"{m}{i}" for i in range(1, 6)]
        assert evts[-1]["type"] == "result"
        assert reponses[m].raw.closed is True


@pytest.mark.django_db
def test_aucun_etat_partage_entre_deux_appels_successifs(monkeypatch):
    """Deux générations d'affilée : compteurs et journaux repartent de zéro."""
    from apps.llm_router import router
    from apps.llm_router.egress import LlmExecutionContext, policy_from_legacy_flags

    monkeypatch.setattr(router, "_gateway_enabled", lambda: True)
    monkeypatch.setattr(router, "_gateway_streaming_enabled", lambda: True)
    reponses = {"A": _Reponse(_flux("A", n=3)), "B": _Reponse(_flux("B", n=7))}
    for r in reponses.values():
        r.porte.set()
    _installer(monkeypatch, reponses)
    monkeypatch.setattr(router, "_build_gateway_call",
                        lambda **k: (_client(), _req(k["prompt"].split()[-1])))

    ctx = LlmExecutionContext(
        operation="test.concurrent",
        policy=policy_from_legacy_flags(space_provider="ollama", allow_external_fallback=False),
    )
    for marqueur, attendu in (("A", 3), ("B", 7)):
        evts = list(router.generate_text_stream(prompt=f"question {marqueur}", context=ctx))
        assert len([e for e in evts if e["type"] == "delta"]) == attendu
        assert evts[-1]["model"] == f"modele-{marqueur}"
