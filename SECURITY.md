# Security Policy

## Supported versions

Only the latest release published on [PyPI](https://pypi.org/project/checkowners/) receives security fixes.

## Reporting a vulnerability

Please report vulnerabilities privately via [GitHub private vulnerability reporting](https://github.com/smusali/checkowners/security/advisories/new). Do not open a public issue for security reports. You can expect an initial response within a few days.

## Design notes relevant to security

- CheckOwners never reads a GitHub token from its config file: `github.token` in `.github/checkowners.yml` is rejected at load time because that file is committed to git. The only supported source is the `GITHUB_TOKEN` environment variable.
- `notifications.webhook_url` supports `${ENV_VAR}` indirection so committed configs never need to contain internal endpoints.
- The state directory (`~/.checkowners/` by default) contains contributor emails and an ownership map derived from git history. Point `CHECKOWNERS_STATE_DIR` at an ephemeral location in CI, and avoid committing it (a `.checkowners/` entry ships in this repo's `.gitignore` as a guard).
- Core inference makes no network calls. Network access is limited to the optional GitHub API features (`github` extra) and the webhook notifier.
- The composite GitHub Action installs the `checkowners` version that matches its own tag from a committed wheel, and installs third-party dependencies from `requirements.lock` with `--require-hashes`. Trusted Publishing and Sigstore attestations on the way out of PyPI are not discarded on the way into consumer CI: a tag pin no longer silently tracks "latest."
- `install_spec` is constrained to a local extras allowlist for this repository's own dogfood workflow. Interpolating untrusted data into that input is remote code execution; downstream callers must omit it.
