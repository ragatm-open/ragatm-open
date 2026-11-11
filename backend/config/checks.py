"""System checks de configuration — invariants centralisés (registre canonique).

Ces checks ne DUPLIQUENT pas les gardes existantes : ils couvrent ce qu'aucune
n'attrapait.

Déjà couvert ailleurs, volontairement laissé en place :

- `rag_core.E001` : cohérence RAG_EMBEDDING_DIM ↔ colonne pgvector ;
- `rag_core.E003` : CORE_CONFIG_RUNTIME_ENABLED sans cache partagé ;
- `llm_router.E001..E005` : politique AI Gateway (obligation, URL, fallback) ;
- `config/settings/prod.py` : clé de dev, ALLOWED_HOSTS vide, provider
  d'embeddings `stub`, FRONTEND_APP_ORIGIN absente (refus de démarrage).

Sévérité : une erreur bloque `manage.py check` (donc le déploiement et la CI).
Les avertissements restent visibles sans bloquer — ils signalent une divergence
descriptive (borne du registre) plutôt qu'une configuration cassée.
"""
from __future__ import annotations

import os
from typing import Any

from django.conf import settings
from django.core.checks import Error
from django.core.checks import Warning as CheckWarning

from config import parsers, registry
from config.parsers import ConfigValueError
from config.settings.from_registry import DEFERRED_ANOMALIES, DEPRECATION_WARNINGS

PROD_LIKE = ("staging", "prod")

#: Valeurs de démonstration interdites en production. Le nom de la variable
#: suffit au diagnostic : la valeur n'est JAMAIS reproduite dans le message.
_DEMO_VALUES: dict[str, tuple[str, ...]] = {
    "DJANGO_SECRET_KEY": ("dev-secret-key", "changeme", "secret", "django-insecure"),
    "DB_PASSWORD": ("ragatm", "postgres", "password", "changeme"),
    "SET_ADMIN_PASSWORD": ("admin", "password", "changeme"),
    "EMAIL_HOST_PASSWORD": ("changeme", "password"),
}


def _env() -> str:
    return (
        getattr(settings, "DJANGO_ENV", "") or os.getenv("DJANGO_ENV", "") or "dev"
    ).strip().lower()


def _is_prod_like() -> bool:
    return _env() in PROD_LIKE


def _severity(message: str, *, hint: str = "", check_id: str) -> Error | CheckWarning:
    """Erreur bloquante en staging/prod, avertissement ailleurs."""
    factory = Error if _is_prod_like() else CheckWarning
    return factory(message, hint=hint or None, id=check_id)


# ---------------------------------------------------------------------------
# config.E001 — anomalies relevées pendant le chargement des settings
# ---------------------------------------------------------------------------
def check_settings_load_anomalies(app_configs: Any = None, **kwargs: Any) -> list[Any]:
    """Valeurs textuelles invalides acceptées à l'import mais signalées ici.

    Les booléens, entiers et flottants invalides lèvent déjà à l'import (le
    démarrage échoue). Restent les URLs et énumérations : elles n'ont jamais
    bloqué le démarrage, on ne change pas ce contrat — on le rend visible.
    """
    return [
        _severity(
            f"{name} : valeur invalide ({reason}).",
            hint="Corrigez la valeur dans l'environnement ou le fichier .env.",
            check_id="config.E001",
        )
        for name, reason in DEFERRED_ANOMALIES
    ]


# ---------------------------------------------------------------------------
# config.W001 — variables dépréciées encore utilisées
# ---------------------------------------------------------------------------
def check_deprecated_variables(app_configs: Any = None, **kwargs: Any) -> list[Any]:
    """Alias historiques encore posés dans l'environnement.

    Toujours un avertissement : la compatibilité ascendante est la promesse de la
    politique de dépréciation. Le conflit de valeurs, lui, refuse déjà le
    démarrage (`config/settings/from_registry.py`).
    """
    issues: list[Any] = [
        CheckWarning(message, id="config.W001") for message in DEPRECATION_WARNINGS
    ]
    for definition in registry.deprecated():
        if os.environ.get(definition.name) in (None, ""):
            continue
        if registry.alias_for(definition.name) is not None:
            continue  # déjà signalée par le pont (avec sa politique de conflit)
        replacement = (
            f" Utiliser {definition.replacement}." if definition.replacement else ""
        )
        issues.append(
            CheckWarning(
                f"{definition.name} est dépréciée et encore définie.{replacement}",
                hint=definition.notes or None,
                id="config.W001",
            )
        )
    return issues


# ---------------------------------------------------------------------------
# config.E002 / config.W002 — conformité des settings au registre
# ---------------------------------------------------------------------------
def check_registry_conformance(app_configs: Any = None, **kwargs: Any) -> list[Any]:
    """Chaque setting couvert par le registre respecte son type et ses bornes.

    Empêche la dérive : une valeur de settings devenue incompatible avec sa
    déclaration (type changé à la main, borne franchie) se voit au démarrage et
    non lors d'un incident.
    """
    issues: list[Any] = []
    for definition in registry.all_definitions():
        if not hasattr(settings, definition.name):
            continue
        value = getattr(settings, definition.name)
        if value is None:
            continue

        # 1) Type — une incompatibilité est une ERREUR : le code lira une valeur
        #    qu'il ne sait pas interpréter.
        try:
            typed = _typed_value(definition, value)
        except ConfigValueError as exc:
            issues.append(
                Error(
                    f"{definition.name} : {exc.reason}.",
                    hint=f"Type attendu : {definition.value_type}.",
                    id="config.E002",
                )
            )
            continue

        # 2) Bornes — un AVERTISSEMENT : la borne est déclarative (déduite du
        #    comportement observé), pas un contrat imposé par le code. Une borne
        #    fausse ne doit pas empêcher un déploiement de démarrer.
        try:
            parsers.check_bounds(
                definition.name,
                typed,
                minimum=definition.minimum,
                maximum=definition.maximum,
            )
        except ConfigValueError as exc:
            issues.append(
                CheckWarning(
                    f"{definition.name} : {exc.reason} (borne déclarée au registre).",
                    hint=(
                        "Ajustez la valeur, ou la borne du registre si elle est "
                        "obsolète — une borne fausse est pire qu'aucune borne."
                    ),
                    id="config.W002",
                )
            )
    return issues


def _typed_value(definition, value: Any) -> Any:
    """Valeur convertie vers le type déclaré, SANS appliquer les bornes."""
    if definition.value_type == parsers.TYPE_BOOL and isinstance(value, bool):
        return value
    if definition.value_type == parsers.TYPE_LIST and isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return parsers.parse(
        definition.name,
        value,
        definition.value_type,
        choices=definition.choices,
        separator=definition.separator,
    )


# ---------------------------------------------------------------------------
# config.W003 — Gateway active sans repli
# ---------------------------------------------------------------------------
def check_gateway_without_fallback(app_configs: Any = None, **kwargs: Any) -> list[Any]:
    """AI_GATEWAY_ENABLED=1 + AI_GATEWAY_FALLBACK_ENABLED=0.

    Combinaison AUTORISÉE (mode strict assumé) mais rendue visible : une panne de
    la Gateway devient alors une panne de génération, sans repli.
    """
    # RAGatm Open : l'absence de repli est le comportement NOMINAL (aucun fournisseur de
    # repli n'existe). L'avertissement historique `config.W003` n'a plus d'objet ; le check
    # est conservé inerte pour ne pas déplacer les identifiants des autres checks.
    return []


# ---------------------------------------------------------------------------
# config.E009 — variables d'un fournisseur d'IA externe dans l'environnement
# ---------------------------------------------------------------------------
#: Variables qui n'ont AUCUN sens dans RAGatm Open : leur présence signale le rechargement
#: d'une configuration d'une autre édition. Fail-closed, sans repli ni avertissement.
EXTERNAL_PROVIDER_ENV_VARS = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_EMBEDDINGS_MODEL",
    "DEFAULT_OPENAI_MODEL",
    "ANTHROPIC_API_KEY",
    "MISTRAL_API_KEY",
    "COHERE_API_KEY",
    "GOOGLE_API_KEY",
)


def check_no_external_provider_env_vars(app_configs: Any = None, **kwargs: Any) -> list[Any]:
    """``config.E009`` — aucune variable OPENAI_*/ANTHROPIC_*/… ne doit être posée.

    RAGatm Open ne lit jamais ces variables ; les refuser au démarrage empêche qu'un ancien
    fichier d'environnement donne l'ILLUSION d'un fournisseur externe configuré.
    """
    present = [name for name in EXTERNAL_PROVIDER_ENV_VARS if (os.environ.get(name) or "").strip()]
    if not present:
        return []
    return [
        Error(
            "Variables d'un fournisseur d'IA externe présentes dans l'environnement : "
            + ", ".join(present) + ". RAGatm Open n'a aucun fournisseur externe.",
            hint="Supprimez ces variables du fichier d'environnement (elles ne sont jamais lues).",
            id="config.E009",
        )
    ]


# ---------------------------------------------------------------------------
# config.E003 — durcissement production
# ---------------------------------------------------------------------------
def check_production_hardening(app_configs: Any = None, **kwargs: Any) -> list[Any]:
    """Interrupteurs de développement laissés ouverts en staging/production.

    Complète (sans les dupliquer) les refus de démarrage de
    `config/settings/prod.py`. Aucune valeur sensible n'est reproduite.
    """
    if not _is_prod_like():
        return []

    env = _env()
    issues: list[Any] = []

    if bool(getattr(settings, "DEBUG", False)):
        issues.append(
            Error(f"DEBUG est actif en DJANGO_ENV={env}.", id="config.E003")
        )

    if not list(getattr(settings, "ALLOWED_HOSTS", []) or []):
        issues.append(
            Error(
                f"ALLOWED_HOSTS est vide en DJANGO_ENV={env}.",
                hint="Renseignez DJANGO_ALLOWED_HOSTS (liste séparée par des virgules).",
                id="config.E003",
            )
        )
    elif "*" in getattr(settings, "ALLOWED_HOSTS", []):
        issues.append(
            Error(
                f"ALLOWED_HOSTS contient le joker '*' en DJANGO_ENV={env}.",
                id="config.E003",
            )
        )

    for name, forbidden in _DEMO_VALUES.items():
        value = getattr(settings, name, None)
        if value is None:
            value = os.environ.get(name)
        if not value:
            continue
        if str(value).strip().lower() in forbidden:
            issues.append(
                Error(
                    f"{name} porte une valeur de DÉMONSTRATION en DJANGO_ENV={env}.",
                    hint="Générez une valeur propre à cet environnement.",
                    id="config.E003",
                )
            )

    if bool(getattr(settings, "ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS", False)):
        issues.append(
            Error(
                "ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS=1 en "
                f"DJANGO_ENV={env} : l'échappatoire de tests désarme la politique IA.",
                id="config.E003",
            )
        )

    if bool(getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False)):
        issues.append(
            Error(
                f"CELERY_TASK_ALWAYS_EAGER=1 en DJANGO_ENV={env} : les tâches longues "
                "s'exécuteraient dans le processus HTTP.",
                id="config.E003",
            )
        )

    if bool(getattr(settings, "INVITE_RETURN_RESET_LINK", False)):
        issues.append(
            Error(
                f"INVITE_RETURN_RESET_LINK=1 en DJANGO_ENV={env} : un token de "
                "réinitialisation valide serait renvoyé dans une réponse API.",
                id="config.E003",
            )
        )

    if bool(getattr(settings, "CORE_CONFIG_EDIT_ENABLED", False)):
        # Prévu pour du staging contrôlé ; jamais pour la production.
        factory = Error if env == "prod" else CheckWarning
        issues.append(
            factory(
                f"CORE_CONFIG_EDIT_ENABLED=1 en DJANGO_ENV={env}.",
                hint="L'édition d'overrides Core est réservée à un staging contrôlé.",
                id="config.E003" if env == "prod" else "config.W004",
            )
        )

    return issues


# ---------------------------------------------------------------------------
# config.E010 — déclarée comme projetée, absente des settings
# ---------------------------------------------------------------------------
#: Ce que ce contrôle attrape, précisément : un module APPLICATIF lit un réglage
#: par `getattr(settings, nom, défaut)`, et aucun module de settings ne définit ce
#: nom. Le code retient alors son défaut local. La variable a l'air configurable,
#: la documentation l'annonce, et la poser ne fait rien. Mesuré avant ce contrôle :
#: vingt variables dans ce cas en production.
#:
#: Ce qu'il n'attrape PAS, volontairement : les réglages consommés À L'INTÉRIEUR
#: des settings (`DB_NAME` alimente `DATABASES`, `DRF_THROTTLE_*` alimente
#: `REST_FRAMEWORK`, `DJANGO_SECRET_KEY` alimente `SECRET_KEY`). Ceux-là n'ont
#: aucune raison d'exister comme attribut de même nom, et l'exiger produirait des
#: dizaines de faux positifs — un contrôle bruyant finit désactivé.
_PROJECTED_SOURCE_PREFIX = "backend/config/settings/"
_APP_CONSUMER_PREFIXES = ("apps.", "api.")


def _read_through_django_settings(definition) -> bool:
    """Un consommateur applicatif lit-il ce réglage via `django.conf.settings` ?"""
    return any(
        consumer.startswith(_APP_CONSUMER_PREFIXES) for consumer in definition.consumers
    )


def check_declared_but_not_projected(app_configs: Any = None, **kwargs: Any) -> list[Any]:
    """Tout réglage lu par un module applicatif est réellement dans les settings."""
    issues: list[Any] = []
    for definition in registry.all_definitions():
        if not definition.source.startswith(_PROJECTED_SOURCE_PREFIX):
            # Le registre déclare un autre porteur (module applicatif qui lit
            # l'environnement directement, compose, script) : rien à vérifier ici.
            continue
        if not _read_through_django_settings(definition):
            continue
        if "test" in definition.environments and len(definition.environments) == 1:
            continue  # clé réservée aux tests : jamais projetée
        if hasattr(settings, definition.name):
            continue
        issues.append(
            Error(
                f"{definition.name} est déclarée au registre (domaine "
                f"« {definition.domain} », portée par {definition.source}) mais "
                "absente des settings.",
                hint=(
                    f"Ajoutez `{definition.name} = reg(\"{definition.name}\")` dans "
                    f"{definition.source}, ou corrigez le champ `source` de la "
                    "définition si un autre module la porte. Sans projection, la "
                    "poser dans un `.env` n'a AUCUN effet : le code lit son défaut."
                ),
                id="config.E010",
            )
        )
    return issues


# ---------------------------------------------------------------------------
# config.W010 — déclarée mais consommée par personne
# ---------------------------------------------------------------------------
#: Consommateurs qui ne sont pas du code applicatif : Docker, Celery, Gunicorn,
#: scripts de déploiement. Un paramètre qu'ils sont seuls à lire est légitime, et
#: aucune analyse du code Python ne peut le voir.
_NON_PYTHON_CONSUMERS = ("docker", "compose", "deploy", "gunicorn", "entrypoint", "script")


def check_declared_but_unconsumed(app_configs: Any = None, **kwargs: Any) -> list[Any]:
    """Signale une définition sans consommateur déclaré.

    Ne prétend PAS analyser le code : le registre porte la liste `consumers`, et
    c'est elle qu'on interroge. Une définition sans consommateur est soit un
    réglage mort, soit une déclaration incomplète — les deux méritent un mot.
    """
    issues: list[Any] = []
    for definition in registry.all_definitions():
        if definition.consumers:
            continue
        if definition.deprecated:
            continue  # traité par W011 : inutile de le dire deux fois
        porteur = definition.source.lower()
        if any(marqueur in porteur for marqueur in _NON_PYTHON_CONSUMERS):
            continue
        issues.append(
            CheckWarning(
                f"{definition.name} est déclarée sans aucun consommateur "
                f"(domaine « {definition.domain} »).",
                hint=(
                    "Renseignez `consumers=(...)` si un module la lit, ou retirez la "
                    "définition. Un réglage que personne ne lit se configure en vain, "
                    "et se documente en pure perte."
                ),
                id="config.W010",
            )
        )
    return issues


# ---------------------------------------------------------------------------
# config.W011 — nom déprécié encore posé dans l'environnement
# ---------------------------------------------------------------------------
def check_deprecated_names_still_set(app_configs: Any = None, **kwargs: Any) -> list[Any]:
    """Un alias déprécié encore posé continue de fonctionner — jusqu'à sa date."""
    issues: list[Any] = []
    env = _env()
    for alias in registry.DEPRECATED_ALIASES:
        if os.environ.get(alias.old_name) is None:
            continue
        issues.append(
            CheckWarning(
                f"{alias.old_name} est posée (DJANGO_ENV={env}) alors qu'elle est "
                f"dépréciée depuis {alias.since} au profit de {alias.new_name}.",
                hint=(
                    f"Renommez la variable en {alias.new_name}. La compatibilité "
                    f"s'arrête après {alias.remove_after} : passé cette date, la "
                    "valeur sera ignorée sans avertissement."
                ),
                id="config.W011",
            )
        )
    deja_signalees = {a.old_name for a in registry.DEPRECATED_ALIASES}
    for definition in registry.all_definitions():
        if not definition.deprecated:
            continue
        if definition.name in deja_signalees:
            # Un nom à la fois déprécié ET porteur d'un alias serait signalé deux
            # fois : le message de l'alias est plus précis (il porte la date de
            # retrait), on garde celui-là.
            continue
        if os.environ.get(definition.name) is None:
            continue
        remplacement = definition.replacement or "aucun remplaçant"
        issues.append(
            CheckWarning(
                f"{definition.name} est posée (DJANGO_ENV={env}) alors qu'elle est "
                f"dépréciée. Remplacement : {remplacement}.",
                hint=(
                    "Retirez la ligne de l'environnement. Une variable dépréciée "
                    "encore posée entretient l'idée qu'elle pilote quelque chose."
                ),
                id="config.W011",
            )
        )
    return issues


# ---------------------------------------------------------------------------
# config.W012 — posée dans l'environnement, inconnue du registre
# ---------------------------------------------------------------------------
#: Préfixes des variables qui appartiennent à RagATM. Hors de cette liste, on ne
#: dit rien : l'environnement d'un conteneur porte des dizaines de variables
#: système, et un contrôle bavard finit ignoré.
_APP_PREFIXES = (
    "RAG_", "RAGATM_", "CHAT_", "CORPUS_", "CORE_CONFIG_", "TABULAR_", "DOCUMENT_",
    "EMBEDDINGS_", "AI_GATEWAY_", "ACTIONS_", "OWNER_SCOPE_", "WORKSPACE_", "FEEDBACK_",
)


def check_unknown_app_variables(app_configs: Any = None, **kwargs: Any) -> list[Any]:
    """Variable posée avec un préfixe RagATM mais inconnue du registre.

    C'est le contrôle qui attrape la variable survivante : celle qu'un `.env` a
    conservée après le retrait d'un flag, ou celle dont le nom a été mal orthographié.
    Elle ne fait rien, ne dit rien, et laisse croire qu'un réglage est en place.
    """
    observees = [
        nom
        for nom in os.environ
        if nom.startswith(_APP_PREFIXES)
    ]
    inconnues = registry.iter_unregistered(observees)
    if not inconnues:
        return []
    return [
        CheckWarning(
            f"{nom} est posée dans l'environnement mais inconnue du registre de "
            "configuration : elle n'a aucun effet.",
            hint=(
                "Retirez la ligne, ou déclarez le paramètre s'il doit exister. "
                "Une variable inconnue est le plus souvent le vestige d'un réglage "
                "supprimé, ou une faute de frappe sur un réglage réel."
            ),
            id="config.W012",
        )
        for nom in inconnues
    ]


# ---------------------------------------------------------------------------
# config.E011 / config.W013 — la révision annoncée est celle qui tourne
# ---------------------------------------------------------------------------
#: Écrit par le Dockerfile à partir de l'argument de build. Immuable dans l'image :
#: aucune variable d'environnement ne peut le contredire sans qu'on le voie.
_BUILD_REVISION_FILE = "/app/BUILD_REVISION"


def _built_revision() -> str:
    try:
        with open(_BUILD_REVISION_FILE, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        # Pas de fichier : exécution hors image (dev local, tests). Il n'y a alors
        # rien à comparer — et rien à reprocher.
        return ""


def check_build_revision(app_configs: Any = None, **kwargs: Any) -> list[Any]:
    """La révision annoncée par la configuration est celle qui a été construite.

    En production, `APP_GIT_SHA` a annoncé pendant plusieurs jours un commit vieux
    de 34 révisions : posée à la main dans un `.env`, elle n'était pas rafraîchie
    par le déploiement. Toute trace d'observabilité et tout rapport d'incident
    attribuaient donc les mesures au mauvais code — un défaut invisible, puisque
    rien ne comparait jamais l'annonce à la réalité.

    PORTÉE EXACTE — ce que ce contrôle ne prouve PAS
    -------------------------------------------------
    Il compare la configuration d'EXÉCUTION (`APP_GIT_SHA`) au fichier gravé dans
    l'IMAGE (`/app/BUILD_REVISION`). Il attrape donc une variable d'environnement
    qui contredit l'image — le défaut de 2026-07.

    Il ne dit RIEN du code réellement contenu dans l'image. En déploiement, les
    deux valeurs descendent de la même source : `DEPLOYED_FROM.txt`, lu par
    `scripts/deploy/build-env.sh`, propagé en `--build-arg` puis écrit par le
    Dockerfile. Si ce fichier est PÉRIMÉ, les deux valeurs sont périmées ET
    concordantes : ce contrôle reste silencieux. C'est exactement l'état rencontré
    le 2026-09-07 — arbre extrait en `79466bb`, fichier annonçant `23c97fc`.

    Le terme manquant est fourni hors application, par le manifeste de release :
    `scripts/deploy/release.sh` l'écrit dans le même geste que l'extraction,
    `build-env.sh --verify` refuse le build s'il diverge, et
    `scripts/deploy/assert-revision.sh` confronte les quatre termes après coup.

    Ce contrôle reste donc NÉCESSAIRE et non suffisant. Il n'est pas remplacé par
    les gardes de déploiement : il couvre le cas qu'elles ne voient pas — une
    variable posée à la main dans l'environnement d'un conteneur déjà construit.
    """
    issues: list[Any] = []
    annoncee = str(getattr(settings, "APP_GIT_SHA", "") or "").strip()
    construite = _built_revision()

    if annoncee.endswith("-dirty"):
        message = (
            "APP_GIT_SHA désigne un arbre de travail MODIFIÉ "
            f"({annoncee[:12]}…-dirty) : ce n'est pas un commit reproductible."
        )
        issues.append(
            _severity(
                message,
                hint=(
                    "Acceptable en développement. En staging ou en production, "
                    "construisez depuis un arbre propre : une révision non "
                    "reproductible rend tout diagnostic ultérieur incertain."
                ),
                check_id="config.W013",
            )
        )
        return issues

    if not construite:
        return issues  # hors image : rien à confronter

    if not annoncee:
        issues.append(
            Error(
                "APP_GIT_SHA est vide alors que l'image porte la révision "
                f"{construite[:12]}… : la provenance est perdue à l'exécution.",
                hint=(
                    "Laissez le compose propager `APP_GIT_SHA` depuis le build "
                    "(`eval \"$(scripts/deploy/build-env.sh)\"`) plutôt que de la "
                    "poser dans un `.env`."
                ),
                id="config.E011",
            )
        )
        return issues

    if annoncee != construite:
        issues.append(
            Error(
                f"APP_GIT_SHA annonce {annoncee[:12]}… alors que l'image a été "
                f"construite depuis {construite[:12]}….",
                hint=(
                    "Une variable d'environnement contredit l'image. Retirez "
                    "`APP_GIT_SHA` du `.env` : elle est injectée par le build "
                    "(`scripts/deploy/build-env.sh`), et une valeur saisie à la "
                    "main finit toujours par dater."
                ),
                id="config.E011",
            )
        )
    return issues


#: Marge entre la fin d'une extraction « délai étendu » et la limite douce Celery.
#: Elle couvre ce qui suit l'extraction dans la même tâche : chunking, appels
#: d'embeddings, écriture des chunks. Sans elle, la tâche serait tuée juste après
#: avoir extrait le texte — le plus coûteux, et pour rien.
#: Réserve NOMINALE laissée sous la limite douce Celery pour le travail qui suit
#: l'extraction (découpage, embeddings, publication).
#:
#: ATTENTION — cette valeur n'est PAS dérivée d'une mesure. Le travail post-extraction
#: a été observé jusqu'à plusieurs centaines de secondes sur des documents lourds,
#: c'est-à-dire bien au-delà de cette réserve. Le contrôle ci-dessous établit donc une
#: condition NÉCESSAIRE, jamais suffisante : le franchir ne prouve pas qu'une tâche
#: survivra, seulement qu'elle n'est pas condamnée par construction.
#:
#: Choisir la bonne valeur exige une distribution (p50, p95, max, N) de la durée
#: post-extraction, pas quelques documents. Tant que cette mesure n'existe pas, aucune
#: valeur nouvelle n'est inventée ici : la corriger au jugé remplacerait une garde
#: fausse par une garde fausse rassurante.
EXTENDED_TIMEOUT_SAFETY_MARGIN_SECONDS = 60


def check_extraction_timeouts(app_configs=None, **kwargs):
    """Le budget d'extraction étendu doit tenir SOUS la limite douce Celery.

    Une incohérence ici ne se voit pas au démarrage ni dans les tests : elle se
    manifeste en production par une tâche tuée en cours d'extraction, sur les
    documents les plus lourds — exactement ceux que la stratégie devait sauver.
    Elle se détecte pourtant par une soustraction.

    Portée EXACTE de ce contrôle, à ne pas surinterpréter : il vérifie une condition
    NÉCESSAIRE. La réserve qu'il applique est nominale et non mesurée (cf.
    ``EXTENDED_TIMEOUT_SAFETY_MARGIN_SECONDS``). Un contrôle vert signifie donc « pas
    condamné par construction », et non « sûr ». La calibration réelle attend une
    distribution des durées post-extraction.
    """
    from django.core.checks import Error
    from django.core.checks import Warning as CheckWarning

    issues = []
    standard = int(getattr(settings, "DOCUMENT_EXTRACTION_TIMEOUT_SECONDS", 180))
    extended = int(getattr(settings, "DOCUMENT_EXTRACTION_EXTENDED_TIMEOUT_SECONDS", 480))

    # La tâche la plus contraignante décide : une reprise unitaire passe par
    # `ingest_document_task` (limite douce globale), un item de job par
    # `reindex_job_item_task` (limite propre). Le budget doit tenir dans les deux.
    soft_limits = {
        "CELERY_TASK_SOFT_TIME_LIMIT": int(getattr(settings, "CELERY_TASK_SOFT_TIME_LIMIT", 540)),
        "REINDEX_ITEM_SOFT_TIME_LIMIT": int(getattr(settings, "REINDEX_ITEM_SOFT_TIME_LIMIT", 600)),
    }
    binding_name = min(soft_limits, key=lambda k: soft_limits[k])
    binding = soft_limits[binding_name]
    budget = binding - EXTENDED_TIMEOUT_SAFETY_MARGIN_SECONDS

    if extended > budget:
        issues.append(
            Error(
                f"DOCUMENT_EXTRACTION_EXTENDED_TIMEOUT_SECONDS={extended} dépasse le "
                f"budget disponible ({budget} s = {binding_name}={binding} − "
                f"{EXTENDED_TIMEOUT_SAFETY_MARGIN_SECONDS} s de marge).",
                hint=(
                    "La tâche serait tuée avant la fin de l'extraction. Baissez le "
                    "budget étendu, ou relevez la limite douce de la tâche qui "
                    "l'exécute. La marge retenue est nominale, pas mesurée : la "
                    "respecter ne garantit pas la survie de la tâche."
                ),
                id="config.E012",
            )
        )
    if extended <= standard:
        issues.append(
            CheckWarning(
                f"DOCUMENT_EXTRACTION_EXTENDED_TIMEOUT_SECONDS={extended} n'excède pas "
                f"DOCUMENT_EXTRACTION_TIMEOUT_SECONDS={standard} : la stratégie de "
                "reprise « délai étendu » n'accorde alors aucun délai supplémentaire.",
                hint="Relevez le budget étendu, ou abaissez le budget standard.",
                id="config.W013",
            )
        )
    return issues


ALL_CHECKS = (
    check_no_external_provider_env_vars,
    check_settings_load_anomalies,
    check_deprecated_variables,
    check_registry_conformance,
    check_gateway_without_fallback,
    check_production_hardening,
    check_declared_but_not_projected,
    check_declared_but_unconsumed,
    check_deprecated_names_still_set,
    check_unknown_app_variables,
    check_build_revision,
    check_extraction_timeouts,
)


def register_all() -> None:
    """Enregistre les checks de configuration (appelé depuis un AppConfig.ready)."""
    from django.core.checks import register

    for check in ALL_CHECKS:
        register(check)
