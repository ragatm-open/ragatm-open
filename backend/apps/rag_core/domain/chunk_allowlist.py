"""Liste blanche de fragments — bornage OPTIONNEL d'un retrieval à un ensemble figé.

Pourquoi ce module existe
-------------------------
Un appelant peut exiger que le retrieval ne voie QUE des fragments figés à l'avance
(le corpus gelé d'un run un module métier : CORPUS-SNAPSHOT-HARDENING). Sans borne, le
retrieval lit l'espace en direct : un fragment visuel publié après le gel, un document
non rattaché ou la nouvelle version d'un document réindexé deviennent visibles au
milieu d'une exécution qui prétend être reproductible.

Contrat
-------
- **Absent = comportement strictement inchangé.** Aucun fragment SQL n'est ajouté, aucun
  paramètre non plus : le chat et tout retrieval non borné exécutent la requête
  historique octet pour octet.
- **Présent = JOIN indexable** sur la table de l'appelant, dans CHAQUE leg qui lit des
  fragments (lexical, vectoriel, temporel, corpus métier). Jamais une liste d'UUID
  transmise depuis Python : la table est l'autorité, l'index fait le travail.
- ``rag_core`` ne connaît PAS un module métier : la liste blanche décrit une table et des
  colonnes, validées comme identifiants SQL. Le propriétaire de la table (un module métier) la
  construit ; le socle se contente de joindre.

Portée
------
La liste blanche active vit dans une ``ContextVar`` posée par ``query_rag`` pour la durée
d'UN appel, puis restaurée. Les repositories la lisent au moment de construire leur
SQL : aucune signature de fonction de recherche injectée n'a donc à changer (les
doublures de test historiques restent valides).
"""
from __future__ import annotations

import re
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterable, Iterator

from django.db import connection

#: Magasins de fragments que le socle sait borner.
STORE_PRIVATE = "private"  # rag_chunk (documents d'un RagSpace)
STORE_CORPUS = "corpus"  # domain_corpus_chunk (corpus métier)
STORES: frozenset[str] = frozenset({STORE_PRIVATE, STORE_CORPUS})

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

#: Alias SQL de la table de liste blanche. Préfixé pour ne jamais entrer en collision
#: avec les alias des requêtes hôtes (c, d, s, co, bd, dt).
_ALIAS = "_allow"
_KEY_PARAM = "_allowlist_key"
_KIND_PARAM = "_allowlist_kind"
_DOCS_PARAM = "_allowlist_documents"


class ChunkAllowlistError(ValueError):
    """Liste blanche mal formée — refusée AVANT toute requête."""


@dataclass(frozen=True)
class ChunkAllowlist:
    """Une liste blanche de fragments, décrite par la table qui la porte.

    ``key`` sélectionne l'ensemble (l'identifiant du run pour un module métier) ; ``kind_column``
    distingue le magasin, parce que ``rag_chunk`` et ``domain_corpus_chunk`` sont deux
    espaces d'identifiants distincts. ``private_kind``/``corpus_kind`` sont les valeurs
    que la table de l'appelant emploie pour chacun.
    """

    table: str
    key_column: str
    key: str
    private_kind: str
    corpus_kind: str
    chunk_column: str = "chunk_id"
    kind_column: str = "source_kind"
    #: Type SQL de ``key_column`` : la clé est castée explicitement, pour que la
    #: comparaison reste indexable quel que soit le mode de liaison du pilote.
    key_type: str = "uuid"
    #: Restriction OPTIONNELLE à un sous-ensemble de documents de la liste blanche
    #: (EVIDENCE-ROLE-ROUTING : une passe de collecte par rôle
    #: métier). ``None`` = aucune restriction, SQL octet pour octet celui d'avant.
    #: Un tuple vide est REFUSÉ : « aucun document » n'est pas une borne, c'est un
    #: appel qui ne devait pas avoir lieu.
    document_ids: tuple[str, ...] | None = None
    document_column: str = "document_id"

    def __post_init__(self) -> None:
        for name in (
            "table", "key_column", "chunk_column", "kind_column", "key_type", "document_column"
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not _IDENTIFIER.match(value):
                raise ChunkAllowlistError(f"identifiant SQL invalide pour {name}: {value!r}")
        if not str(self.key or "").strip():
            raise ChunkAllowlistError("clé de liste blanche vide")
        if not self.private_kind or not self.corpus_kind:
            raise ChunkAllowlistError("valeurs de magasin absentes")
        if self.document_ids is not None:
            if isinstance(self.document_ids, (str, bytes)):
                raise ChunkAllowlistError("restriction documentaire : séquence attendue")
            try:
                normalized = tuple(sorted({str(uuid.UUID(str(d))) for d in self.document_ids}))
            except (TypeError, ValueError, AttributeError) as exc:
                raise ChunkAllowlistError("restriction documentaire : UUID invalide") from exc
            if not normalized:
                raise ChunkAllowlistError("restriction documentaire vide")
            object.__setattr__(self, "document_ids", normalized)

    def restricted_to(self, document_ids) -> "ChunkAllowlist":
        """Même liste blanche, bornée en plus aux documents ``document_ids``."""
        from dataclasses import replace

        return replace(self, document_ids=tuple(document_ids))

    def kind_for(self, store: str) -> str:
        if store == STORE_PRIVATE:
            return self.private_kind
        if store == STORE_CORPUS:
            return self.corpus_kind
        raise ChunkAllowlistError(f"magasin inconnu : {store!r}")

    def sql_join(self, *, alias: str, store: str) -> tuple[str, dict]:
        """Fragment ``JOIN`` + paramètres bornant l'alias ``alias`` à la liste blanche."""
        if not _IDENTIFIER.match(alias):
            raise ChunkAllowlistError(f"alias SQL invalide : {alias!r}")
        join = (
            f"    JOIN {self.table} {_ALIAS} ON {_ALIAS}.{self.chunk_column} = {alias}.id"
            f" AND {_ALIAS}.{self.key_column} = %({_KEY_PARAM})s::{self.key_type}"
            f" AND {_ALIAS}.{self.kind_column} = %({_KIND_PARAM})s"
            + (
                f" AND {_ALIAS}.{self.document_column} = ANY(%({_DOCS_PARAM})s::uuid[])"
                if self.document_ids is not None
                else ""
            )
            + "   -- 🔒 liste blanche de fragments (corpus figé)\n"
        )
        params = {_KEY_PARAM: str(self.key), _KIND_PARAM: self.kind_for(store)}
        if self.document_ids is not None:
            params[_DOCS_PARAM] = list(self.document_ids)
        return join, params

    def members(self, *, store: str, chunk_ids: Iterable[str]) -> set[str]:
        """Sous-ensemble de ``chunk_ids`` réellement présent dans la liste blanche."""
        ids = sorted({str(c) for c in chunk_ids if c})
        if not ids:
            return set()
        sql = (
            f"SELECT {self.chunk_column}::text FROM {self.table}"
            f" WHERE {self.key_column} = %s::{self.key_type} AND {self.kind_column} = %s"
            f" AND {self.chunk_column} = ANY(%s::uuid[])"
        )
        params = [str(self.key), self.kind_for(store), ids]
        if self.document_ids is not None:
            sql += f" AND {self.document_column} = ANY(%s::uuid[])"
            params.append(list(self.document_ids))
        with connection.cursor() as cur:
            cur.execute(sql, params)
            return {row[0] for row in cur.fetchall()}


_ACTIVE: ContextVar[ChunkAllowlist | None] = ContextVar("rag_chunk_allowlist", default=None)


@contextmanager
def chunk_allowlist_scope(allowlist: ChunkAllowlist | None) -> Iterator[None]:
    """Active ``allowlist`` pour la durée du bloc, puis restaure l'état précédent."""
    if allowlist is not None and not isinstance(allowlist, ChunkAllowlist):
        raise ChunkAllowlistError("objet de liste blanche inattendu")
    token = _ACTIVE.set(allowlist)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def active_chunk_allowlist() -> ChunkAllowlist | None:
    return _ACTIVE.get()


def allowlist_join(*, alias: str, store: str) -> tuple[str, dict]:
    """``("", {})`` hors liste blanche : la requête hôte reste celle d'avant."""
    allowlist = _ACTIVE.get()
    if allowlist is None:
        return "", {}
    return allowlist.sql_join(alias=alias, store=store)


__all__ = [
    "STORES",
    "STORE_CORPUS",
    "STORE_PRIVATE",
    "ChunkAllowlist",
    "ChunkAllowlistError",
    "active_chunk_allowlist",
    "allowlist_join",
    "chunk_allowlist_scope",
]
