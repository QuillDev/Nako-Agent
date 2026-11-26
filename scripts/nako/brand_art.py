#!/usr/bin/env python3
"""Replace upstream's logo art with Nako's in a rebranded checkout.

Upstream commits every rendered icon, and its generator draws them from its own mascot, so the
text rebrand leaves the pictures alone. This overwrites each committed picture in place with
`art/icon.png` (and the README banners with `art/banner.png`), keeping every file's size, format
and outline: a tile keeps its rounded shape and margins because the old picture's transparency is
reused as the mask. Upstream's icon generator is not taught the new art: it still draws upstream's
mascot from the vector sources left in assets/, which only the Docker and nix builds run.
The terminal banner spells upstream's name in block letters, which the text rebrand cannot read
either, so those letters are swapped here too.

Needs Pillow. Run by rebrand.py; `--repo <checkout>` runs it alone.
"""

from __future__ import annotations

import argparse
import base64
import io
from pathlib import Path

from PIL import Image, ImageDraw

LANCZOS = Image.Resampling.LANCZOS

ART = Path(__file__).resolve().parent / "art"

# Rendered tiles: a plate (rounded square, maybe with a margin) carrying the mascot.
TILES = (
    "apps/desktop/assets/icon*.png",
    "apps/desktop/assets/appx/*.png",
    "apps/desktop/public/apple-touch-icon.png",
    "apps/desktop/public/nous-girl*.png",
    "apps/bootstrap-installer/src-tauri/icons/*.png",
    "apps/bootstrap-installer/public/nous-girl.png",
    "website/static/img/favicon-*.png",
    "website/static/img/apple-touch-icon.png",
    "website/static/img/nous-logo*.png",
)
CONTAINERS = (
    "apps/desktop/assets/icon*.ico",
    "apps/desktop/assets/icon*.icns",
    "apps/bootstrap-installer/src-tauri/icons/icon.ico",
    "apps/bootstrap-installer/src-tauri/icons/icon.icns",
    "website/static/img/favicon.ico",
    "web/public/favicon.ico",
)
# macOS 26 layers: the system cuts the outline itself, so a layer is the whole square.
LAYERS = ("apps/*/assets/icon.icon/Assets/art-*.png", "apps/*/src-tauri/icons/icon.icon/Assets/art-*.png")
MONO_LAYERS = ("apps/*/assets/icon.icon/Assets/mono.png", "apps/*/src-tauri/icons/icon.icon/Assets/mono.png")
# The mascot alone on a clear background (docs site header).
CUTOUTS = ("website/static/img/logo*.png",)
ROUNDED_SVGS = ("assets/icon-master*.svg", "website/static/img/favicon.svg")
BANNERS = ("assets/banner.png", "website/static/img/*-agent-banner.png")
DMG_VOLUME = "assets/dmg-volume.png"
DMG_VOLUME_ICNS = "apps/desktop/packaging/dmg-volume.icns"
CORNER = 245 / 1024  # upstream's tile corner radius

# The terminal banner, row by row: upstream's name in block letters -> NAKO. The "-AGENT" half stays.
BLOCK_LETTER_FILES = ("*_cli/banner.py", "ui-tui/src/banner.ts")
BLOCK_LETTERS = (
    ("██╗  ██╗███████╗██████╗ ███╗   ███╗███████╗███████╗", "███╗   ██╗ █████╗ ██╗  ██╗ ██████╗ "),
    ("██║  ██║██╔════╝██╔══██╗████╗ ████║██╔════╝██╔════╝", "████╗  ██║██╔══██╗██║ ██╔╝██╔═══██╗"),
    ("███████║█████╗  ██████╔╝██╔████╔██║█████╗  ███████╗", "██╔██╗ ██║███████║█████╔╝ ██║   ██║"),
    ("██╔══██║██╔══╝  ██╔══██╗██║╚██╔╝██║██╔══╝  ╚════██║", "██║╚██╗██║██╔══██║██╔═██╗ ██║   ██║"),
    ("██║  ██║███████╗██║  ██║██║ ╚═╝ ██║███████╗███████║", "██║ ╚████║██║  ██║██║  ██╗╚██████╔╝"),
    ("╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝╚═╝     ╚═╝╚══════╝╚══════╝", "╚═╝  ╚═══╝╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝ "),
)
UPSTREAM_SYMBOL = " \u2624"  # the staff after the README title


def _rounded(art: Image.Image, size: int) -> Image.Image:
    tile = art.resize((size, size), LANCZOS)
    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size * 4 - 1, size * 4 - 1), radius=int(size * 4 * CORNER), fill=255)
    tile.putalpha(mask.resize((size, size), LANCZOS))
    return tile


def _tile(old: Image.Image, art: Image.Image) -> Image.Image:
    """`art` in the shape `old` had: same canvas, same plate position, same outline."""
    old = old.convert("RGBA")
    alpha = old.getchannel("A")
    box = alpha.getbbox() or (0, 0, *old.size)
    new = Image.new("RGBA", old.size, (0, 0, 0, 0))
    new.paste(art.resize((box[2] - box[0], box[3] - box[1]), LANCZOS), box[:2])
    new.putalpha(alpha)
    return new


def _cutout(old: Image.Image, art: Image.Image) -> Image.Image:
    new = Image.new("RGBA", old.size, (0, 0, 0, 0))
    side = min(old.size)
    new.alpha_composite(_rounded(art, side), ((old.width - side) // 2, (old.height - side) // 2))
    return new


def _disk(disk: Image.Image, art: Image.Image) -> Image.Image:
    """The drawn disk with its mascot wiped off the face and the tile put there instead."""
    unit = disk.width / 1024
    left, right, top, bottom = (int(v * unit) for v in (204, 830, 215, 846))  # clean metal around the old art
    pixels = disk.load()
    across = [pixels[x, top - 1] for x in range(left, right)]  # the row above the art: how the face shades sideways
    for y in range(top, bottom):
        down = pixels[right, y][0] - pixels[right, top - 1][0]  # a clean column: how it shades downward
        for x in range(left, right):
            r, g, b, a = across[x - left]
            pixels[x, y] = (r + down, g + down, b + down, a)
    side = int(520 * unit)
    disk.alpha_composite(_rounded(art, side), ((disk.width - side) // 2, int(528 * unit) - side // 2))
    return disk


def _svg(art: Image.Image, pixels: int) -> str:
    data = io.BytesIO()
    art.resize((pixels, pixels), LANCZOS).convert("RGB").save(data, "PNG", optimize=True)
    href = "data:image/png;base64," + base64.b64encode(data.getvalue()).decode("ascii")
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">'
            f'<defs><clipPath id="tile"><rect width="1024" height="1024" rx="{CORNER * 1024:g}"/></clipPath></defs>'
            f'<image width="1024" height="1024" href="{href}" clip-path="url(#tile)"/></svg>\n')


def _container(path: Path, art: Image.Image) -> None:
    old = Image.open(path)
    if path.suffix == ".ico":
        sizes = sorted(old.ico.sizes())
        _tile(old.ico.getimage(sizes[-1]), art).save(path, format="ICO", sizes=sizes)
    else:
        _tile(old, art).save(path, format="ICNS")


def apply(repo: Path) -> int:
    art = Image.open(ART / "icon.png").convert("RGBA")
    done = 0

    def each(patterns):
        nonlocal done
        for pattern in patterns:
            for path in sorted(repo.glob(pattern)):
                done += 1
                yield path

    for path in each(TILES):
        _tile(Image.open(path), art).save(path)
    for path in each(CONTAINERS):
        _container(path, art)
    for path in each(LAYERS):
        art.resize(Image.open(path).size, LANCZOS).save(path)
    for path in each(MONO_LAYERS):
        art.resize(Image.open(path).size, LANCZOS).convert("L").convert("RGBA").save(path)
    for path in each(CUTOUTS):
        _cutout(Image.open(path), art).save(path)
    for path in each(ROUNDED_SVGS):
        path.write_text(_svg(art, 256 if path.name == "favicon.svg" else 512), encoding="utf-8")
    for path in each(BANNERS):
        path.write_bytes((ART / "banner.png").read_bytes())

    for path in each(BLOCK_LETTER_FILES):
        text = path.read_text(encoding="utf-8")
        for theirs, ours in BLOCK_LETTERS:
            text = text.replace(theirs, ours)
        path.write_text(text, encoding="utf-8")
    readme = repo / "README.md"
    if readme.is_file():
        text = readme.read_text(encoding="utf-8")
        readme.write_text(text.replace(UPSTREAM_SYMBOL + "\n", "\n", 1), encoding="utf-8")

    volume = repo / DMG_VOLUME
    if volume.is_file():
        disk = _disk(Image.open(volume).convert("RGBA"), art)
        disk.save(volume)
        done += 1
        icns = repo / DMG_VOLUME_ICNS
        if icns.is_file():
            disk.save(icns, format="ICNS")
            done += 1
    return done


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    print(f"brand art: replaced {apply(Path(parser.parse_args().repo))} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
