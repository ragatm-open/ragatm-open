# backend/apps/rag_core/services/ingest/extraction/eml_email.py
from __future__ import annotations

"""
Adaptation d'un courriel MIME (.eml, RFC 5322 / MIME) vers le modèle interne RagATM.

Pendant de `msg_email` pour les courriels standards : produit le MÊME `ExtractedEmail`
et les MÊMES `AttachmentPayload` transitoires, de sorte que la sélection du corps, la
détection de signature, l'indexation et les citations soient COMMUNES aux deux formats.

- parseur : bibliothèque standard `email` (politique `default`), purement locale et
  passive — aucune ressource distante chargée, aucun lien suivi, aucun contenu actif ;
- en-têtes décodés (RFC 2047), noms de pièces RFC 2231, transferts base64 /
  quoted-printable, charsets MIME — un charset inconnu retombe sur un décodage tolérant
  SIGNALÉ (`eml_charset_fallback`), jamais sur une perte silencieuse ;
- `multipart/alternative` : les deux corps (texte et HTML) sont CANDIDATS, un seul est
  retenu par la sélection qualitative commune — jamais indexé deux fois ;
- `multipart/related` : les ressources liées au HTML (images `cid:`) sont des pièces
  `inline` ;
- `message/rfc822` : message joint analysé récursivement, profondeur bornée ;
- un message MIME DÉFECTUEUX reste exploitable pour ce qui a pu être lu
  (`eml_defects_detected`) ; seul un fichier sans aucun en-tête de courriel lève
  `EmlParseError`.
"""

from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from typing import List, Optional, Tuple

from .email_model import (
    ATT_NESTING_LIMIT,
    AttachmentPayload,
    EmailAttachment,
    ExtractedEmail,
    ParsedEmail,
    _clean_multiline,
    _clean_scalar,
    _coerce_datetime,
    normalize_attachment_name,
    normalize_message_id,
    split_display_and_address,
)

WARN_EML_DEFECTS = "eml_defects_detected"
WARN_EML_CHARSET_FALLBACK = "eml_charset_fallback"
WARN_EML_RECIPIENTS_TRUNCATED = "msg_recipients_truncated"  # même code que MSG : même sens
WARN_EML_ATTACHMENTS_TRUNCATED = "msg_attachments_inventory_truncated"
WARN_EML_ATTACHMENT_UNNAMED = "msg_attachment_unnamed"
ATT_EML_UNDECODABLE = "email_attachment_undecodable"

_KNOWN_HEADERS = ("from", "to", "date", "subject", "message-id", "received", "mime-version")


class EmlParseError(Exception):
    """Le fichier n'a pas pu être lu comme un courriel MIME. Message neutre, sans contenu."""


def _text_of(part) -> Tuple[Optional[str], bool]:
    """Texte décodé d'une partie texte. Renvoie (texte, repli_de_charset)."""
    try:
        content = part.get_content()
        if isinstance(content, str):
            return content, False
    except (LookupError, UnicodeError, AssertionError, ValueError):
        pass
    except Exception:
        pass
    try:
        raw = part.get_payload(decode=True) or b""
    except Exception:
        return None, True
    return raw.decode("utf-8", errors="replace"), True


def _addresses(header) -> List[str]:
    """« Nom <adresse> » par destinataire ; jamais deviné hors des champs d'adresse."""
    out: List[str] = []
    if header is None:
        return out
    try:
        for addr in getattr(header, "addresses", ()) or ():
            name = _clean_scalar(addr.display_name) or ""
            spec = _clean_scalar(addr.addr_spec) or ""
            if name and spec:
                out.append(f"{name} <{spec}>")
            elif spec or name:
                out.append(spec or name)
    except Exception:
        text = _clean_scalar(str(header))
        if text:
            out.append(text)
    return out


def _sender(header) -> Tuple[Optional[str], Optional[str]]:
    if header is None:
        return None, None
    try:
        addrs = getattr(header, "addresses", ()) or ()
        if addrs:
            return (_clean_scalar(addrs[0].display_name), _clean_scalar(addrs[0].addr_spec))
    except Exception:
        pass
    return split_display_and_address(str(header))


def _header(msg, name: str):
    try:
        return msg[name]
    except Exception:  # en-tête malformé : la politique `default` peut lever à l'accès
        return None


def _date(msg):
    header = _header(msg, "date")
    if header is None:
        return _coerce_datetime(None)
    try:
        return _coerce_datetime(getattr(header, "datetime", None))
    except Exception:
        return _coerce_datetime(None)


def _references(msg) -> Tuple[str, ...]:
    raw = _clean_scalar(str(_header(msg, "references") or "")) or ""
    out = []
    for token in raw.replace(",", " ").split():
        ref = normalize_message_id(token)
        if ref and ref not in out:
            out.append(ref)
    return tuple(out[:50])


def _body_parts(msg) -> Tuple[Optional[EmailMessage], Optional[EmailMessage]]:
    """Corps candidats (HTML, texte) selon l'algorithme MIME standard (`get_body`)."""
    html = plain = None
    try:
        html = msg.get_body(preferencelist=("html",))
    except Exception:
        html = None
    try:
        plain = msg.get_body(preferencelist=("plain",))
    except Exception:
        plain = None
    return html, plain


def _is_attachment_part(part, body_ids: set) -> bool:
    ctype = part.get_content_type()
    if ctype == "message/rfc822":
        # Testé AVANT `is_multipart()`, qui vaut vrai pour un message joint.
        return True
    if id(part) in body_ids or part.is_multipart():
        return False
    disposition = (part.get_content_disposition() or "").lower()
    if disposition == "attachment":
        return True
    if ctype.startswith("text/") and not part.get_filename():
        # Variante de corps non retenue (ex. 2e alternative) : ce n'est pas une pièce.
        return False
    return True  # images liées (related), applications, pièces « inline » nommées


def _walk_leaves(msg):
    """Parties feuilles, `message/rfc822` compris (non descendu : traité comme pièce)."""
    stack = [msg]
    while stack:
        part = stack.pop(0)
        if part.get_content_type() == "message/rfc822" and part is not msg:
            yield part
            continue
        if part.is_multipart():
            try:
                stack[0:0] = list(part.iter_parts())
            except Exception:
                continue
            continue
        if part is not msg:
            yield part


def _parse(msg, *, max_recipients: int, max_attachments: int, depth_left: int) -> ParsedEmail:
    warnings: List[str] = []
    defects = list(getattr(msg, "defects", []) or [])

    sender_name, sender_address = _sender(_header(msg, "from"))
    to = _addresses(_header(msg, "to"))
    cc = _addresses(_header(msg, "cc"))
    if max_recipients and (len(to) > max_recipients or len(cc) > max_recipients):
        warnings.append(WARN_EML_RECIPIENTS_TRUNCATED)
        to, cc = to[:max_recipients], cc[:max_recipients]
    sent_at, date_warning = _date(msg)
    if date_warning:
        warnings.append(date_warning)

    html_part, plain_part = _body_parts(msg)
    body_html = body_plain = None
    fallback = False
    if html_part is not None:
        body_html, fb = _text_of(html_part)
        fallback = fallback or fb
    if plain_part is not None:
        text, fb = _text_of(plain_part)
        fallback = fallback or fb
        body_plain = _clean_multiline(text)
    if fallback:
        warnings.append(WARN_EML_CHARSET_FALLBACK)
    body_ids = {id(p) for p in (html_part, plain_part) if p is not None}

    inventory: List[EmailAttachment] = []
    payloads: List[AttachmentPayload] = []
    total = 0
    for part in _walk_leaves(msg):
        defects.extend(getattr(part, "defects", []) or [])
        if not _is_attachment_part(part, body_ids):
            continue
        total += 1
        if max_attachments and len(inventory) >= max_attachments:
            continue
        ctype = part.get_content_type()
        raw_name = part.get_filename()
        if not raw_name and ctype == "message/rfc822":
            raw_name = "message.eml"
        name, extension = normalize_attachment_name(raw_name)
        if not name and WARN_EML_ATTACHMENT_UNNAMED not in warnings:
            warnings.append(WARN_EML_ATTACHMENT_UNNAMED)
        cid = _clean_scalar(str(part.get("content-id") or "")) or ""
        cid = cid.strip("<>")
        disposition = (part.get_content_disposition() or "").lower()
        pos = len(inventory)
        if ctype == "message/rfc822":
            desc = EmailAttachment(filename=name, extension=extension or "eml", mimetype=ctype,
                                   size_bytes=0, content_id=cid, is_inline=disposition == "inline")
            inventory.append(desc)
            if depth_left <= 0:
                payloads.append(AttachmentPayload(pos, desc, error_code=ATT_NESTING_LIMIT))
                continue
            try:
                inner_msg = part.get_content()
                if not isinstance(inner_msg, EmailMessage):
                    raise TypeError
                inner = _parse(inner_msg, max_recipients=max_recipients, max_attachments=max_attachments,
                               depth_left=depth_left - 1)
                payloads.append(AttachmentPayload(pos, desc, embedded=inner))
            except Exception:
                payloads.append(AttachmentPayload(pos, desc, error_code=ATT_EML_UNDECODABLE))
            continue
        try:
            data = part.get_payload(decode=True)
        except Exception:
            data = None
        desc = EmailAttachment(filename=name, extension=extension, mimetype=ctype,
                               size_bytes=len(data) if isinstance(data, (bytes, bytearray)) else 0,
                               content_id=cid, is_inline=bool(cid) or disposition == "inline")
        inventory.append(desc)
        if isinstance(data, (bytes, bytearray)):
            payloads.append(AttachmentPayload(pos, desc, data=bytes(data)))
        else:
            payloads.append(AttachmentPayload(pos, desc, error_code=ATT_EML_UNDECODABLE))
    if max_attachments and total > max_attachments:
        warnings.append(WARN_EML_ATTACHMENTS_TRUNCATED)
    if defects:
        warnings.append(WARN_EML_DEFECTS)

    email = ExtractedEmail(
        subject=_clean_scalar(str(_header(msg, "subject") or "")) or None,
        sender_name=sender_name,
        sender_address=sender_address,
        to=tuple(to),
        cc=tuple(cc),
        sent_at=sent_at,
        internet_message_id=normalize_message_id(str(_header(msg, "message-id") or "")),
        conversation_id=None,
        conversation_topic=_clean_scalar(str(_header(msg, "thread-topic") or "")) or None,
        body_html=body_html,
        body_plain=body_plain,
        body_rtf_text=None,
        attachments=tuple(inventory),
        attachment_count=total,
        warnings=tuple(warnings),
        in_reply_to=normalize_message_id(str(_header(msg, "in-reply-to") or "")),
        references=_references(msg),
        source_format="eml",
    )
    return ParsedEmail(email=email, payloads=payloads)


def parse_eml_bytes(data: bytes, *, max_recipients: int, max_attachments: int,
                    max_depth: int) -> ParsedEmail:
    """Octets d'un .eml -> message + contenus TRANSITOIRES de ses pièces.

    Lève `EmlParseError` si le fichier ne porte aucun en-tête de courriel : un message
    illisible et un message au corps vide sont deux situations distinctes.
    """
    try:
        msg = BytesParser(policy=policy.default).parsebytes(data or b"")
    except Exception as exc:  # pragma: no cover - le parseur standard tolère presque tout
        raise EmlParseError("Courriel illisible ou corrompu.") from exc
    try:
        present = [h for h in _KNOWN_HEADERS if msg.get(h) is not None]
    except Exception:
        present = []
    if len(present) < 2:
        raise EmlParseError("Courriel illisible ou corrompu.")
    return _parse(msg, max_recipients=max_recipients, max_attachments=max_attachments,
                  depth_left=max(0, int(max_depth)))
