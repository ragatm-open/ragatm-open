# backend/api/v1/documents/listing.py
from __future__ import annotations

"""Contrat de LISTE des documents : recherche, agrégats, tri, pagination.

Pourquoi un module dédié
------------------------
L'écran Documents présentait les 200 documents chargés comme s'ils étaient la
totalité du RagSpace : les onglets comptaient `items.length`, la recherche et les
filtres ne voyaient que la page chargée, et 68 documents (scénario de référence synthétique)
n'étaient atteignables par aucun chemin. Corriger cela déplace la recherche, les
compteurs et le tri **côté serveur** — donc dans un endroit unique, testable seul,
plutôt que dispersé dans la vue.

Ce module ne connaît ni HTTP ni DRF : il ne produit que des `Q`, des expressions
et des dictionnaires. La vue (`views_read.DocumentListView`) s'occupe des codes de
retour et des permissions.

Trois correspondances y vivent, et **une seule fois** :

1. `UI_STATUS_TO_DB` — catégories d'onglets ↔ statuts d'ingestion. Miroir exact de
   `toDocStatus()` côté frontend, **y compris son repli** : tout statut inattendu
   compte comme « en attente ». C'est ce qui garantit `somme(counts) == total`.
2. `STATUS_SORT_ORDER` — ordre **métier** du cycle d'ingestion, pas l'ordre
   alphabétique accidentel des valeurs stockées.
3. `FILE_TYPE_BY_MIME` — type court affiché ET trié, dérivé du `content_type`
   (détecté par CONTENU à l'ingestion) avec repli sur l'extension. La même table
   alimente le libellé Python et l'expression SQL de tri : ils ne peuvent pas
   diverger.
"""

import os

from django.db.models import Case, CharField, Count, F, Func, IntegerField, Q, Value, When
from django.db.models.functions import Lower, Upper

from apps.rag_core.models.document import DocumentStatus
from apps.rag_core.services.ingest.extraction import mime as M

# --- Pagination -------------------------------------------------------------
# Mêmes bornes que l'enveloppe de pagination déjà en service côté plateforme
# (api/v1/platform_admin/common.py) : une seule convention dans le dépôt.
# Elles ne sont pas importées de là : une liste utilisateur ne doit pas dépendre
# d'un module dont la garde est « superuser ».
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

#: Longueur maximale d'un terme de recherche (au-delà, tronqué sans erreur : une
#: URL trop longue est un accident de client, pas une faute de l'utilisateur).
MAX_SEARCH_LENGTH = 200


# --- Statuts ----------------------------------------------------------------

UI_ALL = "all"
UI_READY = "ready"
UI_ANALYZING = "analyzing"
UI_QUEUED = "queued"
UI_ERROR = "error"

#: Catégorie d'onglet -> statut d'ingestion stocké.
UI_STATUS_TO_DB = {
    UI_READY: DocumentStatus.READY,
    UI_ANALYZING: DocumentStatus.INGESTING,
    UI_QUEUED: DocumentStatus.PENDING,
    UI_ERROR: DocumentStatus.FAILED,
}

#: Ordre d'affichage des onglets (et des clés de `counts`).
UI_STATUS_KEYS = (UI_READY, UI_ANALYZING, UI_QUEUED, UI_ERROR)

#: Valeurs acceptées par `?status=`.
STATUS_CHOICES = (UI_ALL,) + UI_STATUS_KEYS

#: Statuts explicitement reconnus. Tout le reste retombe sur « en attente », comme
#: le fait `toDocStatus()` côté frontend (son `default:` renvoie "queued").
#: Sans ce repli, un statut inattendu disparaîtrait de tous les compteurs et la
#: somme des onglets ne vaudrait plus le total — exactement le défaut corrigé ici.
_EXPLICIT_DB_STATUSES = (
    DocumentStatus.READY,
    DocumentStatus.INGESTING,
    DocumentStatus.FAILED,
)

#: Ordre MÉTIER du cycle d'ingestion : ce qui attend, ce qui travaille, ce qui est
#: prêt, ce qui a échoué. `-status` inverse cet ordre — jamais l'ordre alphabétique
#: des valeurs stockées ("FAILED" < "INGESTING" < "PENDING" < "READY"), qui ne veut
#: rien dire pour un utilisateur.
STATUS_SORT_ORDER = (
    DocumentStatus.PENDING,
    DocumentStatus.INGESTING,
    DocumentStatus.READY,
    DocumentStatus.FAILED,
)


def status_filter(ui_status: str) -> Q | None:
    """`Q` correspondant à une catégorie d'onglet, ou `None` pour « tous »."""
    if not ui_status or ui_status == UI_ALL:
        return None
    if ui_status == UI_QUEUED:
        # Miroir du repli de `toDocStatus` : PENDING **et** tout statut inattendu.
        return ~Q(status__in=list(_EXPLICIT_DB_STATUSES))
    return Q(status=UI_STATUS_TO_DB[ui_status])


def status_counts(qs) -> dict[str, int]:
    """Compteurs par catégorie d'onglet, en **une seule** requête d'agrégation.

    À appliquer sur le queryset APRÈS recherche et AVANT le filtre de statut :
    sinon l'onglet actif mettrait tous les autres compteurs à zéro.
    """
    agg = qs.aggregate(
        all=Count("id"),
        ready=Count("id", filter=Q(status=DocumentStatus.READY)),
        analyzing=Count("id", filter=Q(status=DocumentStatus.INGESTING)),
        error=Count("id", filter=Q(status=DocumentStatus.FAILED)),
    )
    counts = {k: int(agg.get(k) or 0) for k in ("all", UI_READY, UI_ANALYZING, UI_ERROR)}
    # Déduit plutôt que compté : garantit `all == ready + analyzing + queued + error`
    # quelle que soit la valeur stockée.
    counts[UI_QUEUED] = counts["all"] - counts[UI_READY] - counts[UI_ANALYZING] - counts[UI_ERROR]
    return counts


# --- Type de fichier --------------------------------------------------------

#: `content_type` -> libellé court affiché ET trié. Le `content_type` est posé à
#: l'ingestion par détection de CONTENU (`extraction/mime.py`), jamais d'après
#: l'extension déclarée par le client : c'est la seule valeur digne de confiance.
FILE_TYPE_BY_MIME = {
    M.PDF: "PDF",
    M.DOCX: "DOCX",
    M.XLSX: "XLSX",
    M.PPTX: "PPTX",
    M.CSV: "CSV",
    M.HTML: "HTML",
    M.MARKDOWN: "MD",
    M.PLAIN: "TXT",
    M.PNG: "PNG",
    M.JPEG: "JPEG",
    M.GIF: "GIF",
    M.TIFF: "TIFF",
    M.BMP: "BMP",
    M.WEBP: "WEBP",
    M.OUTLOOK_MSG: "MSG",
}

#: Repli : extension du nom de fichier, en ASCII strict et bornée. Le motif est
#: répété tel quel côté SQL (`_EXTENSION_REGEX`) pour que les deux chemins donnent
#: exactement la même valeur.
_MAX_EXTENSION_LENGTH = 8
_ASCII_ALNUM = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
_EXTENSION_REGEX = r"\.[A-Za-z0-9]{1,%d}$" % _MAX_EXTENSION_LENGTH
_EXTENSION_CAPTURE = r"^.*\.([A-Za-z0-9]{1,%d})$" % _MAX_EXTENSION_LENGTH


def file_type_label(content_type: str | None, filename: str | None) -> str:
    """Type court d'un document (`PDF`, `DOCX`, `JPEG`…). Ne lève jamais.

    `content_type` prime ; l'extension n'est consultée que s'il est absent ou
    inconnu de la table (documents anciens, formats exotiques). Chaîne vide quand
    ni l'un ni l'autre ne renseigne : un type inventé serait pire qu'un tiret.
    """
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    label = FILE_TYPE_BY_MIME.get(ctype)
    if label:
        return label
    ext = os.path.splitext((filename or "").strip())[1].lstrip(".").upper()
    if 1 <= len(ext) <= _MAX_EXTENSION_LENGTH and all(c in _ASCII_ALNUM for c in ext):
        return ext
    return ""


def file_type_expression():
    """Expression SQL du type court, pour trier **en base**.

    Bâtie depuis `FILE_TYPE_BY_MIME` : impossible de faire diverger le libellé
    affiché (`file_type_label`) de la clé de tri.
    """
    whens = [
        When(content_type__iexact=mime, then=Value(label))
        for mime, label in FILE_TYPE_BY_MIME.items()
    ]
    whens.append(
        When(
            filename__regex=_EXTENSION_REGEX,
            then=Upper(
                Func(
                    F("filename"),
                    Value(_EXTENSION_CAPTURE),
                    Value(r"\1"),
                    function="REGEXP_REPLACE",
                    output_field=CharField(),
                )
            ),
        )
    )
    return Case(*whens, default=Value(""), output_field=CharField())


# --- Recherche --------------------------------------------------------------

def search_filter(term: str) -> Q | None:
    """Recherche sur le **nom de fichier**, insensible à la casse.

    Volontairement bornée à `filename` : c'est le nom visible dans la liste, il n'y
    a pas d'autre libellé distinct sur le modèle, et le contenu extrait relève du
    moteur de recherche documentaire (`GET /documents/search/`), pas de la liste.
    `icontains` suffit au volume gouverné par le quota (1 000 documents par
    workspace par défaut) ; l'indexer ne se justifiera qu'au-delà.
    """
    term = (term or "").strip()
    if not term:
        return None
    return Q(filename__icontains=term[:MAX_SEARCH_LENGTH])


# --- Tri --------------------------------------------------------------------

ORDER_FILENAME = "filename"
ORDER_FILE_TYPE = "file_type"
ORDER_STATUS = "status"
ORDER_CREATED_AT = "created_at"

#: Allowlist STRICTE. Aucune chaîne venue du client n'atteint `order_by()`.
ORDERING_FIELDS = (ORDER_FILENAME, ORDER_FILE_TYPE, ORDER_STATUS, ORDER_CREATED_AT)

#: Le plus récent d'abord — un import qui vient d'être lancé doit rester visible.
DEFAULT_ORDERING = "-" + ORDER_CREATED_AT

#: Noms des annotations posées pour le tri (jamais exposés dans la réponse).
_ANNOTATION_FILE_TYPE = "_sort_file_type"
_ANNOTATION_STATUS_RANK = "_sort_status_rank"


def parse_ordering(raw: str | None) -> tuple[str, bool] | None:
    """`"-status"` -> `("status", True)`. `None` si la valeur est refusée.

    Une valeur vide/absente renvoie le tri par défaut ; une valeur **inconnue**
    renvoie `None` pour que la vue réponde 400 plutôt que de trier au hasard.
    """
    raw = (raw or "").strip()
    if not raw:
        return (ORDER_CREATED_AT, True)
    descending = raw.startswith("-")
    field = raw[1:] if descending else raw
    if field not in ORDERING_FIELDS:
        return None
    return (field, descending)


def apply_ordering(qs, field: str, descending: bool):
    """Applique le tri demandé + un départage **stable et déterministe**.

    Sans critère secondaire, deux documents importés dans la même seconde peuvent
    changer de place d'une page à l'autre — et un même document apparaîtrait deux
    fois, ou pas du tout, en paginant.
    """
    if field == ORDER_FILENAME:
        # Insensible à la casse : « annexe.pdf » ne doit pas se retrouver après
        # « Zèbre.pdf » sous prétexte que les majuscules trient avant.
        primary = Lower(ORDER_FILENAME)
    elif field == ORDER_FILE_TYPE:
        qs = qs.annotate(**{_ANNOTATION_FILE_TYPE: file_type_expression()})
        primary = F(_ANNOTATION_FILE_TYPE)
    elif field == ORDER_STATUS:
        qs = qs.annotate(
            **{
                _ANNOTATION_STATUS_RANK: Case(
                    *[
                        When(status=value, then=Value(rank))
                        for rank, value in enumerate(STATUS_SORT_ORDER)
                    ],
                    default=Value(len(STATUS_SORT_ORDER)),
                    output_field=IntegerField(),
                )
            }
        )
        primary = F(_ANNOTATION_STATUS_RANK)
    else:
        primary = F(ORDER_CREATED_AT)

    primary = primary.desc() if descending else primary.asc()
    if field == ORDER_CREATED_AT:
        return qs.order_by(primary, "id")
    # Le plus récent d'abord à type/nom/statut égal, puis l'id pour figer l'ordre.
    return qs.order_by(primary, F(ORDER_CREATED_AT).desc(), "id")


# --- Pagination -------------------------------------------------------------

def parse_pagination(raw_limit, raw_offset) -> tuple[int, int]:
    """(limit, offset) bornés. Une valeur invalide retombe sur le défaut."""
    try:
        limit = int(raw_limit) if raw_limit not in (None, "") else DEFAULT_LIMIT
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    try:
        offset = int(raw_offset) if raw_offset not in (None, "") else 0
    except (TypeError, ValueError):
        offset = 0
    return max(1, min(limit, MAX_LIMIT)), max(0, offset)
