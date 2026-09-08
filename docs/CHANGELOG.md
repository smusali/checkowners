# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Each dated heading is the UTC calendar day that version was published to PyPI (`YYYY-MM-DD`). `Unreleased` has no date.

## [Unreleased]

### Added
- The publish workflow rejects a GitHub Release whose tag has no dated changelog heading.
- The composite Action installs the `checkowners` version that matches its
  own tag from a committed wheel and a hashed `requirements.lock`. New
  `index_url` and `offline` inputs support internal mirrors and air-gapped
  runners. `install_spec` is limited to a local extras allowlist.
- CI and the publish job fail when the Action pin, package version, wheel,
  or lockfile disagree with each other (or with the release tag).

### Fixed
- The composite Action no longer installs an unpinned latest package from
  PyPI. `uses: smusali/checkowners@vX.Y.Z` now installs `checkowners==X.Y.Z`.
- README documentation and license links are now absolute GitHub URLs so they
  resolve on the PyPI project page.
- The composite Action exports `GITHUB_TOKEN` on every CLI step via a
  `github_token` input (default `${{ github.token }}`). The 0.5.0 claim that
  installing the `github` extra made handle resolution work in CI was
  incomplete: the extra was installed, but the token was never passed, so
  only local noreply parsing worked. When `github.api_enabled` is true and
  no token is reachable, the CLI now prints a warning naming `GITHUB_TOKEN`
  instead of degrading silently.

### Changed
- Project URLs, documentation clone and issue links, the Actions example,
  and the security advisory link now point at this repository.
- Changelog version dates are the PyPI publication day; the 0.3.0 and 0.5.0
  headings now match those uploads.

## [0.5.0] - 2026-07-04

Hardening release driven by testing every command against a real production
monorepo (24k commits, 12k active files). Focus: correct CODEOWNERS
semantics, analyze performance, per-repo state isolation, and identity
resolution.

### Added
- CODEOWNERS pattern matching engine (`checkowners/patterns.py`) with
  GitHub's documented gitignore-style semantics: `*` stays within a path
  segment, `**` crosses segments, leading or interior `/` anchors to the repo
  root, trailing `/` matches directory contents, `dir/*` matches direct
  children only, and the last matching rule wins.
- `generate` consolidates per-file inference into directory-level rules when
  every inferred file under a directory shares one owner set (`/src/ @alice`
  instead of hundreds of per-file lines). New files under a consolidated
  directory now match a rule. Disable with `output.consolidate: false`.
- `generate` and `sync` refuse to overwrite a hand-written CODEOWNERS (one
  without the machine-generated header) unless `--force` is passed.
- `--version` flag; running bare `checkowners` now prints help instead of a
  usage error.
- Progress bar (stderr, TTY-only) during the analyze blame pass.
- Local GitHub-noreply email resolution: `12345+login@users.noreply.github.com`
  and `login@users.noreply.github.com` map to `@login` with zero API calls
  and no token.
- Persistent email-to-handle cache at `<state-dir>/handles.json`, including
  remembered misses, so the rate-limited user-search API is only queried once
  per new email.
- Identity merging: when several commit emails resolve to one GitHub handle,
  their owner entries merge and per-path bus factor is recomputed over
  distinct people, so one person with two emails no longer reads as a bus
  factor of two. Decay warnings are remapped to the resolved handle.
- Drift results carry `notes` explaining skipped comparisons (raw emails vs
  @handles, team-owned rules) instead of emitting false drift.
- `balance` reports `fallback_reason` when the GitHub API path was abandoned,
  and labels the load column "Commits (proxy)" when counting authorship.

### Changed
- **Drift detection is now pattern-aware.** Inferred files are matched
  against CODEOWNERS rules with real matching semantics; previously the
  comparison was literal string equality, so directory rules like
  `frontend/` never matched inferred file paths and real-world files
  produced near-100% false "missing"/"stale" reports. New categories:
  `missing` = file no rule covers, `stale` = rule matching no tracked file
  (via `git ls-files`), `changed` = per-rule owner divergence, aggregated and
  ranked by worst per-file delta. Owner comparison is case-insensitive.
- **State is keyed per repo** (schema v3) at
  `~/.checkowners/state/<repo-hash>.json` with the absolute repo path
  embedded and verified on load. Previously a single global `state.json`
  meant analyzing repo A then running `decay` in repo B silently reused repo
  A's data.
- **Analyze is parallel and skips unowned files.** git blame now runs on a
  thread pool sized to the CPU count and only on paths where at least one
  author reaches `min_commits` (4-5x fewer files on a real monorepo).
  Combined effect on a 24k-commit production repo: 80+ minutes to under 3
  minutes.
- `validate` follows GitHub's actual CODEOWNERS rules: relative patterns
  (`docs/`, `apps/*`, `frontend/package.json`) are valid, owner-less rules
  (GitHub's documented exemption mechanism) are valid, escaped spaces are
  parsed, and `!` negation / `[...]` character ranges are correctly rejected.
  It previously demanded every pattern start with `/` or `*`, failing
  perfectly valid real-world files. Handle validation now matches GitHub's
  login rules (no dots, max 39 chars).
- `validate --json` exits non-zero on an invalid file, matching the
  human-readable mode.
- `sync` is a no-op success when the generated file matches the committed one
  ("already in sync"); it previously failed with an empty error because git
  prints "nothing to commit" on stdout.
- Downstream commands print a stderr hint when reusing cached state.
- `notifications.include_unchanged` now means "also notify when no drift was
  detected"; without it, no-drift runs no longer fire webhooks.
- Severity's critical signal honors `bus_factor.critical_threshold` instead
  of a hardcoded 1.
- Review-coverage and balance GitHub scans are bounded to the 200 most
  recently updated closed PRs and scoped to `GITHUB_REPOSITORY`; previously
  unbounded scans exhausted the API rate limit on mature repos.
- `trends` counts distinct commits per period; a commit touching 12 files
  previously counted 12 times.
- `bus-factor`, `expertise`, and `onboard` share one glob semantic
  (previously the same pattern matched different path sets per command).
- Backup-reviewer suggestions fall back to repo-wide top owners for
  root-level files.
- Onboarding steps never label a `bus_factor<=1` path "easy".
- Default exclusions now also cover `package-lock.json`, `pnpm-lock.yaml`,
  `*.min.js`, `*.min.css`, `*.map`, and the CODEOWNERS file itself (a sync
  commit would otherwise make whoever runs the tool its inferred owner,
  perturbing every subsequent run).
- Composite Action: fails fast with a clear error on shallow clones
  (`fetch-depth: 0` guidance), installs the `github` extra so handle
  resolution works in CI, and the PR comment is updated in place (one
  managed comment per PR, marked resolved when drift clears) instead of
  posting a new comment on every push.
- PyGithub moved from a hard dependency to the `github` extra (with an
  `all` convenience extra); core inference is pure git. Unused GitPython
  dependency dropped.
- graph DOT export escapes quotes and backslashes in node IDs and labels.

### Removed
- `drift.compare_to` config option: it was parsed and documented but never
  read by any logic. Configs still containing it are ignored, not rejected.
- Dead internal API surface orphaned by the identity-merge rework:
  `OwnershipMap.handles_only()` and `github.map_owners()`.

### Fixed
- Generated CODEOWNERS never emits `[...]` character ranges: bracket-bearing
  path segments (Next.js dynamic routes like `[companyId]`) become the valid
  `*` wildcard, colliding patterns merge their owners, and literal spaces in
  patterns are backslash-escaped. GitHub ignores lines with `[...]`, which
  silently un-owned those paths. Found by dogfooding against a production
  Next.js monorepo.
- Terminal output renders paths like `[companyId]` verbatim: user-derived
  text (paths, reasons, handles) is markup-escaped so Rich no longer swallows
  bracket segments as style tags.
- Webhook notifications no longer crash the CLI on HTTP or network errors;
  failures log a warning and `notify` reports `sent: false`.
- Rebalance suggestions can no longer propose shifting reviews onto another
  overloaded reviewer.
- Topology reports one mismatch line per overlapping declared team instead of
  only the first.
- Tests never touch the developer's real `~/.checkowners` (isolated state
  dir fixture).

## [0.4.0] - 2026-06-14

### Added
- `checkowners github-action`: runs the full CI flow (`analyze` -> `drift` -> `bus-factor` -> `decay`) in one command, writes the `checkowners_drift`, `bus_factor_summary`, and `decay_summary` keys to `GITHUB_OUTPUT`, and exits non-zero on drift by default (`--no-fail-on-drift` to override).
- `checkowners trends [--periods N] [--period-days D]`: reconstructs the ownership snapshot at the end of each of the last N periods from a single `git log` pass and reports commits, active contributors, tracked paths, average top-owner confidence, and average bus factor over time.
- Review-activity factor of the confidence score is now populated (it was previously always 0.0). When `github.api_enabled`, a token resolves, and `GITHUB_REPOSITORY` is set, closed-PR reviews are aggregated per changed file and folded into the score; the factor stays 0.0 otherwise.
- Serialized knowledge-graph cache at `~/.checkowners/graph/<repo-hash>.json`, keyed by repo and invalidated by the analysis timestamp; the `graph` command reuses a fresh cache.
- Composite Action posts a built-in drift + bus-factor PR comment on pull requests (`comment_on_pr` input, default `true`).
- `docs/` directory housing detailed reference: `USAGE.md`, `FAQ.md`, `CONTRIBUTING.md`, this `CHANGELOG.md`, and the project `CODEOWNERS` (moved from `.github/CODEOWNERS`).

### Changed
- README drops its Mermaid pipeline diagram (PyPI does not render Mermaid) in favor of a prose summary; the diagram now lives in `docs/USAGE.md`.
- `paths.exclude` default now includes `*.generated.*`.
- Composite Action honors its `config` and `mode` inputs via the `CHECKOWNERS_CONFIG` and `CHECKOWNERS_DRIFT_MODE` environment variables, which `load_config` now reads.
- `BusFactorReport` tiers (`tier_for` / `critical_paths`) respect the repo's configured `bus_factor` thresholds instead of hardcoded defaults.
- README slimmed to intro, install, quick start, command table, and links to the new `docs/`.
- Dogfood config sets `output.include_confidence: false` so the committed `CODEOWNERS` no longer publishes a per-file confidence/bus-factor map.
- All Markdown across the repo follows a tightened style: no em dashes, no typographic `--` separators, multi-entry bullets only.

### Fixed
- CI smoke job: `pip install "dist/checkowners-"*"-py3-none-any.whl[graph]"` failed because bash treated `[graph]` as a glob character class and never expanded the wildcard. The wheel path is now resolved via `ls` before installation.
- Removed dead `generate._owners_for_path` helper.

### Security
- `notifications.webhook_url` accepts a `${ENV_VAR}` reference (e.g. `${CHECKOWNERS_WEBHOOK_URL}`) so a committed config can point at a secret/internal endpoint without storing it; an unset variable resolves to "".
- `.checkowners/` is git-ignored so a state or graph cache (contributor emails + ownership map) cannot be committed if `CHECKOWNERS_STATE_DIR` points inside a repo.
- `github.token` remains refused inside `.github/checkowners.yml`; the only supported way to provide a token is the `GITHUB_TOKEN` environment variable.

## [0.3.0] - 2026-06-06

### Added
- Confidence scoring on every path-owner pair. Score is a weighted blend of four signals: commit recency (exponential decay), commit frequency, blame coverage, and PR review activity (last one only when `github.api_enabled`).
- Bus factor calculation per path with backup-reviewer suggestions, plus `checkowners bus-factor [<path>] [--all]`.
- Expertise decay detection that distinguishes dormant from departed owners, recommends transfer targets, and exposes them through `checkowners decay`.
- Knowledge graph builder backed by an optional `networkx` extra: `pip install "checkowners[graph]"`. Render in the terminal or export to DOT via `checkowners graph [--export dot]`.
- Per-path expertise ranking via `checkowners expertise <path>`.
- Team topology inference from commit co-occurrence, with reconciliation against declared GitHub teams when `api_enabled`. Exposed as `checkowners topology`.
- PR review load balancer that detects overloaded reviewers and suggests redistribution. Exposed as `checkowners balance`.
- Onboarding path generator that walks the knowledge graph from broad-ownership files to deep-expertise files and emits a Markdown checklist via `checkowners onboard <path>`.
- Persistent state cache at `~/.checkowners/state.json` (schema v2), with `CHECKOWNERS_STATE_DIR` override for CI and tests.
- Composite GitHub Action (`action.yml`) exposing `checkowners_drift`, `bus_factor_summary`, and `decay_summary` outputs; example workflow at `.github/workflows/checkowners-example.yml`.
- Drift severity tiers (`low` / `medium` / `high` / `critical`) computed from the max confidence delta plus bus-factor and decay signals; `notifications.severity_threshold` gates webhook delivery.
- Config sections `scoring`, `decay`, `bus_factor` and new fields on existing sections (`confidence_threshold`, `min_confidence_delta`, `include_confidence`, `severity_threshold`, `github.api_enabled`).

### Changed
- `analysis.lookback_days` default lifted from 180 to 365.
- `analysis.top_n_owners` default lifted from 2 to 3.
- `paths.exclude` default now includes `node_modules/**`.
- `OwnershipMap` reshaped to carry `PathOwnership` entries (confidence-scored owners, bus factor, decay warnings).
- `DriftResult` now carries `DriftEntry` tuples with per-entry confidence delta and reason.
- `notify` payload includes severity, max delta, and per-entry bus factor / decay flags.

### Fixed
`validate` strips inline confidence comments so `output.include_confidence: true` does not fail the validator. Caught while dogfooding.

### Security
`github.token` is now refused inside `.github/checkowners.yml`. `load_config` raises a clear error if the field is present, since that file gets pushed to GitHub. The only supported way to provide a token is the `GITHUB_TOKEN` environment variable.

## [0.2.0] - 2026-05-26

### Added
- GitHub `@handle` mapping: commit emails are looked up against the GitHub user-search API and rewritten to `@username` when a match is found.
- Team and subteam resolution: owner sets whose handles are a subset of an org team collapse to `@org/team-slug`, with the most deeply-nested matching team winning.
- CODEOWNERS path auto-detection across `.github/CODEOWNERS`, root `CODEOWNERS`, and `docs/CODEOWNERS` in priority order.

## [0.1.1] - 2026-05-26

### Fixed
Deleted files are no longer carried into the generated CODEOWNERS; `analyze` filters out paths that no longer exist on disk.

### Changed
Repo now dogfoods its own generated CODEOWNERS.

## [0.1.0] - 2026-05-26

### Added
- Initial CLI: `analyze`, `generate`, `print`, `validate`, `drift`, `notify`, `sync`.
- Drift detection with three modes (`commit`, `repo`, `both`) and GITHUB_OUTPUT integration.
- Webhook notifications on drift events.
- Syntax-only CODEOWNERS validator.
- Packaging via hatch; published to PyPI under `checkowners`.
- CI workflow running tests and lint across Python 3.11, 3.12, 3.13.

[Unreleased]: https://github.com/smusali/checkowners/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/smusali/checkowners/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/smusali/checkowners/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/smusali/checkowners/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/smusali/checkowners/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/smusali/checkowners/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/smusali/checkowners/releases/tag/v0.1.0
