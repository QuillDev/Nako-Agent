#!/usr/bin/env python3
"""Sync upstream fixes into `patches` and regenerate the rebranded `main` branch.

Workflow:
  1. Fetch upstream/main.
  2. Rebase `patches` onto upstream/main with --empty=drop (keeps custom patches
     in upstream naming and auto-drops patches already merged upstream).
     On conflict, prints a structured dossier (files, conflict line numbers,
     upstream commits touching those files, and exact next commands).
     When it lands, rewrite patches/ and the upstream pin.
  3. Reset `main` to `patches`, run scripts/nako/rebrand.py, verify the brand lint,
     and create a single rebrand commit on top.
  4. Optionally push the source branch and `main` (`main` with --force-with-lease).

Pass `--plan` for a non-destructive preflight check before rebasing.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

import patches as patch_lib


def _run(
    argv: list[str],
    cwd: Path,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=capture,
        stdin=subprocess.DEVNULL,
        check=check,
    )


def _ensure_clean(repo: Path) -> None:
    proc = _run(["git", "status", "--porcelain"], cwd=repo, capture=True)
    if proc.stdout.strip():
        raise RuntimeError("Working tree is dirty; commit or stash changes before syncing.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", help="Path to the work tree (defaults to work/ beside patches/).")
    parser.add_argument("--upstream-remote", default="upstream")
    parser.add_argument("--upstream-branch", default="main")
    parser.add_argument("--patches-branch", default="patches")
    parser.add_argument("--target-branch", default="main")
    parser.add_argument("--plan", action="store_true", help="Dry-run preflight: show which patches are clean/upstreamed/conflicting without mutating git state.")
    parser.add_argument("--json", action="store_true", help="With --plan, emit JSON output.")
    parser.add_argument("--skip-fetch", action="store_true", help="Do not fetch upstream.")
    parser.add_argument(
        "--skip-rebase",
        action="store_true",
        help="Skip rebasing patches onto upstream (e.g. after manual conflict resolution).",
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help="Push patches and target branch to origin with --force-with-lease.",
    )
    args = parser.parse_args(argv)

    repo = patch_lib.repo_root(args.repo)

    if patch_lib.rebase_dir(repo) is not None:
        patch_lib.report_rebase_conflict(repo)
        return 1

    upstream_ref = f"{args.upstream_remote}/{args.upstream_branch}"
    if args.plan:
        if not args.skip_fetch:
            _run(["git", "fetch", args.upstream_remote, args.upstream_branch], cwd=repo, capture=True)
        plan = patch_lib.build_update_plan(repo, base_ref=upstream_ref, branch=args.patches_branch)
        if args.json:
            print(json.dumps(plan, indent=2))
        else:
            patch_lib.print_update_plan(plan)
        return 1 if plan["counts"]["conflict"] > 0 else 0

    try:
        _ensure_clean(repo)
    except RuntimeError as exc:
        print(f"sync.py: {exc}", file=sys.stderr)
        return 2

    for branch_name in (
        ([args.patches_branch] if not args.skip_rebase else []) + [args.target_branch]
    ):
        other_wt = patch_lib.check_branch_worktree(repo, branch_name)
        if other_wt is not None:
            print(
                f"Branch `{branch_name}` is checked out in another worktree: {other_wt}\n"
                "Switch that worktree to a different branch (or remove it) before syncing.",
                file=sys.stderr,
            )
            return 2

    if not args.skip_fetch:
        print(f"==> Fetching {upstream_ref}")
        # With release tags: the build reads its version off the nearest one, so a fork whose tags
        # stop at an old release reports that release and fails plugins' version requirements.
        _run(["git", "fetch", "--tags", "--force", args.upstream_remote, args.upstream_branch], cwd=repo)

    if not args.skip_rebase:
        old_base = _run(
            ["git", "merge-base", args.patches_branch, upstream_ref],
            cwd=repo,
            capture=True,
        ).stdout.strip()
        new_base = _run(
            ["git", "rev-parse", upstream_ref],
            cwd=repo,
            capture=True,
        ).stdout.strip()
        behind = int(
            _run(
                ["git", "rev-list", "--count", f"{old_base}..{new_base}"],
                cwd=repo,
                capture=True,
            ).stdout.strip()
        )
        before = patch_lib.list_patches(repo, base_ref=old_base, branch=args.patches_branch)
        print(
            f"==> Rebasing {len(before)} patch(es) on {args.patches_branch} "
            f"onto {upstream_ref} ({behind} new upstream commit(s))"
        )
        _run(["git", "checkout", args.patches_branch], cwd=repo)
        patch_lib.save_rebase_meta(
            repo,
            {
                "mode": "sync",
                "old_base": old_base,
                "new_base": new_base,
                "base_ref": upstream_ref,
                "patches_branch": args.patches_branch,
                "before_patches": [
                    {"short_sha": p.short_sha, "subject": p.subject} for p in before
                ],
            },
        )
        proc = _run(["git", "rebase", "--empty=drop", upstream_ref], cwd=repo, check=False)
        if proc.returncode != 0:
            patch_lib.report_rebase_conflict(repo)
            return proc.returncode
        patch_lib.clear_rebase_meta(repo)
        patch_lib.export_patches(repo, base_ref=upstream_ref, branch=args.patches_branch)
        after = patch_lib.list_patches(repo, base_ref=upstream_ref, branch=args.patches_branch)
        after_subjects = {p.subject for p in after}
        for dropped in before:
            if dropped.subject not in after_subjects:
                print(f"==> Auto-sunsetted (already upstreamed): {dropped.short_sha} {dropped.subject}")

    patches_sha = _run(
        ["git", "rev-parse", "--short", args.patches_branch],
        cwd=repo,
        capture=True,
    ).stdout.strip()
    upstream_sha = _run(
        ["git", "rev-parse", "--short", upstream_ref],
        cwd=repo,
        capture=True,
    ).stdout.strip()

    print(f"==> Rebuilding {args.target_branch} from {args.patches_branch} ({patches_sha})")
    _run(["git", "checkout", "-B", args.target_branch, args.patches_branch], cwd=repo)

    # The stack carries no tooling, so `main` gets its brand guard and workflow from here.
    tooling = Path(__file__).resolve().parent
    (repo / "scripts" / "nako").mkdir(parents=True, exist_ok=True)
    shutil.copy(tooling / "check_no_upstream_brand.py", repo / "scripts" / "nako")
    shutil.copy(tooling / "nako-brand-lint.yml", repo / ".github" / "workflows")

    rebrand_script = tooling / "rebrand.py"
    _run(
        [sys.executable, str(rebrand_script), "--repo", str(repo), "--stage", "--no-verify"],
        cwd=repo,
    )

    print("==> Running upstream brand lint guard")
    guard_script = tooling / "check_no_upstream_brand.py"
    _run([sys.executable, str(guard_script), "--repo", str(repo), "--tracked-only"], cwd=repo)

    commit_msg = (
        "Rebrand to Nako-Agent\n\n"
        f"Generated from {args.patches_branch} ({patches_sha}) "
        f"based on {upstream_ref} ({upstream_sha}) via scripts/nako/rebrand.py."
    )
    _run(["git", "commit", "-m", commit_msg], cwd=repo)
    new_sha = _run(["git", "rev-parse", "--short", "HEAD"], cwd=repo, capture=True).stdout.strip()
    print(f"==> {args.target_branch} is ready at {new_sha}")
    _run(["git", "checkout", "--quiet", args.patches_branch], cwd=repo)

    if args.push:
        source = patch_lib.SOURCE_ROOT
        if _run(["git", "status", "--porcelain"], cwd=source, capture=True).stdout.strip():
            print(
                f"Not pushed: {source} has uncommitted changes. Commit them, then run "
                "`python3 scripts/nako/sync.py --skip-fetch --skip-rebase --push`.",
                file=sys.stderr,
            )
            return 1
        source_branch = patch_lib.current_branch(source)
        print(f"==> Pushing {source_branch} and {args.target_branch} to origin")
        _run(
            [
                "git", "push", "--atomic", f"--force-with-lease={args.target_branch}",
                "origin", source_branch, args.target_branch,
            ],
            cwd=repo,
        )
        _run(["git", "push", "--force", "--quiet", "origin", "--tags"], cwd=repo)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
