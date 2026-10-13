import ipaddress
import csv
import io
from datetime import datetime, timedelta
import re
import socket
import urllib.request
from pathlib import Path
from urllib.parse import urlparse
from flask import Blueprint, Response, current_app, flash, redirect, render_template, request, send_from_directory, url_for
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import func, or_
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename
from app.extensions import db
from app.models import AdminUser, Category, Expense, FinancialAuditEvent, InventoryMovement, LoyaltyTransaction, Order, PaymentAttempt, PaymentTransaction, Product, ProductEnquiry, ProductImage, ProductSuggestion, ProductVariant, RefundRequest, RestockAlert, Review, ShopSettings
from app.services.formatting import parse_money
from app.services.inventory import InventoryError, adjust_stock, adjust_variant_stock, low_stock_products
from app.services.orders import FULFILMENT_STATUSES, PAYMENT_STATUSES, award_loyalty_for_order, refundable_cents, sales_summary, update_order_status
from app.services.product_images import ImageStorageError, cloudinary_configured, cloudinary_public_id, delete_cloudinary_asset, upload_image_bytes

admin_bp = Blueprint("admin", __name__, template_folder="../templates/admin")

IMAGE_SIGNATURES = {
    "jpg": (b"\xff\xd8\xff",),
    "jpeg": (b"\xff\xd8\xff",),
    "png": (b"\x89PNG\r\n\x1a\n",),
    "webp": (b"RIFF",),
    "gif": (b"GIF87a", b"GIF89a"),
    "svg": (b"<svg", b"<?xml"),
}

def parse_variant_rows(raw_text, product):
    rows = []
    seen = set()
    for line in (raw_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split("|")]
        while len(parts) < 8:
            parts.append("")
        size, color, style, sku, price, cost, stock, active = parts[:8]
        key = (size.lower(), color.lower(), style.lower(), sku.lower())
        if key in seen:
            raise ValueError(f"Duplicate variant row: {line}")
        seen.add(key)
        if sku:
            duplicate_sku = ProductVariant.query.filter(ProductVariant.sku == sku)
            if product.id:
                duplicate_sku = duplicate_sku.filter(ProductVariant.product_id != product.id)
            if duplicate_sku.first():
                raise ValueError(f"Variant SKU {sku} already belongs to another product.")
        rows.append({
            "size": size[:50],
            "color": color[:50],
            "style": style[:80],
            "sku": sku[:80],
            "price_cents": parse_money(price) if price else None,
            "unit_cost_cents": parse_money(cost) if cost else None,
            "stock_quantity": max(0, int(stock or 0)),
            "active": active.lower() not in {"no", "false", "0", "inactive"},
        })
    return rows

def sync_product_variants(product, raw_text):
    rows = parse_variant_rows(raw_text, product)
    existing = {variant.id: variant for variant in product.variants}
    keep_ids = set()
    for index, row in enumerate(rows):
        variant = None
        for candidate in product.variants:
            if (candidate.size or "") == row["size"] and (candidate.color or "") == row["color"] and (candidate.style or "") == row["style"] and (candidate.sku or "") == row["sku"]:
                variant = candidate
                break
        if not variant:
            variant = ProductVariant(product=product)
            db.session.add(variant)
        variant.size = row["size"]
        variant.color = row["color"]
        variant.style = row["style"]
        variant.sku = row["sku"]
        variant.price_cents = row["price_cents"]
        variant.unit_cost_cents = row["unit_cost_cents"]
        variant.stock_quantity = row["stock_quantity"]
        variant.low_stock_threshold = product.low_stock_threshold
        variant.active = row["active"]
        variant.sort_order = index
        keep_ids.add(variant.id)
    for variant in list(product.variants):
        if variant.id and variant.id not in keep_ids and not variant.reserved_quantity:
            db.session.delete(variant)
        elif variant.id and variant.id not in keep_ids:
            variant.active = False

def slugify(value):
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value.lower()).strip("-")
    return value or "item"

def unique_slug(name, product_id=None):
    base = slugify(name)
    slug = base
    i = 2
    while True:
        query = Product.query.filter_by(slug=slug)
        if product_id:
            query = query.filter(Product.id != product_id)
        if not query.first():
            return slug
        slug = f"{base}-{i}"
        i += 1

def image_extension(filename):
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

def looks_like_image(data, ext):
    signatures = IMAGE_SIGNATURES.get(ext, ())
    if ext == "webp":
        return len(data) > 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    probe = data.lstrip()[:16].lower() if ext == "svg" else data[:16]
    return any(probe.startswith(sig.lower() if ext == "svg" else sig) for sig in signatures)

def validate_image_bytes(filename, data):
    ext = image_extension(filename)
    allowed = current_app.config["ALLOWED_IMAGE_EXTENSIONS"]
    if ext not in allowed:
        return None, "Unsupported image type. Use jpg, jpeg, png, webp, or gif."
    if not data:
        return None, "Image file is empty."
    if len(data) > current_app.config["MAX_CONTENT_LENGTH"]:
        return None, "Image is too large."
    if not looks_like_image(data, ext):
        return None, "The file contents do not match a supported image type."
    return ext, None

def is_public_image_url(url):
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    try:
        addresses = socket.getaddrinfo(parsed.hostname, None)
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return False
    except OSError:
        return False
    return True

def append_product_image(product, filename, alt_text, cloudinary_id=None):
    is_primary = not bool(product.images)
    product.images.append(ProductImage(
        filename=filename,
        storage_backend="cloudinary" if cloudinary_id else "local",
        cloudinary_public_id=cloudinary_id,
        alt_text=alt_text[:255] or f"{product.name} image",
        sort_order=len(product.images),
        is_primary=is_primary,
    ))


def append_cloudinary_image(product, original_filename, data, alt_text, prefix="products"):
    public_id = cloudinary_public_id(product, original_filename, prefix=prefix)
    upload_image_bytes(data, public_id)
    safe = secure_filename(original_filename) or "product-image"
    append_product_image(product, safe[:255], alt_text, cloudinary_id=public_id)


def save_image_from_url(url, product):
    url = (url or "").strip()
    if not url:
        return
    if not cloudinary_configured():
        flash("Cloudinary is not configured. Set the Cloudinary environment variables before importing product images.", "error")
        return
    if not is_public_image_url(url):
        flash("Online image URL must be a public http(s) image address.", "error")
        return
    parsed = urlparse(url)
    original_name = Path(parsed.path).name or "online-product.jpg"
    ext = image_extension(original_name) or "jpg"
    if ext not in current_app.config["ALLOWED_IMAGE_EXTENSIONS"]:
        flash("Online image URL must end with jpg, jpeg, png, webp, or gif.", "error")
        return
    request_obj = urllib.request.Request(url, headers={"User-Agent": "FITY-image-import/1.0"})
    try:
        with urllib.request.urlopen(request_obj, timeout=8) as response:
            content_type = response.headers.get("Content-Type", "")
            if not content_type.startswith("image/"):
                flash("Online image URL did not return an image.", "error")
                return
            data = response.read(current_app.config["MAX_CONTENT_LENGTH"] + 1)
    except OSError:
        flash("Could not download the online image. Try uploading the file instead.", "error")
        return
    ext, error = validate_image_bytes(original_name, data)
    if error:
        flash(error, "error")
        return
    try:
        append_cloudinary_image(product, original_name, data, f"{product.name} product image", prefix="imports")
    except ImageStorageError as exc:
        current_app.logger.warning("Could not import product image to Cloudinary: %s", exc)
        flash(str(exc), "error")


def save_images(files, product):
    files = [file for file in files if file and file.filename]
    if files and not cloudinary_configured():
        flash("Cloudinary is not configured. Set the Cloudinary environment variables before uploading product images.", "error")
        return
    for file in files:
        data = file.read(current_app.config["MAX_CONTENT_LENGTH"] + 1)
        file.stream.seek(0)
        ext, error = validate_image_bytes(file.filename, data)
        if error:
            flash(f"Skipped {file.filename}: {error}", "warning")
            continue
        try:
            append_cloudinary_image(product, file.filename, data, f"{product.name} image")
        except ImageStorageError as exc:
            current_app.logger.warning("Could not upload product image to Cloudinary: %s", exc)
            flash(f"Skipped {file.filename}: {exc}", "error")


def remove_image_file(filename):
    if not filename or filename.startswith("sample-"):
        return
    path = Path(current_app.config["UPLOAD_FOLDER"]) / filename
    try:
        if path.resolve().parent == Path(current_app.config["UPLOAD_FOLDER"]).resolve() and path.exists():
            path.unlink()
    except OSError:
        current_app.logger.warning("Could not delete uploaded image %s", filename)


def remove_product_image_asset(image):
    if image.cloudinary_public_id:
        delete_cloudinary_asset(image.cloudinary_public_id)
    else:
        remove_image_file(image.filename)
@admin_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("admin.dashboard"))
    if request.method == "POST":
        user = AdminUser.query.filter_by(username=request.form.get("username", "").strip()).first()
        if user and user.failed_logins >= 8:
            flash("Too many failed login attempts. Reset the password from the server.", "error")
        elif user and check_password_hash(user.password_hash, request.form.get("password", "")):
            user.failed_logins = 0
            db.session.commit()
            login_user(user)
            return redirect(url_for("admin.dashboard"))
        else:
            if user:
                user.failed_logins += 1
                db.session.commit()
            flash("Invalid username or password.", "error")
    return render_template("admin/login.html", title="Admin login")

@admin_bp.post("/logout")
@login_required
def logout():
    logout_user()
    flash("You have signed out.", "success")
    return redirect(url_for("admin.login"))

@admin_bp.route("/")
@login_required
def dashboard():
    summary = sales_summary()
    stats = {
        "products": Product.query.count(),
        "active": Product.query.filter_by(active=True).count(),
        "low_stock": low_stock_products(),
        "out_stock": len([product for product in Product.query.filter(Product.active.is_(True)).all() if product.display_stock_quantity <= 0]),
        "pending_suggestions": ProductSuggestion.query.filter_by(status="pending").count(),
        "pending_enquiries": ProductEnquiry.query.filter_by(status="pending").count(),
        "pending_alerts": RestockAlert.query.filter_by(status="pending").count(),
    }
    return render_template("admin/dashboard.html", title="Admin dashboard", products=stats["products"], orders=Order.query.count(), low_stock=stats["low_stock"], summary=summary, inventory_stats=stats, suggestions=ProductSuggestion.query.count())

@admin_bp.route("/products")
@login_required
def products():
    q = request.args.get("q", "").strip()
    category_id = request.args.get("category_id", "").strip()
    status = request.args.get("status", "").strip()
    query = Product.query.join(Category)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Product.name.ilike(like), Product.sku.ilike(like), Product.description.ilike(like)))
    if category_id:
        query = query.filter(Product.category_id == int(category_id))
    if status == "active":
        query = query.filter(Product.active.is_(True))
    elif status == "inactive":
        query = query.filter(Product.active.is_(False))
    products = query.order_by(Product.updated_at.desc()).all()
    if status == "low":
        products = [product for product in products if product.display_stock_quantity <= product.low_stock_threshold]
    elif status == "out":
        products = [product for product in products if product.display_stock_quantity <= 0]
    low_count = len(low_stock_products())
    stats = {
        "total": Product.query.count(),
        "active": Product.query.filter(Product.active.is_(True)).count(),
        "low": low_count,
        "out": len([product for product in Product.query.filter(Product.active.is_(True)).all() if product.display_stock_quantity <= 0]),
    }
    categories = Category.query.order_by(Category.name).all()
    return render_template("admin/products.html", title="Products", products=products, categories=categories, q=q, category_id=category_id, status=status, stats=stats)

@admin_bp.route("/products/new", methods=["GET", "POST"])
@admin_bp.route("/products/<int:product_id>/edit", methods=["GET", "POST"])
@login_required
def product_form(product_id=None):
    product = Product.query.get(product_id) if product_id else None
    categories = Category.query.order_by(Category.name).all()
    if request.method == "POST":
        if not product:
            product = Product(category_id=categories[0].id if categories else None)
        product.name = request.form["name"].strip()
        product.slug = unique_slug(product.name, product.id)
        product.sku = request.form.get("sku", "").strip()[:80]
        product.category_id = int(request.form["category_id"])
        product.description = request.form.get("description", "").strip()
        product.price_cents = parse_money(request.form.get("price"))
        product.previous_price_cents = parse_money(request.form.get("previous_price"))
        product.stock_quantity = max(0, int(request.form.get("stock_quantity") or 0))
        product.low_stock_threshold = max(0, int(request.form.get("low_stock_threshold") or 0))
        product.unit_cost_cents = parse_money(request.form.get("unit_cost"))
        product.size_options = request.form.get("size_options", "").strip()
        product.color_options = request.form.get("color_options", "").strip()
        product.active = bool(request.form.get("active"))
        product.featured = bool(request.form.get("featured"))
        try:
            sync_product_variants(product, request.form.get("variants_text", ""))
        except ValueError as exc:
            flash(str(exc), "error")
            return render_template("admin/product_form.html", title="Product", product=product if product.id else None, categories=categories)
        db.session.add(product)
        db.session.flush()
        save_images(request.files.getlist("images"), product)
        save_image_from_url(request.form.get("online_image_url"), product)
        if not product.images:
            db.session.rollback()
            flash("Add at least one main product image before saving.", "error")
            return render_template("admin/product_form.html", title="Product", product=product if product.id else None, categories=categories)
        if product.images and not any(image.is_primary for image in product.images):
            product.images[0].is_primary = True
        db.session.commit()
        flash("Product saved.", "success")
        return redirect(url_for("admin.products"))
    return render_template("admin/product_form.html", title="Product", product=product, categories=categories)

@admin_bp.post("/products/<int:product_id>/stock")
@login_required
def stock_adjust(product_id):
    product = Product.query.get_or_404(product_id)
    variant_id = request.form.get("variant_id")
    try:
        if variant_id:
            variant = ProductVariant.query.filter_by(id=int(variant_id), product_id=product.id).first_or_404()
            adjust_variant_stock(variant, int(request.form.get("change") or 0), request.form.get("reason", "Manual variant adjustment"), current_user)
        else:
            adjust_stock(product, int(request.form.get("change") or 0), request.form.get("reason", "Manual adjustment"), current_user)
        db.session.commit()
        flash("Stock updated.", "success")
    except (ValueError, InventoryError) as exc:
        db.session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("admin.products"))

@admin_bp.post("/products/<int:product_id>/sold-out")
@login_required
def mark_sold_out(product_id):
    product = Product.query.get_or_404(product_id)
    product.stock_quantity = 0
    for variant in product.variants:
        variant.stock_quantity = 0
        variant.active = False
    product.active = False
    db.session.add(InventoryMovement(product=product, change=0, quantity_after=0, reason="Marked sold out"))
    db.session.commit()
    flash("Product marked sold out and hidden.", "success")
    return redirect(url_for("admin.products"))

@admin_bp.post("/products/<int:product_id>/delete")
@login_required
def delete_product(product_id):
    product = Product.query.get_or_404(product_id)
    if request.form.get("confirm") != "DELETE":
        flash("Type DELETE to confirm product deletion.", "error")
        return redirect(url_for("admin.product_form", product_id=product.id))
    try:
        for image in product.images:
            remove_product_image_asset(image)
    except ImageStorageError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return redirect(url_for("admin.product_form", product_id=product.id))
    db.session.delete(product)
    db.session.commit()
    flash("Product deleted.", "success")
    return redirect(url_for("admin.products"))

@admin_bp.post("/products/<int:product_id>/images/<int:image_id>/primary")
@login_required
def set_primary_image(product_id, image_id):
    product = Product.query.get_or_404(product_id)
    image = ProductImage.query.filter_by(id=image_id, product_id=product.id).first_or_404()
    for item in product.images:
        item.is_primary = item.id == image.id
    db.session.commit()
    flash("Primary image updated.", "success")
    return redirect(url_for("admin.product_form", product_id=product.id))

@admin_bp.post("/products/<int:product_id>/images/<int:image_id>/delete")
@login_required
def delete_product_image(product_id, image_id):
    product = Product.query.get_or_404(product_id)
    image = ProductImage.query.filter_by(id=image_id, product_id=product.id).first_or_404()
    was_primary = image.is_primary
    try:
        remove_product_image_asset(image)
    except ImageStorageError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return redirect(url_for("admin.product_form", product_id=product.id))
    db.session.delete(image)
    db.session.flush()
    if was_primary and product.images:
        product.images[0].is_primary = True
    db.session.commit()
    flash("Image removed.", "success")
    return redirect(url_for("admin.product_form", product_id=product.id))

@admin_bp.route("/inventory")
@login_required
def inventory():
    movements = InventoryMovement.query.order_by(InventoryMovement.created_at.desc()).limit(100).all()
    return render_template("admin/inventory.html", title="Inventory", products=Product.query.order_by(Product.stock_quantity).all(), movements=movements)

@admin_bp.route("/orders")
@login_required
def orders():
    q = request.args.get("q", "").strip()
    payment_status = request.args.get("payment_status", "").strip()
    status = request.args.get("status", "").strip()
    query = Order.query
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Order.reference.ilike(like), Order.customer_name.ilike(like), Order.customer_phone.ilike(like)))
    if payment_status:
        query = query.filter(Order.payment_status == payment_status)
    if status:
        query = query.filter(Order.status == status)
    orders = query.order_by(Order.created_at.desc()).all()
    return render_template("admin/orders.html", title="Orders", orders=orders, fulfilment_statuses=FULFILMENT_STATUSES, payment_statuses=PAYMENT_STATUSES, q=q, status=status, payment_status=payment_status)

@admin_bp.post("/orders/<int:order_id>")
@login_required
def update_order(order_id):
    order = Order.query.get_or_404(order_id)
    try:
        changes = update_order_status(order, request.form.get("status", order.status), request.form.get("payment_status", order.payment_status), request.form.get("notes", order.notes), current_user)
        db.session.commit()
        flash("Order updated." if changes else "No status change was needed.", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("admin.orders"))
@admin_bp.route("/suggestions")
@login_required
def suggestions():
    return render_template("admin/suggestions.html", title="Mamba suggestions", suggestions=ProductSuggestion.query.order_by(ProductSuggestion.created_at.desc()).all())


@admin_bp.post("/suggestions/<int:suggestion_id>")
@login_required
def update_suggestion(suggestion_id):
    suggestion = ProductSuggestion.query.get_or_404(suggestion_id)
    suggestion.status = request.form.get("status", "pending") if request.form.get("status") in {"pending", "reviewed"} else "pending"
    db.session.commit()
    flash("Suggestion updated.", "success")
    return redirect(url_for("admin.suggestions"))


@admin_bp.route("/reviews")
@login_required
def review_admin():
    return render_template("admin/reviews.html", title="Review moderation", reviews=Review.query.order_by(Review.created_at.desc()).all())

@admin_bp.post("/reviews/<int:review_id>")
@login_required
def update_review(review_id):
    review = Review.query.get_or_404(review_id)
    review.status = request.form.get("status", "pending")
    db.session.commit()
    flash("Review updated.", "success")
    return redirect(url_for("admin.review_admin"))

@admin_bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    settings = ShopSettings.query.first() or ShopSettings()
    if not settings.id:
        db.session.add(settings)
    if request.method == "POST":
        for field in ["shop_name", "tagline", "whatsapp_number", "payment_number", "contact_email", "public_location", "collection_instructions", "delivery_area", "payment_instructions", "currency"]:
            setattr(settings, field, request.form.get(field, "").strip())
        settings.delivery_fee_cents = parse_money(request.form.get("delivery_fee"))
        settings.default_low_stock_threshold = max(0, int(request.form.get("default_low_stock_threshold") or 3))
        settings.loyalty_enabled = bool(request.form.get("loyalty_enabled"))
        settings.loyalty_points_per_100 = max(0, int(request.form.get("loyalty_points_per_100") or 0))
        settings.loyalty_min_redeem_points = max(0, int(request.form.get("loyalty_min_redeem_points") or 0))
        settings.loyalty_point_value_cents = max(0, parse_money(request.form.get("loyalty_point_value")) or 0)
        settings.referral_enabled = bool(request.form.get("referral_enabled"))
        settings.referral_reward_points = max(0, int(request.form.get("referral_reward_points") or 0))
        settings.referral_min_order_cents = max(0, parse_money(request.form.get("referral_min_order")) or 0)
        db.session.commit()
        flash("Settings saved.", "success")
        return redirect(url_for("admin.settings"))
    return render_template("admin/settings.html", title="Settings", settings=settings)

@admin_bp.route("/payment-proof/<path:filename>")
@login_required
def payment_proof(filename):
    return send_from_directory(current_app.config["PAYMENT_PROOF_FOLDER"], filename, as_attachment=False)
@admin_bp.route("/enquiries")
@login_required
def enquiries():
    status = request.args.get("status", "").strip()
    query = ProductEnquiry.query
    if status:
        query = query.filter_by(status=status)
    return render_template("admin/enquiries.html", title="Product enquiries", enquiries=query.order_by(ProductEnquiry.created_at.desc()).all(), alerts=RestockAlert.query.order_by(RestockAlert.created_at.desc()).all(), status=status)


@admin_bp.post("/enquiries/<int:enquiry_id>")
@login_required
def update_enquiry(enquiry_id):
    enquiry = ProductEnquiry.query.get_or_404(enquiry_id)
    if request.form.get("status") in {"pending", "in_progress", "resolved"}:
        enquiry.status = request.form.get("status")
    enquiry.internal_response = request.form.get("internal_response", "").strip()[:1000]
    db.session.add(FinancialAuditEvent(admin=current_user, event_type="enquiry_update", entity_type="ProductEnquiry", entity_id=enquiry.id, note=enquiry.status))
    db.session.commit()
    flash("Enquiry updated.", "success")
    return redirect(url_for("admin.enquiries"))


@admin_bp.post("/restock-alerts/<int:alert_id>")
@login_required
def update_restock_alert(alert_id):
    alert = RestockAlert.query.get_or_404(alert_id)
    if request.form.get("status") in {"pending", "queued", "notified", "cancelled"}:
        alert.status = request.form.get("status")
        if alert.status == "notified":
            alert.notified_at = datetime.utcnow()
    db.session.commit()
    flash("Restock alert updated.", "success")
    return redirect(url_for("admin.enquiries"))


def report_window():
    period = request.args.get("period", "30d")
    now = datetime.utcnow()
    if period == "7d":
        return period, now - timedelta(days=7), now
    if period == "month":
        return period, now.replace(day=1, hour=0, minute=0, second=0, microsecond=0), now
    if period == "all":
        return period, None, now
    return "30d", now - timedelta(days=30), now


@admin_bp.route("/finance", methods=["GET", "POST"])
@login_required
def finance():
    if request.method == "POST":
        try:
            incurred_on = datetime.strptime(request.form.get("incurred_on", ""), "%Y-%m-%d").date()
            amount = parse_money(request.form.get("amount"))
            if amount is None:
                raise ValueError("Enter an expense amount.")
            expense = Expense(amount_cents=amount, category=request.form.get("category", "Other operating costs")[:60], incurred_on=incurred_on, description=request.form.get("description", "").strip()[:255], reference=request.form.get("reference", "").strip()[:120], admin=current_user)
            db.session.add(expense)
            db.session.add(FinancialAuditEvent(admin=current_user, event_type="expense_created", entity_type="Expense", note=expense.description))
            db.session.commit()
            flash("Expense recorded.", "success")
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "error")
        return redirect(url_for("admin.finance"))
    period, start, end = report_window()
    summary = sales_summary(start, end)
    expenses = Expense.query.filter_by(voided=False).order_by(Expense.incurred_on.desc(), Expense.created_at.desc()).limit(50).all()
    return render_template("admin/finance.html", title="Finance", summary=summary, expenses=expenses, period=period)


@admin_bp.post("/expenses/<int:expense_id>/void")
@login_required
def void_expense(expense_id):
    expense = Expense.query.get_or_404(expense_id)
    expense.voided = True
    expense.correction_note = request.form.get("correction_note", "Voided by admin").strip()[:1000]
    db.session.add(FinancialAuditEvent(admin=current_user, event_type="expense_voided", entity_type="Expense", entity_id=expense.id, note=expense.correction_note))
    db.session.commit()
    flash("Expense voided with an audit note.", "success")
    return redirect(url_for("admin.finance"))


@admin_bp.route("/reconciliation")
@login_required
def reconciliation():
    q = request.args.get("q", "").strip()
    payment_status = request.args.get("payment_status", "").strip()
    method = request.args.get("method", "").strip()
    page = max(1, int(request.args.get("page", 1) or 1))
    query = Order.query
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Order.reference.ilike(like), Order.customer_name.ilike(like), Order.customer_phone.ilike(like), Order.payment_reference.ilike(like)))
    if payment_status:
        query = query.filter(Order.payment_status == payment_status)
    if method:
        query = query.join(PaymentAttempt).filter(PaymentAttempt.payment_method == method)
    pagination = query.order_by(Order.created_at.desc()).paginate(page=page, per_page=20, error_out=False)
    confirmed = Order.query.filter(Order.payment_status.in_(["Paid", "Partially refunded", "Refund requested", "Refund processing"])).all()
    refunds = RefundRequest.query.all()
    summary = {
        "confirmed_cents": sum(order.total_cents or 0 for order in confirmed),
        "pending_count": Order.query.filter(Order.payment_status.in_(["Awaiting payment", "Payment reported", "Processing"])).count(),
        "failed_count": Order.query.filter(Order.payment_status.in_(["Failed", "Cancelled"])).count(),
        "investigation_count": Order.query.filter(Order.payment_status.in_(["Unknown", "Reconciliation required"])).count(),
        "refunded_cents": sum(refund.amount_cents for refund in refunds if refund.status == "completed"),
        "refund_pending_count": sum(1 for refund in refunds if refund.status in {"requested", "processing"}),
    }
    attempts = PaymentAttempt.query.order_by(PaymentAttempt.created_at.desc()).limit(30).all()
    transactions = PaymentTransaction.query.order_by(PaymentTransaction.created_at.desc()).limit(30).all()
    refund_requests = RefundRequest.query.order_by(RefundRequest.created_at.desc()).limit(50).all()
    return render_template("admin/reconciliation.html", title="Payment reconciliation", orders=pagination.items, page=page, has_next=pagination.has_next, has_prev=pagination.has_prev, payment_statuses=PAYMENT_STATUSES, q=q, payment_status=payment_status, method=method, summary=summary, attempts=attempts, transactions=transactions, refund_requests=refund_requests, refundable_cents=refundable_cents)

@admin_bp.post("/reconciliation/orders/<int:order_id>/verify")
@login_required
def verify_payment(order_id):
    order = Order.query.get_or_404(order_id)
    amount = parse_money(request.form.get("amount"))
    receipt = request.form.get("receipt", "").strip()[:120]
    method = request.form.get("payment_method", "Manual M-Pesa").strip()[:40] or "Manual M-Pesa"
    reason = request.form.get("reason", "").strip()[:1000]
    if not receipt or amount is None or amount <= 0 or not reason:
        flash("Receipt, amount, and correction reason are required.", "error")
        return redirect(url_for("admin.reconciliation"))
    existing = PaymentTransaction.query.filter_by(mpesa_receipt=receipt).first()
    if existing and existing.order_id != order.id:
        order.payment_status = "Reconciliation required"
        db.session.add(FinancialAuditEvent(admin=current_user, event_type="duplicate_payment_detected", entity_type="Order", entity_id=order.id, note=f"Receipt {receipt} already belongs to another order"))
        db.session.commit()
        flash("That receipt is already attached to another order. This order was flagged for investigation.", "error")
        return redirect(url_for("admin.reconciliation"))
    transaction = existing or PaymentTransaction(order=order, provider=method, mpesa_receipt=receipt, amount_cents=amount, phone=order.customer_phone, transaction_time=datetime.utcnow(), status="verified")
    transaction.order = order
    transaction.amount_cents = amount
    transaction.discrepancy = "Amount differs from order total" if order.total_cents is not None and amount != order.total_cents else ""
    order.payment_status = "Paid" if not transaction.discrepancy else "Reconciliation required"
    attempt = order.payment_attempts[0] if order.payment_attempts else PaymentAttempt(order=order, expected_amount_cents=order.total_cents, customer_phone=order.customer_phone)
    attempt.status = "verified" if order.payment_status == "Paid" else "reconciliation_required"
    attempt.payment_method = method
    attempt.confirmed_at = datetime.utcnow()
    attempt.last_reconciled_at = datetime.utcnow()
    attempt.result_description = reason
    db.session.add(transaction)
    db.session.add(attempt)
    db.session.add(FinancialAuditEvent(admin=current_user, event_type="payment_confirmed" if order.payment_status == "Paid" else "payment_investigation_required", entity_type="Order", entity_id=order.id, note=reason))
    settings = ShopSettings.query.first()
    if order.payment_status == "Paid" and settings:
        award_loyalty_for_order(order, settings, current_user)
    db.session.commit()
    flash("Payment outcome recorded with audit trail.", "success")
    return redirect(url_for("admin.reconciliation"))

@admin_bp.post("/reconciliation/orders/<int:order_id>/flag")
@login_required
def flag_payment(order_id):
    order = Order.query.get_or_404(order_id)
    note = request.form.get("note", "").strip()[:1000]
    if not note:
        flash("Add an investigation note.", "error")
        return redirect(url_for("admin.reconciliation"))
    order.payment_status = "Reconciliation required"
    attempt = order.payment_attempts[0] if order.payment_attempts else PaymentAttempt(order=order, expected_amount_cents=order.total_cents, customer_phone=order.customer_phone)
    attempt.status = "reconciliation_required"
    attempt.investigation_note = note
    attempt.last_reconciled_at = datetime.utcnow()
    db.session.add(attempt)
    db.session.add(FinancialAuditEvent(admin=current_user, event_type="payment_flagged", entity_type="Order", entity_id=order.id, note=note))
    db.session.commit()
    flash("Payment flagged for investigation.", "success")
    return redirect(url_for("admin.reconciliation"))

@admin_bp.post("/reconciliation/orders/<int:order_id>/refunds")
@login_required
def request_refund(order_id):
    order = Order.query.get_or_404(order_id)
    amount = parse_money(request.form.get("amount"))
    reason = request.form.get("reason", "").strip()[:255]
    if order.payment_status not in {"Paid", "Partially refunded"}:
        flash("Only verified paid orders can enter the refund workflow.", "error")
        return redirect(url_for("admin.reconciliation"))
    if amount is None or amount <= 0 or amount > refundable_cents(order):
        flash("Refund amount must be positive and within the remaining refundable amount.", "error")
        return redirect(url_for("admin.reconciliation"))
    if RefundRequest.query.filter_by(order_id=order.id, amount_cents=amount, reason=reason, status="requested").first():
        flash("A matching refund request is already awaiting processing.", "error")
        return redirect(url_for("admin.reconciliation"))
    refund = RefundRequest(order=order, amount_cents=amount, reason=reason or "Manual refund requested", status="requested", admin=current_user)
    order.payment_status = "Refund requested"
    db.session.add(refund)
    db.session.add(FinancialAuditEvent(admin=current_user, event_type="refund_requested", entity_type="Order", entity_id=order.id, note=refund.reason))
    db.session.commit()
    flash("Refund request recorded. Process the actual M-Pesa reversal/manual refund outside the app, then update the status.", "success")
    return redirect(url_for("admin.reconciliation"))

@admin_bp.post("/reconciliation/refunds/<int:refund_id>")
@login_required
def update_refund(refund_id):
    refund = RefundRequest.query.get_or_404(refund_id)
    status = request.form.get("status", refund.status)
    reference = request.form.get("reference", "").strip()[:120]
    note = request.form.get("note", "").strip()[:1000]
    if status == "completed" and not reference:
        flash("A verified refund reference is required before marking a refund completed.", "error")
        return redirect(url_for("admin.reconciliation"))
    refund.status = status
    refund.reference = reference
    refund.note = note
    if status == "completed":
        refund.completed_at = datetime.utcnow()
        remaining = refundable_cents(refund.order) - refund.amount_cents
        refund.order.payment_status = "Refunded" if remaining <= 0 else "Partially refunded"
    elif status == "processing":
        refund.order.payment_status = "Refund processing"
    elif status == "failed":
        refund.order.payment_status = "Refund failed"
    db.session.add(FinancialAuditEvent(admin=current_user, event_type=f"refund_{status}", entity_type="RefundRequest", entity_id=refund.id, note=note))
    db.session.commit()
    flash("Refund status updated.", "success")
    return redirect(url_for("admin.reconciliation"))
@admin_bp.route("/reports/sales.csv")
@login_required
def sales_csv():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["reference", "created_at", "customer", "payment_status", "fulfilment_status", "total_cents"])
    for order in Order.query.order_by(Order.created_at.desc()).all():
        writer.writerow([order.reference, order.created_at.isoformat(), order.customer_name, order.payment_status, order.status, order.total_cents])
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=fity-sales.csv"})


