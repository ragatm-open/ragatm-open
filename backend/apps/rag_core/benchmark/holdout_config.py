"""Gel complet et vérifiable de la configuration de retrieval du holdout (Lot P1-C1).

Doctrine
--------
Un holdout ne mesure QU'UNE configuration gelée. Toute dérive d'un paramètre qui
change le RÉSULTAT (catégorie A) bloque le run AVANT consommation (fail closed). Le
gel est *exhaustif* : un nouveau paramètre influent ajouté au pipeline sans être
classé fait échouer le test d'exhaustivité (``check_registry_exhaustiveness``).

Catégories
----------
- **A** — affecte le résultat (candidats/ordre/scores/sources) → DOIT être gelé.
- **B** — observabilité uniquement (answer_kind, préambules) → non bloquant.
- **C** — performance uniquement → non bloquant.
- **D** — hors périmètre holdout (audit, journalisation) → non bloquant.
- **T** — topologie (adresse de service) → non bloquant, et volontairement HORS du
  hash : y faire entrer une URL privée ferait dépendre l'identité d'un RÉSULTAT de
  l'endroit d'où on l'a obtenu, et exposerait une valeur sensible dans un sceau.

Le holdout mesure le RECALL sur les sources (``doc_rank``/``chunk_rank``) ; seuls les
paramètres qui changent la liste ``sources`` (contenu, ordre, nombre, y compris la
troncature par budget de prompt qui peut retirer des sources) sont en catégorie A.

Versions de schéma de gel
-------------------------
L'ensemble des paramètres qui COMPOSENT l'identité gelée a évolué. Un hash ne veut
rien dire si l'on ignore selon quelle définition il a été calculé : chaque paramètre
déclare donc les schémas auxquels il appartient, et le numéro de schéma entre dans le
hash (``canonical_json``).

- **v1** — définition historique. C'est celle de la campagne P2.6, dont le sceau
  ``MISSION_EXPECTED_CONFIG_HASH`` reste calculable et vérifiable À L'IDENTIQUE.
  Elle contient deux paramètres FANTÔMES (``RAG_RRF_K``,
  ``RAG_ENABLE_NEWS_HEURISTIC``) que le pipeline ne lit plus : ils y étaient entrés
  par le balayage TEXTUEL, qui voyait leur nom dans un commentaire. On les y laisse —
  retirer un paramètre de v1 réécrirait le sceau d'une campagne déjà menée.
- **v2** — ajoute le chemin de reranking AI Gateway, retire les deux fantômes, et
  repose sur un balayage par ANALYSE SYNTAXIQUE : un nom cité dans un commentaire n'y
  entre plus, un nom réellement lu ne peut plus en sortir.
- **v3** — définition courante. Elle scelle l'IDENTITÉ D'EMBEDDING.

Pourquoi v3 : la dimension ne suffit pas
----------------------------------------
Jusqu'à v2, la seule propriété d'embedding scellée était ``RAG_EMBEDDING_DIM``. Or
deux modèles de même dimension produisent des vecteurs **incomparables**, et la
comparaison croisée ne lève aucune erreur : elle rend des scores plausibles et un
contenu quasi aléatoire (``corpus/domain/embedding_identity`` a mesuré 0 à 1 chunk
pertinent sur 10). Deux campagnes pouvaient donc porter le même sceau alors que leurs
vecteurs venaient de modèles différents — un rapport de comparaison sans valeur, et
rien pour le dire.

v3 scelle deux niveaux, parce que le défaut a deux visages :

1. **les réglages** — ``EMBEDDINGS_PROVIDER``, les défauts de RagSpace, la bascule et
   le modèle de la Gateway d'embeddings entrent dans ``values`` ;
2. **ce qui a réellement produit les vecteurs** — le bloc ``embedding_identity``, dont
   ``observed_pipeline_families`` lit les familles présentes sur les chunks indexés.

Le second niveau n'est pas un raffinement. L'identité d'un RagSpace vit en BASE, et la
migration d'embedding de production du 23/08/2026 est passée par un ``UPDATE`` sans
toucher au moindre réglage : un sceau limité aux settings serait resté identique
pendant que tous les vecteurs changeaient.

Ce qui reste volontairement HORS du sceau : adresses de service (topologie), délais et
reprises (performance), secrets (jamais). Aucun ne change le vecteur produit.

Une campagne ancienne reste vérifiable avec sa définition d'origine — les charges
utiles v1 et v2 ne portent pas la clé ``embedding_identity``, et leurs hashes sont
inchangés à l'octet près.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

#: Versions du schéma de gel. Le numéro entre dans le hash : deux définitions
#: différentes ne peuvent pas produire le même sceau par accident.
FREEZE_SCHEMA_V1 = 1
FREEZE_SCHEMA_V2 = 2
FREEZE_SCHEMA_V3 = 3
FREEZE_SCHEMAS: tuple[int, ...] = (FREEZE_SCHEMA_V1, FREEZE_SCHEMA_V2, FREEZE_SCHEMA_V3)

#: Schéma utilisé par toute NOUVELLE campagne. Les campagnes historiques épinglent
#: explicitement le leur (cf. ``mission_frozen_config``).
CURRENT_FREEZE_SCHEMA = FREEZE_SCHEMA_V3

#: Sentinelle : paramètre NON positionné dans l'environnement (le pipeline utilise son
#: défaut inline). Gelé tel quel : si l'env le positionne plus tard -> dérive détectée.
UNSET = "__unset__"

JSONScalar = bool | int | float | str | None


class ParamCategory(str, Enum):
    RESULT = "A"
    OBSERVABILITY = "B"
    PERFORMANCE = "C"
    OUT_OF_SCOPE = "D"
    #: Adresse de service. Peut différer d'un environnement à l'autre sans que le
    #: RÉSULTAT change ; n'entre jamais dans l'identité gelée.
    TOPOLOGY = "T"


class ParamSource(str, Enum):
    SETTINGS = "settings"   # getattr(settings, X, default) / settings.X
    ENV = "env"             # os.getenv(X) résolu à l'import (ex. rewrite.py)


@dataclass(frozen=True)
class ParamSpec:
    name: str
    category: ParamCategory
    source: ParamSource
    default: Any  # défaut utilisé pour la résolution effective (UNSET si aucun)
    description: str
    #: Schémas de gel auxquels ce paramètre appartient. Par défaut tous : un
    #: paramètre n'est restreint que lorsqu'il a été ajouté ou retiré en cours de
    #: route, et cette restriction est alors une DÉCISION écrite, pas un effet de bord.
    schemas: tuple[int, ...] = FREEZE_SCHEMAS

    def in_schema(self, schema_version: int) -> bool:
        return schema_version in self.schemas


# ---------------------------------------------------------------------------
# REGISTRE CENTRAL — source de vérité unique de la classification.
# Toute clé RAG_* lue par le pipeline DOIT y figurer (garde d'exhaustivité).
# ---------------------------------------------------------------------------
_A = ParamCategory.RESULT
_B = ParamCategory.OBSERVABILITY
_D = ParamCategory.OUT_OF_SCOPE
_T = ParamCategory.TOPOLOGY
_S = ParamSource.SETTINGS
_E = ParamSource.ENV

#: Paramètres restreints à certaines versions du schéma.
#:
#: `_SINCE_V2` s'appelait `_V2_ONLY`. Le renommage n'est pas cosmétique : à
#: l'ajout de v3, un tuple `(FREEZE_SCHEMA_V2,)` aurait fait SORTIR du sceau tout
#: le chemin de reranking AI Gateway — silencieusement, puisque `result_params`
#: se contente de filtrer. Un nom qui dit « v2 seulement » là où l'intention est
#: « à partir de v2 » est une bombe à retardement à chaque nouvelle version.
_V1_ONLY = (FREEZE_SCHEMA_V1,)
_SINCE_V2 = (FREEZE_SCHEMA_V2, FREEZE_SCHEMA_V3)
_V3_ONLY = (FREEZE_SCHEMA_V3,)

REGISTRY: tuple[ParamSpec, ...] = (
    # --- Cœur retrieval (A) ------------------------------------------------
    ParamSpec("RAG_HYBRID_ENABLED", _A, _S, False, "active le leg vectoriel"),
    ParamSpec("RAG_MIN_SCORE", _A, _S, 0.76, "seuil vectoriel de filtrage"),
    ParamSpec("RAG_MIN_TEXT_RANK", _A, _S, 0.0, "seuil lexical FTS"),
    ParamSpec("RAG_MIN_SOURCE_SCORE", _A, _S, 1e-6, "score minimal d'une source"),
    ParamSpec("RAG_MIN_SOURCES", _A, _S, UNSET, "nb minimal de sources (answerability stricte)"),
    ParamSpec("RAG_TOP_K", _A, _S, 4, "taille finale du top_k"),
    ParamSpec("RAG_VECTOR_TOP_K", _A, _S, 20, "top_k du leg vectoriel"),
    # FANTÔME v1 : `merge.py` documente lui-même que ce nom n'est projeté par aucun
    # settings et n'est plus lu. Il n'est resté dans le gel que parce que le balayage
    # TEXTUEL voyait son nom dans ce commentaire. Conservé en v1 — l'en retirer
    # réécrirait le sceau de P2.6 — et retiré de v2.
    ParamSpec("RAG_RRF_K", _A, _S, UNSET, "constante RRF de fusion (non lue)", _V1_ONLY),
    ParamSpec("RAG_HNSW_EF_SEARCH", _A, _S, 40, "ef_search HNSW (rappel vectoriel)"),
    ParamSpec("RAG_EMBEDDING_DIM", _A, _S, 768, "dimension d'embedding"),
    # --- Identité d'embedding (A, schéma v3) -------------------------------
    # La dimension était, jusqu'à v2, la SEULE propriété d'embedding scellée. Or
    # deux modèles de même dimension produisent des vecteurs incomparables : la
    # comparaison croisée ne lève aucune erreur, elle rend des scores plausibles
    # et un contenu quasi aléatoire (cf. `corpus/domain/embedding_identity`, qui
    # a mesuré 0 à 1 chunk pertinent sur 10 dans ce cas). Une campagne pouvait
    # donc porter le même sceau qu'une autre alors que ses vecteurs venaient d'un
    # autre modèle.
    #
    # Ces cinq réglages décident du VECTEUR produit. Sont volontairement EXCLUS,
    # parce qu'ils ne le changent pas : `AI_GATEWAY_EMBEDDINGS_BASE_URL`
    # (topologie — et une adresse privée n'a rien à faire dans un sceau),
    # `AI_GATEWAY_EMBEDDINGS_TIMEOUT_SECONDS` et `EMBEDDINGS_TIMEOUT_S`
    # (performance), les clés d'API (jamais).
    ParamSpec("EMBEDDINGS_PROVIDER", _A, _S, "ollama",
              "provider d'embeddings global (ingestion sans identité d'espace)", _V3_ONLY),
    ParamSpec("DEFAULT_RAGSPACE_EMBEDDING_PROVIDER", _A, _S, "ollama",
              "provider donné aux NOUVEAUX RagSpaces", _V3_ONLY),
    ParamSpec("DEFAULT_RAGSPACE_EMBEDDING_MODEL", _A, _S, "nomic-embed-text",
              "modèle donné aux NOUVEAUX RagSpaces", _V3_ONLY),
    ParamSpec("AI_GATEWAY_EMBEDDINGS_ENABLED", _A, _S, False,
              "bascule le chemin d'embedding gouverné (identité de pipeline vérifiée)", _V3_ONLY),
    ParamSpec("AI_GATEWAY_EMBEDDINGS_MODEL", _A, _S, "fr-en-retrieval",
              "modèle logique demandé à la Gateway d'embeddings", _V3_ONLY),
    # --- Pool / diversité (A) ----------------------------------------------
    ParamSpec("RAG_RETRIEVAL_CANDIDATE_K", _A, _S, 0, "pool élargi explicite"),
    ParamSpec("RAG_RETRIEVAL_CANDIDATE_MULTIPLIER", _A, _S, 3, "multiplicateur de pool"),
    ParamSpec("RAG_RETRIEVAL_MAX_PER_GROUP", _A, _S, 2, "plafond par groupe de diversité"),
    ParamSpec("RAG_RETRIEVAL_DIVERSITY_ENABLED", _A, _S, False, "active la diversification"),
    # --- Rerank (A) --------------------------------------------------------
    ParamSpec("RAG_RERANK_ENABLED", _A, _S, UNSET, "active le reranking"),
    ParamSpec("RAG_RERANK_POOL_K", _A, _S, 20, "pool passé au reranker"),
    ParamSpec("RAG_RERANK_TOP_K", _A, _S, UNSET, "nb pris après rerank"),
    ParamSpec("RAG_RERANK_TRIGGER_MIN_CANDIDATES", _A, _S, UNSET, "seuil de déclenchement rerank"),
    # --- Reranking via l'AI Gateway (A, schéma v2) -------------------------
    # Chemin DISTINCT du reranking historique ci-dessus : aucun modèle local, un appel
    # `POST /v1/rerank`. Il échappait entièrement au gel — non par choix, mais parce
    # que le balayage textuel ne regardait pas `services/rerank_gateway.py` et que
    # l'orchestrateur ne nomme aucun de ces réglages (il passe par des fonctions).
    #
    # Les six sont de catégorie A parce qu'ils changent tous, chacun à sa façon, la
    # liste de sources RENDUE :
    #   - ENABLED       : reranker ou ordre RRF ;
    #   - MODEL_LOGICAL : deux modèles ne classent pas pareil ;
    #   - POOL_K        : décide QUELS candidats peuvent seulement être remontés ;
    #   - MAX_PER_GROUP : plafond documentaire du chemin reranké, donc composition ;
    #   - FAILURE_POLICY: décide si une panne rend un résultat DÉGRADÉ ou aucune réponse ;
    #   - TIMEOUT       : décide de la FRÉQUENCE des replis — un délai plus court ne
    #     rend pas « la même réponse en moins de temps », il rend l'ordre RRF à la
    #     place de l'ordre reranké. C'est un budget de performance dont l'effet est
    #     fonctionnel, et c'est pourquoi il est en A et non en C.
    ParamSpec("RAG_RERANKER_ENABLED", _A, _S, False, "active le reranking AI Gateway", _SINCE_V2),
    ParamSpec("RAG_RERANKER_MODEL_LOGICAL", _A, _S, "rerank-default", "nom logique du modèle de rerank", _SINCE_V2),
    ParamSpec("RAG_RERANKER_POOL_K", _A, _S, 40, "profondeur soumise au rerank Gateway", _SINCE_V2),
    ParamSpec("RAG_RERANKER_MAX_PER_GROUP", _A, _S, 3, "plafond par document du chemin reranké", _SINCE_V2),
    ParamSpec("RAG_RERANKER_FAILURE_POLICY", _A, _S, "fail_open_rrf", "conduite en cas d'échec du rerank", _SINCE_V2),
    ParamSpec("RAG_RERANKER_TIMEOUT_SECONDS", _A, _S, 2.0, "délai dédié du rerank (pilote la fréquence des replis)", _SINCE_V2),
    # TOPOLOGIE : l'adresse de la Gateway. Elle DOIT être classée — sans quoi la garde
    # d'exhaustivité la signalerait — mais elle n'entre dans aucun hash : c'est une
    # valeur privée, et l'endroit d'où l'on obtient un résultat ne définit pas ce
    # résultat. Ce que son absence pourrait masquer (drapeau actif + URL vide =>
    # repli permanent) est traité en amont par le system check `rag_core.E009`.
    ParamSpec("RAG_RERANKER_BASE_URL", _T, _S, "", "adresse de l'AI Gateway de reranking", _SINCE_V2),

    # --- Signaux forts / pondération conditionnelle (A) --------------------
    ParamSpec("RAG_STRONG_SIGNALS_ENABLED", _A, _S, True, "active les signaux forts"),
    ParamSpec("RAG_RRF_CONDITIONAL_LEXICAL_WEIGHT_ENABLED", _A, _S, False, "pondération lexicale conditionnelle"),
    ParamSpec("RAG_STRUCTURAL_NOISE_PENALTY_ENABLED", _A, _S, False, "pénalité bruit structurel"),
    ParamSpec("RAG_STRUCTURAL_NOISE_PENALTY_FACTOR", _A, _S, 0.75, "facteur de la pénalité structurelle"),
    ParamSpec("RAG_RRF_STRONG_IDENTIFIER_LEXICAL_WEIGHT", _A, _S, 1.5, "poids lexical si identifiant fort"),
    ParamSpec("RAG_FTS_SLASH_IDENTIFIERS_ENABLED", _A, _S, False, "tokenisation slash des identifiants FTS"),
    # --- Chemin SQL du leg lexical (A, schéma v2) --------------------------
    # Lus par `repositories/chunk_repository.py`, module qui n'était pas balayé : le
    # SQL du retrieval échappait donc au gel, alors qu'il décide QUELS passages
    # existent avant tout classement.
    #
    # `RAG_FTS_CONFIG` change les jetons indexés et donc les candidats : une campagne
    # menée en `simple` ne dit rien d'une production en `french`.
    #
    # `RAG_FTS_STORED_VECTOR_ENABLED` est réputé NEUTRE — la colonne générée
    # matérialise la même expression `to_tsvector('french', text)`. « Réputé » est
    # justement ce qu'une qualification doit vérifier : le geler, c'est empêcher qu'une
    # campagne change de chemin SQL en cours de route et attribue l'écart à autre chose.
    ParamSpec("RAG_FTS_CONFIG", _A, _S, "simple", "configuration FTS PostgreSQL", _SINCE_V2),
    ParamSpec("RAG_FTS_STORED_VECTOR_ENABLED", _A, _S, False, "lit le vecteur lexical stocké (colonne générée)", _SINCE_V2),
    ParamSpec("RAG_FTS_QUERY_NORMALIZATION_V2", _A, _S, False, "construction normalisée de la requête lexicale", _SINCE_V2),
    ParamSpec("RAG_FTS_PLAN_MAX_ATTEMPTS", _A, _S, 6, "borne du repli de plan FTS (privé et corpus)"),
    ParamSpec(
        "RAG_BUSINESS_CORPUS_UNIFIED_FUSION_ENABLED", _A, _S, False,
        "fusion de rangs unifiée privé + corpus métier",
    ),
    ParamSpec(
        "RAG_BUSINESS_CORPUS_RRF_LEG_WEIGHT", _A, _S, 1.0,
        "poids du vote des legs corpus dans la fusion unifiée",
    ),
    ParamSpec(
        "RAG_BUSINESS_CORPUS_CALIBRATED_ADMISSION_ENABLED", _A, _S, False,
        "porte d'admission calibrée des corpus métier",
    ),
    ParamSpec(
        "RAG_BUSINESS_CORPUS_EMBEDDING_FINGERPRINT_ENFORCEMENT_ENABLED", _A, _S, False,
        "filtrage vectoriel corpus par empreinte d'indexation",
    ),
    ParamSpec(
        "RAG_BUSINESS_CORPUS_ADMISSION_PROBABILITY_THRESHOLD", _A, _S, 0.65,
        "seuil sur la probabilité calibrée d'admission d'un corpus",
    ),
    # --- Temporel (A) ------------------------------------------------------
    ParamSpec("RAG_TEMPORAL_CANDIDATES_ENABLED", _A, _S, False, "active le leg temporel"),
    ParamSpec("RAG_TEMPORAL_CANDIDATE_LIMIT", _A, _S, 100, "limite du leg temporel"),
    # --- Espace / boosts (A) ----------------------------------------------
    ParamSpec("RAG_SPACE_MIN_SCORE_ENABLED", _A, _S, False, "seuil par RagSpace"),
    ParamSpec("RAG_WORKSPACE_SOURCE_BOOST", _A, _S, 1.10, "boost sources privées"),
    ParamSpec("RAG_DOMAIN_CORPUS_SOURCE_BOOST", _A, _S, 1.0, "boost sources corpus"),
    # --- Mode news + budget prompt (A : peuvent retirer des sources) -------
    # FANTÔME v1 : même cas — le mode « news » n'est pas branché et l'orchestrateur ne
    # lit plus ce nom ; seul son commentaire le citait.
    ParamSpec("RAG_ENABLE_NEWS_HEURISTIC", _A, _S, False, "heuristique mode news (non lue)", _V1_ONLY),
    ParamSpec("RAG_MAX_PROMPT_CHARS", _A, _S, 18000, "budget prompt (troncature de sources)"),
    ParamSpec("RAG_MIN_PROMPT_SOURCES", _A, _S, 4,
              "sources dont la place est réservée avant la mémoire de conversation"),
    ParamSpec("RAG_SNIPPET_MAX_CHARS", _A, _S, 1200, "budget par extrait"),
    ParamSpec("RAG_SNIPPET_MAX_CHARS_TABLE", _A, _S, 4000, "budget extrait tabulaire"),
    ParamSpec("RAG_MIN_FINAL_SOURCE_CHARS", _A, _S, 400, "extrait minimal de la derniere source conservee"),
    # `_V3_ONLY` : la stratégie d'extrait change ce que le modèle LIT, donc la réponse —
    # catégorie A sans discussion. Mais la porter dans v1/v2 réécrirait le sceau de
    # campagnes déjà menées (dont P2.6, épinglée à v1) pour un paramètre qui n'existait
    # pas quand elles ont tourné. Elle entre donc dans la définition COURANTE seulement.
    ParamSpec("RAG_SNIPPET_STRATEGY", _A, _S, "head",
              "fenetre de l'extrait : head (N premiers caracteres) ou centered", _V3_ONLY),
    # Conformité du RENDU du prompt (lot PROMPT-RENDER-CONFORMANCE-FIX-1A) : marqueur
    # tabulaire compté, en-tête sans date/page répétées, compteurs V4. Change le prompt
    # (donc le résultat) : entre dans le gel. Même doctrine que RAG_SNIPPET_STRATEGY —
    # définition COURANTE seulement, les campagnes passées (v1, v2) restent intactes.
    ParamSpec("RAG_PROMPT_RENDER_CONFORMANCE_V1_ENABLED", _A, _S, False,
              "conformite du rendu du prompt : marqueur tabulaire compte, en-tete deduplique, compteurs V4", _V3_ONLY),
    # Courriels = contenu NON FIABLE : en-tête « COURRIEL » et
    # règles anti-injection quand une source courriel est au contexte. Change le prompt
    # (donc le résultat) : même doctrine — définition COURANTE seulement.
    ParamSpec("RAG_EMAIL_UNTRUSTED_FRAMING_ENABLED", _A, _S, False,
              "cadrage contenu non fiable des sources courriel dans le prompt", _V3_ONLY),
    # Extraits des fragments d'extraction STRUCTURÉE (STRUCTURED-VISUAL-AUTOMATION-AND-
    # CONTEXT-2) : change ce que le modèle lit d'un graphique ou d'un tableau. Même
    # doctrine — définition COURANTE seulement.
    ParamSpec("RAG_SNIPPET_MAX_CHARS_STRUCTURED", _A, _S, 4000,
              "budget extrait structure (graphique/tableau), unites logiques", _V3_ONLY),
    # --- Réécriture de requête (A : change la requête de retrieval) --------
    ParamSpec("RAG_MAX_SEARCH_QUERIES", _A, _S, 3, "nb max de requêtes de recherche"),
    ParamSpec("RAG_QUERY_REWRITE_MAX_CHARS", _A, _S, 200, "longueur max requête réécrite"),
    ParamSpec("RAG_KEYWORDIZE_MAX_TOKENS", _A, _S, 8, "tokens max keywordize"),
    ParamSpec("RAG_QUERY_REWRITE_TIMEOUT_S", _A, _S, 12, "timeout réécriture LLM"),
    ParamSpec("RAG_REWRITE_TEMPERATURE", _A, _S, UNSET, "température réécriture"),
    ParamSpec("RAG_REWRITE_NUM_CTX", _A, _S, UNSET, "num_ctx réécriture"),
    ParamSpec("RAG_REWRITE_NUM_PREDICT", _A, _S, UNSET, "num_predict réécriture"),
    ParamSpec("RAG_EXPLAIN_MIN_SCORE", _A, _S, UNSET, "override seuil mode explain"),
    ParamSpec("RAG_EXPLAIN_MIN_TEXT_RANK", _A, _S, UNSET, "override seuil lexical mode explain"),
    ParamSpec("RAG_EXPLAIN_RERANK_TRIGGER_MIN_CANDIDATES", _A, _S, UNSET, "override trigger rerank explain"),
    # --- Réécriture (env, import-time) — A mais NON overridable par settings.
    # Résolus à l'import dans services/query/rewrite.py : le gel les VÉRIFIE (ne peut
    # pas les forcer). Défaut = valeur EFFECTIVE sous un environnement propre.
    # Défaut ALIGNÉ sur rewrite.py:39 (`os.getenv(..., "0")`). La campagne P2.6 a bien
    # tourné réécriture ACTIVE — mais c'était un choix de campagne, pas le défaut du
    # code : la valeur vit donc dans MISSION_CALIBRATED_OVERRIDES, à sa place.
    ParamSpec("RAG_QUERY_REWRITE_ENABLED", _A, _E, False, "active la réécriture LLM (env import-time)"),
    ParamSpec("RAG_KEYWORDIZE_ENABLED", _A, _E, True, "active keywordize (env import-time)"),
    ParamSpec("RAG_QUERY_REWRITE_PROVIDER", _A, _E, "ollama", "provider réécriture (env import-time)"),
    ParamSpec("RAG_QUERY_REWRITE_MODEL", _A, _E, "", "modèle réécriture (env import-time)"),
    # --- Paramètres par mode (A conditionnel ; défaut = hérite de la base) --
    ParamSpec("RAG_INVENTORY_TOP_K", _A, _S, UNSET, "override top_k mode inventory"),
    ParamSpec("RAG_INVENTORY_VECTOR_TOP_K", _A, _S, UNSET, "override vector_top_k inventory"),
    ParamSpec("RAG_INVENTORY_MIN_UNIQUE_DOCS", _A, _S, UNSET, "docs uniques min inventory"),
    ParamSpec("RAG_INVENTORY_RERANK_POOL_K", _A, _S, UNSET, "pool rerank inventory"),
    ParamSpec("RAG_INVENTORY_RERANK_TAKE", _A, _S, UNSET, "take rerank inventory"),
    ParamSpec("RAG_EXPLAIN_TOP_K", _A, _S, UNSET, "override top_k explain"),
    ParamSpec("RAG_EXPLAIN_VECTOR_TOP_K", _A, _S, UNSET, "override vector_top_k explain"),
    ParamSpec("RAG_EXPLAIN_RERANK_POOL_K", _A, _S, UNSET, "pool rerank explain"),
    ParamSpec("RAG_EXPLAIN_RERANK_TAKE", _A, _S, UNSET, "take rerank explain"),
    ParamSpec("RAG_NEWS_TOP_K", _A, _S, UNSET, "override top_k news"),
    ParamSpec("RAG_NEWS_VECTOR_TOP_K", _A, _S, UNSET, "override vector_top_k news"),
    ParamSpec("RAG_NEWS_RERANK_POOL_K", _A, _S, UNSET, "pool rerank news"),
    ParamSpec("RAG_NEWS_RERANK_TAKE", _A, _S, UNSET, "take rerank news"),
    # --- Answerability (benchmark ; gelé par cohérence avec la calibration) -
    ParamSpec("RAG_ANSWERABILITY_ENABLED", _A, _S, False, "gate answerability (benchmark)"),
    # --- Observabilité (B) : answer_kind / préambules, PAS les sources ------
    # Catégorie B et non A : le drapeau fantôme n'influence AUCUN résultat — il
    # ajoute une section à la trace, rien d'autre, et un test compare les réponses
    # drapeau ON et OFF. Mais le pipeline le LIT, donc la garde d'exhaustivité
    # exige qu'il figure ici : un réglage lu et non déclaré rendrait une campagne
    # incapable de dire sous quelle configuration elle a tourné.
    ParamSpec("RAG_ADMISSION_SHADOW_ENABLED", _B, _S, False,
              "policy d'admission balanced-v1 calculée en observation"),
    # Catégorie B pour la même raison : la trace des candidats n'influence AUCUN
    # résultat — elle ajoute une section à la trace — mais le pipeline la LIT, donc
    # la garde d'exhaustivité exige qu'elle soit déclarée ici.
    ParamSpec("RAG_CANDIDATE_TRACE_ENABLED", _B, _S, False,
              "trace détaillée des candidats du pool (diagnostic)"),
    ParamSpec("RAG_LOWCONF_ENABLED", _B, _S, UNSET, "active la low-confidence"),
    ParamSpec("RAG_LOWCONF_FACTOR", _B, _S, 1.0, "facteur low-confidence"),
    ParamSpec("RAG_LOWCONF_MIN_ABS", _B, _S, 0.0, "seuil absolu low-confidence"),
    ParamSpec("RAG_LOWCONF_MIN_SOURCES", _B, _S, 1, "sources min low-confidence"),
    ParamSpec("RAG_LOWCONF_PROMPT_PREAMBLE", _B, _S, UNSET, "préambule low-confidence"),
    ParamSpec("RAG_PARTIAL_PROMPT_PREAMBLE", _B, _S, UNSET, "préambule partiel"),
    ParamSpec("RAG_EXPLAIN_PROMPT_PREAMBLE", _B, _S, UNSET, "préambule explain"),
    ParamSpec("RAG_COMPARE_PROMPT_PREAMBLE", _B, _S, UNSET, "préambule comparatif"),
    ParamSpec("RAG_NEWS_PROMPT_HINT", _B, _S, UNSET, "hint news"),
    # --- Hors périmètre (D) : audit / journalisation -----------------------
    ParamSpec("RAG_QUERY_LOG_STORE_QUESTION", _D, _S, True, "stockage question audit"),
    ParamSpec("RAG_QUERY_LOG_STORE_ANSWER", _D, _S, True, "stockage réponse audit"),
    ParamSpec("RAG_MAX_SNIPPET_CHARS_AUDIT", _D, _S, 200, "troncature extrait audit"),
    ParamSpec("RAG_MAX_REQUEST_ID_CHARS", _D, _S, 200, "troncature request_id"),
    ParamSpec("RAG_QUERY_REWRITE_PROVIDER_AUDIT", _D, _S, UNSET, "réservé"),
)

_BY_NAME: dict[str, ParamSpec] = {p.name: p for p in REGISTRY}


def result_params(schema_version: int) -> tuple[ParamSpec, ...]:
    """Paramètres de catégorie A qui COMPOSENT l'identité gelée d'un schéma donné.

    Il n'existe volontairement pas de constante `RESULT_PARAMS` sans version : c'est
    précisément l'absence de cette question — « selon quelle définition ? » — qui a
    permis à un hash de changer de signification sans changer de nom.
    """
    if schema_version not in FREEZE_SCHEMAS:
        raise ValueError(
            f"schéma de gel inconnu: {schema_version!r} (connus: {list(FREEZE_SCHEMAS)})"
        )
    return tuple(p for p in REGISTRY if p.category is _A and p.in_schema(schema_version))


def _norm(v: Any) -> JSONScalar:
    """Normalise une valeur en scalaire JSON stable (types explicites)."""
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float, str)):
        return v
    return str(v)


def resolve_effective_retrieval_config(
    schema_version: int = CURRENT_FREEZE_SCHEMA,
) -> dict[str, JSONScalar]:
    """Résout la valeur EFFECTIVE de chaque paramètre de catégorie A d'un schéma.

    Utilise la MÊME résolution que le pipeline (getattr(settings, X, défaut) pour les
    paramètres ``settings`` ; os.getenv pour les paramètres ``env`` import-time). Aucune
    seconde liste : les défauts proviennent du registre unique.
    """
    from django.conf import settings

    out: dict[str, JSONScalar] = {}
    env_effective = _rewrite_env_effective()
    for p in result_params(schema_version):
        if p.source is ParamSource.ENV:
            # Valeur RÉELLEMENT utilisée par le pipeline (constante résolue à l'import
            # dans rewrite.py), pas une re-dérivation : override_settings ne l'affecte
            # pas, le gel la VÉRIFIE.
            out[p.name] = _norm(env_effective.get(p.name, p.default))
        else:
            out[p.name] = _norm(getattr(settings, p.name, p.default))
    return out


#: Réglages de catégorie `_E` : résolus par `os.getenv` À L'IMPORT de `rewrite.py`,
#: donc HORS de portée de `settings` et de `override_settings`. La correspondance
#: nom-de-réglage -> attribut-de-module est déclarée ICI, une seule fois : le lecteur
#: d'effectif ci-dessous s'en sert, et les tests de parité s'en servent pour poser
#: l'environnement de campagne. Dupliquer cette table ailleurs, c'est se donner deux
#: chances de la laisser diverger.
REWRITE_ENV_ATTRS: dict[str, tuple[str, Any]] = {
    "RAG_QUERY_REWRITE_ENABLED": ("QUERY_REWRITE_ENABLED", False),
    "RAG_KEYWORDIZE_ENABLED": ("KEYWORDIZE_ENABLED", True),
    "RAG_QUERY_REWRITE_PROVIDER": ("REWRITE_PROVIDER", "ollama"),
    "RAG_QUERY_REWRITE_MODEL": ("REWRITE_MODEL", ""),
}


def _rewrite_env_effective() -> dict[str, Any]:
    """Constantes de réécriture réellement résolues à l'import (source unique)."""
    try:
        from apps.rag_core.services.query import rewrite as _rw
    except Exception:  # pragma: no cover - rewrite toujours importable en prod
        return {}
    out: dict[str, Any] = {}
    for name, (attr, fallback) in REWRITE_ENV_ATTRS.items():
        value = getattr(_rw, attr, fallback)
        out[name] = bool(value) if isinstance(fallback, bool) else value
    return out


# ---------------------------------------------------------------------------
# Identité d'embedding (schéma v3)
# ---------------------------------------------------------------------------
# Pourquoi un bloc STRUCTURÉ plutôt que cinq clés de plus dans `values`.
#
# Les réglages ci-dessus disent ce que la configuration DEMANDE. Ils ne disent
# pas ce qui a effectivement produit les vecteurs : l'identité d'un RagSpace vit
# en BASE (`RagSpace.embedding_provider/model`), et la migration du 23/08/2026 a
# changé un espace de production par `set_ragspace_embedding_pipeline`, c'est-à-
# dire par un `UPDATE` — sans toucher au moindre réglage. Un sceau qui ne lit que
# les settings serait resté identique pendant que tous les vecteurs changeaient.
#
# Ce bloc porte donc DEUX niveaux, et dit lequel par son champ `source` :
#   - `registry` / `settings` : l'identité DEMANDÉE, dérivable sans base ;
#   - `deployed`              : + les familles de pipeline RÉELLEMENT observées
#                               sur les chunks indexés.
#
# La comparabilité de deux vecteurs est décidée par `pipeline_family` — on
# réutilise donc `llm_router.embedding_pipeline`, source de vérité déjà en place,
# plutôt que d'inventer un second calcul qui divergerait.

EMBEDDING_IDENTITY_REGISTRY = "registry"
EMBEDDING_IDENTITY_SETTINGS = "settings"
EMBEDDING_IDENTITY_DEPLOYED = "deployed"

#: Clés du bloc, dans l'ordre où elles sont documentées. La sérialisation trie de
#: toute façon : cette liste sert à VALIDER qu'aucune clé n'apparaît ou ne
#: disparaît en silence.
EMBEDDING_IDENTITY_KEYS: tuple[str, ...] = (
    "source",
    "provider",
    "model",
    "dimension",
    "gateway_embeddings_enabled",
    "gateway_embeddings_model",
    "pipeline_family",
    "observed_pipeline_families",
)


def _embedding_identity(
    *,
    source: str,
    provider: str,
    model: str,
    dimension: int,
    gateway_enabled: bool,
    gateway_model: str,
    observed: list[str] | None = None,
) -> dict[str, Any]:
    """Bloc d'identité canonique. Aucune valeur inventée, aucun secret."""
    from apps.llm_router.embedding_pipeline import legacy_pipeline_id, pipeline_family

    # `pipeline_family` de l'identité DEMANDÉE. Sur le chemin gouverné, la vraie
    # famille est rendue par la Gateway appel par appel : elle n'est pas dérivable
    # d'un réglage, et la reconstruire serait précisément le mensonge que
    # `embedding_pipeline` interdit. On scelle alors ce que la configuration
    # demande, et `observed_pipeline_families` porte ce qui a réellement servi.
    famille = pipeline_family(
        legacy_pipeline_id(provider=provider, model=model, dimension=dimension)
    )
    return {
        "source": source,
        "provider": provider,
        "model": model,
        "dimension": int(dimension),
        "gateway_embeddings_enabled": bool(gateway_enabled),
        "gateway_embeddings_model": gateway_model,
        "pipeline_family": famille,
        # `None` (et non `[]`) : « non observé » et « observé, aucune famille »
        # sont deux faits différents, et les confondre ferait porter le même
        # sceau à un environnement vide et à un environnement non interrogé.
        "observed_pipeline_families": sorted(observed) if observed is not None else None,
    }


def embedding_identity_from_values(
    values: Mapping[str, JSONScalar],
    *,
    source: str,
    observed: list[str] | None = None,
) -> dict[str, Any]:
    """Identité dérivée d'un jeu de valeurs déjà résolu (instantané, gel relu).

    Sert au cas où la configuration a été MESURÉE ailleurs — un instantané
    `dump_effective_config` produit sur l'hôte cible, par exemple. On ne relit
    alors pas les settings locaux : ce serait sceller la machine qui calcule, pas
    celle qui a été mesurée.
    """
    def _v(name: str) -> Any:
        return values[name] if name in values else _BY_NAME[name].default

    return _embedding_identity(
        source=source,
        provider=str(_v("DEFAULT_RAGSPACE_EMBEDDING_PROVIDER")),
        model=str(_v("DEFAULT_RAGSPACE_EMBEDDING_MODEL")),
        dimension=int(_v("RAG_EMBEDDING_DIM")),
        gateway_enabled=bool(_v("AI_GATEWAY_EMBEDDINGS_ENABLED")),
        gateway_model=str(_v("AI_GATEWAY_EMBEDDINGS_MODEL")),
        observed=observed,
    )


def default_embedding_identity() -> dict[str, Any]:
    """Identité dérivée des DÉFAUTS du registre — pure, sans settings ni base."""
    defaults = {p.name: p.default for p in REGISTRY}
    return _embedding_identity(
        source=EMBEDDING_IDENTITY_REGISTRY,
        provider=str(defaults["DEFAULT_RAGSPACE_EMBEDDING_PROVIDER"]),
        model=str(defaults["DEFAULT_RAGSPACE_EMBEDDING_MODEL"]),
        dimension=int(defaults["RAG_EMBEDDING_DIM"]),
        gateway_enabled=bool(defaults["AI_GATEWAY_EMBEDDINGS_ENABLED"]),
        gateway_model=str(defaults["AI_GATEWAY_EMBEDDINGS_MODEL"]),
    )


def observed_pipeline_families() -> list[str]:
    """Familles de pipeline RÉELLEMENT présentes sur les chunks indexés.

    C'est le seul niveau qui voit une migration d'embedding faite en base. Les
    chunks antérieurs à la migration ``0038`` portent ``NULL`` : cette absence est
    un fait, représentée par ``UNSET`` plutôt qu'écartée en silence.

    Accès base : réservé aux appelants qui en disposent (commandes de gestion).
    """
    from apps.rag_core.models import Chunk

    familles = set(
        Chunk.objects.order_by()
        .values_list("pipeline_family", flat=True)
        .distinct()
    )
    out = {f for f in familles if f}
    if len(out) != len(familles):
        out.add(UNSET)
    return sorted(out)


def resolve_effective_embedding_identity(*, observe: bool = False) -> dict[str, Any]:
    """Identité dérivée des SETTINGS effectifs ; base interrogée si ``observe``.

    ``observe=False`` par défaut : la résolution reste utilisable sans base (tests,
    manifeste). L'appelant qui dispose d'une base demande explicitement le niveau
    ``deployed`` — il n'y a pas de repli silencieux, un sceau dont le niveau
    dépendrait de la disponibilité d'une base ne voudrait rien dire.
    """
    from django.conf import settings

    def _s(name: str) -> Any:
        return getattr(settings, name, _BY_NAME[name].default)

    return _embedding_identity(
        source=EMBEDDING_IDENTITY_DEPLOYED if observe else EMBEDDING_IDENTITY_SETTINGS,
        provider=str(_s("DEFAULT_RAGSPACE_EMBEDDING_PROVIDER")),
        model=str(_s("DEFAULT_RAGSPACE_EMBEDDING_MODEL")),
        dimension=int(_s("RAG_EMBEDDING_DIM")),
        gateway_enabled=bool(_s("AI_GATEWAY_EMBEDDINGS_ENABLED")),
        gateway_model=str(_s("AI_GATEWAY_EMBEDDINGS_MODEL")),
        observed=observed_pipeline_families() if observe else None,
    )


@dataclass(frozen=True)
class FrozenRetrievalConfig:
    """Configuration de retrieval GELÉE, canonique, versionnée et hashable."""

    schema_version: int
    values: Mapping[str, JSONScalar]
    #: Identité d'embedding — REQUISE en v3, INTERDITE avant. Les sceaux v1/v2
    #: existants restent donc calculables à l'octet près : leur charge utile ne
    #: porte pas la clé, et le JSON canonique ne l'ajoute pas.
    embedding_identity: Mapping[str, Any] | None = None

    def canonical_json(self) -> str:
        """Sérialisation DÉTERMINISTE : clés triées, séparateurs canoniques, UTF-8."""
        payload: dict[str, Any] = {
            "schema_version": int(self.schema_version),
            "values": {k: self.values[k] for k in sorted(self.values)},
        }
        if self.schema_version >= FREEZE_SCHEMA_V3:
            identity = dict(self.embedding_identity or {})
            payload["embedding_identity"] = {k: identity[k] for k in sorted(identity)}
        return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    def config_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def validate(self) -> None:
        """Rejette clés inconnues / manquantes / valeurs non-JSON-scalaires.

        Le jeu de clés attendu dépend du SCHÉMA porté par l'instance : une config v1
        validée contre la définition v2 serait déclarée incomplète, ce qui est
        exactement le diagnostic voulu si quelqu'un mélange les deux.
        """
        known = {p.name for p in result_params(self.schema_version)}
        keys = set(self.values)
        missing = sorted(known - keys)
        unexpected = sorted(keys - known)
        if missing:
            raise ValueError(f"FrozenRetrievalConfig: paramètres A manquants: {missing}")
        if unexpected:
            raise ValueError(f"FrozenRetrievalConfig: clés inconnues: {unexpected}")
        for k, v in self.values.items():
            if not (v is None or isinstance(v, (bool, int, float, str))):
                raise ValueError(f"FrozenRetrievalConfig: valeur non-JSON pour {k}: {type(v)}")
        self._validate_embedding_identity()

    def _validate_embedding_identity(self) -> None:
        """v3 EXIGE le bloc ; v1/v2 l'INTERDISENT.

        Le refus en v1/v2 n'est pas du zèle : accepter un bloc ignoré laisserait
        croire qu'une identité a été scellée alors que le hash ne la voit pas.
        """
        if self.schema_version < FREEZE_SCHEMA_V3:
            if self.embedding_identity:
                raise ValueError(
                    "FrozenRetrievalConfig: embedding_identity n'existe qu'à partir du "
                    f"schéma v{FREEZE_SCHEMA_V3} (reçu v{self.schema_version}) — un bloc "
                    "qui n'entre pas dans le hash ne scelle rien."
                )
            return
        if not self.embedding_identity:
            raise ValueError(
                f"FrozenRetrievalConfig: embedding_identity est REQUISE en v{FREEZE_SCHEMA_V3}."
            )
        keys = set(self.embedding_identity)
        attendues = set(EMBEDDING_IDENTITY_KEYS)
        if keys != attendues:
            raise ValueError(
                "FrozenRetrievalConfig: bloc embedding_identity non conforme — "
                f"manquantes: {sorted(attendues - keys)}, inconnues: {sorted(keys - attendues)}"
            )
        source = self.embedding_identity["source"]
        connues = (EMBEDDING_IDENTITY_REGISTRY, EMBEDDING_IDENTITY_SETTINGS, EMBEDDING_IDENTITY_DEPLOYED)
        if source not in connues:
            raise ValueError(f"FrozenRetrievalConfig: source d'identité inconnue: {source!r}")
        for k, v in self.embedding_identity.items():
            if k == "observed_pipeline_families":
                if not (v is None or (isinstance(v, list) and all(isinstance(x, str) for x in v))):
                    raise ValueError(f"FrozenRetrievalConfig: {k} doit être None ou une liste de str")
                continue
            if not (v is None or isinstance(v, (bool, int, float, str))):
                raise ValueError(
                    f"FrozenRetrievalConfig: valeur non-JSON pour embedding_identity.{k}: {type(v)}"
                )

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schema_version": int(self.schema_version),
            "config_hash": self.config_hash(),
            "values": {k: self.values[k] for k in sorted(self.values)},
        }
        if self.schema_version >= FREEZE_SCHEMA_V3:
            identity = dict(self.embedding_identity or {})
            out["embedding_identity"] = {k: identity[k] for k in sorted(identity)}
        return out


def default_frozen_values(
    schema_version: int = CURRENT_FREEZE_SCHEMA,
) -> dict[str, JSONScalar]:
    """Valeurs gelées par DÉFAUT = défauts du REGISTRE (pures, indépendantes de l'env).

    Utiliser le registre (et non ``resolve_effective``) rend la config gelée
    DÉTERMINISTE et son hash STABLE d'un environnement à l'autre : le manifeste est une
    fonction pure de (registre, overrides calibrés).
    """
    return {p.name: _norm(p.default) for p in result_params(schema_version)}


def build_frozen_config(
    overrides: Mapping[str, Any] | None = None,
    *,
    schema_version: int = CURRENT_FREEZE_SCHEMA,
) -> FrozenRetrievalConfig:
    """Construit la config gelée = défauts du registre + overrides calibrés.

    Les ``overrides`` (ex. valeurs calibrées P2.5-b) écrasent le défaut de catégorie A
    pour les clés concernées. Déterministe ; validé (exhaustif, JSON-scalaire).

    ``schema_version`` choisit la DÉFINITION du pipeline. Une campagne historique
    épingle la sienne ; une nouvelle prend le schéma courant.
    """
    values = default_frozen_values(schema_version)
    for k, v in (overrides or {}).items():
        if k not in _BY_NAME:
            raise ValueError(f"override d'un paramètre HORS registre: {k!r}")
        spec = _BY_NAME[k]
        if spec.category is not ParamCategory.RESULT:
            raise ValueError(f"override d'un paramètre NON-A: {k!r}")
        if not spec.in_schema(schema_version):
            # Surcharger un paramètre absent du schéma produirait une config dont le
            # hash ne dépend pas de la valeur surchargée : un réglage qu'on croirait
            # gelé, et qui ne le serait pas.
            raise ValueError(
                f"override de {k!r}, absent du schéma de gel v{schema_version} "
                f"(présent dans: {list(spec.schemas)})"
            )
        values[k] = _norm(v)
    # v3 : le manifeste est une fonction PURE de (registre, overrides). Son
    # identité d'embedding vient donc des défauts du registre, pas des settings —
    # sans quoi le hash d'un manifeste dépendrait de la machine qui le calcule.
    identity = default_embedding_identity() if schema_version >= FREEZE_SCHEMA_V3 else None
    cfg = FrozenRetrievalConfig(
        schema_version=schema_version, values=values, embedding_identity=identity
    )
    cfg.validate()
    return cfg


# ---------------------------------------------------------------------------
# Manifeste de la mission P2.6 : overrides calibrés + hash attendu (gelé en code).
# Toute modification du registre ou des overrides change le hash -> NO-RUN tant que le
# hash attendu n'est pas régénéré volontairement (recalibration explicite).
# ---------------------------------------------------------------------------
#: Divergences CONNUES entre le défaut déclaré ici et le défaut du registre de
#: configuration (`config.registry`), relevées le 22/08/2026.
#:
#: Ce ne sont pas des erreurs d'inattention : ce sont, très probablement, des VALEURS
#: DE CAMPAGNE écrites au mauvais endroit — la même confusion défaut/override que celle
#: corrigée ce jour-là pour `RAG_QUERY_REWRITE_ENABLED`. Tant qu'elles vivent dans le
#: DÉFAUT, le gel affirme du code des choses fausses, et le contrôle « effectif == gelé »
#: ne peut passer que par accident d'environnement.
#:
#: Elles ne sont PAS corrigées ici : chacune demande de vérifier que le déplacement vers
#: MISSION_CALIBRATED_OVERRIDES préserve bien le sceau, et de trancher si le défaut du
#: registre de configuration a changé APRÈS la campagne (auquel cas la valeur gelée est
#: la bonne, mal rangée) ou si le gel s'est trompé (auquel cas le sceau est en cause).
#:
#: L'inventaire est FIGÉ et vérifié par un test : la liste ne peut ni s'allonger en
#: silence, ni se raccourcir sans que quelqu'un l'écrive.
KNOWN_REGISTRY_DEFAULT_DIVERGENCES: frozenset[str] = frozenset({
    "RAG_HYBRID_ENABLED",
    "RAG_TOP_K",
    "RAG_VECTOR_TOP_K",
    "RAG_RETRIEVAL_DIVERSITY_ENABLED",
    "RAG_STRONG_SIGNALS_ENABLED",
    "RAG_RRF_CONDITIONAL_LEXICAL_WEIGHT_ENABLED",
    "RAG_QUERY_REWRITE_MAX_CHARS",
    "RAG_KEYWORDIZE_MAX_TOKENS",
    "RAG_QUERY_REWRITE_TIMEOUT_S",
})


MISSION_CALIBRATED_OVERRIDES: dict[str, JSONScalar] = {
    "RAG_HYBRID_ENABLED": True,
    "RAG_MIN_SCORE": 0.76,
    "RAG_MIN_TEXT_RANK": 0.0,
    "RAG_RERANK_ENABLED": False,
    "RAG_ANSWERABILITY_ENABLED": False,
    "RAG_RRF_K": 60,
    "RAG_FTS_SLASH_IDENTIFIERS_ENABLED": True,
    "RAG_RETRIEVAL_CANDIDATE_K": 96,
    "RAG_RETRIEVAL_MAX_PER_GROUP": 1,
    "RAG_EMBEDDING_DIM": 768,
    # Déplacé ici le 22/08/2026 depuis le DÉFAUT du ParamSpec (voir la note du sceau
    # plus bas). La campagne a tourné réécriture ACTIVE ; le défaut du code est INACTIF.
    "RAG_QUERY_REWRITE_ENABLED": True,
}


def mission_frozen_config() -> FrozenRetrievalConfig:
    """Config gelée canonique de la mission P2.6 (déterministe).

    ÉPINGLE le schéma **v1** : c'est la définition du pipeline selon laquelle cette
    campagne a été menée et son sceau calculé. Le schéma courant peut évoluer autant
    qu'il le faut — ce hash-ci, lui, ne bouge plus. C'est la propriété qui permet de
    dire d'un rapport ancien qu'il est encore vérifiable.
    """
    return build_frozen_config(MISSION_CALIBRATED_OVERRIDES, schema_version=FREEZE_SCHEMA_V1)


#: Hash ATTENDU (LITTÉRAL, gelé) de la config de la mission. Régénéré UNIQUEMENT lors
#: d'une recalibration délibérée (nouveau manifeste). Vérifié au run : tout écart —
#: registre modifié, override changé, défaut d'un paramètre A modifié — bloque AVANT
#: consommation. Ne PAS remplacer par un calcul dynamique (ce serait une tautologie).
#:
#: Régénéré le 26/07/2026 : le registre gagne DEUX paramètres de catégorie A
#: (`RAG_STRUCTURAL_NOISE_PENALTY_ENABLED`, `RAG_STRUCTURAL_NOISE_PENALTY_FACTOR`).
#: C'est bien l'acte délibéré que cette garde exige — et non une dérive : aucune valeur
#: CALIBRÉE n'a changé, et les deux nouveaux paramètres sont NEUTRES par défaut
#: (mécanisme désactivé). La configuration gelée décrit donc exactement le même
#: comportement de retrieval qu'auparavant ; seul le manifeste s'est enrichi.
#:
#: Régénéré le 29/07/2026 (repli de plan FTS partagé) : le registre gagne
#: `RAG_FTS_PLAN_MAX_ATTEMPTS=6`, qui ne tronque aucun plan réel (le plus long en
#: produit 6).
#:
#: Régénéré le 29/07/2026 (fusion bi-source unifiée) : le registre gagne
#: `RAG_BUSINESS_CORPUS_UNIFIED_FUSION_ENABLED=False`, qui laisse l'assemblage
#: bi-source historique en place.
#:
#: Régénéré le 30/07/2026 (pondération des legs RRF) : le registre gagne
#: `RAG_BUSINESS_CORPUS_RRF_LEG_WEIGHT=1.0`, sans effet tant que la fusion unifiée est
#: désactivée, et neutre même activée (1.0 = fusion non pondérée).
#:
#: Régénéré le 30/07/2026 (admission calibrée) : le registre gagne
#: `RAG_BUSINESS_CORPUS_CALIBRATED_ADMISSION_ENABLED=False` et
#: `RAG_BUSINESS_CORPUS_ADMISSION_PROBABILITY_THRESHOLD=0.65`. La porte étant
#: désactivée, le seuil n'est jamais lu : le comportement gelé est inchangé.
#:
#: Régénéré le 30/07/2026 (empreinte d'indexation corpus) : le registre gagne
#: `RAG_BUSINESS_CORPUS_EMBEDDING_FINGERPRINT_ENFORCEMENT_ENABLED=False`. Désactivé,
#: le filtre ne retire aucun document : le comportement gelé est inchangé.
#:
#: Régénéré le 30/07/2026 (invariant d'au moins une source) : le registre gagne
#: `RAG_MIN_FINAL_SOURCE_CHARS=400`. Il ne s'applique QUE lorsque le budget est saturé
#: au point de ne plus laisser passer qu'une source — cas où l'ancien code rendait un
#: prompt sans aucune source.
#:
#: Régénéré le 23/08/2026 (plancher de sources face à la mémoire) : le registre gagne
#: `RAG_MIN_PROMPT_SOURCES=4`. Il réserve la place de N sources avant d'allouer ce qui
#: reste à la mémoire de conversation. NEUTRE pour toute campagne : l'arbitrage n'existe
#: que si une mémoire est fournie, et l'outillage de banc n'en passe aucune — le chemin
#: est simplement sauté. Le comportement gelé est donc identique.
#:
#: Dans tous les cas : paramètre de catégorie A, NEUTRE par défaut, aucune valeur
#: CALIBRÉE modifiée. Le comportement gelé est identique ; seul le manifeste s'enrichit.
#:
#: NON régénéré le 22/08/2026 (alignement du défaut de `RAG_QUERY_REWRITE_ENABLED`) :
#: le registre déclarait `True` en DÉFAUT alors que `rewrite.py:39` résout `False` en
#: l'absence d'environnement. Le gel affirmait donc du code une chose fausse, et le
#: contrôle « gelé == effectif » échouait partout où la variable n'était pas posée.
#: La valeur `True` n'a pas disparu : elle a changé de NATURE — de défaut supposé du
#: code à choix explicite de campagne (MISSION_CALIBRATED_OVERRIDES). Les valeurs
#: gelées finales sont IDENTIQUES au bit près, donc ce hash aussi : il n'est pas
#: régénéré, il est PRÉSERVÉ. C'est la propriété qui rend l'opération sûre — un
#: rapport P2.6 antérieur reste vérifiable contre ce littéral.
MISSION_EXPECTED_CONFIG_HASH = (
    "293004fc5659775ec7872e45df42c894412b17ceb3b3fddfac69ed52581e235d"
)


def compute_config_drift(
    expected: Mapping[str, JSONScalar], effective: Mapping[str, JSONScalar]
) -> dict[str, Any]:
    """Diff structuré et exploitable entre config attendue (gelée) et effective.

    ``missing`` : clés attendues absentes de l'effectif. ``unexpected`` : clés effectives
    hors gel. ``changed`` : {clé: {expected, effective}}. Vide -> aucune dérive.
    """
    exp_keys, eff_keys = set(expected), set(effective)
    missing = sorted(exp_keys - eff_keys)
    unexpected = sorted(eff_keys - exp_keys)
    changed = {
        k: {"expected": expected[k], "effective": effective[k]}
        for k in sorted(exp_keys & eff_keys)
        # Comparaison de TYPE volontaire : distingue False de 0 (même égalité, type
        # différent) — un changement de type est une dérive (cf. §11).
        if expected[k] != effective[k] or type(expected[k]) is not type(effective[k])  # noqa: E721
    }
    return {"missing": missing, "unexpected": unexpected, "changed": changed}


def has_blocking_drift(drift: Mapping[str, Any]) -> bool:
    return bool(drift.get("missing") or drift.get("unexpected") or drift.get("changed"))


# ---------------------------------------------------------------------------
# Garde d'exhaustivité : tout RAG_* LU par le pipeline DOIT être classé.
# ---------------------------------------------------------------------------
# La découverte se fait par ANALYSE SYNTAXIQUE, pas par recherche de motif dans le
# texte. Ce n'est pas un raffinement : le balayage textuel s'est trompé dans les DEUX
# sens, et chaque sens a coûté quelque chose.
#
#   - FAUX POSITIFS — quatre noms cités dans des commentaires (dont `RAG_RRF_K` et
#     `RAG_ENABLE_NEWS_HEURISTIC`, deux réglages que plus personne ne lit) sont entrés
#     dans la configuration gelée, et l'un d'eux porte même une valeur « calibrée »
#     dans le manifeste de P2.6. Le sceau couvrait donc des réglages sans effet.
#   - FAUX NÉGATIFS — les sept réglages du reranking AI Gateway n'ont JAMAIS été vus,
#     parce que l'orchestrateur passe par des fonctions (`reranker_enabled()`,
#     `max_per_group()`) au lieu de nommer les réglages, et que le module client
#     n'était pas balayé. Un chemin qui change l'ordre des sources échappait au gel.
#
# Conséquence opérationnelle du modèle textuel : ÉCRIRE un nom dans un commentaire
# suffisait à le rendre « sensible au pipeline ». Un lot antérieur en a fait
# l'expérience — une phrase d'explication a fait rougir la garde d'exhaustivité.
#
# Le modèle ci-dessous ne reconnaît que des LECTURES réelles :
#     settings.X · getattr(settings, "X", …) · setting*("X") · os.getenv("X")
#     os.environ["X"] · helper("…", "X") pour les lecteurs indirects déclarés
# Un nom dans une chaîne de documentation, un commentaire ou un message d'erreur
# n'est pas une lecture, et n'entre pas.

#: Modules dont les lectures composent le pipeline de retrieval. `rerank_gateway`
#: y figure désormais : c'est lui qui lit les réglages du chemin Gateway, que
#: l'orchestrateur ne fait qu'appeler.
_PIPELINE_FILES = (
    "rag_core/services/query/orchestrator.py",
    "rag_core/services/query/candidate_retriever.py",
    "rag_core/services/query/candidate_reranker.py",
    "rag_core/services/query/prompt_builder.py",
    "rag_core/services/query/rewrite.py",
    "rag_core/services/query/space_policy.py",
    "rag_core/services/query/strong_signals.py",
    "rag_core/services/query/temporal_candidates.py",
    "rag_core/services/query/lowconf.py",
    "rag_core/services/query/heuristics.py",
    "rag_core/services/query/diversify.py",
    "rag_core/services/query/merge.py",
    "rag_core/services/rerank_gateway.py",
    # Le SQL du leg lexical et du leg vectoriel : c'est ici que se décide le chemin
    # `to_tsvector` recalculé contre colonne générée, et la configuration FTS.
    "rag_core/repositories/chunk_repository.py",
    "rag_core/domain/policies.py",
    "rag_core/domain/retrieval.py",
    # Corpus métier : même fusion, autre application.
    "corpus/services/retrieval.py",
)

#: Fonctions dont le PREMIER argument littéral est un nom de réglage lu.
_DIRECT_READERS: frozenset[str] = frozenset({
    "setting", "setting_int", "setting_float", "setting_bool",
    "get_registered_setting", "env_value", "getenv",
})

#: Lecteurs INDIRECTS du dépôt : le nom du réglage voyage en argument, et la lecture
#: a lieu dans la fonction appelée. Sans cette liste, deux drapeaux d'audit
#: réellement lus (`RAG_QUERY_LOG_STORE_*`) sortiraient du registre — un faux négatif
#: introduit par la correction elle-même. La position de l'argument est explicite.
_INDIRECT_READERS: dict[str, int] = {
    "_minimize_log_text": 1,  # _minimize_log_text(texte, "RAG_QUERY_LOG_STORE_…")
}

#: Clés RAG_* lues mais NON influentes pour le holdout, volontairement hors registre
#: (audit interne, constantes de nom). Toute exclusion est explicite et minimale.
_IGNORED_KEYS: frozenset[str] = frozenset({
    "RAG_QUERY_LOG_STORE_QUESTION_DEFAULT",  # placeholder, jamais présent
})

_SETTING_NAME_RE = re.compile(r"^RAG_[A-Z0-9_]+$")


def _apps_dir() -> Path:
    """Racine ``backend/apps`` — le pipeline déborde de ``rag_core``.

    Le corpus métier vit dans une autre application et participe à la même fusion :
    limiter le balayage à `rag_core` reviendrait à décréter qu'un réglage lu par
    `apps/corpus` ne peut pas changer un résultat, ce qui est faux.
    """
    # .../apps/rag_core/benchmark/holdout_config.py -> .../apps
    return Path(__file__).resolve().parents[2]


def _read_names_in_module(tree: ast.AST) -> set[str]:
    """Noms de réglages RÉELLEMENT lus dans un module analysé."""
    found: set[str] = set()

    def _keep(value: Any) -> None:
        if isinstance(value, str) and _SETTING_NAME_RE.match(value):
            found.add(value)

    for node in ast.walk(tree):
        # settings.RAG_X
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "settings":
                _keep(node.attr)
            continue
        # os.environ["RAG_X"]
        if isinstance(node, ast.Subscript):
            target = node.value
            if (
                isinstance(target, ast.Attribute)
                and target.attr == "environ"
                and isinstance(node.slice, ast.Constant)
            ):
                _keep(node.slice.value)
            continue
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        fname = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if fname is None:
            continue
        # getattr(settings, "RAG_X", défaut)
        if (
            fname == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "settings"
            and isinstance(node.args[1], ast.Constant)
        ):
            _keep(node.args[1].value)
            continue
        # setting("RAG_X") / os.getenv("RAG_X") / …
        if fname in _DIRECT_READERS and node.args and isinstance(node.args[0], ast.Constant):
            _keep(node.args[0].value)
            continue
        # helper(…, "RAG_X") pour les lecteurs indirects déclarés
        position = _INDIRECT_READERS.get(fname)
        if position is not None and len(node.args) > position:
            arg = node.args[position]
            if isinstance(arg, ast.Constant):
                _keep(arg.value)
    return found


def scan_pipeline_settings() -> set[str]:
    """Réglages ``RAG_*`` réellement LUS par les modules du pipeline.

    Analyse syntaxique : un nom cité dans un commentaire, une docstring ou un message
    n'est pas une lecture. Voir le commentaire d'en-tête pour ce que le balayage
    textuel précédent a manqué et inventé.
    """
    apps_dir = _apps_dir()
    found: set[str] = set()
    for rel in _PIPELINE_FILES:
        path = apps_dir / rel
        if not path.exists():
            continue
        found |= _read_names_in_module(ast.parse(path.read_text(encoding="utf-8")))
    return found


def check_registry_exhaustiveness() -> list[str]:
    """Retourne les clés RAG_* lues par le pipeline mais ABSENTES du registre.

    Non vide -> un nouveau paramètre influent n'a pas été classé : le test associé doit
    échouer (rouge) tant qu'il n'est pas ajouté au registre avec sa catégorie.
    """
    scanned = scan_pipeline_settings()
    registered = {p.name for p in REGISTRY} | _IGNORED_KEYS
    return sorted(k for k in scanned if k not in registered)
