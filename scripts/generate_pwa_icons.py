"""Generate the DeepSeek Infra PWA icon and favicon assets."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "static"
ICON_DIR = STATIC_DIR / "icons"

BRAND = (77, 107, 254)
BRAND_STRONG = (49, 92, 255)
INDIGO = (31, 50, 138)
CYAN = (93, 230, 255)
WHITE = (255, 255, 255)


def find_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        Path("C:/Windows/Fonts/seguisb.ttf"),
        Path("C:/Windows/Fonts/segoeuib.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    ]
    for path in candidates:
        if path.is_file():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def lerp(start: int, end: int, ratio: float) -> int:
    return round(start + (end - start) * ratio)


def gradient_square(size: int) -> Image.Image:
    image = Image.new("RGBA", (size, size), BRAND)
    pixels = image.load()
    assert pixels is not None
    for y in range(size):
        vertical = y / max(1, size - 1)
        for x in range(size):
            diagonal = (x / max(1, size - 1) + vertical) / 2
            color = tuple(lerp(BRAND_STRONG[index], BRAND[index], diagonal) for index in range(3))
            pixels[x, y] = (*color, 255)
    return image


def draw_spark(draw: ImageDraw.ImageDraw, center: tuple[float, float], radius: float, fill: tuple[int, int, int]) -> None:
    x, y = center
    points = [
        (x, y - radius),
        (x + radius * 0.22, y - radius * 0.22),
        (x + radius, y),
        (x + radius * 0.22, y + radius * 0.22),
        (x, y + radius),
        (x - radius * 0.22, y + radius * 0.22),
        (x - radius, y),
        (x - radius * 0.22, y - radius * 0.22),
    ]
    draw.polygon(points, fill=fill)


def icon_image(size: int, *, maskable: bool = False) -> Image.Image:
    scale = size / 512
    image = gradient_square(size)
    draw = ImageDraw.Draw(image)

    if not maskable:
        mask = Image.new("L", (size, size), 0)
        mask_draw = ImageDraw.Draw(mask)
        mask_draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=round(108 * scale), fill=255)
        image.putalpha(mask)

    # Soft diagonal layer keeps the icon recognizable against both light and dark launchers.
    draw.polygon(
        [
            (round(356 * scale), 0),
            (size, 0),
            (size, round(252 * scale)),
            (round(252 * scale), size),
            (round(106 * scale), size),
        ],
        fill=(92, 136, 255, 88),
    )

    bubble_box = [round(118 * scale), round(140 * scale), round(394 * scale), round(346 * scale)]
    draw.rounded_rectangle(bubble_box, radius=round(58 * scale), fill=(255, 255, 255, 246))
    draw.polygon(
        [
            (round(220 * scale), round(334 * scale)),
            (round(260 * scale), round(334 * scale)),
            (round(218 * scale), round(388 * scale)),
        ],
        fill=(255, 255, 255, 246),
    )

    font = find_font(round(172 * scale))
    label = "D"
    bbox = draw.textbbox((0, 0), label, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    text_x = (size - text_width) / 2 - bbox[0] - round(2 * scale)
    text_y = round(130 * scale) + (round(206 * scale) - text_height) / 2 - bbox[1] - round(4 * scale)
    draw.text((text_x, text_y), label, font=font, fill=BRAND_STRONG)

    ring_box = [round(318 * scale), round(294 * scale), round(384 * scale), round(360 * scale)]
    draw.ellipse(ring_box, outline=INDIGO, width=max(2, round(15 * scale)))
    draw.line(
        [(round(366 * scale), round(346 * scale)), (round(408 * scale), round(388 * scale))],
        fill=INDIGO,
        width=max(2, round(15 * scale)),
    )
    draw_spark(draw, (round(382 * scale), round(122 * scale)), round(36 * scale), CYAN)
    return image


def write_svg(path: Path) -> None:
    path.write_text(
        """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" role="img" aria-label="DeepSeek Infra">
  <defs>
    <linearGradient id="bg" x1="74" y1="32" x2="438" y2="480" gradientUnits="userSpaceOnUse">
      <stop stop-color="#315cff"/>
      <stop offset="1" stop-color="#4d6bfe"/>
    </linearGradient>
  </defs>
  <rect width="512" height="512" rx="108" fill="url(#bg)"/>
  <path d="M356 0h156v252L252 512H106z" fill="#5c88ff" opacity=".35"/>
  <path d="M176 140h160a58 58 0 0 1 58 58v90a58 58 0 0 1-58 58h-76l-42 42 10-42h-52a58 58 0 0 1-58-58v-90a58 58 0 0 1 58-58z" fill="#fff" opacity=".97"/>
  <text x="256" y="311" text-anchor="middle" font-family="Segoe UI, Arial, sans-serif" font-size="172" font-weight="700" fill="#315cff">D</text>
  <circle cx="351" cy="327" r="26" fill="none" stroke="#1f328a" stroke-width="15"/>
  <path d="m368 346 40 40" stroke="#1f328a" stroke-width="15" stroke-linecap="round"/>
  <path d="M382 86 390 114 418 122 390 130 382 158 374 130 346 122 374 114z" fill="#5de6ff"/>
</svg>
""",
        encoding="utf-8",
    )


def main() -> int:
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    write_svg(ICON_DIR / "icon.svg")
    write_svg(ICON_DIR / "favicon.svg")

    for filename, size, maskable in [
        ("pwa-192x192.png", 192, False),
        ("pwa-512x512.png", 512, False),
        ("maskable-192x192.png", 192, True),
        ("maskable-512x512.png", 512, True),
        ("apple-touch-icon.png", 180, False),
        ("favicon-32x32.png", 32, False),
        ("favicon-16x16.png", 16, False),
        ("badge-96x96.png", 96, True),
    ]:
        icon_image(size, maskable=maskable).save(ICON_DIR / filename)

    favicon_images = [icon_image(size, maskable=False).convert("RGBA") for size in (16, 32, 48)]
    favicon_images[0].save(STATIC_DIR / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
