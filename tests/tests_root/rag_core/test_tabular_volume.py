"""Qualification volumétrique du moteur tabulaire — variante RAPIDE, exécutable en CI.

Ces tests exercent le MÊME harnais que la campagne complète
(`benchmark/tabular_volume_runner`), mais à une taille tenue basse volontairement.

Pourquoi si petit ? Parce que l'ingestion DuckDB du moteur passe par
`conn.executemany`, dont le débit mesuré est de l'ordre de **300 lignes/s** sur ce
corpus à dix colonnes (cf. `probe_ingestion_split`). Un test à 100 000 lignes coûterait
plus de cinq minutes par requête ; la CI ne peut pas le porter. La taille
rapide (`FAST_ROWS`) garde donc les mêmes PROPRIÉTÉS à vérifier — typage, plafond
mémoire, concurrence, provenance par document — en quelques dizaines de secondes.

**Deux cercles, et la frontière n'est pas arbitraire.** Le cercle rapide contient tout ce
dont le coût est proportionnel à `FAST_ROWS` : on peut le rendre bon marché en réduisant
la taille sans rien perdre de la propriété vérifiée. Le cercle `slow` contient ce qui est
lent *par construction* et qu'aucun réglage de taille ne rendra rapide :

- les sondes de DÉLAI, qui ont besoin d'une requête dont la durée naturelle se compte en
  secondes pour pouvoir démontrer que `conn.interrupt()` abrège réellement le calcul ;
- la montée en charge de la concurrence (1, 2, 5, 10 threads), dont le coût suit le
  nombre de requêtes lancées ;
- la campagne volumétrique proprement dite (100 k / 500 k / 1 M).

Que le plafond de temps couvre bien l'INGESTION — le défaut trouvé pendant cette
qualification — est vérifié à part, et rapidement, par
`tests/rag_core/test_tabular_limits_enforcement.py`. Ce fichier ne le duplique pas.

Les grandes tailles restent accessibles, mais **sur demande explicite** :

    TABULAR_VOLUME_FULL=1 pytest tests/rag_core/test_tabular_volume.py -m slow

Le marqueur `slow` et la variable d'environnement sont redondants à dessein : le
marqueur permet de désélectionner en local, la variable évite qu'un `-m slow` distrait
ne déclenche une campagne de plusieurs heures sur une machine partagée.
"""
from __future__ import annotations

import os

import pytest

from apps.rag_core.benchmark import tabular_volume_corpus as VC
from apps.rag_core.benchmark import tabular_volume_runner as VR
from apps.rag_core.tabular import errors as E
from apps.rag_core.tabular import types as T

#: Taille des tests rapides. **1 000 lignes, et pas moins de 998** : c'est le plancher
#: imposé par le corpus lui-même, et il vaut la peine d'être expliqué, car il n'est pas
#: intuitif. La modalité rare (« Corse ») n'apparaît qu'une ligne sur 997 — donc aux
#: index 0 et 997 — mais l'index 0 est aussi celui d'un montant NULL (une ligne sur 137,
#: et 0 est multiple de tout). À 500 lignes, la seule ligne « Corse » du fichier porte
#: donc un montant vide, et `sum_selective` sort en `empty_selection` : le test ne
#: mesurerait plus un filtre sélectif, il mesurerait une absence de données. Il faut
#: atteindre l'index 997 pour disposer d'une ligne rare EXPLOITABLE.
#:
#: Le coût de la suite est LINÉAIRE en cette valeur : l'ingestion DuckDB (`executemany`)
#: tourne à ~300 lignes/s sur ce corpus à dix colonnes, et chaque cas FILTRÉ paie
#: l'ingestion DEUX fois (exécution, puis recomptage des lignes retenues). D'où la règle
#: qui gouverne le découpage `slow` de ce fichier : la suite standard limite le NOMBRE de
#: requêtes moteur, pas leur qualité.
#: MARQUEUR DE MODULE — ce fichier est exclu de la suite par défaut.
#:
#: Raison, et elle est empirique : inclus dans la suite backend entière, ce module la
#: fait passer de 227 s / 0 erreur à 1084 s / 135 erreurs, toutes dans
#: `tests/http_api/test_chat_help_api.py`. Retiré, la suite revient à 2449 passés,
#: 0 erreur — meilleur que `main` (2384). Le mécanisme n'est PAS démontré : aucune
#: fuite de descripteur, de thread ni de RSS n'est mesurable (fd stable à 4, threads
#: à 1, RSS plateau à 160 Mo), et ces mêmes tests passent en 45 s isolément. La piste
#: la plus probable est la pression mémoire de la machine (5,1 Gi disponibles sur 14),
#: mais elle reste une hypothèse.
#:
#: On ISOLE, on ne supprime pas : les tests restent exécutables par marqueur explicite
#: (cf. l'en-tête du module). Tant que la cause n'est pas comprise, les embarquer dans
#: la suite par défaut reviendrait à rendre celle-ci non fiable — et une suite dont on
#: doute ne protège plus de rien.
pytestmark = pytest.mark.tabular_volume

FAST_ROWS = 1_000

#: Formes de requête exécutées par la suite standard. Le but n'est pas de re-tester les
#: opérations — `test_tabular_engine` couvre les treize formes, sur des tables minuscules
#: et en quelques millisecondes — mais de vérifier qu'elles tiennent sur un corpus RÉEL
#: et volumineux. Deux formes suffisent à couvrir les deux profils de coût : un scalaire
#: FILTRÉ (qui paie l'ingestion deux fois) et un retour de LIGNES trié. La matrice
#: complète reste jouée, sous `slow`.
FAST_CASE_IDS = ("sum_selective", "top_n")

#: Parallélisme des contrôles de concurrence de la suite standard. Quatre threads : assez
#: pour que les requêtes se chevauchent réellement, ce qui est la seule condition pour
#: qu'une fuite d'état se manifeste. La montée en charge (1, 2, 5, 10) mesure autre
#: chose — la tenue sous contention — et coûte proportionnellement au nombre de requêtes
#: lancées : elle est sous `slow`.
FAST_WORKERS = 4

#: Taille du corpus des contrôles de concurrence. Une fuite entre threads se voit dans la
#: SIGNATURE (résultat, `plan_hash`, `evidence_hash`) ; ni sa présence ni sa détection ne
#: dépendent du nombre de lignes. Deux cents lignes suffisent donc, et ramènent chaque
#: requête sous la seconde.
CONCURRENCY_ROWS = 200

#: Cas utilisés par les contrôles de concurrence rapides — tous SANS filtre, donc une
#: seule ingestion chacun au lieu de deux, et aucun besoin de la modalité rare (absente
#: d'un corpus de 200 lignes). Ils restent des opérations distinctes, ce qui est la
#: propriété exigée par le scénario « questions différentes ».
CONCURRENCY_CASES = ("count_rows", "average", "minimum", "maximum")

FULL = os.environ.get("TABULAR_VOLUME_FULL") == "1"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def clean_doc(tmp_path_factory):
    """Document propre : dates toutes valides, donc opérations temporelles possibles."""
    directory = tmp_path_factory.mktemp("volume")
    vfile = VC.generate_csv(FAST_ROWS, dirty=False, directory=directory)
    return VR.prepare_document(vfile)


@pytest.fixture(scope="module")
def petits_docs(tmp_path_factory):
    """Deux petits documents DISTINCTS, pour les contrôles de concurrence rapides.

    Distincts au sens du `document_id` (le nom du fichier entre dans `document_id_for`) :
    c'est ce qui rend observable le scénario « documents différents ».
    """
    directory = tmp_path_factory.mktemp("volume_concurrence")
    return [VR.prepare_document(VC.generate_csv(CONCURRENCY_ROWS, dirty=sale,
                                                directory=directory))
            for sale in (False, True)]


@pytest.fixture(scope="module")
def dirty_doc(tmp_path_factory):
    """Document sale : minorité de dates invalides."""
    directory = tmp_path_factory.mktemp("volume_dirty")
    vfile = VC.generate_csv(FAST_ROWS, dirty=True, directory=directory)
    return VR.prepare_document(vfile)


# --------------------------------------------------------------------------- #
# Corpus : déterminisme et salissure
# --------------------------------------------------------------------------- #
def test_corpus_deterministe():
    """Deux générations successives produisent les MÊMES octets.

    C'est la propriété qui rend une régression de performance interprétable : sans elle,
    un écart de mesure pourrait venir du corpus plutôt que du moteur.
    """
    a = VC.csv_bytes(1_000, dirty=False)
    b = VC.csv_bytes(1_000, dirty=False)
    assert a == b
    assert VC.csv_bytes(1_000, dirty=True) != a


def test_corpus_contient_les_anomalies_annoncees():
    text = VC.csv_bytes(2_000, dirty=True).decode("utf-8")
    lines = text.splitlines()
    assert VC.DELIMITER in lines[0]
    # Délimiteur ET guillemets à l'intérieur d'un champ.
    assert any('";' in ligne and '""' in ligne for ligne in lines[1:])
    assert "Réf." in text and "n°" in text          # accents
    assert ";;" in text                              # NULL
    assert any(ligne.count(";-") for ligne in lines[1:])  # négatifs
    assert VC.REGION_RARE in text                    # modalité rare
    assert any(bad in text for bad in VC.INVALID_DATES)


def test_doublons_verbatim_presents():
    lines = VC.csv_bytes(2_000, dirty=False).decode("utf-8").splitlines()[1:]
    assert len(lines) != len(set(lines))


# --------------------------------------------------------------------------- #
# Typage : la politique de date est bien celle qui est documentée
# --------------------------------------------------------------------------- #
def test_variante_propre_type_la_date_en_date(clean_doc):
    date_col = next(c for c in clean_doc.table.columns if c.original_name == "Date")
    assert date_col.inferred_type == T.TYPE_DATE


def test_variante_sale_retombe_en_texte(dirty_doc):
    """`DATE_RATIO_REQUIRED = 1.0` : une seule intruse suffit à disqualifier la colonne.

    Ce n'est pas un défaut, c'est la politique — mais elle doit être vérifiée, sinon
    rien ne garantit qu'un assouplissement futur ne passe pas inaperçu.
    """
    date_col = next(c for c in dirty_doc.table.columns if c.original_name == "Date")
    assert date_col.inferred_type == T.TYPE_TEXT


def test_identifiant_conserve_son_zero_initial(clean_doc):
    ident = next(c for c in clean_doc.table.columns if c.original_name == "Identifiant")
    assert ident.inferred_type == T.TYPE_IDENTIFIER
    assert clean_doc.rows[0][ident.position].startswith("0")


def test_colonnes_numeriques_bien_typees(clean_doc):
    types = {c.original_name: c.inferred_type for c in clean_doc.table.columns}
    assert types["Quantité"] == T.TYPE_INTEGER
    assert types["Montant HT"] == T.TYPE_DECIMAL
    assert types["Montant TTC"] == T.TYPE_DECIMAL


def test_operation_temporelle_refusee_sur_variante_sale(dirty_doc):
    """`between` sur une colonne texte doit être REFUSÉ, jamais approximé."""
    obs = VR.run_case(VR.case_by_id("between_date"), dirty_doc, sample_memory=False)
    assert obs.status != "ok"
    assert obs.code == E.ERR_INCOMPATIBLE_FILTER_TYPE


# --------------------------------------------------------------------------- #
# Matrice de requêtes
# --------------------------------------------------------------------------- #
def _verifier_forme_aboutie(obs) -> None:
    assert obs.status == "ok", f"{obs.code}: {obs.message}"
    assert obs.value is not None
    # Toutes les étapes attendues ont été chronométrées.
    for stage in ("parse_plan", "validation", "compilation", "execution",
                  "provenance", "formatting"):
        assert stage in obs.timings_ms


@pytest.mark.parametrize("case_id", FAST_CASE_IDS)
def test_les_formes_representatives_aboutissent(case_id, clean_doc):
    """Deux profils de coût, sur un corpus réel : un scalaire filtré, un retour de lignes."""
    _verifier_forme_aboutie(VR.run_case(VR.case_by_id(case_id), clean_doc,
                                        sample_memory=False))


@pytest.mark.slow
@pytest.mark.parametrize("case", VR.CASES, ids=lambda c: c.case_id)
def test_chaque_forme_de_requete_aboutit(case, clean_doc):
    """Les treize formes doivent produire un résultat, pas une exception nue.

    Sous `slow` : treize requêtes moteur, chacune payant l'ingestion une ou deux fois.
    Les opérations elles-mêmes sont couvertes en millisecondes par `test_tabular_engine` ;
    ce qui est vérifié ici, et nulle part ailleurs, c'est qu'elles tiennent sur le corpus
    volumétrique complet — accents, doublons, NULL, modalité rare.
    """
    _verifier_forme_aboutie(VR.run_case(case, clean_doc, sample_memory=False))


def test_count_rows_compte_toutes_les_lignes(clean_doc):
    obs = VR.run_case(VR.case_by_id("count_rows"), clean_doc, sample_memory=False)
    assert obs.value == len(clean_doc.rows) == FAST_ROWS


@pytest.mark.slow
def test_filtre_selectif_est_bien_plus_selectif(clean_doc):
    """Deux requêtes filtrées, donc quatre ingestions : mesurée à ~13 s, d'où `slow`."""
    selectif = VR.run_case(VR.case_by_id("sum_selective"), clean_doc, sample_memory=False)
    large = VR.run_case(VR.case_by_id("sum_non_selective"), clean_doc, sample_memory=False)
    assert 0 < selectif.selected_row_count < large.selected_row_count
    # Les deux parcourent la MÊME table : la sélectivité ne réduit pas le balayage.
    assert selectif.scanned_row_count == large.scanned_row_count == FAST_ROWS


@pytest.mark.slow
def test_determinisme_sur_repetitions(clean_doc):
    """Cinq exécutions, une seule signature. Deux ne distinguent pas stable de chanceux.

    Sous `slow` : cinq requêtes filtrées, soit dix ingestions. La stabilité du couple
    (résultat, `plan_hash`) est déjà vérifiée en millisecondes par
    `test_tabular_engine.test_resultat_plan_et_hash_stables_sur_cinq_executions` ; ce que
    cette version ajoute, c'est la même exigence sur le corpus volumétrique.
    """
    case = VR.case_by_id("sum_non_selective")
    signatures = {VR.run_case(case, clean_doc, sample_memory=False).signature()
                  for _ in range(5)}
    assert len(signatures) == 1


# --------------------------------------------------------------------------- #
# Plafond mémoire
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def rapport_memoire(clean_doc):
    """La sonde mémoire lance une requête volontairement gourmande : une fois par module."""
    return VR.probe_memory_limit(clean_doc, tiny_mb=1)


def test_plafond_memoire_applique_par_duckdb(rapport_memoire):
    """`TABULAR_MAX_MEMORY_MB` atteint bien DuckDB, qui refuse plutôt que de tronquer."""
    rapport = rapport_memoire

    # 1. Le réglage atteint bien DuckDB, et DuckDB le fait respecter.
    assert rapport["duckdb_applique_le_plafond"] is True, rapport.get("duckdb_erreur")

    # 2. Le mode d'échec inacceptable : un résultat partiel présenté comme un fait.
    assert rapport["resultat_partiel_avec_ok"] is False

    # 3. La requête suivante fonctionne : la connexion fautive est bien fermée
    #    (`finally` de `executor.execute`), rien ne reste verrouillé.
    assert rapport["requete_suivante_statut"] == "ok"


# Le TYPAGE du dépassement mémoire survenu pendant l'ingestion n'est pas vérifié ici.
# Ce fichier a caractérisé le défaut (`duckdb.OutOfMemoryException` nue, donc un 500 au
# lieu du `limit_exceeded` contractuel) ; le défaut est corrigé, et la propriété est
# désormais tenue par `test_tabular_limits_enforcement`, qui l'obtient sans corpus ni
# sonde volumétrique — donc en une fraction du temps. La dupliquer ici coûterait une
# sonde mémoire complète pour ne rien apprendre de plus.


def test_lingestion_est_desormais_dans_le_bloc_garde():
    """La cause racine, vérifiée dans le source.

    Le contrat ne tient que si l'insertion se déroule SOUS la garde. Une régression
    qui la ressortirait rétablirait silencieusement le 500 : ce test la verrouille.
    """
    import inspect

    from apps.rag_core.tabular import executor as EX

    source = inspect.getsource(EX.execute)
    # On raisonne sur le CODE, pas sur le texte : les commentaires de `execute`
    # mentionnent `executemany` pour expliquer le défaut corrigé, et un simple
    # `in` sur la source y trouverait un faux positif.
    avant_garde = source.split("with _Timeout")[0]
    code_avant = "\n".join(
        ligne for ligne in avant_garde.splitlines()
        if not ligne.strip().startswith("#"))

    assert "executemany" not in code_avant, "le chargement est reparti hors du bloc gardé"
    # Le chargement passe par l'aide dédiée, qui contrôle l'échéance entre lots.
    assert "_ingest(" in source


# --------------------------------------------------------------------------- #
# Délai maximal — lent PAR CONSTRUCTION, donc `slow`
# --------------------------------------------------------------------------- #
# Ce que cette section établit n'est PAS que le délai est respecté — c'est le rôle de
# `test_tabular_limits_enforcement`, qui le fait en moins d'une seconde. C'est que
# `conn.interrupt()` abrège RÉELLEMENT le calcul DuckDB au lieu de seulement cesser
# d'attendre : un délai de façade laisserait la requête consommer CPU et mémoire jusqu'à
# sa fin naturelle. La démonstration se fait au chronomètre, donc exige une requête
# témoin dont la durée NATURELLE se compte en secondes. Aucun réglage de taille ne peut
# la rendre rapide : elle ne dépend pas de `FAST_ROWS`. D'où le marqueur, plutôt que la
# suppression d'une propriété qu'aucun autre test ne couvre.
@pytest.fixture(scope="module")
def rapport_timeout(clean_doc):
    """La sonde de délai est coûteuse (requête témoin de plusieurs secondes) : une fois."""
    return VR.probe_timeout(clean_doc, timeout_s=0.1)


@pytest.mark.slow
def test_le_minuteur_sarme_et_la_connexion_reste_saine(rapport_timeout):
    assert rapport_timeout["timer_declenche"] is True
    # DuckDB lève, plutôt que de rendre un résultat partiel.
    assert rapport_timeout["requete_interrompue"] is True
    # La requête suivante passe : rien n'est resté verrouillé.
    assert rapport_timeout["requete_suivante_statut"] == "ok"


@pytest.mark.slow
def test_interruption_duckdb_effective(rapport_timeout):
    """`conn.interrupt()` abandonne-t-il RÉELLEMENT le calcul ?

    La distinction est capitale : un délai qui ne ferait que « cesser d'attendre » ne
    protégerait de rien, la requête continuant de consommer CPU et mémoire jusqu'à sa
    fin naturelle. On tranche au chronomètre : une requête dont la durée naturelle est
    connue est relancée sous un délai bien plus court, et l'on compare les retours.
    """
    if rapport_timeout["duree_naturelle_s"] < 0.5:
        pytest.skip("machine trop rapide : la requête témoin ne dure pas assez "
                    "longtemps pour que l'interruption soit observable")
    assert rapport_timeout["interruption_reelle"] is True, \
        rapport_timeout["verdict_interruption"]
    assert "InterruptException" in rapport_timeout["erreur_interruption"]


# Que `max_query_seconds` borne la requête ENTIÈRE — chargement compris — et qu'aucun
# résultat partiel ne soit rendu après un dépassement N'EST PAS vérifié ici. Ce fichier
# a caractérisé le défaut (34 s d'ingestion rendues `ok` sous un plafond de 0,5 s) ;
# depuis le correctif, `test_tabular_limits_enforcement` tient les deux propriétés sans
# sonde ni corpus, donc en moins d'une seconde. Les redire ici imposerait la sonde de
# délai à la suite standard pour ne rien apprendre de plus.


# --------------------------------------------------------------------------- #
# Concurrence
# --------------------------------------------------------------------------- #
def _verifier_absence_de_fuite(rapport: dict, scenario: str) -> None:
    """Contrôles communs à tous les niveaux de parallélisme.

    Le contrôle décisif n'est pas « rien n'a planté » mais l'identité des SIGNATURES
    avec l'exécution séquentielle : une fuite d'état se manifeste par un résultat, un
    `plan_hash` ou un `evidence_hash` divergent bien avant de produire une exception.
    """
    assert rapport["fuites"] == 0, rapport["details_erreurs"]
    assert rapport["erreurs"] == 0, rapport["details_erreurs"]
    assert rapport["timeouts"] == 0

    # Déterminisme : un même couple (document, question) rend toujours le même plan,
    # quel que soit le parallélisme. L'unité de comparaison est le COUPLE et non la
    # question seule — le plan porte le `document_id` de sa source.
    assert rapport["plan_hash_stable_par_couple"] is True, rapport

    if scenario == "meme_doc_questions_differentes":
        # Absence de collision : au sein d'un document, deux questions différentes ne
        # partagent jamais un `plan_hash`.
        assert rapport["plan_hash_distinct_entre_cas"] is True, rapport

    if scenario == "docs_differents":
        # Provenance PAR DOCUMENT. La même question posée sur deux documents doit
        # produire deux `plan_hash` distincts : exiger ici la stabilité reviendrait à
        # exiger que la preuve soit indépendante du document qu'elle cite — soit
        # exactement le contraire de ce qu'on veut. La distinction est donc la
        # propriété à affirmer, et elle s'affirme explicitement.
        distincts = rapport["plan_hash_distinct_entre_documents"]
        if distincts is None:
            # Un seul thread ne sollicite qu'un seul document : il n'y a alors rien à
            # distinguer. La sonde rend `None` plutôt qu'un `True` de complaisance, et
            # le test le reconnaît au lieu de le confondre avec une propriété vérifiée.
            assert rapport["workers"] == 1, rapport
        else:
            assert distincts is True, rapport


@pytest.mark.parametrize("scenario", VR.CONCURRENCY_SCENARIOS)
@pytest.mark.staging_qualification
def test_concurrence_sans_fuite(scenario, petits_docs):
    """Aucune contamination entre threads, sur les trois scénarios de partage.

    Deux choix rendent ce contrôle abordable sans rien lui retirer : de PETITS documents
    et des cas NON FILTRÉS (`CONCURRENCY_CASES`). Une contamination entre threads ne
    dépend en effet ni du nombre de lignes ni du coût des requêtes — elle se voit dans la
    signature, qui ne change pas de nature parce que la table est plus petite. La version
    chère (corpus complet, cas filtré, 1 → 10 threads) reste jouée sous `slow`.
    """
    rapport = VR.probe_concurrency(petits_docs, scenario=scenario, workers=FAST_WORKERS,
                                   case_id=CONCURRENCY_CASES[0],
                                   case_pool=CONCURRENCY_CASES)
    _verifier_absence_de_fuite(rapport, scenario)


@pytest.mark.slow
@pytest.mark.parametrize("workers", [1, 2, 5, 10])
@pytest.mark.parametrize("scenario", VR.CONCURRENCY_SCENARIOS)
def test_concurrence_sans_fuite_en_charge(scenario, workers, clean_doc, dirty_doc):
    """La même propriété, sur toute la plage de parallélisme, y compris à 1 thread.

    Le niveau 1 n'est pas une redondance : il sépare « une fuite due au parallélisme »
    d'« une fuite due à un état global », qui se manifesterait même en séquentiel.
    """
    rapport = VR.probe_concurrency([clean_doc, dirty_doc], scenario=scenario,
                                   workers=workers)
    _verifier_absence_de_fuite(rapport, scenario)
    assert rapport["resultats_stables"] is True, rapport["details_erreurs"]
    assert rapport["p95_ms"] > 0


# --------------------------------------------------------------------------- #
# Ingestion : la mesure qui explique tout le reste
# --------------------------------------------------------------------------- #
def test_ingestion_domine_le_temps_dexecution(clean_doc):
    """L'insertion doit être MESURÉE, pas fondue dans « exécution ».

    Le test fige le constat central de la qualification : le calcul DuckDB se compte en
    millisecondes, l'ingestion en secondes. Sans ce rapport de grandeur, on optimiserait
    la mauvaise moitié.
    """
    split = VR.probe_ingestion_split(clean_doc)
    assert split["rows"] == FAST_ROWS
    assert split["insert_ms"] > split["select_ms"] * 10
    assert split["insert_rows_per_s"] > 0


# --------------------------------------------------------------------------- #
# Campagne complète — opt-in
# --------------------------------------------------------------------------- #
@pytest.mark.slow
@pytest.mark.skipif(not FULL, reason="campagne longue : exiger TABULAR_VOLUME_FULL=1")
@pytest.mark.parametrize("rows", VC.FULL_SIZES)
def test_campagne_volumetrique_complete(rows):
    """100 k / 500 k / 1 M — plusieurs heures. Jamais en CI.

    Un cas seulement par taille : à ~500 lignes/s d'ingestion, la matrice complète à
    1 M de lignes dépasserait la dizaine d'heures.
    """
    rapport = VR.run_size(rows, case_ids=("count_rows",))
    assert rapport["rows_loaded"] == rows
    assert rapport["cases"][0]["status"] == "ok"
    assert int(rapport["cases"][0]["value"]) == rows
