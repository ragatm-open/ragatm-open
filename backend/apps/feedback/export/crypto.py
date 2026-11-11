"""Chiffrement et intégrité des paquets d'incident.

AUCUNE CRYPTOGRAPHIE MAISON
---------------------------
Fernet (``cryptography``) : AES-128-CBC + HMAC-SHA256, clé unique, format
versionné, primitives auditées. Un paquet qui sort de production porte du
contenu métier — ce n'est pas le lieu d'improviser un XOR.

TROIS GARANTIES DISTINCTES
--------------------------
- **Intégrité** : ``content_sha256``, empreinte du contenu logique, calculée
  HORS manifeste (sinon elle se référencerait elle-même).
- **Authenticité** : HMAC-SHA256 du ``content_sha256`` avec la clé d'export.
  Elle répond à « ce paquet vient-il bien de notre chaîne d'export ? ».
- **Confidentialité** : chiffrement de l'archive, OBLIGATOIRE dès que le paquet
  contient du contenu de production.

La clé vit dans la configuration, jamais dans le paquet, jamais dans les logs.
Sans clé, un export contenant du contenu de production est REFUSÉ — pas produit
en clair.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)


class EncryptionUnavailable(Exception):
    """Clé d'export absente ou invalide alors que le chiffrement est requis."""


def _key() -> str:
    return str(getattr(settings, "FEEDBACK_INCIDENT_EXPORT_KEY", "") or "").strip()


def is_configured() -> bool:
    return bool(_key())


def generate_key() -> str:
    """Clé Fernet neuve (utilitaire d'exploitation — rotation)."""
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode("ascii")


def canonical_content_sha256(files: dict[str, bytes]) -> str:
    """Empreinte du contenu logique du paquet.

    Procédure CANONIQUE, documentée pour être reproductible ailleurs :

    1. on écarte ``manifest.json`` (il contiendra le résultat) ;
    2. on trie les chemins par ordre lexicographique d'octets ;
    3. pour chaque entrée on absorbe ``chemin`` + ``\\x00`` + ``sha256(contenu)``
       + ``\\n``.

    Le tri rend l'empreinte indépendante de l'ordre d'écriture ; l'usage du
    sha256 par fichier plutôt que des octets bruts la rend calculable en flux
    sur de gros fichiers source.
    """
    h = hashlib.sha256()
    for path in sorted(p for p in files if p != "manifest.json"):
        h.update(path.encode("utf-8"))
        h.update(b"\x00")
        h.update(hashlib.sha256(files[path]).hexdigest().encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def file_digests(files: dict[str, bytes]) -> dict[str, str]:
    """SHA-256 de CHAQUE fichier — permet de localiser une altération."""
    return {
        path: hashlib.sha256(blob).hexdigest()
        for path, blob in sorted(files.items())
        if path != "manifest.json"
    }


def sign(content_sha256: str) -> str:
    """HMAC-SHA256 du condensat. Chaîne vide si aucune clé n'est configurée."""
    key = _key()
    if not key:
        return ""
    return hmac.new(key.encode("utf-8"), content_sha256.encode("ascii"), hashlib.sha256).hexdigest()


def verify_signature(content_sha256: str, signature: str) -> bool:
    """Vérifie la signature en temps CONSTANT (``compare_digest``)."""
    expected = sign(content_sha256)
    if not expected or not signature:
        return False
    return hmac.compare_digest(expected, str(signature))


def encrypt(payload: bytes) -> bytes:
    """Chiffre. Lève :class:`EncryptionUnavailable` si la clé manque.

    Lever plutôt que se replier sur du clair est délibéré : le repli silencieux
    est exactement la façon dont un contenu sensible finit par sortir en clair.
    """
    key = _key()
    if not key:
        raise EncryptionUnavailable(
            "FEEDBACK_INCIDENT_EXPORT_KEY absente : un paquet contenant du contenu "
            "de production ne peut pas être produit en clair."
        )
    try:
        from cryptography.fernet import Fernet

        return Fernet(key.encode("ascii")).encrypt(payload)
    except EncryptionUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        # Jamais la clé ni son fragment dans le message.
        raise EncryptionUnavailable(
            f"Clé d'export inutilisable (error_code={type(exc).__name__})."
        ) from None


def decrypt(payload: bytes) -> bytes:
    key = _key()
    if not key:
        raise EncryptionUnavailable("FEEDBACK_INCIDENT_EXPORT_KEY absente : déchiffrement impossible.")
    try:
        from cryptography.fernet import Fernet

        return Fernet(key.encode("ascii")).decrypt(payload)
    except Exception as exc:  # noqa: BLE001
        raise EncryptionUnavailable(
            f"Déchiffrement impossible (error_code={type(exc).__name__})."
        ) from None


def dumps(payload: Any) -> bytes:
    """Sérialisation JSON STABLE (clés triées) — l'empreinte doit être reproductible."""
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str).encode(
        "utf-8"
    )
