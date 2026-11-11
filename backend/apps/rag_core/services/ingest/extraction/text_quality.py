# backend/apps/rag_core/services/ingest/extraction/text_quality.py
from __future__ import annotations

"""Qualité d'un texte extrait — module **pur** (ni I/O, ni réseau, ni LLM).

Pourquoi ce module existe
-------------------------
Le pipeline mesurait l'utilité d'un texte par sa **longueur** (`len(text.strip())`).
Un PDF dont les polices n'exposent aucune table ToUnicode produit pourtant des milliers
de caractères — mais ce sont des sélecteurs de glyphes (``/1 /44 /42 □``) ou des marqueurs
``(cid:12)``. Long ≠ exploitable : le document passait pour « extrait avec succès », le
fallback natif et l'OCR ne se déclenchaient jamais, et l'index se remplissait de chunks
morts, vectorisés pour rien.

On ne se fie donc pas à une regex unique (une liste de départements « 11/18/44/45/47 » la
déclencherait à tort — c'est arrivé). On croise plusieurs mesures indépendantes, dont la
plus discriminante : **la proportion de mots contenant une voyelle**. Un texte français
réel est au-dessus de 90 % ; un mapping de glyphes cassé produit ``ABCD``, ``EDABF``,
``LBN`` — proche de 0.
"""

import re
import unicodedata
from dataclasses import dataclass, field

# Classes de qualité, de la pire à la meilleure.
EMPTY = "empty"                    # rien d'exploitable
GLYPH = "glyph_garbage"            # sélecteurs de glyphes : police sans ToUnicode
CID = "cid_garbage"                # marqueurs (cid:N) : glyphes non mappés (pdfminer)
DEGRADED = "degraded"              # du texte, mais trop bruité (OCR médiocre)
PARTIAL = "partial"                # exploitable mais pauvre
USABLE = "usable"

UNUSABLE_CLASSES = frozenset({EMPTY, GLYPH, CID, DEGRADED})

_GLYPH_TOKEN = re.compile(r"/\d{1,3}\b")
_CID_MARKER = re.compile(r"\(cid:\d+\)")
_WORD = re.compile(r"[^\W\d_]{3,}", re.UNICODE)
_ALPHA = re.compile(r"[^\W\d_]", re.UNICODE)
_DIGIT = re.compile(r"\d")
_PUNCT = re.compile(r"[.,;:!?'\"()\[\]%€$/\\+=&@#*|«»’-]")
_VOWELS = set("aeiouyàâäéèêëîïôöùûüÿœæ")

# Seuils — calibrés sur le corpus terrain (cf. benchmarks/README.md). Volontairement
# prudents : un faux « inexploitable » déclenche un OCR inutile (coûteux mais sans dégât),
# un faux « exploitable » réinjecte des chunks morts dans l'index (silencieux et durable).
MIN_CHARS = 24
# Plancher, pas un critère de qualité : il ne sert qu'à écarter un texte sans aucun mot
# (une page de chiffres purs). Le monter rejetterait des notes courtes légitimes — « Massif
# M01 — note courte de chantier. » ne compte que 4 mots et reste parfaitement exploitable.
MIN_WORDS = 2
MIN_VOWEL_WORD_RATIO = 0.55
MIN_ALPHA_RATIO = 0.30
MAX_GLYPH_RATIO = 0.30
MAX_CID_RATIO = 0.30
MAX_BOX_RATIO = 0.05


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


# Caractères de contrôle : illégaux en base (PostgreSQL rejette 0x00) et absents de tout
# texte légitime. L'OCR en produit sur certaines pages — sans ce nettoyage, un document
# entier est perdu à l'indexation pour un octet.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_text(text: str) -> str:
    """Retire les caractères de contrôle en préservant tabulations et sauts de ligne."""
    return _CONTROL.sub("", text or "")


@dataclass
class TextMetrics:
    """Mesures brutes. Aucune décision ici — seulement des faits mesurés."""

    n_chars: int = 0
    n_alpha: int = 0
    n_words: int = 0
    n_vowel_words: int = 0
    n_glyph_tokens: int = 0
    n_cid_markers: int = 0
    n_boxes: int = 0
    n_lines: int = 0
    n_readable_lines: int = 0
    words_sample: list[str] = field(default_factory=list)

    @property
    def alpha_ratio(self) -> float:
        return self.n_alpha / self.n_chars if self.n_chars else 0.0

    @property
    def vowel_word_ratio(self) -> float:
        """Part des mots contenant une voyelle — le signal le plus discriminant."""
        return self.n_vowel_words / self.n_words if self.n_words else 0.0

    @property
    def glyph_ratio(self) -> float:
        """Part du texte occupée par des sélecteurs de glyphes (≈3 caractères chacun)."""
        return min(1.0, self.n_glyph_tokens * 3 / self.n_chars) if self.n_chars else 0.0

    @property
    def cid_ratio(self) -> float:
        return min(1.0, self.n_cid_markers * 8 / self.n_chars) if self.n_chars else 0.0

    @property
    def box_ratio(self) -> float:
        return self.n_boxes / self.n_chars if self.n_chars else 0.0

    @property
    def readable_line_ratio(self) -> float:
        return self.n_readable_lines / self.n_lines if self.n_lines else 0.0

    def as_dict(self) -> dict:
        return {
            "n_chars": self.n_chars, "n_words": self.n_words,
            "alpha_ratio": round(self.alpha_ratio, 3),
            "vowel_word_ratio": round(self.vowel_word_ratio, 3),
            "glyph_ratio": round(self.glyph_ratio, 3),
            "cid_ratio": round(self.cid_ratio, 3),
            "box_ratio": round(self.box_ratio, 3),
            "readable_line_ratio": round(self.readable_line_ratio, 3),
        }


def text_metrics(text: str) -> TextMetrics:
    t = text or ""
    m = TextMetrics()
    m.n_chars = len(t.strip())
    if not m.n_chars:
        return m

    m.n_cid_markers = len(_CID_MARKER.findall(t))
    clean = _CID_MARKER.sub(" ", t)  # les marqueurs ne sont pas du texte : les neutraliser

    m.n_alpha = len(_ALPHA.findall(clean))
    m.n_glyph_tokens = len(_GLYPH_TOKEN.findall(clean))
    m.n_boxes = clean.count("□") + clean.count("�")

    words = [w for w in _WORD.findall(clean) if w.lower() != "cid"]
    m.n_words = len(words)
    m.n_vowel_words = sum(1 for w in words if set(_strip_accents(w).lower()) & _VOWELS)
    m.words_sample = words[:10]

    lines = [ln for ln in clean.split("\n") if ln.strip()]
    m.n_lines = len(lines)
    m.n_readable_lines = sum(
        1 for ln in lines
        if len([w for w in _WORD.findall(ln) if set(_strip_accents(w).lower()) & _VOWELS]) >= 2
    )
    return m


def classify_text(text: str) -> str:
    """Classe un texte extrait. Les causes sont distinguées : la remédiation diffère.

    - `glyph_garbage` / `cid_garbage` : l'extraction est cassée → OCR de la page rendue.
    - `degraded` : l'OCR a tourné mais le résultat est bruité → révision humaine.
    """
    m = text_metrics(text)
    if m.n_chars < MIN_CHARS:
        return EMPTY

    # Les causes sont testées AVANT le manque de mots : une page de glyphes porte des
    # milliers de caractères et zéro mot. La classer « vide » masquerait la cause réelle
    # et donc la remédiation (réextraire ≠ OCRiser ≠ réviser).
    if m.cid_ratio > MAX_CID_RATIO and m.vowel_word_ratio < MIN_VOWEL_WORD_RATIO:
        return CID
    # Sélecteurs /N nombreux ET « mots » sans voyelle : mapping de police absent. Les deux
    # conditions sont requises — une liste « 11/18/44/45 » dans un vrai texte coche la
    # première, jamais la seconde (faux positif observé sur le corpus terrain).
    if m.glyph_ratio > MAX_GLYPH_RATIO and m.vowel_word_ratio < MIN_VOWEL_WORD_RATIO:
        return GLYPH
    if m.box_ratio > MAX_BOX_RATIO and m.vowel_word_ratio < MIN_VOWEL_WORD_RATIO:
        return GLYPH

    if m.n_words < MIN_WORDS:
        return EMPTY

    # Du texte, mais qui ne ressemble pas à de la langue.
    #
    # Seul le ratio de voyelles condamne. `alpha_ratio` ne peut PAS servir de critère
    # autonome : un tableau financier dense est massivement chiffré (alpha_ratio ≈ 0.2)
    # tout en restant parfaitement lisible — 6 PDF sains du corpus terrain étaient rejetés
    # à ce seul titre. Un faible taux de lettres décrit un contenu chiffré, pas un texte
    # cassé. Il ne sert donc que de signal secondaire, jamais de verdict.
    if m.vowel_word_ratio < MIN_VOWEL_WORD_RATIO:
        return DEGRADED
    if m.readable_line_ratio < 0.25 or m.alpha_ratio < MIN_ALPHA_RATIO:
        return PARTIAL
    return USABLE


def extraction_quality_score(text: str) -> float:
    """Score agrégé dans [0, 1]. Indicatif (traçabilité) — les décisions passent par
    `classify_text`, qui distingue les causes."""
    m = text_metrics(text)
    if not m.n_chars:
        return 0.0
    penalties = (1.0 - m.glyph_ratio) * (1.0 - m.cid_ratio) * (1.0 - min(1.0, m.box_ratio * 10))
    return round(max(0.0, min(1.0, m.vowel_word_ratio * penalties)), 3)


def is_glyph_garbage(text: str) -> bool:
    """Extraction cassée : la police n'expose pas de correspondance vers l'Unicode."""
    return classify_text(text) in (GLYPH, CID)


def is_unusable_pdf_text(text: str) -> bool:
    """Ce texte doit-il être refusé comme source d'indexation ?

    Un `True` ici déclenche le fallback (autre moteur, puis OCR) et, en dernier ressort,
    l'échec explicite du document — jamais un chunk mort.
    """
    return classify_text(text) in UNUSABLE_CLASSES


def usable_chars(text: str) -> int:
    """Longueur du texte **s'il est exploitable**, 0 sinon.

    Remplace le comptage brut de caractères : c'est la mesure qui décide du fallback.
    """
    return 0 if is_unusable_pdf_text(text) else len((text or "").strip())
