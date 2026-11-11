# backend/apps/rag_core/services/ingest/docvision/__init__.py
"""
Rail hybride Document Vision — OPT-IN, éteint par défaut.

Principe : l'extraction native garde l'autorité. MinerU n'intervient que là où la
chaîne actuelle ne produit rien d'exploitable (page sans couche texte utilisable) ou
rien de structuré (tableaux, géométrie). Il ne remplace jamais un texte natif.
"""
from .config import hybrid_enabled  # noqa: F401
from .router import Route, router_image_autonome, router_page  # noqa: F401
