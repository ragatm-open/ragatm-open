# backend/apps/rag_core/services/ingest/extraction/email_sections.py
from __future__ import annotations

"""
Sélection du corps et sectionnement éditorial d'un courriel (RAGatm Open).

Dérivé de la partie GÉNÉRIQUE de l'extracteur Outlook de l'édition privée (même auteur) :
ces fonctions ne manipulent que `ExtractedEmail` et des `Block`, jamais un parseur tiers.
Utilisées par le rail EML (`email_extractor.build_email_result`).

- le corps HTML est nettoyé par le parseur HTML MAISON (`htmltext`) : script/style/iframe
  supprimés AVEC leur contenu, aucune ressource distante chargée, aucun lien suivi ;
- sélection du corps par QUALITÉ (html > plain > rtf), pas par premier champ non vide ;
- signature/disclaimer de fin isolés de façon conservative — marqués `boilerplate` et
  exclus des chunks métier, jamais supprimés silencieusement.
"""

import re
from typing import List, Optional, Tuple

from .base import Block, BlockType
from .email_model import (
    WARN_BODY_FALLBACK_PLAIN,
    WARN_BODY_FALLBACK_RTF,
    WARN_BODY_MISSING,
    WARN_BODY_SHORT,
    ExtractedEmail,
)
from .htmltext import parse_html_document

# Rôles de section du rail email (cf. `indexing._build_email_specs`).
ROLE_HEADER = "header"
ROLE_BODY = "body"
ROLE_BOILERPLATE = "boilerplate"

# --- qualité du corps ---------------------------------------------------------

_REPLACEMENT = "�"
_MAX_REPLACEMENT_RATIO = 0.02
_MARKUP_RE = re.compile(r"</?(?:div|span|table|p|br|td|tr|html|body|font)\b", re.IGNORECASE)
_MAX_MARKUP_HITS = 5
# En deçà, un corps ne porte pas d'information exploitable (accusé de lecture, signature seule).
_MIN_VISIBLE_CHARS = 40
# Un intertitre reste court.
_TITLE_MAX_CHARS = 60


def _visible_chars(text: str) -> int:
    return len(re.sub(r"\s+", "", text or ""))


def _is_usable(text: Optional[str], *, min_visible: int = _MIN_VISIBLE_CHARS) -> bool:
    """Trois disqualifiants objectifs : trop court, mal décodé, balisage non converti."""
    if not text:
        return False
    visible = _visible_chars(text)
    if visible < max(1, min_visible):
        return False
    if text.count(_REPLACEMENT) > max(1, int(len(text) * _MAX_REPLACEMENT_RATIO)):
        return False
    if len(_MARKUP_RE.findall(text)) > _MAX_MARKUP_HITS:
        return False
    return True


# --- boilerplate (signature / disclaimer de fin) ------------------------------

_STRONG_BOILERPLATE_RE = re.compile(
    r"(facebook\.com|linkedin\.com|twitter\.com|x\.com/|instagram\.com|youtube\.com"
    r"|se\s+d[ée]sabonner|unsubscribe"
    r"|does\s+not\s+express\s+the\s+views|reflects?\s+the\s+views\s+of"
    r"|ce\s+message\s+et\s+(?:toutes\s+)?(?:ses|les)\s+pi[èe]ces\s+jointes"
    r"|this\s+(?:e-?mail|message)\s+and\s+any\s+attachments"
    r"|confidentialit[ée]|disclaimer)",
    re.IGNORECASE,
)
_WEAK_BOILERPLATE_RE = re.compile(
    r"^(?:\s*(?:https?://\S+|mailto:\S+|\S+@\S+\.\S+|[-–—•|]+)\s*)+$", re.IGNORECASE
)
_MAX_BOILERPLATE_BLOCK_CHARS = 600
# La signature ne peut pas ÊTRE le message : au-delà de la moitié, on ne retire rien.
_MAX_BOILERPLATE_RATIO = 0.5


def _looks_boilerplate(text: str) -> Tuple[bool, bool]:
    """Renvoie (candidat, signal_fort) pour un bloc terminal."""
    stripped = (text or "").strip()
    if not stripped or len(stripped) > _MAX_BOILERPLATE_BLOCK_CHARS:
        return False, False
    if _STRONG_BOILERPLATE_RE.search(stripped):
        return True, True
    if _WEAK_BOILERPLATE_RE.match(stripped):
        return True, False
    return False, False


def split_boilerplate(blocks: List[Block]) -> Tuple[List[Block], List[Block]]:
    """Isole la signature TERMINALE. Renvoie (corps, boilerplate). Détection positionnelle."""
    if not blocks:
        return [], []
    total_chars = sum(len(b.text or "") for b in blocks)
    cut = len(blocks)
    has_strong = False
    for idx in range(len(blocks) - 1, -1, -1):
        candidate, strong = _looks_boilerplate(blocks[idx].text)
        if not candidate:
            break
        has_strong = has_strong or strong
        cut = idx
    if not has_strong or cut >= len(blocks):
        return blocks, []
    if cut == 0:
        return blocks, []
    removed_chars = sum(len(b.text or "") for b in blocks[cut:])
    if total_chars and removed_chars > total_chars * _MAX_BOILERPLATE_RATIO:
        return blocks, []
    return blocks[:cut], blocks[cut:]


# --- corps -------------------------------------------------------------------


def _blocks_from_html(html: str) -> Tuple[List[Block], bool, int]:
    _title, blocks, removed_active, link_count = parse_html_document(html or "")
    return [b for b in blocks if (b.text or "").strip()], removed_active, link_count


def _blocks_from_text(text: str) -> List[Block]:
    chunks = re.split(r"\n\s*\n", text or "")
    return [Block(type=BlockType.PARAGRAPH, text=c.strip()) for c in chunks if c.strip()]


def select_body(email: ExtractedEmail) -> Tuple[List[Block], str, List[str], dict]:
    """Choisit la MEILLEURE représentation du corps. Renvoie (blocs, kind, warnings, infos)."""
    warnings: List[str] = []
    info: dict = {}

    if email.body_html:
        blocks, removed_active, link_count = _blocks_from_html(email.body_html)
        text = "\n\n".join(b.text for b in blocks)
        if _is_usable(text):
            info["links"] = link_count
            if removed_active:
                warnings.append("active_content_removed")
            warnings.append("html_treated_as_untrusted")
            return blocks, "html", warnings, info

    if email.body_plain and _is_usable(email.body_plain):
        warnings.append(WARN_BODY_FALLBACK_PLAIN)
        return _blocks_from_text(email.body_plain), "plain", warnings, info

    if email.body_rtf_text and _is_usable(email.body_rtf_text):
        warnings.append(WARN_BODY_FALLBACK_RTF)
        return _blocks_from_text(email.body_rtf_text), "rtf", warnings, info

    # Dernier recours : un corps COURT mais lisible est un contenu, pas un défaut. Signalé.
    if email.body_plain and _is_usable(email.body_plain, min_visible=1):
        warnings.append(WARN_BODY_SHORT)
        return _blocks_from_text(email.body_plain), "plain", warnings, info
    if email.body_html:
        blocks, _removed, link_count = _blocks_from_html(email.body_html)
        if _is_usable("\n\n".join(b.text for b in blocks), min_visible=1):
            info["links"] = link_count
            warnings.extend([WARN_BODY_SHORT, "html_treated_as_untrusted"])
            return blocks, "html", warnings, info

    warnings.append(WARN_BODY_MISSING)
    return [], "none", warnings, info


# --- rubriques ---------------------------------------------------------------


def _is_title_like(block: Block) -> bool:
    """Un vrai `<h*>` fait foi ; sinon heuristique : ligne courte, sans ponctuation finale."""
    if block.type == BlockType.TITLE:
        return bool((block.text or "").strip())
    text = (block.text or "").strip()
    if not text or "\n" in text or len(text) > _TITLE_MAX_CHARS:
        return False
    return not text.endswith((".", ":", ";", ",", "!", "?"))


def group_sections(blocks: List[Block]) -> List[dict]:
    """Regroupe les blocs en RUBRIQUES éditoriales : un intertitre et sa suite."""
    sections: List[dict] = []
    title: Optional[str] = None
    buffer: List[str] = []

    def flush() -> None:
        if not buffer:
            return
        text = "\n\n".join(buffer).strip()
        if text:
            sections.append({"role": ROLE_BODY, "index": len(sections) + 1, "title": title, "text": text})

    for block in blocks:
        text = (block.text or "").strip()
        if not text:
            continue
        if _is_title_like(block):
            flush()
            buffer = [text]
            title = text[:_TITLE_MAX_CHARS]
        else:
            buffer.append(text)
    flush()
    return sections
