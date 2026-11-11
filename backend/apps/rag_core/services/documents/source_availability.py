"""Disponibilité de la source documentaire — une seule implémentation.

POURQUOI CE MODULE
------------------
Trois endroits ont besoin de répondre à « le fichier source de ce document est-il
réellement lisible ? » : la réindexation (avant de tenter une extraction), la
suppression (avant d'annoncer qu'un fichier a été supprimé) et le pré-vol de
``reembed_documents`` (avant de lancer N opérations vouées à l'échec). Sans point
commun, chacun aurait sa propre sémantique et le rapport de l'un contredirait
celui de l'autre.

DEUX ÉTATS DISTINCTS, JAMAIS CONFONDUS
--------------------------------------
- ``storage_file`` vide      -> le document n'a jamais eu de source conservée ;
- ``storage_file`` renseigné mais fichier absent -> la source a été perdue OU
  n'a jamais été écrite à cet emplacement.

Le second cas est celui qui manquait : il tombait auparavant dans un
``FileNotFoundError`` non classé, dégradé en erreur *environnementale*, donc
présenté à l'opérateur comme « réessayer » — un conseil qui ne peut jamais
aboutir. Il porte désormais son propre code, :data:`SOURCE_FILE_MISSING`.

VIA L'API DE STOCKAGE, JAMAIS ``os.path``
-----------------------------------------
Le test passe par ``field.storage.exists()`` — comme
``rag_space_deletion._delete_files`` — pour rester compatible avec tout backend
configuré (``FileSystemStorage`` aujourd'hui, cf. ADR-001).

HORS DU CHEMIN CHAUD
--------------------
Ce contrôle coûte un accès disque. Il est réservé aux chemins de réindexation,
d'ingestion et de maintenance. **Il ne doit jamais être appelé depuis
``query_rag``, le retrieval, le chat ou l'embedding de requête** : un
``exists()`` par requête RAG transformerait une lecture en parcours de système de
fichiers.
"""

from __future__ import annotations

#: Le document déclare une source (``storage_file`` renseigné) mais le fichier
#: est introuvable dans le stockage. Aucune reprise à l'identique ne peut
#: aboutir : il faut fournir la source à nouveau.
SOURCE_FILE_MISSING = "source_file_missing"

#: Le document n'a aucune source conservée. Code historique, conservé tel quel.
NO_STORAGE_FILE = "no_storage_file_for_reindex"


def declared_source_name(doc) -> str:
    """Nom de la source déclarée en base (`""` si le document n'en a pas)."""
    f = getattr(doc, "storage_file", None)
    if f is None:
        return ""
    return getattr(f, "name", "") or ""


def source_file_exists(doc) -> bool:
    """``True`` si la source déclarée est réellement présente dans le stockage.

    ``False`` si aucune source n'est déclarée, si le fichier est absent, ou si le
    stockage refuse de répondre. Ne lève jamais : un contrôle de disponibilité qui
    lève ne rend service à aucun de ses trois appelants.
    """
    name = declared_source_name(doc)
    if not name:
        return False
    try:
        return bool(doc.storage_file.storage.exists(name))
    except Exception:  # noqa: BLE001 — stockage indisponible = source indisponible
        return False


def source_error_code(doc) -> str:
    """Code d'erreur décrivant l'indisponibilité, `""` si la source est là.

    Renvoie :data:`NO_STORAGE_FILE` ou :data:`SOURCE_FILE_MISSING` — deux
    situations qui appellent des gestes différents et ne doivent pas être fondues.
    """
    if not declared_source_name(doc):
        return NO_STORAGE_FILE
    return "" if source_file_exists(doc) else SOURCE_FILE_MISSING
