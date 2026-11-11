"""Construction de la requête lexicale globale — fonctions PURES.

Le problème résolu ici est mesuré, pas supposé : sur le corpus réel (9 478
extraits), la stratégie « OU de tous les termes du sujet » ramène 1 482 candidats
pour deux termes courants, soit 142 lots LLM. C'est un coût que personne
n'accepterait, et il croît avec la banalité des mots.

La solution retenue est **pilotée par la fréquence documentaire** : un terme rare
discrimine, un terme courant ne discrimine pas. On exige les termes rares (ET) et
on n'utilise les termes courants que si l'on n'a rien de mieux.

Aucune langue, aucun domaine n'est codé en dur : la rareté est mesurée SUR LE
CORPUS de l'utilisateur.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

# Un terme présent dans plus de DF_COMMON_RATIO des extraits n'apprend rien : il
# ne réduit pas le périmètre. Seuil volontairement bas — un mot qui touche 15 %
# du corpus n'est pas un sujet, c'est du bruit.
DF_COMMON_RATIO = 0.15

#: Au-delà, ajouter des termes obligatoires ne fait plus que dégrader le rappel.
MAX_REQUIRED_TERMS = 4

# Expressions citées : « "protection des données" » est une expression EXACTE et
# doit le rester — la découper en mots la ferait disparaître.
_QUOTED_RE = re.compile(r'"([^"]{2,80})"|«\s*([^»]{2,80})\s*»')

# Jetons sûrs pour `to_tsquery` : uniquement alphanumérique + tiret. Aucun
# opérateur (& | ! : * < >), aucune apostrophe, aucune parenthèse ne peut donc
# provenir de l'utilisateur — l'expression booléenne est construite par NOUS.
_SAFE_TOKEN_RE = re.compile(r"[^0-9a-zA-ZÀ-ÿ\-]")


def _safe(term: str) -> str:
    return _SAFE_TOKEN_RE.sub("", term or "").strip("-")


@dataclass(frozen=True)
class GlobalFtsQuery:
    """Requête lexicale structurée, prête pour ``to_tsquery``."""

    raw: str
    configuration: str
    #: Tous les termes du sujet (portée retirée), accents CONSERVÉS.
    terms: tuple[str, ...] = ()
    #: Termes discriminants — exigés (ET).
    required: tuple[str, ...] = ()
    #: Termes courants — n'excluent rien à eux seuls.
    optional: tuple[str, ...] = ()
    #: Expressions citées, conservées telles quelles.
    phrases: tuple[str, ...] = ()
    #: Fréquence documentaire mesurée de chaque terme.
    frequencies: dict = field(default_factory=dict)
    strategy: str = "hybrid"

    @property
    def is_empty(self) -> bool:
        return not (self.required or self.optional or self.phrases)

    def to_tsquery(self) -> str:
        """Expression booléenne PostgreSQL.

        Construite à partir de jetons déjà assainis : l'utilisateur ne peut y
        injecter aucun opérateur. Elle est de toute façon passée en PARAMÈTRE.
        """
        return build_tsquery_expression(
            required=self.required,
            optional=self.optional,
            phrases=self.phrases,
            strategy=self.strategy,
        )


def extract_quoted_phrases(query: str) -> tuple[list[str], str]:
    """Sort les expressions citées et renvoie (phrases, reste de la requête)."""
    phrases: list[str] = []

    def _take(m: re.Match) -> str:
        phrase = (m.group(1) or m.group(2) or "").strip()
        if phrase:
            phrases.append(phrase)
        return " "

    rest = _QUOTED_RE.sub(_take, query or "")
    return phrases, rest


def _phrase_expr(phrase: str) -> str:
    """Expression de proximité : « protection des données » -> mots adjacents."""
    words = [_safe(w) for w in phrase.split()]
    words = [w for w in words if len(w) >= 2]
    return " <-> ".join(words) if words else ""


def build_tsquery_expression(
    *,
    required: tuple[str, ...] | list[str],
    optional: tuple[str, ...] | list[str],
    phrases: tuple[str, ...] | list[str] = (),
    strategy: str = "hybrid",
) -> str:
    """Assemble l'expression selon la stratégie. Fonction PURE, testable seule."""
    req = [_safe(t) for t in required if _safe(t)]
    opt = [_safe(t) for t in optional if _safe(t)]
    phr = [e for e in (_phrase_expr(p) for p in phrases) if e]

    if strategy == "or":
        # S1 — OU pur. Rappel maximal, précision minimale. Référence de comparaison.
        allt = phr + req + opt
        return " | ".join(allt)

    if strategy == "and":
        # S2 — ET de tous les termes du sujet. Précision maximale, rappel fragile :
        # un document ne mentionnant qu'une partie du sujet disparaît.
        allt = phr + req + opt
        return " & ".join(allt)

    # S3 — HYBRIDE (défaut) : les termes discriminants sont EXIGÉS, les courants
    # sont ignorés pour le filtrage (ils ne réduisent rien) mais restent utiles au
    # classement. Une expression citée est toujours exigée.
    #
    # Si aucun terme n'est discriminant, on retombe sur un ET de tous les termes :
    # c'est le plus restrictif dont on dispose. Si c'est encore trop large, le
    # pré-vol refusera la requête plutôt que de tronquer.
    mandatory = phr + req
    if mandatory:
        return " & ".join(mandatory)
    return " & ".join(opt)


@dataclass(frozen=True)
class QueryPlan:
    """Formulation retenue, et pourquoi."""

    expression: str
    strategy: str          # "or" | "and" | "phrase"
    candidate_chunks: int
    candidate_documents: int
    #: Formulations essayées et rejetées, avec leur volume. Rend le choix auditable.
    attempts: tuple[dict, ...] = ()


class QueryTooBroad(Exception):
    """Aucune formulation ne ramène un volume traitable. On REFUSE, on ne tronque pas."""

    def __init__(self, best_count: int, limit: int) -> None:
        super().__init__("query_too_broad")
        self.best_count = best_count
        self.limit = limit


def plan_global_search_query(
    gq: GlobalFtsQuery,
    *,
    counter: Callable[[str], dict],
    max_candidate_chunks: int,
) -> QueryPlan:
    """Choisit la formulation la plus LARGE qui reste traitable.

    Escalade progressive, pilotée par le volume RÉEL (chaque comptage coûte
    quelques millisecondes sur un index GIN) :

      1. **OU** de tous les termes — meilleur rappel. Retenu s'il tient.
      2. **ET** de tous les termes — resserre quand le OU explose. Le rappel baisse,
         et c'est déclaré.
      3. Sinon : **refus** (``query_too_broad``). Jamais de troncature : prendre
         « les N premiers » présenterait un échantillon comme un parcours.

    On ne choisit donc PAS la formulation la moins chère : on choisit la plus
    large qui soit finançable, et on refuse si aucune ne l'est.
    """
    attempts: list[dict] = []

    for strategy in ("or", "and"):
        expr = build_tsquery_expression(
            required=gq.required, optional=gq.optional,
            phrases=gq.phrases, strategy=strategy,
        )
        if not expr:
            continue
        counts = counter(expr)
        n = int(counts["candidate_chunks"])
        attempts.append({"strategy": strategy, "candidate_chunks": n})

        if n <= max_candidate_chunks:
            return QueryPlan(
                expression=expr,
                strategy=strategy,
                candidate_chunks=n,
                candidate_documents=int(counts["candidate_documents"]),
                attempts=tuple(attempts),
            )

    best = min((a["candidate_chunks"] for a in attempts), default=0)
    raise QueryTooBroad(best, max_candidate_chunks)


def build_global_fts_query(
    user_query: str,
    *,
    configuration: str,
    df_provider: Callable[[list[str]], dict[str, int]] | None = None,
    scanned_chunks: int = 0,
    strategy: str = "hybrid",
) -> GlobalFtsQuery:
    """Construit la requête lexicale à partir de la demande utilisateur.

    ``df_provider`` mesure la fréquence des termes SUR LE CORPUS de l'utilisateur.
    C'est ce qui rend la stratégie générique : la rareté n'est pas devinée par une
    liste figée, elle est constatée.
    """
    from apps.rag_core.analysis.global_search import extract_topic_terms

    raw = user_query or ""
    phrases, rest = extract_quoted_phrases(raw)
    terms = [t for t in extract_topic_terms(rest) if _safe(t)]

    if not terms and not phrases:
        return GlobalFtsQuery(raw=raw, configuration=configuration, strategy=strategy)

    frequencies: dict[str, int] = {}
    if df_provider and terms:
        try:
            frequencies = df_provider(terms) or {}
        except Exception:
            frequencies = {}

    threshold = max(1, int(DF_COMMON_RATIO * scanned_chunks)) if scanned_chunks else 0

    required: list[str] = []
    optional: list[str] = []

    if threshold and frequencies:
        # Le plus rare d'abord : les termes qui réduisent le plus le périmètre.
        for term in sorted(terms, key=lambda t: frequencies.get(t, 0)):
            df = frequencies.get(term, 0)
            if df == 0:
                # Terme absent du corpus : l'exiger viderait la recherche. On le
                # garde en optionnel — c'est peut-être une faute de frappe, ou un
                # sujet réellement absent (ce que le résultat dira honnêtement).
                optional.append(term)
            elif df <= threshold and len(required) < MAX_REQUIRED_TERMS:
                required.append(term)
            else:
                optional.append(term)
    else:
        # Sans mesure de fréquence, aucun terme n'est réputé discriminant : on
        # n'invente pas de hiérarchie.
        optional = list(terms)

    return GlobalFtsQuery(
        raw=raw,
        configuration=configuration,
        terms=tuple(terms),
        required=tuple(required),
        optional=tuple(optional),
        phrases=tuple(phrases),
        frequencies=frequencies,
        strategy=strategy,
    )
