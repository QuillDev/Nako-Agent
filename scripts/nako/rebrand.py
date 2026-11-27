#!/usr/bin/env python3
"""Deterministically rebrand the repository from upstream naming to Nako-Agent.

Designed to be run on top of the clean upstream-tracking ``patches`` branch to
generate the rebranded ``main`` branch. Idempotent: running twice produces no
additional changes.
"""

from __future__ import annotations

import argparse
import collections
from concurrent.futures import ProcessPoolExecutor
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import brand_art

_UPSTREAM_BRAND_BYTES = bytes([104, 101, 114, 109, 101, 115])
_UPSTREAM_BRAND = _UPSTREAM_BRAND_BYTES.decode("ascii")
_UPSTREAM_BRAND_TITLE = _UPSTREAM_BRAND.capitalize()

_UPSTREAM_ORG_BYTES = bytes([78, 111, 117, 115, 32, 82, 101, 115, 101, 97, 114, 99, 104])
_UPSTREAM_ORG = _UPSTREAM_ORG_BYTES.decode("ascii")

_UPSTREAM_ORG_SLUG_BYTES = bytes([110, 111, 117, 115, 114, 101, 115, 101, 97, 114, 99, 104])
_UPSTREAM_ORG_SLUG = _UPSTREAM_ORG_SLUG_BYTES.decode("ascii")

_PORTAL_HOST = "portal.nakode.moe"
_PAGES_HOST = "quilldev.github.io"

_ANY_TARGET_BYTES_RE = re.compile(
    b"|".join(
        re.escape(token)
        for token in (_UPSTREAM_BRAND_BYTES, _UPSTREAM_ORG_BYTES, _UPSTREAM_ORG_SLUG_BYTES)
    ),
    re.IGNORECASE,
)
_BRAND_RE = re.compile(re.escape(_UPSTREAM_BRAND), re.IGNORECASE)
_UPSTREAM_REPO_RE = re.compile(
    re.escape(_UPSTREAM_ORG_SLUG) + r"/" + re.escape(_UPSTREAM_BRAND) + r"-agent",
    re.IGNORECASE,
)
_UPSTREAM_HOST_RE = re.compile(
    r"(?:" + re.escape(_UPSTREAM_BRAND) + r"|nako)-+agent\." + re.escape(_UPSTREAM_ORG_SLUG) + r"\.com",
    re.IGNORECASE,
)
_UPSTREAM_PAGES_RE = re.compile(
    re.escape(_UPSTREAM_ORG_SLUG) + r"\.github\.io",
    re.IGNORECASE,
)
_UPSTREAM_ORG_RE = re.compile(re.escape(_UPSTREAM_ORG), re.IGNORECASE)
_NOUS_PREFIX_RE = re.compile(re.escape(_UPSTREAM_ORG_SLUG[:4]), re.IGNORECASE)
_BRAND_LOWER = _UPSTREAM_BRAND_BYTES.lower()
_ORG_PREFIX_LOWER = _UPSTREAM_ORG_SLUG_BYTES[:4].lower()

_README = Path(__file__).resolve().parents[2] / "README.md"  # main's README is this branch's, not upstream's renamed

_GUARD_ENTRY = '    "no-upstream-brand": ["scripts/nako/check_no_upstream_brand.py"],\n'
_GUARD_ANCHOR = '    "backend-imports": ["scripts/ci/check_backend_imports.py"],\n'


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


def _replace_brand_match(match: re.Match[str]) -> str:
    token = match.group(0)
    if token.islower():
        return "nako"
    if token.isupper():
        return "NAKO"
    if token[0].isupper() and token[1:].islower():
        return "Nako"
    return "nAkO" if token[0].islower() else "NaKo"


def _replace_org_match(match: re.Match[str]) -> str:
    token = match.group(0)
    if token.islower():
        return "quill dev"
    if token.isupper():
        return "QUILL DEV"
    return "Quill Dev"


def _is_license_file(rel: str) -> bool:
    return Path(rel).name.upper() == "LICENSE"


def _rebrand_text(text: str, *, preserve_org: bool = False) -> str:
    updated = text
    if _NOUS_PREFIX_RE.search(updated):
        updated = _UPSTREAM_REPO_RE.sub("QuillDev/Nako-Agent", updated)
        updated = _UPSTREAM_HOST_RE.sub(_PORTAL_HOST, updated)
        updated = _UPSTREAM_PAGES_RE.sub(_PAGES_HOST, updated)
        if not preserve_org:
            updated = _UPSTREAM_ORG_RE.sub(_replace_org_match, updated)
    updated = _BRAND_RE.sub(_replace_brand_match, updated)
    return updated


def _rebrand_path(rel: str) -> str:
    return _BRAND_RE.sub(_replace_brand_match, rel)


def _tracked_files(repo: Path) -> list[str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-z"],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        check=True,
    )
    raw = proc.stdout.decode("utf-8", "surrogateescape")
    return [entry for entry in raw.split("\0") if entry]


def _rename_paths(repo: Path, files: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    mapping: list[tuple[str, str]] = []
    destinations: dict[str, list[str]] = collections.defaultdict(list)
    new_files: list[str] = []

    for rel in files:
        new_rel = _rebrand_path(rel)
        destinations[new_rel.lower()].append(rel)
        new_files.append(new_rel)
        if new_rel != rel:
            mapping.append((rel, new_rel))

    collisions = {dst: srcs for dst, srcs in destinations.items() if len(srcs) > 1}
    if collisions:
        sample = list(collisions.items())[:10]
        raise RuntimeError(f"Path collision(s) during rebrand: {sample}")

    if not mapping:
        return new_files, mapping

    touched_dirs: set[Path] = set()
    for old_rel, new_rel in mapping:
        src = repo / old_rel
        dst = repo / new_rel
        if not src.exists() and not os.path.islink(src):
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        parent = src.parent
        while parent != repo:
            touched_dirs.add(parent)
            parent = parent.parent

    for directory in sorted(touched_dirs, key=lambda p: len(p.parts), reverse=True):
        if directory.is_dir():
            pycache = directory / "__pycache__"
            if pycache.is_dir() and list(directory.iterdir()) == [pycache]:
                shutil.rmtree(pycache, ignore_errors=True)
            try:
                directory.rmdir()
            except OSError:
                pass

    return new_files, mapping


def _tracked_files_and_symlinks(repo: Path) -> tuple[list[str], set[str]]:
    proc = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-s", "-z"],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        check=True,
    )
    raw = proc.stdout.decode("utf-8", "surrogateescape")
    files: list[str] = []
    symlinks: set[str] = set()
    for entry in raw.split("\0"):
        if not entry:
            continue
        meta, sep, path = entry.partition("\t")
        if not sep:
            continue
        files.append(path)
        if meta.startswith("120000 "):
            symlinks.add(path)
    return files, symlinks


def _grep_target_files(repo: Path) -> set[str]:
    """Tracked working-tree files containing any upstream brand/org needle."""
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "grep",
            "--no-recurse-submodules",
            "-a",
            "-i",
            "-l",
            "-F",
            "-z",
            "-e",
            _UPSTREAM_BRAND,
            "-e",
            _UPSTREAM_ORG,
            "-e",
            _UPSTREAM_ORG_SLUG,
        ],
        capture_output=True,
        env=env,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"git grep failed: {proc.stderr.decode('utf-8', 'replace').strip()}")
    raw = proc.stdout.decode("utf-8", "surrogateescape")
    return {entry for entry in raw.split("\0") if entry}


def _rewrite_chunk(args: tuple[str, list[str], str | None]) -> int:
    repo_str, chunk, readme_text = args
    repo = Path(repo_str)
    changed = 0
    for rel in chunk:
        full = repo / rel
        if os.path.islink(full):
            target = os.readlink(full)
            new_target = _rebrand_path(target)
            if new_target != target:
                full.unlink()
                os.symlink(new_target, full)
                changed += 1
            continue

        if not full.is_file():
            continue

        data = full.read_bytes()
        if rel != "README.md":
            lowered = data.lower()
            if _BRAND_LOWER not in lowered and _ORG_PREFIX_LOWER not in lowered:
                continue
            if not _ANY_TARGET_BYTES_RE.search(data):
                continue

        if b"\x00" in data[:8192]:
            continue

        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue

        if rel == "README.md":
            new_text = readme_text if readme_text is not None else _README.read_text(encoding="utf-8")
        else:
            new_text = _rebrand_text(text, preserve_org=_is_license_file(rel))

        if new_text != text:
            full.write_bytes(new_text.encode("utf-8"))
            changed += 1

    return changed


def _rewrite_contents(repo: Path, files: list[str]) -> int:
    readme_text = _README.read_text(encoding="utf-8") if "README.md" in files and _README.is_file() else None
    workers = min(8, os.cpu_count() or 1)
    if workers <= 1 or len(files) < 256:
        return _rewrite_chunk((str(repo), files, readme_text))
    chunk_size = max(64, (len(files) + workers * 2 - 1) // (workers * 2))
    tasks = [
        (str(repo), files[i : i + chunk_size], readme_text if "README.md" in files[i : i + chunk_size] else None)
        for i in range(0, len(files), chunk_size)
    ]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        return sum(pool.map(_rewrite_chunk, tasks))


def _wire_scripts_check(repo: Path) -> bool:
    check_script = repo / "scripts" / "check"
    if not check_script.is_file():
        return False
    text = check_script.read_text(encoding="utf-8-sig")
    if '"no-upstream-brand"' in text:
        return False
    if _GUARD_ANCHOR in text:
        updated = text.replace(_GUARD_ANCHOR, _GUARD_ANCHOR + _GUARD_ENTRY, 1)
    else:
        # Fallback: insert right before the closing brace of _GUARDS.
        marker = "_GUARDS = {"
        start = text.find(marker)
        if start == -1:
            raise RuntimeError("Could not locate _GUARDS in scripts/check")
        end = text.find("}\n", start)
        if end == -1:
            raise RuntimeError("Could not locate end of _GUARDS in scripts/check")
        updated = text[:end] + _GUARD_ENTRY + text[end:]
    check_script.write_text(updated, encoding="utf-8", newline="\n")
    return True


def _sync_shims(repo: Path) -> None:
    gen = repo / "scripts" / "move" / "generate_shims.py"
    if not gen.is_file():
        return
    proc = subprocess.run(
        [sys.executable, str(gen), "--check"],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        check=False,
    )
    if proc.returncode != 0:
        subprocess.run(
            [sys.executable, str(gen)],
            cwd=repo,
            stdin=subprocess.DEVNULL,
            check=True,
        )


def _stage_changes(repo: Path, mapping: list[tuple[str, str]]) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "add", "-A"],
        stdin=subprocess.DEVNULL,
        check=True,
    )
    if not mapping:
        return
    # Force-add renamed paths that were already tracked before the rebrand,
    # so .gitignore entries matching a directory (e.g. generated compat shims)
    # cannot silently drop previously tracked files.
    destinations = "\0".join(new_rel for _, new_rel in mapping).encode("utf-8", "surrogateescape")
    subprocess.run(
        ["git", "-C", str(repo), "add", "-f", "--pathspec-from-file=-", "--pathspec-file-nul"],
        input=destinations,
        check=True,
    )
    tracked = set(_tracked_files(repo))
    missing = [new_rel for _, new_rel in mapping if new_rel not in tracked]
    if missing:
        raise RuntimeError(f"Renamed tracked files missing from index: {missing[:10]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", help="Path to the repository root (defaults to git toplevel).")
    parser.add_argument(
        "--stage",
        action="store_true",
        help="Stage all rebranded changes (preserving tracked files matched by .gitignore).",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip running check_no_upstream_brand.py after rebranding.",
    )
    args = parser.parse_args(argv)

    repo = _repo_root(args.repo)
    files, symlinks = _tracked_files_and_symlinks(repo)
    candidates = _grep_target_files(repo) if files else set()
    candidates.update(symlinks)
    if "README.md" in files:
        candidates.add("README.md")
    new_files, mapping = _rename_paths(repo, files)
    rewrite_targets = [new_rel for old_rel, new_rel in zip(files, new_files) if old_rel in candidates]
    rewritten = _rewrite_contents(repo, rewrite_targets)
    wired_check = _wire_scripts_check(repo)
    _sync_shims(repo)
    art = brand_art.apply(repo)
    if args.stage:
        _stage_changes(repo, mapping)

    print(
        f"rebrand: renamed {len(mapping)} paths, rewrote {rewritten} files, replaced {art} logo files"
        + (", wired scripts/check" if wired_check else "")
    )

    if not args.no_verify:
        guard = repo / "scripts" / "nako" / "check_no_upstream_brand.py"
        proc = subprocess.run(
            [sys.executable, str(guard), "--repo", str(repo)],
            stdin=subprocess.DEVNULL,
            check=False,
        )
        if proc.returncode != 0:
            return proc.returncode
        print("rebrand: brand guard passed (0 leaks outside README.md)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
