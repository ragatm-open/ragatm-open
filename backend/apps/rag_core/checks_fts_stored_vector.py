"""Garde de démarrage du chemin FTS « vecteur stocké ».

La colonne `text_tsv_french` est une colonne GÉNÉRÉE : l'expression
`to_tsvector('french', text)` est figée dans le schéma PostgreSQL. Le code
applicatif, lui, lit la config dans `RAG_FTS_CONFIG`. Ces deux sources peuvent
diverger — et une divergence ne planterait PAS : la requête renverrait des
résultats calculés avec une config différente de celle demandée. C'est le mode
de panne le plus dangereux du lot : silencieux et faussant le classement.

D'où ce refus au démarrage. Les checks ne coûtent RIEN quand le drapeau est
désactivé (défaut, et état de la production) : aucune introspection n'est faite.
"""

from django.core.checks import Error, Warning

CHECK_TABLES = ("rag_chunk", "domain_corpus_chunk")
STORED_COLUMN = "text_tsv_french"
REQUIRED_CONFIG = "french"


def _flag_on() -> bool:
    from django.conf import settings

    return bool(getattr(settings, "RAG_FTS_STORED_VECTOR_ENABLED", False))


def check_fts_stored_vector_config(app_configs, **kwargs):
    """E120 — le drapeau exige `RAG_FTS_CONFIG='french'`.

    On lit la config via `fts_config()` (et non le setting brut) pour évaluer la
    valeur EFFECTIVE : une valeur hors whitelist retombe sur 'simple', ce qui
    doit être refusé tout autant qu'un 'simple' explicite.
    """
    if not _flag_on():
        return []

    from apps.rag_core.repositories.chunk_repository import fts_config

    effective = fts_config()
    if effective == REQUIRED_CONFIG:
        return []

    return [
        Error(
            "RAG_FTS_STORED_VECTOR_ENABLED est activé mais la configuration FTS "
            f"effective est '{effective}' au lieu de '{REQUIRED_CONFIG}'.",
            hint=(
                "La colonne générée fige to_tsvector('french', text) dans le schéma. "
                "L'utiliser avec une autre config renverrait des résultats calculés "
                "avec la mauvaise langue, sans erreur. Corriger RAG_FTS_CONFIG=french, "
                "ou désactiver RAG_FTS_STORED_VECTOR_ENABLED."
            ),
            id="rag_core.E120",
        )
    ]


def _introspect(cursor):
    """Retourne (tables_sans_colonne, tables_sans_index_gin)."""
    cursor.execute(
        """
        SELECT c.relname
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = current_schema()
          AND c.relname = ANY(%s)
          AND a.attname = %s
          AND a.attnum > 0 AND NOT a.attisdropped
          AND a.attgenerated = 's'
        """,
        [list(CHECK_TABLES), STORED_COLUMN],
    )
    with_column = {r[0] for r in cursor.fetchall()}

    cursor.execute(
        """
        SELECT t.relname
        FROM pg_index i
        JOIN pg_class t ON t.oid = i.indrelid
        JOIN pg_class ix ON ix.oid = i.indexrelid
        JOIN pg_am am ON am.oid = ix.relam
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(i.indkey)
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = current_schema()
          AND t.relname = ANY(%s)
          AND a.attname = %s
          AND am.amname = 'gin'
          AND i.indisvalid
        """,
        [list(CHECK_TABLES), STORED_COLUMN],
    )
    with_index = {r[0] for r in cursor.fetchall()}

    missing_column = [t for t in CHECK_TABLES if t not in with_column]
    missing_index = [t for t in CHECK_TABLES if t in with_column and t not in with_index]
    return missing_column, missing_index


def check_fts_stored_vector_schema(app_configs, **kwargs):
    """E121/E122 — le schéma porte-t-il réellement la colonne et son index GIN ?

    Sans colonne, la requête lèverait `UndefinedColumn` à la PREMIÈRE recherche
    utilisateur : bruyant, mais après la mise en service. Sans index, elle
    répondrait juste — en seq scan, soit un effondrement de latence silencieux.
    On refuse donc le démarrage dans les deux cas.

    Base injoignable : on n'invente pas de verdict. Warning (jamais Error), car
    des commandes légitimes (`collectstatic`, build d'image) tournent sans base ;
    le mode dégradé reste couvert par l'échec bruyant côté PostgreSQL.
    """
    if not _flag_on():
        return []

    from django.db import connection
    from django.db.utils import Error as DBError

    if connection.vendor != "postgresql":
        return []

    try:
        with connection.cursor() as cur:
            missing_column, missing_index = _introspect(cur)
    except DBError as exc:
        return [
            Warning(
                "RAG_FTS_STORED_VECTOR_ENABLED est activé mais le schéma n'a pas pu "
                f"être vérifié (base injoignable : {type(exc).__name__}).",
                hint=(
                    "Rejouer `manage.py check` une fois la base accessible. Le chemin "
                    "vecteur stocké échouera bruyamment si la colonne manque."
                ),
                id="rag_core.W120",
            )
        ]

    errors = []
    if missing_column:
        errors.append(
            Error(
                "RAG_FTS_STORED_VECTOR_ENABLED est activé mais la colonne générée "
                f"{STORED_COLUMN} manque sur : {', '.join(missing_column)}.",
                hint=(
                    "Appliquer les migrations rag_core.0039/0040 et corpus.0006/0007 "
                    "AVANT d'activer le drapeau, ou désactiver le drapeau."
                ),
                id="rag_core.E121",
            )
        )
    if missing_index:
        errors.append(
            Error(
                f"La colonne {STORED_COLUMN} existe sans index GIN valide sur : "
                f"{', '.join(missing_index)}.",
                hint=(
                    "Sans index, la recherche plein texte dégénère en parcours "
                    "séquentiel. Appliquer rag_core.0040 / corpus.0007 (ou attendre "
                    "la fin d'un CREATE INDEX CONCURRENTLY en cours)."
                ),
                id="rag_core.E122",
            )
        )
    return errors
