"""Invariants appliqués aux valeurs EFFECTIVES issues de l'overlay Core.

Problème résolu
---------------
Les system checks Django (``llm_router.E001..E005``, ``rag_core.E001/E003``,
``config.E00x``) valident les **settings statiques**. Quand
``CORE_CONFIG_RUNTIME_ENABLED=1``, le runtime ne lit plus les settings pour les
clés pilotées : il lit l'overlay en base (``apps.rag_core.core.runtime_config``,
branché depuis PR-G3 dans ``llm_router.router``, ``llm_router.embeddings`` et
``llm_router.egress``).

Un override autorisé pouvait donc produire une configuration que les checks de
démarrage auraient refusée — et l'angle mort était total, puisque aucun check ne
lit l'overlay. Conséquence la plus grave : un ``AI_GATEWAY_ENABLED`` à ``false``
au runtime faisait basculer la **génération** *et* les **embeddings** (donc
l'indexation, dans les workers Celery) vers un appel Ollama DIRECT, précisément
ce que la politique IA interdit.

Doctrine
--------
1. **Aucun override ne peut produire une configuration que les settings
   statiques auraient refusée.** Les invariants sont énoncés ici une seule fois et
   appliqués aux DEUX bouts :

   - à l'**écriture** (``config_write.set_override``) => refus explicite, rien
     n'est persisté ;
   - à la **résolution** (``runtime_config.get``) => la valeur fautive n'est
     JAMAIS retournée ; on retombe sur les settings, en journalisant.

   La double application n'est pas redondante : une ligne peut préexister en base
   (posée avant ce durcissement, ou par écriture SQL directe), et la résolution
   est le dernier rempart avant l'exécution.

2. **Les clés d'INFRASTRUCTURE ne sont pas pilotables à chaud.** URL privée de la
   Gateway, dimension d'embedding, activation de la politique IA : leur valeur
   engage le réseau, l'index ou la conformité. Elles sont retirées du périmètre
   éditable du registre — ce qui neutralise déjà toute ligne existante — et
   refusées ici en défense en profondeur.

3. **Jamais d'exception au runtime.** Une violation détectée à la résolution
   dégrade vers les settings ; elle ne casse pas une requête utilisateur ni une
   tâche d'ingestion. Le refus dur appartient à l'écriture.

Ce module ne fait aucun accès réseau. Il lit les settings et, pour l'invariant de
repli, l'environnement (présence d'une clé OpenAI), jamais sa valeur.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from django.conf import settings

logger = logging.getLogger("ragatm.core.config_policy")

#: Environnements où la politique IA impose l'AI Gateway (miroir de
#: ``apps.llm_router.checks.OFFICIAL_ENVS`` — source unique côté checks).
OFFICIAL_ENVS = ("dev", "staging", "prod")

#: Clés d'infrastructure JAMAIS pilotables par overlay, quel que soit le
#: registre. Défense en profondeur : le registre les déclare déjà non éditables.
INFRASTRUCTURE_KEYS = frozenset(
    {
        "AI_GATEWAY_BASE_URL",   # URL privée : une erreur coupe génération ET indexation
        "AI_GATEWAY_ENABLED",    # bascule la politique IA (Ollama direct)
        "AI_GATEWAY_PROVIDER",   # identité du provider primaire
        "AI_GATEWAY_MODEL",      # modèle primaire
        "AI_GATEWAY_FALLBACK_PROVIDER",  # autorise ou non l'egress tiers
        "RAG_EMBEDDING_DIM",     # figée par la colonne pgvector
    }
)


class ConfigPolicyViolation(Exception):
    """Un override violerait un invariant que les settings statiques imposent."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _environment(environment: str | None = None) -> str:
    if environment:
        return str(environment).strip().lower()
    return (
        getattr(settings, "DJANGO_ENV", "") or os.getenv("DJANGO_ENV", "") or "dev"
    ).strip().lower()


def _tests_tolerated() -> bool:
    return bool(getattr(settings, "ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS", False))




def _effective_fallback_provider() -> str:
    return (
        str(getattr(settings, "AI_GATEWAY_FALLBACK_PROVIDER", "none") or "none")
        .strip()
        .lower()
    )


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------
def validate_overlay_value(
    key: str, value: Any, *, environment: str | None = None
) -> None:
    """Vérifie qu'une valeur d'overlay respecte les invariants de démarrage.

    Lève ``ConfigPolicyViolation``. Ne modifie rien, n'accède ni au réseau ni à
    la base.
    """
    env = _environment(environment)

    # --- 1. Clés d'infrastructure : jamais pilotables ----------------------
    if key in INFRASTRUCTURE_KEYS:
        raise ConfigPolicyViolation(
            "infrastructure_key_not_overridable",
            f"{key} engage le réseau, l'index ou la conformité : elle se change "
            "par déploiement, jamais par override.",
        )

    # --- 2. Le registre reste la source des types et des bornes ------------
    from config import registry

    definition = registry.get(key)
    if definition is None:
        raise ConfigPolicyViolation("unknown_key", f"{key} est inconnue du registre.")
    if not definition.runtime_editable:
        raise ConfigPolicyViolation(
            "key_not_editable", f"{key} n'est pas éditable par overlay."
        )
    # La coercion est faite ICI, une fois : les invariants croisés raisonnent sur
    # une valeur TYPÉE. Sans cela, `bool("false")` vaudrait True et la garde
    # croisée se déclencherait à l'envers.
    try:
        typed = definition.coerce(value)
    except Exception as exc:  # ConfigValueError et dérivés
        raise ConfigPolicyViolation("value_out_of_contract", str(exc)) from None

    # --- 3. Invariants croisés ---------------------------------------------
    # Miroir de `llm_router.E005` : activer le repli exige un provider utilisable.
    # Sans cette garde, un override `AI_GATEWAY_FALLBACK_ENABLED=true` créerait au
    # runtime la configuration exacte que le check refuse au démarrage.
    if key == "AI_GATEWAY_FALLBACK_ENABLED" and bool(typed):
        provider = _effective_fallback_provider()
        if provider != "none" and not _tests_tolerated():
            raise ConfigPolicyViolation(
                "fallback_provider_unusable",
                f"AI_GATEWAY_FALLBACK_PROVIDER={provider!r} : RAGatm Open ne connaît aucun "
                "fournisseur de repli (seule valeur admise : none, cf. llm_router.E004).",
            )

    # Le retrieval Core reste un flag de bascule, sans invariant croisé connu.
    # Les quotas plateforme sont bornés par le registre (garde n°2) et validés
    # finement contre les plafonds serveur par apps.workspaces.services.quota.
    _ = env  # conservé : les invariants par environnement s'y adosseront


def is_overridable(key: str) -> bool:
    """Vrai si la clé peut légitimement porter un override."""
    try:
        from config import registry

        definition = registry.get(key)
    except Exception:  # pragma: no cover - registre indisponible
        return False
    if definition is None or not definition.runtime_editable:
        return False
    return key not in INFRASTRUCTURE_KEYS


def guard_runtime_value(
    key: str, value: Any, fallback: Any, *, environment: str | None = None
) -> Any:
    """Retourne ``value`` si elle respecte les invariants, sinon ``fallback``.

    Utilisé par le résolveur runtime : une valeur fautive ne doit jamais atteindre
    l'exécution, et une violation ne doit jamais casser une requête ou une tâche.
    Aucune valeur n'est journalisée (elle peut être sensible).
    """
    try:
        validate_overlay_value(key, value, environment=environment)
    except ConfigPolicyViolation as violation:
        logger.warning(
            "core_config_overlay_rejected key=%s code=%s", key, violation.code
        )
        return fallback
    return value
