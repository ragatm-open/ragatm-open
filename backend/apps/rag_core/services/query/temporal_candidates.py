# backend/apps/rag_core/services/query/temporal_candidates.py
from __future__ import annotations

"""
Leg DOCUMENTAIRE borné sur les métadonnées de date (P3-c).

Pourquoi un leg séparé
----------------------
« Quelle est la note la plus récente ? » ne contient aucun thème discriminant. Sur un
corpus de notes quotidiennes au vocabulaire quasi identique :

- le leg lexical ne trouve rien — la question n'a aucun token en commun avec le corps ;
- le leg vectoriel les trouve TOUTES également proches, et le document attendu sort
  du pool avant la fusion, éliminé par les seuils de score ;
- élargir `candidate_k` ne suffit pas : le candidat existe mais reste faible, donc la
  politique de nettoyage le retire quand même.

La bonne source de rappel n'est alors PAS la similarité, mais la métadonnée de date.
Ce module la lit directement, en SQL borné, filtré par périmètre AVANT tri.

Ce qu'il ne fait pas
--------------------
- il ne produit pas de réponse : il produit des CANDIDATS au format du pipeline, qui
  repartent dans la projection et la politique de réponse habituelles ;
- il n'invente aucun score sémantique (cf. `TEMPORAL_PRIORITY`) ;
- il ne s'exécute jamais pour une question sans opérateur temporel ;
- il ne connaît rien au format `.msg` : tout corpus dont les chunks portent une date
  exploitable en bénéficie.
"""

import logging
import time
from typing import Any, Optional

from django.conf import settings
from django.db import connection

from apps.rag_core.domain.provenance import SOURCE_TYPE_WORKSPACE

from .strong_signals import (
    TEMPORAL_AFTER_DATE,
    TEMPORAL_BEFORE_DATE,
    TEMPORAL_BETWEEN_DATES,
    TemporalQueryIntent,
)

logger = logging.getLogger(__name__)

# Borne ABSOLUE du leg. Ce n'est pas un réglage de confort : c'est ce qui garantit que
# la requête reste bornée quelle que soit la taille du RagSpace.
TEMPORAL_CANDIDATE_LIMIT_DEFAULT = 100
TEMPORAL_CANDIDATE_LIMIT_MIN = 1
TEMPORAL_CANDIDATE_LIMIT_MAX = 1000

# Marqueur de provenance porté par chaque candidat produit ici. Il permet de
# distinguer, dans les sources finales, ce qui vient de la métadonnée de ce qui vient
# de la similarité — et donc d'auditer le leg sans relire les logs.
TEMPORAL_LEG = "temporal_metadata"

# Les clés de métadonnée lues, dans l'ordre de préférence. `email_sent_at` est la date
# d'envoi réelle du message ; `sent_at` est son alias historique. En dernier recours
# seulement, `Document.document_date` — c'est une DATE sans heure, donc moins précise.
DATE_KEYS = ("email_sent_at", "sent_at")


def temporal_candidate_limit() -> int:
    """Limite validée du leg. Ne renvoie JAMAIS None : illimité n'est pas une option.

    Une valeur absente, non entière, négative ou nulle retombe sur le défaut plutôt
    que d'échouer : le leg est un complément de rappel, il ne doit pas casser une
    requête utilisateur à cause d'une variable d'environnement mal saisie. Une valeur
    trop grande est ramenée au maximum absolu.
    """
    raw = getattr(settings, "RAG_TEMPORAL_CANDIDATE_LIMIT", TEMPORAL_CANDIDATE_LIMIT_DEFAULT)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("temporal.limit invalide (non entier) — retour au defaut")
        return TEMPORAL_CANDIDATE_LIMIT_DEFAULT
    if value < TEMPORAL_CANDIDATE_LIMIT_MIN:
        logger.warning("temporal.limit hors bornes (< %d) — retour au defaut",
                       TEMPORAL_CANDIDATE_LIMIT_MIN)
        return TEMPORAL_CANDIDATE_LIMIT_DEFAULT
    return min(value, TEMPORAL_CANDIDATE_LIMIT_MAX)


def temporal_leg_enabled() -> bool:
    return bool(getattr(settings, "RAG_TEMPORAL_CANDIDATES_ENABLED", False))


# `~` sur le texte AVANT le cast, à l'intérieur d'un CASE : le CASE garantit que la
# branche THEN n'est évaluée que si la condition tient, donc une métadonnée malformée
# ne fait jamais échouer la requête entière. Un `WHERE ... ::timestamptz` nu, lui,
# peut être évalué avant le filtre et lever une erreur sur une seule ligne corrompue.
_ISO_PREFIX = r"^\d{4}-\d{2}-\d{2}"


def _branches() -> list[str]:
    """Une branche d'agrégat par clé de date, dans l'ordre de préférence.

    `MAX` parce que la date est portée par CHAQUE chunk du document : tous portent la
    même valeur, l'agrégat ne fait que la remonter au niveau documentaire.
    """
    return [
        f"MAX(CASE WHEN c.meta->>'{key}' ~ '{_ISO_PREFIX}' "
        f"THEN (c.meta->>'{key}')::timestamptz END)"
        for key in DATE_KEYS
    ]


def build_temporal_sql(
    *,
    rag_space_id: Any,
    intent: TemporalQueryIntent,
    limit: int,
    document_ids: Optional[list[str]] = None,
) -> tuple[str, dict]:
    """Construit la requête bornée du leg et ses paramètres.

    Extrait de `fetch_temporal_candidates` pour être rejouable tel quel sous
    `EXPLAIN (ANALYZE, BUFFERS)` : la preuve de performance doit porter sur le SQL
    RÉELLEMENT exécuté, et non sur une reconstitution approchante.
    """
    date_expr = f"COALESCE({', '.join(_branches())})"
    where_extra, params = [], {"space": rag_space_id, "limit": int(limit)}
    # Liste blanche (corpus figé) : bornage des DEUX lectures de `rag_chunk` — la CTE
    # qui date les documents et la jointure qui choisit le fragment représentatif.
    # Hors liste blanche : fragment vide, requête inchangée.
    from apps.rag_core.domain.chunk_allowlist import STORE_PRIVATE, allowlist_join

    allow_join, allow_params = allowlist_join(alias="c", store=STORE_PRIVATE)
    params.update(allow_params)

    if document_ids is not None:
        where_extra.append("AND c.document_id = ANY(%(doc_ids)s::uuid[])")
        params["doc_ids"] = list(document_ids)

    # Filtres de date portés par l'opérateur. « avant le 13 mars » EXCLUT le 13 ;
    # « après le 18 mai » commence le 19 ; « entre le 1er et le 13 » INCLUT les bornes.
    having = []
    dates = list(intent.exact_dates)
    if intent.operator == TEMPORAL_BEFORE_DATE and dates:
        having.append(f"AND {date_expr} < %(d_lo)s::timestamptz")
        params["d_lo"] = dates[0].isoformat()
    elif intent.operator == TEMPORAL_AFTER_DATE and dates:
        having.append(f"AND {date_expr} >= (%(d_lo)s::timestamptz + interval '1 day')")
        params["d_lo"] = dates[0].isoformat()
    elif intent.operator == TEMPORAL_BETWEEN_DATES and len(dates) >= 2:
        lo, hi = sorted(dates[:2])
        having.append(f"AND {date_expr} >= %(d_lo)s::timestamptz")
        having.append(f"AND {date_expr} < (%(d_hi)s::timestamptz + interval '1 day')")
        params["d_lo"], params["d_hi"] = lo.isoformat(), hi.isoformat()

    direction = "DESC" if intent.descending else "ASC"

    # Le filtre de périmètre est dans le WHERE de la CTE : il s'applique AVANT
    # l'agrégat et AVANT le tri. Aucun document étranger n'atteint le ORDER BY.
    sql = f"""
    WITH dated AS (
        SELECT c.document_id AS did, {date_expr} AS doc_date
        FROM rag_chunk c
        JOIN rag_document d ON d.id = c.document_id
        JOIN rag_space s   ON s.id = d.rag_space_id
{allow_join}        WHERE c.rag_space_id = %(space)s
          AND d.rag_space_id = %(space)s          -- verrou anti-fuite
          AND s.id = %(space)s
          AND s.deleted_at IS NULL                -- espace supprime = invisible
          -- Éligibilité STRICTE (Lot 2) : `<> FAILED` laissait passer PENDING et
          -- INGESTING, dont les chunks sont soit absents soit périmés.
          AND d.status = 'READY'
          {' '.join(where_extra)}
        GROUP BY c.document_id
        HAVING {date_expr} IS NOT NULL            -- un document sans date n'est
                                                  -- jamais « le plus recent »
          {' '.join(having)}
        ORDER BY doc_date {direction}, c.document_id
        LIMIT %(limit)s
    )
    SELECT DISTINCT ON (dt.did)
           dt.did::text, dt.doc_date,
           c.id::text, c.text, c.chunk_index, c.page_number, c.meta,
           d.filename, d.document_date
    FROM dated dt
    JOIN rag_chunk c    ON c.document_id = dt.did AND c.rag_space_id = %(space)s
{allow_join}    -- Le prédicat d'espace est RÉPÉTÉ ici. Sans lui, le planificateur n'a aucun filtre
    -- sur `rag_document` à ce niveau et parcourt la table ENTIÈRE avant de joindre —
    -- mesuré sous EXPLAIN : Seq Scan de 10 000 lignes (les deux espaces) là où 5 000
    -- suffisent. C'est aussi une défense en profondeur : l'isolation ne repose plus
    -- sur le seul contenu de la CTE.
    JOIN rag_document d ON d.id = dt.did AND d.rag_space_id = %(space)s
    -- Chunk REPRESENTATIF : le premier du document, c'est-a-dire le chunk d'en-tete
    -- (objet, date, expediteur). C'est lui qui identifie le message.
    ORDER BY dt.did, c.chunk_index ASC
    """
    return sql, params


def fetch_temporal_candidates(
    *,
    rag_space_id: Any,
    intent: TemporalQueryIntent,
    limit: Optional[int] = None,
    document_ids: Optional[list[str]] = None,
) -> tuple[list[dict], dict]:
    """Candidats sélectionnés par métadonnée de date, au format du pipeline.

    `document_ids` restreint la sélection à un ensemble documentaire déjà jugé
    PERTINENT (phase B d'une requête thématique). Quand il vaut `None`, la sélection
    porte sur tout le RagSpace autorisé — réservé aux opérateurs GLOBAUX.

    Renvoie `(candidats, observabilité)`. Ne lève jamais : en cas d'erreur SQL, le leg
    s'efface et le pipeline historique reste seul maître.
    """
    started = time.perf_counter()
    bound = limit if limit is not None else temporal_candidate_limit()
    bound = max(TEMPORAL_CANDIDATE_LIMIT_MIN, min(int(bound), TEMPORAL_CANDIDATE_LIMIT_MAX))

    observability = {
        "temporal_leg_applied": False,
        "temporal_operator": intent.operator,
        "temporal_scope": "global" if intent.is_global else "topic",
        "topic_terms_present": bool(intent.has_explicit_topic),
        "metadata_candidates_count": 0,
        "topic_documents_count": len(document_ids) if document_ids is not None else 0,
        "selected_document_count": 0,
        "candidate_limit": bound,
        "temporal_date_field": "|".join(DATE_KEYS) + "|document_date",
        "duration_ms": 0.0,
        "reason": None,
    }

    if document_ids is not None and not document_ids:
        # Requête thématique dont la phase A n'a rien trouvé : basculer en global
        # renverrait le document le plus récent HORS SUJET. On s'abstient.
        observability["reason"] = "temporal_leg_skipped"
        observability["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return [], observability

    sql, params = build_temporal_sql(
        rag_space_id=rag_space_id, intent=intent, limit=bound, document_ids=document_ids,
    )

    try:
        with connection.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    except Exception:
        # Le leg est un COMPLÉMENT : s'il échoue, la question doit rester servie par
        # le pipeline historique plutôt que remonter une erreur à l'utilisateur.
        logger.exception("temporal.leg SQL error — leg ignore")
        observability["reason"] = "temporal_leg_skipped"
        observability["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return [], observability

    # `DISTINCT ON` impose son propre ORDER BY (par document) : on rétablit ici l'ordre
    # temporel. Le tri porte sur AU PLUS `limit` lignes déjà filtrées — ce n'est pas un
    # parcours du RagSpace.
    rows = sorted(rows, key=lambda r: r[1], reverse=intent.descending)

    candidates = []
    for rank, row in enumerate(rows):
        (did, doc_date, chunk_id, text, chunk_index, page_number, meta,
         filename, document_date) = row
        candidates.append(_project(
            document_id=did, doc_date=doc_date, chunk_id=chunk_id, text=text,
            chunk_index=chunk_index, page_number=page_number, meta=meta,
            filename=filename, document_date=document_date,
            intent=intent, rank=rank,
        ))

    observability["temporal_leg_applied"] = bool(candidates)
    observability["metadata_candidates_count"] = len(rows)
    observability["selected_document_count"] = len(candidates)
    observability["reason"] = (
        intent.reason_code() if candidates else "temporal_no_dated_document"
    )
    observability["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return candidates, observability


def _meta_as_dict(meta) -> dict:
    return meta if isinstance(meta, dict) else {}


def _project(*, document_id, doc_date, chunk_id, text, chunk_index, page_number, meta,
             filename, document_date, intent: TemporalQueryIntent, rank: int) -> dict:
    """Met un candidat au format EXACT attendu par le reste du pipeline.

    La forme reproduit celle de `chunk_repository` (métadonnées sous `source.meta`),
    sinon la diversification, le tri temporel et la projection en sources liraient à
    côté.
    """
    meta_dict = _meta_as_dict(meta)
    iso = doc_date.isoformat() if doc_date is not None else None
    return {
        "chunk_id": chunk_id,
        "text": text,
        # PAS de score sémantique inventé : ce candidat n'a pas été retrouvé par
        # similarité. `temporal_priority` porte l'ordre voulu, et c'est lui — et lui
        # seul — que l'orchestrateur utilise pour placer ces candidats.
        "score": 0.0,
        "temporal_priority": rank,
        "retrieval_leg": TEMPORAL_LEG,
        "temporal_reason": intent.reason_code(),
        "chunk_index": chunk_index,
        # Ce leg n'interroge que les métadonnées des documents PRIVÉS : la provenance
        # est connue ici, elle est donc déclarée ici (cf. domain/provenance.py).
        "source_type": SOURCE_TYPE_WORKSPACE,
        "source": {
            "source_type": SOURCE_TYPE_WORKSPACE,
            "document_id": document_id,
            "filename": filename,
            "document_date": str(document_date) if document_date else (iso[:10] if iso else None),
            "page_number": page_number,
            "label": filename,
            "retrieval_leg": TEMPORAL_LEG,
            "temporal_reason": intent.reason_code(),
            "meta": meta_dict,
        },
    }
