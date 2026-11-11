"""Pseudonymisation et empreintes — module PUR (aucun accès base).

Aucun identifiant nominatif (e-mail, nom d'utilisateur) n'entre dans
``AnswerFeedback``. Un incident doit pouvoir dire « trois utilisateurs
DIFFÉRENTS signalent le même défaut » sans jamais dire *lesquels* : c'est
exactement ce qu'un pseudonyme stable permet, et ce qu'un identifiant en clair
rendrait inutilement identifiant.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

from django.conf import settings


def _salt() -> str:
    """Sel de pseudonymisation.

    Repli sur ``SECRET_KEY`` plutôt que sur une constante : un sel vide rendrait
    les pseudonymes calculables par quiconque connaît un identifiant, ce qui
    annule la pseudonymisation. Le repli garantit qu'il n'existe aucun état
    « non salé », même si le réglage dédié n'est pas posé.
    """
    salt = getattr(settings, "FEEDBACK_PSEUDONYM_SALT", "") or ""
    if not salt:
        salt = str(getattr(settings, "SECRET_KEY", "") or "")
    return salt


def pseudonymize_user(user_id) -> str:
    """Pseudonyme STABLE d'un utilisateur : ``sha256(sel + id)``.

    Stable = deux feedbacks du même compte partagent le pseudonyme (donc le
    comptage multi-utilisateurs est correct) ; non réversible sans le sel.
    """
    if user_id is None or user_id == "":
        return ""
    return hashlib.sha256(f"{_salt()}:{user_id}".encode("utf-8")).hexdigest()


def sha256_text(value: str | None) -> str:
    """SHA-256 hexadécimal d'un texte. Chaîne vide -> chaîne vide (pas de hash du vide)."""
    if not value:
        return ""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]", flags=re.UNICODE)


def normalize_question(question: str | None) -> str:
    """Forme canonique d'une question, pour le regroupement de cas identiques.

    Casse, accents, ponctuation et espaces multiples ne distinguent pas deux
    signalements du même défaut : les neutraliser évite de compter trois fois
    « Qu'est-ce que Nordexia Conseil ? », « qu est ce que nordexia
    associates » et « Qu'est-ce que Nordexia Conseil ».
    """
    if not question:
        return ""
    text = unicodedata.normalize("NFKD", str(question))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = _PUNCT_RE.sub(" ", text.lower())
    return _WS_RE.sub(" ", text).strip()


def sources_fingerprint(sources) -> str:
    """Empreinte des chunks RETENUS : ``sha256`` des ``chunk_id`` TRIÉS.

    Le tri est essentiel : deux exécutions qui retiennent les mêmes chunks dans
    un ordre différent décrivent le même retrieval, et doivent se regrouper.
    """
    ids = sorted(
        str(s.get("chunk_id"))
        for s in (sources or [])
        if isinstance(s, dict) and s.get("chunk_id")
    )
    if not ids:
        return ""
    return hashlib.sha256("|".join(ids).encode("utf-8")).hexdigest()


def dedup_fingerprint(
    *,
    question: str | None,
    rag_space_id_hist,
    llm_model_hist: str | None,
    sources_fingerprint_value: str | None,
) -> str:
    """Empreinte de déduplication d'un incident.

    Regroupe « même question, même espace, même modèle, même retrieval ». La
    question normalisée SEULE regrouperait à tort des cas devenus différents
    (index réindexé entre-temps, modèle changé) : la correction attendue ne
    serait alors pas la même.
    """
    parts = [
        normalize_question(question),
        str(rag_space_id_hist or ""),
        str(llm_model_hist or ""),
        str(sources_fingerprint_value or ""),
    ]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
