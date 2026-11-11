"""Audit de COHÉRENCE du stockage documentaire — références en base ↔ fichiers.

Ce module est la logique métier de ``manage.py audit_media_storage``, extraite ici
pour qu'une tâche planifiée puisse l'exécuter **sans parser la sortie texte d'une
commande de management**. Une seule implémentation, trois consommateurs : la
commande (opérateur), la tâche périodique (exploitation), les tests.

POURQUOI CET AUDIT DOIT TOURNER PÉRIODIQUEMENT
----------------------------------------------
La purge d'un RagSpace supprime les fichiers AVANT les lignes de base
(cf. ``rag_space_deletion``). Ce choix est défendable pour une seule raison :
l'échec bascule vers le cas **détectable** « référence sans fichier » plutôt que
vers le cas silencieux « fichier sans référence, introuvable à jamais ». Mais un
risque n'est détectable que si quelqu'un regarde. Tant que cet audit n'était
lancé qu'à la main, la compensation n'existait que sur le papier.

STRICTEMENT READ-ONLY
---------------------
Ce module ne supprime, ne déplace et ne modifie **rien** — ni fichier, ni ligne.
Un fichier orphelin est signalé, jamais nettoyé automatiquement : un orphelin
peut être la trace d'un incident en cours d'analyse, et l'effacer détruirait la
preuve en même temps que le problème.

CONFIDENTIALITÉ
---------------
Le rapport contient ``media_root`` et, sur demande explicite, des **exemples** de
noms de fichiers : c'est nécessaire à l'opérateur devant sa console. Les appelants
automatiques (tâche périodique, supervision) utilisent :func:`public_summary`, qui
n'expose que des compteurs — aucun chemin ne part dans les journaux centraux.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from django.conf import settings

#: Répertoires gérés par les ``upload_to`` des FileField (cf. ADR-001).
MANAGED_SUBDIRS = ("rag_docs", "corpus_docs")

# --- Statuts (stables — consommés par la tâche, la supervision et les tests) ---
STATUS_OK = "ok"
STATUS_MISSING = "missing_file_detected"
STATUS_ORPHAN = "orphan_file_detected"
STATUS_FAILED = "audit_failed"

#: Gravité croissante. Un rapport qui cumule plusieurs anomalies porte la PIRE :
#: une référence sans fichier (document devenu inconsultable) prime sur un
#: orphelin (espace disque gaspillé).
_SEVERITY = {STATUS_OK: 0, STATUS_ORPHAN: 1, STATUS_MISSING: 2, STATUS_FAILED: 3}

#: Clés purement numériques du rapport — sûres pour un journal centralisé.
SUMMARY_KEYS = (
    "status", "referenced_total", "referenced_ok", "missing_file",
    "orphan_file", "files_on_disk", "bytes_on_disk", "media_root_exists",
)


def _worst(*statuses: str) -> str:
    return max(statuses, key=lambda s: _SEVERITY.get(s, 0))


def _referenced_names() -> tuple[set[str], list[str], Path]:
    """Noms référencés en base + ceux dont le fichier manque sur le disque."""
    from apps.corpus.models import DomainCorpusDocument
    from apps.rag_core.models import Document

    media_root = Path(settings.MEDIA_ROOT)
    referenced: set[str] = set()
    missing: list[str] = []
    for model in (Document, DomainCorpusDocument):
        qs = model.objects.exclude(storage_file="").exclude(storage_file__isnull=True)
        # `.iterator()` : on ne charge jamais la table entière en mémoire.
        for name in qs.values_list("storage_file", flat=True).iterator():
            if not name:
                continue
            referenced.add(name)
            if not (media_root / name).is_file():
                missing.append(name)
    # Artefacts de crop (CV-3C) : `rag_docs/<space>/<doc>/vision/<version>/crops/<sha>.png`,
    # référencés par `Crop.storage_key` (plusieurs crops peuvent partager un fichier).
    from apps.rag_core.models import Crop

    crop_keys = (Crop.objects.exclude(storage_key__isnull=True)
                 .values_list("storage_key", flat=True).distinct().iterator())
    for name in crop_keys:
        if not name or name in referenced:
            continue
        referenced.add(name)
        if not (media_root / name).is_file():
            missing.append(name)
    return referenced, missing, media_root


def _files_on_disk(media_root: Path) -> tuple[set[str], int]:
    """Fichiers présents sous les répertoires gérés + volume total."""
    on_disk: set[str] = set()
    total_bytes = 0
    for sub in MANAGED_SUBDIRS:
        base = media_root / sub
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            on_disk.add(str(p.relative_to(media_root)))
            try:
                # `stat` seulement : le contenu des fichiers n'est JAMAIS lu.
                total_bytes += p.stat().st_size
            except OSError:
                pass
    return on_disk, total_bytes


def collect(*, missing_examples: int = 0, orphan_examples: int = 0) -> dict[str, Any]:
    """Compare références en base et fichiers présents. Ne modifie rien.

    ``missing_examples`` / ``orphan_examples`` : nombre d'exemples de noms à
    joindre au rapport (0 = aucun). Réservé à l'usage OPÉRATEUR — ne jamais les
    demander depuis une tâche planifiée, ces noms partiraient dans les journaux.
    """
    referenced, missing, media_root = _referenced_names()
    on_disk, total_bytes = _files_on_disk(media_root)

    orphans = sorted(on_disk - referenced)
    media_root_exists = media_root.is_dir()

    status = STATUS_OK
    if not media_root_exists:
        # MEDIA_ROOT introuvable : on ne conclut PAS « aucun problème ». Un audit
        # qui ne peut rien lire n'est pas un audit vert.
        status = STATUS_FAILED
    if missing:
        status = _worst(status, STATUS_MISSING)
    if orphans:
        status = _worst(status, STATUS_ORPHAN)

    report: dict[str, Any] = {
        "status": status,
        "media_root": str(media_root),
        "media_root_exists": media_root_exists,
        "referenced_total": len(referenced),
        "referenced_ok": len(referenced) - len(missing),
        "missing_file": len(missing),
        "orphan_file": len(orphans),
        "files_on_disk": len(on_disk),
        "bytes_on_disk": total_bytes,
    }
    if missing_examples:
        report["missing_examples"] = missing[:missing_examples]
        report["missing_truncated"] = len(missing) > missing_examples
    if orphan_examples:
        report["orphan_examples"] = orphans[:orphan_examples]
        report["orphan_truncated"] = len(orphans) > orphan_examples
    return report


def public_summary(report: dict[str, Any]) -> dict[str, Any]:
    """Vue SANS chemin ni nom de fichier — la seule qui parte dans les journaux."""
    return {k: report[k] for k in SUMMARY_KEYS if k in report}


def is_clean(report: dict[str, Any]) -> bool:
    return report.get("status") == STATUS_OK
