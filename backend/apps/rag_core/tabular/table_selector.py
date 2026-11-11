"""Sélection de la feuille et de la table visées par la question.

Le mode d'échec évité ici est le jumeau de celui du résolveur de colonnes : **répondre
juste, sur la mauvaise table.** Une feuille « Synthèse » et une feuille « Détail »
portent souvent les mêmes en-têtes ; sommer la mauvaise donne un chiffre plausible et
faux.

Règle : en l'absence de dominance claire, `clarification_required`. Jamais la première
table, jamais la plus grande, jamais celle de la première feuille.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from apps.rag_core.tabular import catalog as C
from apps.rag_core.tabular import errors as E

#: Une feuille dont le nom évoque un archivage ou une projection ne doit pas être
#: choisie implicitement : elle répondrait sur des données historiques ou simulées.
_NON_DEFAULT_SHEETS = re.compile(
    r"\b(archive|archives|historique|history|old|ancien|"
    r"previsionnel|prevision|forecast|budget_prev|simulation|scenario)\b")


def _norm(text: str) -> str:
    s = (text or "").strip().lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s)


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", _norm(text)) if len(t) > 1}


@dataclass
class TableScore:
    table: C.CatalogTable
    score: float
    reasons: list[str]


def select_table(question: str, catalog: C.DocumentCatalog, *,
                 mentioned_columns: list[str] | None = None,
                 max_candidates: int) -> C.CatalogTable:
    """Élit UNE table, ou lève une clarification."""
    tables = catalog.tables
    if not tables:
        raise E.TabularError(E.ERR_TABLE_NOT_FOUND, "aucune table dans ce document")
    if len(tables) > max_candidates:
        raise E.TabularLimitExceeded(
            E.ERR_TOO_MANY_CANDIDATE_TABLES,
            f"{len(tables)} tables candidates, plafond {max_candidates}")

    if len(tables) == 1:
        return tables[0]

    q_tokens = _tokens(question)
    wanted = {t for name in (mentioned_columns or []) for t in _tokens(name)}

    scored: list[TableScore] = []
    for t in tables:
        score, reasons = 0.0, []

        # 1 — la feuille est nommée dans la question. Signal le plus fort : c'est une
        #     désignation explicite, pas une inférence.
        sheet_tokens = _tokens(t.sheet_name)
        if sheet_tokens and sheet_tokens <= q_tokens:
            score += 3.0
            reasons.append(f"feuille « {t.sheet_name} » nommée")

        # 2 — recouvrement entre les colonnes citées et celles de la table.
        headers = {tok for c in t.columns for tok in _tokens(c.original_name)}
        if wanted:
            covered = len(wanted & headers) / len(wanted)
            score += 2.0 * covered
            if covered:
                reasons.append(f"{covered:.0%} des colonnes demandées présentes")

        # 3 — recouvrement avec les mots de la question.
        overlap = len(q_tokens & headers)
        if overlap:
            score += 0.2 * overlap
            reasons.append(f"{overlap} terme(s) de la question dans les en-têtes")

        # 4 — pénalité archive/prévisionnel : ces feuilles ne sont jamais un défaut.
        if _NON_DEFAULT_SHEETS.search(_norm(t.sheet_name)):
            score -= 2.5
            reasons.append("feuille d'archive ou de projection (non retenue par défaut)")

        scored.append(TableScore(table=t, score=score, reasons=reasons))

    scored.sort(key=lambda s: (-s.score, s.table.sheet_name, s.table.data_range))
    best, second = scored[0], scored[1]

    # Aucun signal, ou égalité : on ne tranche pas.
    if best.score <= 0 or (best.score - second.score) < 1.0:
        raise E.TabularClarificationRequired(
            E.ERR_AMBIGUOUS_TABLE,
            "plusieurs tables peuvent répondre à cette question",
            question="Sur quelle feuille souhaitez-vous que le calcul porte ?",
            candidates=[f"{s.table.sheet_name} ({s.table.data_range})"
                        for s in scored[:5]])
    return best.table
