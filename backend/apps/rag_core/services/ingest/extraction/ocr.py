# backend/apps/rag_core/services/ingest/extraction/ocr.py
from __future__ import annotations

"""
OCR OPTIONNEL et configurable.

Garanties :
- OCR désactivé par défaut (DOCUMENT_OCR_ENABLED=0) ;
- aucun appel externe possible sans configuration explicite : seul le provider
  "tesseract" (LOCAL) est implémenté ; tout provider non implémenté lève une
  erreur contrôlée AVANT toute I/O réseau ;
- timeout par page + limite de pages ;
- score de confiance si disponible ;
- aucun contenu OCR n'est logué ;
- fichiers temporaires confinés et supprimés (gérés par les libs, pas d'écriture
  disque ici : on opère en mémoire).

Le provider local (pytesseract/PIL) est import-gardé : son absence n'empêche pas
le reste de l'extraction (échec contrôlé avec warning).
"""

import logging
from dataclasses import dataclass, field
from time import monotonic
from typing import List, Optional, Tuple

from ..limits import run_with_timeout
from . import config
from .base import ExtractionError

logger = logging.getLogger(__name__)


class OcrUnavailable(ExtractionError):
    code = "ocr_unavailable"


class OcrDisabled(ExtractionError):
    code = "ocr_disabled"


class OcrPageTimeout(ExtractionError):
    """Le budget de page a expiré : le travail Tesseract a été ANNULÉ.

    Distinct d'une autre défaillance OCR. Avant ce lot, la boucle de page étiquetait
    `ocr_page_timeout` TOUTE exception — une image illisible était rapportée comme un
    dépassement de délai, et le diagnostic était faux par construction.
    """

    code = "ocr_page_timeout"


#: Marge du filet de second niveau. `run_with_timeout` ne doit JAMAIS se déclencher sur
#: un dépassement Tesseract normal : son budget dépasse strictement celui passé à
#: pytesseract, qui est désormais la protection principale. Deux timeouts réglés à la
#: même valeur produiraient une course dont l'issue déciderait du code d'erreur.
_FILET_MARGE_S = 5


def _est_timeout_tesseract(exc: BaseException) -> bool:
    """`pytesseract` signale l'expiration par ``RuntimeError('Tesseract process timeout')``.

    C'est le SEUL ``RuntimeError`` que la bibliothèque lève (vérifié sur 0.3.13) : le
    couplage au message reste donc sans ambiguïté, et le test de type suffirait presque.
    On teste les deux pour ne pas dépendre d'une formulation.
    """
    return isinstance(exc, RuntimeError) and "timeout" in str(exc).lower()


#: Compteurs d'exploitation publiés par `/ops/metrics`. Entiers, SANS dimension libre
#: (jamais de numéro de page ni d'identifiant de document) : une série temporelle dont les
#: étiquettes sont ouvertes explose en cardinalité. Le détail par page vit dans les
#: métadonnées du document, qui est le bon endroit pour une donnée à haute cardinalité.
OCR_COUNTER_NAMES = (
    "ocr_pages_completed_total",
    "ocr_page_timeout_total",
    "ocr_page_failed_total",
)


def _compter(nom: str) -> None:
    """Compteur d'exploitation entier. Ne lève jamais (contrat d'`ops_counters`)."""
    try:
        from apps.rag_core.services import ops_counters

        ops_counters.incr(nom)
    except Exception:  # noqa: BLE001 - l'observabilité ne fait jamais échouer l'OCR
        pass


def _budget_restant(deadline: Optional[float], etape: str) -> float:
    """Reliquat du budget de PAGE avant un sous-appel Tesseract.

    Le budget est TOTAL pour la page : OSD, texte et confiance se le partagent. Chaque
    sous-appel ne reçoit que ce qui reste, jamais le budget entier — sinon
    ``DOCUMENT_OCR_TIMEOUT_SECONDS=60`` signifierait 120 ou 180 s selon le mode.

    ``0.0`` = aucun timeout côté pytesseract (contrat de la bibliothèque), utilisé quand
    aucun budget n'est fourni. Horloge MONOTONE : un ajustement de l'heure système ne doit
    pas allonger ni raccourcir un budget en cours.
    """
    if deadline is None:
        return 0.0
    reste = deadline - monotonic()
    if reste <= 0:
        raise OcrPageTimeout(
            f"Budget de page épuisé avant {etape} : aucun appel Tesseract n'est lancé.",
            code="ocr_page_timeout",
        )
    return reste


@dataclass
class OcrPage:
    page_number: int
    text: str
    confidence: Optional[float] = None
    rotation: int = 0  # degrés appliqués pour redresser la page (0/90/180/270)


@dataclass
class OcrResult:
    pages: List[OcrPage] = field(default_factory=list)
    used: bool = False
    partial: bool = False
    warnings: List[str] = field(default_factory=list)
    #: Numéros des pages dont le budget a expiré, et de celles ayant échoué autrement.
    #: Un warning est dédupliqué par le contrat d'`add_warning` : sans ces listes, un
    #: document dont 12 pages expirent serait indiscernable d'un document où une seule
    #: a expiré.
    timeout_pages: List[int] = field(default_factory=list)
    failed_pages: List[int] = field(default_factory=list)

    @property
    def avg_confidence(self) -> Optional[float]:
        confs = [p.confidence for p in self.pages if p.confidence is not None]
        if not confs:
            return None
        return round(sum(confs) / len(confs), 1)

    @property
    def text(self) -> str:
        return "\n\n".join(p.text.strip() for p in self.pages if p.text.strip()).strip()


# ---------------------------------------------------------------------
# Provider local : Tesseract (import-gardé)
# ---------------------------------------------------------------------
class _TesseractProvider:
    name = "tesseract"
    external = False

    def _require(self):
        try:
            import pytesseract  # type: ignore
            from PIL import Image  # type: ignore
        except Exception as e:  # pragma: no cover - dépend de l'env
            raise OcrUnavailable(
                "Moteur OCR local (pytesseract/Pillow) indisponible. "
                "Installez l'extra 'ocr' et le binaire tesseract.",
                code="ocr_engine_missing",
            ) from e
        return pytesseract, Image

    def _maybe_rotate(self, im, pytesseract, deadline: Optional[float] = None):
        """Redresse l'image via OSD tesseract si une rotation 90/180/270 est
        détectée AVEC une confiance suffisante. Best-effort : toute erreur OSD =>
        aucune rotation. Renvoie ``(image, rotation_appliquée)``.

        La confiance est déterminante. Sur une page dense en tableaux (peu de prose),
        l'OSD tesseract se prononce avec une confiance très basse et se trompe : il a
        retourné à 180° des pages parfaitement droites d'un tableur imprimé, changeant
        un texte lisible en charabia indexé. Une orientation incertaine n'est pas une
        information : dans le doute, ne rien faire est strictement meilleur, car une page
        déjà droite est le cas le plus fréquent.
        """
        if not config.ocr_auto_rotate():
            return im, 0
        try:
            osd = pytesseract.image_to_osd(
                im, output_type=pytesseract.Output.DICT,
                timeout=_budget_restant(deadline, "OSD"),
            )
            rot = int(osd.get("rotate", 0)) % 360
            conf = float(osd.get("orientation_conf", 0.0) or 0.0)
        except OcrPageTimeout:
            # Un dépassement de budget n'est PAS une orientation incertaine : il doit
            # remonter, sinon la page continuerait à consommer le budget déjà épuisé.
            raise
        except Exception as e:
            if _est_timeout_tesseract(e):
                raise OcrPageTimeout(
                    "Budget de page épuisé pendant l'OSD : processus Tesseract terminé.",
                    code="ocr_page_timeout",
                ) from e
            return im, 0
        if conf < config.ocr_min_orientation_confidence():
            return im, 0
        if rot in (90, 180, 270):
            try:
                # OSD 'rotate' = degrés horaires pour redresser ; PIL.rotate est
                # anti-horaire => -rot. expand=True pour ne rien tronquer.
                return im.rotate(-rot, expand=True), rot
            except Exception:
                return im, 0
        return im, 0

    def ocr_image_bytes(
        self, raw: bytes, *, languages: str, budget_s: Optional[float] = None
    ) -> Tuple[str, Optional[float], int]:
        """OCR d'une image, avec un budget TOTAL et réellement annulable.

        ``budget_s`` couvre l'ENSEMBLE des appels Tesseract de la page (OSD, texte et,
        en mode confiance ON, `image_to_data`). Chaque sous-appel ne reçoit que le
        reliquat. ``None`` = aucun budget, comportement historique.

        L'annulation est celle de `pytesseract` : sur expiration il fait `terminate()`
        puis `kill()` du processus Tesseract. C'est ce qui manquait au mécanisme
        précédent, qui cessait d'attendre sans rien arrêter — mesuré : 24 s de CPU
        consommées après l'abandon, et un processus vivant 18 s au-delà du budget.
        """
        pytesseract, Image = self._require()
        import io as _io

        deadline = (monotonic() + budget_s) if budget_s and budget_s > 0 else None
        with Image.open(_io.BytesIO(raw)) as im:
            im.load()
            im, rotation = self._maybe_rotate(im, pytesseract, deadline=deadline)
            try:
                text = pytesseract.image_to_string(
                    im, lang=languages, timeout=_budget_restant(deadline, "image_to_string"),
                )
            except OcrPageTimeout:
                raise
            except Exception as e:
                if _est_timeout_tesseract(e):
                    raise OcrPageTimeout(
                        "Budget de page épuisé pendant la reconnaissance du texte : "
                        "processus Tesseract terminé.",
                        code="ocr_page_timeout",
                    ) from e
                raise
            conf = None
            # Second passage Tesseract, OPTIONNEL : il refait la reconnaissance complète
            # pour n'en tirer que la moyenne des confiances. `text` est déjà affecté et
            # n'est jamais réécrit ici — éteindre ce bloc ne peut donc pas modifier le
            # texte indexé. Le texte ne doit JAMAIS être reconstruit depuis ce TSV :
            # mesuré, la reconstruction diffère sur 7 pages sur 7 (lignes vides écrasées).
            if config.ocr_compute_confidence():
                try:
                    data = pytesseract.image_to_data(
                        im, lang=languages, output_type=pytesseract.Output.DICT,
                        timeout=_budget_restant(deadline, "image_to_data"),
                    )
                    vals = [float(c) for c in data.get("conf", []) if str(c) not in ("-1", "")]
                    if vals:
                        conf = round(sum(vals) / len(vals), 1)
                except OcrPageTimeout:
                    # Le texte est DÉJÀ acquis : le budget a expiré sur la confiance
                    # seule. On ne perd pas la page pour un indicateur accessoire, mais
                    # on ne prétend pas non plus l'avoir mesuré.
                    logger.warning("ocr confidence skipped: page budget exhausted")
                    conf = None
                except Exception:
                    conf = None
        return (text or "").strip(), conf, rotation

    def image_to_words(
        self, im, *, languages: str, budget_s: Optional[float] = None
    ) -> List[dict]:
        """Mots OCR positionnés (bbox + numéros de ligne) via image_to_data.
        Utilisé par la reconstruction de tableaux OCR (P3-b). Best-effort.

        ``budget_s`` suit exactement le contrat d'``ocr_image_bytes`` : une échéance
        TOTALE partagée entre l'OSD et la reconnaissance. Ce chemin était le dernier
        appel Tesseract encore lancé sans budget opposable — sa sémantique de tableaux
        est inchangée, seule l'annulation est ajoutée.
        """
        pytesseract, _Image = self._require()
        deadline = (monotonic() + budget_s) if budget_s and budget_s > 0 else None
        im2, _rot = self._maybe_rotate(im, pytesseract, deadline=deadline)
        data = pytesseract.image_to_data(
            im2, lang=languages, output_type=pytesseract.Output.DICT,
            timeout=_budget_restant(deadline, "image_to_words"),
        )
        n = len(data.get("text", []))

        def _col(name):
            return data.get(name, [0] * n)

        words: List[dict] = []
        for i in range(n):
            txt = (data["text"][i] or "").strip()
            try:
                conf = float(data["conf"][i])
            except Exception:
                conf = -1.0
            if not txt or conf < 0:
                continue
            words.append({
                "text": txt,
                "left": int(_col("left")[i]), "top": int(_col("top")[i]),
                "width": int(_col("width")[i]), "height": int(_col("height")[i]),
                "block_num": int(_col("block_num")[i]), "par_num": int(_col("par_num")[i]),
                "line_num": int(_col("line_num")[i]), "conf": conf,
            })
        return words

    def rasterize_pdf(self, raw: bytes, page_indices: List[int]):
        """Rend des pages PDF en images (lazy). Lève OcrUnavailable si aucun
        rasteriseur n'est disponible (on ne télécharge rien). L'échelle/DPI est
        pilotée par DOCUMENT_OCR_RASTER_SCALE (plans -> 3–4 recommandé)."""
        scale = config.ocr_raster_scale()
        # pdf2image (poppler) si présent
        try:
            from pdf2image import convert_from_bytes  # type: ignore

            images = convert_from_bytes(raw, dpi=int(round(scale * 72)))
            return [images[i] for i in page_indices if 0 <= i < len(images)]
        except Exception:
            pass
        # pypdfium2 si présent
        try:
            import pypdfium2 as pdfium  # type: ignore

            pdf = pdfium.PdfDocument(raw)
            out = []
            for i in page_indices:
                if 0 <= i < len(pdf):
                    out.append(pdf[i].render(scale=scale).to_pil())
            return out
        except Exception:
            pass
        raise OcrUnavailable(
            "Aucun moteur de rastérisation PDF (pdf2image/pypdfium2) disponible pour l'OCR.",
            code="ocr_pdf_rasterizer_missing",
        )


def get_provider(name: Optional[str] = None):
    """Renvoie une instance de provider OCR, ou lève une erreur contrôlée.

    IMPORTANT : aucun provider externe n'est implémenté ici. Un nom inconnu lève
    `OcrUnavailable` AVANT toute I/O — interdit tout appel réseau accidentel.
    """
    name = (name or config.ocr_provider() or "").lower()
    if name in ("none", "disabled", ""):
        raise OcrDisabled("Aucun provider OCR configuré.", code="ocr_no_provider")
    if name in ("tesseract", "local"):
        return _TesseractProvider()
    # tout autre nom (ex. provider cloud) doit être implémenté explicitement
    raise OcrUnavailable(
        f"Provider OCR '{name}' non implémenté : aucun appel externe n'est effectué "
        "sans implémentation explicite et configuration dédiée.",
        code="ocr_provider_not_implemented",
    )


# ---------------------------------------------------------------------
# Orchestration OCR (timeouts par page + limite de pages)
# ---------------------------------------------------------------------
def ocr_image(
    raw: bytes,
    *,
    languages: Optional[str] = None,
    page_timeout_s: Optional[int] = None,
) -> OcrResult:
    provider = get_provider()
    languages = languages or config.ocr_languages()
    res = OcrResult(used=True)
    budget = max(1, int(page_timeout_s) if page_timeout_s else max(5, config.ocr_timeout_s()))
    try:
        text, conf, rotation = run_with_timeout(
            lambda: provider.ocr_image_bytes(raw, languages=languages, budget_s=budget),
            budget + _FILET_MARGE_S,
        )
    except OcrPageTimeout:
        res.partial = True
        res.warnings.append("ocr_page_timeout")
        res.timeout_pages.append(1)
        _compter("ocr_page_timeout_total")
        return res
    except Exception as e:
        # timeout / moteur absent → résultat partiel contrôlé
        res.partial = True
        res.warnings.append(getattr(e, "code", "ocr_failed"))
        return res
    res.pages.append(OcrPage(page_number=1, text=text, confidence=conf, rotation=rotation))
    _compter("ocr_pages_completed_total")
    return res


def ocr_pdf_pages(
    raw: bytes,
    page_numbers: List[int],
    *,
    languages: Optional[str] = None,
    page_timeout_s: Optional[int] = None,
) -> OcrResult:
    """OCR des pages PDF dont les numéros (1-based) sont fournis.

    Respecte DOCUMENT_OCR_MAX_PAGES et un timeout par page. Ne lève pas pour un
    simple timeout : renvoie un résultat partiel avec warning.
    """
    provider = get_provider()
    languages = languages or config.ocr_languages()
    res = OcrResult(used=True)

    max_pages = config.ocr_max_pages()
    selected = page_numbers[:max_pages]
    if len(page_numbers) > max_pages:
        res.partial = True
        res.warnings.append("ocr_max_pages_reached")

    indices = [p - 1 for p in selected]
    try:
        images = provider.rasterize_pdf(raw, indices)
    except OcrUnavailable as e:
        res.partial = True
        res.warnings.append(e.code)
        return res

    budget = int(page_timeout_s) if page_timeout_s else max(5, config.ocr_timeout_s())
    budget = max(1, budget)
    import io as _io

    for page_no, image in zip(selected, images):
        debut = monotonic()
        try:
            def _do():
                # passe par bytes PNG pour réutiliser ocr_image_bytes
                buf = _io.BytesIO()
                image.save(buf, format="PNG")
                return provider.ocr_image_bytes(
                    buf.getvalue(), languages=languages, budget_s=budget
                )

            # Filet de SECOND niveau : il ne doit pas se déclencher sur un dépassement
            # Tesseract normal, d'où la marge. La protection principale est le timeout
            # natif, seul capable d'arrêter réellement le processus.
            text, conf, rotation = run_with_timeout(_do, budget + _FILET_MARGE_S)
            res.pages.append(OcrPage(page_number=page_no, text=text, confidence=conf, rotation=rotation))
            _compter("ocr_pages_completed_total")
        except (OcrUnavailable, OcrDisabled) as e:
            # Moteur OCR indisponible (binaire tesseract / bindings absents) : cause
            # PRÉCISE (≠ timeout) pour un diagnostic UX correct. On arrête (inutile
            # de réessayer page par page si le moteur manque).
            res.partial = True
            res.warnings.append(getattr(e, "code", "ocr_engine_missing"))
            logger.warning("ocr engine unavailable on page %s: %s", page_no, getattr(e, "code", "?"))
            break
        except OcrPageTimeout:
            # VRAI dépassement de budget : le processus Tesseract a été terminé.
            res.partial = True
            res.warnings.append("ocr_page_timeout")
            res.timeout_pages.append(page_no)
            _compter("ocr_page_timeout_total")
            logger.warning(
                "ocr page timeout document_page=%s provider=%s budget_s=%s elapsed_s=%.1f "
                "outcome=cancelled retryable=true",
                page_no, provider.name, budget, monotonic() - debut,
            )
        except Exception as e:
            # TOUTE AUTRE défaillance OCR. La frontière est volontairement large — la
            # convention du pipeline est qu'une page perdue ne perd pas le document —
            # mais elle n'est plus étiquetée « timeout » : c'est la correction du lot.
            # Le TYPE est journalisé pour qu'un bug reste visible plutôt que déguisé.
            res.partial = True
            res.warnings.append("ocr_page_failed")
            res.failed_pages.append(page_no)
            _compter("ocr_page_failed_total")
            logger.warning(
                "ocr page failed document_page=%s provider=%s budget_s=%s elapsed_s=%.1f "
                "exception=%s code=%s outcome=skipped",
                page_no, provider.name, budget, monotonic() - debut,
                type(e).__name__, getattr(e, "code", "?"),
            )
    return res


# ---------------------------------------------------------------------
# Mots OCR positionnés (P3-b — reconstruction de tableaux). Best-effort, bornés.
# ---------------------------------------------------------------------
def ocr_words_for_pages(raw: bytes, page_numbers: List[int], *, languages: Optional[str] = None) -> dict:
    """Renvoie ``{page_1based: [mots...]}`` pour les pages données. Rastérise puis
    image_to_data. Indisponibilité / erreur -> ``{}`` (jamais de crash)."""
    try:
        provider = get_provider()
    except (OcrUnavailable, OcrDisabled):
        return {}
    languages = languages or config.ocr_languages()
    selected = list(page_numbers)[: config.ocr_max_pages()]
    indices = [p - 1 for p in selected]
    try:
        images = provider.rasterize_pdf(raw, indices)
    except Exception:
        return {}
    per_page_timeout = max(5, config.ocr_timeout_s())
    out: dict = {}
    for page_no, image in zip(selected, images):
        try:
            out[page_no] = run_with_timeout(
                lambda: provider.image_to_words(
                    image, languages=languages, budget_s=per_page_timeout
                ),
                per_page_timeout + _FILET_MARGE_S,
            )
        except Exception:
            continue
    return out


def ocr_words_for_image(raw: bytes, *, languages: Optional[str] = None) -> dict:
    """Mots OCR d'une image autonome -> ``{1: [mots...]}``. Best-effort."""
    try:
        provider = get_provider()
        pytesseract, Image = provider._require()
    except Exception:
        return {}
    languages = languages or config.ocr_languages()
    import io as _io

    try:
        with Image.open(_io.BytesIO(raw)) as im:
            im.load()
            budget = max(5, config.ocr_timeout_s())
            return {1: run_with_timeout(
                lambda: provider.image_to_words(im, languages=languages, budget_s=budget),
                budget + _FILET_MARGE_S,
            )}
    except Exception:
        return {}
