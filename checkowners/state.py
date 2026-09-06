"""Persistent state at ~/.checkowners/state/<repo-hash>.json.

The state file is the cache of the most recent analyze run for a repo.
Downstream commands (drift, decay, bus-factor, topology, balance, onboard)
read from it to avoid re-running git log on every invocation. State is keyed
per repo (schema v4): each repo gets its own file, and the payload embeds the
absolute repo path so state from one repo can never leak into another.

Schema is versioned. Older state files are not auto-migrated; they are
ignored and a fresh state replaces them on the next analyze.

Write safety: all writes go through ``_atomic_write`` which serialises
concurrent writers with cross-platform advisory locking and uses
``tempfile`` + ``os.replace`` for crash-safe atomic replacement.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from checkowners.models import (
    BusFactor,
    ConfidenceScore,
    DecayWarning,
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
    TeamCluster,
)

SCHEMA_VERSION: int = 4
MODEL_VERSION: str = "0.6.0"
_STATE_DIR = Path.home() / ".checkowners"
_STATE_SUBDIR = "state"
_GRAPH_CACHE_SUBDIR = "graph"
_HANDLE_CACHE_FILENAME = "handles.json"

MAX_CACHE_SIZE_BYTES: int = 50 * 1024 * 1024  # 50 MB


class StalenessError(Exception):
    """Raised when cached state is stale relative to the current HEAD."""


def _base_dir() -> Path:
    """Resolve the checkowners state directory, honoring CHECKOWNERS_STATE_DIR."""
    override = os.environ.get("CHECKOWNERS_STATE_DIR")
    return Path(override) if override else _STATE_DIR


def _repo_digest(repo_root: Path) -> str:
    return hashlib.sha256(str(repo_root.resolve()).encode("utf-8")).hexdigest()[:16]


def _state_path(repo_root: Path) -> Path:
    """Resolve the per-repo state file path, honoring CHECKOWNERS_STATE_DIR."""
    return _base_dir() / _STATE_SUBDIR / f"{_repo_digest(repo_root)}.json"


def _graph_cache_path(repo_root: Path) -> Path:
    """Resolve the serialized-graph cache path for a repo (keyed by repo hash)."""
    return _base_dir() / _GRAPH_CACHE_SUBDIR / f"{_repo_digest(repo_root)}.json"


def _handle_cache_path() -> Path:
    """Resolve the email -> @handle cache path."""
    return _base_dir() / _HANDLE_CACHE_FILENAME


# ---------------------------------------------------------------------------
# Cross-platform advisory file locking
# ---------------------------------------------------------------------------


@contextmanager
def _lock_ctx(path: Path) -> Iterator[None]:
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+b")  # noqa: SIM115
    try:
        _acquire(fh)
        yield
    finally:
        try:
            _release(fh)
        finally:
            fh.close()
            with suppress(OSError):
                lock_path.unlink()


if os.name == "nt":
    import msvcrt  # noqa: PLC0415

    def _acquire(fh: BinaryIO) -> None:
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)

    def _release(fh: BinaryIO) -> None:
        with suppress(OSError):
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl  # noqa: E402, PLC0415

    def _acquire(fh: BinaryIO) -> None:
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)  # type: ignore[attr-defined]
                break
            except OSError as exc:
                if exc.errno != errno.EINTR:
                    raise

    def _release(fh: BinaryIO) -> None:
        with suppress(OSError):
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]


def _atomic_write(target: Path, data: str) -> Path:
    """Write *data* to *target* atomically with advisory locking.

    A ``tempfile.NamedTemporaryFile`` is created in the same directory as
    *target* and then ``os.replace``'d into place.  A per-file advisory lock
    serialises concurrent writers so two threads / processes never interleave
    partial JSON. Lock files are cleaned up after each write.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    with _lock_ctx(target):
        fd, tmp_path = tempfile.mkstemp(
            dir=str(target.parent),
            prefix=target.name,
            suffix=".tmp",
        )
        try:
            os.write(fd, data.encode("utf-8"))
            os.fsync(fd)
            os.close(fd)
            os.replace(tmp_path, str(target))
        except BaseException:
            with suppress(OSError):
                os.close(fd)
            with suppress(OSError):
                os.unlink(tmp_path)
            raise
    return target


# ---------------------------------------------------------------------------
# Git ref helpers
# ---------------------------------------------------------------------------


def _git_head_ref(repo_root: Path) -> str | None:
    """Return the current HEAD commit SHA, or ``None`` on failure."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    stdout = getattr(result, "stdout", None)
    if not isinstance(stdout, str):
        return None
    ref = stdout.strip()
    return ref or None


def _is_ancestor(commit: str, head: str, repo_root: Path) -> bool:
    """Return True if *commit* is an ancestor of *head*."""
    import subprocess

    try:
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", commit, head],
            capture_output=True,
            cwd=str(repo_root),
            check=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


# ---------------------------------------------------------------------------
# Staleness validation
# ---------------------------------------------------------------------------


def validate_staleness(
    data: dict[str, Any],
    repo_root: Path,
    *,
    allow_stale: bool = False,
    max_age_days: float | None = None,
) -> None:
    """Validate that cached *data* is not stale.

    Checks:
    1. ``analyzed_ref`` must be an ancestor of the current HEAD.
    2. ``analyzed_at`` must be within ``max_age_days`` when specified.

    Raises :class:`StalenessError` when the state is stale and *allow_stale*
    is ``False``.
    """
    analyzed_ref = data.get("analyzed_ref")
    head = _git_head_ref(repo_root)

    if (
        isinstance(analyzed_ref, str)
        and head
        and not _is_ancestor(analyzed_ref, head, repo_root)
        and not allow_stale
    ):
        raise StalenessError(
            f"State was analyzed at ref {analyzed_ref[:8]}, which is not "
            f"an ancestor of the current HEAD ({head[:8]}). "
            "Run `checkowners analyze` to refresh, or pass --allow-stale."
        )

    if max_age_days is not None:
        analyzed_at_raw = data.get("analyzed_at")
        if isinstance(analyzed_at_raw, str):
            try:
                analyzed_at = datetime.fromisoformat(analyzed_at_raw)
                age_days = (datetime.now(UTC) - analyzed_at).total_seconds() / 86400.0
                if age_days > max_age_days and not allow_stale:
                    raise StalenessError(
                        f"State is {age_days:.1f} days old (max allowed: {max_age_days}). "
                        "Run `checkowners analyze` to refresh, or pass --allow-stale."
                    )
            except ValueError:
                pass


# ---------------------------------------------------------------------------
# Read / write functions
# ---------------------------------------------------------------------------


def write_graph_cache(repo_root: Path, last_analyzed: datetime, graph_data: dict[str, Any]) -> Path:
    """Persist a serialized knowledge graph, tagged with the analysis timestamp."""
    target = _graph_cache_path(repo_root)
    head = _git_head_ref(repo_root)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "model_version": MODEL_VERSION,
        "repo": str(repo_root.resolve()),
        "last_analyzed": last_analyzed.astimezone(UTC).isoformat(),
        "analyzed_ref": head,
        "analyzed_at": datetime.now(UTC).isoformat(),
        "graph": graph_data,
    }
    return _atomic_write(target, json.dumps(payload, indent=2, sort_keys=True))


def read_graph_cache(repo_root: Path, last_analyzed: datetime) -> dict[str, Any] | None:
    """Return the cached graph for a repo when present and not stale, else None.

    Freshness is keyed on the analysis timestamp: a cache built from an older
    ``analyze`` run is ignored so the graph never lags the ownership map.
    """
    target = _graph_cache_path(repo_root)
    if not target.exists():
        return None
    try:
        data: Any = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        return None
    if data.get("last_analyzed") != last_analyzed.astimezone(UTC).isoformat():
        return None
    graph = data.get("graph")
    return graph if isinstance(graph, dict) else None


def read_state(repo_root: Path) -> dict[str, Any] | None:
    """Read a repo's state as a dict, or None if missing/version/repo mismatch."""
    target = _state_path(repo_root)
    if not target.exists():
        return None
    try:
        data: Any = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("schema_version") != SCHEMA_VERSION:
        return None
    if data.get("repo") != str(repo_root.resolve()):
        return None
    return data


def write_state(
    repo_root: Path,
    ownership: OwnershipMap,
    *,
    topology: tuple[TeamCluster, ...] = (),
    bus_factor_summary: tuple[BusFactor, ...] = (),
    drift_detected: bool = False,
) -> Path:
    """Persist the latest ownership map and derived intelligence to disk."""
    target = _state_path(repo_root)
    head = _git_head_ref(repo_root)
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "model_version": MODEL_VERSION,
        "repo": str(repo_root.resolve()),
        "analyzed_ref": head,
        "analyzed_at": datetime.now(UTC).isoformat(),
        "inferred": {path: _serialize_path(po) for path, po in ownership.paths.items()},
        "topology": {"clusters": [asdict(c) for c in topology]},
        "bus_factor_summary": _serialize_bus_factor_summary(bus_factor_summary),
        "last_analyzed": ownership.last_analyzed.astimezone(UTC).isoformat(),
        "drift_detected": drift_detected,
    }
    return _atomic_write(target, json.dumps(payload, indent=2, sort_keys=True))


def read_handle_cache() -> dict[str, str]:
    """Read the persistent email -> @handle cache (shared across repos).

    An empty-string value is a remembered miss: the email was looked up before
    and did not resolve, so callers should not re-query the API for it.
    """
    target = _handle_cache_path()
    if not target.exists():
        return {}
    try:
        data: Any = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}


def write_handle_cache(cache: dict[str, str]) -> Path:
    """Persist the email -> @handle cache, merging over any existing entries."""
    target = _handle_cache_path()
    merged = {**read_handle_cache(), **cache}
    return _atomic_write(target, json.dumps(merged, indent=2, sort_keys=True))


def load_ownership(
    repo_root: Path,
    *,
    allow_stale: bool = False,
    max_age_days: float | None = None,
) -> OwnershipMap | None:
    """Reconstruct a repo's OwnershipMap, or None if state is missing/invalid.

    When *allow_stale* is ``False`` (the default) and *max_age_days* is not
    set, stale state (analyzed_ref is not an ancestor of HEAD) raises
    :class:`StalenessError`.  Set *allow_stale* to ``True`` to accept stale
    state, or *max_age_days* to enforce a TTL.
    """
    data = read_state(repo_root)
    if data is None:
        return None

    validate_staleness(data, repo_root, allow_stale=allow_stale, max_age_days=max_age_days)

    inferred = data.get("inferred")
    last_analyzed_raw = data.get("last_analyzed")
    if not isinstance(inferred, dict) or not isinstance(last_analyzed_raw, str):
        return None
    paths: dict[str, PathOwnership] = {}
    for path, raw in inferred.items():
        if not isinstance(raw, dict):
            continue
        path_ownership = _deserialize_path(raw)
        if path_ownership is None:
            continue
        paths[path] = path_ownership
    try:
        last_analyzed = datetime.fromisoformat(last_analyzed_raw)
    except ValueError:
        return None
    return OwnershipMap(paths=paths, last_analyzed=last_analyzed)


# ---------------------------------------------------------------------------
# Cache directory utilities
# ---------------------------------------------------------------------------


def cache_dir() -> Path:
    """Return the base cache directory (shared by state, graph, handle files)."""
    return _base_dir()


def cache_info() -> dict[str, Any]:
    """Return metadata about the cache: file count, total size, per-repo breakdown."""
    base = _base_dir()
    if not base.exists():
        return {"file_count": 0, "total_bytes": 0, "repos": {}}

    total_bytes = 0
    file_count = 0
    repos: dict[str, dict[str, Any]] = {}

    for subdir_name in (_STATE_SUBDIR, _GRAPH_CACHE_SUBDIR):
        subdir = base / subdir_name
        if not subdir.exists():
            continue
        for entry in subdir.iterdir():
            if entry.is_file() and not entry.name.endswith(".lock"):
                file_count += 1
                size = entry.stat().st_size
                total_bytes += size
                repos.setdefault(entry.stem, {"files": 0, "bytes": 0})
                repos[entry.stem]["files"] += 1
                repos[entry.stem]["bytes"] += size

    handle = _handle_cache_path()
    if handle.exists() and handle.is_file():
        file_count += 1
        size = handle.stat().st_size
        total_bytes += size
        repos.setdefault("handles", {"files": 0, "bytes": 0})
        repos["handles"]["files"] += 1
        repos["handles"]["bytes"] += size

    return {"file_count": file_count, "total_bytes": total_bytes, "repos": repos}


def clear_repo_cache(repo_root: Path) -> int:
    """Remove cached state and graph for *repo_root*. Returns files removed."""
    removed = 0
    for path in (_state_path(repo_root), _graph_cache_path(repo_root)):
        if path.exists():
            path.unlink()
            removed += 1
        lock = path.with_suffix(path.suffix + ".lock")
        if lock.exists():
            lock.unlink()
            removed += 1
    return removed


def purge_cache() -> int:
    """Remove the entire cache directory. Returns files removed."""
    base = _base_dir()
    if not base.exists():
        return 0
    removed = 0
    for entry in sorted(base.rglob("*")):
        if entry.is_file():
            entry.unlink()
            removed += 1
    for entry in sorted(base.rglob("*"), reverse=True):
        if entry.is_dir():
            with suppress(OSError):
                entry.rmdir()
    return removed


def evict_if_oversize() -> int:
    """Evict oldest cache files when total size exceeds MAX_CACHE_SIZE_BYTES.

    Files are evicted oldest-first (by mtime). Returns the number of files
    evicted.
    """
    base = _base_dir()
    if not base.exists():
        return 0

    files: list[Path] = []
    for entry in base.rglob("*"):
        if entry.is_file() and not entry.name.endswith(".lock"):
            files.append(entry)

    total = sum(f.stat().st_size for f in files)
    if total <= MAX_CACHE_SIZE_BYTES:
        return 0

    files.sort(key=lambda f: f.stat().st_mtime)
    evicted = 0
    for f in files:
        if total <= MAX_CACHE_SIZE_BYTES:
            break
        size = f.stat().st_size
        f.unlink()
        total -= size
        evicted += 1
    return evicted


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _serialize_path(po: PathOwnership) -> dict[str, Any]:
    return {
        "owners": [_serialize_owner(o) for o in po.owners],
        "bus_factor": po.bus_factor,
        "decay_warnings": [_serialize_decay(w) for w in po.decay_warnings],
    }


def _serialize_owner(entry: OwnerEntry) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "handle": entry.handle,
        "confidence": entry.confidence,
        "last_commit": entry.last_commit.astimezone(UTC).isoformat() if entry.last_commit else None,
        "commits": entry.commits,
    }
    if entry.score_breakdown is not None:
        payload["score_breakdown"] = asdict(entry.score_breakdown)
    return payload


def _serialize_decay(warning: DecayWarning) -> dict[str, Any]:
    return {
        "handle": warning.handle,
        "path": warning.path,
        "last_commit": warning.last_commit.astimezone(UTC).isoformat(),
        "days_since_last_commit": warning.days_since_last_commit,
        "historical_confidence": warning.historical_confidence,
    }


def _serialize_bus_factor_summary(
    entries: tuple[BusFactor, ...],
) -> dict[str, Any]:
    critical_paths = sorted(e.path for e in entries if e.bus_factor <= 1)
    repo_average = round(sum(e.bus_factor for e in entries) / len(entries), 2) if entries else 0.0
    return {
        "critical_paths": critical_paths,
        "repo_average": repo_average,
        "entries": [asdict(e) for e in entries],
    }


def _deserialize_path(raw: dict[str, Any]) -> PathOwnership | None:
    raw_owners = raw.get("owners")
    if not isinstance(raw_owners, list):
        return None
    owners: list[OwnerEntry] = []
    for entry in raw_owners:
        if not isinstance(entry, dict):
            continue
        deserialized = _deserialize_owner(entry)
        if deserialized is not None:
            owners.append(deserialized)
    bus_factor = int(raw.get("bus_factor", 0))
    raw_decay = raw.get("decay_warnings", [])
    decay_warnings: list[DecayWarning] = []
    if isinstance(raw_decay, list):
        for warning in raw_decay:
            if isinstance(warning, dict):
                deserialized_warning = _deserialize_decay(warning)
                if deserialized_warning is not None:
                    decay_warnings.append(deserialized_warning)
    return PathOwnership(
        owners=tuple(owners),
        bus_factor=bus_factor,
        decay_warnings=tuple(decay_warnings),
    )


def _deserialize_owner(raw: dict[str, Any]) -> OwnerEntry | None:
    handle = raw.get("handle")
    confidence = raw.get("confidence")
    commits = raw.get("commits")
    last_commit_raw = raw.get("last_commit")
    if not isinstance(handle, str) or not isinstance(confidence, int | float):
        return None
    if not isinstance(commits, int):
        return None
    last_commit: datetime | None
    if isinstance(last_commit_raw, str):
        try:
            last_commit = datetime.fromisoformat(last_commit_raw)
        except ValueError:
            last_commit = None
    else:
        last_commit = None
    score_breakdown: ConfidenceScore | None = None
    raw_breakdown = raw.get("score_breakdown")
    if isinstance(raw_breakdown, dict):
        try:
            score_breakdown = ConfidenceScore(
                total=float(raw_breakdown["total"]),
                recency=float(raw_breakdown["recency"]),
                frequency=float(raw_breakdown["frequency"]),
                blame=float(raw_breakdown["blame"]),
                review=float(raw_breakdown["review"]),
            )
        except (KeyError, TypeError, ValueError):
            score_breakdown = None
    return OwnerEntry(
        handle=handle,
        confidence=float(confidence),
        last_commit=last_commit,
        commits=commits,
        score_breakdown=score_breakdown,
    )


def _deserialize_decay(raw: dict[str, Any]) -> DecayWarning | None:
    try:
        return DecayWarning(
            handle=str(raw["handle"]),
            path=str(raw["path"]),
            last_commit=datetime.fromisoformat(str(raw["last_commit"])),
            days_since_last_commit=int(raw["days_since_last_commit"]),
            historical_confidence=float(raw["historical_confidence"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
