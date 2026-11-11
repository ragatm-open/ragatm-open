"""Supervision des suppressions définitives de RagSpace en ÉCHEC.

Le défaut corrigé : une purge qui échoue écrit bien un ``ActionAuditEvent``
``status="failed"``… que personne ne lit. Un échec de purge n'était visible que
par requête SQL, donc en pratique jamais — alors que c'est précisément l'état qui
laisse des fichiers détruits face à une base intacte (cf. ``rag_space_deletion``).

Ce module est **strictement en lecture** : il interroge l'audit, il ne l'écrit
pas, ne le modifie pas et ne le supprime pas. ``ActionAuditEvent`` est append-only ;
lui ajouter un drapeau « alerte lue » exigerait une migration pour un besoin que
la déduplication par curseur couvre déjà.

DÉDUPLICATION
-------------
Deux garde-fous superposés, du plus précis au plus grossier :

1. **curseur** — horodatage du dernier événement déjà signalé, conservé dans
   Redis (le broker Celery, déjà présent), sous une clé **namespacée par
   environnement**. Une exécution ne rapporte que ce qui est strictement
   postérieur ;
2. **fenêtre glissante** — si Redis est injoignable, le curseur absent, illisible
   ou situé dans le futur, on retombe sur une fenêtre bornée. Le pire cas est
   alors un doublon d'alerte : bruit acceptable, mais jamais un silence.

On préfère assumer un doublon qu'un trou. Un échec de purge non signalé est un
incident invisible ; un échec signalé deux fois est une ligne de journal en trop.
C'est aussi pourquoi **aucune anomalie de curseur n'est fatale** : il n'existe
aucun chemin par lequel un état Redis corrompu puisse éteindre l'alerte.

CONFIDENTIALITÉ
---------------
L'alerte ne transporte que des identifiants techniques, des codes et des
compteurs : aucun nom d'espace, de document ou de fichier, aucun chemin, aucun
e-mail, aucun contenu. Le nom du RagSpace vit dans ``metadata.rag_space_name`` de
l'audit — il n'est **volontairement pas** recopié ici, un journal centralisé
n'ayant pas besoin de connaître le nom commercial d'un espace pour déclencher une
investigation par identifiant.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from django.utils import timezone

from apps.audit.models import ActionAuditEvent
from apps.rag_core.services.rag_space_deletion import ACTION_NAME

logger = logging.getLogger(__name__)

STATUS_OK = "ok"
STATUS_ALERT = "deletion_failures_detected"

#: Préfixe de la clé Redis du curseur. Stable : la renommer rejouerait tout
#: l'historique de la fenêtre au premier passage suivant.
CURSOR_KEY_PREFIX = "ragatm:ragspace-delete-monitor:cursor"

#: Le curseur ne porte **aucun TTL**, volontairement. Un curseur qui expire
#: réinitialise silencieusement la déduplication : au réveil suivant, toute la
#: fenêtre ré-alerte. Un curseur périmé, lui, est inoffensif — `scan()` prend
#: `max(fenêtre, curseur)`, donc un curseur ancien n'élargit jamais la fenêtre.
CURSOR_TTL_SECONDS = None


def cursor_key() -> str:
    """Clé du curseur, **namespacée par environnement**.

    Chaque stack a aujourd'hui son propre Redis, mais s'en remettre à cette
    isolation est un pari sur la topologie : le jour où staging et production
    partagent une instance (ou une base Redis), un curseur commun ferait taire
    les alertes de l'un parce que l'autre a déjà « vu » l'événement. Le
    namespace rend cette confusion impossible par construction.
    """
    from django.conf import settings

    env = str(getattr(settings, "DJANGO_ENV", "") or "unknown").strip().lower() or "unknown"
    return f"{CURSOR_KEY_PREFIX}:{env}"

#: Plafond d'événements rapportés en une exécution. Une avalanche (incident de
#: stockage généralisé) ne doit pas produire un message de journal illisible ni
#: une requête non bornée — le compte total reste exact, la liste est tronquée.
DEFAULT_LIMIT = 50

#: Compteurs remontés dans l'alerte (nomenclature du service de suppression).
COUNTER_KEYS = (
    "documents_total", "chunks_total", "files_total",
    "files_deleted", "files_missing", "files_failed",
)


# --------------------------------------------------------------------------- #
# Curseur (dédup) — Redis, avec dégradation SILENCIEUSE vers la fenêtre seule
# --------------------------------------------------------------------------- #
def read_cursor() -> datetime | None:
    """Horodatage du dernier échec déjà signalé. ``None`` si inconnu ou suspect.

    Toute anomalie retombe sur ``None``, donc sur la fenêtre glissante : **rien
    ne doit pouvoir faire taire l'alerte**. Un curseur illisible, mal formé ou
    situé dans le futur est ignoré, pas honoré.
    """
    from apps.rag_core.services.periodic import redis_client

    client = redis_client()
    if client is None:
        return None
    try:
        raw = client.get(cursor_key())
    except Exception:  # noqa: BLE001 — Redis indisponible : on retombe sur la fenêtre.
        # Aucun détail de connexion journalisé (l'URL du broker porte des identifiants).
        logger.warning("ragspace_delete_monitor: curseur illisible -> fenêtre seule")
        return None
    if not raw:
        return None
    try:
        value = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
        parsed = datetime.fromisoformat(value)
    except (ValueError, UnicodeDecodeError):
        logger.warning("ragspace_delete_monitor: curseur illisible (format) -> fenêtre seule")
        return None
    parsed = parsed if timezone.is_aware(parsed) else timezone.make_aware(parsed)

    # Curseur DANS LE FUTUR : horloge décalée, écriture corrompue, restauration
    # d'un dump plus récent. L'honorer supprimerait toute alerte jusqu'à ce que
    # l'heure le rattrape — un silence permanent, invisible. On l'ignore.
    if parsed > timezone.now():
        logger.warning(
            "ragspace_delete_monitor: curseur dans le futur -> ignoré (fenêtre seule)"
        )
        return None
    return parsed


def write_cursor(value: datetime | None) -> bool:
    """Mémorise l'horodatage du dernier échec signalé. ``False`` si non persisté."""
    if value is None:
        return False
    from apps.rag_core.services.periodic import redis_client

    client = redis_client()
    if client is None:
        return False
    try:
        # Sans TTL : cf. CURSOR_TTL_SECONDS — un curseur périmé est inoffensif,
        # un curseur qui expire relance une salve d'alertes déjà traitées.
        client.set(cursor_key(), value.isoformat())
        return True
    except Exception:  # noqa: BLE001
        logger.warning("ragspace_delete_monitor: curseur non persisté (alerte possible en double)")
        return False


# --------------------------------------------------------------------------- #
# Analyse
# --------------------------------------------------------------------------- #
def _alert_payload(ev: ActionAuditEvent) -> dict[str, Any]:
    """Projection MINIMALE d'un événement — identifiants, code, compteurs."""
    md = ev.metadata or {}
    payload: dict[str, Any] = {
        "event_id": str(ev.id),
        "created_at": ev.created_at.isoformat(),
        "workspace_id": ev.workspace_id or "",
        "rag_space_id": str(ev.rag_space_id) if ev.rag_space_id else "",
        "actor_id": ev.actor_id,
        "error_code": ev.error_code or "",
        "duration_ms": ev.duration_ms,
    }
    for key in COUNTER_KEYS:
        value = md.get(key)
        # Seuls des entiers passent : un audit ancien (avant P0-4) n'a pas de
        # compteurs, et un `metadata` inattendu ne doit rien injecter d'autre.
        payload[key] = value if isinstance(value, int) else None
    return payload


def scan(
    *,
    window_seconds: int,
    since: datetime | None = None,
    now: datetime | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Cherche les purges échouées non encore signalées. Aucune écriture.

    ``since`` (curseur) l'emporte sur la fenêtre quand il est plus récent : il
    évite de re-signaler. Quand il est plus ancien — première exécution, Redis
    perdu, longue interruption — la fenêtre borne la requête.
    """
    now = now or timezone.now()
    window_start = now - timedelta(seconds=int(window_seconds))
    start = max(window_start, since) if since else window_start

    qs = (
        ActionAuditEvent.objects.filter(
            action_name=ACTION_NAME, status="failed", created_at__gt=start
        )
        .order_by("created_at")
    )
    total = qs.count()
    events = list(qs[:limit])
    failures = [_alert_payload(ev) for ev in events]

    return {
        "status": STATUS_ALERT if total else STATUS_OK,
        "window_seconds": int(window_seconds),
        "since": start.isoformat(),
        "until": now.isoformat(),
        "cursor_used": since.isoformat() if since else None,
        "failures_total": total,
        "reported": len(failures),
        "truncated": total > len(failures),
        "failures": failures,
        # Curseur à mémoriser : le dernier événement RAPPORTÉ. En cas de
        # troncature, le reste sera repris au passage suivant plutôt que sauté.
        "cursor": events[-1].created_at.isoformat() if events else None,
    }


def log_report(report: dict[str, Any]) -> None:
    """Journalise le rapport : ERROR par échec, INFO si tout va bien.

    Une ligne par échec (et non un bloc unique) : les collecteurs de journaux
    alertent sur des lignes, et un incident portant sur dix espaces doit produire
    dix signaux corrélables, pas un seul message tronqué.
    """
    if report["status"] == STATUS_OK:
        logger.info(
            "ragspace_delete_monitor status=ok window_seconds=%s failures_total=0",
            report["window_seconds"],
        )
        return

    for f in report["failures"]:
        logger.error(
            "ragspace_delete_failed event_id=%s created_at=%s workspace_id=%s "
            "rag_space_id=%s actor_id=%s error_code=%s duration_ms=%s "
            "documents_total=%s chunks_total=%s files_total=%s "
            "files_deleted=%s files_missing=%s files_failed=%s",
            f["event_id"], f["created_at"], f["workspace_id"], f["rag_space_id"],
            f["actor_id"], f["error_code"], f["duration_ms"],
            f["documents_total"], f["chunks_total"], f["files_total"],
            f["files_deleted"], f["files_missing"], f["files_failed"],
        )
    if report["truncated"]:
        logger.error(
            "ragspace_delete_monitor truncated failures_total=%s reported=%s "
            "— le reste sera signalé au passage suivant",
            report["failures_total"], report["reported"],
        )
