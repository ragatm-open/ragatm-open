# backend/apps/rag_core/services/ingest/extraction/zip_security.py
from __future__ import annotations

"""
Garde ZIP partagée par les formats Office « OOXML » (conteneurs ZIP non fiables).

Extraite telle quelle de l'extracteur DOCX (`_guard_zip_bomb`) pour être réutilisable
par les futurs extracteurs Office (Excel, PowerPoint) SANS dupliquer la logique.

Le comportement DOCX est préservé à l'identique : mêmes seuils (lus par l'appelant),
mêmes codes d'erreur (`<prefix>_too_many_entries`, `<prefix>_uncompressed_too_large`,
`<prefix>_compression_ratio`) et mêmes messages. Seuls le préfixe de code et le
libellé de format sont paramétrés.

Ce module ne DÉCOMPRESSE rien : il ne lit que le répertoire central du ZIP
(`infolist()`), donc il est sûr d'être appelé AVANT toute lecture de contenu.
"""

import zipfile

from .base import ExtractionLimitExceeded

# Une entrée dont la taille décompressée dépasse ce seuil est soumise au contrôle
# de ratio (en dessous, un ratio élevé n'est pas significatif : un petit XML très
# répétitif se compresse naturellement très bien).
RATIO_CHECK_MIN_SIZE_BYTES = 1_000_000


def guard_zip_archive(
    zf: zipfile.ZipFile,
    *,
    max_entries: int,
    max_uncompressed_bytes: int,
    max_compression_ratio: int,
    code_prefix: str,
    label: str,
) -> None:
    """Refuse une archive Office piégée (zip bomb) AVANT toute décompression.

    Contrôles :
    - nombre d'entrées ;
    - taille décompressée cumulée ;
    - ratio de compression anormal (sur les entrées significatives) ;
    - tailles incohérentes déclarées dans le répertoire central.

    Lève ``ExtractionLimitExceeded`` (code ``<code_prefix>_*``). Ne décompresse rien.
    """
    infos = zf.infolist()

    if len(infos) > max_entries:
        raise ExtractionLimitExceeded(
            f"{label} : trop d'entrées dans l'archive ({len(infos)} > {max_entries}).",
            code=f"{code_prefix}_too_many_entries",
        )

    total = 0
    for info in infos:
        # Le répertoire central est fourni par le fichier : il n'est pas fiable.
        # Une taille négative/incohérente est un signe de manipulation.
        if info.file_size < 0 or info.compress_size < 0:
            raise ExtractionLimitExceeded(
                f"{label} : entrée d'archive incohérente — archive potentiellement piégée.",
                code=f"{code_prefix}_invalid_entry",
            )

        total += info.file_size
        if total > max_uncompressed_bytes:
            raise ExtractionLimitExceeded(
                f"{label} : taille décompressée excessive (> {max_uncompressed_bytes} octets) — "
                "archive potentiellement piégée (zip bomb).",
                code=f"{code_prefix}_uncompressed_too_large",
            )

        if info.compress_size > 0 and info.file_size > RATIO_CHECK_MIN_SIZE_BYTES:
            ratio = info.file_size / max(1, info.compress_size)
            if ratio > max_compression_ratio:
                raise ExtractionLimitExceeded(
                    f"{label} : ratio de compression suspect ({ratio:.0f}x > {max_compression_ratio}x) — "
                    "archive potentiellement piégée (zip bomb).",
                    code=f"{code_prefix}_compression_ratio",
                )


def read_entry_strict(
    zf: zipfile.ZipFile, name: str, *, max_bytes: int, code_prefix: str, label: str
) -> bytes:
    """Lit une entrée en bornant sa taille **pendant** la décompression.

    Différence essentielle avec `read_bounded_entry` : celui-ci sert les chemins de
    DÉTECTION et rend `b""` en silence sur échec. Ici on est sur un chemin
    d'EXTRACTION — un dépassement doit se dire, pas se taire.

    Différence essentielle avec `guard_zip_archive` : cette garde-là ne peut vérifier
    que les tailles **déclarées** dans le répertoire central. Or `file_size` est écrit
    par le fichier lui-même : une archive qui sous-déclare une entrée passe la garde,
    puis `zf.read()` décompresse la taille RÉELLE, sans borne. On lit donc par
    tranches et on s'arrête dès que le flux dépasse la limite — la borne porte sur ce
    qui sort du décompresseur, pas sur ce que l'archive prétend.
    """
    try:
        info = zf.getinfo(name)
    except KeyError:
        raise
    except Exception as exc:
        raise ExtractionLimitExceeded(
            f"{label} : entrée d'archive illisible.",
            code=f"{code_prefix}_invalid_entry",
        ) from exc

    if info.file_size < 0 or info.file_size > max_bytes:
        raise ExtractionLimitExceeded(
            f"{label} : entrée d'archive trop volumineuse.",
            code=f"{code_prefix}_entry_too_large",
        )

    chunks: list[bytes] = []
    total = 0
    with zf.open(name, "r") as fh:
        while True:
            block = fh.read(65_536)
            if not block:
                break
            total += len(block)
            if total > max_bytes:
                # La taille déclarée mentait : on coupe au lieu de matérialiser.
                raise ExtractionLimitExceeded(
                    f"{label} : entrée d'archive trop volumineuse.",
                    code=f"{code_prefix}_entry_too_large",
                )
            chunks.append(block)
    return b"".join(chunks)


def read_bounded_entry(zf: zipfile.ZipFile, name: str, *, max_bytes: int) -> bytes:
    """Lit une entrée d'archive en bornant sa taille DÉCLARÉE avant décompression.

    Renvoie ``b""`` si l'entrée est absente, trop grosse ou illisible. Ne lève jamais :
    destiné aux chemins de *détection* (qui doivent rester totaux), pas d'extraction.
    """
    try:
        info = zf.getinfo(name)
    except KeyError:
        return b""
    except Exception:
        return b""

    if info.file_size < 0 or info.file_size > max_bytes:
        return b""

    try:
        return zf.read(name)[:max_bytes]
    except Exception:
        return b""
