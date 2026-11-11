"""Client HTTP interne vers l'AI Gateway (PR 5a — INERTE).

Existe mais n'est branché nulle part : ``generate_text`` n'est pas modifié.
Garanties :
  - AUCUNE requête réseau si ``AI_GATEWAY_ENABLED`` est False (garde-fou explicite) ;
  - timeout explicite sur chaque appel ;
  - erreurs TYPÉES (cf. ``exceptions``) ;
  - résultat mappé sur le contrat ``router.generate_text`` (drop-in PR 5c).
"""
from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

import requests
from django.conf import settings

from .dto import (
    GatewayGenerationRequest,
    GatewayGenerationResult,
    _extract_total_tokens,
)
from .exceptions import (
    GatewayConfigError,
    GatewayContractError,
    GatewayDisabledError,
    GatewayStreamError,
    GatewayTemporaryUnavailable,
    GatewayTimeoutError,
    classify_http_status,
)
from .pool_errors import pool_error_from_response
from .request_id import correlation_headers

# Chemin de l'endpoint de génération sur la passerelle (contrat PR 5b).
_GENERATE_PATH = "/v1/generate"
# Variante STREAMING (contrat Gateway 2.0.0) : NDJSON, une ligne JSON par événement.
# Le corps de requête est le MÊME schéma `GenerateRequest` que /v1/generate : la
# parité de payload est donc structurelle (même `to_payload()`), et c'est ce qui
# évite un rechargement de runner Ollama (un `num_ctx` divergent coûte plusieurs
# secondes). Voir le test de parité `test_gateway_stream_client`.
_GENERATE_STREAM_PATH = "/v1/generate/stream"

# Budget de connexion (secondes) : plafonné pour qu'un DNS/TCP bloqué ne consomme
# pas tout le budget total, qui doit rester disponible pour la génération.
_CONNECT_TIMEOUT_CAP_S = 10.0


def streaming_enabled() -> bool:
    """True si le streaming natif de la Gateway est activé (défaut False).

    N'a de sens que si ``AI_GATEWAY_ENABLED`` est vrai : à False, le chat garde le
    pseudo-stream historique. C'est le levier de repli d'exploitation du lot.
    """
    return bool(getattr(settings, "AI_GATEWAY_STREAMING_ENABLED", False))


def gateway_enabled() -> bool:
    """True si le client AI Gateway est activé (défaut False)."""
    return bool(getattr(settings, "AI_GATEWAY_ENABLED", False))


class AIGatewayClient:
    """Client minimal et sans état pour la passerelle IA."""

    def __init__(self, *, base_url: str, timeout_s: float, enabled: bool) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.timeout_s = float(timeout_s)
        self.enabled = bool(enabled)

    @classmethod
    def from_settings(cls) -> "AIGatewayClient":
        return cls(
            base_url=getattr(settings, "AI_GATEWAY_BASE_URL", "") or "",
            timeout_s=float(getattr(settings, "AI_GATEWAY_TIMEOUT_SECONDS", 30)),
            enabled=gateway_enabled(),
        )

    def generate(self, req: GatewayGenerationRequest) -> GatewayGenerationResult:
        """Appelle la passerelle. Lève une erreur typée ; aucun appel si désactivé."""
        self._preflight(req)

        url = f"{self.base_url}{_GENERATE_PATH}"
        try:
            resp = requests.post(
                url,
                headers={"Content-Type": "application/json", **correlation_headers(req.request_id)},
                json=req.to_payload(),
                timeout=self.timeout_s,
            )
        except requests.Timeout as e:
            raise GatewayTimeoutError(f"AI Gateway timeout after {self.timeout_s}s") from e
        except requests.ConnectionError as e:
            # Connexion refusée / DNS / reset : indisponibilité TRANSITOIRE.
            # Le message d'exception de `requests` ne contient pas le corps de la
            # requête, donc pas de prompt — seulement l'URL et la cause réseau.
            raise GatewayTemporaryUnavailable(f"AI Gateway unreachable: {e}") from e
        except requests.RequestException as e:
            # Toute autre anomalie de transport : traitée comme un défaut de
            # contrat/configuration (fail-closed, pas de repli automatique).
            raise GatewayContractError(f"AI Gateway request failed: {e}") from e

        if resp.status_code >= 400:
            # CORRECTIF P0-C : plus de repli indifférencié sur « tout >= 400 ».
            # Le code HTTP est classé (transitoire / auth / contrat) et seule la
            # première catégorie pourra motiver un repli externe, en aval.
            #
            # Le corps de la réponse n'est PLUS repris dans l'exception : il
            # pouvait ré-exposer le prompt par écho du tiers (constat P0-C #14).
            raise classify_http_status(resp.status_code, pool=pool_error_from_response(resp))

        try:
            data = resp.json()
        except Exception as e:
            raise GatewayContractError(
                f"AI Gateway returned invalid JSON ({type(e).__name__})"
            ) from e

        try:
            return GatewayGenerationResult.from_response(data)
        except ValueError as e:
            raise GatewayContractError(f"AI Gateway response does not match contract: {e}") from e

    # ------------------------------------------------------------------
    # Streaming natif (contrat Gateway 2.0.0)
    # ------------------------------------------------------------------
    def _preflight(self, req: GatewayGenerationRequest) -> None:
        """Gardes communes aux deux modes — AUCUN appel réseau si elles échouent.

        - passerelle désactivée : garde-fou, jamais de réseau ;
        - URL vide : erreur de configuration, pas une panne ;
        - prompt vide/blanc : rejet LOCAL (la Gateway le refuserait en 422). Erreur
          typée, sans jamais reprendre le prompt.
        """
        if not self.enabled:
            raise GatewayDisabledError("AI Gateway is disabled (AI_GATEWAY_ENABLED=False)")
        if not self.base_url:
            raise GatewayConfigError("AI_GATEWAY_BASE_URL is empty")
        if not (req.prompt or "").strip():
            raise GatewayConfigError("prompt is blank (rejected before network call)")

    def generate_stream(self, req: GatewayGenerationRequest) -> Iterator[dict[str, Any]]:
        """Génère en flux via ``/v1/generate/stream`` (NDJSON).

        Émet le contrat INTERNE de ``router.generate_text_stream`` :
          - ``{"type": "delta", "delta": "..."}`` — 0..N fois ;
          - ``{"type": "result", "provider", "model", "usage", "tokens", "raw"}`` — 1 fois.

        Garanties :
          - **parité de payload** : le corps est ``req.to_payload()``, octet pour octet
            celui de :meth:`generate` ; seul le chemin diffère ;
          - **budget TOTAL** : ``timeout_s`` borne la génération entière, il n'est pas
            renouvelé à chaque fragment (sinon un flux d'un token par seconde pourrait
            durer indéfiniment) ;
          - **annulation** : si le consommateur cesse d'itérer (déconnexion client), le
            ``finally`` ferme la socket ; la Gateway voit une rupture cliente et arrête
            la génération en amont ;
          - fin de flux sans ``done`` ou événement ``error`` => :class:`GatewayStreamError`.
        """
        self._preflight(req)

        url = f"{self.base_url}{_GENERATE_STREAM_PATH}"
        deadline = time.monotonic() + self.timeout_s
        connect_timeout = min(_CONNECT_TIMEOUT_CAP_S, self.timeout_s)

        try:
            resp = requests.post(
                url,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/x-ndjson",
                    **correlation_headers(req.request_id),
                },
                json=req.to_payload(),
                timeout=(connect_timeout, self.timeout_s),
                stream=True,
            )
        except requests.Timeout as e:
            raise GatewayTimeoutError(f"AI Gateway timeout after {self.timeout_s}s") from e
        except requests.ConnectionError as e:
            raise GatewayTemporaryUnavailable(f"AI Gateway unreachable: {e}") from e
        except requests.RequestException as e:
            raise GatewayContractError(f"AI Gateway request failed: {e}") from e

        try:
            if resp.status_code >= 400:
                raise classify_http_status(resp.status_code, pool=pool_error_from_response(resp))

            seen_done = False
            try:
                for raw_line in resp.iter_lines(decode_unicode=False):
                    if time.monotonic() > deadline:
                        raise GatewayTimeoutError(
                            f"AI Gateway stream exceeded total budget of {self.timeout_s}s"
                        )
                    if not raw_line:
                        continue  # keep-alive / ligne vide NDJSON
                    event = _parse_stream_line(raw_line)
                    etype = event.get("type")
                    if etype == "delta":
                        delta = event.get("delta")
                        if delta:
                            yield {"type": "delta", "delta": str(delta)}
                    elif etype == "done":
                        seen_done = True
                        yield _result_event(event, req)
                        break
                    elif etype == "error":
                        # Le message de la Gateway peut réécho l'entrée : on ne
                        # reprend QUE le code, jamais le corps.
                        code = str(event.get("code") or "unknown")
                        raise GatewayStreamError(
                            f"AI Gateway stream error (code={code})"
                        )
                    # type inconnu : ignoré (tolérance ascendante du contrat)
            except requests.Timeout as e:
                raise GatewayTimeoutError(
                    f"AI Gateway stream timeout after {self.timeout_s}s"
                ) from e
            except requests.ConnectionError as e:
                raise GatewayStreamError(f"AI Gateway stream interrupted: {e}") from e
            except requests.RequestException as e:
                raise GatewayStreamError(f"AI Gateway stream failed: {e}") from e

            if not seen_done:
                raise GatewayStreamError(
                    "AI Gateway stream ended without a terminal 'done' event"
                )
        finally:
            # Annulation cliente incluse (GeneratorExit) : on ferme la socket pour
            # que la Gateway constate la déconnexion et interrompe Ollama.
            _close_quietly(resp)

    def generate_dict(self, req: GatewayGenerationRequest) -> dict:
        """Comme ``generate`` mais renvoie directement le dict ``generate_text``."""
        return self.generate(req).to_generate_text_dict()


# ---------------------------------------------------------------------------
# Helpers NDJSON
# ---------------------------------------------------------------------------
def _parse_stream_line(raw_line: bytes) -> dict[str, Any]:
    """Décode une ligne NDJSON en objet JSON, ou lève une erreur de contrat.

    Le contenu de la ligne n'est JAMAIS repris dans le message : il porte le texte
    généré.
    """
    try:
        event = json.loads(raw_line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise GatewayContractError(
            f"AI Gateway stream returned a non-JSON line ({type(e).__name__})"
        ) from e
    if not isinstance(event, dict):
        raise GatewayContractError("AI Gateway stream line is not a JSON object")
    return event


def _result_event(done: dict[str, Any], req: GatewayGenerationRequest) -> dict[str, Any]:
    """Convertit l'événement ``done`` en événement ``result`` du contrat interne.

    Le ``done`` de la Gateway 2.0.0 porte ``model``, ``tokens`` et ``usage`` — mais
    NI ``provider`` NI le texte final : le texte accumulé côté client est donc la
    seule source de la réponse (constat consigné dans le rapport du lot).

    ``provider`` vaut ``ai_gateway``, valeur que renvoie ``/v1/generate`` dans sa
    réponse — et NON le provider amont demandé (``ollama``). Sans cela, le même
    échange serait journalisé sous deux fournisseurs différents selon le régime,
    et ``RagQueryLog.llm_provider`` deviendrait ininterprétable.
    """
    usage = done.get("usage") if isinstance(done.get("usage"), dict) else None
    return {
        "type": "result",
        "provider": str(done.get("provider") or "ai_gateway"),
        "model": done.get("model") or req.model,
        "usage": usage,
        "tokens": _extract_total_tokens(done, usage),
        "raw": done,
    }


def _close_quietly(resp: requests.Response) -> None:
    """Ferme la réponse en flux sans jamais masquer l'erreur en cours.

    ``resp.raw.close()`` puis ``resp.close()`` : la connexion partiellement
    consommée n'est pas remise au pool, la socket est réellement fermée — c'est ce
    qui matérialise l'annulation côté Gateway.
    """
    try:
        raw = getattr(resp, "raw", None)
        if raw is not None:
            raw.close()
    except Exception:  # noqa: BLE001 - fermeture best effort
        pass
    try:
        resp.close()
    except Exception:  # noqa: BLE001 - fermeture best effort
        pass
