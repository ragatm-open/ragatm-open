# backend/apps/actions/registry.py
"""Registre déclaratif unique des actions (audit §6, §8, §9).

Le registre référence les handlers par **clé** (``handler_key``), pas par import
direct : évite les imports circulaires et garde ce module léger (pas d'import des
gros services au chargement). Le câblage effectif des handlers vit dans
``handlers.py`` (lot 3).

INVARIANT DE SÉCURITÉ (ce lot) : toute action mutante a
``natural_language_enabled=False`` ET ``matchers=()`` -> non déclenchable par une
phrase. ``document.delete`` est en plus ``destructive=True`` et gardé par un
feature flag désactivé par défaut.
"""
from __future__ import annotations

from dataclasses import dataclass

# NB : QuickConfirmMatcher / FullAnalysisMatcher ne sont PAS importés ici : les
# actions corpus.quick_confirm et corpus.full_analysis sont enregistrées avec
# ``matchers=()``. Ce sont des confirmations CONTEXTUELLES (valides uniquement en
# réponse à une clarification en attente), pas des intentions autonomes du routeur.
from .matchers import (
    FallbackMatcher,
    GlobalCorpusMatcher,
    HelpMatcher,
    ListDocumentsMatcher,
    Matcher,
    NewsGateMatcher,
    SummaryMatcher,
)


@dataclass(frozen=True)
class PermissionSpec:
    scope: str  # "rag_space" | "document" | "workspace" | "global"
    level: str  # "read" | "write" | "owner_admin" | "superuser"


@dataclass(frozen=True)
class ActionDefinition:
    name: str
    description: str
    examples: tuple[str, ...]
    schema: str  # clé dans schemas.ACTION_SCHEMAS
    permission: PermissionSpec
    destructive: bool
    execution_mode: str  # "sync" | "async" | "adaptive"
    handler_key: str
    audit_event: str
    matchers: tuple[Matcher, ...] = ()
    feature_flag: str | None = None
    natural_language_enabled: bool = True
    # Proposable au classifieur LLM ? Faux pour les actions dont la détection est
    # purement déterministe et ne doit JAMAIS dépendre d'un modèle (``help.capabilities``) :
    # les proposer élargirait la liste fermée du classifieur sans bénéfice.
    llm_proposable: bool = True
    version: int = 1


ACTION_REGISTRY: dict[str, ActionDefinition] = {}


def register(defn: ActionDefinition) -> None:
    if defn.name in ACTION_REGISTRY:
        raise ValueError(f"action déjà enregistrée: {defn.name}")
    # Invariant de sécurité vérifié à l'enregistrement.
    if not defn.natural_language_enabled and defn.matchers:
        raise ValueError(
            f"{defn.name}: natural_language_enabled=False impose matchers=() "
            "(action non déclenchable par une phrase)"
        )
    ACTION_REGISTRY[defn.name] = defn


def catalog(*, nl_only: bool = False) -> list[dict]:
    """Vue sérialisable du registre (doc UI / prompt LLM / tests)."""
    out = []
    for d in ACTION_REGISTRY.values():
        if nl_only and not (d.natural_language_enabled and d.matchers):
            continue
        out.append(
            {
                "name": d.name,
                "description": d.description,
                "destructive": d.destructive,
                "execution_mode": d.execution_mode,
                "permission": {"scope": d.permission.scope, "level": d.permission.level},
                "natural_language_enabled": d.natural_language_enabled,
                "version": d.version,
            }
        )
    return out


def allowed_actions_for_llm() -> tuple[str, ...]:
    """Liste FERMÉE d'intentions proposables au classifieur LLM (+ 'none')."""
    return tuple(
        d.name
        for d in ACTION_REGISTRY.values()
        if d.natural_language_enabled and d.matchers and d.llm_proposable
    ) + ("none",)


# ===========================================================================
# Lot initial — LECTURE SEULE déclenchable par une phrase
# ===========================================================================
register(ActionDefinition(
    name="help.capabilities",
    description="Présenter les capacités disponibles de RagATM et des exemples de requêtes.",
    examples=("/aide", "Que puis-je faire ?", "Quelles sont les fonctionnalités disponibles ?"),
    schema="search.answer",  # aucun paramètre : la réponse ne dépend que du catalogue
    permission=PermissionSpec(scope="rag_space", level="read"),
    destructive=False,
    execution_mode="sync",  # réponse déterministe immédiate : ni retrieval, ni LLM
    handler_key="help_capabilities",
    audit_event="help.capabilities",
    matchers=(HelpMatcher(),),
    # Détection 100 % déterministe : ne jamais l'offrir au classifieur LLM.
    llm_proposable=False,
))

register(ActionDefinition(
    name="corpus.global_request",
    description=(
        "Demande portant sur l'ensemble du corpus : proposer un choix explicite entre "
        "réponse rapide (top-k, partielle) et analyse complète (couverture mesurée)."
    ),
    examples=(
        "Analyse tous les documents",
        "Fais une synthèse de l'ensemble du corpus",
        "Recense toutes les informations disponibles",
    ),
    schema="search.answer",  # aucun paramètre : la clarification ne dépend que des stats
    permission=PermissionSpec(scope="rag_space", level="read"),
    destructive=False,
    execution_mode="sync",  # clarification déterministe : ni retrieval, ni LLM
    handler_key="corpus_global_request",
    audit_event="corpus.global_request",
    matchers=(GlobalCorpusMatcher(),),
    # Détection 100 % déterministe : ne jamais l'offrir au classifieur LLM. Une
    # erreur de classification coûterait ici une analyse de tout le corpus.
    llm_proposable=False,
))

# Confirmations CONTEXTUELLES : hors priorité du routeur (matchers=()), car elles
# ne sont valides qu'en réponse à une clarification en attente dans le fil. Les
# reconnaître hors contexte ferait reprendre une question inexistante.
register(ActionDefinition(
    name="corpus.quick_confirm",
    description="Confirmer une réponse rapide (top-k) pour une demande globale en attente.",
    examples=("réponse rapide", "mode rapide", "quick answer"),
    schema="search.answer",
    permission=PermissionSpec(scope="rag_space", level="read"),
    destructive=False,
    execution_mode="adaptive",
    handler_key="corpus_quick_confirm",
    audit_event="corpus.quick_confirm",
    matchers=(),  # contextuel : cf. apps/chat/services/corpus_mode_service.py
    llm_proposable=False,
))

register(ActionDefinition(
    name="corpus.full_analysis",
    description=(
        "Analyse complète du corpus. NON IMPLÉMENTÉE : réponse déterministe "
        "d'indisponibilité, aucun traitement déclenché."
    ),
    examples=("analyse complète", "mode complet"),
    schema="search.answer",
    permission=PermissionSpec(scope="rag_space", level="read"),
    destructive=False,
    execution_mode="sync",
    handler_key="corpus_full_analysis",
    audit_event="corpus.full_analysis",
    matchers=(),  # contextuel
    llm_proposable=False,
))

register(ActionDefinition(
    name="search.answer",
    description="Répondre à une question à partir des documents de l'espace (RAG par défaut).",
    examples=("Quelle est la durée du contrat ?", "Qui est le bénéficiaire ?"),
    schema="search.answer",
    permission=PermissionSpec(scope="rag_space", level="read"),
    destructive=False,
    execution_mode="adaptive",  # sync ou stream selon l'appelant
    handler_key="search_answer",
    audit_event="search.answer",
    matchers=(FallbackMatcher(),),
))

register(ActionDefinition(
    name="search.list_documents",
    description="Lister les documents de l'espace qui mentionnent un terme.",
    examples=("Liste les articles sur Nordexia", "Quels documents parlent de Valteria ?"),
    schema="search.list_documents",
    permission=PermissionSpec(scope="rag_space", level="read"),
    destructive=False,
    execution_mode="sync",
    handler_key="list_documents",
    audit_event="search.list_documents",
    matchers=(ListDocumentsMatcher(),),
))

register(ActionDefinition(
    name="search.news",
    description="Rechercher et synthétiser les informations récentes sur une entité.",
    examples=("Quoi de neuf sur Nordexia ?", "Actualités récentes Valteria"),
    schema="search.news",
    permission=PermissionSpec(scope="rag_space", level="read"),
    destructive=False,
    execution_mode="adaptive",
    handler_key="search_news",
    audit_event="search.news",
    matchers=(NewsGateMatcher(),),
))

register(ActionDefinition(
    name="document.summarize",
    description="Résumer un document (ou une page) de l'espace.",
    examples=("Résume le document Rapport.pdf", "Résume la source 2", "Synthèse de ce PDF"),
    schema="document.summarize",
    permission=PermissionSpec(scope="document", level="read"),  # viewers OK (comportement actuel)
    destructive=False,
    execution_mode="adaptive",  # réponse immédiate / cache / Celery selon le doc
    handler_key="summarize_document",
    audit_event="document.summarize",
    matchers=(SummaryMatcher(),),
))

# ===========================================================================
# Déclarées pour documentation/audit — JAMAIS déclenchables par une phrase
# (natural_language_enabled=False, matchers=()). Le câblage conversationnel est
# hors périmètre de ce lot.
# ===========================================================================
for _defn in (
    ActionDefinition(
        name="document.upload",
        description="Téléverser un document dans l'espace.",
        examples=(),
        schema="search.answer",  # non pertinent (pas d'entrée NL)
        permission=PermissionSpec(scope="rag_space", level="write"),
        destructive=False, execution_mode="async",
        handler_key="upload_document", audit_event="document.upload",
        natural_language_enabled=False,
    ),
    ActionDefinition(
        name="document.reindex",
        description="Réindexer un document existant.",
        examples=(),
        schema="search.answer",
        permission=PermissionSpec(scope="document", level="write"),
        destructive=False, execution_mode="async",
        handler_key="reindex_document", audit_event="document.reindex",
        natural_language_enabled=False,
    ),
    ActionDefinition(
        name="document.delete",
        description="Supprimer définitivement un document (chunks + fichier).",
        examples=(),
        schema="search.answer",
        permission=PermissionSpec(scope="document", level="write"),
        destructive=True, execution_mode="sync",
        handler_key="delete_document", audit_event="document.delete",
        natural_language_enabled=False,
        feature_flag="ACTIONS_ALLOW_NL_DELETE",  # off par défaut
    ),
    ActionDefinition(
        name="ragspace.create",
        description="Créer un RagSpace.",
        examples=(),
        schema="search.answer",
        permission=PermissionSpec(scope="workspace", level="owner_admin"),
        destructive=False, execution_mode="sync",
        handler_key="create_ragspace", audit_event="ragspace.create",
        natural_language_enabled=False,
    ),
    ActionDefinition(
        name="configuration.update",
        description="Modifier un override de configuration Core.",
        examples=(),
        schema="search.answer",
        permission=PermissionSpec(scope="global", level="superuser"),
        destructive=True, execution_mode="sync",
        handler_key="update_config", audit_event="configuration.update",
        natural_language_enabled=False,
    ),
):
    register(_defn)
