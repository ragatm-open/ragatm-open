"""Couverture d'analyse : vocabulaire, invariants, empreinte de corpus, rendu.

Module PUR : aucune dépendance Django, aucun accès base, aucun appel IA. Il ne
contient que des faits mesurables et leur mise en forme. C'est ce qui le rend
testable sans base et réutilisable par toute analyse future.

Le vocabulaire (corpus analysé / document considéré / analysé / niveaux A-B-C-D)
est défini dans docs/coverage-semantics.md. Les règles lexicales qui en découlent
sont appliquées ici et VERROUILLÉES PAR TEST :

- ``coverage_complete=False`` → le mot « exhaustif » est interdit dans le rendu ;
- seuls les niveaux A et B sont mesurables ; C et D ne sont jamais produits ici ;
- ``items_without_source > 0`` rend le résultat analytique invalide.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Literal

# Niveaux d'exhaustivité (docs/coverage-semantics.md).
# A et B sont GARANTIS et mesurables. C et D sont PROBABILISTES : ce module ne
# les produit jamais — une analyse qui les revendiquerait devrait le prouver
# contre une vérité terrain.
CoverageLevel = Literal["A", "B", "C", "D"]

MEASURABLE_LEVELS: frozenset[str] = frozenset({"A", "B"})
PROBABILISTIC_LEVELS: frozenset[str] = frozenset({"C", "D"})

# Codes d'exclusion internes. Ensemble BORNÉ : tout statut non reconnu tombe dans
# `unknown` plutôt que de disparaître silencieusement du dénominateur.
REASON_EXTRACTION_FAILED = "extraction_failed"
REASON_NOT_READY = "not_ready"
REASON_PROCESSING = "processing"
REASON_NO_TEXT_CONTENT = "no_text_content"
REASON_FILTERED_OUT = "filtered_out"
REASON_UNSUPPORTED = "unsupported"
REASON_PERMISSION_FILTERED = "permission_filtered"
REASON_LIMIT_APPLIED = "limit_applied"
REASON_UNKNOWN = "unknown"

EXCLUSION_REASONS: frozenset[str] = frozenset(
    {
        REASON_EXTRACTION_FAILED,
        REASON_NOT_READY,
        REASON_PROCESSING,
        REASON_NO_TEXT_CONTENT,
        REASON_FILTERED_OUT,
        REASON_UNSUPPORTED,
        REASON_PERMISSION_FILTERED,
        REASON_LIMIT_APPLIED,
        REASON_UNKNOWN,
    }
)

# Codes JAMAIS sérialisés vers l'utilisateur. `permission_filtered` révélerait
# l'existence de documents auxquels l'appelant n'a pas accès : le périmètre
# visible doit être calculé AVANT la couverture, si bien que ce code ne devrait
# jamais apparaître. Il existe pour que l'oubli soit détectable, pas divulgué.
INTERNAL_ONLY_REASONS: frozenset[str] = frozenset({REASON_PERMISSION_FILTERED})

# Libellés utilisateur : neutres, sans nom de modèle, de flag, de provider ni
# d'exception. Aucun vocabulaire métier.
REASON_LABELS: dict[str, str] = {
    REASON_EXTRACTION_FAILED: "échec d’extraction",
    REASON_NOT_READY: "document non encore prêt",
    REASON_PROCESSING: "traitement en cours",
    REASON_NO_TEXT_CONTENT: "aucun contenu textuel exploitable",
    REASON_FILTERED_OUT: "exclu par un filtre",
    REASON_UNSUPPORTED: "format non exploitable",
    REASON_LIMIT_APPLIED: "limite de traitement appliquée",
    REASON_UNKNOWN: "état non reconnu",
}


class CoverageError(ValueError):
    """Invariant de couverture violé. Jamais rattrapé silencieusement."""


@dataclass(frozen=True)
class CoverageExclusion:
    reason: str
    count: int


@dataclass(frozen=True)
class CoverageScope:
    """Périmètre déclaré. Généré en code, jamais par un modèle."""

    rag_space_id: str
    generated_at: datetime
    corpus_fingerprint: str
    filters_applied: tuple[str, ...] = ()


@dataclass(frozen=True)
class Coverage:
    scope: CoverageScope
    documents_total: int
    documents_considered: int
    documents_analyzed: int
    documents_with_results: int
    documents_excluded: tuple[CoverageExclusion, ...]
    pages_analyzed: int
    chunks_analyzed: int
    items_extracted: int
    items_without_source: int
    coverage_level: CoverageLevel
    coverage_complete: bool
    coverage_ratio: float
    limits_applied: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate(self)

    @property
    def is_valid_result(self) -> bool:
        """Un élément sans source rend le résultat analytique invalide."""
        return self.items_without_source == 0


def _validate(c: Coverage) -> None:
    for name in (
        "documents_total",
        "documents_considered",
        "documents_analyzed",
        "documents_with_results",
        "pages_analyzed",
        "chunks_analyzed",
        "items_extracted",
        "items_without_source",
    ):
        value = getattr(c, name)
        if not isinstance(value, int) or isinstance(value, bool):
            raise CoverageError(f"{name} doit être un entier (reçu {value!r})")
        if value < 0:
            raise CoverageError(f"{name} ne peut pas être négatif (reçu {value})")

    if c.documents_considered > c.documents_total:
        raise CoverageError(
            f"documents_considered ({c.documents_considered}) > documents_total ({c.documents_total})"
        )
    if c.documents_analyzed > c.documents_considered:
        raise CoverageError(
            f"documents_analyzed ({c.documents_analyzed}) > documents_considered ({c.documents_considered})"
        )
    if c.documents_with_results > c.documents_analyzed:
        raise CoverageError(
            f"documents_with_results ({c.documents_with_results}) > documents_analyzed ({c.documents_analyzed})"
        )

    if not 0.0 <= c.coverage_ratio <= 1.0:
        raise CoverageError(f"coverage_ratio hors [0,1] (reçu {c.coverage_ratio})")

    if c.coverage_level not in ("A", "B", "C", "D"):
        raise CoverageError(f"coverage_level inconnu : {c.coverage_level!r}")

    for exclusion in c.documents_excluded:
        if exclusion.reason not in EXCLUSION_REASONS:
            raise CoverageError(f"raison d'exclusion inconnue : {exclusion.reason!r}")
        if exclusion.count <= 0:
            raise CoverageError(
                f"exclusion {exclusion.reason!r} : count doit être > 0 (reçu {exclusion.count})"
            )

    # `coverage_complete` est le verrou anti-affirmation-trompeuse. Il ne peut pas
    # être vrai s'il reste la moindre exclusion ou la moindre limite appliquée.
    if c.coverage_complete:
        if c.documents_analyzed != c.documents_considered:
            raise CoverageError(
                "coverage_complete=True exige documents_analyzed == documents_considered "
                f"({c.documents_analyzed} != {c.documents_considered})"
            )
        if c.documents_excluded:
            raise CoverageError("coverage_complete=True est incompatible avec des exclusions")
        if c.limits_applied:
            raise CoverageError("coverage_complete=True est incompatible avec limits_applied")


def compute_coverage_ratio(*, documents_analyzed: int, documents_considered: int) -> float:
    """Part des documents considérés qui ont réellement été analysés.

    Convention pour un périmètre vide (``documents_considered == 0``) : le ratio
    vaut 1.0. Il n'y a rien à analyser, donc rien n'a été manqué. Le rendu
    utilisateur traite ce cas à part et ne parle jamais de « couverture 100 % »
    sur un espace vide (cf. render_coverage_markdown).
    """
    if documents_considered <= 0:
        return 1.0
    return documents_analyzed / documents_considered


# --------------------------------------------------------------------------- #
# Empreinte de corpus
# --------------------------------------------------------------------------- #

FINGERPRINT_PREFIX = "sha256:"


def compute_corpus_fingerprint(documents: Iterable[tuple[object, object, object]]) -> str:
    """Empreinte stable d'un corpus, à partir de ``(id, content_hash, status)``.

    Change si un document est ajouté, supprimé, si son contenu change ou si son
    statut change. Ne change PAS si l'ordre SQL change : les lignes sont triées
    avant hachage.

    Ne contient aucun nom de fichier, aucun texte de document, aucune donnée
    utilisateur en clair — seulement des identifiants et des empreintes.
    """
    rows = sorted(
        f"{doc_id}:{content_hash or ''}:{status or ''}"
        for doc_id, content_hash, status in documents
    )
    digest = hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()
    return f"{FINGERPRINT_PREFIX}{digest}"


def estimate_tokens(characters_total: int) -> int:
    """Estimation grossière et documentée : ~4 caractères par token.

    Volontairement naïve : aucun tokenizer externe, aucune dépendance. Le champ
    exposé s'appelle `estimated_tokens_total`, jamais `tokens_total`.
    """
    if characters_total <= 0:
        return 0
    return math.ceil(characters_total / 4)


# --------------------------------------------------------------------------- #
# Sérialisation
# --------------------------------------------------------------------------- #

def coverage_to_dict(coverage: Coverage) -> dict:
    """JSON stable. Filtre les codes internes (jamais de `permission_filtered`)."""
    exclusions = [
        {"reason": e.reason, "count": e.count, "label": REASON_LABELS.get(e.reason, e.reason)}
        for e in coverage.documents_excluded
        if e.reason not in INTERNAL_ONLY_REASONS
    ]
    return {
        "scope": {
            "rag_space_id": str(coverage.scope.rag_space_id),
            "generated_at": coverage.scope.generated_at.isoformat(),
            "corpus_fingerprint": coverage.scope.corpus_fingerprint,
            "filters_applied": list(coverage.scope.filters_applied),
        },
        "documents_total": coverage.documents_total,
        "documents_considered": coverage.documents_considered,
        "documents_analyzed": coverage.documents_analyzed,
        "documents_with_results": coverage.documents_with_results,
        "documents_excluded": exclusions,
        "pages_analyzed": coverage.pages_analyzed,
        "chunks_analyzed": coverage.chunks_analyzed,
        "items_extracted": coverage.items_extracted,
        "items_without_source": coverage.items_without_source,
        "coverage_level": coverage.coverage_level,
        "coverage_complete": coverage.coverage_complete,
        "coverage_ratio": round(coverage.coverage_ratio, 4),
        "limits_applied": list(coverage.limits_applied),
    }


# --------------------------------------------------------------------------- #
# Rendu utilisateur
# --------------------------------------------------------------------------- #

def _fr_int(n: int) -> str:
    """Séparateur de milliers = espace simple (convention française).

    Volontairement une espace ASCII (U+0020) et non une espace insécable : le
    rendu est du Markdown destiné à être comparé, testé et cherché en clair.
    """
    return f"{n:,}".replace(",", " ")


def render_coverage_markdown(coverage: Coverage) -> str:
    """Résumé générique du périmètre analysé. Aucun vocabulaire métier.

    Le mot « exhaustif » n'est JAMAIS produit — pas même quand la couverture est
    complète. Une couverture complète au niveau A/B ne dit rien des niveaux C
    (sémantique) et D (métier), qui ne sont pas garantissables : promettre
    l'exhaustivité serait une affirmation que le système ne peut pas tenir.
    """
    lines = ["## Périmètre analysé", ""]

    if coverage.documents_considered == 0:
        lines.append("Aucun document à analyser dans ce périmètre.")
        return "\n".join(lines)

    pct = round(coverage.coverage_ratio * 100)
    not_analyzed = coverage.documents_considered - coverage.documents_analyzed

    lines.append(f"- **{_fr_int(coverage.documents_considered)} documents considérés**")
    lines.append(f"- **{_fr_int(coverage.documents_analyzed)} documents analysés**")
    if not_analyzed > 0:
        lines.append(f"- **{_fr_int(not_analyzed)} documents non analysés**")
    lines.append(f"- **{_fr_int(coverage.chunks_analyzed)} extraits textuels parcourus**")
    lines.append(f"- **Couverture textuelle : {pct} %**")

    visible = [e for e in coverage.documents_excluded if e.reason not in INTERNAL_ONLY_REASONS]
    if visible:
        lines += ["", "### Documents non analysés", ""]
        for e in visible:
            label = REASON_LABELS.get(e.reason, REASON_LABELS[REASON_UNKNOWN])
            noun = "document" if e.count == 1 else "documents"
            lines.append(f"- {_fr_int(e.count)} {noun} : {label}")

    if coverage.limits_applied:
        lines += ["", "### Limites appliquées", ""]
        lines += [f"- {limit}" for limit in coverage.limits_applied]

    lines.append("")
    if coverage.coverage_complete:
        lines.append(
            "Tous les documents et contenus textuels exploitables du périmètre ont été parcourus."
        )
    else:
        lines.append(
            "Cette analyse couvre tous les contenus textuels exploitables disponibles. "
            "Certains documents n’ont pas pu être analysés."
        )

    return "\n".join(lines)
