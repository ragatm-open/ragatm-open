"""System checks du PROFIL D'EMBEDDINGS (source de vérité unique).

Décision produit 2026-07 : ``ollama`` / ``nomic-embed-text`` / dim 768 / hybride actif
en dev, staging ET production. Ces checks rendent toute divergence **visible et
bloquante** hors dev — l'audit de configuration a montré deux dérives silencieuses :

- production sur un autre fournisseur/modèle que celui de la calibration (le seuil
  n'est pas transférable d'un modèle à l'autre) ;
- ``RAG_HYBRID_ENABLED=True`` résolu à ``False`` (parsing ``== "1"``) → leg vectoriel
  désactivé sans le moindre signal.

Sévérité : **Warning en dev** (DEBUG=True, poste sans Ollama) / **Error bloquante**
en staging et production (DEBUG=False). Ces checks ne touchent PAS la base : l'audit
d'index (coûteux) vit dans `manage.py audit_embeddings_config`.
"""
from __future__ import annotations

from django.core.checks import Error, Warning

# Le provider de GÉNÉRATION peut différer : ces checks ne concernent QUE les embeddings.
ID_PROVIDER = "rag_core.E100"
ID_MODEL = "rag_core.E101"
ID_DIM = "rag_core.E102"
ID_HYBRID = "rag_core.E103"

_HINT = (
    "Profil de référence : EMBEDDINGS_PROVIDER=ai_gateway, AI_GATEWAY_EMBEDDINGS_MODEL=legacy-768, "
    "RAG_EMBEDDING_DIM=768, RAG_HYBRID_ENABLED=1. Voir .env.example et "
    "backend/benchmarks/README.md. Vérifier l'index avec "
    "`python manage.py audit_embeddings_config`."
)


def _effective_embeddings_model() -> str:
    """Modèle d'embeddings RÉELLEMENT résolu pour le provider courant."""
    from django.conf import settings

    from apps.llm_router.embeddings import default_embedding_model_for_provider

    provider = str(getattr(settings, "EMBEDDINGS_PROVIDER", "") or "").strip().lower()
    return (default_embedding_model_for_provider(provider) or "").strip()


def check_embeddings_profile(app_configs, **kwargs):
    """Vérifie que le profil d'embeddings effectif est celui de référence."""
    from django.conf import settings

    # Dev (DEBUG) : on n'empêche pas de travailler sans Ollama -> Warning.
    # Staging/prod (DEBUG=False) : divergence = Error bloquante.
    lenient = bool(getattr(settings, "DEBUG", False))
    Level = Warning if lenient else Error
    issues = []

    expected_provider = getattr(settings, "EMBEDDINGS_EXPECTED_PROVIDER", "ai_gateway")
    expected_model = getattr(settings, "EMBEDDINGS_EXPECTED_MODEL", "legacy-768")

    provider = str(getattr(settings, "EMBEDDINGS_PROVIDER", "") or "").strip().lower()
    # "stub" est l'échappatoire explicite des tests/CI : jamais bloquante en dev, et
    # déjà refusée en prod par config/settings/prod.py.
    if provider == "stub":
        return []

    if provider != expected_provider:
        issues.append(
            Level(
                f"EMBEDDINGS_PROVIDER={provider!r} diverge du profil de référence "
                f"{expected_provider!r}.",
                hint=_HINT + " Le seuil RAG_MIN_SCORE est calibré pour nomic : ne pas le "
                "transposer à un autre modèle sans recalibration.",
                id=ID_PROVIDER,
            )
        )

    model = _effective_embeddings_model()
    if model and model != expected_model:
        issues.append(
            Level(
                f"Modèle d'embeddings effectif {model!r} diverge du profil de référence "
                f"{expected_model!r}.",
                hint=_HINT,
                id=ID_MODEL,
            )
        )

    dim = int(getattr(settings, "RAG_EMBEDDING_DIM", 0) or 0)
    if dim != 768:
        issues.append(
            Level(
                f"RAG_EMBEDDING_DIM={dim} diverge du profil de référence (768).",
                hint=_HINT,
                id=ID_DIM,
            )
        )

    if not bool(getattr(settings, "RAG_HYBRID_ENABLED", False)):
        issues.append(
            Level(
                "RAG_HYBRID_ENABLED est désactivé : le leg vectoriel ne sera pas utilisé "
                "et le retrieval dégrade en FTS seul (embeddings calculés mais inutilisés).",
                hint=_HINT + " Valeurs acceptées : 1/true/yes/on.",
                id=ID_HYBRID,
            )
        )

    return issues


# ---------------------------------------------------------------------------
# Embeddings gouvernés : le modèle doit être EXPLICITE
# ---------------------------------------------------------------------------

def check_governed_embeddings_model(app_configs, **kwargs):
    """Refuse un pipeline d'embeddings gouverné dont le modèle est implicite.

    Le défaut de ``AI_GATEWAY_EMBEDDINGS_MODEL`` vaut ``legacy-768``. Ce modèle
    est refusé par la Gateway de PRODUCTION (``production_allowed: false``, vérifié le
    23/08/2026 : ``HTTP 400 — modèle fr-en-retrieval non autorisé``). Activer les
    embeddings gouvernés sans poser ce réglage produit donc un environnement
    syntaxiquement valide et **fonctionnellement mort** : la configuration passe, et
    chaque document échoue — après coup, document par document.

    Le check est **local et déterministe** : il ne joint pas la Gateway. Le démarrage
    ne doit pas dépendre d'un appel réseau, et l'autorisation réelle relève du
    préflight (``check_embedding_capabilities``). Ce qu'il attrape est le mode de
    défaillance qui ne se voit pas : un défaut dangereux appliqué en silence.

    Il ne bloque QUE si les embeddings gouvernés sont activés. Une installation qui ne
    les utilise pas n'est pas concernée.
    """
    from django.conf import settings
    from django.core.checks import Error

    errors = []
    if not bool(getattr(settings, "AI_GATEWAY_EMBEDDINGS_ENABLED", False)):
        return errors

    from apps.llm_router.capabilities import GOVERNED_MODEL_MAX_INPUT_CHARS

    brut = getattr(settings, "AI_GATEWAY_EMBEDDINGS_MODEL", "")
    modele = str(brut or "").strip()

    if not modele:
        errors.append(Error(
            "AI_GATEWAY_EMBEDDINGS_ENABLED=1 mais AI_GATEWAY_EMBEDDINGS_MODEL est vide : "
            "aucun modèle logique ne peut être résolu pour un espace gouverné sans "
            "modèle propre.",
            hint="Posez explicitement le modèle logique, par exemple `legacy-768`.",
            id="rag_core.E010",
        ))
        return errors

    if modele not in GOVERNED_MODEL_MAX_INPUT_CHARS:
        errors.append(Error(
            f"AI_GATEWAY_EMBEDDINGS_MODEL={modele!r} est inconnu de la table de "
            "capacités : son plafond d'entrée n'est pas déclaré, donc le découpage "
            "retomberait sur la borne conservatrice sans qu'aucune mesure ne le "
            "justifie.",
            hint="Déclarez son plafond dans `GOVERNED_MODEL_MAX_INPUT_CHARS` "
                 "(apps/llm_router/capabilities.py) après l'avoir vérifié par "
                 "`manage.py check_embedding_capabilities`.",
            id="rag_core.E011",
        ))

    return errors
