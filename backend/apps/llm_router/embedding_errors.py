"""Erreurs d'embeddings — module SANS dépendance, pour casser un cycle d'import.

``EmbeddingsError`` vivait dans ``embeddings.py``. Le client Gateway (§7 du lot 3)
doit lever la MÊME famille d'erreur, mais ``embeddings.py`` l'importe : les deux
modules ne peuvent pas se référencer mutuellement.

L'erreur est donc définie ici, et ``embeddings.py`` la ré-exporte — le chemin
d'import public ``from apps.llm_router.embeddings import EmbeddingsError`` reste
valide, y compris pour les tests existants.
"""
from __future__ import annotations


class EmbeddingsError(RuntimeError):
    """Échec de production d'embeddings. Toujours une erreur, jamais un repli."""


class GatewayEmbeddingsError(EmbeddingsError):
    """Échec spécifique au chemin GOUVERNÉ (AI Gateway ``/v1/embeddings``).

    Sous-classe d'``EmbeddingsError`` : tout appelant qui gérait déjà l'ancienne
    erreur continue de fonctionner. Le type distinct permet, lui, de dire dans une
    trace *quel* chemin a échoué sans analyser un message.
    """

    def __init__(self, message: str, *, code: str = "gateway_error", pool_error=None) -> None:
        self.code = code
        #: Refus PROPRE à AI Gateway Pool (``PoolErrorInfo``) ou ``None`` ; ``code`` inchangé.
        self.pool_error = pool_error
        super().__init__(message)
