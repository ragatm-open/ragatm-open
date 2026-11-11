"""Contexte d'exécution non métier : révision de code déployée.

Un incident sans la révision qui l'a produit est difficilement exploitable :
« corrigé » n'a de sens que par rapport à un état du code.
"""
from __future__ import annotations

import subprocess

from django.conf import settings

#: Cache PROCESSUS. La valeur ne change pas pendant la vie d'un worker, et le
#: chemin d'écriture du feedback ne doit jamais payer un sous-processus.
_CACHED: str | None = None


def current_git_sha() -> str:
    """SHA de la révision déployée.

    Source de vérité : le réglage ``APP_GIT_SHA`` (posé par le déploiement).
    Repli ``git rev-parse`` UNIQUEMENT si le réglage est absent — utile en
    développement, jamais sur le chemin chaud puisque le résultat, succès comme
    échec, est mémorisé pour la durée du processus.
    """
    global _CACHED
    if _CACHED is not None:
        return _CACHED

    configured = str(getattr(settings, "APP_GIT_SHA", "") or "").strip()
    if configured:
        _CACHED = configured[:64]
        return _CACHED

    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=str(getattr(settings, "BASE_DIR", ".")),
        )
        _CACHED = (out.stdout.strip()[:64] if out.returncode == 0 else "") or ""
    except Exception:  # noqa: BLE001 - jamais bloquant, jamais bruyant
        _CACHED = ""
    return _CACHED
