"""Cloisonnement du cache entre workers xdist — la garde lors de la qualification.

Deux mécanismes, deux corrections, deux familles de tests ici :

* le **préfixe de clés** — qu'une reconstruction de ``settings.CACHES``
  ne doit plus pouvoir effacer ;
* l'**effacement** — ``cache.clear()`` de ``django_redis`` émet un ``FLUSHDB`` et
  vide la base ENTIÈRE, préfixe compris. C'est la cause démontrée de l'échec
  intermittent des compteurs d'exploitation observé lors de la qualification.

Le second point est le plus important : sans lui, le préfixe donne une illusion
d'isolation. ``TestUnClearNEfface QueSonWorker`` échoue si la correction saute.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from django.conf import settings
from django.core.cache import caches
from django.test import override_settings

from tests_root.cache_isolation import (
    ALIAS_ATTENDUS,
    caches_cloisonnes,
    prefixe_de_worker,
)

RACINE_TESTS = Path(__file__).resolve().parents[2]


def _redis_actif(alias: str = "default") -> bool:
    backend = (settings.CACHES or {}).get(alias, {}).get("BACKEND", "")
    return "redis" in backend.lower()


def _caches_pour(prefixe: str) -> dict:
    conf = caches_cloisonnes(settings.CACHES)
    for alias in conf:
        conf[alias]["KEY_PREFIX"] = prefixe
    return conf


# --------------------------------------------------------------------------- #
# Le préfixe survit à une reconstruction
# --------------------------------------------------------------------------- #
class TestLePrefixeSurvitAUneReconstruction:
    def test_le_prefixe_porte_le_nom_du_worker(self):
        assert prefixe_de_worker().startswith("test-")

    def test_tous_les_alias_recoivent_le_prefixe(self):
        conf = caches_cloisonnes(settings.CACHES)
        assert conf, "la suite doit configurer au moins un cache"
        for alias, reglages in conf.items():
            assert reglages.get("KEY_PREFIX") == prefixe_de_worker(), alias

    def test_les_alias_attendus_ne_disparaissent_pas(self):
        conf = caches_cloisonnes(settings.CACHES)
        for alias in ALIAS_ATTENDUS:
            assert alias in conf, f"l'alias {alias} a disparu de la reconstruction"

    def test_la_source_n_est_pas_modifiee_en_place(self):
        """Une copie profonde : poser le préfixe sur le dictionnaire d'origine
        fuiterait vers tous les tests suivants."""
        avant = (settings.CACHES or {}).get("default", {}).get("KEY_PREFIX")
        conf = caches_cloisonnes(settings.CACHES)
        conf["default"]["KEY_PREFIX"] = "autre-chose"
        assert (settings.CACHES or {}).get("default", {}).get("KEY_PREFIX") == avant

    def test_une_configuration_vide_reste_vide(self):
        assert caches_cloisonnes(None) == {}
        assert caches_cloisonnes({}) == {}


# --------------------------------------------------------------------------- #
# LE test qui échoue sans le correctif
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not _redis_actif(), reason="cache local : pas de partage entre workers")
class TestUnClearNEffaceQueSonWorker:
    """``clear()`` d'origine émet ``FLUSHDB`` : il emporte les clés des autres.

    Sans ``clear_cloisonne``, ces trois tests échouent — la valeur écrite par
    « gwA » revient à ``None`` après un ``clear()`` fait sous « gwB ».
    """

    @pytest.mark.parametrize("alias", ALIAS_ATTENDUS)
    def test_le_clear_d_un_autre_worker_epargne_mes_cles(self, alias):
        with override_settings(CACHES=_caches_pour("test-lot10a-A")):
            caches[alias].set("temoin", 7, None)
            assert caches[alias].get("temoin") == 7

        with override_settings(CACHES=_caches_pour("test-lot10a-B")):
            caches[alias].clear()

        with override_settings(CACHES=_caches_pour("test-lot10a-A")):
            survivant = caches[alias].get("temoin")
            caches[alias].delete("temoin")
        assert survivant == 7, (
            f"alias {alias} : un clear() fait sous un autre préfixe a effacé cette clé — "
            "django_redis.clear() émet FLUSHDB, que KEY_PREFIX ne borne pas"
        )

    def test_le_clear_efface_bien_mes_propres_cles(self):
        """La correction ne doit pas transformer ``clear()`` en opération vide."""
        with override_settings(CACHES=_caches_pour("test-lot10a-C")):
            caches["default"].set("temoin", 1, None)
            caches["default"].clear()
            assert caches["default"].get("temoin") is None


# --------------------------------------------------------------------------- #
# Les deux alias, séparément
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not _redis_actif(), reason="cache local : pas de partage entre workers")
class TestLesDeuxAliasSontIsoles:
    def test_les_deux_alias_visent_le_meme_magasin_par_conception(self):
        """`default` et `ops` partagent l'URL Redis — ils diffèrent par le POOL.

        Ce n'est pas un défaut : l'alias `ops` existe pour porter un délai de
        socket court et sa propre tolérance d'erreur, pas pour cloisonner des
        clés (cf. `test_redis_cache_pool_isolation`). Le cloisonnement qui compte
        ici est celui entre WORKERS, vérifié au-dessus pour les deux alias.
        """
        conf = caches_cloisonnes(settings.CACHES)
        assert conf["default"]["LOCATION"] == conf["ops"]["LOCATION"]
        assert conf["default"]["KEY_PREFIX"] == conf["ops"]["KEY_PREFIX"] == prefixe_de_worker()

    def test_le_repli_ops_vers_default_reste_correct(self):
        """``ops_counters`` retombe sur ``default`` quand l'alias ``ops`` manque.

        Ce repli est VOULU (anciens réglages, environnements minimaux). Ce qui ne
        l'est pas, c'est qu'un test le déclenche sans le vouloir en reconstruisant
        ``CACHES`` : il croirait alors mesurer l'alias dédié.
        """
        from apps.rag_core.services import ops_counters

        sans_ops = {"default": dict((settings.CACHES or {})["default"])}
        with override_settings(CACHES=caches_cloisonnes(sans_ops)):
            assert ops_counters._alias() == "default"
        with override_settings(CACHES=caches_cloisonnes(settings.CACHES)):
            assert ops_counters._alias() == "ops"


# --------------------------------------------------------------------------- #
# Garde structurelle : aucune reconstruction ne doit contourner la primitive
# --------------------------------------------------------------------------- #
def _reconstructions_de_caches() -> list[tuple[str, int, bool]]:
    """(fichier, ligne, passe_par_la_primitive) pour chaque `CACHES = ...`."""
    trouvees: list[tuple[str, int, bool]] = []
    for chemin in sorted(RACINE_TESTS.rglob("test_*.py")):
        arbre = ast.parse(chemin.read_text(encoding="utf-8"))
        for noeud in ast.walk(arbre):
            if not isinstance(noeud, ast.Assign):
                continue
            for cible in noeud.targets:
                if isinstance(cible, ast.Attribute) and cible.attr == "CACHES":
                    appel = noeud.value
                    passe = (
                        isinstance(appel, ast.Call)
                        and isinstance(appel.func, ast.Name)
                        and appel.func.id in {"caches_cloisonnes", "_caches_de_test"}
                    )
                    trouvees.append((
                        str(chemin.relative_to(RACINE_TESTS)), noeud.lineno, passe,
                    ))
    return trouvees


def test_toute_reconstruction_de_caches_passe_par_la_primitive():
    """Empêche la réapparition du défaut par un nouveau test distrait.

    Une reconstruction directe de ``settings.CACHES`` efface le ``KEY_PREFIX`` du
    worker sans que rien ne le signale : c'est exactement ainsi que les deux
    chemins corrigés ici étaient passés inaperçus.
    """
    fautives = [(f, l) for f, l, ok in _reconstructions_de_caches() if not ok]
    assert not fautives, (
        "ces reconstructions de settings.CACHES perdent l'isolation xdist ; "
        f"utiliser caches_cloisonnes() : {fautives}"
    )


def test_la_garde_voit_bien_les_reconstructions_existantes():
    """Une garde qui n'inspecte rien passerait toujours."""
    assert len(_reconstructions_de_caches()) >= 4
