"""PromptBuilder — construction du prompt final et des préambules (Lot P1-B2).

Responsabilité UNIQUE : sélectionner le préambule (selon le mode / la confiance /
le refus partiel), formater les sources sous budget, injecter la mémoire de
conversation et produire le prompt final. Ne fait NI retrieval, NI choix des
candidats, NI reranking, NI answerability, NI audit, NI appel LLM.

Sortie : ``PromptBuildResult`` (prompt, sources budgétées, answer_kind, preamble_kind).

Code (préambules + budget) déplacé VERBATIM depuis ``orchestrator.query_rag`` :
prompt STRICTEMENT identique à entrées égales.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from django.conf import settings

from apps.rag_core.domain.retrieval import build_prompt, render_prompt

from .contracts import PromptBuildResult, QueryExecutionContext


# ---------------------------------------------------------------------------
# Helpers de budget de prompt (déplacés depuis orchestrator — zéro ref externe)
# ---------------------------------------------------------------------------
def _truncate_text(s: Any, max_chars: int) -> str:
    t = ("" if s is None else str(s)).replace("\u00a0", " ")
    t = t.strip()
    if max_chars <= 0:
        return t
    if len(t) <= max_chars:
        return t
    return t[: max_chars - 1].rstrip() + "…"


#: Marqueur d'omission d'un extrait TABULAIRE tronqué sur frontière de ligne.
TABLE_TRUNCATION_MARKER = (
    "… (extrait tronqué : les lignes suivantes du tableau ne sont pas affichées)"
)


def _truncate_table_text(s: Any, max_chars: int, *, count_marker: bool = False) -> str:
    """Tronque un extrait TABULAIRE sans jamais couper une ligne en son milieu.

    Couper un tableau Markdown à un nombre de caractères produit une demi-ligne — un
    chiffre amputé, une colonne décalée — que le modèle lira comme une donnée. On coupe
    donc sur une frontière de LIGNE, et on le DIT : un extrait tronqué en silence est
    exactement ce qui fait passer une liste partielle pour complète.

    ``count_marker`` (C1, lot PROMPT-RENDER-CONFORMANCE-FIX-1A). Historiquement le
    marqueur était ajouté APRÈS avoir consommé ``max_chars`` sur les lignes : le texte
    rendu dépassait le plafond de la longueur du marqueur (mesuré : prompt de 8 350
    caractères pour un plafond de 8 300, sans aucun signalement). Avec ``count_marker``,
    une ligne n'est conservée que si lignes + séparateurs + marqueur tiennent dans
    ``max_chars`` ; la longueur rendue est donc ≤ ``max_chars`` dès qu'une ligne tient.
    Un marqueur déjà présent en fin de texte n'est jamais doublé (idempotence). Le défaut
    ``False`` reproduit le comportement historique à l'octet près.
    """
    t = ("" if s is None else str(s)).replace("\u00a0", " ").strip()
    if max_chars <= 0 or len(t) <= max_chars:
        return t
    lines = t.splitlines()
    reserve = 0
    if count_marker:
        reserve = len(TABLE_TRUNCATION_MARKER)
        if lines and lines[-1] == TABLE_TRUNCATION_MARKER:
            # Déjà tronqué plus haut : on recoupe sans empiler un second marqueur.
            lines = lines[:-1]
    kept: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 + reserve > max_chars:
            break
        kept.append(line)
        used += len(line) + 1
    kept.append(TABLE_TRUNCATION_MARKER)
    return "\n".join(kept)


# ---------------------------------------------------------------------------
# Fenêtre de l'extrait : tête (historique) ou centrée sur la preuve
# ---------------------------------------------------------------------------
#
# `_truncate_text` prend les N PREMIERS caractères du chunk. Sur un corpus découpé à
# `RAG_CHUNK_SIZE_CHARS` (2 500) avec `RAG_SNIPPET_MAX_CHARS` (800 dans les deux
# environnements), 68 % d'un chunk plein n'atteint donc jamais le modèle — mesuré au lot
# SNIPPET-TRUNCATION-1A : 77,7 % des chunks de production dépassent 800 caractères et
# 41 % des caractères retenus par le retrieval sont supprimés à cette étape. Le chunk
# PERTINENT est bien retrouvé et bien classé (rang 1 ou 2), mais la phrase qui porte la
# réponse se trouve après le caractère 800 : la source est citée sans sa preuve.
#
# La stratégie `centered` choisit la MÊME quantité de texte (budget inchangé) autour de
# la phrase la plus proche de la question. Mesuré en simulation sur cinq cas d'échec
# reproduits : tête 0/5, tête élargie à 2 000 c 5/5 mais au prix de la moitié des
# sources, fenêtre centrée 5/5 **sans perdre une seule source** et sans toucher au
# budget. C'est le PLACEMENT qui manquait, pas la quantité de texte.
#
# Le mécanisme est purement local : aucune requête SQL, aucun appel réseau, aucun
# embedding, aucun rerank — seulement la question brute et le texte du chunk déjà chargé.
#: Fragment récupéré ENTIER d'une source, conservé à côté de son extrait (`text`) — preuve de présence pour
#: le contrôle des absences seulement (CONTEXT-GROUNDING-11). Jamais projeté vers l'API ni vers l'audit.
RETRIEVED_TEXT_KEY = "retrieved_text"

SNIPPET_STRATEGY_HEAD = "head"
SNIPPET_STRATEGY_CENTERED = "centered"

#: Longueur minimale d'un terme de question retenu pour l'appariement. En dessous, un
#: token appariera n'importe quoi (« eau » est dans « niveau », « réseau », « bureau »).
_MATCH_MIN_TOKEN_CHARS = 4

#: Longueur du PRÉFIXE comparé. L'appariement littéral échoue sur la morphologie :
#: la question dit « exploite », le document dit « L'exploitant » — c'était le dernier
#: échec résiduel du banc 1A. Comparer les six premiers caractères relie les deux, et
#: c'est exactement ce que le FTS fait déjà par radicalisation. Pas de stemmer, pas de
#: dépendance NLP : une troncature de préfixe, déterministe et bornée.
_MATCH_PREFIX_CHARS = 6

_MATCH_TOKEN_RE = re.compile(r"[a-z0-9-]{%d,}" % _MATCH_MIN_TOKEN_CHARS)

#: Segmentation LOCALE et volontairement grossière : le but n'est pas une analyse
#: linguistique mais une fenêtre STABLE autour d'une unité de texte. Le plancher de
#: 20 caractères évite qu'un fragment d'abréviation (« p. ex. ») devienne « la phrase
#: la plus pertinente » ; ces fragments restent dans la fenêtre comme contexte.
_SENTENCE_RE = re.compile(r"[^.!?;\n]{20,}[.!?;\n]?")

#: Distance maximale de recalage d'une borne sur un espace (jamais au milieu d'un mot).
_SNAP_WINDOW_CHARS = 60

_ELLIPSIS = "…"

#: Mots fonctionnels FR/EN écartés de l'appariement. Liste VOLONTAIREMENT courte et
#: générique : aucun terme métier, aucun nom propre de corpus — un mot du domaine
#: écarté ici deviendrait un angle mort silencieux du retrieval.
_MATCH_STOPWORDS = frozenset(
    {
        "quel", "quelle", "quels", "quelles", "combien", "comment", "pourquoi",
        "dans", "pour", "avec", "cette", "leur", "leurs", "elle", "sont", "faut",
        "what", "which", "does", "with", "that", "this", "from", "have", "been",
        "their", "about", "into",
    }
)


def _fold_for_match(s: str) -> str:
    """Minuscule sans accent, **de même longueur** que l'entrée.

    La longueur importe : les positions calculées sur la forme repliée sont réutilisées
    pour découper le texte ORIGINAL. Une normalisation NFD naïve ajoute un caractère par
    accent et décale donc toutes les bornes suivantes — un défaut qui ne se voit que sur
    les documents accentués, c'est-à-dire l'essentiel du corpus.
    """
    out: list[str] = []
    for ch in s:
        base = "".join(
            c for c in unicodedata.normalize("NFD", ch) if unicodedata.category(c) != "Mn"
        )
        folded = (base[:1] or " ").lower()
        out.append(folded[:1] or " ")
    return "".join(out)


def _question_prefixes(question: str | None) -> tuple[str, ...]:
    """Préfixes d'appariement issus de la question BRUTE, dans l'ordre, sans doublon."""
    prefixes: list[str] = []
    for match in _MATCH_TOKEN_RE.finditer(_fold_for_match(question or "")):
        token = match.group(0)
        if token in _MATCH_STOPWORDS:
            continue
        prefix = token[:_MATCH_PREFIX_CHARS]
        if prefix not in prefixes:
            prefixes.append(prefix)
    return tuple(prefixes)


def _best_sentence_span(
    text: str, folded: str, prefixes: tuple[str, ...]
) -> tuple[int, int] | None:
    """Bornes de la phrase la plus dense en termes de la question, ou ``None``.

    Le score divise le nombre de termes DISTINCTS appariés par la racine carrée de la
    longueur : sans cette normalisation, un long paragraphe l'emporte sur la phrase qui
    répond, par accumulation. La comparaison est STRICTE (``>``), donc à score égal la
    PREMIÈRE phrase gagne — c'est ce qui rend le choix déterministe.
    """
    best: tuple[int, int] | None = None
    best_score = 0.0
    for match in _SENTENCE_RE.finditer(text):
        segment = folded[match.start() : match.end()]
        hits = sum(1 for prefix in prefixes if prefix in segment)
        if not hits:
            continue
        score = hits / (len(segment) ** 0.5)
        if score > best_score:
            best_score = score
            best = (match.start(), match.end())
    return best


def _first_match_in_span(
    folded: str, span: tuple[int, int], prefixes: tuple[str, ...]
) -> int:
    """Position du premier terme apparié DANS la phrase retenue (repli : son début)."""
    start, end = span
    segment = folded[start:end]
    found = [start + pos for pos in (segment.find(p) for p in prefixes) if pos >= 0]
    return min(found) if found else start


def _snap_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    """Recale les bornes sur des espaces proches — jamais au milieu d'un mot."""
    if start > 0:
        space = text.rfind(" ", max(0, start - _SNAP_WINDOW_CHARS), start + 1)
        if space != -1:
            start = space + 1
    if end < len(text):
        space = text.find(" ", max(start, end - _SNAP_WINDOW_CHARS), end)
        if space != -1:
            end = space
    return start, end


def _window_for_span(
    text: str, max_chars: int, span: tuple[int, int], anchor: int
) -> str:
    """Fenêtre de ``max_chars`` au plus (marqueurs compris) autour de ``span``.

    La phrase retenue est incluse ENTIÈREMENT quand le budget le permet, puis le reste
    du budget sert de contexte : un tiers avant, deux tiers après. Une source réduite à
    sa seule phrase perd ce qui la rend citable (le sujet, l'unité, le repère).

    Quand la phrase est plus longue que le budget, la fenêtre s'ancre sur le premier
    terme apparié : c'est la partie utile, et le choix reste déterministe.
    """
    # Les marqueurs de troncature sont comptés DANS le budget : un extrait annoncé à
    # 800 caractères qui en fait 802 déborde d'un budget de prompt déjà saturé.
    room = max(1, max_chars - 2 * len(_ELLIPSIS))
    span_start, span_end = span
    span_len = span_end - span_start
    if span_len >= room:
        start = anchor - room // 3
    else:
        start = span_start - (room - span_len) // 3
    start = max(0, min(start, len(text) - room))
    start, end = _snap_bounds(text, start, min(len(text), start + room))

    body = text[start:end].strip()
    prefix = _ELLIPSIS if start > 0 else ""
    suffix = _ELLIPSIS if end < len(text) else ""
    snippet = f"{prefix}{body}{suffix}"
    if len(snippet) > max_chars:
        # Invariant, jamais franchi : on rogne le corps, pas le budget.
        keep = max_chars - len(prefix) - len(_ELLIPSIS)
        snippet = f"{prefix}{body[: max(0, keep)].rstrip()}{_ELLIPSIS}"
    return snippet


def _build_centered_snippet(s: Any, question: str | None, max_chars: int) -> str:
    """Extrait de ``max_chars`` centré sur la phrase la plus proche de ``question``.

    Fonction PURE et déterministe : mêmes (texte, question, budget) ⇒ même extrait.
    Sans terme exploitable, sans phrase appariée, ou si le texte tient dans le budget,
    elle renvoie EXACTEMENT ce que renvoyait ``_truncate_text`` — le repli n'est pas une
    approximation du comportement historique, c'en est l'appel.
    """
    text = ("" if s is None else str(s)).replace("\u00a0", " ").strip()
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    prefixes = _question_prefixes(question)
    if not prefixes:
        return _truncate_text(text, max_chars)
    folded = _fold_for_match(text)
    span = _best_sentence_span(text, folded, prefixes)
    if span is None:
        return _truncate_text(text, max_chars)
    return _window_for_span(text, max_chars, span, _first_match_in_span(folded, span, prefixes))


def _designated_row_span(text: str, question: str | None) -> tuple[int, int] | None:
    """Bornes de la PREMIÈRE ligne du texte qui porte le libellé de ligne désigné par la question
    (« la ligne « X » », « la catégorie « X » »…) : chaque segment « A / B » en mots entiers, casse et
    accents repliés, sans tolérance de troncature. ``None`` sans libellé désigné ou sans ligne qui le porte.

    Un mot générique commun ne suffit jamais : « Local ménage » ≠ « Local technique CFA »."""
    from .numeric_evidence import _question_row_labels, _word_seq

    for label in _question_row_labels(question or ""):
        segments = [seg for seg in (_word_seq(part) for part in re.split(r"\s+/\s+", label)) if seg.strip()]
        if not segments or sum(len(seg.replace(" ", "")) for seg in segments) < 3:
            continue
        for line in re.finditer(r"[^\n]+", text):
            words = _word_seq(line.group(0))
            if all(seg in words for seg in segments):
                return line.start(), line.end()
    return None


def _anchor_on_designated_row(snippet: str, text: Any, question: str | None, max_chars: int) -> str:
    """CONTEXT-GROUNDING-11 : l'extrait d'un fragment qui porte la ligne DÉSIGNÉE par la question doit la
    montrer au modèle. Constat réel (R05) : la fenêtre centrée de 800 c. d'une page de tableau aplati
    omettait « 3 Local ménage 4 3,85 15,38 » — le modèle affirmait alors l'absence de la ligne.

    Stratégie ``centered`` seulement (le mode ``head`` reste l'historique à l'octet près). Même budget,
    même fonction de fenêtre (`_window_for_span`) ; n'agit que si l'extrait centré ne contient pas déjà
    cette ligne. Montrer la ligne ne certifie aucun de ses nombres."""
    full = ("" if text is None else str(text)).replace("\u00a0", " ").strip()
    if max_chars <= 0 or len(full) <= max_chars:
        return snippet
    span = _designated_row_span(full, question)
    if span is None:
        return snippet
    line = full[span[0]:span[1]].strip()
    if line and line in snippet:
        return snippet
    return _window_for_span(full, max_chars, span, span[0])


def _snippet_strategy() -> str:
    """Stratégie de fenêtre effective. Toute valeur inconnue vaut ``head``.

    Le registre valide déjà l'énumération au démarrage (et un `config.E001` signale une
    valeur refusée sans bloquer, cf. `config/settings/from_registry.py`). Ici, on ne
    devine pas : tout ce qui n'est pas explicitement ``centered`` garde le comportement
    historique. Une faute de frappe ne peut donc pas activer une nouvelle sémantique.
    """
    value = getattr(settings, "RAG_SNIPPET_STRATEGY", SNIPPET_STRATEGY_HEAD)
    return (
        SNIPPET_STRATEGY_CENTERED
        if value == SNIPPET_STRATEGY_CENTERED
        else SNIPPET_STRATEGY_HEAD
    )


def _chunk_is_tabular(c: dict[str, Any]) -> bool:
    src = c.get("source") if isinstance(c.get("source"), dict) else {}
    meta = src.get("meta") if isinstance(src.get("meta"), dict) else (
        c.get("meta") if isinstance(c.get("meta"), dict) else {}
    )
    kind = meta.get("kind") if isinstance(meta, dict) else None
    return isinstance(kind, str) and (kind.startswith("excel_") or kind == "table")


def _chunk_is_structured_visual(c: dict[str, Any]) -> bool:
    """Fragment d'extraction STRUCTURÉE (graphique / tableau, ``meta.visual.structured``)."""
    src = c.get("source") if isinstance(c.get("source"), dict) else {}
    meta = src.get("meta") if isinstance(src.get("meta"), dict) else (
        c.get("meta") if isinstance(c.get("meta"), dict) else {}
    )
    visual = meta.get("visual") if isinstance(meta, dict) else None
    return isinstance(visual, dict) and "structured" in visual


STRUCTURED_PARTIAL_MARKER = (
    "[Extrait structuré PARTIEL : {kept}/{total} lignes de données retenues "
    "(les plus proches de la question) ; les autres lignes ne sont pas affichées]"
)
_STRUCTURED_DIGIT_RE = re.compile(r"\d")


def _structured_line_role(line: str) -> str:
    """``data`` (valeur), ``block_header`` (rangée d'en-tête sans chiffre) ou ``context``.

    Grille : cellules jointes par `` | `` ; série : ``- catégorie : valeur``. Tout le reste
    (titre, dimensions, catégories, source, avertissement) est du contexte, toujours gardé.
    """
    if " | " in line:
        cells = [c for c in line.split(" | ") if c.strip()]
        if len(cells) >= 2 and not any(_STRUCTURED_DIGIT_RE.search(c) for c in cells):
            return "block_header"
        return "data" if _STRUCTURED_DIGIT_RE.search(line) else "context"
    if line.lstrip().startswith("- ") and _STRUCTURED_DIGIT_RE.search(line):
        return "data"
    return "context"


def _select_structured_text(s: Any, question: str | None, max_chars: int) -> str:
    """Extrait d'un fragment structuré, en UNITÉS LOGIQUES, sous ``max_chars``.

    Tient entier -> rendu tel quel (aucune coupe). Sinon : contexte et en-têtes de bloc
    TOUJOURS gardés, lignes de données classées par appariement à la question (préfixes
    de la stratégie ``centered``) puis remises dans l'ordre d'origine, sous un en-tête de
    bloc qui reste le leur ; la couverture partielle est DÉCLARÉE, jamais silencieuse.
    Si le seul contexte dépasse, repli sur la coupe tabulaire par ligne (marquée).
    """
    t = ("" if s is None else str(s)).replace("\u00a0", " ").strip()
    if max_chars <= 0 or len(t) <= max_chars:
        return t
    lines = t.splitlines()
    roles = [_structured_line_role(line) for line in lines]
    data_idx = [i for i, r in enumerate(roles) if r == "data"]
    required = [i for i, r in enumerate(roles) if r != "data"]
    marker_room = len(STRUCTURED_PARTIAL_MARKER) + 16
    used = sum(len(lines[i]) + 1 for i in required)
    if used + marker_room > max_chars or not data_idx:
        return _truncate_table_text(t, max_chars, count_marker=True)
    prefixes = _question_prefixes(question)

    def _score(i: int) -> int:
        folded = _fold_for_match(lines[i])
        return sum(1 for p in prefixes if p in folded)

    kept: set[int] = set()
    for i in sorted(data_idx, key=lambda i: (-_score(i), i)):
        if used + len(lines[i]) + 1 + marker_room > max_chars:
            continue
        kept.add(i)
        used += len(lines[i]) + 1
    out = [lines[i] for i in range(len(lines)) if i in kept or roles[i] != "data"]
    out.append(STRUCTURED_PARTIAL_MARKER.format(kept=len(kept), total=len(data_idx)))
    return "\n".join(out)


#: Ligne d'OCR ordinaire écartée au profit de la ligne structurée de la MÊME page (PAGE-TABLE-ROUTING-1).
ARBITRATED_LINE_MARKER = (
    "[ligne d'OCR masquée : mêmes chiffres que la ligne du tableau extraite en structuré sur "
    "cette page, mais signes ou séparateurs perdus ; se fier au tableau structuré]"
)
_NUMBER_RE = re.compile(r"(?<![\w])[+\-−–]?\d+(?:[   ]\d{3})*(?:[.,]\d+)?")
_MIN_ARBITRATION_LABEL = 3


def _fold_label(s: str) -> str:
    import unicodedata

    s = unicodedata.normalize("NFKD", str(s or ""))
    return re.sub(r"[^0-9a-z]", "", "".join(c for c in s if not unicodedata.combining(c)).lower())


def _normalized_numbers(text: str) -> tuple[str, ...]:
    out = []
    for m in _NUMBER_RE.finditer(text or ""):
        t = re.sub(r"[   ]", "", m.group(0)).replace("−", "-").replace("–", "-").replace(",", ".")
        out.append(t.lstrip("+"))
    return tuple(out)


def _structured_rows(text: str) -> list[tuple[str, str, str, tuple[str, ...]]]:
    """Rangées d'un fragment structuré : (libellé replié, chiffres du libellé, chiffres des valeurs,
    valeurs normalisées). Fragment de ligne (« Libellé:\\nvaleur ») ou grille (« a | b | c »)."""
    rows = []
    t = str(text or "")
    blocks = [b.split(":\n", 1) for b in t.split("\n\n") if ":\n" in b and not b.startswith("Context:")]
    if blocks:
        label = blocks[0][1].strip()
        values = [v.strip() for _, v in blocks[1:] if _NUMBER_RE.fullmatch(v.strip())]
        if len(values) >= 2:
            rows.append((label, values))
    for line in t.splitlines():
        if " | " in line:
            cells = [c.strip() for c in line.split(" | ")]
            values = [c for c in cells[1:] if _NUMBER_RE.fullmatch(c)]
            if cells and len(values) >= 2:
                rows.append((cells[0], values))
    return [(_fold_label(lab), re.sub(r"\D", "", lab), re.sub(r"\D", "", "".join(vals)), _normalized_numbers(" ".join(vals)))
            for lab, vals in rows if len(_fold_label(lab)) >= _MIN_ARBITRATION_LABEL]


def _doc_page(c: dict[str, Any]) -> tuple[str, str] | None:
    src = c.get("source") if isinstance(c.get("source"), dict) else {}
    doc = src.get("document_id") or c.get("document_id")
    page = src.get("page_number") or c.get("page_number")
    return (str(doc), str(page)) if doc and page else None


def _arbitrate_same_page_numbers(selected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Écarte, dans l'OCR ordinaire, les lignes que la MÊME page fournit en structuré.

    Une ligne d'une source NON structurée est masquée seulement si : même document ET même page
    qu'un fragment structuré présent dans le contexte ; elle commence par le libellé d'une rangée
    de ce fragment ; ses chiffres (libellé + valeurs) sont EXACTEMENT ceux de la rangée, mais
    leur rendu diffère (signe, séparateur décimal perdu). Autre document, autre page, autres
    chiffres ou même rendu : rien n'est touché. Sans fragment structuré : liste rendue telle quelle."""
    rows_by_page: dict[tuple[str, str], list] = {}
    for c in selected or []:
        if isinstance(c, dict) and _chunk_is_structured_visual(c) and _doc_page(c):
            rows_by_page.setdefault(_doc_page(c), []).extend(_structured_rows(c.get("text")))
    if not any(rows_by_page.values()):
        return selected
    out = []
    for c in selected:
        key = _doc_page(c) if isinstance(c, dict) else None
        rows = rows_by_page.get(key) if key else None
        if not rows or _chunk_is_structured_visual(c) or not isinstance(c.get("text"), str):
            out.append(c)
            continue
        lines, masked = c["text"].split("\n"), 0
        for i, line in enumerate(lines):
            folded = _fold_label(line)
            for label, label_digits, digits, numbers in rows:
                if (folded.startswith(label) and re.sub(r"\D", "", line) == label_digits + digits
                        and _normalized_numbers(line)[-len(numbers):] != numbers):
                    lines[i] = ARBITRATED_LINE_MARKER
                    masked += 1
                    break
        if masked:
            c = dict(c)
            c["text"] = "\n".join(lines)
            c["arbitrated_ocr_lines"] = masked
        out.append(c)
    return out


def _structured_max_chars(snippet_max_chars: int) -> int:
    value = getattr(settings, "RAG_SNIPPET_MAX_CHARS_STRUCTURED", None)
    return max(int(snippet_max_chars), int(value)) if value else int(snippet_max_chars)


def _prepare_selected_for_prompt(
    selected: list[dict[str, Any]],
    *,
    snippet_max_chars: int,
    table_max_chars: int | None = None,
    question: str | None = None,
    snippet_strategy: str = SNIPPET_STRATEGY_HEAD,
    count_table_marker: bool = False,
) -> list[dict[str, Any]]:
    """
    Make prompt generation resilient:
    - clamp large 'text' or 'snippet' fields so LLM doesn't 400 instantly (ctx overflow).
    - do not mutate original list in place.

    ``snippet_strategy`` choisit la FENÊTRE de l'extrait, jamais sa taille : ``head``
    (défaut) garde les N premiers caractères — comportement historique à l'octet près —
    et ``centered`` prend les MÊMES N caractères autour de la phrase la plus proche de
    ``question``. Le budget, l'ordre des sources et leur nombre sont inchangés.

    Seul ``text`` — le champ que `build_prompt` insère dans le prompt et que l'API
    projette en `snippet` — suit la stratégie. ``snippet`` et ``source.text`` restent
    coupés en tête : ce sont des champs de commodité, jamais la preuve citée, et c'est
    le périmètre exact qualifié par le banc SNIPPET-TRUNCATION-1A.

    ⚠️ Un chunk de CLASSEUR n'a pas le même gabarit qu'un paragraphe. L'extracteur Excel
    produit jusqu'à `DOCUMENT_EXCEL_CHUNK_MAX_CHARS` (4 000) par conception, et un en-tête
    hiérarchique (« Premoney Capitalization / Shares Outstanding » × 19 colonnes) en
    consomme à lui seul plus de 1 100. Le raboter à 1 200 comme un paragraphe ne laissait
    passer que l'EN-TÊTE et la première ligne : le modèle répondait « je ne sais pas » à
    des lectures dont la valeur était pourtant indexée. Les extraits tabulaires ont donc
    leur propre budget, et sont coupés sur une frontière de ligne.
    """
    table_max = snippet_max_chars if table_max_chars is None else table_max_chars
    centered = snippet_strategy == SNIPPET_STRATEGY_CENTERED
    out: list[dict[str, Any]] = []
    for c in _arbitrate_same_page_numbers(selected or []):
        if not isinstance(c, dict):
            continue
        cc = dict(c)
        tabular = _chunk_is_tabular(cc)
        structured = _chunk_is_structured_visual(cc)
        budget = max(snippet_max_chars, table_max) if tabular else snippet_max_chars

        # common shapes:
        # - cc["text"] (full chunk text)
        # - cc["snippet"] (short excerpt)
        if "text" in cc and isinstance(c.get("text"), str):
            # CONTEXT-GROUNDING-11 : le fragment RÉCUPÉRÉ entier reste attaché à SA source (même chunk_id,
            # même rang, donc même [n]) — seul le contrôle des absences le consulte ; le modèle, le contrôle
            # numérique et l'API ne voient que l'extrait (`text`, projections sur liste blanche).
            cc[RETRIEVED_TEXT_KEY] = c["text"]
        if "text" in cc:
            if structured:
                # Graphique / tableau structuré : budget dédié, unités logiques, couverture
                # partielle déclarée — jamais la fenêtre centrée de 800 c d'un paragraphe.
                cc["text"] = _select_structured_text(
                    cc.get("text"), question, _structured_max_chars(snippet_max_chars))
            elif tabular:
                # Contrat tabulaire INCHANGÉ : coupe sur frontière de ligne, budget
                # dédié, marqueur explicite. Une fenêtre centrée sur un tableau
                # produirait des colonnes orphelines lues comme des données.
                cc["text"] = _truncate_table_text(
                    cc.get("text"), budget, count_marker=count_table_marker
                )
            elif centered:
                cc["text"] = _anchor_on_designated_row(
                    _build_centered_snippet(cc.get("text"), question, budget), c.get("text"), question, budget)
            else:
                cc["text"] = _truncate_text(cc.get("text"), budget)
        if "snippet" in cc:
            cc["snippet"] = _truncate_text(cc.get("snippet"), snippet_max_chars)

        # sometimes nested source meta can contain long blobs; keep it light
        src = cc.get("source")
        if isinstance(src, dict):
            src2 = dict(src)
            if "text" in src2:
                src2["text"] = _truncate_text(src2.get("text"), snippet_max_chars)
            cc["source"] = src2

        out.append(cc)
    return out


def _min_final_source_chars() -> int:
    """Taille minimale utile de l'extrait de la DERNIÈRE source conservée.

    En dessous, l'extrait ne porte plus de phrase complète : la source resterait citable
    mais ne prouverait plus rien. Valeur mesurée, cf. `docs/03-backend/pipeline-retrieval.md`.
    """
    return int(getattr(settings, "RAG_MIN_FINAL_SOURCE_CHARS", 400))


def _truncate_source_text(
    chunk: dict[str, Any], room: int, *, count_table_marker: bool = False
) -> dict[str, Any]:
    """Copie de ``chunk`` dont l'extrait tient dans ``room`` caractères.

    Un extrait tabulaire est coupé sur une frontière de LIGNE (et le dit) ; un
    paragraphe est coupé au caractère. On ne renvoie jamais un extrait vide : une source
    sans texte est une citation sans preuve. ``count_table_marker`` : cf. C1.
    """
    out = dict(chunk)
    if _chunk_is_structured_visual(out):
        out["text"] = _select_structured_text(out.get("text"), None, max(1, int(room)))
    elif _chunk_is_tabular(out):
        out["text"] = _truncate_table_text(
            out.get("text"), max(1, int(room)), count_marker=count_table_marker
        )
    else:
        out["text"] = _truncate_text(out.get("text"), max(1, int(room)))
    return out


#: Vocabulaire FERMÉ de `prompt_limit_exception_reason` (compteurs V4).
LIMIT_EXCEPTION_MINIMUM_SOURCE = "minimum_source_protection"
LIMIT_EXCEPTION_UNACCOUNTED = "unaccounted_overflow"


def _render_conformance_enabled() -> bool:
    """Flag `RAG_PROMPT_RENDER_CONFORMANCE_V1_ENABLED` (défaut OFF = rendu historique)."""
    return bool(getattr(settings, "RAG_PROMPT_RENDER_CONFORMANCE_V1_ENABLED", False))


def _build_prompt_with_budget(
    *,
    question_for_prompt: str,
    selected: list[dict[str, Any]],
    template_content: str | None,
    max_prompt_chars: int,
    snippet_max_chars: int,
    table_max_chars: int | None = None,
    answer_language: str | None = None,
    conversation_context: str | None = None,
    min_sources: int = 0,
    question: str | None = None,
    snippet_strategy: str = SNIPPET_STRATEGY_HEAD,
    render_conformance: bool = False,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """Construit le prompt sous budget de caractères, SANS jamais le vider de ses sources.

    Trois étapes, dans cet ordre :
      1. borner les extraits (`_prepare_selected_for_prompt`) ;
      2. si le prompt dépasse encore, retirer les sources les MOINS prioritaires —
         mais **jamais la dernière** ;
      3. si la dernière source seule ne rentre pas, TRONQUER son extrait au lieu de la
         supprimer.

    Le défaut corrigé. La boucle s'arrêtait quand la liste était VIDE, pas quand elle
    était à un élément. Une question dont le contexte tenait en une seule grosse source
    produisait donc un prompt dont la section SOURCES était vide — sans `no_answer`, sans
    erreur, sans trace. Mesuré : deux questions sur cent après l'élargissement du gabarit
    de format. Le modèle répondait alors sans aucun ancrage documentaire, et aucune
    citation n'était possible.

    Le budget est un plafond sur le prompt ENTIER : consignes, sources et question
    puisent dans la même enveloppe. Une source tronquée reste citable et vérifiable ;
    une source absente ne prouve plus rien. Entre les deux, on tronque.

    ## La mémoire est une variable d'ajustement, pas un coût fixe

    La mémoire de conversation était injectée dans la question AVANT d'arriver ici.
    Elle entrait donc dans `overhead` — la part réputée intouchable — et les sources
    étaient la seule variable. Une conversation qui s'allonge évinçait donc les sources,
    silencieusement : mesuré le 23/08/2026 sur 25 fils réels, la mémoire médiane de
    staging (3 045 c) coûtait 3 sources sur 8, et un fil descendait à **zéro source**.

    L'inversion était mauvaise. Dans un assistant documentaire, les sources sont la
    substance : sans elles, plus de citation, plus de vérifiabilité. La mémoire est une
    aide à la résolution de références. On peut répondre sans se souvenir ; on ne peut
    pas prouver sans source.

    `min_sources` réserve donc la place de N sources AVANT d'allouer ce qui reste à la
    mémoire, qui est tronquée par la fin si nécessaire.

    ## Conformité du rendu (``render_conformance``, flag OFF par défaut)

    Trois effets, et seulement ceux-là : (C1) le marqueur de troncature tabulaire est
    compté dans la place allouée, donc le plafond n'est plus dépassé en silence ; (C2)
    date et page déjà portées par le libellé ne sont pas répétées dans l'en-tête ; (V4)
    la comptabilité expose ``rules_kind``, ``conditional_rules_chars``,
    ``source_context_chars``, ``prompt_total_chars``, ``prompt_limit_chars``,
    ``prompt_remaining_chars``, ``prompt_limit_exceeded`` et
    ``prompt_limit_exception_reason``. La politique du plus long préfixe, la protection
    de la dernière source et son minimum, les budgets et les règles sont INCHANGÉS. Les
    compteurs historiques gardent leur sens : ``selected_source_chars`` (= prompt −
    coût fixe) contient DÉJÀ les règles conditionnelles, et se décompose en
    ``conditional_rules_chars + source_context_chars``.
    """
    selected2 = _prepare_selected_for_prompt(
        selected,
        snippet_max_chars=snippet_max_chars,
        table_max_chars=table_max_chars,
        question=question,
        snippet_strategy=snippet_strategy,
        count_table_marker=render_conformance,
    )
    # Hors conformité, `build_prompt` est appelé avec sa signature historique : aucun
    # argument nouveau, donc aucun écart possible du rendu OFF.
    render_kwargs: dict[str, Any] = (
        {"dedup_header_metadata": True} if render_conformance else {}
    )

    def _render_with(question: str, chunks: list[dict[str, Any]]) -> str:
        return build_prompt(
            question, chunks,
            template_content=template_content, answer_language=answer_language,
            **render_kwargs,
        )

    # --- Arbitrage mémoire / sources -----------------------------------------
    # Tout est calculé sur le prompt RÉEL, jamais sur une constante : une constante
    # divergerait du texte au premier ajout de règle, et c'est exactement ce silence
    # qui a rendu la régression précédente invisible.
    cc_demande = (conversation_context or "").strip()
    cc_tronque = False
    cc_retenu = cc_demande
    if cc_demande and max_prompt_chars > 0 and min_sources > 0:
        sans_memoire = len(_render_with(question_for_prompt, []))
        # Coût réel des N premières sources, mesuré et non estimé.
        reserve = max(
            0,
            len(_render_with(question_for_prompt, selected2[:min_sources])) - sans_memoire,
        )
        # Le CADRE de l'injection (« uniquement pour résoudre les références… ») coûte
        # lui aussi des caractères. L'oublier faisait perdre une source de plus :
        # on le mesure au lieu de le supposer, avec un contenu d'un seul caractère.
        cadre = (
            len(_inject_conversation_context(question_for_prompt, "x",
                                             answer_language=answer_language))
            - len(question_for_prompt) - 1
        )
        allocation = int(max_prompt_chars) - sans_memoire - reserve - cadre
        cc_retenu, cc_tronque = _truncate_conversation_context(cc_demande, allocation)

    question_for_prompt = _inject_conversation_context(
        question_for_prompt, cc_retenu, answer_language=answer_language
    )

    def _render(chunks: list[dict[str, Any]]) -> str:
        return _render_with(question_for_prompt, chunks)

    # Coût FIXE du prompt : consignes + question (mémoire retenue comprise), sans
    # aucune source.
    overhead = len(_render([]))
    info: dict[str, Any] = {
        "conversation_context_requested_chars": len(cc_demande),
        "conversation_context_kept_chars": len(cc_retenu),
        "conversation_context_truncated": bool(cc_tronque),
        "min_sources_reserved": int(min_sources),
        "prompt_overhead_chars": overhead,
        "source_budget_chars": max(0, int(max_prompt_chars) - overhead) if max_prompt_chars > 0 else None,
        "sources_before_budget": len(selected2),
        "sources_after_budget": len(selected2),
        "sources_dropped_by_budget": 0,
        "final_source_truncated": False,
        "final_source_original_chars": None,
        "final_source_retained_chars": None,
        "prompt_budget_exhausted": False,
        "prompt_budget_unable_to_fit_minimum_source": False,
    }

    def _finish(prompt: str, chunks: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
        info["selected_source_chars"] = max(0, len(prompt) - overhead)
        if render_conformance:
            # Compteurs V4, calculés depuis les parties RÉELLEMENT rendues du prompt
            # final (jamais une constante) — nombres et vocabulaire fermé seulement.
            again, acc = render_prompt(
                question_for_prompt, chunks,
                template_content=template_content, answer_language=answer_language,
                dedup_header_metadata=True,
            )
            assert again == prompt  # le rendu est déterministe : même entrée, même prompt
            limit = int(max_prompt_chars) if max_prompt_chars > 0 else None
            total = len(prompt)
            exceeded = limit is not None and total > limit
            reason = None
            if exceeded:
                reason = (
                    LIMIT_EXCEPTION_MINIMUM_SOURCE
                    if info["prompt_budget_unable_to_fit_minimum_source"]
                    else LIMIT_EXCEPTION_UNACCOUNTED
                )
            info.update({
                "render_conformance_v1": True,
                "rules_kind": acc["rules_kind"],
                "conditional_rules_chars": acc["conditional_rules_chars"],
                "source_context_chars": acc["context_chars"],
                "headers_deduplicated": acc["headers_deduplicated"],
                "prompt_total_chars": total,
                "prompt_limit_chars": limit,
                "prompt_remaining_chars": (limit - total) if limit is not None else None,
                "prompt_limit_exceeded": bool(exceeded),
                "prompt_limit_exception_reason": reason,
            })
        return prompt, chunks, info

    prompt = _render(selected2)
    if max_prompt_chars <= 0 or len(prompt) <= max_prompt_chars:
        return _finish(prompt, selected2)

    info["prompt_budget_exhausted"] = True

    # Retrait des sources les moins prioritaires — l'ordre de priorité est préservé, et
    # la boucle s'arrête à UNE source, jamais zéro.
    trimmed = list(selected2)
    while len(trimmed) > 1 and len(prompt) > max_prompt_chars:
        trimmed.pop()
        prompt = _render(trimmed)
    info["sources_after_budget"] = len(trimmed)
    info["sources_dropped_by_budget"] = len(selected2) - len(trimmed)

    if len(prompt) <= max_prompt_chars or not trimmed:
        return _finish(prompt, trimmed)

    # Une seule source reste et elle ne rentre pas : on TRONQUE son extrait.
    last = trimmed[0]
    original_chars = len(str(last.get("text") or ""))
    # Coût STRUCTUREL du bloc : en-tête, libellé, page, règles conditionnelles
    # (tableau/classeur) — tout sauf le texte. Mesuré, pas estimé.
    structural = len(_render([{**last, "text": ""}]))
    room = int(max_prompt_chars) - structural
    minimum = _min_final_source_chars()

    if room < minimum:
        # Même l'extrait minimal ne tient pas. On CONSERVE quand même la source, avec
        # cet extrait minimal, et on le DIT : dépasser un plafond de caractères de
        # quelques centaines est moins grave que de rendre une réponse sans aucune
        # preuve. L'état est explicite, jamais silencieux.
        info["prompt_budget_unable_to_fit_minimum_source"] = True
        room = minimum

    truncated = _truncate_source_text(last, room, count_table_marker=render_conformance)
    prompt = _render([truncated])
    info["final_source_truncated"] = True
    info["final_source_original_chars"] = original_chars
    info["final_source_retained_chars"] = len(str(truncated.get("text") or ""))
    return _finish(prompt, [truncated])


# Preamble « explain » : autorise une réponse PARTIELLE ancrée aux sources quand le
# sujet n'est couvert que partiellement (évite le refus abusif « Je ne sais pas » des
# modèles stricts type mistral), tout en gardant le garde-fou anti-hallucination
# (n'invente rien, ne complète pas avec des connaissances générales hors sources).
DEFAULT_EXPLAIN_PREAMBLE = (
    "Donne une réponse détaillée et structurée, en t'appuyant sur les sources. "
    "Si les sources ne couvrent le sujet que partiellement, réponds tout de même avec "
    "les éléments présents DANS LES SOURCES et indique explicitement ce qui manque. "
    "N'invente rien et ne complète pas avec des connaissances générales hors sources. "
    "Ne réponds « Je ne sais pas » que si AUCUNE source n'est pertinente.\n"
    "Structure attendue:\n"
    "1) Contexte / objectif\n"
    "2) Points clés\n"
    "3) Impacts / acteurs concernés\n"
    "4) Synthèse courte\n\n"
)


# Préambule « compare » : gabarit des questions COMPARATIVES (différences, points communs,
# avantages/inconvénients). Le gabarit factuel par défaut (« réponse courte et directe », « si
# tu n'es pas sûr : Je ne sais pas ») faisait abandonner les modèles stricts sur ce type de
# question, qui demande de croiser plusieurs documents. Ici, on cadre la SYNTHÈSE sans lever
# le garde-fou : ce qui manque doit être dit, jamais comblé.
#
# La consigne de concision absolue a depuis disparu du gabarit (30/07/2026, cf.
# `domain/retrieval.py`), mais ce préambule reste nécessaire : il apporte la STRUCTURE
# comparative, qu'une consigne de longueur ne fournit pas.
DEFAULT_COMPARE_PREAMBLE = (
    "La question demande une COMPARAISON. Réponds en t'appuyant EXCLUSIVEMENT sur les sources.\n"
    "Règles impératives:\n"
    "- Identifie d'abord les dimensions réellement couvertes par les sources ; ne compare que "
    "celles-là.\n"
    "- Compare point par point, en distinguant explicitement ressemblances et différences.\n"
    "- Signale ce qui manque : dimension non documentée, entité absente des sources.\n"
    "- Si les sources ne couvrent qu'UNE des entités comparées, dis-le et réponds "
    "partiellement — ne construis JAMAIS une comparaison artificielle.\n"
    "- N'invente rien et ne complète pas avec des connaissances générales hors sources.\n"
    "- Place les références [n] au niveau de chaque affirmation concernée.\n"
    "- Ne réponds « Je ne sais pas » que si AUCUNE source n'est pertinente.\n"
    "- Développe chaque point : une comparaison réduite à deux puces n'en est pas une.\n"
    "Structure attendue (un tableau est possible si les données s'y prêtent):\n"
    "1) Objets comparés et dimensions couvertes\n"
    "2) Points communs\n"
    "3) Principales différences\n"
    "4) Conséquences / impacts de ces différences, s'ils ressortent des sources\n"
    "5) Éléments disponibles uniquement pour une partie\n"
    "6) Limites des sources\n"
    "7) Synthèse\n\n"
)


# ---------------------------------------------------------------------------
# Variantes ANGLAISES des préambules (P0 — politique de langue de réponse).
# Miroir des préambules français ci-dessus / dans build_prompt_stage. Utilisées
# UNIQUEMENT quand la cible résolue est « en » ; sinon, le chemin français
# historique est conservé À L'OCTET PRÈS. Les préambules ne sont plus lus depuis
# les settings : cinq d'entre eux semblaient surchargeables (`RAG_*_PREAMBLE`,
# `RAG_NEWS_PROMPT_HINT`) alors qu'aucun settings ne projetait ces noms.
# ---------------------------------------------------------------------------
DEFAULT_EXPLAIN_PREAMBLE_EN = (
    "Give a detailed, structured answer grounded in the sources. "
    "If the sources cover the topic only partially, still answer with what is present "
    "IN THE SOURCES and state explicitly what is missing. "
    "Do not invent anything and do not fill gaps with general knowledge outside the sources. "
    "Answer \"I don't know\" only if NO source is relevant.\n"
    "Expected structure:\n"
    "1) Context / objective\n"
    "2) Key points\n"
    "3) Impacts / stakeholders\n"
    "4) Short summary\n\n"
)

DEFAULT_COMPARE_PREAMBLE_EN = (
    "The question asks for a COMPARISON. Answer using the sources EXCLUSIVELY.\n"
    "Mandatory rules:\n"
    "- First identify the dimensions actually covered by the sources; compare only those.\n"
    "- Compare point by point, explicitly separating similarities and differences.\n"
    "- State what is missing: undocumented dimension, entity absent from the sources.\n"
    "- If the sources cover only ONE of the compared entities, say so and answer partially — "
    "NEVER build an artificial comparison.\n"
    "- Do not invent anything and do not fill gaps with general knowledge outside the sources.\n"
    "- Put the [n] references next to each claim they support.\n"
    "- Answer \"I don't know\" only if NO source is relevant.\n"
    "- Develop each point: a comparison reduced to two bullets is not a comparison.\n"
    "Expected structure (a table is fine when the data suits it):\n"
    "1) Objects compared and dimensions covered\n"
    "2) Common ground\n"
    "3) Main differences\n"
    "4) Consequences / impacts of those differences, where the sources show them\n"
    "5) Available for one side only\n"
    "6) Limits of the sources\n"
    "7) Summary\n\n"
)

LOWCONF_PREAMBLE_EN = (
    "Using the excerpts below, answer even if the information is partial.\n"
    "Mandatory rules:\n"
    "- Do not invent anything.\n"
    "- Cite only what is explicitly present in the excerpts.\n"
    "- Ignore off-topic excerpts.\n"
    "- If a piece of information is incomplete, state it clearly.\n\n"
    "Expected structure:\n"
    "1) Relevant information found\n"
    "2) What the sources say precisely (with references)\n"
    "3) Limits of the available information\n\n"
)

PARTIAL_PREAMBLE_EN = (
    "Answer with the information available in the sources below. "
    "If the sources cover only part of the topic, clearly explain the limits "
    "and provide a structured summary.\n\n"
    "Expected structure:\n"
    "1) Context / objective\n"
    "2) Key points\n"
    "3) Impacts / stakeholders\n"
    "4) Uncertainties / what is missing from the documents\n\n"
)

NEWS_HINT_EN = (
    "Important: the user is asking for news. "
    "Prioritise the most recent excerpts based on the document date (if provided). "
    "Do not infer the date from the file name: use only the date extracted from the content.\n\n"
)


def _truncate_conversation_context(cc: str, allowance: int) -> tuple[str, bool]:
    """Réduit la mémoire à ce que le budget lui laisse, en gardant la FIN.

    La fin est le plus récent, donc le plus utile pour résoudre une référence — c'est
    déjà la politique de `get_thread_memory_context`. Couper le début est cohérent avec
    elle ; couper la fin retirerait précisément ce à quoi la question fait référence.

    Rend ``(texte, tronqué)``. Une allocation nulle rend une mémoire vide : mieux vaut
    aucune mémoire que des sources absentes — un assistant documentaire qui perd ses
    sources ne peut plus rien prouver, alors qu'il peut répondre sans se souvenir.
    """
    cc = (cc or "").strip()
    if not cc or allowance >= len(cc):
        return cc, False
    if allowance <= 0:
        return "", True
    return "…" + cc[-(allowance - 1):], True


def _inject_conversation_context(
    q_for_prompt: str, conversation_context: str | None, *, answer_language: str | None = None
) -> str:
    """Ajoute la mémoire de conversation au prompt SANS diluer l'ancrage aux sources.

    La mémoire est placée APRÈS la question courante et cadrée explicitement : elle
    ne sert QU'À résoudre les références (« même question », « et pour X »). Le modèle
    ne doit ni répondre aux échanges précédents ni les imiter — cela évite le
    sur-refus / l'anchoring observé quand la mémoire était injectée EN TÊTE (les
    modèles stricts type mistral répondaient « Je ne sais pas. » malgré des sources
    pertinentes). Sans mémoire, renvoie le prompt inchangé.
    """
    cc = (conversation_context or "").strip()
    if not cc:
        return q_for_prompt
    if answer_language == "en":
        return (
            f"{q_for_prompt}\n\n"
            "Prior conversation context — ONLY to resolve references (\"same question\", "
            "\"and for X\"). Do NOT answer those exchanges and do NOT use them as a "
            "response template: answer the question above from the SOURCES.\n"
            f"{cc}"
        )
    return (
        f"{q_for_prompt}\n\n"
        "Contexte de conversation antérieure — UNIQUEMENT pour résoudre les "
        "références (« même question », « et pour X »). Ne réponds PAS à ces échanges "
        "et ne t'en sers PAS comme modèle de réponse : réponds à la question ci-dessus "
        "à partir des SOURCES.\n"
        f"{cc}"
    )


# ---------------------------------------------------------------------------
# Composant principal
# ---------------------------------------------------------------------------
def build_prompt_stage(
    ctx: QueryExecutionContext,
    *,
    selected: list[dict[str, Any]],
    low_confidence: bool,
    strict_refusal: bool,
) -> PromptBuildResult:
    """Sélectionne le préambule, injecte la mémoire, applique le budget.

    ``low_confidence`` / ``strict_refusal`` sont DÉCIDÉS en amont (answerability, qui
    reste dans l'orchestrateur). Le mode vient des DTO threadés.
    """
    request = ctx.request
    prepared = ctx.prepared
    assert prepared is not None

    question = request.question
    rag_space = request.rag_space
    conversation_context = request.conversation_context
    explanatory_mode = prepared.explanatory
    comparative_mode = getattr(prepared, "comparative", False)
    news_mode = prepared.news

    template_content = None
    if rag_space and getattr(rag_space, "prompt_template_id", None):
        template_content = rag_space.prompt_template.content

    q_for_prompt = question
    policy_label = "ok"

    # Cible de langue résolue en amont (chat_service) et portée par le DTO. ``None``
    # (flag OFF / fr) ⇒ chemin français historique inchangé À L'OCTET PRÈS.
    answer_language = getattr(request, "answer_language", None)
    en = answer_language == "en"

    if low_confidence:
        policy_label = "low_confidence"
        if en:
            lowconf_preamble = LOWCONF_PREAMBLE_EN.strip()
        else:
            lowconf_preamble = (
                "À partir des extraits ci-dessous, réponds même si l'information est partielle.\n"
                "Règles impératives:\n"
                "- N'invente rien.\n"
                "- Cite uniquement ce qui est explicitement présent dans les extraits.\n"
                "- Ignore les extraits hors sujet.\n"
                "- Si une information est incomplète, précise-le clairement.\n\n"
                "Structure attendue:\n"
                "1) Informations pertinentes trouvées\n"
                "2) Ce que disent précisément les sources (avec références)\n"
                "3) Limites des informations disponibles\n\n"
                ).strip()
        q_for_prompt = f"{lowconf_preamble}Question: {question}"

    elif strict_refusal:
        policy_label = "partial"
        if en:
            partial_preamble = PARTIAL_PREAMBLE_EN.strip()
        else:
            partial_preamble = (
                "Réponds avec les informations disponibles dans les sources ci-dessous. "
                "Si les sources ne couvrent qu'une partie du sujet, explique clairement les limites "
                "et propose une synthèse structurée.\n\n"
                "Structure attendue:\n"
                "1) Contexte / objectif\n"
                "2) Points clés\n"
                "3) Impacts / acteurs concernés\n"
                "4) Zones d'incertitude / ce qui manque dans les documents\n\n"
                ).strip()
        q_for_prompt = f"{partial_preamble}\nQuestion: {question}"

    elif comparative_mode:
        # Précédence : après `low_confidence` et `partial` (qui traduisent l'état des PREUVES,
        # plus contraignant que la forme de la question), avant `explain` (« compare X et Y en
        # détail » est d'abord une comparaison).
        policy_label = "compare"
        if en:
            compare_preamble = DEFAULT_COMPARE_PREAMBLE_EN.strip()
        else:
            # Le préambule n'est PAS surchargeable par configuration. Il l'a
            # semblé (`getattr(settings, "RAG_COMPARE_PROMPT_PREAMBLE", …)`) sans
            # qu'aucun settings ne projette ce nom : la surcharge n'a jamais eu
            # lieu. Et c'est heureux — un préambule de prompt porte les gardes
            # anti-hallucination ; le remplacer par variable d'environnement, sans
            # revue ni mesure, est le genre de levier qu'on ne veut pas exposer.
            compare_preamble = DEFAULT_COMPARE_PREAMBLE.strip()
        q_for_prompt = f"{compare_preamble}\nQuestion: {question}"

    elif explanatory_mode:
        policy_label = "explain"
        if en:
            explain_preamble = DEFAULT_EXPLAIN_PREAMBLE_EN.strip()
        else:
            # Même raison que pour le préambule comparatif : constante, pas réglage.
            explain_preamble = DEFAULT_EXPLAIN_PREAMBLE.strip()
        q_for_prompt = f"{explain_preamble}\nQuestion: {question}"

    if news_mode:
        if en:
            news_hint = NEWS_HINT_EN.strip()
        else:
            news_hint = (
                "Important: l'utilisateur demande des actualités. "
                "Priorise les extraits les plus récents d'après la date du document (si fournie). "
                "Ne déduis pas la date depuis le nom de fichier: utilise seulement la date extraite du contenu.\n\n"
                ).strip()
        q_for_prompt = f"{news_hint}{q_for_prompt}"

    # -----------------------------------------------------------------
    # Conversation memory (prompt-only)
    # - Important: do NOT change retrieval/search question
    # - Only enrich what the LLM sees when generating the final answer
    # -----------------------------------------------------------------
    # L'injection de la mémoire a lieu DANS l'allocateur, pas ici : elle doit pouvoir
    # être tronquée si elle empiète sur la place réservée aux sources. L'injecter avant
    # la ferait entrer dans le coût réputé fixe, et les sources redeviendraient la seule
    # variable d'ajustement — le défaut que ce chemin corrige.

    # ---- Prompt budgets (prevents Ollama fast 400 -> RagQueryLog.ERROR_STAGE_LLM) ----
    max_prompt_chars = int(getattr(settings, "RAG_MAX_PROMPT_CHARS", 18_000))
    min_prompt_sources = int(getattr(settings, "RAG_MIN_PROMPT_SOURCES", 4))
    snippet_max_chars = int(getattr(settings, "RAG_SNIPPET_MAX_CHARS", 1_200))
    # Un extrait de CLASSEUR est un tableau, pas un paragraphe : il a son propre gabarit.
    table_max_chars = int(getattr(settings, "RAG_SNIPPET_MAX_CHARS_TABLE", 4_000))

    # Fiabilité des preuves numériques (OCR-NUMERIC-CAVEAT-1) : statut porté par chaque
    # source jusqu'au prompt et à la réponse ; aucune source n'est retirée ni reclassée.
    from apps.rag_core.services.query.numeric_evidence import annotate_numeric_evidence

    selected = annotate_numeric_evidence(selected)

    prompt_text, selected, budget_info = _build_prompt_with_budget(
        question_for_prompt=q_for_prompt,
        selected=selected,
        template_content=template_content,
        max_prompt_chars=max_prompt_chars,
        snippet_max_chars=snippet_max_chars,
        table_max_chars=table_max_chars,
        answer_language=answer_language,
        conversation_context=conversation_context,
        min_sources=min_prompt_sources,
        # La question BRUTE, jamais `q_for_prompt` : celui-ci porte le préambule de
        # politique, l'indice « actualités » et la mémoire de conversation. Apparier
        # l'extrait sur ce texte-là centrerait la fenêtre sur les mots des CONSIGNES
        # (« sources », « réponse », « structure ») et sur les échanges précédents.
        question=question,
        snippet_strategy=_snippet_strategy(),
        render_conformance=_render_conformance_enabled(),
    )
    return PromptBuildResult(
        prompt=prompt_text,
        sources=selected,
        answer_kind=policy_label,
        preamble_kind=policy_label,
        budget=budget_info,
    )
