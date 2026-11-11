# backend/apps/chat/services/excel_guard.py
from __future__ import annotations

"""
Garde DÉTERMINISTE des questions analytiques portant sur un classeur.

Le problème, et il n'est pas technique. Depuis le lot E2, un classeur budgétaire est
consultable. Quelqu'un demandera « quel est le budget total par direction ? ». Le RAG
documentaire ne voit que les **quelques extraits retrouvés** (top-k), jamais le classeur
entier : additionner ce qu'il voit produirait un **chiffre faux présenté comme
autoritaire**. C'est le pire échec possible pour ce produit.

Le lot E3 a ajouté des règles anti-agrégation dans le prompt. Une consigne dans un
prompt est une *intention*, pas une *garantie* : le modèle peut la contourner, et
rien ne le vérifie. Cette garde, elle, est du **code** — elle décide **avant** tout
retrieval, tout embedding et tout appel LLM.

Deux conditions, toutes deux nécessaires :

    A. la question demande une opération ANALYTIQUE EXHAUSTIVE
       (somme, moyenne, classement, regroupement, comptage, comparaison globale) ;
    B. le périmètre visé désigne bien un CLASSEUR.

Sans A, c'est une question documentaire ordinaire. Sans B, c'est peut-être un PDF —
et « quelle est la moyenne nationale du chômage ? » ne doit pas être bloquée sous
prétexte qu'un `.xlsx` traîne ailleurs dans l'espace.

Ce que la garde ne fait PAS : bloquer toute question contenant un mot de calcul.
« Quel est le total **indiqué sur la ligne** Atlas ? » est une LECTURE — le mot
« total » y est un libellé, pas une opération. C'est la distinction centrale, et
c'est elle qui décide de l'utilité de la garde : trop stricte, elle rend Excel
inutilisable ; trop laxiste, elle laisse passer un faux chiffre.

Aucun appel LLM. Aucune dépendance. Le résultat est reproductible.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

# --------------------------------------------------------------------------- #
# Intentions
# --------------------------------------------------------------------------- #
INTENT_DOCUMENT_LOOKUP = "document_lookup"
INTENT_LOCAL_COMPARISON = "local_comparison"
INTENT_EXHAUSTIVE_AGGREGATION = "exhaustive_aggregation"
INTENT_EXHAUSTIVE_COUNT = "exhaustive_count"
INTENT_EXHAUSTIVE_ENUMERATION = "exhaustive_enumeration"
INTENT_RANKING = "ranking"
INTENT_GROUPING = "grouping"
INTENT_GLOBAL_COMPARISON = "global_comparison"

SCOPE_WHOLE_WORKBOOK = "whole_workbook"
SCOPE_UNSPECIFIED = "unspecified"

#: Intentions qui exigent de parcourir TOUT le classeur -> irréalisables en mode
#: documentaire, donc bloquées.
BLOCKING_INTENTS = frozenset({
    INTENT_EXHAUSTIVE_AGGREGATION,
    INTENT_EXHAUSTIVE_COUNT,
    INTENT_EXHAUSTIVE_ENUMERATION,
    INTENT_RANKING,
    INTENT_GROUPING,
    INTENT_GLOBAL_COMPARISON,
    # M1 — `local_comparison` était défini, jamais bloqué, jamais testé : une
    # comparaison sur classeur retombait en silence vers le RAG vectoriel, qui ne
    # voit que le top-k. « Compare le total 2025 et le total 2024 » exige DEUX
    # agrégats exhaustifs ; y répondre depuis quelques extraits produit un chiffre
    # faux présenté comme autoritaire. Une comparaison n'est autorisée que si un
    # marqueur de LECTURE la rachète (`_LOCAL_READ` / `_CELL_REF`) — auquel cas
    # elle porte l'intention `document_lookup`, hors de cet ensemble.
    INTENT_LOCAL_COMPARISON,
})

#: Motifs journalisés (cf. observabilité) — ils disent POURQUOI on a refusé, sans
#: jamais divulguer de valeur du tableau.
REASON_TOTAL = "excel_total_requires_tabular_analysis"
REASON_LISTING = "excel_listing_may_be_partial"
REASON_POST_RETRIEVAL = "excel_analytics_post_retrieval"

#: Actions possibles. `DEFER` est la nouveauté : l'intention est analytique, mais on ne
#: sait pas ENCORE si elle porte sur un classeur. Seul le retrieval le dira.
ACTION_ALLOW = "allow"
ACTION_BLOCK = "block"
ACTION_DEFER = "defer_until_retrieval"


@dataclass(frozen=True)
class ExcelQueryDecision:
    """Décision de la garde. `applies=True` ⇒ la question est interceptée."""

    applies: bool
    intent: str
    reason: str
    confidence: str = "high"
    #: Amorce pour le futur moteur P2 : ce que l'utilisateur voulait vraiment.
    #: Volontairement PAUVRE — pas d'AST, pas de plan d'exécution dans ce lot.
    scope: str = SCOPE_UNSPECIFIED
    groups: tuple = field(default_factory=tuple)


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #
def _norm(text: str) -> str:
    """Minuscules, accents retirés, apostrophes et blancs normalisés.

    Les accents sont retirés pour que « médiane » et « mediane », « agrège » et
    « agrege » se comportent pareil : une faute d'accent est la plus banale des
    fautes de frappe, et elle ne doit pas ouvrir une brèche dans la garde.
    """
    s = (text or "").strip().lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.replace("’", "'").replace("`", "'")
    return re.sub(r"\s+", " ", s)


# --------------------------------------------------------------------------- #
# B) Contexte CLASSEUR
# --------------------------------------------------------------------------- #
# Un nom de fichier .xlsx explicitement cité.
_XLSX_FILE = re.compile(r"\b[\w .\-()]+\.xlsx\b")

# Vocabulaire de tableur. « feuille » est ambigu (une feuille de papier), mais dans
# une question analytique adressée à un RAG documentaire, le sens tableur domine
# largement — et la garde exige DE TOUTE FAÇON un signal analytique (condition A).
_WORKBOOK_WORDS = re.compile(
    r"\b("
    r"classeur|tableur|feuille|feuilles|onglet|onglets|"
    r"cellule|cellules|"
    r"fichier excel|excel|xlsx|"
    r"workbook|spreadsheet|worksheet|sheet|sheets|tab|cell|cells"
    r")\b"
)


def question_mentions_workbook(question: str) -> bool:
    q = _norm(question)
    return bool(_XLSX_FILE.search(q) or _WORKBOOK_WORDS.search(q))


# --------------------------------------------------------------------------- #
# A) Signaux ANALYTIQUES
# --------------------------------------------------------------------------- #

# 1) Opérations IMPÉRATIVES : le verbe dit explicitement « calcule ». Aucun doute
#    possible, aucun contexte de lecture ne peut les racheter.
_STRONG_OPS = {
    INTENT_EXHAUSTIVE_AGGREGATION: re.compile(
        r"\b("
        r"calcule[rz]?|calculer|"
        r"additionne[rz]?|additionner|"
        r"totalise[rz]?|totaliser|"
        r"fais? la somme|faire la somme|"
        r"cumule[rz]?|cumuler|"
        r"calculate|compute|sum up|add up|add all"
        r")\b"
    ),
    # NB : « classement » et « palmarès » sont des NOMS — ils peuvent être des
    # libellés (« la ligne "Classement 2025" »). Ils ne figurent donc PAS ici :
    # seuls les VERBES sont sans ambiguïté. Cf. _RANKING_NOUNS plus bas.
    INTENT_RANKING: re.compile(
        r"\b("
        r"classe[rz]?|classer|"
        r"ordonne[rz]?|trie[rz]? par|"
        r"top ?\d+|"
        r"rank|sort (all|the) .* by"
        r")\b"
    ),
    INTENT_GROUPING: re.compile(
        r"\b("
        r"regroupe[rz]?|regrouper|"
        r"ventile[rz]?|ventiler|"
        r"agrege[rz]?|agreger|"
        r"repartis|repartir|"
        r"group by|group (all|the)|aggregate|break down by"
        r")\b"
    ),
    INTENT_EXHAUSTIVE_COUNT: re.compile(
        r"\b("
        r"recense[rz]?|recenser|denombre[rz]?|denombrer|"
        r"compte tous|compte toutes|compte les|"
        r"count (all|the|every)"
        r")\b"
    ),
}

# 2) Noms d'agrégat. À EUX SEULS ils ne prouvent rien : « le total indiqué sur la
#    ligne Atlas » est une lecture. Ils ne bloquent que combinés à un ensemble ou
#    à un regroupement (voir plus bas).
_AGG_NOUNS = re.compile(
    r"\b("
    r"somme|total|totaux|totals|cumul|"
    r"sous-total|sous total|soustotal|subtotal|grand total|"
    r"moyenne|mediane|"
    r"minimum|maximum|"
    r"repartition|"
    r"ecart|evolution|"
    r"average|median|sum|breakdown"
    r")\b"
)

# 3) « … DE / DES / POUR <ensemble> » juste après un nom d'agrégat : « la moyenne DES
#    dépenses », « le total DU budget », « le total POUR AirWorks ». C'est cette
#    préposition qui transforme un libellé en opération sur un ENSEMBLE.
#
#    « pour » est admis parce que c'est la tournure spontanée d'une question métier
#    (« le total pour AirWorks ») — celle-là même qui, non bloquée, a produit un chiffre
#    de catégorie présenté comme le total de l'entreprise. « le total pour LA LIGNE
#    Atlas » reste racheté par la lecture locale (cf. _LOCAL_READ).
_AGG_OF_SET = re.compile(
    r"\b(somme|total|totaux|cumul|sous-total|subtotal|grand total|"
    r"moyenne|mediane|minimum|maximum|repartition|"
    r"average|median|sum)\b"
    r"\s+(?:general|generale|globale?|cumule)?\s*"
    r"(de|des|du|de la|de l'|d'|pour|of|for)\s+\w+"
)

# 3bis) TOTAL EN TÊTE DE QUESTION, sans préposition. L'anglais construit le total en
#       ADJECTIF — « what is the TOTAL SHARES outstanding? » — là où le français exige
#       « le total DES actions ». `_AGG_OF_SET`, qui attend une préposition, ne voyait
#       donc rien : un anglophone demandant un total obtenait un chiffre.
#
#       Volontairement ancré en TÊTE de question. Un « total » au milieu d'une phrase
#       est le plus souvent un LIBELLÉ — le classeur porte littéralement une colonne
#       « % Total Fully Diluted » : la bloquer rendrait sa lecture impossible.
_TOTAL_HEAD = re.compile(
    r"\b(?:what|which)\s+(?:is|are)\s+the\s+total\b"
    r"|\bhow much\s+(?:is|are)\s+the\s+total\b"
    r"|^total\s+\w+"
)

# 4bis) Noms de classement. AMBIGUS : « la ligne "Classement 2025" » est un libellé.
#       Ils ne bloquent donc qu'après le rachat par lecture locale.
_RANKING_NOUNS = re.compile(r"\b(classement|palmares|ranking)\b")

# 4) Superlatifs : « le projet le plus coûteux », « le meilleur », « the highest ».
_SUPERLATIVE = re.compile(
    r"\b("
    r"le plus|la plus|les plus|le moins|la moins|les moins|"
    r"meilleur[es]?|pire[s]?|"
    r"highest|lowest|largest|smallest|biggest|best|worst|most|least"
    r")\b"
)

# 5) Regroupement par dimension : « par direction », « par région », « by department ».
#    On exclut les tournures où « par » n'est pas une dimension (« par exemple »,
#    « par rapport à », « par ailleurs »).
_GROUP_BY = re.compile(
    r"\bpar\s+(?!exemple|rapport|ailleurs|contre|consequent|ici|la\b)([a-z][\w'-]{2,})"
    r"|\bby\s+(?!the way)([a-z][\w'-]{2,})"
    r"|\bpour chaque\s+([a-z][\w'-]{2,})"
    r"|\bfor each\s+([a-z][\w'-]{2,})"
)

# 6) Comptage d'un ENSEMBLE : « combien DE projets ». À distinguer de « combien
#    VAUT le budget d'Atlas », qui lit une valeur.
#    Deux pièges, tous deux corrigés ici :
#    - « nombre D'ACTIONS » : l'élision ne laisse AUCUNE espace après l'apostrophe.
#      L'écrire `(de|d')\s+` ne matchait donc jamais la forme élidée — la plus courante
#      en français. C'est `(?:de\s+|d')` qu'il faut ;
#    - « le NOMBRE TOTAL d'actions » : l'adjectif intercalé ne doit pas ouvrir de brèche.
_COUNT_OF_SET = re.compile(
    r"\bcombien\s+(?:y\s+a-?t-?il\s+)?(?:de\s+|d')\w+"
    r"|\bnombre\s+(?:total\s+|global\s+|cumule\s+|exact\s+)?(?:de\s+|d')\w+"
    r"|\bhow many\s+\w+"
    r"|\btotal number of\s+\w+"
)

# 7) Comparaison de plusieurs ensembles.
_COMPARE = re.compile(r"\b(compare[rz]?|comparer|comparaison|compare|comparison)\b")

# 7bis) DELTA entre deux périodes ou deux groupes : « l'évolution du CA entre 2024 et
#     2025 », « l'écart budget/réalisé », « la progression des ventes ». Le mot dit
#     explicitement qu'on veut une DIFFÉRENCE — donc au moins deux agrégats, donc deux
#     parcours exhaustifs. Sans cette règle, « évolution » retombait en
#     `document_lookup` : c'était la seconde moitié de l'angle mort M1.
_DELTA_NOUNS = re.compile(
    r"\b("
    r"evolution|evolutions|"
    r"variation|variations|"
    r"progression|progressions|"
    r"croissance|decroissance|"
    r"difference|differences|"
    r"ecart|ecarts|"
    r"delta|"
    r"growth|change|trend|variance"
    r")\b"
)

# « entre 2024 et 2025 », « de 2024 a 2025 » : deux bornes explicites. Combiné à un
# agrégat ou à un delta, c'est une comparaison de périodes.
_BETWEEN_PERIODS = re.compile(
    r"\b(entre|de)\b[^.]{0,40}\b(et|a|vs|versus)\b"
)

# 7ter) Comparaison IMPLICITE, sans le verbe « comparer » : « le total 2025
#     dépasse-t-il le total 2024 ? », « le CA est-il supérieur à celui de 2024 ? ».
#     C'est bien une comparaison de deux grandeurs — elle ne devient bloquante que
#     combinée à un nom d'agrégat (`_AGG_NOUNS`), sinon « le prix dépasse-t-il 100 € »
#     serait refusé alors que c'est une lecture.
_COMPARATIVE_PREDICATE = re.compile(
    r"\b("
    r"depasse|depassent|depasse-t-il|depasse-t-elle|"
    r"excede|excedent|"
    r"superieure?s?\s+(a|au|aux)|inferieure?s?\s+(a|au|aux)|"
    r"plus\s+(eleve|elevee|grand|grande|important|importante)|"
    r"moins\s+(eleve|elevee|grand|grande|important|importante)|"
    r"exceeds?|higher\s+than|lower\s+than|greater\s+than"
    r")\b"
)

# Référence de PÉRIODE : une année sur 4 chiffres, un mois, un trimestre. Comparer une
# mesure « à celle de 2024 » suppose d'agréger 2024 — même si le nom de la mesure
# (« CA », « effectif ») n'est pas un nom d'agrégat reconnaissable. C'est ce signal,
# et non le nom de la mesure, qui rend la comparaison exhaustive.
_PERIOD_REF = re.compile(
    r"\b("
    r"(19|20)\d{2}|"
    r"t[1-4]|trimestre|semestre|"
    r"janvier|fevrier|mars|avril|mai|juin|juillet|aout|septembre|octobre|"
    r"novembre|decembre|"
    r"annee|exercice|mois"
    r")\b"
)

# 8) Portée EXHAUSTIVE : « tout le classeur », « toutes les lignes », « l'ensemble ».
#    Renforce la décision — et suffit à transformer un nom d'agrégat en opération.
_EXHAUSTIVE_SCOPE = re.compile(
    r"\b("
    r"tout le|toute la|tous les|toutes les|tous|toutes|"
    r"l'ensemble|"
    r"chaque|"
    r"integralite|totalite|"
    r"sans omission|sans exception|sans en oublier|"
    r"exhaustif|exhaustive|exhaustivement|"
    r"globale?ment|global|globale|"
    r"complet|complete|complets|completes|"
    r"du classeur|de ce classeur|du fichier|du tableau|"
    r"all the|all of|every|entire|whole|overall|each row"
    r")\b"
)

# 8bis) VERBES DE LISTE. Seuls, ils ne prouvent rien : « donne-moi la ligne Atlas » est
#       une lecture. C'est leur combinaison avec une portée EXHAUSTIVE (« toutes les
#       lignes », « chaque catégorie ») qui promet une liste complète — promesse que le
#       mode documentaire ne peut PAS tenir : il ne voit que les extraits retrouvés.
#
#       C'est le défaut constaté en production : sur onze lignes présentes, la réponse
#       en a rendu deux, sans jamais signaler qu'elle était partielle.
_LIST_VERBS = re.compile(
    r"\b("
    r"liste[rz]?|lister|"
    r"enumere[rz]?|enumerer|"
    r"detaille[rz]?|detailler|"
    r"donne[rz]?|donne-moi|"
    r"montre[rz]?|montrer|montre-moi|"
    r"affiche[rz]?|"
    r"cite[rz]?|citer|"
    r"list|enumerate|itemize|show|give"
    r")\b"
)

# 8quater) CIBLE D'UNE ÉNUMÉRATION. « TOUS LES ÉLÉMENTS de la colonne » ne porte aucun
#          verbe de liste, mais promet exactement la même exhaustivité. C'est la portée
#          (« tous les ») accolée à l'unité d'un tableau qui fait la demande.
_ENUM_TARGET = re.compile(
    r"\b("
    r"lignes?|colonnes?|cellules?|"
    r"elements?|entrees?|valeurs?|categories?|rubriques?|postes?|items?|"
    r"rows?|columns?|entries|values|categories"
    r")\b"
)

# 8ter) DÉSIGNATION D'UNE LIGNE PRÉCISE. « Donne toutes les valeurs DE LA LIGNE Common »
#       demande bien « tout », mais d'UN objet borné et retrouvable : c'est une lecture.
_ROW_SCOPED = re.compile(
    r"\b("
    r"sur la ligne|de la ligne|dans la ligne|a la ligne|pour la ligne|"
    r"cette ligne|la ligne\b|"
    r"of the row|in the row|the row\b"
    r")"
)

# 9) LECTURE LOCALE : la question désigne UNE valeur, UNE ligne, UNE cellule. Ces
#    marqueurs disent « lis-moi ce qui est écrit là », pas « calcule ».
#    C'est ce qui sauve « le total INDIQUÉ SUR LA LIGNE Atlas ».
_LOCAL_READ = re.compile(
    r"\b("
    r"indique[e]?|inscrit[e]?|figure|affiche[e]?|mentionne[e]?|intitule[e]?|"
    r"le champ|la case|la valeur de|la valeur du|"
    r"lis|lire|montre|affiche|donne-moi la ligne|"
    r"vaut-il|vaut-elle|vaut\b|"
    r"sur la ligne|de la ligne|dans la ligne|a la ligne|pour la ligne|"
    r"shown|displayed|labelled|labeled|listed|read the|the field|the cell"
    r")\b"
)

# Référence de cellule explicite (« en B12 », « la cellule B12 ») : lecture ponctuelle.
_CELL_REF = re.compile(r"\b[a-z]{1,3}\d{1,7}\b")


# --------------------------------------------------------------------------- #
# Classificateur
# --------------------------------------------------------------------------- #
def classify_excel_query(question: str, *, workbook_context: bool) -> ExcelQueryDecision:
    """Décide si une question exige un calcul exhaustif sur un classeur.

    ``workbook_context`` : la question porte-t-elle sur un classeur ? (nom de fichier
    `.xlsx`, vocabulaire de tableur, ou tour précédent ayant cité une source Excel —
    cf. ``thread_cites_excel``). C'est la condition B : sans elle, on ne bloque rien.
    """
    q = _norm(question)

    if not q:
        return ExcelQueryDecision(False, INTENT_DOCUMENT_LOOKUP, "empty_question")

    # -- Condition B ------------------------------------------------------
    if not workbook_context:
        return ExcelQueryDecision(
            False, INTENT_DOCUMENT_LOOKUP, "no_workbook_context",
        )

    # -- Condition A ------------------------------------------------------
    has_exhaustive_scope = bool(_EXHAUSTIVE_SCOPE.search(q))
    scope = SCOPE_WHOLE_WORKBOOK if has_exhaustive_scope else SCOPE_UNSPECIFIED

    # A.1 — Opération IMPÉRATIVE. « Calcule », « classe », « regroupe » : le verbe
    # est sans ambiguïté. Aucun marqueur de lecture ne peut le racheter — « calcule
    # le total indiqué » reste un calcul.
    for intent, rx in _STRONG_OPS.items():
        if rx.search(q):
            return ExcelQueryDecision(
                True, intent, "explicit_operation_verb", "high", scope,
                _groups(q),
            )

    # A.2 — Regroupement par dimension : « le budget total PAR DIRECTION ».
    groups = _groups(q)
    if groups and (_AGG_NOUNS.search(q) or _COUNT_OF_SET.search(q) or _SUPERLATIVE.search(q)):
        return ExcelQueryDecision(
            True, INTENT_GROUPING, "grouping_by_dimension", "high",
            SCOPE_WHOLE_WORKBOOK, groups,
        )

    # A.2bis — ÉNUMÉRATION EXHAUSTIVE : « liste TOUTES les lignes », « énumère CHAQUE
    # catégorie ». Le verbe de liste seul ne suffit pas — c'est la portée exhaustive
    # qui fait la promesse intenable.
    #
    # Exceptions — toutes bloquées AILLEURS, ou bornées :
    #   - la question désigne UNE ligne ou UNE cellule (« toutes les valeurs de la
    #     ligne Common ») : l'objet est fini et retrouvable, c'est une lecture ;
    #   - un agrégat, un superlatif ou un comptage est présent. « Le total de toutes
    #     les catégories » est un CALCUL, « la valeur la moins élevée de tout le
    #     fichier » une SÉLECTION, « combien de lignes » un COMPTAGE. Tous trois sont
    #     bloqués plus bas — mais sous leur vraie intention, pour que le message de
    #     refus nomme le bon danger.
    if (
        (_LIST_VERBS.search(q) or _ENUM_TARGET.search(q))
        and has_exhaustive_scope
        and not _AGG_NOUNS.search(q)
        and not _SUPERLATIVE.search(q)
        and not _RANKING_NOUNS.search(q)
        and not _COUNT_OF_SET.search(q)
        and not _ROW_SCOPED.search(q)
        and not _CELL_REF.search(q)
    ):
        return ExcelQueryDecision(
            True, INTENT_EXHAUSTIVE_ENUMERATION, REASON_LISTING, "high",
            SCOPE_WHOLE_WORKBOOK, groups,
        )

    # -- Rachat par LECTURE LOCALE ---------------------------------------
    # À partir d'ici, les signaux sont AMBIGUS : « total », « maximum », « nombre »
    # peuvent être des LIBELLÉS. Un marqueur de lecture locale, ou une référence de
    # cellule, tranche en faveur de la lecture. C'est ici qu'on évite de rendre Excel
    # inutilisable à force de prudence.
    local = bool(_LOCAL_READ.search(q)) or bool(_CELL_REF.search(q))

    # A.3 — Comptage d'un ENSEMBLE : « combien DE projets », « nombre DE lignes ».
    # (« combien VAUT le budget d'Atlas » ne matche pas : ce n'est pas « combien de ».)
    if _COUNT_OF_SET.search(q) and not local:
        return ExcelQueryDecision(
            True, INTENT_EXHAUSTIVE_COUNT, "count_over_a_set", "high", scope, groups,
        )

    # A.4 — Classement implicite : « le projet LE PLUS coûteux DU CLASSEUR », ou un
    # nom de classement qu'aucun marqueur de lecture n'a racheté.
    if (_SUPERLATIVE.search(q) or _RANKING_NOUNS.search(q)) and not local:
        return ExcelQueryDecision(
            True, INTENT_RANKING, "superlative_over_a_set",
            "high" if has_exhaustive_scope else "medium", scope, groups,
        )

    # A.5 — Agrégat sur un ENSEMBLE : « la moyenne DES dépenses », « le total DU
    # budget », ou n'importe quel agrégat assorti d'une portée exhaustive.
    if not local and (
        _AGG_OF_SET.search(q)
        or _TOTAL_HEAD.search(q)
        or (_AGG_NOUNS.search(q) and has_exhaustive_scope)
    ):
        return ExcelQueryDecision(
            True, INTENT_EXHAUSTIVE_AGGREGATION, REASON_TOTAL,
            "high" if has_exhaustive_scope else "medium", scope, groups,
        )

    # A.6 — Comparaison GLOBALE : « compare toutes les agences », « compare
    # exhaustivement 2025 et 2026 ». Une comparaison LOCALE (« le budget d'Atlas
    # dépasse-t-il celui d'Orion ? ») reste autorisée : elle ne porte que sur des
    # faits retrouvés.
    if _COMPARE.search(q) and (has_exhaustive_scope or groups):
        return ExcelQueryDecision(
            True, INTENT_GLOBAL_COMPARISON, "global_comparison", "high",
            SCOPE_WHOLE_WORKBOOK, groups,
        )

    # A.7 — DELTA entre deux périodes/groupes : « l'évolution du CA entre 2024 et
    # 2025 », « l'écart entre le budget et le réalisé ». Un delta est une soustraction
    # de deux agrégats : il ne se lit pas, il se calcule. (M1)
    if not local and _DELTA_NOUNS.search(q):
        return ExcelQueryDecision(
            True, INTENT_EXHAUSTIVE_AGGREGATION, REASON_TOTAL,
            "high" if (has_exhaustive_scope or _BETWEEN_PERIODS.search(q)) else "medium",
            scope, groups,
        )

    # A.8 — COMPARAISON non rachetée par un marqueur de lecture. Sur un classeur, on ne
    # peut pas vérifier que les deux opérandes sont des valeurs MATÉRIALISÉES : « le
    # total 2025 » peut être une ligne TOTAL existante comme une somme à calculer. Le
    # doute se tranche du côté du refus explicite, jamais de la réponse plausible. (M1)
    if (_COMPARE.search(q)
            or (_COMPARATIVE_PREDICATE.search(q)
                and (_AGG_NOUNS.search(q) or _PERIOD_REF.search(q)))) and not local:
        return ExcelQueryDecision(
            True, INTENT_LOCAL_COMPARISON, REASON_TOTAL,
            "high" if has_exhaustive_scope else "medium", scope, groups,
        )

    # Rien de bloquant : question documentaire ordinaire — y compris une comparaison
    # explicitement LOCALE (« compare le montant de la ligne Atlas et celui d'Orion »),
    # qui ne porte que sur des faits retrouvés.
    return ExcelQueryDecision(False, INTENT_DOCUMENT_LOOKUP, "local_or_documentary")


def _groups(q: str) -> tuple:
    """Dimensions de regroupement détectées (« par direction » -> ("direction",)).

    Amorce pour le futur moteur P2 : on capture CE QUE l'utilisateur voulait, sans
    construire de plan d'exécution.
    """
    out = []
    for m in _GROUP_BY.finditer(q):
        g = next((x for x in m.groups() if x), None)
        if g and g not in out:
            out.append(g)
    return tuple(out[:5])


# --------------------------------------------------------------------------- #
# Contexte : le fil a-t-il déjà cité un classeur ?
# --------------------------------------------------------------------------- #
def sources_cite_excel(sources) -> bool:
    """Une liste de sources (contrat v3) contient-elle une citation de classeur ?"""
    for s in sources or []:
        if not isinstance(s, dict):
            continue
        meta = s.get("meta")
        if isinstance(meta, dict):
            kind = meta.get("kind")
            if isinstance(kind, str) and kind.startswith("excel_"):
                return True
    return False


def thread_cites_excel(thread) -> bool:
    """Le DERNIER tour assistant de ce fil citait-il un classeur ?

    C'est ce qui donne son sens à « quel est le budget total par direction ? » posé
    juste après une réponse issue de `Budget_2026.xlsx` : le « ce classeur » est
    implicite, mais le fil, lui, le sait.

    Volontairement borné au fil COURANT : on n'inspecte jamais les documents d'un
    autre RagSpace, ni l'espace entier — un `.xlsx` sans rapport ne doit pas faire
    basculer une question générale.
    """
    if thread is None:
        return False
    try:
        from apps.chat.models import ChatMessage

        last = (
            ChatMessage.objects.filter(thread=thread, role="assistant")
            .order_by("-created_at")
            .values_list("sources", flat=True)
            .first()
        )
        return sources_cite_excel(last)
    except Exception:  # pragma: no cover - la garde ne doit jamais casser le chat
        return False


# --------------------------------------------------------------------------- #
# Réponse
# --------------------------------------------------------------------------- #
ANSWER_KIND = "excel_analytics_unavailable"

_REFUSAL = (
    "Je peux **retrouver et citer** des lignes ou des cellules d'un classeur — en "
    "indiquant le fichier, la feuille et la plage — mais le mode documentaire ne "
    "garantit pas un **calcul exhaustif** sur toutes les feuilles et toutes les lignes.\n"
    "\n"
    "Je ne vois que les extraits retrouvés, jamais le classeur entier : additionner "
    "ce que je vois donnerait un chiffre **potentiellement faux**, présenté comme sûr. "
    "Je préfère vous le dire.\n"
    "\n"
    "L'analyse tabulaire exacte (sommes, moyennes, classements, regroupements) n'est "
    "pas encore disponible."
)

#: Reformulations DOCUMENTAIRES sûres, par intention. Aucune ne promet l'exhaustivité,
#: aucune ne réintroduit le calcul interdit, et aucune n'est envoyée automatiquement.
_SUGGESTIONS = {
    INTENT_EXHAUSTIVE_AGGREGATION: (
        "Quelles valeurs sont citées pour cette ligne dans les extraits retrouvés ?",
        "Montre-moi les lignes concernées, avec leur feuille et leur plage.",
    ),
    INTENT_EXHAUSTIVE_COUNT: (
        "Quelles lignes correspondantes apparaissent dans les extraits retrouvés ?",
        "Résume la feuille concernée.",
    ),
    INTENT_RANKING: (
        "Quels montants sont cités dans les extraits retrouvés ?",
        "Montre-moi la ligne de l'élément qui t'intéresse.",
    ),
    INTENT_GROUPING: (
        "Retrouve les lignes concernant l'un de ces groupes en particulier.",
        "Quels montants sont cités pour ce groupe dans les extraits retrouvés ?",
    ),
    INTENT_GLOBAL_COMPARISON: (
        "Montre-moi les lignes de chacun des éléments à comparer.",
        "Résume la feuille qui les contient.",
    ),
    INTENT_EXHAUSTIVE_ENUMERATION: (
        "Retrouve la ligne qui t'intéresse, avec sa feuille et sa plage.",
        "Quelles lignes apparaissent dans les extraits retrouvés ?",
    ),
}

#: Une demande de TOTAL a son propre danger : le classeur contient souvent une ligne
#: « TOTAL », mais rien ne dit que c'est le total que l'utilisateur a en tête (total
#: d'une feuille ? d'une section ? après dilution ?). Le mode documentaire ne peut pas
#: en décider — et surtout, il ne doit jamais rabattre sur la première ligne venue.
_REFUSAL_TOTAL = (
    "Je peux **retrouver et citer** une ligne ou une cellule précise du classeur — en "
    "indiquant le fichier, la feuille et la plage — mais je ne peux pas **garantir un "
    "total global**.\n"
    "\n"
    "Le risque est concret : une colonne comporte plusieurs catégories, et présenter la "
    "valeur de l'une d'elles comme le total de l'ensemble donnerait un chiffre **faux, "
    "énoncé avec assurance**. Si le classeur porte une ligne de total, demandez-la "
    "explicitement — je la citerai telle qu'elle est écrite.\n"
    "\n"
    "Le calcul exact d'un total nécessite l'analyse tabulaire déterministe, pas encore "
    "disponible."
)

#: Une demande de LISTE COMPLÈTE promet l'exhaustivité. Le mode documentaire ne voit
#: que les extraits retrouvés : il peut n'en rendre qu'une partie — et c'est
#: exactement ce qui s'est produit (deux lignes rendues sur onze, sans le dire).
_REFUSAL_ENUMERATION = (
    "Je peux **retrouver et citer** des lignes précises du classeur, mais le mode "
    "documentaire ne récupère que les **extraits pertinents** — pas forcément toutes "
    "les lignes d'un tableau.\n"
    "\n"
    "Je ne peux donc pas **garantir une liste exhaustive** : je risquerais d'en omettre "
    "sans vous le signaler, ce qui est pire qu'un refus. Demandez-moi plutôt une ligne "
    "ou une catégorie en particulier.\n"
    "\n"
    "L'énumération complète d'un tableau nécessite l'analyse tabulaire déterministe, "
    "pas encore disponible."
)

_REFUSALS = {
    INTENT_EXHAUSTIVE_AGGREGATION: _REFUSAL_TOTAL,
    INTENT_EXHAUSTIVE_ENUMERATION: _REFUSAL_ENUMERATION,
}


def build_refusal(decision: ExcelQueryDecision) -> tuple:
    """(réponse, suggestions). Déterministe — aucun appel LLM."""
    suggestions = list(_SUGGESTIONS.get(decision.intent, ()))
    if decision.groups:
        # On personnalise SANS jamais promettre le regroupement demandé.
        g = decision.groups[0]
        suggestions = [
            f"Retrouve les lignes concernant une {g} en particulier.",
            f"Quels montants sont cités pour cette {g} dans les extraits retrouvés ?",
        ]
    return _REFUSALS.get(decision.intent, _REFUSAL), suggestions[:2]


def guard(question: str, *, thread=None) -> Optional[ExcelQueryDecision]:
    """Point d'entrée unique. Renvoie une décision bloquante, ou ``None``.

    À appeler **avant** tout retrieval, tout embedding et tout appel LLM.
    """
    ctx = question_mentions_workbook(question) or thread_cites_excel(thread)
    decision = classify_excel_query(question, workbook_context=ctx)
    return decision if decision.applies and decision.intent in BLOCKING_INTENTS else None


def defer_decision(question: str) -> Optional[ExcelQueryDecision]:
    """L'intention est-elle analytique, INDÉPENDAMMENT de tout contexte classeur ?

    C'est la moitié manquante de la garde. Au PREMIER tour d'un fil neuf, « quel est le
    total des shares outstanding pour AirWorks ? » ne contient aucun mot de tableur, et
    l'historique est vide : la condition B (contexte classeur) est fausse, et la question
    part au LLM. La garde protégeait admirablement la SUITE d'une conversation, jamais
    son premier tour.

    On classe donc l'intention seule. Si elle est analytique, la décision est
    **différée** : le retrieval, lui, saura dire si un classeur est concerné.

    Renvoie ``None`` si rien d'analytique — la question suit son chemin normal.
    """
    decision = classify_excel_query(question, workbook_context=True)
    if not decision.applies or decision.intent not in BLOCKING_INTENTS:
        return None
    return decision


def sources_include_excel(sources) -> bool:
    """Au moins UNE source retrouvée est-elle un classeur ?

    Pas « toutes » : le top-k d'un classeur noyé dans un espace de revues de presse
    contient toujours des PDF parasites — exiger l'unanimité désarmerait la garde
    exactement là où elle est nécessaire.

    Pas « la meilleure » non plus : cela ferait dépendre une décision de sûreté du
    classement, précisément ce qui est instable ici.
    """
    for s in sources or []:
        if not isinstance(s, dict):
            continue
        src = s.get("source") if isinstance(s.get("source"), dict) else s
        meta = src.get("meta") if isinstance(src.get("meta"), dict) else {}
        kind = meta.get("kind")
        if isinstance(kind, str) and kind.startswith("excel_"):
            return True
        if meta.get("sheet_name"):
            return True
        fn = src.get("filename")
        if isinstance(fn, str) and fn.lower().endswith(".xlsx"):
            return True
    return False


def guard_after_retrieval(*, question: str, sources) -> Optional[ExcelQueryDecision]:
    """Seconde garde : intention analytique + au moins une source CLASSEUR -> refus.

    Elle s'exécute APRÈS le retrieval mais AVANT tout appel LLM. Le retrieval ne fabrique
    rien — seule la génération ment. Payer une recherche pour ne pas payer un chiffre
    faux est le bon échange.
    """
    decision = defer_decision(question)
    if decision is None or not sources_include_excel(sources):
        return None
    return decision


def guard_for_request(*, question: str, thread_id, rag_space) -> Optional[ExcelQueryDecision]:
    """Variante appelée par les vues chat, AVANT toute résolution de fil.

    Le fil n'est lu que s'il appartient au **RagSpace courant** : la garde n'inspecte
    jamais les documents ni les conversations d'un autre espace pour décider si une
    question concerne un classeur. C'est le même contexte d'autorisation que le chat.
    """
    ctx = question_mentions_workbook(question)

    if not ctx and thread_id and rag_space is not None:
        try:
            from apps.chat.models import ChatMessage

            last = (
                ChatMessage.objects.filter(
                    thread_id=thread_id,
                    thread__rag_space=rag_space,   # 🔒 isolation
                    role="assistant",
                )
                .order_by("-created_at")
                .values_list("sources", flat=True)
                .first()
            )
            ctx = sources_cite_excel(last)
        except Exception:  # pragma: no cover - la garde ne casse jamais le chat
            ctx = False

    decision = classify_excel_query(question, workbook_context=ctx)
    return decision if decision.applies and decision.intent in BLOCKING_INTENTS else None
