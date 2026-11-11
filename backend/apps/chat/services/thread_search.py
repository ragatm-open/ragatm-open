"""Recherche des conversations de l'utilisateur.

Lecture seule, dans le PostgreSQL existant : aucun index, aucune extension, aucune
migration. Le titre et le contenu des messages sont comparés par un motif regex
insensible à la casse (``~*``) dont les voyelles et quelques consonnes sont
remplacées par des classes d'accents FIXES générées ici.

Autorisation (bloquante, cf. plan §9) — les DEUX conditions, toujours :

* ``ChatThread.user = request.user`` : aucune conversation d'un autre utilisateur,
  quel que soit son rôle (un administrateur ne lit pas les fils d'un membre) ;
* ``ChatThread.rag_space_id IN searchable_rag_space_ids(user)`` : prédicat canonique
  ``visible_ragspaces_for_user(mode="content")`` sur les workspaces ACTIFS, recalculé
  à chaque requête. ``user=`` seul laisserait passer les fils d'un espace révoqué.

Les extraits ne sont lus que sur les fils déjà filtrés ainsi, et seulement dans les
messages éligibles (``role`` user/assistant, ``status`` vide ou ``completed``) :
jamais ``system``, ni un message en cours ou échoué, ni ``sources``/``meta``, ni le
résumé de mémoire du fil.

Sécurité des motifs : le motif n'est composé que de littéraux échappés et de classes
fixes (aucun quantificateur venu de l'utilisateur, donc pas de ReDoS) et il est passé
en PARAMÈTRE SQL (``__iregex``), jamais interpolé.
"""
from __future__ import annotations

import base64
import binascii
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from uuid import UUID

from django.db import connection, transaction
from django.db.utils import OperationalError

Q_MIN_CHARS = 2
Q_MAX_CHARS = 200
MAX_TOKENS = 5
MIN_TOKEN_CHARS = 2
DEFAULT_LIMIT = 20
MAX_LIMIT = 50
STATEMENT_TIMEOUT = "3s"
SNIPPET_RADIUS = 80
#: Messages éligibles : ni système, ni en cours, ni échoués.
SEARCHABLE_ROLES = ("user", "assistant")
SEARCHABLE_STATUSES = ("", "completed")
#: Classes d'accents FIXES (lettre de base pliée → classe, majuscules comprises).
_ACCENT_CLASSES = {
    "a": "aàâäáãAÀÂÄÁÃ",
    "e": "eéèêëEÉÈÊË",
    "i": "iîïíìIÎÏÍÌ",
    "o": "oôöóòOÔÖÓÒ",
    "u": "uùûüúUÙÛÜÚ",
    "c": "cçCÇ",
    "n": "nñNÑ",
    "y": "yÿYŸ",
}


class SearchError(Exception):
    """Paramètre invalide : code renvoyé tel quel (HTTP 400)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SearchTimeout(Exception):
    """Requête interrompue par ``statement_timeout`` (HTTP 503)."""


@dataclass(frozen=True)
class SearchPage:
    results: list
    next: Optional[str]


# --------------------------------------------------------------------------- #
# Autorisation
# --------------------------------------------------------------------------- #
def searchable_rag_space_ids(user) -> list:
    """Espaces dont l'utilisateur peut LIRE le contenu, sur ses workspaces actifs.

    Prédicat canonique ``visible_ragspaces_for_user(mode="content")`` — jamais
    ``mode="catalog"`` (l'administrateur y voit des espaces qu'il ne peut pas ouvrir).
    Les workspaces inactifs sont exclus, comme dans la navigation : c'est plus strict
    que ``require_ragspace_access``, jamais plus large."""
    from apps.workspaces.models import Membership
    from apps.workspaces.services.access import visible_ragspaces_for_user

    ids: list = []
    workspace_ids = (Membership.objects.filter(user=user, workspace__is_active=True)
                     .values_list("workspace_id", flat=True))
    for wid in workspace_ids:
        ids.extend(visible_ragspaces_for_user(user=user, workspace_id=wid, mode="content")
                   .values_list("id", flat=True))
    return ids


# --------------------------------------------------------------------------- #
# Motifs
# --------------------------------------------------------------------------- #
def fold(text: str) -> str:
    """Minuscules sans diacritiques (NFD, retrait des marques combinantes)."""
    decomposed = unicodedata.normalize("NFD", text or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def parse_terms(q: str) -> list:
    """Jetons de la requête : ``"…"`` = phrase exacte ; au plus 5 jetons ; jetons de
    moins de 2 caractères ignorés. Lève ``q_too_short`` / ``q_too_long``."""
    q = (q or "").strip()
    if len(q) < Q_MIN_CHARS:
        raise SearchError("q_too_short")
    if len(q) > Q_MAX_CHARS:
        raise SearchError("q_too_long")
    terms = []
    for phrase, word in re.findall(r'"([^"]*)"|(\S+)', q):
        term = " ".join((phrase or word).split())
        if len(term) >= MIN_TOKEN_CHARS:
            terms.append(term)
    if not terms:
        raise SearchError("q_too_short")
    return terms[:MAX_TOKENS]


def build_accent_pattern(term: str) -> str:
    """Motif regex DÉTERMINISTE d'un jeton : classes d'accents fixes, autres
    alphanumériques littéraux, tout le reste échappé, espace d'une phrase → ``\\s+``.
    Aucun opérateur saisi par l'utilisateur ne survit."""
    out = []
    for ch in fold(term):
        if ch.isspace():
            if not out or out[-1] != r"\s+":
                out.append(r"\s+")
        elif ch in _ACCENT_CLASSES:
            out.append("[" + _ACCENT_CLASSES[ch] + "]")
        elif ch.isalnum():
            out.append(ch)
        else:
            out.append("\\" + ch)
    return "".join(out)


# --------------------------------------------------------------------------- #
# Curseur opaque (keyset updated_at DESC, id DESC)
# --------------------------------------------------------------------------- #
def encode_cursor(updated_at: datetime, thread_id) -> str:
    raw = f"{updated_at.isoformat()}|{thread_id}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        stamp, thread_id = raw.split("|", 1)
        return datetime.fromisoformat(stamp), UUID(thread_id)
    except (ValueError, binascii.Error, UnicodeError):
        raise SearchError("invalid_cursor") from None


# --------------------------------------------------------------------------- #
# Extraits
# --------------------------------------------------------------------------- #
def make_snippet(content: str, regexes: list) -> Optional[str]:
    """Fenêtre de ±80 caractères autour de la première occurrence d'un jeton.
    Texte brut (Markdown laissé tel quel), espaces normalisés."""
    text = " ".join((content or "").split())
    first = None
    for rx in regexes:
        m = rx.search(text)
        if m and (first is None or m.start() < first.start()):
            first = m
    if first is None:
        return None
    start = max(0, first.start() - SNIPPET_RADIUS)
    end = min(len(text), first.end() + SNIPPET_RADIUS)
    return ("…" if start > 0 else "") + text[start:end] + ("…" if end < len(text) else "")


# --------------------------------------------------------------------------- #
# Recherche
# --------------------------------------------------------------------------- #
def _is_timeout(exc: OperationalError) -> bool:
    cause = getattr(exc, "__cause__", None)
    return getattr(cause, "pgcode", None) == "57014" or getattr(cause, "sqlstate", None) == "57014" \
        or "statement timeout" in str(exc)


def search_threads(user, q: str, *, scope: str = "all", rag_space_id=None, workspace_id=None,
                   limit: int = DEFAULT_LIMIT, cursor: Optional[str] = None) -> SearchPage:
    """Une page de conversations de ``user`` correspondant à ``q``.

    Tri unique ``updated_at DESC, id DESC`` (variante simple du plan §11) ; ``match``
    indique où porte la correspondance. Aucun total. Un filtre d'espace ou de
    workspace hors du périmètre autorisé rend une page vide, indiscernable d'une
    recherche sans résultat."""
    from apps.chat.models import ChatMessage, ChatThread

    if scope not in ("all", "title"):
        raise SearchError("invalid_parameter")
    terms = parse_terms(q)
    patterns = [build_accent_pattern(t) for t in terms]
    regexes = [re.compile(p, re.IGNORECASE) for p in patterns]
    keyset = decode_cursor(cursor) if cursor else None

    allowed = searchable_rag_space_ids(user)
    if rag_space_id is not None:
        allowed = [sid for sid in allowed if str(sid) == str(rag_space_id)]
    if workspace_id is not None and allowed:
        from apps.rag_core.models import RagSpace

        allowed = list(RagSpace.objects.filter(id__in=allowed, workspace_id=workspace_id)
                       .values_list("id", flat=True))
    if not allowed:
        return SearchPage(results=[], next=None)

    combined = "|".join(f"({p})" for p in patterns)
    try:
        with transaction.atomic():
            with connection.cursor() as c:
                # Borne LOCALE à cette transaction : la connexion retrouve son réglage ensuite.
                c.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
                ordered_ids = _ordered_matches(c, user_id=user.pk, space_ids=allowed,
                                               patterns=patterns, combined=combined, scope=scope,
                                               keyset=keyset, wanted=limit + 1)
            by_id = {t.id: t for t in (ChatThread.objects.filter(id__in=ordered_ids, user=user)
                                       .select_related("rag_space__workspace"))}
            rows = [by_id[i] for i in ordered_ids if i in by_id]
            page = rows[:limit]
            content_ids = [t.id for t in page
                           if not all(rx.search(t.title or "") for rx in regexes)]
            snippets = {}
            if content_ids and scope == "all":
                latest = (ChatMessage.objects
                          .filter(thread_id__in=content_ids, role__in=SEARCHABLE_ROLES,
                                  status__in=SEARCHABLE_STATUSES, content__iregex=combined)
                          .order_by("thread_id", "-created_at", "-id").distinct("thread_id")
                          .values_list("thread_id", "content"))
                snippets = {tid: make_snippet(content, regexes) for tid, content in latest}
    except OperationalError as exc:
        if _is_timeout(exc):
            raise SearchTimeout() from None
        raise

    results = []
    for t in page:
        title_match = all(rx.search(t.title or "") for rx in regexes)
        rs = t.rag_space
        results.append({
            "id": str(t.id),
            "title": t.title,
            "rag_space_id": str(rs.id),
            "rag_space_name": rs.name,
            "workspace_id": rs.workspace_id,
            "workspace_name": rs.workspace.name,
            "updated_at": t.updated_at.isoformat(),
            "match": "title" if title_match else "content",
            "snippet": None if title_match else snippets.get(t.id),
        })
    nxt = encode_cursor(page[-1].updated_at, page[-1].id) if len(rows) > limit and page else None
    return SearchPage(results=results, next=nxt)


#: Chemin rapide : fils parcourus dans l'ordre d'affichage, par lots, avant le passage complet.
FAST_BATCH = 500
FAST_MAX_BATCHES = 2


def _ordered_matches(c, *, user_id, space_ids, patterns, combined, scope, keyset, wanted) -> list:
    """Ids des ``wanted`` premiers fils correspondants, dans l'ordre (updated_at DESC, id DESC).

    Chemin rapide : les fils les plus récents sont examinés par lots de ``FAST_BATCH``
    (messages lus par l'index ``thread_id``) ; un terme fréquent remplit la page dès le
    premier lot. Après ``FAST_MAX_BATCHES`` lots, le passage complet unique reprend
    EXACTEMENT après le dernier fil examiné. Même prédicat SQL dans les deux chemins,
    donc même résultat que le seul passage complet."""
    found: list = []
    position = keyset
    for _ in range(FAST_MAX_BATCHES):
        sql, params = _candidate_sql(user_id=user_id, space_ids=space_ids, patterns=patterns,
                                     combined=combined, scope=scope, keyset=position,
                                     limit=FAST_BATCH, batch=True)
        c.execute(sql, params)
        rows = c.fetchall()
        found += [row[0] for row in rows if row[2]][: wanted - len(found)]
        if len(found) >= wanted or len(rows) < FAST_BATCH:
            return found
        position = (rows[-1][1], rows[-1][0])
    sql, params = _candidate_sql(user_id=user_id, space_ids=space_ids, patterns=patterns,
                                 combined=combined, scope=scope, keyset=position,
                                 limit=wanted - len(found) - 1)
    c.execute(sql, params)
    return found + [row[0] for row in c.fetchall()]


def _candidate_sql(*, user_id, space_ids, patterns, combined, scope, keyset, limit,
                   batch: bool = False) -> tuple:
    """Requête des fils candidats, dans l'ordre ``updated_at DESC, id DESC``.

    ``batch=False`` : passage complet, rend les ids des ``limit + 1`` premiers fils qui
    correspondent. ``batch=True`` : examine les ``limit`` fils suivant ``keyset`` et rend
    chacun avec ``(id, updated_at, correspond)``.

    UN SEUL passage sur les messages : seuls ceux des fils sélectionnés, filtrés par
    l'union des motifs, agrégés par fil (``bool_or`` par jeton). Chaque jeton doit
    figurer dans le titre OU dans un message éligible du fil (ET entre jetons). Un fil
    sort une seule fois (``GROUP BY`` + jointure 1:1). Mesuré : un ``EXISTS`` par jeton
    relisait toute la table une fois PAR jeton (≈ 930 ms pour 2 jetons à 63 Mo). Tout ce
    qui vient de l'utilisateur est PARAMÈTRE."""
    from apps.chat.models import ChatMessage, ChatThread

    thread_table = connection.ops.quote_name(ChatThread._meta.db_table)
    message_table = connection.ops.quote_name(ChatMessage._meta.db_table)
    select_params: list = [user_id, [str(s) for s in space_ids]]
    keyset_params = [keyset[0], keyset[0], keyset[1]] if keyset is not None else []
    threads = (f"SELECT id, title, updated_at FROM {thread_table} "
               f"WHERE user_id = %s AND rag_space_id = ANY(%s::uuid[])")
    outer_keyset = ""
    if batch:
        # Lot : la position borne la SÉLECTION des fils (avec son LIMIT).
        if keyset is not None:
            threads += " AND (updated_at < %s OR (updated_at = %s AND id < %s))"
            select_params += keyset_params
        threads += " ORDER BY updated_at DESC, id DESC LIMIT %s"
        select_params.append(limit)
    elif keyset is not None:
        # Passage complet : position dans la clause finale. Mesuré : dans la sélection, le
        # planificateur quitte le parcours séquentiel + jointure par hachage (≈ ×3).
        outer_keyset = " AND (t.updated_at < %s OR (t.updated_at = %s AND t.id < %s))"

    if scope == "title":
        match = " AND ".join("COALESCE(t.title ~* %s, false)" for _ in patterns)
        match_params = list(patterns)
        hits_cte, hits_join, hits_params = "", "", []
    else:
        flags = ", ".join(f"bool_or(m.content ~* %s) AS h{i}" for i in range(len(patterns)))
        hits_params = list(patterns) + [list(SEARCHABLE_ROLES), list(SEARCHABLE_STATUSES), combined]
        eligible = "m.role = ANY(%s) AND m.status = ANY(%s) AND m.content ~* %s"
        if batch:
            # Lot : messages lus PAR FIL (sous-requête corrélée, index thread_id). Mesuré :
            # en jointure, un motif jugé sélectif faisait relire toute la table À CHAQUE lot.
            hits_cte = ""
            hits_join = (f" LEFT JOIN LATERAL (SELECT {flags} FROM {message_table} m "
                         f"WHERE m.thread_id = t.id AND {eligible}) hits ON true")
        else:
            hits_cte = (f", hits AS (SELECT m.thread_id, {flags} FROM {message_table} m "
                        f"JOIN t ON t.id = m.thread_id WHERE {eligible} GROUP BY m.thread_id)")
            hits_join = " LEFT JOIN hits ON hits.thread_id = t.id"
        match = " AND ".join(f"(COALESCE(t.title ~* %s, false) OR COALESCE(hits.h{i}, false))"
                             for i in range(len(patterns)))
        match_params = list(patterns)

    head = f"WITH t AS ({threads}){hits_cte} "
    order = " ORDER BY t.updated_at DESC, t.id DESC"
    # Ordre des paramètres = ordre d'apparition dans la requête.
    if batch:
        # Le SELECT (motifs du titre) précède la jointure LATERAL dans le texte SQL.
        sql = f"{head}SELECT t.id, t.updated_at, ({match}) FROM t{hits_join}{order}"
        return sql, select_params + match_params + hits_params
    sql = f"{head}SELECT t.id FROM t{hits_join} WHERE {match}{outer_keyset}{order} LIMIT %s"
    return sql, (select_params + hits_params + match_params
                 + (keyset_params if outer_keyset else []) + [limit + 1])
