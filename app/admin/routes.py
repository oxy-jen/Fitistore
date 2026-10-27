import ipaddress
import csv
import io
import json
import zipfile
from datetime import datetime, timedelta
import re
import socket
import urllib.request
from secrets import token_urlsafe
from uuid import UUID, uuid4
import hashlib
from pathlib import Path
from urllib.parse import unquote, urlparse
from flask import Blueprint, Response, current_app, flash, redirect, render_template, request, send_file, send_from_directory, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import func, or_, update
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename
from app.extensions import db
from app.models import AdminUser, CustomerAccount, CustomerCart, CustomerOrderLink, Category, Expense, FinancialAuditEvent, InventoryMovement, LoyaltyTransaction, Order, OrderItem, OrderStatusHistory, PaymentAttempt, PaymentTransaction, Product, ProductEnquiry, ProductImage, ProductSuggestion, ProductVariant, RefundRequest, RestockAlert, Review, ShopSettings, CustomerConsentEvent, CustomerToken, EmailCampaign, EmailCampaignRecipient, EmailDraft, EmailMessage, EmailTemplate
from app.services.formatting import parse_money
from app.services.inventory import InventoryError, adjust_stock, adjust_variant_stock, low_stock_products
from app.services.orders import FULFILMENT_STATUSES, PAYMENT_STATUSES, award_loyalty_for_order, refundable_cents, sales_summary, update_order_status
from app.services.product_images import ImageStorageError, cloudinary_configured, cloudinary_public_id, delete_cloudinary_asset, upload_image_bytes

admin_bp = Blueprint("admin", __name__, template_folder="../templates/admin")


def model_schema_ready(model, inspector=None):
    """Return whether a model's full mapped table is available for ORM use."""
    return not model_schema_missing(model, inspector)


def model_schema_missing(model, inspector=None):
    """Return the concrete missing table or columns needed by an ORM model."""
    if inspector is None:
        from sqlalchemy import inspect
        inspector = inspect(db.engine)
    table_name = model.__tablename__
    if not inspector.has_table(table_name):
        return [f"{table_name} table"]
    actual_columns = {column["name"] for column in inspector.get_columns(table_name)}
    expected_columns = {column.name for column in model.__table__.columns}
    return [f"{table_name}.{name}" for name in sorted(expected_columns - actual_columns)]


@admin_bp.route("/email")
@login_required
def email_center():
    from app.services.customer_mail import mail_configuration_status
    from sqlalchemy import inspect
    search = request.args.get("q", "").strip()[:120]
    inspector = inspect(db.engine)
    if not model_schema_ready(CustomerAccount, inspector):
        flash("Customer email is unavailable because the customer account table is not present in this database.", "warning")
        return render_template("admin/email_unavailable.html", title="Customer email")
    query = CustomerAccount.query.filter_by(active=True, marketing_status="subscribed").filter(CustomerAccount.email_verified_at.isnot(None))
    account_query = CustomerAccount.query
    if search:
        term = f"%{search}%"
        query = query.filter(or_(CustomerAccount.email.ilike(term), CustomerAccount.display_name.ilike(term)))
        customer_match = or_(CustomerAccount.email.ilike(term), CustomerAccount.display_name.ilike(term))
        if model_schema_ready(CustomerOrderLink, inspector) and model_schema_ready(Order, inspector):
            account_query = account_query.outerjoin(CustomerOrderLink, CustomerOrderLink.account_id == CustomerAccount.id).outerjoin(Order, Order.id == CustomerOrderLink.order_id)
            customer_match = or_(customer_match, Order.reference.ilike(term))
        account_query = account_query.filter(customer_match)
    subscribers = query.order_by(CustomerAccount.marketing_consent_at.desc()).paginate(page=max(1, request.args.get("page", 1, type=int)), per_page=25, error_out=False)
    accounts = account_query.order_by(CustomerAccount.created_at.desc()).distinct().limit(100).all()
    order_map = {}
    if accounts and model_schema_ready(CustomerOrderLink, inspector) and model_schema_ready(Order, inspector):
        links = CustomerOrderLink.query.filter(CustomerOrderLink.account_id.in_([a.id for a in accounts])).all()
        order_map = {account.id: [] for account in accounts}
        for link in links:
            order_map.setdefault(link.account_id, []).append(link.order)
    campaigns = EmailCampaign.query.order_by(EmailCampaign.created_at.desc()).limit(25).all() if model_schema_ready(EmailCampaign, inspector) else []
    messages_available = model_schema_ready(EmailMessage, inspector)
    messages = EmailMessage.query.order_by(EmailMessage.created_at.desc()).limit(30).all() if messages_available else []
    products = Product.query.filter_by(active=True).order_by(Product.name).all()
    configured, note = mail_configuration_status()
    eligible_total = CustomerAccount.query.filter_by(active=True, marketing_status="subscribed").filter(CustomerAccount.email_verified_at.isnot(None)).count()
    return render_template("admin/email.html", title="Customer email", subscribers=subscribers, accounts=accounts, order_map=order_map, campaigns=campaigns, messages=messages, messages_available=messages_available, products=products, search=search, eligible_total=eligible_total, mail_configured=configured, mail_note=note, test_address=current_app.config.get("MAIL_TEST_ADDRESS", ""), send_key=uuid4().hex)


@admin_bp.post("/email/send-manual")
@login_required
def send_manual_customer_email():
    from uuid import UUID
    from sqlalchemy import inspect
    from app.services.customer_mail import mail_configuration_status, queue_email
    from app.services.product_images import product_display_image, product_image_alt, product_image_url

    inspector = inspect(db.engine)
    if not all(model_schema_ready(model, inspector) for model in (CustomerAccount, EmailMessage, CustomerToken)):
        flash("Email was not sent. Customer accounts, message history, and unsubscribe-token tables must be available first.", "error")
        return redirect(url_for("admin.email_center"))
    try:
        batch_key = str(UUID(request.form.get("send_key", "")))
    except (ValueError, TypeError, AttributeError):
        flash("Email was not sent because its one-time send key was invalid. Refresh the email centre and try again.", "error")
        return redirect(url_for("admin.email_center"))
    selected_ids = set()
    for raw_id in request.form.getlist("account_ids"):
        try:
            selected_ids.add(int(raw_id))
        except (TypeError, ValueError):
            continue
    if len(selected_ids) > 500:
        flash("This manual send is limited to 500 recipients per request. Nothing was sent.", "error")
        return redirect(url_for("admin.email_center"))
    eligible_query = CustomerAccount.query.filter_by(active=True, marketing_status="subscribed").filter(CustomerAccount.email_verified_at.isnot(None))
    all_eligible = request.form.get("all_eligible") == "yes"
    if all_eligible:
        if eligible_query.count() > 500:
            flash("This manual send is limited to 500 recipients. Use the Email Studio recipient picker to send a smaller confirmed batch.", "error")
            return redirect(url_for("admin.email_center"))
        accounts = eligible_query.order_by(CustomerAccount.id).limit(500).all()
    else:
        accounts = eligible_query.filter(CustomerAccount.id.in_(selected_ids)).order_by(CustomerAccount.id).all() if selected_ids else []
    if not accounts:
        flash("Choose at least one active, verified customer who has subscribed to marketing email.", "error")
        return redirect(url_for("admin.email_center"))
    if len(accounts) > 1 and request.form.get("confirm_send", "").strip() != f"SEND {len(accounts)}":
        flash(f"Email not sent. Type SEND {len(accounts)} to confirm the exact recipient count.", "warning")
        return redirect(url_for("admin.email_center"))
    subject = request.form.get("subject", "").strip()[:200]
    body = request.form.get("body", "").strip()[:5000]
    if not subject or not body:
        flash("Email not sent. Enter both a subject and message.", "error")
        return redirect(url_for("admin.email_center"))
    configured, note = mail_configuration_status()
    if not configured:
        flash("Email was not sent: " + note, "error")
        return redirect(url_for("admin.email_center"))

    products = []
    from urllib.parse import urlparse
    base_url = (current_app.config.get("PUBLIC_BASE_URL") or "").rstrip("/")
    parsed_base = urlparse(base_url)
    if parsed_base.scheme != "https" or not parsed_base.netloc:
        flash("Email was not sent. Set PUBLIC_BASE_URL to the canonical HTTPS store origin first.", "error")
        return redirect(url_for("admin.email_center"))
    if parsed_base.scheme == "https" and parsed_base.netloc:
        for raw_id in request.form.getlist("product_ids"):
            try:
                product = Product.query.filter_by(id=int(raw_id), active=True).first()
                if product and product.display_stock_quantity <= 0:
                    product = None
            except (TypeError, ValueError):
                product = None
            if not product:
                continue
            image = product_display_image(product)
            image_url = product_image_url(image) if image else ""
            if image_url.startswith("/"):
                image_url = base_url + image_url
            if not image_url.startswith("https://"):
                image_url = ""
            products.append({
                "name": product.name,
                "price": product.price_cents,
                "image_url": image_url,
                "image_alt": product_image_alt(image, f"Product image: {product.name}") if image else f"Product image: {product.name}",
                "url": base_url + url_for("public.product_detail", slug=product.slug),
            })
    batch_sent = batch_failed = 0
    for account in accounts:
        key = f"admin-manual:{batch_key}:{account.id}"
        prior = EmailMessage.query.filter_by(idempotency_key=key).first()
        if prior and prior.status in {"sent", "delivered"}:
            batch_sent += 1
            continue
        # The raw token is intentionally never stored; issue a fresh token on retries.
        token = token_urlsafe(32)
        try:
            db.session.add(CustomerToken(account_id=account.id, purpose="unsubscribe", token_hash=hashlib.sha256(token.encode()).hexdigest(), expires_at=datetime.utcnow() + timedelta(days=3650)))
            db.session.commit()
        except Exception:
            db.session.rollback()
            batch_failed += 1
            continue
        unsubscribe_url = base_url + url_for("customer.unsubscribe", token=token)
        first_name = (account.display_name or account.email.partition("@")[0]).strip().split()[0]
        shop_url = base_url + url_for("public.shop")
        contact_url = base_url + url_for("public.contact")
        logo_url = base_url + url_for("static", filename="images/mainLogo-160.png")
        html_body = render_template("emails/manual_customer_email.html", subject=subject, body=body, first_name=first_name, products=products, unsubscribe_url=unsubscribe_url, shop_url=shop_url, contact_url=contact_url, logo_url=logo_url)
        text_body = "\n".join([body, *[f"{item['name']}: {item['url']}" for item in products], *(["Unsubscribe: " + unsubscribe_url] if unsubscribe_url else [])])
        text_body = "\n".join([f"Hi {first_name},", "", body, *[f"{item['name']}: {item['url']}" for item in products], "", "Continue shopping: " + shop_url, "Contact FityStores: " + contact_url, "FityStores · Bei Fity, Fits Poa.", *( ["Unsubscribe: " + unsubscribe_url] if unsubscribe_url else [])])
        try:
            message = queue_email(account.email, subject, "marketing_campaign", key, account_id=account.id, html_body=html_body, text_body=text_body)
            if message.status in {"sent", "delivered"}:
                batch_sent += 1
            else:
                batch_failed += 1
        except Exception:
            db.session.rollback()
            batch_failed += 1
    flash(f"Manual email result: provider accepted {batch_sent}; failed or suppressed {batch_failed}. Provider acceptance does not confirm inbox delivery.", "success" if batch_sent and not batch_failed else "warning")
    return redirect(url_for("admin.email_center"))


@admin_bp.route("/email/automation", methods=["GET", "POST"])
@login_required
def email_automation():
    return render_template("admin/email_automation.html", title="Email automation")


@admin_bp.get("/email/automation/preview/<campaign_type>")
@login_required
def email_automation_preview(campaign_type):
    return Response("Email automation is disabled. Use the manual customer email centre.", status=410)


@admin_bp.route("/email/campaigns", methods=["GET", "POST"])
@login_required
def email_campaigns():
    flash("Promotional campaigns are disabled. Use the manual customer email centre.", "info")
    return redirect(url_for("admin.email_center"))


@admin_bp.route("/email/campaigns/<int:campaign_id>", methods=["GET", "POST"])
@login_required
def email_campaign_detail(campaign_id):
    flash("Promotional campaigns are disabled. Use the manual customer email centre.", "info")
    return redirect(url_for("admin.email_center"))


@admin_bp.post("/email/customers/<int:account_id>/message")
@login_required
def email_customer_message(account_id):
    from app.services.customer_mail import queue_branded_email
    from sqlalchemy import inspect
    inspector = inspect(db.engine)
    if not all(model_schema_ready(model, inspector) for model in (CustomerAccount, EmailMessage, FinancialAuditEvent)):
        flash("Message not sent. Required customer account, email history, or audit schema is unavailable; no email was attempted.", "error")
        return redirect(url_for("admin.email_center"))
    account = CustomerAccount.query.get_or_404(account_id)
    subject = request.form.get("subject", "").strip()[:200]
    body = request.form.get("body", "").strip()[:5000]
    if request.form.get("confirm_send") != "yes" or not account.active or not account.email_verified_at or not subject or not body:
        flash("Choose an active verified customer, enter a subject and message, and confirm the send.", "error")
    else:
        try:
            send_key = str(UUID(request.form.get("send_key", "")))
        except (ValueError, TypeError, AttributeError):
            flash("Message not sent because its one-time send key was invalid. Refresh and try again.", "error")
            return redirect(url_for("admin.email_center"))
        result = queue_branded_email(account.email, subject, "admin_service", f"admin-message:{account.id}:{send_key}", "A message from FityStores", [body], account_id=account.id)
        db.session.add(FinancialAuditEvent(admin=current_user, event_type="customer_service_email", entity_type="CustomerAccount", entity_id=account.id, note=f"EmailMessage {result.id}: {result.status}"))
        db.session.commit()
        flash("Service message status: " + result.status, "success" if result.status == "sent" else "error")
    return redirect(url_for("admin.email_center"))


@admin_bp.post("/email/customers/<int:account_id>/suppress")
@login_required
def suppress_customer_marketing(account_id):
    account = CustomerAccount.query.get_or_404(account_id)
    account.marketing_status = "suppressed"
    account.marketing_withdrawn_at = datetime.utcnow()
    db.session.add(CustomerConsentEvent(account_id=account.id, event="suppressed", method="admin_suppression", wording_version="2026-10"))
    db.session.add(FinancialAuditEvent(admin=current_user, event_type="customer_marketing_suppressed", entity_type="CustomerAccount", entity_id=account.id, note="Admin suppression"))
    db.session.commit()
    flash("Address suppressed from marketing campaigns.", "success")
    return redirect(url_for("admin.email_center"))

@admin_bp.post("/email/customers/<int:account_id>/active")
@login_required
def set_customer_active(account_id):
    account = CustomerAccount.query.get_or_404(account_id)
    account.active = request.form.get("active") == "yes"
    db.session.add(FinancialAuditEvent(admin=current_user, event_type="customer_account_activation_changed", entity_type="CustomerAccount", entity_id=account.id, note="active" if account.active else "inactive"))
    db.session.commit()
    flash("Customer account updated.", "success")
    return redirect(url_for("admin.email_center"))

@admin_bp.before_request
def require_admin_identity():
    if current_user.is_authenticated and not isinstance(current_user._get_current_object(), AdminUser):
        from flask import abort
        abort(403)

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


def cloudinary_public_id_from_input(value):
    value = (value or "").strip()
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme and parsed.netloc:
        marker = "/image/upload/"
        if marker not in parsed.path:
            raise ValueError("Paste a Cloudinary image URL or public ID.")
        value = unquote(parsed.path.split(marker, 1)[1]).lstrip("/")
    value = value.split("?", 1)[0].strip().strip("/")
    parts = [part for part in value.split("/") if part]
    while parts and (parts[0].startswith("c_") or parts[0].startswith("w_") or parts[0].startswith("q_") or parts[0].startswith("f_")):
        parts.pop(0)
    if parts and re.fullmatch(r"v\d+", parts[0]):
        parts.pop(0)
    value = "/".join(parts)
    if not value:
        raise ValueError("Paste a Cloudinary image URL or public ID.")
    if "." in value.rsplit("/", 1)[-1]:
        stem, ext = value.rsplit(".", 1)
        if ext.lower() in current_app.config["ALLOWED_IMAGE_EXTENSIONS"]:
            value = stem
    if "/" not in value and len(value) < 3:
        raise ValueError("Cloudinary public ID looks too short.")
    return value
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
        for image_url in request.form.getlist("online_image_url"):
            save_image_from_url(image_url, product)
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
        if any(field == "status" for field, _old, _new in changes):
            event = OrderStatusHistory.query.filter_by(order_id=order.id, field="status").order_by(OrderStatusHistory.id.desc()).first()
            try:
                from app.services.customer_mail import notify_order_event
                notify_order_event(order, "order_status", f"order-status:{event.id}" if event else f"order-status:{order.id}:{order.status}")
            except Exception:
                current_app.logger.error("Order status notification could not be prepared for order %s", order.id)
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
    status = request.form.get("status", "pending")
    if status not in {"pending", "approved", "rejected"}:
        flash("Choose a valid review status.", "error")
        return redirect(url_for("admin.review_admin"))
    review.status = status
    db.session.commit()
    flash("Review updated.", "success")
    return redirect(url_for("admin.review_admin"))

def parse_nonnegative_int_field(name, default=0):
    value = (request.form.get(name) or "").strip()
    if not value:
        return default
    try:
        return max(0, int(value))
    except ValueError as exc:
        raise ValueError(f"Enter a whole number for {name.replace('_', ' ')}.") from exc


def parse_nonnegative_money_field(name, default=0):
    value = (request.form.get(name) or "").strip()
    if not value:
        return default
    try:
        parsed = parse_money(value)
    except ValueError as exc:
        raise ValueError(f"Enter a valid amount for {name.replace('_', ' ')}.") from exc
    if parsed is None:
        return default
    return max(0, parsed)


@admin_bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    settings = ShopSettings.query.first() or ShopSettings()
    if not settings.id:
        db.session.add(settings)
    if request.method == "POST":
        try:
            for field in ["shop_name", "tagline", "whatsapp_number", "payment_number", "contact_email", "public_location", "collection_instructions", "delivery_area", "payment_instructions", "currency"]:
                setattr(settings, field, request.form.get(field, "").strip())
            settings.delivery_fee_cents = parse_nonnegative_money_field("delivery_fee", None)
            settings.default_low_stock_threshold = parse_nonnegative_int_field("default_low_stock_threshold", 3)
            settings.loyalty_enabled = bool(request.form.get("loyalty_enabled"))
            settings.loyalty_points_per_100 = parse_nonnegative_int_field("loyalty_points_per_100", 0)
            settings.loyalty_min_redeem_points = parse_nonnegative_int_field("loyalty_min_redeem_points", 0)
            settings.loyalty_point_value_cents = parse_nonnegative_money_field("loyalty_point_value", 0)
            settings.referral_enabled = bool(request.form.get("referral_enabled"))
            settings.referral_reward_points = parse_nonnegative_int_field("referral_reward_points", 0)
            settings.referral_min_order_cents = parse_nonnegative_money_field("referral_min_order", 0)
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "error")
            return render_template("admin/settings.html", title="Settings", settings=settings), 400
        db.session.commit()
        flash("Settings saved.", "success")
        return redirect(url_for("admin.settings"))
    return render_template("admin/settings.html", title="Settings", settings=settings)


@admin_bp.route("/cloudinary-images", methods=["GET", "POST"])
@login_required
def cloudinary_images():
    from app import backfill_cloudinary_image_ids, ensure_cloudinary_image_columns, product_image_inventory_rows

    rows = []
    if request.method == "POST":
        action = request.form.get("action")
        if action == "schema":
            try:
                ensure_cloudinary_image_columns()
                flash("Cloudinary image columns are ready.", "success")
            except Exception as exc:
                db.session.rollback()
                current_app.logger.exception("Cloudinary schema setup failed")
                flash(f"Cloudinary schema setup failed: {exc}", "error")
            return redirect(url_for("admin.cloudinary_images"))

        if action == "single":
            try:
                image = ProductImage.query.get_or_404(int(request.form.get("image_id") or 0))
                image.cloudinary_public_id = cloudinary_public_id_from_input(request.form.get("cloudinary_value"))
                image.storage_backend = "cloudinary"
                db.session.commit()
                flash(f"Cloudinary image saved for {image.product.name}.", "success")
            except ValueError as exc:
                db.session.rollback()
                flash(str(exc), "error")
            except Exception as exc:
                db.session.rollback()
                current_app.logger.exception("Cloudinary image save failed")
                flash(f"Cloudinary image save failed: {exc}", "error")
            return redirect(url_for("admin.cloudinary_images"))

        if action == "mapping":
            file = request.files.get("mapping")
            if not file or not file.filename:
                flash("Choose a CSV mapping file first.", "error")
                return redirect(url_for("admin.cloudinary_images"))
            data = file.read(current_app.config["MAX_CONTENT_LENGTH"] + 1)
            if len(data) > current_app.config["MAX_CONTENT_LENGTH"]:
                flash("Mapping file is too large.", "error")
                return redirect(url_for("admin.cloudinary_images"))
            try:
                text = data.decode("utf-8-sig")
                mapping_rows = list(csv.DictReader(io.StringIO(text)))
                apply_changes = request.form.get("apply") == "1"
                ignore_filename = request.form.get("ignore_filename") == "1"
                migrated, skipped, missing, failed = backfill_cloudinary_image_ids(mapping_rows, apply=apply_changes, ignore_filename=ignore_filename)
                mode = "applied" if apply_changes else "dry run"
                category = "success" if failed == 0 else "warning"
                flash(f"Cloudinary mapping {mode}: migrated={migrated} skipped={skipped} missing={missing} failed={failed}.", category)
            except Exception as exc:
                db.session.rollback()
                current_app.logger.exception("Cloudinary image mapping failed")
                flash(f"Cloudinary image mapping failed: {exc}", "error")
            return redirect(url_for("admin.cloudinary_images"))

    try:
        rows = product_image_inventory_rows()
    except Exception:
        rows = []
    missing_cloudinary = sum(1 for row in rows if not row["cloudinary_public_id"])
    cloudinary_ready = sum(1 for row in rows if row["cloudinary_public_id"])
    return render_template("admin/cloudinary_images.html", title="Cloudinary images", rows=rows, missing_cloudinary=missing_cloudinary, cloudinary_ready=cloudinary_ready)


@admin_bp.route("/cloudinary-images/export.csv")
@login_required
def cloudinary_images_export():
    from app import product_image_inventory_rows

    rows = product_image_inventory_rows()
    fieldnames = ["product_id", "product_slug", "product_sku", "product_name", "image_id", "filename", "sort_order", "is_primary", "storage_backend", "cloudinary_public_id"]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=fity-product-image-map.csv"})

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
    if order.payment_status == "Paid":
        try:
            from app.services.customer_mail import notify_order_event
            notify_order_event(order, "payment_confirmed", f"payment-confirmed:{transaction.id}")
        except Exception:
            current_app.logger.error("Payment notification could not be prepared for order %s", order.id)
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





DATA_EXPORT_MAX_AGE_SECONDS = 24 * 60 * 60
DATA_CLEAR_CONFIRMATION = "CLEAR OPERATIONAL DATA"
DATA_CLEANUP_CATEGORIES = {
    "orders": {"label": "Orders and payment records", "tables": ("customer_order_link", "refund_request", "payment_transaction", "payment_attempt", "order_status_history", "order_item", "order")},
    "customers": {"label": "Customer accounts and verification data", "tables": ("customer_token", "customer_consent_event", "customer_order_link", "email_campaign_recipient", "email_message", "referral_click", "customer_account", "customer_profile")},
    "customer_activity": {"label": "Reviews, enquiries, suggestions, and restock alerts", "tables": ("review", "product_enquiry", "product_suggestion", "restock_alert")},
    "referrals_loyalty": {"label": "Referrals and loyalty history", "tables": ("referral_click", "referral", "loyalty_transaction")},
    "finance_inventory": {"label": "Expenses and inventory movements", "tables": ("expense", "inventory_movement")},
    "email": {"label": "Email campaigns and message logs", "tables": ("email_campaign_recipient", "email_message", "email_campaign")},
    "audit": {"label": "Previous audit history and temporary login limits", "tables": ("financial_audit_event", "auth_rate_limit")},
    "products": {"label": "Product catalog, variants, and uploaded images", "tables": ("cart_add_count", "inventory_movement", "restock_alert", "product_variant", "product_image", "product")},
}


def _share_card_cache_dir():
    return Path(current_app.instance_path) / "cache" / "product-share-cards"


def _admin_data_export_dir():
    folder = Path(current_app.instance_path) / "data_exports"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _collect_admin_data_records():
    """Serialize every mapped application table into a stable, JSON-safe snapshot."""
    records = {}
    mappers = sorted(db.Model.registry.mappers, key=lambda mapper: mapper.local_table.name)
    for mapper in mappers:
        model = mapper.class_
        primary_keys = list(mapper.primary_key)
        query = db.session.query(model)
        if primary_keys:
            query = query.order_by(*primary_keys)
        rows = query.all()
        records[model.__tablename__] = [
            {
                column.name: (
                    getattr(row, column.key).isoformat()
                    if hasattr(getattr(row, column.key), "isoformat")
                    else getattr(row, column.key)
                )
                for column in model.__table__.columns
            }
            for row in rows
        ]
    return records


def _admin_export_bytes(records, exported_at):
    payload = {
        "format": "fitystores-database-export-v1",
        "exported_at": exported_at,
        "records": records,
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        default=lambda value: value.isoformat() if hasattr(value, "isoformat") else str(value),
    ).encode("utf-8")


def _collect_payment_proof_files(records):
    folder = Path(current_app.config["PAYMENT_PROOF_FOLDER"]).resolve()
    files = []
    order_rows = records.get("order", [])
    filenames = {row.get("payment_proof_filename") for row in order_rows if row.get("payment_proof_filename")}
    if folder.is_dir():
        for path in folder.iterdir():
            try:
                if path.is_file() and path.resolve().parent == folder:
                    filenames.add(path.name)
            except OSError:
                continue
    for filename in sorted(filenames):
        if Path(filename).name != filename:
            raise ValueError("Unsafe payment proof filename in order data")
        path = (folder / filename).resolve()
        if path.parent != folder:
            raise ValueError("Payment proof path escapes its configured folder")
        if path.is_file():
            data = path.read_bytes()
            files.append({"filename": filename, "sha256": hashlib.sha256(data).hexdigest(), "data": data})
        else:
            files.append({"filename": filename, "sha256": None, "data": None})
    return files


def _collect_product_image_files(records):
    """Collect local product images for the downloadable database archive."""
    folder = Path(current_app.config["UPLOAD_FOLDER"]).resolve()
    files = []
    for row in records.get("product_image", []):
        filename = row.get("filename")
        if not filename or filename.startswith("sample-") or row.get("cloudinary_public_id") or row.get("storage_backend") == "cloudinary":
            continue
        if Path(filename).name != filename:
            raise ValueError("Unsafe product image filename in catalog data")
        path = (folder / filename).resolve()
        if path.parent != folder:
            raise ValueError("Product image path escapes its configured folder")
        if path.is_file():
            data = path.read_bytes()
            files.append({"filename": filename, "sha256": hashlib.sha256(data).hexdigest(), "data": data})
        else:
            files.append({"filename": filename, "sha256": None, "data": None})
    return files


def _admin_export_archive(records, exported_at):
    database = _admin_export_bytes(records, exported_at)
    proof_files = _collect_payment_proof_files(records)
    product_images = _collect_product_image_files(records)
    manifest = {
        "format": "fitystores-data-archive-v1",
        "exported_at": exported_at,
        "database_sha256": hashlib.sha256(database).hexdigest(),
        "payment_proofs": [
            {"filename": item["filename"], "sha256": item["sha256"], "included": item["data"] is not None}
            for item in proof_files
        ],
        "product_images": [
            {"filename": item["filename"], "sha256": item["sha256"], "included": item["data"] is not None}
            for item in product_images
        ],
    }
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("database.json", database)
        bundle.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for item in proof_files:
            if item["data"] is not None:
                bundle.writestr(f"payment_proofs/{item['filename']}", item["data"])
        for item in product_images:
            if item["data"] is not None:
                bundle.writestr(f"product_images/{item['filename']}", item["data"])
    return archive.getvalue()


def _valid_admin_data_export():
    metadata = session.get("admin_data_export") or {}
    export_id = metadata.get("id", "")
    exported_at = metadata.get("exported_at", 0)
    if not re.fullmatch(r"[a-f0-9]{32}", export_id):
        return None
    if not isinstance(exported_at, (int, float)) or datetime.utcnow().timestamp() - exported_at > DATA_EXPORT_MAX_AGE_SECONDS:
        return None
    path = _admin_data_export_dir() / f"{export_id}.zip"
    try:
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != metadata.get("sha256"):
            return None
        with zipfile.ZipFile(io.BytesIO(data), "r") as bundle:
            manifest = json.loads(bundle.read("manifest.json").decode("utf-8"))
            database = bundle.read("database.json")
        if manifest.get("format") != "fitystores-data-archive-v1":
            return None
        if hashlib.sha256(database).hexdigest() != manifest.get("database_sha256"):
            return None
        payload = json.loads(database.decode("utf-8"))
        if payload.get("format") != "fitystores-database-export-v1":
            return None
        return {
            "id": export_id,
            "exported_at": payload.get("exported_at"),
            "records": payload.get("records"),
            "payment_proofs": manifest.get("payment_proofs", []),
            "product_images": manifest.get("product_images", []),
        }
    except (OSError, ValueError, TypeError, KeyError, zipfile.BadZipFile):
        return None


@admin_bp.route("/data", methods=["GET"])
@login_required
def data_management():
    export = _valid_admin_data_export()
    from app.services.share_cards import product_share_card_cache_count
    cache_count = product_share_card_cache_count(_share_card_cache_dir())
    all_clear_tables = set().union(*(set(category["tables"]) for category in DATA_CLEANUP_CATEGORIES.values()))
    clear_counts = {table: len(export["records"].get(table, [])) for table in all_clear_tables} if export else {}
    cleanup_categories = [{"key": key, "label": definition["label"], "tables": definition["tables"], "total": sum(clear_counts.get(table, 0) for table in definition["tables"])} for key, definition in DATA_CLEANUP_CATEGORIES.items()]
    return render_template(
        "admin/data.html",
        title="Data export and cleanup",
        export=export,
        export_max_age_hours=DATA_EXPORT_MAX_AGE_SECONDS // 3600,
        clear_confirmation=DATA_CLEAR_CONFIRMATION,
        clear_total=sum(clear_counts.values()),
        clear_counts=clear_counts,
        cleanup_categories=cleanup_categories,
        cache_count=cache_count,
    )


@admin_bp.post("/data/export")
@login_required
def export_admin_data():
    from datetime import timezone
    records = _collect_admin_data_records()
    exported_at = datetime.now(timezone.utc).isoformat()
    data = _admin_export_archive(records, exported_at)
    export_id = uuid4().hex
    path = _admin_data_export_dir() / f"{export_id}.zip"
    path.write_bytes(data)
    session["admin_data_export"] = {
        "id": export_id,
        "exported_at": datetime.utcnow().timestamp(),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    db.session.rollback()
    return send_file(
        io.BytesIO(data),
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"fitystores-data-export-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.zip",
        max_age=0,
    )


@admin_bp.post("/data/clear-cache")
@login_required
def clear_admin_cache():
    export = _valid_admin_data_export()
    if not export or request.form.get("export_id") != export["id"]:
        flash("Cache clear stopped: download a fresh data export first, then refresh this page.", "error")
        return redirect(url_for("admin.data_management"))
    if request.form.get("export_confirmed") != "yes":
        flash("Cache clear stopped: confirm that you downloaded the export first.", "error")
        return redirect(url_for("admin.data_management"))
    try:
        current_records = _collect_admin_data_records()
        current_proofs = _collect_payment_proof_files(current_records)
        proof_state = [{"filename": item["filename"], "sha256": item["sha256"], "included": item["data"] is not None} for item in current_proofs]
        if current_records != export["records"] or proof_state != export["payment_proofs"]:
            db.session.rollback()
            flash("Cache clear stopped: records or payment proof files changed after export. Download a fresh export first.", "error")
            return redirect(url_for("admin.data_management"))
        db.session.rollback()
        from app.services.share_cards import clear_product_share_card_cache
        cleared = clear_product_share_card_cache(_share_card_cache_dir())
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Admin product share-card cache clear failed")
        flash("Cache clear failed. No database records were changed.", "error")
        return redirect(url_for("admin.data_management"))
    flash(f"Cleared {cleared} cached product share-card image(s). Other records were not changed.", "success")
    return redirect(url_for("admin.data_management"))


from .email_studio import register_email_studio_routes
register_email_studio_routes(admin_bp)


@admin_bp.post("/data/clear")
@login_required
def clear_admin_data():
    export = _valid_admin_data_export()
    if not export or request.form.get("export_id") != export["id"]:
        flash("Cleanup stopped: download a fresh data export first, then refresh this page.", "error")
        return redirect(url_for("admin.data_management"))
    if request.form.get("export_confirmed") != "yes" or request.form.get("confirmation", "").strip() != DATA_CLEAR_CONFIRMATION:
        flash(f"Cleanup stopped: confirm the downloaded export and type {DATA_CLEAR_CONFIRMATION} exactly.", "error")
        return redirect(url_for("admin.data_management"))
    selected_categories = set(request.form.getlist("categories"))
    if len(selected_categories) != 1 or not selected_categories.issubset(DATA_CLEANUP_CATEGORIES):
        flash("Cleanup stopped: select exactly one valid data section. Sections must be cleared separately.", "error")
        return redirect(url_for("admin.data_management"))
    selected_tables = set().union(*(set(DATA_CLEANUP_CATEGORIES[key]["tables"]) for key in selected_categories))

    from app.models import (
        AuthRateLimit, CartAddCount, CustomerAccount, CustomerConsentEvent, CustomerOrderLink,
        CustomerProfile, CustomerToken, EmailCampaign, EmailCampaignRecipient,
        EmailMessage, Expense, FinancialAuditEvent, InventoryMovement,
        LoyaltyTransaction, Order, OrderItem, OrderStatusHistory, PaymentAttempt,
        PaymentTransaction, Product, ProductEnquiry, ProductImage, ProductSuggestion, ProductVariant, Referral,
        ReferralClick, RefundRequest, RestockAlert, Review,
    )
    try:
        current_records = _collect_admin_data_records()
        current_proofs = _collect_payment_proof_files(current_records)
        current_product_images = _collect_product_image_files(current_records)
        proof_state = [{"filename": item["filename"], "sha256": item["sha256"], "included": item["data"] is not None} for item in current_proofs]
        image_state = [{"filename": item["filename"], "sha256": item["sha256"], "included": item["data"] is not None} for item in current_product_images]
        if current_records != export["records"] or proof_state != export["payment_proofs"] or image_state != export.get("product_images", []):
            db.session.rollback()
            flash("Cleanup stopped: database records or payment proof files changed after export. Download a fresh export and review it before retrying.", "error")
            return redirect(url_for("admin.data_management"))

        delete_models = [
            CartAddCount, EmailCampaignRecipient, EmailMessage, CustomerToken, CustomerConsentEvent,
            CustomerOrderLink, ReferralClick, RefundRequest, PaymentTransaction,
            PaymentAttempt, OrderStatusHistory, OrderItem, Referral,
            LoyaltyTransaction, InventoryMovement, Review, ProductEnquiry,
            ProductSuggestion, RestockAlert, Expense, FinancialAuditEvent,
            EmailCampaign, Order, CustomerProfile, CustomerAccount, AuthRateLimit,
        ]
        order_proof_filenames = {row.get("payment_proof_filename") for row in export["records"].get("order", []) if row.get("payment_proof_filename")} if "orders" in selected_categories else set()
        deleted = {}
        updated = {}
        if "orders" in selected_categories:
            order_ids = [row["id"] for row in export["records"].get("order", [])]
            if order_ids:
                updated["email_message_order_links_detached"] = EmailMessage.query.filter(EmailMessage.order_id.in_(order_ids)).update(
                    {EmailMessage.order_id: None}, synchronize_session=False
                )
                updated["referral_order_links_detached"] = Referral.query.filter(Referral.qualifying_order_id.in_(order_ids)).update(
                    {Referral.qualifying_order_id: None}, synchronize_session=False
                )
                updated["loyalty_order_links_detached"] = LoyaltyTransaction.query.filter(LoyaltyTransaction.order_id.in_(order_ids)).update(
                    {LoyaltyTransaction.order_id: None}, synchronize_session=False
                )
        if "customers" in selected_categories:
            account_ids = [row["id"] for row in export["records"].get("customer_account", [])]
            if account_ids:
                updated["review_customer_links_detached"] = Review.query.filter(Review.customer_account_id.in_(account_ids)).update(
                    {Review.customer_account_id: None}, synchronize_session=False
                )
        for model in delete_models:
            if model.__tablename__ not in selected_tables:
                continue
            query = db.session.query(model)
            deleted[model.__tablename__] = query.count()
            query.delete(synchronize_session=False)

        if "products" in selected_categories:
            product_ids = [row["id"] for row in export["records"].get("product", [])]
            variant_ids = [row["id"] for row in export["records"].get("product_variant", [])]
            if product_ids:
                order_items = OrderItem.query.filter(OrderItem.product_id.in_(product_ids))
                updated["order_item_product_links_detached"] = order_items.update(
                    {OrderItem.product_id: None, OrderItem.variant_id: None}, synchronize_session=False
                )
                if variant_ids:
                    updated["order_item_variant_links_detached"] = OrderItem.query.filter(OrderItem.variant_id.in_(variant_ids)).update(
                        {OrderItem.variant_id: None}, synchronize_session=False
                    )
                updated["review_product_links_detached"] = Review.query.filter(Review.product_id.in_(product_ids)).update(
                    {Review.product_id: None}, synchronize_session=False
                )
                updated["enquiry_product_links_detached"] = ProductEnquiry.query.filter(ProductEnquiry.product_id.in_(product_ids)).update(
                    {ProductEnquiry.product_id: None}, synchronize_session=False
                )
                if "inventory_movement" not in deleted:
                    deleted["inventory_movement"] = InventoryMovement.query.filter(InventoryMovement.product_id.in_(product_ids)).count()
                    InventoryMovement.query.filter(InventoryMovement.product_id.in_(product_ids)).delete(synchronize_session=False)
                if "restock_alert" not in deleted:
                    deleted["restock_alert"] = RestockAlert.query.filter(RestockAlert.product_id.in_(product_ids)).count()
                    RestockAlert.query.filter(RestockAlert.product_id.in_(product_ids)).delete(synchronize_session=False)
                deleted["product_variant"] = ProductVariant.query.filter(ProductVariant.product_id.in_(product_ids)).count()
                ProductVariant.query.filter(ProductVariant.product_id.in_(product_ids)).delete(synchronize_session=False)
                deleted["product_image"] = ProductImage.query.filter(ProductImage.product_id.in_(product_ids)).count()
                ProductImage.query.filter(ProductImage.product_id.in_(product_ids)).delete(synchronize_session=False)
                deleted["product"] = Product.query.filter(Product.id.in_(product_ids)).count()
                Product.query.filter(Product.id.in_(product_ids)).delete(synchronize_session=False)

        db.session.add(FinancialAuditEvent(
            admin_id=current_user.id,
            event_type="operational_data_cleared",
            entity_type="DatabaseExport",
            note=json.dumps({"export_id": export["id"], "deleted_counts": deleted, "updated_links": updated}, sort_keys=True),
        ))
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception("Admin operational data cleanup failed; transaction rolled back")
        flash(f"Cleanup failed ({type(exc).__name__}) and was rolled back. No records were cleared; check the server log for details.", "error")
        return redirect(url_for("admin.data_management"))

    proof_folder = Path(current_app.config["PAYMENT_PROOF_FOLDER"]).resolve()
    failed_proof_deletes = 0
    for item in current_proofs:
        if item["filename"] not in order_proof_filenames:
            continue
        if item["data"] is None:
            continue
        proof_path = (proof_folder / item["filename"]).resolve()
        if proof_path.parent != proof_folder:
            failed_proof_deletes += 1
            continue
        try:
            proof_path.unlink(missing_ok=True)
        except OSError:
            failed_proof_deletes += 1
            current_app.logger.exception("Could not remove archived payment proof %s", item["filename"])
    failed_image_deletes = 0
    if "products" in selected_categories:
        upload_folder = Path(current_app.config["UPLOAD_FOLDER"]).resolve()
        for item in current_product_images:
            if item["data"] is None:
                continue
            image_path = (upload_folder / item["filename"]).resolve()
            if image_path.parent != upload_folder:
                failed_image_deletes += 1
                continue
            try:
                image_path.unlink(missing_ok=True)
            except OSError:
                failed_image_deletes += 1
                current_app.logger.exception("Could not remove archived product image %s", item["filename"])
    session.pop("admin_data_export", None)
    try:
        (_admin_data_export_dir() / f"{export['id']}.zip").unlink(missing_ok=True)
    except OSError:
        current_app.logger.warning("Could not remove consumed data export %s", export["id"])
    labels = ", ".join(DATA_CLEANUP_CATEGORIES[key]["label"] for key in DATA_CLEANUP_CATEGORIES if key in selected_categories)
    message = f"Removed {sum(deleted.values())} record(s) from {labels} after matching the verified export. Admin accounts, category definitions, shop settings, and a cleanup audit entry were preserved."
    warnings = []
    if failed_proof_deletes:
        warnings.append(f"{failed_proof_deletes} archived payment proof file(s) could not be removed from the server")
    if failed_image_deletes:
        warnings.append(f"{failed_image_deletes} archived local product image(s) could not be removed from the server")
    if warnings:
        flash(message + ". Cleanup note: " + "; ".join(warnings) + ".", "warning")
    else:
        flash(message, "success")
    return redirect(url_for("admin.data_management"))
