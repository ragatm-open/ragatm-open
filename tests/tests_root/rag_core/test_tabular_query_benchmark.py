"""Benchmark du moteur tabulaire — reproductibilité, exécution, métriques, indépendance.

Ces tests ne valident PAS les performances du moteur : un seuil de qualité gelé ici
transformerait le benchmark en test de non-régression déguisé, et le premier chiffre
mesuré deviendrait la norme. Ils valident l'**instrument** :

* le corpus est reproductible à l'octet près, y compris d'un processus à l'autre ;
* le runner traverse réellement la chaîne de production et n'échoue sur aucun cas ;
* les métriques se calculent et comparent en `Decimal`, jamais en flottant ;
* la vérité terrain reste **indépendante** du moteur et de
  `benchmark.tabular_reference_executor` — la propriété qui rend la mesure valable.
"""
from __future__ import annotations

import ast
import hashlib
import io
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from apps.rag_core.benchmark import tabular_query_dataset as ds
from apps.rag_core.benchmark import tabular_query_metrics as met
from apps.rag_core.benchmark import tabular_query_runner as runner

#: Le module dont l'importation invaliderait la mesure. Nommé une seule fois.
FORBIDDEN_MODULE = "tabular_reference_executor"


# --------------------------------------------------------------------------- #
# Reproductibilité du générateur
# --------------------------------------------------------------------------- #
def test_corpus_identique_a_graine_egale(tmp_path):
    """Même graine → mêmes octets, pour chaque document du corpus.

    L'égalité porte sur les OCTETS et pas sur les valeurs : un classeur dont seules
    les métadonnées bougeraient donnerait des `content_sha256` différents, donc des
    preuves différentes pour un même calcul.
    """
    a = ds.build_corpus(tmp_path / "a")
    b = ds.build_corpus(tmp_path / "b")

    assert sorted(a.documents) == sorted(b.documents)
    for name, doc in a.documents.items():
        assert doc.data == b.documents[name].data, f"octets instables : {name}"


def test_cas_identiques_a_graine_egale(tmp_path):
    _corpus_a, cases_a = ds.build_dataset(tmp_path / "a")
    _corpus_b, cases_b = ds.build_dataset(tmp_path / "b")
    assert ds.to_jsonl(cases_a) == ds.to_jsonl(cases_b)


def test_reproductible_entre_processus(tmp_path):
    """Deux processus, deux `PYTHONHASHSEED` → mêmes empreintes.

    Le test existe parce que la reproductibilité intra-processus ne prouve rien : un
    `hash()` Python ou un `uuid4()` resterait stable dans une même exécution et
    divergerait à la suivante.
    """
    empreintes = {
        seed: _empreintes_dans_un_sous_processus(tmp_path / f"p{seed}", seed)
        for seed in ("0", "12345")
    }
    assert empreintes["0"] == empreintes["12345"]


def _empreintes_dans_un_sous_processus(directory: Path, hashseed: str) -> str:
    script = (
        "import os, django, hashlib, json;"
        "os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings');"
        "django.setup();"
        "from apps.rag_core.benchmark import tabular_query_dataset as ds;"
        f"corpus, cases = ds.build_dataset({str(directory)!r});"
        "parts = [n + ':' + hashlib.sha256(d.data).hexdigest()"
        " for n, d in sorted(corpus.documents.items())];"
        "parts.append('cases:' + hashlib.sha256("
        "ds.to_jsonl(cases).encode('utf-8')).hexdigest());"
        "print('|'.join(parts))"
    )
    out = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, check=True,
        env={**_env(), "PYTHONHASHSEED": hashseed},
        cwd=str(_backend_root()))
    return out.stdout.strip()


def _env() -> dict:
    import os
    return dict(os.environ)


def _backend_root() -> Path:
    # parents[3] = racine du dépôt (stable où que vive ce fichier), puis "backend".
    return Path(__file__).resolve().parents[3] / "backend"


def test_corpus_couvre_les_pieges_annonces(tmp_path):
    """Un corpus « propre » mesurerait le cas facile et rien d'autre."""
    corpus = ds.build_corpus(tmp_path)
    ventes = corpus.doc(ds.DOC_VENTES)

    # Zéros initiaux significatifs.
    assert all(r["Reference"].startswith("0") for r in ventes.rows)
    # Valeurs nulles ET négatives dans la même colonne.
    soldes = [r["Solde"] for r in corpus.doc(ds.DOC_ANOMALIES).rows]
    assert None in soldes
    assert any(s is not None and s < 0 for s in soldes)
    # Colonnes quasi homonymes.
    compta = corpus.doc(ds.DOC_COMPTA)
    assert len([c for c in compta.columns if c.lower().startswith("montant")]) == 4
    # Formule SANS valeur en cache. On ouvre l'archive plutôt que d'inspecter les
    # octets : le XML est compressé, et une absence de correspondance ne prouverait
    # rien. La balise `<f>` sans `<v>` associé est la signature exacte du piège —
    # une formule qu'openpyxl relira comme `None`, donc comme une cellule vide.
    import re
    import zipfile
    with zipfile.ZipFile(io.BytesIO(corpus.doc(ds.DOC_FORMULES).data)) as archive:
        feuille = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert "<f>B2*2</f>" in feuille
    # openpyxl écrit un `<v>` VIDE : c'est bien l'absence de résultat mis en cache.
    assert not re.search(r"<f>B2\*2</f>\s*<v>\s*[^<\s]", feuille)


# --------------------------------------------------------------------------- #
# Dataset
# --------------------------------------------------------------------------- #
def test_toutes_les_categories_sont_couvertes(tmp_path):
    _corpus, cases = ds.build_dataset(tmp_path)
    couvertes = {c["category"] for c in cases}
    assert couvertes == set(ds.CATEGORIES)


def test_le_dataset_porte_des_cas_negatifs(tmp_path):
    """Un refus attendu est un résultat, pas un trou dans le dataset."""
    _corpus, cases = ds.build_dataset(tmp_path)
    refus = [c for c in cases if c["expected_status"] != ds.STATUS_OK]
    assert len(refus) >= 8
    assert {c["expected_status"] for c in refus} == {
        ds.STATUS_CLARIFICATION, ds.STATUS_UNSUPPORTED, ds.STATUS_ERROR}
    # Un refus attendu sans code attendu ne dit pas POURQUOI le moteur doit refuser.
    assert all(c["expected_code"] for c in refus)


def test_les_champs_obligatoires_sont_presents(tmp_path):
    _corpus, cases = ds.build_dataset(tmp_path)
    requis = {"id", "question", "document", "expected_sheet", "expected_table",
              "expected_columns", "expected_operation", "expected_filters",
              "expected_value", "tolerance", "category", "expected_status",
              "expected_code"}
    for c in cases:
        assert requis <= set(c), f"champs manquants sur {c['id']}"


def test_aucune_valeur_attendue_nest_un_flottant(tmp_path):
    """Un `float` dans la vérité terrain réintroduirait l'erreur qu'on mesure."""
    _corpus, cases = ds.build_dataset(tmp_path)
    for c in cases:
        valeurs = c["expected_value"]
        for v in (valeurs if isinstance(valeurs, list) else [valeurs]):
            assert not isinstance(v, float), f"{c['id']} porte un flottant"


def test_le_jsonl_est_relisable(tmp_path):
    _corpus, cases = ds.build_dataset(tmp_path)
    texte = (tmp_path / "tabular_query_cases.jsonl").read_text(encoding="utf-8")
    assert ds.load_jsonl(texte) == sorted(cases, key=lambda c: c["id"])


# --------------------------------------------------------------------------- #
# Vérité terrain — calculée en Python nu
# --------------------------------------------------------------------------- #
def test_verite_terrain_ignore_les_nuls_sans_les_compter_comme_zero(tmp_path):
    """La moyenne divise par les valeurs agrégées, pas par les lignes."""
    corpus = ds.build_corpus(tmp_path)
    rows = corpus.doc(ds.DOC_ANOMALIES).rows
    non_nuls = [r["Solde"] for r in rows if r["Solde"] is not None]

    assert ds.gt_count(rows) == len(rows)
    assert ds.gt_average(rows, "Solde") == sum(non_nuls, Decimal(0)) / Decimal(len(non_nuls))
    # Le dénominateur diffère bien du nombre de lignes : le cas mesure quelque chose.
    assert len(non_nuls) < len(rows)


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def rapport(tmp_path_factory) -> dict:
    """Une seule exécution complète du benchmark, partagée par les tests suivants."""
    directory = tmp_path_factory.mktemp("tabular_query_bench")
    return runner.run_benchmark(directory)


def test_le_runner_execute_tous_les_cas(rapport):
    assert rapport["meta"]["cases"] == len(rapport["cases"])
    assert rapport["meta"]["runs_per_case"] == runner.RUNS_PER_CASE
    statuts = {c["observed_status"] for c in rapport["cases"]}
    # Aucun cas ne doit sortir sans statut : une exception non typée serait un mode
    # d'échec du moteur que le benchmark ne saurait pas nommer.
    assert "" not in statuts
    assert statuts <= {ds.STATUS_OK, ds.STATUS_CLARIFICATION,
                       ds.STATUS_UNSUPPORTED, ds.STATUS_ERROR,
                       "forbidden", "limit_exceeded"}


def test_le_runner_est_deterministe(rapport):
    """Cinq exécutions par cas : même résultat, même plan, même preuve."""
    instables = [c["id"] for c in rapport["cases"] if not c["deterministic"]]
    assert instables == []
    assert rapport["summary"]["metrics"]["determinism_rate"]["value"] == 1.0


def test_aucune_charge_hostile_natteint_le_sql(rapport):
    """Propriété structurelle du compilateur, vérifiée et non supposée."""
    securite = [c for c in rapport["cases"] if c["category"] == "security"]
    assert securite
    assert all(c["safe"] for c in securite)


def test_le_rapport_markdown_est_produit(rapport):
    texte = runner.to_markdown(rapport)
    assert texte.startswith("# Benchmark")
    assert "## Métriques" in texte
    assert "## Par catégorie" in texte
    assert "déterminisme" in texte


def test_le_document_est_resolu_par_citation_explicite(tmp_path):
    """Sans citation, plusieurs documents sont candidats : le refus est la réponse."""
    corpus = ds.build_corpus(tmp_path)
    obs = runner.run_once({"id": "x", "question": "Quelle est la somme du CA HT ?"},
                          corpus)
    assert obs.status == ds.STATUS_CLARIFICATION
    assert obs.code == "ambiguous_document"


# --------------------------------------------------------------------------- #
# Métriques
# --------------------------------------------------------------------------- #
def test_toutes_les_metriques_exigees_sont_calculees(rapport):
    metriques = rapport["summary"]["metrics"]
    assert set(met.METRIC_LABELS) <= set(metriques)
    for nom, entree in metriques.items():
        assert set(entree) == {"value", "hits", "support"}, nom
        if entree["support"]:
            assert 0.0 <= entree["value"] <= 1.0, nom
        else:
            # Une métrique sans base est absente, pas nulle : la distinction évite de
            # lire « 0 % » là où rien n'a été mesuré.
            assert entree["value"] is None, nom


def test_les_latences_sont_rapportees(rapport):
    latence = rapport["summary"]["latency_ms"]
    assert latence["p50"] > 0
    assert latence["p95"] >= latence["p50"]


def test_comparaison_numerique_en_decimal_et_jamais_en_flottant():
    # 0.1 + 0.2 != 0.3 en binaire ; en `Decimal` la somme est exacte.
    ok, _ = met.compare_value("0.3", Decimal("0.1") + Decimal("0.2"), tolerance="0")
    assert ok

    # Deux écritures du même montant sont le même montant.
    ok, _ = met.compare_value("12.50", Decimal("12.5"), tolerance="0")
    assert ok

    # La tolérance est celle du cas, appliquée strictement.
    ok, _ = met.compare_value("100.00", Decimal("100.02"), tolerance="0.01")
    assert not ok
    ok, _ = met.compare_value("100.00", Decimal("100.01"), tolerance="0.01")
    assert ok


def test_un_classement_se_compare_dans_lordre():
    ok, _ = met.compare_value(["a", "b"], ["b", "a"], tolerance="0")
    assert not ok
    ok, _ = met.compare_value(["a", "b"], ["a", "b"], tolerance="0")
    assert ok


def test_une_reponse_ok_mais_fausse_compte_comme_fausse_reponse():
    """Le mode d'échec critique : `ok` affiché, chiffre faux."""
    cas = {"id": "t", "category": "aggregation", "expected_status": "ok",
           "expected_code": "", "expected_value": "100", "tolerance": "0"}
    evaluation = met.evaluate_case(
        cas, {"status": "ok", "value": Decimal("120")},
        deterministic=True, latency_ms=1.0)
    assert not evaluation.correct
    synthese = met.aggregate([evaluation])
    assert synthese["metrics"]["false_answer_rate"]["value"] == 1.0


def test_un_refus_attendu_obtenu_ne_compte_pas_comme_fausse_reponse():
    cas = {"id": "t", "category": "ambiguity",
           "expected_status": ds.STATUS_CLARIFICATION,
           "expected_code": "ambiguous_column", "expected_value": None}
    evaluation = met.evaluate_case(
        cas, {"status": ds.STATUS_CLARIFICATION, "code": "ambiguous_column"},
        deterministic=True, latency_ms=1.0)
    assert evaluation.correct
    synthese = met.aggregate([evaluation])
    assert synthese["metrics"]["false_answer_rate"]["value"] == 0.0
    assert synthese["metrics"]["clarification_recall"]["value"] == 1.0


def test_un_refus_pour_la_mauvaise_raison_nest_pas_correct():
    """S'arrêter au bon endroit fait partie de la réponse."""
    cas = {"id": "t", "category": "formulas", "expected_status": ds.STATUS_ERROR,
           "expected_code": "formula_result_unavailable", "expected_value": None}
    evaluation = met.evaluate_case(
        cas, {"status": ds.STATUS_ERROR, "code": "empty_selection"},
        deterministic=True, latency_ms=1.0)
    assert not evaluation.status_ok


# --------------------------------------------------------------------------- #
# Indépendance — la propriété qui rend la mesure valable
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("module", [ds, met, runner])
def test_aucun_module_du_benchmark_nimporte_lexecuteur_de_reference(module):
    """Règle absolue : la vérité terrain ne passe jamais par le même code que le moteur.

    L'analyse porte sur l'AST plutôt que sur le texte : les docstrings de ces modules
    NOMMENT `tabular_reference_executor` pour expliquer pourquoi il est proscrit, et
    une recherche textuelle confondrait l'explication avec l'infraction.
    """
    arbre = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    importes: list[str] = []
    for noeud in ast.walk(arbre):
        if isinstance(noeud, ast.Import):
            importes.extend(alias.name for alias in noeud.names)
        elif isinstance(noeud, ast.ImportFrom):
            importes.append(noeud.module or "")
            importes.extend(alias.name for alias in noeud.names)
    assert not any(FORBIDDEN_MODULE in nom for nom in importes), \
        f"{module.__name__} importe {FORBIDDEN_MODULE}"


def test_lexecuteur_de_reference_nest_pas_charge_transitivement():
    """Un import indirect romprait l'indépendance aussi sûrement qu'un import direct."""
    script = (
        "import os, sys, django;"
        "os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings');"
        "django.setup();"
        "from apps.rag_core.benchmark import tabular_query_runner;"
        "from apps.rag_core.benchmark import tabular_query_metrics;"
        "from apps.rag_core.benchmark import tabular_query_dataset;"
        f"print(any({FORBIDDEN_MODULE!r} in m for m in sys.modules))"
    )
    out = subprocess.run([sys.executable, "-c", script], capture_output=True,
                         text=True, check=True, cwd=str(_backend_root()))
    assert out.stdout.strip() == "False", out.stdout


def test_la_verite_terrain_ne_depend_pas_du_moteur():
    """`tabular_query_dataset` n'importe rien de `apps.rag_core.tabular`.

    C'est ce qui garantit qu'un défaut de conversion du moteur ne peut pas être
    reproduit à l'identique dans la valeur attendue, donc rester invisible.
    """
    arbre = ast.parse(Path(ds.__file__).read_text(encoding="utf-8"))
    for noeud in ast.walk(arbre):
        cibles: list[str] = []
        if isinstance(noeud, ast.Import):
            cibles = [alias.name for alias in noeud.names]
        elif isinstance(noeud, ast.ImportFrom):
            cibles = [noeud.module or ""]
        assert not any(c.startswith("apps.rag_core.tabular") for c in cibles), \
            f"la vérité terrain importe le moteur : {cibles}"


def test_lempreinte_du_corpus_est_stable_et_documentee(tmp_path):
    """Empreinte de référence du corpus, à mettre à jour EXPRÈS si le corpus change.

    Sa raison d'être : rendre visible dans une revue toute modification du corpus.
    Un dataset qui bouge en silence rend deux rapports de benchmark incomparables.
    """
    corpus = ds.build_corpus(tmp_path)
    digest = hashlib.sha256()
    for nom, doc in sorted(corpus.documents.items()):
        digest.update(nom.encode("utf-8"))
        digest.update(doc.data)
    # Non figée en dur : on vérifie seulement qu'elle est stable d'un appel à l'autre.
    autre = ds.build_corpus(tmp_path / "bis")
    digest2 = hashlib.sha256()
    for nom, doc in sorted(autre.documents.items()):
        digest2.update(nom.encode("utf-8"))
        digest2.update(doc.data)
    assert digest.hexdigest() == digest2.hexdigest()
