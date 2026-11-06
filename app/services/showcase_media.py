from __future__ import annotations

from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path

from flask import current_app, url_for
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import inspect
from werkzeug.utils import secure_filename

from app.services.product_images import ImageStorageError, cloudinary_configured, upload_image_bytes

IMAGE_TYPES = {".jpg": "image", ".jpeg": "image", ".png": "image", ".webp": "image", ".gif": "gif"}
VIDEO_TYPES = {".mp4": "video", ".m4v": "video", ".mov": "video", ".webm": "video"}
MAX_MEDIA_UPLOAD = 3_500_000


def inspect_media_upload(filename: str, data: bytes) -> dict:
    safe_name = secure_filename(filename or "")
    suffix = Path(safe_name).suffix.lower()
    if not data:
        raise ValueError("Choose a non-empty media file.")
    if len(data) > min(current_app.config.get("MAX_CONTENT_LENGTH", MAX_MEDIA_UPLOAD), MAX_MEDIA_UPLOAD):
        raise ValueError("Media uploads are limited to 3.5 MB per file (within the app's 4 MB request limit).")
    if suffix in IMAGE_TYPES:
        try:
            with Image.open(BytesIO(data)) as image:
                image_format = (image.format or "").upper()
                dimensions = image.size
                frames = getattr(image, "n_frames", 1)
                image.verify()
            allowed_formats = {"JPEG", "PNG", "WEBP", "GIF"}
            if image_format not in allowed_formats:
                raise ValueError("Upload a valid JPEG, PNG, WebP, or GIF image.")
            extensions = {"JPEG": {".jpg", ".jpeg"}, "PNG": {".png"}, "WEBP": {".webp"}, "GIF": {".gif"}}
            if suffix not in extensions[image_format]:
                raise ValueError("The file extension does not match the actual image format.")
            if dimensions[0] <= 0 or dimensions[1] <= 0 or dimensions[0] * dimensions[1] > 30_000_000:
                raise ValueError("Image dimensions must be below 30 megapixels.")
            kind = "gif" if image_format == "GIF" else "image"
            return {"type": kind, "format": image_format.lower(), "width": dimensions[0], "height": dimensions[1], "frames": frames}
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise ValueError("The file contents are not a readable supported image.") from exc
    if suffix in VIDEO_TYPES:
        if suffix == ".webm":
            valid_signature = data.startswith(b"\x1a\x45\xdf\xa3")
        else:
            valid_signature = len(data) > 12 and data[4:8] == b"ftyp"
        if not valid_signature:
            raise ValueError("The file contents do not match an MP4, MOV, M4V, or WebM video.")
        return {"type": "video", "format": suffix[1:], "width": None, "height": None, "frames": None}
    raise ValueError("Choose a JPEG, PNG, WebP, GIF, MP4, MOV, M4V, or WebM file.")


def store_showcase_upload(filename: str, data: bytes, info: dict) -> dict:
    digest = sha256(data).hexdigest()
    safe_name = secure_filename(filename or "media")[:180] or "media"
    if cloudinary_configured():
        resource_type = "video" if info["type"] == "video" else "image"
        public_id = f"fity/media-showcases/{digest}"
        result = upload_image_bytes(data, public_id, resource_type=resource_type, overwrite=True)
        return {
            "url": result.get("secure_url", ""), "source": "cloudinary", "filename": safe_name,
            "size": int(result.get("bytes", len(data))), "width": result.get("width", info["width"]),
            "height": result.get("height", info["height"]), "duration": result.get("duration"),
            "public_id": result.get("public_id", public_id),
        }

    if os.environ.get("RENDER", "").lower() == "true" or current_app.config.get("SQLALCHEMY_DATABASE_URI", "").startswith(("postgres://", "postgresql://", "postgresql+")):
        raise ImageStorageError("Cloudinary is not configured. Uploads are disabled on hosted storage; paste a direct URL or reuse an existing media file.")

    static_root = Path(current_app.static_folder).resolve()
    upload_root = Path(current_app.config["UPLOAD_FOLDER"]).resolve() / "media-showcases"
    upload_root.mkdir(parents=True, exist_ok=True)
    upload_root = upload_root.resolve()
    if not upload_root.is_relative_to(static_root):
        raise ImageStorageError("Local uploads are not stored beneath the public static directory. Configure Cloudinary or a static upload folder.")
    ext = Path(safe_name).suffix.lower()
    stored_name = f"{digest}{ext}"
    target = (upload_root / stored_name).resolve()
    if target.parent != upload_root.resolve():
        raise ImageStorageError("The upload path was not safe; no file was stored.")
    if not target.exists():
        target.write_bytes(data)
    return {
        "url": url_for("static", filename=(target.relative_to(static_root)).as_posix()),
        "source": "upload", "filename": safe_name, "size": len(data),
        "width": info["width"], "height": info["height"], "duration": None,
    }


def _image_dimensions(path: Path):
    try:
        with Image.open(path) as image:
            return image.width, image.height
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
        return None, None


def existing_media_assets() -> dict:
    """Return bounded local assets and already-referenced public cloud URLs."""
    root = Path(current_app.static_folder).resolve()
    found = {}
    usages = {}
    broken = []
    for path in root.rglob("*"):
        if len(found) >= 250:
            break
        try:
            resolved = path.resolve()
            if not path.is_file() or not resolved.is_relative_to(root) or resolved.stat().st_size > 100 * 1024 * 1024:
                continue
            kind = IMAGE_TYPES.get(path.suffix.lower()) or VIDEO_TYPES.get(path.suffix.lower())
            if not kind:
                continue
            width, height = _image_dimensions(resolved) if kind in {"image", "gif"} else (None, None)
            relative = resolved.relative_to(root).as_posix()
            media_url = url_for("static", filename=relative)
            found[media_url] = {
                "url": media_url, "type": kind, "source": "library", "filename": path.name,
                "size": resolved.stat().st_size, "width": width, "height": height, "duration": None,
            }
        except (OSError, RuntimeError, ValueError):
            continue

    cloudinary_state = "not_configured"
    from app.models import CategoryThumbnail, ProductImage
    from app.services.product_images import cloudinary_image_url, legacy_local_image_path

    inspector = inspect(current_app.extensions["sqlalchemy"].engine)
    if inspector.has_table(ProductImage.__tablename__):
        for image in ProductImage.query.all():
            if image.cloudinary_public_id:
                media_url = cloudinary_image_url(image)
                if media_url:
                    found.setdefault(media_url, {"url": media_url, "type": "image", "source": "cloudinary", "filename": image.filename, "size": None, "width": None, "height": None, "duration": None})
                    usages[media_url] = usages.get(media_url, 0) + 1
            else:
                path = legacy_local_image_path(image)
                if path and path.is_file():
                    media_url = url_for("static", filename="uploads/" + image.filename)
                    width, height = _image_dimensions(path)
                    found.setdefault(media_url, {"url": media_url, "type": "gif" if path.suffix.lower() == ".gif" else "image", "source": "library", "filename": path.name, "size": path.stat().st_size, "width": width, "height": height, "duration": None})
                    usages[media_url] = usages.get(media_url, 0) + 1
    if inspector.has_table(CategoryThumbnail.__tablename__):
        for thumbnail in CategoryThumbnail.query.all():
            if thumbnail.cloudinary_public_id:
                media_url = cloudinary_image_url(thumbnail)
                if media_url:
                    found.setdefault(media_url, {"url": media_url, "type": "image", "source": "cloudinary", "filename": thumbnail.filename or "Category thumbnail", "size": None, "width": None, "height": None, "duration": None})
                    usages[media_url] = usages.get(media_url, 0) + 1

    from app.models import MediaShowcaseItem
    from urllib.parse import unquote, urlparse
    if inspector.has_table(MediaShowcaseItem.__tablename__):
        for item in MediaShowcaseItem.query.all():
            if not item.media_url:
                continue
            usages[item.media_url] = usages.get(item.media_url, 0) + 1
            parsed = urlparse(item.media_url)
            if item.media_url.startswith("/static/"):
                candidate = (root / unquote(parsed.path[len("/static/"):])).resolve()
                if not candidate.is_relative_to(root) or not candidate.is_file():
                    broken.append({"title": item.title, "url": item.media_url})

    if cloudinary_configured():
        cloudinary_state = "available"
        try:
            from app.services.product_images import configure_cloudinary
            configure_cloudinary()
            import cloudinary.api
            for resource_type in ("image", "video"):
                response = cloudinary.api.resources(resource_type=resource_type, type="upload", prefix="fity", max_results=100)
                for resource in response.get("resources", []):
                    url = resource.get("secure_url", "")
                    if not url:
                        continue
                    ext = str(resource.get("format", "")).lower()
                    kind = "video" if resource_type == "video" else ("gif" if ext == "gif" else "image")
                    found.setdefault(url, {
                        "url": url, "type": kind, "source": "cloudinary", "filename": resource.get("public_id", "").rsplit("/", 1)[-1],
                        "size": resource.get("bytes"), "width": resource.get("width"), "height": resource.get("height"),
                        "duration": resource.get("duration"), "public_id": resource.get("public_id", ""),
                    })
        except Exception as exc:
            cloudinary_state = "unavailable"
            current_app.logger.warning("Could not list Cloudinary media assets: %s", type(exc).__name__)
    assets = list(found.values())
    for asset in assets:
        asset["usage_count"] = usages.get(asset["url"], 0)
    return {"items": assets, "cloudinary": cloudinary_state, "limit": 250, "broken_local_references": broken}
