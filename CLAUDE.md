# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

checkOwners: a CODEOWNERS inference engine driven by git commit history with confidence scoring, knowledge graph, expertise decay detection, bus factor analysis, team topology inference, review load balancing, onboarding-path generation, drift detection, and CI integration. Pure-git, no-LLM. Python 3.11+, packaged with hatch, distributed on PyPI and as a composite GitHub Action.

## Commands

```bash
hatch run test                          # pytest with coverage (85%+ target)
hatch run test -- tests/test_analyze.py # single test file
hatch run test -- -k "test_name"        # single test by name
hatch run lint                          # ruff check + mypy --strict
hatch run fmt                           # ruff format
hatch build                             # sdist + wheel in dist/
```

CLI entry point is `checkowners` (Typer app in `checkowners/cli.py`):
`analyze`, `generate`, `print`, `validate`, `drift`, `notify`, `sync`,
`expertise`, `decay`, `graph`, `bus-factor`, `topology`, `balance`, `onboard`,
`trends`, `github-action`.
All subcommands support `--json` for structured JSON output (except `graph`, which
supports `--export dot`).

## Architecture

```
checkowners/
  cli.py            # Typer app; all subcommands + --version; progress bar;
                    # identity merging (emails -> @handles, bus factor recomputed)
  analyze.py        # git log + parallel git blame -> confidence-scored
                    # OwnershipMap; blames only min_commits-qualified paths;
                    # optional on_progress hook (console-free module)
  patterns.py       # CODEOWNERS pattern matcher: gitignore-style semantics,
                    # last matching rule wins; used by drift
  generate.py       # OwnershipMap -> CODEOWNERS writer; consolidates uniform
                    # directories into dir rules (output.consolidate); refuses
                    # to overwrite a hand-written file without --force
  drift.py          # Pattern-aware compare -> DriftResult with notes:
                    # missing (uncovered file), stale (rule matches no tracked
                    # file via git ls-files), changed (per-rule owner divergence)
  notify.py         # Webhook POST with severity gating; never raises on network
                    # errors; skips no-drift runs unless include_unchanged
  validate.py       # Syntax-only CODEOWNERS validator (GitHub's real rules:
                    # relative patterns + owner-less rules valid; ! and [] rejected)
  config.py         # PyYAML loader, CODEOWNERS path auto-detection
  state.py          # Per-repo state at ~/.checkowners/state/<repo-hash>.json
                    # (schema v3; repo path verified on load); handles.json
                    # email->@handle cache; graph cache
  expertise.py      # Per-path expertise ranking; shared path_matches_glob helper
  decay.py          # Expertise decay detector with transfer recommendations
  graph.py          # Knowledge graph builder (lazy networkx import; DOT-escaped)
  busfactor.py      # Bus factor calculator with backup-reviewer suggestions
  topology.py       # Team topology inference from commit co-occurrence
  balance.py        # PR review load balancer (GITHUB_REPOSITORY-scoped, bounded
                    # PR scan, fallback_reason when API path abandoned)
  onboard.py        # Onboarding path generator
  trends.py         # Historical ownership-confidence trends (per-period snapshots)
  github.py         # GitHub API: email->@handle mapping (noreply parsed locally,
                    # then disk cache, then search API), team resolution,
                    # per-path PR-review coverage (bounded closed-PR scan)
  models.py         # Dataclasses (OwnershipMap, PathOwnership, OwnerEntry,
                    # ConfidenceScore, DriftResult, DriftEntry, ExpertiseRank,
                    # TeamCluster, BusFactor, DecayWarning, all Config sections)
tests/
  conftest.py       # autouse fixture: CHECKOWNERS_STATE_DIR isolated per test
  test_<module>.py  # one per module; mocked subprocess; networkx-backed
action.yml          # Composite GitHub Action (shallow-clone guard, drift +
                    # bus-factor + decay, update-in-place PR comment)
```

Key data flow: `config.py` loads `.github/checkowners.yml` + auto-detects CODEOWNERS location -> `analyze.py` runs git log, prefilters to paths where an author reaches `min_commits`, runs git blame on a thread pool, and computes the four-factor confidence (recency, frequency, blame, review) -> `models.OwnershipMap` (PathOwnership with confidence-scored owners + bus_factor + decay_warnings) -> `cli._resolve_github_owners` maps emails to @handles (noreply parsing works with no token) and merges same-person identities -> `state.py` persists the inferred map per repo at `~/.checkowners/state/<repo-hash>.json` (and caches the serialized graph at `~/.checkowners/graph/<repo-hash>.json`) -> downstream commands (`drift`, `decay`, `bus-factor`, `topology`, `balance`, `onboard`) read state and emit per-domain reports. The review factor is populated by `github.build_review_coverage` only when `github.api_enabled` + a token + `GITHUB_REPOSITORY` are present; otherwise it is 0.0. `trends` is independent of state: it runs its own single `git log` pass to reconstruct per-period snapshots. CODEOWNERS auto-detected at: `.github/CODEOWNERS`, `CODEOWNERS` (root), or `docs/CODEOWNERS`.

## Conventions

- Functional style throughout; no classes except dataclasses in `models.py` (and small frozen dataclasses inside modules for return types)
- Type hints on every function signature; strict mypy (`--strict`)
- All file paths via `pathlib.Path`, never hardcoded strings
- All CLI commands support `--json` for structured JSON output
- Config file: `.github/checkowners.yml` (per-repo; loaded via `config.py`)
- State: per-repo file at `~/.checkowners/state/<repo-hash>.json`, schema v3 (auto-maintained; never hardcode the path, use `state.py`; override the base dir via `CHECKOWNERS_STATE_DIR` for tests)
- Every write to `.github/CODEOWNERS` must include: `# Generated by checkOwners. Do not edit manually.` Overwriting a file without that header requires `--force`
- Ownership is NOT binary; every path-owner pair carries a confidence score in `[0.0, 1.0]` clamped to the valid range
- Confidence score = weighted average of recency (exponential decay, default 90-day half-life), frequency (commits / path max), blame coverage (lines attributed / total lines), and review activity (PR reviews / total reviews; 0.0 when `github.api_enabled` is false)
- Default exclusions: `*.lock`, `package-lock.json`, `pnpm-lock.yaml`, `dist/**`, `vendor/**`, `node_modules/**`, `*.generated.*`, `*.min.js`, `*.min.css`, `*.map`, `*.whl`, plus the three CODEOWNERS locations (the generated file itself is never inferred)
- CODEOWNERS matching semantics live in `patterns.py` only; never string-compare patterns against file paths
- Config defaults: `lookback_days: 365`, `min_commits: 3`, `top_n_owners: 3`, `confidence_threshold: 0.3`
- Drift state machine modes: `commit`, `repo`, `both`
- Severity tiers (notify): low / medium / high / critical; computed from max confidence delta and bus factor / decay signals; gated by `notifications.severity_threshold`
- Expertise decay threshold: configurable, default 180 days since last commit to flag as decaying
- Bus factor tiers: `critical` (≤ `bus_factor.critical_threshold`, default 1), `warning` (≤ `bus_factor.warn_threshold`, default 2), `ok`
- Knowledge graph + onboarding may rely on `networkx`; declared as the optional `[graph]` extra and lazy-imported

## Testing

- Unit tests mock all subprocess calls (`git log`, `git blame`); never require a real git repo
- Every module has a corresponding `tests/test_<module>.py`
- `state.py` tests isolate `~/.checkowners` via the `CHECKOWNERS_STATE_DIR` env var
- Coverage target: 85%+ across the project

## Do NOT

- Add LLM or AI dependencies; checkOwners is pure-git inference only
- Write to `.github/CODEOWNERS` without the machine-generated header
- Make external network calls except in `github.py` (GitHub user/team lookup), `notify.py` (webhook POST), `balance.py` (GitHub PR review API), `topology.py` (GitHub teams API), and `action.yml` (Action runtime)
- Use classes except dataclasses
- Skip type annotations on any function signature
- Modify the state schema without bumping `state.SCHEMA_VERSION`
- Treat networkx as a hard dependency; it must stay opt-in via `pip install checkowners[graph]`
- Emit a confidence score < 0.0 or > 1.0; always clamp
- Treat PyGithub as a hard dependency for core inference; GitHub API features stay opt-in (`github.api_enabled`)
