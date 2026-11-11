"""
Ingestion documentaire d'un dessin AutoCAD DWG.

ARCHITECTURE — le DWG n'a PAS de parseur sémantique propre dans RagATM :

    octets DWG (source durable)
        ↓  conversion LOCALE, hors ligne, en répertoire temporaire jetable
    GNU LibreDWG 0.14 `dwg2dxf` (exécutable séparé, sous-processus, jamais de shell)
        ↓  DXF ASCII (artefact TEMPORAIRE, jamais persisté, jamais un second Document)
    `DxfExtractor` déjà qualifié (R1 fins de ligne, R2 entités exotiques)
        ↓  `StructuredUnit` du socle 1A, provenance ré-étiquetée `dwg`
    chunker commun, embeddings ordinaires, publication 1F

Aucune sémantique CAO n'est réimplémentée ici : textes, MTEXT, blocs, ATTRIB, cotes,
calques, layouts, unités et XREF viennent du `DxfExtractor`. Cet extracteur ne fait que
convertir, valider la conversion et adapter la provenance.

SÉCURITÉ : `subprocess` avec liste d'arguments et `shell=False` (jamais `os.system`, ni
`/bin/sh -c`) ; chemin de l'exécutable issu de la configuration serveur, jamais d'une
donnée utilisateur ; noms de fichiers internes FIXES (`input.dwg` / `output.dxf`) — le nom
déposé par l'utilisateur n'atteint jamais le système de fichiers ni la ligne de commande ;
répertoire temporaire 0700 généré par le serveur, fichiers 0600, détruit dans tous les cas
(succès, échec, délai dépassé) ; environnement réduit (aucun secret) ; session/process
group dédié, tué en entier au délai ; sorties du convertisseur redirigées vers des fichiers
(jamais accumulées en mémoire), seuls des COMPTES sont conservés ; taille du DXF
intermédiaire vérifiée par `stat` AVANT toute lecture. Les XREF restent déclaratives :
le répertoire temporaire ne contient que le dessin courant.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import subprocess  # noqa: S404 - exécutable fixe, liste d'arguments, jamais de shell
import tempfile
import time
from typing import Optional

from . import config
from .base import (
    CorruptedFile,
    ExtractionComplexityExceeded,
    ExtractionContext,
    ExtractionResult,
    RejectedFile,
    StructuredUnit,
    build_locator,
)
from .dwg_signature import DWG_MIME, dwg_version, is_supported_version, release_label
from .dxf_extractor import DxfExtractor
from .dxf_extractor import parser_available as dxf_parser_available

logger = logging.getLogger(__name__)

FORMAT = "dwg"
CONVERTER = "libredwg"
#: Version EXACTE qualifiée. Toute autre version est un défaut de
#: déploiement : le writer DXF de 0.13.3 casse les MTEXT longs (documents entiers perdus).
REQUIRED_CONVERTER_VERSION = "0.14"
INTERMEDIATE_FORMAT = "dxf"
PARSER = "ezdxf"

DEPENDENCY_CODE = "dwg_dependency_unavailable"
DISABLED_CODE = "dwg_ingestion_disabled"
MALFORMED_CODE = "dwg_malformed"
VERSION_CODE = "dwg_unsupported_version"
CONVERSION_CODE = "dwg_conversion_failed"
TIMEOUT_CODE = "dwg_conversion_timeout"
COMPLEXITY_CODE = "dwg_complexity_limit"

DEPENDENCY_MESSAGE = (
    "L'ingestion DWG est activée mais le convertisseur n'est pas disponible sur cet "
    "environnement. Contactez un administrateur."
)
MALFORMED_MESSAGE = "Le fichier DWG est invalide ou endommagé et n'a pas pu être lu."
VERSION_MESSAGE = "Cette version de DWG n'est pas prise en charge."
CONVERSION_MESSAGE = "Le dessin DWG n'a pas pu être converti pour extraction."
TIMEOUT_MESSAGE = "La conversion du dessin DWG a dépassé le temps alloué."
COMPLEXITY_MESSAGE = "Le dessin DWG est trop volumineux pour être analysé."

INPUT_NAME = "input.dwg"
OUTPUT_NAME = "output.dxf"
TEMP_PREFIX = "ragatm-dwg-"
_STDOUT_NAME = "converter.out"
_STDERR_NAME = "converter.err"
_PROBE_TIMEOUT_SECONDS = 10
_VERSION_RE = re.compile(r"(\d+\.\d+(?:\.\d+)*)")
#: Marqueurs de diagnostic du convertisseur (comptés, jamais stockés tels quels).
_READ_ERROR_RE = re.compile(rb"READ ERROR|Invalid or unimplemented|not a DWG|Not a DWG")


# ---------------------------------------------------------------------------
# Disponibilité du convertisseur (sonde mise en cache)
# ---------------------------------------------------------------------------
_probe_cache: dict[tuple, Optional[str]] = {}


def converter_path() -> str:
    return str(config.dwg_converter_path())


def _probe_key(path: str) -> Optional[tuple]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    if not os.path.isfile(path) or not os.access(path, os.X_OK):
        return None
    return (os.path.realpath(path), st.st_size, int(st.st_mtime))


def _run_version_probe(path: str) -> Optional[str]:
    """`dwg2dxf --version` → « 0.14 », ou None. Sous-processus court, sans shell."""
    try:
        proc = subprocess.run(  # noqa: S603 - chemin de configuration, liste d'arguments
            [path, "--version"], capture_output=True, timeout=_PROBE_TIMEOUT_SECONDS,
            check=False, shell=False, env=_converter_env(None),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = (proc.stdout or b"") + b" " + (proc.stderr or b"")
    m = _VERSION_RE.search(out[:512].decode("utf-8", errors="replace"))
    return m.group(1) if m else None


def _probe_cache_clear() -> None:
    """Réinitialise la sonde (tests, rechargement de configuration)."""
    _probe_cache.clear()


def converter_version() -> Optional[str]:
    """Version du convertisseur déployé, sondée UNE fois par binaire (chemin+taille+mtime)."""
    path = converter_path()
    key = _probe_key(path)
    if key is None:
        return None
    if key not in _probe_cache:
        _probe_cache[key] = _run_version_probe(path)
    return _probe_cache[key]


def converter_available() -> bool:
    """Le convertisseur EXACT (0.14) est-il exécutable ici ? Aucun import, aucune lecture."""
    return converter_version() == REQUIRED_CONVERTER_VERSION


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------
def _converter_env(workdir: Optional[str]) -> dict:
    """Environnement MINIMAL : aucun secret d'application n'est transmis au convertisseur."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LC_ALL": "C",
        "LANG": "C",
    }
    if workdir:
        env["TMPDIR"] = workdir
        env["HOME"] = workdir
    return env


def _count_diagnostics(path: str) -> int:
    """Nombre de lignes de diagnostic — lecture EN FLUX, jamais en mémoire."""
    count = 0
    try:
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                count += chunk.count(b"\n")
    except OSError:
        return 0
    return count


def _tail(path: str, limit: int = 2048) -> bytes:
    """Derniers octets d'un fichier de diagnostic (journalisation bornée, jamais publiée)."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > limit:
                fh.seek(size - limit)
            return fh.read(limit)
    except OSError:
        return b""


def _kill_group(proc: subprocess.Popen) -> None:
    """Tue le groupe de processus entier : le convertisseur ET ses éventuels enfants."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover - le groupe est tué par SIGKILL
        pass


def _run_converter(path: str, workdir: str, timeout: float) -> tuple[int, int]:
    """Lance `dwg2dxf -y -o output.dxf input.dwg` dans `workdir`. → (code de sortie, diagnostics)."""
    out_path = os.path.join(workdir, _STDOUT_NAME)
    err_path = os.path.join(workdir, _STDERR_NAME)
    argv = [path, "-y", "-o", OUTPUT_NAME, INPUT_NAME]
    with open(out_path, "wb") as out, open(err_path, "wb") as err:
        try:
            proc = subprocess.Popen(  # noqa: S603 - chemin de configuration, liste d'arguments
                argv, cwd=workdir, stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                env=_converter_env(workdir), shell=False, start_new_session=True,
            )
        except OSError as exc:
            raise RejectedFile(DEPENDENCY_MESSAGE, code=DEPENDENCY_CODE,
                               meta={"converter": CONVERTER, "reason": type(exc).__name__}) from exc
        try:
            proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            raise CorruptedFile(TIMEOUT_MESSAGE, code=TIMEOUT_CODE,
                                meta={"converter": CONVERTER, "timeout_seconds": int(timeout)}) from None
    return int(proc.returncode), _count_diagnostics(err_path)


def _validate_output(workdir: str, max_bytes: int) -> int:
    """Existence, non-vacuité et taille du DXF intermédiaire — `stat` AVANT toute lecture."""
    out_path = os.path.join(workdir, OUTPUT_NAME)
    if not os.path.isfile(out_path):
        raise CorruptedFile(CONVERSION_MESSAGE, code=CONVERSION_CODE,
                            meta={"converter": CONVERTER, "reason": "no_output"})
    size = os.path.getsize(out_path)
    if size == 0:
        raise CorruptedFile(CONVERSION_MESSAGE, code=CONVERSION_CODE,
                            meta={"converter": CONVERTER, "reason": "empty_output"})
    if size > max_bytes:
        raise ExtractionComplexityExceeded(
            COMPLEXITY_MESSAGE, code=COMPLEXITY_CODE,
            meta={"converter": CONVERTER, "intermediate_dxf_bytes": size,
                  "intermediate_dxf_max_bytes": max_bytes},
        )
    return size


# ---------------------------------------------------------------------------
# Provenance : le DXF est un DÉTAIL D'IMPLÉMENTATION
# ---------------------------------------------------------------------------
#: Clés de `DxfExtractor` reprises telles quelles (sémantique CAO partagée).
_CARRIED = (
    "units", "units_name", "layouts", "layouts_count", "layers", "layers_count",
    "layers_off", "layers_frozen", "entity_count", "entity_counts", "blocks", "blocks_count",
    "xrefs_declared", "xrefs_count", "semantic_unit_count", "annotation_count",
    "block_unit_count", "unknown_entity_count", "unknown_entity_counts",
)


def _adapt_units(units: list) -> list:
    """Ré-étiquette la provenance en `dwg`. Le handle du DXF converti est ABANDONNÉ :
    LibreDWG renumérote les handles, il ne peut pas servir d'identité stable du DWG."""
    out: list[StructuredUnit] = []
    for u in units:
        meta = dict(u.meta or {})
        loc = dict(meta.get("locator") or {})
        meta["locator"] = build_locator(type=FORMAT, path=loc.get("path") or "", identifier=None)
        meta["structured_format"] = FORMAT
        out.append(StructuredUnit(text=u.text, meta=meta))
    return out


class DwgExtractor:
    """Octets DWG → `ExtractionResult(units=…)` via LibreDWG puis `DxfExtractor`."""

    name = "dwg"

    def can_handle(self, *, detected_mime: str, ctx: ExtractionContext) -> bool:
        return detected_mime == DWG_MIME

    def extract(self, data: bytes, ctx: ExtractionContext) -> ExtractionResult:
        data = data or b""
        t0 = time.monotonic()

        version = dwg_version(data)
        if version is None:
            raise CorruptedFile(MALFORMED_MESSAGE, code=MALFORMED_CODE)
        if not is_supported_version(version):
            raise RejectedFile(VERSION_MESSAGE, code=VERSION_CODE, meta={"dwg_version": version})
        if not dxf_parser_available():
            raise RejectedFile(DEPENDENCY_MESSAGE, code=DEPENDENCY_CODE, meta={"parser": PARSER})

        deployed = converter_version()
        if deployed != REQUIRED_CONVERTER_VERSION:
            raise RejectedFile(
                DEPENDENCY_MESSAGE, code=DEPENDENCY_CODE,
                meta={"converter": CONVERTER, "converter_version": deployed,
                      "expected_converter_version": REQUIRED_CONVERTER_VERSION},
            )

        path = converter_path()
        timeout = float(config.dwg_conversion_timeout_seconds())
        max_bytes = int(config.dwg_max_intermediate_dxf_mb()) * 1024 * 1024

        workdir = tempfile.mkdtemp(prefix=TEMP_PREFIX)
        try:
            os.chmod(workdir, 0o700)
            src = os.path.join(workdir, INPUT_NAME)
            fd = os.open(src, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)

            exit_code, diagnostics = _run_converter(path, workdir, timeout)
            if exit_code != 0:
                err_tail = _tail(os.path.join(workdir, _STDERR_NAME))
                logger.warning(
                    "dwg conversion failed: version=%s exit=%s diagnostics=%d tail=%r",
                    version, exit_code, diagnostics, err_tail[-400:],
                )
                if exit_code > 0 and _READ_ERROR_RE.search(err_tail):
                    # Le convertisseur a refusé de LIRE la source : le DWG est en cause.
                    raise CorruptedFile(MALFORMED_MESSAGE, code=MALFORMED_CODE,
                                        meta={"converter": CONVERTER, "converter_exit": exit_code})
                raise CorruptedFile(CONVERSION_MESSAGE, code=CONVERSION_CODE,
                                    meta={"converter": CONVERTER, "converter_exit": exit_code,
                                          "reason": "nonzero_exit"})

            size = _validate_output(workdir, max_bytes)
            with open(os.path.join(workdir, OUTPUT_NAME), "rb") as fh:
                dxf_bytes = fh.read()
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

        # Le DXF converti passe par l'extracteur DXF STRICT (aucune réparation, jamais
        # `ezdxf.recover`) : un DXF invalide écrit par le convertisseur est un ÉCHEC DE
        # CONVERSION, pas un défaut du DWG déposé par l'utilisateur.
        dxf_ctx = ExtractionContext(filename=OUTPUT_NAME, declared_content_type="application/dxf")
        try:
            inner = DxfExtractor().extract(dxf_bytes, dxf_ctx)
        except ExtractionComplexityExceeded as exc:
            raise ExtractionComplexityExceeded(
                COMPLEXITY_MESSAGE, code=COMPLEXITY_CODE,
                meta={"converter": CONVERTER, "intermediate_dxf_bytes": size,
                      "intermediate_reason": exc.code},
            ) from exc
        except RejectedFile as exc:
            raise RejectedFile(DEPENDENCY_MESSAGE, code=DEPENDENCY_CODE,
                               meta={"parser": PARSER, "intermediate_reason": exc.code}) from exc
        except CorruptedFile as exc:
            raise CorruptedFile(CONVERSION_MESSAGE, code=CONVERSION_CODE,
                                meta={"converter": CONVERTER, "reason": "invalid_dxf",
                                      "intermediate_reason": exc.code}) from exc

        units = _adapt_units(inner.units)
        metadata = {
            "converter": CONVERTER,
            "converter_version": deployed,
            "converter_warning_count": diagnostics,
            "source_dwg_version": version,
            "source_dwg_release": release_label(version),
            "intermediate_format": INTERMEDIATE_FORMAT,
            "intermediate_dxf_version": inner.metadata.get("dxf_version"),
            "intermediate_dxf_bytes": size,
            "parser": PARSER,
            "parser_version": inner.metadata.get("parser_version"),
        }
        for key in _CARRIED:
            if key in inner.metadata:
                metadata[key] = inner.metadata[key]

        result = ExtractionResult(text="", units=units, metadata=metadata)
        for w in inner.warnings:
            result.add_warning(w)
        if diagnostics:
            result.add_warning("dwg_converter_warnings")
        logger.info(
            "dwg extracted: version=%s converter=%s dxf_bytes=%d units=%d entities=%s "
            "xrefs=%s diagnostics=%d duration_ms=%d",
            version, deployed, size, len(units), metadata.get("entity_count"),
            metadata.get("xrefs_count"), diagnostics, int((time.monotonic() - t0) * 1000),
        )
        return result.finalize(fmt=FORMAT, mime=DWG_MIME, method=CONVERTER)
