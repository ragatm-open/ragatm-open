"""DTOs du client AI Gateway (PR 5a).

Le résultat se mappe sur le contrat EXACT renvoyé par
``apps.llm_router.router.generate_text`` :
``{text, tokens, cost, provider, model, usage, raw}``
afin qu'un futur routage (PR 5c) soit un simple drop-in, sans changer les
consommateurs (chat, résumé, benchmark).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class GatewayGenerationRequest:
    """Demande de génération envoyée à la passerelle."""

    prompt: str
    provider: str | None = None
    model: str | None = None
    stream: bool = False
    options: dict[str, Any] = field(default_factory=dict)
    request_id: str | None = None
    # Champs additionnels du contrat Gateway v2.0.0 (le serveur IA) : ``system``
    # (prompt système) et ``think`` (raisonnement interne des modèles qwen3).
    # Optionnels et OMIS s'ils sont None => parité stricte avec le payload
    # historique (aucun impact sur les appels existants qui ne les fournissent pas).
    system: str | None = None
    think: bool | None = None

    def to_payload(self) -> dict[str, Any]:
        """Corps JSON CANONIQUE de ``/v1/generate`` et ``/v1/generate/stream``.

        FRONTIÈRE DE SÉRIALISATION. L'état interne de ce DTO reste riche —
        ``provider`` (résolution de routage), ``stream`` (état applicatif),
        ``request_id`` (corrélation locale) — mais **aucun de ces trois champs ne
        descend sur le fil** : ils n'ont jamais appartenu au contrat public
        ``GenerateRequest``. La passerelle actuelle les ignore en silence ; la
        candidate applique ``extra="forbid"`` et les refuse en 400
        ``extra_forbidden``, ce qui ferait échouer toute génération.

        Le mode FLUX est porté par la ROUTE (``/v1/generate/stream``), jamais par un
        champ de corps : les deux modes partagent ce sérialiseur à l'identique.

        Champs vides omis : parité stricte avec le comportement historique.
        """
        payload: dict[str, Any] = {"prompt": self.prompt}
        if self.model:
            payload["model"] = self.model
        if self.options:
            payload["options"] = self.options
        if self.system:
            payload["system"] = self.system
        if self.think is not None:
            payload["think"] = self.think
        return payload


def _extract_total_tokens(data: dict[str, Any], usage: dict[str, Any] | None) -> int:
    """Total de tokens, tolérant aux formes du contrat Gateway (PR 5b) :

    - ``tokens`` entier (forme simple) ;
    - ``tokens.total`` (forme imbriquée du contrat /v1/generate) ;
    - repli sur ``usage.total_tokens``.
    Sinon 0 (jamais d'exception).
    """
    tokens = data.get("tokens")
    if isinstance(tokens, int) and not isinstance(tokens, bool):
        return tokens
    if isinstance(tokens, dict) and isinstance(tokens.get("total"), int):
        return int(tokens["total"])
    if usage and isinstance(usage.get("total_tokens"), int):
        return int(usage["total_tokens"])
    return 0


@dataclass
class GatewayGenerationResult:
    """Résultat de génération renvoyé par la passerelle."""

    text: str
    provider: str
    model: str | None = None
    tokens: int = 0
    usage: dict[str, Any] | None = None
    raw: dict[str, Any] | None = None

    @classmethod
    def from_response(cls, data: dict[str, Any]) -> "GatewayGenerationResult":
        """Mappe la réponse JSON de la passerelle vers le DTO (tolérant)."""
        if not isinstance(data, dict):
            raise ValueError("gateway response is not a JSON object")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
        return cls(
            text=str(data.get("text") or ""),
            provider=str(data.get("provider") or ""),
            model=data.get("model"),
            tokens=_extract_total_tokens(data, usage),
            usage=usage,
            raw=data,
        )

    def to_generate_text_dict(self) -> dict[str, Any]:
        """Contrat identique à ``router.generate_text`` (drop-in pour PR 5c).

        ``cost`` reste None : le router est agnostique de la tarification ; le coût
        est estimé par les consommateurs via ``apps.rag_core.services.pricing``.
        """
        return {
            "text": self.text,
            "tokens": self.tokens,
            "cost": None,
            "provider": self.provider,
            "model": self.model,
            "usage": self.usage,
            "raw": self.raw,
        }
