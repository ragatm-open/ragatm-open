# backend/apps/rag_core/services/query/strong_signals.py
from __future__ import annotations

"""
Détection des SIGNAUX FORTS d'une question : dates, valeurs numériques, opérateurs
temporels.

Un signal fort est une contrainte que l'utilisateur a formulée explicitement et qui
identifie un document mieux que la similarité sémantique. Sur un corpus de notes
quotidiennes au vocabulaire quasi identique, « la note du 18 mai 2026 » ne se
distingue QUE par sa date : le vectoriel les trouve toutes également proches, et le
lexical ne rapproche pas « 18 mai 2026 » de la forme indexée `2026-05-18`.

Cette couche est volontairement :

- DÉTERMINISTE : que des règles, aucun LLM, aucun réseau, aucun apprentissage ;
- CONSERVATRICE : en l'absence de signal, elle ne renvoie rien et le pipeline
  historique s'applique à l'identique ;
- SANS EFFET DE BORD : elle ne lit ni la base, ni le corpus — elle ne fait qu'analyser
  une chaîne. Les décisions d'usage (boost, tri) sont prises par l'appelant.

Elle ne connaît RIEN au format `.msg` : elle raisonne sur des dates et des nombres,
et sert donc n'importe quel corpus daté.
"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Optional, Tuple

# Borne de longueur : au-delà, on n'analyse pas. Une question légitime dépasse
# rarement quelques centaines de caractères, et l'analyse doit rester bornée quel que
# soit ce qui arrive par l'API.
MAX_QUESTION_CHARS = 2000

# Opérateurs temporels relatifs. Ils ne sont JAMAIS convertis en date absolue ici :
# « la plus récente » n'a de sens que par rapport à un ensemble de documents, que
# cette couche ne connaît pas. L'appelant tranche, après retrieval.
TEMPORAL_LATEST = "latest"
TEMPORAL_EARLIEST = "earliest"
TEMPORAL_BEFORE = "before"
TEMPORAL_AFTER = "after"

_MONTHS = {
    "janvier": 1, "janv": 1, "january": 1, "jan": 1,
    "fevrier": 2, "fev": 2, "february": 2, "feb": 2,
    "mars": 3, "march": 3, "mar": 3,
    "avril": 4, "avr": 4, "april": 4, "apr": 4,
    "mai": 5, "may": 5,
    "juin": 6, "june": 6, "jun": 6,
    "juillet": 7, "juil": 7, "july": 7, "jul": 7,
    "aout": 8, "august": 8, "aug": 8,
    "septembre": 9, "sept": 9, "september": 9, "sep": 9,
    "octobre": 10, "oct": 10, "october": 10,
    "novembre": 11, "nov": 11, "november": 11,
    "decembre": 12, "dec": 12, "december": 12,
}
_MONTH_ALTERNATION = "|".join(sorted(_MONTHS, key=len, reverse=True))

_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DMY_RE = re.compile(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{4})\b")
_TEXT_DATE_RE = re.compile(
    rf"\b(\d{{1,2}})(?:er)?\s+({_MONTH_ALTERNATION})\s+(\d{{4}})\b", re.IGNORECASE
)
# « March 12, 2026 » / « March 12 2026 »
_TEXT_DATE_EN_RE = re.compile(
    rf"\b({_MONTH_ALTERNATION})\s+(\d{{1,2}}),?\s+(\d{{4}})\b", re.IGNORECASE
)
_MONTH_YEAR_RE = re.compile(rf"\b({_MONTH_ALTERNATION})\s+(\d{{4}})\b", re.IGNORECASE)

_LATEST_RE = re.compile(
    r"\b(plus\s+r[eé]cente?|derni[eè]re?|dernier|most\s+recent|latest)\b", re.IGNORECASE
)
# `ancien(?:ne)?` et non `ancienne?` : la seconde forme n'apparie que « ancienn(e) »
# et manquait donc le masculin « le document le plus ancien ».
#
# La négation `(?!\s+fois)` / `(?!\s+time)` écarte l'idiome « pour la première fois
# depuis 2022 » / « for the first time since 2022 », très fréquent dans des notes de
# marché : c'est un superlatif de VALEUR, pas une demande du document le plus ancien.
# Sans elle, des questions de prix et d'objet basculaient à tort dans le rail temporel.
# `(?<!une )` écarte l'emploi NOMINAL « une première depuis 2022 » (= un fait inédit),
# à distinguer de l'emploi ORDINAL « la première note ». L'article indéfini est le
# discriminant fiable : on ne dit pas « une première note » pour désigner la plus
# ancienne d'un corpus.
_EARLIEST_RE = re.compile(
    r"\b(plus\s+ancien(?:ne)?|(?<!une )premi[eè]re?(?!\s+fois)|(?<!un )premier(?!\s+fois)"
    r"|earliest|first(?!\s+time))\b",
    re.IGNORECASE,
)
_BEFORE_RE = re.compile(r"\b(avant|before|ant[eé]rieure?)\b", re.IGNORECASE)
_AFTER_RE = re.compile(r"\b(apr[eè]s|after|post[eé]rieure?)\b", re.IGNORECASE)

# Valeurs numériques : entiers ou décimaux, virgule ou point. Le symbole monétaire et
# l'unité sont facultatifs et ne servent qu'à ne pas manquer les formes collées
# (« $98/bl », « 98$/bl »).
_NUMBER_RE = re.compile(r"(?<![\w.-])\$?\s?(\d{1,7}(?:[.,]\d{1,4})?)\s?\$?", re.IGNORECASE)

# Une valeur à quatre chiffres dans une plage plausible d'années est écartée : sur ce
# type de corpus, « depuis 2022 » est un repère temporel, pas un prix. La borne haute
# est volontairement large — un prix de 2100 dollars le baril n'existe pas, mais si
# un jour c'est le cas, mieux vaut manquer un boost que polluer tous les résultats.
_YEAR_MIN, _YEAR_MAX = 1900, 2100


@dataclass(frozen=True)
class StrongQuerySignals:
    """Contraintes explicites lues dans une question. Toujours sûr à ignorer."""

    exact_dates: Tuple[date, ...] = ()
    month_years: Tuple[Tuple[int, int], ...] = ()
    numeric_values: Tuple[Decimal, ...] = ()
    temporal_operator: Optional[str] = None

    @property
    def has_strong_signal(self) -> bool:
        return bool(self.exact_dates or self.month_years or self.numeric_values)

    @property
    def has_any_signal(self) -> bool:
        return self.has_strong_signal or self.temporal_operator is not None

    def reason_codes(self) -> Tuple[str, ...]:
        """Codes de raison stables pour les logs. Jamais de valeur, jamais de texte."""
        codes = []
        if self.exact_dates:
            codes.append("strong_date_exact")
        if self.month_years:
            codes.append("strong_date_month")
        if self.numeric_values:
            codes.append("strong_numeric")
        if self.temporal_operator == TEMPORAL_LATEST:
            codes.append("temporal_latest")
        elif self.temporal_operator == TEMPORAL_EARLIEST:
            codes.append("temporal_earliest")
        elif self.temporal_operator in (TEMPORAL_BEFORE, TEMPORAL_AFTER):
            codes.append(f"temporal_{self.temporal_operator}")
        return tuple(codes)


def _strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn"
    )


def _safe_date(year: int, month: int, day: int) -> Optional[date]:
    try:
        return date(year, month, day)
    except ValueError:  # 31 février, mois 13…
        return None


def extract_signals(question: str) -> StrongQuerySignals:
    """Analyse une question. Ne lève jamais, ne consulte rien d'externe."""
    if not question:
        return StrongQuerySignals()
    raw = question[:MAX_QUESTION_CHARS]
    # Les accents sont retirés pour la détection uniquement : « février » et
    # « fevrier » doivent donner le même résultat.
    text = _strip_accents(raw)

    dates: list[date] = []
    consumed: list[Tuple[int, int]] = []  # bornes déjà interprétées comme date

    for match in _ISO_RE.finditer(text):
        found = _safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if found:
            dates.append(found)
            consumed.append(match.span())
    for match in _DMY_RE.finditer(text):
        found = _safe_date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
        if found:
            dates.append(found)
            consumed.append(match.span())
    for match in _TEXT_DATE_RE.finditer(text):
        month = _MONTHS.get(match.group(2).lower())
        found = _safe_date(int(match.group(3)), month, int(match.group(1))) if month else None
        if found:
            dates.append(found)
            consumed.append(match.span())
    for match in _TEXT_DATE_EN_RE.finditer(text):
        month = _MONTHS.get(match.group(1).lower())
        found = _safe_date(int(match.group(3)), month, int(match.group(2))) if month else None
        if found:
            dates.append(found)
            consumed.append(match.span())

    month_years: list[Tuple[int, int]] = []
    for match in _MONTH_YEAR_RE.finditer(text):
        if any(start <= match.start() < end for start, end in consumed):
            continue  # déjà couvert par une date complète
        month = _MONTHS.get(match.group(1).lower())
        year = int(match.group(2))
        if month and _YEAR_MIN <= year <= _YEAR_MAX:
            month_years.append((year, month))
            consumed.append(match.span())

    numbers: list[Decimal] = []
    for match in _NUMBER_RE.finditer(text):
        if any(start <= match.start() < end for start, end in consumed):
            continue  # ce chiffre appartient à une date
        literal = match.group(1).replace(",", ".")
        try:
            value = Decimal(literal)
        except (InvalidOperation, ValueError):
            continue
        # Une année isolée n'est pas une valeur métier (« depuis 2022 »).
        if value == value.to_integral_value() and _YEAR_MIN <= int(value) <= _YEAR_MAX:
            continue
        numbers.append(value)

    operator = None
    if _LATEST_RE.search(text):
        operator = TEMPORAL_LATEST
    elif _EARLIEST_RE.search(text):
        operator = TEMPORAL_EARLIEST
    elif _BEFORE_RE.search(text):
        operator = TEMPORAL_BEFORE
    elif _AFTER_RE.search(text):
        operator = TEMPORAL_AFTER

    return StrongQuerySignals(
        exact_dates=tuple(dict.fromkeys(dates)),
        month_years=tuple(dict.fromkeys(month_years)),
        numeric_values=tuple(dict.fromkeys(numbers)),
        temporal_operator=operator,
    )


def canonical_date_terms(signals: StrongQuerySignals) -> Tuple[str, ...]:
    """Formes canoniques à ajouter à la recherche LEXICALE.

    Les chunks portent la date en ISO (`Date : 2026-05-18`). Une question qui dit
    « 18 mai 2026 » n'a aucun token en commun avec elle : ajouter la forme ISO à la
    requête rétablit l'appariement, sans toucher au contenu indexé.

    On n'ajoute QUE la forme ISO : multiplier les variantes (`18/05/2026`,
    `May 18, 2026`) élargirait la requête sans rien apparier, puisque l'index ne
    contient que l'ISO.
    """
    terms = [d.isoformat() for d in signals.exact_dates]
    terms.extend(f"{year:04d}-{month:02d}" for year, month in signals.month_years)
    return tuple(dict.fromkeys(terms))


def _chunk_date(chunk) -> Optional[str]:
    """Date d'un chunk, au format ISO triable. `None` si le chunk n'est pas daté.

    On lit la métadonnée `email_sent_at` si elle existe, sinon `document_date` — deux
    clés déjà produites par le pipeline. Rien ici n'est propre au format `.msg` : tout
    corpus dont les chunks portent une date bénéficie du même tri.
    """
    if not isinstance(chunk, dict):
        return None
    # Le pipeline expose les métadonnées de chunk sous `source.meta` (cf.
    # chunk_repository), et non à la racine : lire seulement `chunk["meta"]` renvoyait
    # `None` pour TOUT candidat réel, rendant le tri inopérant en production alors
    # qu'il passait sur des dicts de test construits à plat. On tente donc les deux
    # emplacements, comme le fait déjà `diversify.build_diversity_group`.
    source = chunk.get("source")
    candidates = []
    if isinstance(source, dict):
        if isinstance(source.get("meta"), dict):
            candidates.append(source["meta"])
        candidates.append(source)
    if isinstance(chunk.get("meta"), dict):
        candidates.append(chunk["meta"])
    candidates.append(chunk)

    for holder in candidates:
        for key in ("email_sent_at", "sent_at", "document_date"):
            value = holder.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def apply_temporal_operator(chunks, operator: Optional[str]):
    """Réordonne des CANDIDATS déjà pertinents selon un opérateur temporel relatif.

    « La note la plus récente » ne peut pas être résolue par similarité : toutes les
    notes se ressemblent, et le moteur vectoriel n'a aucune notion d'ordre. On applique
    donc un tri DÉTERMINISTE — mais uniquement sur l'ensemble déjà retenu par le
    retrieval, jamais sur tout le corpus : la contrainte sémantique de la question
    reste première, l'opérateur ne fait que départager.

    Les chunks non datés gardent leur ordre relatif et passent après les chunks datés :
    un corpus mixte n'est donc pas désorganisé par la présence d'un seul document daté.
    """
    if operator not in (TEMPORAL_LATEST, TEMPORAL_EARLIEST) or not chunks:
        return chunks

    dated, undated = [], []
    for position, chunk in enumerate(chunks):
        stamp = _chunk_date(chunk)
        (dated if stamp else undated).append((stamp, position, chunk))
    if not dated:
        return chunks

    # Le rang d'origine sert de départage : à date égale, l'ordre du retrieval
    # (donc la pertinence) prime — le tri reste totalement déterministe.
    reverse = operator == TEMPORAL_LATEST
    dated.sort(key=lambda item: (item[0], -item[1] if reverse else item[1]), reverse=reverse)
    return [chunk for _stamp, _pos, chunk in dated] + [chunk for _s, _p, chunk in undated]


# ---------------------------------------------------------------------------
# Intention temporelle (P3-c) — global vs thématique
# ---------------------------------------------------------------------------
#
# `StrongQuerySignals.temporal_operator` dit QU'IL y a un opérateur, pas SUR QUOI il
# porte. « La note la plus récente » et « la note la plus récente sur le Brent » ont
# le même opérateur mais ne se résolvent pas du tout de la même façon : la première
# n'a aucun thème discriminant et le retrieval lexical/vectoriel ne peut pas en
# constituer un pool fiable ; la seconde doit rester contrainte par son thème, sinon
# on renverrait le document le plus récent HORS SUJET.
#
# D'où cette couche : même détection déterministe, mais qui tranche explicitement
# entre portée GLOBALE et portée THÉMATIQUE, et qui extrait le thème le cas échéant.

TEMPORAL_LATEST_GLOBAL = "latest_global"
TEMPORAL_EARLIEST_GLOBAL = "earliest_global"
TEMPORAL_LATEST_TOPIC = "latest_about_topic"
TEMPORAL_EARLIEST_TOPIC = "earliest_about_topic"
TEMPORAL_BEFORE_DATE = "before_date"
TEMPORAL_AFTER_DATE = "after_date"
TEMPORAL_BETWEEN_DATES = "between_dates"

_BETWEEN_RE = re.compile(r"\b(entre|between)\b", re.IGNORECASE)

# Mots vides et mots « de gestion documentaire ». Un token qui figure ici ne peut pas
# à lui seul faire d'une question une question THÉMATIQUE : « quelle est la note la
# plus récente du corpus » ne parle de rien en particulier.
#
# La liste est volontairement CONSERVATRICE : en cas de doute on garde le token, ce
# qui classe la question en thématique. Se tromper dans ce sens est bénin (on reste
# contraint par le thème) ; l'inverse ferait remonter un document hors sujet.
_GENERIC_TOKENS = frozenset(
    """
    a ai as au aux avec ce ces cet cette dans de des du en est et eux il ils l la le
    les leur lui ma mais me mes moi mon ne nos notre nous on ou par pas plus pour qu
    que quel quelle quelles quels qui quoi sa se ses son sont sur ta te tes toi ton tu
    un une vos votre vous y d s n c j m t
    the a an of in on at to for from by with and or is are was were be been this that
    these those what which who whom whose it its
    montre montrer donne donner trouve trouver retrouve retrouver affiche afficher
    liste lister indique indiquer dis dire cherche chercher recherche rechercher
    disait disent dit parle parlent parlant parler traite traitent traitant concerne
    concernent concernant evoque evoquent evoquant mentionne mentionnent mentionnant
    publie publiee publies publiees publication redige redigee date datee
    show me give find list search tell about says said mentions mentioning published
    note notes document documents documentation message messages mail mails email
    emails courriel courriels fichier fichiers corpus doc docs dossier piece pieces
    base espace
    """.split()
)


@dataclass(frozen=True)
class TemporalQueryIntent:
    """Intention temporelle exploitable par le leg documentaire.

    `topic_query` est le résidu de la question une fois retirés l'opérateur, les
    dates et les mots de gestion documentaire : c'est ce qui sera passé au retrieval
    thématique. Il vaut `None` pour une portée globale.
    """

    operator: str
    exact_dates: Tuple[date, ...] = ()
    topic_query: Optional[str] = None
    has_explicit_topic: bool = False

    @property
    def is_global(self) -> bool:
        return self.operator in (TEMPORAL_LATEST_GLOBAL, TEMPORAL_EARLIEST_GLOBAL)

    @property
    def descending(self) -> bool:
        """Sens du tri : le plus récent d'abord."""
        return self.operator in (TEMPORAL_LATEST_GLOBAL, TEMPORAL_LATEST_TOPIC)

    def reason_code(self) -> str:
        """Code de raison stable pour les logs. Jamais de valeur, jamais de texte."""
        return {
            TEMPORAL_LATEST_GLOBAL: "temporal_latest_global",
            TEMPORAL_EARLIEST_GLOBAL: "temporal_earliest_global",
            TEMPORAL_LATEST_TOPIC: "temporal_latest_topic",
            TEMPORAL_EARLIEST_TOPIC: "temporal_earliest_topic",
            TEMPORAL_BEFORE_DATE: "temporal_before_date",
            TEMPORAL_AFTER_DATE: "temporal_after_date",
            TEMPORAL_BETWEEN_DATES: "temporal_between_dates",
        }[self.operator]


def _topic_terms(question: str, signals: StrongQuerySignals) -> Tuple[str, ...]:
    """Tokens SIGNIFIANTS restants une fois l'habillage temporel retiré.

    On travaille sur le texte désaccentué pour le test d'appartenance, mais on
    conserve la forme d'origine des tokens retenus : c'est elle qui repartira vers le
    retrieval, où les accents comptent.
    """
    raw = (question or "")[:MAX_QUESTION_CHARS]
    text = _strip_accents(raw)

    # Neutralise tout ce qui a déjà été interprété : dates, mois-année, opérateurs.
    for pattern in (
        _ISO_RE, _DMY_RE, _TEXT_DATE_RE, _TEXT_DATE_EN_RE, _MONTH_YEAR_RE,
        _LATEST_RE, _EARLIEST_RE, _BEFORE_RE, _AFTER_RE, _BETWEEN_RE,
    ):
        text = pattern.sub(" ", text)

    terms = []
    for token in re.findall(r"[0-9A-Za-z]+", text):
        lowered = token.lower()
        if lowered in _GENERIC_TOKENS:
            continue
        # Un nombre isolé n'est pas un thème : c'est un reliquat de date ou une valeur
        # déjà captée par `numeric_values`.
        if lowered.isdigit():
            continue
        if len(lowered) < 2:
            continue
        terms.append(token)
    return tuple(dict.fromkeys(terms))


def extract_temporal_intent(question: str) -> Optional[TemporalQueryIntent]:
    """Intention temporelle d'une question, ou `None` si elle n'en porte aucune.

    Ne lève jamais, ne consulte rien d'externe, bornée en longueur. Renvoyer `None`
    est le cas NORMAL : le pipeline historique s'applique alors à l'identique.
    """
    if not question:
        return None
    signals = extract_signals(question)
    text = _strip_accents(question[:MAX_QUESTION_CHARS])
    dates = signals.exact_dates
    terms = _topic_terms(question, signals)
    topic = " ".join(terms) if terms else None

    # Les opérateurs ANCRÉS sur une date passent avant les opérateurs relatifs : ils
    # sont plus spécifiques. « les notes après le 13 mars » est un after_date, même si
    # « dernière » apparaît ailleurs dans la phrase.
    if _BETWEEN_RE.search(text) and len(dates) >= 2:
        operator = TEMPORAL_BETWEEN_DATES
    elif _BEFORE_RE.search(text) and dates:
        operator = TEMPORAL_BEFORE_DATE
    elif _AFTER_RE.search(text) and dates:
        operator = TEMPORAL_AFTER_DATE
    elif _LATEST_RE.search(text):
        operator = TEMPORAL_LATEST_TOPIC if terms else TEMPORAL_LATEST_GLOBAL
    elif _EARLIEST_RE.search(text):
        operator = TEMPORAL_EARLIEST_TOPIC if terms else TEMPORAL_EARLIEST_GLOBAL
    else:
        # Une date seule (« que disait la note du 12 mars ? ») n'est PAS un opérateur :
        # elle est déjà servie par `canonical_date_terms`, sans leg documentaire.
        return None

    return TemporalQueryIntent(
        operator=operator,
        exact_dates=dates,
        topic_query=topic,
        has_explicit_topic=bool(terms),
    )
