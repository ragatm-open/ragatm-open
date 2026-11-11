# backend/apps/rag_core/services/ingest/import_paths.py
from __future__ import annotations

"""
Normalisation et validation des métadonnées de chemin issues d'un import de
DOSSIER (webkitdirectory côté frontend).

IMPORTANT — sécurité : ``relative_path`` / ``source_folder`` sont des données
CLIENT. Elles sont conservées UNIQUEMENT comme métadonnées (Document.meta) et ne
doivent JAMAIS servir à construire un chemin de stockage disque (le stockage
utilise déjà le basename sanitisé via ``document_upload_to``). On rejette
néanmoins toute valeur dangereuse (traversal, chemin absolu, octets de contrôle)
pour ne jamais persister de donnée piégée.
"""

import unicodedata

# Limites de longueur (métadonnée uniquement — bornées pour éviter tout abus).
MAX_RELATIVE_PATH_LEN = 1024
MAX_SOURCE_FOLDER_LEN = 255


class UnsafeImportPath(Exception):
    """Un chemin d'import client est invalide/dangereux (traversal, absolu…)."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(detail)


def _clean_component_string(raw: str) -> str:
    """Normalise une chaîne de chemin client : Unicode NFC, séparateurs unifiés,
    suppression des octets de contrôle. NE valide PAS encore la sûreté."""
    s = unicodedata.normalize("NFC", str(raw or "")).strip()
    # Séparateurs Windows -> POSIX (une seule convention interne).
    s = s.replace("\\", "/")
    # Retire les octets de contrôle (dont NUL) — jamais dans un nom légitime.
    s = "".join(ch for ch in s if ch == "\t" or (ord(ch) >= 32 and ord(ch) != 127)).strip()
    return s


def _reject_if_dangerous(value: str, *, field: str) -> None:
    if value.startswith("/"):
        raise UnsafeImportPath("import_path_absolute", f"{field} ne doit pas être un chemin absolu.")
    # Lecteur Windows type "C:/..." après normalisation des backslashes.
    if len(value) >= 2 and value[1] == ":" and value[0].isalpha():
        raise UnsafeImportPath("import_path_absolute", f"{field} ne doit pas contenir de lettre de lecteur.")
    segments = value.split("/")
    for seg in segments:
        if seg in ("..",):
            raise UnsafeImportPath("import_path_traversal", f"{field} ne doit pas contenir '..'.")


def normalize_import_metadata(
    *, source_folder: object, relative_path: object
) -> tuple[str, str]:
    """Valide + normalise ``(source_folder, relative_path)``.

    - Optionnels : si les deux sont vides/absents => renvoie ``("", "")`` (upload
      unitaire classique, aucune métadonnée d'import).
    - Rejette (``UnsafeImportPath``) : chemin absolu, lettre de lecteur, segment
      ``..``, octets de contrôle, dépassement de longueur.
    - Renvoie des chaînes normalisées (NFC, séparateurs POSIX, sans ``/`` initial
      ni segments vides superflus). ``source_folder`` est réduit à un seul niveau
      (pas de séparateur).
    """
    sf = _clean_component_string(source_folder if source_folder is not None else "")
    rp = _clean_component_string(relative_path if relative_path is not None else "")

    if not sf and not rp:
        return "", ""

    if len(rp) > MAX_RELATIVE_PATH_LEN:
        raise UnsafeImportPath("import_path_too_long", "relative_path trop long.")
    if len(sf) > MAX_SOURCE_FOLDER_LEN:
        raise UnsafeImportPath("import_path_too_long", "source_folder trop long.")

    if rp:
        _reject_if_dangerous(rp, field="relative_path")
        # Retire un éventuel '/' initial et compacte les segments vides.
        rp = "/".join(seg for seg in rp.split("/") if seg != "")
        if not rp:
            raise UnsafeImportPath("import_path_empty", "relative_path vide après normalisation.")

    if sf:
        _reject_if_dangerous(sf, field="source_folder")
        # source_folder = un seul niveau : on garde le premier segment non vide.
        first = next((seg for seg in sf.split("/") if seg != ""), "")
        if not first:
            raise UnsafeImportPath("import_path_empty", "source_folder vide après normalisation.")
        sf = first

    return sf, rp
