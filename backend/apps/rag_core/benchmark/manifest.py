"""Manifeste de figement d'un corpus RÉEL (benchmark terrain).

Un corpus de production/dev n'est pas versionnable : les documents sont clients et
peuvent changer. On fige donc son **empreinte**, pas son contenu — un manifeste qui
permet de détecter toute dérive (document ajouté/supprimé/modifié, re-chunking,
changement de provider) et de **refuser** une comparaison devenue invalide.

⚠️ Confidentialité : le manifeste ne contient **aucun contenu documentaire**. Le
checksum est calculé sur le TEXTE DES CHUNKS (ce qui est réellement indexé et
interrogé) — un document ré-extrait/re-chunké différemment change de checksum même si
le fichier source est identique, ce qui est exactement le signal recherché.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

MANIFEST_VERSION = "1"


def _sha256(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update((p or "").encode("utf-8"))
        h.update(b"\x1f")  # séparateur : évite les collisions par concaténation
    return h.hexdigest()


def document_checksum(filename: str, chunk_texts: list[str]) -> str:
    """Empreinte d'un document = nom + texte de ses chunks, dans l'ordre d'index."""
    return _sha256(filename, *chunk_texts)


def manifest_checksum(documents: list[dict[str, Any]]) -> str:
    """Empreinte globale du corpus : dérivée des checksums documentaires triés."""
    return _sha256(*sorted(d["checksum"] for d in documents))


def build_manifest(
    *,
    rag_space_id: str,
    rag_space_name: str,
    embedding_provider: str,
    embedding_model: str,
    embedding_dimension: int,
    chunking_version: str | None,
    documents: list[dict[str, Any]],
    created_at: str,
) -> dict[str, Any]:
    docs = sorted(documents, key=lambda d: str(d["document_id"]))
    return {
        "manifest_version": MANIFEST_VERSION,
        "rag_space_id": str(rag_space_id),
        "rag_space_name": rag_space_name,
        "created_at": created_at,
        "document_count": len(docs),
        "chunk_count": sum(int(d.get("chunk_count") or 0) for d in docs),
        "embedding_provider": embedding_provider,
        "embedding_model": embedding_model,
        "embedding_dimension": int(embedding_dimension),
        "chunking_version": chunking_version,
        "manifest_checksum": manifest_checksum(docs),
        "documents": docs,
    }


def dump_manifest(manifest: dict[str, Any]) -> str:
    return json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


# --- Détection de dérive -------------------------------------------------------
DRIFT_NONE = "none"


def diff_manifests(reference: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Compare deux manifestes. Renvoie un rapport de dérive structuré.

    ``compatible=False`` dès qu'un élément invalidant change : documents
    ajoutés/supprimés/modifiés, comptes de chunks, profil d'embeddings, chunking.
    """
    reasons: list[str] = []

    for key in ("rag_space_id", "embedding_provider", "embedding_model",
                "embedding_dimension", "chunking_version"):
        if reference.get(key) != current.get(key):
            reasons.append(
                f"{key}: {reference.get(key)!r} -> {current.get(key)!r}"
            )

    ref_docs = {str(d["document_id"]): d for d in reference.get("documents") or []}
    cur_docs = {str(d["document_id"]): d for d in current.get("documents") or []}

    added = sorted(set(cur_docs) - set(ref_docs))
    removed = sorted(set(ref_docs) - set(cur_docs))
    changed: list[str] = []
    chunk_count_changed: list[str] = []
    for doc_id in sorted(set(ref_docs) & set(cur_docs)):
        r, c = ref_docs[doc_id], cur_docs[doc_id]
        if r.get("checksum") != c.get("checksum"):
            changed.append(doc_id)
        elif int(r.get("chunk_count") or 0) != int(c.get("chunk_count") or 0):
            chunk_count_changed.append(doc_id)

    if added:
        reasons.append(f"{len(added)} document(s) ajouté(s)")
    if removed:
        reasons.append(f"{len(removed)} document(s) supprimé(s)")
    if changed:
        reasons.append(f"{len(changed)} document(s) modifié(s) (checksum)")
    if chunk_count_changed:
        reasons.append(f"{len(chunk_count_changed)} document(s) re-chunké(s)")

    return {
        "compatible": not reasons,
        "reasons": reasons,
        "added": added,
        "removed": removed,
        "changed": changed,
        "chunk_count_changed": chunk_count_changed,
        "reference_checksum": reference.get("manifest_checksum"),
        "current_checksum": current.get("manifest_checksum"),
    }
