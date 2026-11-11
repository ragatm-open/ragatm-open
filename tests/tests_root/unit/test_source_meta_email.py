"""Citations de courriels : date d'envoi et NOM de l'expéditeur
projetés, ADRESSE jamais (règle de citation des courriels)."""
from apps.rag_core.services.query.source_meta import project_source_meta


def test_sender_name_and_date_are_projected():
    out = project_source_meta({
        "email_part": "body", "email_subject": "Validation du planning",
        "email_sent_at": "2026-05-12T08:14:03+00:00", "email_sender": "Fournisseur SA",
    })
    assert out["email_sender"] == "Fournisseur SA"
    assert out["email_sent_at"] == "2026-05-12T08:14:03+00:00"
    assert out["email_subject"] == "Validation du planning"


def test_sender_address_is_never_projected():
    out = project_source_meta({"email_part": "body", "email_sender": "fournisseur@example.com"})
    assert "email_sender" not in out


def test_recipients_are_never_projected():
    out = project_source_meta({"email_part": "body", "to": ["a@b.c"], "cc": ["d@e.f"],
                               "email_recipients": ["a@b.c"]})
    assert out == {"email_part": "body"}
