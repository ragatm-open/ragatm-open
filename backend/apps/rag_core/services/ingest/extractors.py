# backend/apps/rag_core/services/ingest/extractors.py
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from io import BytesIO
from typing import Callable, Dict, Iterable, List, Tuple

logger = logging.getLogger(__name__)


def extract_text_from_txt_bytes(raw: bytes) -> str:
    """
    Best-effort decoding for text uploads.
    We keep it conservative: try utf-8 first, then common fallbacks.
    """
    raw = raw or b""
    if not raw:
        return ""

    for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc, errors="replace")
        except Exception:
            continue
    return raw.decode("utf-8", errors="replace")


def extract_text_pages_from_pdf_bytes(
    raw: bytes,
    *,
    max_pages: int | None = None,
    timeout_s: int | None = None,
    enforce_limits: bool = True,
) -> List[Tuple[int, str]]:
    """
    Extract text per page from a PDF (NO OCR).
    Returns: [(page_number (1-based), text), ...]

    Implementation notes:
    - Uses pypdf (preferred) if available.
    - Returns [] on a genuine parse failure (corrupt/empty PDF).
    - When `enforce_limits=True` (default), applique les limites applicatives :
        * leve `UploadLimitExceeded` si le PDF depasse UPLOAD_PDF_MAX_PAGES ;
        * leve `PdfExtractionTimeout` si l'extraction depasse UPLOAD_EXTRACTION_TIMEOUT_S.
      Ces exceptions sont volontairement propagees pour etre surfacees (HTTP /
      error_message). Mettre `enforce_limits=False` pour les appels best-effort
      (ex. extraction de date) qui ne doivent jamais bloquer.
    - Normalizes whitespace lightly.
    """
    raw = raw or b""
    if not raw:
        return []

    try:
        # pypdf is the modern successor of PyPDF2 in most environments
        from pypdf import PdfReader  # type: ignore
    except Exception:
        try:
            # fallback: some envs still ship PyPDF2
            from PyPDF2 import PdfReader  # type: ignore
        except Exception as e:
            logger.warning("PDF extractor missing dependency (pypdf/PyPDF2): %s", e)
            return []

    try:
        reader = PdfReader(BytesIO(raw))
    except Exception as e:
        logger.exception("PDF open/parse failed: %s", e)
        return []

    n_total = len(reader.pages)

    # Limite applicative: nombre de pages (avant toute extraction couteuse).
    if enforce_limits:
        from .limits import UploadLimitExceeded, max_pdf_pages

        page_limit = max_pdf_pages()
        if page_limit and n_total > page_limit:
            raise UploadLimitExceeded(
                "pdf_too_many_pages",
                f"PDF trop volumineux : {n_total} pages (limite : {page_limit}). "
                "Ajustez UPLOAD_PDF_MAX_PAGES si necessaire.",
            )

    n_pages = n_total
    if max_pages is not None:
        n_pages = min(n_pages, int(max_pages))

    def _extract() -> List[Tuple[int, str]]:
        out: List[Tuple[int, str]] = []
        for i in range(n_pages):
            try:
                page = reader.pages[i]
                txt = page.extract_text() or ""
            except Exception:
                txt = ""

            txt = _normalize_pdf_text(txt)
            out.append((i + 1, txt))
        return out

    # Limite applicative: timeout d'extraction.
    if enforce_limits:
        from .limits import run_with_timeout

        return run_with_timeout(_extract, timeout_s)

    try:
        return _extract()
    except Exception as e:
        logger.exception("PDF text extraction failed: %s", e)
        return []


_WS_RE = re.compile(r"[ \t]+")


def _normalize_pdf_text(s: str) -> str:
    if not s:
        return ""
    # Keep newlines (useful for headers), but normalize spaces.
    s = s.replace("\u00a0", " ")
    s = _WS_RE.sub(" ", s)
    # Collapse excessive blank lines
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


# =====================================================================
# Fallback d'extraction PDF NATIVE (sans OCR) \u2014 P0
# =====================================================================
# Objectif : ne plus conclure "scann\u00e9/OCR requis" tant qu'un autre moteur
# d'extraction LOCAL peut r\u00e9cup\u00e9rer du texte. `pypdf` reste le moteur primaire ;
# les moteurs de secours sont import-gard\u00e9s (leur absence ne casse RIEN : on
# retombe proprement sur pypdf seul).
#
# Tous les moteurs renvoient le M\u00caME format : [(page_number 1-based, text), ...].
# Aucun appel r\u00e9seau, aucune d\u00e9pendance syst\u00e8me : extraction native uniquement.

PdfPages = List[Tuple[int, str]]


def useful_pdf_chars(pages: PdfPages) -> int:
    """Nombre total de caract\u00e8res **exploitables** sur l'ensemble des pages.

    \u00ab Utile \u00bb ne veut pas dire \u00ab pr\u00e9sent \u00bb. Une police sans table ToUnicode fait produire
    des milliers de caract\u00e8res de s\u00e9lecteurs de glyphes (``/1 /44 /42``) : compter leur
    longueur faisait passer le document pour correctement extrait, donc ni fallback natif
    ni OCR \u2014 et un index rempli de chunks morts. Une page inexploitable compte d\u00e9sormais
    z\u00e9ro, ce qui d\u00e9clenche la suite de la cha\u00eene (cf. `extraction.text_quality`).
    """
    from .extraction.text_quality import usable_chars

    return sum(usable_chars(t or "") for _, t in (pages or []))


def extract_text_pages_from_pdf_bytes_pymupdf(raw: bytes, *, max_pages: int | None = None) -> PdfPages:
    """Extraction native via PyMuPDF (fitz). Import-gard\u00e9 : [] si indisponible/\u00e9chec."""
    raw = raw or b""
    if not raw:
        return []
    try:
        import fitz  # PyMuPDF
    except Exception:
        return []
    out: PdfPages = []
    try:
        with fitz.open(stream=raw, filetype="pdf") as doc:
            n = doc.page_count
            if max_pages is not None:
                n = min(n, int(max_pages))
            for i in range(n):
                try:
                    txt = doc.load_page(i).get_text("text") or ""
                except Exception:
                    txt = ""
                out.append((i + 1, _normalize_pdf_text(txt)))
    except Exception as e:  # PDF illisible pour ce moteur : on abandonne proprement
        logger.debug("pymupdf extraction failed: %s", e)
        return []
    return out


def extract_text_pages_from_pdf_bytes_pdfminer(raw: bytes, *, max_pages: int | None = None) -> PdfPages:
    """Extraction native via pdfminer.six. Import-gard\u00e9 : [] si indisponible/\u00e9chec."""
    raw = raw or b""
    if not raw:
        return []
    try:
        from pdfminer.high_level import extract_pages  # type: ignore
    except Exception:
        return []
    out: PdfPages = []
    try:
        for i, layout in enumerate(extract_pages(BytesIO(raw))):
            if max_pages is not None and i >= int(max_pages):
                break
            parts: List[str] = []
            try:
                for el in layout:
                    getter = getattr(el, "get_text", None)
                    if callable(getter):
                        parts.append(getter())
            except Exception:
                parts = []
            out.append((i + 1, _normalize_pdf_text("".join(parts))))
    except Exception as e:
        logger.debug("pdfminer extraction failed: %s", e)
        return []
    return out


def extract_text_pages_from_pdf_bytes_pdfplumber(raw: bytes, *, max_pages: int | None = None) -> PdfPages:
    """Extraction native via pdfplumber. Import-gard\u00e9 : [] si indisponible/\u00e9chec.

    Pr\u00e9par\u00e9 pour P2 (extraction de tableaux) ; ici on n'extrait que le texte.
    """
    raw = raw or b""
    if not raw:
        return []
    try:
        import pdfplumber  # type: ignore
    except Exception:
        return []
    out: PdfPages = []
    try:
        with pdfplumber.open(BytesIO(raw)) as pdf:
            n = len(pdf.pages)
            if max_pages is not None:
                n = min(n, int(max_pages))
            for i in range(n):
                try:
                    txt = pdf.pages[i].extract_text() or ""
                except Exception:
                    txt = ""
                out.append((i + 1, _normalize_pdf_text(txt)))
    except Exception as e:
        logger.debug("pdfplumber extraction failed: %s", e)
        return []
    return out


# Moteurs de secours disponibles (le moteur primaire pypdf est g\u00e9r\u00e9 \u00e0 part).
_FALLBACK_ENGINES: Dict[str, Callable[..., PdfPages]] = {
    "pymupdf": extract_text_pages_from_pdf_bytes_pymupdf,
    "fitz": extract_text_pages_from_pdf_bytes_pymupdf,  # alias
    "pdfminer": extract_text_pages_from_pdf_bytes_pdfminer,
    "pdfplumber": extract_text_pages_from_pdf_bytes_pdfplumber,
}


#: Noms configurables d\u00e9signant PyMuPDF (cf. ``_FALLBACK_ENGINES`` et son alias).
_PYMUPDF_ALIASES = frozenset({"pymupdf", "fitz"})

#: Classes de qualit\u00e9 de la PIRE \u00e0 la MEILLEURE. Reprises **telles quelles** de
#: ``extraction.text_quality``, qui les d\u00e9clare d\u00e9j\u00e0 dans cet ordre : aucune classe
#: n'est invent\u00e9e ici, aucune n'est red\u00e9finie.
_QUALITY_ORDER = ("empty", "glyph_garbage", "cid_garbage", "degraded", "partial", "usable")


def _pages_text(pages: PdfPages) -> str:
    """Texte d'un jeu de pages, assembl\u00e9 comme l'extracteur assemble ``res.text``."""
    return "\n\n".join(
        (t or "").strip() for _, t in (pages or []) if (t or "").strip()
    ).strip()


def _quality_rank(pages: PdfPages) -> int:
    """Rang de qualit\u00e9 dans ``_QUALITY_ORDER``, ou ``-1`` si la classe est inconnue."""
    from .extraction.text_quality import classify_text

    try:
        return _QUALITY_ORDER.index(classify_text(_pages_text(pages)))
    except ValueError:  # classe hors liste : on ne bloque rien
        return -1


def _is_quality_regression(candidate: PdfPages, current: PdfPages) -> bool:
    """Le candidat est-il STRICTEMENT moins bon que l'extraction courante ?

    Compter les caract\u00e8res ne suffit pas \u00e0 d\u00e9cider. Mesur\u00e9 sur le corpus terrain
    (INGEST-NATIVE-CHAIN-01) : sur une planche A0, pypdf et pymupdf rendent 32 caract\u00e8res
    propres class\u00e9s ``usable`` (\u00ab Voie priv\u00e9e \u00bb), pdfminer en rend 40 class\u00e9s ``partial``
    \u2014 le M\u00caME texte \u00e9clat\u00e9 caract\u00e8re par caract\u00e8re et invers\u00e9. Les 8 caract\u00e8res de plus
    ne sont pas du contenu : ce sont les s\u00e9parateurs de la fragmentation. Le compte seul
    faisait donc pr\u00e9f\u00e9rer la pire des deux extractions.

    Une classe inconnue ne bloque jamais : le comportement historique est conserv\u00e9.
    """
    rang_candidat = _quality_rank(candidate)
    rang_courant = _quality_rank(current)
    if rang_candidat < 0 or rang_courant < 0:
        return False
    return rang_candidat < rang_courant


@dataclass
class NativePdfSelection:
    """R\u00e9sultat de la s\u00e9lection multi-moteur d'extraction PDF native."""

    pages: PdfPages
    engine: str
    fallback_used: bool
    attempted_engines: List[str] = field(default_factory=list)
    chars_by_engine: Dict[str, int] = field(default_factory=dict)
    #: La porte native a-t-elle court-circuit\u00e9 les moteurs suivants ? Tra\u00e7abilit\u00e9 :
    #: sans ce drapeau, une cha\u00eene \u00e9court\u00e9e est indiscernable d'une cha\u00eene qui a tout
    #: tent\u00e9 sans rien trouver.
    gate_applied: bool = False


def select_best_native_pdf_extraction(
    raw: bytes,
    *,
    pypdf_pages: PdfPages,
    engines: Iterable[str],
    max_pages: int | None = None,
    min_total_chars: int = 200,
) -> NativePdfSelection:
    """Choisit la MEILLEURE extraction native disponible.

    Politique (P0) :
    - pypdf est le moteur primaire et le r\u00e9sultat de r\u00e9f\u00e9rence ;
    - si pypdf atteint d\u00e9j\u00e0 ``min_total_chars``, on le garde tel quel (aucun
      fallback d\u00e9clench\u00e9) ;
    - sinon on tente les moteurs de secours dans l'ordre fourni et on retient
      le r\u00e9sultat avec le PLUS de caract\u00e8res utiles ;
    - en cas d'\u00e9galit\u00e9 on conserve pypdf (changement de comportement minimal) ;
    - on ne remplace jamais pypdf par un fallback STRICTEMENT moins bon.

    Deux garde-fous s'ajoutent \u00e0 cette politique (INGEST-NATIVE-CHAIN-02) :

    - **porte native** : si PyMuPDF a bien \u00e9num\u00e9r\u00e9 les pages mais n'y trouve AUCUN
      caract\u00e8re exploitable, les moteurs suivants sont court-circuit\u00e9s (cf. ci-dessous) ;
    - **garde de qualit\u00e9** : un candidat plus riche mais de classe strictement pire ne
      remplace jamais l'extraction courante (cf. ``_is_quality_regression``).
    """
    best_pages: PdfPages = list(pypdf_pages or [])
    best_engine = "pypdf"
    best_chars = useful_pdf_chars(best_pages)

    attempted: List[str] = []
    chars_by_engine: Dict[str, int] = {"pypdf": best_chars}

    # pypdf d\u00e9j\u00e0 suffisant : aucune tentative de fallback.
    if best_chars >= int(min_total_chars):
        return NativePdfSelection(
            pages=best_pages, engine=best_engine, fallback_used=False,
            attempted_engines=attempted, chars_by_engine=chars_by_engine,
        )

    # Moteurs retenus, dans l'ordre configur\u00e9, d\u00e9dupliqu\u00e9s, connus seuls.
    noms: List[str] = []
    for raw_name in engines:
        name = (raw_name or "").strip().lower()
        if not name or name == "pypdf" or name in noms:
            continue
        if name in _FALLBACK_ENGINES:
            noms.append(name)

    # M\u00e9mo\u00efsation PAR FONCTION : garantit **au plus une** extraction par moteur et par
    # document, y compris quand la sonde et la boucle visent le m\u00eame moteur, et y compris
    # si la configuration cite \u00e0 la fois ``pymupdf`` et son alias ``fitz``.
    cache: Dict[object, Tuple[PdfPages, int]] = {}

    def _tenter(nom: str) -> Tuple[PdfPages, int]:
        func = _FALLBACK_ENGINES[nom]
        if func in cache:
            return cache[func]
        try:
            pages = func(raw, max_pages=max_pages) or []
        except Exception as e:  # d\u00e9fense en profondeur : un moteur ne crashe jamais la s\u00e9lection
            logger.debug("fallback engine %s raised: %s", nom, e)
            pages = []
        resultat = (pages, useful_pdf_chars(pages))
        cache[func] = resultat
        return resultat

    # --- PORTE NATIVE -----------------------------------------------------------
    # PyMuPDF lit la structure d'objets texte pour un co\u00fbt n\u00e9gligeable (mesur\u00e9 0,04 s
    # m\u00e9dian, 0,45 s au pire) l\u00e0 o\u00f9 pdfminer et pdfplumber co\u00fbtent 7,5 s m\u00e9dians et
    # jusqu'\u00e0 100 s sur une planche. Mesur\u00e9 sur les 1947 PDF du corpus terrain : sur les
    # 91 documents o\u00f9 PyMuPDF ne trouve aucun caract\u00e8re exploitable, **aucun** moteur
    # suivant n'en trouve (pdfminer : jamais plus de 196 caract\u00e8res en 109 tentatives ;
    # pdfplumber : aucune s\u00e9lection remport\u00e9e). Les tenter est donc du temps pur perdu.
    #
    # La sonde vise PyMuPDF **explicitement**, jamais \u00ab le premier moteur configur\u00e9 \u00bb :
    # la s\u00fbret\u00e9 de la porte ne doit pas d\u00e9pendre de l'ordre de
    # ``DOCUMENT_PDF_NATIVE_FALLBACK_ENGINES``. Elle ne r\u00e9introduit pas un moteur que la
    # configuration a retir\u00e9 : sans PyMuPDF dans la liste, la porte ne s'applique pas.
    #
    # ``pages`` NON VIDE est exig\u00e9 en plus de z\u00e9ro caract\u00e8re. Les moteurs sont
    # import-gard\u00e9s et rendent ``[]`` aussi bien quand la biblioth\u00e8que est absente que
    # quand le PDF leur est illisible : sans cette condition, un PyMuPDF non install\u00e9
    # ferait sauter pdfminer et pdfplumber **sans avoir rien lu** \u2014 exactement la perte de
    # contenu que la porte doit interdire. Un document sans texte, lui, rend une entr\u00e9e
    # par page avec un texte vide.
    nom_sonde = next((n for n in noms if n in _PYMUPDF_ALIASES), None)
    if nom_sonde is not None:
        pages_sonde, chars_sonde = _tenter(nom_sonde)
        if pages_sonde and chars_sonde == 0:
            attempted.append(nom_sonde)
            chars_by_engine[nom_sonde] = chars_sonde
            logger.info(
                "pdf native gate: no text object found by %s on %s page(s) "
                "-> skipping %s",
                nom_sonde, len(pages_sonde),
                ",".join(n for n in noms if n != nom_sonde) or "(aucun)",
            )
            return NativePdfSelection(
                pages=best_pages, engine=best_engine, fallback_used=False,
                attempted_engines=attempted, chars_by_engine=chars_by_engine,
                gate_applied=True,
            )

    for name in noms:
        attempted.append(name)
        pages, c = _tenter(name)
        chars_by_engine[name] = c
        # On ne remplace que si STRICTEMENT meilleur (\u00e9galit\u00e9 => garde pypdf/pr\u00e9c\u00e9dent)
        # ET si la qualit\u00e9 ne r\u00e9gresse pas.
        if pages and c > best_chars:
            if _is_quality_regression(pages, best_pages):
                logger.info(
                    "pdf native quality guard: %s rejected (%s chars > %s but class worse)",
                    name, c, best_chars,
                )
            else:
                best_chars, best_pages, best_engine = c, list(pages), name
        # Assez bon via un fallback : on s'arr\u00eate (\u00e9conomie de temps CPU).
        if best_engine != "pypdf" and best_chars >= int(min_total_chars):
            break

    return NativePdfSelection(
        pages=best_pages,
        engine=best_engine,
        fallback_used=(best_engine != "pypdf"),
        attempted_engines=attempted,
        chars_by_engine=chars_by_engine,
    )
