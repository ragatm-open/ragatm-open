"""Construction du dataset d'admission — module PUR (lot 4, v3).

Aucune dépendance Django, aucun accès base, aucun appel réseau : la commande
``build_admission_dataset`` s'occupe de la base et de la Gateway, ce module de la
seule chose qui décide de la validité d'une mesure — **comment les cas sont
dédupliqués, répartis, et comment un verdict humain les modifie**.

## Ce que la v1 avait de faux

Le découpage se faisait par empreinte de ``case_id``. Deux questions dérivées de
**deux chunks du même document** pouvaient donc tomber l'une en calibration, l'autre
en test. Or le retrieval ne traite pas des chunks isolés : un document bien classé
l'est pour toutes ses questions, un document mal découpé les pénalise toutes. Le jeu
de test contenait ainsi des documents déjà vus, et sa mesure était optimiste.

La v2 découpe par **groupe** : tous les cas d'un même document vont du même côté.
C'est le seul découpage qui rende le holdout réellement indépendant.

## Déduplication AVANT le découpage

Un modèle qui génère 88 questions en produit de quasi identiques. Dédupliquer après
le découpage laisserait un doublon de part et d'autre — une fuite de la même nature.
On déduplique donc d'abord, exactement puis par similarité de tokens.

## Verdicts de revue humaine

Le vocabulaire vit ici, et pas dans la commande, pour deux raisons : il définit la
sémantique du fichier (donc sa lecture par la calibration autant que son écriture
par la revue), et il doit pouvoir être éprouvé sans machinerie Django.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

# --- Catégories de négatifs et politique de difficulté ------------------------
#
# Chaque catégorie attaque une faiblesse différente d'un seuil absolu ; les réunir
# empêche de calibrer sur un seul mode d'échec et de croire le problème réglé.
#
# Elles ne se valident pas toutes de la même façon, et le confondre a coûté une
# campagne : le 02/08/2026, une génération complète a échoué avec `out_of_corpus`
# à 0/11 parce qu'on lui appliquait un seuil de proximité vectorielle.
#
#   `out_of_corpus` exige que le sujet soit ABSENT du fonds ;
#   `--min-negative-score` exige qu'il soit PROCHE du fonds.
#
# Les deux propriétés se contredisent. Un négatif d'absence loin du corpus n'est
# pas « trop facile » : c'est exactement ce qu'il doit être. D'où deux politiques,
# déclarées ici et non éparpillées dans la commande — ajouter une catégorie oblige
# désormais à choisir laquelle s'applique.

#: Le négatif doit être PROCHE : son meilleur score vectoriel doit atteindre le
#: seuil, sans quoi il ne mesure rien — aucune politique plausible ne l'admettrait.
POLICY_MIN_SCORE = "requires_minimum_vector_score"

#: Le négatif est défini par une ABSENCE. Aucun seuil de proximité ne s'applique ;
#: sa validité se démontre en cherchant la réponse dans le corpus, puis par une
#: revue humaine. Le score vectoriel reste conservé — comme signal, jamais comme
#: preuve : un score faible n'établit pas une absence.
POLICY_ABSENCE_REVIEW = "requires_absence_review"

NEGATIVE_CATEGORY_POLICIES = (POLICY_MIN_SCORE, POLICY_ABSENCE_REVIEW)

#: Motif d'exemption inscrit sur chaque cas exempté, pour qu'un lecteur du dataset
#: n'ait pas à deviner pourquoi un négatif à 0,42 a été conservé.
EXEMPTION_ABSENCE_REVIEW = "out_of_corpus_requires_absence_review"

#: ``{nom: (justification, politique)}`` — la politique est OBLIGATOIRE.
NEGATIVE_CATEGORY_SPECS: dict[str, tuple[str, str]] = {
    "out_of_corpus": (
        "sujet totalement absent du fonds documentaire",
        POLICY_ABSENCE_REVIEW,
    ),
    "lexical_lure": (
        "vocabulaire du corpus, fait absent — le piège le plus proche d'un vrai faux positif",
        POLICY_MIN_SCORE,
    ),
    "ambiguous_entity": (
        "entité nommée que le corpus ne désambiguïse pas",
        POLICY_MIN_SCORE,
    ),
    "too_vague": (
        "question sans objet identifiable, à laquelle tout passage répond mal",
        POLICY_MIN_SCORE,
    ),
    "false_premise": (
        "présuppose un fait que le corpus contredit ou ignore",
        POLICY_MIN_SCORE,
    ),
    "absent_temporality": (
        "période ou millésime hors de ce que le corpus couvre",
        POLICY_MIN_SCORE,
    ),
    "place_org_confusion": (
        "lieu ou organisation substitué à celui du corpus",
        POLICY_MIN_SCORE,
    ),
    "unsatisfiable": (
        "demande qu'un corpus documentaire ne peut structurellement pas satisfaire",
        POLICY_MIN_SCORE,
    ),
}

# Garde d'intégrité : une politique inconnue est une erreur de déclaration, pas une
# valeur à interpréter au runtime.
for _name, (_just, _pol) in NEGATIVE_CATEGORY_SPECS.items():
    if _pol not in NEGATIVE_CATEGORY_POLICIES:
        raise ValueError(f"politique inconnue pour {_name!r} : {_pol!r}")

#: ``{nom: justification}`` — forme historique, conservée : l'en-tête des datasets
#: la sérialise et la revue humaine la lit pour valider un réétiquetage.
NEGATIVE_CATEGORIES = {n: j for n, (j, _p) in NEGATIVE_CATEGORY_SPECS.items()}

#: Catégories soumises au seuil vectoriel, et catégories exemptées. Ordre stable.
SCORE_FILTERED_CATEGORIES = tuple(
    n for n, (_j, p) in NEGATIVE_CATEGORY_SPECS.items() if p == POLICY_MIN_SCORE
)
SCORE_EXEMPT_CATEGORIES = tuple(
    n for n, (_j, p) in NEGATIVE_CATEGORY_SPECS.items() if p == POLICY_ABSENCE_REVIEW
)


def category_policy(category: str) -> str:
    """Politique de difficulté d'une catégorie. Lève sur une catégorie inconnue."""
    try:
        return NEGATIVE_CATEGORY_SPECS[category][1]
    except KeyError:
        raise ValueError(f"catégorie de négatif inconnue : {category!r}") from None


def is_score_exempt(category: str) -> bool:
    """``True`` si le seuil de proximité ne s'applique PAS à cette catégorie."""
    return category_policy(category) == POLICY_ABSENCE_REVIEW


def score_stats(scores: list[float]) -> dict:
    """``min`` / ``median`` / ``p95`` / ``max`` d'une série de scores.

    Rendus pour que la distribution soit lisible SANS rejouer la campagne : la
    première génération v3 a échoué sur une catégorie, et le seul moyen de savoir
    pourquoi aurait été de relancer 440 appels de modèle. Un `kept_score_min` ne
    suffit pas — il ne dit rien des cas écartés.

    ``p95`` par rang, sur la série triée : sans interpolation, la valeur rendue est
    toujours un score réellement observé.
    """
    if not scores:
        return {"count": 0, "min": None, "median": None, "p95": None, "max": None}
    s = sorted(scores)
    n = len(s)
    mid = n // 2
    median = s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2
    p95 = s[min(n - 1, max(0, int(round(0.95 * (n - 1)))))]
    return {
        "count": n,
        "min": round(s[0], 4),
        "median": round(median, 4),
        "p95": round(p95, 4),
        "max": round(s[-1], 4),
    }

# --- Vocabulaire de revue -----------------------------------------------------
#
# La v2 n'offrait que `ok` / `reject` / `relabel`, et `relabel` SUPPRIMAIT le cas :
# le nom du verdict ne décrivait pas ce que le code faisait. Un cas presque bon ne
# pouvait être que gardé tel quel ou détruit — un relecteur consciencieux jetait
# donc de la matière utilisable, ce qui appauvrit le dataset sans que rien ne le dise.

#: Le cas est juste tel quel.
VERDICT_VALID = "valid"
#: Le cas devient juste APRÈS correction de son énoncé. L'ancienne et la nouvelle
#: valeur sont conservées : une correction non tracée est une réécriture silencieuse.
VERDICT_VALID_WITH_CORRECTION = "valid_with_correction"
#: Le relecteur ne peut pas trancher. Le cas RESTE dans le fichier — le retirer
#: masquerait la difficulté réelle du corpus — mais sort du calcul des métriques.
VERDICT_AMBIGUOUS = "ambiguous"
#: L'étiquetage est faux. Le cas est RE-ÉTIQUETÉ et reste dans le dataset — soit
#: en changeant de label (positive <-> negative), soit, à label constant, en
#: changeant de catégorie de négatif.
VERDICT_RELABEL = "relabel"
#: Le cas est inexploitable : il est retiré.
VERDICT_REJECT = "reject"

VERDICTS = (
    VERDICT_VALID,
    VERDICT_VALID_WITH_CORRECTION,
    VERDICT_AMBIGUOUS,
    VERDICT_RELABEL,
    VERDICT_REJECT,
)

#: `ok` était le verdict positif de la v2. Conservé en alias pour que les fichiers de
#: revue déjà annotés restent applicables ; toute autre orthographe est refusée.
VERDICT_ALIASES = {"ok": VERDICT_VALID}

#: Colonnes qu'un relecteur peut remplir en plus du verdict.
CORRECTION_FIELDS = (
    "corrected_question",
    "new_label",
    "new_negative_category",
    "new_expected_document_id",
)

LABELS = ("positive", "negative")


class ReviewError(ValueError):
    """Verdict incohérent avec le cas qu'il prétend corriger."""

#: Seuil de Jaccard au-delà duquel deux questions sont réputées quasi identiques.
#: 0,80 sur des sacs de tokens : deux formulations qui partagent 4 mots sur 5 ne
#: mesurent pas deux choses différentes.
NEAR_DUPLICATE_JACCARD = 0.80

#: Part visée du jeu de calibration. Le découpage étant par GROUPE, la part réelle
#: s'en écarte : un document porte un nombre variable de cas, et on ne coupe pas un
#: groupe pour atteindre un ratio.
CALIBRATION_TARGET_RATIO = 0.60

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")


def normalize_question(text: str) -> str:
    """Forme normalisée servant à comparer deux questions.

    Minuscules, accents dépliés, ponctuation retirée, espaces réduits. Assez
    agressive pour rapprocher « Quels sont les chiffres ? » et « Quels sont les
    chiffres… », assez conservatrice pour ne pas confondre deux vraies questions.
    """
    t = unicodedata.normalize("NFKD", (text or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = _PUNCT.sub(" ", t)
    return _SPACES.sub(" ", t).strip()


def _tokens(text: str) -> set[str]:
    return {w for w in normalize_question(text).split() if len(w) > 2}


def jaccard(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def deduplicate(cases: list[dict], *, threshold: float = NEAR_DUPLICATE_JACCARD) -> tuple[list[dict], list[dict]]:
    """Retire les doublons EXACTS puis les quasi-doublons. Rend ``(gardés, retirés)``.

    Fait AVANT le découpage : dédupliquer après laisserait un exemplaire de chaque
    côté, ce qui est la fuite qu'on cherche à éliminer.

    L'ordre d'entrée est préservé : le premier exemplaire d'un groupe de doublons est
    conservé, les suivants sont retirés avec la raison et l'identifiant du cas qui
    les a évincés — sans quoi une catégorie appauvrie serait indiscernable d'une
    catégorie que le modèle n'a pas su produire.
    """
    kept: list[dict] = []
    dropped: list[dict] = []
    seen_exact: dict[str, str] = {}
    for case in cases:
        q = case.get("question", "")
        norm = normalize_question(q)
        if norm in seen_exact:
            dropped.append({**case, "dropped_reason": "exact_duplicate",
                            "duplicate_of": seen_exact[norm]})
            continue
        near = next(
            (k for k in kept if jaccard(q, k.get("question", "")) >= threshold), None
        )
        if near is not None:
            dropped.append({**case, "dropped_reason": "near_duplicate",
                            "duplicate_of": near["case_id"],
                            "jaccard": round(jaccard(q, near["question"]), 3)})
            continue
        seen_exact[norm] = case["case_id"]
        kept.append(case)
    return kept, dropped


def group_key(case: dict) -> str:
    """Clé de GROUPE d'un cas — l'unité qui ne doit jamais être coupée.

    Positif  : le document dont la question est dérivée. Un document bien classé
               l'est pour toutes ses questions ; le séparer entre les deux jeux
               reviendrait à tester sur du déjà-vu.
    Négatif  : le cas lui-même. Un négatif n'appartient à aucun document, et la
               déduplication a déjà écarté ses quasi-jumeaux.
    """
    if case.get("label") == "positive":
        return f"doc:{case.get('expected_document_id') or case['case_id']}"
    return f"neg:{case['case_id']}"


def assign_splits(
    cases: list[dict], *, target_ratio: float = CALIBRATION_TARGET_RATIO
) -> dict[str, str]:
    """Répartit les GROUPES entre `calibration` et `holdout`. Rend ``{case_id: split}``.

    Déterministe : l'ordre vient de l'empreinte de la clé de groupe, jamais d'un
    tirage. Rejouer la génération ne déplace donc aucun cas — sans quoi le holdout
    finirait par contenir des cas ayant servi à choisir le seuil.

    Les groupes sont parcourus par empreinte croissante et versés dans `calibration`
    tant que la part visée n'est pas atteinte. On ne coupe jamais un groupe pour
    approcher le ratio : l'indépendance prime sur l'équilibre.
    """
    groups: dict[str, list[dict]] = {}
    for c in cases:
        groups.setdefault(group_key(c), []).append(c)

    ordered = sorted(
        groups.items(),
        key=lambda kv: hashlib.sha256(kv[0].encode("utf-8")).hexdigest(),
    )
    total = len(cases)
    quota = int(round(total * target_ratio))
    out: dict[str, str] = {}
    placed = 0
    for _key, members in ordered:
        split = "calibration" if placed < quota else "holdout"
        for c in members:
            out[c["case_id"]] = split
        placed += len(members)
    return out


def split_leakage_report(cases: list[dict]) -> dict:
    """Preuve d'indépendance : aucun groupe ne doit apparaître des deux côtés.

    Rendu avec le dataset, et vérifié par test. Une fuite ne se voit pas dans les
    métriques — elle les améliore.
    """
    by_group: dict[str, set[str]] = {}
    for c in cases:
        by_group.setdefault(group_key(c), set()).add(c.get("split", "?"))
    straddling = sorted(k for k, v in by_group.items() if len(v) > 1)
    docs = {c.get("expected_document_id") for c in cases if c.get("label") == "positive"}
    docs.discard(None)
    docs_by_split: dict[str, set[str]] = {}
    for c in cases:
        if c.get("label") == "positive" and c.get("expected_document_id"):
            docs_by_split.setdefault(c["split"], set()).add(c["expected_document_id"])
    shared = sorted(
        docs_by_split.get("calibration", set()) & docs_by_split.get("holdout", set())
    )
    return {
        "groups_total": len(by_group),
        "groups_straddling_splits": straddling,
        "documents_total": len(docs),
        "documents_in_both_splits": shared,
        "independent": not straddling and not shared,
    }


# --- Application d'un verdict humain -----------------------------------------

def normalize_verdict(raw: str) -> str:
    """Verdict canonique. Lève ``ReviewError`` sur une valeur inconnue.

    On refuse plutôt que d'ignorer : un verdict mal orthographié silencieusement
    sauté laisserait le cas non validé tout en faisant croire à une revue complète.
    """
    v = (raw or "").strip().lower()
    v = VERDICT_ALIASES.get(v, v)
    if v not in VERDICTS:
        raise ReviewError(
            f"verdict inconnu {raw!r} (attendu : {', '.join(VERDICTS)})"
        )
    return v


def apply_review_row(
    case: dict, row: dict, *, reviewer: str, at: str
) -> tuple[dict | None, list[dict]]:
    """Applique UN verdict à UN cas. Rend ``(cas modifié ou None, corrections)``.

    ``None`` signifie « retiré du dataset » — le seul verdict qui retire est
    ``reject``. Chaque champ modifié produit une entrée de correction portant le
    relecteur, la date, l'ancienne et la nouvelle valeur : sans cela une relecture
    ultérieure ne pourrait pas distinguer une donnée générée d'une donnée corrigée.

    Fonction PURE : elle ne lit ni fichier ni base, et ne mute pas ``case``.
    """
    verdict = normalize_verdict(row.get("verdict", ""))
    note = (row.get("reviewer_note") or "").strip()
    out = dict(case)
    corrections: list[dict] = []

    def _set(field: str, new_value):
        old = out.get(field)
        if old == new_value:
            return
        corrections.append({"field": field, "old": old, "new": new_value,
                            "by": reviewer, "at": at})
        out[field] = new_value

    if verdict == VERDICT_REJECT:
        return None, []

    if verdict == VERDICT_VALID_WITH_CORRECTION:
        corrected = (row.get("corrected_question") or "").strip()
        if not corrected:
            raise ReviewError(
                f"{case['case_id']} : `{VERDICT_VALID_WITH_CORRECTION}` exige "
                "`corrected_question` — un verdict « corrigé » sans correction n'est "
                "qu'un `valid` déguisé."
            )
        if corrected == (case.get("question") or "").strip():
            raise ReviewError(
                f"{case['case_id']} : `corrected_question` est identique à l'énoncé "
                f"d'origine — utiliser `{VERDICT_VALID}`."
            )
        _set("question", corrected)

    if verdict == VERDICT_RELABEL:
        new_label = (row.get("new_label") or "").strip().lower()
        if new_label not in LABELS:
            raise ReviewError(
                f"{case['case_id']} : `{VERDICT_RELABEL}` exige `new_label` parmi "
                f"{', '.join(LABELS)} (reçu {row.get('new_label')!r})."
            )
        if new_label == case.get("label"):
            # RECLASSEMENT de catégorie, à label constant. « Ce n'est pas une fausse
            # prémisse, c'est une question trop vague » est un jugement de revue
            # parfaitement légitime, et il n'existait aucun verdict pour l'exprimer :
            # `relabel` n'acceptait qu'un changement positive <-> negative, `valid`
            # aurait laissé le cas dans la mauvaise catégorie. On échangeait une
            # erreur visible contre une erreur silencieuse dans le bilan par catégorie.
            if new_label != "negative":
                raise ReviewError(
                    f"{case['case_id']} : un positif ne porte pas de catégorie, il n'y "
                    f"a donc rien à reclasser — utiliser `{VERDICT_VALID}`."
                )
            category = (row.get("new_negative_category") or "").strip()
            if category not in NEGATIVE_CATEGORIES:
                raise ReviewError(
                    f"{case['case_id']} : reclassement à label constant exige "
                    f"`new_negative_category` parmi {', '.join(NEGATIVE_CATEGORIES)}."
                )
            if category == case.get("negative_category"):
                raise ReviewError(
                    f"{case['case_id']} : ni le label ni la catégorie ne changent "
                    f"(déjà {category!r}) — utiliser `{VERDICT_VALID}`."
                )
            _set("negative_category", category)
            _set("justification", NEGATIVE_CATEGORIES[category])
        elif new_label == "negative":
            # Un positif dont le corpus ne porte pas la réponse : sa vérité terrain
            # devient fausse et doit disparaître, sans quoi une métrique la lirait
            # encore. La catégorie est EXIGÉE — un négatif sans catégorie échappe au
            # bilan par catégorie, qui est ce qui rend le dataset lisible.
            category = (row.get("new_negative_category") or "").strip()
            if category not in NEGATIVE_CATEGORIES:
                raise ReviewError(
                    f"{case['case_id']} : réétiquetage en `negative` exige "
                    f"`new_negative_category` parmi {', '.join(NEGATIVE_CATEGORIES)}."
                )
            _set("label", "negative")
            _set("negative_category", category)
            _set("expected_document_id", None)
            _set("expected_chunk_ids", [])
            _set("justification", NEGATIVE_CATEGORIES[category])
        else:
            # Un négatif auquel le corpus répond en réalité : il lui faut une vérité
            # terrain, sinon il deviendrait un positif qu'aucune mesure ne peut
            # trancher.
            doc = (row.get("new_expected_document_id") or "").strip()
            if not doc:
                raise ReviewError(
                    f"{case['case_id']} : réétiquetage en `positive` exige "
                    "`new_expected_document_id` — un positif sans document attendu "
                    "n'est pas mesurable."
                )
            _set("label", "positive")
            _set("expected_document_id", doc)
            _set("negative_category", None)

    # `ambiguous` reste dans le fichier mais sort des métriques : le retirer
    # embellirait le dataset en supprimant précisément ce qui est difficile.
    out["human_validated"] = verdict in (
        VERDICT_VALID, VERDICT_VALID_WITH_CORRECTION, VERDICT_RELABEL
    )
    out["excluded_from_metrics"] = verdict == VERDICT_AMBIGUOUS
    previous = list((case.get("validation") or {}).get("corrections") or [])
    out["validation"] = {
        "validated_by": reviewer,
        "validated_at": at,
        "verdict": verdict,
        "note": note,
        "corrections": previous + corrections,
    }
    return out, corrections
