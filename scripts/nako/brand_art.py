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
from concurrent.futures import ThreadPoolExecutor
import io
import os
import threading
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


class _ArtCache:
    """Thread-safe cache for repeated LANCZOS resizes, rounded tiles, and SVG embeds."""

    def __init__(self, art: Image.Image) -> None:
        self._art = art
        self._lock = threading.Lock()
        self._resized: dict[tuple[int, int], Image.Image] = {}
        self._rounded: dict[int, Image.Image] = {}
        self._svgs: dict[int, str] = {}

    def resized(self, size: tuple[int, int]) -> Image.Image:
        with self._lock:
            cached = self._resized.get(size)
        if cached is not None:
            return cached
        img = self._art.resize(size, LANCZOS)
        with self._lock:
            return self._resized.setdefault(size, img)

    def rounded(self, size: int) -> Image.Image:
        with self._lock:
            cached = self._rounded.get(size)
        if cached is not None:
            return cached
        tile = self.resized((size, size)).copy()
        mask = Image.new("L", (size * 4, size * 4), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, size * 4 - 1, size * 4 - 1), radius=int(size * 4 * CORNER), fill=255
        )
        tile.putalpha(mask.resize((size, size), LANCZOS))
        with self._lock:
            return self._rounded.setdefault(size, tile)

    def svg(self, pixels: int) -> str:
        with self._lock:
            cached = self._svgs.get(pixels)
        if cached is not None:
            return cached
        data = io.BytesIO()
        self.resized((pixels, pixels)).convert("RGB").save(data, "PNG", optimize=True)
        href = "data:image/png;base64," + base64.b64encode(data.getvalue()).decode("ascii")
        text = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">'
            f'<defs><clipPath id="tile"><rect width="1024" height="1024" rx="{CORNER * 1024:g}"/></clipPath></defs>'
            f'<image width="1024" height="1024" href="{href}" clip-path="url(#tile)"/></svg>\n'
        )
        with self._lock:
            return self._svgs.setdefault(pixels, text)


def _rounded(art: Image.Image, size: int, cache: _ArtCache | None = None) -> Image.Image:
    if cache is not None:
        return cache.rounded(size)
    tile = art.resize((size, size), LANCZOS)
    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size * 4 - 1, size * 4 - 1), radius=int(size * 4 * CORNER), fill=255)
    tile.putalpha(mask.resize((size, size), LANCZOS))
    return tile


def _tile(old: Image.Image, art: Image.Image, cache: _ArtCache | None = None) -> Image.Image:
    """`art` in the shape `old` had: same canvas, same plate position, same outline."""
    old = old.convert("RGBA")
    alpha = old.getchannel("A")
    box = alpha.getbbox() or (0, 0, *old.size)
    new = Image.new("RGBA", old.size, (0, 0, 0, 0))
    target_size = (box[2] - box[0], box[3] - box[1])
    scaled = cache.resized(target_size) if cache is not None else art.resize(target_size, LANCZOS)
    new.paste(scaled, box[:2])
    new.putalpha(alpha)
    return new


def _cutout(old: Image.Image, art: Image.Image, cache: _ArtCache | None = None) -> Image.Image:
    new = Image.new("RGBA", old.size, (0, 0, 0, 0))
    side = min(old.size)
    new.alpha_composite(_rounded(art, side, cache=cache), ((old.width - side) // 2, (old.height - side) // 2))
    return new


def _disk(disk: Image.Image, art: Image.Image, cache: _ArtCache | None = None) -> Image.Image:
    """The drawn disk with its mascot wiped off the face and the tile put there instead."""
    unit = disk.width / 1024
    left, right, top, bottom = (int(v * unit) for v in (204, 830, 215, 846))  # clean metal around the old art
    width, height = right - left, bottom - top
    if width > 0 and height > 0:
        across = disk.crop((left, top - 1, right, top))
        across_rgb = across.convert("RGB").tobytes()
        across_a = across.getchannel("A").tobytes()
        col_r = disk.crop((right, top - 1, right + 1, bottom)).getchannel("R").tobytes()
        ref_r = col_r[0]
        row_cache: dict[int, bytes] = {}
        rows: list[bytes] = []
        for r_val in col_r[1:]:
            down = r_val - ref_r
            row = row_cache.get(down)
            if row is None:
                table = bytes(max(0, min(255, i + down)) for i in range(256))
                row = across_rgb.translate(table)
                row_cache[down] = row
            rows.append(row)
        patch = Image.frombytes("RGB", (width, height), b"".join(rows)).convert("RGBA")
        patch.putalpha(Image.frombytes("L", (width, height), across_a * height))
        disk.paste(patch, (left, top))
    side = int(520 * unit)
    disk.alpha_composite(_rounded(art, side, cache=cache), ((disk.width - side) // 2, int(528 * unit) - side // 2))
    return disk


def _svg(art: Image.Image, pixels: int, cache: _ArtCache | None = None) -> str:
    if cache is not None:
        return cache.svg(pixels)
    data = io.BytesIO()
    art.resize((pixels, pixels), LANCZOS).convert("RGB").save(data, "PNG", optimize=True)
    href = "data:image/png;base64," + base64.b64encode(data.getvalue()).decode("ascii")
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">'
            f'<defs><clipPath id="tile"><rect width="1024" height="1024" rx="{CORNER * 1024:g}"/></clipPath></defs>'
            f'<image width="1024" height="1024" href="{href}" clip-path="url(#tile)"/></svg>\n')


def _container(path: Path, art: Image.Image, cache: _ArtCache | None = None) -> None:
    with Image.open(path) as old:
        if path.suffix == ".ico":
            sizes = sorted(old.ico.sizes())
            rendered = _tile(old.ico.getimage(sizes[-1]), art, cache=cache)
            rendered.save(path, format="ICO", sizes=sizes)
        else:
            rendered = _tile(old, art, cache=cache)
            rendered.save(path, format="ICNS")


def apply(repo: Path) -> int:
    with Image.open(ART / "icon.png") as raw_art:
        art = raw_art.convert("RGBA")
    cache = _ArtCache(art)
    banner_bytes: bytes | None = None
    done = 0

    def each(patterns):
        nonlocal done
        paths: list[Path] = []
        for pattern in patterns:
            for path in sorted(repo.glob(pattern)):
                done += 1
                paths.append(path)
        return paths

    def _do_tile(path: Path) -> None:
        with Image.open(path) as old:
            rendered = _tile(old, art, cache=cache)
        rendered.save(path)

    def _do_container(path: Path) -> None:
        _container(path, art, cache=cache)

    def _do_layer(path: Path) -> None:
        with Image.open(path) as old:
            size = old.size
        cache.resized(size).save(path)

    def _do_mono(path: Path) -> None:
        with Image.open(path) as old:
            size = old.size
        cache.resized(size).convert("L").convert("RGBA").save(path)

    def _do_cutout(path: Path) -> None:
        with Image.open(path) as old:
            rendered = _cutout(old, art, cache=cache)
        rendered.save(path)

    def _do_svg(path: Path) -> None:
        path.write_text(cache.svg(256 if path.name == "favicon.svg" else 512), encoding="utf-8")

    tasks: list[tuple] = []
    for path in each(TILES):
        tasks.append((_do_tile, path))
    for path in each(CONTAINERS):
        tasks.append((_do_container, path))
    for path in each(LAYERS):
        tasks.append((_do_layer, path))
    for path in each(MONO_LAYERS):
        tasks.append((_do_mono, path))
    for path in each(CUTOUTS):
        tasks.append((_do_cutout, path))
    for path in each(ROUNDED_SVGS):
        tasks.append((_do_svg, path))

    banner_paths = each(BANNERS)
    if banner_paths:
        banner_bytes = (ART / "banner.png").read_bytes()
        for path in banner_paths:
            path.write_bytes(banner_bytes)

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
        done += 1
        icns = repo / DMG_VOLUME_ICNS
        write_icns = icns.is_file()
        if write_icns:
            done += 1

        def _do_dmg(_: Path) -> None:
            with Image.open(volume) as raw_vol:
                disk = _disk(raw_vol.convert("RGBA"), art, cache=cache)
            disk.save(volume)
            if write_icns:
                disk.save(icns, format="ICNS")

        tasks.append((_do_dmg, volume))

    workers = min(8, os.cpu_count() or 1)
    if workers <= 1 or len(tasks) < 4:
        for fn, arg in tasks:
            fn(arg)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(fn, arg) for fn, arg in tasks]
            for fut in futures:
                fut.result()
    return done


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    print(f"brand art: replaced {apply(Path(parser.parse_args().repo))} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
