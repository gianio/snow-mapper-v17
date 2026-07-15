#!/usr/bin/env python3
"""Generate the App Store icon + splash source images for @capacitor/assets.

Outputs (into apple-app/resources/):
  icon.png    1024x1024, fully opaque, NO transparency / NO rounded corners
              (Apple rounds the corners itself; alpha/rounding => App Store reject).
  splash.png  2732x2732, light background with the centred violet snowflake mark,
              matching the web app's minimal loader.

Run: python3 scripts/make-resources.py   (from apple-app/)
Then: npx capacitor-assets generate --ios  (produces every required size).
"""
import math
from pathlib import Path
from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "resources"
OUT.mkdir(parents=True, exist_ok=True)

# Black & white identity (matches the app's monochrome steering UI)
INDIGO = (34, 34, 38)       # dark charcoal (gradient top)
VIOLET = (58, 58, 64)       # mid charcoal
NIGHT = (0, 0, 0)           # pure black (gradient bottom)
PAGE = (255, 255, 255)      # white app background


def _vgrad(size, top, bottom):
    img = Image.new("RGB", (size, size), top)
    px = img.load()
    for y in range(size):
        t = y / (size - 1)
        r = int(top[0] + (bottom[0] - top[0]) * t)
        g = int(top[1] + (bottom[1] - top[1]) * t)
        b = int(top[2] + (bottom[2] - top[2]) * t)
        for x in range(size):
            px[x, y] = (r, g, b)
    return img


def _snowflake(draw, cx, cy, arm, lw, color):
    for k in range(6):
        a = math.pi / 3 * k
        ex, ey = cx + math.cos(a) * arm, cy + math.sin(a) * arm
        draw.line([(cx, cy), (ex, ey)], fill=color, width=lw)
        for frac, blen in ((0.45, arm * 0.28), (0.72, arm * 0.22)):
            bx, by = cx + math.cos(a) * arm * frac, cy + math.sin(a) * arm * frac
            for s in (a + math.pi / 3, a - math.pi / 3):
                draw.line([(bx, by), (bx + math.cos(s) * blen, by + math.sin(s) * blen)],
                          fill=color, width=max(2, int(lw * 0.7)))


def make_icon(size=1024):
    img = _vgrad(size, INDIGO, NIGHT).convert("RGBA")
    d = ImageDraw.Draw(img)
    _snowflake(d, size / 2, size / 2, size * 0.30, max(6, int(size * 0.028)), (255, 255, 255, 255))
    img.convert("RGB").save(OUT / "icon.png")
    print("wrote", OUT / "icon.png")


def make_splash(size=2732):
    img = Image.new("RGB", (size, size), PAGE).convert("RGBA")
    d = ImageDraw.Draw(img)
    # centred rounded-square mark (matches the web loader's .mark)
    m = int(size * 0.135)
    x0, y0 = (size - m) // 2, (size - m) // 2
    rad = int(m * 0.30)
    # simple two-tone fill
    tile = _vgrad(m, VIOLET, INDIGO).convert("RGBA")
    mask = Image.new("L", (m, m), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, m - 1, m - 1], radius=rad, fill=255)
    img.paste(tile, (x0, y0), mask)
    _snowflake(d, size / 2, size / 2, m * 0.30, max(4, int(m * 0.035)), (255, 255, 255, 255))
    img.convert("RGB").save(OUT / "splash.png")
    # dark splash variant (optional; capacitor-assets picks it up if present)
    dark = Image.new("RGB", (size, size), NIGHT).convert("RGBA")
    dd = ImageDraw.Draw(dark)
    dark.paste(tile, (x0, y0), mask)
    _snowflake(dd, size / 2, size / 2, m * 0.30, max(4, int(m * 0.035)), (255, 255, 255, 255))
    dark.convert("RGB").save(OUT / "splash-dark.png")
    print("wrote", OUT / "splash.png", "and splash-dark.png")


if __name__ == "__main__":
    make_icon()
    make_splash()
