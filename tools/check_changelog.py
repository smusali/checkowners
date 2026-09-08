#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _normalize(raw: str) -> str:
    return raw[1:] if raw.startswith(("v", "V")) else raw


def _pyproject_version() -> str:
    text = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'(?m)^version = "([^"]+)"', text)
    if not match:
        raise ValueError("version missing from pyproject.toml")
    return match.group(1)


def _init_version() -> str:
    text = (_ROOT / "checkowners" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'(?m)^__version__ = "([^"]+)"', text)
    if not match:
        raise ValueError("__version__ missing from checkowners/__init__.py")
    return match.group(1)


def _action_input_default(name: str) -> str:
    text = (_ROOT / "action.yml").read_text(encoding="utf-8")
    match = re.search(
        rf"(?m)^  {re.escape(name)}:\n(?:    .*\n)*?    default: \"([^\"]+)\"",
        text,
    )
    if not match:
        raise ValueError(f"{name} default missing from action.yml")
    return match.group(1)


def _action_pinned_version() -> str:
    text = (_ROOT / "action.yml").read_text(encoding="utf-8")
    match = re.search(r'CHECKOWNERS_PINNED_VERSION: "([^"]+)"', text)
    if not match:
        raise ValueError("CHECKOWNERS_PINNED_VERSION missing from action.yml")
    return match.group(1)


def _check_pins(expected: str) -> list[str]:
    errors: list[str] = []
    sources = {
        "pyproject.toml": _pyproject_version(),
        "checkowners/__init__.py": _init_version(),
        "action.yml checkowners_version default": _action_input_default("checkowners_version"),
        "action.yml CHECKOWNERS_PINNED_VERSION": _action_pinned_version(),
    }
    for label, value in sources.items():
        if value != expected:
            errors.append(f"{label} is {value!r}, expected {expected!r}")

    wheel = _ROOT / f"checkowners-{expected}-py3-none-any.whl"
    if not wheel.is_file():
        errors.append(f"missing {wheel.name}")

    lock = _ROOT / "requirements.lock"
    if not lock.is_file() or lock.stat().st_size == 0:
        errors.append("requirements.lock is missing or empty")
    return errors


def _check_changelog(version: str) -> int:
    heading = re.compile(rf"^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}$")
    text = (_ROOT / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")
    if any(heading.match(line) for line in text.splitlines()):
        return 0
    print(f"::error::No dated changelog heading for {version}")
    return 1


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_changelog.py <version>")
        print("       check_changelog.py --check-pins")
        return 2

    if argv[1] == "--check-pins":
        try:
            errors = _check_pins(_pyproject_version())
        except ValueError as exc:
            print(f"::error::{exc}")
            return 1
        for err in errors:
            print(f"::error::{err}")
        return 1 if errors else 0

    version = _normalize(argv[1])
    if version.lower() == "unreleased":
        print("::error::Unreleased is not a dated release entry")
        return 1

    changelog_rc = _check_changelog(version)
    try:
        pin_errors = _check_pins(version)
    except ValueError as exc:
        print(f"::error::{exc}")
        return 1
    for err in pin_errors:
        print(f"::error::{err}")
    if changelog_rc or pin_errors:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
