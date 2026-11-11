"""Préfiltrage FTS sur l'INTÉGRALITÉ d'un RagSpace.

Distinct de ``chunk_repository.search_chunks_hybrid``, qui est le retrieval
CONVERSATIONNEL : celui-là porte un ``LIMIT top_k`` (8) et **aucun filtre de
statut**. Le réutiliser ici reviendrait à présenter un top-k comme un parcours du
corpus — exactement ce que ces lots suppriment.

Ici :
- **aucun ``LIMIT`` de pertinence** : tous les candidats sont comptés puis itérés ;
- filtre ``d.status = 'READY'`` explicite ;
- double verrou ``rag_space_id`` (chunk ET document), comme le retrieval ;
- pagination par **keyset** stable — jamais d'``OFFSET`` sur de gros ensembles ;
- la requête utilisateur n'est **jamais** concaténée dans le SQL : elle est
  passée en paramètre à ``websearch_to_tsquery``.
"""
from __future__ import annotations

from typing import Any, Iterator

from django.conf import settings
from django.db import connection

# Configurations FTS pour lesquelles un index GIN EXISTE réellement
# (migrations 0001/0014 : 'simple' ; 0023 : 'french'). Toute autre valeur ferait
# un seq scan sur 156 Mo : on retombe sur 'simple' (fail-safe).
_INDEXED_FTS_CONFIGS = ("simple", "french")
DEFAULT_FTS_CONFIG = "simple"


def resolve_fts_configuration(*, language: str | None = None) -> str:
    """Configuration linguistique effective.

    Stratégie, documentée et testée :
      1. langue explicitement demandée, si elle a un index ;
      2. sinon ``RAG_FTS_CONFIG`` (réglage d'espace existant), si elle a un index ;
      3. sinon ``simple`` — neutre, sans stemming, robuste aux sigles et aux
         entités, et indépendant de toute langue métier.

    Aucune langue n'est codée en dur pour un domaine : seules les configurations
    réellement indexées sont acceptées.
    """
    requested = (language or "").strip().lower()
    if requested in _INDEXED_FTS_CONFIGS:
        return requested

    configured = str(getattr(settings, "RAG_FTS_CONFIG", "") or "").strip().lower()
    if configured in _INDEXED_FTS_CONFIGS:
        return configured

    return DEFAULT_FTS_CONFIG


def _cfg(configuration: str) -> str:
    """Littéral de configuration validé — jamais une valeur utilisateur."""
    c = (configuration or "").strip().lower()
    return c if c in _INDEXED_FTS_CONFIGS else DEFAULT_FTS_CONFIG


# Fonctions tsquery autorisées. Le NOM est un littéral validé ; la REQUÊTE reste
# toujours un paramètre lié.
#   websearch : langage naturel (ET implicite, « or », guillemets)
#   boolean   : expression booléenne construite PAR NOUS (& | <-> ), à partir de
#               jetons déjà assainis — l'utilisateur ne peut y injecter aucun opérateur
_TSQUERY_FUNCS = {
    "websearch": "websearch_to_tsquery",
    "boolean": "to_tsquery",
    "plain": "plainto_tsquery",
}
DEFAULT_TSQUERY_MODE = "websearch"


def _tsq(mode: str) -> str:
    return _TSQUERY_FUNCS.get((mode or "").strip().lower(), _TSQUERY_FUNCS[DEFAULT_TSQUERY_MODE])


def scan_scope(*, rag_space_id: Any) -> dict:
    """Périmètre RÉELLEMENT interrogé par le préfiltre, avant toute recherche.

    C'est le dénominateur du niveau B : combien de chunks le FTS a-t-il pu voir ?
    """
    sql = """
    SELECT
      COUNT(c.id)                       AS chunks_scanned,
      COUNT(DISTINCT c.document_id)     AS documents_text_scanned
    FROM rag_chunk c
    JOIN rag_document d ON d.id = c.document_id
    WHERE c.rag_space_id = %(space)s
      AND d.rag_space_id = %(space)s          -- verrou anti-fuite
      AND d.status = 'READY'
    """
    with connection.cursor() as cur:
        cur.execute(sql, {"space": rag_space_id})
        row = cur.fetchone()
    return {
        "chunks_scanned": int(row[0] or 0),
        "documents_text_scanned": int(row[1] or 0),
    }


def count_global_fts_candidates(
    *, rag_space_id: Any, query: str, configuration: str, mode: str = DEFAULT_TSQUERY_MODE
) -> dict:
    """Compte TOUS les candidats. Aucun ``LIMIT`` : c'est le compte exact.

    Établi AVANT tout traitement, donc avant toute dépense LLM. C'est ce compte
    qui permet de refuser une recherche trop large plutôt que de tronquer.
    """
    cfg = _cfg(configuration)
    tsq = _tsq(mode)
    sql = f"""
    SELECT
      COUNT(c.id)                    AS candidate_chunks,
      COUNT(DISTINCT c.document_id)  AS candidate_documents
    FROM rag_chunk c
    JOIN rag_document d ON d.id = c.document_id
    WHERE c.rag_space_id = %(space)s
      AND d.rag_space_id = %(space)s
      AND d.status = 'READY'
      AND to_tsvector('{cfg}', c.text) @@ {tsq}('{cfg}', %(q)s)
    """
    with connection.cursor() as cur:
        cur.execute(sql, {"space": rag_space_id, "q": query})
        row = cur.fetchone()
    return {
        "candidate_chunks": int(row[0] or 0),
        "candidate_documents": int(row[1] or 0),
    }


def iterate_global_fts_candidates(
    *, rag_space_id: Any, query: str, configuration: str, page_size: int = 200,
    mode: str = DEFAULT_TSQUERY_MODE,
) -> Iterator[dict]:
    """Itère TOUS les candidats, par pages keyset.

    Ordre déterministe et total : ``(document_id, page_number, chunk_id)``. La
    pagination reprend strictement après la dernière clé lue — donc **ni doublon,
    ni omission**, même si des chunks sont insérés pendant l'itération.

    ``OFFSET`` serait à la fois O(n²) et instable. Rien n'est chargé en mémoire
    au-delà d'une page.
    """
    cfg = _cfg(configuration)
    tsq = _tsq(mode)
    sql = f"""
    SELECT
      c.id::text          AS chunk_id,
      c.document_id::text AS document_id,
      c.page_number,
      c.text
    FROM rag_chunk c
    JOIN rag_document d ON d.id = c.document_id
    WHERE c.rag_space_id = %(space)s
      AND d.rag_space_id = %(space)s
      AND d.status = 'READY'
      AND to_tsvector('{cfg}', c.text) @@ {tsq}('{cfg}', %(q)s)
      AND (
        %(after_doc)s::uuid IS NULL
        OR (c.document_id, COALESCE(c.page_number, -1), c.id)
           > (%(after_doc)s::uuid, %(after_page)s::int, %(after_chunk)s::uuid)
      )
    ORDER BY c.document_id, COALESCE(c.page_number, -1), c.id
    LIMIT %(page_size)s
    """
    after_doc = after_page = after_chunk = None

    while True:
        with connection.cursor() as cur:
            cur.execute(sql, {
                "space": rag_space_id,
                "q": query,
                "after_doc": after_doc,
                "after_page": after_page,
                "after_chunk": after_chunk,
                # `page_size` borne la PAGE, pas le nombre total de candidats :
                # la boucle continue jusqu'à épuisement. Aucune troncature.
                "page_size": int(page_size),
            })
            rows = cur.fetchall()

        if not rows:
            return

        for chunk_id, document_id, page_number, text in rows:
            yield {
                "chunk_id": chunk_id,
                "document_id": document_id,
                "page_number": page_number,
                "text": text,
            }

        last_chunk_id, last_doc_id, last_page, _ = rows[-1]
        after_doc = last_doc_id
        after_page = -1 if last_page is None else int(last_page)
        after_chunk = last_chunk_id

        if len(rows) < page_size:
            return


def term_document_frequencies(
    *, rag_space_id: Any, terms: list[str], configuration: str
) -> dict[str, int]:
    """Fréquence de chaque terme dans le périmètre (nb de chunks le contenant).

    UNE seule requête, indexée. Sert à distinguer les termes DISCRIMINANTS (rares)
    des termes courants : c'est ce qui permet de construire une requête précise
    plutôt que de subir un OU large qui ramène des milliers de candidats.

    Les termes sont passés en PARAMÈTRE (tableau), jamais concaténés dans le SQL.
    """
    if not terms:
        return {}

    cfg = _cfg(configuration)
    sql = f"""
    SELECT t.term, COUNT(c.id) AS df
    FROM unnest(%(terms)s::text[]) AS t(term)
    LEFT JOIN rag_chunk c
      ON c.rag_space_id = %(space)s
     AND to_tsvector('{cfg}', c.text) @@ plainto_tsquery('{cfg}', t.term)
    LEFT JOIN rag_document d
      ON d.id = c.document_id
     AND d.rag_space_id = %(space)s
     AND d.status = 'READY'
    WHERE c.id IS NULL OR d.id IS NOT NULL
    GROUP BY t.term
    """
    with connection.cursor() as cur:
        cur.execute(sql, {"space": rag_space_id, "terms": list(terms)})
        return {row[0]: int(row[1] or 0) for row in cur.fetchall()}
