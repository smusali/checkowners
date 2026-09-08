"""Git history analysis for confidence-scored ownership inference."""

from __future__ import annotations

import fnmatch
import math
import os
import subprocess
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from checkowners.models import (
    ConfidenceScore,
    Config,
    DecayWarning,
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
    ScoringConfig,
)

_COMMIT_SENTINEL = "COMMIT_START"

#: A review provider maps a set of contributor emails to per-path, per-email
#: review-coverage fractions (path -> {email: fraction in [0, 1]}). It is
#: injected so analyze.py itself stays free of network calls; the CLI supplies
#: a GitHub-backed implementation only when the API is enabled.
ReviewProvider = Callable[[set[str]], dict[str, dict[str, float]]]

#: Optional progress hook: called as on_progress(done, total) while blame runs.
#: Injected by the CLI to drive a progress bar; analyze.py stays console-free.
ProgressHook = Callable[[int, int], None]


@dataclass(frozen=True)
class _Contribution:
    """Raw per-(path, author) signal aggregated from git log."""

    commits: int
    last_commit: datetime


@dataclass(frozen=True)
class _RawCommit:
    """A single commit's parsed metadata."""

    author: str
    timestamp: datetime
    files: tuple[str, ...]


def analyze_ownership(
    repo_root: Path,
    config: Config,
    *,
    review_provider: ReviewProvider | None = None,
    on_progress: ProgressHook | None = None,
) -> OwnershipMap:
    """Analyze git history and return a confidence-scored ownership map.

    When ``review_provider`` is supplied, its per-path, per-email review
    coverage feeds the review factor of the confidence score; otherwise the
    review factor is 0.0 (the pure-git default). ``on_progress`` receives
    (done, total) updates while the per-file blame pass runs.
    """
    commits = _get_commit_history(repo_root, config.analysis.lookback_days)
    contributions = _aggregate_contributions(commits)
    contributions = _filter_excluded(contributions, config.paths.exclude)
    contributions = _filter_nonexistent(contributions, repo_root)
    if config.analysis.exclude_bots:
        contributions = _filter_bot_authors(contributions)
    contributions = _filter_unqualified(contributions, config.analysis.min_commits)
    blame_coverage = _gather_blame_coverage(
        contributions.keys(), repo_root, on_progress=on_progress
    )
    review_coverage = _gather_review_coverage(contributions, review_provider)
    now = datetime.now(UTC)
    paths = _build_path_ownerships(contributions, blame_coverage, review_coverage, config, now)
    return OwnershipMap(paths=paths, last_analyzed=now)


def _gather_review_coverage(
    contributions: dict[str, dict[str, _Contribution]],
    review_provider: ReviewProvider | None,
) -> dict[str, dict[str, float]]:
    """Collect per-path, per-email review coverage from the injected provider."""
    if review_provider is None:
        return {}
    emails = {author for authors in contributions.values() for author in authors}
    if not emails:
        return {}
    return review_provider(emails)


def _build_path_ownerships(
    contributions: dict[str, dict[str, _Contribution]],
    blame_coverage: dict[str, dict[str, float]],
    review_coverage: dict[str, dict[str, float]],
    config: Config,
    now: datetime,
) -> dict[str, PathOwnership]:
    """Compute confidence-scored owners + bus factor + decay per path."""
    result: dict[str, PathOwnership] = {}
    for path, authors in contributions.items():
        qualified = {
            author: contrib
            for author, contrib in authors.items()
            if contrib.commits >= config.analysis.min_commits
        }
        if not qualified:
            continue
        max_commits = max(c.commits for c in qualified.values())
        path_blame = blame_coverage.get(path, {})
        path_review = review_coverage.get(path, {})
        entries = _score_owners(
            qualified, path_blame, path_review, max_commits, config.scoring, now
        )
        filtered = tuple(e for e in entries if e.confidence >= config.analysis.confidence_threshold)
        if not filtered:
            continue
        top = filtered[: config.analysis.top_n_owners]
        decay = _detect_decay(path, qualified, top, config.decay.threshold_days, now)
        bus_factor = _compute_bus_factor(top, config.analysis.confidence_threshold)
        result[path] = PathOwnership(owners=top, bus_factor=bus_factor, decay_warnings=decay)
    return result


def _score_owners(
    qualified: dict[str, _Contribution],
    path_blame: dict[str, float],
    path_review: dict[str, float],
    max_commits: int,
    scoring: ScoringConfig,
    now: datetime,
) -> tuple[OwnerEntry, ...]:
    scored: list[OwnerEntry] = []
    for author, contrib in qualified.items():
        recency = _recency_score(contrib.last_commit, now, scoring.recency_half_life_days)
        frequency = _frequency_score(contrib.commits, max_commits)
        blame = path_blame.get(author, 0.0)
        review = _clamp(path_review.get(author, 0.0))
        total = _clamp(
            scoring.recency_weight * recency
            + scoring.frequency_weight * frequency
            + scoring.blame_weight * blame
            + scoring.review_weight * review
        )
        breakdown = ConfidenceScore(
            total=total,
            recency=recency,
            frequency=frequency,
            blame=blame,
            review=review,
        )
        scored.append(
            OwnerEntry(
                handle=author,
                confidence=total,
                last_commit=contrib.last_commit,
                commits=contrib.commits,
                score_breakdown=breakdown,
            )
        )
    scored.sort(key=lambda e: (-e.confidence, e.handle))
    return tuple(scored)


def _recency_score(last_commit: datetime, now: datetime, half_life_days: int) -> float:
    if half_life_days <= 0:
        return 1.0
    delta_days = max(0.0, (now - last_commit).total_seconds() / 86400.0)
    return _clamp(math.pow(0.5, delta_days / half_life_days))


def _frequency_score(commits: int, max_commits: int) -> float:
    if max_commits <= 0:
        return 0.0
    return _clamp(commits / max_commits)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _detect_decay(
    path: str,
    qualified: dict[str, _Contribution],
    top: tuple[OwnerEntry, ...],
    threshold_days: int,
    now: datetime,
) -> tuple[DecayWarning, ...]:
    warnings: list[DecayWarning] = []
    for entry in top:
        contrib = qualified.get(entry.handle)
        if contrib is None:
            continue
        days = int((now - contrib.last_commit).total_seconds() // 86400)
        if days > threshold_days:
            warnings.append(
                DecayWarning(
                    handle=entry.handle,
                    path=path,
                    last_commit=contrib.last_commit,
                    days_since_last_commit=days,
                    historical_confidence=entry.confidence,
                )
            )
    return tuple(warnings)


def _compute_bus_factor(top: tuple[OwnerEntry, ...], threshold: float) -> int:
    return sum(1 for entry in top if entry.confidence >= threshold)


def _get_commit_history(repo_root: Path, since_days: int) -> list[_RawCommit]:
    """Run git log and parse (author, timestamp, files) triples."""
    result = subprocess.run(
        [
            "git",
            "log",
            f"--format={_COMMIT_SENTINEL}%n%ae%n%cI",
            "--name-only",
            f"--since={since_days} days ago",
        ],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
        check=True,
    )
    return _parse_log_output(result.stdout)


def _parse_log_output(stdout: str) -> list[_RawCommit]:
    if not stdout.strip():
        return []
    chunks = stdout.split(_COMMIT_SENTINEL)
    commits: list[_RawCommit] = []
    for chunk in chunks:
        lines = [line for line in chunk.splitlines() if line.strip()]
        if len(lines) < 2:
            continue
        author = lines[0].strip()
        timestamp = _parse_timestamp(lines[1].strip())
        if timestamp is None:
            continue
        files = tuple(line for line in lines[2:] if line)
        if files:
            commits.append(_RawCommit(author=author, timestamp=timestamp, files=files))
    return commits


def _parse_timestamp(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _aggregate_contributions(
    commits: list[_RawCommit],
) -> dict[str, dict[str, _Contribution]]:
    """Aggregate per-(path, author) commit counts and most-recent commit time."""
    counts: dict[str, dict[str, int]] = {}
    latest: dict[str, dict[str, datetime]] = {}
    for commit in commits:
        for file_path in commit.files:
            counts.setdefault(file_path, {})
            latest.setdefault(file_path, {})
            counts[file_path][commit.author] = counts[file_path].get(commit.author, 0) + 1
            prior = latest[file_path].get(commit.author)
            if prior is None or commit.timestamp > prior:
                latest[file_path][commit.author] = commit.timestamp
    result: dict[str, dict[str, _Contribution]] = {}
    for path, authors in counts.items():
        result[path] = {
            author: _Contribution(commits=commits_n, last_commit=latest[path][author])
            for author, commits_n in authors.items()
        }
    return result


def _filter_excluded(
    contributions: dict[str, dict[str, _Contribution]],
    exclude_patterns: tuple[str, ...],
) -> dict[str, dict[str, _Contribution]]:
    return {
        path: authors
        for path, authors in contributions.items()
        if not _is_excluded(path, exclude_patterns)
    }


def _filter_nonexistent(
    contributions: dict[str, dict[str, _Contribution]],
    repo_root: Path,
) -> dict[str, dict[str, _Contribution]]:
    return {path: authors for path, authors in contributions.items() if (repo_root / path).exists()}


def _is_bot_email(email: str) -> bool:
    """True for automation authors (GitHub Apps sign as `name[bot]@...`)."""
    lowered = email.lower()
    return "[bot]" in lowered or lowered == "actions@github.com" or lowered.startswith("bot@")


def _filter_bot_authors(
    contributions: dict[str, dict[str, _Contribution]],
) -> dict[str, dict[str, _Contribution]]:
    result: dict[str, dict[str, _Contribution]] = {}
    for path, authors in contributions.items():
        humans = {a: c for a, c in authors.items() if not _is_bot_email(a)}
        if humans:
            result[path] = humans
    return result


def _filter_unqualified(
    contributions: dict[str, dict[str, _Contribution]],
    min_commits: int,
) -> dict[str, dict[str, _Contribution]]:
    """Drop paths where no author reaches min_commits.

    Those paths can never produce owners, so filtering them before the blame
    pass avoids running git blame on files that would be discarded anyway
    (on active monorepos this cuts the blame workload several-fold).
    """
    return {
        path: authors
        for path, authors in contributions.items()
        if any(contrib.commits >= min_commits for contrib in authors.values())
    }


def _is_excluded(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def _gather_blame_coverage(
    paths: Iterable[str],
    repo_root: Path,
    *,
    on_progress: ProgressHook | None = None,
) -> dict[str, dict[str, float]]:
    """Run git blame per path (in parallel) and return author -> coverage.

    git blame is a subprocess, so threads parallelize cleanly; the worker
    count tracks the CPU count since blame is compute-bound inside git.
    """
    path_list = list(paths)
    total = len(path_list)
    if total == 0:
        return {}
    coverage: dict[str, dict[str, float]] = {}
    workers = min(32, os.cpu_count() or 4)
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for path, per_author in zip(
            path_list, pool.map(lambda p: _blame_for_path(repo_root, p), path_list), strict=True
        ):
            done += 1
            if on_progress is not None:
                on_progress(done, total)
            if per_author:
                coverage[path] = per_author
    return coverage


def _blame_for_path(repo_root: Path, path: str) -> dict[str, float]:
    """Run `git blame --line-porcelain` on a single path; return coverage fractions."""
    try:
        result = subprocess.run(
            ["git", "blame", "--line-porcelain", "--", path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(repo_root),
            check=True,
        )
    except subprocess.CalledProcessError:
        return {}
    return _parse_blame_output(result.stdout)


def _parse_blame_output(stdout: str) -> dict[str, float]:
    counts: dict[str, int] = {}
    total = 0
    for line in stdout.splitlines():
        if line.startswith("author-mail "):
            email = line[len("author-mail ") :].strip().strip("<>")
            counts[email] = counts.get(email, 0) + 1
            total += 1
    if total == 0:
        return {}
    return {author: count / total for author, count in counts.items()}
