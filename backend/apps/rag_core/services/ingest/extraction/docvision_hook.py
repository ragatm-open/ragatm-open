# backend/apps/rag_core/services/ingest/extraction/docvision_hook.py
from __future__ import annotations

"""
Point de rattachement du rail hybride Document Vision.

Ce module existe pour une raison précise : garder le chemin ÉTEINT strictement inerte.
Le drapeau est lu avant tout import du paquet `docvision` ; éteint, rien de ce paquet
n'est chargé, et l'objet reçu est renvoyé tel quel — le même objet, pas une copie.

Il vit dans `extraction` et non dans `docvision` pour éviter un import circulaire :
`docvision` importe déjà le contrat d'extraction.
"""

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover
    from .base import ExtractionResult

#: Types MIME traités comme image autonome par le rail.
_IMAGE_MIMES = ("image/",)


def appliquer_si_actif(
    result: "ExtractionResult",
    *,
    data: bytes,
    filename: str = "",
    content_type: str = "",
    budget_extraction_s: Optional[int] = None,
    ecoule_extraction_s: float = 0.0,
) -> "ExtractionResult":
    """Applique le rail si le drapeau l'autorise, sinon rend `result` inchangé.

    Toute défaillance du rail — sidecar absent, télémétrie douteuse, budget insuffisant,
    erreur inattendue — laisse le résultat historique intact. Le rail est un supplément :
    il ne peut pas faire échouer une extraction qui a réussi.
    """
    try:
        from ..docvision import config as dv_config
    except Exception:  # pragma: no cover - paquet absent
        return result
    if not dv_config.hybrid_enabled():
        return result

    # Porte licence. Elle est DISTINCTE du drapeau hybride et distincte de la
    # readiness du sidecar : celui-ci peut être parfaitement apte pendant que la
    # porte bloque. Un périmètre d'usage non déclaré bloque, par échec fermé.
    try:
        from ..docvision.license_gate import mineru_license_gate

        porte = mineru_license_gate()
    except Exception as e:  # noqa: BLE001 - une porte illisible bloque
        meta = dict(result.metadata or {})
        meta["docvision"] = {"skipped_reason": f"porte licence illisible ({type(e).__name__})"}
        result.metadata = meta
        return result
    if not porte.eligible:
        meta = dict(result.metadata or {})
        meta["docvision"] = {"skipped_reason": f"LICENSE_GATE:{porte.status}",
                             **porte.as_dict()}
        result.metadata = meta
        return result

    try:
        from ..docvision.rail import appliquer_rail

        enrichi, rapport = appliquer_rail(
            result,
            data=data,
            filename=filename,
            est_image=str(content_type or "").startswith(_IMAGE_MIMES),
            budget_extraction_s=budget_extraction_s,
            ecoule_extraction_s=ecoule_extraction_s,
        )
        meta = dict(enrichi.metadata or {})
        meta["docvision"] = rapport.as_dict()
        enrichi.metadata = meta
        return enrichi
    except Exception as e:  # noqa: BLE001 - le rail ne doit jamais casser l'extraction
        meta = dict(result.metadata or {})
        meta["docvision"] = {"skipped_reason": f"erreur inattendue ({type(e).__name__})"}
        result.metadata = meta
        return result
