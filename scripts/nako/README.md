# Nako-Agent Branch & Patch Stack Workflow

Two branches keep upstream rebases clean while `main` stays fully rebranded:

- `patches`: sits directly on `upstream/main` in upstream naming. One commit per logical feature or fix.
- `main`: generated on top of `patches` as one rebrand commit (`scripts/nako/rebrand.py`). Never hand-edit `main`.

## Turn-based update flow (non-interactive)

```bash
# 1. Dry-run preflight (never mutates branches): see clean / upstreamed / conflicting patches
python3 scripts/nako/patches.py plan
python3 scripts/nako/patches.py plan --json

# 2. Apply the update (rebuilds main automatically if clean; pauses with a dossier on conflict)
python3 scripts/nako/patches.py update --skip-fetch

# 3. After fixing conflicted files, validate + auto-stage + advance (or drop/abort)
python3 scripts/nako/patches.py continue --rebuild --push
python3 scripts/nako/patches.py skip --rebuild --push
python3 scripts/nako/patches.py abort
```

## Managing existing patches

```bash
python3 scripts/nako/patches.py list --files           # inspect stack
python3 scripts/nako/patches.py status                 # sync state or active pause dossier
python3 scripts/nako/patches.py fixup 2 --rebuild      # fold staged edits into patch #2
python3 scripts/nako/patches.py edit 2                 # pause at patch #2, edit/test, then `continue --rebuild`
python3 scripts/nako/patches.py sunset 2 --rebuild     # drop an upstreamed/obsolete patch
```

## Brand lint guard

`scripts/nako/check_no_upstream_brand.py` fails if the upstream brand appears in any tracked path, symlink target, or file content outside `README.md`. On `main` it runs in `scripts/check` (`no-upstream-brand`) and `.github/workflows/nako-brand-lint.yml`.
