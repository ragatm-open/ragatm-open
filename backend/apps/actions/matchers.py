# backend/apps/actions/matchers.py
"""Matchers déterministes — nouveau foyer des règles aujourd'hui dispersées.

Chaque matcher reçoit le texte NORMALISÉ (``normalize.normalize``) + le texte
original (pour extraire, ex., un nom de fichier en conservant la casse) et renvoie
un ``MatchResult`` ou ``None``.

Faithful migration :
- ``ListDocumentsMatcher`` reprend ``_LISTING_PATTERNS`` / ``_extract_term_for_listing``
  de ``api/v1/chat/views.py`` (rendus **insensibles aux accents** : corrige P3).
- ``NewsGateMatcher`` reprend la garde ``_NEWS_HINTS`` de ``intents/news.py``
  (parité vérifiée par test).
- ``SummaryMatcher`` porte ``parseSummaryIntent`` (``frontend/.../summaryIntent.ts``)
  côté backend, qui devient l'autorité.
"""
from __future__ import annotations

import re
from typing import Protocol

from .plan import MatchResult


class Matcher(Protocol):
    action: str

    def match(self, *, norm: str, original: str) -> MatchResult | None: ...


# ---------------------------------------------------------------------------
# help.capabilities — aide produit / catalogue des capacités
#
# DÉTERMINISTE, JAMAIS DE LLM. Trois niveaux, dans cet ordre :
#   0. garde anti-faux-positifs (la demande porte sur le CONTENU documentaire) ;
#   1. commande explicite (égalité stricte sur le texte normalisé) ;
#   2. formulations naturelles ANCRÉES (jamais un mot isolé).
#
# Règle non négociable : « aide » / « help » ne déclenchent l'aide produit QUE
# comme commande exacte (niveau 1) ou dans « help me use RagATM » (niveau 2).
# Ils ne doivent jamais la déclencher lorsqu'ils introduisent une demande métier
# (« aide-moi à résumer ce document » -> document.summarize).
# ---------------------------------------------------------------------------

# Niveau 0 — la requête porte sur le contenu des documents, pas sur le produit.
_HELP_DENY_RE = re.compile(
    # « dans ce document », « du document », « de la source », « sur ce contrat »…
    r"\b(?:dans|de|du|sur)\s+(?:ce|cet|cette|le|la|les|mon|ma|mes|notre|votre)?\s*"
    r"(?:document|documents|pdf|fichier|fichiers|dossier|contrat|contrats|rapport|"
    r"rapports|texte|source|sources|accord|clause|clauses)\b"
    # « cité dans », « décrite dans », « mentionnés par »…
    r"|\b(?:cite|cites|citee|citees|decrit|decrits|decrite|decrites|"
    r"mentionne|mentionnes|mentionnee|mentionnees)\s+(?:dans|par)\b"
    # formulations transitives FR : « aide-moi à … », « aidez-moi à … »
    r"|\baide[zs]?[- ]moi\b"
    # formulations transitives EN : « help me summarize … » (mais PAS « help me use RagATM »)
    r"|\bhelp\s+me\s+(?!use\b)\w+"
)

# Niveau 1 — commandes explicites (égalité stricte sur le texte NORMALISÉ).
# Volontairement fermé : aucune regex globale sur « ? », qui matcherait toute question.
_HELP_COMMANDS = frozenset({
    "/aide",
    "/help",
    "/?",
    "?",
    "aide",
    "help",
    "/exemples",
})

# Ponctuation terminale tolérée (« aide ? » -> « aide »). Appliquée en PLUS de
# l'égalité stricte sur le texte brut, jamais à la place (sinon « ? » -> "").
_HELP_TRAIL_PUNCT_RE = re.compile(r"[?!.\s]+$")

# Sous-commandes (Lot 2) — reconnues UNIQUEMENT derrière une forme SLASHÉE.
#
# C'est toute la sûreté du mécanisme. « /help analyse » ne peut être qu'une demande
# d'aide ; « aide analyse » ou « analyse le contrat » restent des phrases, et donc
# potentiellement des demandes documentaires. On n'élargit donc jamais le matcher
# aux formes nues : le sujet ne se lit qu'après « /aide » ou « /help ».
_HELP_SLASH_VERBS = frozenset({"/aide", "/help"})


def _slash_help_topic(norm: str) -> str | None:
    """« /help analyse » -> « analyse ». « /help » -> « ». Sinon ``None``.

    Le sujet n'est pas VALIDÉ ici : le matcher transporte le mot tapé, le catalogue
    décide seul de ce qui est un sujet connu. Une seule source de vérité.
    """
    verb, _, rest = norm.partition(" ")
    if verb not in _HELP_SLASH_VERBS:
        return None
    return _HELP_TRAIL_PUNCT_RE.sub("", rest).strip()

# Niveau 2 — formulations naturelles ANCRÉES sur la fin de la requête. L'ancrage
# est ce qui protège des négatifs : « quelles sont les fonctionnalités du produit
# décrit dans le rapport ? » ne matche aucun de ces motifs.
_HELP_NL_PATTERNS = (
    # « quelles sont les fonctionnalités (disponibles) (sous RagATM) ? »
    r"\bquelles?\s+sont\s+les\s+(?:fonctionnalites|capacites|commandes|possibilites)"
    r"(?:\s+disponibles)?(?:\s+(?:de|dans|sous|avec)\s+ragatm)?\s*$",
    # « quelles fonctionnalités sont disponibles (sous RagATM) ? »
    r"\bquelles?\s+(?:fonctionnalites|capacites|commandes|possibilites)\s+sont\s+disponibles"
    r"(?:\s+(?:de|dans|sous|avec)\s+ragatm)?\s*$",
    # « que puis-je faire / demander ? »
    r"\bque\s+(?:puis-je|puis je)\s+(?:faire|demander)\s*$",
    # « qu'est-ce que je peux faire / demander ? »
    r"\bqu'est-ce\s+que\s+je\s+peux\s+(?:faire|demander)\s*$",
    # « que pouvez-vous faire ? »
    r"\bque\s+(?:pouvez-vous|pouvez vous|peux-tu|peux tu)\s+faire\s*$",
    # « montre-moi des exemples (de requêtes) »
    r"^(?:montre-moi|montre moi|donne-moi|donne moi)\s+des\s+exemples"
    r"(?:\s+de\s+(?:requetes|questions|commandes))?\s*$",
    # « exemples de requêtes / questions / commandes »
    r"^exemples\s+de\s+(?:requetes|questions|commandes)\s*$",
    # « comment utiliser / fonctionne RagATM ? »
    r"\bcomment\s+(?:utiliser|fonctionne|marche)\s+(?:ragatm|l'assistant|cet outil)\s*$",
    # EN
    r"\bwhat\s+can\s+i\s+(?:ask|do)\s*$",
    r"\bhelp\s+me\s+use\s+ragatm\s*$",
    r"\bhow\s+(?:do\s+i|to)\s+use\s+ragatm\s*$",
)
_HELP_NL_RE = tuple(re.compile(p) for p in _HELP_NL_PATTERNS)


class HelpMatcher:
    """Détection de l'intention d'aide produit. Aucun LLM, aucun retrieval.

    Unique implémentation : utilisée par ``ActionRouter`` ET, en amont, par la vue
    chat (afin que l'aide fonctionne même avec ``ACTIONS_ROUTER_ENABLED=0``).
    """

    action = "help.capabilities"

    def match(self, *, norm: str, original: str) -> MatchResult | None:
        if not norm:
            return None

        # 0) Forme slashée — traitée AVANT le garde-fou documentaire, à dessein.
        #    Une requête qui commence par « /help » ou « /aide » n'est jamais une
        #    question sur les documents : elle ne doit donc JAMAIS partir dans le
        #    RAG, même quand son sujet est inconnu. Aucun négatif du corpus ne
        #    commence par une barre oblique, ce garde-fou reste donc intact.
        topic = _slash_help_topic(norm)
        if topic is not None:
            return MatchResult(
                action=self.action,
                parameters={"topic": topic} if topic else {},
                detection_method="explicit_command",
                trigger_index=0,
            )

        # 1) La demande porte sur le contenu documentaire -> jamais d'aide produit.
        if _HELP_DENY_RE.search(norm):
            return None

        # 2) Commande explicite. On teste le texte normalisé brut (pour « ? » et
        #    « /? ») puis sa version sans ponctuation terminale (pour « aide ? »).
        stripped = _HELP_TRAIL_PUNCT_RE.sub("", norm)
        if norm in _HELP_COMMANDS or (stripped and stripped in _HELP_COMMANDS):
            return MatchResult(
                action=self.action,
                parameters={},
                detection_method="explicit_command",
                trigger_index=0,
            )

        # 3) Formulations naturelles ancrées (sur le texte sans ponctuation finale).
        for rx in _HELP_NL_RE:
            m = rx.search(stripped)
            if m:
                return MatchResult(
                    action=self.action,
                    parameters={},
                    detection_method="rule",
                    trigger_index=m.start(),
                )

        return None


# ---------------------------------------------------------------------------
# corpus.global_request — la demande porte sur TOUT l'espace documentaire
#
# DÉTERMINISTE, JAMAIS DE LLM. La détection ne déclenche AUCUN traitement : elle
# provoque une clarification (réponse rapide vs analyse complète). C'est ce qui
# empêche une demande globale de partir silencieusement dans le RAG top-k comme
# une question ordinaire.
#
# Trois niveaux, dans cet ordre :
#   0. garde « portée restreinte » — la demande vise UN document/page/fichier ;
#   1. garde « restriction thématique » — « … sur Nordexia » reste un listing ;
#   2. motifs de portée corpus (le mot « tous » seul ne suffit JAMAIS).
# ---------------------------------------------------------------------------

# Niveau 0 — la portée est explicitement limitée à une ressource unique.
# « analyse ce document », « résume la page 3 », « compare ces deux documents »…
_GLOBAL_DENY_RE = re.compile(
    # déictiques + nom documentaire (avec numéral optionnel : « ces deux documents »)
    r"\b(?:ce|cet|cette|ces)\s+(?:\d+\s+|deux\s+|trois\s+|quatre\s+|seuls?\s+|memes?\s+)?"
    r"(?:document|documents|fichier|fichiers|page|pages|contrat|contrats|rapport|rapports|"
    r"texte|textes|dossier|dossiers|pdf|accord|accords|source|sources|clause|clauses|"
    r"chapitre|chapitres|paragraphe|paragraphes)\b"
    # nom documentaire SINGULIER défini : « du document », « de la page », « le fichier »
    # NB : « du corpus », « la base » ne sont PAS ici — ce sont des portées globales.
    r"|\b(?:du|de\s+la|de\s+l'|le|la|l')\s*"
    r"(?:document|fichier|texte|contrat|rapport|dossier|pdf|accord)\b"
    # « la page 3 », « cette page »
    r"|\b(?:la|de\s+la|sur\s+la)\s+page\b"
    # portée « plusieurs » / « quelques » : explicitement PAS tout le corpus
    r"|\b(?:plusieurs|quelques|certains|certaines)\s+(?:documents|fichiers)\b"
    # EN
    r"|\bthis\s+(?:document|file|page|report|contract)\b"
)

# Nom de fichier explicite (« Rapport.pdf ») -> la demande vise CE document.
_GLOBAL_FILENAME_RE = re.compile(
    r"[^\s\"«»']+\.(?:pdf|docx?|txt|md|markdown|csv|html?|pptx?|xlsx?)\b", re.IGNORECASE
)

# Niveau 1 — la demande porte sur les DOCUMENTS eux-mêmes, pas sur leur contenu.
#
# « liste tous les documents SUR Nordexia » réclame la LISTE des documents -> listing.
# « recherche dans toute la base les informations SUR la sécurité » réclame le
# CONTENU -> analyse globale (L4).
#
# La restriction thématique seule ne suffit donc pas à trancher : c'est le VERBE
# DE LISTING, combiné à une restriction thématique, qui marque un listing. Une
# garde thématique aveugle bloquerait toutes les recherches globales thématiques —
# c'est-à-dire précisément ce que L4 sait faire.
#
# Deux marqueurs de listing, et deux seulement :
#   a) la restriction thématique porte DIRECTEMENT sur le nom documentaire
#      (« documents SUR X », « articles CONCERNANT X ») — on demande les
#      documents eux-mêmes. C'est le motif de `ListDocumentsMatcher`.
#   b) un verbe de listing gouverne une restriction thématique.
#
# En revanche « les INFORMATIONS sur X » porte sur le CONTENU : c'est une
# recherche globale, pas un listing.
_GLOBAL_DOC_TOPIC_RE = re.compile(
    r"\b(?:documents|document|fichiers|fichier|articles|article)\s+"
    r"(?:sur|concernant|a\s+propos\s+de|qui\s+parlent?|qui\s+mentionn\w+|"
    r"mentionnant|relatifs?\s+a|about|regarding)\b"
)
_GLOBAL_LISTING_VERB_RE = re.compile(
    r"\b(?:liste|listes|lister|listez)\b"
    r"|\bquels?\s+(?:documents|fichiers|articles)\b"
    r"|\bquelles?\s+sources\b"
)
_GLOBAL_TOPIC_RE = re.compile(
    r"\b(?:sur|concernant|a\s+propos\s+de|qui\s+parlent?\s+de|qui\s+mentionn\w+|"
    r"mentionnant|relatifs?\s+a|about|regarding)\s+\S+"
)

# Niveau 2 — portée corpus. Générique, sans aucun vocabulaire métier.
# Le mot « tous » seul ne suffit jamais : il doit porter sur un collectif
# DOCUMENTAIRE (« tous les documents ») ou sur la base / le corpus / l'espace.
_GLOBAL_SCOPE_PATTERNS = (
    # « (analyse|parcours|résume) tous les documents / fichiers / éléments / extraits »
    r"\b(?:tous|tout)\s+(?:les\s+)?(?:documents|fichiers|articles|elements|extraits)\b",
    # « toute la base (documentaire) », « prends en compte toute la base »
    r"\btoute\s+la\s+base(?:\s+documentaire)?\b",
    r"\bbase\s+documentaire\b",
    # « tout le corpus », « du corpus », « the entire corpus »
    r"\bcorpus\b",
    # « l'ensemble des documents / du RagSpace / de la base »
    r"\bensemble\s+(?:des|du|de\s+la)\s+"
    r"(?:documents|fichiers|corpus|base|ragspace|rag\s+space|espace)\b",
    # « l'intégralité des documents »
    r"\bintegralite\s+(?:des|du|de\s+la)\s+(?:documents|fichiers|corpus|base)\b",
    # « recense toutes les informations disponibles » (ancré sur « disponibles » :
    # « toutes les informations SUR UniCredit » ne doit PAS matcher)
    r"\btoutes\s+les\s+informations\s+disponibles\b",
    # « analyse l'ensemble du RagSpace »
    r"\bragspace\b",
    r"\brag\s+space\b",
    # « ne te limite pas aux sources les plus pertinentes » — demande explicite de
    # sortir du top-k. NB : c'est une NÉGATION, d'où l'exemption de garde-fou.
    r"\bne\s+(?:te|vous)\s+limite[sz]?\s+pas\s+aux\s+(?:sources|extraits|documents)\b",
    # EN
    r"\ball\s+(?:the\s+)?(?:documents|files|docs)\b",
    r"\bevery\s+document\b",
    r"\b(?:entire|whole)\s+(?:corpus|collection)\b",
)
_GLOBAL_SCOPE_RE = tuple(re.compile(p) for p in _GLOBAL_SCOPE_PATTERNS)


class GlobalCorpusMatcher:
    """Détection d'une demande portant sur l'ensemble du corpus.

    Implémentation UNIQUE : consommée par ``ActionRouter`` **et** par la vue chat
    (afin que la clarification fonctionne quel que soit ``ACTIONS_ROUTER_ENABLED``).
    Aucune règle n'est dupliquée dans la vue ni côté frontend.

    Ne déclenche jamais de traitement : la détection mène à une clarification.
    """

    action = "corpus.global_request"

    def match(self, *, norm: str, original: str) -> MatchResult | None:
        if not norm:
            return None

        # 0) La portée est limitée à une ressource unique -> jamais global.
        if _GLOBAL_DENY_RE.search(norm) or _GLOBAL_FILENAME_RE.search(original):
            return None

        # 1) Listing thématique -> jamais global. La restriction thématique seule
        #    ne suffit pas à trancher : « les informations SUR X » est justement le
        #    cas nominal d'une recherche globale.
        if _GLOBAL_DOC_TOPIC_RE.search(norm):
            return None
        if _GLOBAL_LISTING_VERB_RE.search(norm) and _GLOBAL_TOPIC_RE.search(norm):
            return None

        # 2) Portée corpus explicite.
        for rx in _GLOBAL_SCOPE_RE:
            m = rx.search(norm)
            if m:
                return MatchResult(
                    action=self.action,
                    parameters={},
                    detection_method="rule",
                    trigger_index=m.start(),
                )
        return None


# ---------------------------------------------------------------------------
# corpus.quick_confirm / corpus.full_analysis — confirmations CONTEXTUELLES.
#
# Volontairement HORS de la priorité du routeur : ce sont des réponses à une
# clarification, pas des intentions autonomes. Elles ne sont reconnues que si le
# fil porte une demande globale en attente (cf. chat/services/corpus_mode_service).
# Égalité stricte sur le texte normalisé — jamais de regex large, sinon « rapide »
# dans une phrase quelconque déclencherait une reprise de question.
# ---------------------------------------------------------------------------
_MODE_TRAIL_PUNCT_RE = re.compile(r"[?!.\s]+$")

_QUICK_COMMANDS = frozenset({
    "reponse rapide",
    "mode rapide",
    "rapide",
    "continue en mode rapide",
    "continuer en mode rapide",
    "quick answer",
    "quick",
    "fast answer",
})

_FULL_COMMANDS = frozenset({
    "analyse complete",
    "mode complet",
    "analyse exhaustive",
    "complet",
    "complete",
    "full analysis",
    "complete analysis",
})


def _mode_command(norm: str, commands: frozenset) -> bool:
    if not norm:
        return False
    stripped = _MODE_TRAIL_PUNCT_RE.sub("", norm)
    return norm in commands or (bool(stripped) and stripped in commands)


class QuickConfirmMatcher:
    """« réponse rapide » — reconnue UNIQUEMENT en commande exacte."""

    action = "corpus.quick_confirm"

    def match(self, *, norm: str, original: str) -> MatchResult | None:
        if not _mode_command(norm, _QUICK_COMMANDS):
            return None
        return MatchResult(
            action=self.action, parameters={}, detection_method="explicit_command", trigger_index=0
        )


class FullAnalysisMatcher:
    """« analyse complète » — reconnue UNIQUEMENT en commande exacte."""

    action = "corpus.full_analysis"

    def match(self, *, norm: str, original: str) -> MatchResult | None:
        if not _mode_command(norm, _FULL_COMMANDS):
            return None
        return MatchResult(
            action=self.action, parameters={}, detection_method="explicit_command", trigger_index=0
        )


# ---------------------------------------------------------------------------
# search.list_documents  (ex-_LISTING_PATTERNS, api/v1/chat/views.py:31)
# Patterns adaptés au texte NORMALISÉ (accents retirés) -> "a propos de".
# ---------------------------------------------------------------------------
_LISTING_PATTERNS = [
    r"\bliste\b",
    r"\btous les (articles|documents|fichiers)\b",
    r"\bquels (articles|documents|fichiers)\b",
    r"\b(?:articles|documents|fichiers)\s+(?:sur|concernant|a propos de)\b",
    r"\bqui (?:parlent|mentionn(?:e|ent))\b",
]
_LISTING_RE = [re.compile(p) for p in _LISTING_PATTERNS]
_LISTING_TERM_RE = re.compile(r"(?:sur|de|concernant|a propos de)\s+(.+)$")
_LISTING_TAIL_TOKENS_RE = re.compile(r"[a-z0-9\-_.]+")
_LISTING_TRAIL_PUNCT_RE = re.compile(r"[?.!,;:]+$")


def _extract_listing_term(norm: str) -> str:
    m = _LISTING_TERM_RE.search(norm)
    if m:
        term = m.group(1).strip().strip("\"'")
        term = _LISTING_TRAIL_PUNCT_RE.sub("", term).strip()
        if term:
            return term
    parts = _LISTING_TAIL_TOKENS_RE.findall(norm)
    return parts[-1] if parts else norm


class ListDocumentsMatcher:
    action = "search.list_documents"

    def match(self, *, norm: str, original: str) -> MatchResult | None:
        idx = None
        for rx in _LISTING_RE:
            m = rx.search(norm)
            if m:
                idx = m.start()
                break
        if idx is None:
            return None
        term = _extract_listing_term(norm)
        return MatchResult(
            action=self.action,
            parameters={"term": term},
            detection_method="rule",
            trigger_index=idx,
        )


# ---------------------------------------------------------------------------
# search.news  (garde déterministe ex-_NEWS_HINTS, intents/news.py:16)
# Ne DÉCIDE pas seul : marque une intention candidate qui pourra être confirmée
# par la classification LLM structurée (lot 4). Ici on renvoie l'action news.
# ---------------------------------------------------------------------------
# Copie fidèle de _NEWS_HINTS adaptée au texte normalisé (accents retirés).
# Parité avec la source vérifiée par tests/actions/test_parity_news_gate.py.
_NEWS_HINTS_NORM = re.compile(
    r"\b(news|actu|actualit(?:e)s?|quoi de neuf|dernier(?:es)?|recent(?:es)?|mise a jour|updates?)\b"
)


class NewsGateMatcher:
    action = "search.news"

    def match(self, *, norm: str, original: str) -> MatchResult | None:
        m = _NEWS_HINTS_NORM.search(norm)
        if not m:
            return None
        return MatchResult(
            action=self.action,
            parameters={},  # term/fenêtre extraits par le classifieur/handler
            detection_method="keyword_gate",
            trigger_index=m.start(),
            # gate = signal faible : à confirmer avant exécution (lot 4).
            ambiguous=True,
        )


# ---------------------------------------------------------------------------
# document.summarize  (port de parseSummaryIntent, summaryIntent.ts:37)
# ---------------------------------------------------------------------------
_SUMMARY_TRIGGER_RE = re.compile(r"\b(resum|synthes|synthetis)")
_SUMMARY_POINTS_RE = re.compile(r"points?\s+cles?")
# Cibles explicitement NON documentaires -> on laisse le chat normal répondre.
_SUMMARY_DENY_RE = re.compile(
    r"\b(espace|conversation|discussion|echange|chat|thread|fil|historique)\b"
)
_DOC_NOUNS = r"document|article|pdf|fichier|texte|rapport|doc|note|compte[- ]?rendu"
_SUMMARY_SRC_RE = re.compile(r"\bsources?\s+(?:numero\s+|n[°o]?\s*)?(\d{1,2})\b")
_SUMMARY_DEICTIC_RE = re.compile(
    r"\b(ce|cet|cette|le|la|du|des|les|mon|notre|votre)\s+(" + _DOC_NOUNS + r")\b"
)
_SUMMARY_DOCNOUN_RE = re.compile(r"\b(" + _DOC_NOUNS + r")\b")
# Nom de fichier explicite : conserve la casse d'origine (texte ORIGINAL).
_SUMMARY_FILE_RE = re.compile(
    r"([^\s\"«»']+\.(?:pdf|docx?|txt|md|markdown|csv|html?|pptx?|xlsx?))", re.IGNORECASE
)


class SummaryMatcher:
    action = "document.summarize"

    def match(self, *, norm: str, original: str) -> MatchResult | None:
        mt = _SUMMARY_TRIGGER_RE.search(norm)
        mp = _SUMMARY_POINTS_RE.search(norm)
        trig = mt or mp
        if not trig:
            return None
        if _SUMMARY_DENY_RE.search(norm):
            return None  # cible non documentaire -> chat normal

        idx = trig.start()

        # « la source 2 »
        msrc = _SUMMARY_SRC_RE.search(norm)
        if msrc:
            return MatchResult(
                action=self.action,
                parameters={"ref_kind": "source", "source_n": int(msrc.group(1))},
                detection_method="rule",
                trigger_index=idx,
            )

        # Nom de fichier explicite (sur texte original pour garder la casse).
        mfile = _SUMMARY_FILE_RE.search(original)
        if mfile:
            return MatchResult(
                action=self.action,
                parameters={"ref_kind": "filename", "document_reference": mfile.group(1)},
                detection_method="rule",
                trigger_index=idx,
            )

        # Déictique (« ce document ») ou simple nom documentaire.
        if _SUMMARY_DEICTIC_RE.search(norm) or _SUMMARY_DOCNOUN_RE.search(norm):
            return MatchResult(
                action=self.action,
                parameters={"ref_kind": "deictic"},
                detection_method="rule",
                trigger_index=idx,
            )

        # Déclencheur présent, cible implicite : résolution déictique tentée en aval.
        return MatchResult(
            action=self.action,
            parameters={"ref_kind": "implicit"},
            detection_method="rule",
            trigger_index=idx,
        )


# ---------------------------------------------------------------------------
# search.answer — capte tout le reste (défaut RAG).
# ---------------------------------------------------------------------------
class FallbackMatcher:
    action = "search.answer"

    def match(self, *, norm: str, original: str) -> MatchResult | None:
        if not norm:
            return None
        return MatchResult(action=self.action, parameters={}, detection_method="fallback")
