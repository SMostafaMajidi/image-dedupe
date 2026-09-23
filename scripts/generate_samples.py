"""Generate fixed synthetic sample images for reproducible CLIP similarity tests.

Uses only Pillow so it works before torch/transformers are installed.
Paths are POSIX-style relative paths for portability (Windows + Linux).
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "sample_images"


def _save(img: Image.Image, name: str) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / name
    img.convert("RGB").save(path, format="JPEG", quality=92)
    print(f"wrote {path.as_posix()}")
    return path


def make_sunset(width: int = 384, height: int = 256) -> Image.Image:
    img = Image.new("RGB", (width, height))
    pixels = img.load()
    for y in range(height):
        t = y / (height - 1)
        r = int(255 * (1.0 - 0.35 * t))
        g = int(120 * (1.0 - t) + 40 * t)
        b = int(40 + 140 * t)
        for x in range(width):
            pixels[x, y] = (r, g, b)
    draw = ImageDraw.Draw(img)
    cx, cy, rad = width // 2, int(height * 0.42), 36
    draw.ellipse((cx - rad, cy - rad, cx + rad, cy + rad), fill=(255, 220, 80))
    return img


def make_ocean(width: int = 384, height: int = 256) -> Image.Image:
    img = Image.new("RGB", (width, height))
    pixels = img.load()
    for y in range(height):
        t = y / (height - 1)
        r = int(20 + 30 * t)
        g = int(90 + 80 * t)
        b = int(160 + 70 * t)
        for x in range(width):
            wave = int(8 * ((x / 20) % 2))
            pixels[x, y] = (r, min(255, g + wave), min(255, b + wave // 2))
    return img


def make_forest(width: int = 384, height: int = 256) -> Image.Image:
    img = Image.new("RGB", (width, height), (34, 90, 40))
    draw = ImageDraw.Draw(img)
    for i, x in enumerate(range(30, width, 55)):
        top = 40 + (i % 3) * 18
        draw.polygon([(x, top), (x - 22, height - 20), (x + 22, height - 20)], fill=(20, 70 + i * 3, 28))
        draw.rectangle((x - 4, height - 40, x + 4, height), fill=(60, 40, 20))
    return img


def make_city(width: int = 384, height: int = 256) -> Image.Image:
    img = Image.new("RGB", (width, height), (180, 200, 220))
    draw = ImageDraw.Draw(img)
    heights = [90, 140, 110, 160, 100, 150, 120]
    x = 20
    for i, h in enumerate(heights):
        w = 40 + (i % 3) * 8
        y0 = height - h
        draw.rectangle((x, y0, x + w, height), fill=(70 + i * 8, 75, 90))
        for wy in range(y0 + 10, height - 10, 18):
            for wx in range(x + 6, x + w - 6, 12):
                draw.rectangle((wx, wy, wx + 6, wy + 8), fill=(230, 220, 120))
        x += w + 10
    return img


def main() -> None:
    sunset = make_sunset()
    sunset_similar = ImageEnhance.Brightness(sunset.copy()).enhance(1.12)
    sunset_similar = sunset_similar.filter(ImageFilter.SMOOTH)

    ocean = make_ocean()
    ocean_similar = ocean.copy().crop((12, 8, 372, 248)).resize((384, 256))

    forest = make_forest()
    city = make_city()

    _save(sunset, "sunset.jpg")
    _save(sunset_similar, "sunset_bright.jpg")
    _save(ocean, "ocean.jpg")
    _save(ocean_similar, "ocean_crop.jpg")
    _save(forest, "forest.jpg")
    _save(city, "city.jpg")

    marker = OUT_DIR / ".generated"
    marker.write_text("ok\n", encoding="utf-8")
    print("sample images ready")


if __name__ == "__main__":
    main()
