# backend/apps/rag_core/services/ingest/extraction/htmltext.py
from __future__ import annotations

"""
Traitement HTML SÉCURISÉ basé sur la stdlib (html.parser).

Tout HTML est traité comme contenu hostile :
- les balises actives (script/style/iframe/object/embed/svg/...) sont supprimées
  AVEC leur contenu ;
- aucune ressource externe n'est chargée, aucun lien n'est suivi ;
- la sortie est du TEXTE neutralisé (jamais réinterprété comme HTML) ;
- on ne dépend d'aucune librairie tierce (pas de bs4).
"""

import re
from html.parser import HTMLParser
from typing import List, Optional, Tuple

from .base import Block, BlockType

# Balises dont le CONTENU doit être entièrement ignoré (bruit / actif / dangereux).
# NB: on n'inclut PAS <head> pour pouvoir récupérer <title> ; les éléments actifs
# de <head> (script/style) sont déjà couverts ci-dessous.
_DROP_CONTENT = {
    "script", "style", "noscript", "iframe", "object", "embed",
    "svg", "math", "template", "nav", "footer", "form",
}
# Balises de structure utiles à la lisibilité (provoquent un saut de bloc).
_BLOCK_TAGS = {
    "p", "div", "section", "article", "br", "li", "tr", "table",
    "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "pre", "blockquote",
}
_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}


class _SafeHTMLParser(HTMLParser):
    """Parse tolérant aux erreurs ; collecte un texte propre + des blocs typés."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: Optional[str] = None
        self.blocks: List[Block] = []
        self.removed_active = False
        self.link_count = 0

        self._drop_depth = 0           # >0 => on est dans une balise à ignorer
        self._in_title = False
        self._cur_heading: Optional[int] = None
        self._buf: List[str] = []      # buffer du bloc courant
        self._cur_type = BlockType.PARAGRAPH

    # -- gestion des balises ------------------------------------------
    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in _DROP_CONTENT:
            if tag in ("script", "style", "iframe", "object", "embed"):
                self.removed_active = True
            self._drop_depth += 1
            return
        if self._drop_depth:
            return

        if tag == "title":
            self._in_title = True
            return
        if tag == "a":
            self.link_count += 1
        if tag in _HEADINGS:
            self._flush()
            self._cur_heading = _HEADINGS[tag]
            self._cur_type = BlockType.TITLE
            return
        if tag == "li":
            self._flush()
            self._cur_type = BlockType.LIST
            return
        if tag in _BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _DROP_CONTENT:
            if self._drop_depth:
                self._drop_depth -= 1
            return
        if self._drop_depth:
            return
        if tag == "title":
            self._in_title = False
            self.title = (" ".join(self._buf)).strip() or self.title
            self._buf = []
            return
        if tag in _HEADINGS or tag in _BLOCK_TAGS:
            self._flush()

    def handle_data(self, data):
        if self._drop_depth or not data:
            return
        if self._in_title:
            self._buf.append(data)
            return
        text = data.strip("\n")
        if text.strip():
            self._buf.append(text)

    # -- finalisation -------------------------------------------------
    def _flush(self):
        text = re.sub(r"[ \t]+", " ", " ".join(self._buf)).strip()
        self._buf = []
        if text:
            level = self._cur_heading
            self.blocks.append(
                Block(type=self._cur_type, text=text, level=level)
            )
        self._cur_heading = None
        self._cur_type = BlockType.PARAGRAPH

    def finish(self):
        self._flush()


def parse_html_document(html: str) -> Tuple[Optional[str], List[Block], bool, int]:
    """Parse un document HTML. Renvoie (title, blocks, removed_active, link_count)."""
    parser = _SafeHTMLParser()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:
        # html.parser est très tolérant ; en dernier recours on ne casse jamais.
        pass
    parser.finish()
    return parser.title, parser.blocks, parser.removed_active, parser.link_count


# ---------------------------------------------------------------------
# Neutralisation du HTML inline (utilisé par l'extracteur Markdown)
# ---------------------------------------------------------------------
_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style|iframe|object|embed)\b[^>]*>.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")


def neutralize_inline_html(text: str) -> Tuple[str, bool]:
    """Retire le HTML inline d'un texte : supprime script/style avec leur contenu,
    puis dé-balise le reste (on garde le texte visible).

    Renvoie (texte_neutralisé, quelque_chose_a_été_retiré).
    """
    if not text or "<" not in text:
        return text, False

    cleaned = _SCRIPT_STYLE_RE.sub(" ", text)
    removed = cleaned != text
    cleaned2 = _TAG_RE.sub("", cleaned)
    removed = removed or (cleaned2 != cleaned)
    return cleaned2, removed
