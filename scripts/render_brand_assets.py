"""Render the brand stills of every ladder kit into ``assets/brand/`` (1920x1080 RGB PNGs), and with ``--demo`` the
demo creatives' stills (demand demo mode) into ``assets/demo/``.

MediaConvert's VideoGenerator turns each still into a slate or house ad of a whole number of GOPs on the ladder
(SPEC §5.2), so the files are inputs, not decoration: changing one re-provisions every kit (``LadderProvisioner``
fingerprints them). The committed PNGs are what this script renders; run it after changing the design::

    uv run python scripts/render_brand_assets.py [--out assets/brand] [--font /path/to/Font.ttf]
    uv run python scripts/render_brand_assets.py --demo [--out assets/demo]

The default font is Noto Sans (SIL Open Font License), with DejaVu Sans as fallback; pass ``--font`` on systems
that have neither.
"""

import argparse
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from PIL import Image, ImageDraw, ImageFont

from fast_channels.core.demo_demand import DEMO_CREATIVES
from fast_channels.core.timeline import DEMO_AD_GOPS, HOUSE_AD_GOPS

WIDTH, HEIGHT = 1920, 1080
GOP_SECONDS: Final = 2  # nominal; at 29.97 fps a GOP lasts 2.002 s
OUT: Final = Path(__file__).resolve().parents[1] / "assets" / "brand"
DEMO_OUT: Final = OUT.parent / "demo"
FONTS: Final = (
    "/usr/share/fonts/google-noto-vf/NotoSans[wght].ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/noto/NotoSans-Regular.ttf",
    "/Library/Fonts/NotoSans-Regular.ttf",
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)
INK: Final = (0x23, 0x2F, 0x3E)  # AWS squid ink
DEEP_INK: Final = (0x0F, 0x14, 0x1A)
ORANGE: Final = (0xFF, 0x99, 0x00)
WHITE: Final = (0xFF, 0xFF, 0xFF)
MUTED: Final = (0xC6, 0xCD, 0xD6)
MARGIN: Final = 144  # inside the 90 % action-safe area
APOSTROPHE: Final = "\u2019"  # typographic, not the straight '


@dataclass(frozen=True, slots=True)
class Still:
    """One brand still: background gradient (top-left to bottom-right) and its text."""

    file: str
    top: tuple[int, int, int]
    bottom: tuple[int, int, int]
    headline: str
    subline: str
    badge: str | None = None


def stills() -> list[Still]:
    """The kit's stills: filler slate, break slate and one house ad per ``HOUSE_AD_GOPS`` length."""
    house_colours = (((0x09, 0x72, 0xD3), (0x03, 0x2A, 0x5C)), ((0x7D, 0x3A, 0xC1), (0x2A, 0x10, 0x4A)))
    return [
        Still("filler.png", INK, DEEP_INK, f"We{APOSTROPHE}ll be right back", "Stay tuned"),
        Still("break.png", INK, DEEP_INK, "Advertisement", "Programming resumes shortly"),
        *(
            Still(
                f"house-ad-{n}.png",
                top,
                bottom,
                f"House ad {n}",
                "Your message on every screen of the channel",
                badge=f"{gops * GOP_SECONDS} seconds",
            )
            for n, (gops, (top, bottom)) in enumerate(zip(HOUSE_AD_GOPS, house_colours, strict=True), start=1)
        ),
    ]


DEMO_COLOURS: Final = (
    ((0x1D, 0x81, 0x02), (0x0B, 0x3D, 0x01)),
    ((0xD1, 0x32, 0x12), (0x5C, 0x12, 0x05)),
    ((0x00, 0x73, 0xBB), (0x00, 0x2E, 0x4D)),
    ((0x00, 0x73, 0xBB), (0x00, 0x2E, 0x4D)),  # sim4: sim3's advertiser
    ((0x8D, 0x6E, 0x00), (0x3B, 0x2E, 0x00)),
    ((0x9A, 0x1F, 0x6E), (0x3D, 0x0B, 0x2B)),
    ((0x00, 0x7E, 0x7A), (0x00, 0x33, 0x31)),
    ((0x5A, 0x4F, 0xCF), (0x22, 0x1C, 0x5C)),
    ((0x41, 0x4D, 0x5C), (0x16, 0x1D, 0x26)),
    ((0x41, 0x4D, 0x5C), (0x16, 0x1D, 0x26)),
)
"""Background gradients of the demo creatives, in ``DEMO_CREATIVES`` order."""


def demo_stills() -> list[Still]:
    """One still per demo creative: its advertiser, domain and creative id, labelled simulated."""
    return [
        Still(
            f"{creative.crid}.png",
            top,
            bottom,
            creative.advertiser,
            f"{creative.adomain} \u00b7 {creative.crid} \u00b7 simulated demo ad",
            badge=f"{DEMO_AD_GOPS * GOP_SECONDS} seconds",
        )
        for creative, (top, bottom) in zip(DEMO_CREATIVES, DEMO_COLOURS, strict=True)
    ]


def font(path: str, size: int, weight: int) -> ImageFont.FreeTypeFont:
    """``path`` at ``size`` px, at ``weight`` when the font is variable."""
    face = ImageFont.truetype(path, size)
    with suppress(OSError):  # not a variable font
        face.set_variation_by_axes([weight])
    return face


def gradient(top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    """Diagonal two-colour gradient, rendered small and scaled up (smooth, and fast)."""
    small = Image.new("RGB", (64, 36))
    for x in range(64):
        for y in range(36):
            t = (x / 63 + y / 35) / 2
            small.putpixel((x, y), tuple(round(a + (b - a) * t) for a, b in zip(top, bottom, strict=True)))
    return small.resize((WIDTH, HEIGHT), Image.Resampling.BICUBIC)


def render(still: Still, font_path: str) -> Image.Image:
    """The still: brand line with an orange accent top-left, headline and subline centred, optional badge."""
    image = gradient(still.top, still.bottom)
    draw = ImageDraw.Draw(image)
    brand = font(font_path, 40, 600)
    draw.rectangle((MARGIN, MARGIN, MARGIN + 12, MARGIN + 52), fill=ORANGE)
    draw.text((MARGIN + 36, MARGIN + 26), "FAST Channels on AWS", font=brand, fill=WHITE, anchor="lm")
    centre = (WIDTH // 2, HEIGHT // 2)
    draw.text((centre[0], centre[1] - 40), still.headline, font=font(font_path, 140, 700), fill=WHITE, anchor="ms")
    draw.rectangle((centre[0] - 80, centre[1] + 6, centre[0] + 80, centre[1] + 14), fill=ORANGE)
    draw.text((centre[0], centre[1] + 96), still.subline, font=font(font_path, 52, 400), fill=MUTED, anchor="ms")
    if still.badge:
        label = font(font_path, 40, 600)
        left, _, right, _ = draw.textbbox((0, 0), still.badge, font=label)
        box = (WIDTH - MARGIN - (right - left) - 56, HEIGHT - MARGIN - 72, WIDTH - MARGIN, HEIGHT - MARGIN)
        draw.rounded_rectangle(box, radius=36, outline=ORANGE, width=4)
        draw.text(((box[0] + box[2]) // 2, (box[1] + box[3]) // 2), still.badge, font=label, fill=WHITE, anchor="mm")
    return image


def default_font() -> str:
    for candidate in FONTS:
        if Path(candidate).is_file():
            return candidate
    raise SystemExit("no default font found; pass --font /path/to/Font.ttf")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--demo", action="store_true", help="render the demo creatives' stills instead")
    parser.add_argument("--out", type=Path, help="output directory (default: assets/brand, or assets/demo)")
    parser.add_argument("--font", help="TrueType/OpenType font file (default: Noto Sans or DejaVu Sans)")
    args = parser.parse_args(argv)
    font_path = args.font or default_font()
    out: Path = args.out or (DEMO_OUT if args.demo else OUT)
    out.mkdir(parents=True, exist_ok=True)
    for still in demo_stills() if args.demo else stills():
        render(still, font_path).save(out / still.file, optimize=True)
        print(out / still.file)


if __name__ == "__main__":
    main()
