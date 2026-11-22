# Nako-Agent Patch Workflow

The patches are files in `patches/`, applying to the upstream commit named in `upstream`. Both live on the `nako-agent` branch, which is never rewritten, so every change to a patch is an ordinary commit.

To work on them, `apply` builds the files into a commit stack: the `patches` branch, checked out in `work/` (ignored by git here). All commands below run from this checkout and act on `work/`. `patches` is scratch: it is rebuilt from the files and never pushed.

`main` is generated on top of `patches` as one rebrand commit (`scripts/nako/rebrand.py`). Never hand-edit `main`.

## Changing patches

```bash
python3 scripts/nako/patches.py apply                  # patches/ -> commit stack in work/
python3 scripts/nako/patches.py list --files           # inspect stack
python3 scripts/nako/patches.py status                 # sync state or active pause dossier

# New patch: commit it in work/, then write it out
python3 scripts/nako/patches.py export                 # commit stack -> patches/

# Existing patch (each of these exports when it finishes)
python3 scripts/nako/patches.py fixup 2 --rebuild      # fold staged edits into patch #2
python3 scripts/nako/patches.py edit 2                 # pause at patch #2, edit/test, then `continue --rebuild`
python3 scripts/nako/patches.py sunset 2 --rebuild     # drop an upstreamed/obsolete patch
```

Review the diff under `patches/` and commit it here. `apply` refuses to throw away commits that were never exported (`--force` overrides).

## Turn-based update flow (non-interactive)

```bash
# 1. Dry-run preflight (never mutates branches): see clean / upstreamed / conflicting patches
python3 scripts/nako/patches.py plan
python3 scripts/nako/patches.py plan --json

# 2. Apply the update (rewrites patches/ and the pin, rebuilds main if clean; pauses with a dossier on conflict)
python3 scripts/nako/patches.py update --skip-fetch

# 3. After fixing conflicted files in work/, validate + auto-stage + advance (or drop/abort)
python3 scripts/nako/patches.py continue --rebuild
python3 scripts/nako/patches.py skip --rebuild
python3 scripts/nako/patches.py abort
```

## Publishing

`--push` pushes this branch and `main` together, `main` with `--force-with-lease`. It stops while this checkout has uncommitted changes, so `main` is never published ahead of the patches it was built from. After committing: `python3 scripts/nako/sync.py --skip-fetch --skip-rebase --push`.

## Brand lint guard

`scripts/nako/check_no_upstream_brand.py` fails if the upstream brand appears in any tracked path, symlink target, or file content outside `README.md`. It is copied into `main` at rebuild time with `nako-brand-lint.yml`, and runs there in `scripts/check` (`no-upstream-brand`).
