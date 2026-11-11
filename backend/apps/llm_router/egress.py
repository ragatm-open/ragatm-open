"""Politique d'egress externe et contexte d'exécution LLM (Lot 1 — P0-C).

INVARIANT DE SÉCURITÉ
--------------------
Aucun contenu utilisateur, document, chunk, résumé, historique ou métadonnée ne
peut quitter l'infrastructure privée sans une autorisation **explicite**, résolue
pour un **périmètre identifié**, propagée jusqu'au routeur et **auditée**.

Le comportement est **fail-closed**, sans exception :

* absence de contexte           -> refus (``MissingEgressContext``) ;
* politique non résolue         -> refus (politique ``DENIED``) ;
* egress non explicitement permis -> refus (``ExternalEgressDenied``) ;
* provider non listé            -> refus (``ExternalProviderDenied``).

Ce module ne connaît NI le transport (Gateway / direct), NI la raison de l'appel
(panne, mode dégradé). Il ne répond qu'à une question : *ce périmètre a-t-il le
droit d'envoyer des données à ce provider externe ?* La distinction est
volontaire — le rapport P0-C a montré qu'un contrôle de confidentialité adossé à
un flag de transport (``AI_GATEWAY_ENABLED``) se contourne par construction.

ORDRE DE RÉSOLUTION (du plus local au plus global)
--------------------------------------------------
1. interdiction explicite au niveau RagSpace ;
2. autorisation explicite au niveau RagSpace ;
3. politique Workspace (défaut hérité) ;
4. politique globale (interrupteur général) ;
5. défaut du code = **interdit**.

Une politique plus locale ne peut jamais ÉLARGIR une interdiction supérieure :
l'interrupteur global agit comme un plafond, jamais comme un plancher.

Voir ``docs/06-ai/politique-egress.md``.
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

#: Providers qui font SORTIR les données de l'infrastructure privée.
#: Tout provider inconnu est traité comme externe (fail-closed) : ajouter un
#: provider ici est un acte délibéré, l'oublier ne crée pas de trou.
EXTERNAL_PROVIDERS: frozenset[str] = frozenset(
    {"openai", "azure_openai", "anthropic", "mistral", "cohere", "google"}
)

#: Providers exécutés SUR l'infrastructure privée (Gateway incluse).
#:
#: ``ai_gateway`` y figure explicitement : c'est le nom que porte le pipeline
#: d'embeddings GOUVERNÉ (``AI_GATEWAY_EMBEDDINGS_BASE_URL``, ai-gateway:8100),
#: au même titre que ``gateway`` pour la génération. Son absence n'avait jamais eu
#: de conséquence tant que personne ne consultait la politique côté embeddings —
#: elle en aurait eu une immédiate en la branchant : le seul chemin d'embeddings
#: réellement gouverné aurait été traité comme une sortie externe et refusé.
INTERNAL_PROVIDERS: frozenset[str] = frozenset(
    {"ollama", "gateway", "ai_gateway", "stub", "local"}
)


def normalize_provider(provider: Any) -> str:
    """Normalise un identifiant de provider (casse, espaces). ``""`` si vide."""
    return (str(provider) if provider is not None else "").strip().lower()


def is_external_provider(provider: Any) -> bool:
    """True si ``provider`` fait sortir les données du périmètre privé.

    Fail-closed : un provider **inconnu** est considéré comme EXTERNE. Un nom mal
    orthographié ou un nouveau provider non déclaré ne doit jamais être traité
    comme interne par défaut.
    """
    p = normalize_provider(provider)
    if not p:
        # Provider indéterminé : on ne peut pas prouver qu'il est interne.
        return True
    if p in INTERNAL_PROVIDERS:
        return False
    return True


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class EgressPolicyError(RuntimeError):
    """Base des refus de politique d'egress. Jamais un repli silencieux."""


class MissingEgressContext(EgressPolicyError):
    """Un appel LLM a été tenté sans contexte d'exécution résolu.

    C'est une **erreur de programmation** : tout appelant doit fournir un
    ``LlmExecutionContext``. Le refus est volontaire — la version historique du
    routeur autorisait l'egress quand le contexte manquait (fail-open), ce qui
    est précisément le défaut corrigé ici.
    """


class ExternalEgressDenied(EgressPolicyError):
    """Le périmètre concerné n'autorise aucun envoi vers un provider externe."""


class ExternalProviderDenied(EgressPolicyError):
    """Le périmètre autorise l'egress, mais pas vers CE provider."""


# ---------------------------------------------------------------------------
# Politique
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExternalEgressPolicy:
    """Décision de confidentialité applicable à un périmètre donné.

    Immuable : une politique résolue ne peut pas être élargie en aval par un
    appelant. Pour restreindre, utiliser :meth:`intersect`.
    """

    external_generation_allowed: bool = False
    external_embedding_allowed: bool = False
    allowed_external_providers: tuple[str, ...] = ()

    #: D'où vient la décision — tracé dans l'audit (``code-default``,
    #: ``global-kill-switch``, ``ragspace``, ``ragspace-provider``,
    #: ``system-local-only``, ``system-explicit``…).
    policy_source: str = "code-default"
    scope_type: str = "none"
    scope_ref: str = ""
    reason: str = ""

    @classmethod
    def denied(cls, *, source: str = "code-default", reason: str = "", **kw: Any) -> "ExternalEgressPolicy":
        """Politique de refus total — le défaut du code."""
        return cls(
            external_generation_allowed=False,
            external_embedding_allowed=False,
            allowed_external_providers=(),
            policy_source=source,
            reason=reason,
            **kw,
        )

    def intersect(self, other: "ExternalEgressPolicy") -> "ExternalEgressPolicy":
        """Combine deux politiques en ne gardant que ce que les DEUX autorisent.

        Utilisé pour appliquer un plafond global à une politique locale : le
        résultat ne peut jamais être plus permissif que la plus stricte des deux.
        """
        providers = tuple(
            p for p in self.allowed_external_providers if p in other.allowed_external_providers
        )
        return replace(
            self,
            external_generation_allowed=(
                self.external_generation_allowed and other.external_generation_allowed
            ),
            external_embedding_allowed=(
                self.external_embedding_allowed and other.external_embedding_allowed
            ),
            allowed_external_providers=providers,
        )

    def permits(self, provider: Any, *, embedding: bool = False) -> bool:
        """True si ``provider`` est autorisé pour ce périmètre."""
        allowed = self.external_embedding_allowed if embedding else self.external_generation_allowed
        if not allowed:
            return False
        return normalize_provider(provider) in self.allowed_external_providers


#: Refus total, réutilisable. Le défaut de toute résolution qui échoue.
POLICY_DENIED = ExternalEgressPolicy.denied(reason="défaut fail-closed")


# ---------------------------------------------------------------------------
# Résolution
# ---------------------------------------------------------------------------


def _global_external_allowed() -> bool:
    """Interrupteur général d'egress externe (plafond, jamais plancher)."""
    from apps.rag_core.core import runtime_config

    return bool(runtime_config.get("AI_GATEWAY_FALLBACK_ENABLED", default=True))


def _global_allowed_providers() -> tuple[str, ...]:
    """Providers externes admis globalement.

    Dérivé de ``AI_GATEWAY_FALLBACK_PROVIDER`` : ``none`` (ou toute valeur non
    externe) ferme complètement la porte.
    """
    from apps.rag_core.core import runtime_config

    raw = normalize_provider(
        runtime_config.get("AI_GATEWAY_FALLBACK_PROVIDER", default="none") or ""
    )
    if not raw or raw == "none":
        return ()
    if raw not in EXTERNAL_PROVIDERS:
        # Valeur non reconnue comme provider externe légitime (ex. "ollama") :
        # refus, cohérent avec la policy « le repli n'est jamais Ollama direct ».
        return ()
    return (raw,)


def _global_embedding_egress_allowed() -> bool:
    """Plafond PROPRE à la sortie d'embeddings. Fermé par défaut.

    Découplé de ``AI_GATEWAY_FALLBACK_ENABLED``, qui décrit le repli de **génération**
    quand la passerelle tombe. Les deux décisions étaient portées par une seule
    variable : rouvrir le repli de génération rouvrait aussi, sans que personne ne le
    demande, la porte de l'egress d'embeddings.

    Ce sont deux gestes d'exploitation différents. « J'accepte que le chat bascule chez
    un tiers pendant une panne » ne dit rien de « j'accepte d'envoyer mon fonds
    documentaire chez un tiers ». Le second doit être un acte séparé, et fermé tant qu'il
    n'a pas été posé.
    """
    from apps.rag_core.core import runtime_config

    return bool(runtime_config.get("EXTERNAL_EMBEDDINGS_EGRESS_ENABLED", default=False))


def global_policy() -> ExternalEgressPolicy:
    """Politique globale — le plafond que nul périmètre ne peut dépasser.

    Les deux permissions ont désormais leur propre interrupteur. Une politique locale
    ne peut jamais élargir ce plafond : l'intersection reste la règle.
    """
    generation_open = _global_external_allowed()
    embedding_open = _global_embedding_egress_allowed()
    providers = _global_allowed_providers()

    if not providers:
        return ExternalEgressPolicy.denied(
            source="global-kill-switch",
            scope_type="global",
            reason="aucun provider externe admis globalement",
        )
    if not generation_open and not embedding_open:
        return ExternalEgressPolicy.denied(
            source="global-kill-switch",
            scope_type="global",
            reason="AI_GATEWAY_FALLBACK_ENABLED=0 et EXTERNAL_EMBEDDINGS_EGRESS_ENABLED=0",
        )
    return ExternalEgressPolicy(
        external_generation_allowed=generation_open,
        external_embedding_allowed=embedding_open,
        allowed_external_providers=providers,
        policy_source="global",
        scope_type="global",
    )


def resolve_policy_for_rag_space(space: Any) -> ExternalEgressPolicy:
    """Résout la politique d'egress d'un RagSpace. Fail-closed si ``space`` est None.

    Deux voies d'autorisation, toutes deux **explicites**, et elles n'ouvrent PAS
    les mêmes portes :

    1. ``allow_external_fallback=True`` — opt-in de confidentialité assumé par le
       propriétaire de l'espace. Seule voie qui autorise l'egress d'**embeddings**.
    2. ``llm_provider`` est lui-même un provider externe — l'espace a été
       configuré pour parler à ce provider sur le chemin NOMINAL ; lui refuser le
       repli vers ce même provider n'apporterait aucune protection. Cette voie
       n'autorise QUE la **génération**.

    Pourquoi la voie 2 n'ouvre pas les embeddings : elle raisonne sur
    ``llm_provider``, qui décrit ce que l'espace envoie déjà en génération. Les
    embeddings sont gouvernés par un champ SÉPARÉ (``embedding_provider``), et un
    espace pourrait porter un ``llm_provider`` externe et un ``embedding_provider``
    local — il n'aurait alors jamais consenti à ce que ses DOCUMENTS partent chez
    un tiers. Laisser la voie 2 accorder
    ``external_embedding_allowed`` reviendrait à déduire un consentement sur le
    fonds documentaire depuis un choix de modèle de conversation.

    L'invariant qui en découle, et que les tests verrouillent :

        allow_external_fallback=False  =>  external_embedding_allowed=False

    Dans les deux cas le résultat est plafonné par :func:`global_policy`.
    """
    if space is None:
        return ExternalEgressPolicy.denied(
            source="missing-scope", reason="aucun RagSpace résolu"
        )

    scope_ref = str(getattr(space, "id", "") or "")


    space_provider = normalize_provider(getattr(space, "llm_provider", "") or "")
    opt_in = bool(getattr(space, "allow_external_fallback", False))

    if opt_in:
        local = ExternalEgressPolicy(
            external_generation_allowed=True,
            external_embedding_allowed=True,
            allowed_external_providers=tuple(sorted(EXTERNAL_PROVIDERS)),
            policy_source="ragspace",
            scope_type="ragspace",
            scope_ref=scope_ref,
            reason="allow_external_fallback=True",
        )
    elif space_provider and space_provider in EXTERNAL_PROVIDERS:
        local = ExternalEgressPolicy(
            external_generation_allowed=True,
            # GÉNÉRATION SEULEMENT — cf. docstring. Un provider de chat externe ne
            # vaut pas consentement à l'envoi du fonds documentaire.
            external_embedding_allowed=False,
            allowed_external_providers=(space_provider,),
            policy_source="ragspace-provider",
            scope_type="ragspace",
            scope_ref=scope_ref,
            reason=f"llm_provider={space_provider} (provider nominal externe, génération seule)",
        )
    else:
        # Provider interne, vide ou inconnu + pas d'opt-in => REFUS.
        # C'est le cas qui fuyait avant le Lot 1 : un espace sans llm_provider
        # court-circuitait la garde et basculait vers le fournisseur externe.
        return ExternalEgressPolicy.denied(
            source="ragspace",
            scope_type="ragspace",
            scope_ref=scope_ref,
            reason="allow_external_fallback=False et provider nominal non externe",
        )

    return local.intersect(global_policy())


def policy_from_legacy_flags(
    *, space_provider: Any = None, allow_external_fallback: bool = False
) -> ExternalEgressPolicy:
    """Politique dérivée des anciens paramètres ``space_provider`` / ``allow_external_fallback``.

    Compatibilité de transition pour les appelants pas encore porteurs d'un
    :class:`LlmExecutionContext`. Applique EXACTEMENT les mêmes règles que
    :func:`resolve_policy_for_rag_space`, y compris le point qui fuyait :
    ``space_provider`` vide ne vaut plus autorisation.
    """
    sp = normalize_provider(space_provider)
    if allow_external_fallback:
        local = ExternalEgressPolicy(
            external_generation_allowed=True,
            external_embedding_allowed=True,
            allowed_external_providers=tuple(sorted(EXTERNAL_PROVIDERS)),
            policy_source="legacy-flags",
            reason="allow_external_fallback=True (appelant non migré)",
        )
    elif sp and sp in EXTERNAL_PROVIDERS:
        local = ExternalEgressPolicy(
            external_generation_allowed=True,
            # Génération seulement, à l'identique de `resolve_policy_for_rag_space`.
            external_embedding_allowed=False,
            allowed_external_providers=(sp,),
            policy_source="legacy-flags",
            reason=f"space_provider={sp} (provider nominal externe, génération seule)",
        )
    else:
        return ExternalEgressPolicy.denied(
            source="legacy-flags",
            reason="ni opt-in explicite ni provider nominal externe",
        )
    return local.intersect(global_policy())


# ---------------------------------------------------------------------------
# Contexte d'exécution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LlmExecutionContext:
    """Contexte OBLIGATOIRE de tout appel LLM susceptible de sortir du périmètre.

    Porte à la fois l'identité du périmètre (pour l'audit) et la décision de
    confidentialité (pour le contrôle). Les deux voyagent ensemble : c'est ce qui
    empêche un appelant d'oublier l'un des deux.
    """

    operation: str
    policy: ExternalEgressPolicy = POLICY_DENIED
    workspace_id: str | None = None
    rag_space_id: str | None = None
    user_id: str | None = None
    correlation_id: str = ""

    def __post_init__(self) -> None:
        if not self.correlation_id:
            object.__setattr__(self, "correlation_id", uuid.uuid4().hex)

    # -- lecture ------------------------------------------------------------

    @property
    def external_egress_allowed(self) -> bool:
        return self.policy.external_generation_allowed

    @property
    def allowed_external_providers(self) -> tuple[str, ...]:
        return self.policy.allowed_external_providers

    # -- contrôle -----------------------------------------------------------

    def ensure_external_allowed(self, provider: Any, *, embedding: bool = False) -> None:
        """Autorise ou REFUSE l'envoi vers ``provider``. Lève, ne retourne jamais False.

        Aucun appel réseau externe ne doit être émis sans passer par ici.
        """
        p = normalize_provider(provider)
        allowed = (
            self.policy.external_embedding_allowed
            if embedding
            else self.policy.external_generation_allowed
        )
        if not allowed:
            raise ExternalEgressDenied(
                f"egress externe interdit pour operation={self.operation!r} "
                f"scope={self.policy.scope_type}:{self.policy.scope_ref or '-'} "
                f"(source={self.policy.policy_source}, raison={self.policy.reason or 'non spécifiée'})"
            )
        if p not in self.policy.allowed_external_providers:
            raise ExternalProviderDenied(
                f"provider externe {p!r} non autorisé pour operation={self.operation!r} "
                f"scope={self.policy.scope_type}:{self.policy.scope_ref or '-'} "
                f"(autorisés: {', '.join(self.policy.allowed_external_providers) or 'aucun'})"
            )

    def audit_fields(self) -> dict[str, Any]:
        """Métadonnées d'audit — jamais de prompt, de chunk ni de secret."""
        return {
            "operation": self.operation,
            "workspace_id": self.workspace_id,
            "rag_space_id": self.rag_space_id,
            "user_id": self.user_id,
            "correlation_id": self.correlation_id,
            "external_egress_allowed": self.external_egress_allowed,
            "policy_source": self.policy.policy_source,
        }

    # -- fabriques ----------------------------------------------------------

    @classmethod
    def for_rag_space(
        cls,
        space: Any,
        *,
        operation: str,
        user_id: Any = None,
        workspace_id: Any = None,
        correlation_id: str = "",
    ) -> "LlmExecutionContext":
        """Contexte d'un appel rattaché à un RagSpace. ``space=None`` => refus."""
        ws = workspace_id
        if ws is None and space is not None:
            ws = getattr(space, "workspace_id", None)
        return cls(
            operation=operation,
            policy=resolve_policy_for_rag_space(space),
            workspace_id=str(ws) if ws else None,
            rag_space_id=str(getattr(space, "id", "") or "") or None,
            user_id=str(user_id) if user_id else None,
            correlation_id=correlation_id,
        )

    @classmethod
    def system_local_only(
        cls, *, operation: str, correlation_id: str = "", **kw: Any
    ) -> "LlmExecutionContext":
        """Tâche système qui ne doit JAMAIS sortir du périmètre privé.

        À utiliser pour les opérations d'administration hors RagSpace. Le refus
        est structurel, pas conditionnel.
        """
        return cls(
            operation=operation,
            policy=ExternalEgressPolicy.denied(
                source="system-local-only",
                scope_type="system",
                reason="opération déclarée strictement locale",
            ),
            correlation_id=correlation_id,
            **kw,
        )

    @classmethod
    def for_domain_corpus(
        cls, corpus: Any, *, operation: str, correlation_id: str = ""
    ) -> "LlmExecutionContext":
        """Contexte d'un appel rattaché au CORPUS MÉTIER (socle partagé).

        ⚠️ EXCEPTION RECENSÉE — lire avant de s'en inspirer.

        Le corpus métier n'a **aucun champ de consentement** équivalent à
        ``RagSpace.allow_external_fallback``. Sa seule expression de volonté est son
        propre ``embedding_provider``, choisi par un superuser au niveau du corpus.
        C'est donc lui qui fait autorité ici — et rien d'autre.

        Deux conséquences assumées :

        1. La politique autorise l'egress **vers le provider que le corpus déclare**,
           et vers lui seul. Un corpus ``ollama`` n'ouvre aucune porte externe.
        2. Elle n'est **pas** plafonnée par :func:`global_policy`. Ce plafond est
           dérivé de ``AI_GATEWAY_FALLBACK_*``, qui décrit le repli de GÉNÉRATION
           lorsque la passerelle tombe. L'appliquer ici ferait dépendre l'indexation
           délibérée d'un corpus d'un réglage qui ne parle pas d'elle : en production,
           ``AI_GATEWAY_FALLBACK_PROVIDER=none`` couperait du jour au lendemain un
           corpus explicitement configuré sur un fournisseur externe et qualifié.
           Ce serait un changement fonctionnel déguisé en correctif.

        Ce que cette fabrique NE fait pas : rendre le corpus plus sûr. Elle rend son
        comportement actuel **explicite et auditable** (``policy_source=domain-corpus``)
        au lieu de le laisser passer par l'absence de garde. La question « le corpus
        métier doit-il pouvoir sortir, et sur quel consentement ? » relève d'un lot
        dédié — elle demande un champ de politique au niveau corpus, donc une décision
        de produit.
        """
        return domain_corpus_context(
            corpus_id=getattr(corpus, "id", ""),
            provider=getattr(corpus, "embedding_provider", ""),
            operation=operation,
            correlation_id=correlation_id,
        )

    @classmethod
    def system_external_allowed(
        cls,
        *,
        operation: str,
        providers: Iterable[str],
        reason: str,
        correlation_id: str = "",
        **kw: Any,
    ) -> "LlmExecutionContext":
        """Tâche système AUTORISÉE à sortir — autorisation volontaire et motivée.

        ``reason`` est obligatoire : une autorisation d'egress sans justification
        n'est pas auditable, donc n'est pas accordée.
        """
        if not (reason or "").strip():
            raise ValueError("system_external_allowed exige une raison explicite")
        wanted = tuple(sorted({normalize_provider(p) for p in providers if normalize_provider(p)}))
        local = ExternalEgressPolicy(
            external_generation_allowed=bool(wanted),
            external_embedding_allowed=bool(wanted),
            allowed_external_providers=wanted,
            policy_source="system-explicit",
            scope_type="system",
            reason=reason,
        )
        return cls(
            operation=operation,
            policy=local.intersect(global_policy()),
            correlation_id=correlation_id,
            **kw,
        )


# ---------------------------------------------------------------------------
# Barrière
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Portée « espace alimenté par un connecteur de messagerie » — défense AU POINT D'APPEL
# ---------------------------------------------------------------------------
#
# Défaut historique : un contexte d'egress légitime (corpus métier) servait une
# requête issue d'un espace de messagerie sans le savoir. Les politiques sont construites par
# les appelants ; un nouveau chemin peut toujours en oublier une. La portée ci-dessous
# est posée par les POINTS D'ENTRÉE d'une opération d'espace (retrieval, chat) d'après
# des données SERVEUR (`space_has_email_connector`), et `enforce_external_egress` — la
# barrière unique avant tout appel externe — refuse TOUT provider externe tant qu'elle
# est active, QUEL QUE SOIT le contexte transmis (corpus, legacy, system-explicit…).

_EMAIL_PROTECTED_SCOPE: "ContextVar[str | None]" = ContextVar("ragatm_email_protected_scope", default=None)


def email_protected_scope_active() -> str | None:
    """Identifiant de l'espace protégé dont l'opération courante relève, ou None."""
    return _EMAIL_PROTECTED_SCOPE.get()


@contextmanager
def email_protected_scope(rag_space=None, *, rag_space_id=None):
    """Pose la portée protégée pour la durée du bloc si l'espace est alimenté par un connecteur.

    Fail-closed : un espace désigné mais illisible (erreur de lecture) est protégé
    (`space_has_email_connector` vaut alors « oui »). Imbricable ; toujours restaurée.
    """
    # RAGatm Open : aucun connecteur de messagerie n'existe ; la portée protégée n'est
    # jamais posée. Le gestionnaire est conservé pour les appelants (contrat inchangé).
    space = None
    if space is None:
        yield False
        return
    token = _EMAIL_PROTECTED_SCOPE.set(str(getattr(space, "id", None) or getattr(space, "pk", "")))
    try:
        yield True
    finally:
        _EMAIL_PROTECTED_SCOPE.reset(token)


def email_scoped(func):
    """Décorateur : exécute `func` sous `email_protected_scope` de l'espace désigné par ses
    arguments nommés `rag_space` / `rag_space_id`. Nom, signature et corps de `func`
    inchangés (`functools.wraps`) — les contrôles qui inspectent ces fonctions les voient
    telles quelles."""
    import functools

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with email_protected_scope(kwargs.get("rag_space"), rag_space_id=kwargs.get("rag_space_id")):
            return func(*args, **kwargs)

    return wrapper


def enforce_external_egress(
    *,
    provider: Any,
    context: "LlmExecutionContext | None",
    operation: str,
    embedding: bool = False,
) -> None:
    """Barrière UNIQUE avant tout appel réseau vers un provider externe.

    Implémentation canonique : ``router._enforce_egress`` n'est plus qu'un alias,
    et ``embeddings`` l'appelle directement. Une seule décision, un seul endroit —
    c'est précisément parce qu'il en existait *zéro* du côté embeddings que le
    P0 du lot SPACE-AI-CONFIG-ROUTING a été possible.

    * provider interne (``ollama``, ``ai_gateway``, stub…) -> autorisé, aucun
      contexte requis : rien ne quitte l'infrastructure privée ;
    * provider externe sans contexte -> ``MissingEgressContext`` (fail-closed) ;
    * provider externe avec contexte -> décision déléguée à la politique.

    ``embedding=True`` fait porter la décision sur
    ``ExternalEgressPolicy.external_embedding_allowed`` au lieu de
    ``external_generation_allowed``. Le défaut ``False`` garantit que tout appelant
    de génération existant conserve EXACTEMENT son comportement.
    """
    if not is_external_provider(provider):
        return

    protected = _EMAIL_PROTECTED_SCOPE.get()
    if protected:
        raise ExternalEgressDenied(
            f"egress externe interdit pour operation={operation!r} : opération d'un espace "
            f"alimenté par un connecteur de messagerie (source=connector-scope, provider={normalize_provider(provider)!r})"
        )

    if context is None:
        raise MissingEgressContext(
            f"appel externe vers {provider!r} refusé : aucun contexte d'egress fourni "
            f"(operation={operation!r}). Fournir un LlmExecutionContext explicite."
        )

    context.ensure_external_allowed(provider, embedding=embedding)


def domain_corpus_context(
    *, corpus_id: Any, provider: Any, operation: str, correlation_id: str = ""
) -> LlmExecutionContext:
    """Contexte d'egress du corpus métier, à partir du couple (corpus, provider).

    Forme la plus directe de :meth:`LlmExecutionContext.for_domain_corpus` — utile
    quand l'appelant a déjà résolu le provider (regroupement de retrieval, commande
    de vérification qui encode avec un couple PROPOSÉ) et n'a pas d'objet
    ``DomainCorpus`` complet sous la main. Recharger le corpus uniquement pour
    relire un champ déjà connu serait une requête pour rien.

    ⚠️ Exception recensée : voir :meth:`LlmExecutionContext.for_domain_corpus` pour
    la raison pour laquelle cette politique n'est pas plafonnée par
    :func:`global_policy`.
    """
    p = normalize_provider(provider)
    # RAGatm Open : le corpus métier ne peut PAS sortir, quel que soit le provider qu'il
    # déclare. L'exception historique (politique non plafonnée par le plafond global)
    # est supprimée : un corpus configuré sur un fournisseur externe est refusé à
    # l'encodage (``ExternalEgressDenied``), jamais servi.
    return LlmExecutionContext(
        operation=operation,
        policy=ExternalEgressPolicy(
            external_generation_allowed=False,
            external_embedding_allowed=False,
            allowed_external_providers=(),
            policy_source="domain-corpus-local-only",
            scope_type="domain_corpus",
            scope_ref=str(corpus_id or ""),
            reason=f"embedding_provider={p or '(non défini)'} déclaré sur le corpus",
        ),
        correlation_id=correlation_id,
    )
