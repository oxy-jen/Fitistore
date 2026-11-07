import json
import re
from datetime import datetime
from pathlib import Path
from secrets import token_hex
from urllib.parse import parse_qs, unquote, urlparse

from flask import abort, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.orm import selectinload

from app.extensions import db
from app.models import Category, MediaShowcase, MediaShowcaseItem, Product
from app.services.media_showcases import CARD_ASPECT_RATIOS, DENSITIES, LAYOUTS, SHOWCASE_PLACEMENTS, showcase_schema_ready
from app.services.showcase_media import existing_media_assets, inspect_media_upload, store_showcase_upload


VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
VIMEO_ID = re.compile(r"^\d{5,12}$")


def _video_embed_url(value):
    parsed = urlparse((value or "").strip())
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = [part for part in parsed.path.split("/") if part]
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        return ""
    if host in {"youtube.com", "m.youtube.com", "youtube-nocookie.com"}:
        video_id = ""
        if parsed.path == "/watch":
            video_id = (parse_qs(parsed.query).get("v") or [""])[0]
        elif len(path) >= 2 and path[0] in {"embed", "shorts", "live"}:
            video_id = path[1]
        if VIDEO_ID.fullmatch(video_id):
            return f"https://www.youtube-nocookie.com/embed/{video_id}"
    if host == "youtu.be" and path and VIDEO_ID.fullmatch(path[0]):
        return f"https://www.youtube-nocookie.com/embed/{path[0]}"
    if host in {"vimeo.com", "player.vimeo.com"}:
        video_id = path[1] if host == "player.vimeo.com" and len(path) >= 2 and path[0] == "video" else (path[0] if path else "")
        if VIMEO_ID.fullmatch(video_id):
            privacy_hash = (parse_qs(parsed.query).get("h") or [""])[0]
            suffix = f"?h={privacy_hash}" if re.fullmatch(r"[A-Za-z0-9]{6,64}", privacy_hash) else ""
            return f"https://player.vimeo.com/video/{video_id}{suffix}"
    return ""


def _safe_color(value, allow_transparent=False):
    value = (value or "").strip().lower()
    if allow_transparent and value == "transparent":
        return value
    if re.fullmatch(r"#[0-9a-f]{3}(?:[0-9a-f]{3})?", value):
        return value
    return ""


def _color_contrast(first, second):
    def luminance(value):
        if value == "transparent":
            value = "#ffffff"
        if len(value) == 4:
            value = "#" + "".join(char * 2 for char in value[1:])
        channels = [int(value[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        channels = [channel / 12.92 if channel <= .04045 else ((channel + .055) / 1.055) ** 2.4 for channel in channels]
        return .2126 * channels[0] + .7152 * channels[1] + .0722 * channels[2]
    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + .05) / (low + .05)


def _safe_url(value, media_type=None, required=False):
    value = (value or "").strip()
    if not value:
        if required:
            raise ValueError("Enter a media URL for this item.")
        return ""
    parsed = urlparse(value)
    if value.startswith("/") and not value.startswith("//"):
        if media_type is None:
            return value
        if not value.startswith("/static/"):
            raise ValueError("Local media must come from the app's /static/ directory.")
        static_root = Path(current_app.static_folder).resolve()
        relative_path = unquote(parsed.path[len("/static/"):])
        local_file = (static_root / relative_path).resolve()
        if not local_file.is_relative_to(static_root) or not local_file.is_file():
            raise ValueError("Choose an existing media file inside the app's static directory.")
        local_ext = local_file.suffix.lower()
        local_checks = {"video": {".mp4", ".webm", ".mov", ".m4v"}, "gif": {".gif"}, "image": {".jpg", ".jpeg", ".png", ".webp"}}
        if media_type in local_checks and local_ext not in local_checks[media_type]:
            raise ValueError(f"The local file does not have a supported {media_type} extension.")
        return value
    if parsed.scheme not in {"https", "http"} or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("Use a valid http or https URL. Script and data URLs are not allowed.")
    path = parsed.path.lower()
    ext = Path(path).suffix
    checks = {"video": (".mp4", ".webm", ".mov", ".m4v"), "gif": (".gif",), "image": (".jpg", ".jpeg", ".png", ".webp")}
    host = (parsed.hostname or "").lower()
    cloudinary_host = host == "cloudinary.com" or host.endswith(".cloudinary.com")
    cloudinary_resource = (media_type == "video" and "/video/upload/" in path) or (media_type in {"gif", "image"} and "/image/upload/" in path)
    if media_type in checks and ext not in checks[media_type] and not (cloudinary_host and cloudinary_resource):
        raise ValueError(f"The URL does not look like a supported {media_type} file. Use a direct media URL.")
    return value


def _parse_date(value):
    value = (value or "").strip()
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value)
        return result.replace(tzinfo=None)
    except ValueError:
        raise ValueError("Enter a valid schedule date and time.")


def _slug(value):
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:140] or "showcase"


def _save_items(showcase, raw):
    try:
        values = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        raise ValueError("Media items must be valid JSON. See the example below the editor.")
    if not isinstance(values, list) or len(values) > 30:
        raise ValueError("Provide a list of up to 30 media items.")
    created_items = []
    for idx, data in enumerate(values):
        if not isinstance(data, dict) or data.get("type") not in {"video", "gif", "image", "link"}:
            raise ValueError(f"Media item {idx + 1} must have type video, gif, image, or link.")
        kind = data["type"]
        raw_url = data.get("url", "")
        embed_url = _video_embed_url(raw_url) if kind == "video" else ""
        if kind == "video" and data.get("source") == "video_embed" and not embed_url:
            raise ValueError(f"Media item {idx + 1} needs a supported YouTube or Vimeo link.")
        item_url = embed_url or _safe_url(raw_url, kind, required=kind != "link")
        poster = _safe_url(data.get("poster"), "image") if data.get("poster") else ""
        destination = _safe_url(data.get("destination")) if data.get("destination") else ""
        product_id = data.get("product_id")
        product = None
        if product_id not in (None, ""):
            try:
                product = Product.query.filter_by(id=int(product_id)).first()
            except (TypeError, ValueError):
                product = None
            if not product:
                raise ValueError(f"Media item {idx + 1} refers to an unknown product.")
            destination = url_for("public.product_detail", slug=product.slug)
        if kind == "link" and not destination:
            raise ValueError(f"Media item {idx + 1} needs a destination URL or linked product.")
        title = str(data.get("title", "")).strip()[:160]
        if not title:
            raise ValueError(f"Media item {idx + 1} needs a title.")
        presentation = str(data.get("presentation", "original")).strip().lower()
        if presentation not in {"original", "contain", "cover"}:
            raise ValueError(f"Media item {idx + 1} has an unsupported presentation mode.")
        aspect_ratio = str(data.get("aspect_ratio", "")).strip()
        if aspect_ratio:
            ratio_match = re.fullmatch(r"(\d{1,4}(?:\.\d{1,3})?):(\d{1,4}(?:\.\d{1,3})?)", aspect_ratio)
            if not ratio_match or any(float(part) <= 0 or float(part) > 1000 for part in ratio_match.groups()):
                raise ValueError(f"Media item {idx + 1} aspect ratio must look like 9:16, 16:9, or 1.85:1.")
        source_type = "video_embed" if embed_url else str(data.get("source", "external_url"))
        if source_type not in {"library", "upload", "cloudinary", "external_url", "video_embed"}:
            source_type = "external_url"
        if not embed_url:
            item_host = (urlparse(item_url).hostname or "").lower()
            if item_host == "res.cloudinary.com" or item_host.endswith(".cloudinary.com"):
                source_type = "cloudinary"
            elif item_url.startswith("/static/") and source_type not in {"upload", "library"}:
                source_type = "library"
        if embed_url and "youtube-nocookie.com/embed/" in embed_url and not poster:
            video_id = embed_url.rsplit("/", 1)[-1]
            poster = f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"

        def optional_integer(key, maximum=2_000_000_000):
            value = data.get(key)
            if value in (None, ""):
                return None
            try:
                number = int(value)
            except (ValueError, TypeError):
                return None
            return number if 0 <= number <= maximum else None

        try:
            duration = float(data.get("duration")) if data.get("duration") not in (None, "") else None
        except (ValueError, TypeError):
            duration = None
        if duration is not None and (duration < 0 or duration > 1_000_000):
            duration = None

        item = MediaShowcaseItem(
            media_type=kind, title=title, description=str(data.get("description", ""))[:2000],
            source_type=source_type, source_filename=str(data.get("filename", ""))[:255],
            file_size_bytes=optional_integer("size"), media_width=optional_integer("width", 100_000),
            media_height=optional_integer("height", 100_000), duration_seconds=duration,
            media_url=item_url, poster_url=poster, thumbnail_url=poster,
            destination_url=destination, linked_product=product,
            cta_label=str(data.get("cta", ""))[:60], alt_text=str(data.get("alt", title))[:255],
            display_order=idx, is_active=data.get("active", True) is not False,
            autoplay=data.get("autoplay", True) is not False, muted=data.get("muted", True) is not False,
            loop=data.get("loop", True) is not False, show_controls=bool(data.get("controls", False)),
            presentation_mode=presentation, aspect_ratio=aspect_ratio,
        )
        created_items.append(item)
    return created_items


def _form(showcase=None):
    if request.method != "POST":
        items_json = "[]"
        if showcase:
            items_json = json.dumps([{
                "type": item.media_type, "title": item.title, "description": item.description,
                "url": item.media_url, "poster": item.poster_url, "destination": item.destination_url,
                "product_id": item.linked_product_id, "cta": item.cta_label, "alt": item.alt_text,
                "active": item.is_active, "autoplay": item.autoplay, "muted": item.muted,
                "loop": item.loop, "controls": item.show_controls,
                "presentation": item.presentation_mode, "aspect_ratio": item.aspect_ratio,
                "source": item.source_type, "filename": item.source_filename,
                "size": item.file_size_bytes, "width": item.media_width, "height": item.media_height,
                "duration": item.duration_seconds,
            } for item in showcase.items], indent=2)
        return items_json, None
    page = request.form.get("page", "")
    placement = request.form.get("placement", "")
    if page not in SHOWCASE_PLACEMENTS or placement not in SHOWCASE_PLACEMENTS[page]:
        return None, "Choose a currently supported page placement."
    name = request.form.get("name", "").strip()
    if not name or len(name) > 120:
        return None, "Enter a showcase name up to 120 characters."
    layout = request.form.get("layout", "carousel")
    if layout not in LAYOUTS:
        return None, "Choose a supported layout."
    density = request.form.get("density", "compact")
    aspect_ratio = request.form.get("card_aspect_ratio", "4:3")
    if density not in DENSITIES or aspect_ratio not in CARD_ASPECT_RATIOS:
        return None, "Choose a supported density and card aspect ratio."

    def bounded_number(field, default, minimum, maximum, optional=False):
        raw = (request.form.get(field) or "").strip()
        if optional and not raw:
            return None
        try:
            result = int(raw) if raw else default
        except ValueError:
            raise ValueError(f"{field.replace('_', ' ').capitalize()} must be a whole number.")
        if result < minimum or result > maximum:
            raise ValueError(f"{field.replace('_', ' ').capitalize()} must be from {minimum} to {maximum}.")
        return result

    try:
        visible_mobile = bounded_number("visible_cards_mobile", 2, 1, 4)
        visible_tablet = bounded_number("visible_cards_tablet", 3, 1, 5)
        visible_desktop = bounded_number("visible_cards_desktop", 4, 1, 6)
        section_padding = bounded_number("section_padding_px", 0, 0, 40, optional=True)
        card_gap = bounded_number("card_gap_px", 0, 0, 40, optional=True)
    except ValueError as exc:
        return None, str(exc)
    starts = _parse_date(request.form.get("starts_at"))
    expires = _parse_date(request.form.get("expires_at"))
    if starts and expires and expires <= starts:
        return None, "The expiration must be later than the start date."
    color_defaults = {
        "background_color": "#ffffff", "heading_color": "#252525", "description_color": "#666666",
        "accent_color": "#e52335", "card_background_color": "#ffffff", "border_color": "#e8e8eb",
        "button_background_color": "#e52335", "button_text_color": "#ffffff",
    }
    colors = {}
    for field, default in color_defaults.items():
        allow_transparent = field in {"background_color", "card_background_color", "border_color"}
        raw = request.form.get(field, default if field != "background_color" else "")
        selected = _safe_color(raw or default, allow_transparent=allow_transparent)
        if not selected:
            return None, f"Choose a valid colour for {field.replace('_', ' ')}."
        colors[field] = selected
    for text_field, background_field, label in (
        ("heading_color", "background_color", "Heading"),
        ("heading_color", "card_background_color", "Card heading"),
        ("description_color", "background_color", "Description"),
        ("description_color", "card_background_color", "Card description"),
        ("button_text_color", "button_background_color", "Button text"),
    ):
        if _color_contrast(colors[text_field], colors[background_field]) < 4.5:
            return None, f"Improve colour contrast for {label}; aim for a ratio of at least 4.5:1."
    try:
        items = _save_items(None, request.form.get("items_json", "[]"))
    except ValueError as exc:
        return None, str(exc)
    publish_action = request.form.get("publish_action", "keep")
    published = publish_action == "publish" or (publish_action == "keep" and bool(request.form.get("is_published")))
    if published and not any(item.is_active for item in items):
        return None, "Add at least one active media item before publishing."
    if layout == "single" and len(items) > 1:
        return None, "The single media card layout accepts one item."
    return {
        "name": name, "title": request.form.get("title", "").strip()[:160],
        "subtitle": request.form.get("subtitle", "").strip()[:200],
        "description": request.form.get("description", "").strip()[:3000],
        "page_identifier": page, "placement_identifier": placement, "layout": layout,
        "density": density, "card_aspect_ratio": aspect_ratio,
        "visible_cards_mobile": visible_mobile, "visible_cards_tablet": visible_tablet,
        "visible_cards_desktop": visible_desktop, "section_padding_px": section_padding,
        "card_gap_px": card_gap,
        "is_published": published,
        "is_enabled": bool(request.form.get("is_enabled")), "starts_at": starts,
        "expires_at": expires, **colors,
        "items": items,
    }, None


def register_media_showcase_routes(admin_bp):
    def migration_required():
        flash("Media Showcase database setup is pending or failed. Public pages remain available; setup retries at the next application startup. Check deployment logs if this message continues.", "warning")
        return redirect(url_for("admin.media_showcases"))

    @admin_bp.route("/media-showcases/library")
    @login_required
    def media_showcase_library():
        return jsonify(existing_media_assets())

    @admin_bp.route("/media-showcases/upload", methods=["POST"])
    @login_required
    def media_showcase_upload():
        uploaded = request.files.get("media")
        if not uploaded or not uploaded.filename:
            return jsonify({"error": "Choose a media file to upload."}), 400
        filename = Path(uploaded.filename).name
        data = uploaded.stream.read(4 * 1024 * 1024 + 1)
        try:
            info = inspect_media_upload(filename, data)
            stored = store_showcase_upload(filename, data, info)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            from app.services.product_images import ImageStorageError
            if isinstance(exc, ImageStorageError):
                current_app.logger.warning("Showcase media upload unavailable: %s", str(exc))
                return jsonify({"error": str(exc)}), 503
            current_app.logger.exception("Showcase media upload failed")
            return jsonify({"error": "The upload could not be completed. Check the configured media storage and try again."}), 500
        return jsonify({**stored, **info, "type": info["type"], "ok": True})

    @admin_bp.route("/media-showcases")
    @login_required
    def media_showcases():
        if not showcase_schema_ready():
            flash("Media Showcase database setup is pending or failed. Public pages remain available; setup retries at the next application startup. Check deployment logs if this message continues.", "warning")
            return render_template("admin/media_showcases.html", showcases=[], pages=SHOWCASE_PLACEMENTS, now=datetime.utcnow(), statuses=("published", "draft", "scheduled", "expired", "disabled"))
        query = MediaShowcase.query.options(selectinload(MediaShowcase.items)).order_by(MediaShowcase.page_identifier, MediaShowcase.display_order, MediaShowcase.id)
        page = request.args.get("page", "")
        status = request.args.get("status", "")
        media_type = request.args.get("media_type", "")
        search = request.args.get("q", "").strip()
        if page in SHOWCASE_PLACEMENTS:
            query = query.filter_by(page_identifier=page)
        if status == "published": query = query.filter_by(is_published=True, is_enabled=True)
        elif status == "draft": query = query.filter_by(is_published=False)
        elif status == "disabled": query = query.filter_by(is_enabled=False)
        elif status == "scheduled": query = query.filter(MediaShowcase.starts_at > datetime.utcnow())
        elif status == "expired": query = query.filter(MediaShowcase.expires_at <= datetime.utcnow())
        if media_type in {"video", "gif", "image", "link"}:
            query = query.filter(MediaShowcase.items.any(MediaShowcaseItem.media_type == media_type))
        if search: query = query.filter(MediaShowcase.name.ilike(f"%{search}%"))
        return render_template("admin/media_showcases.html", showcases=query.all(), pages=SHOWCASE_PLACEMENTS, now=datetime.utcnow(), statuses=("published", "draft", "scheduled", "expired", "disabled"))

    @admin_bp.route("/media-showcases/new", methods=["GET", "POST"])
    @login_required
    def media_showcase_new():
        if not showcase_schema_ready():
            return migration_required()
        items_json = "[]"
        if request.method == "POST":
            payload, error = _form()
            if error:
                flash(error, "error")
                payload = request.form
                items_json = request.form.get("items_json", "[]")
            else:
                slug = _slug(payload["name"])
                base = slug
                n = 2
                while MediaShowcase.query.filter_by(slug=slug).first(): slug = f"{base}-{n}"; n += 1
                next_order = db.session.query(db.func.max(MediaShowcase.display_order)).filter_by(page_identifier=payload["page_identifier"], placement_identifier=payload["placement_identifier"]).scalar()
                showcase = MediaShowcase(slug=slug, created_by_id=current_user.id, display_order=(next_order if next_order is not None else -1) + 1, **payload)
                db.session.add(showcase); db.session.commit()
                flash("Media showcase created.", "success")
                return redirect(url_for("admin.media_showcases"))
        else:
            payload = None
        products = Product.query.order_by(Product.name).all()
        products_data = [{"id": product.id, "name": product.name} for product in products]
        categories_data = [{"slug": category.slug, "name": category.name} for category in Category.query.order_by(Category.name)]
        return render_template("admin/media_showcase_form.html", showcase=None, pages=SHOWCASE_PLACEMENTS, layouts=LAYOUTS, densities=DENSITIES, aspect_ratios=CARD_ASPECT_RATIOS, items_json=items_json, form_data=payload or {}, products_data=products_data, categories_data=categories_data)

    @admin_bp.route("/media-showcases/<int:showcase_id>/edit", methods=["GET", "POST"])
    @login_required
    def media_showcase_edit(showcase_id):
        if not showcase_schema_ready():
            return migration_required()
        showcase = MediaShowcase.query.options(selectinload(MediaShowcase.items)).get_or_404(showcase_id)
        if request.method == "POST":
            payload, error = _form(showcase)
            if error:
                flash(error, "error")
                items_json = request.form.get("items_json", "[]")
                form_data = request.form
            else:
                items = payload.pop("items")
                for key, value in payload.items(): setattr(showcase, key, value)
                showcase.items.clear()
                showcase.items.extend(items)
                db.session.commit()
                flash("Media showcase updated.", "success")
                return redirect(url_for("admin.media_showcases"))
        else:
            items_json, _ = _form(showcase); form_data = None
        products = Product.query.order_by(Product.name).all()
        products_data = [{"id": product.id, "name": product.name} for product in products]
        categories_data = [{"slug": category.slug, "name": category.name} for category in Category.query.order_by(Category.name)]
        return render_template("admin/media_showcase_form.html", showcase=showcase, pages=SHOWCASE_PLACEMENTS, layouts=LAYOUTS, densities=DENSITIES, aspect_ratios=CARD_ASPECT_RATIOS, items_json=items_json, form_data=form_data or {}, products_data=products_data, categories_data=categories_data)

    @admin_bp.route("/media-showcases/<int:showcase_id>/preview")
    @login_required
    def media_showcase_preview(showcase_id):
        if not showcase_schema_ready():
            return migration_required()
        showcase = MediaShowcase.query.options(selectinload(MediaShowcase.items).selectinload(MediaShowcaseItem.linked_product)).get_or_404(showcase_id)
        return render_template("admin/media_showcase_preview.html", showcase=showcase)

    @admin_bp.route("/media-showcases/<int:showcase_id>/duplicate", methods=["POST"])
    @login_required
    def media_showcase_duplicate(showcase_id):
        if not showcase_schema_ready():
            return migration_required()
        source = MediaShowcase.query.options(selectinload(MediaShowcase.items)).get_or_404(showcase_id)
        copy = MediaShowcase(name=f"{source.name} copy"[:120], title=source.title, subtitle=source.subtitle, description=source.description,
            slug=_slug(source.slug + "-copy-" + token_hex(4)), page_identifier=source.page_identifier,
            placement_identifier=source.placement_identifier, layout=source.layout, display_order=source.display_order + 1,
            density=source.density, card_aspect_ratio=source.card_aspect_ratio,
            visible_cards_mobile=source.visible_cards_mobile, visible_cards_tablet=source.visible_cards_tablet,
            visible_cards_desktop=source.visible_cards_desktop, section_padding_px=source.section_padding_px,
            card_gap_px=source.card_gap_px, heading_color=source.heading_color,
            description_color=source.description_color, accent_color=source.accent_color,
            card_background_color=source.card_background_color, border_color=source.border_color,
            button_background_color=source.button_background_color, button_text_color=source.button_text_color,
            is_published=False, is_enabled=True, starts_at=None, expires_at=None, background_color=source.background_color,
            created_by_id=current_user.id)
        for item in source.items:
            copy.items.append(MediaShowcaseItem(media_type=item.media_type, title=item.title, description=item.description,
                media_url=item.media_url, poster_url=item.poster_url, thumbnail_url=item.thumbnail_url, destination_url=item.destination_url,
                linked_product_id=item.linked_product_id, cta_label=item.cta_label, alt_text=item.alt_text, display_order=item.display_order,
                is_active=item.is_active, autoplay=item.autoplay, muted=item.muted, loop=item.loop, show_controls=item.show_controls,
                presentation_mode=item.presentation_mode, aspect_ratio=item.aspect_ratio, source_type=item.source_type,
                source_filename=item.source_filename, file_size_bytes=item.file_size_bytes, media_width=item.media_width,
                media_height=item.media_height, duration_seconds=item.duration_seconds))
        db.session.add(copy); db.session.commit(); flash("Draft copy created.", "success")
        return redirect(url_for("admin.media_showcase_edit", showcase_id=copy.id))

    @admin_bp.route("/media-showcases/<int:showcase_id>/action", methods=["POST"])
    @login_required
    def media_showcase_action(showcase_id):
        if not showcase_schema_ready():
            return migration_required()
        showcase = MediaShowcase.query.get_or_404(showcase_id)
        action = request.form.get("action")
        if action == "delete":
            db.session.delete(showcase); flash("Showcase and item records deleted. Media files, Cloudinary assets, and products were not removed.", "success")
        elif action in {"publish", "unpublish"}:
            if action == "publish" and not any(item.is_active for item in showcase.items):
                flash("Add at least one active media item before publishing.", "error")
                return redirect(url_for("admin.media_showcases"))
            showcase.is_published = action == "publish"
            if action == "publish": showcase.is_enabled = True
            flash("Showcase published." if showcase.is_published else "Showcase unpublished and saved as a draft.", "success")
        elif action == "toggle":
            showcase.is_enabled = not showcase.is_enabled; flash("Showcase availability updated.", "success")
        elif action in {"up", "down"}:
            siblings = MediaShowcase.query.filter_by(page_identifier=showcase.page_identifier, placement_identifier=showcase.placement_identifier).order_by(MediaShowcase.display_order, MediaShowcase.id).all()
            idx = siblings.index(showcase); other = idx - 1 if action == "up" else idx + 1
            if 0 <= other < len(siblings):
                siblings[idx], siblings[other] = siblings[other], siblings[idx]
                for order, sibling in enumerate(siblings):
                    sibling.display_order = order
        else:
            abort(400)
        db.session.commit()
        return redirect(url_for("admin.media_showcases"))


