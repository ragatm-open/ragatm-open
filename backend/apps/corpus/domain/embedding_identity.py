"""Identité d'embeddings d'un corpus métier — SOURCE DE VÉRITÉ UNIQUE.

Module PUR : aucune dépendance Django, aucun accès base, aucun appel réseau. Il ne
connaît que des chaînes, une dimension et une empreinte.

## Ce que ce module empêche

Une identité d'embeddings est le couple (provider, modèle) **plus** la dimension
effectivement demandée au provider. Deux vecteurs ne sont comparables que si ces trois
éléments coïncident. Mesuré sur cette installation, une comparaison croisée ne lève
AUCUNE erreur : elle rend une page pleine de candidats, des scores plausibles
(0,512–0,522, à l'intérieur de la plage légitime des négatifs) et un contenu quasi
aléatoire (0 à 1 chunk pertinent sur 10). C'est le pire mode de défaillance possible :
crédible et faux.

Le côté privé se protège déjà ainsi (empreinte par document + `reindex_required` +
filtre au retrieval). Ce module apporte la même garantie au corpus métier.

## Pourquoi un défaut métier centralisé plutôt qu'une exigence explicite

L'API de création acceptait `embedding_model=""` et l'écrivait tel quel : le défaut de
champ Django (`text-embedding-3-small`) était court-circuité, et l'interface — qui
n'envoie que domaine, nom et slug — créait des corpus dont le leg vectoriel était mort
d'avance. Exiger le modèle dans la requête aurait cassé l'interface existante ; on
applique donc un défaut **métier**, explicite et unique, comme le fait déjà `RagSpace`
avec `default_ragspace_embedding_*`. Un défaut dupliqué n'est pas un défaut : c'est une
divergence en attente — d'où ce module unique.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

#: Providers d'embeddings admis pour un corpus métier. Aligné sur les `choices` du
#: modèle et sur `ALLOWED_EMBEDDING_PROVIDERS` côté privé.
ALLOWED_CORPUS_EMBEDDING_PROVIDERS: frozenset[str] = frozenset({"ai_gateway"})

#: Provider par défaut d'un corpus métier (édition Open : pipeline gouverné, comme les
#: RagSpaces — aucun fournisseur externe n'existe).
DEFAULT_CORPUS_EMBEDDING_PROVIDER = "ai_gateway"

#: Modèle (profil LOGIQUE du registre Gateway) par défaut PAR provider. Aucune autre
#: table de défauts ne doit exister.
DEFAULT_CORPUS_EMBEDDING_MODEL: dict[str, str] = {
    "ai_gateway": "legacy-768",
}

#: Version du pipeline d'embeddings entrant dans l'empreinte. À incrémenter si la
#: façon de produire un vecteur change sans que provider/modèle/dimension changent
#: (prétraitement, normalisation, paramètres d'appel). Les vecteurs produits par deux
#: versions différentes ne sont pas réputés comparables.
EMBEDDING_PIPELINE_VERSION = 1

#: Codes d'erreur STABLES, consommés par l'API, la trace et l'exploitation.
CORPUS_EMBEDDING_MODEL_REQUIRED = "corpus_embedding_model_required"
CORPUS_EMBEDDING_PROVIDER_INVALID = "corpus_embedding_provider_invalid"
CORPUS_EMBEDDING_MODEL_INVALID = "corpus_embedding_model_invalid"
CORPUS_EMBEDDING_DIMENSION_INVALID = "corpus_embedding_dimension_invalid"
CORPUS_EMBEDDING_CHANGE_REQUIRES_REINDEX = "corpus_embedding_change_requires_reindex"
CORPUS_EMBEDDING_IDENTITY_MISMATCH = "corpus_embedding_identity_mismatch"
CORPUS_EMBEDDING_FINGERPRINT_MISSING = "corpus_embedding_fingerprint_missing"
CORPUS_CALIBRATION_INVALIDATED = "corpus_calibration_invalidated"


class CorpusEmbeddingIdentityError(ValueError):
    """Identité d'embeddings invalide. Toujours une erreur, jamais un repli."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(f"{code}{': ' + detail if detail else ''}")


@dataclass(frozen=True)
class EmbeddingIdentity:
    """(provider, modèle, dimension) — le triplet qui rend deux vecteurs comparables."""

    provider: str
    model: str
    dimension: int

    def __post_init__(self) -> None:
        if self.provider not in ALLOWED_CORPUS_EMBEDDING_PROVIDERS:
            raise CorpusEmbeddingIdentityError(
                CORPUS_EMBEDDING_PROVIDER_INVALID, f"provider={self.provider!r}"
            )
        if not self.model:
            raise CorpusEmbeddingIdentityError(CORPUS_EMBEDDING_MODEL_REQUIRED)
        if not _is_plausible_model_name(self.model):
            raise CorpusEmbeddingIdentityError(
                CORPUS_EMBEDDING_MODEL_INVALID, f"model={self.model!r}"
            )
        if not isinstance(self.dimension, int) or self.dimension <= 0:
            raise CorpusEmbeddingIdentityError(
                CORPUS_EMBEDDING_DIMENSION_INVALID, f"dimension={self.dimension!r}"
            )

    @property
    def fingerprint(self) -> str:
        """Empreinte STABLE du triplet + version de pipeline. Aucun secret n'y entre.

        Sert à comparer deux identités en un seul champ indexé, et à repérer une
        divergence de pipeline invisible dans le seul couple (provider, modèle).
        """
        payload = f"v{EMBEDDING_PIPELINE_VERSION}|{self.provider}|{self.model}|{self.dimension}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def matches(self, provider: str | None, model: str | None, dimension: int | None) -> bool:
        return (
            normalize_provider(provider) == self.provider
            and normalize_model(model) == self.model
            and int(dimension or 0) == self.dimension
        )

    def __str__(self) -> str:  # pragma: no cover - confort de journalisation
        return f"{self.provider}/{self.model}@{self.dimension}"


def _is_plausible_model_name(name: str) -> bool:
    """Refuse les noms manifestement invalides sans prétendre connaître le catalogue.

    On ne peut pas valider qu'un modèle EXISTE chez un provider sans l'appeler ; on
    refuse donc seulement ce qui ne peut pas être un nom de modèle — et surtout le
    placeholder ``default``, qui provoquait un 404 `model_not_found` chez OpenAI.
    """
    if name.strip().lower() in {"default", "none", "null", "auto"}:
        return False
    return 1 <= len(name) <= 128 and " " not in name


def normalize_provider(provider: str | None) -> str:
    return (provider or "").strip().lower()


def normalize_model(model: str | None) -> str:
    return (model or "").strip()


def default_model_for_provider(provider: str | None) -> str:
    """Modèle métier par défaut d'un provider. Lève si le provider est inconnu."""
    p = normalize_provider(provider)
    if p not in ALLOWED_CORPUS_EMBEDDING_PROVIDERS:
        raise CorpusEmbeddingIdentityError(CORPUS_EMBEDDING_PROVIDER_INVALID, f"provider={provider!r}")
    return DEFAULT_CORPUS_EMBEDDING_MODEL[p]


def resolve_creation_identity(
    *, provider: str | None, model: str | None, dimension: int
) -> EmbeddingIdentity:
    """Identité d'un corpus À LA CRÉATION — jamais vide, jamais implicite.

    Provider absent -> défaut métier. Modèle absent ou vide -> défaut du provider,
    RÉSOLU ICI et persisté. C'est ce qui empêche qu'une chaîne vide atteigne la base et
    laisse la résolution à des variables globales au moment du retrieval.
    """
    p = normalize_provider(provider) or DEFAULT_CORPUS_EMBEDDING_PROVIDER
    if p not in ALLOWED_CORPUS_EMBEDDING_PROVIDERS:
        raise CorpusEmbeddingIdentityError(CORPUS_EMBEDDING_PROVIDER_INVALID, f"provider={provider!r}")
    m = normalize_model(model) or default_model_for_provider(p)
    return EmbeddingIdentity(provider=p, model=m, dimension=int(dimension))


def identity_of_corpus(corpus, *, dimension: int) -> EmbeddingIdentity:
    """Identité DÉCLARÉE d'un corpus existant. Lève si elle est incomplète.

    Un corpus historique sans provider n'a pas d'identité : ce n'est pas un corpus mal
    configuré, c'est un corpus non vectorisé — l'appelant doit traiter ce cas avant.
    """
    return EmbeddingIdentity(
        provider=normalize_provider(getattr(corpus, "embedding_provider", "")),
        model=normalize_model(getattr(corpus, "embedding_model", "")),
        dimension=int(dimension),
    )


# ---------------------------------------------------------------------------
# États de cohérence (valeurs STABLES, exposées par l'API et la trace)
# ---------------------------------------------------------------------------
CONSISTENCY_CONSISTENT = "consistent"
CONSISTENCY_PARTIALLY_CONSISTENT = "partially_consistent"
CONSISTENCY_REINDEX_REQUIRED = "reindex_required"
CONSISTENCY_FINGERPRINT_MISSING = "fingerprint_missing"
CONSISTENCY_IDENTITY_MISMATCH = "identity_mismatch"
CONSISTENCY_NO_READY_DOCUMENTS = "no_ready_documents"


@dataclass(frozen=True)
class ConsistencyReport:
    """Décompte documentaire d'un corpus, et le verdict qui en découle."""

    ready: int = 0
    compatible: int = 0
    incompatible: int = 0
    unknown: int = 0
    reindex_required: int = 0

    @property
    def status(self) -> str:
        """Verdict CALCULÉ, jamais deviné, du plus structurel au plus circonstanciel."""
        if self.ready == 0:
            return CONSISTENCY_NO_READY_DOCUMENTS
        if self.compatible == self.ready:
            return CONSISTENCY_CONSISTENT
        if self.compatible == 0:
            if self.reindex_required == self.ready:
                return CONSISTENCY_REINDEX_REQUIRED
            if self.incompatible > 0:
                return CONSISTENCY_IDENTITY_MISMATCH
            return CONSISTENCY_FINGERPRINT_MISSING
        return CONSISTENCY_PARTIALLY_CONSISTENT

    @property
    def vector_retrieval_available(self) -> bool:
        """Le leg vectoriel a-t-il au moins un document sûr à interroger ?"""
        return self.compatible > 0

    @property
    def error_code(self) -> str | None:
        """Code d'erreur STABLE quand la situation en appelle un ; sinon ``None``."""
        status = self.status
        if status in (CONSISTENCY_CONSISTENT, CONSISTENCY_NO_READY_DOCUMENTS):
            return None
        if status == CONSISTENCY_IDENTITY_MISMATCH:
            return CORPUS_EMBEDDING_IDENTITY_MISMATCH
        if status == CONSISTENCY_FINGERPRINT_MISSING:
            return CORPUS_EMBEDDING_FINGERPRINT_MISSING
        return CORPUS_EMBEDDING_CHANGE_REQUIRES_REINDEX

    def to_dict(self) -> dict:
        return {
            "ready_document_count": int(self.ready),
            "embedding_consistent_document_count": int(self.compatible),
            "embedding_inconsistent_document_count": int(self.incompatible),
            "embedding_unknown_document_count": int(self.unknown),
            "embedding_reindex_required_document_count": int(self.reindex_required),
            "embedding_consistency_status": self.status,
            "embedding_consistency_error_code": self.error_code,
            "vector_retrieval_available": bool(self.vector_retrieval_available),
        }
