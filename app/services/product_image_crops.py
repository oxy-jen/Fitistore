"""Persistence and validation for per-card product image crop profiles."""

import json

from flask import current_app
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.models import ProductImageCrop


DEFAULT_PRODUCT_IMAGE_CROP = {
    "fit": "contain",
    "zoom": 100,
    "x": 50,
    "y": 50,
    "top": 0,
    "right": 0,
    "bottom": 0,
    "left": 0,
}
PRODUCT_IMAGE_CROP_POSTGRES_ADVISORY_LOCK = 724930126
_CROP_PROFILES = {"normal", "shop"}
_CROP_FITS = {"contain", "cover"}


def normalize_product_image_crop(value):
    """Validate a crop profile and return its canonical JSON-ready values."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            raise ValueError("Crop settings must be valid JSON.") from None
    if not isinstance(value, dict):
        raise ValueError("Crop settings must be an object.")
    legacy_keys = {"fit", "zoom", "x", "y"}
    crop_keys = {"top", "right", "bottom", "left"}
    if set(value) == legacy_keys:
        value = {**value, **{key: 0 for key in crop_keys}}
    elif set(value) != legacy_keys | crop_keys:
        raise ValueError("Crop settings must include fit, zoom, position, and all four crop edges.")
    if not isinstance(value["fit"], str) or value["fit"] not in _CROP_FITS:
        raise ValueError("Choose contain or cover for image fit.")
    normalized: dict[str, str | int] = {"fit": value["fit"]}
    for key, lower, upper in (
        ("zoom", 100, 300),
        ("x", 0, 100),
        ("y", 0, 100),
        ("top", 0, 45),
        ("right", 0, 45),
        ("bottom", 0, 45),
        ("left", 0, 45),
    ):
        number = value[key]
        if isinstance(number, bool) or not isinstance(number, int) or not lower <= number <= upper:
            raise ValueError(f"{key.capitalize()} must be a whole number from {lower} to {upper}.")
        normalized[key] = number
    if normalized["left"] + normalized["right"] > 90:
        raise ValueError("Left and right crop edges must leave at least 10% of the image visible.")
    if normalized["top"] + normalized["bottom"] > 90:
        raise ValueError("Top and bottom crop edges must leave at least 10% of the image visible.")
    return normalized


def _ensure_crop_table(connection):
    table = db.metadata.tables[ProductImageCrop.__tablename__]
    inspector = inspect(connection)
    if not inspector.has_table("product_image"):
        raise RuntimeError("The product image table is missing.")
    if not inspector.has_table(table.name):
        db.metadata.create_all(bind=connection, tables=[table], checkfirst=True)
    inspector = inspect(connection)
    if not inspector.has_table(table.name):
        raise RuntimeError("The product image crop table was not created.")
    columns = {column["name"] for column in inspector.get_columns(table.name)}
    if {column.name for column in table.columns} - columns:
        raise RuntimeError("The product image crop table is incomplete.")


def ensure_product_image_crop_schema(engine=None):
    """Create only the additive crop-settings table, serializing concurrent startups."""
    engine = engine or db.engine
    if engine.dialect.name == "postgresql":
        with engine.begin() as connection:
            connection.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"),
                {"lock_id": PRODUCT_IMAGE_CROP_POSTGRES_ADVISORY_LOCK},
            )
            _ensure_crop_table(connection)
    elif engine.dialect.name == "sqlite":
        with engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                _ensure_crop_table(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
    else:
        raise RuntimeError("Unsupported database dialect for product image crop settings.")
    return True


def product_image_crop_schema_ready():
    ready = current_app.extensions.get("product_image_crop_schema_ready")
    if ready is not None:
        return bool(ready)
    try:
        with db.engine.connect() as connection:
            inspector = inspect(connection)
            if not inspector.has_table(ProductImageCrop.__tablename__):
                return False
            actual = {column["name"] for column in inspector.get_columns(ProductImageCrop.__tablename__)}
            table = db.metadata.tables[ProductImageCrop.__tablename__]
            expected = {column.name for column in table.columns}
            return expected <= actual
    except SQLAlchemyError as exc:
        current_app.logger.error("Product image crop settings are unavailable (%s).", type(exc).__name__)
        return False


def product_image_crop_settings(image, profile):
    if profile not in _CROP_PROFILES:
        raise ValueError("Choose the normal or Shop image crop profile.")
    defaults = DEFAULT_PRODUCT_IMAGE_CROP.copy()
    if image is None or not product_image_crop_schema_ready():
        return defaults
    try:
        row = image.crop_settings
        raw = getattr(row, f"{profile}_settings", "") if row else ""
        return normalize_product_image_crop(raw) if raw else defaults
    except (TypeError, ValueError) as exc:
        current_app.logger.warning(
            "Invalid product image crop settings image_id=%s profile=%s (%s).",
            image.id,
            profile,
            str(exc),
        )
        return defaults
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.warning(
            "Could not load product image crop settings image_id=%s (%s).",
            image.id,
            type(exc).__name__,
        )
        return defaults


def save_product_image_crop_settings(image_id, profile, settings):
    """Update one image/profile within the caller's database transaction."""
    if profile not in _CROP_PROFILES:
        raise ValueError("Choose the normal or Shop image crop profile.")
    if not product_image_crop_schema_ready():
        raise RuntimeError("Product image crop storage is not ready.")
    normalized = normalize_product_image_crop(settings)
    row = db.session.get(ProductImageCrop, image_id)
    if row is None:
        row = ProductImageCrop()
        row.image_id = image_id
        db.session.add(row)
    setattr(row, f"{profile}_settings", json.dumps(normalized, separators=(",", ":")))
