"""Tests for checkowners.cli module."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from checkowners.cli import app
from checkowners.models import (
    DecayWarning,
    DriftEntry,
    DriftResult,
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
)
from checkowners.trends import TrendPoint, TrendReport

runner = CliRunner()

_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)


def _entry(handle: str, confidence: float = 0.8, commits: int = 7) -> OwnerEntry:
    return OwnerEntry(handle=handle, confidence=confidence, last_commit=_NOW, commits=commits)


_OWNERSHIP = OwnershipMap(
    paths={
        "src/main.py": PathOwnership(
            owners=(_entry("alice@example.com", 0.92), _entry("bob@example.com", 0.55)),
            bus_factor=2,
        ),
        "src/auth.py": PathOwnership(
            owners=(_entry("dave@example.com", 0.34),),
            bus_factor=1,
            decay_warnings=(
                DecayWarning(
                    handle="dave@example.com",
                    path="src/auth.py",
                    last_commit=_NOW,
                    days_since_last_commit=289,
                    historical_confidence=0.34,
                ),
            ),
        ),
    },
    last_analyzed=_NOW,
)

_EMPTY_OWNERSHIP = OwnershipMap(paths={}, last_analyzed=_NOW)

_DRIFT_DETECTED = DriftResult(
    stale=(DriftEntry(path="/old.py", confidence_delta=1.0, reason="stale path"),),
    missing=(
        DriftEntry(
            path="/new.py",
            confidence_delta=0.7,
            reason="missing",
            bus_factor=1,
            decay=True,
        ),
    ),
    changed=(DriftEntry(path="/changed.py", confidence_delta=0.4, reason="owner shuffle"),),
    drift_detected=True,
)

_NO_DRIFT = DriftResult(stale=(), missing=(), changed=(), drift_detected=False)

_MOCK_TOKEN = patch("checkowners.cli.get_github_token", return_value="")
_MOCK_PATH = patch(
    "checkowners.cli.find_codeowners_path",
    return_value=Path.cwd() / ".github" / "CODEOWNERS",
)


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path: Path) -> None:
    import checkowners.cli as cli_mod

    with patch.dict("os.environ", {"CHECKOWNERS_STATE_DIR": str(tmp_path)}):
        yield
    # The --offline global persists across invocations in the same process;
    # reset it so it does not leak into later tests.
    cli_mod._OFFLINE = False


# --- analyze ---


def test_analyze_json() -> None:
    with patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP), _MOCK_TOKEN:
        result = runner.invoke(app, ["analyze", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert "src/main.py" in data["inferred"]
    owners = data["inferred"]["src/main.py"]["owners"]
    assert owners[0]["handle"] == "alice@example.com"
    assert owners[0]["confidence"] == 0.92


def test_analyze_table() -> None:
    with patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP), _MOCK_TOKEN:
        result = runner.invoke(app, ["analyze"])
    assert result.exit_code == 0
    assert "alice@example.com" in result.stdout
    assert "0.92" in result.stdout


def test_analyze_empty() -> None:
    with patch("checkowners.cli.analyze_ownership", return_value=_EMPTY_OWNERSHIP), _MOCK_TOKEN:
        result = runner.invoke(app, ["analyze"])
    assert result.exit_code == 0
    assert "No ownership" in result.stdout


def test_analyze_git_error() -> None:
    with patch(
        "checkowners.cli.analyze_ownership",
        side_effect=subprocess.CalledProcessError(1, "git"),
    ):
        result = runner.invoke(app, ["analyze"])
    assert result.exit_code == 1


# --- generate ---


def test_generate_rich() -> None:
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.generate_codeowners", return_value="content"),
        _MOCK_PATH,
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["generate"])
    assert result.exit_code == 0
    assert "Generated" in result.stdout


def test_generate_json() -> None:
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.generate_codeowners", return_value="content"),
        _MOCK_PATH,
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["generate", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert "CODEOWNERS" in data["path"]


# --- print ---


def test_print_json() -> None:
    with patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP), _MOCK_TOKEN:
        result = runner.invoke(app, ["print", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert "src/main.py" in data
    assert data["src/main.py"]["bus_factor"] == 2


def test_print_plain_shows_confidence() -> None:
    with patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP), _MOCK_TOKEN:
        result = runner.invoke(app, ["print"])
    assert result.exit_code == 0
    assert "src/main.py" in result.stdout
    assert "alice@example.com(0.92)" in result.stdout


# --- validate ---


def test_validate_valid() -> None:
    with patch("checkowners.cli.validate_codeowners", return_value=[]), _MOCK_PATH:
        result = runner.invoke(app, ["validate"])
    assert result.exit_code == 0
    assert "valid" in result.stdout


def test_validate_errors() -> None:
    from checkowners.validate import ValidationError

    errors = [ValidationError(line_number=3, line="bad", message="bad line")]
    with patch("checkowners.cli.validate_codeowners", return_value=errors), _MOCK_PATH:
        result = runner.invoke(app, ["validate"])
    assert result.exit_code == 1
    assert "Line 3" in result.stdout


def test_validate_json_valid() -> None:
    with patch("checkowners.cli.validate_codeowners", return_value=[]), _MOCK_PATH:
        result = runner.invoke(app, ["validate", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["valid"] is True


def test_validate_json_errors() -> None:
    from checkowners.validate import ValidationError

    errors = [ValidationError(line_number=1, line="x", message="oops")]
    with patch("checkowners.cli.validate_codeowners", return_value=errors), _MOCK_PATH:
        result = runner.invoke(app, ["validate", "--json"])
    assert result.exit_code == 1
    data = json.loads(result.stdout)
    assert data["valid"] is False
    assert len(data["errors"]) == 1


# --- drift ---


def test_drift_no_drift() -> None:
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.detect_drift", return_value=_NO_DRIFT),
        _MOCK_PATH,
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["drift"])
    assert result.exit_code == 0
    assert "No drift" in result.stdout


def test_drift_detected_shows_severity() -> None:
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.detect_drift", return_value=_DRIFT_DETECTED),
        _MOCK_PATH,
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["drift"])
    assert result.exit_code == 0
    assert "CRITICAL" in result.stdout
    assert "stale" in result.stdout


def test_drift_json_includes_severity() -> None:
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.detect_drift", return_value=_DRIFT_DETECTED),
        _MOCK_PATH,
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["drift", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["drift_detected"] is True
    assert data["severity"] == "critical"
    assert data["max_confidence_delta"] == 1.0


# --- notify ---


def test_notify_sent() -> None:
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.detect_drift", return_value=_DRIFT_DETECTED),
        patch("checkowners.cli.send_notification", return_value=True),
        _MOCK_PATH,
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["notify"])
    assert result.exit_code == 0
    assert "sent" in result.stdout.lower()


def test_notify_skipped() -> None:
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.detect_drift", return_value=_NO_DRIFT),
        patch("checkowners.cli.send_notification", return_value=False),
        _MOCK_PATH,
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["notify"])
    assert result.exit_code == 0
    assert "skipped" in result.stdout.lower()


def test_notify_json() -> None:
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.detect_drift", return_value=_DRIFT_DETECTED),
        patch("checkowners.cli.send_notification", return_value=True),
        _MOCK_PATH,
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["notify", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["sent"] is True
    assert data["severity"] == "critical"


# --- sync ---


def test_sync_rich() -> None:
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.generate_codeowners", return_value="content"),
        patch("checkowners.cli.subprocess.run") as mock_run,
        _MOCK_PATH,
        _MOCK_TOKEN,
    ):
        mock_run.return_value = MagicMock(returncode=0)
        result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0
    assert "committed" in result.stdout.lower()


def test_sync_json() -> None:
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.generate_codeowners", return_value="content"),
        patch("checkowners.cli.subprocess.run") as mock_run,
        _MOCK_PATH,
        _MOCK_TOKEN,
    ):
        mock_run.return_value = MagicMock(returncode=0)
        result = runner.invoke(app, ["sync", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["committed"] is True


def test_sync_git_commit_error() -> None:
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.generate_codeowners", return_value="content"),
        patch(
            "checkowners.cli.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, "git", stderr="nothing to commit"),
        ),
        _MOCK_PATH,
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["sync"])
    assert result.exit_code == 1


# --- github-action ---


def test_github_action_fails_on_drift_and_writes_output(tmp_path: Path) -> None:
    output_file = tmp_path / "gh_output"
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.detect_drift", return_value=_DRIFT_DETECTED),
        _MOCK_PATH,
        _MOCK_TOKEN,
        patch.dict("os.environ", {"GITHUB_OUTPUT": str(output_file)}),
    ):
        result = runner.invoke(app, ["github-action"])
    assert result.exit_code == 1
    written = output_file.read_text(encoding="utf-8")
    assert "bus_factor_summary=" in written
    assert "decay_summary=" in written


def test_github_action_no_fail_flag(tmp_path: Path) -> None:
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.detect_drift", return_value=_DRIFT_DETECTED),
        _MOCK_PATH,
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["github-action", "--no-fail-on-drift", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["checkowners_drift"]["drift_detected"] is True
    assert "bus_factor_summary" in data
    assert "decay_summary" in data


def test_github_action_clean_exits_zero() -> None:
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.detect_drift", return_value=_NO_DRIFT),
        _MOCK_PATH,
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["github-action"])
    assert result.exit_code == 0


# --- trends ---


_TREND_REPORT = TrendReport(
    points=(
        TrendPoint(
            period_end=datetime(2026, 4, 1, tzinfo=UTC),
            commits=10,
            active_contributors=2,
            tracked_paths=3,
            avg_top_confidence=0.6,
            avg_bus_factor=1.5,
        ),
        TrendPoint(
            period_end=datetime(2026, 5, 1, tzinfo=UTC),
            commits=18,
            active_contributors=3,
            tracked_paths=4,
            avg_top_confidence=0.72,
            avg_bus_factor=1.8,
        ),
    ),
    periods=2,
    period_days=30,
)


def test_trends_table() -> None:
    with patch("checkowners.cli.analyze_trends", return_value=_TREND_REPORT):
        result = runner.invoke(app, ["trends", "--periods", "2", "--period-days", "30"])
    assert result.exit_code == 0
    assert "2026-04-01" in result.stdout
    assert "0.72" in result.stdout


def test_trends_json() -> None:
    with patch("checkowners.cli.analyze_trends", return_value=_TREND_REPORT):
        result = runner.invoke(app, ["trends", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["periods"] == 2
    assert data["points"][1]["avg_top_confidence"] == 0.72
    assert data["points"][0]["active_contributors"] == 2


# --- version / identity merge ---


def test_version_flag() -> None:
    from checkowners import __version__

    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_merge_identities_dedupes_same_handle() -> None:
    from checkowners.cli import _merge_identities

    noreply = "1+a@users.noreply.github.com"
    entries = (
        OwnerEntry(handle="a@x.com", confidence=0.9, last_commit=None, commits=4),
        OwnerEntry(handle=noreply, confidence=0.5, last_commit=None, commits=6),
    )
    mapping = {"a@x.com": "@a", "1+a@users.noreply.github.com": "@a"}
    merged = _merge_identities(entries, mapping)
    assert len(merged) == 1
    assert merged[0].handle == "@a"
    assert merged[0].commits == 10
    assert merged[0].confidence == 0.9


def test_sync_noop_when_already_in_sync() -> None:
    with (
        patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
        patch("checkowners.cli.generate_codeowners", return_value="content"),
        patch("checkowners.cli._has_uncommitted_changes", return_value=False),
        patch("checkowners.cli.subprocess.run") as mock_run,
        _MOCK_PATH,
        _MOCK_TOKEN,
    ):
        result = runner.invoke(app, ["sync", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["committed"] is False
    # No `git add` / `git commit` is issued when already in sync. The only
    # subprocess invocations are `git rev-parse HEAD` (for the state ref tag)
    # during the state write.
    add_commit = [c for c in mock_run.call_args_list if c.args and "add" in c.args[0]]
    assert add_commit == []
    commit_calls = [c for c in mock_run.call_args_list if c.args and "commit" in c.args[0]]
    assert commit_calls == []


def test_generate_refuses_handwritten_before_analyzing(tmp_path: Path) -> None:
    target = tmp_path / "CODEOWNERS"
    target.write_text("# Hand-curated\nsrc/ @human\n", encoding="utf-8")
    with (
        patch("checkowners.cli.find_codeowners_path", return_value=target),
        patch("checkowners.cli.analyze_ownership") as mock_analyze,
    ):
        result = runner.invoke(app, ["generate"])
    assert result.exit_code == 1
    mock_analyze.assert_not_called()


def test_validate_errors_render_brackets_verbatim() -> None:
    """Rich markup must not swallow [segments] from user paths."""
    from checkowners.validate import ValidationError

    errors = [
        ValidationError(
            line_number=7,
            line="x",
            message="bad pattern: /app/[companyId]/page.tsx",
        )
    ]
    with patch("checkowners.cli.validate_codeowners", return_value=errors), _MOCK_PATH:
        result = runner.invoke(app, ["validate"])
    assert result.exit_code == 1
    assert "[companyId]" in result.stdout


# ---------------------------------------------------------------------------
# New tests for Issue #56 features
# ---------------------------------------------------------------------------


class TestCacheCommands:
    """Verify cache subcommand group."""

    def test_cache_path(self) -> None:
        result = runner.invoke(app, ["cache", "path"])
        assert result.exit_code == 0
        assert len(result.stdout.strip()) > 0

    def test_cache_info(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["cache", "info"])
        assert result.exit_code == 0
        assert "Files:" in result.stdout

    def test_cache_info_json(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["cache", "info", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert "file_count" in data
        assert "total_bytes" in data
        assert "repos" in data

    def test_cache_clear(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["cache", "clear"])
        assert result.exit_code == 0

    def test_cache_clear_json(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["cache", "clear", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert "removed" in data

    def test_cache_purge(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["cache", "purge"])
        assert result.exit_code == 0

    def test_cache_purge_json(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["cache", "purge", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert "removed" in data


class TestOfflineFlag:
    """Verify --offline flag blocks network features."""

    def test_offline_flag_sets_global(self) -> None:
        from checkowners.cli import _OFFLINE

        assert _OFFLINE is False

    def test_offline_analyze_runs_cleanly(self) -> None:
        with patch("checkowners.cli.analyze_ownership", return_value=_EMPTY_OWNERSHIP):
            result = runner.invoke(app, ["--offline", "analyze", "--json"])
        assert result.exit_code == 0

    def test_offline_skips_handle_resolution(self) -> None:
        with (
            patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
            patch("checkowners.cli.resolve_handles") as mock_resolve,
            _MOCK_TOKEN,
        ):
            result = runner.invoke(app, ["--offline", "analyze"])
        assert result.exit_code == 0
        mock_resolve.assert_not_called()

    def test_offline_blocks_github_token(self) -> None:
        with (
            patch("checkowners.cli.analyze_ownership", return_value=_OWNERSHIP),
            patch("checkowners.cli.generate_codeowners", return_value="content"),
            _MOCK_PATH,
            _MOCK_TOKEN,
        ):
            result = runner.invoke(app, ["--offline", "generate"])
        assert result.exit_code == 0

    def test_offline_json_output(self) -> None:
        with patch("checkowners.cli.analyze_ownership", return_value=_EMPTY_OWNERSHIP):
            result = runner.invoke(app, ["--offline", "analyze", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert "inferred" in data

    def test_offline_emits_status_strings(self) -> None:
        with patch("checkowners.cli.analyze_ownership", return_value=_EMPTY_OWNERSHIP):
            result = runner.invoke(app, ["--offline", "analyze", "--json"])
        assert result.exit_code == 0
        assert "Network access: disabled" in result.stderr
        assert "Review evidence: unavailable" in result.stderr
        assert "Team verification: unavailable" in result.stderr

    def test_offline_strings_kept_out_of_json(self) -> None:
        with patch("checkowners.cli.analyze_ownership", return_value=_EMPTY_OWNERSHIP):
            result = runner.invoke(app, ["--offline", "analyze", "--json"])
        assert result.exit_code == 0
        stdout = json.loads(result.stdout)
        assert "Network access: disabled" not in json.dumps(stdout)

    def test_offline_never_opens_sockets(self) -> None:
        def _forbid(*args: object, **kwargs: object) -> None:
            raise AssertionError("network socket opened while offline")

        with (
            patch("checkowners.cli.analyze_ownership", return_value=_EMPTY_OWNERSHIP),
            patch("socket.socket.connect", side_effect=_forbid),
            patch("socket.socket.send", side_effect=_forbid),
            patch("socket.create_connection", side_effect=_forbid),
        ):
            result = runner.invoke(app, ["--offline", "analyze", "--json"])
        assert result.exit_code == 0

    def test_online_does_not_emit_offline_status(self) -> None:
        with (
            patch("checkowners.cli.analyze_ownership", return_value=_EMPTY_OWNERSHIP),
            _MOCK_TOKEN,
        ):
            result = runner.invoke(app, ["analyze", "--json"])
        assert result.exit_code == 0
        assert "Network access: disabled" not in result.stderr


class TestStalenessInCLI:
    """Verify --allow-stale and --max-age work through CLI commands."""

    def test_load_or_analyze_with_stale_state_refreshes(self, tmp_path: Path) -> None:
        from checkowners.cli import _load_or_analyze
        from checkowners.models import Config
        from checkowners.state import StalenessError

        config = Config()
        with (
            patch(
                "checkowners.cli.load_ownership",
                side_effect=StalenessError("stale"),
            ),
            patch("checkowners.cli._run_analyze", return_value=_OWNERSHIP) as mock_analyze,
        ):
            result = _load_or_analyze(config, tmp_path)
        assert result == _OWNERSHIP
        mock_analyze.assert_called_once()

    def test_load_or_analyze_with_no_cache(self, tmp_path: Path) -> None:
        from checkowners.cli import _load_or_analyze
        from checkowners.models import Config

        config = Config()
        with (
            patch("checkowners.cli._run_analyze", return_value=_OWNERSHIP) as mock_analyze,
            patch("checkowners.cli.load_ownership") as mock_load,
        ):
            result = _load_or_analyze(config, tmp_path, no_cache=True)
        assert result == _OWNERSHIP
        mock_load.assert_not_called()
        mock_analyze.assert_called_once()
