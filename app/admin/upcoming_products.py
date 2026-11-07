from pathlib import Path
from urllib.parse import unquote, urlparse

from flask import abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import login_required
from app.extensions import db
from app.models import UpcomingProduct
from app.services.product_images import ImageStorageError
from app.services.upcoming_products import upcoming_product_schema_ready
from app.services.showcase_media import inspect_media_upload, store_showcase_upload


CARD_SIZES = {
    "compact": "Compact",
    "standard": "Standard",
    "wide": "Wide feature",
    "tall": "Tall feature",
    "featured": "Large feature",
}
MAX_UPLOAD_BYTES = 3_500_000


def _validate_image_url(value):
    value = (value or "").strip()
    if not value:
        return ""
    parsed = urlparse(value)
    if value.startswith("/") and not value.startswith("//"):
        if not value.startswith("/static/"):
            raise ValueError("Local image links must start with /static/.")
        static_root = Path(current_app.static_folder).resolve()
        file_path = (static_root / unquote(parsed.path[len("/static/"):])).resolve()
        if not file_path.is_relative_to(static_root) or not file_path.is_file():
            raise ValueError("Choose an existing image from the static media library.")
        if file_path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            raise ValueError("Use a JPEG, PNG, WebP, or GIF image.")
        return value
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Use an http or https image URL.")
    image_path = parsed.path.lower()
    cloudinary = (parsed.hostname == "cloudinary.com" or parsed.hostname.endswith(".cloudinary.com")) and "/image/upload/" in image_path
    if Path(image_path).suffix not in {".jpg", ".jpeg", ".png", ".webp", ".gif"} and not cloudinary:
        raise ValueError("Use a direct JPEG, PNG, WebP, or GIF URL, or a Cloudinary image URL.")
    return value


def _form_payload(item=None):
    name = request.form.get("name", "").strip()
    category_label = request.form.get("category_label", "").strip()
    description = request.form.get("description", "").strip()
    image_alt = request.form.get("image_alt", "").strip()
    card_size = request.form.get("card_size", "tall").strip()
    try:
        display_order = max(0, int(request.form.get("display_order", item.display_order if item else 0)))
    except (TypeError, ValueError):
        raise ValueError("Display position must be a whole number.")
    if not name or len(name) > 140:
        raise ValueError("Enter a title of up to 140 characters.")
    if len(category_label) > 80 or len(description) > 2000 or len(image_alt) > 255:
        raise ValueError("Category, description, or image description is too long.")
    if card_size not in CARD_SIZES:
        raise ValueError("Choose a valid card size.")
    image_url = _validate_image_url(request.form.get("image_url", ""))
    if len(image_url) > 1000:
        raise ValueError("Keep the image URL under 1000 characters.")
    uploaded = request.files.get("image_file")
    if uploaded and uploaded.filename:
        data = uploaded.stream.read(MAX_UPLOAD_BYTES + 1)
        info = inspect_media_upload(uploaded.filename, data)
        if info["type"] not in {"image", "gif"}:
            raise ValueError("Upcoming product cards use image files. Choose an image, not a video.")
        stored = store_showcase_upload(uploaded.filename, data, info)
        image_url = stored["url"]
    return {
        "name": name, "category_label": category_label, "description": description,
        "image_url": image_url, "image_alt": image_alt,
        "card_size": card_size, "display_order": display_order,
        "is_enabled": request.form.get("is_enabled") == "1",
    }


def _render_form(item=None, form_data=None):
    return render_template(
        "admin/upcoming_product_form.html", item=item, card_sizes=CARD_SIZES,
        form_data=form_data or {},
    )


def register_upcoming_product_routes(admin_bp):
    @admin_bp.route("/upcoming-products")
    @login_required
    def upcoming_products():
        if not upcoming_product_schema_ready():
            return render_template("admin/upcoming_products.html", items=[], schema_ready=False)
        items = UpcomingProduct.query.order_by(UpcomingProduct.display_order, UpcomingProduct.id).all()
        return render_template("admin/upcoming_products.html", items=items, schema_ready=True)

    @admin_bp.route("/upcoming-products/new", methods=["GET", "POST"])
    @login_required
    def upcoming_product_new():
        if not upcoming_product_schema_ready():
            flash("Upcoming product storage is unavailable. Automatic setup will retry when the web app restarts.", "error")
            return redirect(url_for("admin.upcoming_products"))
        if request.method == "POST":
            try:
                payload = _form_payload()
                item = UpcomingProduct(**payload)
                db.session.add(item)
                db.session.commit()
                flash("Upcoming product card added.", "success")
                return redirect(url_for("admin.upcoming_products"))
            except ValueError as exc:
                db.session.rollback()
                flash(str(exc), "error")
            except Exception as exc:
                db.session.rollback()
                if isinstance(exc, ImageStorageError):
                    flash(str(exc), "error")
                else:
                    current_app.logger.exception("Upcoming product creation failed")
                    flash("The upcoming product could not be saved. Check the image storage settings and try again.", "error")
        return _render_form(form_data=request.form if request.method == "POST" else None)

    @admin_bp.route("/upcoming-products/<int:item_id>/edit", methods=["GET", "POST"])
    @login_required
    def upcoming_product_edit(item_id):
        if not upcoming_product_schema_ready():
            flash("Upcoming product storage is unavailable. Automatic setup will retry when the web app restarts.", "error")
            return redirect(url_for("admin.upcoming_products"))
        item = UpcomingProduct.query.get_or_404(item_id)
        if request.method == "POST":
            try:
                payload = _form_payload(item)
                for key, value in payload.items():
                    setattr(item, key, value)
                db.session.commit()
                flash("Upcoming product card updated.", "success")
                return redirect(url_for("admin.upcoming_products"))
            except ValueError as exc:
                db.session.rollback()
                flash(str(exc), "error")
                item = UpcomingProduct.query.get_or_404(item_id)
            except Exception as exc:
                db.session.rollback()
                if isinstance(exc, ImageStorageError):
                    flash(str(exc), "error")
                else:
                    current_app.logger.exception("Upcoming product update failed")
                    flash("The upcoming product could not be updated. Check the image storage settings and try again.", "error")
        return _render_form(item=item, form_data=request.form if request.method == "POST" else None)

    @admin_bp.route("/upcoming-products/<int:item_id>/action", methods=["POST"])
    @login_required
    def upcoming_product_action(item_id):
        if not upcoming_product_schema_ready():
            flash("Upcoming product storage is unavailable. Automatic setup will retry when the web app restarts.", "error")
            return redirect(url_for("admin.upcoming_products"))
        item = UpcomingProduct.query.get_or_404(item_id)
        action = request.form.get("action", "")
        if action == "toggle":
            item.is_enabled = not item.is_enabled
        elif action in {"up", "down"}:
            siblings = UpcomingProduct.query.order_by(UpcomingProduct.display_order, UpcomingProduct.id).all()
            position = siblings.index(item)
            target = position - 1 if action == "up" else position + 1
            if 0 <= target < len(siblings):
                siblings[position], siblings[target] = siblings[target], siblings[position]
                for order, sibling in enumerate(siblings):
                    sibling.display_order = order
        elif action == "delete":
            db.session.delete(item)
        else:
            abort(400)
        db.session.commit()
        flash("Upcoming product card deleted." if action == "delete" else "Upcoming product cards updated.", "success")
        return redirect(url_for("admin.upcoming_products"))
