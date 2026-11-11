"""Registre de QUALIFICATION des modèles et pipelines — source versionnée.

## Pourquoi ce fichier existe

`GET /v1/models` dit ce qui **existe**. Il ne dit pas ce que RagATM a **mesuré**.
Ces deux notions étaient confondues : le seul signal de « modèle recommandé » était
`DEFAULT_RAGSPACE_*`, un littéral d'environnement sans mesure attachée et sans date.

Ce registre porte le chaînon manquant : *nous l'avons mesuré, voici quand, où, et ce
que ça a donné*. Il est **versionné avec le code**, délibérément :

- les fichiers de `calibration/` sont des artefacts d'exécution, non versionnés, et
  parfois régénérés — ils ne peuvent pas servir d'autorité au runtime ;
- une table en base ferait dépendre une décision d'ingénierie d'un état mutable, sans
  revue.

Un statut `qualified` doit se relire dans une diff. C'est tout l'objet.

## Ce que ce registre n'est PAS

Il ne contient pas les benchmarks. Il porte le **strict nécessaire pour justifier** le
statut : identifiant de qualification, date, environnement, référence de qualité,
référence de latence, note. Le détail vit dans `calibration/`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Révision du registre. À incrémenter à chaque modification de son contenu.
#: Elle est tracée dans l'audit des changements de configuration modèle : savoir
#: SOUS QUELLE révision une décision a été prise vaut mieux que stocker le catalogue.
QUALIFICATION_REVISION = "2026-11-02.1"


@dataclass(frozen=True)
class Qualification:
    """Preuve minimale qu'un modèle/pipeline a été mesuré par RagATM."""

    qualification_id: str
    qualified_at: str            # ISO date
    environment: str             # environnement où la mesure a été faite
    quality_reference: str       # ce qui a été mesuré, et le résultat
    latency_reference: str       # idem côté latence
    notes: str = ""
    #: Lot qui a produit la mesure — permet de remonter aux artefacts complets.
    source_lot: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "qualification_id": self.qualification_id,
            "qualified_at": self.qualified_at,
            "environment": self.environment,
            "quality_reference": self.quality_reference,
            "latency_reference": self.latency_reference,
            "notes": self.notes,
            "source_lot": self.source_lot,
        }


# ---------------------------------------------------------------------------
# Pipelines d'EMBEDDINGS gouvernés (clé = modèle LOGIQUE, jamais le nom amont)
# ---------------------------------------------------------------------------
#
# La clé est le `model_logical` publié par le registre de la Gateway. Le nom amont
# (`nomic-embed-text`, `granite-embedding:278m`) est une métadonnée : il peut changer
# sans que le pipeline change de nom — et c'est le digest, donc l'identité des
# vecteurs, qui dirait alors la différence.

QUALIFIED_EMBEDDING_PIPELINES: dict[str, Qualification] = {
    "legacy-768": Qualification(
        qualification_id="EMB-LEGACY768-STAGING-20260822",
        qualified_at="2026-08-22",
        environment="staging",
        quality_reference=(
            "Référence de base. Poids identiques au chemin historique "
            "(`ollama`/`nomic-embed-text`) : score moyen 0,8812 sur 8 questions du "
            "pilote PILOTE-GOUVERNE-20260822."
        ),
        latency_reference=(
            "Query embedding warm p50 = 26,8 ms via `:8200/v1/embeddings` (HTTP brut). "
            "Soit 1,5 ms de MOINS que le chemin `ollama`/`:8100` (28,3 ms) : la "
            "gouvernance ne coûte rien, elle est même légèrement favorable."
        ),
        notes=(
            "Mêmes vecteurs que le chemin historique, mais avec identité vérifiée "
            "(digest, pipeline_id, filtrage strict). C'est l'option qui gouverne sans payer."
        ),
        source_lot="GOVERNED-EMBEDDING-PIPELINE-ENABLEMENT-STAGING",
    ),
    "fr-en-retrieval": Qualification(
        qualification_id="EMB-FRENRETRIEVAL-STAGING-20260822",
        qualified_at="2026-08-22",
        environment="staging",
        quality_reference=(
            "Score moyen 0,9061 contre 0,8812 pour la référence de base (+0,025) sur "
            "8 questions du pilote ; aucune question en recul. Corpus SYNTHÉTIQUE de "
            "24 documents d'un chunk : mesure de non-régression, pas verdict de qualité."
        ),
        latency_reference=(
            "Query embedding warm p50 = 170,5 ms (HTTP brut), soit ×6,0 contre la "
            "référence de base. Le surcoût vient du modèle amont "
            "(granite-embedding:278m, 278 M paramètres contre 137 M), pas de la "
            "gouvernance : +2,2 ms seulement pour la validation RagATM."
        ),
        notes=(
            "Fenêtre de contexte amont réduite (512 tokens contre 8192 pour bge-m3) : "
            "la marge de troncature n'a pas été mesurée sur un corpus réel."
        ),
        source_lot="GOVERNED-EMBEDDING-PIPELINE-ENABLEMENT-STAGING",
    ),
}


# ---------------------------------------------------------------------------
# Modèles de GÉNÉRATION (clé = nom amont, celui que `RagSpace.llm_model` stocke)
# ---------------------------------------------------------------------------

QUALIFIED_CHAT_MODELS: dict[str, Qualification] = {
    "qwen3:30b-instruct": Qualification(
        qualification_id="CHAT-QWEN3-30B-PROD-20260822",
        qualified_at="2026-08-22",
        environment="production+staging",
        quality_reference=(
            "Modèle servant l'intégralité du trafic de chat des deux environnements "
            "depuis le 27/07/2026 (197 générations journalisées en production, "
            "459 en staging). Qualifié par l'usage, pas par un banc dédié."
        ),
        latency_reference=(
            "Streaming natif via `/v1/generate/stream` qualifié en production "
            "(qualification interne)."
        ),
        notes=(
            "Défaut d'environnement (`DEFAULT_RAGSPACE_CHAT_MODEL`). Banc à contexte figé "
            "(33 cas, qualification interne) : 32/33, qualité "
            "indiscernable de mistral-small3.2:24b, gemma3:27b, llama3.1:8b et "
            "deepseek-r1:32b (écart ≤ 1 cas), latence la plus basse (p50 457 ms). "
            "Réserve : sur deux sources contradictoires, il a tranché par une préséance "
            "non étayée au lieu de signaler l'écart."
        ),
        source_lot="qualification-interne-generation-par-defaut",
    ),
}


# ---------------------------------------------------------------------------
# Politique de SORTIE du chat (modèles exclus quelle que soit leur capacité)
# ---------------------------------------------------------------------------
#
# Clé = nom amont SANS tag. Une règle ciblée, et assumée comme telle : la Gateway ne
# publie aucune métadonnée qui dise qu'un modèle expose son raisonnement dans la
# réponse (`reasoning.expose_thinking` n'existe que pour les modèles inscrits). Le
# constat vient d'une MESURE, pas d'une détection textuelle à l'exécution : RagATM ne
# filtre pas les réponses, il refuse de proposer le modèle.

CHAT_OUTPUT_POLICY_REJECTED: dict[str, str] = {
    "semx-minicpm-v4.5": (
        "Raisonnement exposé dans la réponse sur 3 cas sur 3 (« So, let's tackle this… "
        "</think> »), en anglais pour des questions en français "
        "(qualification interne). À requalifier avant toute réadmission."
    ),
}


# ---------------------------------------------------------------------------
# Recommandation
# ---------------------------------------------------------------------------
#
# Un seul recommandé par usage. Embeddings : l'invariant `recommended => qualified`
# est vérifié par test — recommander ce qu'on n'a pas mesuré est exactement l'erreur
# que ce registre existe pour empêcher. Chat : `recommended => sélectionnable` et
# `recommended == défaut effectif` ; le test vérifie en outre que le défaut CODE est
# qualifié.

RECOMMENDED_EMBEDDING_PIPELINE = "legacy-768"

#: Le modèle de chat recommandé n'est PLUS un littéral : c'est le défaut effectif de
#: l'environnement (`DEFAULT_RAGSPACE_CHAT_MODEL`), dès lors que la Gateway le sert en
#: génération (cf. `model_catalog._chat_entry`). Badge et présélection avaient deux
#: sources ; changer l'une sans l'autre aurait affiché « Recommandé » sur un modèle
#: que personne ne présélectionnait. La justification reste volontairement modeste :
#: le banc du 02/11/2026 n'a établi aucune supériorité de qualité.
RECOMMENDED_CHAT_REASON = (
    "Choix par défaut opérationnel de la plateforme. Qualité équivalente aux autres "
    "modèles compatibles sur le banc à contexte figé ; latence la plus basse."
)

#: Justification de la recommandation d'embeddings, exposée par l'API.
#: Elle doit tenir en une phrase lisible par un exploitant.
RECOMMENDED_EMBEDDING_REASON = (
    "Identité vectorielle vérifiée sans coût de latence : mêmes poids que le chemin "
    "historique, 26,8 ms contre 170,5 ms pour fr-en-retrieval. Le gain de qualité de "
    "fr-en-retrieval (+0,025 sur corpus synthétique) ne justifie pas ×6 de latence par "
    "défaut ; il reste disponible et qualifié pour un espace qui privilégie la qualité."
)


def embedding_qualification(logical_model: str) -> Qualification | None:
    return QUALIFIED_EMBEDDING_PIPELINES.get((logical_model or "").strip())


def chat_qualification(model: str) -> Qualification | None:
    return QUALIFIED_CHAT_MODELS.get((model or "").strip())


def chat_output_policy_rejection(model: str) -> str | None:
    """Motif d'exclusion de sortie pour ``model`` (tout tag confondu), sinon ``None``."""
    base = (model or "").strip().lower().split(":", 1)[0]
    return CHAT_OUTPUT_POLICY_REJECTED.get(base)
