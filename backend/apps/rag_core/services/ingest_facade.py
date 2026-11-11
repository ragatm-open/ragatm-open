from __future__ import annotations

# Source unique : l'implémentation vit dans le package ingest (évite la duplication
# et l'import circulaire). On la ré-expose ici pour la rétro-compat.
from apps.rag_core.services.ingest import ingest_plain_text  # noqa: F401

__all__ = ["ingest_plain_text"]
