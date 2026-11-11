"""Heuristiques d'intention du chat — reconnaissance de FORMES DE DEMANDE.

Doctrine, en une phrase :

    on cherche une DEMANDE, pas un mot du champ lexical de l'analyse.

« Rédige un rapport détaillé » et « Quel est le nom du rapport ? » partagent le mot
« rapport » et n'appellent pas la même réponse. Ces règles privilégient donc la
PRÉCISION : un cas ambigu reste `factual`, parce qu'une réponse trop courte se
rattrape par une reformulation, tandis qu'un gabarit comparatif appliqué à une
question qui n'oppose rien fait comparer les SOURCES entre elles (mode d'échec
mesuré en dev le 25/07/2026, cf. docs/06-ai/politique-comparative.md).

Ce n'est **pas** un système de compréhension linguistique. C'est un jeu de règles
déterministes, locales, explicables et sans dépendance, arbitré par le benchmark
versionné `tests/tests_root/benchmarks/heuristics/`.

Chaîne de décision :

    question brute
    → normalisation (repliage accents, longueur préservée)
    → neutralisation des CONSIGNES CITÉES
    → segmentation en propositions, marquage des propositions NIÉES
    → comparaison   (objets nommés, puis verbe de demande)
    → interrogatif explicatif
    → verbe analytique en tête de demande
    → verbe de production + livrable analytique
    → forme nominale (groupe nominal entier, tête = livrable)
    → ellipse autonome
    → factual

Les vocabulaires FR et EN sont déclarés UNE SEULE FOIS ci-dessous et réutilisés par
toutes les règles ; aucune liste n'est dupliquée dans une expression régulière.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


def _fold(text: str) -> str:
    """Minuscules sans accents, LONGUEUR PRÉSERVÉE.

    Le repliage se fait caractère par caractère : « é » → « e », jamais « œ » → « oe ».
    C'est ce qui permet d'extraire les segments dans le texte ORIGINAL à partir des
    positions trouvées dans le texte replié — sans quoi les objets comparés
    ressortiraient désaccentués et pollueraient les requêtes de recherche.
    """
    out = []
    for ch in text or "":
        decomposed = unicodedata.normalize("NFD", ch)
        base = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
        out.append((base[:1] or ch).lower())
    return "".join(out)


def is_inventory_question(question: str) -> bool:
    q = (question or "").lower().strip()
    if not q:
        return False
    has_list_intent = any(k in q for k in ("liste", "list", "quelles", "quels", "toutes", "tous"))
    has_collection = any(k in q for k in ("startup", "startups", "pitch", "pitches", "pitchs", "deck", "pitchdeck"))
    return has_list_intent and has_collection


# ===========================================================================
# VOCABULAIRES — source de vérité UNIQUE, par langue et par famille
# ===========================================================================
#
# Chaque constante est un fragment d'alternance sans groupe capturant. Les règles
# les composent ; elles ne les recopient jamais. Toutes les formes sont données
# REPLIÉES (sans accent, en minuscules) : c'est sur le texte replié qu'on cherche.

# --- verbes ANALYTIQUES : portent seuls l'intention, en tête de demande -----
_ANALYTIC_REQUEST_VERBS_FR = (
    r"analys(?:e|es|ez|er|ons)|"
    r"evalu(?:e|es|ez|er|ons)|"
    r"apprecie[rz]?|appreciez|"
    r"examin(?:e|es|ez|er|ons)|"
    r"etudi(?:e|es|ez|er|ons)|"
    r"discut(?:e|es|ez|er|ons)|"
    r"interpret(?:e|es|ez|er|ons)|"
    r"expliqu(?:e|es|ez|er|ons)|explicit(?:e|es|ez|er)|"
    r"decri(?:s|t|re)|decrivez|"
    r"detaill(?:e|es|ez|er)|"
    r"developp(?:e|es|ez|er|ons)|"
    r"present(?:e|es|ez|er|ons)|"
    r"resum(?:e|es|ez|er|ons)|"
    r"synthetis(?:e|es|ez|er|ons)|"
    r"comment(?:e|es|ez|er)|"
    r"justifi(?:e|es|ez|er)|"
    r"precis(?:e|es|ez|er)|"
    r"eclair(?:e|es|ez|er)|"
    r"expos(?:e|es|ez|er)|"
    r"approfondi(?:s|r)|approfondissez|"
    r"recapitul(?:e|es|ez|er)|"
    r"diagnostiqu(?:e|es|ez|er)|"
    r"degag(?:e|es|ez|er)|"
    r"creuse[rz]?"
)
_ANALYTIC_REQUEST_VERBS_EN = (
    r"analyz(?:e|es)|analys(?:e|es)|"
    r"assess(?:es)?|evaluate[s]?|review[s]?|examine[s]?|investigate[s]?|"
    r"discuss(?:es)?|interpret[s]?|explain[s]?|describe[s]?|detail[s]?|"
    r"elaborate[s]?|clarify|justify|outline[s]?|explore[s]?|"
    r"summariz(?:e|es)|summaris(?:e|es)|recap|expand|condense[s]?|brief"
)

# --- verbes de PRODUCTION : neutres seuls, analytiques si le livrable l'est --
_PRODUCTION_VERBS_FR = (
    r"fai(?:s|t|tes|re)|"
    r"redig(?:e|es|ez|er)|"
    r"prepar(?:e|es|ez|er)|"
    r"produi(?:s|t|sez|re)|"
    r"etabli(?:s|t|ssez|r)|"
    r"elabor(?:e|es|ez|er)|"
    r"dress(?:e|es|ez|er)|"
    r"ecri(?:s|t|vez|re)|"
    r"donn(?:e|es|ez|er)|"
    r"compil(?:e|es|ez|er)|"
    r"monte[rz]?"
)
_PRODUCTION_VERBS_EN = (
    r"prepare[s]?|draft[s]?|write[s]?|produce[s]?|provide[s]?|create[s]?|"
    r"give[s]?|develop[s]?|compile[s]?|establish(?:es)?|deliver[s]?|"
    r"draw|put|issue[s]?"
)

# --- LIVRABLES analytiques : un rapport DEMANDÉ, pas un rapport MENTIONNÉ ---
_ANALYTIC_DELIVERABLES_FR = (
    r"analyses?|syntheses?|rapports?|bilans?|revues?|diagnostics?|panoramas?|"
    r"recapitulatifs?|resumes?|comparatifs?|"
    r"notes?|dossiers?|notes? d'analyse|notes? de synthese|notes? de cadrage|"
    r"etats? des lieux|vue d'ensemble|points? de situation|point|"
    r"lecture critique|points? cles|contexte"
)
_ANALYTIC_DELIVERABLES_EN = (
    r"analys(?:is|es)|assessments?|reviews?|summar(?:y|ies)|reports?|"
    r"overviews?|diagnos(?:is|tics?)|panoramas?|stocktakes?|recaps?|"
    r"notes?|files?|briefing notes?|scoping notes?|analysis notes?|state of play|"
    r"status updates?|key (?:findings|points|takeaways)|highlights?|"
    r"gap analysis|risk assessment|control assessment|critical review|context"
)

# --- INTERROGATIFS explicatifs -------------------------------------------------
# `comment` et `how` sont ambigus : ils introduisent aussi bien une explication
# (« comment fonctionne… ») qu'un dénombrement (« how many… »). Les quantitatifs
# sont donc exclus EXPLICITEMENT, par une garde négative dans le motif lui-même.
_ANALYTIC_INTERROGATIVES_FR = (
    r"pourquoi|en quoi|dans quelle mesure|de quelle maniere|pour quelles raisons|"
    r"que signifie|qu'implique|que revel(?:e|ent)|que chang(?:e|ent)|"
    r"qu'est-ce que cela implique|qu'est-ce que cela change|"
    r"que peut-on en deduire|qu'en deduire|qu'est-ce qui explique|"
    r"quelles sont les implications|quelles sont les consequences|"
    r"quelles causes expliquent|quels enseignements|"
    r"comment(?!\s+s'appelle)"
)
_ANALYTIC_INTERROGATIVES_EN = (
    r"why|to what extent|in what way|what explains|what caused|what lessons|"
    r"what can (?:be inferred|we infer)|what are the (?:implications|consequences)|"
    r"what do(?:es)?\s[^?.!]{0,40}?\b(?:mean|imply|reveal|indicate|change|affect)|"
    r"how (?:does|do|did|has|have|had)(?!\s+many)"
)
#: Quantitatifs : `how` suivi de ces mots demande une VALEUR, pas une explication.
_QUANTITATIVE_HOW_EN = r"how\s+(?:many|much|long|often|far|old|big|large|soon|fast)"

# --- verbes COMPARATIFS de demande ----------------------------------------------
_COMPARATIVE_REQUEST_VERBS_FR = (
    r"compar(?:e|es|ez|er|ons)|confront(?:e|es|ez|er)|"
    r"oppos(?:e|es|ez|er)|distingu(?:e|es|ez|er)|differenci(?:e|es|ez|er)|"
    r"met(?:s|tez|tre)\s+en\s+(?:regard|parallele|perspective comparee)"
)
_COMPARATIVE_REQUEST_VERBS_EN = (
    r"compare[s]?|contrast[s]?|differentiate[s]?|distinguish(?:es)?|"
    r"benchmark|weigh[s]?|set"
)

# --- LOCUTIONS analytiques figées -------------------------------------------------
# Elles ne se réduisent ni à un verbe ni à un livrable : « mets en perspective »
# n'est analytique qu'ENTIER. Elles sont soumises aux mêmes gardes (négation,
# citation) que les verbes, mais pas à la position de tête — « fais ressortir les
# implications » place le verbe en tête, « donne une lecture critique » aussi.
_ANALYTIC_IDIOMS = (
    r"met(?:s|tez|tre|tons)\s+(?:[^.?!]{0,30}?\s)?en perspective|"
    r"tir(?:e|es|ez|er)\s+les enseignements|"
    r"degag(?:e|es|ez|er)\s+les implications|"
    r"fai(?:s|tes|re)\s+ressortir|"
    # Volontairement PAS « état des lieux » : c'est un LIVRABLE, pas une locution.
    # Comme locution il déclenchait sur « quel service a produit l'état des lieux ? »,
    # qui demande un service, pas un état des lieux.
    r"lecture critique|analyse critique|"
    r"put\s+[^.?!]{0,30}?into perspective|"
    r"draw (?:out|conclusions)|derive the implications|"
    r"identify the implications|critical reading|go deeper|dig deeper"
)

# --- MARQUEURS lexicaux non ambigus ------------------------------------------------
# Volontairement COURTS. « contexte » n'y figure plus : « le contexte réglementaire
# a-t-il changé ? » est une question de fait. Le contexte reste atteignable comme
# LIVRABLE (« présente le contexte réglementaire »), donc sous garde de demande.
_ANALYTIC_MARKERS = (
    r"en detail|in detail|in depth|de maniere approfondie|"
    r"approfondie?s?|thorough(?:ly)?|comprehensive"
)


# ===========================================================================
# NORMALISATION ET SEGMENTATION
# ===========================================================================

#: Consigne CITÉE : le texte entre guillemets appartient au document, pas à
#: l'utilisateur. Les cinq familles de guillemets usuelles sont couvertes.
#:
#: L'apostrophe DROITE est le cas piégeux : en français elle est un caractère de mot,
#: pas un guillemet. « les points communs entre l'offre A et l'offre B » contient deux
#: apostrophes, et les traiter comme une citation effaçait le cœur de la question — les
#: deux objets comparés disparaissaient. Elle n'ouvre donc une citation que précédée
#: ET suivie d'une frontière de mot.
_QUOTED_SPAN_RE = re.compile(
    r"«[^»]{0,200}»|“[^”]{0,200}”|‘[^’]{0,200}’|\"[^\"]{0,200}\""
    r"|(?<![^\W\d_])'[^']{2,200}'(?![^\W\d_])"
)

#: Frontières de proposition. On coupe sur la ponctuation forte ET sur les
#: coordonnants qui introduisent une NOUVELLE demande — « et », « puis », « mais »,
#: « and », « then », « but ». Couper sur « et » est sans danger ICI parce que la
#: détection comparative à objets nommés travaille sur la phrase ENTIÈRE, en amont :
#: « Compare A et B » a déjà été tranché quand cette segmentation s'applique.
_CLAUSE_SPLIT_RE = re.compile(
    r"[.,;:!?\n()\[\]]+|\b(?:puis|ensuite|mais|et|and|then|but)\b"
)

#: Amorces de politesse / de requête, FR et EN, retirées avant de lire le verbe.
_REQUEST_LEAD_RE = re.compile(
    r"^(?:(?:bonjour|bonsoir|merci de|merci|stp|svp|s'il te plait|s'il vous plait|"
    r"peux[- ]tu|pourrais[- ]tu|pouvez[- ]vous|pourriez[- ]vous|tu peux|vous pouvez|"
    r"j'aimerais|je voudrais|je souhaite|je veux|il faut|maintenant|alors|donc|"
    r"hello|hi|please|kindly|could you|can you|would you|will you|"
    r"i'd like|i would like|i want|let's|lets|now|so|to|"
    # Adverbes de manière antéposés : « Critically assess… », « Analyse brièvement… ».
    # Ils modifient la consigne, ils ne la suppriment pas.
    r"critically|briefly|carefully|thoroughly|quickly|precisely|"
    r"brievement|rapidement|succinctement|precisement|attentivement)"
    r"\s+(?:de\s+|d'|que tu\s+|me\s+|nous\s+|to\s+|and\s+)?)+"
)

#: Terminaison de PARTICIPE PASSÉ, lue sur le texte ORIGINAL : seul le « é »
#: ACCENTUÉ en fin de verbe la signale. « Détaille » (accent en tête) n'en est pas un.
_PAST_PARTICIPLE_RE = re.compile(r"é(?:e?s?)$", re.IGNORECASE)

#: Participes passés ANGLAIS : l'anglais n'a pas d'accent pour les trahir, mais ses
#: participes analytiques se terminent tous en « -ed » (ou sont irréguliers connus).
#: `analyzed`, `compared`, `summarised`… décrivent un état, pas une consigne.
_PAST_PARTICIPLE_EN_RE = re.compile(r"(?:ed|written|drawn|given)$", re.IGNORECASE)

#: NÉGATION d'une consigne. Évaluée PROPOSITION PAR PROPOSITION : « Ne compare pas
#: les dispositifs, mais explique leurs différences » doit rester une demande
#: d'explication. Une garde globale annulerait la phrase entière et perdrait la
#: consigne positive finale.
_NEGATED_REQUEST_RE = re.compile(
    r"\bne\s+\w+\s+pas\b|\bn'\w+\s+pas\b|\bne pas\s+\w+|"
    r"\bsans\s+\w+(?:er|ir|re)\b|\binutile d[e']|\bpas besoin d[e']|"
    r"\bdo not\b|\bdon't\b|\bwithout\s+\w+ing\b|\bno need to\b|\bnever\b"
)


def _strip_quoted_instructions(raw: str) -> str:
    """Neutralise les consignes CITÉES sans casser une demande PORTANT sur une citation.

    « Le document indique : "analyse les risques" » ne demande rien : la consigne est
    dans la citation. « Analyse la phrase "les contrôles sont insuffisants" » demande
    bien une analyse : le verbe est HORS citation, et le retrait de la citation le
    laisse intact. Retirer la citation traite donc les deux cas d'un seul geste.
    """
    return _QUOTED_SPAN_RE.sub(" ", raw or "")


def _clause_spans(folded: str) -> list[tuple[int, int]]:
    """Bornes des propositions dans le texte REPLIÉ (donc valides sur l'original)."""
    spans: list[tuple[int, int]] = []
    cursor = 0
    for sep in _CLAUSE_SPLIT_RE.finditer(folded):
        if sep.start() > cursor:
            spans.append((cursor, sep.start()))
        cursor = sep.end()
    if cursor < len(folded):
        spans.append((cursor, len(folded)))
    return spans


@dataclass(frozen=True)
class _Clause:
    """Proposition prête à être interrogée : repliée, alignée, et son état de négation."""

    folded: str
    raw: str
    negated: bool


def _clauses(question: str, *, strip_quotes: bool = True) -> list[_Clause]:
    """Découpe la question en propositions exploitables.

    Les citations sont neutralisées AVANT le découpage : une pseudo-consigne citée ne
    doit jamais devenir une proposition à part entière.
    """
    raw = _strip_quoted_instructions(question) if strip_quotes else (question or "")
    folded = _fold(raw)
    out: list[_Clause] = []
    for start, end in _clause_spans(folded):
        segment = folded[start:end]
        shift = len(segment) - len(segment.lstrip())
        folded_clause = segment.strip()
        if not folded_clause:
            continue
        raw_clause = raw[start + shift: start + shift + len(folded_clause)]
        out.append(_Clause(
            folded=folded_clause,
            raw=raw_clause,
            negated=bool(_NEGATED_REQUEST_RE.search(folded_clause)),
        ))
    return out


def _leading_request(clause: _Clause, verbs: str) -> str | None:
    """Ce qui SUIT `verbs` employé en TÊTE de cette proposition, sinon ``None``.

    Trois gardes, et c'est tout ce qui sépare une consigne d'un constat :

    - **position** : un verbe qui n'ouvre pas la proposition appartient à un tiers
      (« le rapport compare plusieurs valeurs ») ;
    - **accent final** (FR) : « comparé », « analysé » sont des participes que le
      repliage rend identiques à l'impératif ;
    - **terminaison -ed** (EN) : même rôle, l'anglais n'ayant pas d'accent.

    Renvoie `""` quand le verbe est seul — une commande elliptique (« Développe. »)
    est une demande valide, mais l'appelant doit pouvoir la distinguer.

    La NÉGATION est jugée sur ce qui PRÉCÈDE le verbe, pas sur la proposition entière :
    dans « Compare les dates sans analyser les causes », le « sans » nie l'analyse, pas
    la comparaison. Une garde par proposition annulerait la consigne positive qui
    l'ouvre. En pratique, « ne compare pas » est déjà écarté par la position — c'est
    « ne » qui ouvre la proposition, pas le verbe.
    """
    lead = _REQUEST_LEAD_RE.match(clause.folded)
    offset = lead.end() if lead else 0
    match = re.match(rf"(?:{verbs})\b", clause.folded[offset:])
    if match is None:
        return None
    if _NEGATED_REQUEST_RE.search(clause.folded[:offset + match.start()]):
        return None
    raw_verb = clause.raw[offset + match.start(): offset + match.end()]
    if _PAST_PARTICIPLE_RE.search(raw_verb) or _PAST_PARTICIPLE_EN_RE.search(raw_verb):
        return None
    return clause.folded[offset + match.end():].strip()


# --- motifs COMPILÉS une seule fois -------------------------------------------
_ANALYTIC_VERBS_ALL = f"{_ANALYTIC_REQUEST_VERBS_FR}|{_ANALYTIC_REQUEST_VERBS_EN}"
_PRODUCTION_VERBS_ALL = f"{_PRODUCTION_VERBS_FR}|{_PRODUCTION_VERBS_EN}"
_DELIVERABLES_ALL = f"{_ANALYTIC_DELIVERABLES_FR}|{_ANALYTIC_DELIVERABLES_EN}"
_COMPARATIVE_VERBS_ALL = f"{_COMPARATIVE_REQUEST_VERBS_FR}|{_COMPARATIVE_REQUEST_VERBS_EN}"

_ANALYTIC_INTERROGATIVE_RE = re.compile(
    rf"\b(?:{_ANALYTIC_INTERROGATIVES_FR}|{_ANALYTIC_INTERROGATIVES_EN})\b"
)
_QUANTITATIVE_HOW_RE = re.compile(rf"\b{_QUANTITATIVE_HOW_EN}\b")
_ANALYTIC_IDIOM_RE = re.compile(rf"\b(?:{_ANALYTIC_IDIOMS})\b")
_ANALYTIC_MARKER_RE = re.compile(rf"\b(?:{_ANALYTIC_MARKERS})\b")

#: Livrable demandé « dans la foulée » d'un verbe de production.
_DELIVERABLE_NEARBY_RE = re.compile(rf"^.{{0,40}}?\b(?:{_DELIVERABLES_ALL})\b")

# --- FORME NOMINALE : l'énoncé ENTIER est un groupe nominal ---------------------
#
# Doctrine. « Synthèse des écarts constatés. » est une commande elliptique ; « Le
# rapport compare plusieurs valeurs. » est un constat. Les deux commencent par un
# livrable. Ce qui les sépare, c'est que le premier est un groupe nominal COMPLET :
# déterminant optionnel, modifieurs, livrable, puis soit la fin de l'énoncé, soit un
# complément prépositionnel. Dès qu'un verbe suit le livrable, l'énoncé n'est plus un
# groupe nominal et la règle ne s'applique pas — sans avoir à énumérer les verbes.
_NP_DETERMINER = r"(?:une?|le|la|les|l'|des|du|mon|ma|mes|ce|cet|cette|a|an|the|this|these)"
_NP_MODIFIER = r"[a-z0-9'-]+"
_NP_PREPOSITION = r"(?:de|des|du|d'|sur|pour|entre|dans|of|on|for|about|regarding|across)"
_NOMINAL_REQUEST_RE = re.compile(
    rf"^(?:{_NP_DETERMINER}\s+)?"
    rf"(?:{_NP_MODIFIER}\s+){{0,2}}"
    rf"(?:{_DELIVERABLES_ALL})"
    # Les modifieurs qui SUIVENT le livrable ne peuvent pas être des déterminants :
    # un déterminant après le livrable signale un COMPLÉMENT D'OBJET, donc un verbe
    # avant lui. C'est ce qui sépare « Une analyse plus poussée ? » (groupe nominal)
    # de « Le dispositif analyse les événements. » (phrase : sujet + verbe + objet).
    rf"(?:\s+(?!{_NP_DETERMINER}\b){_NP_MODIFIER}){{0,2}}"
    rf"(?:\s+{_NP_PREPOSITION}\s+[^.?!]{{2,90}})?$"
)
#: Interrogatifs FACTUELS : ils demandent un nom, une date, un auteur, un nombre.
#: Leur présence en tête interdit la lecture « groupe nominal » — et, plus
#: généralement, toute lecture analytique par forme nominale ou ellipse.
_FACTUAL_INTERROGATIVE_RE = re.compile(
    r"^(?:quel(?:le|s|les)?|qui|quand|ou|combien|"
    r"what|which|who|whom|whose|when|where|how many|how much|how long)\b"
)

#: VERBE CONJUGUÉ. Sa présence interdit la lecture « groupe nominal » : un énoncé qui
#: porte un verbe conjugué est une phrase, donc un CONSTAT ou une question fermée sur
#: un document (« le panorama des risques est-il à jour ? », « the detailed report was
#: published on 3 May »), jamais une commande elliptique. C'est cette garde — et non
#: une liste de verbes analytiques — qui sépare « Synthèse des écarts constatés. » de
#: « La synthèse a été rédigée par le contrôle interne. ».
_FINITE_VERB_RE = re.compile(
    r"\b(?:est|sont|etait|etaient|sera|seront|fut|furent|"
    r"a ete|ont ete|avait ete|figure|figurent|contient|contiennent|indique|indiquent|"
    r"comporte|comportent|existe|reste|demeure|peut|doit|"
    # Verbes de DISCOURS RAPPORTÉ conjugués. Une fois la citation neutralisée, il ne
    # reste que « la note cite » — un groupe nominal suivi d'un verbe, donc une phrase,
    # jamais une commande. Sans eux, « La note cite "explique les modalités" » se lisait
    # comme la forme nominale « la note ».
    r"cite|citent|mentionne|mentionnent|reprend|reprennent|emploie|emploient|"
    r"dispose|precise que|commence|stipule|prevoit|"
    r"says|say|contains|contain|mentions|mention|quotes|quote|repeats|repeat|"
    r"uses|use|states|state|starts|start|provides|provide|"
    r"is|are|was|were|has|have|had|been|will|would|shall|should|"
    r"can|could|may|might|must|does|do|did)\b"
    r"|[- ]t[- ](?:il|elle|ils|elles|on)\b"
)

# --- ELLIPSES autonomes ---------------------------------------------------------
#
# On ne reconstruit PAS le contexte conversationnel : seules les ellipses qui se
# suffisent à elles-mêmes sont reconnues. « Et les autres ? » ou « Le reste ? »
# restent factuelles — leur intention n'est pas dans la phrase.
_ELLIPSIS_RE = re.compile(
    r"^(?:et\s+|and\s+)?"
    r"(?:les?\s+|la\s+|the\s+|une?\s+|an?\s+)?"
    r"(?:plus de detail|plus de details|more detail|more details|"
    r"impacts?|causes?|consequences?|implications?|enjeux|"
    r"root causes?)"
    r"\s*\??$"
)


def analytic_intent_family(question: str) -> str | None:
    """Famille de règle ayant classé la question comme analytique, ou ``None``.

    Sert l'EXPLICABILITÉ : `is_explanatory_question` ne rend qu'un booléen, mais
    savoir *laquelle* des sept familles a tranché est indispensable pour diagnostiquer
    un faux positif. Valeurs : ``interrogative``, ``idiom``, ``marker``,
    ``analytic_verb``, ``production_deliverable``, ``nominal_form``, ``ellipsis``.
    """
    raw = question or ""
    if not raw.strip():
        return None
    cleaned = _strip_quoted_instructions(raw)
    folded_full = _fold(cleaned).strip()
    if not folded_full:
        return None

    clauses = _clauses(raw)
    open_clauses = [c for c in clauses if not c.negated]

    # (1) interrogatif explicatif — sur une proposition NON niée, hors quantitatifs.
    for clause in open_clauses:
        if _QUANTITATIVE_HOW_RE.search(clause.folded):
            continue
        if _ANALYTIC_INTERROGATIVE_RE.search(clause.folded):
            return "interrogative"

    # (2) locution analytique figée.
    for clause in open_clauses:
        if _ANALYTIC_IDIOM_RE.search(clause.folded):
            return "idiom"

    # (3) marqueur lexical non ambigu (« en détail », « approfondie »).
    for clause in open_clauses:
        if _ANALYTIC_MARKER_RE.search(clause.folded):
            return "marker"

    # (4) verbe analytique en tête de demande.
    for clause in clauses:
        rest = _leading_request(clause, _ANALYTIC_VERBS_ALL)
        if rest is None:
            continue
        # Objet présent, ou commande elliptique réduite au verbe seul (« Développe. »).
        if rest or len(clauses) == 1:
            return "analytic_verb"

    # (5) verbe de production suivi d'un LIVRABLE analytique.
    for clause in clauses:
        rest = _leading_request(clause, _PRODUCTION_VERBS_ALL)
        if rest and _DELIVERABLE_NEARBY_RE.search(rest):
            return "production_deliverable"

    # (6) forme nominale : l'énoncé ENTIER est un groupe nominal à tête de livrable.
    stripped = folded_full.rstrip(" .?!").strip()
    nominal_allowed = (
        not _FACTUAL_INTERROGATIVE_RE.match(stripped)
        and not _NEGATED_REQUEST_RE.search(stripped)
        and not _FINITE_VERB_RE.search(stripped)
    )
    if nominal_allowed and _NOMINAL_REQUEST_RE.match(stripped):
        return "nominal_form"

    # (7) ellipse autonome.
    if nominal_allowed and _ELLIPSIS_RE.match(stripped):
        return "ellipsis"

    return None


def is_explanatory_question(question: str) -> bool:
    """La question appelle-t-elle une réponse DÉVELOPPÉE (analyse, explication, rapport) ?

    Déclenche la politique de prompt ``explain`` (cf. ``prompt_builder``) et, en amont,
    l'expansion de requête historique (cf. ``rewrite.build_queries_for_search``).

    Un mot analytique isolé ne suffit PAS : « Quel est le nom du rapport ? » reste une
    question factuelle. Voir `analytic_intent_family` pour la famille ayant tranché.
    """
    return analytic_intent_family(question) is not None


# ===========================================================================
# Intention COMPARATIVE — signal fort exigé (deux objets mis en relation)
# ===========================================================================
#
# Doctrine, établie par mesure (cf. docs/06-ai/answerability.md § sur-abstention) :
#
#   un mot du vocabulaire de l'ÉCART n'est pas un signal de COMPARAISON.
#
# La première version déclenchait sur des lexèmes isolés (« écart », « différent »,
# « avantage », « distinguer »…). Conséquence mesurée sur une question de CONSTAT
# (« quels contrôles ont montré des écarts ou des limites ? ») : le gabarit comparatif
# s'appliquait, le modèle comparait alors les SOURCES entre elles au lieu des objets,
# perdait le rattachement aux entités que produisait la politique standard, et ajoutait
# une affirmation non étayée. Un faux positif comparatif coûte plus qu'il ne rapporte.
#
# Règle retenue : intention comparative explicite ET deux objets coordonnés, tous deux
# repérés par un motif SYNTAXIQUE générique. En cas de doute → PAS de `compare` : la
# politique standard ou explicative est le repli le moins risqué.
#
# Hors périmètre assumé (repli sûr, jamais de faux positif) : les comparaisons à trois
# termes séparés par des virgules (« entre A, B et C ») et les objets implicites sous
# forme NOMINALE (« comparaison des résultats » — comparés à quoi ?).
#
# Précision du 30/07/2026 — cette doctrine vise le vocabulaire NOMINAL de l'écart. Le
# VERBE « comparer » à l'impératif n'en fait pas partie : c'est une consigne, pas un
# constat. Il déclenche donc `compare` même sans objets nommés, mais SANS en inventer —
# `objects` reste None, l'expansion déterministe de requêtes ne s'applique pas, et seule
# la politique de prompt change. Voir docs/06-ai/politique-comparative.md §1bis.

#: Segment d'objet : borné, sans ponctuation forte ni virgule (qui signalent une
#: énumération ou une autre proposition). Deux variantes : non-gourmande pour le
#: segment de GAUCHE (qui doit s'arrêter au séparateur), gourmande pour celui de DROITE
#: (qui doit courir jusqu'à la fin de la proposition).
#: Les motifs ANCRÉS (relation nommée + « entre … et … », verbe + préposition) portent
#: le signal dans leur charpente : une étiquette d'une seule lettre y est un objet
#: légitime (« différences entre A et B »). L'opérateur nu « A vs B » n'a, lui, aucune
#: amorce pour confirmer que « A » est un objet — il garde donc le plancher à deux
#: caractères (cf. la doctrine verrouillée dans test_comparative_intent).
_SEG_L = r"(?P<a>[^?!.;:,\n]{2,80}?)"
_SEG_R = r"(?P<b>[^?!.;:,\n]{2,80})"
_SEG_L1 = r"(?P<a>[^?!.;:,\n]{1,80}?)"
_SEG_R1 = r"(?P<b>[^?!.;:,\n]{1,80})"

#: « avantages et inconvénients » / « similitudes et différences » sont des collocations
#: comparatives : prises ENSEMBLE elles annoncent une mise en regard, là où « quels
#: avantages présente ce dispositif ? » reste un constat. Elles restent soumises à la
#: même exigence que les autres noms de relation : deux objets coordonnés derrière.
_REL_FR = (
    r"(?:difference|differences|ecart|ecarts|divergence|divergences|point commun|"
    r"points communs|similitude|similitudes|comparaison|comparatif|distinction|"
    r"distinctions|mise en regard|avantages? et inconvenients?|"
    r"similitudes? et differences?|points communs et differences?)"
)
_REL_EN = (
    r"(?:difference|differences|similarity|similarities|comparison|gap|gaps|"
    r"distinction|distinctions|divergence|divergences|pros and cons|"
    r"respective strengths and weaknesses|similarities and differences)"
)

#: Motifs comparatifs. Chacun DOIT capturer exactement deux segments d'objet.
#: Le booléen final autorise les objets d'un seul caractère (motifs ancrés — cf. `_SEG_L1`).
_PATTERNS: tuple[tuple[str, re.Pattern[str], bool], ...] = (
    # « différences entre A et B », « écarts entre A et B », « points communs entre A et B »
    (
        "relation_entre_fr",
        re.compile(rf"\b{_REL_FR}\b[^?!.;:\n]{{0,40}}?\bentre\s+{_SEG_L1}\s+et\s+{_SEG_R1}"),
        True,
    ),
    # « avantages (et inconvénients) respectifs de A et de B »
    (
        "respectifs_fr",
        re.compile(
            r"\b(?:avantages?|inconvenients?|merites?|forces?|faiblesses?)\b[^?!.;:\n]{0,30}?"
            rf"\brespectifs?\b[^?!.;:\n]{{0,20}}?\b(?:de|des|du|entre)\s+{_SEG_L1}\s+et\s+{_SEG_R1}"
        ),
        True,
    ),
    # « differences between A and B », « similarities between A and B »
    (
        "relation_between_en",
        re.compile(rf"\b{_REL_EN}\b[^?!.;:\n]{{0,40}}?\bbetween\s+{_SEG_L1}\s+and\s+{_SEG_R1}"),
        True,
    ),
    # « compare A with/to/and B », « compared A against B ». Évalué AVANT la variante
    # française : sans cela, « compare system A with system B » se ferait couper sur le
    # « A » (replié en « a », homographe de la préposition « à »).
    (
        "compare_verb_en",
        re.compile(
            rf"\b(?:compared?|contrasted?|benchmarked?|weighed?|set)\b\s+{_SEG_L1}"
            rf"\s+(?:with|to|and|against|versus)\s+{_SEG_R1}"
        ),
        True,
    ),
    # « compare A et/avec/à/au B », « comparer A à B », « comparez A avec B »
    (
        "compare_verbe_fr",
        re.compile(
            rf"\b(?:compar(?:e|es|er|ez|ons)|confront(?:e|es|ez|er)|oppos(?:e|es|ez|er))\b"
            rf"\s+{_SEG_L1}\s+(?P<sep>et|avec|a|au|aux)\s+{_SEG_R1}"
        ),
        True,
    ),
    # « en quoi A diffère(-t-il) de B ». L'amorce « en quoi » est ce qui sépare la
    # DEMANDE de comparaison du simple constat d'écart : « quels éléments diffèrent de
    # la procédure attendue ? » n'a pas cette amorce et reste hors périmètre.
    (
        "en_quoi_differe_fr",
        re.compile(
            rf"\ben quoi\s+{_SEG_L1}\s+diff(?:ere|erent|erencie|erencient)"
            rf"(?:[- ]t[- ](?:il|elle|elles|ils|on))?\s+(?:de la|de l'|du|des|de|d')\s*{_SEG_R1}"
        ),
        True,
    ),
    # « how does A differ from B », « how do A and B differ »
    (
        "how_differ_en",
        re.compile(
            rf"\bhow\s+(?:does|do|did)\s+{_SEG_L1}\s+differ\s+from\s+{_SEG_R1}"
        ),
        True,
    ),
    # « A vs B », « A versus B » — opérateur binaire par construction, sans amorce :
    # c'est le seul motif où la longueur des objets est encore le garde-fou.
    ("versus", re.compile(rf"{_SEG_L}\s+(?:vs|versus)\b\s+{_SEG_R}"), False),
)

#: Locutions comparatives SANS objets nommés. Elles annoncent une mise en regard de
#: façon assez explicite pour déclencher la politique, mais rien n'en est extrait.
_COMPARATIVE_IDIOM_RE = re.compile(
    r"\b(?:compare and contrast|"
    r"forces et faiblesses respectives|avantages et inconvenients respectifs|"
    r"points communs et differences|similitudes et differences|"
    r"respective strengths and weaknesses|similarities and differences|"
    r"pros and cons of|"
    r"comparaison avec \d{4}|compared with \d{4}|"
    r"par rapport a l'an dernier|versus last year|"
    r"how do(?:es)? [^?.!]{0,60}\bdiffer\b)\b"
)

_WORD_RE = re.compile(r"[a-z0-9]+")

#: Mots vides FR + EN. Un segment qui n'en contient QUE ne désigne aucun objet.
_SEGMENT_STOPWORDS = {
    "le", "la", "les", "un", "une", "des", "du", "de", "d", "l", "au", "aux", "ce", "cet",
    "cette", "ces", "son", "sa", "ses", "leur", "leurs", "notre", "nos", "votre", "vos",
    "et", "ou", "en", "dans", "sur", "pour", "par", "avec", "sans", "chez", "vers",
    "deux", "autres", "autre", "meme", "memes", "tel", "telle",
    "the", "a", "an", "of", "in", "on", "for", "with", "and", "or", "to", "by", "two", "other",
}

#: Déterminants retirés en TÊTE d'un objet avant d'en faire une requête de recherche.
#: Retrait purement déterministe : aucun mot n'est ajouté, traduit ou deviné.
_LEADING_DETERMINERS = (
    "de la", "de l'", "du", "des", "de", "d'", "le", "la", "les", "l'", "au", "aux",
    "the", "of the", "of", "a", "an",
)


def _segment_tokens(segment: str) -> list[str]:
    return _WORD_RE.findall(_fold(segment))


def _is_meaningful_segment(segment: str, *, allow_short: bool = False) -> bool:
    """Un objet doit porter au moins un mot plein d'au moins deux caractères.

    ``allow_short`` (motifs ANCRÉS uniquement) : une étiquette d'un seul caractère est
    alors admise. Dans « différences entre A et B », le « A » ne peut être qu'un objet —
    la charpente du motif l'a déjà établi. Le filtre de mots vides ne s'y applique pas :
    il existe pour écarter « le », « des », pas une étiquette homographe d'un mot vide.
    """
    for token in _segment_tokens(segment):
        if len(token) >= 2 and token not in _SEGMENT_STOPWORDS:
            return True
        if allow_short and len(token) == 1:
            return True
    return False


def _normalized_segment(segment: str) -> str:
    """Forme de comparaison — les mots courts sont CONSERVÉS.

    « le système A » et « le système B » ne diffèrent que par leur dernière lettre :
    les écarter comme identiques rejetterait une comparaison parfaitement explicite.
    """
    return " ".join(_segment_tokens(segment))


def _clean_object(segment: str) -> str:
    """Objet prêt à servir de requête : bords nettoyés, déterminant de tête retiré."""
    obj = (segment or "").strip(" \t'\"«»()[]-–—")
    low = _fold(obj)
    for det in _LEADING_DETERMINERS:
        if low.startswith(det + " ") or low.startswith(det + "'"):
            obj = obj[len(det):].lstrip(" '")
            break
    return obj.strip()


@dataclass(frozen=True)
class ComparativeIntent:
    """Décision de classification comparative, EXPLICABLE.

    Structure interne (jamais exposée telle quelle par l'API) : elle sert à tester
    *pourquoi* une question a été classée `compare`, et à alimenter l'expansion
    déterministe de requêtes sans refaire l'analyse (cf. `rewrite.build_queries_for_search`).

    Deux niveaux de confiance déclenchent la politique `compare`, et ils n'ont pas les
    mêmes conséquences :

    - ``explicit`` — deux objets NOMMÉS ont été identifiés. Ils alimentent l'expansion
      déterministe de requêtes (aucun appel LLM) ;
    - ``implicit`` — la DEMANDE de comparaison est explicite (verbe « comparer » à
      l'impératif, locution comparative) mais les objets restent sous-entendus
      (« compare les risques entre les deux dispositifs »). ``objects`` reste ``None`` :
      rien n'est deviné, et l'expansion de requête suit le chemin analytique historique.
    """

    is_comparative: bool
    matched_pattern: str | None = None
    objects: tuple[str, str] | None = None
    confidence: str = "none"  # "explicit" | "implicit" | "none"

    @property
    def has_multiple_objects(self) -> bool:
        return self.objects is not None


NO_COMPARATIVE_INTENT = ComparativeIntent(is_comparative=False)


def detect_comparative_intent(question: str) -> ComparativeIntent:
    """Détecte une demande de comparaison.

    Trois voies, dans cet ordre — la première seule fournit des objets exploitables :

    1. **objets coordonnés** (« différences entre A et B », « compare A à B », « A vs
       B », « en quoi A diffère de B », « how does A differ from B ») → ``explicit`` ;
    2. **locution comparative** (« compare and contrast », « forces et faiblesses
       respectives ») → ``implicit`` ;
    3. **verbe comparatif en tête de demande**, objets sous-entendus (« compare les
       risques entre les deux dispositifs », « contrast the two approaches ») →
       ``implicit``.

    Insensible à la casse et aux accents, bornes de mots systématiques (« vs » ne peut
    pas matcher à l'intérieur d'un mot). Une consigne CITÉE ou NIÉE ne déclenche jamais.
    Renvoie `NO_COMPARATIVE_INTENT` au moindre doute sur la voie 1 : objets vides,
    réduits à des mots vides, ou identiques après normalisation — auquel cas les voies
    2 et 3 peuvent encore trancher.
    """
    raw = question or ""
    if not raw.strip():
        return NO_COMPARATIVE_INTENT

    # Une pseudo-consigne citée n'est pas la demande de l'utilisateur.
    cleaned = _strip_quoted_instructions(raw)
    folded = _fold(cleaned)

    for name, pattern, allow_short in _PATTERNS:
        match = pattern.search(folded)
        if not match:
            continue
        # « à » se replie en « a » : sans cette vérification sur le texte ORIGINAL, la
        # lettre « A » d'un objet anglophone (« compare system A with… ») passerait pour
        # la préposition française et couperait la question au mauvais endroit.
        if "sep" in (pattern.groupindex or {}):
            sep_folded = match.group("sep")
            sep_raw = cleaned[match.start("sep"):match.end("sep")]
            if sep_folded == "a" and sep_raw.lower() != "à":
                continue
        # Une comparaison NIÉE n'est pas demandée.
        if _NEGATED_REQUEST_RE.search(folded[:match.start()]):
            continue
        # Positions issues du texte REPLIÉ, appliquées au texte ORIGINAL (même longueur).
        # Groupes NOMMÉS : la numérotation varie d'un motif à l'autre (le séparateur
        # français en capture un au milieu).
        left = cleaned[match.start("a"):match.end("a")]
        right = cleaned[match.start("b"):match.end("b")]
        if not (
            _is_meaningful_segment(left, allow_short=allow_short)
            and _is_meaningful_segment(right, allow_short=allow_short)
        ):
            continue
        if _normalized_segment(left) == _normalized_segment(right):
            continue
        obj_a, obj_b = _clean_object(left), _clean_object(right)
        if not obj_a or not obj_b:
            continue
        return ComparativeIntent(
            is_comparative=True,
            matched_pattern=name,
            objects=(obj_a, obj_b),
            confidence="explicit",
        )

    clauses = _clauses(raw)

    # Voie 2 — locution comparative explicite, objets non extraits. Cherchée sur la
    # phrase ENTIÈRE : la segmentation coupe sur « et » / « and », or ces locutions en
    # contiennent (« forces et faiblesses respectives », « pros and cons ») et une
    # recherche par proposition les couperait en deux.
    if not _NEGATED_REQUEST_RE.search(folded) and _COMPARATIVE_IDIOM_RE.search(folded):
        return ComparativeIntent(
            is_comparative=True, matched_pattern="comparative_idiom",
            objects=None, confidence="implicit",
        )

    # Voie 3 — la demande est explicite, les objets ne le sont pas. On ne devine AUCUN
    # objet : `objects` reste None, donc l'expansion déterministe ne s'applique pas.
    # Seule la politique de prompt change.
    for clause in clauses:
        rest = _leading_request(clause, _COMPARATIVE_VERBS_ALL)
        if rest:
            return ComparativeIntent(
                is_comparative=True, matched_pattern="compare_demande_fr",
                objects=None, confidence="implicit",
            )

    return NO_COMPARATIVE_INTENT


def is_comparative_question(question: str) -> bool:
    """La question demande-t-elle de COMPARER deux objets explicitement mis en relation ?

    Déclenche la politique de prompt ``compare`` (cf. ``prompt_builder``). Un terme de
    constat isolé (« écart », « avantage », « différent ») ne suffit PAS : voir
    `detect_comparative_intent` pour la doctrine et ses limites assumées.
    """
    return detect_comparative_intent(question).is_comparative
