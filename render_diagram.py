#!/usr/bin/env python3
"""Regenerate the derived images: the diagram PNGs and the colour swatches.

diagram.svg carries its palette as CSS custom properties so a browser can pick
light or dark from prefers-color-scheme. librsvg, which does the rasterising
here, does not understand var(), and silently resolves every colour to black.
So the variables are flattened to literals for one theme at a time before the
SVG is handed over.

    python3 render_diagram.py

The swatches are generated rather than fetched from a badge service, for two
reasons: the README then has no external dependency for them, and each one gets
a border, without which white, silver and lavender are invisible on a light page
and black is invisible on a dark one.
"""

from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
IMAGES = os.path.join(HERE, "images")
SOURCE = os.path.join(IMAGES, "diagram.svg")
OUTPUTS = {
    "light": os.path.join(IMAGES, "diagram.png"),
    "dark": os.path.join(IMAGES, "diagram-dark.png"),
}
SCALE = 2

SWATCHES = os.path.join(IMAGES, "swatches")
SWATCH_PX = 32          # rendered size; the README displays them at 14px


def read_palette(css: str, block: str) -> dict[str, str]:
    """Pull --name: value pairs out of one :root declaration block."""
    match = re.search(re.escape(block) + r"\s*\{(.*?)\}", css, re.S)
    if not match:
        raise SystemExit(f"could not find the {block!r} block in {SOURCE}")
    return dict(re.findall(r"--([\w-]+)\s*:\s*([^;}]+)", match.group(1)))


def flatten(svg: str, palette: dict[str, str]) -> str:
    # Drop the media query: its declarations would otherwise survive as dead
    # text, and for the dark render they would contradict the substitution.
    svg = re.sub(r"@media[^{]*\{.*?\}\s*\}", "", svg, flags=re.S)
    for name, value in palette.items():
        svg = svg.replace(f"var(--{name})", value.strip())
    leftover = re.search(r"var\(--([\w-]+)\)", svg)
    if leftover:
        raise SystemExit(f"no value for --{leftover.group(1)}")
    return svg


def write_swatches() -> int:
    """One bordered PNG per colour name, taken from loq_kbd.COLOR_NAMES."""
    import importlib.util
    import cairo

    spec = importlib.util.spec_from_file_location(
        "loq_kbd", os.path.join(HERE, "loq_kbd.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    os.makedirs(SWATCHES, exist_ok=True)
    for hex_value in mod.COLOR_NAMES:
        r, g, b = (int(hex_value[i:i + 2], 16) / 255 for i in (0, 2, 4))
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, SWATCH_PX, SWATCH_PX)
        ctx = cairo.Context(surface)
        ctx.set_source_rgb(r, g, b)
        ctx.paint()
        # A half-transparent grey border reads against a light or a dark page.
        ctx.set_source_rgba(0.5, 0.5, 0.5, 0.55)
        ctx.set_line_width(2)
        ctx.rectangle(1, 1, SWATCH_PX - 2, SWATCH_PX - 2)
        ctx.stroke()
        surface.write_to_png(os.path.join(SWATCHES, f"{hex_value}.png"))

    print(f"{os.path.relpath(SWATCHES, HERE)}/  "
          f"{len(mod.COLOR_NAMES)} swatches at {SWATCH_PX}px")
    return len(mod.COLOR_NAMES)


def main() -> int:
    try:
        import gi
        gi.require_version("Rsvg", "2.0")
        from gi.repository import Rsvg
        import cairo
    except (ImportError, ValueError) as exc:
        print(f"need pycairo and gobject-introspection with Rsvg: {exc}",
              file=sys.stderr)
        print("on Debian/Ubuntu:  sudo apt install python3-gi python3-cairo "
              "gir1.2-rsvg-2.0", file=sys.stderr)
        return 1

    with open(SOURCE) as fh:
        svg = fh.read()

    light = read_palette(svg, ":root")
    dark = dict(light)
    media = re.search(r"@media[^{]*\{\s*:root\s*\{(.*?)\}", svg, re.S)
    if media:
        dark.update(re.findall(r"--([\w-]+)\s*:\s*([^;}]+)", media.group(1)))

    for theme, out in OUTPUTS.items():
        palette = light if theme == "light" else dark
        handle = Rsvg.Handle.new_from_data(flatten(svg, palette).encode())
        ok, width, height = handle.get_intrinsic_size_in_pixels()
        if not ok:
            width, height = 920.0, 920.0
        surface = cairo.ImageSurface(
            cairo.FORMAT_ARGB32, int(width * SCALE), int(height * SCALE)
        )
        context = cairo.Context(surface)
        rect = Rsvg.Rectangle()
        rect.x, rect.y = 0.0, 0.0
        rect.width, rect.height = width * SCALE, height * SCALE
        handle.render_document(context, rect)
        surface.write_to_png(out)
        print(f"{os.path.relpath(out, HERE)}  "
              f"{int(width*SCALE)}x{int(height*SCALE)}  ({theme})")

    write_swatches()
    return 0


if __name__ == "__main__":
    sys.exit(main())
