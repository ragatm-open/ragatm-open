"""Planner DÉTERMINISTE — question en français vers plan structuré.

Aucun LLM. Des règles, des motifs et le catalogue. Le choix est assumé : pour ce lot,
**la fiabilité prime sur la couverture linguistique**. Un planner déterministe qui
comprend 60 % des formulations et refuse proprement les 40 % restantes vaut mieux qu'un
planner qui en comprend 90 % et se trompe silencieusement sur 5 %.

Quand le planner ne sait pas, il ne devine pas : `planner_could_not_build_plan`. La
question repart alors vers le refus explicite existant, jamais vers le RAG vectoriel.

Le planner LLM éventuel (`TABULAR_LLM_PLANNER_ENABLED`) produira le MÊME objet
`TabularPlan`, et passera par les MÊMES validations. Il n'aura aucun privilège.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from apps.rag_core.tabular import catalog as C
from apps.rag_core.tabular import column_resolver as CR
from apps.rag_core.tabular import errors as E
from apps.rag_core.tabular import plan_schema as PS
from apps.rag_core.tabular import types as T

#: Opération -> motifs déclencheurs. L'ordre du dict compte : les motifs les plus
#: spécifiques sont testés en premier (`count_distinct` avant `count_rows`).
_OPERATION_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("count_distinct", re.compile(
        r"\b(combien de (valeurs |modalites )?(distinctes|differentes|uniques)"
        r"|nombre de .{0,30}(distinct|different|unique)s?"
        r"|count distinct)\b")),
    ("average", re.compile(r"\b(moyenne|moyen|average|mean)\b")),
    ("sum", re.compile(r"\b(somme|total|totale|totaux|cumul|sum|montant total)\b")),
    ("maximum", re.compile(r"\b(maximum|max|plus (eleve|haut|grand)|le plus fort)\b")),
    ("minimum", re.compile(r"\b(minimum|min|plus (bas|faible|petit))\b")),
    ("count_rows", re.compile(
        r"\b(combien de (lignes|enregistrements|entrees)|nombre de lignes"
        r"|combien y a-?t-?il|count)\b")),
]

#: `argmax`/`argmin` : « QUEL dossier a le plus grand montant ». La question porte sur
#: l'identité d'une ligne, pas sur une valeur — c'est ce « quel » qui les distingue
#: de `maximum`/`minimum`.
_ARG_PATTERN = re.compile(
    r"\b(quel|quelle|quels|quelles|which|who)\b.{0,60}"
    r"\b(plus|moins|maximum|minimum|max|min|meilleur|pire|"
    r"highest|lowest|largest|smallest|biggest|best|worst)\b")

#: Mots portant la DIRECTION d'un extremum. Testés avant les intensificateurs
#: (« plus », « le plus ») qui, seuls, ne disent pas le sens.
_MINIMISING = re.compile(
    r"\b(faible|faibles|petit|petite|petits|bas|basse|moindre|minimum|min|"
    r"pire|dernier|derniere|moins|lowest|smallest|worst|minimal|minimale)\b")
_MAXIMISING = re.compile(
    r"\b(eleve|elevee|eleves|elevees|grand|grande|grands|grandes|gros|grosse|"
    r"grosses|haut|haute|fort|forte|important|importante|"
    r"maximum|max|meilleur|meilleure|premier|premiere|highest|largest|biggest|"
    r"best|maximal|maximale)\b")

_TOP_PATTERN = re.compile(
    r"\b(top|les|le)\s*(\d{1,3})\b|"
    r"\b(\d{1,3})\s+(premiers?|meilleurs?|plus (gros|grands|eleves))\b")
_BOTTOM_PATTERN = re.compile(
    r"\b(\d{1,3})\s+(derniers?|pires?|plus (petits?|faibles?|bas))\b|"
    r"\bbottom\s*(\d{1,3})\b")

#: Filtres : « où <colonne> vaut <valeur> », « de la région Ouest », « pour Ouest ».
_FILTER_PATTERNS = [
    re.compile(r"\b(?:ou|dont|avec)\s+(?:la |le |l')?(?P<col>[\w\s'’-]{2,40}?)\s+"
               r"(?:vaut|est|=|egale?)\s+«?\s*(?P<val>[^»,.?]{1,60}?)\s*»?(?:$|[,.?])"),
    re.compile(r"\bde la (?P<col>region|categorie|zone)\s+(?P<val>[\w'’-]{2,40})"),
    re.compile(r"\bpour (?:la |le |l')?(?P<col>[\w\s'’-]{2,30}?)\s+"
               r"«\s*(?P<val>[^»]{1,60})\s*»"),
]

# --------------------------------------------------------------------------- #
# Seuils numériques (R1)
# --------------------------------------------------------------------------- #
# Le groupe `col` capture ce qui PRÉCÈDE immédiatement l'opérateur. C'est le signal
# qui rattache un seuil à sa colonne : « somme du CA HT supérieur à 1 000 » désigne
# `CA HT`, « dont la quantité dépasse 100 » désigne `Quantité`. Sans lui, un seuil
# flottait dans la question et devait être abandonné ou deviné.
#
# La capture est bornée (40 caractères, pas de ponctuation) : au-delà, ce n'est plus
# un groupe nominal mais le reste de la phrase, et le score lexical y trouverait
# n'importe quoi.
_COL_BEFORE = r"(?P<col>[\w\s'’-]{0,40}?)\s*"
_NUM = r"(?P<val>-?[\d][\d\s\u00a0.,]*)\s*(?P<unit>%|euros?|€)?"

_SUPERIOR = re.compile(
    _COL_BEFORE +
    r"\b(?:superieure?s?|plus grande?s?|plus de|au-?dela de|depasse(?:nt)?|excede(?:nt)?|"
    r"greater than|more than|above|over|exceeds?|>)\b"
    r"\s*(?:a|à|au|aux)?\s*" + _NUM)
_INFERIOR = re.compile(
    _COL_BEFORE +
    r"\b(?:inferieure?s?|plus petite?s?|moins de|en-?dessous de|sous|"
    r"less than|lower than|below|under|<)\b"
    r"\s*(?:a|à|au|aux)?\s*" + _NUM)
_BETWEEN = re.compile(
    _COL_BEFORE +
    r"\b(?:entre|compris(?:es)? entre|between)\b\s*"
    r"(?P<lo>-?[\d][\d\s\u00a0.,]*)\s*(?:%|euros?|€)?\s*"
    r"\b(?:et|and)\b\s*"
    r"(?P<hi>-?[\d][\d\s\u00a0.,]*)\s*(?P<unit>%|euros?|€)?")

#: Mots qui désignent la POPULATION, pas une mesure. « les lignes supérieures à
#: 1 000 » ne nomme aucune colonne : le seuil reste non rattaché, donc ambigu dès
#: qu'il existe plusieurs colonnes numériques.
_POPULATION_WORDS = re.compile(
    r"^\s*(?:les |le |la |l'|des |du |de la |de |d')?\s*"
    r"(lignes?|enregistrements?|entrees?|elements?|items?|donnees?|valeurs?|"
    r"resultats?|rows?|records?|entries)\s*$")


def _norm(text: str) -> str:
    s = (text or "").strip().lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.replace("’", "'")
    return re.sub(r"\s+", " ", s)


@dataclass
class PlannerOutput:
    plan_payload: dict
    #: Colonnes citées dans la question — sert aussi à la sélection de table.
    mentioned_columns: list[str]


#: Opérations RECONNAISSABLES mais hors périmètre du lot. Les nommer permet de
#: répondre « hors périmètre » plutôt que « je n'ai pas compris » : l'utilisateur
#: apprend que la demande est claire mais non servie, pas qu'il s'est mal exprimé.
_DEFERRED_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("median", re.compile(r"\b(mediane|median|percentile|centile|quartile)\b")),
    ("stddev", re.compile(r"\b(ecart[- ]type|variance|standard deviation|stddev)\b")),
    ("group_by", re.compile(r"\b(par (region|categorie|statut|client|mois|annee)|"
                            r"group(ed)? by|regroupe[rz]?|ventile[rz]?)\b")),
    ("join", re.compile(r"\b(jointure|joindre|join|croise[rz]? avec)\b")),
    ("pivot", re.compile(r"\b(pivot|tableau croise|crosstab)\b")),
]


def detect_deferred_operation(question: str) -> str | None:
    """Opération comprise mais hors périmètre, ou `None`."""
    q = _norm(question)
    for name, pattern in _DEFERRED_PATTERNS:
        if pattern.search(q):
            return name
    return None


def detect_operation(question: str) -> str | None:
    """Opération demandée, ou `None` si aucune règle ne s'applique."""
    q = _norm(question)
    if _ARG_PATTERN.search(q):
        # L'ordre compte. « le CA le PLUS FAIBLE » contient « plus » ET « faible » :
        # tester « plus » en premier renvoyait argmax pour une question qui demande un
        # minimum — une réponse fausse rendue avec un statut `ok`. Le mot qui porte la
        # DIRECTION (faible, petit, bas, dernier, pire) prime donc sur l'intensificateur.
        if _MINIMISING.search(q):
            return "argmin"
        if _MAXIMISING.search(q):
            return "argmax"
        return "argmin"
    if _BOTTOM_PATTERN.search(q):
        return "bottom_n"
    if _TOP_PATTERN.search(q) and re.search(r"\b(top|premiers?|meilleurs?|plus gros)\b", q):
        return "top_n"
    for op, pattern in _OPERATION_PATTERNS:
        if pattern.search(q):
            return op
    return None


def _extract_limit(question: str) -> int | None:
    q = _norm(question)
    for pattern in (_TOP_PATTERN, _BOTTOM_PATTERN):
        m = pattern.search(q)
        if m:
            for g in m.groups():
                if g and g.isdigit():
                    return int(g)
    return None


def _measure_phrase(question: str, op: str) -> str:
    """Isole la portion de la question qui désigne la MESURE.

    « la somme du montant net » -> « montant net ». On retire le mot d'opération et
    les articles : ce qui reste est ce que l'utilisateur veut mesurer.
    """
    q = _norm(question)
    q = re.sub(r"^(quel(le)?s?\s+(est|sont)\s+)", " ", q)
    for _, pattern in _OPERATION_PATTERNS:
        q = pattern.sub(" ", q)
    q = re.sub(r"\b(dans|de|du|de la|des|le|la|les|l'|un|une|pour|sur|par|"
               r"la feuille|le classeur|le tableau|le fichier|colonne)\b", " ", q)
    q = re.sub(r"\b\w+\.(xlsx|csv)\b", " ", q)
    q = re.sub(r"[?!.,;«»]", " ", q)
    return re.sub(r"\s+", " ", q).strip()


def build_plan(question: str, catalog: C.DocumentCatalog, table: C.CatalogTable,
               *, limits) -> PlannerOutput:
    """Construit un plan depuis la question. Lève si la demande n'est pas couverte."""
    op = detect_operation(question)
    if op is None:
        # Distinguer « hors périmètre » de « non compris » : ce ne sont pas les
        # mêmes informations pour l'utilisateur, ni les mêmes suites pour nous.
        deferred = detect_deferred_operation(question)
        if deferred is not None:
            raise E.TabularUnsupported(
                E.ERR_UNSUPPORTED_OPERATION,
                f"l'opération « {deferred} » est reconnue mais hors périmètre de ce lot")
        raise E.TabularUnsupported(
            E.ERR_PLANNER_FAILED,
            "cette formulation n'est pas couverte par le planner déterministe")

    candidates = [
        CR.ColumnCandidate(
            column_id=c.column_id, original_name=c.original_name,
            normalized_name=c.normalized_name, position=c.position,
            inferred_type=c.inferred_type, unit=c.unit)
        for c in table.columns
    ]
    if len(candidates) > limits.max_candidate_columns:
        raise E.TabularLimitExceeded(
            E.ERR_TOO_MANY_CANDIDATE_COLUMNS,
            f"{len(candidates)} colonnes candidates")

    mentioned: list[str] = []
    payload: dict = {
        "schema_version": PS.PLAN_SCHEMA_VERSION,
        "operation": op,
        "source": {
            "document_id": str(catalog.document_id),
            "sheet_id": str(table.sheet_id),
            "table_id": str(table.table_id),
        },
    }

    # -- Colonne cible -----------------------------------------------------
    target_col: CR.ColumnCandidate | None = None
    if op in PS.NEEDS_TARGET_COLUMN or op in ("top_n", "bottom_n"):
        phrase = _measure_phrase(question, op)
        required = T.NUMERIC_TYPES if op in ("sum", "average") else None
        if op in ("minimum", "maximum", "argmin", "argmax", "top_n", "bottom_n"):
            required = T.ORDERED_TYPES
        if not phrase:
            raise E.TabularClarificationRequired(
                E.ERR_AMBIGUOUS_COLUMN,
                "la mesure à calculer n'est pas identifiable dans la question",
                question="Sur quelle colonne le calcul doit-il porter ?",
                candidates=[c.original_name for c in candidates[:10]])
        col = CR.resolve_column(
            phrase, candidates,
            min_score=limits.column_match_min_score,
            min_margin=limits.column_match_min_margin,
            required_types=required)
        target_col = col
        mentioned.append(col.original_name)
        if op in PS.NEEDS_TARGET_COLUMN:
            payload["target"] = {"column_id": str(col.column_id)}
        if op in ("top_n", "bottom_n"):
            payload["order_by"] = [{
                "column_id": str(col.column_id),
                "direction": "desc" if op == "top_n" else "asc",
            }]
            payload["limit"] = _extract_limit(question) or 10

    # -- Filtres -----------------------------------------------------------
    # La mesure agrégée est passée aux filtres : « total des ventes supérieures à
    # 1 000 » rattache le seuil à ce qu'on somme (R1, signal 3).
    filters = _build_filters(question, candidates, limits, mentioned,
                             target_column=target_col)
    if filters:
        payload["filters"] = filters

    # -- Identité rendue par argmin/argmax ---------------------------------
    if op in ("argmin", "argmax"):
        identity = _identity_column(candidates)
        if identity is None:
            raise E.TabularClarificationRequired(
                E.ERR_AMBIGUOUS_COLUMN,
                "aucune colonne d'identité évidente pour désigner la ligne",
                question="Quelle colonne identifie la ligne recherchée ?",
                candidates=[c.original_name for c in candidates[:10]])
        payload["select_columns"] = [str(identity.column_id)]

    return PlannerOutput(plan_payload=payload, mentioned_columns=mentioned)


def _build_filters(question: str, candidates: list[CR.ColumnCandidate],
                   limits, mentioned: list[str], *,
                   target_column: CR.ColumnCandidate | None = None) -> list[dict]:
    q = _norm(question)
    out: list[dict] = []

    for pattern in _FILTER_PATTERNS:
        for m in pattern.finditer(q):
            col_phrase = (m.groupdict().get("col") or "").strip()
            value = (m.groupdict().get("val") or "").strip()
            if not col_phrase or not value:
                continue
            try:
                col = CR.resolve_column(
                    col_phrase, candidates,
                    min_score=limits.column_match_min_score,
                    min_margin=limits.column_match_min_margin)
            except E.TabularError:
                # Un filtre dont la colonne n'est pas identifiable ne doit PAS être
                # abandonné en silence : renvoyer un résultat sans lui donnerait une
                # somme sur toute la table présentée comme une somme filtrée.
                raise
            mentioned.append(col.original_name)
            out.append({"column_id": str(col.column_id),
                        "operator": "equals", "value": value})

    # -- Seuils numériques (R1) --------------------------------------------
    for pattern, operator in ((_SUPERIOR, "greater_than"),
                              (_INFERIOR, "less_than"),
                              (_BETWEEN, "between")):
        for m in pattern.finditer(q):
            col = _resolve_threshold_column(
                (m.groupdict().get("col") or "").strip(),
                candidates, target_column=target_column, limits=limits)
            unit = (m.groupdict().get("unit") or "").strip()

            if operator == "between":
                lo = _threshold_value(m.group("lo"), unit, col)
                hi = _threshold_value(m.group("hi"), unit, col)
                if lo > hi:
                    lo, hi = hi, lo
                entry = {"column_id": str(col.column_id), "operator": "between",
                         "values": [str(lo), str(hi)]}
            else:
                value = _threshold_value(m.group("val"), unit, col)
                entry = {"column_id": str(col.column_id), "operator": operator,
                         "value": str(value)}

            mentioned.append(col.original_name)
            out.append(entry)

    if len(out) > limits.max_filters:
        raise E.TabularLimitExceeded(
            E.ERR_TOO_MANY_FILTERS, f"{len(out)} filtres détectés")
    return out


def _identity_column(candidates: list[CR.ColumnCandidate]) -> CR.ColumnCandidate | None:
    """Colonne identifiant une ligne : un identifiant, sinon le premier texte.

    Le départage est lexicographique — arbitraire dans son fond, mais **stable** entre
    exécutions et entre machines, ce qui est la propriété qui compte ici.
    """
    ids = sorted((c for c in candidates if c.inferred_type == T.TYPE_IDENTIFIER),
                 key=lambda c: c.normalized_name)
    if ids:
        return ids[0]
    texts = sorted((c for c in candidates if c.inferred_type == T.TYPE_TEXT),
                   key=lambda c: c.position)
    return texts[0] if texts else None


def _threshold_value(raw: str, unit: str, col: CR.ColumnCandidate):
    """Normalise la valeur d'un seuil, en tenant compte de l'unité et du type cible.

    « 12 % » vaut 0.12 sur une colonne de pourcentage — comme Excel, qui stocke la
    fraction. Écrire 12 dans un filtre sur une telle colonne ne retiendrait rien et
    rendrait un résultat vide présenté comme un fait.
    """
    value = T.parse_decimal(raw)
    if unit in ("%",) and col.inferred_type != T.TYPE_PERCENTAGE:
        # L'utilisateur écrit un pourcentage sur une colonne qui n'en est pas une :
        # on ne réinterprète pas sa valeur, on la prend telle quelle.
        return value
    if unit in ("%",):
        return value / 100
    return value


def _resolve_threshold_column(phrase: str, candidates: list[CR.ColumnCandidate], *,
                              target_column: CR.ColumnCandidate | None,
                              limits) -> CR.ColumnCandidate:
    """Rattache un seuil à SA colonne — ou refuse (R1).

    L'ordre suit des signaux explicites, du plus fort au plus faible. Aucun niveau
    n'invente : le dernier recours est une clarification, jamais « la première
    colonne numérique ».

    1. le groupe nominal qui précède l'opérateur nomme une colonne ;
    2. il n'existe qu'UNE colonne numérique — le rattachement est forcé ;
    3. la mesure agrégée est numérique et rien d'autre n'est nommé
       (« total des ventes supérieures à 1 000 » : le seuil porte sur ce qu'on somme) ;
    4. sinon : `ambiguous_filter_column`.
    """
    numeric = [c for c in candidates if c.inferred_type in T.NUMERIC_TYPES]
    if not numeric:
        raise E.TabularError(
            E.ERR_INCOMPATIBLE_FILTER_TYPE,
            "un seuil numérique a été demandé mais la table n'a aucune colonne numérique")

    # 1 — groupe nominal explicite. Un mot de POPULATION (« les lignes ») ne nomme
    #     aucune mesure : on ne tente même pas de le résoudre, sans quoi le score
    #     lexical finirait par accrocher une colonne au hasard.
    if phrase and not _POPULATION_WORDS.match(phrase):
        try:
            return CR.resolve_column(
                phrase, numeric,
                min_score=limits.column_match_min_score,
                min_margin=limits.column_match_min_margin,
                required_types=T.NUMERIC_TYPES, what="colonne de filtre")
        except E.TabularClarificationRequired as exc:
            if exc.code != E.ERR_COLUMN_NOT_FOUND:
                # Ambiguïté RÉELLE entre deux colonnes proches : elle se tranche avec
                # l'utilisateur, pas ici.
                raise E.TabularClarificationRequired(
                    E.ERR_AMBIGUOUS_FILTER_COLUMN, exc.message,
                    question=exc.question, candidates=exc.candidates) from exc
            # « ne correspond à aucune colonne » : le groupe capturé n'était pas un
            # nom de mesure. On poursuit avec les signaux suivants.
        except E.TabularError:
            pass

    # 2 — une seule colonne numérique : aucun autre rattachement n'est possible.
    if len(numeric) == 1:
        return numeric[0]

    # 3 — la mesure agrégée, quand elle est numérique et qu'aucune autre n'est nommée.
    if target_column is not None and any(
            c.column_id == target_column.column_id for c in numeric):
        return target_column

    # 4 — le seuil reste flottant. Refus explicite.
    raise E.TabularClarificationRequired(
        E.ERR_AMBIGUOUS_FILTER_COLUMN,
        "le seuil demandé peut porter sur plusieurs colonnes numériques",
        question="Sur quelle colonne le seuil doit-il s'appliquer ?",
        candidates=[c.original_name for c in numeric[:10]])
