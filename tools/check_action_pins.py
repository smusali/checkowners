#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_VERSION = re.compile(r"v?\d+\.\d+")
_USES = re.compile(
    r"""^\s*(?:-\s+)?uses:\s*(?P<q>['"]?)(?P<ref>.+?)(?P=q)(?:\s+#\s*(?P<comment>\S.*))?\s*$"""
)


def _is_local(ref: str) -> bool:
    return ref == "." or ref.startswith(("./", "../"))


def _display(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(_ROOT))
    except ValueError:
        return str(path)


def _check_line(path: Path, lineno: int, line: str) -> str | None:
    match = _USES.match(line)
    if not match:
        return None
    ref = match.group("ref").strip()
    comment = match.group("comment")
    if _is_local(ref):
        return None
    loc = f"{_display(path)}:{lineno}"
    if "@" not in ref:
        return f"{loc}: uses: {ref!r} is missing a ref"
    spec = ref.rsplit("@", 1)[1]
    if not _SHA.fullmatch(spec):
        return f"{loc}: uses: {ref!r} is not pinned to a 40-character SHA"
    if not comment or not _VERSION.search(comment):
        return f"{loc}: uses: {ref!r} needs a version comment like # v1.2.3"
    return None


def _targets() -> list[Path]:
    files = [_ROOT / "action.yml"]
    workflow_dir = _ROOT / ".github" / "workflows"
    if workflow_dir.is_dir():
        files.extend(sorted(workflow_dir.glob("*.yml")))
        files.extend(sorted(workflow_dir.glob("*.yaml")))
    return files


def _check_file(path: Path) -> list[str]:
    errors: list[str] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        error = _check_line(path, lineno, line)
        if error:
            errors.append(error)
    return errors


def main(argv: list[str]) -> int:
    paths = [Path(arg) for arg in argv[1:]] if len(argv) > 1 else _targets()
    errors: list[str] = []
    for path in paths:
        errors.extend(_check_file(path))
    for error in errors:
        print(f"::error::{error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
