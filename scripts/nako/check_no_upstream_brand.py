#!/usr/bin/env python3
"""Fail if the upstream brand string appears anywhere outside README.md.

Checks every tracked (and optionally untracked non-ignored) path, symlink target,
and file content case-insensitively. Only ``README.md`` may mention the upstream
brand in its contents (never in any file path).
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

# Encoded as byte values so this guard itself contains zero literal matches.
_UPSTREAM_BRAND_BYTES = bytes([104, 101, 114, 109, 101, 115])
_UPSTREAM_BRAND = _UPSTREAM_BRAND_BYTES.decode("ascii")
_UPSTREAM_ORG_BYTES = bytes([78, 111, 117, 115, 32, 82, 101, 115, 101, 97, 114, 99, 104])
_UPSTREAM_ORG = _UPSTREAM_ORG_BYTES.decode("ascii")
_UPSTREAM_ORG_SLUG_BYTES = bytes([110, 111, 117, 115, 114, 101, 115, 101, 97, 114, 99, 104])
_UPSTREAM_ORG_SLUG = _UPSTREAM_ORG_SLUG_BYTES.decode("ascii")

_BYTES_RE = re.compile(re.escape(_UPSTREAM_BRAND_BYTES), re.IGNORECASE)
_TEXT_RE = re.compile(re.escape(_UPSTREAM_BRAND), re.IGNORECASE)
_ORG_BYTES_RE = re.compile(re.escape(_UPSTREAM_ORG_BYTES), re.IGNORECASE)
_ORG_TEXT_RE = re.compile(re.escape(_UPSTREAM_ORG), re.IGNORECASE)
_HOST_BYTES_RE = re.compile(
    b"(?:(?:" + re.escape(_UPSTREAM_BRAND_BYTES) + b"|nako)-+agent\\." + re.escape(_UPSTREAM_ORG_SLUG_BYTES) + b"\\.com|"
    + re.escape(_UPSTREAM_ORG_SLUG_BYTES) + b"\\.github\\.io)",
    re.IGNORECASE,
)
_HOST_TEXT_RE = re.compile(
    r"(?:(?:" + re.escape(_UPSTREAM_BRAND) + r"|nako)-+agent\." + re.escape(_UPSTREAM_ORG_SLUG) + r"\.com|"
    + re.escape(_UPSTREAM_ORG_SLUG) + r"\.github\.io)",
    re.IGNORECASE,
)

ALLOWED_CONTENT_FILES = frozenset({"README.md"})


def _is_license_file(rel: str) -> bool:
    return Path(rel).name.upper() == "LICENSE"


def _repo_root(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git rev-parse --show-toplevel failed: {proc.stderr.strip()}")
    return Path(proc.stdout.strip())


def _list_files(repo: Path, include_untracked: bool) -> list[str]:
    cmd = ["git", "-C", str(repo), "ls-files", "-z", "--cached"]
    if include_untracked:
        cmd.extend(["--others", "--exclude-standard"])
    proc = subprocess.run(
        cmd,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git ls-files failed: {proc.stderr.decode('utf-8', 'replace').strip()}")
    raw = proc.stdout.decode("utf-8", "surrogateescape")
    return [entry for entry in raw.split("\0") if entry]


def check_repo(repo: Path, include_untracked: bool = True) -> list[str]:
    violations: list[str] = []
    files = _list_files(repo, include_untracked=include_untracked)

    for rel in files:
        if _TEXT_RE.search(rel):
            violations.append(f"path: {rel}")

        full = repo / rel
        if os.path.islink(full):
            target = os.readlink(full)
            if _TEXT_RE.search(target):
                violations.append(f"symlink: {rel} -> {target}")
            continue

        if not full.is_file():
            continue

        if rel in ALLOWED_CONTENT_FILES:
            continue

        data = full.read_bytes()
        check_org = not _is_license_file(rel)
        hit_brand = bool(_BYTES_RE.search(data))
        hit_host = bool(_HOST_BYTES_RE.search(data))
        hit_org = check_org and bool(_ORG_BYTES_RE.search(data))
        if not (hit_brand or hit_host or hit_org):
            continue

        if b"\x00" in data[:8192]:
            violations.append(f"binary: {rel}")
            continue

        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            violations.append(f"non-utf8: {rel}")
            continue

        file_lines = text.splitlines()
        checks: list[tuple[str, bool, re.Pattern[str]]] = [
            ("content", hit_brand, _TEXT_RE),
            ("host", hit_host, _HOST_TEXT_RE),
            ("org", hit_org, _ORG_TEXT_RE),
        ]
        for label, active, pattern in checks:
            if not active:
                continue
            lines = [idx for idx, line in enumerate(file_lines, 1) if pattern.search(line)]
            if not lines:
                continue
            sample = ", ".join(str(n) for n in lines[:5])
            suffix = f" (+{len(lines) - 5} more)" if len(lines) > 5 else ""
            violations.append(f"{label}: {rel} (lines {sample}{suffix})")

    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", help="Path to the repository root (defaults to git toplevel).")
    parser.add_argument(
        "--tracked-only",
        action="store_true",
        help="Only scan git-tracked files (skip untracked non-ignored files).",
    )
    parser.add_argument(
        "--max-report",
        type=int,
        default=50,
        help="Maximum violations to print (0 = unlimited, default: 50).",
    )
    args = parser.parse_args(argv)

    try:
        repo = _repo_root(args.repo)
        violations = check_repo(repo, include_untracked=not args.tracked_only)
    except RuntimeError as exc:
        print(f"check_no_upstream_brand: {exc}", file=sys.stderr)
        return 2

    if not violations:
        return 0

    print(f"Upstream brand guard failed: {len(violations)} violation(s) outside README.md:", file=sys.stderr)
    limit = len(violations) if args.max_report <= 0 else args.max_report
    for item in violations[:limit]:
        print(f"  {item}", file=sys.stderr)
    if len(violations) > limit:
        print(f"  ... and {len(violations) - limit} more", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
