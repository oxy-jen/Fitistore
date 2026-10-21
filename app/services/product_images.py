from __future__ import annotations

from pathlib import Path
from uuid import uuid4
from urllib.parse import quote

from flask import current_app, url_for

SAMPLE_IMAGE_PREFIX = "sample-"


class ImageStorageError(RuntimeError):
    pass


def cloudinary_configured() -> bool:
    return all(
        current_app.config.get(name)
        for name in ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET")
    )


def configure_cloudinary(require_secret: bool = True):
    required = ("CLOUDINARY_CLOUD_NAME",) if not require_secret else ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET")
    if not all(current_app.config.get(name) for name in required):
        raise ImageStorageError("Cloudinary is not configured. Set the Cloudinary environment variables before uploading product images.")
    import cloudinary

    cloudinary.config(
        cloud_name=current_app.config["CLOUDINARY_CLOUD_NAME"],
        api_key=current_app.config.get("CLOUDINARY_API_KEY"),
        api_secret=current_app.config.get("CLOUDINARY_API_SECRET"),
        secure=True,
    )


def cloudinary_public_id(product, original_filename: str, prefix: str = "products") -> str:
    safe_name = Path(original_filename or "product").stem[:60] or "product"
    safe_name = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in safe_name).strip("-_") or "product"
    product_part = f"product-{product.id}" if getattr(product, "id", None) else "new-product"
    return f"fity/{prefix}/{product_part}/{uuid4().hex}-{safe_name}"


def upload_image_bytes(data: bytes, public_id: str, resource_type: str = "image") -> dict:
    configure_cloudinary()
    import cloudinary.uploader

    try:
        return cloudinary.uploader.upload(
            data,
            public_id=public_id,
            resource_type=resource_type,
            overwrite=False,
            unique_filename=False,
            secure=True,
        )
    except Exception as exc:
        raise ImageStorageError("Cloudinary upload failed. Please try again.") from exc


def delete_cloudinary_asset(public_id: str) -> None:
    if not public_id:
        return
    configure_cloudinary()
    import cloudinary.uploader

    try:
        result = cloudinary.uploader.destroy(public_id, resource_type="image", invalidate=True)
    except Exception as exc:
        raise ImageStorageError("Cloudinary deletion failed. The product image was kept so cleanup can be retried.") from exc
    if result.get("result") not in {"ok", "not found"}:
        raise ImageStorageError("Cloudinary deletion was not confirmed. The product image was kept so cleanup can be retried.")


def cloudinary_image_url(image) -> str:
    public_id = getattr(image, "cloudinary_public_id", "")
    cloud_name = current_app.config.get("CLOUDINARY_CLOUD_NAME")
    if not public_id or not cloud_name:
        return ""
    return f"https://res.cloudinary.com/{quote(cloud_name, safe='')}/image/upload/{quote(public_id, safe='/')}"

def legacy_local_image_path(image):
    if not image or not getattr(image, "filename", ""):
        return None
    upload_dir = Path(current_app.config["UPLOAD_FOLDER"]).resolve()
    image_path = (upload_dir / image.filename).resolve()
    if image_path.parent != upload_dir:
        return None
    ext = image_path.suffix.lower().lstrip(".")
    if ext not in current_app.config["ALLOWED_IMAGE_EXTENSIONS"] and not image.filename.startswith(SAMPLE_IMAGE_PREFIX):
        return None
    return image_path


def valid_product_image(image) -> bool:
    if not image:
        return False
    if getattr(image, "cloudinary_public_id", ""):
        return bool(cloudinary_image_url(image))
    image_path = legacy_local_image_path(image)
    return bool(image_path and image_path.is_file())


def product_image_url(image) -> str:
    if image and getattr(image, "cloudinary_public_id", ""):
        url = cloudinary_image_url(image)
        if url:
            return url
    image_path = legacy_local_image_path(image)
    if image_path and image_path.is_file():
        return url_for("static", filename="uploads/" + image.filename)
    return url_for("static", filename="images/mainLogo.png")


def product_image_alt(image, fallback="Temporary product placeholder") -> str:
    if valid_product_image(image):
        return image.alt_text
    return fallback


def product_display_image(product):
    images = list(product.images)
    primary_cloudinary = [image for image in images if image.is_primary and getattr(image, "cloudinary_public_id", "")]
    cloudinary = [image for image in images if getattr(image, "cloudinary_public_id", "")]
    primary_legacy = [image for image in images if image.is_primary and not getattr(image, "cloudinary_public_id", "")]
    legacy = [image for image in images if not getattr(image, "cloudinary_public_id", "")]
    for image in primary_cloudinary + cloudinary + primary_legacy + legacy:
        if valid_product_image(image):
            return image
    return None