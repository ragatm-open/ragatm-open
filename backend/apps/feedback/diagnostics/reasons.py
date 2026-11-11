"""Vocabulaire STABLE des raisons d'exclusion d'un candidat.

Ces valeurs sont un contrat : elles sont écrites dans le tampon, promues dans le
snapshot, exportées dans les paquets d'incident et lues par le replay. Les
renommer casse la lecture des incidents déjà capturés.

RÈGLE ABSOLUE : ne jamais inventer une raison. Quand l'étape qui a écarté un
candidat n'est pas connue avec certitude, la valeur est ``unknown``. Une raison
plausible mais fausse enverrait la correction dans la mauvaise direction — c'est
pire que pas de raison du tout.
"""
from __future__ import annotations

#: Le candidat a été retenu dans le contexte final envoyé au modèle.
SELECTED = "selected"

#: Sous le seuil de score (lexical ou vectoriel).
#: ⚠️ NON ÉMIS à ce jour : le filtrage par seuil a lieu dans le
#: `CandidateRetriever`, en amont du point de capture. Réservé pour une
#: instrumentation plus profonde ; voir la limite documentée.
BELOW_THRESHOLD = "below_threshold"

#: Pénalisé comme bruit structurel (question ou titre du corpus).
STRUCTURAL_PENALTY = "structural_penalty"

#: Doublon éliminé par la déduplication.
DUPLICATE = "duplicate"

#: Écarté par le plafond de diversité (documents/feuilles/groupes de colonnes).
DIVERSITY_MAX_PER_GROUP = "diversity_max_per_group"

#: Présent dans la sélection avant reranking, absent après.
RERANK_CUT = "rerank_cut"

#: Retenu par le ranking mais évincé par le budget de caractères du prompt.
PROMPT_BUDGET = "prompt_budget"

#: Écarté par le cloisonnement (périmètre owner, ACL, corpus non autorisé).
FILTERED_BY_SCOPE = "filtered_by_scope"

#: Type de source non exploitable par l'étape de génération.
UNSUPPORTED_SOURCE = "unsupported_source"

#: Simplement au-delà du `top_k` final — classé, mais pas assez haut.
NOT_SELECTED_TOP_K = "not_selected_top_k"

#: Étape d'exclusion INCONNUE. Valeur par défaut, jamais un pis-aller honteux.
UNKNOWN = "unknown"

ALL: tuple[str, ...] = (
    SELECTED,
    BELOW_THRESHOLD,
    STRUCTURAL_PENALTY,
    DUPLICATE,
    DIVERSITY_MAX_PER_GROUP,
    RERANK_CUT,
    PROMPT_BUDGET,
    FILTERED_BY_SCOPE,
    UNSUPPORTED_SOURCE,
    NOT_SELECTED_TOP_K,
    UNKNOWN,
)
