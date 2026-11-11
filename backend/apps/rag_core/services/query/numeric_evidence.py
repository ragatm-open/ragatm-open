"""
Fiabilité des preuves NUMÉRIQUES (OCR-NUMERIC-CAVEAT-1, NUMERIC-CLAIM-GUARD-2).

Défauts corrigés, mesurés en staging sur 4 documents réels :
  * e9d80f7 : 4 réponses fausses données avec assurance, toutes issues du texte OCR ordinaire
    de pages tabulaires (virgule perdue, ligne voisine, autre colonne) ;
  * 66a5282 : décision GLOBALE — un nombre de contexte (« Base aérienne 123 ») retrouvé dans un
    fragment structuré « confirmait » la réponse entière, et une valeur fausse restait affichée
    avec une simple réserve ; l'OCR d'une page non reconnue comme tabulaire n'était que réservé.

1. ``annotate_numeric_evidence`` — avant le prompt, chaque source reçoit
   ``meta.numeric_evidence`` d'après des provenances VÉRIFIABLES (marqueur structuré,
   ``source_is_ocr``, registre Structured Visual de la page). Calculé à la lecture : aucune
   migration, fragments déjà indexés couverts.
2. ``guard_answer`` — après génération, contrôle DÉTERMINISTE **par affirmation** : la réponse
   est découpée en propositions (lignes, puis phrases) ; chaque nombre affirmé reçoit un statut
   SUPPORTED / UNVERIFIED / UNSUPPORTED / CONFLICTING selon les sources qui le portent ET leur
   pertinence (même ligne et même colonne d'un tableau structuré, recoupement lexical local pour
   un texte natif). Une proposition qui contient un nombre non SUPPORTED est retirée ENTIÈRE ;
   s'il ne reste aucun nombre étayé, abstention contrôlée. Un nombre étayé ne confirme jamais un
   autre nombre. L'OCR et la lecture visuelle ne sont JAMAIS une preuve suffisante, tableau
   détecté ou non.

Périmètre : un contexte qui contient au moins une source OCR ou visuelle (``context_is_protected``).
Hors périmètre (texte natif, classeurs, corpus métier seuls), la réponse est inchangée.

Limites : le statut OCR est porté par la PAGE ; la pertinence d'un texte natif se lit par
recoupement de mots (pas de structure ligne/colonne) ; un fragment structuré accepté peut être
faux (valeur sous la mauvaise colonne) et le contrôle le tient pour fiable ; les nombres d'un
seul chiffre ne sont contrôlés que s'ils forment toute la réponse.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

META_KEY = "numeric_evidence"
REASON_KEY = "numeric_evidence_reason"

STRUCTURED_ACCEPTED = "structured_accepted"
NATIVE_TEXT = "native_text"
OCR_TABLE_UNVERIFIED = "ocr_table_unverified"
OCR_OTHER = "ocr_other"
UNKNOWN = "unknown"

#: Statuts dont un nombre n'est JAMAIS une preuve suffisante à lui seul.
#: Tableau de PDF natif structuré par le rail générique (lignes, colonnes, en-têtes prouvés).
NATIVE_TABLE_STRUCTURED = "native_table_structured"
#: Texte natif d'une page où une grille a été détectée SANS être structurée (tableau aplati).
NATIVE_TABLE_UNVERIFIED = "native_table_unverified"

#: Sortie structurée du Gateway (StructX) NON probante — moteur non qualifié, valeur non certifiée (`evidence`),
#: ou anomalie de cohérence : recherchable et citable, jamais une preuve chiffrée (22B, 23A). Raison détaillée dans
#: `numeric_evidence_reason`.
STRUCTURED_UNQUALIFIED = "structured_unqualified"

#: Transcription AUTOMATIQUE d'un enregistrement audio : recherchable et
#: citable avec sa plage temporelle, JAMAIS une preuve chiffrée (un nombre peut avoir été
#: mal entendu). Toujours NON VÉRIFIÉE, quelle que soit la qualité apparente du texte.
ASR_TRANSCRIPT = "asr_transcript"

UNVERIFIED = frozenset({OCR_TABLE_UNVERIFIED, OCR_OTHER, NATIVE_TABLE_UNVERIFIED, STRUCTURED_UNQUALIFIED,
                        ASR_TRANSCRIPT})

#: Extensions audio : repli FAIL-CLOSED quand la provenance ASR d'un chunk est perdue.
_AUDIO_SUFFIXES = (".mp3", ".m4a", ".wav", ".flac", ".ogg", ".webm")
#: Statuts dont la provenance n'est pas une lecture d'image (le contenu peut rester faux).
RELIABLE = frozenset({STRUCTURED_ACCEPTED, NATIVE_TABLE_STRUCTURED, NATIVE_TEXT, UNKNOWN})
#: Sources dont la preuve chiffrée exige la MÊME ligne et la MÊME colonne (pas un mot commun).
STRUCTURED = frozenset({STRUCTURED_ACCEPTED, NATIVE_TABLE_STRUCTURED})

VERDICT_PASS = "pass"
VERDICT_REDACT = "redact"
VERDICT_BLOCK = "block"
ANSWER_KIND_BLOCKED = "numeric_evidence_unverified"


# --------------------------------------------------------------------------- #
# Classification des sources
# --------------------------------------------------------------------------- #
def _source_of(chunk: dict) -> dict:
    src = chunk.get("source")
    return src if isinstance(src, dict) else {}


def _meta_of(chunk: dict) -> dict:
    meta = _source_of(chunk).get("meta")
    if not isinstance(meta, dict):
        meta = chunk.get("meta")
    return meta if isinstance(meta, dict) else {}


def _is_audio_source(chunk: dict) -> bool:
    src = _source_of(chunk)
    name = str(src.get("filename") or chunk.get("filename") or src.get("label") or "").strip().lower()
    ctype = str(src.get("content_type") or chunk.get("content_type") or "").strip().lower()
    return ctype.startswith("audio/") or name.endswith(_AUDIO_SUFFIXES)


def _document_id(chunk: dict) -> Optional[str]:
    doc = _source_of(chunk).get("document_id") or chunk.get("document_id")
    return str(doc) if doc else None


def _page(chunk: dict) -> Optional[int]:
    page = _source_of(chunk).get("page_number") or chunk.get("page_number")
    try:
        return int(page) if page not in (None, "") else None
    except (TypeError, ValueError):
        return None


def table_pages_from_meta(doc_meta: Any) -> dict:
    """``{page: raison}`` des pages où le registre Structured Visual porte un TABLEAU.

    Sources : classification automatique (``auto.assets[*].class == "TABLE"``) et
    enregistrements d'extraction ``<empreinte>:table``. Raison = ``table_<statut>``
    (published, refused, failed, skipped…). Aucun registre : ``{}``."""
    from apps.rag_core.services.ingest.visual.structured import DOCUMENT_META_KEY

    reg = doc_meta.get(DOCUMENT_META_KEY) if isinstance(doc_meta, dict) else None
    if not isinstance(reg, dict):
        return {}
    pages: dict = {}
    auto = reg.get("auto") if isinstance(reg.get("auto"), dict) else {}
    entries = [e for e in (auto.get("assets") or {}).values() if isinstance(e, dict) and e.get("class") == "TABLE"]
    entries += [r for k, r in reg.items() if k != "auto" and str(k).endswith(":table") and isinstance(r, dict)]
    for e in entries:
        page = e.get("source_page")
        if isinstance(page, int) and not isinstance(page, bool):
            pages.setdefault(page, f"table_{e.get('status') or 'unknown'}")
    return pages


def classify(chunk: dict, table_pages: dict) -> tuple:
    """(statut, raison) d'une source, d'après sa seule provenance déclarée."""
    from apps.rag_core.services.ingest.visual.structured import is_structured_meta, structured_evidence_decision

    meta = _meta_of(chunk)
    # AVANT toute autre règle : une transcription ASR n'est jamais
    # « fiable ». Provenance déclarée, OU repli fail-closed sur la nature AUDIO du document
    # source (méta perdue au chunking, au retrieval ou à l'assemblage du contexte) : jamais
    # de retombée silencieuse sur `unknown`, compté fiable.
    if meta.get("source_type") == "AUDIO_ASR_TRANSCRIPT" or meta.get("source_is_asr") is True:
        return ASR_TRANSCRIPT, "audio_asr_transcript"
    if _is_audio_source(chunk):
        return ASR_TRANSCRIPT, "audio_document_provenance_missing"
    page = _page(chunk)
    on_table_page = page is not None and page in table_pages
    if is_structured_meta(meta):
        # Une cellule alignée par un moteur expérimental n'est pas une relation prouvée : seule une provenance
        # déclarée qualifiée rend la sortie probante (ligne + colonne, règle stricte ci-dessous).
        # Contrat 22A.1 : qualification du moteur ≠ certification de la valeur.
        eligible, reason = structured_evidence_decision(meta)
        return (STRUCTURED_ACCEPTED if eligible else STRUCTURED_UNQUALIFIED), reason
    if isinstance(meta.get("visual"), dict):
        # Lecture d'image par le modèle de vision (« Visible text ») : jamais vérifiée.
        if on_table_page:
            return OCR_TABLE_UNVERIFIED, f"visual_description_{table_pages[page]}"
        return OCR_OTHER, "visual_description"
    if meta.get("kind") == "table":
        # Rail tabulaire (générique ou spécialisé) : chaque valeur porte son en-tête de colonne.
        table = meta.get("table") if isinstance(meta.get("table"), dict) else {}
        if meta.get("source_is_ocr") is True or table.get("source") == "ocr":
            return OCR_TABLE_UNVERIFIED, "ocr_table_rail"
        return NATIVE_TABLE_STRUCTURED, "native_table_rail"
    if meta.get("native_table_unverified") is True:
        return NATIVE_TABLE_UNVERIFIED, "native_page_with_unstructured_table"
    if meta.get("source_is_ocr") is True:
        if on_table_page:
            return OCR_TABLE_UNVERIFIED, f"ocr_page_{table_pages[page]}"
        return OCR_OTHER, "ocr_page_without_table_evidence"
    if meta.get("source_is_ocr") is False:
        return NATIVE_TEXT, "text_layer"
    return UNKNOWN, "provenance_not_declared"


def annotate_numeric_evidence(selected: list, *, table_pages_by_doc: Optional[dict] = None) -> list:
    """Copie de ``selected`` où chaque source porte ``meta.numeric_evidence`` (+ raison).

    Une seule requête : le registre des documents qui ont une source OCR ou visuelle dans le
    contexte. ``table_pages_by_doc`` (tests) remplace cette requête. Jamais d'exception : en
    cas d'échec de lecture, les sources OCR restent ``ocr_other`` (prudent, jamais « fiable »)."""
    chunks = [c for c in selected or [] if isinstance(c, dict)]
    if table_pages_by_doc is None:
        table_pages_by_doc = {}
        wanted = {_document_id(c) for c in chunks
                  if _document_id(c) and (_meta_of(c).get("source_is_ocr") is True
                                          or isinstance(_meta_of(c).get("visual"), dict))}
        if wanted:
            try:
                from apps.rag_core.models import Document

                for doc_id, meta in Document.objects.filter(id__in=wanted).values_list("id", "meta"):
                    table_pages_by_doc[str(doc_id)] = table_pages_from_meta(meta)
            except Exception:  # lecture best-effort : un échec ne rend aucune source « fiable »
                table_pages_by_doc = {}
    out = []
    for c in selected or []:
        if not isinstance(c, dict):
            out.append(c)
            continue
        status, reason = classify(c, table_pages_by_doc.get(_document_id(c) or "", {}))
        cc = dict(c)
        src = dict(_source_of(c))
        meta = dict(_meta_of(c))
        meta[META_KEY], meta[REASON_KEY] = status, reason
        if status in (OCR_TABLE_UNVERIFIED, STRUCTURED_UNQUALIFIED):
            meta["requires_visual_check"] = True
        if src or "source" in c:
            src["meta"] = meta
            cc["source"] = src
        else:
            cc["meta"] = meta
        out.append(cc)
    return out


def status_of(chunk: dict) -> str:
    return _meta_of(chunk).get(META_KEY) or UNKNOWN


# --------------------------------------------------------------------------- #
# Nombres affirmés
# --------------------------------------------------------------------------- #
_NUMBER_RE = re.compile(
    r"(?<![\w.,/-])[+\-−]?(?:\d{1,3}(?:[   ]\d{3})+|\d+)(?:[.,]\d+)?(?:\s?%)?(?![\w/]|[.,]\d)")
_CITATION_RE = re.compile(r"\[\d+(?:\s*[-–,à]\s*\d+)*\]")
_DATE_RE = re.compile(r"\b\d{1,4}[/.-]\d{1,2}[/.-]\d{1,4}\b")
#: Repère TEMPOREL d'un enregistrement (`00:12:35`) : une position dans
#: l'audio, jamais une valeur. Seule la forme à TROIS groupes est concernée ; « 14:30 »
#: reste un nombre contrôlé comme avant.
_TIMECODE_RE = re.compile(r"(?<![\d:])\d{1,3}:[0-5]\d:[0-5]\d(?![\d:])")
#: Repères de lecture, jamais des valeurs : page, section, lot, article, numéro…
_REFERENCE_BEFORE_RE = re.compile(
    r"(?:\bp\.|\bpages?|\bsections?|\bchapitres?|\barticles?|\blots?|\bannexes?|§|\bn°|\bno\.?|\bétape|\bpoint)\s*$",
    re.IGNORECASE)
_LIST_MARKER_RE = re.compile(r"^\s*\d+[.)]\s")
_YEAR_RE = re.compile(r"^(?:19|20)\d\d$")
_UNIT_RE = re.compile(r"^\s?(%|‰|€|k€|M€|kWh|MWh|kW|MW|W|m²|m2|m³|m3|ml|m|km|kg|t|h|°C|A|V|kVA)(?![\w²³])")
#: Fin de phrase : ponctuation précédée de deux caractères non blancs (« p. 3 » ne coupe pas),
#: suivie d'une majuscule, d'un guillemet ou d'une puce. Un décimal n'a pas d'espace : jamais coupé.
#: NATIVE-TABLE-RESPONSE-HARDENING-4 : la ponctuation peut aussi suivre OU précéder un caractère
#: fermant (« … 10 ». ; « …). » ; « …." ») — constat réel : deux phrases fusionnées, la valeur
#: prouvée de la seconde retirée avec le nombre non prouvé de la première.
_CLOSERS = '»”’")\\]'
_SENTENCE_SPLIT_RE = re.compile(
    r"(?:(?<=[^\s.]{2}[.!?;])|(?<=[" + _CLOSERS + r"][.!?;])|(?<=[^\s.][.!?][" + _CLOSERS + r"])"
    r"|(?<=[^\s.][.!?][\s\u202f\u00a0][»”]))"
    r"\s+(?=[A-ZÀ-ÝÉÈ«\"(*\-])")
_WORD_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset("""
les des une dans pour par sur avec sans aux est sont que qui quoi dont cette ces ses son leur leurs
pas plus moins tres entre comme mais donc car ainsi elle elles ils lui nous vous tout tous toute
valeur valeurs ligne lignes colonne colonnes tableau tableaux document documents source sources
page pages selon indique indiquee mentionne mentionnee figure correspond donne the and for with
""".split())


def _decimal(token: str) -> Optional[Decimal]:
    t = re.sub(r"[\s  %]", "", token).replace("−", "-")
    if t.count(",") == 1 and "." not in t:
        t = t.replace(",", ".")
    elif t.count(",") >= 1:
        return None
    try:
        return Decimal(t)
    except InvalidOperation:
        return None


def _digits(value: Decimal) -> str:
    """Signature de chiffres, sans signe ni séparateur : 19,6 / 196 / 1,96 -> « 196 »."""
    return re.sub(r"\D", "", format(abs(value), "f")).lstrip("0") or "0"


def numbers_in(text: str) -> set:
    """Valeurs numériques d'un texte (groupes de milliers fusionnés ET séparés). Signe conservé."""
    out = set()
    for m in _NUMBER_RE.finditer(text or ""):
        tok = m.group(0)
        for part in {tok, *tok.split()}:
            value = _decimal(part)
            if value is not None:
                out.add(value)
    return out


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text or "").lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def _tokens(text: str) -> set:
    """Mots significatifs (pliés, >= 3 caractères, au moins une lettre, hors mots vides)."""
    return {w for w in _WORD_RE.findall(_fold(text))
            if len(w) >= 3 and re.search(r"[a-z]", w) and w not in _STOPWORDS}


@dataclass(frozen=True)
class NumericClaim:
    token: str
    value: Decimal
    #: Unité de tête (règle historique) : sert au choix de la colonne, jamais à la certifier.
    unit: Optional[str] = None
    #: Unité AFFICHÉE en entier (« kg eq.CO2/m² »), gras Markdown toléré : objet de la certification.
    unit_phrase: Optional[str] = None


# --------------------------------------------------------------------------- #
# Unités : certification séparée de la valeur (PRODUCTION-BLOCKERS-14)
#
# Constat réel (FINAL-QUALIFICATION-13) : « **15,38** m² » publié sept fois alors qu'aucune source ne porte
# d'unité ; le gras masquait l'unité et `_units_compatible` acceptait une source sans unité. Une valeur
# prouvée ne prouve pas son unité : l'unité affichée doit être portée par la preuve de CETTE valeur
# (cellule, en-tête de sa colonne, unité du libellé de ligne, colonne d'unité d'une colonne de quantité,
# ligne de texte qui la porte). La question n'est jamais une preuve. Sinon l'unité est retirée, la valeur
# conservée.
_UNIT_BASE = r"(?:%|‰|€|k€|M€|kWh|MWh|kVA|kW|MW|W|m²|m2|m³|m3|ml|km|kg|m|t|h|°C|A|V)"
_UNIT_PHRASE = (_UNIT_BASE + r"(?:[  ]?(?:[eé]q\.?[  ]?)?CO2(?:e|eq)?)?"
                + r"(?:[  ]?/[  ]?(?:" + _UNIT_BASE + r"|[^\W\d_]{1,12}))?")
_UNIT_PHRASE_AFTER_RE = re.compile(r"^((?:\*{1,2}|_{1,2})?)([   ]?)(" + _UNIT_PHRASE + r")(?![\w²³])")
_UNIT_PHRASE_FULL_RE = re.compile(r"^\s*(" + _UNIT_PHRASE + r")\s*$")
_TRAILING_PAREN_RE = re.compile(r"\(([^()]{1,30})\)\s*$")
#: Colonne d'unité d'une ligne (« UF: m² ») : elle qualifie la seule colonne de QUANTITÉ de la ligne.
_UNIT_COLUMN_HEADERS = frozenset({"uf", "u", "unite", "unites", "unit", "units", "unite fonctionnelle"})
_QUANTITY_HEADER_WORDS = frozenset({"qte", "quantite", "quantites", "qty", "quantity", "quantities"})

UNIT_CERTIFIED = "unit_certified"
UNIT_ABSENT_FROM_SOURCE = "unit_absent_from_source"
UNIT_AMBIGUOUS = "unit_ambiguous"
UNIT_CONTRADICTED = "unit_contradicted"


def _norm_unit(unit: Optional[str]) -> str:
    """Forme de comparaison : « m2 » = « m² », casse, accents, espaces et points repliés ; notations
    d'équivalent CO2 confondues (« kg eq.CO2/m² » = « kgCO2/m² » = « kg CO2e/m² »)."""
    folded = re.sub(r"[\s.  ]", "", _fold(unicodedata.normalize("NFKC", str(unit or ""))))
    return re.sub(r"(?:eq)?co2(?:eq|e)?", "co2", folded)


def _unit_phrase_after(text: str, end: int) -> Optional[str]:
    m = _UNIT_PHRASE_AFTER_RE.match(text[end:])
    return m.group(3) if m else None


def _full_unit(text: str) -> Optional[str]:
    m = _UNIT_PHRASE_FULL_RE.match(text or "")
    return m.group(1) if m else None


def _unit_phrases_after(text: str, value: Decimal) -> set:
    """Unités écrites juste après CETTE valeur dans le texte (« 12,5 % », « 900 kg eq.CO2/m² »)."""
    out = set()
    for m in _NUMBER_RE.finditer(text or ""):
        if _decimal(m.group(0)) != value:
            continue
        if m.group(0).strip().endswith("%"):
            out.add("%")
        else:
            phrase = _unit_phrase_after(text, m.end())
            if phrase:
                out.add(phrase)
    return out


def _header_unit_phrase(header: str) -> Optional[str]:
    """Unité déclarée par un en-tête ou un libellé : parenthèse finale (« SDP (m²) »), en-tête entier
    (« kg CO2/m² ») ou dernier mot de plus d'un caractère (« SU m² ») — jamais une lettre isolée
    (« Pompe A » n'est pas en ampères)."""
    header = (header or "").strip()
    paren = _TRAILING_PAREN_RE.search(header)
    if paren:
        return _full_unit(paren.group(1))
    whole = _full_unit(header)
    if whole:
        return whole
    last = header.split()[-1] if header.split() else ""
    return _full_unit(last) if len(last) > 1 and len(header.split()) > 1 else None


def _row_units(label: str, cells: list, header: str) -> set:
    """Unités portées par la LIGNE pour la valeur de la colonne ``header`` : unité du libellé
    (« Eges PCE (kg eq.CO2/m²) ») ; colonne d'unité (« UF: m² ») si ``header`` est une colonne de quantité."""
    out = set()
    paren = _TRAILING_PAREN_RE.search((label or "").strip())
    if paren and _full_unit(paren.group(1)):
        out.add(_full_unit(paren.group(1)))
    if _all_words(header) & _QUANTITY_HEADER_WORDS:
        for h, cell in cells:
            if " ".join(_WORD_RE.findall(_fold(h))) in _UNIT_COLUMN_HEADERS and _full_unit(cell):
                out.add(_full_unit(cell))
    return out


def unit_status(claim: NumericClaim, evidence: list) -> Optional[str]:
    """Statut de l'unité AFFICHÉE d'une valeur prouvée, d'après les unités portées par ses preuves
    (``evidence`` : un ensemble par preuve). ``None`` : aucune unité affichée."""
    if not claim.unit_phrase:
        return None
    found = {_norm_unit(u) for units in evidence for u in units}
    if not found:
        return UNIT_ABSENT_FROM_SOURCE
    if found == {_norm_unit(claim.unit_phrase)}:
        return UNIT_CERTIFIED
    return UNIT_AMBIGUOUS if _norm_unit(claim.unit_phrase) in found else UNIT_CONTRADICTED


def _strip_unit(sentence: str, claim: NumericClaim) -> str:
    """Retire l'unité affichée après CETTE valeur (gras conservé : « **15,38** m² » -> « **15,38** »)."""
    target = _norm_unit(claim.unit_phrase)
    out, last = [], 0
    for m in _NUMBER_RE.finditer(sentence):
        tok = m.group(0)
        if _decimal(tok.strip()) != claim.value:
            continue
        if tok.rstrip().endswith("%") and target == "%":
            cut = re.search(r"[   ]?%\s*$", tok)
            out.append(sentence[last:m.start() + cut.start()])
            last = m.end()
            continue
        after = _UNIT_PHRASE_AFTER_RE.match(sentence[m.end():])
        if after and _norm_unit(after.group(3)) == target:
            out.append(sentence[last:m.end()] + after.group(1))
            last = m.end() + after.end()
    out.append(sentence[last:])
    return "".join(out)


def _claims_in(text: str, asked: set, *, lone: bool = False) -> list:
    line = _TIMECODE_RE.sub(" ", _DATE_RE.sub(" ", _CITATION_RE.sub(" ", text)))
    if _LIST_MARKER_RE.match(line):
        line = _LIST_MARKER_RE.sub(" ", line, count=1)
    claims, seen = [], set()
    for m in _NUMBER_RE.finditer(line):
        tok = m.group(0).strip()
        digits = re.sub(r"\D", "", tok)
        if (len(digits) < 2 and not lone) or _YEAR_RE.match(tok):
            continue
        if _REFERENCE_BEFORE_RE.search(line[:m.start()]):
            continue
        value = _decimal(tok)
        if value is None or value in asked or abs(value) in asked or value in seen:
            continue
        seen.add(value)
        unit = _UNIT_RE.match(line[m.end():])
        claims.append(NumericClaim(token=tok, value=value,
                                   unit="%" if tok.endswith("%") else (unit.group(1) if unit else None),
                                   unit_phrase="%" if tok.endswith("%") else _unit_phrase_after(line, m.end())))
    return claims


def numeric_claims(answer: str, question: str = "") -> list:
    """Nombres AFFIRMÉS par la réponse : hors citations [n], dates, années, marqueurs de liste,
    repères (p. 5, lot 06, section 5…), nombres d'un seul chiffre (sauf réponse réduite à un
    nombre) et nombres repris de la question. Le signe et les décimales COMPTENT."""
    asked = numbers_in(question)
    body = _CITATION_RE.sub(" ", answer or "").strip(" \n.:;")
    lone = bool(_NUMBER_RE.fullmatch(body.strip()))
    out, seen = [], set()
    for line in (answer or "").split("\n"):
        for c in _claims_in(line, asked, lone=lone):
            if c.value not in seen:
                seen.add(c.value)
                out.append(c)
    return out


# --------------------------------------------------------------------------- #
# Pertinence d'une preuve
# --------------------------------------------------------------------------- #
_BLOCK_RE = re.compile(r"^([^\n:]{1,120}):\n([^\n]*)$", re.M)


def _structured_rows(text: str) -> list:
    """Lignes d'un fragment structuré : ``[(libellé, [(en-tête, cellule), …]), …]``.

    Fragment de LIGNE (« En-tête:\\nvaleur » par bloc) : le libellé est la première valeur
    porteuse d'une lettre. Fragment d'ENSEMBLE (« a | b | c ») : la première rangée donne les
    en-têtes, chaque rangée suivante est une ligne, son libellé est la première cellule
    porteuse d'une lettre. Rien d'autre n'est deviné."""
    lines = [ln for ln in (text or "").split("\n") if " | " in ln]
    if len(lines) < 2:
        blocks = [(h.strip(), v.strip()) for h, v in _BLOCK_RE.findall(text or "") if h.strip() != "Context"]
        if not blocks:
            return []
        label = next((v for _, v in blocks if re.search(r"[^\W\d_]", v)), "")
        return [(label, blocks)]
    header = [c.strip() for c in lines[0].split("|")]
    rows = []
    for ln in lines[1:]:
        cells = [c.strip() for c in ln.split("|")]
        label = next((c for c in cells if re.search(r"[^\W\d_]", c)), "")
        rows.append((label, [(header[k] if k < len(header) else "", c) for k, c in enumerate(cells)]))
    return rows


def _unit_after(text: str, value: Decimal) -> set:
    units = set()
    for m in _NUMBER_RE.finditer(text or ""):
        if _decimal(m.group(0)) == value:
            u = _UNIT_RE.match(text[m.end():])
            if m.group(0).strip().endswith("%"):
                units.add("%")
            elif u:
                units.add(u.group(1))
    return units


_HEADER_UNIT_RE = re.compile(r"\(([^()]{1,24})\)\s*$|(?:^|\s)(%|€|m²|m2|m³|m3|kWh|MWh|kW|MW|W|kg|A|V)\s*$")
_UNIT_ALIASES = {"m2": "m²", "m3": "m³"}


def _header_unit(header: str) -> Optional[str]:
    """Unité déclarée par l'en-tête de colonne (« P installée (W) » -> « W »), sinon ``None``."""
    m = _HEADER_UNIT_RE.search((header or "").strip())
    if not m:
        return None
    raw = (m.group(1) or m.group(2) or "").strip()
    u = _UNIT_RE.match(raw)
    return u.group(1) if u and len(u.group(0).strip()) == len(raw.split("/")[0].strip()) else None


def _units_compatible(claim_unit: Optional[str], source_unit: Optional[str]) -> bool:
    """CHOIX DE COLONNE seulement : unités absentes d'un côté, non décidable, accepté ; présentes des deux :
    identiques. Ce n'est jamais une certification de l'unité affichée (voir ``unit_status``)."""
    if not claim_unit or not source_unit:
        return True
    return _UNIT_ALIASES.get(claim_unit, claim_unit) == _UNIT_ALIASES.get(source_unit, source_unit)


_LABEL_STOPWORDS = frozenset("le la les l de du des d un une et en au aux a à".split())


def _all_words(text: str) -> set:
    """TOUS les jetons (lettres seules, chiffres, « P1 », « 1er »), hors articles, sans borne."""
    return {w for w in _WORD_RE.findall(_fold(text)) if w not in _LABEL_STOPWORDS}


def _label_tokens(text: str) -> set:
    """Jetons d'un LIBELLÉ de ligne : « Pompe A » et « Pompe B » ne se confondent pas
    (NATIVE-TABLE-GUARD-1). Seul le libellé est borné (8 premiers jetons), jamais le contexte."""
    words = [w for w in _WORD_RE.findall(_fold(text)) if w not in _LABEL_STOPWORDS]
    return set(words[:8])


#: Sans ligne désignée, une ligne tabulaire qui porte au moins ce nombre de valeurs ne prouve rien.
TABULAR_LINE_MIN_NUMBERS = 3
#: Ligne désignée par la question : « pour la ligne « X » », « la rangée « X » », « la catégorie « X » ».
_QUESTION_ROW_RE = re.compile(r"(?i)\b(?:lignes?|rangées?|libellés?|catégories?|rows?)\b\s*«\s*([^«»\n]{2,200}?)\s*»")


def _question_row_labels(question: str) -> list:
    return [m.group(1) for m in _QUESTION_ROW_RE.finditer(question or "")]


def _tabular_line(line: str) -> int:
    """Nombre de valeurs d'une ligne d'allure TABULAIRE (libellé court + valeurs : « Local CTA 1 38,74 38,74 »),
    0 pour une phrase. Prudence : une ligne courte est tabulaire — seule une phrase où les mots
    dominent nettement (plus de deux par valeur, au moins cinq) est narrative."""
    nums = [tok for tok in line.split() if re.search(r"\d", tok)]
    words = re.findall(r"[^\W\d_]{2,}", line)
    return len(nums) if nums and len(words) <= max(2 * len(nums), 4) else 0


_DECIMAL_TOKEN_RE = re.compile(r"\d[.,]\d")


def _unambiguous_in_line(line: str, value: Decimal) -> bool:
    """Ligne de tableau APLATIE (aucune colonne prouvée) : une valeur DÉCIMALE n'est prouvée que si elle est la
    SEULE valeur décimale distincte de la ligne — sinon la colonne est indéterminable. Les entiers gardent la
    règle antérieure (un groupe de milliers « 4 250 » n'est pas séparable des comptes voisins).
    « Local technique CFA 1 9,03 9,03 » prouve 9,03 ; « 3 Local ménage 4 3,85 15,38 » ne prouve ni 3,85 ni
    15,38 (CONTEXT-GROUNDING-11 : la ligne devient visible au modèle, ses colonnes restent non prouvées)."""
    decimal_shape = value.as_tuple().exponent < 0
    if not decimal_shape:
        return True  # entiers (comptes, groupes de milliers « 4 250 ») : règle antérieure inchangée
    same = set()
    for tok in line.split():
        tok = tok.strip("()[]{};:")
        number = _decimal(tok)
        if number is None:
            continue
        if bool(_DECIMAL_TOKEN_RE.search(tok)) == decimal_shape:
            same.add(number)
    return same == {value}


def _line_has_row(line: str, label: str) -> bool:
    """La ligne de texte porte-t-elle le libellé de la ligne désignée ? Premier segment (« A / B ») en
    mots entiers, sans tolérance : « Local technique CFA » ≠ « Local CTA », « Lot 6 » ≠ « Lot 16 »."""
    first = next((seg for seg in re.split(r"\s+/\s+", label) if len(re.sub(r"[^\w]", "", seg)) >= 2), "")
    return bool(first.strip()) and _word_seq(first) in _word_seq(line)


def _pertinent(chunk: dict, claim: NumericClaim, context: set, context_text: tuple = (),
               evidence: Optional[set] = None) -> bool:
    """La source porte-t-elle CETTE valeur pour CE fait ? (pas seulement le même nombre)

    Structuré : la valeur doit figurer dans une ligne dont TOUS les mots du libellé (6 premiers)
    sont dans le contexte de l'affirmation (question + proposition), et sous un en-tête dont un
    mot y figure (en-tête neutre « Column N » : non vérifiable, accepté seulement si la valeur est
    le seul nombre de la ligne ; en-tête de jetons courts « SU m² » : vérifié par tous ses mots).
    Texte : la ligne qui porte la valeur partage au moins un mot significatif avec le contexte ; une
    ligne d'allure tabulaire doit en outre porter le libellé de la ligne désignée par la question
    (mots entiers), ou, sans ligne désignée, ne porter qu'une ou deux valeurs. Unités : si la
    réponse ET la source en portent une, elles doivent coïncider (choix de la colonne seulement).
    ``evidence`` (facultatif) reçoit les unités que porte la preuve retenue : la décision sur la
    valeur n'en dépend pas (PRODUCTION-BLOCKERS-14)."""
    text = str(chunk.get("text") or "")

    def proved(units: set) -> bool:
        if evidence is not None:
            evidence.update(units)
        return True
    if claim.unit:
        units = _unit_after(text, claim.value)
        if units and claim.unit not in units and not ({claim.unit, *units} <= {"m2", "m²"} or {claim.unit, *units} <= {"m3", "m³"}):
            return False
    if status_of(chunk) in STRUCTURED:
        rows = _structured_rows(text)
        if not rows:
            return False
        for label, cells in rows:
            if not any(claim.value in numbers_in(cell) for _, cell in cells):
                continue
            words = _label_tokens(label)
            if not words or not words <= _all_words(" ".join(context_text)):
                continue
            # Colonne : parmi les en-têtes de la ligne, celui de la valeur doit être l'un de ceux qui
            # correspondent LE MIEUX au contexte (jetons courts compris : « Niveau C1 » ≠ « Niveau C2 »).
            ctx_words = _all_words(" ".join(context_text))
            scores = {h: len(_all_words(h) & ctx_words) for h, _ in cells}
            best = max(scores.values(), default=0)
            for header, cell in cells:
                if claim.value in numbers_in(cell):
                    if not _units_compatible(claim.unit, _header_unit(header)):
                        continue  # « 11 000 € » n'est pas prouvé par une colonne « P (W) »
                    units = _unit_phrases_after(cell, claim.value) | _row_units(label, cells, header)
                    if _header_unit_phrase(header):
                        units.add(_header_unit_phrase(header))
                    hw = _tokens(header)
                    if re.match(r"(?i)^column \d+", header) or not _all_words(header):
                        # En-tête neutre : la colonne n'est pas vérifiable. La ligne prouvée
                        # suffit seulement si la valeur est le SEUL nombre de la ligne ; sinon
                        # n'importe quelle colonne pourrait la porter (NATIVE-TABLE-RECOVERY-2).
                        if sum(1 for _, c in cells if numbers_in(c)) == 1:
                            return proved(units)
                        continue
                    if not hw:
                        # En-tête NOMMÉ de jetons courts seulement (« SU m² », « UF ») : vérifié
                        # par tous ses mots, et il doit être parmi les mieux appariés.
                        if scores[header] and scores[header] == best:
                            return proved(units)
                        continue
                    if hw & context and scores[header] == best:
                        return proved(units)
        return False
    # Texte natif (NATIVE-TABLE-SAFETY-6) : constat réel — « Local CTA 1 38,74 38,74 » prouvait une valeur
    # demandée pour « Local technique CFA » (un seul mot commun). Une ligne d'allure TABULAIRE doit porter le
    # libellé de la ligne désignée par la question (mots entiers) ; sans ligne désignée, elle ne prouve rien
    # si elle porte plusieurs valeurs (colonne indéterminable). Une phrase garde la règle de recouvrement.
    rows = _question_row_labels(context_text[0] if context_text else "")
    for line in text.split("\n"):
        if claim.value not in numbers_in(line) or not _tokens(line) & context:
            continue
        tabular = _tabular_line(line)
        if tabular and rows:
            if any(_line_has_row(line, label) for label in rows) and _unambiguous_in_line(line, claim.value):
                return proved(_unit_phrases_after(line, claim.value))
            continue
        if tabular >= TABULAR_LINE_MIN_NUMBERS:
            continue
        return proved(_unit_phrases_after(line, claim.value))
    return False


def _named_documents(question: str, chunks: list) -> set:
    """Documents NOMMÉS par la question et présents dans le contexte (nom de fichier cité)."""
    folded = _fold(question)
    named = set()
    for c in chunks:
        name = _fold(_source_of(c).get("filename") or "")
        stem = re.sub(r"\.[a-z0-9]{2,4}$", "", name).strip()
        if len(stem) >= 6 and stem in folded:
            named.add(_document_id(c))
    return named


# --------------------------------------------------------------------------- #
# Contrôle de la réponse
# --------------------------------------------------------------------------- #
CLAIM_SUPPORTED = "supported"
CLAIM_UNVERIFIED = "unverified"
CLAIM_UNSUPPORTED = "unsupported"
CLAIM_CONFLICTING = "conflicting"


def context_is_protected(sources: list) -> bool:
    """Périmètre du contrôle : au moins une source OCR, lecture visuelle ou TABLEAU NATIF
    (structuré ou aplati, NATIVE-TABLE-GUARD-1) dans le contexte."""
    return any(isinstance(c, dict) and (status_of(c) in UNVERIFIED or status_of(c) == NATIVE_TABLE_STRUCTURED)
               for c in sources or [])


def evaluate_claim(claim: NumericClaim, *, context: set, sources: list, context_text: tuple = (),
                   unit_evidence: Optional[list] = None) -> tuple:
    """(statut, sources probantes) d'UNE affirmation numérique — jamais d'une autre.

    SUPPORTED : une source NON issue d'une lecture d'image porte la valeur exacte (signe et
    décimales compris) de façon pertinente, sans variante contradictoire pertinente.
    CONFLICTING : une source fiable et pertinente porte les mêmes chiffres avec une autre valeur
    (séparateur ou signe perdus d'un côté). UNVERIFIED : la valeur n'est portée que par de l'OCR,
    une lecture visuelle ou une source non pertinente. UNSUPPORTED : aucune source ne la porte.
    ``unit_evidence`` (facultatif) reçoit, par source probante, les unités qu'elle porte pour cette valeur."""
    exact, proof, variant = [], [], False
    for c in sources:
        nums = numbers_in(str(c.get("text") or ""))
        reliable = status_of(c) in RELIABLE
        if claim.value in nums:
            exact.append(c)
            units: set = set()
            if reliable and _pertinent(c, claim, context, context_text, evidence=units):
                proof.append(c)
                if unit_evidence is not None:
                    unit_evidence.append(units)
        if reliable and any(v != claim.value and _digits(v) == _digits(claim.value) for v in nums):
            other = NumericClaim(claim.token, next(v for v in nums if v != claim.value and _digits(v) == _digits(claim.value)))
            variant = variant or _pertinent(c, other, context, context_text)
    if variant:
        return CLAIM_CONFLICTING, []
    if proof:
        return CLAIM_SUPPORTED, proof
    return (CLAIM_UNVERIFIED if exact else CLAIM_UNSUPPORTED), []


@dataclass
class GuardDecision:
    verdict: str
    answer: str
    claims: list = field(default_factory=list)
    removed: int = 0
    consulted: list = field(default_factory=list)
    #: propositions retirées dont une affirmation prouvée et séparable a été conservée seule.
    reduced: int = 0
    #: phrases retirées : ligne attribuée à des sources dont AUCUNE ne la contient.
    attributions_removed: int = 0
    #: phrases dont les citations invalides (sources sans la ligne) ont été retirées.
    citations_dropped: int = 0
    #: phrases retirées : absence d'un élément nommé affirmée alors qu'une source de sa portée le contient.
    absences_removed: int = 0
    #: unités affichées retirées (valeur prouvée conservée) : ``[{value, unit, status}]``.
    units: list = field(default_factory=list)
    #: phrases retirées : non-quantification affirmée alors que le passage cité de la source porte le chiffre.
    quantifications_removed: int = 0
    #: phrases retirées : valeur d'une énumération « respectively » rattachée à une autre entité de l'énumération.
    list_attributions_removed: int = 0

    def counts(self) -> dict:
        out = {CLAIM_SUPPORTED: 0, CLAIM_UNVERIFIED: 0, CLAIM_UNSUPPORTED: 0, CLAIM_CONFLICTING: 0}
        for c in self.claims:
            out[c["status"]] += 1
        return out

    def to_dict(self) -> dict:
        return {"verdict": self.verdict, "claims": len(self.claims), **self.counts(),
                "propositions_removed": self.removed, "propositions_reduced": self.reduced,
                "attributions_removed": self.attributions_removed, "citations_dropped": self.citations_dropped,
                "absences_removed": self.absences_removed, "units_removed": len(self.units),
                "quantifications_removed": self.quantifications_removed,
                "list_attributions_removed": self.list_attributions_removed,
                "documents_consulted": self.consulted}


_PAGE_IN_LABEL_RE = re.compile(r"\s*(?:[-—–,]\s*)?(?:p\.|page)\s*\d+\s*$", re.IGNORECASE)


def document_label(chunk: dict) -> str:
    """Nom du document SANS repère de page (le libellé d'un visuel porte déjà « p.N »)."""
    src = _source_of(chunk)
    label = str(src.get("filename") or src.get("label") or chunk.get("filename") or "document")
    return _PAGE_IN_LABEL_RE.sub("", label).strip() or label


def _consulted(sources: list) -> list:
    out = []
    for c in sources:
        label = document_label(c)
        if label not in out:
            out.append(label)
    return out[:4]


def abstention_answer(consulted: list, language: Optional[str] = None) -> str:
    """Abstention contrôlée : AUCUN nombre, aucune page présentée comme preuve. Libellé NEUTRE quant à la
    provenance (NATIVE-TABLE-GEOMETRY-12 : « texte lu sur l'image » était affiché pour du texte PDF natif)."""
    docs = ", ".join(f"« {d} »" for d in consulted)
    if language == "en":
        return ("I cannot establish this value reliably: the available evidence does not allow the requested value "
                "to be certified."
                + (f" Documents consulted, which do not confirm it: {docs}." if docs else "")
                + " Please check the original document.")
    return ("Je ne peux pas établir cette valeur de manière fiable : les éléments disponibles ne permettent pas de "
            "certifier la valeur demandée."
            + (f" Documents consultés, qui ne la confirment pas : {docs}." if docs else "")
            + " Vérifiez le document original.")


def removal_notice(removed: int, language: Optional[str] = None) -> str:
    if language == "en":
        return (f"⚠️ {removed} statement(s) containing figures without verifiable support were removed from this answer; "
                "the requested value may not be established. Check the original document.\n\n")
    return (f"⚠️ {removed} affirmation(s) contenant des chiffres sans preuve vérifiable ont été retirées de cette "
            "réponse ; la valeur demandée peut ne pas être établie. Vérifiez le document original.\n\n")


# --------------------------------------------------------------------------- #
# Attribution explicite d'une LIGNE à une source citée (NATIVE-TABLE-RESPONSE-HARDENING-4)
# --------------------------------------------------------------------------- #
#: « la ligne « X » … [n] », « une ligne intitulée « X » », « la rangée « X » » : une phrase qui
#: désigne une ligne par son libellé ET cite des sources affirme que ces sources la contiennent.
_ROW_ATTRIBUTION_RE = re.compile(
    r"(?i)\b(?:lignes?|rangées?|libellés?|catégories?|rows?)\b([^«»\n\[\]]{0,40})«\s*([^«»\n]{3,200}?)\s*»")
#: Entre « ligne » et le guillemet, un AUTRE objet nommé (« la ligne figure dans la section « X » ») :
#: le libellé cité n'est pas celui de la ligne — constat au rejeu réel.
_OTHER_OBJECT_RE = re.compile(
    r"(?i)\b(?:sections?|lots?|documents?|fichiers?|tableaux?|colonnes?|pages?|sources?|contextes?|"
    r"rubriques?|en-têtes?|chapitres?|columns?|files?|tables?|headers?)\b")
_CITATION_RUN_RE = re.compile(r"\[\d+\](?:\s*(?:,|et|and)\s*\[\d+\])*")
_PAREN_RE = re.compile(r"\([^()]*\)")
#: Attribution NIÉE ou CONDITIONNELLE (« ne contient pas la ligne « X » [5] », « sans ligne « X » »,
#: « il manque … pour valider que la ligne « X » ») : elle n'affirme pas une présence — hors champ.
#: Cherchée dans la SEULE portée de l'attribution (début de phrase → fin du libellé ou de ses
#: citations) : « … dans [2] et [5], mais sans valeur » reste une attribution affirmative.
_NEGATION_RE = re.compile(
    r"(?i)\b(?:ne|n['’])\s*\S+(?:\s+\S+){0,3}?\s+(?:pas|plus|jamais|aucune?|nulle(?:ment)?)\b"
    r"|\b(?:aucune?|absente?s?|absence|manquante?s?|manque(?:nt)?|introuvables?|sans|ni|non)\b"
    r"|\bpour\s+(?:valider|vérifier|confirmer)\b|\bvérifier\s+si\b|\bsi\s+(?:la|le|les|une?)\b"
    r"|\b(?:not|no|none|absent|missing|without)\b")
#: Libellé tronqué par le modèle (« … / Indivi ») : dernier mot d'au moins ce nombre de caractères.
TRUNCATED_LABEL_MIN_CHARS = 4


def _word_seq(text: str) -> str:
    """Forme de comparaison : NFKC (« CO₂ » = « CO2 »), casse et accents repliés, MOTS entiers
    séparés par un espace — la mise en forme d'un libellé par le modèle ne fait pas une autre
    ligne, mais « Pompe A » n'est jamais « Pompe AB »."""
    words = re.findall(r"[a-z0-9]+", _fold(unicodedata.normalize("NFKC", str(text or ""))))
    return " " + " ".join(words) + " "


def _truncated_in(segment: str, hay: str) -> bool:
    """Le segment, dont le dernier mot est tronqué, se complète-t-il de façon UNIQUE dans la source ?
    (« Indivi » → « Individuel » ; deux complétions possibles = ambigu = absent)."""
    words = segment.split()
    if not words or len(words[-1]) < TRUNCATED_LABEL_MIN_CHARS or words[-1].isdigit():
        return False
    head = " ".join(words[:-1])
    pattern = r" " + (re.escape(head) + r" " if head else "") + re.escape(words[-1]) + r"([a-z0-9]+) "
    return len({m.group(1) for m in re.finditer(r"(?=" + pattern + ")", hay)}) == 1


def _source_has_row(chunk: dict, label: str) -> bool:
    """La source contient-elle CETTE ligne ? Chaque segment du libellé (« A / B / C ») doit y
    figurer comme suite de mots entiers : une ligne voisine qui partage un mot, un préfixe ou un
    nombre ne suffit pas. Seule tolérance : le DERNIER segment tronqué par le modèle, s'il se
    complète de façon unique (contrôle de citation seulement — jamais une preuve numérique)."""
    hay = _word_seq(chunk.get("text"))
    segments = [_word_seq(seg) for seg in re.split(r"\s+/\s+", label)]
    segments = [seg for seg in segments if len(seg.replace(" ", "")) >= 3]
    if not segments:
        return False
    *body, last = segments
    return all(seg in hay for seg in body) and (last in hay or _truncated_in(last.strip(), hay))


def _attributions(sentence: str) -> list:
    """Attributions de ligne de la phrase : ``(libellé, [séries de citations rattachées], niée)``.

    Seules comptent les citations SYNTAXIQUEMENT rattachées au libellé : celles qui le suivent dans
    la même proposition (jusqu'à « ; » ou au libellé suivant), y compris une parenthèse qui le suit
    immédiatement ; à défaut, la série la plus proche qui le précède. Une citation placée dans une
    parenthèse incidente ailleurs (« le document [1] (même projet que [3]), la ligne « X » »)
    porte une autre relation : elle n'est pas contrôlée — constat réel (R06)."""
    matches = [m for m in _ROW_ATTRIBUTION_RE.finditer(sentence or "") if not _OTHER_OBJECT_RE.search(m.group(1))]
    out = []
    for i, m in enumerate(matches):
        after_end = matches[i + 1].start() if i + 1 < len(matches) else len(sentence)
        semi = sentence.find(";", m.end(), after_end)
        after_end = semi if semi >= 0 else after_end
        tail = sentence[m.end():after_end]
        lead = re.match(r"\s*\([^()]*\)", tail)
        lead_len = lead.end() if lead else 0
        masked_tail = tail[:lead_len] + _PAREN_RE.sub(lambda p: " " * len(p.group(0)), tail[lead_len:])
        runs = [(m.end() + r.start(), m.end() + r.end(), r.group(0)) for r in _CITATION_RUN_RE.finditer(masked_tail)]
        if not runs:
            head = sentence[:m.start()]
            masked_head = _PAREN_RE.sub(lambda p: " " * len(p.group(0)), head)
            before = [r for r in _CITATION_RUN_RE.finditer(masked_head) if ";" not in masked_head[r.end():]]
            if before:
                r = before[-1]
                runs = [(r.start(), r.end(), r.group(0))]
        if not runs:
            continue
        scope_end = max(m.end(), max(end for _, end, _ in runs))
        negated = bool(_NEGATION_RE.search(sentence[:scope_end]))
        out.append((m.group(2), runs, negated))
    return out


def _check_row_attribution(sentence: str, chunks: list):
    """``(phrase, statut)`` ; statut ``ok``, ``citations_dropped`` ou ``removed``.

    Ne vise que la forme vérifiable : une ligne désignée par son libellé entre guillemets, dans une
    attribution AFFIRMATIVE, avec ses citations rattachées (`_attributions`). Chaque source citée
    doit contenir CETTE ligne (`_source_has_row`). Sinon :

    1. chaque série rattachée garde au moins une citation valide : les invalides sont retirées ;
    2. sinon la phrase est retirée. JAMAIS de citation de remplacement (NATIVE-TABLE-SAFETY-6 :
       constat réel, le remplacement changeait le sens de la phrase)."""
    attributions = [(label, runs) for label, runs, negated in _attributions(sentence) if not negated]
    if not attributions:
        return sentence, "ok"
    edits = {}
    for label, runs in attributions:
        for start, end, run in runs:
            nums = [int(x) for x in _CITATION_NUM_RE.findall(run)]
            valid = [n for n in dict.fromkeys(nums) if 1 <= n <= len(chunks) and _source_has_row(chunks[n - 1], label)]
            if not valid:
                return None, "removed"
            if len(valid) < len(dict.fromkeys(nums)):
                prev = edits.get((start, end))
                keep = [n for n in valid if prev is None or n in prev]
                if not keep:
                    return None, "removed"
                edits[(start, end)] = keep
    if not edits:
        return sentence, "ok"
    fixed = sentence
    for (start, end), keep in sorted(edits.items(), reverse=True):
        fixed = fixed[:start] + ", ".join(f"[{n}]" for n in keep) + fixed[end:]
    return re.sub(r"\s+([,.;:])", r"\1", fixed), "citations_dropped"


def attribution_notice(removed: int, language: Optional[str] = None) -> str:
    if language == "en":
        return (f"⚠️ {removed} statement(s) attributing a row to a source that does not contain it were removed.\n\n")
    return (f"⚠️ {removed} affirmation(s) attribuant une ligne à une source qui ne la contient pas ont été retirées.\n\n")


# --------------------------------------------------------------------------- #
# Affirmations d'absence contredites par les sources (NATIVE-TABLE-RECOVERY-9)
#
# Constat réel (GENERATION-8, R05 flux) : « Je ne trouve pas de mention de « Local ménage » dans le document
# « A6 … » [1] » publié alors que [1] porte « 3 Local ménage 4 3,85 15,38 ». Une phrase qui affirme l'ABSENCE
# d'une ligne / catégorie / mention nommée entre guillemets n'est pas publiée quand une source fiable de SA
# PORTÉE la contient. Prouver qu'une ligne EXISTE (texte, mots entiers) n'est pas prouver la valeur d'une colonne.

_LABEL = r"«\s*([^«»\n]{2,200}?)\s*»"
_OBJ = r"(?:mentions?|lignes?|références?|occurrences?|entrées?|catégories?|traces?|libellés?|champs?|colonnes?)"
#: Formes d'absence d'un objet nommé (prédicat de PRÉSENCE nié). Les négations portant sur une valeur, une
#: colonne non certifiée, une unité ou une preuve ne sont pas des absences d'objet (voir _VALUE_SUBJECT_RE).
_ABSENCE_RES = (
    # « aucune mention de « X » », « pas de ligne « X » », « aucune ligne intitulée « X » », « aucune mention du champ « X » »
    re.compile(r"(?i)\b(?:aucune?|pas\s+d['’]|pas\s+de|nulle)\s+(?:autre\s+)?" + _OBJ
               + r"\s+(?:(?:de|d['’]|à|au|du|des|intitulée?s?|nommée?s?|appelée?s?|exactement|la|le|l['’]|"
               + r"correspondante?s?|relative?s?|concernant|mentionnant|portant\s+sur|"
               + r"ligne|catégorie|champ|libellé|colonne)\s*){0,4}" + _LABEL),
    # « aucune ne correspond à « X » », « aucune ne mentionne « X » » (pronom repris d'une énumération)
    re.compile(r"(?i)\baucune?\s+(?:ne|n['’])\s*(?:correspond(?:ent)?|figure(?:nt)?|apparaî?t|mentionne(?:nt)?|concerne(?:nt)?)"
               r"\s+(?:à\s+|au\s+|la\s+|le\s+|l['’])?(?:ligne\s+|catégorie\s+)?" + _LABEL),
    # « la ligne « X » n'apparaît pas », « la catégorie « X » n'est pas mentionnée », « « X » ne figure pas »
    re.compile(r"(?i)" + _LABEL + r"\s*(?:\([^()]*\)\s*)?(?:n['’]|ne\s+)\s*(?:apparaît|apparait|figure|existe|"
               r"(?:est|sont)\s+(?:pas|nullement|jamais)\s+(?:mentionnée?s?|présente?s?|référencée?s?|listée?s?|"
               r"incluse?s?|indiquée?s?|citée?s?|répertoriée?s?))\b"),
    # « mais pas « X » »
    re.compile(r"(?i)\bmais\s+pas\s+(?:de\s+|la\s+|le\s+|l['’])?(?:ligne\s+|catégorie\s+)?" + _LABEL),
    # « « X » est absente de », « « X » est introuvable »
    re.compile(r"(?i)" + _LABEL + r"\s*(?:est|sont)\s+(?:absente?s?|introuvables?|manquante?s?)\b"),
    # « ne contient pas (de ligne) « X » », « ne mentionne pas la catégorie « X » »
    re.compile(r"(?i)\b(?:ne|n['’])\s*(?:contien(?:nen)?t|compor?te(?:nt)?|mentionne(?:nt)?|inclu(?:en)?t|"
               r"référence(?:nt)?|cite(?:nt)?|trouve)\s+(?:pas|aucune?)\s+(?:de\s+|d['’]|la\s+|le\s+|l['’])?"
               r"(?:" + _OBJ + r"\s+)?(?:(?:de|d['’]|à|intitulée?|nommée?|la|le|l['’])\s*){0,2}" + _LABEL),
)
#: Exclusivité : « les seules mentions de « X » se trouvent dans [2] » — absence affirmée partout ailleurs.
_EXCLUSIVE_RE = re.compile(r"(?i)\b(?:les?\s+seule?s?|la\s+seule|seule?s?)\s+(?:mentions?|références?|occurrences?)\s+"
                           r"(?:de\s+|à\s+|d['’]|du\s+|au\s+|mentionnant\s+|concernant\s+|relatives?\s+à\s+)?"
                           r"(?:la\s+|le\s+|l['’])?(?:ligne\s+|catégorie\s+)?" + _LABEL)
#: Négation portant sur une VALEUR / colonne / unité / preuve, ou phrase conditionnelle : hors champ.
_VALUE_SUBJECT_RE = re.compile(r"(?i)\b(?:valeurs?|données?\s+numériques?|chiffres?|montants?|unités?|preuves?)\b")
#: « … ne mentionne pas de « X » avec une valeur numérique » : la négation porte sur la valeur, pas sur l'objet.
_VALUE_QUALIFIER_RE = re.compile(r"(?i)[^.;]{0,40}?\b(?:avec|de|d['’]|pour)\s+(?:une\s+|la\s+|aucune\s+)?(?:valeurs?|chiffres?|données?\s+numériques?|montants?)\b")
_HYPOTHETICAL_RE = re.compile(r"(?i)\b(?:si|selon|d['’]après|il est possible|peut-être|semble)\b|\?\s*$")
#: Portée « corpus » : la phrase vise l'ensemble des sources transmises.
_CORPUS_SCOPE_RE = re.compile(r"(?i)\b(?:(?:les|des|aucune\s+des|toutes\s+les|autres)\s+sources?|"
                              r"(?:documents?|extraits?|textes?|tableaux?)\s+(?:fournis|analysés|consultés|disponibles)|"
                              r"sources?\s+(?:fournies|disponibles|consultées))\b")
_PDF_NAME_RE = re.compile(r"«\s*([^«»\n]{3,200}?\.(?:pdf|docx?|xlsx?|pptx?|csv|txt|msg))\s*»", re.IGNORECASE)
#: Un libellé qui est un NOM DE FICHIER désigne un document, pas une ligne : hors champ (portée, pas objet).
_FILE_LABEL_RE = re.compile(r"(?i)\.(?:pdf|docx?|xlsx?|pptx?|csv|txt|msg|dwg|dxf|ifc)\s*$")


def _presence_source(chunk: dict) -> bool:
    """Source admise comme preuve de PRÉSENCE d'un libellé : texte natif ou tableau, jamais une lecture d'image."""
    return status_of(chunk) not in (OCR_TABLE_UNVERIFIED, OCR_OTHER)


#: Clé du fragment récupéré entier (cf. `prompt_builder.RETRIEVED_TEXT_KEY`).
RETRIEVED_TEXT_KEY = "retrieved_text"
_EXCERPT_MARKERS_RE = re.compile(r"…\s*\(extrait tronqué[^)]*\)|…")


def _presence_text(chunk: dict) -> str:
    """Texte où chercher la PRÉSENCE d'un libellé : le fragment récupéré entier de CETTE source quand il est
    attaché et COHÉRENT avec l'extrait transmis (chaque morceau de l'extrait, hors marqueurs d'omission, y figure
    tel quel, espaces normalisés) ; sinon l'extrait. Aucune autre source, aucun nouvel accès (CONTEXT-GROUNDING-11)."""
    excerpt = str(chunk.get("text") or "")
    full = chunk.get(RETRIEVED_TEXT_KEY)
    if not isinstance(full, str) or not full or full == excerpt:
        return excerpt
    norm = lambda t: " ".join(t.replace("\u00a0", " ").split())
    hay = norm(full)
    pieces = [norm(p) for p in _EXCERPT_MARKERS_RE.split(excerpt)]
    if pieces and all(p in hay for p in pieces if p):
        return full
    return excerpt


def _has_label(chunk: dict, label: str) -> bool:
    """Le libellé est-il dans la source ? CHAQUE segment « A / B », courts compris (« Eges / SO / SO » n'est pas
    « Eges »), en mots entiers, sans tolérance de troncature ; au moins trois caractères alphanumériques au total.
    Cherché dans le fragment récupéré entier de la source quand il est attaché et cohérent (`_presence_text`)."""
    hay = _word_seq(_presence_text(chunk))
    segments = [_word_seq(seg) for seg in re.split(r"\s+/\s+", label)]
    segments = [seg for seg in segments if seg.strip()]
    return (bool(segments) and sum(len(seg.replace(" ", "")) for seg in segments) >= 3
            and all(seg in hay for seg in segments))


def _absence_claims(sentence: str) -> list:
    """``(libellé, exclusif)`` des affirmations d'absence d'un objet nommé dans la phrase."""
    out = []
    if _HYPOTHETICAL_RE.search(sentence):
        return out
    for rx in _ABSENCE_RES:
        for m in rx.finditer(sentence):
            if (_VALUE_SUBJECT_RE.search(sentence[:m.start()]) or _VALUE_QUALIFIER_RE.match(sentence[m.end():])
                    or _FILE_LABEL_RE.search(m.group(1))):
                continue
            out.append((m.group(1), False))
    for m in _EXCLUSIVE_RE.finditer(sentence):
        if not _FILE_LABEL_RE.search(m.group(1)):
            out.append((m.group(1), True))
    return out


def _absence_scope(sentence: str, chunks: list):
    """Portée de l'affirmation : ``("sources", indices)`` (citations [n] et documents nommés par leur fichier),
    ``("corpus", tous)`` ou ``(None, [])`` (portée non résolue : « ce tableau », « le document »)."""
    idx = {n - 1 for n in _citation_numbers(sentence) if 1 <= n <= len(chunks)}
    names = {_fold(n.strip()) for n in _PDF_NAME_RE.findall(sentence)}
    if names:
        idx |= {i for i, c in enumerate(chunks) if _fold(document_label(c)) in names}
    if idx:
        return "sources", sorted(idx)
    if _CORPUS_SCOPE_RE.search(sentence):
        return "corpus", list(range(len(chunks)))
    return None, []


def _contradicted_absences(sentence: str, chunks: list) -> set:
    """Libellés dont la phrase affirme l'absence alors qu'une source fiable DE SA PORTÉE les contient."""
    claims = _absence_claims(sentence)
    if not claims:
        return set()
    kind, idx = _absence_scope(sentence, chunks)
    out = set()
    for label, exclusive in claims:
        if exclusive:
            # « Les seules mentions de « X » se trouvent dans [2] » : contredite si une source fiable HORS des
            # sources / documents désignés porte X. Sans désignation, la portée est indéterminable.
            if kind != "sources":
                continue
            docs = {_document_id(chunks[i]) for i in idx}
            pool = [c for i, c in enumerate(chunks) if i not in idx and _document_id(c) not in docs]
        elif kind is None:
            continue
        else:
            pool = [chunks[i] for i in idx]
        if any(_presence_source(c) and _has_label(c, label) for c in pool):
            out.add(_fold(label))
    return out


def _check_absence(sentence: str, chunks: list, contradicted: set) -> bool:
    """Vrai si la phrase affirme une absence démontrée FAUSSE : contredite dans sa portée, ou de portée non
    résolue (« ce tableau ») sur un libellé que la même réponse a déjà affirmé absent à tort."""
    if _contradicted_absences(sentence, chunks):
        return True
    kind, _ = _absence_scope(sentence, chunks)
    return kind is None and any(not exclusive and _fold(label) in contradicted
                                for label, exclusive in _absence_claims(sentence))


# --------------------------------------------------------------------------- #
# Non-quantification contredite par le passage CITÉ (PRODUCTION-BLOCKERS-14)
#
# Constat réel (FINAL-QUALIFICATION-13, B001, sync ET flux) : « While the report notes that "most of the
# transactions included in the matched DB may be classified as VC investments" [1], it does not quantify this
# share » alors que, dans le fragment récupéré de [1], la même proposition porte « (43 %) » — coupé de l'extrait
# par la fenêtre de contexte. Le libellé n'est pas entre guillemets : la règle des absences ne s'applique pas.
#
# Seule forme vérifiable sans juge : la phrase nie une quantification ET cite mot pour mot un passage de la
# source qu'elle désigne ; la PROPOSITION de la source qui contient ce passage porte un nombre du type nié
# (pourcentage pour une part). Un nombre ailleurs dans le fragment ne suffit pas. Une fois une telle
# contradiction démontrée, les autres négations de la MÊME réponse portant sur la grandeur demandée par la
# question (même famille, portée compatible) sont retirées aussi. Rien d'autre n'est deviné.
_QUANT_FAMILIES = {
    "share": r"shares?|percentages?|proportions?|percent|fractions?|pourcentages?|parts?|taux|rates?",
    "count": r"how\s+many|numbers?|counts?|nombres?|combien",
    "amount": r"amounts?|figures?|values?|sums?|montants?|chiffres?|valeurs?|sommes?",
}
_QUANT_NOUN_RES = {k: re.compile(r"(?i)\b(?:" + v + r")\b") for k, v in _QUANT_FAMILIES.items()}
_QUANT_VERB_RE = re.compile(r"(?i)\b(?:quantif\w*|chiffr\w*|numerical|quantitative|statistical|breakdowns?|precision)\b")
#: Propagation par recouvrement : mots significatifs communs avec le passage cité de l'ancre.
QUOTE_PROPAGATION_MIN_SHARED = 2
_QUANT_NEGATION_RE = re.compile(
    r"(?i)\b(?:does|do|did)\s+not\s+(?:\w+\s+)?(?:quantify|provide|specify|give|report|state|indicate|disclose|"
    r"mention|include|offer|contain)\b"
    r"|\b(?:is|are|was|were)\s+not\s+(?:\w+\s+)?(?:quantified|specified|reported|given|provided|stated|"
    r"disclosed|available|indicated|mentioned)\b"
    r"|\bno\s+(?:[\w-]+\s+){0,3}?(?:shares?|percentages?|proportions?|figures?|numbers?|amounts?|data|breakdowns?|"
    r"values?|rates?|estimates?)\s+(?:is|are|was|were)\s+(?:given|provided|reported|specified|stated|available|"
    r"indicated|mentioned)\b"
    r"|\b(?:remains?|is|are)\s+(?:unknown|unspecified|unquantified|not\s+known)\b"
    r"|\bwithout\s+(?:numerical|quantitative|statistical)\s+\w+|\bwithout\s+knowing\b"
    r"|\blacks?\s+(?:precision|quantification|figures?|numbers?)\b"
    r"|\bthere\s+(?:is|are)\s+no\s+(?:[\w-]+\s+){0,3}?(?:data|figures?|breakdowns?|statistics?|numbers?|estimates?)\b"
    r"|\b(?:ne|n['’])\s*(?:(?:la|le|les|l['’]|en|y)\s*)?(?:quantifient|quantifie|précisent|précise|fournissent|fournit|donnent|donne|indiquent|"
    r"indique|chiffrent|chiffre|mentionnent|mentionne|communiquent|communique)\s+pas\b"
    r"|\bn['’](?:est|sont)\s+pas\s+(?:\w+\s+)?(?:précisée?s?|quantifiée?s?|chiffrée?s?|indiquée?s?|fournie?s?|"
    r"disponibles?|mentionnée?s?|communiquée?s?)\b"
    r"|\b(?:reste|restent|demeure|demeurent)\s+(?:inconnue?s?|indéterminée?s?|non\s+précisée?s?)\b")
#: Négation d'une PROPRIÉTÉ (« ne précisent pas si … », « do not specify whether … ») : pas une quantification.
_QUANT_PROPERTY_RE = re.compile(r"(?i)\b(?:si|whether|if)\b")
_QUOTED_RE = re.compile(r"[\"“«]\s*([^\"“”«»\n]{12,400}?)\s*[\"”»]")
#: Un passage cité ne sert d'ancre que s'il est assez long pour désigner UNE proposition.
QUOTE_ANCHOR_MIN_WORDS = 6
_CLAUSE_END_RE = re.compile(r"[,;:.!?](?=\s)|\n\s*(?:[▪•*–-]|\n)")
_PERCENT_RE = re.compile(r"\d(?:[.,]\d+)?\s?%")


def _quant_families(text: str) -> set:
    return {k for k, rx in _QUANT_NOUN_RES.items() if rx.search(text or "")}


def _negates_quantification(sentence: str) -> set:
    """Familles de grandeur dont la phrase NIE la quantification (vide : pas une telle négation)."""
    bare = _CITATION_RE.sub(" ", sentence or "")
    if not _QUANT_NEGATION_RE.search(bare) or _QUANT_PROPERTY_RE.search(bare):
        return set()
    families = _quant_families(bare)
    if not families and _QUANT_VERB_RE.search(bare):
        families = {"any"}
    return families


def _quantified(clause: str, families: set) -> bool:
    """La proposition porte-t-elle un nombre du type nié ? Part : un pourcentage ; sinon un nombre d'au moins
    deux chiffres hors année (ou un pourcentage)."""
    if _PERCENT_RE.search(clause):
        return True
    if families <= {"share"}:
        return False
    return any(len(re.sub(r"\D", "", m.group(0))) >= 2 and not _YEAR_RE.match(m.group(0).strip())
               for m in _NUMBER_RE.finditer(clause))


def _anchor_clause(text: str, anchor: str) -> Optional[str]:
    """Proposition de ``text`` qui contient ``anchor`` (mots entiers, mise en forme repliée) : du début de la
    proposition jusqu'à la fin de celle où le passage s'achève. ``None`` : passage absent."""
    words = [(_fold(m.group(0)), m.start(), m.end()) for m in re.finditer(r"[^\W_]+", text or "")]
    target = [_fold(w) for w in re.findall(r"[^\W_]+", anchor or "")]
    if not target:
        return None
    seq = [w for w, _, _ in words]
    for i in range(len(seq) - len(target) + 1):
        if seq[i:i + len(target)] == target:
            start, end = words[i][1], words[i + len(target) - 1][2]
            before = max((m.end() for m in _CLAUSE_END_RE.finditer(text, 0, start)), default=0)
            after = _CLAUSE_END_RE.search(text, end)
            return text[before:after.start() if after else len(text)]
    return None


def _anchored_quantification(sentence: str, chunks: list) -> Optional[tuple]:
    """``(familles, indices des sources, mots du passage)`` si la phrase nie une quantification que la proposition
    du passage qu'elle CITE porte dans la source désignée (citations [n], sinon toute source du contexte)."""
    families = _negates_quantification(sentence)
    if not families:
        return None
    cited = [n - 1 for n in _citation_numbers(sentence) if 1 <= n <= len(chunks)]
    pool = cited or list(range(len(chunks)))
    for quote in _QUOTED_RE.findall(sentence):
        if len(re.findall(r"[^\W_]+", quote)) < QUOTE_ANCHOR_MIN_WORDS:
            continue
        hits = [i for i in pool if _presence_source(chunks[i])
                and (clause := _anchor_clause(_presence_text(chunks[i]), quote)) is not None
                and _quantified(clause, families)]
        if hits:
            return families, hits, _tokens(quote)
    return None


def _contradicted_quantifications(sentences: list, chunks: list, question: str) -> set:
    """Indices (ligne, phrase) des négations de quantification démontrées fausses. Ancre : `_anchored_quantification`.
    Propagation, SEULEMENT si une ancre existe, à une autre négation de quantification de la même réponse, de portée
    compatible (cite une source contredite, ou aucune), qui porte sur le même objet : famille de la grandeur
    demandée par la question et d'une ancre, ou au moins ``QUOTE_PROPAGATION_MIN_SHARED`` mots significatifs
    communs avec un passage cité d'ancre."""
    anchored, families, sources, quoted = set(), set(), set(), set()
    for li, line in enumerate(sentences):
        for si, sentence in enumerate(line):
            hit = _anchored_quantification(sentence, chunks)
            if hit:
                anchored.add((li, si))
                families |= hit[0]
                sources |= set(hit[1])
                quoted |= hit[2]
    if not anchored:
        return set()
    asked = _quant_families(question) & (families | ({"share"} if "any" in families else set()))
    out = set(anchored)
    for li, line in enumerate(sentences):
        for si, sentence in enumerate(line):
            if (li, si) in out:
                continue
            fam = _negates_quantification(sentence)
            cited = {n - 1 for n in _citation_numbers(sentence)}
            same_object = bool(fam & asked) or (
                bool(fam) and len(_tokens(_CITATION_RE.sub(" ", sentence)) & quoted) >= QUOTE_PROPAGATION_MIN_SHARED)
            if same_object and (not cited or cited & sources):
                out.add((li, si))
    return out


# --------------------------------------------------------------------------- #
# Valeur d'une énumération « respectively » rattachée à une AUTRE entité (RELATION-GROUNDING-16)
#
# Constats réels (PB14 C B002 flux, PG15 B002 sync) : source « … collaborate similarly with EU (16) and Chinese ones
# (20 % and 16 %, respectively, of the total mixed deals with a US partner …) » ; réponses « 16 % des deals mixtes avec
# un partenaire américain impliquent un investisseur européen » [1]. La valeur existe dans [1] : le contrôle numérique
# l'acceptait. L'énumération attribue 16 % à la Chine et 20 % à l'UE.
#
# Seule forme vérifiable sans juge ni traduction libre : une phrase CITÉE place une valeur v près d'une entité E' d'une
# énumération « A, B … v1, v2 … respectively » de la source citée, sans nommer l'entité E à qui l'énumération donne v.
# Entités : lexique FERMÉ de noms géopolitiques EN / FR (équivalences factuelles, jamais issues d'un benchmark).
# Garde d'indépendance : aucune phrase de la source citée hors de l'énumération ne relie v à E'. Propriété : une
# affirmation que sa source citée étaye n'est jamais retirée. Hors champ : sens d'une relation (« A avec B » contre
# « B avec A »), nature d'un montant (taille de fonds / engagement), entités hors lexique, énumérations ambiguës.
_GEO_ENTITY_PATTERNS = {
    "US": r"united states|u\.s\.a?\.?|\bus\b|american|états-unis|etats-unis|américain\w*|americain\w*",
    "CN": r"\bchina\b|chinese|\bchine\b|chinois\w*",
    "EU": r"\beu\b|european union|\bue\b|union européenne|européen\w*|europeen\w*",
    "UK": r"united kingdom|\buk\b|royaume-uni|britanni\w*",
    "DE": r"germany|\bgerman\b|allemagne|allemand\w*",
    "FR": r"\bfrance\b|\bfrench\b|français\w*|francais\w*",
    "JP": r"japan\w*|\bjapon\w*|japonais\w*",
    "IN": r"\bindia\b|\bindian\b|\binde\b|\bindien\w*",
}
_GEO_ENTITY_RES = [(k, re.compile(v, re.IGNORECASE)) for k, v in _GEO_ENTITY_PATTERNS.items()]
_RESPECTIVELY_RE = re.compile(r"(?i)\b(?:respectively|respectivement)\b")
_LIST_PERCENT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s?%")
_LIST_GAP_RE = re.compile(r"\s*(?:,|and|et|,\s*and|,\s*et)\s*")
#: Découpage d'une phrase de réponse en propositions (une valeur se rattache aux entités de SA proposition).
_ANSWER_CLAUSE_RE = re.compile(r";|\(|\)|,\s+(?:et|and)\s+|\s+et entre\s+|\s+and between\s+")


def _geo_entities(text: str) -> list:
    hits = sorted((m.start(), k) for k, rx in _GEO_ENTITY_RES for m in rx.finditer(text or ""))
    out = []
    for _pos, k in hits:
        if not out or out[-1] != k:
            out.append(k)
    return out


def _respectively_maps(text: str) -> list:
    """``[{entité: valeur}]`` des énumérations « A, B … v1, v2 … respectively » NON ambiguës : suite contiguë de
    pourcentages juste avant le mot, autant d'entités du lexique (les dernières nommées dans la phrase), entités et
    valeurs toutes distinctes. Un appel de note « (16) » n'est pas un pourcentage : il n'entre pas dans la suite."""
    maps = []
    for m in _RESPECTIVELY_RE.finditer(text or ""):
        start = max(text.rfind(".", 0, max(m.start() - 1, 0)), text.rfind("\n\n", 0, m.start()), 0)
        sent = text[start:m.start()]
        vals = list(_LIST_PERCENT_RE.finditer(sent))
        if len(vals) < 2:
            continue
        run = [vals[-1]]
        for v in reversed(vals[:-1]):
            if _LIST_GAP_RE.fullmatch(sent[v.end():run[0].start()]):
                run.insert(0, v)
            else:
                break
        ents = _geo_entities(sent[:run[0].start()])
        if len(run) < 2 or len(ents) < len(run):
            continue
        ents, values = ents[-len(run):], [v.group(1).replace(",", ".") for v in run]
        if len(set(ents)) == len(ents) and len(set(values)) == len(values):
            maps.append(dict(zip(ents, values)))
    return maps


def _list_misattribution(sentence: str, texts: list) -> bool:
    maps = [mp for t in texts for mp in _respectively_maps(t)]
    if not maps:
        return False
    others = [x for t in texts for x in re.split(r"(?<=[.!?])\s+", t or "") if not _RESPECTIVELY_RE.search(x)]

    def linked_elsewhere(value: str, ents: set) -> bool:
        pat = re.compile(r"(?<![\d.,])" + re.escape(value).replace(r"\.", "[.,]") + r"\s?%")
        return any(pat.search(x) and set(_geo_entities(x)) & ents for x in others)

    for clause in _ANSWER_CLAUSE_RE.split(_CITATION_RE.sub(" ", sentence)):
        values = [v.replace(",", ".") for v in _LIST_PERCENT_RE.findall(clause)]
        named = set(_geo_entities(clause))
        for mp in maps:
            owner_of = {v: e for e, v in mp.items()}
            for v in values:
                owner = owner_of.get(v)
                if owner is None or owner in named:
                    continue
                wrong = (named & set(mp)) - {owner}
                if wrong and not linked_elsewhere(v, wrong):
                    return True
    return False


def _misattributed_list_values(sentences: list, chunks: list) -> set:
    """Indices (ligne, phrase) des phrases CITÉES qui rattachent une valeur d'énumération « respectively » de leur
    source citée à une autre entité de l'énumération (`_list_misattribution`)."""
    out = set()
    for li, line in enumerate(sentences):
        for si, sentence in enumerate(line):
            cited = [n - 1 for n in dict.fromkeys(_citation_numbers(sentence)) if 1 <= n <= len(chunks)]
            texts = [_presence_text(chunks[i]) for i in cited if _presence_source(chunks[i])]
            if texts and _list_misattribution(sentence, texts):
                out.add((li, si))
    return out


def list_attribution_notice(removed: int, language: Optional[str] = None) -> str:
    if language == "en":
        return (f"⚠️ {removed} statement(s) attributing a value to an entity other than the one the cited source assigns "
                "it to were removed.\n\n")
    return (f"⚠️ {removed} affirmation(s) attribuant une valeur à une autre entité que celle que la source citée lui "
            "associe ont été retirée(s).\n\n")


def quantification_notice(removed: int, language: Optional[str] = None) -> str:
    if language == "en":
        return (f"⚠️ {removed} statement(s) claiming that a source does not quantify an item were removed: the "
                "quoted source passage carries that figure.\n\n")
    return (f"⚠️ {removed} affirmation(s) selon lesquelles une source ne chiffre pas un élément ont été retirée(s) : "
            "le passage cité de la source porte ce chiffre.\n\n")


def absence_notice(removed: int, language: Optional[str] = None) -> str:
    if language == "en":
        return (f"⚠️ {removed} statement(s) claiming that an item is absent, although a source contains it, were removed.\n\n")
    return (f"⚠️ {removed} affirmation(s) d'absence contredite(s) par une source qui contient l'élément ont été retirée(s).\n\n")


def unit_notice(units: list, language: Optional[str] = None) -> str:
    shown = ", ".join(f"{u['token']} (« {u['unit']} »)" for u in units[:4])
    if language == "en":
        return (f"ℹ️ {len(units)} unit(s) not established by the source were removed; the value(s) are kept: {shown}. "
                "The unit is not specified in the source that supports the value.\n\n")
    return (f"ℹ️ {len(units)} unité(s) non établie(s) par la source retirée(s) ; la ou les valeurs sont conservées : "
            f"{shown}. L'unité n'est pas précisée dans la source qui porte la valeur.\n\n")


def _certify_units(sentence: str, *, question: str, asked: set, lone: bool, q_tokens: set,
                   line_text: str, proof_chunks: list) -> tuple:
    """``(phrase, unités retirées)`` : chaque valeur affichée avec une unité garde cette unité seulement si
    la preuve de la valeur la porte, seule (`unit_status`). Sinon l'unité est retirée et la valeur conservée.
    Valeur évaluée dans le contexte de la ligne, puis de la seule phrase (proposition réduite) ; preuve
    introuvable : unité retirée par prudence."""
    removed = []
    for claim in _claims_in(sentence, asked, lone=lone):
        if not claim.unit_phrase:
            continue
        status, evidence = CLAIM_UNSUPPORTED, []
        for ctx in (line_text, sentence):
            evidence = []
            status, _proof = evaluate_claim(claim, context=q_tokens | _tokens(ctx), sources=proof_chunks,
                                            context_text=(question or "", ctx), unit_evidence=evidence)
            if status == CLAIM_SUPPORTED:
                break
        unit = unit_status(claim, evidence) if status == CLAIM_SUPPORTED else UNIT_ABSENT_FROM_SOURCE
        if unit != UNIT_CERTIFIED:
            stripped = _strip_unit(sentence, claim)
            if stripped != sentence:
                sentence = stripped
                removed.append({"token": re.sub(r"[ \u00a0\u202f]?%$", "", claim.token.strip()),
                                "value": format(claim.value, "f"),
                                "unit": claim.unit_phrase, "status": unit})
    return sentence, removed


def _split(answer: str) -> list:
    """Propositions : ``[[phrase, …] par ligne]`` ; une ligne de liste reste une proposition."""
    return [_SENTENCE_SPLIT_RE.split(line) if line.strip() else [line] for line in (answer or "").split("\n")]


# --------------------------------------------------------------------------- #
# Réduction d'une proposition à ses affirmations prouvées (NATIVE-TABLE-GROUNDING-3)
# --------------------------------------------------------------------------- #
#: Marqueurs de DÉPENDANCE entre nombres : comparaison, écart, total, ratio, fourchette,
#: équivalence, condition, énumération appariée, pourcentage, signe. Présents : aucune réduction.
_DEPENDENCY_RE = re.compile(
    r"(?i)\b(?:sup[ée]rieure?s?|inf[ée]rieure?s?|plus|moins|que|qu['’]|d[ée]pass\w*|[ée]cart\w*|"
    r"diff[ée]ren\w*|contre|versus|vs|entre|jusqu\w*|soit|dont|totale?s?|totaux|somme\w*|ratio\w*|"
    r"rapport\w*|proportion\w*|respectivement|si|lorsqu\w*|sauf|environ|hausse|baisse|"
    r"augment\w*|diminu\w*|multipli\w*|divis\w*)\b"
    r"|\d\s*[/×*x]\s*\d|[+=<>≤≥%±]|(?:^|\s)[-−–]\s*\d|\b\d[\d\s.,]*\s*(?:à|-|–)\s*\d")
#: Une proposition conservée ne peut pas commencer par un renvoi ou une liaison : son sens
#: dépendrait d'un fragment retiré.
_DEPENDENT_START_RE = re.compile(
    r"(?i)^(?:et|mais|ou|donc|puis|alors|tandis|son|sa|ses|leur|leurs|il|elle|ils|elles|on|celui|"
    r"celle|ceux|celles|ce|cet|cette|ces|ceci|cela|qui|que|qu['’]|dont|où|lequel|laquelle|lesquels|"
    r"lesquelles|y|en|lui)\b")
_CLAUSE_SPLIT_RE = re.compile(r"\s*;\s+|,\s+")
_CITATION_NUM_RE = re.compile(r"\d+")


def _citation_numbers(text: str) -> list:
    out = []
    for m in _CITATION_RE.finditer(text or ""):
        out.extend(int(n) for n in _CITATION_NUM_RE.findall(m.group(0)))
    return out


def _free_numbers(text: str, keep: set, asked: set) -> bool:
    """Le texte porte-t-il un nombre LIBRE (ni l'affirmation conservée, ni repris de la question,
    ni collé à des lettres comme « CO2 ») ? Un chiffre seul compte : il n'est pas contrôlé."""
    line = _CITATION_RE.sub(" ", text or "")
    for m in _NUMBER_RE.finditer(line):
        before = line[m.start() - 1] if m.start() else " "
        after = line[m.end()] if m.end() < len(line) else " "
        if before.isalpha() or after.isalpha():
            continue
        value = _decimal(m.group(0).strip())
        if value is None or value in keep or value in asked or (value is not None and abs(value) in asked):
            continue
        return True
    return False


def _reduce_proposition(sentence: str, sentence_claims: list, *, question: str,
                        asked: set, lone: bool, chunks: list, proof_chunks: list):
    """``(proposition réduite, nombre d'affirmations conservées)`` ou ``None``.

    Conserve UNIQUEMENT des sous-propositions autonomes portant chacune UNE affirmation prouvée,
    dans le texte du modèle tel quel (aucune reformulation). Chaque partie est évaluée dans SON
    contexte — question, sujet de tête, partie — et non dans celui de la ligne entière : les
    parties à retirer ne déplacent plus la colonne la mieux appariée (constat : « niveau C1 »,
    cité à tort dans la même phrase, rendait « Projet » non vérifiable). Refus — la proposition
    reste retirée entière — dès qu'un des points suivants n'est pas démontré :

    1. aucune dépendance entre nombres dans la proposition (`_DEPENDENCY_RE`) ;
    2. découpage sur « ; » ou « , » + espace seulement ; une partie conservée porte UNE seule
       affirmation (une partie à plusieurs affirmations n'est admise que si aucune n'est prouvée :
       elle est alors retirée) ; une partie sans affirmation n'est admise qu'en TÊTE (le sujet) ;
    3. une partie conservée ne commence pas par un renvoi (`_DEPENDENT_START_RE`) et le texte
       conservé partage un mot avec la question (il dit de quoi il parle) ;
    4. aucun autre nombre libre dans le texte conservé (un chiffre seul compris) ;
    5. chaque affirmation reste PROUVÉE dans la proposition réduite entière (nouvelle évaluation) ;
    6. citations : seules celles qui désignent une source probante sont gardées ; aucune ne
       subsiste -> la source probante est citée ; une source probante hors liste -> refus."""
    if len(sentence_claims) < 2:
        return None
    bare = _CITATION_RE.sub(" ", sentence)
    if _DEPENDENCY_RE.search(_DATE_RE.sub(" ", bare)):
        return None
    parts = [p.strip(" .:") for p in _CLAUSE_SPLIT_RE.split(bare.strip()) if p.strip(" .:")]
    if len(parts) < 2:
        return None
    q_tokens = _tokens(question)

    def proven(claim, text: str) -> bool:
        status, _proof = evaluate_claim(claim, context=q_tokens | _tokens(text), sources=proof_chunks,
                                        context_text=(question or "", text))
        return status == CLAIM_SUPPORTED

    prefix, kept_parts, seen_claim = [], [], False
    for part in parts:
        part_claims = _claims_in(part, asked, lone=lone)
        if not part_claims:
            if seen_claim or _free_numbers(part, set(), asked):
                return None  # qualificatif après une affirmation, ou nombre non contrôlé
            prefix.append(part)
            continue
        seen_claim = True
        local = ", ".join(prefix + [part])
        ok = [proven(c, local) for c in part_claims]
        if len(part_claims) > 1:
            if any(ok):
                return None  # affirmations liées dans une même partie : inséparables
            continue  # partie entièrement non prouvée : retirée
        if ok[0]:
            kept_parts.append((part, part_claims[0]))
    if not kept_parts:
        return None
    if any(_DEPENDENT_START_RE.match(part) for part, _claim in kept_parts):
        return None
    text = ", ".join(prefix + [p for p, _ in kept_parts])
    if _DEPENDENT_START_RE.match(text) or not (_tokens(text) & q_tokens):
        return None
    if _free_numbers(text, {c.value for _, c in kept_parts}, asked):
        return None
    proof_ids = set()
    for _part, claim in kept_parts:
        status, proof = evaluate_claim(claim, context=q_tokens | _tokens(text), sources=proof_chunks,
                                       context_text=(question or "", text))
        if status != CLAIM_SUPPORTED:
            return None
        proof_ids.update(id(c) for c in proof)
    index = {id(c): n for n, c in enumerate(chunks, start=1)}
    if not proof_ids <= set(index):
        return None
    cited = [n for n in dict.fromkeys(_citation_numbers(sentence))
             if 1 <= n <= len(chunks) and id(chunks[n - 1]) in proof_ids]
    if not cited:
        cited = [min(index[i] for i in proof_ids)]
    return text + " " + "".join(f"[{n}]" for n in cited) + ".", len(kept_parts)


def guard_answer(answer: str, *, question: str, sources: list, language: Optional[str] = None) -> GuardDecision:
    """Décision DÉTERMINISTE par affirmation (voir l'en-tête du module). Même fonction pour le
    chemin synchrone et le flux : une même sortie de modèle reçoit la même décision."""
    chunks = [c for c in sources or [] if isinstance(c, dict)]
    split = _split(answer)
    false_quant = _contradicted_quantifications(split, chunks, question)
    false_list = _misattributed_list_values(split, chunks) - false_quant
    narrative = false_quant | false_list
    narrative_notices = ((quantification_notice(len(false_quant), language) if false_quant else "")
                         + (list_attribution_notice(len(false_list), language) if false_list else ""))
    if not context_is_protected(chunks):
        # Hors périmètre numérique : SEULS les contrôles narratifs (non-quantification ancrée, valeur d'énumération
        # rattachée à une autre entité) s'appliquent ; aucun nombre n'est contrôlé. Lignes sans retrait inchangées.
        if not narrative:
            return GuardDecision(VERDICT_PASS, answer)
        lines = []
        for li, (sentences, raw) in enumerate(zip(split, (answer or "").split("\n"))):
            if not any((li, si) in narrative for si in range(len(sentences))):
                lines.append(raw)
                continue
            kept = [x for si, x in enumerate(sentences) if (li, si) not in narrative]
            if kept:
                lines.append(" ".join(kept))
        body = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
        decision = GuardDecision(VERDICT_REDACT, answer, consulted=_consulted(chunks),
                                 quantifications_removed=len(false_quant), list_attributions_removed=len(false_list))
        if not _CITATION_RE.sub("", body).strip(" \n.:;"):
            decision.verdict, decision.answer = VERDICT_BLOCK, abstention_answer(decision.consulted, language)
        else:
            decision.answer = narrative_notices + body
        return decision
    named = _named_documents(question, chunks)
    if named:
        chunks_for_proof = [c for c in chunks if _document_id(c) in named]
    else:
        chunks_for_proof = chunks
    asked = numbers_in(question)
    body = _CITATION_RE.sub(" ", answer or "").strip(" \n.:;")
    lone = bool(_NUMBER_RE.fullmatch(body.strip()))
    q_tokens = _tokens(question)
    kept_lines, removed, claims, kept_supported, reduced_count = [], 0, [], 0, 0
    attr_removed = attr_dropped = absent_removed = 0
    units_removed: list = []

    def certify(text: str) -> str:
        text, gone = _certify_units(text, question=question, asked=asked, lone=lone, q_tokens=q_tokens,
                                    line_text=line_text, proof_chunks=chunks_for_proof)
        units_removed.extend(gone)
        return text
    contradicted = set()
    for sentences in split:
        for sentence in sentences:
            contradicted |= _contradicted_absences(sentence, chunks)
    for li, sentences in enumerate(split):
        line_text = " ".join(sentences)
        kept = []
        for si, sentence in enumerate(sentences):
            if (li, si) in narrative:
                continue
            if contradicted and _check_absence(sentence, chunks, contradicted):
                absent_removed += 1
                continue
            sentence, attribution = _check_row_attribution(sentence, chunks)
            if attribution == "removed":
                attr_removed += 1
                continue
            attr_dropped += attribution == "citations_dropped"
            sentence_claims = _claims_in(sentence, asked, lone=lone)
            statuses = []
            for claim in sentence_claims:
                status, proof = evaluate_claim(claim, context=q_tokens | _tokens(line_text), sources=chunks_for_proof,
                                               context_text=(question or "", line_text))
                statuses.append(status)
                claims.append({"value": format(claim.value, "f"), "unit": claim.unit, "status": status,
                               "proof": [{"document": document_label(c), "page": _page(c), "status": status_of(c)}
                                         for c in proof][:3]})
            if all(st == CLAIM_SUPPORTED for st in statuses):
                kept.append(certify(sentence))
                kept_supported += len(statuses)  # seuls les nombres d'une proposition CONSERVÉE comptent
            else:
                removed += 1
                # NATIVE-TABLE-GROUNDING-3 : une affirmation prouvée SÉPARABLE de ses voisines non
                # prouvées est conservée seule ; sinon la proposition reste retirée entière.
                reduced = _reduce_proposition(sentence, sentence_claims, question=question,
                                              asked=asked, lone=lone, chunks=chunks, proof_chunks=chunks_for_proof)
                if reduced is not None:
                    kept.append(certify(reduced[0]))
                    kept_supported += reduced[1]
                    reduced_count += 1
        if kept or not line_text.strip():
            kept_lines.append(" ".join(kept))
    decision = GuardDecision(VERDICT_PASS, answer, claims=claims, removed=removed, consulted=_consulted(chunks),
                             reduced=reduced_count, attributions_removed=attr_removed,
                             citations_dropped=attr_dropped, absences_removed=absent_removed, units=units_removed,
                             quantifications_removed=len(false_quant), list_attributions_removed=len(false_list))
    body_out = re.sub(r"\n{3,}", "\n\n", "\n".join(kept_lines)).strip()
    notices = ((attribution_notice(attr_removed, language) if attr_removed else "")
               + (absence_notice(absent_removed, language) if absent_removed else "")
               + narrative_notices
               + (unit_notice(units_removed, language) if units_removed else ""))
    if not removed:
        if attr_removed or attr_dropped or absent_removed or units_removed or narrative:
            # Aucune affirmation chiffrée retirée, mais des attributions de ligne, des absences ou des unités corrigées.
            decision.verdict = VERDICT_REDACT
            if (absent_removed or narrative) and not _CITATION_RE.sub("", body_out).strip(" \n.:;"):
                # Absence fausse retirée et plus rien d'exploitable : abstention conservatrice existante.
                decision.verdict, decision.answer = VERDICT_BLOCK, abstention_answer(decision.consulted, language)
                return decision
            decision.answer = notices + body_out
        return decision
    if not kept_supported:
        decision.verdict, decision.answer = VERDICT_BLOCK, abstention_answer(decision.consulted, language)
        return decision
    decision.verdict = VERDICT_REDACT
    decision.answer = removal_notice(removed, language) + notices + body_out
    return decision
