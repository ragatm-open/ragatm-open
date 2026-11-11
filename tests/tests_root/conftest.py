"""Conftest racine de la suite backend (tests/backend).

Particularités vs backend/tests :
- le code applicatif Django est dans ../../backend, qu'on force en tête du sys.path ;
- on force EMBEDDINGS_PROVIDER=stub AVANT tout import (le .env du repo pointe sur un
  vrai provider Ollama, indisponible en CI/local sans serveur). Le stub est
  déterministe (seed = sha256 du texte), donc la recherche reste reproductible.
"""
import os
import sys
from pathlib import Path

# 1) backend/ en tête du sys.path (idem backend/conftest.py)
BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
backend_str = str(BACKEND_DIR)
if not sys.path or sys.path[0] != backend_str:
    if backend_str in sys.path:
        sys.path.remove(backend_str)
    sys.path.insert(0, backend_str)

# 2) éjecter un éventuel 'api'/'config' importé depuis ailleurs
for _modname in ("api", "config"):
    _mod = sys.modules.get(_modname)
    if _mod is not None:
        _f = getattr(_mod, "__file__", "") or ""
        if not _f.startswith(backend_str):
            del sys.modules[_modname]

# 3) embeddings déterministes par défaut pour toute la suite
os.environ.setdefault("EMBEDDINGS_PROVIDER", "stub")
# Édition Open : la suite ne dépend d'aucun `.env` local. Les tests qui rechargent
# `config.settings.prod` exigent une clé différente du repli de développement.
os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-secret-key-not-for-deployment-0123456789")
# on écrase même si une valeur non-stub traîne (ex: chargée via .env)
if os.environ.get("EMBEDDINGS_PROVIDER", "stub").lower() != "stub":
    os.environ["EMBEDDINGS_PROVIDER"] = "stub"

# 4) Routage LLM déterministe pour toute la suite. Le .env dev impose désormais la
#    policy (AI Gateway ON, provider openai), mais les tests unitaires
#    exercent sans réseau les chemins direct/fallback. On force donc la Gateway OFF
#    par défaut (les tests qui la veulent ON le posent via la fixture `settings`) et
#    on active l'échappatoire de tests pour que les system checks restent verts.
#    load_dotenv() étant appelé avec override=False, ces valeurs ne sont PAS écrasées.
os.environ["AI_GATEWAY_ENABLED"] = "0"
os.environ["RAG_LLM_PROVIDER"] = "ollama"
os.environ.setdefault("ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS", "1")

# 5) Fonctionnalités OPT-IN forcées OFF pour toute la suite.
#    Ces flags valent 0 par défaut dans base.py, mais un `.env` de dev peut les poser
#    à 1 : la suite doit rester déterministe quel que soit l'environnement. Un test qui
#    veut l'un d'eux ON l'active explicitement (fixture `settings` / override_settings).
#    Ce forçage vivait dans `backend/conftest.py`, qui ne s'applique PAS à cet arbre
#    (backend/ n'est pas un ancêtre de tests/tests_root/) : il a été perdu lors du
#    déplacement des paquets legacy vers l'arbre canonique, et n'était visible qu'avec
#    un `.env` posant ces flags à 1 — jamais en CI.
os.environ["ACTIONS_ROUTER_ENABLED"] = "0"
os.environ["ACTIONS_LLM_CLASSIFIER_ENABLED"] = "0"
os.environ["RAG_REPHRASING_SUGGESTIONS_ENABLED"] = "0"
#    Dispositif de feedback/incidents : mêmes raisons, même traitement. Un poste
#    de développement configuré pour EXPLOITER des incidents (buffer, export et
#    import à 1, cf. docs/03-backend/feedback-incidents.md) rendrait sinon la
#    suite dépendante de son `.env` — constaté : `test_import_is_disabled_by_default`
#    échouait parce que l'import était activé localement. Les tests qui veulent
#    l'un de ces drapeaux ON le posent explicitement (`override_settings`).
os.environ["RAG_FEEDBACK_DIAGNOSTIC_BUFFER_ENABLED"] = "0"
os.environ["RAG_FEEDBACK_DIAGNOSTIC_CAPTURE_PROMPT"] = "0"
os.environ["RAG_FEEDBACK_DIAGNOSTIC_REPLAY_ENABLED"] = "0"
os.environ["FEEDBACK_INCIDENT_EXPORT_ENABLED"] = "0"
os.environ["FEEDBACK_INCIDENT_IMPORT_ENABLED"] = "0"

import pytest  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402


@pytest.fixture(autouse=True, scope="session")
def _cache_cloisonne_par_worker():
    """Un espace de clés de cache PROPRE à chaque worker pytest-xdist.

    Pourquoi cette fixture existe
    -----------------------------
    En test, `DJANGO_CACHE_URL` pointe sur un vrai Redis (`127.0.0.1:63790/2`), et
    les alias `default` et `ops` y écrivent SANS préfixe. Sous `-n 4`, les quatre
    workers partagent donc les mêmes clés `ops:counter:*`. Or les tests de compteurs
    remettent l'état à zéro autour de chaque cas (`RG.reset_counters()`) : le reset
    d'un worker efface ce qu'un autre vient d'incrémenter, en plein milieu de son
    assertion.

    Le symptôme est reconnaissable : la suite est verte en série (42/42) et rouge
    sous `-n 4`, avec des tests qui **changent de nom d'une exécution à l'autre** —
    `test_nominal`, `test_gateway_injoignable`, `test_totaux_sur_appels_mixtes`… Ce
    n'est pas de l'aléa d'horloge, c'est un état partagé non cloisonné.

    Ce qui n'est PAS corrigé ici, et pourquoi
    -----------------------------------------
    Le partage lui-même est **voulu en production** : le backend tourne sous quatre
    workers gunicorn qui doivent additionner leurs compteurs, sinon un exploitant
    lirait le quart de la réalité (cf. `services/ops_counters.py`). Et les
    déploiements ne se mélangent pas — staging et production ont chacun leur
    instance Redis, vérifié le 05/09/2026. Le code produit n'a donc aucune raison de
    changer : c'est la suite de tests qui manquait d'isolation, et c'est elle qu'on
    corrige.

    `KEY_PREFIX` plutôt qu'une base Redis par worker : le nombre de workers est
    variable (`PYTEST_WORKERS`), or Redis n'offre que seize bases. Un préfixe n'a pas
    de plafond, et il cloisonne TOUT le cache — compteurs d'exploitation, contrôle de
    débit DRF, catalogue de modèles — pas seulement ce qui a rougi aujourd'hui.
    """
    from django.conf import settings
    from django.test import override_settings

    from tests_root.cache_isolation import caches_cloisonnes

    if not (settings.CACHES or {}):
        yield
        return
    # `override_settings` émet `setting_changed`, que Django écoute pour reconstruire
    # le gestionnaire de caches : les backends sont donc recréés avec le nouveau
    # préfixe, sans qu'on ait à toucher aux internes de `django.core.cache`.
    with override_settings(CACHES=caches_cloisonnes(settings.CACHES)):
        yield


@pytest.fixture(autouse=True, scope="session")
def _clear_ne_vide_que_ce_worker():
    """``cache.clear()`` ne doit pas emporter les clés des autres workers.

    ``django_redis`` implémente ``clear()`` par un **FLUSHDB** : il vide la base
    entière, `KEY_PREFIX` compris. La fixture ci-dessus ne protège donc de rien
    contre lui, et une quinzaine de tests de la suite appellent ``cache.clear()``
    en toute légitimité — pour repartir d'un cache propre, pas pour effacer celui
    du voisin.

    Effet observé avant ce correctif : un worker perdait ses compteurs
    d'exploitation **au milieu d'une assertion**, sans erreur ni journal, parce
    qu'un autre worker venait de vider la base. Le test lisait alors zéro et
    l'échec paraissait aléatoire (puis qui l'a démontré).

    On restreint donc l'effacement au préfixe du worker. Le code produit n'est pas
    touché : en production, ``clear()`` garde son sens de purge complète.
    """
    from django_redis.cache import RedisCache

    from tests_root.cache_isolation import clear_cloisonne

    origine = RedisCache.clear
    RedisCache.clear = clear_cloisonne
    try:
        yield
    finally:
        RedisCache.clear = origine


@pytest.fixture(autouse=True)
def _reset_gateway_pipeline_reference():
    """La référence d'identités de pipeline (`/health/embeddings`) est mise en cache
    PAR PROCESSUS : un test qui la lit laisserait au suivant une référence « valide »
    venue d'ailleurs. Remise à zéro avant et après chaque cas."""
    from apps.llm_router import embedding_reference as _ref

    _ref.reset_reference_cache()
    _ref.reset_counters()
    yield
    _ref.reset_reference_cache()


@pytest.fixture(autouse=True)
def _force_stub_embeddings(settings):
    """Baseline déterministe pour toute la suite.

    - EMBEDDINGS_PROVIDER=stub (recherche reproductible sans provider réel) ;
    - AI Gateway OFF + RAG_LLM_PROVIDER=ollama : le .env dev impose désormais la
      policy (Gateway ON, provider openai), mais les tests unitaires exercent sans
      réseau les chemins direct/fallback. Les tests qui veulent la Gateway ON la
      posent explicitement via la fixture `settings` (leur valeur écrase ce baseline).
    - ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS=1 : garde les system checks verts.
    - fonctionnalités opt-in (routeur d'actions, classifieur LLM, reformulation)
      forcées OFF : réassertion runtime du forçage d'environnement ci-dessus, pour
      qu'un `.env` posant ces flags à 1 ne rende pas la suite non déterministe.
    - PASSWORD_HASHERS = MD5, uniquement en test (voir plus bas).
    """
    # ------------------------------------------------------------------
    # Hachage de mot de passe — MD5 UNIQUEMENT DANS LES TESTS
    # ------------------------------------------------------------------
    # Le hasher MD5 est utilisé uniquement dans les tests afin d'éviter le coût
    # de PBKDF2 lors des nombreuses créations d'utilisateurs.
    #
    # Les tests vérifiant explicitement le comportement ou le format du hash
    # doivent restaurer localement le hasher de production.
    #
    # Ce n'est pas un réglage de confort, c'est le premier poste de dépense de la
    # suite. Le défaut Django 5.2 est PBKDF2 à 1 000 000 d'itérations, soit ~349 ms
    # par `create_user()` ; le dépôt en compte 219 sites d'appel, dont 147 dans
    # `http_api/`. Mesuré : `http_api/` seul passe de 541 s à 67 s (-88 %), et la
    # suite standard de 1 190 s à 499 s (-58 %), à couverture strictement
    # identique. Quatre modules d'`integration/` posaient déjà cette surcharge
    # localement, avec le même constat ; elle est ici généralisée et eux ont été
    # nettoyés, pour qu'il n'y ait qu'une source de vérité.
    #
    # AUCUN effet hors test : ce forçage vit dans une fixture de la suite, jamais
    # dans `config/settings/`. La production continue d'utiliser le hasher par
    # défaut de Django, et `accounts/test_ensure_superuser.py` le vérifie en
    # restaurant explicitement PBKDF2 (`override_settings`).
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

    settings.EMBEDDINGS_PROVIDER = "stub"
    settings.AI_GATEWAY_ENABLED = False
    settings.RAG_LLM_PROVIDER = "ollama"
    settings.ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS = True
    settings.ACTIONS_ROUTER_ENABLED = False
    settings.ACTIONS_LLM_CLASSIFIER_ENABLED = False
    settings.RAG_REPHRASING_SUGGESTIONS_ENABLED = False
    # Dispositif de feedback : tout OFF par défaut, comme en CI.
    settings.RAG_FEEDBACK_DIAGNOSTIC_BUFFER_ENABLED = False
    settings.RAG_FEEDBACK_DIAGNOSTIC_CAPTURE_PROMPT = False
    settings.RAG_FEEDBACK_DIAGNOSTIC_REPLAY_ENABLED = False
    settings.FEEDBACK_INCIDENT_EXPORT_ENABLED = False
    settings.FEEDBACK_INCIDENT_IMPORT_ENABLED = False
    # Ingestion/tâches SYNCHRONES par défaut, comme la CI (env CELERY_TASK_ALWAYS_EAGER=1).
    # La CI posait ce flag par l'environnement ; le baseline ne répliquait que
    # `EMBEDDINGS_PROVIDER=stub` et pas l'eager → une invocation `pytest` nue (sans
    # l'env) faisait basculer l'upload en asynchrone (réponse `{queued, document_id}`
    # sans `document`, document non `READY`) et échouait 9 tests Excel/PPTX. On aligne
    # donc le baseline sur la CI. Les tests qui exercent le chemin ASYNCHRONE posent
    # explicitement `CELERY_TASK_ALWAYS_EAGER=False` (leur valeur écrase ce baseline).
    settings.CELERY_TASK_ALWAYS_EAGER = True
    return settings


# `user` provient désormais du fournisseur canonique partagé par les deux arbres
# (backend/testing/fixtures.py) : utilisateur actif minimal, SANS email.
from testing.fixtures import user  # noqa: E402,F401


@pytest.fixture
def user_with_email(user):
    """Variante pour les tests qui se connectent réellement (login par email).

    L'email n'est pas une garantie partagée : seuls les tests d'authentification et
    ceux qui l'affichent en dépendent. Le demander explicitement évite que 600 tests
    héritent d'une donnée qu'ils n'utilisent pas — et rend la dépendance visible là
    où elle compte.
    """
    user.email = "u1@example.com"
    user.save(update_fields=["email"])
    return user


@pytest.fixture
def other_user(db):
    User = get_user_model()
    return User.objects.create_user(username="u2", password="pw", email="u2@example.com")




# `member` et `api_client` : fournisseurs canoniques partagés
# (cf. backend/testing/fixtures.py). Exposés depuis le conftest RACINE pour couvrir
# aussi `integration/`, qui n'a pas de conftest propre.
from testing.fixtures import api_client, member, workspace  # noqa: E402,F401


# `rag_space` : fournisseur canonique partagé (cf. backend/testing/fixtures.py).
from testing.fixtures import rag_space  # noqa: E402,F401


@pytest.fixture(autouse=True)
def _model_catalog_inert(monkeypatch):
    """Le catalogue de modèles est INERTE par défaut dans la suite.

    Sans cela, la validation de catalogue interrogerait la passerelle IA réelle à
    chaque création/modification d'espace : la suite dépendrait du réseau, et son
    résultat de la disponibilité d'une machine tierce. Constaté en écrivant ce lot —
    `test_su_create_with_other_model_accepted` a commencé à échouer parce que la
    passerelle de développement était joignable et refusait `llama3.1:8b`.

    L'état simulé est « aucune passerelle déclarée », pas « passerelle en panne » : la
    gouvernance ne s'applique pas, et les tests antérieurs conservent leur contrat.
    Les tests QUI portent sur le catalogue neutralisent cette fixture en injectant
    leur propre catalogue (cf. `catalog_models` dans les tests dédiés).
    """
    from apps.llm_router import model_catalog

    model_catalog.invalidate_catalog_cache()
    monkeypatch.setattr(model_catalog, "catalog_applicable", lambda: False)
    monkeypatch.setattr(model_catalog, "gateway_models", lambda **kw: (None, False))
    monkeypatch.setattr(model_catalog, "gateway_runtime_models", lambda **kw: (None, False))
    yield
    model_catalog.invalidate_catalog_cache()


@pytest.fixture
def ingest_text():
    """Helper d'ingestion synchrone (embeddings stub)."""
    from apps.rag_core.services.ingest_facade import ingest_plain_text

    def _ingest(rag_space, text, filename="doc.txt"):
        return ingest_plain_text(rag_space_id=rag_space.id, filename=filename, text=text)

    return _ingest


# ---------------------------------------------------------------------------
# Registre de TEST — clé synthétique d'overlay
# ---------------------------------------------------------------------------
# La suite Admin Core a besoin d'une clé éditable jusqu'au **ragspace** pour
# prouver la précédence `ragspace > workspace > environment > global`. Aucun
# réglage de production n'a cette portée, et lui en donner une ouvrirait un
# levier runtime que personne n'a décidé d'exposer.
#
# La clé est donc SYNTHÉTIQUE : elle n'existe ni dans le registre canonique, ni
# dans `EXPOSED_KEYS`, ni dans aucune réponse d'API. Elle est injectée ici, pour
# la durée d'un test, puis retirée. `tests/tests_root/unit/config/
# test_synthetic_key_absent_from_production.py` verrouille cette absence.
SYNTHETIC_CORE_KEY = "CORE_CONFIG_SELFTEST_SCOPE"


@pytest.fixture
def synthetic_core_key(monkeypatch):
    """Injecte la clé de banc d'essai dans le registre ET dans le résolveur Core.

    Les deux sont nécessaires : le registre porte le type et le défaut, le
    résolveur porte la portée maximale et l'exposition Admin Core. Le
    `monkeypatch` restaure l'état d'origine à la fin du test — la clé ne peut pas
    fuir vers un autre test, encore moins vers un environnement.
    """
    from config import registry as _registry
    from config.registry.definition import ConfigDefinition
    from apps.rag_core.core import config_resolver as _resolver

    definition = ConfigDefinition(
        name=SYNTHETIC_CORE_KEY,
        domain="core",
        value_type="bool",
        default=False,
        description=(
            "Clé SYNTHÉTIQUE de test. Aucun consommateur applicatif, aucun effet. "
            "Elle n'existe que pendant l'exécution d'un test d'overlay."
        ),
        runtime_editable=True,
        max_scope="ragspace",
        restart_required=False,
        environments=("test",),
        consumers=("tests",),
    )

    monkeypatch.setitem(_registry._BY_NAME, SYNTHETIC_CORE_KEY, definition)
    monkeypatch.setattr(
        _registry, "DEFINITIONS", _registry.DEFINITIONS + (definition,), raising=True
    )

    spec = _resolver._spec_from_registry(SYNTHETIC_CORE_KEY)
    monkeypatch.setattr(_resolver, "EXPOSED_KEYS", (SYNTHETIC_CORE_KEY, *_resolver.EXPOSED_KEYS))
    monkeypatch.setattr(_resolver, "_REGISTRY", [spec, *_resolver._REGISTRY])
    monkeypatch.setitem(_resolver._SPECS, SYNTHETIC_CORE_KEY, spec)
    return SYNTHETIC_CORE_KEY


# =============================================================================
# Profil FAST : `--no-db`
# =============================================================================
# Désélectionne, à la collecte, tout test qui demande la base de données : marqueur
# `django_db` (y compris via `pytestmark`) ou fixture `db` / `transactional_db` /
# `django_db_setup` / `live_server`. Le profil FAST tourne ainsi sans PostgreSQL ni
# Redis. C'est une DÉSÉLECTION, jamais une exécution dégradée : un test qui a besoin
# de la base n'est pas lancé sans elle. Les tests désélectionnés restent dans les
# profils STANDARD et FULL (cf. Makefile et docs/08-testing/strategie.md).
_DB_FIXTURES = frozenset(
    {"db", "transactional_db", "django_db_setup", "django_db_reset_sequences",
     "django_db_serialized_rollback", "live_server"}
)


def pytest_addoption(parser):
    parser.addoption(
        "--no-db",
        action="store_true",
        default=False,
        help="Profil FAST : désélectionne les tests qui demandent la base de données.",
    )


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--no-db"):
        return
    kept, dropped = [], []
    for item in items:
        needs_db = item.get_closest_marker("django_db") is not None or bool(
            _DB_FIXTURES & set(getattr(item, "fixturenames", ()))
        )
        (dropped if needs_db else kept).append(item)
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = kept
