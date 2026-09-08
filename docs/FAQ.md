# FAQ

Common questions about configuring and operating checkowners. For the full configuration reference see [docs/USAGE.md](USAGE.md).

## Ownership identity

### Will the generated CODEOWNERS show GitHub usernames or commit email addresses?

GitHub usernames whenever they can be resolved. `github.resolve_handles` (on by default) resolves in three stages, cheapest first: GitHub noreply emails (`12345+login@users.noreply.github.com`) parse to `@login` locally with no token and no network; previously resolved emails come from the on-disk cache (`~/.checkowners/handles.json`, misses remembered); everything else goes through the GitHub user-search API when `GITHUB_TOKEN` is set. When resolution misses (private email, no GitHub account, API unavailable) the entry falls back to the raw email so the output stays usable.

On squash-merge repos most contributors have noreply author emails, so usernames appear even without a token. When two emails resolve to the same username they merge into one owner and the path's bus factor is recomputed over distinct people.

```yaml
github:
  resolve_handles: true  # default
```

### Does it handle GitHub teams and subteams?

Yes. When `github.org` is set and a token is available, `checkowners generate` collects every team in the org (including nested subteams), and any owner set whose handles are a subset of a team's membership is collapsed to that team. The most deeply-nested matching team wins ties, so subteams are preferred over their parents.

```yaml
github:
  org: my-org
  resolve_teams: true    # default; emits @my-org/platform/backend etc.
```

Disable `resolve_teams` if you want raw `@username` entries even when a team would match.

## GitHub API access

### Does checkowners require a GitHub token?

No. The core inference is pure git and runs offline. A token is only needed for three optional features:

| Feature | Config gate | Why a token is needed |
|---------|-------------|-----------------------|
| Email to `@username` resolution (non-noreply emails) | `github.resolve_handles` | GitHub user-search API |
| Team / subteam resolution | `github.resolve_teams` + `github.org` | List org teams + members |
| Review-load + topology reconciliation | `github.api_enabled` | PR review API + team membership |

Noreply emails (`login@users.noreply.github.com`) resolve to `@login` without any token. The API-backed features also need the `github` extra (`pip install "checkowners[github]"`); without it they degrade gracefully with a log hint.

Without a token you still get confidence-scored ownership, drift detection, bus factor, expertise decay, and onboarding paths; they just operate on email handles and skip the review-activity signal in the confidence score.

### What environment variable holds the token?

`GITHUB_TOKEN` (not `GITHUB_API_KEY`). This is the **only** supported way to provide a token. `github.token` is intentionally **not** accepted in `checkowners.yml` because that file gets committed to git and storing a secret there would publish it to GitHub. `load_config` refuses to load a config that contains `github.token` so a misconfigured repo fails fast instead of silently leaking.

```bash
export GITHUB_TOKEN=ghp_...
checkowners generate
```

In GitHub Actions the job token exists as `${{ secrets.GITHUB_TOKEN }}` / `${{ github.token }}`, but a `run:` step only sees `GITHUB_TOKEN` if the workflow exports it. The composite action does this for you: `github_token` defaults to `${{ github.token }}` and is exported on every CLI step. Pass a PAT or App token only when the default job token is not enough (org team listing). See [docs/USAGE.md](USAGE.md#github-actions).

```yaml
- uses: smusali/checkowners@v0.5.0
  # github_token defaults to github.token; override only when you need a PAT.
  with:
    github_token: ${{ secrets.GITHUB_TOKEN }}
```

If you invoke the CLI yourself in a `run:` step, export the token:

```yaml
- run: checkowners drift --json
  env:
    GITHUB_TOKEN: ${{ github.token }}
```

### What token scopes are needed?

| Capability | Job token (`permissions`) | Classic PAT | Fine-grained PAT |
|---|---|---|---|
| Email to `@username` (user search) | Default job token is enough (`contents: read` is already required for checkout) | Authenticated token; `read:user` if you want the user-search scope stated explicitly | No extra repository permission |
| Team / subteam resolution | Not available: the default `GITHUB_TOKEN` is repo-scoped and cannot list org teams. Pass a PAT or App token via `github_token` | `read:org` | Organization members: Read |
| Review coverage and review-load balance (`github.api_enabled`) | `pull-requests: read` | `repo` (private) or `public_repo` | Pull requests: Read |
| PR comment (Action `comment_on_pr`) | `pull-requests: write` | `repo` | Pull requests: Write |

A fine-grained PAT scoped to the target org with the minimums above is the recommended setup when the job token cannot reach org teams.

## File locations

### Can the CODEOWNERS file live in the repo root instead of `.github/`?

Yes. `checkowners` auto-detects the file at any of the three locations GitHub itself supports, in priority order:

1. `.github/CODEOWNERS`
2. `CODEOWNERS` (repo root)
3. `docs/CODEOWNERS`

The first one that exists wins for `analyze`, `drift`, `validate`, and `sync`. If none exists, `generate` creates `.github/CODEOWNERS` by default; move the file manually if you want a different layout and checkowners will keep using it there.

Note that `generate` and `sync` refuse to overwrite a CODEOWNERS that was not generated by checkOwners (it looks for the machine-generated header). Pass `--force` to replace a hand-written file deliberately.

### Where does the config file live?

`.github/checkowners.yml`. There's no auto-detection for the config; it has to live there.

### Where is the state cache?

`~/.checkowners/state/<repo-hash>.json` (schema v3, one file per repo; the payload embeds the absolute repo path and is verified on load, so state from one repo can never leak into another). Downstream commands (`bus-factor`, `decay`, `topology`, `balance`, `onboard`, `expertise`, `graph`) read it so they don't re-run `git log`, and print a stderr hint when they do. Override the directory with `CHECKOWNERS_STATE_DIR` for CI or tests.

## Inference behavior

### How is confidence computed?

A weighted sum of four signals, each in `[0.0, 1.0]`:

- **Recency**: `exp(-ln 2 × days_since_last_commit / half_life)`. Default half-life is 90 days.
- **Frequency**: contributor's commits on the path divided by the path's max contributor.
- **Blame coverage**: fraction of current lines `git blame --line-porcelain` attributes to the contributor.
- **Review activity**: PR reviews on the path divided by total reviews; `0.0` unless `github.api_enabled` is true.

Weights are configurable under `scoring`. The final score is clamped to `[0.0, 1.0]`; owners below `analysis.confidence_threshold` are dropped from the generated CODEOWNERS.

### Can I tune the inference for a high-turnover team?

Yes. Common tunings:

```yaml
scoring:
  recency_half_life_days: 45   # decay expertise faster
  recency_weight: 0.5          # weigh "what did you touch last month" higher

decay:
  threshold_days: 90           # flag dormant owners after 3 months

analysis:
  confidence_threshold: 0.4    # stricter cutoff
```

### Why are some paths missing from `checkowners analyze`?

Four filters can drop a path: it matches a `paths.exclude` pattern, it no longer exists on disk (deleted files are filtered automatically so CODEOWNERS doesn't pin removed paths), its only contributors are bots (`analysis.exclude_bots`, on by default), or no contributor reaches `analysis.min_commits` within the lookback window.

## Drift, severity, and CI

### What does drift "severity" mean in CI?

`notify.compute_severity` maps the max confidence delta plus bus-factor / decay flags to a tier:

| Severity | Trigger |
|----------|---------|
| `critical` | Any drift entry has `bus_factor <= 1` or is `decay = true` |
| `high` | `max_confidence_delta >= 0.7` |
| `medium` | `max_confidence_delta >= 0.3` |
| `low` | otherwise |

`notifications.severity_threshold` decides when a webhook fires, and `--json` always includes the severity field so CI workflows can branch on it.

### How do I fail a PR only on critical drift?

The example workflow in `.github/workflows/checkowners-example.yml` does this with `fromJson(steps.checkowners.outputs.checkowners_drift).severity == 'critical'`. The composite action also accepts `fail_on_drift: "false"` if you want to comment without blocking.

## Troubleshooting

### `networkx` is not installed but I want `checkowners graph`.

Install the extra: `pip install "checkowners[graph]"`. The error message points to this too.

### `checkowners drift` complains about lines with `# alice(0.92)`.

You're on an older version that predates the inline-comment fix. Upgrade to v0.3.0+ or strip the annotations by setting `output.include_confidence: false` and regenerating.

### The bus factor report says `repo_average: 1.0`. Is that right?

For a solo-maintainer repo, yes. Bus factor is the number of selected owners with confidence above `analysis.confidence_threshold`; a single committer caps out at 1 per path. Invite a co-owner and let them rack up commits to move the needle.
