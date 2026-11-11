"""Identité de pipeline d'embeddings — module PUR.

Aucune dépendance Django, aucun accès base, aucun appel réseau. Il ne connaît que
des chaînes.

## Ce que ce module tranche

Deux vecteurs ne sont comparables que s'ils ont été produits par le même modèle,
avec les mêmes poids. L'AI Gateway rend pour chaque lot un ``pipeline_id`` : c'est
l'identité **opaque** de la transformation exacte (modèle, poids, tâche ``query`` ou
``document``). Depuis le contrat Gateway v2 il a la forme ``emb-v2:<empreinte>`` et
**ne porte aucune sémantique lisible** : ni la tâche, ni le modèle, ni le digest ne
s'en déduisent. Il se compare par égalité à la référence publiée par la Gateway, il ne
se découpe pas.

La question et le fragment ont des ``pipeline_id`` **différents** (la tâche fait partie
de la transformation) tout en étant **comparables**. Ce qui décide de la comparabilité
est la **famille** de vecteurs : ``modèle_logique@digest8``. Elle n'est pas extraite de
l'identifiant ; elle est dérivée des champs d'identité **attestés** par la Gateway dans
la réponse (``model_logical`` + ``model_digest`` vérifié), cf. ``attested_pipeline_family``.
Deux vecteurs sont comparables si et seulement si leurs familles sont égales.

## Ce que ce module NE fait PAS

Il ne **fabrique** aucun ``pipeline_id`` et n'en **interprète** aucun. Un ancien format
``modele@digest/document-v1`` a existé ; en déduire la famille par découpage était une
hypothèse sur la forme de l'identifiant, et elle a cessé d'être vraie sans que rien ne
le signale — toute indexation gouvernée a échoué. La famille vient donc toujours
d'une attestation explicite, jamais d'un parsing.

La seule identité construite localement est l'identité LEGACY (§8 du lot), qui est
explicitement marquée comme telle et ne prétend porter aucun digest.
"""
from __future__ import annotations

#: Tâches admises par le contrat Gateway ``/v1/embeddings``.
TASK_QUERY = "query"
TASK_DOCUMENT = "document"
ALLOWED_TASKS: frozenset[str] = frozenset({TASK_QUERY, TASK_DOCUMENT})

#: Préfixe des identités construites LOCALEMENT pour le chemin historique (non
#: gouverné). Il est volontairement reconnaissable : lire ``legacy`` dans une trace
#: doit suffire à savoir qu'aucun digest n'a été vérifié.
LEGACY_PREFIX = "legacy"

#: Suffixe de version de l'identité legacy. À incrémenter si la façon dont le chemin
#: historique produit un vecteur change (prétraitement, paramètres d'appel).
LEGACY_VERSION = "legacy-v1"

#: Longueur maximale stockée en base (cf. ``Chunk.pipeline_id``).
MAX_PIPELINE_ID_LENGTH = 255
MAX_PIPELINE_FAMILY_LENGTH = 191

#: Nombre de caractères du digest conservés dans la famille. C'est le format DÉJÀ
#: persisté dans ``rag_chunk.pipeline_family`` (``legacy-768@0a109f42``) : le changer
#: imposerait une migration de données.
FAMILY_DIGEST_LENGTH = 8


def attested_pipeline_family(*, model_logical: str | None, model_digest: str | None) -> str:
    """Famille de vecteurs à partir des champs d'identité ATTESTÉS par la Gateway.

        >>> attested_pipeline_family(model_logical="legacy-768",
        ...     model_digest="0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f")
        'legacy-768@0a109f42'

    Les deux champs viennent de la réponse ``/v1/embeddings`` (``model_logical``,
    ``model_digest``), et l'appelant n'y recourt qu'après avoir vérifié
    ``identity_verified`` et la concordance avec ``expected_digest`` : la famille ne
    repose jamais sur un digest que la Gateway n'a pas elle-même confirmé.

    Un champ manquant rend une chaîne vide : l'appelant refuse, il ne devine pas.
    """
    m = (model_logical or "").strip()
    d = (model_digest or "").strip().lower()
    if not m or len(d) < FAMILY_DIGEST_LENGTH:
        return ""
    return f"{m}@{d[:FAMILY_DIGEST_LENGTH]}"


def pipeline_family(pipeline_id: str | None) -> str:
    """Famille d'une identité qui ne porte PAS d'attestation séparée.

    Seule l'identité LEGACY, construite localement, est sa propre famille :

        >>> pipeline_family("legacy:ollama:nomic-embed-text:768:legacy-v1")
        'legacy:ollama:nomic-embed-text:768:legacy-v1'

    Une identité gouvernée est OPAQUE : rien ne s'en déduit, la fonction rend une
    chaîne vide. Sa famille est portée par ``EmbeddingBatch.family``, dérivée des
    champs attestés de la réponse (``attested_pipeline_family``).

        >>> pipeline_family("emb-v2:9e883f0fe1d326ef17f86027a1d4a5d8")
        ''

    ``None`` / vide -> chaîne vide. L'appelant décide ce qu'il en fait ; ce module
    ne devine pas une identité absente.
    """
    raw = (pipeline_id or "").strip()
    if not raw:
        return ""
    if is_legacy_pipeline_id(raw):
        return raw
    return ""


def same_pipeline_family(a: str | None, b: str | None) -> bool:
    """Deux FAMILLES sont-elles comparables ? Deux familles ABSENTES ne le sont pas.

    Prend des familles (``legacy-768@0a109f42``) ou des identités legacy (leur propre
    famille) : égalité de deux chaînes non vides. ``None`` n'est pas égal à ``None``
    ici — une identité inconnue ne prouve pas une identité commune (§11).
    """
    fa, fb = (a or "").strip(), (b or "").strip()
    return bool(fa) and fa == fb


def legacy_pipeline_id(*, provider: str | None, model: str | None, dimension: int) -> str:
    """Identité CONSTRUITE LOCALEMENT pour le chemin historique non gouverné.

    Forme : ``legacy:{provider}:{model}:{dimension}:legacy-v1``.

    Elle est délibérément *sans digest*. Le chemin historique n'a aucun moyen de
    savoir quels poids ont réellement servi : prétendre le contraire produirait une
    identité mensongère, plus dangereuse que pas d'identité du tout.

    Elle n'est apposée qu'aux indexations NOUVELLES passant par le chemin
    historique. Les chunks déjà en base restent à ``NULL`` : voir le module
    docstring et §8 du lot.
    """
    p = (provider or "").strip().lower() or "unknown"
    m = (model or "").strip() or "unknown"
    try:
        d = int(dimension)
    except (TypeError, ValueError):
        d = 0
    return f"{LEGACY_PREFIX}:{p}:{m}:{d}:{LEGACY_VERSION}"


def is_legacy_pipeline_id(pipeline_id: str | None) -> bool:
    """Vrai si l'identité a été construite localement (donc sans digest vérifié)."""
    return (pipeline_id or "").strip().startswith(f"{LEGACY_PREFIX}:")


def is_governed_pipeline_id(pipeline_id: str | None) -> bool:
    """Vrai si l'identité vient de la Gateway : non vide et non legacy.

    Aucune forme n'est exigée au-delà : l'identifiant gouverné est opaque, sa
    validité se juge par égalité avec la référence publiée, pas par son aspect.
    """
    raw = (pipeline_id or "").strip()
    return bool(raw) and not is_legacy_pipeline_id(raw)
