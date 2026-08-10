"""Generate the quote-card template PNG (1122x1402) used by the quote renderer.

QuotePost.tsx shows only this card's header (top ~400px after scaling): avatar,
display name, handle. Everything below is painted over with the card background
(#f8f8f8) and replaced by the real quote text, so the body here stays blank.

Operators brand their install by re-running this script:

  .venv/Scripts/python.exe pipeline/scripts/make_quote_template.py \
      --name "Your Name" --handle "@yourhandle" [--avatar path/to/photo.jpg]

Requires Pillow (pip install pillow). Output overwrites
pipeline/src/contenido_bionico/shared/quote-template/quote_layout.png
unless --out is given.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont, ImageOps
except ImportError:
    sys.exit("Pillow is required: .venv/Scripts/pip.exe install pillow")

W, H = 1122, 1402
CARD_BG = (0xF8, 0xF8, 0xF8)
INK = (0x0F, 0x14, 0x19)
GRAY = (0x53, 0x64, 0x71)
AVATAR_BG = (0xCF, 0xD9, 0xDE)
# Header geometry (template px). Everything must sit above y=400 (the visible band).
AV_X, AV_Y, AV_D = 96, 140, 112
NAME_X, NAME_Y, NAME_PX = 236, 158, 46
HANDLE_Y, HANDLE_PX = 222, 34

BOLD_CANDIDATES = [
    r"C:\Windows\Fonts\segoeuib.ttf", r"C:\Windows\Fonts\arialbd.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
REGULAR_CANDIDATES = [
    r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def _font(candidates: list[str], px: int) -> ImageFont.FreeTypeFont:
    for c in candidates:
        if Path(c).exists():
            return ImageFont.truetype(c, px)
    try:
        return ImageFont.load_default(size=px)
    except TypeError:  # Pillow < 10.1: load_default() takes no size argument
        return ImageFont.load_default()


def _avatar(img: Image.Image, name: str, photo: Path | None) -> None:
    mask = Image.new("L", (AV_D, AV_D), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, AV_D, AV_D), fill=255)
    if photo and photo.exists():
        face = ImageOps.fit(Image.open(photo).convert("RGB"), (AV_D, AV_D))
    else:
        face = Image.new("RGB", (AV_D, AV_D), AVATAR_BG)
        initials = "".join(w[0] for w in name.split()[:2]).upper() or "CB"
        d = ImageDraw.Draw(face)
        f = _font(BOLD_CANDIDATES, 44)
        box = d.textbbox((0, 0), initials, font=f)
        d.text(((AV_D - box[2] + box[0]) / 2 - box[0], (AV_D - box[3] + box[1]) / 2 - box[1]),
               initials, font=f, fill=GRAY)
    img.paste(face, (AV_X, AV_Y), mask)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--name", default="Contenido Biónico")
    ap.add_argument("--handle", default="@contenidobionico")
    ap.add_argument("--avatar", type=Path, default=None)
    ap.add_argument("--out", type=Path,
                    default=Path(__file__).resolve().parents[1]
                    / "src" / "contenido_bionico" / "shared" / "quote-template" / "quote_layout.png")
    a = ap.parse_args()

    img = Image.new("RGB", (W, H), CARD_BG)
    d = ImageDraw.Draw(img)
    _avatar(img, a.name, a.avatar)
    d.text((NAME_X, NAME_Y), a.name, font=_font(BOLD_CANDIDATES, NAME_PX), fill=INK)
    d.text((NAME_X, HANDLE_Y + 36), a.handle, font=_font(REGULAR_CANDIDATES, HANDLE_PX), fill=GRAY)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    img.save(a.out, "PNG")
    print(f"wrote {a.out} ({W}x{H})")


if __name__ == "__main__":
    main()
