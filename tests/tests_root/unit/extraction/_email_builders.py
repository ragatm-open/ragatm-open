"""Fabrique de courriels `.eml` AVEC pièces jointes (édition Open).

Tout le contenu est ÉCRIT PAR LE TEST, relisible en revue, sans aucune donnée
personnelle (domaines `example.invalid`). Les `.eml` sont écrits par la bibliothèque
standard `email`, donc conformes RFC 5322 / MIME. Les fabriques `.msg` (conteneur CFB
Outlook) ont été retirées avec le parseur tiers GPL : le format n'existe plus ici.
"""
from __future__ import annotations

import struct
from email.message import EmailMessage
from email.policy import SMTP


DEFAULT_BODY = (
    "Veuillez trouver ci-joint les documents de reference pour la reunion de validation.\n\n"
    "Merci de valider le budget avant vendredi."
)


# --- EML (bibliothèque standard) -----------------------------------------------------


def build_eml(*, subject="Transmission des previsions", plain=DEFAULT_BODY, html=None, attachments=(),
              inline_images=(), forwarded=None, sender="Unite Test <unite@example.invalid>",
              to="Destinataire <dest@example.invalid>", message_id="<eml-27a@example.invalid>") -> bytes:
    """`.eml` RFC 5322 conforme, écrit par `email.message.EmailMessage`.

    ``attachments`` : ``[(nom, octets, "type/sous-type")]`` ; ``inline_images`` :
    ``[(cid, octets, "image/png")]`` (multipart/related) ; ``forwarded`` : octets d'un
    autre .eml joint en `message/rfc822`.
    """
    from email import message_from_bytes
    from email.policy import default as default_policy

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg["Date"] = "Tue, 03 Nov 2026 09:30:00 +0100"
    msg["Message-ID"] = message_id
    if plain is not None:
        msg.set_content(plain)
    if html is not None:
        if plain is None:
            msg.set_content(html, subtype="html")
        else:
            msg.add_alternative(html, subtype="html")
        if inline_images:
            html_part = msg.get_body(("html",))
            for cid, data, ctype in inline_images:
                maintype, subtype = ctype.split("/", 1)
                html_part.add_related(data, maintype=maintype, subtype=subtype, cid=f"<{cid}>")
    for name, data, ctype in attachments:
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    if forwarded is not None:
        inner = message_from_bytes(forwarded, policy=default_policy)
        msg.add_attachment(inner, filename="forwarded.eml")
    return msg.as_bytes(policy=SMTP)
