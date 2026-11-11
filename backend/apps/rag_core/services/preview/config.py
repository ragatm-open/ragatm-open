# backend/apps/rag_core/services/preview/config.py
from __future__ import annotations

"""Bornes de l'aperçu tabulaire — **centralisées**, surchargeables par `.env`.

Pourquoi des bornes DURES et pas seulement des défauts
------------------------------------------------------
L'aperçu relit le fichier source à chaque appel. Le coût de lecture d'openpyxl en
`read_only` est **linéaire dans l'index de la ligne visée** : atteindre la ligne
19 000 impose de parser les 18 999 précédentes (mesuré : ~1,1 à 4,6 s selon le
classeur). Un paramètre de contexte non borné transformerait donc un clic en une
requête arbitrairement longue occupant un worker.

Les valeurs par défaut visent la VÉRIFICATION d'une citation — quelques lignes
autour de la plage — pas l'exploration du classeur. Au-delà, l'utilisateur
télécharge l'original.

Toutes ces limites produisent soit un refus typé (`context_limit_exceeded`), soit
une **troncature déclarée** dans la réponse. Jamais de troncature silencieuse : un
aperçu tronqué sans le dire ment sur le contexte.
"""

from django.conf import settings


def _int(name: str, default: int) -> int:
    try:
        return int(getattr(settings, name, default))
    except Exception:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(getattr(settings, name, default))
    except Exception:
        return default


# --- Contexte demandé par le client ---------------------------------------
def default_context_rows() -> int:
    return _int("PREVIEW_CONTEXT_ROWS_DEFAULT", 15)


def max_context_rows() -> int:
    return _int("PREVIEW_CONTEXT_ROWS_MAX", 50)


def default_context_columns() -> int:
    return _int("PREVIEW_CONTEXT_COLUMNS_DEFAULT", 3)


def max_context_columns() -> int:
    return _int("PREVIEW_CONTEXT_COLUMNS_MAX", 10)


# --- Bornes de la réponse --------------------------------------------------
def max_rows() -> int:
    """Lignes maximum renvoyées. Au-delà, la fenêtre est tronquée et DÉCLARÉE."""
    return _int("PREVIEW_MAX_ROWS", 300)


def max_columns() -> int:
    return _int("PREVIEW_MAX_COLUMNS", 60)


def max_cells() -> int:
    """Borne CARDINALE : elle prime sur les deux précédentes."""
    return _int("PREVIEW_MAX_CELLS", 10_000)


def max_cell_chars() -> int:
    """Caractères affichés par cellule.

    Distinct de la limite d'ingestion (`DOCUMENT_EXCEL_MAX_CELL_CHARS`, 2000) :
    celle-ci s'applique à la NORMALISATION (fidélité), celle-là à l'AFFICHAGE. La
    normalisation canonique est toujours appliquée avec la limite d'ingestion —
    sans quoi la chaîne différerait de celle indexée — puis l'affichage tronque,
    en le signalant par un drapeau sur la cellule.
    """
    return _int("PREVIEW_MAX_CELL_CHARS", 500)


# --- Budget de temps -------------------------------------------------------
def soft_timeout_s() -> float:
    """Budget visé. Dépassé, la lecture s'interrompt et rend `preview_timeout`.

    Vérifié PENDANT l'itération des lignes (et non après) : sans cela, une feuille
    très haute rendrait la main bien après le budget.
    """
    return _float("PREVIEW_TIMEOUT_SECONDS", 5.0)


def hard_timeout_s() -> float:
    """Plafond absolu, quelle que soit la surcharge de `PREVIEW_TIMEOUT_SECONDS`."""
    return _float("PREVIEW_HARD_TIMEOUT_SECONDS", 10.0)


def effective_timeout_s() -> float:
    return min(soft_timeout_s(), hard_timeout_s())


# --- Aperçu documentaire (DOCX) --------------------------------------------
#
# Le profil de coût est très différent du tabulaire. Une ré-extraction COMPLÈTE
# d'un DOCX est bon marché (mesuré : ~6 ms à 7 pages, ~550 ms à ~700 pages) et
# ne dépend pas de la position du passage cité — il n'y a pas d'équivalent du
# streaming linéaire d'openpyxl. La contrainte dominante n'est donc pas le temps
# mais la CHARGE UTILE : rendre tous les blocs d'un document de 700 pages
# produirait plus de 4 Mo de JSON. D'où une fenêtre bornée, comme pour le XLSX.


def doc_context_before() -> int:
    return _int("DOCX_PREVIEW_CONTEXT_BEFORE", 10)


def doc_max_context_before() -> int:
    return _int("DOCX_PREVIEW_CONTEXT_BEFORE_MAX", 50)


def doc_context_after() -> int:
    return _int("DOCX_PREVIEW_CONTEXT_AFTER", 20)


def doc_max_context_after() -> int:
    return _int("DOCX_PREVIEW_CONTEXT_AFTER_MAX", 100)


def doc_max_blocks_per_response() -> int:
    return _int("DOCX_PREVIEW_MAX_BLOCKS", 300)


def doc_max_block_chars() -> int:
    """Caractères AFFICHÉS par bloc. La valeur canonique n'est jamais modifiée."""
    return _int("DOCX_PREVIEW_MAX_BLOCK_CHARS", 2000)


def doc_max_total_chars() -> int:
    """Borne CARDINALE de la réponse : elle prime sur le nombre de blocs."""
    return _int("DOCX_PREVIEW_MAX_TOTAL_CHARS", 400_000)


def doc_max_headings() -> int:
    return _int("DOCX_PREVIEW_MAX_HEADINGS", 500)


def doc_max_document_blocks() -> int:
    """Au-delà, le document est refusé (`document_too_large`) plutôt que servi."""
    return _int("DOCX_PREVIEW_MAX_DOCUMENT_BLOCKS", 50_000)


# --- Aperçu de présentation (PPTX) -----------------------------------------
#
# La diapositive est l'unité de navigation : une fenêtre de quelques
# diapositives suffit à vérifier une citation. La ré-extraction est complète
# (mesuré : ~50 ms à 10 diapositives, ~1,3 s à 300) car les bases d'offset
# exigent les longueurs de TOUTES les unités précédentes — cibler une seule
# diapositive supposerait de persister ces longueurs, ce qui est le lot 2.


def pptx_context_before() -> int:
    return _int("PPTX_PREVIEW_CONTEXT_BEFORE", 1)


def pptx_max_context_before() -> int:
    return _int("PPTX_PREVIEW_CONTEXT_BEFORE_MAX", 5)


def pptx_context_after() -> int:
    return _int("PPTX_PREVIEW_CONTEXT_AFTER", 1)


def pptx_max_context_after() -> int:
    return _int("PPTX_PREVIEW_CONTEXT_AFTER_MAX", 5)


def pptx_max_slides_per_response() -> int:
    return _int("PPTX_PREVIEW_MAX_SLIDES", 10)


def pptx_max_blocks_per_slide() -> int:
    return _int("PPTX_PREVIEW_MAX_BLOCKS_PER_SLIDE", 200)


def pptx_max_block_chars() -> int:
    """Caractères AFFICHÉS par bloc. La valeur canonique n'est jamais modifiée."""
    return _int("PPTX_PREVIEW_MAX_BLOCK_CHARS", 2000)


def pptx_max_total_chars() -> int:
    """Borne CARDINALE de la réponse : elle prime sur le nombre de diapositives."""
    return _int("PPTX_PREVIEW_MAX_TOTAL_CHARS", 400_000)


def pptx_max_inventory() -> int:
    """Entrées de l'inventaire — aligné sur la limite d'ingestion (500 slides)."""
    return _int("PPTX_PREVIEW_MAX_INVENTORY", 500)


# --- Cache -----------------------------------------------------------------
def cache_enabled() -> bool:
    v = getattr(settings, "PREVIEW_CACHE_ENABLED", True)
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def cache_ttl_s() -> int:
    return _int("PREVIEW_CACHE_TTL_SECONDS", 300)
