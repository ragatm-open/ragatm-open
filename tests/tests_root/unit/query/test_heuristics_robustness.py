"""Robustesse et coût des heuristiques d'intention.

Le classifieur tourne sur CHAQUE requête de chat, avant tout retrieval. Deux
risques le guettent, et ils sont d'une autre nature que les erreurs de
classification :

  - le **backtracking catastrophique** — une alternance volumineuse combinée à des
    quantificateurs imbriqués peut rendre une seule question exponentielle ;
  - la **dérive de latence** — invisible en test unitaire, coûteuse en production.

Les seuils sont volontairement LARGES : ils détectent un effondrement, pas une
variation de machine. Le comportement observé est deux ordres de grandeur en
dessous.

## Pourquoi le budget absolu a été retiré (04/09/2026)

Ce module a longtemps gardé `MAX_SINGLE_CALL_MS = 250`, comparé au temps d'un
appel unique. Ce garde-fou a fait **rougir `main`** sur un runner partagé :
`258.9 ms` mesurées pour une entrée de 20 000 caractères, alors que la même
révision était verte sur la PR quelques minutes plus tôt.

La mesure a tranché — coût de `_classify` sur `"x" * N` :

| N | 2 500 | 5 000 | 10 000 | 20 000 | 40 000 | 80 000 |
|---|---|---|---|---|---|---|
| ms | 7,7 | 15,6 | 30,3 | 60,4 | 124,7 | 250,6 |
| ratio | — | ×2,04 | ×1,94 | ×1,99 | ×2,07 | ×2,01 |

**La croissance est strictement linéaire.** Il n'y avait aucun backtracking : le
pire cas local vaut 62 ms, et le seuil de 250 ms n'offrait qu'une marge de 4×,
qu'un runner mutualisé consomme entièrement.

Un budget en millisecondes ne distingue donc pas les deux choses que ce module
doit séparer : *l'algorithme a explosé* et *la machine était lente*. Il est
remplacé par deux invariants qui, eux, font cette distinction :

1. **la croissance reste linéaire** — ratio entre tailles doublées. Un runner
   uniformément lent ralentit les DEUX mesures : le ratio, lui, ne bouge pas.
   C'est la propriété réellement visée, et elle est sans échelle ;
2. **aucun motif ne porte de quantificateur imbriqué** — la signature
   structurelle du backtracking catastrophique, vérifiée sur le source, sans la
   moindre mesure de temps.

Un plafond d'horloge subsiste, mais comme **barrière extrême** : 5 s, soit ~75×
le pire cas observé. Seule une explosion réelle l'atteint ; aucun bruit de
runner ne s'en approche. La latence fine, elle, relève d'un banc de mesure —
pas d'un test bloquant.
"""
from __future__ import annotations

import ast
import re
import statistics
import time
from pathlib import Path

import pytest

from apps.rag_core.services.query.heuristics import (
    detect_comparative_intent,
    is_explanatory_question,
)

#: Garde-fou de latence. Cible technique du lot : < 1 ms. Seuil de test : 10 ms,
#: pour rester stable sur une machine chargée ou un runner CI partagé.
MAX_P95_MS = 10.0

#: Barrière EXTRÊME, pas un budget. ~75× le pire cas mesuré (62 ms pour 20 000
#: caractères). Elle existe pour qu'une explosion exponentielle échoue vite au
#: lieu de bloquer la CI — jamais pour arbitrer une latence.
ABSOLUTE_SAFETY_CEILING_S = 5.0

#: QUADRUPLER l'entrée doit au plus quadrupler le temps. On tolère 8×.
#:
#: L'amplitude n'est pas un détail. La première version doublait l'entrée et
#: tolérait 3× — le milieu entre le linéaire (2×) et le quadratique (4×). Une
#: marge de 1,5× de chaque côté n'a pas tenu : la CI a mesuré **×3,14 sur un cas
#: strictement linéaire** (5000:21,32 ms 10000:66,92 ms 20000:132,17 ms — le
#: second rapport valait 1,98, c'est la mesure à 5 000 qui était anormalement
#: RAPIDE). Un rapport reste sans échelle, mais deux points trop rapprochés
#: laissent le bruit dominer.
#:
#: Sur une amplitude ×4, la séparation est franche — mesuré le 04/09/2026 :
#: cas linéaires ×3,98 à ×4,08, cas quadratique ×16,44. Le seuil de 8 est à
#: 2× du linéaire ET à 2× du quadratique.
MAX_QUADRUPLING_RATIO = 8.0

#: Amplitude ×8 (5 000 → 40 000 caractères) : linéaire ≈ ×8, quadratique ≈ ×64.
#: Mesuré ×7,79. Le seuil de 20 est à 2,5× du linéaire et 3× du quadratique.
MAX_WIDE_SPAN_RATIO = 20.0
WIDE_SPAN_SIZES = (5_000, 40_000)

#: Chaque mesure est le MINIMUM de plusieurs exécutions. Le bruit d'un runner
#: n'ajoute jamais de temps négatif : le minimum est l'estimateur robuste du
#: coût réel, et une pause de l'ordonnanceur pendant une répétition ne fausse
#: plus le verdict.
TIMING_REPEATS = 5

#: Cas MESURÉS comme quadratiques — documentés ici plutôt qu'effacés du jeu.
#:
#: `detect_comparative_intent` croît en O(n²) sur les guillemets droits répétés.
#: Mesuré le 04/09/2026, quatre doublements consécutifs, sur `'"' * n` :
#: 0,79 → 3,50 → 14,55 → 59,37 → 240,85 ms, soit ×4,40 ×4,16 ×4,08 ×4,06 — là où
#: le témoin `'a' * n` reste à ×2,0. `is_explanatory_question` est linéaire sur
#: la même entrée : le coût vient bien du seul chemin comparatif.
#:
#: L'ancien budget absolu ne pouvait PAS le voir : à 500 guillemets le calcul
#: prend 3,8 ms, très loin sous 250 ms. C'est l'invariant de croissance qui l'a
#: fait apparaître.
#:
#: La borne reste à ×24 : elle tolère le quadratique CONNU (×16,44 mesuré sur
#: l'amplitude ×4) et refuse toujours l'exponentiel. Ce n'est pas une exemption,
#: c'est une dette bornée et surveillée — le jour où le chemin comparatif est
#: corrigé, ramener ce cas au régime commun. Le classifieur tourne sur CHAQUE
#: requête de chat : à 4 000 guillemets il coûte déjà 241 ms.
QUADRATIC_CASES: dict[str, float] = {"guillemets_droits_repetes": 24.0}


def _classify(text: str) -> str:
    if detect_comparative_intent(text).is_comparative:
        return "compare"
    return "explain" if is_explanatory_question(text) else "factual"


def _best_seconds(text: str, repeats: int = TIMING_REPEATS) -> float:
    """Coût de `_classify`, estimé par le MINIMUM de `repeats` exécutions.

    Une moyenne intègre les pauses de l'ordonnanceur ; le minimum les écarte.
    C'est ce qui rend les invariants de RAPPORT ci-dessous insensibles à un
    runner momentanément chargé.
    """
    _classify(text)  # chauffe : premier appel, caches de `re` froids
    best = float("inf")
    for _ in range(repeats):
        start = time.perf_counter()
        _classify(text)
        best = min(best, time.perf_counter() - start)
    return best


def _best_seconds_interleaved(a: str, b: str, repeats: int = TIMING_REPEATS) -> tuple[float, float]:
    """Deux coûts mesurés en ALTERNANCE (a, b, a, b, …), minimum de `repeats` passes chacun.

    Mesurer `a` puis `b` en séquence expose le rapport b/a à une rafale de charge qui
    ne frappe qu'une des deux séries (observé sous pytest-xdist à quatre workers :
    rapport ×9 sur une entrée linéaire, test vert en isolation). En alternant, les deux
    séries traversent les mêmes instants : une perturbation commune n'altère plus le
    rapport — c'est exactement ce que les invariants de RAPPORT revendiquent.
    """
    _classify(a)  # chauffe
    _classify(b)
    best_a = best_b = float("inf")
    for _ in range(repeats):
        start = time.perf_counter()
        _classify(a)
        best_a = min(best_a, time.perf_counter() - start)
        start = time.perf_counter()
        _classify(b)
        best_b = min(best_b, time.perf_counter() - start)
    return best_a, best_b


# --- entrées PATHOLOGIQUES : aucune ne doit exploser -------------------------
PATHOLOGICAL = [
    pytest.param("analyse " * 2000, id="repetition_verbe_analytique"),
    pytest.param("compare " * 2000, id="repetition_verbe_comparatif"),
    pytest.param("a et " * 1500 + "b", id="longue_coordination_et"),
    pytest.param("A vs " * 1000 + "B", id="repetition_operateur_versus"),
    pytest.param('"' * 500, id="guillemets_droits_repetes"),
    pytest.param("« " * 300 + " »" * 300, id="guillemets_francais_imbriques"),
    pytest.param("l'" * 2000, id="apostrophes_repetees"),
    pytest.param("x" * 20000, id="mot_unique_tres_long"),
    pytest.param("Quelle est la différence entre " + "a " * 500 + "et " + "b " * 500,
                 id="objets_comparatifs_tres_longs"),
    pytest.param(" ".join(["mot"] * 5000), id="phrase_sans_ponctuation"),
    pytest.param("Analyse les risques, " * 400, id="propositions_repetees"),
    pytest.param("différences entre " * 300 + "A et B", id="pseudo_comparaisons_empilees"),
    pytest.param("how many " * 1000 + "reports?", id="quantitatifs_empiles"),
    pytest.param("Synthèse des " * 800 + "écarts.", id="forme_nominale_tres_longue"),
]


@pytest.mark.parametrize("text", PATHOLOGICAL)
def test_aucune_entree_pathologique_n_explose(text):
    """Le traitement rend un verdict valide et termine — barrière extrême.

    Ce test ne juge PAS la latence : `ABSOLUTE_SAFETY_CEILING_S` vaut 5 s, soit
    deux ordres de grandeur au-dessus du pire cas. Il garantit qu'une explosion
    exponentielle échoue vite au lieu d'immobiliser la CI. La propriété
    algorithmique, elle, est portée par les deux tests suivants.
    """
    start = time.perf_counter()
    result = _classify(text)
    elapsed = time.perf_counter() - start
    assert result in ("factual", "explain", "compare")
    assert elapsed < ABSOLUTE_SAFETY_CEILING_S, (
        f"{elapsed:.2f} s pour une entrée de {len(text)} caractères — "
        "l'exécution ne termine plus en temps borné."
    )


@pytest.mark.parametrize("text", PATHOLOGICAL)
def test_quadrupler_l_entree_ne_fait_pas_exploser_le_cout(text, request):
    """Invariant de RAPPORT : quadrupler l'entrée au plus quadruple le temps.

    C'est la propriété que le budget absolu cherchait à protéger, énoncée sans
    échelle. Un backtracking catastrophique se paie en croissance super-linéaire —
    et la croissance, contrairement à une durée, ne dépend pas de la charge de la
    machine : les deux mesures subissent le même ralentissement.

    L'amplitude est ×4 et non ×2 : voir `MAX_QUADRUPLING_RATIO`. Doubler ne
    séparait pas assez le linéaire du quadratique pour tenir sur un runner
    mutualisé.

    Les cas de `QUADRATIC_CASES` portent une borne plus haute, nommée et justifiée :
    leur coût quadratique est mesuré et assumé, pas ignoré.
    """
    cas = request.node.callspec.id
    limite = QUADRATIC_CASES.get(cas, MAX_QUADRUPLING_RATIO)

    # Trois répétitions et non cinq : l'entrée quadruplée coûte quatre fois plus,
    # et l'amplitude ×4 tolère largement le bruit résiduel. Un test de robustesse
    # qui ralentit la suite finit par être désactivé.
    # Mesure ALTERNÉE : les deux séries subissent la même charge instantanée (voir
    # `_best_seconds_interleaved`) ; trois répétitions chacune, comme auparavant.
    t1, t4 = _best_seconds_interleaved(text, " ".join([text] * 4), repeats=3)
    # Plancher : sous 1 ms, la granularité de l'horloge domine et le rapport ne
    # veut plus rien dire. On ne conclut alors pas — on ne prétend pas mesurer.
    if t1 < 0.001:
        pytest.skip(f"coût trop faible pour un rapport fiable ({t1*1000:.3f} ms)")

    ratio = t4 / t1
    assert ratio < limite, (
        f"{t1*1000:.2f} ms → {t4*1000:.2f} ms (×{ratio:.2f}) en quadruplant l'entrée : "
        f"au-delà de ×{limite} la croissance n'est plus celle attendue pour « {cas} » — "
        "signature d'un backtracking catastrophique."
    )

    # Un cas déclaré quadratique qui redevient linéaire n'est pas une anomalie :
    # c'est une bonne nouvelle qu'il faut ENTENDRE, pour retirer l'exception
    # plutôt que la laisser couvrir un régime qui n'existe plus.
    if cas in QUADRATIC_CASES and ratio < MAX_QUADRUPLING_RATIO:
        pytest.fail(
            f"« {cas} » croît désormais en ×{ratio:.2f}, sous la limite commune de "
            f"×{MAX_QUADRUPLING_RATIO} : le coût quadratique documenté a disparu. "
            "Retirer l'entrée de QUADRATIC_CASES et sa justification."
        )


def test_le_cout_reste_lineaire_sur_une_large_amplitude():
    """L'entrée qui avait fait rougir `main`, mesurée sur une amplitude ×8.

    `"x" * 20000` est le cas exact de l'échec du 04/09/2026. Deux points écartés
    d'un facteur 8 séparent le linéaire (×8) du quadratique (×64) avec une marge
    que le bruit d'un runner ne franchit pas — là où deux points écartés d'un
    facteur 2 ont mesuré ×3,14 sur un coût pourtant strictement linéaire.
    """
    petit, grand = WIDE_SPAN_SIZES
    t_petit = _best_seconds("x" * petit)
    t_grand = _best_seconds("x" * grand)
    if t_petit < 0.001:
        pytest.skip(f"coût trop faible pour un rapport fiable ({t_petit*1000:.3f} ms)")

    ratio = t_grand / t_petit
    assert ratio < MAX_WIDE_SPAN_RATIO, (
        f"×{ratio:.2f} entre {petit} et {grand} caractères (limite ×{MAX_WIDE_SPAN_RATIO}, "
        f"linéaire attendu ×{grand // petit}) — "
        f"{petit}:{t_petit*1000:.2f}ms {grand}:{t_grand*1000:.2f}ms"
    )


def test_aucun_motif_ne_porte_de_quantificateur_imbrique():
    """Invariant STATIQUE : la signature du backtracking catastrophique.

    `(a+)+`, `(a*)*`, `(?:x+)*` — un quantificateur non borné appliqué à un
    groupe qui en contient déjà un — font exploser le nombre de chemins
    explorés. Les détecter sur le source ne coûte aucune milliseconde et ne
    dépend d'aucune machine.

    Portée assumée : détection par motif, pas analyse formelle. Elle attrape la
    forme classique, pas toutes les ambiguïtés possibles. C'est pour cela que
    les invariants de croissance ci-dessus restent nécessaires — les deux
    familles se complètent, aucune ne remplace l'autre.
    """
    source = (
        Path(__file__).resolve().parents[4]
        / "backend/apps/rag_core/services/query/heuristics.py"
    )
    assert source.is_file(), f"module introuvable : {source}"

    motifs: list[str] = []
    for noeud in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if not isinstance(noeud, ast.Call):
            continue
        cible = noeud.func
        if not (isinstance(cible, ast.Attribute) and cible.attr == "compile"):
            continue
        if noeud.args and isinstance(noeud.args[0], ast.Constant) and isinstance(
            noeud.args[0].value, str
        ):
            motifs.append(noeud.args[0].value)

    assert motifs, "aucun motif littéral trouvé : le test ne vérifierait rien"

    # Un groupe dont le corps porte `+`, `*` ou `{n,}` ET qui est lui-même suivi
    # d'un quantificateur non borné.
    imbrique = re.compile(r"\((?:\?[:=!])?[^()]*(?:[+*]|\{\d+,\})[^()]*\)\s*(?:[+*]|\{\d+,\})")
    fautes = [
        f"{m.group(0)!r} dans {p[:60]!r}"
        for p in motifs
        for m in [imbrique.search(p)]
        if m
    ]
    assert not fautes, "quantificateur imbriqué (backtracking catastrophique) :\n" + "\n".join(fautes)


# --- latence NOMINALE ---------------------------------------------------------
REALISTIC = [
    "Quelle est la date du rapport ?",
    "Analyse les principaux risques du dispositif de contrôle interne.",
    "Compare les deux dispositifs de reporting réglementaire.",
    "Explain the framework and its main limitations.",
    "Draft a detailed report on the second-line controls.",
    "What are the differences between framework A and framework B?",
    "Synthèse des écarts constatés lors de la dernière inspection.",
    "Ne compare pas les dispositifs ; donne seulement leur date de création.",
    "Le document indique : « analyse les risques opérationnels ».",
    "How many reports were produced during the last financial year?",
]


def test_latence_nominale_negligeable():
    for text in REALISTIC * 5:
        _classify(text)  # chauffe
    samples = []
    for _ in range(30):
        for text in REALISTIC:
            start = time.perf_counter()
            _classify(text)
            samples.append((time.perf_counter() - start) * 1000.0)
    samples.sort()
    p95 = samples[int(0.95 * (len(samples) - 1))]
    assert p95 < MAX_P95_MS, (
        f"p95 = {p95:.3f} ms (garde-fou {MAX_P95_MS} ms, "
        f"médiane {statistics.median(samples):.3f} ms). "
        "Ce seuil ne détecte qu'un effondrement."
    )


# --- déterminisme et pureté ----------------------------------------------------
@pytest.mark.parametrize("text", [t for t in REALISTIC])
def test_la_classification_est_pure_et_stable(text):
    verdicts = {_classify(text) for _ in range(10)}
    assert len(verdicts) == 1, f"{text!r} rend {verdicts}"


@pytest.mark.parametrize("text", ["", "   ", "\n\t", "?", "...", "«»", "42"])
def test_entrees_degenerees(text):
    assert _classify(text) in ("factual", "explain", "compare")


def test_entree_none_ne_casse_pas():
    assert is_explanatory_question(None) is False
    assert detect_comparative_intent(None).is_comparative is False
