# Nako-Agent source

This branch is the source of Nako-Agent: a set of patches on top of an upstream commit, plus the tooling that turns them into the installable build.

- `patches/`: one file per feature or fix, in upstream naming, applied in order.
- `upstream`: the upstream commit the patches apply to.
- `scripts/nako/`: the tooling. See `scripts/nako/README.md`.

The `main` branch is generated from these: the patches applied to the pinned commit, then one rebrand commit on top. Installs and updates pull `main`. Never edit it by hand.

```bash
git remote add upstream <upstream repo url>   # once per clone
git fetch upstream main
python3 scripts/nako/patches.py apply         # builds the commit stack in work/
```
