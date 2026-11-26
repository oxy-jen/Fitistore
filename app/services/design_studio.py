from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from html.parser import HTMLParser
import json
import math
import re

from flask import current_app
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.models import (
    AdminUser, DesignStudioRole, Product, ProductVariant, StorefrontDesign,
    StorefrontDesignAudit, StorefrontDesignRevision,
)

DESIGN_TABLES = (
    DesignStudioRole.__table__, StorefrontDesign.__table__,
    StorefrontDesignRevision.__table__, StorefrontDesignAudit.__table__,
)
BASE_TABLES = {"admin_user", "product"}
ROLE_NAMES = {"owner", "designer", "editor", "approver", "publisher", "viewer"}
MAX_HTML = 20_000
MAX_CSS = 20_000
MAX_JS = 10_000
MAX_LOTTIE_BYTES = 300_000
MAX_PRODUCTS = 30
MOTION_PRESETS = {"none", "fade", "rise", "scale"}
TEMPLATE_KEYS = {"editorial", "product-grid", "seasonal"}


class DesignStudioSchemaError(RuntimeError):
    pass


def ensure_design_studio_schema(engine=None):
    engine = engine or db.engine
    with engine.begin() as connection:
        if engine.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": 724930121})
        elif engine.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            raise DesignStudioSchemaError(f"Unsupported database dialect: {engine.dialect.name}")
        inspector = inspect(connection)
        missing_base = BASE_TABLES - set(inspector.get_table_names())
        if missing_base:
            raise DesignStudioSchemaError("Required store tables are missing: " + ", ".join(sorted(missing_base)))
        db.metadata.create_all(bind=connection, tables=list(DESIGN_TABLES), checkfirst=True)
        for table in DESIGN_TABLES:
            for index in table.indexes:
                index.create(bind=connection, checkfirst=True)
        _validate_schema(connection)
        _bootstrap_roles(connection)
    return True


def _validate_schema(connection):
    inspector = inspect(connection)
    problems = []
    for table in DESIGN_TABLES:
        if not inspector.has_table(table.name):
            problems.append(f"{table.name} table")
            continue
        columns = {column["name"] for column in inspector.get_columns(table.name)}
        missing = {column.name for column in table.columns} - columns
        if missing:
            problems.append(f"{table.name} columns: " + ", ".join(sorted(missing)))
    if problems:
        raise DesignStudioSchemaError("Incomplete Design Studio schema: " + "; ".join(problems))


def _bootstrap_roles(connection):
    inspector = inspect(connection)
    if not inspector.has_table(DesignStudioRole.__tablename__):
        raise DesignStudioSchemaError("Design Studio role table is unavailable.")
    existing = {
        row[0] for row in connection.execute(
            text("SELECT admin_user_id FROM design_studio_role")
        )
    }
    admins = connection.execute(text("SELECT id FROM admin_user ORDER BY id ASC")).all()
    if not admins:
        return
    has_owner = connection.execute(
        text("SELECT 1 FROM design_studio_role WHERE role='owner' LIMIT 1")
    ).first() is not None
    if not has_owner:
        owner_id = admins[0][0]
        if owner_id in existing:
            connection.execute(
                text("UPDATE design_studio_role SET role='owner', updated_at=CURRENT_TIMESTAMP "
                     "WHERE admin_user_id=:admin_id"),
                {"admin_id": owner_id},
            )
        else:
            connection.execute(
                text("INSERT INTO design_studio_role (admin_user_id, role, created_at, updated_at) "
                     "VALUES (:admin_id, 'owner', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"),
                {"admin_id": owner_id},
            )
            existing.add(owner_id)
        has_owner = True
    for row in admins:
        admin_id = row[0]
        if admin_id in existing:
            continue
        role = "designer"
        connection.execute(
            text("INSERT INTO design_studio_role (admin_user_id, role, created_at, updated_at) "
                 "VALUES (:admin_id, :role, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"),
            {"admin_id": admin_id, "role": role},
        )


def schema_ready():
    startup = current_app.extensions.get("design_studio_schema_ready")
    if startup is not None:
        return bool(startup)
    try:
        with db.engine.connect() as connection:
            _validate_schema(connection)
    except (SQLAlchemyError, DesignStudioSchemaError) as exc:
        current_app.logger.error("Design Studio is unavailable; run migrate-design-studio-schema (%s).", type(exc).__name__)
        return False
    current_app.extensions["design_studio_schema_ready"] = True
    return True


def role_for(admin):
    if not isinstance(admin, AdminUser):
        return None
    if not schema_ready():
        return None
    assignment = DesignStudioRole.query.filter_by(admin_user_id=admin.id).first()
    has_owner = DesignStudioRole.query.filter_by(role="owner").first() is not None
    first_admin = AdminUser.query.order_by(AdminUser.id.asc()).with_entities(AdminUser.id).first()
    if assignment:
        if not has_owner and first_admin and first_admin[0] == admin.id and assignment.role != "owner":
            assignment.role = "owner"
            audit("role_bootstrapped", details={"role": "owner"}, admin=admin)
            db.session.commit()
        return assignment.role
    role = "owner" if not has_owner and first_admin and first_admin[0] == admin.id else "designer"
    db.session.add(DesignStudioRole(admin_user_id=admin.id, role=role))
    audit("role_bootstrapped", details={"role": role}, admin=admin)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        assignment = DesignStudioRole.query.filter_by(admin_user_id=admin.id).first()
        if assignment is None:
            raise
        role = assignment.role
    return role


def audit(action, design=None, details=None, admin=None):
    db.session.add(StorefrontDesignAudit(
        design_id=design.id if design else None,
        admin_user_id=admin.id if admin else None,
        action=action,
        details=json.dumps(details or {}, separators=(",", ":"), sort_keys=True),
    ))


def snapshot_design(design):
    return {
        "name": design.name, "title": design.title, "description": design.description,
        "placement": design.placement, "status": design.status, "is_enabled": design.is_enabled,
        "starts_at": design.starts_at.isoformat() if design.starts_at else None,
        "expires_at": design.expires_at.isoformat() if design.expires_at else None,
        "html": design.html, "css": design.css, "javascript": design.javascript,
        "lottie_json": design.lottie_json or "",
        "product_ids": json.loads(design.product_ids or "[]"), "template_key": design.template_key,
        "motion_preset": design.motion_preset, "motion_config": json.loads(design.motion_config or "{}"),
    }


def save_revision(design, admin, action):
    snapshot = snapshot_design(design)
    db.session.add(StorefrontDesignRevision(
        design_id=design.id, revision_number=design.version,
        snapshot=json.dumps(snapshot, separators=(",", ":"), sort_keys=True),
        created_by_id=admin.id,
    ))
    audit(action, design, {"version": design.version}, admin)


SAFE_TAGS = {
    "a", "article", "aside", "b", "br", "button", "div", "em", "figure", "figcaption",
    "h1", "h2", "h3", "h4", "hr", "i", "img", "li", "ol", "p", "section", "small",
    "span", "strong", "ul", "video", "source",
}
VOID_TAGS = {"br", "hr", "img", "source"}
SAFE_ATTRS = {"alt", "aria-hidden", "aria-label", "class", "controls", "data-product-id", "data-studio-action", "data-studio-lottie", "height", "id", "loading", "loop", "muted", "playsinline", "poster", "src", "type", "width", "href"}


def _safe_url(value, *, image=False):
    value = (value or "").strip()
    if "\\" in value or re.search(r"%(?:2e|2f|5c)", value, re.I) or any(ord(char) < 32 for char in value):
        return ""
    if value.startswith("/static/") and not value.startswith("//") and ".." not in value:
        return value
    if image and value.startswith("https://res.cloudinary.com/") and len(value) <= 1000:
        return value
    if not image and (value in {"/shop", "/upcoming", "/cart"}
                      or re.fullmatch(r"/product/[A-Za-z0-9_-]{1,170}", value)):
        return value
    if image and re.match(r"^data:image/(png|jpeg|webp|gif);base64,[A-Za-z0-9+/=]+$", value):
        return value[:250_000]
    return ""


class _DesignMarkupSanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.output = []
        self.open_tags = []
        self.suppressed_tags = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in {"script", "iframe", "object", "embed", "form", "style", "svg", "math"}:
            self.suppressed_tags.append(tag)
            return
        if self.suppressed_tags or tag not in SAFE_TAGS:
            return
        safe = []
        for name, value in attrs:
            name, value = name.lower(), value or ""
            if name.startswith("on") or name not in SAFE_ATTRS:
                continue
            if name in {"href", "src", "poster"}:
                value = _safe_url(value, image=name in {"src", "poster"})
                if not value:
                    continue
            if name == "data-product-id" and not value.isdigit():
                continue
            if name == "data-studio-action" and value not in {"add-to-cart", "open-product", "navigate"}:
                continue
            safe.append(f' {name}="{escape(value, quote=True)}"')
        self.output.append("<" + tag + "".join(safe) + ">")
        if tag not in VOID_TAGS:
            self.open_tags.append(tag)

    def handle_endtag(self, tag):
        if self.suppressed_tags:
            if tag.lower() in self.suppressed_tags:
                self.suppressed_tags.pop()
            return
        if tag in self.open_tags:
            while self.open_tags:
                current = self.open_tags.pop()
                self.output.append(f"</{current}>")
                if current == tag:
                    break

    def handle_data(self, data):
        if not self.suppressed_tags:
            self.output.append(escape(data, quote=False))

    def close(self):
        super().close()
        while self.open_tags:
            self.output.append(f"</{self.open_tags.pop()}>")


def sanitize_design_html(value):
    parser = _DesignMarkupSanitizer()
    parser.feed(value or "")
    parser.close()
    return "".join(parser.output)


def parse_lottie_animation(value):
    if not value:
        return None
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_LOTTIE_BYTES:
        raise ValueError("Lottie JSON must be no larger than 300 KB.")
    try:
        animation = json.loads(value)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("Upload valid Lottie animation JSON.") from exc
    if not isinstance(animation, dict):
        raise ValueError("Lottie animation data must be a JSON object.")
    required = ("w", "h", "fr", "ip", "op", "layers")
    if any(key not in animation for key in required):
        raise ValueError("Lottie JSON is missing required animation fields.")
    for key in ("w", "h", "fr", "ip", "op"):
        number = animation[key]
        try:
            is_finite = math.isfinite(number) if isinstance(number, (int, float)) else False
        except (OverflowError, TypeError):
            is_finite = False
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not is_finite:
            raise ValueError("Lottie dimensions and timing values must be finite numbers.")
    if not (1 <= animation["w"] <= 2048 and 1 <= animation["h"] <= 2048):
        raise ValueError("Lottie dimensions must be between 1 and 2048 pixels.")
    duration_frames = animation["op"] - animation["ip"]
    if not (
        1 <= animation["fr"] <= 60
        and 0 <= animation["ip"] < animation["op"] <= 1800
        and duration_frames / animation["fr"] <= 30
    ):
        raise ValueError("Lottie frame rate or duration is outside the supported range.")
    if not isinstance(animation["layers"], list) or not animation["layers"] or len(animation["layers"]) > 200:
        raise ValueError("Lottie animations must contain between 1 and 200 layers.")
    if not isinstance(animation.get("assets", []), list) or len(animation.get("assets", [])) > 100:
        raise ValueError("Lottie animations may include up to 100 embedded assets.")

    nodes: list[tuple[object, int]] = [(animation, 0)]
    visited = 0
    while nodes:
        node, depth = nodes.pop()
        visited += 1
        if depth > 64 or visited > 12_000:
            raise ValueError("Lottie animation data is too complex.")
        if isinstance(node, dict):
            assets = node.get("assets")
            if assets is not None:
                if not isinstance(assets, list):
                    raise ValueError("Lottie asset references must be a list.")
                for asset in assets:
                    if not isinstance(asset, dict):
                        raise ValueError("Lottie assets must be JSON objects.")
                    if asset.get("u") not in (None, ""):
                        raise ValueError("Lottie assets must not load external files.")
                    asset_path = asset.get("p")
                    if asset_path and not re.fullmatch(
                        r"data:image/(?:png|jpeg|webp|gif);base64,[A-Za-z0-9+/=]+",
                        str(asset_path),
                        re.I,
                    ):
                        raise ValueError("Lottie images must be embedded data images.")
            for child in node.values():
                if isinstance(child, (dict, list)):
                    nodes.append((child, depth + 1))
        elif isinstance(node, list):
            for child in node:
                if isinstance(child, (dict, list)):
                    nodes.append((child, depth + 1))
    try:
        json.dumps(animation, separators=(",", ":"), ensure_ascii=True)
    except RecursionError as exc:
        raise ValueError("Lottie animation data is too complex.") from exc
    return animation


def validate_design_payload(data):
    if not isinstance(data, dict):
        raise ValueError("Design data must be an object.")
    for field in ("name", "title", "description", "html", "css", "javascript", "template_key", "lottie_json"):
        if field in data and not isinstance(data[field], str):
            raise ValueError(f"{field.replace('_', ' ').title()} must be text.")
    name = data.get("name", "").strip()[:120]
    if not name:
        raise ValueError("Enter a design name.")
    placement = data.get("placement", "storefront_banner")
    if placement not in {"storefront_banner", "storefront_section"}:
        raise ValueError("Choose a supported storefront placement.")
    html = data.get("html", "")
    css = data.get("css", "")
    javascript = data.get("javascript", "")
    if len(html) > MAX_HTML or len(css) > MAX_CSS or len(javascript) > MAX_JS:
        raise ValueError("Design code exceeds the editor size limits.")
    if re.search(r"@import|expression\s*\(|-moz-binding|behavior\s*:|</\s*style", css, re.I):
        raise ValueError("CSS imports and executable CSS are not allowed.")
    for raw_url in re.findall(r"url\(\s*(['\"]?)(.*?)\1\s*\)", css, re.I):
        if not _safe_url(raw_url[1], image=True):
            raise ValueError("CSS images must use local static assets, Cloudinary, or inline image data.")
    if re.search(r"\b(fetch|XMLHttpRequest|WebSocket|EventSource|importScripts|eval|Function|setInterval|setTimeout)\s*\(", javascript, re.I):
        raise ValueError("Network APIs, dynamic code evaluation, and timer APIs are not allowed in custom JavaScript.")
    lottie_animation = parse_lottie_animation(data.get("lottie_json", ""))
    product_ids = data.get("product_ids", [])
    if isinstance(product_ids, str):
        try:
            product_ids = json.loads(product_ids or "[]")
        except json.JSONDecodeError as exc:
            raise ValueError("Product links must be a JSON list of active product IDs.") from exc
    if not isinstance(product_ids, list) or len(product_ids) > MAX_PRODUCTS:
        raise ValueError("Select up to 30 catalog products.")
    normalized_ids = []
    for raw_id in product_ids:
        if isinstance(raw_id, bool) or not isinstance(raw_id, (int, str)):
            raise ValueError("Product links must use active product IDs.")
        try:
            product_id = int(raw_id)
        except (TypeError, ValueError):
            raise ValueError("Product links must use active product IDs.")
        if product_id < 1:
            raise ValueError("Product links must use active product IDs.")
        normalized_ids.append(product_id)
    if len(set(normalized_ids)) != len(normalized_ids):
        raise ValueError("A product can only be linked once.")
    if normalized_ids:
        found = {row[0] for row in db.session.query(Product.id).filter(
            Product.id.in_(normalized_ids), Product.active.is_(True)
        ).all()}
        if found != set(normalized_ids):
            raise ValueError("Every linked product must exist and be active.")
    motion = data.get("motion_preset", "none")
    if motion not in MOTION_PRESETS:
        raise ValueError("Choose a supported motion preset.")
    motion_config = data.get("motion_config", {})
    if not isinstance(motion_config, dict):
        raise ValueError("Motion settings must be an object.")
    duration = motion_config.get("duration", 600)
    if isinstance(duration, bool):
        raise ValueError("Motion duration must be from 100 to 3000 milliseconds.")
    try:
        duration = max(100, min(3000, int(duration)))
    except (TypeError, ValueError):
        raise ValueError("Motion duration must be from 100 to 3000 milliseconds.")
    motion_config = {"duration": duration}
    template_key = data.get("template_key", "editorial")
    if template_key not in TEMPLATE_KEYS:
        raise ValueError("Choose one of the reusable Design Studio templates.")
    starts_at = _parse_datetime(data.get("starts_at"))
    expires_at = _parse_datetime(data.get("expires_at"))
    if starts_at and expires_at and starts_at >= expires_at:
        raise ValueError("The schedule end must be after its start.")
    return {
        "name": name, "title": str(data.get("title", "")).strip()[:160],
        "description": str(data.get("description", "")).strip()[:500],
        "placement": placement, "html": sanitize_design_html(html),
        "css": css, "javascript": javascript,
        "lottie_json": json.dumps(lottie_animation, separators=(",", ":"), ensure_ascii=True) if lottie_animation else "",
        "product_ids": normalized_ids,
        "template_key": template_key,
        "motion_preset": motion, "motion_config": motion_config,
        "starts_at": starts_at, "expires_at": expires_at,
    }


def _parse_datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Enter a valid schedule date and time.") from exc
    if parsed.tzinfo:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def active_designs():
    if not schema_ready():
        return {"storefront_banner": [], "storefront_section": []}
    now = datetime.utcnow()
    try:
        designs = StorefrontDesign.query.filter_by(status="published", is_enabled=True).filter(
            StorefrontDesign.approved_by_id.is_not(None),
            (StorefrontDesign.starts_at.is_(None)) | (StorefrontDesign.starts_at <= now),
            (StorefrontDesign.expires_at.is_(None)) | (StorefrontDesign.expires_at > now),
        ).order_by(StorefrontDesign.id).limit(12).all()
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.error("Design Studio storefront placements are hidden because the query failed (%s).", type(exc).__name__)
        return {"storefront_banner": [], "storefront_section": []}
    return {
        "storefront_banner": [design for design in designs if design.placement == "storefront_banner"],
        "storefront_section": [design for design in designs if design.placement == "storefront_section"],
    }


def product_api_data(design):
    try:
        ids = json.loads(design.product_ids or "[]")
    except (TypeError, json.JSONDecodeError):
        current_app.logger.error("Design Studio design %s has invalid linked product data; the links were omitted.", design.id)
        return []
    if not isinstance(ids, list) or any(not isinstance(item, int) or item < 1 for item in ids):
        current_app.logger.error("Design Studio design %s has invalid linked product IDs; the links were omitted.", design.id)
        return []
    if not ids:
        return []
    try:
        products = Product.query.filter(Product.id.in_(ids), Product.active.is_(True)).all()
        result = []
        for product in products:
            variants = ProductVariant.query.filter_by(product_id=product.id, active=True).all()
            result.append({
                "id": product.id,
                "url": f"/product/{product.slug}",
                "variant_ids": [variant.id for variant in variants],
            })
        return result
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.error("Design Studio product links are temporarily unavailable (%s).", type(exc).__name__)
        return []
