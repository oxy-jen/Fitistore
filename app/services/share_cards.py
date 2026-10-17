from __future__ import annotations

from io import BytesIO
from pathlib import Path
from urllib.request import Request, urlopen

from PIL import Image, ImageDraw, ImageFont, ImageOps

WIDTH, HEIGHT = 1200, 630
RED = "#E52335"
DARK = "#252525"
PALE = "#F2F2F2"
WHITE = "#FFFFFF"


def _font(size: int, bold: bool = False):
    candidates = (
        ["C:/Windows/Fonts/arialbd.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
        if bold else
        ["C:/Windows/Fonts/arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    )
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            pass
    return ImageFont.load_default()


def _read_source_image(image_path: Path | None, image_url: str):
    try:
        if image_path:
            with Image.open(image_path) as source:
                return ImageOps.exif_transpose(source).convert("RGBA")
        if image_url.startswith("https://res.cloudinary.com/"):
            request = Request(image_url, headers={"User-Agent": "FityStores-ShareCard/1.0"})
            with urlopen(request, timeout=5) as response:
                content_type = response.headers.get("Content-Type", "")
                if not content_type.startswith("image/"):
                    return None
                data = response.read(8 * 1024 * 1024 + 1)
            if len(data) > 8 * 1024 * 1024:
                return None
            with Image.open(BytesIO(data)) as source:
                return ImageOps.exif_transpose(source).convert("RGBA")
    except Exception:
        return None
    return None


def _fit_text(draw, text, font, max_width):
    text = " ".join((text or "FityStores product").split())
    if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
        return text
    while text and draw.textbbox((0, 0), text + "…", font=font)[2] > max_width:
        text = text[:-1]
    return text.rstrip() + "…"


def render_product_share_card(name: str, category: str, price: str, image_path: Path | None, image_url: str) -> bytes:
    canvas = Image.new("RGB", (WIDTH, HEIGHT), PALE)
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, 0, WIDTH, 24), fill=RED)
    draw.rounded_rectangle((52, 66, 1148, 570), radius=32, fill=WHITE)
    draw.rounded_rectangle((52, 66, 582, 570), radius=32, fill=DARK)
    draw.rectangle((550, 66, 582, 570), fill=DARK)
    draw.rounded_rectangle((80, 96, 554, 540), radius=22, fill=WHITE)

    source = _read_source_image(image_path, image_url)
    if source:
        source.thumbnail((430, 380), Image.Resampling.LANCZOS)
        x = 317 - source.width // 2
        y = 318 - source.height // 2
        canvas.paste(source, (x, y), source)
    else:
        draw.rounded_rectangle((146, 196, 488, 438), radius=24, fill=PALE)
        draw.text((184, 292), "FityStores", font=_font(42, True), fill=RED)

    draw.rounded_rectangle((80, 464, 228, 510), radius=18, fill=RED)
    draw.text((101, 475), "SHOP ONLINE", font=_font(18, True), fill=WHITE)
    draw.text((632, 122), "FITYSTORES", font=_font(25, True), fill=RED)
    draw.text((632, 170), _fit_text(draw, category.upper(), _font(17, True), 450), font=_font(17, True), fill="#737373")
    draw.multiline_text((628, 216), _fit_text(draw, name, _font(43, True), 452), font=_font(43, True), fill=DARK, spacing=8)
    draw.rounded_rectangle((628, 360, 1098, 456), radius=20, fill=PALE)
    draw.text((656, 373), "PRICE", font=_font(16, True), fill="#737373")
    draw.text((656, 398), _fit_text(draw, price, _font(35, True), 418), font=_font(35, True), fill=RED)
    draw.line((632, 492, 1098, 492), fill="#E5E5E5", width=2)
    draw.text((632, 514), "Shop with confidence at fitystore.top", font=_font(18, True), fill=DARK)

    output = BytesIO()
    canvas.save(output, format="PNG", optimize=True)
    return output.getvalue()
