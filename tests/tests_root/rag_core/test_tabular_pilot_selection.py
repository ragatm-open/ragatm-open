"""Passe B — sélection du lot pilote : quotas, plafonds, déterminisme.

Ce qu'on teste ici n'est pas « la sélection renvoie 80 cas » — n'importe quel parcours
y arrive. On teste les propriétés qui distinguent une sélection **sous contraintes**
d'un simple prélèvement, et qui sont toutes silencieuses en cas de régression :

- un lot conforme au corpus (dominé par `filtered_count` et `easy`) est un lot faux ;
- un plafond de concentration relâché pour atteindre un quota ne se voit qu'en
  recoupant document par document ;
- une clé de valeur attendue trop grossière (`row_identifier` générique) ou trop fine
  (`integer:7` vs `decimal:7`) produit des chiffres d'apparence normale.

Tests PURS : facettes en mémoire, aucun classeur, aucune DB, aucun retrieval, aucun
moteur.
"""
from __future__ import annotations

import datetime as dt

import pytest

from apps.rag_core.benchmark import tabular_aggregation_schema as s
from apps.rag_core.benchmark import tabular_case_generator as gen
from apps.rag_core.benchmark import tabular_pilot_selection as sel
from apps.rag_core.benchmark import tabular_reference_executor as ex

OPERATIONS = sorted(s.SUPPORTED_OPERATIONS)
FILTERS = sorted(sel.FILTER_QUOTAS)


def _facets(i: int, *, operation="sum", filter_operator=None, filter_value_type=None,
            difficulty=gen.DIFFICULTY_EASY, table_rows=30, value=None,
            doc=None, sheet=None, table=None, zero=False, count_key=None):
    """Un candidat synthétique, par défaut sur son propre document/feuille/table.

    Le défaut « un candidat, un document » neutralise les plafonds de concentration :
    les tests qui les visent les rétablissent explicitement.
    """
    return sel.CandidateFacets(
        case_id=f"case-{i:05d}",
        document_sha256=doc or f"doc-{i:05d}",
        filename=f"{doc or f'doc-{i:05d}'}.xlsx",
        sheet_name=sheet or f"F{i}",
        table_range=table or f"A1:D{table_rows + 1}",
        operation=operation,
        filter_operator=filter_operator,
        filter_value_type=filter_value_type,
        difficulty=difficulty,
        table_rows=table_rows,
        expected_value_key=value or f"integer:{i}",
        is_zero_result=zero,
        small_count_key=count_key,
    )


def _rich_pool(n_per_combo: int = 30):
    """Pool abondant et équilibré : toute contrainte y est satisfaisable.

    Sert de témoin — si un quota n'est pas atteint sur ce pool, c'est le sélecteur qui
    est en cause, pas le corpus.
    """
    pool, i = [], 0
    combos = []
    for op in OPERATIONS:
        needs_filter = op in (s.OP_FILTERED_COUNT, s.OP_FILTERED_SUM)
        for fop in (FILTERS if needs_filter else [None]):
            for diff in (gen.DIFFICULTY_EASY, gen.DIFFICULTY_MEDIUM, gen.DIFFICULTY_HARD):
                for rows in (7, 12, 20, 60):
                    combos.append((op, fop, diff, rows))
    for op, fop, diff, rows in combos:
        for _ in range(n_per_combo):
            i += 1
            vtype = None
            if fop in (s.F_BEFORE, s.F_AFTER):
                vtype = "date"
            elif fop == s.F_EQ:
                vtype = "category"
            elif fop:
                vtype = "numeric"
            pool.append(_facets(i, operation=op, filter_operator=fop,
                                filter_value_type=vtype, difficulty=diff,
                                table_rows=rows))
    return pool


# --------------------------------------------------------------------------- #
# Tranches de taille
# --------------------------------------------------------------------------- #
def test_les_tranches_distinguent_10_14_de_15_24():
    """La passe A mesurait `10-24` d'un bloc ; le quota `≥ 15` exige la coupe.

    Sans elle, un lot entièrement composé de tables de 10 à 14 lignes satisferait
    formellement un quota conçu pour garantir des tables réellement volumineuses.
    """
    assert sel.size_bucket(12) == "10-14"
    assert sel.size_bucket(15) == "15-24"
    assert "10-14" not in sel.SIZE_AT_LEAST_15
    assert "15-24" in sel.SIZE_AT_LEAST_15


def test_le_seuil_global_de_5_lignes_est_une_condition_pas_un_quota():
    """Une table sous 5 lignes n'est jamais éligible, quel que soit le déficit."""
    state = sel.SelectionState()
    assert state.blocking_cap(_facets(1, table_rows=4)) == "min_table_rows"
    assert state.blocking_cap(_facets(2, table_rows=5)) is None


# --------------------------------------------------------------------------- #
# Plafonds de concentration — jamais relâchés
# --------------------------------------------------------------------------- #
def test_les_plafonds_de_concentration_tiennent_sur_un_pool_concentre():
    """Un pool tout entier logé dans trois documents ne doit pas produire 80 cas.

    C'est le test qui distingue « sélection sous contraintes » de « prélèvement » :
    face à un quota inatteignable, le sélecteur doit s'arrêter, pas puiser deux fois
    dans la même feuille.
    """
    pool = []
    for i in range(600):
        pool.append(_facets(i, doc=f"doc-{i % 3}", sheet=f"S{i % 7}",
                            table=f"A1:D{i % 11}", operation=OPERATIONS[i % 9],
                            difficulty=gen.DIFFICULTY_MEDIUM))
    outcome = sel.select_pilot(pool)
    state = outcome.state

    assert max(state.per_document.values()) <= 2
    assert max(state.per_sheet.values()) <= 1
    assert max(state.per_table.values()) <= 1
    # 3 documents × 2 cas : le lot plafonne à 6, très loin de la cible — et c'est la
    # réponse correcte.
    assert len(state.selected) <= 6
    assert outcome.unmet, "les quotas manqués doivent être rapportés, pas masqués"


def test_un_quota_secondaire_ne_relache_jamais_un_plafond():
    """`hard` est en déficit, mais tous les `hard` restants sont sur un document saturé."""
    pool = [_facets(i, difficulty=gen.DIFFICULTY_EASY, operation=OPERATIONS[i % 9])
            for i in range(200)]
    # Tous les `hard` du pool tiennent dans un seul document.
    pool += [_facets(1000 + i, difficulty=gen.DIFFICULTY_HARD, doc="doc-hard",
                     sheet=f"H{i}", table=f"Z1:Z{i}") for i in range(40)]

    outcome = sel.select_pilot(pool)
    assert outcome.state.per_document["doc-hard"] <= 2
    assert outcome.state.by_difficulty[gen.DIFFICULTY_HARD] <= 2
    assert "difficulty:hard" in outcome.unmet


# --------------------------------------------------------------------------- #
# Valeurs attendues
# --------------------------------------------------------------------------- #
def test_le_plafond_de_valeur_attendue_porte_sur_la_valeur_complete():
    pool = [_facets(i, value="integer:42", operation=OPERATIONS[i % 9])
            for i in range(100)]
    outcome = sel.select_pilot(pool)
    assert len(outcome.selected) <= sel.CONCENTRATION_CAPS["per_expected_value"]


def test_row_identifier_est_plafonne_sur_l_identifiant_pas_sur_le_type():
    """Deux `argmax` désignant deux lignes différentes sont deux réponses différentes.

    L'inventaire réduit tout `row_identifier` au marqueur générique — utile pour
    mesurer, ruineux pour sélectionner : le lot entier serait limité à 2 argmin/argmax.
    """
    pool = [_facets(i, operation=s.OP_ARGMAX,
                    value=f'row_identifier:{{"code":"ABC-{i}"}}') for i in range(40)]
    outcome = sel.select_pilot(pool)
    n = outcome.state.by_operation[s.OP_ARGMAX]
    assert n >= sel.OPERATION_QUOTAS[s.OP_ARGMAX][0]

    # …mais le même identifiant reste plafonné à 2.
    identical = [_facets(i, operation=s.OP_ARGMAX,
                         value='row_identifier:{"code":"ABC"}') for i in range(40)]
    assert len(sel.select_pilot(identical).selected) <= 2


def test_le_plafond_de_petit_comptage_survit_au_changement_de_type():
    """`integer:7` et `decimal:7` ne doivent pas se partager deux quotas distincts."""
    pool = [_facets(i, operation=s.OP_COUNT_ROWS,
                    value=f"{'integer' if i % 2 else 'decimal'}:7",
                    count_key="count:7") for i in range(40)]
    outcome = sel.select_pilot(pool)
    assert len(outcome.selected) <= sel.MAX_SAME_SMALL_COUNT


def test_les_resultats_nuls_sont_plafonnes():
    pool = [_facets(i, zero=True, value=f"decimal:0#{i}", operation=OPERATIONS[i % 9])
            for i in range(60)]
    outcome = sel.select_pilot(pool)
    assert outcome.state.zero_results <= sel.MAX_ZERO_RESULTS


# --------------------------------------------------------------------------- #
# Quotas de couverture
# --------------------------------------------------------------------------- #
def test_un_pool_conforme_au_corpus_ne_produit_pas_un_lot_conforme_au_corpus():
    """Le cœur de la passe B.

    Le pool imite la distribution réelle : 70 % de `filtered_count`, 90 % de `easy`,
    `before`/`after` à moins de 0,5 %. Un parcours qui accepte les premiers cas
    disponibles reproduirait ces proportions ; la sélection doit au contraire couvrir
    les neuf opérations, les six filtres et le quota `hard`.
    """
    pool = _rich_pool(n_per_combo=2)
    # Écrasante majorité de filtered_count/easy/petites tables, comme le corpus.
    pool += [_facets(50_000 + i, operation=s.OP_FILTERED_COUNT,
                     filter_operator=s.F_GT, filter_value_type="numeric",
                     difficulty=gen.DIFFICULTY_EASY, table_rows=8)
             for i in range(5_000)]

    report = sel.quota_report(sel.select_pilot(pool))

    assert all(line["observed"] > 0 for line in report["operations"].values())
    assert all(line["observed"] > 0 for line in report["filters"].values())
    assert report["difficulties"]["hard"]["observed"] >= 10
    assert report["filters"]["before"]["observed"] >= 4
    assert report["filters"]["after"]["observed"] >= 4
    assert report["table_sizes"]["at_least_15"]["observed"] >= sel.MIN_TABLES_AT_LEAST_15
    assert report["table_sizes"]["small_5_9"]["observed"] <= sel.MAX_SMALL_TABLES


def test_les_minima_de_filtre_excedent_structurellement_le_plafond_d_operation():
    """Contrainte structurelle, à documenter plutôt qu'à contourner.

    Seules `filtered_count` et `filtered_sum` portent un filtre ; plafonnées à 10
    chacune, elles bornent le lot à 20 cas filtrés alors que les minima de filtre en
    totalisent 40. Aucun corpus ne lève cette contrainte : c'est l'arithmétique des
    quotas eux-mêmes. Si un jour une troisième opération porte des filtres, ce test
    échouera — et c'est le bon moment pour rouvrir les quotas.
    """
    ceiling = (sel.OPERATION_QUOTAS[s.OP_FILTERED_COUNT][2]
               + sel.OPERATION_QUOTAS[s.OP_FILTERED_SUM][2])
    assert sum(low for low, _, _ in sel.FILTER_QUOTAS.values()) > ceiling
    # Le plancher de recevabilité, lui, tient sous le plafond.
    assert sum(sel.FILTER_FLOORS.values()) <= ceiling


def test_le_lot_est_recevable_sur_un_pool_abondant():
    report = sel.quota_report(sel.select_pilot(_rich_pool()))
    assert report["admissible"], report["unmet"]
    # Les minima de filtre cèdent (priorité opération > filtre), le plancher tient.
    for f, floor in sel.FILTER_FLOORS.items():
        assert report["filters"][f]["observed"] >= floor, f
    low, high = sel.ACCEPTABLE_RANGE
    assert low <= report["n_cases"] <= high
    assert report["concentration"]["max_per_document"] <= 2
    assert report["concentration"]["max_per_sheet"] <= 1
    assert report["concentration"]["max_per_table"] <= 1


def test_les_maxima_par_operation_et_par_filtre_sont_respectes():
    report = sel.quota_report(sel.select_pilot(_rich_pool()))
    for op, line in report["operations"].items():
        assert line["observed"] <= line["max"], op
    for f, line in report["filters"].items():
        assert line["observed"] <= line["max"], f
    for d, line in report["difficulties"].items():
        assert line["observed"] <= line["max"], d


def test_un_between_sur_date_ne_compte_ni_dans_before_ni_dans_after():
    """Il compte dans `between` et dans le type `date` — c'est tout."""
    pool = [_facets(i, operation=s.OP_FILTERED_COUNT, filter_operator=s.F_BETWEEN,
                    filter_value_type="date") for i in range(30)]
    state = sel.select_pilot(pool).state
    assert state.by_filter[s.F_BETWEEN] > 0
    assert state.by_filter[s.F_BEFORE] == 0
    assert state.by_filter[s.F_AFTER] == 0
    assert state.by_filter_value_type["date"] == state.by_filter[s.F_BETWEEN]


def test_les_quotas_manques_sont_rapportes_et_jamais_compenses():
    """Un pool sans dates : `before`/`after` sont inatteignables.

    Le lot doit le dire, et ne pas fabriquer du volume en dupliquant des variantes
    faibles ailleurs.
    """
    pool = [f for f in _rich_pool()
            if f.filter_operator not in (s.F_BEFORE, s.F_AFTER)]
    outcome = sel.select_pilot(pool)
    report = sel.quota_report(outcome)

    assert report["filters"]["before"]["observed"] == 0
    assert report["filters"]["after"]["observed"] == 0
    assert "filter:before" in outcome.unmet and "filter:after" in outcome.unmet
    assert not report["admissible"]
    # Les plafonds tiennent malgré le déficit.
    assert report["concentration"]["max_per_sheet"] <= 1


# --------------------------------------------------------------------------- #
# Déterminisme
# --------------------------------------------------------------------------- #
def test_la_selection_ne_depend_pas_de_l_ordre_du_pool():
    """Même pool, ordre inverse : même lot, dans le même ordre.

    Le pool réel arrive dans l'ordre du balayage, qui dépend de PostgreSQL et du
    système de fichiers. Une sélection sensible à cet ordre ne serait pas reproductible
    d'une machine à l'autre.
    """
    pool = _rich_pool(n_per_combo=6)
    a = [c.case_id for c in sel.select_pilot(pool).selected]
    b = [c.case_id for c in sel.select_pilot(list(reversed(pool))).selected]
    assert a == b
    assert len(set(a)) == len(a), "aucun cas ne doit être retenu deux fois"


def _case(case_id: str, question: str):
    return {"case_id": case_id, "question": question}


def test_les_enonces_indecidables_sont_detectes():
    """Deux classeurs jumeaux, mêmes feuilles, énoncés identiques, réponses différentes.

    Le générateur nomme la feuille et la table, jamais le document ; le corpus contient
    des versions successives d'un même classeur (`… 122025` / `… 092024`). Un cas dont
    l'énoncé revient à l'identique est structurellement insoluble : le compter comme un
    échec mesurerait l'ambiguïté du corpus, pas la capacité de calcul.
    """
    cases = [
        _case("a", "Dans la feuille « 3. Données », quelle est la valeur maximale ?"),
        _case("b", "Dans la feuille « 3. Données », quelle est la valeur maximale ?"),
        _case("c", "Dans la feuille « Autre », combien de lignes ?"),
    ]
    assert sel.ambiguous_question_case_ids(cases) == {"a", "b"}


def test_la_detection_d_ambiguite_normalise_espaces_et_casse():
    cases = [_case("a", "Combien de  lignes ?"), _case("b", "combien de lignes ?")]
    assert sel.ambiguous_question_case_ids(cases) == {"a", "b"}


def test_un_lot_sans_doublon_d_enonce_ne_signale_rien():
    cases = [_case("a", "Question un ?"), _case("b", "Question deux ?")]
    assert sel.ambiguous_question_case_ids(cases) == set()


def test_la_reduction_du_pool_ne_change_pas_la_selection():
    """La déduplication par feuille est **exacte**, pas heuristique.

    Elle existe pour la performance — 4·10⁵ facettes balayées à chaque prise —, mais une
    réduction qui changerait le lot serait un biais silencieux, indétectable dans le
    rapport de quotas. Le pool brut contient ici de nombreux doublons de signature au
    sein d'une même feuille : le lot doit être identique dans les deux cas.
    """
    pool = []
    for i, (op, diff) in enumerate(
            [(op, d) for op in OPERATIONS
             for d in (gen.DIFFICULTY_EASY, gen.DIFFICULTY_MEDIUM, gen.DIFFICULTY_HARD)]):
        for sheet in range(30):
            for clone in range(4):        # 4 candidats interchangeables par feuille
                pool.append(_facets(
                    i * 1000 + sheet * 10 + clone, operation=op, difficulty=diff,
                    doc=f"doc-{sheet}", sheet=f"S{sheet}-{i}",
                    table=f"T{clone}", table_rows=20,
                    value=f"integer:{i}-{sheet}"))

    pruned = sel.prune_pool(pool)
    assert len(pruned) < len(pool)
    assert ([c.case_id for c in sel.select_pilot(pool).selected]
            == [c.case_id for c in sel.select_pilot(pruned).selected])


def test_la_reduction_conserve_le_plus_petit_case_id():
    """C'est exactement le représentant que le départage aurait choisi."""
    clones = [_facets(i, doc="d", sheet="S", table=f"T{i}", value="integer:1")
              for i in range(5)]
    pruned = sel.prune_pool(clones)
    assert len(pruned) == 1
    assert pruned[0].case_id == min(c.case_id for c in clones)


def test_la_reduction_ne_fusionne_pas_deux_feuilles():
    """Deux feuilles distinctes offrent deux prises possibles : rien à fusionner."""
    a = _facets(1, doc="d", sheet="S1", value="integer:1")
    b = _facets(2, doc="d", sheet="S2", value="integer:1")
    assert len(sel.prune_pool([a, b])) == 2


def test_deux_executions_donnent_le_meme_lot():
    pool = _rich_pool(n_per_combo=6)
    assert ([c.case_id for c in sel.select_pilot(pool).selected]
            == [c.case_id for c in sel.select_pilot(pool).selected])


# --------------------------------------------------------------------------- #
# Facettes issues d'un cas réel
# --------------------------------------------------------------------------- #
HEADERS = ["Dossier", "Statut", "Montant", "Échéance"]
DATA = [
    ["D-1", "En cours", 100, dt.date(2025, 1, 15)],
    ["D-2", "Clos",     250, dt.date(2025, 3, 20)],
    ["D-3", "En cours",  50, dt.date(2024, 11, 5)],
    ["D-4", "En cours", 300, dt.date(2025, 6, 30)],
    ["D-5", "Clos",     175, dt.date(2024, 2, 1)],
    ["D-6", "En cours", 425, dt.date(2025, 9, 9)],
]


def _source(sha="a" * 64, sheet="Demandes", rows=6):
    return gen.TableSource(
        content_sha256=sha, filename="d.xlsx", path="/tmp/d.xlsx",
        locator=s.TableLocator(sheet_name=sheet, table_range=f"A1:D{rows + 1}",
                               header_rows=(1,), data_row_start=2,
                               data_row_end=rows + 1))


def test_les_facettes_sont_collectees_sans_plafond_du_generateur():
    """La collecte doit voir tout ce que la table offre.

    Les plafonds du générateur (3 cas par table) borneraient l'offre *avant* que la
    sélection ait choisi : on sélectionnerait dans un échantillon déjà biaisé.
    """
    facets = sel.collect_table_facets(_source(), ex.build_table(HEADERS, DATA),
                                      rag_space="_test")
    assert len(facets) > gen.DEFAULT_CAPS["per_table"]
    assert {f.operation for f in facets} >= {s.OP_SUM, s.OP_AVERAGE, s.OP_COUNT_ROWS}
    assert all(f.table_rows == len(DATA) for f in facets)
    assert [f.case_id for f in facets] == sorted(f.case_id for f in facets)


def test_une_table_sous_le_seuil_ne_produit_aucune_facette():
    facets = sel.collect_table_facets(_source(rows=3),
                                      ex.build_table(HEADERS, DATA[:3]),
                                      rag_space="_test")
    assert facets == []


def test_les_facettes_d_une_meme_table_sont_dedupliquees_par_signature():
    """Le plafond `per_table` vaut 1 : deux cas de même signature sont interchangeables."""
    facets = sel.collect_table_facets(_source(), ex.build_table(HEADERS, DATA),
                                      rag_space="_test")
    signatures = [f.signature for f in facets]
    assert len(signatures) == len(set(signatures))


def test_le_type_de_valeur_filtree_distingue_date_et_categorie():
    facets = sel.collect_table_facets(_source(), ex.build_table(HEADERS, DATA),
                                      rag_space="_test")
    by_operator = {}
    for f in facets:
        if f.filter_operator:
            by_operator.setdefault(f.filter_operator, set()).add(f.filter_value_type)
    assert by_operator.get(s.F_EQ) == {"category"}
    for date_operator in (s.F_BEFORE, s.F_AFTER):
        if date_operator in by_operator:
            assert by_operator[date_operator] == {"date"}


@pytest.mark.parametrize("operation", sorted(s.SUPPORTED_OPERATIONS))
def test_chaque_operation_supportee_a_un_quota(operation):
    """Une opération ajoutée au MVP sans quota passerait inaperçue dans le lot."""
    assert operation in sel.OPERATION_QUOTAS
    low, target, high = sel.OPERATION_QUOTAS[operation]
    assert 0 < low <= target <= high
