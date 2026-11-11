# backend/apps/rag_core/services/ingest/extraction/excel_render.py
from __future__ import annotations

"""
Rendu d'un bloc Excel en chunks indexables. Module PUR.

Deux représentations :

- **Markdown** (par défaut) — le LLM le lit bien, le frontend le rend déjà, et
  l'en-tête n'apparaît qu'une fois par chunk (au lieu d'une fois par ligne) ;
- **`col: valeur` par ligne** (repli automatique) au-delà de
  `DOCUMENT_EXCEL_MAX_COLUMNS_MARKDOWN` colonnes : un tableau Markdown à 60 colonnes
  est illisible et sature le budget de caractères dès la première ligne. Cette
  représentation est déjà éprouvée — c'est celle de l'extracteur CSV.

**Chevauchement = 0, délibérément.** Le chevauchement textuel existe pour ne pas
couper une phrase en deux ; une ligne de tableau n'est JAMAIS coupée (c'est tout
l'intérêt du fenêtrage). Il n'apporterait donc presque rien, et il coûterait deux
fois : la même ligne indexée deux fois (budget de contexte gaspillé), et surtout
**tout comptage faussé** — « combien de projets ? » devient trompeur si le modèle voit
deux chunks qui se recouvrent.

Le contexte perdu à la frontière d'un chunk est récupéré autrement, et mieux : le
**titre**, les **en-têtes** et la ligne « Feuille : … » sont répétés dans CHAQUE chunk.
"""

from typing import List

from .excel_blocks import BLOCK_NARRATIVE, Block
from .excel_workbook import col_letter


def _md_escape(s: str) -> str:
    """Un `|` dans une cellule casserait la table Markdown."""
    return s.replace("|", "\\|").replace("\n", " ")


def cell_range(block: Block, row_start: int, row_end: int) -> str:
    """Plage Excel EXACTE des lignes couvertes par un chunk (`A12:F28`)."""
    return (
        f"{col_letter(block.col_start)}{row_start}:"
        f"{col_letter(block.col_end)}{row_end}"
    )


def context_line(sheet_name: str, block: Block, rng: str) -> str:
    """Ligne de contexte, INDEXÉE (pas seulement en méta).

    C'est elle qui rend le chunk retrouvable sur « la feuille Budget 2026 » alors
    qu'aucune cellule ne contient ces mots.
    """
    head = f"Feuille : {sheet_name}"
    if block.title:
        head += f" — {block.title}"
    return f"{head} ({rng})"


def _markdown_header(headers: List[str], numeric_cols: List[bool]) -> List[str]:
    cols = " | ".join(_md_escape(h) for h in headers)
    sep = " | ".join("---:" if n else "---" for n in numeric_cols)
    return [f"| {cols} |", f"| {sep} |"]


def _row_md(row: List[str]) -> str:
    return "| " + " | ".join(_md_escape(c) for c in row) + " |"


def _row_kv(headers: List[str], row: List[str]) -> str:
    """Repli `col: valeur ; col: valeur` — auto-descriptif, comme l'extracteur CSV."""
    parts = [
        f"{headers[i] if i < len(headers) else f'col{i + 1}'}: {v}"
        for i, v in enumerate(row)
        if v
    ]
    return " ; ".join(parts)


def _numeric_columns(block: Block) -> List[bool]:
    """Colonnes majoritairement numériques -> alignées à droite en Markdown."""
    n = len(block.headers)
    if not block.rows:
        return [False] * n
    out = []
    sample = block.rows[:50]
    for i in range(n):
        vals = [r[i] for r in sample if i < len(r) and r[i]]
        if not vals:
            out.append(False)
            continue
        num = sum(1 for v in vals if _looks_numeric(v))
        out.append(num >= len(vals) * 0.6)
    return out


def _looks_numeric(v: str) -> bool:
    s = v.strip().rstrip("%")
    if not s:
        return False
    try:
        float(s)
        return True
    except ValueError:
        return False


# --- Plafond strict : budget & marge ------------------------------------
#
# Le critère final EST ``len(chunk.text) <= chunk_max_chars`` pour 100 % des chunks.
# On fenêtre contre une CIBLE inférieure (``chunk_max_chars - SAFETY_MARGIN``) pour
# absorber le coût du rendu final (séparateurs Markdown, ``"\n".join``, ``.strip()``,
# arrondi d'estimation d'overhead) ; un GARANT terminal (`_finalize_chunks`) rattrape
# tout résidu par un découpage étiqueté. La marge est petite mais suffisante : un chunk
# ajoute au plus ~1 séparateur par ligne, et l'overhead est déjà compté ligne à ligne.
SAFETY_MARGIN = 120

#: Plancher de budget de corps : en deçà, un préfixe démesuré rendrait le fenêtrage
#: absurde — on bascule alors sur un découpage caractères pur (dernier recours).
MIN_BODY_CHARS = 200


def _wrap_segment(text: str, budget: int) -> List[str]:
    """Découpe UN segment en morceaux ``<= budget``. Dernier recours, sans perte.

    Frontières préférées, dans l'ordre : une espace proche de la borne, sinon coupe
    dure au caractère. Aucun caractère n'est supprimé (hors espaces de jointure, que la
    normalisation retrieval réunit de toute façon). Jamais ``texte[:budget]`` seul.
    """
    budget = max(1, budget)
    out: List[str] = []
    s = text
    while len(s) > budget:
        cut = s.rfind(" ", 0, budget)
        if cut <= budget // 2:          # pas d'espace exploitable -> coupe dure
            cut = budget
        head = s[:cut].rstrip()
        if head:
            out.append(head)
        s = s[cut:].lstrip()
    if s:
        out.append(s)
    return out or [""]


def _hard_wrap_chunk(ch: dict, max_chars: int) -> List[dict]:
    """GARANT terminal : redécoupe un chunk encore trop grand, sans perte ni silence.

    Découpe sur les frontières de LIGNES (les lignes de tableau/contexte restent
    entières), la 1re ligne (contexte) étant répétée en préfixe. Une ligne isolée
    encore trop longue passe par ``_wrap_segment``. Étiquetage ``overflow_part_*``.
    Garantit ``len(text) <= max_chars`` par construction.
    """
    text = ch["text"]
    lines = text.split("\n")
    prefix = lines[0] if lines else ""
    rest = lines[1:]
    avail = max_chars - len(prefix) - 1

    if avail < MIN_BODY_CHARS:
        # Préfixe démesuré : on ne peut pas le répéter. Découpage caractères pur.
        pieces = _wrap_segment(text, max_chars)
        return [{"text": p,
                 "meta": {**ch["meta"], "overflow_part_index": i, "overflow_part_count": len(pieces)}}
                for i, p in enumerate(pieces, 1)]

    parts: List[List[str]] = []
    cur: List[str] = []
    cur_len = 0
    for ln in rest:
        for piece in _wrap_segment(ln, avail):
            add = len(piece) + 1
            if cur and cur_len + add > avail:
                parts.append(cur)
                cur, cur_len = [], 0
            cur.append(piece)
            cur_len += add
    if cur:
        parts.append(cur)
    if not parts:
        parts = [[""]]

    out: List[dict] = []
    total = len(parts)
    for i, seg in enumerate(parts, 1):
        body = "\n".join(seg)
        out.append({
            "text": f"{prefix}\n{body}" if prefix else body,
            "meta": {**ch["meta"], "overflow_part_index": i, "overflow_part_count": total},
        })
    return out


def _finalize_chunks(chunks: List[dict], warnings: List[str], max_chars: int) -> tuple:
    """Point de sortie UNIQUE de ``render_block`` : plafond strict + pas de chunk vide.

    Chaque branche de rendu passe ici. Invariant garanti en sortie :
    ``all(0 < len(c["text"]) <= max_chars for c in chunks)``.
    """
    out: List[dict] = []
    hard = False
    for ch in chunks:
        if not (ch.get("text") or "").strip():
            continue                                   # aucun chunk vide
        if len(ch["text"]) <= max_chars:
            out.append(ch)
        else:
            hard = True
            out.extend(_hard_wrap_chunk(ch, max_chars))
    if hard and "excel_block_hard_cap_split" not in warnings:
        warnings.append("excel_block_hard_cap_split")
    return out, warnings


def _render_narrative(
    block: Block, *, sheet_name: str, sheet_index: int, sheet_hidden: bool,
    sheet_state: str, chunk_max_chars: int, has_formulas: bool,
) -> tuple:
    """Rend un bloc NARRATIF en respectant le plafond (défaut historique : 1 seul chunk).

    Découpe par lignes SOURCES (chaque ligne Excel = une unité), puis par
    phrases/caractères si une ligne seule dépasse. Chaque part restreint sa plage aux
    lignes réellement couvertes et porte ``narrative_part_index/part_count``.
    """
    warnings: List[str] = []
    row_texts: List[tuple] = []
    for i, r in enumerate(block.rows):
        line = " ".join(c for c in r if c).strip()
        if line:
            row_texts.append((block.row_start + i, line))
    if not row_texts:
        return [], warnings

    full_rng = cell_range(block, block.row_start, block.row_end)
    reserve = len(context_line(sheet_name, block, full_rng)) + 4     # + "\n\n"
    body_budget = max(MIN_BODY_CHARS, chunk_max_chars - reserve - SAFETY_MARGIN)

    parts: List[tuple] = []          # (row_lo, row_hi, body)
    cur: List[tuple] = []            # [(excel_row, piece)]
    cur_len = 0

    def flush() -> None:
        nonlocal cur, cur_len
        if cur:
            parts.append((cur[0][0], cur[-1][0], "\n".join(p for _, p in cur)))
            cur, cur_len = [], 0

    for excel_row, line in row_texts:
        pieces = _wrap_segment(line, body_budget) if len(line) > body_budget else [line]
        if len(pieces) > 1:
            warnings.append("excel_narrative_split")
        for piece in pieces:
            add = len(piece) + 1
            if cur and cur_len + add > body_budget:
                flush()
            cur.append((excel_row, piece))
            cur_len += add
    flush()

    if len(parts) > 1 and "excel_narrative_split" not in warnings:
        warnings.append("excel_narrative_split")

    total = len(parts)
    chunks: List[dict] = []
    for idx, (lo, hi, body) in enumerate(parts, 1):
        rng = cell_range(block, lo, hi)
        meta = _meta(block, sheet_name, sheet_index, sheet_hidden, rng, lo, hi,
                     idx, total, has_formulas, kind="excel_narrative", sheet_state=sheet_state)
        meta["narrative_part_index"] = idx
        meta["narrative_part_count"] = total
        chunks.append({"text": f"{context_line(sheet_name, block, rng)}\n\n{body}", "meta": meta})
    return chunks, warnings


def _split_wide_row(
    block: Block, local_idx: int, *, sheet_name: str, sheet_index: int,
    sheet_hidden: bool, sheet_state: str, chunk_max_chars: int, has_formulas: bool,
    max_columns_per_chunk: int,
) -> tuple:
    """Découpe UNE ligne trop large en fenêtres de COLONNES (valeurs liées à leur en-tête).

    Chaque sous-chunk : la colonne d'identification (si détectée) + une fenêtre contiguë
    de colonnes, rendue en petite table Markdown (en-tête + la ligne). Plages EXACTES
    (`AA3:AZ3`), ``column_start/end``, ``column_part_index/part_count``. Fonctionne aussi
    bien avec des en-têtes fiables qu'avec ``col1..colN``.
    """
    from .excel_columns import detect_identity_columns

    warnings: List[str] = []
    excel_row = block.data_row_start + local_idx
    row = block.rows[local_idx]
    n = len(block.headers)

    def val(i: int) -> str:
        return row[i] if i < len(row) else ""

    numeric_all = [_column_is_numeric(block, i) for i in range(n)]
    idset = detect_identity_columns(block.headers, block.rows)
    id_idx = idset[0] if idset else None
    target = chunk_max_chars - SAFETY_MARGIN

    def build(wcols: List[int], part_index: int, part_count: int) -> dict:
        cols = ([id_idx] if (id_idx is not None and id_idx not in wcols) else []) + list(wcols)
        headers_sub = [block.headers[c] for c in cols]
        numeric_sub = [numeric_all[c] for c in cols]
        main_range = _range_of(block, tuple(wcols), excel_row, excel_row)

        head = f"Feuille : {sheet_name}"
        if block.title:
            head += f" — {block.title}"
        lines = [f"{head} ({main_range})"]
        id_range = None
        if id_idx is not None and id_idx not in wcols:
            id_range = _range_of(block, (id_idx,), excel_row, excel_row)
            lines.append(f"Identifiants : {id_range}")
        lines.append("")
        lines += _markdown_header(headers_sub, numeric_sub)
        lines.append(_row_md([val(c) for c in cols]))

        meta = _meta(block, sheet_name, sheet_index, sheet_hidden, main_range,
                     excel_row, excel_row, part_index, part_count, has_formulas,
                     sheet_state=sheet_state)
        meta["headers"] = list(headers_sub)
        meta["column_start"] = _col_ref(block, wcols[0])
        meta["column_end"] = _col_ref(block, wcols[-1])
        meta["chunk_axis"] = "columns"
        meta["wide_table"] = True
        meta["column_part_index"] = part_index
        meta["column_part_count"] = part_count
        meta["identity_columns"] = [_col_ref(block, id_idx)] if id_range else []
        meta["identity_range"] = id_range
        meta["data_range"] = main_range
        meta["data_ranges"] = [r for r in (id_range, main_range) if r]
        return {"text": "\n".join(lines).strip(), "meta": meta}

    # Empaquetage glouton sur la taille RÉELLEMENT rendue (préfixe + en-tête + séparateur
    # + valeur inclus) : plus fiable qu'une estimation. Une fenêtre d'une seule colonne
    # qui dépasse encore (cellule géante) est laissée au garant terminal.
    data_cols = [i for i in range(n) if i != id_idx]
    windows: List[List[int]] = []
    cur: List[int] = []
    for i in data_cols:
        trial = cur + [i]
        if cur and len(build(trial, 1, 1)["text"]) > target:
            windows.append(cur)
            cur = [i]
        else:
            cur = trial
    if cur:
        windows.append(cur)
    if not windows:
        windows = [[id_idx if id_idx is not None else 0]]

    if len(windows) > 1:
        warnings.append("excel_wide_row_split")

    total = len(windows)
    chunks = [build(wcols, k, total) for k, wcols in enumerate(windows, 1)]
    return chunks, warnings


def render_block(
    block: Block,
    *,
    sheet_name: str,
    sheet_index: int,
    sheet_hidden: bool,
    sheet_state: str = "visible",
    rows_per_chunk: int,
    chunk_max_chars: int,
    max_columns_markdown: int,
    has_formulas: bool,
    wide_threshold: int = 0,
    max_columns_per_chunk: int = 8,
    max_chunks: int = 500,
) -> tuple:
    """Rend un bloc en une liste de chunks. Renvoie ``(chunks, warnings)``.

    Chaque chunk : ``{"text": str, "meta": dict}``. Une ligne Excel n'apparaît que
    dans **un seul** chunk (aucun chevauchement). Invariant de sortie garanti par
    ``_finalize_chunks`` : ``len(text) <= chunk_max_chars`` pour 100 % des chunks.
    """
    warnings: List[str] = []

    # --- Bloc narratif : de la prose, pas un tableau ---------------------
    if block.kind == BLOCK_NARRATIVE:
        chunks, w = _render_narrative(
            block, sheet_name=sheet_name, sheet_index=sheet_index,
            sheet_hidden=sheet_hidden, sheet_state=sheet_state,
            chunk_max_chars=chunk_max_chars, has_formulas=has_formulas,
        )
        return _finalize_chunks(chunks, w, chunk_max_chars)

    if not block.rows:
        return [], warnings

    # --- Tableau LARGE à en-têtes fiables : découpage bidimensionnel ------
    #
    # C'est la correction centrale. Sans elle, les 44 intitulés d'une feuille de
    # capitalisation étaient répétés sur chacun de ses 50 chunks : tous devenaient
    # lexicalement interchangeables, `ts_rank` les mettait à ÉGALITÉ PARFAITE, et le
    # chunk qui portait la réponse sortait dernier. Aucun classement ne peut départager
    # une égalité parfaite — il fallait cesser de rendre les chunks identiques.
    #
    # On n'y touche que si les en-têtes sont FIABLES : sans eux, il n'y a ni groupe
    # sémantique ni colonne d'identification à honorer, et découper à l'aveugle
    # séparerait des colonnes dont on ignore le lien.
    if (
        wide_threshold
        and len(block.headers) > wide_threshold
        and not block.headers_inferred
    ):
        chunks, w = _render_wide_block(
            block,
            sheet_name=sheet_name, sheet_index=sheet_index,
            sheet_hidden=sheet_hidden, sheet_state=sheet_state,
            rows_per_chunk=rows_per_chunk, chunk_max_chars=chunk_max_chars,
            max_columns_per_chunk=max_columns_per_chunk,
            max_chunks=max_chunks, has_formulas=has_formulas,
        )
        return _finalize_chunks(chunks, w, chunk_max_chars)

    wide = len(block.headers) > max_columns_markdown
    if wide:
        warnings.append("excel_wide_table_fallback")

    numeric = _numeric_columns(block) if not wide else []

    # Ligne Excel de la 1re ligne de DONNÉES. Elle se DÉDUIT des lignes d'en-tête
    # réellement détectées : un tableau financier en empile couramment deux, et un
    # `+ 1` codé en dur décalait alors toutes les citations d'un cran.
    data_row0 = block.data_row_start

    # --- Fenêtrage : la borne en CARACTÈRES prime sur la borne en LIGNES --
    windows: List[tuple] = []          # (index de départ, nb de lignes)
    i = 0
    n = len(block.rows)
    while i < n:
        # En-tête + contexte sont répétés dans chaque chunk : ils comptent dans le budget.
        rng_guess = cell_range(block, data_row0 + i, data_row0 + i)
        overhead = len(context_line(sheet_name, block, rng_guess)) + 2
        if not wide:
            overhead += sum(len(x) + 3 for x in _markdown_header(block.headers, numeric))

        used = overhead
        take = 0
        while i + take < n and take < rows_per_chunk:
            row = block.rows[i + take]
            line = _row_md(row) if not wide else _row_kv(block.headers, row)
            if take > 0 and used + len(line) + 1 > chunk_max_chars:
                break
            used += len(line) + 1
            take += 1
        # Garde-fou : une ligne unique qui dépasse déjà le budget est CONSERVÉE
        # ENTIÈRE (jamais coupée en son milieu) — les cellules ont déjà été
        # tronquées en amont par DOCUMENT_EXCEL_MAX_CELL_CHARS.
        take = max(1, take)
        windows.append((i, take))
        i += take

    chunks = []
    total = len(windows)
    for k, (start, take) in enumerate(windows, start=1):
        rows = block.rows[start:start + take]
        r0 = data_row0 + start
        r1 = r0 + take - 1
        rng = cell_range(block, r0, r1)

        # --- Ligne UNIQUE trop large pour le budget : découpage par COLONNES ---
        # Le fenêtrage par lignes garde une ligne unique ENTIÈRE (une ligne n'est jamais
        # coupée en son milieu). Mais une ligne de 200+ colonnes dépasse alors le plafond
        # à elle seule. On la redécoupe ici en fenêtres de colonnes, valeurs liées à
        # leur en-tête et plages exactes — au lieu de la laisser passer surdimensionnée.
        if take == 1:
            single = _row_md(rows[0]) if not wide else _row_kv(block.headers, rows[0])
            over = len(context_line(sheet_name, block, rng))
            if not wide:
                over += sum(len(x) + 3 for x in _markdown_header(block.headers, numeric))
            if over + len(single) > chunk_max_chars - SAFETY_MARGIN:
                sub, wr = _split_wide_row(
                    block, start,
                    sheet_name=sheet_name, sheet_index=sheet_index,
                    sheet_hidden=sheet_hidden, sheet_state=sheet_state,
                    chunk_max_chars=chunk_max_chars, has_formulas=has_formulas,
                    max_columns_per_chunk=max_columns_per_chunk,
                )
                chunks.extend(sub)
                for w in wr:
                    if w not in warnings:
                        warnings.append(w)
                continue

        lines = [context_line(sheet_name, block, rng), ""]
        if wide:
            lines += [_row_kv(block.headers, r) for r in rows]
        else:
            lines += _markdown_header(block.headers, numeric)
            lines += [_row_md(r) for r in rows]

        chunks.append({
            "text": "\n".join(lines).strip(),
            "meta": _meta(block, sheet_name, sheet_index, sheet_hidden, rng, r0, r1,
                          k, total, has_formulas, sheet_state=sheet_state),
        })

    return _finalize_chunks(chunks, warnings, chunk_max_chars)


def _col_ref(block: Block, i: int) -> str:
    return col_letter(block.col_start + i)


def _range_of(block: Block, cols: tuple, r0: int, r1: int) -> str:
    """Plage d'une fenêtre CONTIGUË de colonnes (`M120:S142`)."""
    return f"{_col_ref(block, cols[0])}{r0}:{_col_ref(block, cols[-1])}{r1}"


def _render_wide_block(
    block: Block,
    *,
    sheet_name: str,
    sheet_index: int,
    sheet_hidden: bool,
    sheet_state: str,
    rows_per_chunk: int,
    chunk_max_chars: int,
    max_columns_per_chunk: int,
    max_chunks: int,
    has_formulas: bool,
) -> tuple:
    """Découpe un tableau large en fenêtres LIGNES × COLONNES.

    Chaque chunk ne porte QUE les colonnes de sa fenêtre, plus la colonne qui nomme les
    lignes. Deux chunks de fenêtres différentes n'ont donc presque aucun mot en commun :
    c'est exactement ce qui manquait pour que le retrieval puisse les distinguer.
    """
    from .excel_columns import build_column_windows, detect_identity_columns

    warnings: List[str] = ["excel_wide_table_column_chunking"]

    identity = detect_identity_columns(block.headers, block.rows)
    windows, wnotes = build_column_windows(
        block.headers, block.rows,
        max_columns=max_columns_per_chunk, identity=identity,
    )
    warnings.extend(w for w in wnotes if w not in warnings)
    if not windows:
        return [], warnings

    data_row0 = block.data_row_start
    header_r0 = min(block.header_rows) if block.header_rows else None
    header_r1 = max(block.header_rows) if block.header_rows else None

    chunks: List[dict] = []
    truncated = False

    for win in windows:
        cols = win.rendered
        headers = [block.headers[i] for i in cols]
        numeric = [_column_is_numeric(block, i) for i in cols]

        # --- Fenêtrage des LIGNES, recalculé pour CETTE largeur ----------
        # Moins de colonnes -> plus de lignes tiennent dans le budget. Recalculer est
        # la seule façon de ne pas gaspiller le budget ni de le dépasser.
        overhead = sum(len(h) + 3 for h in headers) * 2 + 120
        row_windows: List[tuple] = []
        i = 0
        n = len(block.rows)
        while i < n:
            used = overhead
            take = 0
            while i + take < n and take < rows_per_chunk:
                line = _row_md([block.rows[i + take][c] for c in cols])
                if take > 0 and used + len(line) + 1 > chunk_max_chars:
                    break
                used += len(line) + 1
                take += 1
            take = max(1, take)
            row_windows.append((i, take))
            i += take

        total = len(row_windows)
        for k, (start, take) in enumerate(row_windows, start=1):
            if len(chunks) >= max_chunks:
                truncated = True
                break

            rows = block.rows[start:start + take]
            r0 = data_row0 + start
            r1 = r0 + take - 1

            main_range = _range_of(block, win.columns, r0, r1)
            id_range = _range_of(block, tuple(win.identity), r0, r1) if win.identity else None

            head = f"Feuille : {sheet_name}"
            if block.title:
                head += f" — {block.title}"
            if win.group:
                head += f" — {win.group}"
            lines = [f"{head} ({main_range})"]
            if id_range:
                # La plage principale ne couvre PAS la colonne d'identification : la
                # taire, ou l'agréger dans un « A120:S142 », désignerait des colonnes
                # absentes du chunk. On la cite séparément.
                lines.append(f"Identifiants : {id_range}")
            lines.append("")
            lines += _markdown_header(headers, numeric)
            lines += [_row_md([r[c] for c in cols]) for r in rows]

            meta = _meta(block, sheet_name, sheet_index, sheet_hidden, main_range,
                         r0, r1, k, total, has_formulas, sheet_state=sheet_state)
            meta["headers"] = list(headers)
            meta["column_start"] = _col_ref(block, win.columns[0])
            meta["column_end"] = _col_ref(block, win.columns[-1])
            meta["chunk_axis"] = "rows_and_columns"
            meta["wide_table"] = True
            meta["column_window_index"] = win.index
            meta["column_group"] = win.group
            meta["identity_columns"] = [_col_ref(block, i) for i in win.identity]
            meta["identity_range"] = id_range
            meta["data_range"] = main_range
            meta["data_ranges"] = [r for r in (id_range, main_range) if r]
            if header_r0 is not None:
                hdr_main = _range_of(block, win.columns, header_r0, header_r1)
                hdr_id = (_range_of(block, tuple(win.identity), header_r0, header_r1)
                          if win.identity else None)
                meta["header_range"] = hdr_main
                meta["header_ranges"] = [r for r in (hdr_id, hdr_main) if r]

            chunks.append({"text": "\n".join(lines).strip(), "meta": meta})

        if truncated:
            break

    if truncated:
        warnings.append("excel_wide_table_chunk_limit_reached")
    return chunks, warnings


def _column_is_numeric(block: Block, i: int) -> bool:
    vals = [r[i] for r in block.rows[:50] if i < len(r) and r[i]]
    if not vals:
        return False
    return sum(1 for v in vals if _looks_numeric(v)) >= len(vals) * 0.6


def _meta(block: Block, sheet_name: str, sheet_index: int, sheet_hidden: bool,
          rng: str, r0: int, r1: int, chunk_of: int, chunk_total: int,
          has_formulas: bool, kind: str = "excel_table",
          sheet_state: str = "visible") -> dict:
    # --- Convention de plages (une seule, et documentée) -----------------
    #   `data_range`   : les lignes de DONNÉES portées par CE chunk. Contigu, exact.
    #   `header_range` : les lignes d'EN-TÊTE du tableau (répétées dans chaque chunk),
    #                    ou celles du tableau dont un bloc de totaux hérite.
    #   `cell_range`   : ALIAS de `data_range`, conservé — c'est la plage affichée à
    #                    l'utilisateur et la réponse à « d'où vient ce chiffre ».
    #
    # `cell_range` ne couvre PAS l'en-tête, et c'est délibéré : un chunk n° 2 porte les
    # lignes 43-82 mais réaffiche l'en-tête des lignes 1-2. Une plage « A1:S82 »
    # engloberait les lignes 3-42, absentes du chunk — elle mentirait sur la preuve.
    # Les deux plages sont donc citées séparément plutôt que fusionnées.
    header_rows = list(block.header_rows) if kind == "excel_table" else []
    header_row = header_rows[0] if header_rows else None
    header_range = block.header_range if kind == "excel_table" else None
    return {
        "kind": kind,
        "sheet_name": sheet_name,
        "sheet_index": sheet_index,
        "sheet_hidden": bool(sheet_hidden),
        # `hidden` (rangée par commodité) et `veryHidden` (intention technique) ne
        # disent pas la même chose : on ne les réduit pas à un booléen.
        "sheet_state": sheet_state or "visible",
        "table_index": block.index,
        "cell_range": rng,
        "data_range": rng,
        "header_range": header_range,
        "row_start": r0,
        "row_end": r1,
        "column_start": col_letter(block.col_start),
        "column_end": col_letter(block.col_end),
        "headers": list(block.headers),
        "headers_inferred": bool(block.headers_inferred),
        "header_row": header_row,                    # compat : 1re ligne d'en-tête
        # En-têtes multi-lignes (P1.1) : un tableau financier empile couramment un
        # bandeau fusionné (« Premoney Capitalization ») au-dessus des vrais intitulés.
        "header_rows": header_rows,
        "header_depth": len(header_rows),
        "header_strategy": block.header_strategy if kind == "excel_table" else None,
        "merged_header_ranges": list(block.merged_header_ranges)[:20],
        # Un bloc de totaux séparé par une ligne vide emprunte les intitulés du tableau
        # qui le précède : sans cela, « 23379395 » serait indexé sous « col2 ».
        "headers_inherited": bool(block.headers_inherited) if kind == "excel_table" else False,
        "headers_inherited_from_table_index": block.inherited_from_table_index,
        "title": block.title,
        "has_formulas": bool(has_formulas),
        "chunk_of": chunk_of,
        "chunk_total": chunk_total,
        # Une valeur de cellule Excel est EXACTE (au cache de formule près). Ce
        # drapeau signifie « issu d'un OCR, à vérifier sur la source » : le poser à
        # True brouillerait un signal qui a un sens précis pour les plans/PDF.
        "requires_visual_check": False,
    }
