import hashlib
import hmac
import json
from secrets import token_urlsafe
import re
from urllib.parse import quote
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4
from flask import Blueprint, Response, current_app, flash, jsonify, make_response, redirect, render_template, request, session, url_for
from sqlalchemy import func, or_
from sqlalchemy.orm import joinedload, selectinload
from xml.sax.saxutils import escape
from app.extensions import db, csrf
from app.models import CartAddCount, Category, CategoryThumbnail, CustomerAccount, CustomerCart, CustomerConsentEvent, CustomerOrderLink, CustomerProfile, EmailCampaignRecipient, EmailMessage, LoyaltyTransaction, Order, OrderItem, Product, ProductEnquiry, ProductSuggestion, ProductVariant, ReferralClick, RestockAlert, Review
from app.services.cart import validate_cart
from app.services.orders import create_checkout_order
from app.services.settings import current_settings
from app.services.mamba_collection import mamba_products_query
from app.services.category_thumbnails import CATEGORY_GROUPS, CATEGORY_GROUPS_BY_KEY, category_group_key, category_thumbnails_schema_ready
from app.services.whatsapp import LOCAL_CONTACT_NUMBER, build_general_message, build_mamba_suggestion_message, product_whatsapp_link, whatsapp_link, whatsapp_text_link
from app.services.product_images import product_display_image, product_image_alt, product_image_url, valid_product_image
from app.services.referrals import get_or_create_profile, normalize_phone, record_referral_click
from werkzeug.utils import secure_filename
from flask_login import current_user, logout_user

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
    footer_candidates = product_list_options(Product.query.filter_by(active=True)).order_by(
        Product.featured.desc(), Product.updated_at.desc(), Product.id.desc()
    ).limit(60).all()
    footwear_terms = ("shoe", "sneaker", "footwear", "sandal", "slipper", "boot")
    footer_products = [
        product for product in footer_candidates
        if not any(term in " ".join((product.name or "", product.category.name if product.category else "")).lower() for term in footwear_terms)
    ]
    footer_product_groups = [
        footer_products[index:index + 3]
        for index in range(0, min(len(footer_products), 18), 3)
        if len(footer_products[index:index + 3]) == 3
    ]
    return {"settings": current_settings(), "money": money, "split_options": split_options, "product_display_image": product_display_image, "product_image_url": product_image_url, "product_image_alt": product_image_alt, "valid_product_image": valid_product_image, "product_whatsapp_link": product_whatsapp_link, "whatsapp_text_link": whatsapp_text_link, "build_general_message": build_general_message, "local_contact_number": LOCAL_CONTACT_NUMBER, "footer_products": footer_products[:3], "footer_product_groups": footer_product_groups, "current_year": datetime.now().year}

def product_query():
    q = request.args.get("q", "").strip()
    category_slug = request.args.get("category", "").strip()
    category_group = CATEGORY_GROUPS_BY_KEY.get(request.args.get("category_group", "").strip())
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
    elif category_group:
        category_ids = [category.id for category in Category.query.all() if category_group_key(category.name) == category_group["key"]]
        query = query.filter(Product.category_id.in_(category_ids or [-1]))
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
    fresh_arrivals = product_list_options(Product.query.filter_by(active=True)).order_by(Product.created_at.desc(), Product.id.desc()).limit(8).all()
    used_product_ids = {product.id for product in fresh_arrivals}

    discounted_query = Product.query.filter(
        Product.active.is_(True),
        Product.previous_price_cents.isnot(None),
        Product.price_cents.isnot(None),
        Product.previous_price_cents > Product.price_cents,
        Product.id.notin_(used_product_ids),
    )
    discounted = product_list_options(discounted_query).order_by(Product.updated_at.desc(), Product.id.desc()).limit(8).all()
    used_product_ids.update(product.id for product in discounted)

    featured_query = Product.query.filter_by(active=True, featured=True).filter(Product.id.notin_(used_product_ids))
    featured = product_list_options(featured_query).order_by(Product.updated_at.desc(), Product.id.desc()).limit(8).all()
    used_product_ids.update(product.id for product in featured)

    top_added = [product for product in product_list_options(Product.query.join(CartAddCount, CartAddCount.product_id == Product.id).filter(Product.active.is_(True)).order_by(CartAddCount.add_count.desc(), Product.name.asc())).all() if product.display_stock_quantity > 0][:8]

    categories = Category.query.join(Product).filter(Product.active.is_(True)).group_by(Category.id).order_by(Category.name).all()
    category_sections = []
    category_thumbnails_ready = category_thumbnails_schema_ready()
    thumbnails = {
        thumbnail.group_key: thumbnail
        for thumbnail in (CategoryThumbnail.query.all() if category_thumbnails_ready else [])
    }
    for group in CATEGORY_GROUPS:
        category_ids = [category.id for category in categories if category_group_key(category.name) == group["key"]]
        products = product_list_options(
            Product.query.filter(Product.active.is_(True), Product.category_id.in_(category_ids or [-1]))
        ).order_by(Product.updated_at.desc(), Product.id.desc()).limit(4).all()
        thumbnail = thumbnails.get(group["key"])
        if not valid_product_image(thumbnail):
            thumbnail = product_display_image(products[0]) if products else None
        thumbnail_url = product_image_url(thumbnail) if thumbnail else url_for("static", filename=group["default_image"])
        thumbnail_alt = product_image_alt(thumbnail, group["name"]) if thumbnail else group["name"]
        category_sections.append({
            "group": group,
            "products": products,
            "thumbnail_url": thumbnail_url,
            "thumbnail_alt": thumbnail_alt,
        })

    # Keep existing catalogue behavior until an admin saves a curated list.
    mamba_query, mamba_collection_configured = mamba_products_query()
    mamba_products = product_list_options(mamba_query).all()
    mamba_category_ids = {product.category_id for product in mamba_products}
    mamba_categories = [category for category in categories if category.id in mamba_category_ids]
    mamba_filter_order = {"boxers": 0, "shoes": 1, "vests": 2, "ankle socks": 3, "socks": 3}
    mamba_categories.sort(key=lambda category: (mamba_filter_order.get(category.name.strip().lower(), 4), category.name.lower()))
    home_share_specs = {
        "home": ("Everyday essentials. Fits poa.", "Shop everyday essentials with clear prices and quick WhatsApp support.", "SHOP NOW"),
        "featured": ("Fresh picks, fair prices.", "Explore featured FityStores products selected for everyday wear.", "VIEW PICKS"),
        "popular": ("Shopper favourites.", "See the products shoppers are adding to their carts right now.", "SHOP FAVOURITES"),
        "categories": ("Find your everyday fit.", "Explore shoes, socks, hair accessories, and men's essentials.", "BROWSE CATEGORIES"),
        "offers": ("Good finds at reduced prices.", "Browse current FityStores offers and product details.", "VIEW OFFERS"),
        "mamba": ("Your style. Your choice.", "Explore the Mamba collection and available men's essentials.", "EXPLORE MAMBA"),
        "upcoming": ("See what's coming next.", "Browse upcoming merchandise and tell us what you would like stocked.", "SEE WHAT'S NEXT"),
    }
    requested_share = request.args.get("share", "home")
    if requested_share not in home_share_specs:
        requested_share = "home"
    section_title, section_description, _section_button = home_share_specs[requested_share]
    share_product_queries = {
        "home": product_list_options(Product.query.filter_by(active=True)).order_by(Product.created_at.desc(), Product.id.desc()),
        "featured": product_list_options(Product.query.filter_by(active=True, featured=True)).order_by(Product.updated_at.desc(), Product.id.desc()),
        "popular": product_list_options(Product.query.join(CartAddCount, CartAddCount.product_id == Product.id).filter(Product.active.is_(True)).order_by(CartAddCount.add_count.desc(), Product.name.asc())),
        "categories": product_list_options(Product.query.filter_by(active=True)).order_by(Product.category_id, Product.updated_at.desc(), Product.id.desc()),
        "offers": product_list_options(Product.query.filter(Product.active.is_(True), Product.previous_price_cents.isnot(None), Product.price_cents.isnot(None), Product.previous_price_cents > Product.price_cents)).order_by(Product.updated_at.desc(), Product.id.desc()),
        "mamba": product_list_options(mamba_products_query()[0]),
    }
    share_product = share_product_queries.get(requested_share, share_product_queries["home"]).first()
    home_og_image = url_for("public.home_share_card", section=requested_share, _external=True)
    public_origin = (current_app.config.get("PUBLIC_BASE_URL") or "https://fitystore.top").rstrip("/")
    home_canonical = public_origin + "/" if requested_share == "home" and "share" not in request.args else public_origin + url_for("public.home", share=requested_share)
    home_share_urls = {
        key: url_for("public.home", share=key, _anchor={"featured": "featured-picks", "popular": "most-added", "categories": "category-showcase", "offers": "discounted-goods", "mamba": "mamba-collection", "upcoming": "upcoming-strip"}.get(key, ""), _external=True)
        for key in home_share_specs if key != "home"
    }
    return render_template(
        "home.html", title="FityStores essentials", seo_title=section_title + " | FityStores",
        seo_description=section_description, canonical_url=home_canonical, og_image=home_og_image,
        og_image_width=1200, og_image_height=630, og_image_type="image/png", og_image_alt=section_title + " at FityStores",
        fresh_arrivals=fresh_arrivals, featured=featured, top_added=top_added, discounted=discounted,
        category_sections=category_sections, mamba_products=mamba_products, mamba_categories=mamba_categories,
        mamba_collection_configured=mamba_collection_configured,
        suggestion_categories=SUGGESTION_CATEGORIES, upcoming_merchandise=UPCOMING_MERCHANDISE,
        home_share_urls=home_share_urls, requested_share=requested_share,
    )


@public_bp.route("/share/home/<section>/social.png")
def home_social_card(section):
    from app.services.share_cards import render_social_post_image
    from app.services.product_images import legacy_local_image_path
    from app.services.formatting import money
    from app.models import CartAddCount
    specs={
      "home":("Everyday essentials. Fits poa.","Shop everyday essentials with clear prices and quick WhatsApp support.","SHOP NOW"),
      "featured":("Fresh picks, fair prices.","Explore featured FityStores products selected for everyday wear.","VIEW PICKS"),
      "popular":("Shopper favourites.","See products shoppers are adding to their carts right now.","SHOP FAVOURITES"),
      "categories":("Find your everyday fit.","Explore shoes, socks, hair accessories, and men's essentials.","BROWSE CATEGORIES"),
      "offers":("Good finds at reduced prices.","Browse current FityStores offers and product details.","VIEW OFFERS"),
      "mamba":("Your style. Your choice.","Explore the Mamba collection and men's everyday essentials.","EXPLORE MAMBA"),
      "upcoming":("See what's coming next.","Browse upcoming merchandise and tell us what you would like stocked.","SEE WHAT'S NEXT"),
    }
    if section not in specs:return jsonify({"error":"Not found"}),404
    q=Product.query.filter_by(active=True)
    if section=="featured":q=q.filter(Product.featured.is_(True))
    elif section=="popular":q=q.join(CartAddCount,CartAddCount.product_id==Product.id).order_by(CartAddCount.add_count.desc())
    elif section=="offers":q=q.filter(Product.previous_price_cents.isnot(None),Product.price_cents.isnot(None),Product.previous_price_cents>Product.price_cents)
    elif section=="mamba":q=mamba_products_query()[0]
    product=product_list_options(q).order_by(Product.updated_at.desc(),Product.id.desc()).first()
    image=product_display_image(product) if product else None
    image_url=product_image_url(image) if image else ""
    image_path=legacy_local_image_path(image) if image and not getattr(image,"cloudinary_public_id","") else None
    if not image_path and image_url.startswith("/static/"):
        candidate=(Path(current_app.static_folder)/image_url.removeprefix("/static/")).resolve()
        if Path(current_app.static_folder).resolve() in candidate.parents and candidate.is_file():image_path=candidate
    if not image_path and not image_url:image_path=Path(current_app.static_folder)/"images"/"shop-hero"/"hair_bonnet-removebg-preview.webp"
    title,description,button=specs[section]
    price=money(product.price_cents,current_settings().currency) if product and product.price_cents is not None else ""
    card=render_social_post_image(title,description,image_path,image_url,price,button)
    return Response(card,mimetype="image/png",headers={"Cache-Control":"public, max-age=3600","Content-Length":str(len(card))})


@public_bp.route("/share/home/<section>.png")
def home_share_card(section):
    from app.services.share_cards import render_promo_share_card
    from app.services.product_images import legacy_local_image_path
    from app.models import CartAddCount

    specs = {
        "home": ("Everyday essentials. Fits poa.", "Shop everyday essentials with clear prices and quick WhatsApp support.", "SHOP NOW"),
        "featured": ("Fresh picks, fair prices.", "Explore featured FityStores products selected for everyday wear.", "VIEW PICKS"),
        "popular": ("Shopper favourites.", "See the products shoppers are adding to their carts right now.", "SHOP FAVOURITES"),
        "categories": ("Find your everyday fit.", "Explore shoes, socks, hair accessories, and men's essentials.", "BROWSE CATEGORIES"),
        "offers": ("Good finds at reduced prices.", "Browse current FityStores offers and product details.", "VIEW OFFERS"),
        "mamba": ("Your style. Your choice.", "Explore the Mamba collection and available men's essentials.", "EXPLORE MAMBA"),
        "upcoming": ("See what's coming next.", "Browse upcoming merchandise and tell us what you would like stocked.", "SEE WHAT'S NEXT"),
    }
    if section not in specs:
        return jsonify({"error": "Not found"}), 404
    queries = {
        "home": product_list_options(Product.query.filter_by(active=True)).order_by(Product.created_at.desc(), Product.id.desc()),
        "featured": product_list_options(Product.query.filter_by(active=True, featured=True)).order_by(Product.updated_at.desc(), Product.id.desc()),
        "popular": product_list_options(Product.query.join(CartAddCount, CartAddCount.product_id == Product.id).filter(Product.active.is_(True)).order_by(CartAddCount.add_count.desc(), Product.name.asc())),
        "categories": product_list_options(Product.query.filter_by(active=True)).order_by(Product.category_id, Product.updated_at.desc(), Product.id.desc()),
        "offers": product_list_options(Product.query.filter(Product.active.is_(True), Product.previous_price_cents.isnot(None), Product.price_cents.isnot(None), Product.previous_price_cents > Product.price_cents)).order_by(Product.updated_at.desc(), Product.id.desc()),
        "mamba": product_list_options(mamba_products_query()[0]),
    }
    product = queries.get(section, queries["home"]).first()
    image = product_display_image(product) if product else None
    image_path = legacy_local_image_path(image) if image else None
    image_url = product_image_url(image) if image else ""
    if not image_path and not image_url:
        image_path = Path(current_app.static_folder) / "images" / "shop-hero" / "hair_bonnet-removebg-preview.webp"
    title_text, description, button_text = specs[section]
    image_bytes = render_promo_share_card(title_text, description, image_path, image_url, button_text)
    return Response(image_bytes, mimetype="image/png", headers={"Cache-Control": "public, max-age=3600", "Content-Length": str(len(image_bytes))})


@public_bp.route("/referrals", methods=["GET", "POST"])
def referrals():
    if not current_user.is_authenticated or not isinstance(current_user._get_current_object(), CustomerAccount):
        flash("Sign in or create a customer account to access rewards.", "warning")
        return redirect(url_for("customer.login", next=url_for("public.referrals")))
    customer_account = current_user._get_current_object()
    if not customer_account.active:
        flash("This customer account is unavailable. Contact FityStores for help.", "error")
        return redirect(url_for("customer.login"))

    settings = current_settings()
    profile = CustomerProfile.query.filter_by(account_id=customer_account.id).first()
    if request.method == "POST":
        phone = request.form.get("phone", "").strip()
        try:
            profile = get_or_create_profile(
                phone,
                customer_account.display_name,
                session.get("referral_code", ""),
                account_id=customer_account.id,
            )
            db.session.commit()
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "error")
            return redirect(url_for("public.referrals"))
        flash("Your account rewards profile is ready. Share your referral link to earn points.", "success")
        return redirect(url_for("public.referrals"))

    share_url = url_for("public.referral_visit", code=profile.referral_code, _external=True) if profile else ""
    click_count = ReferralClick.query.filter_by(referral_code=profile.referral_code).count() if profile else 0
    rewarded_clicks = ReferralClick.query.filter_by(referral_code=profile.referral_code, rewarded=True).count() if profile else 0
    customer_keys = {profile.phone.strip().lower()} if profile else set()
    account_orders = db.session.query(Order.customer_phone, Order.customer_name).join(
        CustomerOrderLink, CustomerOrderLink.order_id == Order.id
    ).filter(CustomerOrderLink.account_id == customer_account.id).all()
    customer_keys.update((phone or name or "").strip().lower() for phone, name in account_orders)
    points = 0
    if customer_keys:
        points = db.session.query(func.coalesce(func.sum(LoyaltyTransaction.points), 0)).filter(
            LoyaltyTransaction.customer_key.in_(customer_keys),
            LoyaltyTransaction.status == "posted",
        ).scalar() or 0
    return render_template("referrals.html", title="Rewards", profile=profile, share_url=share_url, click_count=click_count, rewarded_clicks=rewarded_clicks, points=points, referral_settings=settings, customer_account=customer_account)

@public_bp.route("/r/<code>")
def referral_visit(code):
    settings = current_settings()
    current_phone = session.get("referral_phone", "")
    if current_user.is_authenticated and isinstance(current_user._get_current_object(), CustomerAccount):
        owned_profile = CustomerProfile.query.filter_by(account_id=current_user.id).first()
        if owned_profile:
            current_phone = owned_profile.phone
    profile, rewarded, message = record_referral_click(code, request, settings, current_phone)
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
    active_category_group = CATEGORY_GROUPS_BY_KEY.get(request.args.get("category_group", "").strip())
    page = 1
    pagination = query.paginate(page=page, per_page=PRODUCTS_PER_PAGE, error_out=False)
    categories = Category.query.order_by(Category.name).all()
    active_title = active_category.name if active_category else (active_category_group["name"] if active_category_group else "Shop products")
    return render_template("shop.html", title="Shop", seo_title=active_title + " | Shop at FityStores", seo_description=("Browse available " + active_title + " products, check current listing details, and contact FityStores with questions."), products=pagination.items, categories=categories, category_groups=CATEGORY_GROUPS, active_category=active_category, active_category_group=active_category_group, q=q, sort=sort, page=page, has_next=pagination.has_next)

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
    product_description = re.sub(r"<[^>]+>", " ", product.description or "")
    product_description = " ".join(product_description.split())[:300] or ("View details and current availability for " + product.name + " at FityStores.")
    image_url = product_image_url(product_display_image(product))
    if image_url and image_url.startswith("/"):
        image_url = "https://fitystore.top" + image_url
    from app.services.formatting import money
    active_variants = [variant for variant in product.variants if variant.active]
    variant_prices = [variant.effective_price_cents for variant in active_variants if variant.effective_price_cents is not None]
    if variant_prices:
        low_price, high_price = min(variant_prices), max(variant_prices)
        share_price = ("Price: " + money(low_price, current_settings().currency)) if low_price == high_price else ("Options: " + money(low_price, current_settings().currency) + " - " + money(high_price, current_settings().currency))
    elif product.price_cents is not None:
        share_price = "Price: " + money(product.price_cents, current_settings().currency)
    else:
        share_price = "Contact the store for the current price"
    share_url = "https://fitystore.top" + request.path
    share_message = f"Check out {product.name} at FityStores! {share_price}. View it here: {share_url}"
    seo_description = (product_description + " " + share_price + ".")[:300]
    whatsapp_share_url = "https://wa.me/?text=" + quote(share_message)
    card_url = "https://fitystore.top" + url_for("public.product_share_card", slug=product.slug)
    return render_template("product_detail.html", title=product.name, seo_title=product.name + " | FityStores", seo_description=seo_description, og_type="product", og_image=card_url, og_image_width=1200, og_image_height=630, og_image_type="image/png", og_image_alt=product.name + " at FityStores ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â " + share_price, share_url=share_url, share_message=share_message, whatsapp_share_url=whatsapp_share_url, product=product, related=related)

@public_bp.route("/share/product/<slug>/social.png")
def product_social_card(slug):
    from app.services.share_cards import render_social_post_image
    from app.services.product_images import legacy_local_image_path
    from app.services.formatting import money
    product=product_list_options(Product.query.filter_by(slug=slug,active=True)).first_or_404()
    image=product_display_image(product);image_url=product_image_url(image) if image else ""
    image_path=legacy_local_image_path(image) if image and not getattr(image,"cloudinary_public_id","") else None
    if not image_path and image_url.startswith("/static/"):
        candidate=(Path(current_app.static_folder)/image_url.removeprefix("/static/")).resolve()
        if Path(current_app.static_folder).resolve() in candidate.parents and candidate.is_file():image_path=candidate
    desc=" ".join(re.sub(r"<[^>]+>"," ",product.description or "").split())[:150]
    price=money(product.price_cents,current_settings().currency) if product.price_cents is not None else ""
    card=render_social_post_image(product.name,desc,image_path,image_url,price,"VIEW PRODUCT")
    return Response(card,mimetype="image/png",headers={"Cache-Control":"public, max-age=3600","Content-Length":str(len(card))})


@public_bp.route("/share/product/<slug>.png")
def product_share_card(slug):
    product = product_list_options(Product.query.filter_by(slug=slug, active=True)).first_or_404()
    from app.services.formatting import money
    active_variants = [variant for variant in product.variants if variant.active and variant.effective_price_cents is not None]
    prices = [variant.effective_price_cents for variant in active_variants]
    if prices:
        low, high = min(prices), max(prices)
    elif product.price_cents is not None:
        low = high = product.price_cents
    else:
        low = high = None
    price_label = (money(low, current_settings().currency) if low == high else money(low, current_settings().currency) + " ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Â¦ÃƒÂ¢Ã¢â€šÂ¬Ã…â€œ " + money(high, current_settings().currency)) if low is not None else "Ask us for today's price"
    image = product_display_image(product)
    image_url = product_image_url(image)
    image_path = None
    if image and not getattr(image, "cloudinary_public_id", ""):
        from app.services.product_images import legacy_local_image_path
        image_path = legacy_local_image_path(image)
    if not image_path and image_url.startswith("/static/"):
        local = (Path(current_app.static_folder) / image_url.removeprefix("/static/")).resolve()
        if Path(current_app.static_folder).resolve() in local.parents and local.is_file():
            image_path = local
    category_name = product.category.name if product.category else "Shop FityStores"
    revision = "|".join(
        [str(product.updated_at or ""), str(getattr(image, "updated_at", "") or "")]
        + [str(variant.updated_at or "") for variant in product.variants]
    )
    from app.services.share_cards import cached_product_share_card
    cache_dir = Path(current_app.instance_path) / "cache" / "product-share-cards"
    card = cached_product_share_card(
        product.name, category_name, price_label, image_path, image_url, cache_dir, revision
    )
    return Response(card, mimetype="image/png", headers={"Cache-Control": "no-store, max-age=0, must-revalidate", "Pragma": "no-cache", "Expires": "0", "Content-Length": str(len(card))})
@public_bp.route("/cart")
def cart():
    return render_template("cart.html", title="Cart")

@public_bp.post("/api/cart/add")
def api_record_cart_add():
    payload = request.get_json(silent=True) or {}
    try:
        product_id = int(payload.get("product_id"))
        quantity = max(1, min(99, int(payload.get("quantity", 1))))
    except (TypeError, ValueError):
        return jsonify({"ok": False}), 400
    product = Product.query.filter_by(id=product_id, active=True).first()
    if not product:
        return jsonify({"ok": False}), 404
    counter = CartAddCount.query.get(product_id)
    if counter:
        counter.add_count += quantity
    else:
        db.session.add(CartAddCount(product_id=product_id, add_count=quantity))
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Could not record cart addition")
        return jsonify({"ok": False}), 503
    return jsonify({"ok": True})


@public_bp.post("/api/cart/snapshot")
def api_sync_customer_cart():
    account=current_user._get_current_object()
    if not isinstance(account,CustomerAccount) or not account.active or not account.email_verified_at:
        return jsonify({"ok":False,"reason":"sign_in_required"}),401
    payload=request.get_json(silent=True) or {}
    now=datetime.utcnow()
    cart=CustomerCart.query.filter_by(account_id=account.id).first()
    if not cart:
        cart=CustomerCart(account_id=account.id,items_json="[]",episode_key=token_urlsafe(24))
        db.session.add(cart)
    action=payload.get("action","update")
    if action=="checkout":
        cart.checkout_until=now+timedelta(minutes=30)
        db.session.commit()
        return jsonify({"ok":True})
    if action=="clear":
        cart.items_json="[]";cart.checkout_until=None;cart.reminder_sent_at=None
        cart.episode_key=token_urlsafe(24);cart.updated_at=now
        db.session.commit()
        return jsonify({"ok":True})
    normalized=[]
    for item in (payload.get("items") or [])[:40]:
        try:
            product_id=int(item.get("product_id") or item.get("id"))
            quantity=max(1,min(99,int(item.get("quantity",1))))
            variant_id=int(item.get("variant_id")) if item.get("variant_id") else None
        except (AttributeError,TypeError,ValueError):
            continue
        if not Product.query.filter_by(id=product_id,active=True).first():continue
        if variant_id and not ProductVariant.query.filter_by(id=variant_id,product_id=product_id,active=True).first():continue
        if item.get("selected") is False:continue
        normalized.append({"product_id":product_id,"variant_id":variant_id,"quantity":quantity})
    encoded=json.dumps(normalized,separators=(",",":"),sort_keys=True)
    if encoded!=cart.items_json:
        cart.items_json=encoded;cart.updated_at=now;cart.reminder_sent_at=None;cart.episode_key=token_urlsafe(24)
    cart.checkout_until=None
    db.session.commit()
    return jsonify({"ok":True})


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
    account = current_user._get_current_object() if current_user.is_authenticated else None
    if not isinstance(account, CustomerAccount) or not account.active or not account.email_verified_at:
        if isinstance(account, CustomerAccount):
            logout_user()
        flash("Sign in with a verified customer account to place your order. New customers can create an account first.", "info")
        return redirect(url_for("customer.login", next=url_for("public.checkout")))
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
            "\n".join(filter(None, [request.form.get("instructions", "").strip(), "M-Pesa payer: " + request.form.get("mpesa_payer_name", "").strip(), "M-Pesa payment phone: " + request.form.get("mpesa_payer_phone", "").strip(), "Delivery address: " + request.form.get("delivery_address", "").strip()])),
            request.form.get("payment_reference", ""),
            proof_filename,
            session.get("referral_code", ""),
            customer_account_id=current_user.id if isinstance(current_user._get_current_object(), CustomerAccount) and current_user.email_verified_at else None,
        )
        if not order:
            flash("Your cart could not be checked out. Please review the items and stock levels.", "error")
            return redirect(url_for("public.cart"))
        if isinstance(current_user._get_current_object(), CustomerAccount):
            saved_cart = CustomerCart.query.filter_by(account_id=current_user.id).first()
            if saved_cart:
                saved_cart.items_json = "[]"
                saved_cart.checkout_until = None
                saved_cart.reminder_sent_at = None
                saved_cart.episode_key = token_urlsafe(24)
                db.session.commit()
        if order.referral_code:
            session["referral_code"] = order.referral_code
        if isinstance(current_user._get_current_object(), CustomerAccount) and current_user.email_verified_at:
            try:
                from app.services.customer_mail import notify_order_event
                notify_order_event(order, "order_received", f"order-received:{order.id}")
            except Exception:
                current_app.logger.error("Order notification could not be prepared for order %s", order.id)
        link = whatsapp_link(order, settings)
        return render_template("checkout_ready.html", title="Open WhatsApp", order=order, validation=validation, whatsapp_url=link)
    return render_template("checkout.html", title="Checkout", whatsapp_ready=bool(settings.whatsapp_number))

@csrf.exempt
@public_bp.post("/webhooks/brevo/email-events")
def brevo_email_events():
    configured=(current_app.config.get("BREVO_WEBHOOK_TOKEN") or "").strip()
    supplied=(request.headers.get("X-Fity-Brevo-Token") or "").strip()
    if not configured or not hmac.compare_digest(configured,supplied):return jsonify({"ok":False}),403
    from sqlalchemy import inspect
    try:
        inspector = inspect(db.engine)
        required_models = (EmailMessage, CustomerAccount, CustomerConsentEvent, EmailCampaignRecipient)
        for model in required_models:
            table_name = model.__tablename__
            if not inspector.has_table(table_name):
                return jsonify({"ok": False, "error": "email schema unavailable"}), 503
            actual_columns = {column["name"] for column in inspector.get_columns(table_name)}
            expected_columns = {column.name for column in model.__table__.columns}
            if not expected_columns <= actual_columns:
                return jsonify({"ok": False, "error": "email schema unavailable"}), 503
    except Exception:
        db.session.rollback()
        return jsonify({"ok": False, "error": "email schema unavailable"}), 503
    payload=request.get_json(silent=True) or {};events=payload if isinstance(payload,list) else [payload];suppressed=0
    for event in events[:100]:
        if not isinstance(event,dict):continue
        event_name=str(event.get("event") or event.get("type") or "").strip().lower().replace("-", "_")
        if event_name not in {"delivered","hardbounce","hard_bounce","softbounce","soft_bounce","spam","complaint","invalid","unsubscribed"}:continue
        provider_id=str(event.get("message-id") or event.get("messageId") or event.get("message_id") or "").strip()[:255]
        message=EmailMessage.query.filter_by(provider_message_id=provider_id).first() if provider_id else None
        new_message_status=None
        if event_name=="delivered" and message and message.status=="sent":
            new_message_status="delivered"
        elif event_name in {"hardbounce","hard_bounce","softbounce","soft_bounce","invalid"} and message and message.status=="sent":
            new_message_status="bounced"
        elif event_name in {"spam","complaint"} and message and message.status=="sent":
            new_message_status="complained"
        if new_message_status and message:
            message.status=new_message_status
            message.failure_reason="" if new_message_status=="delivered" else "Brevo reported a " + new_message_status + "."
            if message.campaign_id and message.account_id:
                campaign_recipient=EmailCampaignRecipient.query.filter_by(
                    campaign_id=message.campaign_id,account_id=message.account_id
                ).first()
                if campaign_recipient and campaign_recipient.status in {"sent","delivered"}:
                    campaign_recipient.status=new_message_status
        if event_name=="delivered":
            continue
        address=str(event.get("email") or event.get("recipient") or (message.recipient if message else "")).strip().lower()[:254]
        if not address:continue
        account=CustomerAccount.query.filter(db.func.lower(CustomerAccount.email)==address).first()
        if not account:continue
        if event_name=="unsubscribed":
            target_status="unsubscribed";consent_event="unsubscribed"
        elif event_name in {"hardbounce","hard_bounce","spam","complaint","invalid"}:
            target_status="suppressed";consent_event="suppressed"
        else:
            target_status=None;consent_event=None
        if target_status and account.marketing_status != target_status:
            account.marketing_status=target_status;account.marketing_withdrawn_at=datetime.utcnow()
            already_recorded=CustomerConsentEvent.query.filter_by(
                account_id=account.id,event=consent_event,method="brevo_webhook"
            ).first()
            if not already_recorded:
                db.session.add(CustomerConsentEvent(account_id=account.id,event=consent_event,method="brevo_webhook",wording_version="2026-10"))
            if target_status=="suppressed":suppressed+=1
    db.session.commit();return jsonify({"ok":True,"suppressed":suppressed})


@public_bp.route("/contact")
def contact():
    return render_template("contact.html", title="Contact FityStores", seo_title="Contact FityStores | Customer Support", seo_description="Contact FityStores about products, orders, payment instructions, delivery, or collection.")

@public_bp.route("/about")
def about():
    # Keep About imagery tied to active catalogue records. Eager loading here
    # mirrors the shop route and keeps the shared product cards from issuing
    # per-product image or variant queries.
    active_products = Product.query.filter(Product.active.is_(True))
    featured_products = product_list_options(
        active_products.order_by(Product.featured.desc(), Product.created_at.desc(), Product.id.desc())
    ).limit(4).all()
    categories = (
        Category.query.filter(Category.products.any(Product.active.is_(True)))
        .order_by(Category.name.asc())
        .all()
    )
    category_products = product_list_options(
        active_products.order_by(Product.featured.desc(), Product.created_at.desc(), Product.id.desc())
    ).all()
    category_images = {}
    for product in category_products:
        if product.category_id not in category_images and product_display_image(product):
            category_images[product.category_id] = product
    response = make_response(render_template(
        "about.html",
        title="About FityStores",
        seo_title="About FityStores",
        seo_description="Meet FityStores, browse current products, and find out how to order or get help.",
        featured_products=featured_products,
        categories=categories,
        category_images=category_images,
    ))
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response

@public_bp.route("/reviews", methods=["GET", "POST"])
def reviews():
    products = Product.query.filter_by(active=True).order_by(Product.name).all()
    account = current_user._get_current_object() if current_user.is_authenticated else None
    customer_account = account if isinstance(account, CustomerAccount) else None
    form_values = {}

    if request.method == "POST":
        form_values = request.form
        if request.form.get("website_url", "").strip():
            flash("We could not accept that review. Please try again.", "error")
        else:
            try:
                rating = int(request.form.get("rating", ""))
            except (TypeError, ValueError):
                rating = 0
            title = request.form.get("title", "").strip()
            review_text = request.form.get("text", "").strip()
            raw_product_id = request.form.get("product_id", "").strip()
            product = None
            invalid_product = False
            if raw_product_id:
                try:
                    product = Product.query.filter_by(id=int(raw_product_id), active=True).first()
                except (TypeError, ValueError):
                    product = None
                invalid_product = product is None

            display_name = (
                (customer_account.display_name or "").strip() or "Customer"
                if customer_account
                else request.form.get("display_name", "").strip()
            )
            errors = []
            if not 1 <= rating <= 5:
                errors.append("Choose a star rating from 1 to 5.")
            if not display_name:
                errors.append("Add a display name for your review.")
            if not title:
                errors.append("Add a short title for your review.")
            elif len(title) > 120:
                errors.append("Keep the review title to 120 characters or fewer.")
            if not review_text:
                errors.append("Write a few words about your experience.")
            elif len(review_text) > 1000:
                errors.append("Keep your review to 1000 characters or fewer.")
            if invalid_product:
                errors.append("That product is no longer available for review. Choose another product or a general review.")

            if customer_account and product:
                duplicate = Review.query.filter_by(
                    customer_account_id=customer_account.id,
                    product_id=product.id,
                ).filter(Review.status.in_(["pending", "approved"])).first()
                if duplicate:
                    errors.append("You have already submitted a review for this product.")

            if errors:
                for error in errors:
                    flash(error, "error")
            else:
                ip_hash = hashlib.sha256((request.remote_addr or "unknown").encode()).hexdigest()
                review = Review(
                    display_name=display_name[:80],
                    title=title[:120],
                    rating=rating,
                    text=review_text[:1000],
                    product=product,
                    customer_account_id=(customer_account.id if customer_account and customer_account.email_verified_at else None),
                    ip_hash=ip_hash,
                )
                db.session.add(review)
                try:
                    db.session.commit()
                except Exception:
                    db.session.rollback()
                    current_app.logger.exception("Could not save customer review")
                    flash("We could not save your review just now. Please try again.", "error")
                else:
                    flash("Thanks for sharing your experience. Your review is saved and will appear after moderation.", "success")
                    return redirect(url_for("public.reviews", product_id=product.id if product else None, _anchor="approved-reviews"))

    approved_query = Review.query.filter_by(status="approved")
    selected_product = None
    raw_selected_product = request.values.get("product_id", "").strip()
    if raw_selected_product:
        try:
            selected_product = Product.query.filter_by(id=int(raw_selected_product), active=True).first()
        except (TypeError, ValueError):
            selected_product = None
        if selected_product is None:
            flash("That product review page is unavailable. Showing store reviews instead.", "info")
        else:
            approved_query = approved_query.filter_by(product_id=selected_product.id)

    rating_filter = request.values.get("rating_filter", "").strip()
    if rating_filter not in {"1", "2", "3", "4", "5"}:
        rating_filter = ""
    sort_by = request.values.get("sort", "recent").strip()
    if sort_by not in {"recent", "highest", "lowest"}:
        sort_by = "recent"

    average_rating, approved_count = db.session.query(
        func.avg(Review.rating), func.count(Review.id)
    ).filter(Review.status == "approved").filter(
        Review.product_id == selected_product.id if selected_product else True
    ).first()
    distribution_rows = db.session.query(
        Review.rating, func.count(Review.id)
    ).filter(Review.status == "approved").filter(
        Review.product_id == selected_product.id if selected_product else True
    ).group_by(Review.rating).all()
    rating_distribution = {star: 0 for star in range(1, 6)}
    rating_distribution.update({int(star): count for star, count in distribution_rows})
    approved_count = int(approved_count or 0)
    average_rating = float(average_rating or 0)

    reviews_query = approved_query
    if rating_filter:
        reviews_query = reviews_query.filter_by(rating=int(rating_filter))
    sort_column = {
        "highest": (Review.rating.desc(), Review.created_at.desc()),
        "lowest": (Review.rating.asc(), Review.created_at.desc()),
        "recent": (Review.created_at.desc(), Review.id.desc()),
    }[sort_by]
    try:
        page = max(1, int(request.values.get("page", "1")))
    except (TypeError, ValueError):
        page = 1
    pagination = reviews_query.order_by(*sort_column).paginate(page=page, per_page=12, error_out=False)
    public_reviews = pagination.items

    verified_review_ids = set()
    account_ids = {review.customer_account_id for review in public_reviews if review.customer_account_id and review.product_id}
    product_ids = {review.product_id for review in public_reviews if review.customer_account_id and review.product_id}
    if account_ids and product_ids:
        verified_pairs = db.session.query(
            CustomerOrderLink.account_id, OrderItem.product_id
        ).join(Order, CustomerOrderLink.order_id == Order.id).join(
            OrderItem, OrderItem.order_id == Order.id
        ).filter(
            CustomerOrderLink.account_id.in_(account_ids),
            OrderItem.product_id.in_(product_ids),
            Order.status == "Completed",
            Order.payment_status == "Paid",
        ).distinct().all()
        verified_pair_set = set(verified_pairs)
        verified_review_ids = {
            review.id for review in public_reviews
            if (review.customer_account_id, review.product_id) in verified_pair_set
        }

    return render_template(
        "reviews.html",
        title="Customer Reviews",
        reviews=public_reviews,
        products=products,
        selected_product=selected_product,
        rating_filter=rating_filter,
        sort_by=sort_by,
        average_rating=average_rating,
        approved_count=approved_count,
        rating_distribution=rating_distribution,
        pagination=pagination,
        verified_review_ids=verified_review_ids,
        customer_account=customer_account,
        form_values=form_values,
    )


def render_policy_page(slug):
    # Terms are static content. Render them before evaluating store settings used
    # by the delivery and payment policies, so unrelated configuration cannot
    # make the terms page fail.
    if slug == "terms":
        policy = {
            "title": "Terms and Conditions",
            "description": "Terms for using FityStores, placing an order request, paying, and arranging collection or delivery.",
            "sections": [
                ("About these terms and account checkout", ["FityStores is the shop name used on this website. These terms apply when you browse the site, create an account, or place an order request. Checkout requires a customer account; keep your account details accurate and protect your password and verification codes. You are responsible for activity carried out through your signed-in account, subject to rights that cannot be excluded by law."]),
                ("Products, prices, and availability", ["Product descriptions, images, options, prices, and stock show information currently recorded by the shop. They may change or be incorrect. Check the listing and confirm availability, selected size or colour, final price, and total with the seller before paying. No unlisted price or unavailable item is guaranteed." ]),
                ("Order requests and confirmation", ["Submitting checkout creates an order request for seller review; it does not by itself reserve stock, accept the order, or confirm a sale. The seller will confirm the item, final total, payment instructions, and fulfilment arrangement. Keep the order reference and check the order status in your account or through the order-tracking page." ]),
                ("Payment and payment references", ["Follow the payment instructions confirmed for your order. M-Pesa payment is handled outside the website; the site may let you enter a transaction reference or upload proof for the seller to review. Entering a reference or uploading proof does not automatically verify payment. Never share your M-Pesa PIN, password, or one-time sign-in code." ]),
                ("Collection and delivery", ["The available fulfilment method, meeting point, timing, delivery area, and any fee must be confirmed with the seller for your order. The listed collection reference is Othaya Mathenge Technical Training Institute; confirm the exact meeting place and time before travelling. Do not treat an order request as a confirmed delivery booking." ]),
                ("Cancellations, returns, and consumer rights", ["Contact the seller promptly if you need to change or cancel an order, or if an item is incorrect, damaged, or faulty. The Returns, Exchanges, and Cancellations policy explains how to raise a request. Nothing in these terms removes a consumer right or remedy that applies under Kenyan law, including rights under the Consumer Protection Act." ]),
                ("Website use and customer content", ["Use the site lawfully, do not interfere with its operation or security, and submit accurate information. Reviews and other customer submissions must not be unlawful, misleading, abusive, or infringe another person's rights. Reviews may be moderated before publication. Product photos, descriptions, and site branding belong to their respective rights holders; do not copy or reuse them without permission." ]),
                ("Questions, changes, and applicable law", ["Contact FityStores through the Contact page or call 0791305329 about an order or these terms. These terms are governed by applicable Kenyan law, and any dispute remains subject to consumer protections and the jurisdiction of Kenyan courts. Updated terms will be posted on this page with a revised effective date; changes do not remove rights that already apply to an order."]),
            ],
        }
        return render_template(
            "policy.html", title=policy["title"],
            seo_title=policy["title"] + " | FityStores",
            seo_description=policy["description"], policy=policy,
        )

    from app.services.formatting import money

    settings = current_settings()
    policies = {
        "delivery": {
            "title": "Delivery and Collection Policy",
            "description": "Check delivery availability, discuss any fee, and agree collection or delivery details before paying or travelling.",
            "sections": [
                ("Delivery availability", [settings.delivery_area or "Contact FityStores to check whether delivery is available for your location. Availability, timing, and any fee are confirmed for each order."]),
                ("Collection", [settings.collection_instructions or "The collection reference point is Othaya Mathenge Technical Training Institute. Contact the seller to agree on the exact meeting point and time before travelling."]),
                ("Delivery fee", [("The configured delivery fee is " + money(settings.delivery_fee_cents, settings.currency) + ". Confirm that it applies to your order before paying.") if settings.delivery_fee_cents is not None else "No fixed delivery fee is configured here. The seller will confirm any fee for your location before payment."]),
                ("Before travelling or paying", ["Confirm the item, selected options, price, stock, fulfilment method, any delivery fee, and the collection or delivery arrangements with the seller. A checkout request or payment reference alone does not confirm fulfilment."]),
            ],
        },
        "payment": {
            "title": "Payment Policy",
            "description": "Follow the current payment instructions and understand how payment is checked and confirmed.",
            "sections": [
                ("Available instructions", [settings.payment_instructions or "Ask the seller to confirm the payment method and amount for your order. The website does not process card payments."]),
                ("Confirm before paying", ["Confirm product availability, the final total, and the payment instructions with the seller before sending payment. Keep the M-Pesa transaction message and enter the requested reference at checkout. Never share your M-Pesa PIN or account password."]),
                ("Payment review and confirmation", ["M-Pesa is paid outside the website. A reference or optional proof submitted with an order is information for seller review; it is not automatic payment verification or order confirmation. Do not pay a second time just because the status has not updated—contact the seller with your order reference first."]),
            ],
        },
        "returns": {
            "title": "Returns, exchanges, and cancellations",
            "description": "How to raise a return, exchange, cancellation, or refund request for review.",
            "sections": [
                ("Report a problem", ["Contact FityStores as soon as practical if an item is incorrect, damaged, or appears faulty. Prompt reporting—ideally within 48 hours of receiving it—helps the seller investigate while details are fresh; this is not a limit on rights or remedies provided by law. Include your order reference and describe the issue. Photos may help the review."]),
                ("Cancellation or change of mind", ["If you need to cancel or change an order, contact the seller as soon as possible before it is prepared or handed over. A change-of-mind return is not automatic and must be agreed with the seller, subject always to any applicable legal right to cancel or obtain a remedy."]),
                ("Review and next steps", ["The seller will review the order and discuss the next step for the specific case. Do not send an item back until the seller confirms the return method. A request starts a review; it does not by itself approve a refund or exchange. Applicable consumer rights are not waived."]),
                ("Refunds", ["If a refund is agreed or required, the seller will confirm the amount and method with you. The store does not promise an automatic refund or a fixed processing time on this page."]),
                ("Contact", ["Use the Contact page or call 0791305329 with your order reference and a description of the issue. Never include your M-Pesa PIN or password."]),
            ],
        },
        "privacy": {
            "title": "Privacy information",
            "description": "What shopping and account features collect, how FityStores uses information, and how to ask a question.",
            "sections": [
                ("Who is responsible and how to contact us", ["FityStores is responsible for personal information collected through this shop. For a privacy request or question, use the Contact page or call 0791305329 and ask for the person responsible for privacy. The shop currently publishes under the FityStores trading name; this notice does not invent a separate registered legal entity or postal address."]),
                ("Information the site collects", ["Depending on how you use the site, it may process your name, email address, phone number, account and verification details, order items, delivery or collection instructions, order history, payment reference, and payment proof you choose to upload. The site also receives the information you submit in contact, enquiry, review, suggestion, and restock forms. Account passwords are stored as password hashes; verification and reset tokens are stored as hashes. Google sign-in may provide your Google account identifier, email, and display name."]),
                ("Why information is used", ["Information is used to create and secure accounts, process and link order requests, check payment reports, arrange fulfilment, answer enquiries, handle reviews and restock requests, prevent misuse, maintain records, and meet applicable legal obligations. The site may create a one-way hash of an IP address for abuse controls or review moderation. A review may be displayed publicly after moderation; avoid putting private information in a review." ]),
                ("Marketing and service messages", ["Promotional email is optional and requires a separate marketing opt-in; accepting the Terms and Conditions or creating an account does not subscribe you to marketing. You can withdraw marketing consent using the unsubscribe link or account preference controls. The shop may still send necessary account, security, order, and service messages that are not promotional." ]),
                ("Cookies, sessions, and browser storage", ["The site uses session and security data needed to sign you in, protect forms, and keep shopping functions working. Cart information may be stored in your browser or account to preserve your selections. This notice does not claim that analytics or advertising trackers are used." ]),
                ("Service providers and external links", ["Information is hosted and processed by the web host and database provider configured for the site. If enabled, Google handles Google sign-in; Cloudinary may deliver product images; and the configured SMTP, Resend, or Brevo provider may handle email. These providers receive only information needed for their service. The site links to WhatsApp; if you choose to open it and send a message, WhatsApp/Meta receives the message and account details under its own terms. M-Pesa transactions are made outside this website; the shop may record the reference or proof you provide. Provider locations and transfer safeguards depend on the services configured by the shop, so information may be processed outside Kenya." ]),
                ("Retention and security", ["The shop keeps information only as long as it is reasonably needed for the purposes described, account operation, order records, dispute handling, and legal obligations. The site does not publish fixed retention periods for each record type; contact the shop to ask about a particular record. The shop uses access controls and technical safeguards, but no internet transmission or storage system can be guaranteed completely secure." ]),
                ("Your choices and data rights", ["Subject to applicable law, you may ask to be informed about, access, correct, or object to processing of your personal information, or request deletion where appropriate. You may also withdraw consent for optional marketing. Contact the shop through the Contact page or call 0791305329; include enough detail to identify the account or order, but do not send a password, PIN, or sign-in code. You may raise a complaint with Kenya's Office of the Data Protection Commissioner."]),
                ("Children and sensitive information", ["Do not submit another person's personal information unless you are authorised to do so. Do not enter passwords, M-Pesa PINs, payment credentials, or unrelated sensitive information in reviews, enquiries, or order notes. A parent or guardian should contact the shop about information submitted by a child."]),
            ],
        },
        "terms": {
            "title": "Terms of Service",
            "description": "How FityStores listings, order requests, payment checks, and fulfilment arrangements work.",
            "sections": [
                ("Listings, prices, and stock", ["Product information, prices, variants, and stock are shown as currently recorded by the store and may change. Confirm availability, selected options, and the total with the seller before paying. An unavailable or unpriced item cannot be treated as a confirmed sale at a guessed price."]),
                ("Order review and payment", ["Submitting checkout details creates an order request for seller review. Follow the payment instructions shown by the store and confirm them with the seller. A payment reference or WhatsApp message does not alone mean the order has been accepted or payment verified."]),
                ("Delivery and collection", ["The seller must confirm the available fulfilment method, location, timing, and any delivery fee for each order before you travel or rely on delivery."]),
                ("Problems and legal rights", ["Contact the seller promptly about an order problem. These terms do not remove rights that apply under Kenyan law. No legal entity details or guarantees are added by this page."]),
            ],
        },
    }
    policy = policies[slug]
    return render_template(
        "policy.html",
        title=policy["title"],
        seo_title=policy["title"] + " | FityStores",
        seo_description=policy["description"],
        policy=policy,
    )


@public_bp.route("/delivery-policy")
def delivery_policy():
    return render_policy_page("delivery")


@public_bp.route("/payment-policy")
def payment_policy():
    return render_policy_page("payment")


@public_bp.route("/returns-policy")
def returns_policy():
    return render_policy_page("returns")


@public_bp.route("/privacy")
def privacy_policy():
    return render_policy_page("privacy")


@public_bp.route("/terms")
def terms():
    return render_policy_page("terms")

@public_bp.route("/robots.txt")
def robots():
    return Response("User-agent: *\nDisallow: /admin\nSitemap: " + url_for("public.sitemap", _external=True) + "\n", mimetype="text/plain")

@public_bp.route("/sitemap.xml")
def sitemap():
    urls = [url_for("public.home", _external=True), url_for("public.shop", _external=True), url_for("public.upcoming", _external=True), url_for("public.contact", _external=True), url_for("public.about", _external=True), url_for("public.reviews", _external=True)]
    urls += [url_for("public.product_detail", slug=p.slug, _external=True) for p in Product.query.filter_by(active=True).all()]
    urls += [url_for("public.delivery_policy", _external=True), url_for("public.payment_policy", _external=True), url_for("public.returns_policy", _external=True), url_for("public.privacy_policy", _external=True), url_for("public.terms", _external=True)]
    body = "<?xml version='1.0' encoding='UTF-8'?><urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>" + "".join(f"<url><loc>{escape(u)}</loc></url>" for u in urls) + "</urlset>"
    return Response(body, mimetype="application/xml")










