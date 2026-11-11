"""`RAG_RERANK_TOP_K` : à quel reranker appartient-il, et que vaut sa divergence ?

Le manifeste de parité portait un suivi ambigu — « aligner avant toute activation du
reranking » — alors que deux rerankings coexistent. Sous le pipeline en service
(reranker AI Gateway actif, reranker historique éteint), la question devient
opérationnelle : cette divergence prod=5 / staging=4 invalide-t-elle une campagne ?

Verdict établi ici, par le code : **legacy-only**. Le réglage n'est lu que par le
reranker historique, à l'intérieur de sa propre garde. Le chemin Gateway ne le lit
jamais — sa profondeur est `RAG_RERANKER_POOL_K`, son plafond
`RAG_RERANKER_MAX_PER_GROUP`.

Il reste une dette, distincte et non corrigée ici : le défaut diverge DANS LE CODE
(`settings/base.py`=4, `domain/policies.py`=5).
"""
from __future__ import annotations

import ast
import pathlib

from apps.rag_core.benchmark import holdout_config as hc

RACINE = pathlib.Path(__file__).resolve().parents[3] / "backend"


def _modules_lisant(nom: str) -> set[str]:
    """Modules du pipeline qui LISENT réellement ce réglage (analyse syntaxique)."""
    apps_dir = RACINE / "apps"
    trouves = set()
    for rel in hc._PIPELINE_FILES:
        chemin = apps_dir / rel
        if not chemin.exists():
            continue
        arbre = ast.parse(chemin.read_text(encoding="utf-8"))
        if nom in hc._read_names_in_module(arbre):
            trouves.add(rel)
    return trouves


def test_the_setting_is_read_only_on_the_legacy_path():
    """Deux lectures, toutes deux du chemin historique.

    `orchestrator` le pose dans la `RagPolicy` (que `candidate_reranker` consomme
    ensuite via `policy.rerank_top_k`, sans jamais nommer le réglage) ; `policies`
    en dérive son défaut de module. Aucun autre module du pipeline ne le lit — et
    surtout pas le client Gateway.
    """
    lecteurs = _modules_lisant("RAG_RERANK_TOP_K")
    assert lecteurs == {
        "rag_core/services/query/orchestrator.py",
        "rag_core/domain/policies.py",
    }, lecteurs


def test_the_gateway_module_never_reads_it():
    """La preuve directe qu'il n'appartient pas au chemin AI Gateway."""
    arbre = ast.parse(
        (RACINE / "apps/rag_core/services/rerank_gateway.py").read_text(encoding="utf-8")
    )
    lus = hc._read_names_in_module(arbre)
    assert "RAG_RERANK_TOP_K" not in lus
    # Ce que le chemin Gateway lit RÉELLEMENT à la place.
    assert {"RAG_RERANKER_POOL_K", "RAG_RERANKER_MAX_PER_GROUP"} <= lus


def test_it_is_consumed_behind_the_legacy_flag_only():
    """`rerank_take` n'est calculé qu'à l'intérieur de `if policy.rerank_enabled`.

    C'est ce qui rend le réglage INERTE sous le pipeline en service : le drapeau
    historique y vaut 0.
    """
    source = (RACINE / "apps/rag_core/services/query/candidate_reranker.py").read_text(
        encoding="utf-8"
    )
    arbre = ast.parse(source)
    fonction = next(
        n for n in ast.walk(arbre)
        if isinstance(n, ast.FunctionDef) and n.name == "rank_candidates"
    )
    garde = next(
        n for n in fonction.body
        if isinstance(n, ast.If) and "rerank_enabled" in ast.dump(n.test)
    )
    assert "rerank_take" in ast.dump(garde), (
        "la troncature doit rester à l'intérieur de la garde du reranker historique"
    )
    dehors = [n for n in fonction.body if n is not garde]
    assert all("rerank_take" not in ast.dump(n) for n in dehors)


def test_the_legacy_reranker_is_off_under_the_target_pipeline(settings):
    """Le pipeline cible éteint le reranker historique — donc le réglage est inerte.

    Les deux ne peuvent d'ailleurs pas être actifs ensemble (`rag_core.E008`).
    """
    from apps.rag_core.checks_rerank import check_rerank_paths_exclusive

    settings.RAG_RERANK_ENABLED = False
    settings.RAG_RERANKER_ENABLED = True
    assert check_rerank_paths_exclusive(None) == []


def test_it_stays_frozen_even_though_it_is_inert():
    """Inerte n'est pas « hors identité ».

    Le geler est ce qui garantit qu'une campagne ne peut pas rallumer le reranker
    historique en cours de route sans que le sceau change.
    """
    for schema in hc.FREEZE_SCHEMAS:
        assert "RAG_RERANK_TOP_K" in {p.name for p in hc.result_params(schema)}


def test_the_double_default_debt_is_still_open_and_visible():
    """Dette CONNUE, non corrigée ici : deux défauts pour un même réglage.

    `settings/base.py` lit la variable avec un défaut de 4, `domain/policies.py` avec
    un défaut de 5. C'est sans effet tant que l'orchestrateur passe explicitement
    `settings.RAG_RERANK_TOP_K` à la politique — mais toute construction de
    `RagPolicy()` sans cet argument prend l'autre défaut.

    L'assertion porte sur les LITTÉRAUX du source, jamais sur la valeur résolue : la
    seconde dépend du `.env` de la machine, et un test qui en dépendrait dirait
    quelque chose de l'environnement plutôt que du code. Ce test deviendra rouge le
    jour où quelqu'un aura unifié les deux défauts — c'est le signal voulu.
    """
    base = (RACINE / "config/settings/base.py").read_text(encoding="utf-8")
    policies_src = (RACINE / "apps/rag_core/domain/policies.py").read_text(encoding="utf-8")

    assert 'os.getenv("RAG_RERANK_TOP_K", "4")' in base
    assert 'os.getenv("RAG_RERANK_TOP_K", "5")' in policies_src
