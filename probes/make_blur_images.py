#!/usr/bin/env python3
"""Synthetic artwork for measuring a PMS's BlurHash and UltraBlur colours, written by rex's own hand.

Eight images with known colour layouts -- quadrants, gradients, stripes, a solid, a wide one, an
artist's and four bands -- saved as JPEG and PNG. They were placed beside plexav's `Rex Blur` albums (`cover.jpg`/
`cover.png`, `artist.jpg`) on 2026-10-31 so plexav's local media agent used them, and are checked in
(`tests/fixtures/artwork/`) for rex's suites to serve as the same artwork.

    python3 probes/make_blur_images.py <out dir>
"""
import os
import sys

from PIL import Image


def make(out):
    os.makedirs(out, exist_ok=True)
    w = h = 600
    img = Image.new("RGB", (w, h))
    px = img.load()
    quads = [(220, 40, 40), (40, 180, 60), (40, 60, 200), (230, 210, 40)]
    for y in range(h):
        for x in range(w):
            px[x, y] = quads[(x >= w // 2) + 2 * (y >= h // 2)]
    img.save(os.path.join(out, "quadrants.jpg"), quality=92)
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            v = x * 255 // (w - 1)
            px[x, y] = (v, v, v)
    img.save(os.path.join(out, "gradient.png"))
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = (x * 255 // (w - 1), y * 255 // (h - 1), 128)
    img.save(os.path.join(out, "diagonal.jpg"), quality=92)
    Image.new("RGB", (w, h), (30, 120, 140)).save(os.path.join(out, "solid.png"))
    img = Image.new("RGB", (w, h))
    px = img.load()
    stripes = [(255, 0, 0), (255, 128, 0), (255, 255, 0), (0, 200, 0), (0, 160, 255), (40, 40, 220), (150, 0, 200), (255, 255, 255)]
    for y in range(h):
        for x in range(w):
            px[x, y] = stripes[x * 8 // w]
    img.save(os.path.join(out, "stripes.jpg"), quality=92)
    img = Image.new("RGB", (1200, 600))
    px = img.load()
    for y in range(600):
        for x in range(1200):
            px[x, y] = (240, 140, 30) if x < 600 else (110, 40, 160)
    img.save(os.path.join(out, "wide.jpg"), quality=92)
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = (20, 20, 20) if y < h // 2 else (230, 120, 170)
    img.save(os.path.join(out, "artist.jpg"), quality=92)
    # Placed by `record_blur.py --fresh` as a new album, to catch a row before its analysis.
    img = Image.new("RGB", (500, 500))
    px = img.load()
    bands = [(200, 30, 30), (30, 30, 200), (30, 160, 60), (220, 200, 40)]
    for y in range(500):
        for x in range(500):
            px[x, y] = bands[y * 4 // 500]
    img.save(os.path.join(out, "fresh.jpg"), quality=92)


if __name__ == "__main__":
    make(sys.argv[1])
