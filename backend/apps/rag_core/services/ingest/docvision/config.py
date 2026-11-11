# backend/apps/rag_core/services/ingest/docvision/config.py
from __future__ import annotations

"""
Réglages du rail hybride Document Vision (MinerU).

Le rail est OPT-IN et **éteint par défaut**. Éteint, aucun code de ce paquet n'est
atteint : le chemin d'extraction reste celui de la ligne de base, à l'octet près.

Le rail est un ENRICHISSEMENT. Il ne remplace jamais le texte natif d'une page qui en
porte un exploitable : la mesure du banc a montré qu'un passage systématique divise par
trois la matière indexable sur les planches vectorielles.
"""

from django.conf import settings


def _bool(name: str, default: bool = False) -> bool:
    v = getattr(settings, name, default)
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(getattr(settings, name, default))
    except Exception:
        return default


def _str(name: str, default: str) -> str:
    v = getattr(settings, name, default)
    return str(v).strip() if v is not None else default


def hybrid_enabled() -> bool:
    """Rail hybride MinerU (OPT-IN, défaut False)."""
    return _bool("DOCUMENT_MINERU_HYBRID_ENABLED", False)


def sidecar_url() -> str:
    """Serveur de parsing MinerU LOCAL. Jamais mineru.net, jamais une API distante."""
    return _str("DOCUMENT_MINERU_SIDECAR_URL", "http://127.0.0.1:16580/v1")


def page_timeout_s() -> int:
    """Budget par page. 60 s vient de la mesure du banc : p50 = 5,5 s, max = 45,1 s."""
    return _int("DOCUMENT_MINERU_PAGE_TIMEOUT_S", 60)


def document_timeout_s() -> int:
    """Budget MinerU par document. N'entame aucun budget Celery existant."""
    return _int("DOCUMENT_MINERU_DOCUMENT_TIMEOUT_S", 300)


def tier() -> str:
    return _str("DOCUMENT_MINERU_TIER", "basic")


def output_format() -> str:
    """`middle_json` est le SEUL format qui porte la géométrie. La sortie markdown du
    CLI, elle, la jette — c'est ce qui avait fait conclure à tort que le moteur n'avait
    pas de bbox."""
    return _str("DOCUMENT_MINERU_OUTPUT_FORMAT", "middle_json")


def attribution_satisfied() -> bool:
    """Porte de service liée à la licence MinerU.

    La licence impose, pour un service en ligne fourni à des tiers, d'indiquer
    clairement l'usage de MinerU ; son non-respect résilie automatiquement la licence.
    Le prototype hors ligne n'est pas concerné ; toute activation hors prototype doit
    vérifier ce drapeau. Défaut **False** : on ne présume pas la conformité.
    """
    return _bool("MINERU_SERVICE_ATTRIBUTION_SATISFIED", False)


def use_scope() -> str:
    """Périmètre d'usage DÉCLARÉ pour ce déploiement.

    `personal_internal` | `third_party_service` | `unknown`.

    C'est une déclaration humaine, jamais une détection : le code ne cherche pas
    à établir qui est un tiers. Une valeur absente, vide ou non reconnue rend
    `unknown`, qui bloque — un booléen manquant ne doit jamais être lu comme un
    « non » implicite.
    """
    v = _str("MINERU_SERVICE_USE_SCOPE", "unknown").strip().lower()
    return v if v in ("personal_internal", "third_party_service") else "unknown"


def sidecar_hotes_autorises() -> frozenset:
    """Noms d'hôtes acceptés EN PLUS de la boucle locale.

    Le sidecar ne vit pas toujours sur `127.0.0.1` : sur un pont Docker `--internal`
    il est joignable par son nom de conteneur, et une allowlist purement loopback le
    refuserait. Déclarer un nom ici ne suffit pourtant pas à l'autoriser — le client
    exige en plus que toutes les adresses résolues soient privées, de sorte qu'un nom
    pointant vers l'extérieur reste refusé même déclaré par erreur.
    """
    brut = _str("DOCUMENT_MINERU_SIDECAR_ALLOWED_HOSTS", "")
    return frozenset(h.strip().lower() for h in brut.split(",") if h.strip())


def max_inline_bytes() -> int:
    """Plafond du document transmis en ligne au sidecar.

    32 Mio n'est pas un chiffre rond choisi par confort : sur les 3443 documents
    réellement stockés, le plus gros pèse 24,4 Mo et le p99 10,6 Mo. Aucun ne
    dépasse ce plafond, qui garde donc une marge sans ouvrir la porte à des
    charges utiles arbitraires.
    """
    return _int("DOCUMENT_MINERU_MAX_INLINE_BYTES", 33554432)
