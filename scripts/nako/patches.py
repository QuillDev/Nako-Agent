#!/usr/bin/env python3
"""Manage the Nako-Agent patch stack.

The patches live as files in `patches/`, applying to the upstream commit named in `upstream`.
`apply` turns them into a commit stack on the `patches` branch in `work/`, where every other
command operates; each finished change is written back to `patches/`.

Designed for turn-based, non-interactive agent workflows (no TTY menus or
interactive editors). Every command prints structured state and exact next
actions, then exits so you can inspect, edit, test, and advance cleanly.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[2]
PATCH_DIR = SOURCE_ROOT / "patches"
PIN_FILE = SOURCE_ROOT / "upstream"
WORK_DIR = SOURCE_ROOT / "work"
_META_FILENAME = "nako-rebase-meta.json"


@dataclass(frozen=True)
class PatchInfo:
    index: int
    sha: str
    short_sha: str
    subject: str
    files: tuple[str, ...]


@dataclass(frozen=True)
class PlanEntry:
    index: int
    sha: str
    short_sha: str
    subject: str
    status: str  # clean | upstreamed | conflict
    files: tuple[str, ...]
    conflict_files: tuple[str, ...] = ()
    overlap_files: tuple[str, ...] = ()
    upstream_commits: tuple[str, ...] = ()
    note: str = ""


def run_git(
    repo: Path,
    *args: str,
    check: bool = True,
    capture: bool = True,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=capture,
        stdin=subprocess.DEVNULL,
        check=check,
    )


def repo_root(explicit: str | None = None, must_exist: bool = True) -> Path:
    """The work tree holding the applied stack: `work/` beside `patches/` unless overridden."""
    repo = Path(explicit).resolve() if explicit else WORK_DIR
    if must_exist and not repo.is_dir():
        raise RuntimeError(f"No work tree at {repo}. Run `python3 scripts/nako/patches.py apply` first.")
    return repo


def git_dir(repo: Path) -> Path:
    proc = run_git(repo, "rev-parse", "--path-format=absolute", "--git-dir")
    return Path(proc.stdout.strip())


def rebase_dir(repo: Path) -> Path | None:
    gdir = git_dir(repo)
    for name in ("rebase-merge", "rebase-apply"):
        candidate = gdir / name
        if candidate.is_dir():
            return candidate
    return None


def _meta_path(repo: Path) -> Path:
    return git_dir(repo) / _META_FILENAME


def save_rebase_meta(repo: Path, data: dict) -> None:
    _meta_path(repo).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def load_rebase_meta(repo: Path) -> dict:
    path = _meta_path(repo)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}


def clear_rebase_meta(repo: Path) -> None:
    path = _meta_path(repo)
    if path.is_file():
        path.unlink(missing_ok=True)


def current_branch(repo: Path) -> str:
    proc = run_git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return proc.stdout.strip()


def check_branch_worktree(repo: Path, branch: str, allowed_wt: Path | None = None) -> Path | None:
    """Return the other worktree path if `branch` is checked out elsewhere."""
    proc = run_git(repo, "worktree", "list", "--porcelain")
    target_ref = f"refs/heads/{branch}"
    current_wt: Path | None = None
    allowed_resolved = (allowed_wt or repo).resolve()
    for line in proc.stdout.splitlines():
        if line.startswith("worktree "):
            current_wt = Path(line[len("worktree "):]).resolve()
        elif line.startswith("branch ") and current_wt is not None:
            if line[len("branch "):] == target_ref and current_wt != allowed_resolved:
                return current_wt
        elif not line.strip():
            current_wt = None
    return None


def list_patches(repo: Path, base_ref: str = "upstream/main", branch: str = "HEAD") -> list[PatchInfo]:
    proc = run_git(
        repo,
        "log",
        "--reverse",
        "--no-renames",
        "--name-only",
        "-z",
        "--format=%x1e%H%x00%h%x00%s%x1f",
        f"{base_ref}..{branch}",
    )
    patches: list[PatchInfo] = []
    for idx, rec in enumerate((r for r in proc.stdout.split("\x1e") if r), 1):
        header, _, files_part = rec.partition("\x1f")
        sha, short_sha, subject = header.split("\x00", 2)
        files_raw = files_part.lstrip("\x00\n")
        files = tuple(entry for entry in files_raw.split("\x00") if entry)
        patches.append(PatchInfo(idx, sha, short_sha, subject, files))
    return patches


def _stack_base(repo: Path, base_ref: str) -> str:
    """The commit the stack sits on now, so reworking a patch never pulls in new upstream."""
    return run_git(repo, "merge-base", "HEAD", base_ref).stdout.strip()


# Fixed diff settings so the same stack always exports the same bytes, whatever the local git config.
_FORMAT_PATCH = (
    "format-patch", "-k", "--zero-commit", "--full-index", "--binary", "--no-signature", "--no-stat",
    "--no-renames", "--diff-algorithm=myers", "-U3", "--src-prefix=a/", "--dst-prefix=b/",
)


def read_pin() -> str:
    if not PIN_FILE.is_file():
        raise RuntimeError(f"Missing {PIN_FILE}: it names the upstream commit the patches apply to.")
    return PIN_FILE.read_text(encoding="utf-8-sig").strip()


def _render_stack(repo: Path, base_ref: str, branch: str, out_dir: Path) -> str:
    """Write the stack on `branch` as patch files into `out_dir`; return the commit it sits on."""
    base = run_git(repo, "merge-base", branch, base_ref).stdout.strip()
    run_git(repo, *_FORMAT_PATCH, "-o", str(out_dir), f"{base}..{branch}")
    return base


def _patch_dirs_equal(left: Path, right: Path) -> bool:
    left_files = sorted(left.glob("*.patch"))
    right_files = sorted(right.glob("*.patch"))
    if [f.name for f in left_files] != [f.name for f in right_files]:
        return False
    for a, b in zip(left_files, right_files):
        if a.stat().st_size != b.stat().st_size:
            return False
    for a, b in zip(left_files, right_files):
        if a.read_bytes() != b.read_bytes():
            return False
    return True


def exported_matches(repo: Path, base_ref: str = "upstream/main", branch: str = "patches") -> bool:
    """True when `patches/` and the pin already describe the stack on `branch`."""
    if not PIN_FILE.is_file() or not PATCH_DIR.is_dir():
        return False
    base = run_git(repo, "merge-base", branch, base_ref).stdout.strip()
    if read_pin() != base:
        return False
    with tempfile.TemporaryDirectory() as tmp:
        run_git(repo, *_FORMAT_PATCH, "-o", tmp, f"{base}..{branch}")
        return _patch_dirs_equal(Path(tmp), PATCH_DIR)


def export_patches(repo: Path, base_ref: str = "upstream/main", branch: str = "patches") -> None:
    """Rewrite `patches/` and the pin from the stack on `branch`."""
    with tempfile.TemporaryDirectory() as tmp:
        base = _render_stack(repo, base_ref, branch, Path(tmp))
        PATCH_DIR.mkdir(exist_ok=True)
        for old in PATCH_DIR.glob("*.patch"):
            old.unlink()
        count = 0
        for new in sorted(Path(tmp).glob("*.patch")):
            shutil.copy(new, PATCH_DIR / new.name)
            count += 1
    PIN_FILE.write_text(base + "\n", encoding="utf-8")
    print(f"==> Exported {count} patch(es) to {PATCH_DIR} (upstream {base[:10]})")


def resolve_target(patches: list[PatchInfo], selector: str) -> PatchInfo:
    if not patches:
        raise RuntimeError("Patch stack is empty; nothing to target.")
    if selector.isdigit():
        idx = int(selector)
        for p in patches:
            if p.index == idx:
                return p
        raise RuntimeError(f"Patch index {idx} out of range (1..{len(patches)}).")

    needle = selector.lower()
    sha_hits = [p for p in patches if p.sha.lower().startswith(needle)]
    if len(sha_hits) == 1:
        return sha_hits[0]
    if len(sha_hits) > 1:
        raise RuntimeError(f"Ambiguous SHA prefix {selector!r}: matches {[p.short_sha for p in sha_hits]}")

    subj_hits = [p for p in patches if needle in p.subject.lower()]
    if len(subj_hits) == 1:
        return subj_hits[0]
    if len(subj_hits) > 1:
        matches = ", ".join(f"#{p.index} {p.short_sha} ({p.subject})" for p in subj_hits)
        raise RuntimeError(f"Ambiguous selector {selector!r}: matches {matches}")

    raise RuntimeError(f"No patch matches {selector!r}. Run `python3 scripts/nako/patches.py list`.")


def _read_small(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8-sig", errors="replace").strip()


def unmerged_files(repo: Path) -> list[str]:
    proc = run_git(repo, "diff", "--name-only", "--diff-filter=U")
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _conflict_marker_lines(path: Path) -> list[int] | None:
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    lines: list[int] = []
    for idx, line in enumerate(text.splitlines(), 1):
        if line.startswith(("<<<<<<<", "=======", ">>>>>>>")):
            lines.append(idx)
    return lines


def _conflict_block_starts(path: Path) -> list[int] | None:
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    return [idx for idx, line in enumerate(text.splitlines(), 1) if line.startswith("<<<<<<<")]


def _upstream_commits_for_files(
    repo: Path, old_base: str, new_base: str, files: list[str] | tuple[str, ...], limit: int = 8
) -> list[str]:
    if not old_base or not new_base or old_base == new_base or not files:
        return []
    proc = run_git(
        repo,
        "log",
        "--oneline",
        f"-n{limit}",
        f"{old_base}..{new_base}",
        "--",
        *files,
        check=False,
    )
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def rebase_state_dict(repo: Path) -> dict:
    rdir = rebase_dir(repo)
    meta = load_rebase_meta(repo)
    msgnum = _read_small(rdir / "msgnum") if rdir else ""
    total = _read_small(rdir / "end") if rdir else ""
    conflicts = unmerged_files(repo)

    head_ref = "REBASE_HEAD" if conflicts else "HEAD"
    head_proc = run_git(repo, "show", "-s", "--format=%H%x00%h%x00%s", head_ref, check=False)
    if head_proc.returncode != 0 and head_ref == "REBASE_HEAD":
        head_proc = run_git(repo, "show", "-s", "--format=%H%x00%h%x00%s", "HEAD", check=False)
    parts = head_proc.stdout.rstrip("\n").split("\0") if head_proc.returncode == 0 else []
    sha = parts[0] if len(parts) > 0 else ""
    short_sha = parts[1] if len(parts) > 1 else ""
    subject = parts[2] if len(parts) > 2 else "(unknown commit)"

    patch_files: list[str] = []
    if sha:
        fproc = run_git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", sha, check=False)
        if fproc.returncode == 0:
            patch_files = [f for f in fproc.stdout.splitlines() if f]

    conflict_details = []
    for rel in conflicts:
        starts = _conflict_block_starts(repo / rel)
        conflict_details.append({"path": rel, "blocks": starts})

    upstream_commits = _upstream_commits_for_files(
        repo,
        meta.get("old_base", ""),
        meta.get("new_base", ""),
        conflicts or patch_files,
    )

    state = "conflict" if conflicts else "edit"
    return {
        "state": state,
        "mode": meta.get("mode", "rebase"),
        "step": int(msgnum) if msgnum.isdigit() else None,
        "total_steps": int(total) if total.isdigit() else None,
        "patch": {
            "sha": sha,
            "short_sha": short_sha,
            "subject": subject,
            "files": patch_files,
        },
        "conflicts": conflict_details,
        "upstream_commits": upstream_commits,
        "next_commands": (
            {
                "continue": "python3 scripts/nako/patches.py continue --rebuild",
                "skip": "python3 scripts/nako/patches.py skip --rebuild",
                "abort": "python3 scripts/nako/patches.py abort",
            }
            if state == "conflict"
            else {
                "continue": "python3 scripts/nako/patches.py continue --rebuild",
                "abort": "python3 scripts/nako/patches.py abort",
            }
        ),
    }


def report_rebase_conflict(repo: Path, stream=sys.stderr) -> None:
    info = rebase_state_dict(repo)
    state = info["state"]
    patch = info["patch"]
    conflicts = info["conflicts"]

    title = "CONFLICT DURING REBASE" if state == "conflict" else "PAUSED FOR PATCH EDIT"
    print("", file=stream)
    print(title, file=stream)
    print("-" * len(title), file=stream)
    if info["step"] and info["total_steps"]:
        print(f"Progress:   step {info['step']} of {info['total_steps']}", file=stream)
    print(f"Patch:      {patch['short_sha']} {patch['subject']}", file=stream)
    if state == "conflict":
        print(f"Conflicts:  {len(conflicts)} file(s)", file=stream)
        for item in conflicts:
            rel = item["path"]
            blocks = item["blocks"]
            if blocks is None:
                detail = "delete/modify conflict"
            elif not blocks:
                detail = "no inline markers (mode/rename/delete conflict)"
            else:
                loc = ", ".join(f"L{n}" for n in blocks[:6])
                if len(blocks) > 6:
                    loc += f", +{len(blocks) - 6} more"
                detail = f"{len(blocks)} block(s): {loc}"
            print(f"  - {rel} ({detail})", file=stream)
        if info["upstream_commits"]:
            print("Upstream commits touching conflicted file(s):", file=stream)
            for line in info["upstream_commits"]:
                print(f"  - {line}", file=stream)
        print("", file=stream)
        print("Inspect original patch:  git show REBASE_HEAD", file=stream)
        print("Next steps:", file=stream)
        print("  1. Edit the conflicted file(s) above to resolve markers.", file=stream)
        print("  2. Continue (auto-stages resolved files):  python3 scripts/nako/patches.py continue --rebuild", file=stream)
        print("     Drop if already upstreamed:             python3 scripts/nako/patches.py skip --rebuild", file=stream)
        print("     Back out cleanly:                       python3 scripts/nako/patches.py abort", file=stream)
    else:
        if patch["files"]:
            print(f"Files:      {', '.join(patch['files'][:10])}", file=stream)
        print("", file=stream)
        print("Next steps:", file=stream)
        print("  1. Make edits and run any tests against this commit.", file=stream)
        print("  2. Amend & replay remaining patches:  python3 scripts/nako/patches.py continue --rebuild", file=stream)
        print("     (pass -m \"...\" to also update the commit message)", file=stream)
        print("     Back out cleanly:                  python3 scripts/nako/patches.py abort", file=stream)


def build_update_plan(
    repo: Path,
    base_ref: str = "upstream/main",
    branch: str = "patches",
) -> dict:
    """Simulate rebasing `branch` onto `base_ref` in memory via `git merge-tree`."""
    old_base = run_git(repo, "merge-base", branch, base_ref).stdout.strip()
    new_base = run_git(repo, "rev-parse", base_ref).stdout.strip()
    old_short = run_git(repo, "rev-parse", "--short", old_base).stdout.strip()
    new_short = run_git(repo, "rev-parse", "--short", new_base).stdout.strip()
    behind = int(run_git(repo, "rev-list", "--count", f"{old_base}..{new_base}").stdout.strip())

    upstream_files: set[str] = set()
    if behind > 0:
        diff_out = run_git(repo, "diff", "--name-only", f"{old_base}..{new_base}").stdout
        upstream_files = {f for f in diff_out.splitlines() if f}

    patches = list_patches(repo, base_ref=old_base, branch=branch)
    acc_tree = run_git(repo, "rev-parse", f"{new_base}^{{tree}}").stdout.strip()
    prior_conflict_files: set[str] = set()
    entries: list[PlanEntry] = []

    for p in patches:
        overlap = tuple(sorted(f for f in p.files if f in upstream_files))
        if behind == 0:
            entries.append(
                PlanEntry(
                    index=p.index,
                    sha=p.sha,
                    short_sha=p.short_sha,
                    subject=p.subject,
                    status="clean",
                    files=p.files,
                )
            )
            continue

        proc = run_git(
            repo,
            "merge-tree",
            "--write-tree",
            "--name-only",
            "--messages",
            f"--merge-base={p.sha}^",
            acc_tree,
            p.sha,
            check=False,
        )
        lines = proc.stdout.splitlines()
        out_tree = lines[0].strip() if lines else ""

        if proc.returncode == 0:
            if out_tree == acc_tree:
                entries.append(
                    PlanEntry(
                        index=p.index,
                        sha=p.sha,
                        short_sha=p.short_sha,
                        subject=p.subject,
                        status="upstreamed",
                        files=p.files,
                        overlap_files=overlap,
                        upstream_commits=tuple(
                            _upstream_commits_for_files(repo, old_base, new_base, p.files, limit=5)
                        ),
                        note="diff is empty on new upstream; will auto-drop",
                    )
                )
            else:
                acc_tree = out_tree
                note = ""
                if overlap:
                    note = f"merged cleanly, but upstream also touched {len(overlap)} file(s)"
                entries.append(
                    PlanEntry(
                        index=p.index,
                        sha=p.sha,
                        short_sha=p.short_sha,
                        subject=p.subject,
                        status="clean",
                        files=p.files,
                        overlap_files=overlap,
                        note=note,
                    )
                )
        else:
            conflict_files: list[str] = []
            for line in lines[1:]:
                if not line.strip():
                    break
                conflict_files.append(line.strip())
            prior_overlap = sorted(set(p.files) & prior_conflict_files)
            prior_conflict_files.update(conflict_files)
            note = ""
            if prior_overlap:
                note = f"also overlaps earlier conflicted file(s): {', '.join(prior_overlap)}"
            entries.append(
                PlanEntry(
                    index=p.index,
                    sha=p.sha,
                    short_sha=p.short_sha,
                    subject=p.subject,
                    status="conflict",
                    files=p.files,
                    conflict_files=tuple(conflict_files),
                    overlap_files=overlap,
                    upstream_commits=tuple(
                        _upstream_commits_for_files(repo, old_base, new_base, conflict_files, limit=5)
                    ),
                    note=note,
                )
            )

    counts = {
        "clean": sum(1 for e in entries if e.status == "clean"),
        "upstreamed": sum(1 for e in entries if e.status == "upstreamed"),
        "conflict": sum(1 for e in entries if e.status == "conflict"),
    }
    if behind == 0:
        next_cmd = "Already up to date with upstream."
    else:
        next_cmd = "python3 scripts/nako/patches.py update --skip-fetch"

    return {
        "branch": branch,
        "base_ref": base_ref,
        "old_base": old_short,
        "new_base": new_short,
        "behind": behind,
        "counts": counts,
        "patches": [asdict(e) for e in entries],
        "next_command": next_cmd,
    }


def print_update_plan(plan: dict, stream=sys.stdout) -> None:
    print(
        f"Update plan: {plan['branch']} ({plan['old_base']}) -> "
        f"{plan['base_ref']} ({plan['new_base']}) | {plan['behind']} new upstream commit(s)",
        file=stream,
    )
    c = plan["counts"]
    print(
        f"Summary: {c['clean']} clean, {c['upstreamed']} upstreamed (auto-drop), {c['conflict']} conflict",
        file=stream,
    )
    for item in plan["patches"]:
        status = item["status"].upper()
        print(
            f"  {item['index']}. [{status:10}] {item['short_sha']} {item['subject']}",
            file=stream,
        )
        if item["note"]:
            print(f"       note: {item['note']}", file=stream)
        if item["conflict_files"]:
            print(f"       conflicts: {', '.join(item['conflict_files'])}", file=stream)
        if item["upstream_commits"]:
            print("       upstream commits:", file=stream)
            for uline in item["upstream_commits"]:
                print(f"         - {uline}", file=stream)
    print(f"Next: {plan['next_command']}", file=stream)


def _finish(repo: Path, args: argparse.Namespace) -> int:
    """A finished stack change is written to `patches/` before anything is built from it."""
    branch = current_branch(repo)
    if branch != "patches":
        print(
            f"Note: on `{branch}`, not `patches` — nothing exported. Fast-forward `patches` to `{branch}` "
            "and run `python3 scripts/nako/patches.py export`.",
            file=sys.stderr,
        )
        return 2 if args.rebuild or args.push else 0
    export_patches(repo)
    if args.rebuild or args.push:
        return _run_rebuild(repo, push=args.push)
    return 0


def _run_rebuild(repo: Path, push: bool) -> int:
    sync_script = Path(__file__).resolve().parent / "sync.py"
    cmd = [sys.executable, str(sync_script), "--repo", str(repo), "--skip-fetch", "--skip-rebase"]
    if push:
        cmd.append("--push")
    proc = subprocess.run(cmd, cwd=repo, stdin=subprocess.DEVNULL, check=False)
    return proc.returncode


def _report_auto_sunsetted(repo: Path, meta: dict) -> None:
    before = meta.get("before_patches") or []
    base_ref = meta.get("base_ref")
    branch = meta.get("patches_branch") or current_branch(repo)
    if not before or not base_ref:
        return
    after = list_patches(repo, base_ref=base_ref, branch=branch)
    after_subjects = {p.subject for p in after}
    for item in before:
        if item.get("subject") not in after_subjects:
            print(f"==> Auto-sunsetted (already upstreamed): {item.get('short_sha')} {item.get('subject')}")


def _find_leftover_markers(repo: Path, rel_paths: list[str]) -> list[tuple[str, list[int]]]:
    leftovers: list[tuple[str, list[int]]] = []
    for rel in rel_paths:
        lines = _conflict_marker_lines(repo / rel)
        if lines:
            leftovers.append((rel, lines))
    return leftovers


def _run_internal_seq_edit(todo_path: Path) -> int:
    target_sha = os.environ.get("NAKO_EDIT_SHA", "").strip().lower()
    if not target_sha:
        print("patches.py _seq-edit: NAKO_EDIT_SHA is not set", file=sys.stderr)
        return 2
    lines = todo_path.read_text(encoding="utf-8-sig").splitlines()
    matched = False
    out: list[str] = []
    for line in lines:
        parts = line.split()
        cmd = parts[0] if parts else ""
        token = parts[1].lower() if len(parts) >= 2 else ""
        if cmd in ("pick", "p") and len(token) >= 4 and (
            target_sha.startswith(token) or token.startswith(target_sha)
        ):
            out.append("edit" + line[len(cmd):])
            matched = True
        else:
            out.append(line)
    if not matched:
        print(f"patches.py _seq-edit: target {target_sha} not found in rebase todo", file=sys.stderr)
        return 2
    todo_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return 0


def cmd_list(repo: Path, args: argparse.Namespace) -> int:
    base_sha = run_git(repo, "merge-base", args.branch, args.base).stdout.strip()[:10]
    patches = list_patches(repo, base_ref=args.base, branch=args.branch)
    if args.json:
        print(json.dumps({"base": base_sha, "branch": args.branch, "patches": [asdict(p) for p in patches]}, indent=2))
        return 0
    print(f"Patch stack on {args.branch} above upstream {base_sha}: {len(patches)} patch(es)")
    if not patches:
        return 0
    for p in patches:
        print(f"  {p.index}. {p.short_sha} {p.subject} ({len(p.files)} file(s))")
        if args.files:
            for f in p.files:
                print(f"       {f}")
    return 0


def cmd_status(repo: Path, args: argparse.Namespace) -> int:
    if rebase_dir(repo) is not None:
        info = rebase_state_dict(repo)
        if args.json:
            print(json.dumps(info, indent=2))
        else:
            report_rebase_conflict(repo, stream=sys.stdout)
        return 1 if info["state"] == "conflict" else 0

    branch = current_branch(repo)
    revs = run_git(repo, "rev-parse", args.base, args.patches_branch, args.target_branch).stdout.splitlines()
    upstream_full, patches_full, main_full = revs[0].strip(), revs[1].strip(), revs[2].strip()
    upstream_sha, patches_sha, main_sha = upstream_full[:10], patches_full[:10], main_full[:10]
    behind = int(run_git(repo, "rev-list", "--count", f"{args.patches_branch}..{args.base}").stdout.strip())
    pinned_sha = run_git(repo, "merge-base", args.patches_branch, args.base).stdout.strip()[:10]
    patches = list_patches(repo, base_ref=args.base, branch=args.patches_branch)

    main_parent_proc = run_git(repo, "rev-parse", f"{args.target_branch}^", check=False)
    main_in_sync = main_parent_proc.returncode == 0 and main_parent_proc.stdout.strip() == patches_full
    dirty = bool(run_git(repo, "status", "--porcelain").stdout.strip())
    exported = exported_matches(repo, args.base, args.patches_branch)

    if args.json:
        print(
            json.dumps(
                {
                    "state": "idle",
                    "branch": branch,
                    "dirty": dirty,
                    "pinned": pinned_sha,
                    "upstream": upstream_sha,
                    "patches": patches_sha,
                    "patch_count": len(patches),
                    "behind_upstream": behind,
                    "main": main_sha,
                    "main_in_sync": main_in_sync,
                    "exported": exported,
                },
                indent=2,
            )
        )
        return 0

    print(f"Branch:   {branch} ({'dirty' if dirty else 'clean'})")
    print(f"Pinned:   upstream @ {pinned_sha} ({behind} commit(s) behind {args.base} @ {upstream_sha})")
    print(f"Patches:  {args.patches_branch} @ {patches_sha} ({len(patches)} patch(es))")
    sync_label = "built from this stack" if main_in_sync else "not built from this stack; before publishing: python3 scripts/nako/sync.py --skip-fetch --skip-rebase"
    print(f"Main:     {args.target_branch} @ {main_sha} ({sync_label})")
    export_label = "match this stack" if exported else "behind this stack; run: python3 scripts/nako/patches.py export"
    print(f"Files:    {PATCH_DIR} ({export_label})")
    return 0


def cmd_apply(repo: Path, args: argparse.Namespace) -> int:
    pin = read_pin()
    files = sorted(PATCH_DIR.glob("*.patch"))
    if run_git(SOURCE_ROOT, "cat-file", "-e", f"{pin}^{{commit}}", check=False).returncode != 0:
        raise RuntimeError(f"Pinned upstream commit {pin} is not in this clone. Run `git fetch upstream main` first.")

    if repo.is_dir():
        if rebase_dir(repo) is not None:
            report_rebase_conflict(repo)
            return 1
        if run_git(repo, "status", "--porcelain").stdout.strip():
            print(f"{repo} is dirty; commit or discard the changes before applying.", file=sys.stderr)
            return 2
    other_wt = check_branch_worktree(SOURCE_ROOT, "patches", allowed_wt=repo)
    if other_wt is not None:
        print(
            f"Branch `patches` is checked out in another worktree: {other_wt}\n"
            "Switch that worktree to a different branch (or remove it) before applying.",
            file=sys.stderr,
        )
        return 2
    has_branch = run_git(SOURCE_ROOT, "rev-parse", "-q", "--verify", "refs/heads/patches", check=False).returncode == 0
    if has_branch and not args.force and not exported_matches(SOURCE_ROOT, args.base):
        print(
            "`patches` holds commits that are not in patches/. Keep them with "
            "`python3 scripts/nako/patches.py export`, or discard them with `apply --force`.",
            file=sys.stderr,
        )
        return 2

    if repo.is_dir():
        run_git(repo, "checkout", "-q", "-B", "patches", pin)
    else:
        run_git(SOURCE_ROOT, "worktree", "add", "-q", "-B", "patches", str(repo), pin)
    if files:
        # Committer date follows the author date so the same files always rebuild the same commits.
        proc = run_git(
            repo, "am", "-k", "--keep-cr", "--3way", "--committer-date-is-author-date",
            *(str(f) for f in files), check=False,
        )
        if proc.returncode != 0:
            run_git(repo, "am", "--abort", check=False)
            print(proc.stdout + proc.stderr, file=sys.stderr)
            print(f"patches/ does not apply to {pin[:10]}; `patches` was left at the pin.", file=sys.stderr)
            return 1
    print(f"==> Applied {len(files)} patch(es) onto upstream {pin[:10]} in {repo}")
    return 0


def cmd_export(repo: Path, args: argparse.Namespace) -> int:
    if rebase_dir(repo) is not None:
        report_rebase_conflict(repo)
        return 1
    export_patches(repo, base_ref=args.base)
    return 0


def cmd_plan(repo: Path, args: argparse.Namespace) -> int:
    if not args.skip_fetch:
        remote, _, remote_branch = args.base.partition("/")
        if remote and remote_branch:
            run_git(repo, "fetch", remote, remote_branch, capture=True)
    plan = build_update_plan(repo, base_ref=args.base, branch=args.branch)
    if args.json:
        print(json.dumps(plan, indent=2))
    else:
        print_update_plan(plan)
    return 1 if plan["counts"]["conflict"] > 0 else 0


def cmd_update(repo: Path, args: argparse.Namespace) -> int:
    if args.plan:
        plan_args = argparse.Namespace(
            base=f"{args.upstream_remote}/{args.upstream_branch}",
            branch=args.patches_branch,
            skip_fetch=args.skip_fetch,
            json=args.json,
        )
        return cmd_plan(repo, plan_args)
    sync_script = Path(__file__).resolve().parent / "sync.py"
    cmd = [
        sys.executable,
        str(sync_script),
        "--repo",
        str(repo),
        "--upstream-remote",
        args.upstream_remote,
        "--upstream-branch",
        args.upstream_branch,
        "--patches-branch",
        args.patches_branch,
        "--target-branch",
        args.target_branch,
    ]
    if args.skip_fetch:
        cmd.append("--skip-fetch")
    if args.push:
        cmd.append("--push")
    proc = subprocess.run(cmd, cwd=repo, stdin=subprocess.DEVNULL, check=False)
    return proc.returncode


def cmd_edit(repo: Path, args: argparse.Namespace) -> int:
    if rebase_dir(repo) is not None:
        report_rebase_conflict(repo)
        return 1

    branch = current_branch(repo)
    if branch == "main":
        print("Refusing to edit on `main`. Check out `patches` (or a worktree off `patches`) first.", file=sys.stderr)
        return 2

    if run_git(repo, "status", "--porcelain").stdout.strip():
        print("Working tree is dirty; commit, stash, or use `fixup` instead.", file=sys.stderr)
        return 2

    patches = list_patches(repo, base_ref=args.base, branch="HEAD")
    target = resolve_target(patches, args.target)

    save_rebase_meta(
        repo,
        {
            "mode": "edit",
            "base_ref": args.base,
            "patches_branch": branch,
            "target": {"sha": target.sha, "short_sha": target.short_sha, "subject": target.subject},
        },
    )
    seq_editor = f"{shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} _seq-edit"
    proc = run_git(
        repo,
        "rebase",
        "-i",
        _stack_base(repo, args.base),
        check=False,
        capture=True,
        env_extra={"GIT_SEQUENCE_EDITOR": seq_editor, "NAKO_EDIT_SHA": target.sha},
    )
    if rebase_dir(repo) is not None:
        report_rebase_conflict(repo, stream=sys.stdout)
        return 0
    clear_rebase_meta(repo)
    if proc.returncode != 0:
        print(proc.stderr or proc.stdout, file=sys.stderr)
        return proc.returncode
    return 0


def cmd_fixup(repo: Path, args: argparse.Namespace) -> int:
    if rebase_dir(repo) is not None:
        report_rebase_conflict(repo)
        return 1

    branch = current_branch(repo)
    if branch == "main":
        print("Refusing to fixup on `main`. Check out `patches` (or a worktree off `patches`) first.", file=sys.stderr)
        return 2

    patches = list_patches(repo, base_ref=args.base, branch="HEAD")
    target = resolve_target(patches, args.target)

    if args.all:
        run_git(repo, "add", "-u", capture=False)

    staged = run_git(repo, "diff", "--cached", "--name-only").stdout.strip()
    if not staged:
        print("No staged changes to fold into the patch. Stage files with `git add` or pass --all.", file=sys.stderr)
        return 2

    unstaged = run_git(repo, "diff", "--name-only").stdout.strip()
    if unstaged:
        print("Unstaged modifications present; stage or stash them (or pass --all) before fixup.", file=sys.stderr)
        return 2

    if args.message:
        commit_args = ["commit", "-m", f"amend! {target.subject}", "-m", args.message]
    else:
        commit_args = ["commit", f"--fixup={target.sha}"]

    print(f"==> Folding staged changes into patch #{target.index} {target.short_sha}: {target.subject}")
    run_git(repo, *commit_args, capture=False)

    save_rebase_meta(repo, {"mode": "fixup", "base_ref": args.base, "patches_branch": branch})
    proc = run_git(
        repo,
        "rebase",
        "-i",
        "--autosquash",
        "--empty=drop",
        _stack_base(repo, args.base),
        check=False,
        capture=False,
        env_extra={"GIT_SEQUENCE_EDITOR": "true", "GIT_EDITOR": "true"},
    )
    if proc.returncode != 0:
        report_rebase_conflict(repo)
        return proc.returncode

    clear_rebase_meta(repo)
    print(f"==> Folded fix into patch #{target.index} ({target.subject}) cleanly.")
    return _finish(repo, args)


def cmd_sunset(repo: Path, args: argparse.Namespace) -> int:
    if rebase_dir(repo) is not None:
        report_rebase_conflict(repo)
        return 1

    branch = current_branch(repo)
    if branch == "main":
        print("Refusing to sunset on `main`. Check out `patches` (or a worktree off `patches`) first.", file=sys.stderr)
        return 2

    if run_git(repo, "status", "--porcelain").stdout.strip():
        print("Working tree is dirty; commit or stash changes before sunsetting a patch.", file=sys.stderr)
        return 2

    patches = list_patches(repo, base_ref=args.base, branch="HEAD")
    target = resolve_target(patches, args.target)
    print(f"==> Sunsetting patch #{target.index} {target.short_sha}: {target.subject}")
    save_rebase_meta(repo, {"mode": "sunset", "base_ref": args.base, "patches_branch": branch})
    proc = run_git(
        repo,
        "rebase",
        "--empty=drop",
        "--onto",
        f"{target.sha}^",
        target.sha,
        branch,
        check=False,
        capture=False,
    )
    if proc.returncode != 0:
        report_rebase_conflict(repo)
        return proc.returncode

    clear_rebase_meta(repo)
    print(f"==> Sunsetted {target.short_sha}: {target.subject}")
    return _finish(repo, args)


def cmd_continue(repo: Path, args: argparse.Namespace) -> int:
    if rebase_dir(repo) is None:
        print("No rebase in progress.", file=sys.stderr)
        return 2

    meta = load_rebase_meta(repo)
    unmerged = unmerged_files(repo)

    modified = [
        line.strip()
        for line in run_git(repo, "diff", "--name-only").stdout.splitlines()
        if line.strip()
    ]
    staged = [
        line.strip()
        for line in run_git(repo, "diff", "--cached", "--name-only").stdout.splitlines()
        if line.strip()
    ]
    check_paths = sorted(set(unmerged) | set(modified) | set(staged))
    leftovers = _find_leftover_markers(repo, check_paths)
    if leftovers:
        print("Cannot continue — conflict markers still remain:", file=sys.stderr)
        for rel, lines in leftovers:
            loc = ", ".join(f"L{n}" for n in lines[:8])
            print(f"  - {rel} ({loc})", file=sys.stderr)
        return 1

    if unmerged:
        missing: list[str] = []
        to_stage: list[str] = []
        for rel in unmerged:
            if (repo / rel).exists():
                to_stage.append(rel)
            else:
                missing.append(rel)
        if to_stage:
            run_git(repo, "add", "--", *to_stage)
            print(f"==> Auto-staged resolved file(s): {', '.join(to_stage)}")
        if missing:
            print("Cannot continue — delete/modify conflict(s) still need explicit resolution:", file=sys.stderr)
            for rel in missing:
                print(f"  - {rel} (run `git rm {rel}` to delete, or restore the file and `git add {rel}`)", file=sys.stderr)
            return 1
    elif meta.get("mode") == "edit":
        if modified or args.all:
            run_git(repo, "add", "-u")
        has_staged = bool(run_git(repo, "diff", "--cached", "--name-only").stdout.strip())
        if has_staged or args.message:
            amend_cmd = ["commit", "--amend"]
            if args.message:
                amend_cmd += ["-m", args.message]
            else:
                amend_cmd.append("--no-edit")
            run_git(repo, *amend_cmd, capture=False)

    proc = run_git(
        repo,
        "rebase",
        "--continue",
        check=False,
        capture=False,
        env_extra={"GIT_EDITOR": "true"},
    )
    if proc.returncode != 0:
        if rebase_dir(repo) is not None:
            report_rebase_conflict(repo)
        return proc.returncode

    _report_auto_sunsetted(repo, meta)
    clear_rebase_meta(repo)
    print("==> Rebase completed cleanly.")
    return _finish(repo, args)


def cmd_skip(repo: Path, args: argparse.Namespace) -> int:
    if rebase_dir(repo) is None:
        print("No rebase in progress.", file=sys.stderr)
        return 2

    meta = load_rebase_meta(repo)
    head_proc = run_git(repo, "show", "-s", "--format=%h %s", "REBASE_HEAD", check=False)
    skipped = head_proc.stdout.strip() if head_proc.returncode == 0 else "current patch"
    print(f"==> Skipping patch: {skipped}")

    proc = run_git(repo, "rebase", "--skip", check=False, capture=False)
    if proc.returncode != 0:
        if rebase_dir(repo) is not None:
            report_rebase_conflict(repo)
        return proc.returncode

    _report_auto_sunsetted(repo, meta)
    clear_rebase_meta(repo)
    print("==> Rebase completed cleanly.")
    return _finish(repo, args)


def cmd_abort(repo: Path, _args: argparse.Namespace) -> int:
    if rebase_dir(repo) is None:
        clear_rebase_meta(repo)
        print("No rebase in progress.", file=sys.stderr)
        return 2
    run_git(repo, "rebase", "--abort", capture=False)
    clear_rebase_meta(repo)
    print(f"==> Aborted rebase; back on {current_branch(repo)}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(line_buffering=True)  # keep our lines in order with git's when piped
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if len(raw_argv) == 2 and raw_argv[0] == "_seq-edit":
        return _run_internal_seq_edit(Path(raw_argv[1]))

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", help="Path to the work tree (defaults to work/ beside patches/).")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="List the patches in the stack.")
    p_list.add_argument("--base", default="upstream/main")
    p_list.add_argument("--branch", default="patches")
    p_list.add_argument("--files", action="store_true", help="Also show files touched by each patch.")
    p_list.add_argument("--json", action="store_true", help="Emit JSON output.")

    p_status = sub.add_parser("status", help="Show patch stack or active rebase/edit dossier.")
    p_status.add_argument("--base", default="upstream/main")
    p_status.add_argument("--patches-branch", default="patches")
    p_status.add_argument("--target-branch", default="main")
    p_status.add_argument("--json", action="store_true", help="Emit JSON output.")

    p_apply = sub.add_parser("apply", help="Build the `patches` commit stack in the work tree from patches/.")
    p_apply.add_argument("--base", default="upstream/main")
    p_apply.add_argument("--force", action="store_true", help="Discard commits on `patches` that were never exported.")

    p_export = sub.add_parser("export", help="Rewrite patches/ and the upstream pin from the `patches` commit stack.")
    p_export.add_argument("--base", default="upstream/main")

    p_plan = sub.add_parser("plan", help="Non-destructive preflight: test every patch against upstream.")
    p_plan.add_argument("--base", default="upstream/main")
    p_plan.add_argument("--branch", default="patches")
    p_plan.add_argument("--skip-fetch", action="store_true", help="Do not fetch upstream first.")
    p_plan.add_argument("--json", action="store_true", help="Emit JSON output.")

    p_update = sub.add_parser("update", help="Fetch upstream, rebase patches, and rebuild main (or --plan).")
    p_update.add_argument("--plan", action="store_true", help="Preview which patches are clean/upstreamed/conflicting without mutating git state.")
    p_update.add_argument("--upstream-remote", default="upstream")
    p_update.add_argument("--upstream-branch", default="main")
    p_update.add_argument("--patches-branch", default="patches")
    p_update.add_argument("--target-branch", default="main")
    p_update.add_argument("--skip-fetch", action="store_true")
    p_update.add_argument("--push", action="store_true")
    p_update.add_argument("--json", action="store_true", help="With --plan, emit JSON output.")

    p_edit = sub.add_parser("edit", help="Pause non-interactively at an existing patch to edit/test it in place.")
    p_edit.add_argument("target", help="Patch index (from `list`), SHA prefix, or unique subject substring.")
    p_edit.add_argument("--base", default="upstream/main")

    p_fixup = sub.add_parser("fixup", help="Fold staged changes into an existing patch commit.")
    p_fixup.add_argument("target", help="Patch index (from `list`), SHA prefix, or unique subject substring.")
    p_fixup.add_argument("--all", action="store_true", help="Stage all tracked modifications before folding.")
    p_fixup.add_argument("--message", "-m", help="Optionally replace the target patch's commit message.")
    p_fixup.add_argument("--base", default="upstream/main")
    p_fixup.add_argument("--rebuild", action="store_true", help="Rebuild `main` after folding.")
    p_fixup.add_argument("--push", action="store_true", help="Rebuild `main` and push both branches.")

    p_sunset = sub.add_parser("sunset", help="Remove an upstreamed or obsolete patch from the stack.")
    p_sunset.add_argument("target", help="Patch index (from `list`), SHA prefix, or unique subject substring.")
    p_sunset.add_argument("--base", default="upstream/main")
    p_sunset.add_argument("--rebuild", action="store_true", help="Rebuild `main` after sunsetting.")
    p_sunset.add_argument("--push", action="store_true", help="Rebuild `main` and push both branches.")

    p_cont = sub.add_parser("continue", help="Validate, auto-stage/amend, and resume an active rebase or edit.")
    p_cont.add_argument("--message", "-m", help="When resuming `edit`, replace the patch's commit message.")
    p_cont.add_argument("--all", action="store_true", help="Stage tracked modifications before amending in `edit` mode.")
    p_cont.add_argument("--rebuild", action="store_true", help="Rebuild `main` once the rebase finishes.")
    p_cont.add_argument("--push", action="store_true", help="Rebuild `main` and push both branches once finished.")

    p_skip = sub.add_parser("skip", help="Drop the currently conflicting patch (e.g. already upstreamed).")
    p_skip.add_argument("--rebuild", action="store_true", help="Rebuild `main` once the rebase finishes.")
    p_skip.add_argument("--push", action="store_true", help="Rebuild `main` and push both branches once finished.")

    sub.add_parser("abort", help="Abort an active rebase or edit and clean up state.")

    args = parser.parse_args(raw_argv)
    try:
        repo = repo_root(args.repo, must_exist=args.command != "apply")
        handlers = {
            "apply": cmd_apply,
            "export": cmd_export,
            "list": cmd_list,
            "status": cmd_status,
            "plan": cmd_plan,
            "update": cmd_update,
            "edit": cmd_edit,
            "fixup": cmd_fixup,
            "sunset": cmd_sunset,
            "continue": cmd_continue,
            "skip": cmd_skip,
            "abort": cmd_abort,
        }
        return handlers[args.command](repo, args)
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"patches.py: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
