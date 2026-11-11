"""Les réglages du résumé de conversation portent enfin le nom qu'ils exercent.

Ce que la cartographie a établi, et que ce fichier verrouille :

- le registre DÉCLARAIT `CHAT_MEMORY_SUMMARY_TRIGGER_MESSAGES` (24),
  `..._MAX_INPUT_CHARS` (12000), `..._TARGET_CHARS` (1400) et `..._ENABLED` (1) ;
- le code LISAIT `CHAT_MEMORY_MIN_MESSAGES_BEFORE_SUMMARY` (18),
  `CHAT_MEMORY_SUMMARIZE_CHUNK_MAX_CHARS` (6000) et
  `CHAT_MEMORY_SUMMARY_MAX_CHARS` (4000), aucun n'étant projeté ;
- `..._ENABLED` n'était lu par personne : le résumé se produisait quoi qu'il arrive ;
- `..._TARGET_CHARS` n'était jamais demandé au modèle.

Point délicat, et c'est l'objet de la première section : les DÉFAUTS déclarés
reprennent les valeurs RÉELLEMENT appliquées (18, 6000), pas celles qui étaient
écrites (24, 12000). Corriger un nom sans corriger la valeur aurait changé le
comportement de production en prétendant ne faire que du rangement.
"""
from __future__ import annotations

import pytest
from django.conf import settings
from django.test import override_settings

from config import registry
from config.settings.access import UnregisteredSettingError, get_registered_setting


# ===========================================================================
# 1. Les défauts déclarés = le comportement observé
# ===========================================================================
@pytest.mark.parametrize(
    "name,attendu,pourquoi",
    [
        ("CHAT_MEMORY_SUMMARY_TRIGGER_MESSAGES", 18, "valeur réellement appliquée"),
        ("CHAT_MEMORY_SUMMARY_MAX_INPUT_CHARS", 6000, "valeur réellement appliquée"),
        ("CHAT_MEMORY_SUMMARY_MAX_CHARS", 4000, "borne du résumé stocké"),
        ("CHAT_MEMORY_SUMMARY_ENABLED", True, "le résumé était toujours actif"),
        ("CHAT_MEMORY_SUMMARY_LLM_TIMEOUT_S", 0.0, "0 = aucun budget propre"),
    ],
)
def test_declared_default_matches_observed_behaviour(name, attendu, pourquoi):
    from config.settings.from_registry import env_value

    assert registry.require(name).default == attendu, pourquoi
    # Environnement VIDE : sans cela le test dépendrait du `.env` du poste, qui
    # porte encore les valeurs jamais appliquées (24, 12000) — précisément les
    # lignes que ce lot demande à l'opérateur de retirer.
    assert env_value(name, environ={}) == attendu
    assert hasattr(settings, name), "le réglage doit être projeté pour être configurable"


def test_the_never_applied_values_are_gone():
    """24 et 12000 n'ont jamais été appliqués : les conserver aurait menti."""
    assert registry.require("CHAT_MEMORY_SUMMARY_TRIGGER_MESSAGES").default != 24
    assert registry.require("CHAT_MEMORY_SUMMARY_MAX_INPUT_CHARS").default != 12000


# ===========================================================================
# 2. Les noms fantômes ne sont plus lisibles
# ===========================================================================
@pytest.mark.parametrize(
    "fantome",
    [
        "CHAT_MEMORY_MIN_MESSAGES_BEFORE_SUMMARY",
        "CHAT_MEMORY_SUMMARIZE_CHUNK_MAX_CHARS",
        "CHAT_MEMORY_SUMMARY_TARGET_CHARS",
    ],
)
def test_ghost_names_are_refused(fantome):
    with pytest.raises(UnregisteredSettingError):
        get_registered_setting(fantome)
    assert not hasattr(settings, fantome)


def test_no_alias_was_created_for_the_ghost_names():
    """Aucun de ces noms n'a jamais été projeté : aucune configuration réelle ne
    peut en dépendre, donc un alias de compatibilité n'aurait rien à préserver."""
    anciens = {a.old_name for a in registry.DEPRECATED_ALIASES}
    assert "CHAT_MEMORY_MIN_MESSAGES_BEFORE_SUMMARY" not in anciens
    assert "CHAT_MEMORY_SUMMARIZE_CHUNK_MAX_CHARS" not in anciens


# ===========================================================================
# 3. Chaque réglage est lu par le code
# ===========================================================================
@pytest.mark.parametrize(
    "name",
    [
        "CHAT_MEMORY_MAX_MESSAGES",
        "CHAT_MEMORY_MAX_CHARS",
        "CHAT_MEMORY_SUMMARY_ENABLED",
        "CHAT_MEMORY_SUMMARY_TRIGGER_MESSAGES",
        "CHAT_MEMORY_SUMMARY_MAX_INPUT_CHARS",
        "CHAT_MEMORY_SUMMARY_MAX_CHARS",
        "CHAT_MEMORY_SUMMARY_LLM_TIMEOUT_S",
    ],
)
def test_every_declared_name_is_read_by_the_code(name):
    import inspect

    from apps.chat.services import chat_threads

    source = inspect.getsource(chat_threads)
    assert f'_s("{name}")' in source, (
        f"{name} est déclarée mais aucun code ne la lit — c'est exactement le "
        "défaut que ce lot corrige"
    )


def test_the_reader_refuses_an_undeclared_name():
    from apps.chat.services import chat_threads

    with pytest.raises(UnregisteredSettingError):
        chat_threads._s("CHAT_MEMORY_CE_NOM_N_EXISTE_PAS")


# ===========================================================================
# 4. L'interrupteur coupe réellement
# ===========================================================================
@pytest.mark.django_db
@override_settings(CHAT_MEMORY_SUMMARY_ENABLED=False)
def test_disabling_the_summary_stops_the_generation(monkeypatch):
    """Le garde manquait : le résumé se produisait même à 0."""
    from apps.chat.services import chat_threads

    appels = {"n": 0}

    def _ne_doit_pas_etre_appele(**kwargs):  # pragma: no cover
        appels["n"] += 1
        raise AssertionError("aucune génération ne doit avoir lieu quand le résumé est coupé")

    monkeypatch.setattr(
        "apps.llm_router.router.generate_text", _ne_doit_pas_etre_appele, raising=False
    )

    class _FilFactice:
        id = None
        memory_summary = ""
        memory_summary_until_message_id = None
        rag_space = None

    chat_threads.maybe_update_thread_summary(thread=_FilFactice())
    assert appels["n"] == 0


# ===========================================================================
# 5. L'environnement pilote effectivement la valeur
# ===========================================================================
@pytest.mark.parametrize(
    "name,raw,attendu",
    [
        ("CHAT_MEMORY_SUMMARY_ENABLED", "0", False),
        ("CHAT_MEMORY_SUMMARY_TRIGGER_MESSAGES", "40", 40),
        ("CHAT_MEMORY_SUMMARY_MAX_INPUT_CHARS", "9000", 9000),
        ("CHAT_MEMORY_SUMMARY_MAX_CHARS", "2000", 2000),
        ("CHAT_MEMORY_SUMMARY_LLM_TIMEOUT_S", "45", 45.0),
    ],
)
def test_environment_drives_the_effective_value(name, raw, attendu):
    from config.settings.from_registry import env_value

    assert env_value(name, environ={name: raw}) == attendu


def test_the_timeout_posed_in_production_would_now_apply():
    """`CHAT_MEMORY_SUMMARY_LLM_TIMEOUT_S=45` est posée en production depuis
    longtemps sans effet. Elle en aura un après ce lot : c'est une action
    opérateur à connaître, pas une surprise à découvrir."""
    from config.settings.from_registry import env_value

    assert env_value(
        "CHAT_MEMORY_SUMMARY_LLM_TIMEOUT_S",
        environ={"CHAT_MEMORY_SUMMARY_LLM_TIMEOUT_S": "45"},
    ) == 45.0
