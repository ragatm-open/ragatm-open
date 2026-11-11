"""Savoir si l'AI Gateway est déjà occupée — avant de l'occuper davantage.

## Pourquoi ce module existe

Les deux « instances » de Gateway (`:8100` production, `:8200` staging) tournent sur le
**même hôte** et se partagent le calcul. Mesuré le 22/08/2026 : quatre flux de banc en
staging multiplient par **2,5** la latence de reranking de la PRODUCTION et lui font
franchir sa porte de 250 ms. Un banc en staging est donc un incident de production.

L'isolation réelle n'est pas à notre portée : l'hôte de la Gateway n'est joignable ni en
SSH depuis les serveurs applicatifs, ni depuis un poste de développement. Elle relève
d'une action sur le serveur IA, hors de ce dépôt.

Ce module ne fait donc pas de l'isolation. Il fait de la **retenue** : il permet à toute
opération lourde de constater que la Gateway travaille déjà, et de renoncer plutôt que
d'aggraver. C'est moins bien qu'une isolation — et c'est ce qui est atteignable.

## Les deux signaux

**1. `/health/runtime` — l'occupation DÉCLARÉE.** L'audit du 23/08/2026 a établi que la
Gateway expose bien un compteur d'occupation, contrairement à ce que ce module supposait
au départ :

    {"inflight": {"total": 0, "by_model": {}},
     "queued":   {"total": 0, "by_model": {}},
     "compute":  {"isolation": "none", "gpu_count": 1,
                  "shared_resources": ["gpu", "ollama", "rerank_backend"],
                  "sibling_inflight": "not_observable_here"}}

C'est le signal AUTORITAIRE, et il ne coûte rien : il n'ajoute aucun travail à une
Gateway qu'on soupçonne déjà surchargée, là où la sonde de latence, elle, en ajoute.

**2. La latence d'un embedding court — l'occupation SUBIE.** Un texte court, répété
quelques fois. La Gateway **sérialise** : si elle traite autre chose, la sonde attend son
tour, et cela se voit immédiatement. Mesuré le 23/08/2026, sans aucun recouvrement entre
les deux populations :

    au repos       min  23,9   médiane  25,5   max  74,4 ms
    sous charge    min 248,6   médiane 249,4   max 277,9 ms

Un ordre de grandeur d'écart. Le seuil par défaut (120 ms) tombe dans le vide qui les
sépare — il n'y a aucune valeur observée entre 74 et 249.

Ce second signal n'est pas un doublon du premier, et c'est
`sibling_inflight: "not_observable_here"` qui le dit : `:8100` ne voit pas ce que fait
`:8200`. Or c'est exactement le scénario qui a motivé ce module — un banc en staging qui
dégrade la production. Le compteur de l'instance interrogée resterait à zéro pendant que
le GPU partagé, lui, est saturé. Seule la latence traverse cette frontière, parce que le
calcul est partagé : sonder une instance renseigne sur l'autre.

Les deux sont donc évalués, et l'un OU l'autre suffit à faire renoncer : le premier voit
sa propre instance avec certitude, le second voit la voisine par son ombre.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

from django.conf import settings

logger = logging.getLogger("rag.gateway_load")

#: Texte de sonde : court et neutre, pour mesurer l'ATTENTE et non le travail.
_SONDE = "sonde de disponibilite"

#: Modèle de sonde. Un modèle logique gouverné, autorisé partout, et le moins cher.
SONDE_MODEL = "legacy-768"


class GatewayBusy(RuntimeError):
    """La Gateway travaille déjà. Poursuivre dégraderait un service en cours."""

    def __init__(
        self,
        *,
        base_url: str,
        mesure_ms: float | None = None,
        seuil_ms: float | None = None,
        constat: str | None = None,
    ) -> None:
        if constat is None:
            constat = (
                f"sonde à {mesure_ms:.0f} ms (seuil {seuil_ms:.0f} ms)"
                if mesure_ms is not None and seuil_ms is not None
                else "occupation constatée"
            )
        super().__init__(
            f"la Gateway {base_url} est déjà occupée : {constat}. Le calcul est PARTAGÉ "
            "avec la production — lancer une opération lourde maintenant dégraderait un "
            "service en cours."
        )
        self.base_url = base_url
        self.mesure_ms = mesure_ms
        self.seuil_ms = seuil_ms
        self.constat = constat


def busy_threshold_ms() -> float:
    return float(getattr(settings, "AI_GATEWAY_BUSY_THRESHOLD_MS", 120.0))


def runtime_load(base_url: str, *, timeout_s: float = 5.0) -> dict | None:
    """Occupation DÉCLARÉE par la Gateway, ou ``None`` si elle ne la dit pas.

    Rend ``{"inflight": int, "queued": int, "isolation": str, "detail": {...}}``.

    ``None`` distingue « la Gateway ne publie pas cette information » de « elle publie
    zéro ». Les confondre ferait passer une Gateway muette pour une Gateway au repos —
    exactement l'erreur que la sonde de latence est là pour rattraper.
    """
    base = (base_url or "").rstrip("/")
    if not base:
        return None
    try:
        with urllib.request.urlopen(base + "/health/runtime", timeout=timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    inflight = (data.get("inflight") or {}).get("total")
    queued = (data.get("queued") or {}).get("total")
    if not isinstance(inflight, int):
        # Le champ manque ou n'est pas un entier : on ne devine pas. Une valeur
        # inventée ici deviendrait une autorisation de lancer.
        return None
    return {
        "inflight": inflight,
        "queued": queued if isinstance(queued, int) else 0,
        "isolation": str((data.get("compute") or {}).get("isolation") or "unknown"),
        "detail": data,
    }


def probe_latency_ms(base_url: str, *, samples: int = 5, timeout_s: float = 30.0) -> float | None:
    """Latence MÉDIANE d'un embedding court, ou ``None`` si la sonde échoue.

    ``None`` n'est pas « libre » : c'est « on ne sait pas ». L'appelant décide, et la
    politique par défaut de `ensure_quiet` est de laisser passer — une Gateway
    injoignable produira de toute façon une erreur franche à la première vraie requête,
    et bloquer sur une sonde ratée transformerait un incident réseau en panne d'outil.
    """
    base = (base_url or "").rstrip("/")
    if not base:
        return None
    corps = json.dumps({"model": SONDE_MODEL, "task": "query", "texts": [_SONDE]}).encode()
    mesures: list[float] = []
    for _ in range(max(1, samples)):
        req = urllib.request.Request(
            base + "/v1/embeddings", data=corps, headers={"Content-Type": "application/json"}
        )
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                resp.read()
        except (urllib.error.URLError, OSError, ValueError):
            return None
        mesures.append((time.perf_counter() - t0) * 1000.0)
    mesures.sort()
    return mesures[len(mesures) // 2]


def ensure_quiet(base_url: str, *, force: bool = False, samples: int = 5) -> float | None:
    """Lève `GatewayBusy` si la Gateway travaille déjà. Rend la mesure sinon.

    ``force`` journalise le passage en force au lieu de le taire : une dérogation qui ne
    laisse pas de trace est une dérogation qu'on ne saura pas avoir prise.
    """
    # Signal 1 — l'occupation déclarée. Interrogée EN PREMIER : si la Gateway dit
    # elle-même qu'elle travaille, il n'y a aucune raison de lui envoyer cinq embeddings
    # de plus pour le confirmer.
    charge = runtime_load(base_url)
    if charge is not None and (charge["inflight"] > 0 or charge["queued"] > 0):
        constat = (
            f"{charge['inflight']} requête(s) en cours et {charge['queued']} en attente "
            f"(/health/runtime, isolation={charge['isolation']})"
        )
        if force:
            logger.warning(
                "gateway_load.forced base_url=%s %s — opération lourde lancée sur une "
                "Gateway occupée", base_url, constat,
            )
        else:
            raise GatewayBusy(base_url=base_url, constat=constat)

    # Signal 2 — l'occupation subie. Indispensable même quand le compteur dit zéro :
    # l'instance voisine partage le GPU et n'apparaît dans aucun compteur d'ici
    # (`sibling_inflight: not_observable_here`).
    mesure = probe_latency_ms(base_url, samples=samples)
    seuil = busy_threshold_ms()
    if mesure is None:
        logger.warning("gateway_load.probe_failed base_url=%s — garde inopérante", base_url)
        return None
    if mesure <= seuil:
        return mesure
    if force:
        logger.warning(
            "gateway_load.forced base_url=%s mesure_ms=%.0f seuil_ms=%.0f — "
            "opération lourde lancée sur une Gateway occupée",
            base_url, mesure, seuil,
        )
        return mesure
    raise GatewayBusy(base_url=base_url, mesure_ms=mesure, seuil_ms=seuil)
