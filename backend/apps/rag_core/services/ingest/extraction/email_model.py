# backend/apps/rag_core/services/ingest/extraction/email_model.py
from __future__ import annotations

"""
Modèle interne des courriels (RAGatm Open) — SANS parseur Outlook.

Dérivé de la couche `msg_email` de l'édition privée (même auteur), AMPUTÉE de tout ce qui
touchait au parseur tiers `extract-msg` (GPL-3.0) : ne restent que les dataclasses
figées (`EmailAttachment`, `ExtractedEmail`, `AttachmentPayload`, `ParsedEmail`), les
codes d'avertissement stables et les normalisations pures (adresses, destinataires,
Message-ID, noms de pièces jointes, dates). Le rail EML (`eml_email`, bibliothèque
standard) produit ces mêmes objets.

Le format Outlook `.msg` n'est PAS pris en charge par RAGatm Open : `parser_available()`
rend toujours False, et la validation refuse le fichier avec un code stable
(`msg_unsupported_in_open_edition`). Rebrancher un parseur relève d'un module séparé,
distribué sous sa propre licence.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Tuple

# Codes de raison stables, exposés en métadonnées/logs. Toute nouvelle valeur doit
# rester un identifiant machine : jamais de phrase, jamais de contenu de message.
# (Préfixe historique `msg_` conservé : les codes sont partagés par le rail EML.)
WARN_BODY_FALLBACK_PLAIN = "msg_body_fallback_plain"
WARN_BODY_FALLBACK_RTF = "msg_body_fallback_rtf"
WARN_BODY_MISSING = "msg_body_missing"
WARN_BODY_SHORT = "msg_body_short"
WARN_RTF_UNDECODABLE = "msg_rtf_undecodable"
WARN_HTML_FROM_RTF = "msg_body_html_from_rtf"
WARN_DATE_MISSING = "msg_date_missing"
WARN_DATE_NAIVE = "msg_date_naive"
WARN_RECIPIENTS_TRUNCATED = "msg_recipients_truncated"
WARN_ATTACHMENTS_TRUNCATED = "msg_attachments_inventory_truncated"
WARN_ATTACHMENT_UNNAMED = "msg_attachment_unnamed"

#: Code de refus stable d'un fichier Outlook dans l'édition Open.
MSG_UNSUPPORTED_CODE = "msg_unsupported_in_open_edition"


class MsgParseError(Exception):
    """Un conteneur de message n'a pas pu être ouvert ou lu. Message volontairement neutre."""


def parser_available() -> bool:
    """Parseur Outlook (.msg) : JAMAIS disponible dans RAGatm Open (dépendance GPL retirée)."""
    return False


@dataclass(frozen=True)
class EmailAttachment:
    """Description d'une pièce jointe. Le CONTENU n'est jamais porté par cet objet."""

    filename: str
    extension: str
    mimetype: str
    size_bytes: int
    content_id: str
    is_inline: bool

    def as_dict(self) -> dict:
        return {
            "filename": self.filename,
            "extension": self.extension,
            "mimetype": self.mimetype,
            "size_bytes": self.size_bytes,
            "content_id": self.content_id,
            "is_inline": self.is_inline,
        }


@dataclass(frozen=True)
class ExtractedEmail:
    """Courriel normalisé, indépendant du parseur d'origine."""

    subject: Optional[str] = None
    sender_name: Optional[str] = None
    sender_address: Optional[str] = None
    to: Tuple[str, ...] = ()
    cc: Tuple[str, ...] = ()
    sent_at: Optional[datetime] = None
    internet_message_id: Optional[str] = None
    conversation_id: Optional[str] = None
    conversation_topic: Optional[str] = None
    body_html: Optional[str] = None
    body_plain: Optional[str] = None
    body_rtf_text: Optional[str] = None
    attachments: Tuple[EmailAttachment, ...] = ()
    attachment_count: int = 0
    warnings: Tuple[str, ...] = ()
    # Fil de discussion (EML : In-Reply-To / References) et format d'origine. Le Message-ID
    # n'est JAMAIS une preuve d'authenticité.
    in_reply_to: Optional[str] = None
    references: Tuple[str, ...] = ()
    source_format: str = "eml"


# Codes de raison des pièces jointes NON lisibles par le parseur.
ATT_BROKEN = "email_attachment_broken"
ATT_UNSUPPORTED_KIND = "email_attachment_unsupported_kind"
ATT_NESTING_LIMIT = "email_nesting_limit"


@dataclass
class AttachmentPayload:
    """Contenu TRANSITOIRE d'une pièce jointe.

    Distinct d'`EmailAttachment` — qui reste une description SANS contenu — pour que les
    octets ne puissent jamais fuiter dans des métadonnées persistées. Vit uniquement
    dans le processus d'extraction. Exactement une des trois formes : ``data`` (octets),
    ``embedded`` (message incorporé déjà analysé), ou ``error_code``.
    """

    position: int
    attachment: EmailAttachment
    data: Optional[bytes] = None
    embedded: Optional["ParsedEmail"] = None
    error_code: Optional[str] = None


@dataclass
class ParsedEmail:
    """Message analysé + contenus transitoires de ses pièces jointes."""

    email: ExtractedEmail
    payloads: List[AttachmentPayload] = field(default_factory=list)


# --- normalisations -----------------------------------------------------------

# `Nom affiché <adresse@domaine>` — tabulation comprise.
_ADDR_RE = re.compile(r"<([^<>]+@[^<>]+)>")
_WS_RE = re.compile(r"[\s ]+")


def _clean_scalar(value) -> Optional[str]:
    """Normalise une valeur d'en-tête scalaire en texte court et sûr."""
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    text = _WS_RE.sub(" ", text).strip()
    return text or None


def _clean_multiline(value) -> Optional[str]:
    """Normalise un corps texte : fins de ligne unifiées, espaces de fin retirés."""
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n")).strip()
    return text or None


def split_display_and_address(raw: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Sépare « Nom affiché <adresse> » en (nom, adresse). Ne devine jamais une adresse."""
    cleaned = _clean_scalar(raw)
    if not cleaned:
        return None, None
    match = _ADDR_RE.search(cleaned)
    if not match:
        return cleaned, None
    address = match.group(1).strip() or None
    display = _ADDR_RE.sub("", cleaned).strip().strip(",;") or None
    return display, address


def split_recipients(raw: Optional[str], *, limit: int) -> Tuple[Tuple[str, ...], bool]:
    """Découpe une liste de destinataires. Renvoie (liste, tronquée)."""
    cleaned = _clean_scalar(raw)
    if not cleaned:
        return (), False
    parts = [p.strip().strip(",;") for p in re.split(r"[;,](?![^<]*>)", cleaned)]
    entries = tuple(p for p in parts if p)
    if limit and len(entries) > limit:
        return entries[:limit], True
    return entries, False


def normalize_message_id(raw: Optional[str]) -> Optional[str]:
    """Nettoie un Internet Message-ID (chevrons, espaces)."""
    cleaned = _clean_scalar(raw)
    if not cleaned:
        return None
    return cleaned.strip("<> ").strip() or None


def normalize_attachment_name(raw: Optional[str]) -> Tuple[str, str]:
    """Réduit un nom de pièce jointe à un BASENAME sûr. Renvoie (nom, extension)."""
    text = _clean_scalar(raw) or ""
    text = text.replace("\x00", "")
    basename = re.split(r"[\\/]", text)[-1].strip()
    if basename in ("", ".", ".."):
        return "", ""
    basename = basename[:255]
    _, _, ext = basename.rpartition(".")
    extension = ext.lower() if ext and ext != basename else ""
    return basename, extension


def _coerce_datetime(value) -> Tuple[Optional[datetime], Optional[str]]:
    """Normalise la date d'envoi. Ne fabrique JAMAIS de fuseau."""
    if value is None:
        return None, WARN_DATE_MISSING
    if not isinstance(value, datetime):
        return None, WARN_DATE_MISSING
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value, WARN_DATE_NAIVE
    return value.astimezone(timezone.utc), None
