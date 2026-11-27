#!/usr/bin/env python3
"""Behavior tests for the Nako-Agent patch, rebrand, brand-art, and lint tooling."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import check_no_upstream_brand as brand_lint
import patches as patch_lib
import rebrand
import sync as sync_lib

try:
    from PIL import Image
    import brand_art
except ImportError:  # pragma: no cover - Pillow optional outside full builds
    Image = None
    brand_art = None


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        check=True,
    )
    return proc.stdout


def _init_repo(repo: Path) -> None:
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")


class BrandLintTests(unittest.TestCase):
    def test_detects_paths_symlinks_binary_host_org_and_respects_allowlists(self) -> None:
        brand = brand_lint._UPSTREAM_BRAND
        org = brand_lint._UPSTREAM_ORG
        slug = brand_lint._UPSTREAM_ORG_SLUG

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo)
            (repo / ".gitignore").write_text("ignored_tracked.txt\n", encoding="utf-8")
            (repo / "README.md").write_text(f"Built on {brand} by {org}.\n", encoding="utf-8")
            (repo / "LICENSE").write_text(f"Copyright {org}\n", encoding="utf-8")
            (repo / "clean.txt").write_text("nako agent portal.example.com\n", encoding="utf-8")
            (repo / f"{brand}_mod.py").write_text("x = 1\n", encoding="utf-8")
            (repo / "leak_content.txt").write_text(f"line1\nhello {brand.upper()}\n", encoding="utf-8")
            (repo / "leak_org.txt").write_text(f"by {org}\n", encoding="utf-8")
            (repo / "leak_host.txt").write_text(
                f"https://nako-agent.{slug}.com/docs\nhttps://{slug}.github.io/site\n",
                encoding="utf-8",
            )
            (repo / "allowed_slug_only.txt").write_text(f"https://portal.{slug}.com\n", encoding="utf-8")
            (repo / "leak_binary.bin").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00" + brand.encode("ascii") + b"\x00")
            (repo / "ignored_tracked.txt").write_text(f"tracked despite ignore: {brand}\n", encoding="utf-8")
            os.symlink(f"target/{brand}.txt", repo / "bad_link")
            os.symlink("leak_content.txt", repo / "good_link_to_leaking_file")
            (repo / "untracked_leak.txt").write_text(f"untracked {brand}\n", encoding="utf-8")

            _git(
                repo,
                "add",
                "-f",
                ".gitignore",
                "README.md",
                "LICENSE",
                "clean.txt",
                f"{brand}_mod.py",
                "leak_content.txt",
                "leak_org.txt",
                "leak_host.txt",
                "allowed_slug_only.txt",
                "leak_binary.bin",
                "ignored_tracked.txt",
                "bad_link",
                "good_link_to_leaking_file",
            )
            _git(repo, "commit", "-m", "fixture")

            tracked_only = brand_lint.check_repo(repo, include_untracked=False)
            self.assertIn(f"path: {brand}_mod.py", tracked_only)
            self.assertIn("content: leak_content.txt (lines 2)", tracked_only)
            self.assertIn("org: leak_org.txt (lines 1)", tracked_only)
            self.assertIn("host: leak_host.txt (lines 1, 2)", tracked_only)
            self.assertIn("binary: leak_binary.bin", tracked_only)
            self.assertIn("content: ignored_tracked.txt (lines 1)", tracked_only)
            self.assertIn(f"symlink: bad_link -> target/{brand}.txt", tracked_only)
            # Allowed/benign files must not be flagged
            for forbidden in ("README.md", "LICENSE", "clean.txt", "allowed_slug_only.txt", "good_link_to_leaking_file"):
                self.assertFalse(any(forbidden in v for v in tracked_only), f"unexpected violation for {forbidden}: {tracked_only}")
            self.assertFalse(any("untracked_leak.txt" in v for v in tracked_only))

            with_untracked = brand_lint.check_repo(repo, include_untracked=True)
            self.assertIn("content: untracked_leak.txt (lines 1)", with_untracked)


class RebrandTests(unittest.TestCase):
    def test_rebrand_text_and_end_to_end_is_clean_and_idempotent(self) -> None:
        brand = brand_lint._UPSTREAM_BRAND
        org = brand_lint._UPSTREAM_ORG
        slug = brand_lint._UPSTREAM_ORG_SLUG

        self.assertEqual(rebrand._rebrand_text(f"{brand} {brand.capitalize()} {brand.upper()}"), "nako Nako NAKO")
        self.assertEqual(rebrand._rebrand_text(f"https://github.com/{slug}/{brand}-agent"), "https://github.com/QuillDev/Nako-Agent")
        self.assertEqual(rebrand._rebrand_text(f"https://{brand}-agent.{slug}.com/docs"), f"https://{rebrand._PORTAL_HOST}/docs")
        self.assertEqual(rebrand._rebrand_text(f"https://{slug}.github.io/x"), f"https://{rebrand._PAGES_HOST}/x")
        self.assertEqual(rebrand._rebrand_text(f"Copyright {org}", preserve_org=True), f"Copyright {org}")
        self.assertEqual(rebrand._rebrand_text(f"Copyright {org}", preserve_org=False), "Copyright Quill Dev")

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo)
            (repo / f"{brand}_pkg").mkdir()
            (repo / f"{brand}_pkg" / f"{brand}_mod.py").write_text(
                f"NAME = '{brand.capitalize()}'\nURL = 'https://{brand}-agent.{slug}.com'\n",
                encoding="utf-8",
            )
            (repo / "README.md").write_text("upstream readme\n", encoding="utf-8")
            (repo / "LICENSE").write_text(f"Copyright {org}\n", encoding="utf-8")
            (repo / "untouched.txt").write_text("plain text\n", encoding="utf-8")
            os.symlink(f"{brand}_pkg/{brand}_mod.py", repo / "entry_link")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-m", "upstream")

            rc = rebrand.main(["--repo", str(repo), "--stage", "--no-verify"])
            self.assertEqual(rc, 0)
            self.assertTrue((repo / "nako_pkg" / "nako_mod.py").is_file())
            self.assertFalse((repo / f"{brand}_pkg").exists())
            self.assertEqual(os.readlink(repo / "entry_link"), "nako_pkg/nako_mod.py")
            self.assertEqual((repo / "LICENSE").read_text(encoding="utf-8"), f"Copyright {org}\n")
            self.assertEqual(brand_lint.check_repo(repo, include_untracked=False), [])

            # Idempotent second run changes nothing
            rc2 = rebrand.main(["--repo", str(repo), "--stage", "--no-verify"])
            self.assertEqual(rc2, 0)
            self.assertEqual(brand_lint.check_repo(repo, include_untracked=False), [])


class PatchesAndSyncTests(unittest.TestCase):
    def test_list_patches_handles_multiple_commits_spaces_and_empty_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo)
            (repo / "base.txt").write_text("base\n", encoding="utf-8")
            _git(repo, "add", "base.txt")
            _git(repo, "commit", "-m", "base")
            base_sha = _git(repo, "rev-parse", "HEAD").strip()

            (repo / "a b.txt").write_text("hello\n", encoding="utf-8")
            (repo / "c.txt").write_text("world\n", encoding="utf-8")
            _git(repo, "add", "a b.txt", "c.txt")
            _git(repo, "commit", "-m", "feat: first patch")

            _git(repo, "commit", "--allow-empty", "-m", "chore: empty patch")

            (repo / "d.txt").write_text("three\n", encoding="utf-8")
            _git(repo, "add", "d.txt")
            _git(repo, "commit", "-m", "fix: third patch")

            items = patch_lib.list_patches(repo, base_ref=base_sha, branch="HEAD")
            self.assertEqual(len(items), 3)
            self.assertEqual(items[0].subject, "feat: first patch")
            self.assertEqual(items[0].files, ("a b.txt", "c.txt"))
            self.assertEqual(items[1].subject, "chore: empty patch")
            self.assertEqual(items[1].files, ())
            self.assertEqual(items[2].subject, "fix: third patch")
            self.assertEqual(items[2].files, ("d.txt",))

    def test_seq_edit_supports_full_sha_short_sha_and_abbreviated_pick(self) -> None:
        full_sha = "abcdef1234567890abcdef1234567890abcdef12"
        other_sha = "1234567890abcdef1234567890abcdef12345678"
        with tempfile.TemporaryDirectory() as tmp:
            todo = Path(tmp) / "git-rebase-todo"
            todo.write_text(f"p {other_sha[:7]} first\np {full_sha[:7]} second\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"NAKO_EDIT_SHA": full_sha}):
                rc = patch_lib._run_internal_seq_edit(todo)
            self.assertEqual(rc, 0)
            self.assertEqual(
                todo.read_text(encoding="utf-8"),
                f"p {other_sha[:7]} first\nedit {full_sha[:7]} second\n",
            )

            todo.write_text(f"pick {full_sha[:10]} second\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"NAKO_EDIT_SHA": full_sha[:7]}):
                rc = patch_lib._run_internal_seq_edit(todo)
            self.assertEqual(rc, 0)
            self.assertEqual(todo.read_text(encoding="utf-8"), f"edit {full_sha[:10]} second\n")

    def test_exported_matches_short_circuits_on_pin_mismatch_and_matches_exact_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            _init_repo(repo)
            (repo / "file.txt").write_text("v1\n", encoding="utf-8")
            _git(repo, "add", "file.txt")
            _git(repo, "commit", "-m", "upstream")
            base_sha = _git(repo, "rev-parse", "HEAD").strip()
            _git(repo, "checkout", "-b", "patches")
            (repo / "file.txt").write_text("v2\n", encoding="utf-8")
            _git(repo, "add", "file.txt")
            _git(repo, "commit", "-m", "feat: patch one")

            pin_file = root / "upstream"
            patch_dir = root / "patches"
            with mock.patch.object(patch_lib, "PIN_FILE", pin_file), mock.patch.object(patch_lib, "PATCH_DIR", patch_dir):
                self.assertFalse(patch_lib.exported_matches(repo, base_ref=base_sha, branch="patches"))
                patch_lib.export_patches(repo, base_ref=base_sha, branch="patches")
                self.assertTrue(patch_lib.exported_matches(repo, base_ref=base_sha, branch="patches"))
                pin_file.write_text("0" * 40 + "\n", encoding="utf-8")
                self.assertFalse(patch_lib.exported_matches(repo, base_ref=base_sha, branch="patches"))

    def test_cmd_apply_refuses_when_patches_checked_out_in_another_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "src"
            source.mkdir()
            _init_repo(source)
            (source / "f.txt").write_text("base\n", encoding="utf-8")
            _git(source, "add", "f.txt")
            _git(source, "commit", "-m", "base")
            pin = _git(source, "rev-parse", "HEAD").strip()
            _git(source, "checkout", "-b", "patches")

            pin_file = source / "upstream"
            patch_dir = source / "patches"
            pin_file.write_text(pin + "\n", encoding="utf-8")
            patch_dir.mkdir()
            work_target = root / "work"

            with (
                mock.patch.object(patch_lib, "SOURCE_ROOT", source),
                mock.patch.object(patch_lib, "PIN_FILE", pin_file),
                mock.patch.object(patch_lib, "PATCH_DIR", patch_dir),
            ):
                rc = patch_lib.cmd_apply(work_target, mock.Mock(base="main", force=True))
            self.assertEqual(rc, 2)
            self.assertFalse(work_target.exists())

    def test_sync_push_refuses_detached_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            work = root / "work"
            source.mkdir()
            work.mkdir()
            _init_repo(source)
            (source / "README.md").write_text("hi\n", encoding="utf-8")
            _git(source, "add", "README.md")
            _git(source, "commit", "-m", "init")
            sha = _git(source, "rev-parse", "HEAD").strip()
            _git(source, "checkout", "--detach", sha)

            _init_repo(work)
            (work / "clean.txt").write_text("hello\n", encoding="utf-8")
            _git(work, "add", "clean.txt")
            _git(work, "commit", "-m", "base")
            _git(work, "branch", "upstream_base")
            _git(work, "checkout", "-b", "patches")

            with mock.patch.object(patch_lib, "SOURCE_ROOT", source):
                rc = sync_lib.main([
                    "--repo",
                    str(work),
                    "--upstream-remote",
                    "refs/heads",
                    "--upstream-branch",
                    "upstream_base",
                    "--skip-fetch",
                    "--skip-rebase",
                    "--push",
                ])
            self.assertEqual(rc, 1)


@unittest.skipIf(Image is None or brand_art is None, "Pillow not installed")
class BrandArtTests(unittest.TestCase):
    def test_disk_vectorized_matches_reference_pixel_loop(self) -> None:
        size = 256
        base = Image.new("RGBA", (size, size), (120, 130, 140, 255))
        px = base.load()
        for y in range(size):
            for x in range(size):
                px[x, y] = ((40 + x // 2) % 256, (60 + y // 2) % 256, (80 + (x + y) // 4) % 256, 255)
        art = Image.new("RGBA", (128, 128), (236, 72, 153, 255))

        expected = base.copy()
        unit = expected.width / 1024
        left, right, top, bottom = (int(v * unit) for v in (204, 830, 215, 846))
        epx = expected.load()
        across = [epx[x, top - 1] for x in range(left, right)]
        for y in range(top, bottom):
            down = epx[right, y][0] - epx[right, top - 1][0]
            for x in range(left, right):
                r, g, b, a = across[x - left]
                epx[x, y] = (r + down, g + down, b + down, a)
        side = int(520 * unit)
        expected.alpha_composite(brand_art._rounded(art, side), ((expected.width - side) // 2, int(528 * unit) - side // 2))

        actual = brand_art._disk(base.copy(), art, cache=brand_art._ArtCache(art))
        self.assertEqual(actual.tobytes(), expected.tobytes())


if __name__ == "__main__":
    unittest.main()
