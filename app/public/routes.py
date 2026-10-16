import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4
from flask import Blueprint, Response, current_app, flash, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import or_
from sqlalchemy.orm import joinedload, selectinload
from app.extensions import db, csrf
from app.models import Category, CustomerProfile, Order, Product, ProductEnquiry, ProductSuggestion, ProductVariant, ReferralClick, RestockAlert, Review
from app.services.cart import validate_cart
from app.services.orders import create_checkout_order
from app.services.settings import current_settings
from app.services.whatsapp import LOCAL_CONTACT_NUMBER, build_general_message, build_mamba_suggestion_message, product_whatsapp_link, whatsapp_link, whatsapp_text_link
from app.services.product_images import product_display_image, product_image_alt, product_image_url, valid_product_image
from app.services.referrals import get_or_create_profile, normalize_phone, record_referral_click, referral_points_balance
from werkzeug.utils import secure_filename

public_bp = Blueprint("public", __name__)
PRODUCTS_PER_PAGE = 8

def product_list_options(query):
    return query.options(
        joinedload(Product.category),
        selectinload(Product.images),
        selectinload(Product.variants).joinedload(ProductVariant.image),
    )

TRACKING_STEP_COPY = {
    "Order received": "Your order has been received and is waiting for seller review.",
    "Awaiting payment": "The seller is checking payment or waiting for payment details.",
    "Payment confirmed": "Payment has been confirmed for this order.",
    "Preparing order": "Your items are being prepared.",
    "Ready for collection": "Your order is ready. Use the collection instructions below.",
    "Out for delivery": "Your order is on the way. Keep your phone nearby for delivery updates.",
    "Completed": "Your order has been completed.",
    "Cancelled": "This order has been cancelled. Contact the seller if you need help.",
}


def tracking_steps_for(order):
    if not order:
        return []
    base_steps = ["Order received", "Awaiting payment", "Payment confirmed", "Preparing order"]
    if order.fulfilment_method == "Collection":
        base_steps.append("Ready for collection")
    else:
        base_steps.append("Out for delivery")
    base_steps.append("Completed")
    if order.status == "Cancelled":
        base_steps.append("Cancelled")
    history_by_status = {}
    status_events = [event for event in order.history if event.field == "status"]
    for event in sorted(status_events, key=lambda item: item.created_at):
        history_by_status[event.new_value] = event
    current_index = base_steps.index(order.status) if order.status in base_steps else 0
    steps = []
    for index, status in enumerate(base_steps):
        event = history_by_status.get(status)
        reached = bool(event) or index <= current_index
        steps.append({
            "label": status,
            "description": TRACKING_STEP_COPY[status],
            "time": event.created_at if event else (order.created_at if status == "Order received" else None),
            "state": "current" if status == order.status else "done" if reached else "upcoming",
        })
    return steps

def allowed_upload(filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in current_app.config["ALLOWED_IMAGE_EXTENSIONS"]

def save_payment_proof(file):
    if not file or not file.filename:
        return ""
    if not allowed_upload(file.filename):
        flash("Payment screenshot must be jpg, jpeg, png, webp, or gif.", "error")
        return ""
    proof_dir = Path(current_app.config["PAYMENT_PROOF_FOLDER"])
    proof_dir.mkdir(parents=True, exist_ok=True)
    safe = secure_filename(file.filename)
    filename = f"{uuid4().hex}_{safe}"
    file.save(proof_dir / filename)
    return filename

@public_bp.app_context_processor
def helpers():
    from app.services.formatting import money, split_options
    return {"settings": current_settings(), "money": money, "split_options": split_options, "product_display_image": product_display_image, "product_image_url": product_image_url, "product_image_alt": product_image_alt, "valid_product_image": valid_product_image, "product_whatsapp_link": product_whatsapp_link, "whatsapp_text_link": whatsapp_text_link, "build_general_message": build_general_message, "local_contact_number": LOCAL_CONTACT_NUMBER}

def product_query():
    q = request.args.get("q", "").strip()
    category_slug = request.args.get("category", "").strip()
    sort = request.args.get("sort", "newest")
    query = product_list_options(Product.query.filter_by(active=True).join(Category))
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Product.name.ilike(like), Product.description.ilike(like), Product.sku.ilike(like)))
    active_category = None
    if category_slug:
        active_category = Category.query.filter_by(slug=category_slug).first()
        if active_category:
            query = query.filter(Product.category == active_category)
    if sort == "price-low":
        query = query.order_by(Product.price_cents.is_(None), Product.price_cents.asc(), Product.name.asc())
    elif sort == "price-high":
        query = query.order_by(Product.price_cents.is_(None), Product.price_cents.desc(), Product.name.asc())
    elif sort == "name":
        query = query.order_by(Product.name.asc())
    else:
        query = query.order_by(Product.created_at.desc(), Product.id.desc())
    return query, active_category, q, sort

def product_card_payload(product):
    from app.services.formatting import money
    image = product_display_image(product)
    return {
        "id": product.id,
        "name": product.name,
        "category": product.category.name,
        "price": money(product.price_cents, current_settings().currency),
        "stock_quantity": product.display_stock_quantity,
        "has_variants": any(variant.active for variant in product.variants),
        "url": url_for("public.product_detail", slug=product.slug),
        "image_url": product_image_url(image),
        "alt": product_image_alt(image),
    }

UPCOMING_MERCHANDISE = [
    {"name": "Campus fits", "category": "Outfit bundles", "detail": "Ready-to-wear combinations for class, errands, and weekend plans."},
    {"name": "Sneaker drops", "category": "Shoes", "detail": "Affordable men's sneakers and casual shoes in requested sizes."},
    {"name": "Gym and lounge basics", "category": "Vests and boxers", "detail": "Comfort-first basics for daily wear and training days."},
    {"name": "Accessory restocks", "category": "Socks and extras", "detail": "Ankle socks, grooming extras, and small add-ons for quick checkout."},
]

@public_bp.route("/")
def home():
    latest_products = product_list_options(Product.query.filter_by(active=True)).order_by(Product.updated_at.desc(), Product.id.desc()).limit(12).all()
    featured = product_list_options(Product.query.filter_by(active=True, featured=True)).order_by(Product.updated_at.desc(), Product.id.desc()).limit(8).all() or latest_products[:8]
    discounted = product_list_options(Product.query.filter(Product.active.is_(True), Product.previous_price_cents.isnot(None), Product.price_cents.isnot(None), Product.previous_price_cents > Product.price_cents)).order_by(Product.updated_at.desc()).limit(8).all()
    categories = Category.query.join(Product).filter(Product.active.is_(True)).group_by(Category.id).order_by(Category.name).all()
    category_sections = []
    for category in categories:
        products = product_list_options(Product.query.filter_by(active=True, category=category)).order_by(Product.updated_at.desc(), Product.id.desc()).limit(6).all()
        if products:
            category_sections.append({"category": category, "products": products})
    mamba_category_names = ["Boxers", "Shoes", "Vests", "Ankle socks", "Socks"]
    mamba_products = product_list_options(Product.query.join(Category).filter(Product.active.is_(True), Category.name.in_(mamba_category_names))).order_by(Category.name.asc(), Product.updated_at.desc()).all()
    if not mamba_products:
        mamba_products = latest_products[:8]
    mamba_category_ids = {product.category_id for product in mamba_products}
    mamba_categories = [category for category in categories if category.id in mamba_category_ids]
    return render_template("home.html", title="FityStores essentials", featured=featured, discounted=discounted, latest_products=latest_products, categories=categories, category_sections=category_sections, mamba_products=mamba_products, mamba_categories=mamba_categories, suggestion_categories=SUGGESTION_CATEGORIES, upcoming_merchandise=UPCOMING_MERCHANDISE)


@public_bp.route("/referrals", methods=["GET", "POST"])
def referrals():
    settings = current_settings()
    profile = None
    phone = session.get("referral_phone", "")
    if request.method == "POST":
        phone = request.form.get("phone", "").strip()
        name = request.form.get("display_name", "").strip()
        try:
            profile = get_or_create_profile(phone, name, session.get("referral_code", ""))
            db.session.commit()
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "error")
            return redirect(url_for("public.referrals"))
        session["referral_phone"] = profile.phone
        flash("Referral profile ready. Share your link to earn points.", "success")
        return redirect(url_for("public.referrals"))
    normalized = normalize_phone(phone)
    if normalized:
        profile = CustomerProfile.query.filter_by(phone=normalized).first()
    share_url = url_for("public.referral_visit", code=profile.referral_code, _external=True) if profile else ""
    click_count = ReferralClick.query.filter_by(referral_code=profile.referral_code).count() if profile else 0
    rewarded_clicks = ReferralClick.query.filter_by(referral_code=profile.referral_code, rewarded=True).count() if profile else 0
    points = referral_points_balance(profile.phone) if profile else 0
    return render_template("referrals.html", title="Referrals", profile=profile, share_url=share_url, click_count=click_count, rewarded_clicks=rewarded_clicks, points=points, referral_settings=settings)


@public_bp.route("/r/<code>")
def referral_visit(code):
    settings = current_settings()
    profile, rewarded, message = record_referral_click(code, request, settings, session.get("referral_phone", ""))
    session["referral_code"] = (code or "").strip().upper()
    if profile:
        flash("Referral link applied. Thanks for supporting FityStores.", "success" if rewarded else "info")
    else:
        flash(message, "error")
    return redirect(url_for("public.shop", ref=(code or "").strip().upper()))

@public_bp.route("/upcoming")
def upcoming():
    return render_template("upcoming.html", title="Upcoming merchandise", upcoming_merchandise=UPCOMING_MERCHANDISE, suggestion_categories=SUGGESTION_CATEGORIES)

SUGGESTION_CATEGORIES = ["Boxers", "Men's shoes and sneakers", "Vests", "Socks", "Slippers", "Other men's accessories"]


def suggestion_form_data():
    return {
        "product_name": request.form.get("product_name", "").strip(),
        "category": request.form.get("category", "").strip(),
        "preferences": request.form.get("preferences", "").strip(),
        "price_range": request.form.get("price_range", "").strip(),
        "details": request.form.get("details", "").strip(),
        "website_url": request.form.get("website_url", "").strip(),
    }


def validate_suggestion_payload(data):
    errors = []
    if data["website_url"]:
        errors.append("Please submit the suggestion using the visible fields only.")
    if len(data["product_name"]) < 2:
        errors.append("Enter the product or item name.")
    if len(data["product_name"]) > 140:
        errors.append("Keep the product name under 140 characters.")
    if data["category"] not in SUGGESTION_CATEGORIES:
        errors.append("Choose a valid Mamba collection category.")
    if len(data["preferences"]) > 255:
        errors.append("Keep preferences under 255 characters.")
    if len(data["price_range"]) > 80:
        errors.append("Keep the price range under 80 characters.")
    if len(data["details"]) > 1000:
        errors.append("Keep additional details under 1000 characters.")
    return errors


def suggestion_ip_hash():
    return hashlib.sha256((request.remote_addr or "unknown").encode()).hexdigest()


def validate_enquiry_payload(data):
    errors = []
    if data["website_url"]:
        errors.append("Please submit the enquiry using the visible fields only.")
    if len(data["product_name"]) < 2:
        errors.append("Enter the product name or item you want.")
    if len(data["message"]) < 3:
        errors.append("Add a short question or request.")
    if data["preferred_contact"] not in {"WhatsApp", "Call", "SMS", "Email"}:
        errors.append("Choose a valid contact method.")
    if data["contact"] and len(data["contact"]) > 120:
        errors.append("Keep contact details under 120 characters.")
    return errors


@public_bp.route("/mamba-suggestion", methods=["POST"])
def mamba_suggestion():
    data = suggestion_form_data()
    errors = validate_suggestion_payload(data)
    ip_hash = suggestion_ip_hash()
    recent_cutoff = datetime.utcnow() - timedelta(hours=1)
    recent_count = ProductSuggestion.query.filter(ProductSuggestion.ip_hash == ip_hash, ProductSuggestion.created_at >= recent_cutoff).count()
    if recent_count >= 5:
        errors.append("Too many suggestions were sent recently. Please try again later.")
    if errors:
        for error in errors:
            flash(error, "error")
        return redirect(url_for("public.home") + "#mamba-suggestions")
    suggestion = ProductSuggestion(
        product_name=data["product_name"][:140],
        category=data["category"],
        preferences=data["preferences"][:255],
        price_range=data["price_range"][:80],
        details=data["details"][:1000],
        ip_hash=ip_hash,
    )
    try:
        db.session.add(suggestion)
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Could not save Mamba Collection suggestion")
        flash("We could not save your suggestion right now. Please try again or use WhatsApp.", "error")
        return redirect(url_for("public.home") + "#mamba-suggestions")
    flash("Thanks. Your Mamba Collection suggestion has been saved for review.", "success")
    return redirect(url_for("public.home") + "#mamba-suggestions")


@public_bp.route("/mamba-suggestion/whatsapp", methods=["POST"])
def mamba_suggestion_whatsapp():
    data = suggestion_form_data()
    errors = validate_suggestion_payload(data)
    if errors:
        for error in errors:
            flash(error, "error")
        return redirect(url_for("public.home") + "#mamba-suggestions")
    message = build_mamba_suggestion_message(data["product_name"], data["category"], data["preferences"], data["price_range"], data["details"])
    return redirect(whatsapp_text_link(current_settings(), message))

@public_bp.route("/product/<slug>/enquiry", methods=["POST"])
def product_enquiry(slug):
    product = Product.query.filter_by(slug=slug, active=True).first_or_404()
    data = {
        "product_name": product.name,
        "size": request.form.get("size", "").strip()[:50],
        "color": request.form.get("color", "").strip()[:50],
        "message": request.form.get("message", "").strip()[:1000],
        "contact": request.form.get("contact", "").strip()[:120],
        "preferred_contact": request.form.get("preferred_contact", "WhatsApp").strip(),
        "website_url": request.form.get("website_url", "").strip(),
        "consent": bool(request.form.get("consent_to_contact")),
    }
    errors = validate_enquiry_payload(data)
    if errors:
        for error in errors:
            flash(error, "error")
        return redirect(url_for("public.product_detail", slug=product.slug))
    enquiry = ProductEnquiry(product=product, product_name=product.name, size=data["size"], color=data["color"], message=data["message"], contact=data["contact"], preferred_contact=data["preferred_contact"], consent_to_contact=data["consent"], ip_hash=suggestion_ip_hash())
    db.session.add(enquiry)
    db.session.commit()
    flash("Your product enquiry has been saved for the owner.", "success")
    return redirect(url_for("public.product_detail", slug=product.slug))


@public_bp.route("/product/<slug>/restock-alert", methods=["POST"])
def restock_alert(slug):
    product = Product.query.filter_by(slug=slug, active=True).first_or_404()
    contact = request.form.get("contact", "").strip()[:120]
    size = request.form.get("size", "").strip()[:50]
    color = request.form.get("color", "").strip()[:50]
    if not contact or not request.form.get("consent_to_contact"):
        flash("Add contact details and consent so FityStores can notify you.", "error")
        return redirect(url_for("public.product_detail", slug=product.slug))
    alert = RestockAlert.query.filter_by(product_id=product.id, contact=contact, status="pending", size=size, color=color).first()
    if alert:
        flash("You already have an open restock alert for this item.", "success")
        return redirect(url_for("public.product_detail", slug=product.slug))
    db.session.add(RestockAlert(product=product, size=size, color=color, contact=contact, preferred_contact=request.form.get("preferred_contact", "WhatsApp"), consent_to_contact=True))
    db.session.commit()
    flash("Restock alert saved. Automated sending is only used when a provider is configured.", "success")
    return redirect(url_for("public.product_detail", slug=product.slug))


def render_tracking_page(order=None):
    return render_template("track_order.html", title="Track order", order=order, tracking_steps=tracking_steps_for(order))


@public_bp.route("/track", methods=["GET", "POST"])
def track_order():
    order = None
    reference = (request.values.get("reference") or "").strip()
    token = (request.values.get("tracking_token") or "").strip()
    if reference or token:
        order = Order.query.filter_by(reference=reference, tracking_token=token).first()
        if not order:
            flash("We could not find an order with that reference and tracking code.", "error")
    return render_tracking_page(order)


@public_bp.route("/track/<reference>/<tracking_token>", methods=["GET", "POST"])
def track_order_link(reference, tracking_token):
    if request.method == "POST":
        return track_order()
    order = Order.query.filter_by(reference=reference, tracking_token=tracking_token).first_or_404()
    return redirect(url_for("public.track_order", reference=order.reference, tracking_token=order.tracking_token))
@public_bp.route("/shop")
def shop():
    query, active_category, q, sort = product_query()
    page = 1
    pagination = query.paginate(page=page, per_page=PRODUCTS_PER_PAGE, error_out=False)
    categories = Category.query.order_by(Category.name).all()
    return render_template("shop.html", title="Shop", products=pagination.items, categories=categories, active_category=active_category, q=q, sort=sort, page=page, has_next=pagination.has_next)

@public_bp.route("/api/products")
def api_products():
    query, _active_category, _q, _sort = product_query()
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    pagination = query.paginate(page=page, per_page=PRODUCTS_PER_PAGE, error_out=False)
    cards = [render_template("partials/product_card.html", product=product) for product in pagination.items]
    return jsonify({
        "page": page,
        "has_next": pagination.has_next,
        "next_page": page + 1 if pagination.has_next else None,
        "count": len(pagination.items),
        "ids": [product.id for product in pagination.items],
        "html": "".join(cards),
        "products": [product_card_payload(product) for product in pagination.items],
    })

@public_bp.route("/product/<slug>")
def product_detail(slug):
    product = product_list_options(Product.query.filter_by(slug=slug, active=True)).first_or_404()
    related = product_list_options(Product.query.filter(Product.active.is_(True), Product.category_id == product.category_id, Product.id != product.id)).limit(4).all()
    return render_template("product_detail.html", title=product.name, product=product, related=related)

@public_bp.route("/cart")
def cart():
    return render_template("cart.html", title="Cart")

@csrf.exempt
@public_bp.post("/api/cart/validate")
def api_validate_cart():
    validation = validate_cart((request.get_json(silent=True) or {}).get("items", []))
    def cart_image(product, variant=None):
        return variant.image if variant and variant.image else product_display_image(product) if product else None

    return jsonify({
        "errors": validation.errors,
        "has_unconfirmed_prices": validation.has_unconfirmed_prices,
        "total_cents": validation.total_cents,
        "lines": [{
            "product_id": line.product.id,
            "name": line.product.name,
            "quantity": line.quantity,
            "price_cents": line.unit_price_cents,
            "subtotal_cents": line.subtotal_cents,
            "stock_quantity": line.stock_quantity,
            "variant_id": line.variant.id if line.variant else None,
            "variant_label": line.variant_label,
            "sku": (line.variant.sku if line.variant and line.variant.sku else line.product.sku) or "",
            "image_url": product_image_url(cart_image(line.product, line.variant)),
            "image_alt": product_image_alt(cart_image(line.product, line.variant), line.product.name),
            "url": url_for("public.product_detail", slug=line.product.slug),
            "restock_url": url_for("public.product_detail", slug=line.product.slug) + "#product-restock",
        } for line in validation.lines],
        "issues": [{
            "product_id": issue.product.id if issue.product else None,
            "name": issue.product.name if issue.product else "Unavailable product",
            "quantity": issue.quantity,
            "stock_quantity": issue.stock_quantity,
            "variant_id": issue.variant.id if issue.variant else None,
            "variant_label": issue.variant_label,
            "sku": (issue.variant.sku if issue.variant and issue.variant.sku else issue.product.sku if issue.product else "") or "",
            "reason": issue.reason,
            "image_url": product_image_url(cart_image(issue.product, issue.variant)) if issue.product else "",
            "image_alt": product_image_alt(cart_image(issue.product, issue.variant), issue.product.name) if issue.product else "Unavailable product",
            "url": url_for("public.product_detail", slug=issue.product.slug) if issue.product else url_for("public.shop"),
            "restock_url": (url_for("public.product_detail", slug=issue.product.slug) + "#product-restock") if issue.product else url_for("public.contact"),
        } for issue in validation.issues]
    })

@public_bp.route("/checkout", methods=["GET", "POST"])
def checkout():
    settings = current_settings()
    if request.method == "POST":
        try:
            raw_items = request.get_json(silent=True) if request.is_json else {"items": json.loads(request.form.get("cart_json", "[]") or "[]")}
        except json.JSONDecodeError:
            raw_items = {"items": []}
        proof_filename = save_payment_proof(request.files.get("payment_proof"))
        order, validation = create_checkout_order(
            raw_items.get("items", []),
            request.form.get("customer_name", ""),
            request.form.get("customer_phone", ""),
            request.form.get("fulfilment_method", "Collection"),
            request.form.get("instructions", ""),
            request.form.get("payment_reference", ""),
            proof_filename,
            session.get("referral_code", ""),
        )
        if not order:
            flash("Your cart could not be checked out. Please review the items and stock levels.", "error")
            return redirect(url_for("public.cart"))
        if order.referral_code:
            session["referral_code"] = order.referral_code
        link = whatsapp_link(order, settings)
        return render_template("checkout_ready.html", title="Open WhatsApp", order=order, validation=validation, whatsapp_url=link)
    return render_template("checkout.html", title="Checkout", whatsapp_ready=bool(settings.whatsapp_number))

@public_bp.route("/contact")
def contact():
    return render_template("contact.html", title="Contact and delivery")

@public_bp.route("/about")
def about():
    return render_template("about.html", title="About FityStores")

@public_bp.route("/reviews", methods=["GET", "POST"])
def reviews():
    products = Product.query.filter_by(active=True).order_by(Product.name).all()
    if request.method == "POST":
        rating = int(request.form.get("rating", 0) or 0)
        if not 1 <= rating <= 5:
            flash("Choose a star rating from 1 to 5.", "error")
        else:
            ip_hash = hashlib.sha256((request.remote_addr or "unknown").encode()).hexdigest()
            review = Review(
                display_name=request.form.get("display_name", "Customer").strip()[:80] or "Customer",
                rating=rating,
                text=request.form.get("text", "").strip()[:1000],
                product_id=request.form.get("product_id") or None,
                ip_hash=ip_hash,
            )
            db.session.add(review)
            db.session.commit()
            flash("Thanks. Your review will appear after the owner approves it.", "success")
            return redirect(url_for("public.reviews"))
    approved = Review.query.filter_by(status="approved").order_by(Review.created_at.desc()).all()
    return render_template("reviews.html", title="Reviews", reviews=approved, products=products)

@public_bp.route("/robots.txt")
def robots():
    return Response("User-agent: *\nDisallow: /admin\nSitemap: " + url_for("public.sitemap", _external=True) + "\n", mimetype="text/plain")

@public_bp.route("/sitemap.xml")
def sitemap():
    urls = [url_for("public.home", _external=True), url_for("public.shop", _external=True), url_for("public.upcoming", _external=True), url_for("public.contact", _external=True), url_for("public.about", _external=True), url_for("public.reviews", _external=True)]
    urls += [url_for("public.product_detail", slug=p.slug, _external=True) for p in Product.query.filter_by(active=True).all()]
    body = "<?xml version='1.0' encoding='UTF-8'?><urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>" + "".join(f"<url><loc>{u}</loc></url>" for u in urls) + "</urlset>"
    return Response(body, mimetype="application/xml")









