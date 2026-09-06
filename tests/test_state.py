"""Tests for checkowners.state module."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from checkowners.models import (
    BusFactor,
    ConfidenceScore,
    DecayWarning,
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
    TeamCluster,
)
from checkowners.state import (
    SCHEMA_VERSION,
    StalenessError,
    _state_path,
    cache_dir,
    cache_info,
    clear_repo_cache,
    evict_if_oversize,
    load_ownership,
    purge_cache,
    read_graph_cache,
    read_handle_cache,
    read_state,
    validate_staleness,
    write_graph_cache,
    write_handle_cache,
    write_state,
)

_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


def _make_ownership() -> OwnershipMap:
    breakdown = ConfidenceScore(total=0.85, recency=0.9, frequency=0.7, blame=0.8, review=0.6)
    owner = OwnerEntry(
        handle="@alice",
        confidence=0.85,
        last_commit=_NOW,
        commits=12,
        score_breakdown=breakdown,
    )
    decay = DecayWarning(
        handle="@bob",
        path="src/auth.py",
        last_commit=_NOW,
        days_since_last_commit=200,
        historical_confidence=0.4,
    )
    po = PathOwnership(owners=(owner,), bus_factor=1, decay_warnings=(decay,))
    return OwnershipMap(paths={"src/auth.py": po}, last_analyzed=_NOW)


def _write_raw_state(repo_root: Path, payload: object) -> None:
    target = _state_path(repo_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        payload if isinstance(payload, str) else json.dumps(payload),
        encoding="utf-8",
    )


def test_state_path_is_per_repo(tmp_path: Path, repo: Path) -> None:
    other = tmp_path / "other-repo"
    other.mkdir()
    assert _state_path(repo) != _state_path(other)
    assert _state_path(repo).parent.name == "state"


def test_read_state_missing_returns_none(repo: Path) -> None:
    assert read_state(repo) is None


def test_read_state_invalid_json_returns_none(repo: Path) -> None:
    _write_raw_state(repo, "not json")
    assert read_state(repo) is None


def test_read_state_wrong_schema_returns_none(repo: Path) -> None:
    _write_raw_state(repo, {"schema_version": 2, "repo": str(repo.resolve())})
    assert read_state(repo) is None


def test_read_state_non_dict_returns_none(repo: Path) -> None:
    _write_raw_state(repo, ["not", "a", "dict"])
    assert read_state(repo) is None


def test_read_state_repo_mismatch_returns_none(repo: Path) -> None:
    """State written under this repo's digest but naming another repo is rejected."""
    _write_raw_state(
        repo,
        {"schema_version": SCHEMA_VERSION, "repo": "/somewhere/else"},
    )
    assert read_state(repo) is None


def test_write_and_read_roundtrip(repo: Path) -> None:
    ownership = _make_ownership()
    topology = (
        TeamCluster(
            name="backend",
            members=("@alice", "@bob"),
            primary_paths=("src/api/",),
            declared=True,
        ),
    )
    bus_factor = (
        BusFactor(
            path="src/auth.py",
            bus_factor=1,
            contributors_above_threshold=("@alice",),
            recommended_backups=("@bob",),
        ),
    )
    with patch("checkowners.state._git_head_ref", return_value="abc123"):
        target = write_state(
            repo,
            ownership,
            topology=topology,
            bus_factor_summary=bus_factor,
            drift_detected=True,
        )
    assert target.exists()
    data = read_state(repo)
    assert data is not None
    assert data["schema_version"] == SCHEMA_VERSION
    assert data["repo"] == str(repo.resolve())
    assert data["drift_detected"] is True
    assert data["topology"]["clusters"][0]["name"] == "backend"
    assert data["bus_factor_summary"]["critical_paths"] == ["src/auth.py"]
    assert data["bus_factor_summary"]["repo_average"] == 1.0
    assert "src/auth.py" in data["inferred"]
    assert data["analyzed_ref"] == "abc123"
    assert "analyzed_at" in data
    assert data["model_version"] == "0.6.0"


def test_state_isolated_between_repos(tmp_path: Path, repo: Path) -> None:
    """Analyzing repo A must never leak ownership into repo B."""
    other = tmp_path / "other-repo"
    other.mkdir()
    with patch("checkowners.state._git_head_ref", return_value="abc123"):
        write_state(repo, _make_ownership())
    assert load_ownership(other) is None
    assert load_ownership(repo) is not None


def test_load_ownership_roundtrip(repo: Path) -> None:
    original = _make_ownership()
    with patch("checkowners.state._git_head_ref", return_value="abc123"):
        write_state(repo, original)
    loaded = load_ownership(repo)
    assert loaded is not None
    assert set(loaded.paths) == set(original.paths)
    loaded_owner = loaded.paths["src/auth.py"].owners[0]
    assert loaded_owner.handle == "@alice"
    assert loaded_owner.confidence == pytest.approx(0.85)
    assert loaded_owner.commits == 12
    assert loaded_owner.last_commit == _NOW
    assert loaded_owner.score_breakdown is not None
    assert loaded_owner.score_breakdown.recency == pytest.approx(0.9)
    decay = loaded.paths["src/auth.py"].decay_warnings[0]
    assert decay.handle == "@bob"
    assert decay.days_since_last_commit == 200


def test_load_ownership_missing_returns_none(repo: Path) -> None:
    assert load_ownership(repo) is None


def test_load_ownership_invalid_returns_none(repo: Path) -> None:
    _write_raw_state(
        repo,
        {
            "schema_version": SCHEMA_VERSION,
            "repo": str(repo.resolve()),
            "inferred": "not a dict",
        },
    )
    assert load_ownership(repo) is None


def test_load_ownership_skips_malformed_path(repo: Path) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "repo": str(repo.resolve()),
        "inferred": {
            "src/good.py": {
                "owners": [
                    {
                        "handle": "@alice",
                        "confidence": 0.5,
                        "last_commit": _NOW.isoformat(),
                        "commits": 3,
                    }
                ],
                "bus_factor": 1,
                "decay_warnings": [],
            },
            "src/bad.py": "garbage",
        },
        "last_analyzed": _NOW.isoformat(),
        "drift_detected": False,
    }
    _write_raw_state(repo, payload)
    loaded = load_ownership(repo)
    assert loaded is not None
    assert set(loaded.paths) == {"src/good.py"}


def test_write_state_creates_parent_dirs(tmp_path: Path, repo: Path) -> None:
    nested = tmp_path / "nested" / "dir"
    with (
        patch.dict("os.environ", {"CHECKOWNERS_STATE_DIR": str(nested)}),
        patch("checkowners.state._git_head_ref", return_value="abc123"),
    ):
        target = write_state(repo, _make_ownership())
    assert target.exists()
    assert target.is_relative_to(nested)


def test_bus_factor_summary_empty(repo: Path) -> None:
    ownership = _make_ownership()
    with patch("checkowners.state._git_head_ref", return_value="abc123"):
        target = write_state(repo, ownership)
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["bus_factor_summary"]["critical_paths"] == []
    assert data["bus_factor_summary"]["repo_average"] == 0.0


def test_handle_cache_roundtrip() -> None:
    write_handle_cache({"alice@example.com": "@alice", "gone@example.com": ""})
    cache = read_handle_cache()
    assert cache["alice@example.com"] == "@alice"
    assert cache["gone@example.com"] == ""


def test_handle_cache_merges_on_write() -> None:
    write_handle_cache({"alice@example.com": "@alice"})
    write_handle_cache({"bob@example.com": "@bob"})
    cache = read_handle_cache()
    assert cache == {"alice@example.com": "@alice", "bob@example.com": "@bob"}


def test_handle_cache_missing_returns_empty() -> None:
    assert read_handle_cache() == {}


def test_graph_cache_roundtrip(tmp_path: Path) -> None:
    graph_data = {"nodes": [{"id": "contrib::a"}], "edges": []}
    with patch("checkowners.state._git_head_ref", return_value="abc123"):
        target = write_graph_cache(tmp_path, _NOW, graph_data)
    assert target.exists()
    assert read_graph_cache(tmp_path, _NOW) == graph_data


def test_graph_cache_stale_timestamp_ignored(tmp_path: Path) -> None:
    with patch("checkowners.state._git_head_ref", return_value="abc123"):
        write_graph_cache(tmp_path, _NOW, {"nodes": [], "edges": []})
    newer = datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)
    assert read_graph_cache(tmp_path, newer) is None


def test_graph_cache_missing_returns_none(tmp_path: Path) -> None:
    assert read_graph_cache(tmp_path, _NOW) is None


def test_graph_cache_keyed_by_repo(tmp_path: Path) -> None:
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    repo_a.mkdir()
    repo_b.mkdir()
    with patch("checkowners.state._git_head_ref", return_value="abc123"):
        write_graph_cache(repo_a, _NOW, {"nodes": [{"id": "a"}], "edges": []})
    assert read_graph_cache(repo_b, _NOW) is None
    assert read_graph_cache(repo_a, _NOW) == {"nodes": [{"id": "a"}], "edges": []}


# ---------------------------------------------------------------------------
# New tests for Issue #56 features
# ---------------------------------------------------------------------------


class TestAtomicWrites:
    """Verify atomic write helpers produce valid JSON files."""

    def test_atomic_write_roundtrip(self, tmp_path: Path) -> None:
        from checkowners.state import _atomic_write

        target = tmp_path / "test.json"
        data = json.dumps({"hello": "world"})
        result = _atomic_write(target, data)
        assert result == target
        assert target.read_text(encoding="utf-8") == data

    def test_atomic_write_overwrites_existing(self, tmp_path: Path) -> None:
        from checkowners.state import _atomic_write

        target = tmp_path / "test.json"
        _atomic_write(target, json.dumps({"v": 1}))
        _atomic_write(target, json.dumps({"v": 2}))
        assert json.loads(target.read_text(encoding="utf-8")) == {"v": 2}

    def test_atomic_write_no_temp_files_left(self, tmp_path: Path) -> None:
        from checkowners.state import _atomic_write

        target = tmp_path / "test.json"
        _atomic_write(target, json.dumps({"x": 1}))
        remaining = list(tmp_path.iterdir())
        assert len(remaining) == 1
        assert remaining[0].name == "test.json"

    def test_write_state_uses_analyzed_ref(self, repo: Path) -> None:
        with patch("checkowners.state._git_head_ref", return_value="deadbeef"):
            write_state(repo, _make_ownership())
        data = read_state(repo)
        assert data is not None
        assert data["analyzed_ref"] == "deadbeef"
        assert isinstance(data["analyzed_at"], str)
        assert data["model_version"] == "0.6.0"

    def test_write_state_analyzed_ref_none_when_git_fails(self, repo: Path) -> None:
        with patch("checkowners.state._git_head_ref", return_value=None):
            write_state(repo, _make_ownership())
        data = read_state(repo)
        assert data is not None
        assert data["analyzed_ref"] is None


class TestConcurrency:
    """Parallel atomic writes must not corrupt state."""

    def test_parallel_writes_no_corruption(self, tmp_path: Path) -> None:
        from checkowners.state import _atomic_write

        target = tmp_path / "shared.json"
        errors: list[Exception] = []

        def writer(value: int) -> None:
            try:
                for _ in range(20):
                    _atomic_write(target, json.dumps({"value": value}))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        raw = target.read_text(encoding="utf-8")
        data = json.loads(raw)
        assert "value" in data
        assert isinstance(data["value"], int)

    def test_no_orphaned_lock_files(self, tmp_path: Path) -> None:
        from checkowners.state import _atomic_write

        target = tmp_path / "test.json"
        for i in range(10):
            _atomic_write(target, json.dumps({"i": i}))
        lock_files = list(tmp_path.glob("*.lock"))
        assert lock_files == []


class TestStaleness:
    """Validate staleness detection and --allow-stale bypass."""

    def test_staleness_error_raised_when_ref_not_ancestor(self, repo: Path) -> None:
        """State analyzed at commit A, current HEAD is commit B (not a descendant)."""
        payload = {
            "schema_version": SCHEMA_VERSION,
            "repo": str(repo.resolve()),
            "analyzed_ref": "aaa111",
            "analyzed_at": _NOW.isoformat(),
            "last_analyzed": _NOW.isoformat(),
            "inferred": {},
            "drift_detected": False,
        }
        _write_raw_state(repo, payload)

        with (
            patch("checkowners.state._git_head_ref", return_value="bbb222"),
            patch("checkowners.state._is_ancestor", return_value=False),
            pytest.raises(StalenessError, match="not an ancestor"),
        ):
            load_ownership(repo)

    def test_staleness_bypassed_with_allow_stale(self, repo: Path) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "repo": str(repo.resolve()),
            "analyzed_ref": "aaa111",
            "analyzed_at": _NOW.isoformat(),
            "last_analyzed": _NOW.isoformat(),
            "inferred": {
                "src/main.py": {
                    "owners": [
                        {
                            "handle": "@alice",
                            "confidence": 0.5,
                            "last_commit": _NOW.isoformat(),
                            "commits": 5,
                        }
                    ],
                    "bus_factor": 1,
                    "decay_warnings": [],
                },
            },
            "drift_detected": False,
        }
        _write_raw_state(repo, payload)

        with (
            patch("checkowners.state._git_head_ref", return_value="bbb222"),
            patch("checkowners.state._is_ancestor", return_value=False),
        ):
            loaded = load_ownership(repo, allow_stale=True)
            assert loaded is not None

    def test_staleness_error_on_max_age(self, repo: Path) -> None:
        old_time = (_NOW - timedelta(days=300)).isoformat()
        payload = {
            "schema_version": SCHEMA_VERSION,
            "repo": str(repo.resolve()),
            "analyzed_ref": "aaa111",
            "analyzed_at": old_time,
            "last_analyzed": _NOW.isoformat(),
            "inferred": {},
            "drift_detected": False,
        }
        _write_raw_state(repo, payload)

        with (
            patch("checkowners.state._git_head_ref", return_value="aaa111"),
            patch("checkowners.state._is_ancestor", return_value=True),
            pytest.raises(StalenessError, match="days old"),
        ):
            load_ownership(repo, max_age_days=30)

    def test_max_age_bypassed_with_allow_stale(self, repo: Path) -> None:
        old_time = (_NOW - timedelta(days=300)).isoformat()
        payload = {
            "schema_version": SCHEMA_VERSION,
            "repo": str(repo.resolve()),
            "analyzed_ref": "aaa111",
            "analyzed_at": old_time,
            "last_analyzed": _NOW.isoformat(),
            "inferred": {},
            "drift_detected": False,
        }
        _write_raw_state(repo, payload)

        with (
            patch("checkowners.state._git_head_ref", return_value="aaa111"),
            patch("checkowners.state._is_ancestor", return_value=True),
        ):
            loaded = load_ownership(repo, allow_stale=True, max_age_days=30)
            assert loaded is not None

    def test_fresh_state_not_rejected(self, repo: Path) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "repo": str(repo.resolve()),
            "analyzed_ref": "aaa111",
            "analyzed_at": _NOW.isoformat(),
            "last_analyzed": _NOW.isoformat(),
            "inferred": {},
            "drift_detected": False,
        }
        _write_raw_state(repo, payload)

        with (
            patch("checkowners.state._git_head_ref", return_value="bbb222"),
            patch("checkowners.state._is_ancestor", return_value=True),
        ):
            loaded = load_ownership(repo)
            assert loaded is not None

    def test_validate_staleness_fresh_ref(self) -> None:
        data = {"analyzed_ref": "aaa"}
        with (
            patch("checkowners.state._git_head_ref", return_value="bbb"),
            patch("checkowners.state._is_ancestor", return_value=True),
        ):
            validate_staleness(data, Path("."), allow_stale=False)

    def test_validate_staleness_stale_ref_raises(self) -> None:
        data = {"analyzed_ref": "aaa"}
        with (
            patch("checkowners.state._git_head_ref", return_value="bbb"),
            patch("checkowners.state._is_ancestor", return_value=False),
            pytest.raises(StalenessError),
        ):
            validate_staleness(data, Path("."), allow_stale=False)

    def test_validate_staleness_missing_ref_skips_check(self) -> None:
        data = {}
        with patch("checkowners.state._git_head_ref", return_value="bbb"):
            validate_staleness(data, Path("."), allow_stale=False)

    def test_validate_staleness_missing_head_skips_check(self) -> None:
        data = {"analyzed_ref": "aaa"}
        with patch("checkowners.state._git_head_ref", return_value=None):
            validate_staleness(data, Path("."), allow_stale=False)


class TestCacheCommands:
    """Verify cache utility functions."""

    def test_cache_dir_returns_path(self) -> None:
        result = cache_dir()
        assert isinstance(result, Path)

    def test_cache_info_empty(self, tmp_path: Path) -> None:
        with patch("checkowners.state._base_dir", return_value=tmp_path):
            info = cache_info()
            assert info["file_count"] == 0
            assert info["total_bytes"] == 0
            assert info["repos"] == {}

    def test_cache_info_with_files(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "abc.json").write_text('{"x": 1}', encoding="utf-8")
        with patch("checkowners.state._base_dir", return_value=tmp_path):
            info = cache_info()
            assert info["file_count"] == 1
            assert info["total_bytes"] > 0
            assert "abc" in info["repos"]

    def test_clear_repo_cache(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        graph_dir = tmp_path / "graph"
        state_dir.mkdir()
        graph_dir.mkdir()
        (state_dir / "abc.json").write_text('{"x": 1}', encoding="utf-8")
        (graph_dir / "abc.json").write_text('{"g": 1}', encoding="utf-8")

        with patch("checkowners.state._base_dir", return_value=tmp_path):
            removed = clear_repo_cache(Path("/nonexistent"))
            assert removed == 0

    def test_purge_cache(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "test.json").write_text("data", encoding="utf-8")

        with patch("checkowners.state._base_dir", return_value=tmp_path):
            removed = purge_cache()
            assert removed >= 1
            assert not (state_dir / "test.json").exists()

    def test_evict_if_oversize_under_limit(self, tmp_path: Path) -> None:
        small = tmp_path / "small.json"
        small.write_text("{}", encoding="utf-8")
        with patch("checkowners.state._base_dir", return_value=tmp_path):
            evicted = evict_if_oversize()
            assert evicted == 0

    def test_evict_if_oversize_over_limit(self, tmp_path: Path) -> None:
        with patch("checkowners.state.MAX_CACHE_SIZE_BYTES", 100):
            for i in range(10):
                f = tmp_path / f"file_{i}.json"
                f.write_text(json.dumps({"i": i, "data": "x" * 20}), encoding="utf-8")
            with patch("checkowners.state._base_dir", return_value=tmp_path):
                evicted = evict_if_oversize()
                assert evicted > 0


class TestSchemaVersion:
    """Schema v4 contract checks."""

    def test_schema_version_is_4(self) -> None:
        assert SCHEMA_VERSION == 4

    def test_write_state_includes_model_version(self, repo: Path) -> None:
        with patch("checkowners.state._git_head_ref", return_value="abc123"):
            write_state(repo, _make_ownership())
        data = read_state(repo)
        assert data is not None
        assert data["model_version"] == "0.6.0"

    def test_write_graph_cache_includes_model_version(self, tmp_path: Path) -> None:
        from checkowners.state import _graph_cache_path

        with patch("checkowners.state._git_head_ref", return_value="abc123"):
            write_graph_cache(tmp_path, _NOW, {"nodes": [], "edges": []})
        raw = json.loads(_graph_cache_path(tmp_path).read_text(encoding="utf-8"))
        assert raw["model_version"] == "0.6.0"
        assert raw["analyzed_ref"] == "abc123"
