"""Construction du paquet d'incident.

Le paquet est une ARCHIVE AUTOPORTANTE : il contient tout ce qu'il faut pour
rejouer un incident en développement, et rien d'autre. Sa structure est figée et
versionnée, parce qu'un format d'échange qui dérive silencieusement rend
inutilisables les incidents déjà exportés.

    incident/
    ├── manifest.json              provenance, intégrité, autorisation
    ├── request.json               question, langue, contexte conversationnel
    ├── effective_config.json      seuils, budgets, flags, modèles
    ├── retrieval_candidates.json  candidats, rangs, scores, raisons d'exclusion
    ├── selected_chunks.json       sources retenues (contrat v3)
    ├── generation.json            provider, modèle, politique, [prompt]
    ├── source_fragments/          niveau ≥ 2 uniquement
    └── expected_behavior.md       ce qui aurait dû se produire

ORDRE DES OPÉRATIONS — il n'est pas négociable :

    autorisation -> rédaction -> détection de secrets -> intégrité -> chiffrement

Chiffrer avant de rédiger produirait un paquet sûr en transit et sale à
l'arrivée. Signer avant de chiffrer garantit que la signature porte sur le
contenu, pas sur son enveloppe.
"""
from __future__ import annotations

import io
import logging
import zipfile
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.utils import timezone

from apps.feedback.export import crypto, levels, redaction

logger = logging.getLogger(__name__)

#: Version du FORMAT de paquet. Un import refuse une majeure inconnue.
PACKAGE_SCHEMA_VERSION = 1

MANIFEST = "manifest.json"


class ExportBlocked(Exception):
    """Export interrompu — secret détecté, chiffrement impossible, état invalide."""


def _ttl_days() -> int:
    try:
        return max(1, int(getattr(settings, "FEEDBACK_INCIDENT_EXPORT_TTL_DAYS", 30)))
    except (TypeError, ValueError):
        return 30


def _source_environment() -> str:
    return str(getattr(settings, "FEEDBACK_INCIDENT_SOURCE_ENVIRONMENT", "dev") or "dev")


def build_expected_behavior(feedback) -> str:
    """Le comportement ATTENDU, rédigé à partir du signalement.

    Ce fichier n'est pas décoratif : c'est lui qui devient l'assertion de la
    fixture de non-régression. Un incident sans comportement attendu ne peut
    pas se promouvoir en test.
    """
    lines = [
        "# Comportement attendu",
        "",
        f"- Incident : `{feedback.id}`",
        f"- Catégorie : `{feedback.kind or 'non précisée'}`",
        f"- Politique observée : `{feedback.policy_hist or 'inconnue'}`",
        f"- Modèle : `{feedback.llm_model_hist or 'inconnu'}`",
        "",
        "## Attendu",
        "",
    ]
    if feedback.expected_term:
        lines.append(
            f"- La réponse doit exploiter le terme **{feedback.expected_term}**, "
            "présent dans le corpus."
        )
    if feedback.expected_document:
        lines.append(
            f"- Le document `{redaction.pseudonymize_filename(feedback.expected_document)}` "
            "doit figurer parmi les sources retenues."
        )
    if feedback.policy_hist == "no_answer":
        lines.append("- La réponse ne doit PAS être une abstention.")
    if feedback.kind == "wrong_language":
        lines.append("- La réponse doit être rendue dans la langue attendue.")
    if len(lines) == 8:  # aucune attente exploitable n'a été ajoutée
        lines.append(
            "- _À compléter par le qualifieur : le signalement ne portait aucun "
            "élément objectif exploitable._"
        )
    lines += [
        "",
        "## Non attendu",
        "",
        "- Aucune source hors sujet parmi les extraits retenus.",
        "",
        "> Rédigé automatiquement à partir du signalement ; à relire avant "
        "promotion en fixture de non-régression.",
    ]
    return "\n".join(lines) + "\n"


def _collect_source_fragments(snapshot, *, level: levels.ExportLevel) -> dict[str, bytes]:
    """Fichiers source, niveau 3 uniquement.

    Les noms sont pseudonymisés : le contenu est déjà l'exception qu'on
    autorise, l'intitulé du fichier n'a pas à s'y ajouter.
    """
    if not level.includes_source_file:
        return {}

    out: dict[str, bytes] = {}
    try:
        from apps.rag_core.models import Document

        doc_ids = {
            str(s.get("document_id"))
            for s in (snapshot.selected_chunks.get("sources") or [])
            if isinstance(s, dict) and s.get("document_id")
        }
        for doc in Document.objects.filter(id__in=doc_ids):
            f = getattr(doc, "file", None)
            if not f:
                continue
            try:
                with f.open("rb") as fh:
                    blob = fh.read()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "feedback_export_source_unreadable document_id=%s error_code=%s",
                    doc.id,
                    type(exc).__name__,
                )
                continue
            alias = redaction.pseudonymize_filename(getattr(doc, "filename", "") or str(doc.id))
            out[f"source_fragments/{alias}"] = blob
    except Exception as exc:  # noqa: BLE001
        logger.warning("feedback_export_sources_failed error_code=%s", type(exc).__name__)
    return out


def build_files(
    *, feedback, snapshot, level: levels.ExportLevel
) -> tuple[dict[str, bytes], dict[str, Any]]:
    """Construit le contenu du paquet, rédigé selon le niveau.

    Renvoie ``(fichiers, rapport_de_rédaction)``.
    """
    include_text = level.includes_text

    request_payload = redaction.redact_payload(
        dict(snapshot.request_payload or {}), include_text=include_text
    )
    effective_config = dict(snapshot.effective_config or {})  # que de la configuration
    retrieval = redaction.redact_payload(
        dict(snapshot.retrieval_trace or {}), include_text=include_text
    )
    selected = redaction.redact_payload(
        dict(snapshot.selected_chunks or {}), include_text=include_text
    )
    generation = redaction.redact_payload(
        dict(snapshot.generation_metadata or {}), include_text=include_text
    )

    logical = {
        "request": request_payload,
        "effective_config": effective_config,
        "retrieval_candidates": retrieval,
        "selected_chunks": selected,
        "generation": generation,
    }
    status, secrets = redaction.evaluate(logical)

    files: dict[str, bytes] = {
        "request.json": crypto.dumps(request_payload),
        "effective_config.json": crypto.dumps(effective_config),
        "retrieval_candidates.json": crypto.dumps(retrieval),
        "selected_chunks.json": crypto.dumps(selected),
        "generation.json": crypto.dumps(generation),
        "expected_behavior.md": build_expected_behavior(feedback).encode("utf-8"),
    }
    files.update(_collect_source_fragments(snapshot, level=level))

    return files, {"redaction_status": status, "secrets_detected": secrets}


def build_package(
    *,
    feedback,
    snapshot,
    level_value: int,
    user,
    justification: str = "",
    approval_event_id: str = "",
    allow_secrets_override: bool = False,
) -> dict[str, Any]:
    """Produit le paquet complet. Lève ``ExportRefused`` / ``ExportBlocked``.

    Renvoie ``{filename, payload, manifest, encrypted}``. La fonction ne
    persiste rien et n'envoie rien : c'est l'appelant, déjà authentifié et
    audité, qui décide quoi faire de l'archive.
    """
    if not bool(getattr(settings, "FEEDBACK_INCIDENT_EXPORT_ENABLED", False)):
        raise levels.ExportRefused(
            "L'export d'incident est désactivé (FEEDBACK_INCIDENT_EXPORT_ENABLED=0)."
        )

    level = levels.check_authorisation(
        user=user, level_value=level_value, justification=justification
    )

    if snapshot is None:
        raise ExportBlocked(
            "Aucun snapshot diagnostique : il n'y a rien à exporter. "
            "Le matériel a peut-être expiré (la rétention du snapshot est bornée)."
        )

    files, report = build_files(feedback=feedback, snapshot=snapshot, level=level)

    # --- Secrets : on BLOQUE, on ne nettoie pas -------------------------
    redaction_status = report["redaction_status"]
    if redaction_status == redaction.STATUS_BLOCKED:
        if not allow_secrets_override:
            raise ExportBlocked(
                "Export bloqué : motifs de secret détectés "
                f"({', '.join(report['secrets_detected'])}). "
                "Résoudre la cause, ou approuver manuellement en connaissance de cause."
            )
        redaction_status = redaction.STATUS_MANUALLY_APPROVED

    content_sha256 = crypto.canonical_content_sha256(files)
    now = timezone.now()

    manifest = {
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "retrieval_trace_schema_version": int(snapshot.retrieval_trace_version or 1),
        "sources_schema_version": int(snapshot.sources_schema_version or 3),
        "incident_id": str(feedback.id),
        "source_environment": _source_environment(),
        "git_sha": feedback.app_git_sha or "",
        "exported_at": now.isoformat(),
        "expires_at": (now + timedelta(days=_ttl_days())).isoformat(),
        "contains_production_content": bool(snapshot.contains_production_content and level.includes_text),
        "redaction_status": redaction_status,
        "encryption_status": "none",
        "source_model": feedback.llm_model_hist or "",
        "embedding_model": str((snapshot.effective_config or {}).get("embedding_model") or ""),
        "content_sha256": content_sha256,
        "file_sha256": crypto.file_digests(files),
        "signature": crypto.sign(content_sha256),
        "approved_by": getattr(user, "username", "") or "",
        "approval_event_id": str(approval_event_id or ""),
        "export_level": level.value,
        "export_level_label": level.label,
        "justification": (justification or "")[:500],
        "capture_mode": snapshot.capture_mode,
        "capture_is_exact": snapshot.is_exact_capture,
        "replay_modes_supported": _replay_modes(snapshot, level),
        "truncated": bool(snapshot.truncated),
        "truncation_reason": snapshot.truncation_reason or "",
    }

    must_encrypt = manifest["contains_production_content"]
    if must_encrypt and not crypto.is_configured():
        raise ExportBlocked(
            "Ce paquet contient du contenu de production et aucune clé de "
            "chiffrement n'est configurée : l'export est refusé plutôt que produit "
            "en clair."
        )
    if must_encrypt:
        manifest["encryption_status"] = "fernet"

    files[MANIFEST] = crypto.dumps(manifest)
    archive = _zip(files)

    if must_encrypt:
        payload = crypto.encrypt(archive)
        filename = f"incident-{feedback.id}.zip.enc"
    else:
        payload = archive
        filename = f"incident-{feedback.id}.zip"

    logger.info(
        "feedback_exported feedback_id=%s incident_id=%s level=%s encrypted=%s "
        "redaction_status=%s snapshot_size=%s capture_mode=%s",
        feedback.id,
        feedback.id,
        level.value,
        must_encrypt,
        redaction_status,
        len(payload),
        snapshot.capture_mode,
    )

    return {
        "filename": filename,
        "payload": payload,
        "manifest": manifest,
        "encrypted": must_encrypt,
    }


def _replay_modes(snapshot, level: levels.ExportLevel) -> list[str]:
    """Modes de replay RÉELLEMENT possibles avec ce paquet.

    ⚠️ ÉTAT RÉEL DE ``prompt-only`` — le prompt final n'est **jamais** présent
    dans un snapshot issu d'une capture réelle. Il est construit dans
    ``query_rag``, renvoyé à l'appelant, et **personne ne le persiste** : le
    tampon exclut délibérément tout texte, et la tâche de promotion appelle
    ``build_generation_metadata(log, prompt=None)``. ``generation_metadata``
    reste donc en ``hash_only``, y compris avec
    ``RAG_FEEDBACK_DIAGNOSTIC_CAPTURE_PROMPT=1`` — ce drapeau n'ouvre en
    pratique que la capture du contexte conversationnel.

    Conséquence assumée ici : ce test sur ``generation.prompt`` n'est jamais
    vrai en exploitation, et ``prompt-only`` n'est donc jamais annoncé. C'est le
    comportement VOULU — annoncer un mode inexécutable ferait perdre du temps en
    aval. La condition est conservée plutôt que supprimée : le jour où un lot de
    conception dédié rendra le prompt disponible, elle deviendra exacte sans
    changement. Combler l'écart en stockant le prompt dans Redis est EXCLU.

    Les modes ``retrieval`` et ``full-pipeline`` exigent
    des extraits, donc au minimum le niveau 2.
    """
    modes: list[str] = []
    generation = snapshot.generation_metadata or {}
    if generation.get("prompt") and level.includes_text:
        modes.append("prompt-only")
    if level.includes_text and (snapshot.selected_chunks or {}).get("sources"):
        modes += ["retrieval", "full-pipeline"]
    return modes


def _zip(files: dict[str, bytes]) -> bytes:
    """Archive DÉTERMINISTE : ordre trié, horodatage figé.

    Deux exports du même incident doivent produire des octets identiques —
    sinon l'empreinte du paquet ne prouve plus rien.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(files):
            info = zipfile.ZipInfo(f"incident/{path}", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, files[path])
    return buf.getvalue()


def read_package(payload: bytes) -> dict[str, bytes]:
    """Relit un paquet (déchiffre si nécessaire) et renvoie ses fichiers."""
    blob = payload
    if not payload[:2] == b"PK":
        blob = crypto.decrypt(payload)

    files: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        for name in zf.namelist():
            key = name[len("incident/") :] if name.startswith("incident/") else name
            if key:
                files[key] = zf.read(name)
    return files
