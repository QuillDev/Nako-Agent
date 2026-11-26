# Nako-Agent Patch Workflow

The patches are files in `patches/`, applying to the upstream commit named in `upstream`. Both live on the `nako-agent` branch, which is never rewritten, so every change to a patch is an ordinary commit.

To work on them, `apply` builds the files into a commit stack: the `patches` branch, checked out in `work/` (ignored by git here). All commands below run from this checkout and act on `work/`. `patches` is scratch: it is rebuilt from the files and never pushed.

`main` is generated on top of `patches` as one rebrand commit (`scripts/nako/rebrand.py`). Never hand-edit `main`.

The rebrand also swaps upstream's logo for ours (`brand_art.py`, needs Pillow): every committed icon is redrawn from `art/icon.png` and the README banner from `art/banner.png`. To change the logo, replace those two files and rebuild `main`.

`main`'s README is this branch's root `README.md`, copied in whole. Upstream's README is not used.

## First time

A fresh clone needs the upstream history once:

```bash
git remote add upstream <upstream repo url>
git fetch upstream main
```

Then build the commit stack in `work/`:

```bash
python3 scripts/nako/patches.py apply
```

## Changing patches

`work/` is an ordinary checkout of the code with the patches applied as commits. Edit, run and test there; its `AGENTS.md` says how. Everything in it keeps upstream's names, and patches must too: the rename to Nako happens only when `main` is built.

```bash
python3 scripts/nako/patches.py apply       # patches/ -> commit stack in work/ (safe to rerun)
python3 scripts/nako/patches.py list        # the stack, numbered
python3 scripts/nako/patches.py status      # the pin, the stack, and whether patches/ and main match it

# New patch: commit it in work/ (subject like the others, e.g. `feat(nako): ...`), then write it out
python3 scripts/nako/patches.py export      # commit stack -> patches/

# Existing patch, picked by number, sha or a piece of its subject. Each of these exports when it finishes.
python3 scripts/nako/patches.py fixup 2     # fold changes staged in work/ into patch #2
python3 scripts/nako/patches.py edit 2      # pause at patch #2; edit, test, then `continue`
python3 scripts/nako/patches.py sunset 2    # drop a patch
```

Then review the diff under `patches/` and commit it here, like any other change. `apply` stops if `work/` has uncommitted edits, and refuses to throw away commits that were never exported (`--force` overrides).

None of the above touches `main`; it only needs rebuilding before a publish.

## Updating to newer upstream

```bash
python3 scripts/nako/patches.py plan                  # dry run: which patches are clean / upstreamed / conflicting
python3 scripts/nako/patches.py update --skip-fetch   # rebase the stack, rewrite patches/ and the pin, rebuild main
```

On a conflict `update` pauses and lists the files. Fix them in `work/`, then `continue --rebuild` (or `skip --rebuild` to drop the patch, `abort` to back out).

## Building and publishing main

`--rebuild` on `fixup`, `sunset`, `continue` or `skip` also regenerates the local `main` branch from the stack. On its own that is `python3 scripts/nako/sync.py --skip-fetch --skip-rebase`.

`--push` rebuilds, then pushes this branch and `main` together, `main` with `--force-with-lease`. It stops while this checkout has uncommitted changes, so `main` is never published ahead of the patches it was built from.

## Brand lint guard

`scripts/nako/check_no_upstream_brand.py` fails if the upstream brand appears in any tracked path, symlink target, or file content outside `README.md`. It is copied into `main` at rebuild time with `nako-brand-lint.yml`, and runs there in `scripts/check` (`no-upstream-brand`).
