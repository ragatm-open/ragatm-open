# backend/apps/llm_router/capabilities.py
"""
Réglages de moteur (chat / embeddings) par RAG space — édition Open : moteur local via AI Gateway.

Helpers partagés entre :
  - la validation du PATCH rag-space (provider/model) ;
  - l'endpoint capacités (GET .../llm-settings/) ;
  - l'endpoint test connexion (POST .../llm-settings/test/).

Contraintes de sécurité :
  - ne JAMAIS exposer un secret serveur (ni sa valeur, ni un fragment) ;
  - n'exposer que des booléens / messages sobres ;
  - aucune stack trace ni payload brut long renvoyé au client.
"""
from __future__ import annotations

import re

import requests

from .embeddings import default_embedding_model_for_provider, resolve_space_embedding
from .router import _default_model_for_provider, _get_setting

# Providers de CHAT admissibles pour un RagSpace (édition Open) : `ollama` désigne le
# modèle LOCAL servi par l'AI Gateway (le nom historique est conservé pour les données
# existantes et le frontend) ; aucun fournisseur externe n'existe.
ALLOWED_CHAT_PROVIDERS = ("ollama",)

#: Providers d'embeddings admissibles (corpus métier et RagSpaces) : le pipeline GOUVERNÉ
#: de l'AI Gateway est le SEUL chemin d'encodage de l'édition Open.
ALLOWED_EMBEDDING_PROVIDERS = ("ai_gateway",)

#: Provider d'embeddings GOUVERNÉ (AI Gateway, registre + digest + pipeline_id).
PROVIDER_AI_GATEWAY = "ai_gateway"

#: Plafond d'entrée, EN CARACTÈRES, de chaque modèle d'embeddings gouverné.
#:
#: ## Pourquoi cette table existe
#:
#: Le budget de découpage était attaché au **provider** : tout espace `ai_gateway`
#: était découpé à 1 200 caractères. C'était exact tant qu'`ai_gateway` n'exposait
#: qu'un seul modèle — `fr-en-retrieval`, refusé au-delà de 1 200. Le commit qui a
#: introduit la borne le dit d'ailleurs lui-même : « la fenêtre du modèle étant une
#: propriété du MODÈLE et non de l'espace ». Le provider n'était qu'un proxy exact.
#:
#: `legacy-768` a rompu cette équivalence le 22/08/2026 : même provider, plafond de
#: 6 000. Depuis, la règle par provider redécoupe inutilement un corpus qui n'a
#: aucune raison de l'être — et transforme une migration de PROVENANCE d'embedding
#: en migration de STRUCTURE documentaire.
#:
#: ## Unité : des CARACTÈRES, pas des tokens
#:
#: Vérifié contre les deux Gateway le 22/08/2026 — le refus est littéral :
#: « texte 0 trop long (1201 caractères > 1200) ». RagATM découpe en caractères, la
#: Gateway compte en caractères : aucune conversion, donc aucune dérive d'unité.
#:
#: ## Un PLAFOND, pas une cible
#:
#: 6 000 caractères est ce que `legacy-768` accepte, pas ce qu'il faut lui donner.
#: La granularité de découpage reste une décision produit (`RAG_CHUNK_SIZE_CHARS`) ;
#: cette table ne fait que l'ÉCRÊTER quand le modèle ne peut pas suivre.
#:
#: ## Dérive
#:
#: Déclarée ici plutôt que lue au runtime : l'ingestion ne doit pas dépendre d'un
#: appel réseau pour savoir comment découper. Mais une déclaration qui ment est pire
#: qu'une absence de déclaration — `manage.py check_embedding_capabilities` la
#: confronte au registre publié par la Gateway et échoue en cas d'écart.
GOVERNED_MODEL_MAX_INPUT_CHARS: dict[str, int] = {
    "fr-en-retrieval": 1200,   # granite-embedding:278m, fenêtre 512 tokens
    "legacy-768": 6000,        # nomic-embed-text, mêmes poids que le chemin historique
}

#: Préfixe interne que la Gateway AJOUTE au texte AVANT de compter, par modèle et par
#: tâche. Il est décompté du plafond publié.
#:
#: Découvert le 23/08/2026 par le rapport AIGW-WORKLOAD-ISOLATION, puis vérifié par
#: dichotomie contre la Gateway de production :
#:
#:     legacy-768 / document   dernier accepté 5983  (préfixe « search_document: », 17)
#:     legacy-768 / query      dernier accepté 5986  (préfixe « search_query: », 14)
#:
#: Sans cette table, `governed_model_max_input_chars` rendait 6 000 et un fragment de
#: 5 990 caractères passait toutes nos gardes pour être refusé par la Gateway. Pire :
#: le contrôle de dérive comparait 6 000 déclaré à 6 000 publié et concluait
#: « conforme » — une déclaration fausse EN EFFET, revêtue de l'autorité d'une
#: déclaration vérifiée. C'est le mode de défaillance que ce contrôle existe pour
#: empêcher, et il y était lui-même exposé.
GOVERNED_MODEL_PREFIX_CHARS: dict[str, dict[str, int]] = {
    "legacy-768": {"document": 17, "query": 14},
    "fr-en-retrieval": {},   # sans préfixe, vérifié
}

#: Tâche par défaut du dimensionnement : c'est le DÉCOUPAGE de documents qui doit
#: respecter le plafond, jamais la question — une question de 5 000 caractères n'existe
#: pas. En cas de doute on retient donc le préfixe le PLUS LONG du modèle.
TASK_DOCUMENT = "document"


def governed_model_max_input_chars(model: str, *, task: str = TASK_DOCUMENT) -> int | None:
    """Plafond UTILE — ce que le texte peut réellement faire, préfixe déduit.

    ``None`` n'est pas « pas de limite » : c'est « on ne sait pas ». L'appelant doit
    alors se rabattre sur une valeur conservatrice, jamais sur l'absence de borne —
    un fragment trop long ne dégrade pas la qualité, il fait ÉCHOUER son document.
    """
    nom = (model or "").strip()
    publie = GOVERNED_MODEL_MAX_INPUT_CHARS.get(nom)
    if publie is None:
        return None
    prefixes = GOVERNED_MODEL_PREFIX_CHARS.get(nom) or {}
    # Sans tâche connue, on retient le préfixe le plus coûteux : se tromper vers le
    # bas coûte quelques caractères, se tromper vers le haut coûte un document.
    cout = prefixes.get(task, max(prefixes.values(), default=0))
    return max(0, publie - cout)


def governed_model_published_max_input_chars(model: str) -> int | None:
    """Plafond tel que la Gateway le PUBLIE, préfixe compris.

    Distinct du précédent, et c'est délibéré : le contrôle de dérive doit comparer ce
    que la Gateway annonce à ce que nous avons noté d'elle, pas à ce que nous en
    déduisons pour nous-mêmes.
    """
    return GOVERNED_MODEL_MAX_INPUT_CHARS.get((model or "").strip())


#: Providers d'embeddings admissibles pour un **RagSpace**.
#: `ai_gateway` y figure depuis le lot GOVERNED-EMBEDDING-PIPELINE-ENABLEMENT : le
#: modèle Django l'acceptait déjà et le backend savait le traiter, mais l'API le
#: refusait — un espace gouverné ne pouvait naître que par commande de gestion.
#: L'admission reste CONDITIONNELLE : voir `governed_embeddings_unavailable_code`.
ALLOWED_RAGSPACE_EMBEDDING_PROVIDERS = (PROVIDER_AI_GATEWAY,)


def governed_embeddings_unavailable_code() -> str | None:
    """Code d'erreur si le pipeline gouverné n'est pas exploitable ici, sinon ``None``.

    Le refus doit tomber à la CONFIGURATION, pas à la première ingestion. Un espace
    créé en `ai_gateway` sur un environnement qui n'a pas de Gateway d'embeddings
    serait syntaxiquement valide et fonctionnellement mort : chaque document
    échouerait, après que l'utilisateur ait cru avoir configuré son espace.

    Les deux conditions sont exactement celles que `embed_via_gateway` vérifie en
    aval — la garde d'API est un miroir avancé, jamais une seconde politique.
    """
    from .embeddings_gateway import gateway_embeddings_base_url, gateway_embeddings_enabled

    if not gateway_embeddings_enabled():
        return "governed_embeddings_disabled"
    if not gateway_embeddings_base_url():
        return "governed_embeddings_base_url_missing"
    return None


#: Message humain par code d'erreur d'embeddings. Une seule table, partagée par les
#: deux endpoints : le frontend mappe les codes qu'il connaît et retombe sur ce texte
#: pour les autres — il ne doit donc jamais y avoir deux formulations pour un code.
EMBEDDING_ERROR_MESSAGES: dict[str, str] = {
    "invalid_embedding_provider": "Provider d'embeddings non supporté.",
    "governed_embeddings_disabled": (
        "Le pipeline d'embeddings gouverné n'est pas activé sur cet environnement "
        "(AI_GATEWAY_EMBEDDINGS_ENABLED=0)."
    ),
    "governed_embeddings_base_url_missing": (
        "Le pipeline d'embeddings gouverné n'a pas d'URL de passerelle configurée "
        "(AI_GATEWAY_EMBEDDINGS_BASE_URL vide)."
    ),
    # --- Catalogue de modèles (lot MODEL-CATALOG-AND-ALLOWLIST-GOVERNANCE) ---
    "model_catalog_unavailable": (
        "Le catalogue des modèles est indisponible (passerelle IA injoignable et aucun "
        "état antérieur connu). La configuration modèle est refusée plutôt qu'acceptée "
        "sans vérification."
    ),
    "embedding_model_not_declared_by_pool": (
        "Ce profil d'embeddings n'est pas déclaré par l'AI Gateway Pool utilisé sur cet "
        "environnement : il serait refusé (aucune substitution de modèle n'est faite)."
    ),
    "model_not_in_catalog": (
        "Ce modèle ne figure pas au catalogue de la passerelle IA. Vérifiez son "
        "orthographe, ou qu'il est bien publié sur cet environnement."
    ),
    "model_capability_mismatch": (
        "Ce modèle n'a pas la capacité requise pour cet usage (un modèle d'embeddings "
        "ne génère pas de texte, et inversement)."
    ),
    "model_dimension_mismatch": (
        "La dimension de ce modèle d'embeddings ne correspond pas à celle de l'index "
        "vectoriel. L'accepter rendrait l'espace inindexable."
    ),
    "model_registry_not_ready": (
        "Ce pipeline n'est pas déclaré `ready` au registre de la passerelle : il n'est "
        "pas gouvernable en l'état."
    ),
    "model_not_qualified": (
        "Ce modèle est compatible mais n'a pas été qualifié par RagATM. Un "
        "administrateur plateforme peut passer outre explicitement (`force`)."
    ),
    "gateway_embeddings_not_enabled": (
        "Le pipeline d'embeddings gouverné n'est pas activé sur cet environnement."
    ),
    "gateway_embeddings_base_url_missing": (
        "Le pipeline d'embeddings gouverné n'a pas d'URL de passerelle configurée."
    ),
    # --- Génération ---
    "model_generation_not_supported": (
        "Ce modèle ne peut pas être utilisé pour le chat documentaire : la passerelle IA "
        "ne le sert pas en génération (/v1/generate). Sélectionnez un modèle compatible."
    ),
    "model_generation_not_verified": (
        "La compatibilité de ce modèle avec le chat documentaire n'est pas établie : il "
        "n'est pas inscrit au registre de génération de la passerelle IA (ou ce registre "
        "est momentanément illisible). Sélectionnez un modèle compatible."
    ),
    "model_output_policy_rejected": (
        "Ce modèle est exclu du chat documentaire : ses réponses exposent son raisonnement "
        "interne. Sélectionnez un modèle compatible."
    ),
    "llm_provider_not_operational": (
        "Ce moteur n'est pas opérationnel pour le chat des espaces sur cet "
        "environnement : les générations passent par la passerelle IA, qui ne "
        "l'utilise pas. Choisissez le moteur « Local »."
    ),
    "governed_model_required": (
        "Un modèle logique est requis pour le pipeline gouverné (ex. « fr-en-retrieval ») : "
        "cet environnement ne déclare aucun défaut."
    ),
}


def embedding_provider_error(provider: str) -> str | None:
    """Code d'erreur si ``provider`` n'est pas admissible pour un RagSpace, sinon ``None``."""
    p = (provider or "").strip().lower()
    if p not in ALLOWED_RAGSPACE_EMBEDDING_PROVIDERS:
        return "invalid_embedding_provider"
    if p == PROVIDER_AI_GATEWAY:
        return governed_embeddings_unavailable_code()
    return None


def governed_model_error(provider: str, model: str) -> str | None:
    """Code d'erreur si le couple (provider, modèle) est inexploitable, sinon ``None``.

    Pour `ai_gateway`, ``model`` est le **modèle logique** du registre Gateway
    (ex. ``fr-en-retrieval``), jamais le nom du modèle amont (``granite-embedding:278m``).
    Un modèle vide n'est accepté que si l'environnement déclare un défaut gouverné
    (``AI_GATEWAY_EMBEDDINGS_MODEL``) : sans lui, `embed_via_gateway` lèverait
    `model_missing` au premier document, c'est-à-dire trop tard.
    """
    if (provider or "").strip().lower() != PROVIDER_AI_GATEWAY:
        return None
    if (model or "").strip():
        return None

    from .embeddings_gateway import gateway_embeddings_default_model

    return None if gateway_embeddings_default_model() else "governed_model_required"

# Modèle : alphanumérique + . _ : - / (couvre "gpt-4.1-mini", "llama3.1:8b", "org/model").
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-/]{0,127}$")

# Timeout court pour les tests de connexion (probe), indépendant de LLM_TIMEOUT_S.
_PROBE_TIMEOUT_S = 5.0


def is_valid_chat_model(model: str | None) -> bool:
    """
    Modèle valide si :
      - vide => OK (=> modèle par défaut du provider) ;
      - sinon longueur raisonnable et caractères sûrs.
    """
    m = (model or "").strip()
    if not m:
        return True
    if len(m) > 128:
        return False
    return bool(_MODEL_RE.match(m))


# Même grammaire de validation pour les modèles d'embeddings.
is_valid_model_name = is_valid_chat_model


def chat_provider_error(provider: str) -> str | None:
    """Code d'erreur si ``provider`` ne peut pas servir le chat d'un NOUVEL état, sinon None.

    Édition Open : seul `ollama` (modèle local servi par l'AI Gateway) est admissible.
    """
    p = (provider or "").strip().lower()
    if p not in ALLOWED_CHAT_PROVIDERS:
        return "invalid_llm_provider"
    return None


def default_chat_model_error() -> str | None:
    """Le défaut d'environnement est-il PROUVÉ inutilisable en génération ?

    Utilisé à la création par un non-superuser, qui reçoit les valeurs canoniques sans
    choisir. On ne refuse que sur preuve positive (modèle absent, sans génération,
    exclu) : une passerelle injoignable ne bloque pas la création d'un espace avec le
    défaut — comportement antérieur conservé.
    """
    from .model_catalog import (
        ERR_GENERATION_UNSUPPORTED, ERR_OUTPUT_POLICY, ERR_UNKNOWN_MODEL,
        ERR_WRONG_CAPABILITY, validate_chat_model,
    )
    from .model_defaults import default_ragspace_chat_model

    code = validate_chat_model(default_ragspace_chat_model())
    if code in (ERR_GENERATION_UNSUPPORTED, ERR_OUTPUT_POLICY, ERR_UNKNOWN_MODEL,
                ERR_WRONG_CAPABILITY):
        return code
    return None


def provider_capabilities() -> dict:
    """Capacités des providers (sans aucun secret), pour le chat ET les embeddings.

    Édition Open : `ollama` = génération LOCALE (servie par l'AI Gateway) ; `ai_gateway` =
    embeddings GOUVERNÉS. Aucun fournisseur externe n'est listé — il n'en existe pas.
    """
    return {
        "ollama": {
            "available": True,
            "configured": True,
            "default_model": _default_model_for_provider("ollama"),
            "default_embedding_model": default_embedding_model_for_provider(PROVIDER_AI_GATEWAY),
        },
        PROVIDER_AI_GATEWAY: {
            "available": governed_embeddings_unavailable_code() is None,
            "configured": governed_embeddings_unavailable_code() is None,
            "unavailable_reason": governed_embeddings_unavailable_code() or "",
            "default_embedding_model": default_embedding_model_for_provider(PROVIDER_AI_GATEWAY),
        },
    }


def embeddings_info() -> dict:
    """
    Infos embeddings GLOBALES (réglage env), exposées par /api/v1/capabilities/.

    Sert de repli d'affichage (ex. message d'erreur d'import) quand on ne dispose
    pas du contexte d'un RagSpace précis. La configuration *effective* d'un espace
    est renvoyée par ``space_embeddings_settings`` (endpoint llm-settings).
    """
    provider = (_get_setting("EMBEDDINGS_PROVIDER") or "stub").strip().lower()
    model = default_embedding_model_for_provider(provider)
    return {
        "provider": provider,
        "model": model,
        "scope": "global",
    }


def space_embeddings_settings(space) -> dict:
    """Réglage d'embeddings EFFECTIF d'un RagSpace (configurable, sans secret).

    Le provider/modèle proviennent de l'espace ; le modèle vide est résolu vers le
    défaut du provider pour l'affichage. Inclut l'avertissement de réindexation.
    """
    provider, model = resolve_space_embedding(space)
    provider = (provider or (_get_setting("EMBEDDINGS_PROVIDER") or PROVIDER_AI_GATEWAY)).strip().lower()
    model = (model or "").strip() or default_embedding_model_for_provider(provider)
    return {
        "configurable": True,
        "provider": provider,
        "model": model,
        "warning": (
            "Changer le provider ou le modèle d'embeddings nécessite de réindexer "
            "les documents de cet espace."
        ),
    }


def test_provider_connection(provider: str, model: str | None = None) -> dict:
    """
    Test de connexion léger et peu coûteux (pas de génération).

    Retour normalisé :
      - {"ok": True, "provider": ...}
      - {"ok": False, "code": "...", "message": "...", "provider": ...}

    Ne renvoie jamais : clé API, prompt, payload brut long, stack trace.
    """
    p = (provider or "").strip().lower()
    if p not in ALLOWED_CHAT_PROVIDERS:
        return {"ok": False, "code": "invalid_llm_provider", "message": "Provider inconnu.", "provider": p}

    if model is not None and not is_valid_chat_model(model):
        return {"ok": False, "code": "invalid_llm_model", "message": "Modèle invalide.", "provider": p}

    # Provider "ollama" : sous l'AI Gateway (config obligatoire), RagATM n'appelle
    # JAMAIS Ollama en direct → tester la Gateway (et la présence du modèle), pas
    # localhost:11434 (qui donnerait un faux « Ollama injoignable »). Gateway
    # désactivée (dev/tests dégradés) => on garde le probe Ollama direct historique.
    if bool(_get_setting("AI_GATEWAY_ENABLED")):
        return _test_gateway(model)
    return _test_ollama()


def _model_served(requested: str, names: list[str]) -> bool:
    """True si le modèle demandé est servi par la Gateway (tolérant au tag).

    Match exact, ou même famille avant ':' (ex. « nomic-embed-text » ↔
    « nomic-embed-text:latest », « mistral-small3.2:24b » ↔ « mistral-small3.2:24b »).
    """
    req = (requested or "").strip()
    if not req:
        return True
    if req in names:
        return True
    base = req.split(":", 1)[0]
    return any(n == req or n.split(":", 1)[0] == base for n in names)


def _test_gateway(model: str | None) -> dict:
    """Test de connexion via l'AI Gateway : GET {base}/v1/models.

    Vérifie l'accessibilité de la Gateway et, si un modèle est fourni, sa présence
    dans la liste servie. Ne renvoie jamais de secret ni de payload brut long.
    """
    base_url = (_get_setting("AI_GATEWAY_BASE_URL") or "").rstrip("/")
    if not base_url:
        return {
            "ok": False, "code": "gateway_base_url_missing",
            "message": "AI Gateway non configurée (AI_GATEWAY_BASE_URL vide).",
            "provider": "ollama",
        }
    url = f"{base_url}/v1/models"
    try:
        resp = requests.get(url, timeout=_PROBE_TIMEOUT_S)
    except requests.Timeout:
        return {"ok": False, "code": "gateway_timeout", "message": "Délai dépassé (AI Gateway).", "provider": "ollama"}
    except requests.RequestException:
        return {"ok": False, "code": "gateway_unreachable", "message": "AI Gateway injoignable.", "provider": "ollama"}

    if resp.status_code != 200:
        return {
            "ok": False, "code": "gateway_unreachable",
            "message": f"Réponse inattendue de l'AI Gateway (HTTP {resp.status_code}).",
            "provider": "ollama",
        }

    m = (model or "").strip()
    if m:
        try:
            names = [str(x.get("name", "")) for x in (resp.json().get("models") or [])]
        except Exception:
            names = []
        if names and not _model_served(m, names):
            return {
                "ok": False, "code": "model_not_available",
                "message": f"Le modèle « {m} » n'est pas servi par l'AI Gateway.",
                "provider": "ollama",
            }
    return {"ok": True, "provider": "ollama", "via": "ai_gateway"}


def _test_ollama() -> dict:
    base_url = (_get_setting("OLLAMA_BASE_URL") or "").rstrip("/")
    url = f"{base_url}/api/tags"
    try:
        resp = requests.get(url, timeout=_PROBE_TIMEOUT_S)
    except requests.Timeout:
        return {"ok": False, "code": "ollama_timeout", "message": "Délai dépassé.", "provider": "ollama"}
    except requests.RequestException:
        return {"ok": False, "code": "ollama_unreachable", "message": "Service injoignable.", "provider": "ollama"}

    if resp.status_code == 200:
        return {"ok": True, "provider": "ollama"}
    return {
        "ok": False,
        "code": "ollama_unreachable",
        "message": f"Réponse inattendue (HTTP {resp.status_code}).",
        "provider": "ollama",
    }


# ---------------------------------------------------------------------------
# Gouvernance de catalogue — validation sémantique d'une config d'espace
# ---------------------------------------------------------------------------


def catalog_config_error(
    *,
    old: dict[str, str],
    new: dict[str, str],
    force: bool = False,
) -> tuple[str | None, str]:
    """Valide la config modèle RÉSULTANTE contre le catalogue. ``(code, champ)``.

    Trois principes, et chacun corrige un défaut observé :

    1. **Seuls les champs qui CHANGENT sont validés.** Un espace historique portant un
       modèle que le catalogue ne qualifie pas doit continuer à fonctionner et à être
       renommable (§23). Valider l'état complet à chaque PATCH invaliderait des espaces
       que personne n'a touchés.
    2. **La validation précède tout effet de bord.** Le cas `bge-m3` était accepté à la
       configuration, marquait tous les documents `reindex_required`, puis échouait à
       la première ingestion sur un écart de dimension : l'espace se retrouvait avec un
       leg vectoriel vide et aucune réindexation possible.
    3. **``force`` ne franchit que la qualification.** Un modèle non qualifié est un
       pari assumé ; un modèle de la mauvaise dimension ou de la mauvaise capacité est
       une erreur, et aucun droit ne la rend correcte.
    """
    from .model_catalog import (
        ERR_NOT_QUALIFIED,
        KIND_EMBEDDING,
        catalog_applicable,
        is_qualified,
        validate_chat_model,
        validate_embedding_model,
    )

    def changed(field: str) -> bool:
        return (old.get(field) or "").strip() != (new.get(field) or "").strip()

    # Moteur de chat : même règle à la création et à la modification. Seul un
    # CHANGEMENT est jugé — un espace historique conserve son moteur.
    if changed("llm_provider"):
        code = chat_provider_error(new.get("llm_provider") or "")
        if code:
            return code, "llm_provider"

    if changed("llm_model") or changed("llm_provider"):
        code = validate_chat_model((new.get("llm_model") or "").strip())
        if code:
            return code, "llm_model"
        # PAS de garde de qualification sur le chat. `qualified` y est une ÉTIQUETTE du
        # catalogue, pas une condition : la plateforme utilise légitimement plusieurs
        # modèles de génération non qualifiés individuellement (`qwen3:14b` sert de
        # juge, `mistral-small3.2:24b` porte la vision). Les refuser fermerait des
        # usages existants au nom d'une mesure que personne n'a demandée.

    if changed("embedding_model") or changed("embedding_provider"):
        provider = (new.get("embedding_provider") or "").strip().lower()
        model = (new.get("embedding_model") or "").strip()
        code = validate_embedding_model(provider, model)
        if code:
            return code, "embedding_model"
        # SEULE garde de qualification du lot, et elle porte là où le risque est réel :
        # choisir un pipeline d'embeddings gouverné change l'identité vectorielle de
        # TOUT l'espace et impose une réindexation complète. Le faire avec un pipeline
        # que RagATM n'a jamais mesuré est un pari — assumable, mais qui doit être
        # explicite (`force`), superuser et audité.
        #
        # Elle ne s'applique pas au chemin historique : `ollama`/`nomic-embed-text`
        # désigne les mêmes poids que `legacy-768` sans être le même objet (§28), et
        # l'exiger qualifié refuserait une configuration que ce lot ne migre pas.
        #
        # Ni, évidemment, quand aucun catalogue n'est lisible : on ne peut pas exiger
        # une qualification qu'on n'a pas les moyens de constater.
        if (
            provider == "ai_gateway"
            and model
            and catalog_applicable()
            and not is_qualified(KIND_EMBEDDING, model)
            and not force
        ):
            return ERR_NOT_QUALIFIED, "embedding_model"

    return None, ""
