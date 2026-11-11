"""Trace des CANDIDATS du retrieval — observabilité de diagnostic.

## Pourquoi ce module existe

Une version antérieure n'a persisté qu'un scalaire par requête : ``top1_score``. Le diagnostic a donc pu
établir *que* le signal échouait, mais pas *où* : impossible de distinguer un
support jamais remonté d'un support mal classé, ni de dire si un candidat venait
du corpus privé ou du corpus métier. Le diagnostic a dû écrire « non prouvable »
là où une colonne aurait suffi, et le jeu consommé ne se rejoue pas.

Ce module rend ces questions décidables pour les requêtes À VENIR.

## Ce qu'il n'est pas

`candidate_trace` est de l'**observation**. Aucune décision ne la lit : ni
l'admission, ni le classement, ni la réponse. Un test compare les résultats
drapeau ON et drapeau OFF plutôt que de faire confiance à cette phrase.

## Ce qu'il ne contient jamais

Aucun texte métier : ni question, ni passage, ni document, ni vecteur, ni secret.
Uniquement des identifiants, des rangs, des scores et des identités techniques.
L'hydratation du texte se fait à part, à partir des identifiants, comme auparavant.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

#: Version du schéma. Toute évolution INCOMPATIBLE l'incrémente : un consommateur
#: doit pouvoir refuser une trace qu'il ne sait pas lire plutôt que la mésinterpréter.
SCHEMA_VERSION = 1

#: Plafond dur du nombre de candidats tracés. Le pool est déjà borné en amont
#: (`candidate_k`), mais une trace d'observabilité ne doit avoir AUCUNE croissance
#: libre : c'est ce qui transforme un diagnostic en incident de production.
MAX_CANDIDATS_TRACES = 200

# --- Magasins ---------------------------------------------------------------
STORE_PRIVE = "private_ragspace"
STORE_METIER = "business_corpus"
STORE_INCONNU = "unknown"

#: Valeurs de `source_type` posées par le pipeline lui-même. Le magasin est donc
#: LU, jamais déduit d'un nom de document ni d'un score.
_SOURCE_TYPE_METIER = "domain_corpus"
_SOURCE_TYPE_PRIVE = "workspace_document"

# --- Méthodes de retrieval --------------------------------------------------
METHODE_LEXICALE = "lexical"
METHODE_VECTORIELLE = "vector"

# --- Modes de classement ----------------------------------------------------
#: Le classement vient du reranker, qui a répondu.
MODE_RERANKER = "normal_reranker"
#: Le reranker a été appelé et a échoué ; l'ordre RRF canonique a été restauré.
MODE_FALLBACK_RRF = "fallback_rrf"
#: Le reranker est éteint ou indisponible : l'ordre est celui de la fusion.
MODE_RERANKER_INDISPONIBLE = "reranker_unavailable"
#: Aucun candidat à classer.
MODE_SANS_CANDIDAT = "no_candidates"


def _premier_dict(*valeurs: Any) -> Mapping[str, Any]:
    for v in valeurs:
        if isinstance(v, Mapping):
            return v
    return {}


def magasin_du_candidat(candidat: Mapping[str, Any]) -> str:
    """Magasin d'origine, LU sur le candidat — jamais inféré.

    Le pipeline pose `source_type` au moment où il sait d'où vient le chunk
    (`_tag_private_chunk` côté privé, l'ingestion corpus côté métier). Le relire
    ici est donc une lecture, pas une reconstruction.
    """
    source = _premier_dict(candidat.get("source"))
    brut = candidat.get("source_type") or source.get("source_type")
    if brut == _SOURCE_TYPE_METIER:
        return STORE_METIER
    if brut == _SOURCE_TYPE_PRIVE:
        return STORE_PRIVE
    return STORE_INCONNU


def methodes_du_candidat(candidat: Mapping[str, Any]) -> list[str]:
    """Mécanismes qui ont fait remonter ce candidat.

    La fusion enregistre un rang PAR MODALITÉ (`text_rank`, `vector_rank`) et ne
    l'écrase pas : un candidat trouvé par les deux voies conserve les deux. C'est
    la présence du RANG qui fait foi, pas celle du score — `merge_rrf` initialise
    `text_score` et `vector_score` à 0.0 pour tous, donc un score nul ne distingue
    pas « absent de ce leg » de « trouvé avec une pertinence nulle ».
    """
    methodes = []
    if candidat.get("text_rank") is not None:
        methodes.append(METHODE_LEXICALE)
    if candidat.get("vector_rank") is not None:
        methodes.append(METHODE_VECTORIELLE)
    return methodes


def _nombre_ou_none(valeur: Any) -> float | None:
    """Rend un flottant fini, ou ``None``. JAMAIS 0.0 en remplacement d'un absent.

    Un 0 se lirait comme « jugé non pertinent », ce qui est une affirmation ; un
    ``None`` dit « cette voie n'a pas produit de score », ce qui est le fait.
    """
    if valeur is None or isinstance(valeur, bool):
        return None
    try:
        f = float(valeur)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _score_de_leg(candidat: Mapping[str, Any], cle_score: str, cle_rang: str) -> float | None:
    """Score d'un leg — ``None`` si le candidat n'a pas participé à ce leg."""
    if candidat.get(cle_rang) is None:
        return None
    return _nombre_ou_none(candidat.get(cle_score))


def identites_par_magasin(candidats: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    """Familles d'embedding OBSERVÉES, groupées par magasin.

 a montré que le corpus privé et le corpus métier n'utilisent pas la même
    famille. Sans cette ventilation, un score vectoriel privé et un score vectoriel
    métier se liraient comme comparables alors qu'ils ne le sont pas.

    Portée REQUÊTE : la valeur est identique pour tous les candidats d'un même
    magasin, la répéter sur chacun gonflerait la trace sans rien apprendre. Une
    liste, et non une chaîne, parce qu'un pool hétérogène est un fait à voir plutôt
    qu'à masquer par un choix arbitraire.

    RÔLE DEPUIS : cette dérivation est un COMPLÉMENT, plus la source. Les
    couches qui emploient réellement l'identité la consignent désormais elles-mêmes
    (``RetrievalTraceBuilder.record_store_embedding_identity``), et cette valeur-là
    prime toujours. La dérivation ne s'applique qu'aux magasins qu'aucune couche n'a
    renseignés, et sa clé le dit : ``pipeline_family_observee``, pour qu'on ne la
    confonde pas avec une identité déclarée.

    Le pipeline n'expose pas ``pipeline_family`` sur les candidats — la colonne sert
    de FILTRE au dépôt (mode gouverné), elle n'est pas projetée. Cette fonction rend
    donc ``{}`` en pratique, ce qui est sans conséquence depuis que l'identité arrive
    par la voie directe.
    """
    par_magasin: dict[str, set[str]] = {}
    for candidat in candidats or ():
        famille = candidat.get("pipeline_family") or _premier_dict(
            candidat.get("source")).get("pipeline_family")
        if not famille:
            continue
        par_magasin.setdefault(magasin_du_candidat(candidat), set()).add(str(famille))
    return {magasin: sorted(familles) for magasin, familles in sorted(par_magasin.items())}


def construire(
    *,
    pool_avant: Sequence[Mapping[str, Any]],
    pool_apres: Sequence[Mapping[str, Any]],
    info_rerank: Mapping[str, Any] | None,
    reranker_disponible: bool,
    identites: Mapping[str, Any] | None = None,
    ordre_canonique: bool = True,
) -> dict[str, Any]:
    """Construit la trace des candidats. Fonction PURE : ne mute aucune entrée.

    ``pool_avant`` doit être dans l'ordre RRF **canonique** — celui que le
    reranker a réellement vu. Le passer dans l'ordre d'hygiène produirait des
    rangs « avant » qui ne correspondent à rien : c'est la confusion que
    ``select_rrf_pool`` existe pour empêcher.
    """
    info = dict(info_rerank or {})
    avant = list(pool_avant or ())[:MAX_CANDIDATS_TRACES]
    apres = list(pool_apres or ())

    if not avant:
        mode = MODE_SANS_CANDIDAT
    elif not reranker_disponible:
        mode = MODE_RERANKER_INDISPONIBLE
    elif info.get("fallback"):
        mode = MODE_FALLBACK_RRF
    elif info.get("success"):
        mode = MODE_RERANKER
    else:
        # Appelé sans succès ni repli déclaré : pool vide côté Gateway, ou sortie
        # anticipée. On ne prétend pas que le reranker a classé.
        mode = MODE_RERANKER_INDISPONIBLE

    reranke = mode == MODE_RERANKER

    # Index du pool APRÈS, PAR IDENTIFIANT. Deux raisons, toutes deux nécessaires :
    #
    # 1. le rang final ne doit jamais être reconstruit depuis une position supposée ;
    # 2. `_apply_order` du reranker pose `rerank_score` et `rerank_rank` sur des
    #    COPIES. Les dicts du pool d'avant ne les portent pas — les y chercher
    #    rendrait `reranker_score = None` alors que le reranker a bien répondu, ou
    #    pire, laisserait passer un score résiduel appartenant à une autre requête.
    rang_apres: dict[str, int] = {}
    apres_par_id: dict[str, Mapping[str, Any]] = {}
    for position, candidat in enumerate(apres, start=1):
        cid = str(candidat.get("chunk_id") or "")
        if cid and cid not in rang_apres:
            rang_apres[cid] = position
            apres_par_id[cid] = candidat

    #: Le rang rendu par la Gateway, quand elle a répondu. Il prime sur la position
    #: dans la liste : la liste finale contient aussi la queue non soumise.
    candidats: list[dict[str, Any]] = []
    soumis = int(info.get("documents") or 0)
    for position, candidat in enumerate(avant, start=1):
        cid = str(candidat.get("chunk_id") or "")
        if not cid:
            continue
        source = _premier_dict(candidat.get("source"))
        document_id = candidat.get("document_id") or source.get("document_id")
        final = apres_par_id.get(cid, {})
        rerank_rank = final.get("rerank_rank")
        post = None
        if reranke:
            post = rerank_rank if isinstance(rerank_rank, int) else rang_apres.get(cid)
        entree: dict[str, Any] = {
            "candidate_id": cid,
            "chunk_id": cid,
            "document_id": str(document_id) if document_id else None,
            "source_store": magasin_du_candidat(candidat),
            "source_methods": methodes_du_candidat(candidat),
            "pre_rerank_rank": position,
            "post_rerank_rank": post,
            # Score NATIF du cross-encoder, jamais normalisé. Absent — et non nul —
            # pour un candidat qui n'a pas été reranké : un 0 se lirait comme un
            # jugement de non-pertinence.
            "reranker_score": _nombre_ou_none(final.get("rerank_score")) if reranke else None,
            "vector_score": _score_de_leg(candidat, "vector_score", "vector_rank"),
            "lexical_score": _score_de_leg(candidat, "text_score", "text_rank"),
            "fusion_score": _nombre_ou_none(candidat.get("fusion_score")),
            "submitted_to_reranker": bool(soumis) and position <= soumis,
        }
        candidats.append(entree)

    trace: dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "ranking_mode": mode,
        "reranker_used": reranke,
        "fallback_used": bool(info.get("fallback")),
        "pool_size": len(candidats),
        "submitted_to_reranker": soumis or None,
        "truncated": len(list(pool_avant or ())) > MAX_CANDIDATS_TRACES,
        "candidates": candidats,
    }
    if info.get("error_kind"):
        trace["failure_kind"] = str(info["error_kind"])[:80]

    identite = dict(identites or {})
    # L'identité FOURNIE par le pipeline prime : elle vient des couches qui l'ont
    # réellement employée (la Gateway côté privé, le groupement de requêtes côté
    # corpus). La dérivation depuis les candidats ne sert que de complément quand
    # ces couches n'ont rien consigné — elle ne doit jamais l'écraser.
    embeddings = dict(identite.get("embedding_by_store") or {})
    for magasin, familles in identites_par_magasin(avant).items():
        embeddings.setdefault(magasin, {"pipeline_family_observee": familles})
    if embeddings:
        identite["embedding_by_store"] = embeddings
    # Portées à la REQUÊTE, jamais dupliquées sur chaque candidat.
    reranker_identite = {
        cle: info.get(source)
        for cle, source in (
            ("model_logical", "model_logical"),
            ("model_revision", "model_revision"),
            ("pool_k", "pool_k"),
        )
        if info.get(source) is not None
    }
    if reranker_identite:
        identite["reranker"] = reranker_identite
    if identite:
        trace["identities"] = identite
    if not ordre_canonique:
        # Dire que les rangs « avant » ne sont pas ceux du RRF canonique vaut mieux
        # que les publier comme s'ils l'étaient.
        trace["pre_rerank_order_canonical"] = False
    return trace
