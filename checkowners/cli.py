"""checkOwners CLI entry point."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.table import Table

from checkowners import __version__
from checkowners.analyze import ReviewProvider, analyze_ownership
from checkowners.balance import BalanceReport, analyze_balance
from checkowners.busfactor import BusFactorReport, classify, compute_bus_factor
from checkowners.config import find_codeowners_path, load_config
from checkowners.decay import DecayReport, detect_decay
from checkowners.drift import detect_drift
from checkowners.expertise import rank_expertise
from checkowners.generate import (
    CodeownersOverwriteError,
    ensure_overwrite_safe,
    generate_codeowners,
)
from checkowners.github import build_review_coverage, get_github_token, resolve_handles
from checkowners.graph import (
    GraphExtraMissingError,
    build_graph,
    from_serializable,
    to_dot,
    to_serializable,
    to_text,
)
from checkowners.models import (
    Config,
    DecayWarning,
    DriftEntry,
    DriftResult,
    ExpertiseRank,
    OwnerEntry,
    OwnershipMap,
    PathOwnership,
)
from checkowners.notify import compute_severity, send_notification
from checkowners.onboard import OnboardingPath, generate_onboarding_path
from checkowners.state import (
    StalenessError,
    cache_dir,
    cache_info,
    clear_repo_cache,
    evict_if_oversize,
    load_ownership,
    purge_cache,
    read_graph_cache,
    write_graph_cache,
    write_state,
)
from checkowners.topology import (
    TopologyReport,
    declared_teams_from_github,
    infer_topology,
)
from checkowners.trends import TrendPoint, analyze_trends
from checkowners.validate import validate_codeowners

if TYPE_CHECKING:
    import networkx as nx

app = typer.Typer(
    name="checkowners",
    help="Infer and maintain CODEOWNERS from git history.",
    rich_markup_mode="rich",
    no_args_is_help=True,
)

cache_app = typer.Typer(help="Manage the local checkowners cache.", no_args_is_help=True)
app.add_typer(cache_app, name="cache")

console = Console()
err_console = Console(stderr=True)

JsonOption = Annotated[bool, typer.Option("--json", help="Output as JSON.")]


# ---------------------------------------------------------------------------
# Global --offline state
# ---------------------------------------------------------------------------

_OFFLINE: bool = False


def is_offline() -> bool:
    """Return True when the global --offline flag is active."""
    return _OFFLINE


def _emit_offline_status() -> None:
    """Print offline-mode diagnostics to stderr (never stdout, to keep --json clean)."""
    if not _OFFLINE:
        return
    for line in (
        "Network access: disabled",
        "Review evidence: unavailable",
        "Team verification: unavailable",
    ):
        err_console.print(f"[dim]{line}[/dim]")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"checkowners {__version__}")
        raise typer.Exit()


@app.callback()
def _app_callback(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
    offline: Annotated[
        bool,
        typer.Option(
            "--offline",
            help="Disable all outbound network requests.",
        ),
    ] = False,
) -> None:
    """Infer and maintain CODEOWNERS from git history."""
    global _OFFLINE  # noqa: PLW0603
    _OFFLINE = offline
    _emit_offline_status()


def _resolve_github_owners(ownership: OwnershipMap, config: Config) -> OwnershipMap:
    """Rewrite email identities to GitHub @handles and merge duplicates.

    Handle resolution works even without a token (noreply emails parse
    locally; prior lookups come from the on-disk cache). When two commit
    emails resolve to the same @handle they are one person: their entries
    merge and the path's bus factor is recomputed over distinct identities,
    so one owner with two emails can no longer masquerade as a bus factor
    of two.
    """
    if _OFFLINE or not config.github.resolve_handles:
        return ownership
    emails = {o.handle for po in ownership.paths.values() for o in po.owners}
    for po in ownership.paths.values():
        emails.update(w.handle for w in po.decay_warnings)
    if not emails:
        return ownership
    email_to_handle = resolve_handles(emails, get_github_token())
    if not email_to_handle:
        return ownership
    new_paths: dict[str, PathOwnership] = {}
    for path, po in ownership.paths.items():
        merged = _merge_identities(po.owners, email_to_handle)
        decay_warnings = tuple(
            DecayWarning(
                handle=email_to_handle.get(w.handle, w.handle),
                path=w.path,
                last_commit=w.last_commit,
                days_since_last_commit=w.days_since_last_commit,
                historical_confidence=w.historical_confidence,
            )
            for w in po.decay_warnings
        )
        bus_factor = sum(1 for e in merged if e.confidence >= config.analysis.confidence_threshold)
        new_paths[path] = PathOwnership(
            owners=merged,
            bus_factor=bus_factor,
            decay_warnings=decay_warnings,
        )
    return OwnershipMap(paths=new_paths, last_analyzed=ownership.last_analyzed)


def _merge_identities(
    owners: tuple[OwnerEntry, ...],
    email_to_handle: dict[str, str],
) -> tuple[OwnerEntry, ...]:
    """Merge owner entries whose emails resolve to the same @handle."""
    grouped: dict[str, list[OwnerEntry]] = {}
    for owner in owners:
        identity = email_to_handle.get(owner.handle, owner.handle)
        grouped.setdefault(identity, []).append(owner)
    merged: list[OwnerEntry] = []
    for identity, entries in grouped.items():
        best = max(entries, key=lambda e: e.confidence)
        last_commits = [e.last_commit for e in entries if e.last_commit is not None]
        merged.append(
            OwnerEntry(
                handle=identity,
                confidence=best.confidence,
                last_commit=max(last_commits) if last_commits else None,
                commits=sum(e.commits for e in entries),
                score_breakdown=best.score_breakdown,
            )
        )
    merged.sort(key=lambda e: (-e.confidence, e.handle))
    return tuple(merged)


def _confidence_style(confidence: float) -> str:
    if confidence >= 0.7:
        return "green"
    if confidence >= 0.4:
        return "yellow"
    return "red"


def _format_last_commit(value: datetime | None) -> str:
    return value.date().isoformat() if value else "-"


def _owner_payload(owner: OwnerEntry) -> dict[str, Any]:
    return {
        "handle": owner.handle,
        "confidence": round(owner.confidence, 4),
        "commits": owner.commits,
        "last_commit": owner.last_commit.isoformat() if owner.last_commit else None,
    }


def _path_payload(po: PathOwnership) -> dict[str, Any]:
    return {
        "owners": [_owner_payload(o) for o in po.owners],
        "bus_factor": po.bus_factor,
        "decay_warnings": [
            {
                "handle": w.handle,
                "days_since_last_commit": w.days_since_last_commit,
                "last_commit": w.last_commit.isoformat(),
                "historical_confidence": round(w.historical_confidence, 4),
            }
            for w in po.decay_warnings
        ],
    }


def _drift_entry_payload(entry: DriftEntry) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "path": entry.path,
        "confidence_delta": round(entry.confidence_delta, 4),
        "reason": entry.reason,
    }
    if entry.bus_factor is not None:
        payload["bus_factor"] = entry.bus_factor
    if entry.decay:
        payload["decay"] = entry.decay
    return payload


def _render_ownership_table(ownership: OwnershipMap) -> None:
    if not ownership.paths:
        console.print("[yellow]No ownership data inferred.[/yellow]")
        return
    table = Table(title="Inferred Ownership")
    table.add_column("Path", style="cyan")
    table.add_column("Owners (confidence)", style="white")
    table.add_column("Bus", justify="right")
    table.add_column("Decay", justify="right")
    for path in sorted(ownership.paths):
        po = ownership.paths[path]
        owners_str = ", ".join(
            f"[{_confidence_style(o.confidence)}]{escape(o.handle)} ({o.confidence:.2f})[/]"
            for o in po.owners
        )
        bus = str(po.bus_factor)
        if po.bus_factor <= 1:
            bus = f"[red]{bus}[/red]"
        decay = str(len(po.decay_warnings)) if po.decay_warnings else "-"
        table.add_row(escape(path), owners_str, bus, decay)
    console.print(table)


def _review_provider(config: Config) -> ReviewProvider | None:
    """Build a GitHub-backed review provider when the API is enabled.

    Requires github.api_enabled, a resolvable token, and the GITHUB_REPOSITORY
    slug (set in GitHub Actions). Returns None otherwise, leaving the review
    factor at 0.0.
    """
    if _OFFLINE or not config.github.api_enabled:
        return None
    repo_full_name = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo_full_name:
        return None
    token = get_github_token()
    if not token:
        return None

    def provider(emails: set[str]) -> dict[str, dict[str, float]]:
        return build_review_coverage(token, repo_full_name, emails)

    return provider


def _run_analyze(config: Config, repo_root: Path) -> OwnershipMap:
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeRemainingColumn(),
        console=err_console,
        transient=True,
        disable=not err_console.is_terminal,
    )
    try:
        with progress:
            task_id = progress.add_task("Analyzing git history (blame pass)", total=None)

            def on_progress(done: int, total: int) -> None:
                progress.update(task_id, completed=done, total=total)

            ownership = analyze_ownership(
                repo_root,
                config,
                review_provider=_review_provider(config),
                on_progress=on_progress,
            )
    except subprocess.CalledProcessError as exc:
        console.print(f"[red]Git command failed:[/red] {exc}")
        raise typer.Exit(code=1) from None
    ownership = _resolve_github_owners(ownership, config)
    write_state(repo_root, ownership)
    evict_if_oversize()
    return ownership


def _load_or_analyze(
    config: Config,
    repo_root: Path,
    *,
    allow_stale: bool = False,
    max_age_days: float | None = None,
    no_cache: bool = False,
) -> OwnershipMap:
    """Use this repo's cached state when available; otherwise re-analyze."""
    if no_cache:
        return _run_analyze(config, repo_root)
    try:
        cached = load_ownership(
            repo_root,
            allow_stale=allow_stale,
            max_age_days=max_age_days,
        )
    except StalenessError as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        return _run_analyze(config, repo_root)
    if cached is not None:
        cached_at = cached.last_analyzed.isoformat(timespec="seconds")
        err_console.print(
            f"[dim]Using cached analysis from {cached_at}; "
            "run `checkowners analyze` to refresh.[/dim]"
        )
        return cached
    return _run_analyze(config, repo_root)


def _expertise_rank_payload(rank: ExpertiseRank) -> dict[str, Any]:
    return {
        "handle": rank.handle,
        "confidence": round(rank.confidence, 4),
        "commits": rank.commits,
        "last_commit": rank.last_commit.isoformat() if rank.last_commit else None,
    }


# ---------------------------------------------------------------------------
# Common option types for staleness
# ---------------------------------------------------------------------------

AllowStaleOption = Annotated[
    bool,
    typer.Option(
        "--allow-stale",
        help="Accept cached state even when the analyzed ref is stale.",
    ),
]

MaxAgeOption = Annotated[
    float | None,
    typer.Option(
        "--max-age",
        help="Maximum age of cached state in days before re-analysis.",
    ),
]

NoCacheOption = Annotated[
    bool,
    typer.Option(
        "--no-cache",
        help="Bypass cached state and force a fresh analysis.",
    ),
]


# --- analyze ---


@app.command()
def analyze(
    json_output: JsonOption = False,
    allow_stale: AllowStaleOption = False,
    max_age: MaxAgeOption = None,
    no_cache: NoCacheOption = False,
) -> None:
    """Analyze git history to infer confidence-scored ownership."""
    config = load_config()
    ownership = _run_analyze(config, Path.cwd())
    if json_output:
        data = {
            "inferred": {path: _path_payload(po) for path, po in ownership.paths.items()},
            "last_analyzed": ownership.last_analyzed.isoformat(),
        }
        typer.echo(json.dumps(data, indent=2))
    else:
        _render_ownership_table(ownership)


ForceOption = Annotated[
    bool,
    typer.Option(
        "--force",
        help="Overwrite a CODEOWNERS file that was not generated by checkOwners.",
    ),
]


def _check_overwrite_or_exit(codeowners_path: Path, config: Config, force: bool) -> None:
    """Fail before the expensive analyze when the target would be refused."""
    try:
        ensure_overwrite_safe(codeowners_path, config.output.header, force=force)
    except CodeownersOverwriteError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None


@app.command()
def generate(
    json_output: JsonOption = False,
    force: ForceOption = False,
    allow_stale: AllowStaleOption = False,
    max_age: MaxAgeOption = None,
    no_cache: NoCacheOption = False,
) -> None:
    """Generate a CODEOWNERS file from inferred ownership."""
    config = load_config()
    repo_root = Path.cwd()
    codeowners_path = find_codeowners_path(repo_root)
    _check_overwrite_or_exit(codeowners_path, config, force)
    ownership = _run_analyze(config, repo_root)
    token = get_github_token() if not _OFFLINE else ""
    try:
        content = generate_codeowners(
            repo_root,
            ownership,
            config,
            codeowners_path=codeowners_path,
            token=token,
            org=config.github.org,
            force=force,
        )
    except CodeownersOverwriteError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    rel_path = codeowners_path.relative_to(repo_root)
    if json_output:
        typer.echo(json.dumps({"path": str(rel_path), "content": content}, indent=2))
    else:
        console.print(f"[green]Generated {rel_path}[/green]")


@app.command(name="print")
def print_cmd(json_output: JsonOption = False) -> None:
    """Print inferred ownership to stdout."""
    config = load_config()
    ownership = _run_analyze(config, Path.cwd())
    if json_output:
        data = {path: _path_payload(po) for path, po in sorted(ownership.paths.items())}
        typer.echo(json.dumps(data, indent=2))
    else:
        for path in sorted(ownership.paths):
            owners = " ".join(
                f"{o.handle}({o.confidence:.2f})" for o in ownership.paths[path].owners
            )
            typer.echo(f"{path}\t{owners}")


@app.command()
def validate(json_output: JsonOption = False) -> None:
    """Validate CODEOWNERS file syntax."""
    repo_root = Path.cwd()
    codeowners_path = find_codeowners_path(repo_root)
    errors = validate_codeowners(repo_root, codeowners_path=codeowners_path)
    if json_output:
        data = {
            "valid": len(errors) == 0,
            "errors": [{"line": e.line_number, "message": e.message} for e in errors],
        }
        typer.echo(json.dumps(data, indent=2))
        if errors:
            raise typer.Exit(code=1)
        return
    if not errors:
        console.print("[green]CODEOWNERS is valid.[/green]")
    else:
        for err in errors:
            console.print(f"[red]Line {err.line_number}:[/red] {escape(err.message)}")
        raise typer.Exit(code=1)


def _render_drift_table(result: DriftResult) -> None:
    table = Table(title="CODEOWNERS Drift")
    table.add_column("Category", style="bold")
    table.add_column("Path", style="cyan")
    table.add_column("Δ", justify="right")
    table.add_column("Reason")
    for entry in result.stale:
        table.add_row(
            "[red]stale[/red]",
            escape(entry.path),
            f"{entry.confidence_delta:.2f}",
            escape(entry.reason),
        )
    for entry in result.missing:
        bf_low = entry.bus_factor is not None and entry.bus_factor <= 1
        flag = " [red](bf=1)[/red]" if bf_low else ""
        decay = " [magenta](decay)[/magenta]" if entry.decay else ""
        table.add_row(
            "[yellow]missing[/yellow]",
            escape(entry.path),
            f"{entry.confidence_delta:.2f}",
            escape(entry.reason) + flag + decay,
        )
    for entry in result.changed:
        table.add_row(
            "[cyan]changed[/cyan]",
            escape(entry.path),
            f"{entry.confidence_delta:.2f}",
            escape(entry.reason),
        )
    console.print(table)


@app.command()
def drift(
    json_output: JsonOption = False,
    allow_stale: AllowStaleOption = False,
    max_age: MaxAgeOption = None,
    no_cache: NoCacheOption = False,
) -> None:
    """Detect drift between inferred and current CODEOWNERS."""
    config = load_config()
    repo_root = Path.cwd()
    codeowners_path = find_codeowners_path(repo_root)
    ownership = _run_analyze(config, repo_root)
    result = detect_drift(repo_root, ownership, config, codeowners_path=codeowners_path)
    severity = compute_severity(result, config)
    if json_output:
        data = {
            "stale": [_drift_entry_payload(e) for e in result.stale],
            "missing": [_drift_entry_payload(e) for e in result.missing],
            "changed": [_drift_entry_payload(e) for e in result.changed],
            "drift_detected": result.drift_detected,
            "severity": severity,
            "max_confidence_delta": round(result.max_confidence_delta, 4),
            "notes": list(result.notes),
        }
        typer.echo(json.dumps(data, indent=2))
        return
    for note in result.notes:
        console.print(f"[yellow]note:[/yellow] {escape(note)}")
    if not result.drift_detected:
        console.print("[green]No drift detected.[/green]")
        return
    console.print(
        f"[bold]severity:[/bold] [{_severity_style(severity)}]{severity.upper()}[/] "
        f"(Δmax={result.max_confidence_delta:.2f})"
    )
    _render_drift_table(result)


def _severity_style(severity: str) -> str:
    return {"critical": "red", "high": "red", "medium": "yellow", "low": "green"}[severity]


@app.command()
def notify(json_output: JsonOption = False) -> None:
    """Send webhook notification on drift events."""
    config = load_config()
    repo_root = Path.cwd()
    codeowners_path = find_codeowners_path(repo_root)
    ownership = _run_analyze(config, repo_root)
    result = detect_drift(repo_root, ownership, config, codeowners_path=codeowners_path)
    sent = send_notification(result, config)
    severity = compute_severity(result, config)
    if json_output:
        data = {
            "sent": sent,
            "drift_detected": result.drift_detected,
            "severity": severity,
        }
        typer.echo(json.dumps(data, indent=2))
        return
    if sent:
        console.print(f"[green]Notification sent ({severity}).[/green]")
    elif not config.notifications.webhook_url:
        console.print("[yellow]No webhook URL configured; skipped.[/yellow]")
    else:
        console.print(
            f"[yellow]Severity {severity} below threshold "
            f"{config.notifications.severity_threshold}; skipped.[/yellow]"
        )


@app.command()
def sync(
    json_output: JsonOption = False,
    force: ForceOption = False,
) -> None:
    """Sync CODEOWNERS with inferred ownership (generate + commit)."""
    config = load_config()
    repo_root = Path.cwd()
    codeowners_path = find_codeowners_path(repo_root)
    _check_overwrite_or_exit(codeowners_path, config, force)
    ownership = _run_analyze(config, repo_root)
    token = get_github_token() if not _OFFLINE else ""
    try:
        content = generate_codeowners(
            repo_root,
            ownership,
            config,
            codeowners_path=codeowners_path,
            token=token,
            org=config.github.org,
            force=force,
        )
    except CodeownersOverwriteError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    rel_path = codeowners_path.relative_to(repo_root)
    if not _has_uncommitted_changes(repo_root, rel_path):
        if json_output:
            data = {"path": str(rel_path), "committed": False, "content": content}
            typer.echo(json.dumps(data, indent=2))
        else:
            console.print(f"[green]{rel_path} is already in sync; nothing to commit.[/green]")
        return
    try:
        subprocess.run(
            ["git", "add", str(rel_path)],
            cwd=str(repo_root),
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "chore: sync CODEOWNERS via checkowners"],
            cwd=str(repo_root),
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        console.print(f"[red]Git commit failed:[/red] {detail}")
        raise typer.Exit(code=1) from None
    if json_output:
        data = {"path": str(rel_path), "committed": True, "content": content}
        typer.echo(json.dumps(data, indent=2))
    else:
        console.print(f"[green]Generated and committed {rel_path}[/green]")


def _has_uncommitted_changes(repo_root: Path, rel_path: Path) -> bool:
    """True when the generated file differs from what is committed."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--", str(rel_path)],
            cwd=str(repo_root),
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError:
        return True
    return bool(result.stdout.strip())


def _write_github_outputs(outputs: dict[str, Any]) -> None:
    """Append compact JSON outputs to GITHUB_OUTPUT when running in Actions."""
    output_file = os.environ.get("GITHUB_OUTPUT")
    if not output_file:
        return
    with open(output_file, "a", encoding="utf-8") as fh:
        for key, value in outputs.items():
            fh.write(f"{key}={json.dumps(value, separators=(',', ':'))}\n")


@app.command(name="github-action")
def github_action(
    fail_on_drift: Annotated[
        bool,
        typer.Option(
            "--fail-on-drift/--no-fail-on-drift",
            help="Exit non-zero when drift is detected.",
        ),
    ] = True,
    json_output: JsonOption = False,
) -> None:
    """Run the full CI flow (drift + bus factor + decay) and write GITHUB_OUTPUT."""
    config = load_config()
    repo_root = Path.cwd()
    codeowners_path = find_codeowners_path(repo_root)
    ownership = _run_analyze(config, repo_root)

    # detect_drift writes the `checkowners_drift` key to GITHUB_OUTPUT itself.
    result = detect_drift(repo_root, ownership, config, codeowners_path=codeowners_path)
    severity = compute_severity(result, config)
    bus_report = compute_bus_factor(ownership, config, target=None)
    decay_reports = detect_decay(ownership, config)

    drift_payload = {
        "drift_detected": result.drift_detected,
        "severity": severity,
        "max_confidence_delta": round(result.max_confidence_delta, 4),
        "stale": [_drift_entry_payload(e) for e in result.stale],
        "missing": [_drift_entry_payload(e) for e in result.missing],
        "changed": [_drift_entry_payload(e) for e in result.changed],
        "notes": list(result.notes),
    }
    bus_payload = _bus_factor_payload(bus_report, config)
    decay_payload = {"reports": [_decay_report_payload(r) for r in decay_reports]}

    _write_github_outputs(
        {
            "checkowners_drift": drift_payload,
            "bus_factor_summary": bus_payload,
            "decay_summary": decay_payload,
        }
    )

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "checkowners_drift": drift_payload,
                    "bus_factor_summary": bus_payload,
                    "decay_summary": decay_payload,
                },
                indent=2,
            )
        )
    else:
        console.print(
            f"[bold]drift:[/bold] {result.drift_detected} "
            f"([{_severity_style(severity)}]{severity}[/]) "
            f"· critical paths: {len(bus_report.critical_paths)} "
            f"· decay warnings: {len(decay_reports)}"
        )

    if fail_on_drift and result.drift_detected:
        raise typer.Exit(code=1)


def _decay_report_payload(report: DecayReport) -> dict[str, Any]:
    return {
        "handle": report.warning.handle,
        "path": report.warning.path,
        "days_since_last_commit": report.warning.days_since_last_commit,
        "last_commit": report.warning.last_commit.isoformat(),
        "historical_confidence": round(report.warning.historical_confidence, 4),
        "recommended_transfer": report.recommended_transfer,
        "departed": report.departed,
    }


def _build_or_load_graph(repo_root: Path, ownership: OwnershipMap) -> nx.Graph:
    """Return the knowledge graph, reusing a fresh on-disk cache when available."""
    cached = read_graph_cache(repo_root, ownership.last_analyzed)
    if cached is not None:
        return from_serializable(cached)
    graph_obj = build_graph(ownership)
    write_graph_cache(repo_root, ownership.last_analyzed, to_serializable(graph_obj))
    return graph_obj


@app.command()
def graph(
    export: Annotated[
        str | None,
        typer.Option(
            "--export",
            help="Export the graph in the given format (currently 'dot').",
            case_sensitive=False,
        ),
    ] = None,
) -> None:
    """Render the contributor-file knowledge graph in the terminal."""
    config = load_config()
    repo_root = Path.cwd()
    ownership = _load_or_analyze(config, repo_root)
    try:
        graph_obj = _build_or_load_graph(repo_root, ownership)
    except GraphExtraMissingError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    if export is None:
        typer.echo(to_text(graph_obj))
        return
    fmt = export.strip().lower()
    if fmt == "dot":
        typer.echo(to_dot(graph_obj))
        return
    console.print(f"[red]Unsupported export format: {export!r}; supported: dot[/red]")
    raise typer.Exit(code=1)


@app.command()
def decay(json_output: JsonOption = False) -> None:
    """Detect contributors whose expertise on a path has gone stale."""
    config = load_config()
    ownership = _load_or_analyze(config, Path.cwd())
    reports = detect_decay(ownership, config)
    if json_output:
        typer.echo(
            json.dumps(
                {"reports": [_decay_report_payload(r) for r in reports]},
                indent=2,
            )
        )
        return
    if not reports:
        console.print("[green]No decaying expertise detected.[/green]")
        return
    table = Table(title="Expertise Decay")
    table.add_column("Path", style="cyan")
    table.add_column("Handle")
    table.add_column("Days", justify="right")
    table.add_column("Historical Δ", justify="right")
    table.add_column("Status")
    table.add_column("Recommended transfer")
    for report in reports:
        status = "[red]departed[/red]" if report.departed else "[yellow]dormant[/yellow]"
        target = report.recommended_transfer or "[dim]triage[/dim]"
        table.add_row(
            escape(report.warning.path),
            escape(report.warning.handle),
            str(report.warning.days_since_last_commit),
            f"{report.warning.historical_confidence:.2f}",
            status,
            escape(target) if report.recommended_transfer else target,
        )
    console.print(table)


@app.command(name="bus-factor")
def bus_factor(
    path: Annotated[
        str | None,
        typer.Argument(help="Path (or glob) to limit the report to."),
    ] = None,
    all_paths: Annotated[
        bool,
        typer.Option("--all", help="Report every path in the repo."),
    ] = False,
    json_output: JsonOption = False,
) -> None:
    """Calculate the bus factor for each path."""
    if path is None and not all_paths:
        console.print("[yellow]Specify a path or pass --all to report every path.[/yellow]")
        raise typer.Exit(code=1)
    config = load_config()
    ownership = _load_or_analyze(config, Path.cwd())
    target = path if path else None
    report = compute_bus_factor(ownership, config, target=target)
    if json_output:
        data = _bus_factor_payload(report, config)
        typer.echo(json.dumps(data, indent=2))
        return
    if not report.entries:
        console.print("[yellow]No paths matched.[/yellow]")
        return
    table = Table(title="Bus Factor")
    table.add_column("Path", style="cyan")
    table.add_column("BF", justify="right")
    table.add_column("Tier")
    table.add_column("Owners")
    table.add_column("Recommended backups")
    for entry in report.entries:
        tier = classify(entry.bus_factor, config.bus_factor)
        owners = ", ".join(entry.contributors_above_threshold) or "-"
        backups = ", ".join(entry.recommended_backups) or "-"
        tier_str = {
            "critical": "[red]CRITICAL[/red]",
            "warning": "[yellow]WARN[/yellow]",
            "ok": "[green]OK[/green]",
        }[tier]
        table.add_row(
            escape(entry.path), str(entry.bus_factor), tier_str, escape(owners), escape(backups)
        )
    console.print(table)
    console.print(f"[dim]repo average bus factor: {report.repo_average:.2f}[/dim]")


def _bus_factor_payload(report: BusFactorReport, config: Config) -> dict[str, Any]:
    return {
        "repo_average": report.repo_average,
        "entries": [
            {
                "path": entry.path,
                "bus_factor": entry.bus_factor,
                "tier": classify(entry.bus_factor, config.bus_factor),
                "contributors_above_threshold": list(entry.contributors_above_threshold),
                "recommended_backups": list(entry.recommended_backups),
            }
            for entry in report.entries
        ],
        "critical_paths": list(report.critical_paths),
    }


def _topology_payload(report: TopologyReport) -> dict[str, Any]:
    return {
        "clusters": [
            {
                "name": cluster.name,
                "members": list(cluster.members),
                "primary_paths": list(cluster.primary_paths),
                "declared": cluster.declared,
            }
            for cluster in report.clusters
        ],
        "mismatches": list(report.mismatches),
    }


def _balance_payload(report: BalanceReport) -> dict[str, Any]:
    return {
        "source": report.source,
        "fallback_reason": report.fallback_reason,
        "average": report.average,
        "loads": [{"handle": load.handle, "reviews": load.reviews} for load in report.loads],
        "overloaded": [
            {"handle": load.handle, "reviews": load.reviews} for load in report.overloaded
        ],
        "suggestions": [
            {
                "overloaded": suggestion.overloaded,
                "candidate": suggestion.candidate,
                "confidence": round(suggestion.confidence, 4),
                "proposed_shift": suggestion.proposed_shift,
            }
            for suggestion in report.suggestions
        ],
    }


@app.command()
def balance(json_output: JsonOption = False) -> None:
    """Analyze PR review load distribution and suggest rebalancing."""
    config = load_config()
    ownership = _load_or_analyze(config, Path.cwd())
    report = analyze_balance(ownership, config)
    if json_output:
        typer.echo(json.dumps(_balance_payload(report), indent=2))
        return
    if not report.loads:
        console.print("[yellow]No review load data available.[/yellow]")
        return
    console.print(f"[dim]source: {report.source}; average reviews: {report.average:.1f}[/dim]")
    if report.fallback_reason:
        console.print(
            f"[dim]GitHub API unavailable ({report.fallback_reason}); "
            "loads below are commit counts, not reviews.[/dim]"
        )
    table = Table(title="Review Load")
    table.add_column("Handle", style="cyan")
    load_label = "Commits (proxy)" if report.source == "git_authorship" else "Reviews"
    table.add_column(load_label, justify="right")
    table.add_column("Status")
    overloaded_handles = {load.handle for load in report.overloaded}
    for load in report.loads:
        status = (
            "[red]overloaded[/red]" if load.handle in overloaded_handles else "[green]ok[/green]"
        )
        table.add_row(escape(load.handle), str(load.reviews), status)
    console.print(table)
    if report.suggestions:
        console.print()
        console.print("[bold]Rebalance suggestions:[/bold]")
        for suggestion in report.suggestions:
            console.print(
                f"  - shift ~{suggestion.proposed_shift} reviews from {suggestion.overloaded}"
                f" to {suggestion.candidate} (confidence {suggestion.confidence:.2f})"
            )


@app.command()
def topology(json_output: JsonOption = False) -> None:
    """Infer team topology from commit co-occurrence patterns."""
    config = load_config()
    ownership = _load_or_analyze(config, Path.cwd())
    declared = declared_teams_from_github(config) if not _OFFLINE else None
    report = infer_topology(ownership, config, declared_teams=declared)
    if json_output:
        typer.echo(json.dumps(_topology_payload(report), indent=2))
        return
    if not report.clusters:
        console.print("[yellow]No clusters inferred.[/yellow]")
        return
    table = Table(title="Inferred Team Topology")
    table.add_column("Cluster", style="cyan")
    table.add_column("Members")
    table.add_column("Primary paths")
    table.add_column("Source")
    for cluster in report.clusters:
        source = "[green]declared[/green]" if cluster.declared else "[yellow]inferred[/yellow]"
        table.add_row(
            escape(cluster.name),
            escape(", ".join(cluster.members)),
            escape(", ".join(cluster.primary_paths)) or "-",
            source,
        )
    console.print(table)
    if report.mismatches:
        console.print()
        console.print("[bold]Mismatches:[/bold]")
        for line in report.mismatches:
            console.print(f"  - {line}")


def _onboarding_payload(report: OnboardingPath) -> dict[str, Any]:
    return {
        "target": report.target,
        "steps": [
            {
                "order": step.order,
                "path": step.path,
                "reviewer": step.reviewer,
                "complexity": step.complexity,
                "description": step.description,
            }
            for step in report.steps
        ],
    }


@app.command()
def onboard(
    path: Annotated[str, typer.Argument(help="Path or directory to onboard into.")],
    json_output: JsonOption = False,
    markdown: Annotated[
        bool,
        typer.Option("--markdown", help="Emit a Markdown checklist."),
    ] = False,
) -> None:
    """Generate a structured onboarding path for a codebase area."""
    config = load_config()
    ownership = _load_or_analyze(config, Path.cwd())
    report = generate_onboarding_path(ownership, config, target=path)
    if json_output:
        typer.echo(json.dumps(_onboarding_payload(report), indent=2))
        return
    if markdown:
        typer.echo(report.to_markdown())
        return
    if not report.steps:
        console.print(f"[yellow]No onboarding path could be built for {path!r}.[/yellow]")
        return
    table = Table(title=f"Onboarding path: {path}")
    table.add_column("#", justify="right")
    table.add_column("Path", style="cyan")
    table.add_column("Reviewer")
    table.add_column("Complexity")
    table.add_column("Why")
    for step in report.steps:
        table.add_row(
            str(step.order),
            escape(step.path),
            escape(step.reviewer),
            step.complexity,
            escape(step.description),
        )
    console.print(table)


@app.command()
def expertise(
    path: Annotated[str, typer.Argument(help="Path or glob to rank expertise for.")],
    json_output: JsonOption = False,
) -> None:
    """Show expertise ranking for a specific path."""
    config = load_config()
    ownership = _load_or_analyze(config, Path.cwd())
    ranking = rank_expertise(ownership, path)
    if json_output:
        data = {
            "path": path,
            "ranking": [_expertise_rank_payload(r) for r in ranking],
        }
        typer.echo(json.dumps(data, indent=2))
        return
    if not ranking:
        console.print(f"[yellow]No experts found for {path!r}.[/yellow]")
        return
    table = Table(title=f"Expertise: {path}")
    table.add_column("#", justify="right")
    table.add_column("Handle", style="cyan")
    table.add_column("Confidence", justify="right")
    table.add_column("Commits", justify="right")
    table.add_column("Last commit", justify="right")
    for idx, rank in enumerate(ranking, start=1):
        table.add_row(
            str(idx),
            f"[{_confidence_style(rank.confidence)}]{escape(rank.handle)}[/]",
            f"{rank.confidence:.2f}",
            str(rank.commits),
            _format_last_commit(rank.last_commit),
        )
    console.print(table)


def _trend_point_payload(point: TrendPoint) -> dict[str, Any]:
    return {
        "period_end": point.period_end.date().isoformat(),
        "commits": point.commits,
        "active_contributors": point.active_contributors,
        "tracked_paths": point.tracked_paths,
        "avg_top_confidence": point.avg_top_confidence,
        "avg_bus_factor": point.avg_bus_factor,
    }


@app.command()
def trends(
    periods: Annotated[
        int,
        typer.Option("--periods", min=1, max=36, help="Number of periods to report."),
    ] = 6,
    period_days: Annotated[
        int,
        typer.Option("--period-days", min=1, help="Length of each period in days."),
    ] = 30,
    json_output: JsonOption = False,
) -> None:
    """Show how ownership confidence and bus factor have evolved over time."""
    config = load_config()
    try:
        report = analyze_trends(Path.cwd(), config, periods=periods, period_days=period_days)
    except subprocess.CalledProcessError as exc:
        console.print(f"[red]Git command failed:[/red] {exc}")
        raise typer.Exit(code=1) from None
    if json_output:
        data = {
            "periods": report.periods,
            "period_days": report.period_days,
            "points": [_trend_point_payload(p) for p in report.points],
        }
        typer.echo(json.dumps(data, indent=2))
        return
    if not report.points or all(p.commits == 0 for p in report.points):
        console.print("[yellow]No history available for the requested range.[/yellow]")
        return
    table = Table(title=f"Ownership Trends ({report.periods}x{report.period_days}d)")
    table.add_column("Period end", style="cyan")
    table.add_column("Commits", justify="right")
    table.add_column("Contributors", justify="right")
    table.add_column("Tracked paths", justify="right")
    table.add_column("Avg top conf.", justify="right")
    table.add_column("Avg bus factor", justify="right")
    for point in report.points:
        table.add_row(
            point.period_end.date().isoformat(),
            str(point.commits),
            str(point.active_contributors),
            str(point.tracked_paths),
            f"[{_confidence_style(point.avg_top_confidence)}]{point.avg_top_confidence:.2f}[/]",
            f"{point.avg_bus_factor:.2f}",
        )
    console.print(table)


# ---------------------------------------------------------------------------
# cache subcommand group
# ---------------------------------------------------------------------------


@cache_app.command(name="path")
def cache_path_cmd() -> None:
    """Print the cache directory path."""
    typer.echo(str(cache_dir()))


@cache_app.command(name="info")
def cache_info_cmd(json_output: JsonOption = False) -> None:
    """Print cache file count, total size, and repository breakdown."""
    info = cache_info()
    if json_output:
        typer.echo(json.dumps(info, indent=2))
        return
    total_kb = info["total_bytes"] / 1024.0
    console.print(f"Files: {info['file_count']}")
    console.print(f"Total size: {total_kb:.1f} KB ({info['total_bytes']} bytes)")
    if info["repos"]:
        console.print("[bold]Repository breakdown:[/bold]")
        for repo_hash, detail in info["repos"].items():
            kb = detail["bytes"] / 1024.0
            console.print(f"  {repo_hash}: {detail['files']} file(s), {kb:.1f} KB")


@cache_app.command(name="clear")
def cache_clear_cmd(json_output: JsonOption = False) -> None:
    """Remove cached state and graph for the current repository."""
    repo_root = Path.cwd()
    removed = clear_repo_cache(repo_root)
    if json_output:
        typer.echo(json.dumps({"removed": removed}, indent=2))
        return
    if removed:
        console.print(f"[green]Removed {removed} file(s) for this repository.[/green]")
    else:
        console.print("[yellow]No cached files found for this repository.[/yellow]")


@cache_app.command(name="purge")
def cache_purge_cmd(json_output: JsonOption = False) -> None:
    """Purge the entire cache directory."""
    removed = purge_cache()
    if json_output:
        typer.echo(json.dumps({"removed": removed}, indent=2))
        return
    if removed:
        console.print(f"[green]Purged {removed} file(s) from the cache.[/green]")
    else:
        console.print("[yellow]Cache directory is already empty.[/yellow]")


def main() -> None:
    """Entry point for the checkowners CLI."""
    app()
