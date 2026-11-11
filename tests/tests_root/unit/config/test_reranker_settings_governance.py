"""Les leviers d'exploitation du reranking Gateway sont gouvernés par le registre.

Ce que ce fichier verrouille, et pourquoi il existe.

Sept réglages `RAG_RERANKER_*` — activation, URL de passerelle, modèle logique,
profondeur de pool, délai dédié, politique d'échec, plafond documentaire — étaient
lus par `getattr(settings, …)` sans figurer au registre de configuration. Ils
*fonctionnaient* : `base.py` les projette depuis l'environnement. Ce qui manquait
était la garde qui assure qu'ils continueront de fonctionner — celle qui fait qu'un
réglage ne peut pas devenir un défaut d'appel silencieux.

Le cas est plus sérieux que la moyenne parce que ces réglages pilotent une
politique de **dégradation silencieuse** : sous `fail_open_rrf`, une panne ne casse
aucune requête, elle rend des réponses moins bien classées. Un délai ou un plafond
qui « a l'air de fonctionner » sans agir serait indétectable en exploitation.

`RAG_OPS_METRICS_TOKEN` posait un problème voisin mais distinct : lu depuis
`config/urls.py`, il échappait entièrement au balayage des réglages non déclarés,
qui ne parcourait que `apps/` et `api/`. C'est un secret ; l'angle mort portait donc
sur la surface la plus sensible.
"""
from __future__ import annotations

import pathlib
import pytest
from django.conf import settings

from config import registry
from config.parsers import ConfigValueError

#: Les sept réglages du chemin Gateway, avec la valeur GOUVERNÉE — celle qui est
#: déjà en vigueur. Ce lot déclare ; il ne redéfinit rien.
RERANKER_SETTINGS = {
    "RAG_RERANKER_ENABLED": ("bool", False),
    "RAG_RERANKER_BASE_URL": ("url", ""),
    "RAG_RERANKER_MODEL_LOGICAL": ("str", "rerank-default"),
    "RAG_RERANKER_POOL_K": ("int", 40),
    "RAG_RERANKER_TIMEOUT_SECONDS": ("float", 2.0),
    "RAG_RERANKER_FAILURE_POLICY": ("enum", "fail_open_rrf"),
    "RAG_RERANKER_MAX_PER_GROUP": ("int", 3),
}


# ===========================================================================
# 1. Tout réglage lu par l'implémentation est déclaré
# ===========================================================================
def _literal_reranker_names() -> set[str]:
    """Noms `RAG_RERANKER_*` réellement lus par le client durable, relus du source."""
    import ast
    import pathlib

    chemin = (
        pathlib.Path(__file__).resolve().parents[4]
        / "backend/apps/rag_core/services/rerank_gateway.py"
    )
    arbre = ast.parse(chemin.read_text(encoding="utf-8"))
    noms: set[str] = set()
    for noeud in ast.walk(arbre):
        if (
            isinstance(noeud, ast.Call)
            and isinstance(noeud.func, ast.Name)
            and noeud.func.id == "getattr"
            and len(noeud.args) >= 2
            and isinstance(noeud.args[0], ast.Name)
            and noeud.args[0].id == "settings"
            and isinstance(noeud.args[1], ast.Constant)
            and isinstance(noeud.args[1].value, str)
        ):
            noms.add(noeud.args[1].value)
    return {n for n in noms if n.startswith("RAG_RERANKER_")}


def test_every_setting_read_by_the_client_is_declared():
    """Relu du source : aucune liste écrite à la main ne peut se désynchroniser."""
    lus = _literal_reranker_names()
    assert lus, "le balayage doit voir les lectures ; s'il n'en voit aucune, il est cassé"
    non_declares = sorted(n for n in lus if registry.get(n) is None)
    assert not non_declares, f"réglages lus mais non déclarés : {non_declares}"


def test_no_eighth_hidden_control():
    """Le test en échec en signalait sept. Rien de plus ne doit se cacher."""
    assert _literal_reranker_names() == set(RERANKER_SETTINGS)


# ===========================================================================
# 2. Le registre décrit le comportement en vigueur — il ne le redéfinit pas
# ===========================================================================
@pytest.mark.parametrize("name,attendu", sorted(RERANKER_SETTINGS.items()))
def test_declared_type_and_default_match_governed_behaviour(name, attendu):
    value_type, default = attendu
    definition = registry.require(name)
    assert definition.value_type == value_type
    assert definition.default == default, (
        "le registre doit refléter la valeur gouvernée, jamais en introduire une autre"
    )
    assert definition.domain == "retrieval"
    assert definition.consumers, "un réglage sans consommateur déclaré est ingouvernable"


@pytest.mark.parametrize("name", sorted(RERANKER_SETTINGS))
def test_setting_is_projected_into_django_settings(name):
    """Une définition déconnectée de la projection réelle ne gouverne rien."""
    assert hasattr(settings, name)


@pytest.mark.parametrize("name", sorted(RERANKER_SETTINGS))
def test_declared_default_is_what_an_empty_environment_yields(name):
    """Environnement vide : le défaut du registre doit être celui qui s'applique.

    Sans cette vérification, le test dépendrait du `.env` du poste.
    """
    from config.settings.from_registry import env_value

    assert env_value(name, environ={}) == registry.require(name).default


# ===========================================================================
# 3. Les invariants de sécurité de production
# ===========================================================================
def test_reranking_is_off_when_nothing_is_set():
    """Invariant de sûreté : l'absence de variable laisse le reranking ÉTEINT."""
    from config.settings.from_registry import env_value

    assert env_value("RAG_RERANKER_ENABLED", environ={}) is False
    assert registry.require("RAG_RERANKER_ENABLED").default is False


def test_the_governed_operational_values_are_unchanged():
    """Délai, plafond et politique d'échec : ce lot déclare, il ne retouche pas."""
    assert registry.require("RAG_RERANKER_TIMEOUT_SECONDS").default == 2.0
    assert registry.require("RAG_RERANKER_MAX_PER_GROUP").default == 3
    assert registry.require("RAG_RERANKER_FAILURE_POLICY").default == "fail_open_rrf"


def test_the_gateway_url_is_treated_as_a_private_value():
    """Une URL de passerelle privée ne s'affiche pas en clair — convention du projet."""
    definition = registry.require("RAG_RERANKER_BASE_URL")
    assert definition.secret is True
    assert definition.redact_mode == "url"
    rendu = definition.display_value("http://ai-gateway:8100")
    assert "<hôte-gateway>" not in rendu


# ===========================================================================
# 4. La validation refuse au lieu de retomber en silence
# ===========================================================================
def test_an_unknown_failure_policy_is_rejected():
    """Une politique inconnue doit être REFUSÉE, jamais ramenée au défaut.

    Retomber silencieusement sur `fail_open_rrf` ferait passer une faute de frappe
    pour une configuration valide — et la panne suivante serait un repli qu'on
    croirait choisi.
    """
    definition = registry.require("RAG_RERANKER_FAILURE_POLICY")
    with pytest.raises(ConfigValueError):
        definition.parse("fail_open")
    assert definition.parse("fail_closed") == "fail_closed"
    assert definition.choices == ("fail_open_rrf", "fail_closed")


@pytest.mark.parametrize(
    "name,invalide",
    [
        ("RAG_RERANKER_TIMEOUT_SECONDS", "deux secondes"),
        ("RAG_RERANKER_POOL_K", "beaucoup"),
        ("RAG_RERANKER_MAX_PER_GROUP", "3.5"),
    ],
)
def test_a_wrong_type_never_becomes_valid_configuration(name, invalide):
    with pytest.raises(ConfigValueError):
        registry.require(name).parse(invalide)


@pytest.mark.parametrize(
    "name,hors_bornes",
    [
        ("RAG_RERANKER_POOL_K", "0"),
        ("RAG_RERANKER_TIMEOUT_SECONDS", "0"),
        ("RAG_RERANKER_MAX_PER_GROUP", "0"),
    ],
)
def test_out_of_bounds_values_are_rejected(name, hors_bornes):
    """Les bornes reprennent les conventions du projet ; aucune plage inventée ici."""
    with pytest.raises(ConfigValueError):
        registry.require(name).parse(hors_bornes)


def test_the_pool_depth_cannot_exceed_the_gateway_contract():
    """Le contrat `/v1/rerank` plafonne à 64 documents : au-delà, l'appel serait rejeté."""
    definition = registry.require("RAG_RERANKER_POOL_K")
    assert definition.maximum == 64
    with pytest.raises(ConfigValueError):
        definition.parse("65")


# ===========================================================================
# 5. Le jeton de l'endpoint de métriques
# ===========================================================================
def test_the_metrics_token_is_declared():
    assert registry.get("RAG_OPS_METRICS_TOKEN") is not None
    assert hasattr(settings, "RAG_OPS_METRICS_TOKEN")


def test_the_metrics_token_is_declared_sensitive():
    definition = registry.require("RAG_OPS_METRICS_TOKEN")
    assert definition.secret is True
    assert definition.default == "", "aucun secret par défaut"
    assert definition.domain == "observability"


def test_the_metrics_token_never_leaks_through_introspection():
    """Un jeton témoin ne doit ressortir d'aucune surface de restitution."""
    definition = registry.require("RAG_OPS_METRICS_TOKEN")
    sentinelle = "jeton-temoin-ne-doit-pas-fuir"

    assert sentinelle not in definition.display_value(sentinelle)
    assert sentinelle not in definition.default_display()

    metadonnees = definition.as_dict()
    assert metadonnees["secret"] is True
    assert metadonnees["default"] is None, "le défaut d'un secret n'est pas restitué"
    assert sentinelle not in str(metadonnees)


def test_the_generated_reference_shows_no_token_value():
    """La documentation engendrée depuis le registre ne montre aucune valeur."""
    from config.registry import reference

    rendu = reference.render()
    assert "RAG_OPS_METRICS_TOKEN" in rendu, "le réglage doit être documenté"
    assert "(non affiché)" in rendu


# ===========================================================================
# 6. L'angle mort du balayage est refermé
# ===========================================================================
def _scanner():
    """Charge le module voisin PAR CHEMIN, sans supposer que `tests/` soit un paquet.

    L'import `from tests.tests_root...` ne résout que si pytest tourne depuis la
    racine du dépôt ; la suite backend, elle, tourne depuis `backend/`. Charger le
    fichier voisin ne dépend d'aucun `sys.path`.
    """
    import importlib.util

    chemin = pathlib.Path(__file__).with_name("test_no_undeclared_settings.py")
    spec = importlib.util.spec_from_file_location("_scanner_reglages", chemin)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_undeclared_scanner_now_covers_config_modules():
    """`config/urls.py` doit être vu : c'est là que vivait l'angle mort."""
    scanner = _scanner()

    assert "config" in scanner.SCANNED
    noms = scanner._literal_setting_names()
    assert "RAG_OPS_METRICS_TOKEN" in noms
    assert any(f.startswith("config/") for f in noms["RAG_OPS_METRICS_TOKEN"])


def test_the_scanner_still_ignores_generated_and_frozen_code():
    """Étendre la portée ne doit pas produire de faux positifs."""
    scanner = _scanner()

    for exclu in ("migrations", "__pycache__", "settings", "tests"):
        assert exclu in scanner.SKIPPED_PARTS
