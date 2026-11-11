# backend/tests/conftest.py
import os
import sys
from pathlib import Path

# Routage LLM déterministe (arbre legacy) : la Gateway est OFF par défaut dans les
# tests (les tests qui la veulent ON la posent via `settings`), et l'échappatoire de
# tests garde les system checks verts. load_dotenv(override=False) ne les écrase pas.
os.environ["AI_GATEWAY_ENABLED"] = "0"
os.environ.setdefault("RAG_LLM_PROVIDER", "ollama")
os.environ.setdefault("ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS", "1")

# Fonctionnalités OPT-IN désactivées par défaut dans les tests, indépendamment du
# `.env` DEV du développeur (qui peut les activer localement). Un test qui veut les
# activer le fait explicitement via `settings`/fixture (sa valeur écrase ce baseline).
# NB : posé AVANT le chargement des settings ; load_dotenv(override=False) ne les
# écrase pas. Le baseline runtime (fixture ci-dessous) est la garantie robuste.
os.environ["ACTIONS_ROUTER_ENABLED"] = "0"
os.environ["ACTIONS_LLM_CLASSIFIER_ENABLED"] = "0"
os.environ["RAG_REPHRASING_SUGGESTIONS_ENABLED"] = "0"

BACKEND_DIR = Path(__file__).resolve().parents[1]  # .../backend

# 1) on force backend/ en premier dans le sys.path
backend_str = str(BACKEND_DIR)
if sys.path[0] != backend_str:
    if backend_str in sys.path:
        sys.path.remove(backend_str)
    sys.path.insert(0, backend_str)

# 2) si un 'api' a déjà été importé et ne vient pas de backend/, on l’éjecte
mod = sys.modules.get("api")
if mod is not None:
    mod_file = getattr(mod, "__file__", "") or ""
    if not mod_file.startswith(backend_str):
        del sys.modules["api"]


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _deterministic_llm_baseline(settings):
    """Baseline LLM déterministe (arbre legacy) : AI Gateway OFF + provider ollama.

    Un `.env` de développement impose la policy (Gateway ON) ; les tests exercent
    sans réseau le chemin direct et l'erreur contrôlée (aucun repli en édition Open). Les tests qui veulent la Gateway ON la
    posent via `settings` (leur valeur écrase ce baseline).
    """
    settings.AI_GATEWAY_ENABLED = False
    settings.RAG_LLM_PROVIDER = "ollama"
    settings.ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS = True
    # Fonctionnalités opt-in OFF par défaut (le .env dev peut les activer localement).
    # Un test qui les veut ON les pose via `settings`/fixture -> sa valeur écrase ceci
    # (les fixtures autouse sont instanciées AVANT les fixtures explicites du test).
    settings.ACTIONS_ROUTER_ENABLED = False
    settings.ACTIONS_LLM_CLASSIFIER_ENABLED = False
    settings.RAG_REPHRASING_SUGGESTIONS_ENABLED = False
    return settings
