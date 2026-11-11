"""Machine à état HOLDOUT à USAGE UNIQUE — techniquement non rejouable.

Un holdout ne vaut que s'il n'est vu qu'une seule fois. Cette machine garantit :

- consommation enregistrée AVANT le premier cas (un crash en cours ne rouvre jamais
  une seconde tentative) ;
- écriture d'état ATOMIQUE (tmp + fsync + os.replace) — jamais de JSON tronqué ;
- exclusion CONCURRENTE (``flock`` : un seul process passe, l'autre échoue) ;
- vérification des checksums (dataset, holdout, manifeste) et de la configuration
  GELÉE avant toute exécution ;
- aucun ``--force``, aucun reset dans le code normal.

Indépendante du holdout P2.6 (jamais touché).
"""
from __future__ import annotations

import fcntl
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

# Codes d'erreur STABLES (exposables).
ERR_ALREADY_CONSUMED = "holdout_already_consumed"
ERR_HOLDOUT_CHECKSUM_MISMATCH = "holdout_checksum_mismatch"
ERR_DATASET_MANIFEST_MISMATCH = "dataset_manifest_mismatch"
ERR_CORPUS_MANIFEST_MISMATCH = "corpus_manifest_mismatch"
ERR_CONFIGURATION_MISMATCH = "holdout_configuration_mismatch"
ERR_HUMAN_VALIDATION_INCOMPLETE = "human_validation_incomplete"
ERR_STATE_CORRUPT = "holdout_state_corrupt"
ERR_LOCK_UNAVAILABLE = "holdout_lock_unavailable"

STATE_FILENAME = "holdout-state.json"
LOCK_FILENAME = "holdout-state.lock"

HUMAN_VALIDATION_COMPLETED = "completed"


class HoldoutConsumeError(Exception):
    """Refus de consommation du holdout. Porte un ``code`` stable."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class HoldoutState:
    dataset_id: str
    dataset_sha256: str
    holdout_sha256: str
    dataset_manifest_sha256: str
    used: bool = False
    run_count: int = 0
    used_at: str | None = None
    run_id: str | None = None


def _atomic_write_json(path: Path, obj: dict) -> None:
    """Écriture atomique : fichier temporaire -> flush -> fsync -> os.replace."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True)
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, data.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))
    # Durcit aussi le répertoire (le rename n'est durable qu'après fsync du dir).
    dfd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dfd)
    except OSError:
        pass
    finally:
        os.close(dfd)


def init_holdout_state(dataset_dir: str | Path, *, dataset_id: str, dataset_sha256: str,
                       holdout_sha256: str, dataset_manifest_sha256: str) -> HoldoutState:
    """Crée l'état INITIAL (used=false) — uniquement après validation complète du
    dataset. Écrase un état existant (construction atomique du dataset)."""
    state = HoldoutState(
        dataset_id=dataset_id, dataset_sha256=dataset_sha256, holdout_sha256=holdout_sha256,
        dataset_manifest_sha256=dataset_manifest_sha256, used=False, run_count=0,
        used_at=None, run_id=None,
    )
    _atomic_write_json(Path(dataset_dir) / STATE_FILENAME, asdict(state))
    return state


def read_holdout_state(dataset_dir: str | Path) -> HoldoutState:
    p = Path(dataset_dir) / STATE_FILENAME
    if not p.exists():
        raise HoldoutConsumeError(ERR_STATE_CORRUPT, f"état absent: {p}")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return HoldoutState(**{k: raw.get(k) for k in HoldoutState.__annotations__})
    except (json.JSONDecodeError, TypeError) as e:
        raise HoldoutConsumeError(ERR_STATE_CORRUPT, str(e)) from e


def verify_integrity(
    state: HoldoutState, *, holdout_sha256: str, dataset_manifest_sha256: str,
    corpus_manifest_checksum: str, expected_corpus_checksum: str,
    frozen_config: dict, current_config: dict,
    human_validation_status: str,
) -> None:
    """Vérifie checksums + config gelée + validation humaine. Lève au premier écart."""
    if state.holdout_sha256 != holdout_sha256:
        raise HoldoutConsumeError(ERR_HOLDOUT_CHECKSUM_MISMATCH)
    if state.dataset_manifest_sha256 != dataset_manifest_sha256:
        raise HoldoutConsumeError(ERR_DATASET_MANIFEST_MISMATCH)
    if corpus_manifest_checksum != expected_corpus_checksum:
        raise HoldoutConsumeError(ERR_CORPUS_MANIFEST_MISMATCH)
    if frozen_config != current_config:
        raise HoldoutConsumeError(ERR_CONFIGURATION_MISMATCH)
    if human_validation_status != HUMAN_VALIDATION_COMPLETED:
        raise HoldoutConsumeError(ERR_HUMAN_VALIDATION_INCOMPLETE, human_validation_status or "pending")


def consume_holdout_once(
    dataset_dir: str | Path, *, run_id: str, now_iso: str, runner_callable,
    integrity_check=None,
):
    """Consomme le holdout EXACTEMENT une fois puis lance ``runner_callable()``.

    Ordre (§27-28) : verrou -> préflight/intégrité -> ÉTAT CONSOMMÉ PERSISTÉ -> runner.
    L'état ``used=true`` est écrit AVANT le premier cas : un crash ultérieur ne rouvre
    jamais une seconde tentative. ``integrity_check`` (callable, optionnel) est exécuté
    avant la consommation et peut lever ``HoldoutConsumeError``.
    """
    dataset_dir = Path(dataset_dir)
    lock_path = dataset_dir / LOCK_FILENAME
    lock_fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise HoldoutConsumeError(ERR_LOCK_UNAVAILABLE, "consommation concurrente") from e

        state = read_holdout_state(dataset_dir)
        if state.used or state.run_count != 0:
            raise HoldoutConsumeError(ERR_ALREADY_CONSUMED,
                                      f"used={state.used} run_count={state.run_count}")
        if integrity_check is not None:
            integrity_check(state)     # lève HoldoutConsumeError sur écart

        # ÉTAT CONSOMMÉ PERSISTÉ AVANT LE PREMIER CAS (atomique).
        consumed = HoldoutState(
            dataset_id=state.dataset_id, dataset_sha256=state.dataset_sha256,
            holdout_sha256=state.holdout_sha256,
            dataset_manifest_sha256=state.dataset_manifest_sha256,
            used=True, run_count=1, used_at=now_iso, run_id=run_id,
        )
        _atomic_write_json(dataset_dir / STATE_FILENAME, asdict(consumed))

        # Seulement MAINTENANT : le premier cas.
        return runner_callable()
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(lock_fd)
