"""Rédaction et détection de secrets avant export.

DEUX MÉCANISMES DISTINCTS, QU'IL NE FAUT PAS CONFONDRE
------------------------------------------------------
- **Rédaction** — on retire ce qui n'a aucune raison de sortir : adresses
  e-mail, identifiants de compte, chemins de stockage, et le **commentaire
  libre de l'utilisateur**, exclu par défaut parce qu'il peut contenir n'importe
  quoi, y compris des données personnelles.
- **Détection de secrets** — on cherche ce qui ne devrait *jamais* se trouver là
  (clé d'API, jeton, clé privée). Une détection positive **bloque** l'export ;
  elle ne le nettoie pas. Nettoyer un secret automatiquement donnerait
  l'illusion qu'il n'y en avait pas.

Le statut résultant est explicite : ``passed``, ``blocked``, ou
``manually_approved`` — cette dernière valeur n'étant posée que par une décision
humaine tracée.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

STATUS_PENDING = "pending"
STATUS_PASSED = "passed"
STATUS_BLOCKED = "blocked"
STATUS_MANUALLY_APPROVED = "manually_approved"

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
#: Chemins absolus POSIX et Windows. Un chemin de stockage révèle la topologie
#: interne et n'aide en rien à reproduire un défaut.
_PATH_RE = re.compile(r"(?:/(?:home|media|var|opt|srv|mnt|data)/[\w./-]+|[A-Za-z]:\\[\w\\.-]+)")

#: Motifs de SECRETS. Volontairement larges : un faux positif coûte une
#: vérification humaine, un faux négatif coûte une fuite.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("bearer_token", re.compile(r"\b[Bb]earer\s+[A-Za-z0-9._-]{20,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}")),
    # `['\"\\]{0,3}` et non `['\"]?` : la détection opère sur une sérialisation
    # JSON, où les guillemets sont ÉCHAPPÉS (`\"`). Sans tolérer l'antislash, le
    # motif ne voyait jamais les affectations les plus courantes.
    ("generic_secret_assignment", re.compile(
        r"(?i)\b(api[_-]?key|secret|password|passwd|token)\b\s*[:=]\s*['\"\\]{0,3}[A-Za-z0-9/+_-]{12,}"
    )),
)

REDACTED = "[redacted]"


def pseudonymize_filename(filename: str | None) -> str:
    """Alias STABLE d'un nom de fichier : ``doc-<8 hex>.<ext>``.

    Le nom d'un document est une donnée métier (« Budget_2026_confidentiel.xlsx »
    en dit long). L'alias reste stable pour qu'un même document se reconnaisse
    d'un incident à l'autre, sans jamais révéler son intitulé.
    """
    if not filename:
        return ""
    name = str(filename)
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return f"doc-{digest}.{ext}" if ext and len(ext) <= 8 else f"doc-{digest}"


def scrub_text(value: str) -> str:
    """Retire e-mails et chemins d'un texte libre."""
    if not value:
        return value
    out = _EMAIL_RE.sub(REDACTED, str(value))
    return _PATH_RE.sub(REDACTED, out)


def detect_secrets(payload: Any) -> list[str]:
    """Types de secrets détectés dans une structure. Jamais leur valeur.

    Renvoie des CATÉGORIES (``openai_key``…) : journaliser le secret détecté
    reviendrait à le recopier dans un second endroit.
    """
    import json

    try:
        blob = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        blob = str(payload)

    found: list[str] = []
    for name, pattern in _SECRET_PATTERNS:
        if pattern.search(blob):
            found.append(name)
    return found


def redact_payload(payload: Any, *, include_text: bool, pseudonymize_documents: bool = True) -> Any:
    """Applique la rédaction récursivement.

    ``include_text=False`` (niveau 1) supprime purement les champs porteurs de
    contenu documentaire : extrait, texte, prompt, réponse, question. Ce n'est
    pas un masquage — la clé disparaît, pour qu'aucun consommateur ne croie
    disposer d'un contenu vide alors qu'il a été retiré.
    """
    text_bearing = {"snippet", "text", "content", "prompt", "answer", "question"}
    filename_bearing = {"filename", "expected_document", "source_filename"}
    #: Jamais exporté, quel que soit le niveau.
    always_dropped = {"comment", "user_comment", "created_by", "username", "email"}

    def _walk(node: Any) -> Any:
        if isinstance(node, dict):
            out: dict[str, Any] = {}
            for key, value in node.items():
                lkey = str(key).lower()
                if lkey in always_dropped:
                    continue
                if lkey in text_bearing and not include_text:
                    continue
                if lkey in filename_bearing and pseudonymize_documents:
                    out[key] = pseudonymize_filename(value if isinstance(value, str) else "")
                    continue
                out[key] = _walk(value)
            return out
        if isinstance(node, list):
            return [_walk(v) for v in node]
        if isinstance(node, str):
            return scrub_text(node)
        return node

    return _walk(payload)


def evaluate(payload: Any) -> tuple[str, list[str]]:
    """Statut de rédaction et secrets détectés.

    Un secret détecté BLOQUE. La levée du blocage est une décision humaine, et
    elle passe par ``manually_approved`` — jamais par un nettoyage silencieux.
    """
    secrets = detect_secrets(payload)
    return (STATUS_BLOCKED if secrets else STATUS_PASSED), secrets
