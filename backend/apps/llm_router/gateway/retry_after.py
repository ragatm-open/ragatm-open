"""Lecture du délai ``Retry-After`` demandé par l'AI Gateway — primitive PARTAGÉE.

Utilisée telle quelle par le client vision V1 (``vision``) et le client V2 (``vision_v2``) :
une seule définition, aucun client ne dépend des symboles privés de l'autre.

Source : en-tête HTTP ``Retry-After``, sinon ``detail.retry_after_seconds`` de
l'enveloppe d'erreur Gateway. Lecture défensive : seules des secondes ENTIÈRES dans
``[0, RETRY_AFTER_MAX_S]`` sont retenues ; absent, booléen, non entier (date HTTP
comprise), négatif ou trop grand → ``None`` (l'appelant retombe sur son backoff).
"""
from __future__ import annotations

from typing import Optional

#: Plus long délai accepté (secondes) ; au-delà la valeur est ignorée.
RETRY_AFTER_MAX_S = 300


def parse_retry_after(headers, detail) -> Optional[float]:
    raw = None
    if headers is not None:
        raw = headers.get("Retry-After")
    if raw is None and isinstance(detail, dict):
        raw = detail.get("retry_after_seconds")
    if isinstance(raw, bool):
        return None
    try:
        value = int(str(raw).strip()) if isinstance(raw, str) else int(raw) if isinstance(raw, int) else None
    except ValueError:
        return None
    if value is None or not 0 <= value <= RETRY_AFTER_MAX_S:
        return None
    return float(value)
