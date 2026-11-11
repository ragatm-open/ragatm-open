from __future__ import annotations

import hashlib
import logging
import os
import random
from typing import Any, List

import requests
from django.conf import settings

from .config_access import router_setting
from .embedding_errors import EmbeddingsError, GatewayEmbeddingsError
from .embedding_pipeline import TASK_DOCUMENT, TASK_QUERY, legacy_pipeline_id
from .embeddings_gateway import (
    PROVIDER_AI_GATEWAY,
    EmbeddingBatch,
    build_gateway_embeddings_request,
    embed_via_gateway,
)

logger = logging.getLogger(__name__)

# Noms d'opération d'egress pour les embeddings (§21 du lot). Stables : ils servent
# de clé de lecture dans `llm_egress_audit_event` et ne doivent pas être renommés
# sans migration de requêtes d'exploitation.
EGRESS_OPERATION_BY_TASK = {
    TASK_DOCUMENT: "embedding.document",
    TASK_QUERY: "embedding.query",
}
EGRESS_OPERATION_DEFAULT = "embedding"


def _egress_operation(task: str) -> str:
    return EGRESS_OPERATION_BY_TASK.get(task, EGRESS_OPERATION_DEFAULT)

# `EmbeddingsError` était défini ICI ; il vit désormais dans `embedding_errors` pour
# que le client Gateway puisse lever la même famille sans cycle d'import. Le nom reste
# exporté à l'identique : `from apps.llm_router.embeddings import EmbeddingsError`
# continue de fonctionner (tests compris).
__all__ = [
    "EmbeddingsError",
    "GatewayEmbeddingsError",
    "EmbeddingBatch",
    "PROVIDER_AI_GATEWAY",
    "TASK_DOCUMENT",
    "TASK_QUERY",
    "embed_texts",
    "embed_texts_with_identity",
    "resolve_space_embedding",
    "space_is_governed",
]


def _get_setting(name: str) -> Any:
    """Réglage DÉCLARÉ : settings d'abord, environnement ensuite.

    Le repli sur l'environnement est CONSERVÉ — plusieurs réglages du routeur ne
    sont volontairement pas projetés dans les settings. Ce qui change : le nom
    doit être déclaré au registre, et le défaut vient de là. Auparavant,
    n'importe quel nom écrit dans le code devenait un réglage réel mais invisible
    du registre, de la documentation et de la comparaison de parité.
    """
    return router_setting(name)


def _stub_embed(texts: List[str], dim: int) -> List[list[float]]:
    """Deterministic dev-only stub.

    Generates reproducible pseudo-embeddings from the text sha256.
    """
    out: List[list[float]] = []
    for t in texts:
        h = hashlib.sha256(t.encode("utf-8")).digest()
        seed = int.from_bytes(h[:8], "big")
        rng = random.Random(seed)
        out.append([rng.uniform(-1.0, 1.0) for _ in range(dim)])
    return out


def _generic_embeddings_model() -> str:
    """Valeur de EMBEDDINGS_MODEL, en ignorant le sentinel placeholder "default".

    ``config.settings.base`` initialise ``EMBEDDINGS_MODEL = os.getenv(..., "default")``.
    Ce "default" est un placeholder signifiant « laisser le provider décider » : il
    ne doit JAMAIS être envoyé tel quel à un provider (sinon le fournisseur répond
    404 model_not_found pour le modèle « default »).
    """
    m = (_get_setting("EMBEDDINGS_MODEL") or "").strip()
    return "" if m.lower() == "default" else m


def default_embedding_model_for_provider(provider: str) -> str:
    """Modèle d'embeddings par défaut d'un provider (sans secret).

    Édition Open : le seul provider réel est ``ai_gateway`` (profil LOGIQUE du registre
    de la Gateway, ``AI_GATEWAY_EMBEDDINGS_MODEL``). ``stub`` (tests) et toute autre
    valeur retombent sur ``EMBEDDINGS_MODEL`` quand il n'est pas le placeholder ``default``.
    """
    p = (provider or "").strip().lower()
    generic = _generic_embeddings_model()
    if p == PROVIDER_AI_GATEWAY:
        from .embeddings_gateway import gateway_embeddings_default_model

        return (gateway_embeddings_default_model() or "").strip() or generic or "legacy-768"
    return generic or "default"


def is_blank_text(t: Any) -> bool:
    """True si ``t`` est None ou une chaîne vide/blanche (espaces, tabs, sauts de ligne)."""
    return not (isinstance(t, str) and t.strip())


def filter_nonblank_texts(texts: List[Any]) -> List[str]:
    """Retourne uniquement les textes NON blancs (None/""/"  "/"\\n\\t" retirés).

    Utilitaire pour les appelants qui veulent pré-filtrer AVANT ``embed_texts``
    tout en gérant eux-mêmes l'alignement (ex. sauter le chunk correspondant).
    """
    return [t for t in (texts or []) if not is_blank_text(t)]


def space_is_governed(space) -> bool:
    """Un RagSpace est GOUVERNÉ si son provider d'embeddings est ``ai_gateway``.

    C'est le seul critère, et il est porté par l'espace — pas par une variable
    globale. Un espace gouverné voit sa recherche vectorielle filtrée strictement par
    famille de pipeline (§11) ; un espace historique garde son comportement actuel.
    """
    provider, _model = resolve_space_embedding(space)
    return provider == PROVIDER_AI_GATEWAY


def resolve_space_embedding(space) -> tuple[str | None, str | None]:
    """Provider/modèle d'embeddings d'un RagSpace (source de vérité de l'indexation).

    Renvoie ``(provider, model)`` en lisant ``space.embedding_provider`` /
    ``space.embedding_model``. Une valeur vide => ``None`` (l'appelant retombera
    alors sur le réglage global, utile pour les très anciens espaces non migrés).
    """
    provider = (getattr(space, "embedding_provider", "") or "").strip().lower() or None
    model = (getattr(space, "embedding_model", "") or "").strip() or None
    return provider, model


def _is_external_embedding_provider(provider: str) -> bool:
    """Ce provider fait-il sortir le contenu de l'infrastructure privée ?

    Délègue à la source de vérité unique (``egress.EXTERNAL_PROVIDERS``) plutôt que
    de tenir une seconde liste : `ai_gateway` et `ollama` sont des services PRIVÉS,
    et les confondre avec un provider externe bloquerait le chemin nominal.
    """
    from .egress import is_external_provider

    return is_external_provider(provider)


def _guard_external_embedding(
    *, provider: str, model: str, context: Any, operation: str, batch_size: int
) -> None:
    """Refuse ou autorise UN LOT d'embeddings externe, avant tout appel réseau.

    Le refus est AUDITÉ puis propagé : une sortie interdite doit laisser une trace,
    sinon elle n'est pas distinguable d'une sortie qui n'a jamais été tentée.
    """
    from .egress import enforce_external_egress
    from .egress_audit import record_egress_event

    try:
        enforce_external_egress(
            provider=provider, context=context, operation=operation, embedding=True
        )
    except Exception as denial:
        # `context` peut être None (fail-closed sans périmètre) : `record_egress_event`
        # ne lève jamais et tolère un contexte absent, l'événement reste écrit avec
        # les seules métadonnées disponibles.
        record_egress_event(
            context=context,
            requested_provider=provider,
            effective_provider="",
            model=model,
            gateway_attempted=False,
            status="denied",
            error_type=type(denial).__name__,
            prompt_chars=batch_size,
            embedding=True,
        )
        logger.warning(
            "embedding_egress_denied operation=%s provider=%s batch=%d reason=%s",
            operation, provider, batch_size, type(denial).__name__,
        )
        raise


def _audit_external_embedding(
    *, provider: str, model: str, context: Any, operation: str,
    batch_size: int, latency_ms: int, status: str, error_type: str = "",
) -> None:
    """Un lot externe -> UN événement d'egress (§18). Jamais un par texte.

    ``prompt_chars`` porte ici la TAILLE DU LOT (nombre de textes), pas un contenu :
    la colonne existe déjà et reste un simple compteur.
    """
    from .egress_audit import record_egress_event

    record_egress_event(
        context=context,
        requested_provider=provider,
        effective_provider=provider,
        model=model,
        gateway_attempted=False,
        status=status,
        error_type=error_type,
        latency_ms=latency_ms,
        prompt_chars=batch_size,
        embedding=True,
    )


def embed_texts_with_identity(
    texts: List[str],
    dim: int | None = None,
    *,
    provider: str | None = None,
    model: str | None = None,
    task: str = TASK_DOCUMENT,
    rag_space_id: Any = None,
    document_id: Any = None,
    context: "Any | None" = None,
) -> EmbeddingBatch:
    """Comme ``embed_texts``, mais rend AUSSI l'identité du pipeline (lot 3).

    C'est désormais l'unique implémentation : ``embed_texts`` en est une vue qui ne
    garde que les vecteurs. Il n'existe donc pas deux chemins d'encodage susceptibles
    de diverger.

    ``task`` (``document`` à l'indexation, ``query`` à la recherche) est transmise à
    TOUT chemin qui passe par la Gateway — gouverné comme historique. Seuls les
    chemins hors Gateway (stub ; historiquement les chemins directs) l'ignorent : leur
    protocole n'a pas de notion de tâche.

    Identité rendue selon le chemin emprunté :

    - ``ai_gateway`` : identité VÉRIFIÉE, rendue par la Gateway, jamais reconstruite ;
    - tout autre provider réel est REFUSÉ (édition Open : pas de chemin externe ni de
      chemin Ollama direct, qui produisaient une identité legacy sans digest) ;
    - ``stub`` : identité legacy sur le provider ``stub``, pour que les tests aient une
      famille cohérente entre question et documents sans jamais ressembler à du gouverné.

    ``context`` (``LlmExecutionContext``) porte la POLITIQUE D'EGRESS du périmètre.
    Il est obligatoire dès que le provider effectif est EXTERNE (aucun n'existe en édition Open) : sans
    lui, l'appel est refusé par ``MissingEgressContext`` avant tout octet réseau.
    Les providers internes (``ollama``, ``ai_gateway``, ``stub``) ne le requièrent
    pas — rien ne quitte l'infrastructure privée.

    C'est la correction du P0 relevé par le lot SPACE-AI-CONFIG-ROUTING : la
    politique portait déjà ``external_embedding_allowed`` et
    ``permits(provider, embedding=True)``, mais **aucun appelant ne les consultait**.
    La garde n'est donc pas une sécurité nouvelle ; c'est la mise en service de
    celle qui existait.
    """
    if not texts:
        return EmbeddingBatch(vectors=[], pipeline_id="", dimension=int(dim or 0), task=task)

    if dim is None:
        try:
            dim = int(_get_setting("RAG_EMBEDDING_DIM"))
        except Exception:
            dim = 768

    timeout_s = float(_get_setting("EMBEDDINGS_TIMEOUT_S"))
    global_provider = (_get_setting("EMBEDDINGS_PROVIDER") or "stub").strip().lower()

    # Escape hatch : le stub global prime (tests/dev déterministes). Inchangé.
    if global_provider == "stub":
        effective_model = (model or "").strip() or default_embedding_model_for_provider(
            (provider or global_provider or "").strip().lower()
        )
        return EmbeddingBatch(
            vectors=_stub_embed(texts, dim),
            pipeline_id=legacy_pipeline_id(provider="stub", model=effective_model, dimension=dim),
            dimension=dim,
            task=task,
        )

    # Garde fail-closed : AUCUN chunk vide/blanc ne doit être transmis à un vrai
    # provider (Gateway). Inchangée.
    blanks = [i for i, t in enumerate(texts) if is_blank_text(t)]
    if blanks:
        raise EmbeddingsError(
            f"blank/empty text refused before embeddings call at positions {blanks[:5]}"
            f"{'…' if len(blanks) > 5 else ''} (filter chunks before embedding)."
        )

    effective_provider = (provider or global_provider or "").strip().lower()
    effective_model = (model or "").strip() or default_embedding_model_for_provider(effective_provider)

    # --- BARRIÈRE D'EGRESS ------------------------------------------------------
    # Posée AVANT toute branche de provider, et non dans une branche de fournisseur :
    # un provider externe ajouté demain hériterait de la garde sans qu'on y pense.
    # `enforce_external_egress` ne fait RIEN pour un provider interne (ollama,
    # ai_gateway, stub) — le chemin local nominal ne paie donc ni requête DB ni
    # décision supplémentaire.
    _egress_op = _egress_operation(task)
    _external = _is_external_embedding_provider(effective_provider)
    if _external:
        _guard_external_embedding(
            provider=effective_provider,
            model=effective_model,
            context=context,
            operation=_egress_op,
            batch_size=len(texts),
        )

    # --- Chemin GOUVERNÉ (lot 3) ------------------------------------------------
    # Placé AVANT les chemins historiques et strictement séparé d'eux : `ai_gateway`
    # est un provider à part entière, il ne détourne pas `ollama`. Aucun repli n'est
    # prévu ici — un espace gouverné qui échoue lève (cf. embeddings_gateway).
    if effective_provider == PROVIDER_AI_GATEWAY:
        return embed_via_gateway(
            texts,
            model=effective_model,
            task=task,
            dim=dim,
            rag_space_id=rag_space_id,
            document_id=document_id,
        )

    # Édition Open : AUCUN autre chemin d'encodage. Les anciens chemins directs
    # (fournisseur externe, Ollama direct, Ollama via `/v1/embeddings` sans identité)
    # produisaient une identité « legacy » construite localement, sans digest : ils ont
    # été retirés pour qu'un vecteur stocké porte toujours une identité VÉRIFIÉE.
    raise EmbeddingsError(
        f"embeddings provider {effective_provider!r} is not available in RAGatm Open: "
        f"only '{PROVIDER_AI_GATEWAY}' (governed AI Gateway pipeline) is supported "
        "(and 'stub' for tests)."
    )


def embed_texts(
    texts: List[str],
    dim: int | None = None,
    *,
    provider: str | None = None,
    model: str | None = None,
    task: str = TASK_DOCUMENT,
    context: "Any | None" = None,
) -> List[list[float]]:
    """Return embeddings for a list of texts.

    Le provider/modèle proviennent en priorité du RagSpace (``provider``/``model``
    passés par le pipeline d'ingestion ou la requête). À défaut, on retombe sur le
    réglage GLOBAL :
      - EMBEDDINGS_PROVIDER: "ai_gateway" | "stub" (édition Open)
      - AI_GATEWAY_EMBEDDINGS_MODEL / EMBEDDINGS_MODEL
      - EMBEDDINGS_TIMEOUT_S (default: 30)

    Escape hatch dev/test : si EMBEDDINGS_PROVIDER (global) vaut "stub", on force le
    stub déterministe quel que soit le réglage de l'espace (CI/local sans provider
    réel ; interdit en prod par config.settings.prod).

    Dimension : RAG_EMBEDDING_DIM (default: 768). DOIT correspondre à la dimension
    du VectorField pgvector.

    Compatibilité : cette fonction ne rend que les VECTEURS, comme depuis l'origine.
    Les appelants qui ont besoin de l'identité du pipeline (indexation, recherche
    gouvernée) utilisent ``embed_texts_with_identity``.

    ``context`` et ``task`` sont transmis tels quels : cette fonction est une VUE,
    elle ne doit jamais avoir sa propre politique d'egress. Sans ``context``, un
    provider externe est refusé ici comme il l'est en dessous.
    """
    return embed_texts_with_identity(
        texts, dim, provider=provider, model=model, task=task, context=context
    ).vectors
