from __future__ import annotations

from io import BytesIO
from pathlib import Path
import hashlib
import json
import os
from uuid import uuid4
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
    # Facebook link previews use this landscape card. Give the product photo
    # most of the canvas so it remains recognizable at feed preview size.
    canvas = Image.new("RGB", (WIDTH, HEIGHT), WHITE)
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, 0, WIDTH, 16), fill=RED)
    draw.rounded_rectangle((34, 42, 1166, 588), radius=28, fill=PALE)

    photo_box = (52, 60, 748, 570)
    draw.rounded_rectangle(photo_box, radius=22, fill=WHITE)
    source = _read_source_image(image_path, image_url)
    if source:
        fitted = ImageOps.contain(source, (photo_box[2] - photo_box[0] - 28, photo_box[3] - photo_box[1] - 28), Image.Resampling.LANCZOS)
        x = photo_box[0] + (photo_box[2] - photo_box[0] - fitted.width) // 2
        y = photo_box[1] + (photo_box[3] - photo_box[1] - fitted.height) // 2
        canvas.paste(fitted, (x, y), fitted if fitted.mode == "RGBA" else None)
    else:
        draw.text((220, 285), "FityStores", font=_font(48, True), fill=RED)

    draw.rounded_rectangle((778, 60, 1148, 570), radius=22, fill=WHITE)
    draw.text((812, 96), "FITYSTORES", font=_font(22, True), fill=RED)
    draw.text((812, 145), _fit_text(draw, category.upper(), _font(16, True), 300), font=_font(16, True), fill="#737373")
    draw.multiline_text((808, 188), _fit_text(draw, name, _font(34, True), 300), font=_font(34, True), fill=DARK, spacing=7)
    draw.rounded_rectangle((808, 336, 1118, 426), radius=18, fill=PALE)
    draw.text((830, 350), "PRICE", font=_font(14, True), fill="#737373")
    draw.text((830, 375), _fit_text(draw, price, _font(28, True), 270), font=_font(28, True), fill=RED)
    draw.line((812, 454, 1118, 454), fill="#E5E5E5", width=2)
    draw.rounded_rectangle((812, 472, 1072, 520), radius=14, fill=RED)
    draw.text((838, 484), "SHOP NOW", font=_font(18, True), fill=WHITE)
    draw.text((812, 535), "fitystore.top", font=_font(15), fill="#737373")

    output = BytesIO()
    canvas.save(output, format="PNG", optimize=True)
    return output.getvalue()


def render_promo_share_card(title: str, description: str, image_path: Path | None, image_url: str = "", button_text: str = "LEARN MORE") -> bytes:
    """Render an ad-style Open Graph card for a public page or homepage section."""
    canvas = Image.new("RGB", (WIDTH, HEIGHT), WHITE)
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, 0, WIDTH, 16), fill=RED)
    draw.rounded_rectangle((34, 42, 1166, 588), radius=28, fill=PALE)
    photo_box = (52, 60, 748, 570)
    draw.rounded_rectangle(photo_box, radius=22, fill=WHITE)
    source = _read_source_image(image_path, image_url)
    if source:
        fitted = ImageOps.contain(source, (668, 482), Image.Resampling.LANCZOS)
        x = photo_box[0] + (photo_box[2] - photo_box[0] - fitted.width) // 2
        y = photo_box[1] + (photo_box[3] - photo_box[1] - fitted.height) // 2
        canvas.paste(fitted, (x, y), fitted if fitted.mode == "RGBA" else None)
    else:
        draw.text((220, 285), "FityStores", font=_font(48, True), fill=RED)
    draw.rounded_rectangle((778, 60, 1148, 570), radius=22, fill=DARK)
    draw.text((812, 92), "FityStores", font=_font(18, True), fill=WHITE)
    title_font = _font(34, True)
    title_lines = []
    words = " ".join((title or "Shop FityStores").split()).split()
    line = ""
    for word in words:
        candidate = (line + " " + word).strip()
        if draw.textbbox((0, 0), candidate, font=title_font)[2] > 300 and line:
            title_lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        title_lines.append(line)
    draw.multiline_text((808, 158), "\\n".join(title_lines[:3]), font=title_font, fill=WHITE, spacing=8)
    desc_font = _font(19)
    words = " ".join((description or "Everyday essentials, clear prices, and friendly support.").split()).split()
    lines, line = [], ""
    for word in words:
        candidate = (line + " " + word).strip()
        if draw.textbbox((0, 0), candidate, font=desc_font)[2] > 300 and line:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    draw.multiline_text((812, 310), "\\n".join(lines[:4]), font=desc_font, fill="#EEEEEE", spacing=8)
    draw.rounded_rectangle((812, 455, 1088, 510), radius=16, fill=RED)
    draw.text((835, 470), _fit_text(draw, button_text, _font(19, True), 230), font=_font(19, True), fill=WHITE)
    draw.text((812, 530), "fitystore.top", font=_font(15), fill="#D7D7D7")
    output = BytesIO()
    canvas.save(output, format="PNG", optimize=True)
    return output.getvalue()


def render_social_post_image(title: str, description: str, image_path: Path | None, image_url: str = "", price: str = "", button_text: str = "SHOP NOW") -> bytes:
    """Create a portrait share image with readable details."""
    width,height=1080,1350
    canvas=Image.new("RGB",(width,height),WHITE);draw=ImageDraw.Draw(canvas)
    draw.rectangle((0,0,width,18),fill=RED);draw.text((58,44),"FityStores",font=_font(24,True),fill=DARK)
    draw.rounded_rectangle((38,96,1042,836),radius=30,fill="#F2F2F2")
    source=_read_source_image(image_path,image_url)
    if source:
        if source.mode=="RGBA" and source.getchannel("A").getbbox():source=source.crop(source.getchannel("A").getbbox())
        fitted=ImageOps.contain(source,(950,700),Image.Resampling.LANCZOS)
        x,y=(width-fitted.width)//2,112+(700-fitted.height)//2
        canvas.paste(fitted,(x,y),fitted if fitted.mode=="RGBA" else None)
    else:draw.text((355,420),"FityStores",font=_font(56,True),fill=RED)
    draw.rounded_rectangle((38,858,1042,1312),radius=28,fill=WHITE,outline="#E4E6EA",width=2)
    draw.text((76,886),"EVERYDAY ESSENTIALS  |  FITS POA",font=_font(20,True),fill=RED)
    def wrap(text,font,limit,max_lines):
        result=[];line=""
        for word in " ".join((text or "").split()).split():
            trial=(line+" "+word).strip()
            if draw.textbbox((0,0),trial,font=font)[2]>limit and line:result.append(line);line=word
            else:line=trial
        if line:result.append(line)
        return "\n".join(result[:max_lines])
    title_font=_font(46,True);title_lines=wrap(title or "Shop FityStores",title_font,900,2)
    draw.multiline_text((76,932),title_lines,font=title_font,fill=DARK,spacing=4)
    desc_y=1048 if "\n" in title_lines else 1005
    desc_font=_font(25);draw.multiline_text((76,desc_y),wrap(description or "Everyday essentials at FityStores.",desc_font,900,2),font=desc_font,fill="#4E5663",spacing=7)
    if price:draw.text((76,1166),price,font=_font(34,True),fill=RED)
    draw.rounded_rectangle((680,1162,1002,1228),radius=20,fill=RED)
    bf=_font(22,True);draw.text((710,1181),_fit_text(draw,button_text,bf,270),font=bf,fill=WHITE)
    draw.text((76,1270),"Open the post link to view details and availability",font=_font(17),fill="#697386")
    out=BytesIO();canvas.save(out,format="PNG",optimize=True);return out.getvalue()


def cached_product_share_card(
    name: str,
    category: str,
    price: str,
    image_path: Path | None,
    image_url: str,
    cache_dir: Path,
    revision: str = "",
) -> bytes:
    """Return a cached share card, regenerating it when its source data changes."""
    image_stat = None
    if image_path:
        try:
            stat = image_path.stat()
            image_stat = {"mtime_ns": stat.st_mtime_ns, "size": stat.st_size}
        except OSError:
            image_stat = None
    key_material = json.dumps(
        ["large-product-preview-v2", name, category, price, image_url, str(image_path or ""), image_stat, revision],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    key = hashlib.sha256(key_material).hexdigest()
    folder = Path(cache_dir)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{key}.png"
    try:
        return target.read_bytes()
    except OSError:
        pass

    card = render_product_share_card(name, category, price, image_path, image_url)
    temporary = folder / f".{key}.{uuid4().hex}.tmp"
    try:
        temporary.write_bytes(card)
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return card


def clear_product_share_card_cache(cache_dir: Path) -> int:
    """Remove cached share-card files without following paths outside the cache folder."""
    folder = Path(cache_dir)
    if not folder.exists():
        return 0
    root = folder.resolve()
    removed = 0
    for path in folder.iterdir():
        if path.suffix not in {".png", ".tmp"}:
            continue
        try:
            resolved = path.resolve()
            if resolved.parent != root or not path.is_file():
                continue
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def product_share_card_cache_count(cache_dir: Path) -> int:
    folder = Path(cache_dir)
    if not folder.exists():
        return 0
    return sum(1 for path in folder.iterdir() if path.suffix == ".png" and path.is_file())
