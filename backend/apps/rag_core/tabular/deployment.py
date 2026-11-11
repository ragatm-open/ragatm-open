"""Capacité de DÉPLOIEMENT — rendre calculable ce qui n'était que documenté.

Le moteur borne correctement un processus. Mais un processus n'est pas un déploiement :

    capacité instance    = workers × limite_locale
    capacité déploiement = capacité instance × réplicas

Tant que `workers` et `réplicas` ne sont pas *déclarés et vérifiés*, la capacité totale
reste une inconnue — et une inconnue ne se déploie pas en production.

---- Ce que ce module sait, et ce qu'il ne peut pas savoir ----

Un processus Python peut connaître sa propre limite et lire `GUNICORN_WORKERS`. Il ne
peut **pas** connaître le nombre de réplicas : cette information vit dans
l'orchestrateur, pas dans le conteneur. Elle doit donc être **déclarée** par le
déploiement (`TABULAR_DEPLOYMENT_REPLICAS`).

Quand elle ne l'est pas, ce module répond `unknown` — jamais une valeur supposée. Une
capacité supposée est pire qu'une capacité inconnue : elle inspire une confiance que
rien ne soutient.

---- Source de vérité des workers ----

Vérifiée dans `backend/entrypoint.sh` : `GUNICORN_WORKERS` (défaut 2) est passé à
`--workers`. Source unique, aucun `gunicorn.conf.py`, aucun `GUNICORN_CMD_ARGS`. Les
`--threads 4` ne multiplient PAS la capacité : le sémaphore est par processus et les
threads d'un même worker le partagent.
"""
from __future__ import annotations

import logging
import os

from django.conf import settings

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Codes de diagnostic de DÉPLOIEMENT (jamais des réponses métier utilisateur)
# --------------------------------------------------------------------------- #
CFG_MISSING = "tabular_capacity_config_missing"
CFG_INVALID = "tabular_capacity_config_invalid"
WORKERS_UNKNOWN = "tabular_worker_count_unknown"
CFG_MISMATCH = "tabular_capacity_config_mismatch"
CFG_UNSAFE = "tabular_capacity_unsafe"

#: Mémoire estimée par exécution tabulaire ACTIVE, en Mo.
#:
#: Fondée sur la mesure du lot précédent : 159 Mo à une requête, 195 Mo à vingt
#: requêtes dont deux actives — soit ~18 Mo par exécution active au-dessus d'une base
#: d'environ 160 Mo. On retient 64 Mo, largement au-dessus de l'observé : le modèle
#: doit se tromper du côté prudent, et les mesures viennent d'un corpus de 20 000
#: lignes, pas du plafond de 100 000.
ESTIMATED_MB_PER_ACTIVE_QUERY = 64
#: Empreinte de base d'un worker (Django, DuckDB chargé, interpréteur).
ESTIMATED_MB_PER_WORKER = 200


class CapacityConfigurationError(RuntimeError):
    """Configuration de capacité inexploitable. Bloquante en production."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _env_int(name: str) -> int | None:
    """Entier d'environnement, ou `None` si absent/vide/illisible.

    `None` signifie « inconnu », jamais « défaut » : c'est l'appelant qui décide si une
    valeur inconnue est acceptable, et il ne peut le décider que s'il la voit.
    """
    brut = os.getenv(name, "").strip()
    if not brut:
        return None
    try:
        return int(brut)
    except ValueError:
        logger.warning("tabular.deployment %s=%r non numérique -> inconnu", name, brut)
        return None


def worker_count() -> tuple[int | None, str]:
    """`(nombre, source)`. `None` si indéterminable.

    Le défaut de `entrypoint.sh` est 2 ; on le reflète explicitement plutôt que de le
    deviner, et on dit d'où vient la valeur — un opérateur doit pouvoir vérifier.
    """
    declare = _env_int("GUNICORN_WORKERS")
    if declare is not None:
        return declare, "GUNICORN_WORKERS"
    # Aucune variable : `entrypoint.sh` appliquera son propre défaut.
    return 2, "entrypoint.sh (défaut)"


def replica_count() -> tuple[int | None, str]:
    """`(nombre, source)`. `None` = INCONNU, et c'est une réponse légitime.

    Le nombre de réplicas vit dans l'orchestrateur. Un conteneur ne peut pas le
    découvrir ; il doit lui être déclaré.
    """
    declare = _env_int("TABULAR_DEPLOYMENT_REPLICAS")
    if declare is not None:
        return declare, "TABULAR_DEPLOYMENT_REPLICAS"
    return None, "non déclaré"


def available_memory_mb() -> int | None:
    """Mémoire disponible de l'hôte, ou `None` si indéterminable."""
    try:
        with open("/proc/meminfo", encoding="ascii") as flux:
            for ligne in flux:
                if ligne.startswith("MemAvailable:"):
                    return int(ligne.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def describe(*, strict: bool = False) -> dict:
    """Capacité effective, avec la provenance et la validité de chaque valeur.

    `strict=True` (production) transforme une inconnue en erreur ; sinon elle est
    rapportée telle quelle.
    """
    from apps.rag_core.tabular import capacity as CAP

    problemes: list[dict] = []

    limite = CAP.configured_limit()
    brut_limite = getattr(settings, "TABULAR_MAX_CONCURRENT_QUERIES", None)
    if brut_limite is None:
        problemes.append({"code": CFG_MISSING,
                          "detail": "TABULAR_MAX_CONCURRENT_QUERIES non défini"})
    else:
        try:
            if int(brut_limite) != limite:
                problemes.append({
                    "code": CFG_INVALID,
                    "detail": f"valeur {brut_limite!r} corrigée en {limite}"})
        except (TypeError, ValueError):
            problemes.append({"code": CFG_INVALID,
                              "detail": f"valeur non numérique : {brut_limite!r}"})

    workers, source_workers = worker_count()
    if workers is None or workers < 1:
        problemes.append({"code": WORKERS_UNKNOWN,
                          "detail": f"nombre de workers indéterminable ({source_workers})"})
        workers = None

    replicas, source_replicas = replica_count()

    instance = limite * workers if workers else None
    deploiement = instance * replicas if (instance and replicas) else None

    # -- mémoire : estimation prudente, jamais présentée comme un plafond ----
    dispo = available_memory_mb()
    besoin = None
    if instance:
        besoin = (workers * ESTIMATED_MB_PER_WORKER
                  + instance * ESTIMATED_MB_PER_ACTIVE_QUERY)
        marge = int(getattr(settings, "TABULAR_MIN_MEMORY_HEADROOM_MB", 512) or 0)
        if dispo is not None and dispo < besoin + marge:
            problemes.append({
                "code": CFG_UNSAFE,
                "detail": (f"mémoire disponible {dispo} Mo < besoin estimé {besoin} Mo "
                           f"+ marge {marge} Mo")})

    plafond = _env_int("TABULAR_MAX_DEPLOYMENT_CAPACITY")
    if plafond is not None and deploiement is not None and deploiement > plafond:
        problemes.append({
            "code": CFG_UNSAFE,
            "detail": f"capacité déploiement {deploiement} > plafond déclaré {plafond}"})

    if strict and replicas is None:
        problemes.append({
            "code": WORKERS_UNKNOWN,
            "detail": ("nombre de réplicas non déclaré : la capacité de déploiement "
                       "n'est pas calculable (TABULAR_DEPLOYMENT_REPLICAS)")})

    return {
        "scope": "process",
        "local_limit": limite,
        "worker_count": workers,
        "worker_count_source": source_workers,
        "instance_capacity": instance,
        "replica_count": replicas,
        "replica_count_source": source_replicas,
        # `None` et non 0 : une capacité inconnue n'est pas une capacité nulle.
        "deployment_capacity": deploiement,
        "deployment_capacity_known": deploiement is not None,
        "globally_enforced": False,
        "estimated_memory_need_mb": besoin,
        "available_memory_mb": dispo,
        "configuration_valid": not problemes,
        "problems": problemes,
    }


def validate_or_raise(*, strict: bool) -> dict:
    """Garde de démarrage. Lève en mode strict si la configuration est inexploitable."""
    etat = describe(strict=strict)
    if etat["configuration_valid"]:
        logger.info(
            "event=tabular_capacity_configuration local_limit=%s worker_count=%s "
            "instance_capacity=%s replica_count=%s deployment_capacity=%s "
            "scope=process globally_enforced=false configuration_valid=true",
            etat["local_limit"], etat["worker_count"], etat["instance_capacity"],
            etat["replica_count"], etat["deployment_capacity"])
        return etat

    for probleme in etat["problems"]:
        logger.error("event=tabular_capacity_configuration_error code=%s detail=%s",
                     probleme["code"], probleme["detail"])

    if strict:
        premier = etat["problems"][0]
        raise CapacityConfigurationError(
            premier["code"],
            f"configuration de capacité tabulaire inexploitable : {premier['detail']}")
    return etat
