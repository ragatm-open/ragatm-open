# backend/apps/rag_core/repositories/chunk_repository.py
from __future__ import annotations

import json
import math
import re
from typing import Any, Callable, Iterable, Sequence

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import connection, transaction

from apps.rag_core.domain.eligibility import (
    SQL_ELIGIBILITY_CLAUSES as ELIGIBILITY_CLAUSES,
    SQL_RAG_SPACE_JOIN as ELIGIBILITY_JOIN,
)
from apps.rag_core.domain.chunk_allowlist import STORE_PRIVATE, allowlist_join
from apps.rag_core.domain.fts_plan import FtsPlanExecution, execute_fts_plan
from apps.rag_core.domain.provenance import SOURCE_TYPE_WORKSPACE
from apps.rag_core.domain.ranking import SQL_TIE_BREAK

# Stopwords FR minimal (sans dépendances) + verbes fréquents de questions
STOPWORDS_FR = {
    "le", "la", "les", "un", "une", "des", "du", "de", "d", "a", "à", "au", "aux",
    "et", "ou", "mais", "donc", "or", "ni", "car",
    "est", "sont", "été", "etre", "être",
    "que", "qui", "quoi", "dont", "où",
    "ce", "cet", "cette", "ces", "ça", "cela",
    "je", "tu", "il", "elle", "nous", "vous", "ils", "elles",
    "mon", "ton", "son", "notre", "votre", "leur",
    "quelle", "quelles", "quel", "quels",
    "dans", "sur", "sous", "avec", "sans", "pour", "par", "vers", "chez",
    "document",
    # verbes/intent fréquents
    "explique", "expliquer", "expliques", "expliquerai",
    "donne", "donner", "donnes", "donnemoi", "donne-moi",
    "dis", "dire", "dit", "raconte", "décris", "decris",
    "liste", "lister", "enumerer", "énumère", "énumérer",
    "montre", "trouve", "recherche", "cherche",
    # bruit conversationnel
    "moi", "toi", "svp", "s'il", "sil", "plait", "merci",
}

_TOKEN_RE = re.compile(r"[a-zA-ZÀ-ÿ0-9]+")
# CapWords / acronymes (ex: "Nordexia", "Conseil", "Valteria", "Tessaly")
_CAPWORD_RE = re.compile(r"\b([A-ZÀ-ÖØ-Ý][a-zà-öø-ÿ]{2,}|[A-Z0-9]{2,})\b")

# Configurations FTS PostgreSQL autorisées (B3). Le nom de la config est un
# `regconfig` qui DOIT être un littéral dans le SQL (sinon l'index d'expression
# `to_tsvector('<config>', text)` n'est pas utilisé par le planner). Comme la
# valeur provient d'un setting de confiance (jamais d'une entrée utilisateur), on
# l'interpole après validation stricte par cette whitelist.
_ALLOWED_FTS_CONFIGS = {"simple", "french", "english"}


def fts_config() -> str:
    """Config FTS effective (``RAG_FTS_CONFIG``, défaut ``simple``), validée.

    'simple'  : pas de stemming/stop words (robuste sigles/entités, défaut actuel).
    'french'  : stemming + stop words FR (meilleur rappel morphologique).
    Toute valeur hors whitelist retombe sur 'simple' (fail-safe).
    """
    cfg = str(getattr(settings, "RAG_FTS_CONFIG", "simple") or "simple").strip().lower()
    return cfg if cfg in _ALLOWED_FTS_CONFIGS else "simple"


# Colonne générée stockée (`GENERATED ALWAYS AS ... STORED`) portant le vecteur
# lexical pré-calculé. Elle matérialise EXACTEMENT `to_tsvector('french', text)` :
# la config est figée dans le schéma, donc l'usage de la colonne n'est licite que
# si `RAG_FTS_CONFIG` vaut la même chose. Cet invariant est vérifié au démarrage
# (checks rag_core.E120/E121/E122) — voir apps/rag_core/checks.py.
STORED_VECTOR_COLUMN = "text_tsv_french"
STORED_VECTOR_CONFIG = "french"


def stored_vector_enabled() -> bool:
    """Le chemin « vecteur stocké » est-il demandé (``RAG_FTS_STORED_VECTOR_ENABLED``) ?"""
    return bool(getattr(settings, "RAG_FTS_STORED_VECTOR_ENABLED", False))


def fts_vector_sql(alias: str = "c") -> str:
    """Fragment SQL désignant le vecteur lexical du chunk ``alias``.

    Flag OFF (défaut) : expression calculée à l'exécution — chemin historique,
    strictement inchangé, y compris pour ``RAG_FTS_CONFIG`` ∈ {simple, english}.

    Flag ON : référence la colonne stockée. AUCUN repli silencieux n'est prévu —
    si la config ne correspond pas au schéma, on lève ; si la colonne manque,
    PostgreSQL lève. Une dégradation muette rendrait le flag indétectable.
    """
    if not stored_vector_enabled():
        return f"to_tsvector('{fts_config()}', {alias}.text)"

    cfg = fts_config()
    if cfg != STORED_VECTOR_CONFIG:
        raise ImproperlyConfigured(
            "RAG_FTS_STORED_VECTOR_ENABLED=1 exige RAG_FTS_CONFIG='%s' "
            "(la colonne stockée fige cette config dans le schéma) ; valeur lue : '%s'."
            % (STORED_VECTOR_CONFIG, cfg)
        )
    return f"{alias}.{STORED_VECTOR_COLUMN}"


def _sanitize_ts_token(t: str) -> str:
    # garde uniquement alnum (sécurité tsquery)
    t = re.sub(r"[^a-zA-ZÀ-ÿ0-9]", "", t)
    return t


# --------------------------------------------------------------------------- #
# Identifiants à tiret
# --------------------------------------------------------------------------- #
# « Series **A-1** », « Noteholder **2020-3** », « Seed-1 ». Le tiret y est PORTEUR :
# c'est lui qui distingue A-1 de A-2, 2020-1 de 2020-3.
#
# `_sanitize_ts_token` supprimait tout non-alphanumérique : « A-1 » devenait « a1 »,
# puis était jeté (moins de 3 caractères). Le token discriminant disparaissait, et la
# requête se réduisait à `series:*` — qui ne distingue plus rien.
#
# PostgreSQL, lui, sait très bien les traiter : en configuration `simple`,
# `to_tsvector('Series A-1')` produit `'series' 'a' '-1'`, et `to_tsquery('a-1')`
# matche « A-1 » sans matcher « A-2 ». Il suffisait de ne pas les détruire en amont.
_IDENTIFIER_RE = re.compile(r"\b([a-zA-Z0-9]{1,12}(?:-[a-zA-Z0-9]{1,12})+)\b")

#: Un identifiant purement alphabétique (« porte-monnaie », « c'est-à-dire ») n'est pas
#: un identifiant : c'est un mot composé. On exige au moins un CHIFFRE quelque part —
#: c'est ce qui fait la valeur discriminante d'une référence (`A-1`, `2020-3`, `FY2026-A`).
_HAS_DIGIT = re.compile(r"\d")

#: Référence composée d'un slash : `FIN01/FIN01-01`, `TRA51/TRA51-04a`.
#:
#: 🔴 Le piège, mesuré en P2.5-b. `_IDENTIFIER_RE` s'appuie sur `\b`, donc sur
#: `FIN01/FIN01-01` il ne capture que le fragment `FIN01-01`. Or PostgreSQL en
#: configuration `simple` tokenise la forme complète en **un seul lexème**
#: `'fin01/fin01-01'` (règle `file`) — il n'émet ni `fin01`, ni `fin01-01`. La requête
#: `fin01:* & fin01-01` ne pouvait donc JAMAIS matcher le chunk qui contient pourtant
#: la référence :
#:
#:     to_tsquery('fin01/fin01-01')     -> match
#:     to_tsquery('fin01 & fin01-01')   -> AUCUN match
#:
#: 7 des 13 échecs Mission de calibration portaient une référence de cette forme.
_SLASH_IDENTIFIER_RE = re.compile(
    r"\b([a-zA-Z]{2,5}\d{1,3}(?:/[a-zA-Z0-9]{1,12}(?:-[a-zA-Z0-9]{1,12})*)+)\b"
)


def extract_identifiers(question: str) -> list[str]:
    """Identifiants alphanumériques à tiret, normalisés pour `to_tsquery`.

    Renvoie une liste bornée, en minuscules, sans doublon. Les formes sans chiffre sont
    écartées : « c'est-à-dire » n'est pas une référence.
    """
    out: list[str] = []
    for m in _IDENTIFIER_RE.finditer(question or ""):
        raw = m.group(1)
        if not _HAS_DIGIT.search(raw):
            continue
        tok = raw.lower()
        # Sécurité tsquery : seuls [a-z0-9-] survivent, et jamais un tiret aux bords
        # (`to_tsquery('-1')` est une syntaxe invalide).
        tok = re.sub(r"[^a-z0-9-]", "", tok).strip("-")
        if not tok or "-" not in tok:
            continue
        if tok not in out:
            out.append(tok)
    return out[:4]


def extract_slash_identifiers(question: str) -> list[str]:
    """Références à slash, sous la forme de **lexèmes tsquery quotés** prêts à l'emploi.

    Renvoie `["'fin01/fin01-01'"]` et non `["fin01/fin01-01"]` : le slash n'est pas un
    opérateur tsquery valide hors quotes. Le quotage est donc structurel, pas cosmétique.

    Sécurité : seuls `[a-z0-9/-]` survivent — une apostrophe ne peut pas s'échapper du
    lexème et refermer la quote (`to_tsquery` recevrait alors une expression arbitraire).
    """
    out: list[str] = []
    for m in _SLASH_IDENTIFIER_RE.finditer(question or ""):
        raw = m.group(1)
        if not _HAS_DIGIT.search(raw):
            continue
        tok = re.sub(r"[^a-z0-9/-]", "", raw.lower()).strip("-/")
        if not tok or "/" not in tok:
            continue
        quoted = f"'{tok}'"
        if quoted not in out:
            out.append(quoted)
    return out[:4]


def _extract_capitalized_terms(question: str) -> list[str]:
    """
    Détecte des termes "entité" via mots capitalisés / acronymes dans la question brute.
    Ex:
      "explique moi Nordexia Conseil" -> ["nordexia", "conseil"]
      "news sur Valteria" -> ["valteria"]
    """
    q = (question or "").strip()
    if not q:
        return []

    hits = _CAPWORD_RE.findall(q)
    out: list[str] = []
    for h in hits:
        t = _sanitize_ts_token(h)
        if not t:
            continue
        t_low = t.lower()
        if t_low in STOPWORDS_FR:
            continue
        if len(t_low) < 3:
            continue
        out.append(t_low)

    # unique stable
    seen: set[str] = set()
    deduped: list[str] = []
    for x in out:
        if x not in seen:
            seen.add(x)
            deduped.append(x)
    return deduped[:6]


def _extract_keywords(question: str) -> list[str]:
    """
    Extraction fallback mots-clés (min 3 chars, stopwords filtrés).
    """
    tokens = _TOKEN_RE.findall((question or "").lower())
    keywords: list[str] = []

    for t in tokens:
        t = t.strip("'").strip()
        if len(t) < 3:
            continue
        if t in STOPWORDS_FR:
            continue
        t = _sanitize_ts_token(t)
        if not t:
            continue
        keywords.append(t)

    # unique stable
    seen: set[str] = set()
    deduped: list[str] = []
    for k in keywords:
        if k not in seen:
            seen.add(k)
            deduped.append(k)

    return deduped[:8]


# --------------------------------------------------------------------------- #
# Normalisation de requête lexicale (V2)
# --------------------------------------------------------------------------- #
# Le défaut corrigé, mesuré et reproduit en SQL. La configuration de recherche
# plein texte vaut `french` pour les DEUX magasins, alors que le corpus métier est
# anglophone. Le dictionnaire français ne connaît pas `what` / `does` / `the` comme
# mots vides : ils survivent à l'extraction, l'entrée « sélective » du plan se réduit
# à `what:*`, elle ramène des résultats NON VIDES — et le repli, conditionné à ZÉRO
# résultat, n'est donc jamais atteint. Le passage qui contient le terme exact n'est
# jamais ramené.
#
# La correction ne touche NI le score lexical, NI l'index : elle construit une
# disjonction unique des termes porteurs de la question. Elle est générique — elle ne
# lit ni la langue (le produit n'en a aucune métadonnée), ni le magasin visé.
_NORMALIZED_TOKEN_RE = re.compile(r"[A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9'’-]*")

#: Mots vides ANGLAIS. Le pendant français (`STOPWORDS_FR`) n'est volontairement PAS
#: appliqué ici : la variante qualifiée par la campagne ne filtrait que l'anglais, et
#: c'est elle — pas une version « améliorée » — qui a produit le gain mesuré.
STOPWORDS_EN = {
    "a", "an", "the", "of", "in", "on", "for", "to", "and", "or",
    "is", "are", "was", "were", "be", "been",
    "what", "which", "who", "whom", "how", "why", "when", "where",
    "does", "do", "did", "can", "could", "should", "would", "will", "shall",
    "may", "might", "must",
    "this", "that", "these", "those", "it", "its",
    "with", "without", "from", "by", "as", "at", "than", "then",
    "there", "here", "about", "into", "over", "under",
    "more", "most", "less", "least",
    "exactly", "designate", "refer", "expression",
}

#: Bornes de la variante qualifiée. Les modifier produirait une variante NON évaluée.
NORMALIZED_QUERY_MIN_TOKEN_LEN = 3
NORMALIZED_QUERY_MAX_TERMS = 12
NORMALIZED_QUERY_FALLBACK_TOKENS = 6


def normalized_query_terms(question: str) -> tuple[list[str], int]:
    """Termes retenus par la normalisation V2, et nombre de jetons bruts.

    Le tiret et l'apostrophe sont CONSERVÉS : `bring-along`, `anti-dilution`,
    `d'ajustement` sont les termes les plus discriminants de leur question, et
    `_sanitize_ts_token` les détruirait (il supprime tout non-alphanumérique). Le dépôt
    documente déjà ce piège pour les identifiants à tiret, cf. `_IDENTIFIER_RE`.

    Aucune déduplication : la variante qualifiée n'en fait pas, et `to_tsquery` réduit
    de toute façon les répétitions.
    """
    raw = _NORMALIZED_TOKEN_RE.findall(question or "")
    kept = [
        t for t in raw
        if t.lower() not in STOPWORDS_EN and len(t) >= NORMALIZED_QUERY_MIN_TOKEN_LEN
    ]
    # Une question entièrement composée de mots vides ne doit pas produire une requête
    # vide : on retombe sur ses premiers jetons bruts plutôt que de ne rien chercher.
    if not kept:
        kept = raw[:NORMALIZED_QUERY_FALLBACK_TOKENS]
    return kept[:NORMALIZED_QUERY_MAX_TERMS], len(raw)


def _normalized_ts_token(token: str) -> str:
    """Rogne les séparateurs de bord — garde-fou de syntaxe `to_tsquery`.

    Le motif peut produire `start-` sur « start- up » ; `to_tsquery('start-:*')` lève
    une erreur de syntaxe. Aucun des 63 jeux de traces gelés ne contient de séparateur
    en tête ou en queue : ce rognage est un NO-OP sur la variante évaluée, et il
    supprime un mode d'échec que l'expérimentation n'avait pas exercé.
    """
    t = token.strip("-'’")
    return t if any(c.isalnum() for c in t) else ""


def build_normalized_tsquery(question: str) -> tuple[str, int, int]:
    """Requête lexicale V2 : `(tsquery, jetons_bruts, termes_retenus)`."""
    terms, raw_count = normalized_query_terms(question)
    cleaned = [c for c in (_normalized_ts_token(t) for t in terms) if c]
    return " | ".join(f"{t.lower()}:*" for t in cleaned), raw_count, len(cleaned)


def normalized_query_enabled() -> bool:
    """Drapeau de la normalisation V2. OFF par défaut, partout."""
    return bool(getattr(settings, "RAG_FTS_QUERY_NORMALIZATION_V2", False))


def build_tsquery_plan(question: str) -> list[str]:
    """Requêtes FTS candidates, de la plus SÉLECTIVE à la plus LARGE.

    L'heuristique historique privilégie les **entités** (termes capitalisés) : « que
    dit-on de ORVANIS ? » -> chercher ``ORVANIS``, et rien d'autre. Sur un corpus de revues
    de presse, c'est excellent : ça écrase le bruit.

    Mais elle **jetait tous les autres mots-clés**. Or une entité peut très bien ne
    figurer **nulle part dans le texte** — typiquement quand elle est dans le NOM DU
    FICHIER. Cas réel qui a motivé ce correctif : « donne les shares outstanding pour
    **Airworks** » sur un classeur `AirWorks - 2026 Financing Pro Forma.xlsx`. La
    requête devenait ``airworks:*``, qui ne matche **aucun** chunk (le contenu ne parle
    que de `Series A-3`, `Common`, `Warrants`) — pendant que ``shares`` et
    ``outstanding``, les seuls mots utiles, étaient supprimés. Zéro résultat FTS, et
    une réponse « Je ne sais pas » sur un document parfaitement indexé.

    On ne renonce pas à l'heuristique : on **ne jette plus rien**. La requête entité
    reste tentée d'abord ; si elle ne ramène RIEN, on retombe sur les mots-clés.

    ⚠️ Normalisation V2 (`RAG_FTS_QUERY_NORMALIZATION_V2`). Quand elle est active, le
    plan se réduit à UNE entrée : la disjonction normalisée. C'est délibéré et c'est le
    cœur de la correction — l'entrée « sélective » du plan historique était précisément
    ce qui ramenait des résultats non vides sans jamais déclencher le repli. Un plan à
    une entrée n'a pas de repli à déclencher : il n'en a plus besoin.
    """
    if normalized_query_enabled():
        tsq, _raw, _kept = build_normalized_tsquery(question)
        return [tsq] if tsq else []

    plan: list[str] = []

    # Les identifiants à tiret (`A-1`, `2020-3`) sont les termes les PLUS
    # discriminants d'une question — et ils étaient purement et simplement détruits.
    # Ils s'ajoutent en conjonction : ils resserrent, ils n'élargissent jamais.
    idents = extract_identifiers(question)

    caps = _extract_capitalized_terms(question)

    # Référence à slash : requête la PLUS sélective, donc en TÊTE de plan (cf.
    # `_SLASH_IDENTIFIER_RE`). Purement additive — le plan historique reste en aval, et
    # le repli de `search_chunks_hybrid` s'enclenche si celle-ci ne ramène rien.
    if getattr(settings, "RAG_FTS_SLASH_IDENTIFIERS_ENABLED", False):
        for slash_tok in extract_slash_identifiers(question):
            plan.append(slash_tok)
    if caps:
        if len(caps) >= 2:
            # AND sur les 2-4 premiers termes pour éviter les "five" seuls
            head = " & ".join(f"{k}:*" for k in caps[:4])
        else:
            head = f"{caps[0]}:*"
        if idents:
            head = " & ".join([head] + list(idents))
        plan.append(head)
    elif idents:
        # « 2020-3 ? » sans aucune entité : l'identifiant EST la question.
        plan.append(" & ".join(idents))

    # Le repli n'a de sens que pour une entité NOMMÉE. Un « terme capitalisé » purement
    # NUMÉRIQUE (« chiffre d'affaires **2030** ») n'est pas un nom : c'est une
    # CONTRAINTE. S'il est absent du corpus, la réponse est vraiment « je ne sais pas »
    # — élargir à `chiffre | affaires | entreprise` ne répondrait pas à la question, ça
    # ramènerait du bruit et détruirait une non-réponse correcte. (Régression attrapée
    # par le benchmark RAG : `no_answer_accuracy` était tombée de 1,0 à 0,5.)
    #
    # Pour un NOM, au contraire, l'absence du texte a une explication banale : il est
    # dans le nom du fichier. C'est là, et seulement là, qu'on élargit.
    entity_is_named = any(not k.isdigit() for k in caps) if caps else True

    kws = _extract_keywords(question)
    if kws and entity_is_named:
        broad = " | ".join(f"{k}:*" for k in kws[:8])
        if broad not in plan:
            plan.append(broad)

    return plan


def build_tsquery_plan_with_telemetry(question: str) -> tuple[list[str], str, int, int]:
    """`(plan, mode, jetons_bruts, termes_retenus)` — compteurs SANS contenu lexical.

    Point d'entrée unique des deux magasins : le leg privé et le leg corpus métier
    partagent la construction de requête, ce qui rend impossible la divergence qui
    avait déjà privé le corpus de tout repli.
    """
    if normalized_query_enabled():
        tsq, raw_n, norm_n = build_normalized_tsquery(question)
        return ([tsq] if tsq else []), "v2", raw_n, norm_n
    plan = build_tsquery_plan(question)
    return plan, "v1", len(_TOKEN_RE.findall((question or "").lower())), 0


def _build_tsquery(question: str) -> str | None:
    """Requête FTS PRIMAIRE (compatibilité). Cf. `build_tsquery_plan` pour le repli."""
    plan = build_tsquery_plan(question)
    return plan[0] if plan else None


def _meta_as_dict(meta: Any) -> dict:
    """`Chunk.meta` lu en SQL BRUT -> dict.

    🔴 La colonne est un `jsonb`. L'ORM la décode (via `JSONField.from_db_value`), mais
    le curseur SQL brut la renvoie **telle quelle, en CHAÎNE**. Tous les `source.meta`
    construits ici étaient donc des `str`, jamais des dicts.

    Conséquences, silencieuses et anciennes :
    - la citation Excel (feuille + plage, lot E3) ne s'affichait JAMAIS en production —
      `project_source_meta` recevait une chaîne et renvoyait `{}` ;
    - les RÈGLES TABLEAUX du prompt (P3) ne se déclenchaient JAMAIS : le test
      `meta.get("kind") == "table"` échouait sur une chaîne.

    Les tests ne le voyaient pas : ils fabriquaient des `dict` en mémoire, sans jamais
    passer par le SQL. C'est le premier vrai classeur qui l'a révélé.
    """
    if isinstance(meta, dict):
        return meta
    if isinstance(meta, (str, bytes)):
        try:
            parsed = json.loads(meta)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def _build_label(filename: str | None, page_number: int | None, document_date: Any | None) -> str:
    fn = filename or "document"
    dd = None
    try:
        if document_date:
            dd = str(document_date)
    except Exception:
        dd = None

    parts = [fn]
    if dd:
        parts.append(f"({dd})")
    if page_number is not None:
        parts.append(f"p.{page_number}")
    return " ".join(parts)


def search_chunks_hybrid(
    *,
    rag_space_id: Any,
    question: str,
    top_k: int,
    min_text_rank: float,
    execution_sink: Callable[[FtsPlanExecution], None] | None = None,
) -> list[dict]:
    """Recherche lexicale privée, avec REPLI.

    On tente les requêtes de `build_tsquery_plan` de la plus **sélective** (entités) à
    la plus **large** (mots-clés), et l'on s'arrête à la première qui ramène quelque
    chose. Une requête entité qui ne matche RIEN ne doit plus condamner la recherche :
    l'entité est peut-être dans le nom du fichier, jamais dans le texte.

    Le repli ne se déclenche que sur **zéro résultat** — il ne dilue donc jamais une
    recherche qui fonctionnait.

    La stratégie elle-même vit dans `domain/fts_plan.execute_fts_plan` : le leg corpus
    métier consomme EXACTEMENT la même, ce qui rend impossible la divergence qui l'avait
    privé de tout repli. `execution_sink` (optionnel) reçoit la télémétrie du plan ;
    la signature d'appel historique reste inchangée.
    """
    plan, mode, raw_n, norm_n = build_tsquery_plan_with_telemetry(question)
    rows, execution = execute_fts_plan(
        plan=plan,
        query_mode=mode,
        raw_term_count=raw_n,
        normalized_term_count=norm_n,
        runner=lambda tsq: _search_chunks_fts(
            rag_space_id=rag_space_id, tsq=tsq, top_k=top_k, min_text_rank=min_text_rank
        ),
    )
    if execution_sink is not None:
        execution_sink(execution)
    return rows


def _search_chunks_fts(*, rag_space_id: Any, tsq: str, top_k: int, min_text_rank: float) -> list[dict]:
    if not tsq:
        return []

    # Config FTS configurable (B3). Littéral validé : voir fts_config().
    cfg = fts_config()
    tsv = fts_vector_sql("c")
    # Liste blanche (corpus figé) : JOIN indexable, ou rien du tout hors liste blanche.
    allow_join, allow_params = allowlist_join(alias="c", store=STORE_PRIVATE)
    sql = f"""
    SELECT
      c.id::text as chunk_id,
      c.text,
      c.page_number,
      c.chunk_index,
      c.meta,
      d.id::text as document_id,
      d.filename,
      d.document_date,
      ts_rank(
        {tsv},
        to_tsquery('{cfg}', %(tsq)s)
      ) AS text_rank
    FROM rag_chunk c
    JOIN rag_document d ON d.id = c.document_id
{ELIGIBILITY_JOIN}{allow_join}    WHERE c.rag_space_id = %(space)s
      AND d.rag_space_id = %(space)s              -- 🔒 verrou anti-fuite
{ELIGIBILITY_CLAUSES}      AND {tsv} @@ to_tsquery('{cfg}', %(tsq)s)
      AND ts_rank({tsv}, to_tsquery('{cfg}', %(tsq)s)) >= %(min_rank)s
    ORDER BY text_rank DESC, {SQL_TIE_BREAK}
    LIMIT %(k)s;
    """

    with connection.cursor() as cur:
        cur.execute(
            sql,
            {
                "space": rag_space_id,
                "tsq": tsq,
                "k": int(top_k),
                "min_rank": float(min_text_rank),
                **allow_params,
            },
        )
        rows = cur.fetchall()

    results: list[dict] = []
    for row in rows:
        (chunk_id, text, page_number, chunk_index, meta,
         document_id, filename, document_date, text_rank) = row
        score = float(text_rank or 0.0)

        results.append(
            {
                "chunk_id": chunk_id,
                "text": text,
                "score": score,
                # Position documentaire — exposée au niveau RACINE (et non lue dans `meta`)
                # pour que les tris Python en aval puissent départager les ex æquo sur une
                # valeur typée et garantie. Cf. domain/ranking.py.
                "chunk_index": chunk_index,
                # Provenance AFFIRMÉE dès la source : ce repository ne lit que
                # `rag_chunk`, donc des documents privés du RagSpace. La déclarer ici
                # plutôt qu'en aval évite qu'un chemin oublie de le faire et hérite d'un
                # défaut — cf. rag_core/domain/provenance.py.
                "source_type": SOURCE_TYPE_WORKSPACE,
                "source": {
                    "source_type": SOURCE_TYPE_WORKSPACE,
                    "document_id": document_id,
                    "filename": filename,
                    "document_date": str(document_date) if document_date else None,
                    "page_number": page_number,
                    "label": _build_label(filename, page_number, document_date),
                    "meta": _meta_as_dict(meta),
                },
            }
        )

    return results


# ---------------------------------------------------------------------
# Vector retrieval (pgvector) — cosine distance
# ---------------------------------------------------------------------

def _vector_literal(emb: Sequence[float] | Iterable[float]) -> str:
    """
    Convert a python embedding to pgvector literal: '[0.1,0.2,...]'

    We deliberately pass it as text and cast to vector in SQL to avoid relying
    on a specific psycopg adapter. Values are sanitized as finite floats.
    """
    vals: list[str] = []
    for x in emb:
        try:
            fx = float(x)
        except Exception:
            fx = 0.0
        if not math.isfinite(fx):
            fx = 0.0
        # repr gives a compact/precise float string
        vals.append(repr(fx))
    return "[" + ",".join(vals) + "]"


def pipeline_families_present(
    *,
    rag_space_id: Any,
    embedding_provider: str | None = None,
    embedding_model: str | None = None,
    limit: int = 8,
) -> dict[str | None, int]:
    """Familles de pipeline RÉELLEMENT présentes parmi les chunks comparables.

    Mêmes conditions d'éligibilité et de cohérence que :func:`search_chunks_vector`,
    MOINS le filtre de famille — c'est précisément ce filtre qu'il s'agit d'expliquer.

    Sert au diagnostic de DÉRIVE DE FAMILLE : la Gateway peut changer les poids d'un
    modèle sans changer son nom, auquel cas elle rend un `pipeline_id` de digest
    différent. Le filtre SQL, lui, ne trouve alors plus rien — et un vectoriel vide
    est indistinguable d'un espace sans réponse. Cette fonction n'est appelée QUE sur
    ce chemin-là : elle ne coûte rien tant que la recherche trouve quelque chose.
    """
    ep = (embedding_provider or "").strip().lower()
    em = (embedding_model or "").strip()
    params: dict[str, Any] = {"space": rag_space_id, "lim": max(1, int(limit or 1))}
    coherence_clause = ""
    if ep and em:
        coherence_clause = (
            "      AND d.reindex_required = FALSE\n"
            "      AND lower(d.embedding_provider) = %(ep)s\n"
            "      AND d.embedding_model = %(em)s\n"
        )
        params["ep"] = ep
        params["em"] = em
    # Liste blanche (corpus figé) : le diagnostic ne regarde que ce que la recherche
    # bornée aurait pu voir. Hors liste blanche : fragment vide, requête inchangée.
    allow_join, allow_params = allowlist_join(alias="c", store=STORE_PRIVATE)
    params.update(allow_params)

    sql = f"""
    SELECT c.pipeline_family, count(*)
    FROM rag_chunk c
    JOIN rag_document d ON d.id = c.document_id
{ELIGIBILITY_JOIN}{allow_join}    WHERE c.rag_space_id = %(space)s
      AND d.rag_space_id = %(space)s              -- 🔒 verrou anti-fuite
{ELIGIBILITY_CLAUSES}      AND c.embedding IS NOT NULL
{coherence_clause}    GROUP BY c.pipeline_family
    ORDER BY count(*) DESC
    LIMIT %(lim)s;
    """
    with connection.cursor() as cur:
        cur.execute(sql, params)
        return {row[0]: int(row[1]) for row in cur.fetchall()}


def search_chunks_vector(
    *,
    rag_space_id: Any,
    query_embedding: Sequence[float] | Iterable[float],
    top_k: int,
    embedding_provider: str | None = None,
    embedding_model: str | None = None,
    pipeline_family: str | None = None,
) -> list[dict]:
    """
    Vector search using pgvector cosine distance operator (<=>).
    Returns chunks ordered by increasing distance, with a similarity score in (0, 1].

    Output schema matches search_chunks_hybrid() for merge/rerank compatibility.

    Cohérence des embeddings (P1-D) : quand ``embedding_provider``/``embedding_model``
    (empreinte COURANTE de l'espace) sont fournis, la recherche ne considère QUE les
    documents encodés avec CE provider+modèle et non marqués ``reindex_required``.
    Les documents incohérents (indexés avec un ancien modèle après un changement de
    config) sont EXCLUS du leg vectoriel — jamais de comparaison de vecteurs entre
    modèles différents. Ils restent trouvables via le FTS.

    Identité de PIPELINE (lot 3) : ``pipeline_family`` renseigné => mode GOUVERNÉ.
    Seuls les chunks portant EXACTEMENT cette famille sont comparés. La garde P1-D
    ci-dessus porte sur la configuration déclarée du document ; celle-ci porte sur les
    POIDS réellement utilisés, que la configuration ne peut pas prouver.

    Un chunk à ``pipeline_family IS NULL`` (historique) est donc INVISIBLE en mode
    gouverné, et c'est voulu : ``NULL`` signifie « on ne sait pas », pas « compatible ».
    L'égalité SQL l'exclut d'elle-même — mais elle est ici un choix documenté, pas un
    effet de bord.

    ``pipeline_family=None`` => mode LEGACY, comportement strictement inchangé.
    """
    if query_embedding is None:
        return []

    k = max(1, int(top_k or 10))
    emb_lit = _vector_literal(query_embedding)

    params: dict[str, Any] = {"space": rag_space_id, "emb": emb_lit, "k": k}
    # Liste blanche (corpus figé) : JOIN indexable, ou rien du tout hors liste blanche.
    allow_join, allow_params = allowlist_join(alias="c", store=STORE_PRIVATE)
    params.update(allow_params)

    ep = (embedding_provider or "").strip().lower()
    em = (embedding_model or "").strip()
    coherence_clause = ""
    if ep and em:
        coherence_clause = (
            "      AND d.reindex_required = FALSE\n"
            "      AND lower(d.embedding_provider) = %(ep)s\n"
            "      AND d.embedding_model = %(em)s\n"
        )
        params["ep"] = ep
        params["em"] = em

    # Mode GOUVERNÉ : filtre STRICT sur la famille de pipeline. Placé sur le chunk et
    # non sur le document, parce que c'est le chunk qui porte le vecteur comparé.
    # S'appuie sur l'index composite `rag_chunk_space_pipeline_idx` (migration 0038).
    pf = (pipeline_family or "").strip()
    if pf:
        coherence_clause += "      AND c.pipeline_family = %(pf)s\n"
        params["pf"] = pf

    # NOTE:
    # - We cast the literal to vector; requires pgvector extension.
    # - Cosine distance: lower is better.
    # - Similarity: 1 / (1 + dist) -> (0, 1], stable and monotonic.
    sql = f"""
    SELECT
      c.id::text as chunk_id,
      c.text,
      c.page_number,
      c.chunk_index,
      c.meta,
      d.id::text as document_id,
      d.filename,
      d.document_date,
      (c.embedding <=> (%(emb)s)::vector) AS dist
    FROM rag_chunk c
    JOIN rag_document d ON d.id = c.document_id
{ELIGIBILITY_JOIN}{allow_join}    WHERE c.rag_space_id = %(space)s
      AND d.rag_space_id = %(space)s              -- 🔒 verrou anti-fuite
{ELIGIBILITY_CLAUSES}      AND c.embedding IS NOT NULL
{coherence_clause}    ORDER BY dist ASC, {SQL_TIE_BREAK}
    LIMIT %(k)s;
    """

    # HNSW recall/latency tuning: higher ef_search -> better recall, slower.
    # SET LOCAL is transaction-scoped, so it must run inside an explicit
    # transaction (the default autocommit mode would silently ignore it).
    ef_search = int(getattr(settings, "RAG_HNSW_EF_SEARCH", 40) or 40)
    with transaction.atomic(), connection.cursor() as cur:
        cur.execute("SET LOCAL hnsw.ef_search = %s", [ef_search])
        cur.execute(sql, params)
        rows = cur.fetchall()

    results: list[dict] = []
    for row in rows:
        (chunk_id, text, page_number, chunk_index, meta,
         document_id, filename, document_date, dist) = row
        d = float(dist or 0.0)
        score = 1.0 / (1.0 + max(0.0, d))

        results.append(
            {
                "chunk_id": chunk_id,
                "text": text,
                "score": score,
                # Cf. `_search_chunks_fts` : position documentaire au niveau racine, pour
                # le départage des ex æquo en aval (domain/ranking.py).
                "chunk_index": chunk_index,
                "source_type": SOURCE_TYPE_WORKSPACE,
                "source": {
                    "source_type": SOURCE_TYPE_WORKSPACE,
                    "document_id": document_id,
                    "filename": filename,
                    "document_date": str(document_date) if document_date else None,
                    "page_number": page_number,
                    "label": _build_label(filename, page_number, document_date),
                    "meta": _meta_as_dict(meta),
                },
            }
        )

    return results
