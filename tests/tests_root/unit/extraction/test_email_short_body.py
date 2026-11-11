"""Corps de courriel COURT.

Le seuil de 40 caractères visibles départage les représentations d'un même message
(HTML, texte, RTF). Il faisait disparaître un message laconique ENTIER (« OK, validé
pour jeudi. ») : constaté sur une boîte synthétique, où un message transféré et une
réponse courte n'avaient plus que leur en-tête indexé. Un corps court mais lisible est
désormais retenu, avec l'avertissement `msg_body_short`.
"""
from email.message import EmailMessage
from email.policy import SMTP

from apps.rag_core.services.ingest.extraction.eml_email import parse_eml_bytes
from apps.rag_core.services.ingest.extraction.email_sections import select_body


def _eml(plain=None, html=None) -> bytes:
    msg = EmailMessage()
    msg["Subject"] = "Re: planning"
    msg["From"] = "a@example.invalid"
    msg["To"] = "b@example.invalid"
    msg["Date"] = "Tue, 03 Nov 2026 09:30:00 +0100"
    if plain is not None:
        msg.set_content(plain)
    if html is not None:
        if plain is None:
            msg.set_content(html, subtype="html")
        else:
            msg.add_alternative(html, subtype="html")
    return msg.as_bytes(policy=SMTP)


def _select(data: bytes):
    parsed = parse_eml_bytes(data, max_recipients=50, max_attachments=10, max_depth=1)
    return select_body(parsed.email)


def test_corps_court_retenu_et_signale():
    blocks, kind, warnings, _ = _select(_eml(plain="OK, validé pour jeudi."))
    assert kind == "plain" and "validé pour jeudi" in " ".join(b.text for b in blocks)
    assert "msg_body_short" in warnings


def test_corps_html_court_retenu():
    blocks, kind, warnings, _ = _select(_eml(html="<p>Bien reçu.</p>"))
    assert kind == "html" and "Bien reçu" in " ".join(b.text for b in blocks)


def test_corps_vide_reste_absent():
    _blocks, kind, warnings, _ = _select(_eml(plain="   \n  "))
    assert kind == "none" and "msg_body_missing" in warnings


def test_corps_long_inchange():
    long = "Le comité valide le planning du lot 3 et la livraison en semaine 14."
    _blocks, kind, warnings, _ = _select(_eml(plain=long))
    assert kind == "plain" and "msg_body_short" not in warnings

