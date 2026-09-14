#!/usr/bin/env python3
"""Generate the App Store icon + splash source images for @capacitor/assets.

Both are derived from the SAME artwork the web app's favicon and home-screen
icon use -- ../../pipeline/assets/brand_icon.png (see _brand_icon() in
pipeline/interactive_export.py). This script used to draw its own charcoal
snowflake from the app's older monochrome era, which meant the iOS icon did
not match the icon users already knew from the web app.

Outputs (into apple-app/resources/):
  icon.png        1024x1024, fully opaque, NO transparency / NO rounded corners
                  (Apple rounds the corners itself; alpha or pre-rounding is an
                  App Store rejection). The mark is cropped to its own bounding
                  box and scaled to fill the canvas the way an app icon should,
                  rather than inheriting the source artwork's wide margins.
  splash.png      2732x2732, mark centred small on the web loader's white.
  splash-dark.png same, on near-black (capacitor-assets picks it up if present).

Run: python3 scripts/make-resources.py   (from apple-app/)
Then: npx capacitor-assets generate --ios  (produces every required size).
"""
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "resources"
SRC = HERE.parent.parent / "pipeline" / "assets" / "brand_icon.png"
OUT.mkdir(parents=True, exist_ok=True)

PAGE = (255, 255, 255)      # the web app's paper white
NIGHT = (10, 10, 12)        # dark-splash background

# How much of the icon's width the mark should span. Apple's own icons leave a
# little air; filling edge to edge looks cramped once the corner mask is on.
ICON_FILL = 0.78
SPLASH_FILL = 0.20


def _mark() -> Image.Image:
    """The brand mark, cropped to its bounding box, with white keyed out to alpha.

    The source is an opaque RGB logo (blue mark on white), so the alpha has to
    be derived from luminance -- that is what lets the same mark sit on both the
    light and the dark splash without a white box around it.
    """
    src = Image.open(SRC).convert("RGB")
    lum = src.convert("L")
    # white -> 0 alpha, ink -> 255 alpha, with a soft ramp so edges stay smooth
    alpha = lum.point(lambda v: 0 if v >= 250 else (255 if v <= 235 else int((250 - v) / 15 * 255)))
    mark = src.convert("RGBA")
    mark.putalpha(alpha)
    box = alpha.getbbox()
    return mark.crop(box) if box else mark


def _centred(mark: Image.Image, size: int, bg, fill: float) -> Image.Image:
    """Scale the mark to `fill` of the canvas width and centre it on `bg`."""
    target = max(1, int(size * fill))
    w, h = mark.size
    scale = target / max(w, h)
    m = mark.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    canvas = Image.new("RGBA", (size, size), bg + (255,))
    canvas.paste(m, ((size - m.width) // 2, (size - m.height) // 2), m)
    return canvas


def make_icon(size=1024):
    # Flattened to RGB on purpose: an app icon with an alpha channel is rejected.
    img = _centred(_mark(), size, PAGE, ICON_FILL).convert("RGB")
    img.save(OUT / "icon.png")
    print("wrote", OUT / "icon.png")


def make_splash(size=2732):
    mark = _mark()
    _centred(mark, size, PAGE, SPLASH_FILL).convert("RGB").save(OUT / "splash.png")
    _centred(mark, size, NIGHT, SPLASH_FILL).convert("RGB").save(OUT / "splash-dark.png")
    print("wrote", OUT / "splash.png", "and splash-dark.png")


if __name__ == "__main__":
    if not SRC.exists():
        raise SystemExit("brand artwork not found: %s" % SRC)
    make_icon()
    make_splash()
