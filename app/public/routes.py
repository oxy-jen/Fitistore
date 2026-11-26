import hashlib
import hmac
import json
from secrets import token_urlsafe
import re
from urllib.parse import quote
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4
from flask import Blueprint, Response, current_app, flash, has_request_context, jsonify, make_response, redirect, render_template, request, session, url_for
from sqlalchemy import and_, case, func, or_
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import joinedload, selectinload
from xml.sax.saxutils import escape
from app.extensions import db, csrf
from app.models import CartAddCount, Category, CategoryThumbnail, CustomerAccount, CustomerCart, CustomerConsentEvent, CustomerOrderLink, CustomerProfile, EmailCampaignRecipient, EmailMessage, LoyaltyTransaction, Order, OrderItem, Product, ProductContentSection, ProductDetails, ProductLike, ProductEnquiry, ProductSuggestion, ProductVariant, ProductVariantDetails, ReferralClick, RestockAlert, Review, UpcomingProduct, UpcomingProductVote
from app.services.cart import validate_cart
from app.services.orders import create_checkout_order
from app.services.settings import current_settings
from app.services.mamba_collection import mamba_products_query
from app.services.media_showcases import showcases_by_placement
from app.services.upcoming_products import upcoming_product_schema_ready
from app.services.product_likes import product_like_schema_ready
from app.services.category_thumbnails import CATEGORY_GROUPS, category_group_key, category_thumbnails_schema_ready
from app.services.product_discounts import current_discounted_products, discounted_products_query
from app.services.whatsapp import LOCAL_CONTACT_NUMBER, build_general_message, build_mamba_suggestion_message, product_whatsapp_link, whatsapp_link, whatsapp_text_link
from app.services.product_images import product_display_image, product_gallery_images, product_image_alt, product_image_url, valid_product_image
from app.services.referrals import get_or_create_profile, normalize_phone, record_referral_click
from werkzeug.utils import secure_filename
from flask_login import current_user, logout_user

public_bp = Blueprint("public", __name__)
PRODUCTS_PER_PAGE = 8
SHOP_CATEGORY_PREVIEW_LIMIT = 12
OTHER_SHOP_CATEGORY_GROUP = {"key": "other-products", "name": "Other products", "description": "More products from the FityStores catalogue."}
SHOP_CATEGORY_GROUPS = (*CATEGORY_GROUPS, OTHER_SHOP_CATEGORY_GROUP)
SHOP_CATEGORY_GROUPS_BY_KEY = {group["key"]: group for group in SHOP_CATEGORY_GROUPS}
SEARCH_SUGGESTION_LIMIT = 8
SHOP_SORT_OPTIONS = ("newest", "name", "price-low", "price-high")


def _search_pattern(value):
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _product_search_filter(value):
    normalized = " ".join((value or "").split())
    if not normalized:
        return None
    def matches(term):
        pattern = f"%{_search_pattern(term)}%"
        return or_(
            Product.name.ilike(pattern, escape="\\"),
            Product.sku.ilike(pattern, escape="\\"),
            Product.description.ilike(pattern, escape="\\"),
            Category.name.ilike(pattern, escape="\\"),
            Product.variants.any(and_(
                ProductVariant.active.is_(True),
                or_(
                    ProductVariant.sku.ilike(pattern, escape="\\"),
                    ProductVariant.size.ilike(pattern, escape="\\"),
                    ProductVariant.color.ilike(pattern, escape="\\"),
                    ProductVariant.style.ilike(pattern, escape="\\"),
                    ProductVariant.material.ilike(pattern, escape="\\"),
                    ProductVariant.pack_size.ilike(pattern, escape="\\"),
                ),
            )),
        )

    phrase_match = matches(normalized)
    tokens = [token for token in re.findall(r"[\w-]+", normalized, flags=re.UNICODE) if len(token) > 1]
    if len(tokens) > 1:
        return or_(phrase_match, and_(*(matches(token) for token in tokens)))
    return phrase_match


def _product_search_rank(value):
    normalized = " ".join((value or "").split()).lower()
    escaped = _search_pattern(normalized)
    return case(
        (func.lower(Product.name) == normalized, 0),
        (Product.sku.ilike(normalized, escape="\\"), 1),
        (func.lower(Product.name).like(f"{escaped}%", escape="\\"), 2),
        (Product.sku.ilike(f"{escaped}%", escape="\\"), 3),
        (func.lower(Product.name).like(f"%{escaped}%", escape="\\"), 4),
        (Category.name.ilike(f"%{escaped}%", escape="\\"), 5),
        else_=6,
    )

def product_list_options(query):
    return query.options(
        joinedload(Product.category),
        selectinload(Product.images),
        selectinload(Product.variants).joinedload(ProductVariant.image),
    )


def _product_like_voter_hash(create=False):
    """Return a stable pseudonymous voter key without storing account/email data."""
    identity = None
    user = current_user._get_current_object()
    if isinstance(user, CustomerAccount):
        identity = f"customer:{user.id}"
    else:
        identity = session.get("product_like_voter")
        if not identity and create:
            identity = token_urlsafe(32)
            session["product_like_voter"] = identity
    if not identity:
        return None
    secret = str(current_app.secret_key or "").encode("utf-8")
    return hmac.new(secret, identity.encode("utf-8"), hashlib.sha256).hexdigest()

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
    storefront_designs = {"storefront_banner": [], "storefront_section": []}
    from app.services.design_studio import product_api_data
    if has_request_context():
        endpoint = request.endpoint or ""
        private_path = request.path.startswith((
            "/admin", "/account", "/auth", "/login", "/register", "/reset", "/verify", "/track/",
        )) or request.path in {"/cart", "/checkout", "/track", "/track-order", "/referrals"}
        excluded_endpoint = endpoint in {"public.checkout", "public.checkout_ready"}
        if endpoint.startswith("public.") and not private_path and not excluded_endpoint and not request.path.startswith("/checkout"):
            from app.services.design_studio import active_designs
            storefront_designs = active_designs()
    return {"settings": current_settings(), "money": money, "split_options": split_options, "product_display_image": product_display_image, "product_gallery_images": product_gallery_images, "product_image_url": product_image_url, "product_image_alt": product_image_alt, "valid_product_image": valid_product_image, "product_whatsapp_link": product_whatsapp_link, "whatsapp_text_link": whatsapp_text_link, "build_general_message": build_general_message, "local_contact_number": LOCAL_CONTACT_NUMBER, "category_group_key": category_group_key, "footer_products": footer_products[:3], "footer_product_groups": footer_product_groups, "design_studio_designs": storefront_designs, "product_api_data": product_api_data, "current_year": datetime.now().year}

def product_query(category_group_override=None, sort_override=None):
    q = request.args.get("q", "").strip()
    category_slug = request.args.get("category", "").strip()
    category_group_key_value = category_group_override or request.args.get("category_group", "").strip()
    category_group = SHOP_CATEGORY_GROUPS_BY_KEY.get(category_group_key_value)
    sort = sort_override or request.args.get("sort", "newest")
    if sort not in SHOP_SORT_OPTIONS:
        sort = "newest"
    query = product_list_options(Product.query.filter_by(active=True).join(Category))
    if q:
        search_filter = _product_search_filter(q)
        if not category_slug and not category_group_key_value:
            matched_categories = (
                db.session.query(Category.name)
                .select_from(Product)
                .join(Category, Product.category_id == Category.id)
                .filter(Product.active.is_(True), search_filter)
                .distinct()
                .all()
            )
            matched_group_keys = {
                category_group_key(category_name) or OTHER_SHOP_CATEGORY_GROUP["key"]
                for (category_name,) in matched_categories
            }
            inferred_group_key = category_group_key(q)
            if inferred_group_key:
                matched_group_keys.add(inferred_group_key)
            matching_category_ids = [
                category.id for category in Category.query.all()
                if (category_group_key(category.name) or OTHER_SHOP_CATEGORY_GROUP["key"]) in matched_group_keys
            ]
            query = query.filter(or_(search_filter, Product.category_id.in_(matching_category_ids)))
        else:
            query = query.filter(search_filter)
    active_category = None
    if category_slug:
        active_category = Category.query.filter_by(slug=category_slug).first()
        if active_category:
            query = query.filter(Product.category == active_category)
    elif category_group_key_value == OTHER_SHOP_CATEGORY_GROUP["key"]:
        category_ids = [category.id for category in Category.query.all() if category_group_key(category.name) is None]
        query = query.filter(Product.category_id.in_(category_ids or [-1]))
    elif category_group:
        category_ids = [category.id for category in Category.query.all() if category_group_key(category.name) == category_group["key"]]
        query = query.filter(Product.category_id.in_(category_ids or [-1]))
    category_order = {
        category.id: next(
            (index for index, group in enumerate(CATEGORY_GROUPS) if category_group_key(category.name) == group["key"]),
            len(CATEGORY_GROUPS),
        )
        for category in Category.query.all()
    }
    group_order = case(category_order, value=Product.category_id, else_=len(CATEGORY_GROUPS)).asc()
    if q:
        query = query.order_by(_product_search_rank(q).asc())
    query = query.order_by(group_order)
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

    discounted = current_discounted_products()
    used_product_ids.update(product.id for product in discounted)

    featured_query = Product.query.filter_by(active=True, featured=True).filter(Product.id.notin_(used_product_ids))
    featured = product_list_options(featured_query).order_by(Product.updated_at.desc(), Product.id.desc()).limit(8).all()
    used_product_ids.update(product.id for product in featured)

    top_added_candidates = [product for product in product_list_options(Product.query.join(CartAddCount, CartAddCount.product_id == Product.id).filter(Product.active.is_(True)).order_by(CartAddCount.add_count.desc(), Product.name.asc())).all() if product.display_stock_quantity > 0]
    product_like_counts = {}
    liked_product_ids = set()
    likes_enabled = product_like_schema_ready()
    if likes_enabled and top_added_candidates:
        product_ids = [product.id for product in top_added_candidates]
        try:
            product_like_counts = dict(
                db.session.query(ProductLike.product_id, func.count(ProductLike.id))
                .filter(ProductLike.product_id.in_(product_ids))
                .group_by(ProductLike.product_id).all()
            )
            voter_hash = _product_like_voter_hash(create=True)
            if voter_hash:
                liked_product_ids = {
                    product_id for (product_id,) in db.session.query(ProductLike.product_id)
                    .filter(ProductLike.product_id.in_(product_ids), ProductLike.voter_hash == voter_hash).all()
                }
        except SQLAlchemyError as exc:
            db.session.rollback()
            current_app.logger.error("Homepage product like data could not be loaded (%s).", type(exc).__name__)
            product_like_counts = {}
            liked_product_ids = set()
            likes_enabled = False
    cart_add_counts = {
        product_id: count for product_id, count in db.session.query(CartAddCount.product_id, CartAddCount.add_count)
        .filter(CartAddCount.product_id.in_([product.id for product in top_added_candidates])).all()
    } if top_added_candidates else {}
    top_added_candidates.sort(key=lambda product: (-product_like_counts.get(product.id, 0), -cart_add_counts.get(product.id, 0), product.name.casefold()))
    top_added = top_added_candidates[:8]

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
        "offers": product_list_options(discounted_products_query()).order_by(Product.updated_at.desc(), Product.id.desc()),
        "mamba": product_list_options(mamba_products_query()[0]),
    }
    share_product = share_product_queries.get(requested_share, share_product_queries["home"]).first()
    home_og_image = url_for("public.home_share_card", section=requested_share, _external=True)
    public_origin = (current_app.config.get("PUBLIC_BASE_URL") or "https://fitystore.top").rstrip("/")
    home_canonical = public_origin + "/" if requested_share == "home" and "share" not in request.args else public_origin + url_for("public.home", share=requested_share)
    media_showcases = showcases_by_placement("home")
    return render_template(
        "home.html", title="FityStores essentials", seo_title=section_title + " | FityStores",
        seo_description=section_description, canonical_url=home_canonical, og_image=home_og_image,
        og_image_width=1200, og_image_height=630, og_image_type="image/png", og_image_alt=section_title + " at FityStores",
        fresh_arrivals=fresh_arrivals, featured=featured, top_added=top_added, discounted=discounted,
        cart_add_counts=cart_add_counts, product_like_counts=product_like_counts,
        liked_product_ids=liked_product_ids, product_likes_enabled=likes_enabled,
        category_sections=category_sections, mamba_products=mamba_products, mamba_categories=mamba_categories,
        mamba_collection_configured=mamba_collection_configured,
        suggestion_categories=SUGGESTION_CATEGORIES, upcoming_merchandise=UPCOMING_MERCHANDISE,
        media_showcases=media_showcases,
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
    elif section=="offers":q=discounted_products_query()
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
        "offers": product_list_options(discounted_products_query()).order_by(Product.updated_at.desc(), Product.id.desc()),
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
    upcoming_products = []
    upcoming_vote_rankings = []
    upcoming_voted_ids = set()
    if upcoming_product_schema_ready():
        try:
            upcoming_products = (UpcomingProduct.query
                                 .filter_by(is_enabled=True)
                                 .order_by(UpcomingProduct.display_order, UpcomingProduct.id).all())
            voter_hash = _upcoming_voter_hash()
            vote_rows = (db.session.query(UpcomingProduct, func.count(UpcomingProductVote.id).label("vote_count"))
                         .outerjoin(UpcomingProductVote, UpcomingProductVote.upcoming_product_id == UpcomingProduct.id)
                         .filter(UpcomingProduct.is_enabled.is_(True))
                         .group_by(UpcomingProduct.id)
                         .order_by(func.count(UpcomingProductVote.id).desc(), UpcomingProduct.display_order,
                                   UpcomingProduct.id).all())
            upcoming_vote_rankings = [{"product": product, "count": count} for product, count in vote_rows]
            upcoming_voted_ids = {vote.upcoming_product_id for vote in UpcomingProductVote.query
                                  .filter_by(voter_hash=voter_hash).all()}
        except SQLAlchemyError as exc:
            db.session.rollback()
            current_app.logger.error("Upcoming product previews are hidden because their query failed (%s).", type(exc).__name__)
    available_catalogue = product_list_options(
        Product.query.filter_by(active=True)
    ).order_by(Product.featured.desc(), Product.updated_at.desc(), Product.id.desc()).all()
    recent_requests = (ProductSuggestion.query
                       .filter(ProductSuggestion.category.in_(UPCOMING_SUGGESTION_CATEGORIES))
                       .order_by(ProductSuggestion.created_at.desc(), ProductSuggestion.id.desc())
                       .limit(4).all())
    request_rows = (db.session.query(ProductSuggestion.category, func.count(ProductSuggestion.id))
                    .filter(ProductSuggestion.category.in_(UPCOMING_SUGGESTION_CATEGORIES))
                    .group_by(ProductSuggestion.category)
                    .order_by(func.count(ProductSuggestion.id).desc(), ProductSuggestion.category.asc()).all())
    total_suggestions = sum(count for _category, count in request_rows)
    request_image_terms = {
        "Clothing": ("clothing", "shirt", "trouser", "dress", "jacket"),
        "Shoes": ("shoe", "sneaker", "slipper"),
        "Accessories": ("accessory", "cap", "sock", "bag", "belt"),
        "Beauty & self-care": ("beauty", "hair", "skin", "self-care"),
        "Other": (),
    }
    request_activity = [{
        "category": category, "count": count,
        "share": round(count * 100 / total_suggestions) if total_suggestions else 0,
        "sample_product": next((product for product in available_catalogue
                                 if product_display_image(product)
                                 and any(term in f"{product.name} {product.category.name if product.category else ''}".lower()
                                         for term in request_image_terms.get(category, ()))), None),
    } for category, count in request_rows[:5]]
    popular_rows = product_list_options(
        db.session.query(Product, CartAddCount.add_count)
        .join(CartAddCount, CartAddCount.product_id == Product.id)
        .filter(Product.active.is_(True), CartAddCount.add_count > 0)
        .order_by(CartAddCount.add_count.desc(), Product.name.asc())
    ).limit(8).all()
    shopper_picks = [
        {"product": product, "add_count": add_count}
        for product, add_count in popular_rows if product.display_stock_quantity > 0
    ][:4]
    return render_template(
        "upcoming.html", title="Upcoming merchandise", upcoming_products=upcoming_products,
        suggestion_categories=UPCOMING_SUGGESTION_CATEGORIES, request_activity=request_activity,
        total_suggestions=total_suggestions, shopper_picks=shopper_picks,
        recent_requests=recent_requests,
        upcoming_vote_rankings=upcoming_vote_rankings, upcoming_voted_ids=upcoming_voted_ids,
        media_showcases=showcases_by_placement("upcoming"),
    )


def _upcoming_voter_hash():
    """Return a stable, privacy-preserving identity for this account or browser."""
    if current_user.is_authenticated:
        identity = "account:" + str(current_user.get_id())
    else:
        token = session.get("upcoming_vote_token")
        if not token:
            token = token_urlsafe(32)
            session["upcoming_vote_token"] = token
        identity = "browser:" + token
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


@public_bp.route("/upcoming-products/<int:product_id>/vote", methods=["POST"])
def vote_upcoming_product(product_id):
    if not upcoming_product_schema_ready():
        flash("Product voting is temporarily unavailable. Please try again later.", "error")
        return redirect(url_for("public.upcoming") + "#upcoming-products")
    product = UpcomingProduct.query.filter_by(id=product_id, is_enabled=True).first()
    if product is None:
        flash("That upcoming product is no longer available to vote for.", "info")
        return redirect(url_for("public.upcoming") + "#upcoming-products")
    db.session.add(UpcomingProductVote(upcoming_product_id=product.id, voter_hash=_upcoming_voter_hash()))
    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        if isinstance(exc, IntegrityError):
            flash("You have already voted for this product. Thanks for supporting its launch!", "info")
        else:
            current_app.logger.error("Upcoming product vote could not be saved (%s).", type(exc).__name__)
            flash("Your vote could not be saved right now. Please try again.", "error")
        return redirect(url_for("public.upcoming") + "#upcoming-products")
    flash("Your vote for “{}” has been counted. Thanks for helping shape what comes next!".format(product.name), "success")
    return redirect(url_for("public.upcoming") + "#community-requests")

SUGGESTION_CATEGORIES = ["Boxers", "Men's shoes and sneakers", "Vests", "Socks", "Slippers", "Other men's accessories"]
UPCOMING_SUGGESTION_CATEGORIES = ["Clothing", "Shoes", "Accessories", "Beauty & self-care", "Other"]


def suggestion_form_data():
    return {
        "product_name": request.form.get("product_name", "").strip(),
        "category": request.form.get("category", "").strip(),
        "preferences": request.form.get("preferences", "").strip(),
        "price_range": request.form.get("price_range", "").strip(),
        "details": request.form.get("details", "").strip(),
        "website_url": request.form.get("website_url", "").strip(),
    }


def validate_suggestion_payload(data, categories=None, category_error="Choose a valid product category."):
    errors = []
    if data["website_url"]:
        errors.append("Please submit the suggestion using the visible fields only.")
    if len(data["product_name"]) < 2:
        errors.append("Enter the product or item name.")
    if len(data["product_name"]) > 140:
        errors.append("Keep the product name under 140 characters.")
    if data["category"] not in (categories or SUGGESTION_CATEGORIES):
        errors.append(category_error)
    if len(data["preferences"]) > 255:
        errors.append("Keep preferences under 255 characters.")
    if len(data["price_range"]) > 80:
        errors.append("Keep the price range under 80 characters.")
    if len(data["details"]) > 1000:
        errors.append("Keep additional details under 1000 characters.")
    return errors


def suggestion_ip_hash():
    return hashlib.sha256((request.remote_addr or "unknown").encode()).hexdigest()


def suggestion_return_url():
    if request.form.get("return_to") == "upcoming":
        return url_for("public.upcoming") + "#upcoming-request"
    return url_for("public.home") + "#mamba-suggestions"


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
    errors = validate_suggestion_payload(
        data, SUGGESTION_CATEGORIES, "Choose a valid Mamba collection category."
    )
    ip_hash = suggestion_ip_hash()
    recent_cutoff = datetime.utcnow() - timedelta(hours=1)
    recent_count = ProductSuggestion.query.filter(ProductSuggestion.ip_hash == ip_hash, ProductSuggestion.created_at >= recent_cutoff).count()
    if recent_count >= 5:
        errors.append("Too many suggestions were sent recently. Please try again later.")
    if errors:
        for error in errors:
            flash(error, "error")
        return redirect(suggestion_return_url())
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
        return redirect(suggestion_return_url())
    flash("Thanks. Your Mamba Collection suggestion has been saved for review.", "success")
    return redirect(suggestion_return_url())


@public_bp.route("/mamba-suggestion/whatsapp", methods=["POST"])
def mamba_suggestion_whatsapp():
    data = suggestion_form_data()
    errors = validate_suggestion_payload(
        data, SUGGESTION_CATEGORIES, "Choose a valid Mamba collection category."
    )
    if errors:
        for error in errors:
            flash(error, "error")
        return redirect(suggestion_return_url())
    message = build_mamba_suggestion_message(data["product_name"], data["category"], data["preferences"], data["price_range"], data["details"])
    return redirect(whatsapp_text_link(current_settings(), message))


@public_bp.route("/upcoming-suggestion", methods=["POST"])
def upcoming_suggestion():
    data = suggestion_form_data()
    errors = validate_suggestion_payload(
        data, UPCOMING_SUGGESTION_CATEGORIES,
        "Choose a valid category for your all-gender product request.",
    )
    ip_hash = suggestion_ip_hash()
    recent_cutoff = datetime.utcnow() - timedelta(hours=1)
    recent_count = ProductSuggestion.query.filter(
        ProductSuggestion.ip_hash == ip_hash,
        ProductSuggestion.created_at >= recent_cutoff,
    ).count()
    if recent_count >= 5:
        errors.append("Too many product requests were sent recently. Please try again later.")
    if errors:
        for error in errors:
            flash(error, "error")
        return redirect(url_for("public.upcoming") + "#upcoming-request")

    suggestion = ProductSuggestion(
        product_name=data["product_name"][:140], category=data["category"],
        preferences=data["preferences"][:255], price_range=data["price_range"][:80],
        details=data["details"][:1000], ip_hash=ip_hash,
    )
    try:
        db.session.add(suggestion)
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Could not save Upcoming page product request")
        flash("We could not save your product request right now. Please try again or use WhatsApp.", "error")
        return redirect(url_for("public.upcoming") + "#upcoming-request")
    flash("Thanks. Your product request has been sent to FityStores for review.", "success")
    return redirect(url_for("public.upcoming") + "#upcoming-request")


@public_bp.route("/upcoming-suggestion/whatsapp", methods=["POST"])
def upcoming_suggestion_whatsapp():
    data = suggestion_form_data()
    errors = validate_suggestion_payload(
        data, UPCOMING_SUGGESTION_CATEGORIES,
        "Choose a valid category for your all-gender product request.",
    )
    if errors:
        for error in errors:
            flash(error, "error")
        return redirect(url_for("public.upcoming") + "#upcoming-request")
    lines = [
        "Hello FityStores! I would like to suggest a product for the Upcoming page.",
        "This request is for the all-gender range.",
        "",
        f"Product or item: {data['product_name']}",
        f"Category: {data['category']}",
        f"Preferred colour, size, or style: {data['preferences'] or 'Not specified'}",
        f"Suggested price range: {data['price_range'] or 'Not specified'}",
    ]
    if data["details"]:
        lines.extend(["", f"Additional details: {data['details']}"])
    lines.extend(["", "Please consider this item for a future release. Thank you!"])
    return redirect(whatsapp_text_link(current_settings(), "\n".join(lines)))

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
    flash("Your question has been saved for the FityStores team.", "success")
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
    flash("Thanks. Your request is saved. FityStores may contact you if this item is restocked.", "success")
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
    active_category_group = SHOP_CATEGORY_GROUPS_BY_KEY.get(request.args.get("category_group", "").strip())
    page = 1
    shop_sections = []
    search_match_product_ids = set()
    related_products = []
    categories = Category.query.order_by(Category.name).all()
    if q and not active_category and not active_category_group:
        search_match_product_ids = {
            product_id for (product_id,) in db.session.query(Product.id)
            .join(Category, Product.category_id == Category.id)
            .filter(Product.active.is_(True), _product_search_filter(q))
            .all()
        }
    if not active_category and not active_category_group and not q:
        category_product_rows = (
            db.session.query(Product.category_id, Product.name, Product.slug)
            .filter(Product.active.is_(True))
            .order_by(Product.name.asc())
            .all()
        )
        products_by_category = {}
        for category_id, product_name, product_slug in category_product_rows:
            products_by_category.setdefault(category_id, []).append({
                "name": product_name,
                "slug": product_slug,
            })
        categories_by_group = {}
        for category in categories:
            category_products = products_by_category.get(category.id, [])
            if category_products:
                group_key = category_group_key(category.name) or OTHER_SHOP_CATEGORY_GROUP["key"]
                categories_by_group.setdefault(group_key, []).append({
                    "category": category,
                    "products": category_products,
                })
        for group in SHOP_CATEGORY_GROUPS:
            sort_param = f"sort_{group['key']}"
            section_sort = request.args.get(sort_param, sort)
            group_query, _category, _query_text, section_sort = product_query(group["key"], section_sort)
            total = group_query.count()
            if total:
                shop_sections.append({
                    "group": group,
                    "products": group_query.limit(SHOP_CATEGORY_PREVIEW_LIMIT).all(),
                    "total": total,
                    "preview_limit": SHOP_CATEGORY_PREVIEW_LIMIT,
                    "categories": categories_by_group.get(group["key"], []),
                    "sort": section_sort,
                    "sort_param": sort_param,
                })
        products = []
        has_next = False
    elif q and not active_category and not active_category_group:
        products = query.filter(Product.id.in_(search_match_product_ids)).all() if search_match_product_ids else []
        related_products = query.filter(~Product.id.in_(search_match_product_ids)).all() if search_match_product_ids else []
        has_next = False
    else:
        pagination = query.paginate(page=page, per_page=PRODUCTS_PER_PAGE, error_out=False)
        products = pagination.items
        has_next = pagination.has_next
    search_other_places = []
    if q and not products and (active_category or active_category_group):
        matches = (
            db.session.query(Category.id, Category.name, Category.slug, func.count(Product.id))
            .join(Product, Product.category_id == Category.id)
            .filter(Product.active.is_(True), _product_search_filter(q))
            .group_by(Category.id, Category.name, Category.slug)
            .order_by(func.count(Product.id).desc(), Category.name.asc())
            .all()
        )
        destinations = {}
        for category_id, category_name, category_slug, match_count in matches:
            group_key = category_group_key(category_name)
            if group_key:
                group = SHOP_CATEGORY_GROUPS_BY_KEY[group_key]
                destination_key = ("group", group_key)
                destination = {
                    "label": group["name"],
                    "url": url_for("public.shop", q=q, category_group=group_key, sort=sort),
                    "categories": [],
                    "count": 0,
                }
            else:
                destination_key = ("category", category_id)
                destination = {
                    "label": category_name,
                    "url": url_for("public.shop", q=q, category=category_slug, sort=sort),
                    "categories": [],
                    "count": 0,
                }
            current = destinations.setdefault(destination_key, destination)
            current["count"] += match_count
            if category_name not in current["categories"]:
                current["categories"].append(category_name)
        search_other_places = sorted(destinations.values(), key=lambda item: (-item["count"], item["label"].casefold()))[:5]
    active_title = active_category.name if active_category else (active_category_group["name"] if active_category_group else "Shop products")
    return render_template("shop.html", title="Shop", seo_title=active_title + " | Shop at FityStores", seo_description=("Browse available " + active_title + " products, check current listing details, and contact FityStores with questions."), products=products, related_products=related_products, shop_sections=shop_sections, categories=categories, category_groups=SHOP_CATEGORY_GROUPS, active_category=active_category, active_category_group=active_category_group, search_other_places=search_other_places, search_match_product_ids=search_match_product_ids, q=q, sort=sort, page=page, has_next=has_next, media_showcases=showcases_by_placement("shop"))

@public_bp.route("/api/products")
def api_products():
    query, _active_category, _q, _sort = product_query()
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    pagination = query.paginate(page=page, per_page=PRODUCTS_PER_PAGE, error_out=False)
    search_match_product_ids = set()
    if _q and not request.args.get("category", "").strip() and not request.args.get("category_group", "").strip():
        search_match_product_ids = {
            product_id for (product_id,) in db.session.query(Product.id)
            .join(Category, Product.category_id == Category.id)
            .filter(Product.active.is_(True), _product_search_filter(_q))
            .all()
        }
    cards = [render_template("partials/product_card.html", product=product, shop_listing_card=True, search_match_product_ids=search_match_product_ids) for product in pagination.items]
    return jsonify({
        "page": page,
        "has_next": pagination.has_next,
        "next_page": page + 1 if pagination.has_next else None,
        "count": len(pagination.items),
        "ids": [product.id for product in pagination.items],
        "html": "".join(cards),
        "products": [product_card_payload(product) for product in pagination.items],
    })


@public_bp.route("/api/search/suggestions")
def api_search_suggestions():
    query_text = " ".join(request.args.get("q", "").split())[:100]
    if len(query_text) < 2:
        return jsonify({"query": query_text, "suggestions": []})

    query = product_list_options(Product.query.filter_by(active=True).join(Category))
    query = query.filter(_product_search_filter(query_text))
    group_key = request.args.get("category_group", "").strip()
    group = SHOP_CATEGORY_GROUPS_BY_KEY.get(group_key)
    if group_key == OTHER_SHOP_CATEGORY_GROUP["key"]:
        category_ids = [category.id for category in Category.query.all() if category_group_key(category.name) is None]
        query = query.filter(Product.category_id.in_(category_ids or [-1]))
    elif group:
        category_ids = [category.id for category in Category.query.all() if category_group_key(category.name) == group_key]
        query = query.filter(Product.category_id.in_(category_ids or [-1]))
    category_slug = request.args.get("category", "").strip()
    if category_slug:
        category = Category.query.filter_by(slug=category_slug).first()
        query = query.filter(Product.category == category) if category else query.filter(False)

    products = query.order_by(
        _product_search_rank(query_text).asc(),
        Product.featured.desc(),
        Product.updated_at.desc(),
        Product.id.desc(),
    ).limit(SEARCH_SUGGESTION_LIMIT).all()

    from app.services.formatting import money
    currency = current_settings().currency
    suggestions = []
    for product in products:
        image = product_display_image(product)
        if product.min_variant_price_cents is not None:
            price = f"From {money(product.min_variant_price_cents, currency)}"
        elif product.price_cents is not None:
            price = money(product.price_cents, currency)
        else:
            price = "Price to be confirmed"
        available = product.display_stock_quantity
        suggestions.append({
            "id": product.id,
            "name": product.name,
            "category": product.category.name,
            "price": price,
            "availability": f"{available} available" if available else "Out of stock",
            "image_url": product_image_url(image),
            "image_alt": product_image_alt(image),
            "url": url_for("public.product_detail", slug=product.slug),
        })
    return jsonify({"query": query_text, "suggestions": suggestions})

@public_bp.route("/product/<slug>")
def product_detail(slug):
    product = product_list_options(Product.query.filter_by(slug=slug, active=True)).first_or_404()
    from app.services.product_display_notes import get_product_display_note
    from app.services.product_content import product_details_schema_ready, sanitize_product_html
    from markupsafe import Markup
    details_ready = product_details_schema_ready()
    product_details = (
        ProductDetails.query.filter_by(product_id=product.id).first()
        if details_ready else None
    )
    variant_details = {}
    if details_ready and product.variants:
        variant_details = {
            detail.variant_id: sanitize_product_html(detail.description_override_html)
            for detail in ProductVariantDetails.query.filter(
                ProductVariantDetails.variant_id.in_([variant.id for variant in product.variants])
            ).all()
            if detail.description_override_html
        }
    product_story_sections = []
    if details_ready:
        for section in ProductContentSection.query.filter_by(product_id=product.id).order_by(
            ProductContentSection.sort_order, ProductContentSection.id
        ).all():
            image = section.image
            product_story_sections.append({
                "title": section.title,
                "body_text": section.body_text,
                "image_url": product_image_url(image) if image else "",
                "image_alt": section.image_alt or (
                    product_image_alt(image, product.name) if image else ""
                ),
                "image_caption": section.image_caption,
                "image_position": section.image_position,
            })
    product_display_note = get_product_display_note(product.id)
    related = product_list_options(Product.query.filter(Product.active.is_(True), Product.category_id == product.category_id, Product.id != product.id)).order_by(Product.featured.desc(), Product.updated_at.desc(), Product.id.desc()).limit(4).all()
    related_same_category_count = len(related)
    if len(related) < 4:
        related_ids = [item.id for item in related] + [product.id]
        additional_related = product_list_options(Product.query.filter(Product.active.is_(True), Product.id.notin_(related_ids))).order_by(Product.featured.desc(), Product.updated_at.desc(), Product.id.desc()).limit(4 - len(related)).all()
        related.extend(additional_related)
    approved_review_query = Review.query.filter_by(product_id=product.id, status="approved")
    review_count = approved_review_query.count()
    review_average = db.session.query(func.avg(Review.rating)).filter_by(product_id=product.id, status="approved").scalar() or 0
    review_distribution = {
        rating: count for rating, count in db.session.query(Review.rating, func.count(Review.id))
        .filter_by(product_id=product.id, status="approved")
        .group_by(Review.rating).all()
    }
    approved_reviews = approved_review_query.order_by(Review.created_at.desc(), Review.id.desc()).limit(3).all()
    product_likes_enabled = product_like_schema_ready()
    product_like_count = 0
    product_is_liked = False
    if product_likes_enabled:
        try:
            product_like_count = ProductLike.query.filter_by(product_id=product.id).count()
            voter_hash = _product_like_voter_hash(create=True)
            product_is_liked = bool(
                voter_hash
                and ProductLike.query.filter_by(
                    product_id=product.id,
                    voter_hash=voter_hash,
                ).first()
            )
        except SQLAlchemyError as exc:
            db.session.rollback()
            current_app.logger.error(
                "Product like data could not be loaded (%s).",
                type(exc).__name__,
            )
            product_likes_enabled = False
    safe_rich_description = sanitize_product_html(
        product_details.detailed_description_html if product_details else ""
    )
    product_description = re.sub(r"<[^>]+>", " ", product.description or "")
    if product_details and product_details.introduction:
        product_description = product_details.introduction + " " + product_description
    if safe_rich_description:
        product_description += " " + re.sub(r"<[^>]+>", " ", safe_rich_description)
    if product_story_sections:
        product_description += " " + " ".join(
            f"{section['title']}. {section['body_text']}"
            for section in product_story_sections
        )
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
    if product_details and product_details.seo_description:
        seo_description = product_details.seo_description
    seo_title = (
        product_details.seo_title
        if product_details and product_details.seo_title else product.name + " | FityStores"
    )
    whatsapp_share_url = "https://wa.me/?text=" + quote(share_message)
    card_url = "https://fitystore.top" + url_for("public.product_share_card", slug=product.slug)
    return render_template(
        "product_detail.html",
        title=product.name,
        seo_title=seo_title,
        seo_description=seo_description,
        og_type="product",
        og_image=card_url,
        og_image_width=1200,
        og_image_height=630,
        og_image_type="image/png",
        og_image_alt=product.name + " at FityStores",
        product_description=product_description,
        schema_image_url=image_url,
        share_url=share_url,
        share_message=share_message,
        whatsapp_share_url=whatsapp_share_url,
        product=product,
        product_details=product_details,
        product_story_sections=product_story_sections,
        product_rich_description=Markup(safe_rich_description),
        variant_descriptions=variant_details,
        product_display_note=product_display_note,
        related=related,
        related_same_category_count=related_same_category_count,
        approved_reviews=approved_reviews,
        review_count=review_count,
        review_average=float(review_average),
        review_distribution=review_distribution,
        variant_offer_count=len(variant_prices),
        product_likes_enabled=product_likes_enabled,
        product_like_count=product_like_count,
        product_is_liked=product_is_liked,
    )


@public_bp.get("/api/products/recently-viewed")
def recently_viewed_products():
    raw_ids = request.args.get("ids", "")
    product_ids = []
    for raw_id in raw_ids.split(","):
        try:
            product_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if product_id > 0 and product_id not in product_ids:
            product_ids.append(product_id)
        if len(product_ids) == 8:
            break
    if not product_ids:
        return jsonify({"products": []})

    products = Product.query.filter(
        Product.id.in_(product_ids),
        Product.active.is_(True),
    ).all()
    by_id = {product.id: product for product in products if product.display_stock_quantity > 0}
    from app.services.product_images import product_display_image, product_image_alt, product_image_url
    from app.services.formatting import money
    currency = current_settings().currency
    result = []
    for product_id in product_ids:
        product = by_id.get(product_id)
        if product is None:
            continue
        image = product_display_image(product)
        price_cents = product.min_variant_price_cents
        if price_cents is None:
            price_cents = product.price_cents
        result.append({
            "id": product.id,
            "name": product.name,
            "url": url_for("public.product_detail", slug=product.slug),
            "image_url": product_image_url(image),
            "image_alt": product_image_alt(image, product.name),
            "price": money(price_cents, currency) if price_cents is not None else "Contact for price",
            "price_high": (
                money(product.max_variant_price_cents, currency)
                if product.max_variant_price_cents is not None
                and product.max_variant_price_cents != price_cents else None
            ),
        })
    return jsonify({
        "products": result
    })


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
        counter = CartAddCount(product_id=product_id, add_count=quantity)
        db.session.add(counter)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Could not record cart addition")
        return jsonify({"ok": False}), 503
    return jsonify({"ok": True, "add_count": counter.add_count})


@public_bp.post("/api/design-studio/validate-product")
def api_validate_design_studio_product():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "message": "The requested product addition is invalid."}), 400
    raw_product_id = payload.get("product_id")
    quantity = payload.get("quantity", 1)
    raw_variant_id = payload.get("variant_id")
    if set(payload) - {"product_id", "variant_id", "quantity"} or (
        type(raw_product_id) is not int or type(quantity) is not int
        or (raw_variant_id is not None and type(raw_variant_id) is not int)
    ):
        return jsonify({"ok": False, "message": "The requested product addition is invalid."}), 400
    product_id, variant_id = raw_product_id, raw_variant_id
    if quantity < 1 or quantity > 5:
        return jsonify({"ok": False, "message": "Choose a quantity from 1 to 5."}), 400
    product = Product.query.filter_by(id=product_id, active=True).first()
    if not product:
        return jsonify({"ok": False, "message": "This product is no longer available."}), 404
    active_variants = ProductVariant.query.filter_by(product_id=product.id, active=True).all()
    if active_variants:
        variant = next((item for item in active_variants if item.id == variant_id), None)
        if variant is None or variant.available_quantity < quantity:
            return jsonify({"ok": False, "message": "This product or option is no longer available in the requested quantity."}), 409
    elif variant_id is not None or product.available_quantity < quantity:
        return jsonify({"ok": False, "message": "This product or option is no longer available in the requested quantity."}), 409
    return jsonify({"ok": True})


@public_bp.post("/api/products/<int:product_id>/like")
def api_toggle_product_like(product_id):
    if not product_like_schema_ready():
        return jsonify({"ok": False, "message": "Likes are temporarily unavailable."}), 503
    product = Product.query.filter_by(id=product_id, active=True).first()
    if not product:
        return jsonify({"ok": False, "message": "Product not found."}), 404
    voter_hash = _product_like_voter_hash(create=True)
    existing = ProductLike.query.filter_by(product_id=product.id, voter_hash=voter_hash).first()
    try:
        if existing:
            db.session.delete(existing)
            liked = False
        else:
            db.session.add(ProductLike(product_id=product.id, voter_hash=voter_hash))
            liked = True
        db.session.commit()
    except IntegrityError:
        # A parallel request from the same shopper may have inserted the
        # unique vote first. Recover to the persisted state without a 500.
        db.session.rollback()
        liked = ProductLike.query.filter_by(product_id=product.id, voter_hash=voter_hash).first() is not None
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.error("Could not record product like (%s).", type(exc).__name__)
        return jsonify({"ok": False, "message": "Your like could not be saved. Please try again."}), 503
    like_count = ProductLike.query.filter_by(product_id=product.id).count()
    return jsonify({"ok": True, "liked": liked, "like_count": like_count})


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
                email_record = notify_order_event(order, "order_received", f"order-received:{order.id}")
                if email_record is None:
                    current_app.logger.warning(
                        "order_confirmation_not_queued order_id=%s reason=missing_or_unverified_customer_link",
                        order.id,
                    )
                elif email_record.status == "sent":
                    current_app.logger.info(
                        "order_confirmation_provider_accepted order_id=%s email_message_id=%s",
                        order.id, email_record.id,
                    )
                elif email_record.status == "delivered":
                    current_app.logger.info(
                        "order_confirmation_provider_delivery_confirmed order_id=%s email_message_id=%s",
                        order.id, email_record.id,
                    )
                elif email_record.status == "queued":
                    current_app.logger.warning(
                        "order_confirmation_awaiting_recovery order_id=%s email_message_id=%s",
                        order.id, email_record.id,
                    )
                elif email_record.status == "failed":
                    current_app.logger.error(
                        "order_confirmation_delivery_failed order_id=%s email_message_id=%s category=%s",
                        order.id, email_record.id, email_record.error_category or "unknown",
                    )
                else:
                    current_app.logger.info(
                        "order_confirmation_not_dispatched order_id=%s email_message_id=%s status=%s",
                        order.id, email_record.id, email_record.status,
                    )
            except Exception:
                db.session.rollback()
                current_app.logger.exception(
                    "order_confirmation_enqueue_failed order_id=%s",
                    order.id,
                )
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
        from app.models import MarketingSuppressionCase, MarketingSuppressionEvent
        required_models = (EmailMessage, CustomerAccount, CustomerConsentEvent, EmailCampaignRecipient, MarketingSuppressionCase, MarketingSuppressionEvent)
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
    payload = request.get_json(silent=True)
    if not isinstance(payload, (dict, list)) or not payload:
        return jsonify({"ok": False, "error": "invalid webhook payload"}), 400
    events = payload if isinstance(payload, list) else [payload]
    if len(events) > 100 or any(not isinstance(item, dict) for item in events):
        return jsonify({"ok": False, "error": "invalid webhook payload"}), 400
    suppressed = 0
    for event in events:
        event_name=str(event.get("event") or event.get("type") or "").strip().lower().replace("-", "_")
        if event_name not in {"delivered","hardbounce","hard_bounce","softbounce","soft_bounce","spam","complaint","invalid","invalid_email","unsubscribe","unsubscribed"}:continue
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
        if not address:
            if event_name in {"hardbounce", "hard_bounce", "invalid", "invalid_email", "spam", "complaint", "unsubscribe", "unsubscribed"}:
                db.session.rollback()
                return jsonify({"ok": False, "error": "event identity unavailable"}), 422
            continue
        account=CustomerAccount.query.filter(db.func.lower(CustomerAccount.email)==address).first()
        if not account:continue
        from app.services.marketing_suppressions import fingerprint_provider_event, record_suppression
        provider_reason = {
            "hardbounce": "hard_bounce", "hard_bounce": "hard_bounce",
            "invalid": "invalid_address", "invalid_email": "invalid_address",
            "spam": "spam_complaint", "complaint": "spam_complaint",
            # Brevo's generic unsubscribe payload does not prove who initiated it.
            "unsubscribe": "provider_unsubscribe_unclassified",
            "unsubscribed": "provider_unsubscribe_unclassified",
        }.get(event_name)
        if provider_reason:
            fingerprint = fingerprint_provider_event(event, normalized_event=event_name, email=address)
            if not fingerprint:
                db.session.rollback()
                current_app.logger.warning(
                    "brevo_event_quarantined event_type=%s reason=insufficient_identity",
                    event_name,
                )
                return jsonify({"ok": False, "error": "event identity unavailable"}), 422
            duplicate_event = bool(fingerprint and MarketingSuppressionEvent.query.filter_by(dedupe_fingerprint=fingerprint).first())
            case = record_suppression(
                account.id, source="provider", reason_code=provider_reason,
                reason_text="Brevo reported " + event_name.replace("_", " ") + "; actor origin is not established by this webhook payload.",
                actor_type="provider", provider_name="brevo",
                provider_message_id=provider_id or None,
                dedupe_fingerprint=fingerprint,
                metadata={
                    "event_type": event_name,
                    "event_timestamp": str(event.get("ts_event") or event.get("ts") or "")[:40],
                },
            )
            if case:
                already_recorded=CustomerConsentEvent.query.filter_by(
                    account_id=account.id,event="suppressed",method="brevo_webhook"
                ).first()
                if not already_recorded:
                    db.session.add(CustomerConsentEvent(account_id=account.id,event="suppressed",method="brevo_webhook",wording_version="2026-10"))
                if not duplicate_event and provider_reason in {"hard_bounce", "invalid_address", "spam_complaint"}:
                    suppressed += 1
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
    category_product_counts = {}
    in_stock_product_count = 0
    for product in category_products:
        category_product_counts[product.category_id] = category_product_counts.get(product.category_id, 0) + 1
        if product.display_stock_quantity > 0:
            in_stock_product_count += 1
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
        category_product_counts=category_product_counts,
        active_product_count=len(category_products),
        in_stock_product_count=in_stock_product_count,
        media_showcases=showcases_by_placement("about"),
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
    updated_on = "26 November 2026"

    if slug == "terms":
        policy = {
            "title": "Terms and Conditions",
            "description": "How FityStores listings, customer accounts, order requests, payment checks, and fulfilment work.",
            "updated_on": updated_on,
            "notice": "Checkout creates an order request and an inventory hold, not a confirmed sale. Wait for the seller to confirm the items, final total, payment, and fulfilment details.",
            "sections": [
                ("Who these terms cover", [
                    "These terms apply when you browse FityStores, create or use a customer account, or submit an order request. Checkout is available to signed-in, verified customer accounts. Give accurate contact and fulfilment details, keep your sign-in credentials private, and contact the store promptly if you think someone else has accessed your account.",
                    "FityStores is the shop name used on this website. Use the Contact page for current ways to reach the store about an account, order, or these terms.",
                ]),
                ("Product information, prices, and offers", [
                    "Product descriptions, images, specifications, variants, stock counts, and prices reflect information currently recorded by the store. Listings can change, and an error or delay may occur. Check the selected product and variant, quantity, promotional price, and displayed total before submitting a request.",
                    "A displayed offer is based on the prices and discount currently shown for that product or variant. The seller will confirm availability and the final amount for your order, especially where a price is marked for confirmation. No unlisted price or unavailable item is promised.",
                ]),
                ("Order requests and inventory holds", [
                    "Submitting checkout creates an order request for seller review. The store records the requested quantities as reserved inventory while the request is pending. This operational hold is not acceptance of the order, a confirmed sale, or a guarantee that every listing detail is correct; no automatic expiry time for the hold is published.",
                    "The seller will review the items, selected options, final amount, payment report, and requested collection or delivery method. Wait for the seller to confirm the order before treating it as accepted or making travel plans. If an item or price needs correction, ask the seller to clarify before proceeding. A cancelled order releases its inventory hold.",
                    "Keep your order reference and use the account order history or order-tracking page for updates. An order status or payment reference is not a substitute for the seller's confirmation.",
                ]),
                ("Payment and payment verification", [
                    "The website currently records manual M-Pesa payment reports; it does not automatically verify an M-Pesa transfer. Follow the payment instructions shown by the store and confirm the amount and recipient with the seller before paying.",
                    "A transaction reference or optional screenshot supplied at checkout helps the seller reconcile a payment. It is not proof that the store has received or verified the funds, and it does not by itself confirm the order. Keep the M-Pesa transaction message. Never share your M-Pesa PIN, password, or one-time sign-in code.",
                    "If you have paid but the order status has not changed, contact the seller with your order reference and transaction reference before sending another payment. See the Payment Policy for payment-reporting and discrepancy steps.",
                ]),
                ("Collection, delivery, and handover", [
                    "The store's current Contact, product, and checkout pages show the collection instructions, delivery area, and any listed fee. The seller must confirm whether the selected method is available for your order, the exact meeting point or delivery address, timing, and any fee before you rely on the arrangement.",
                    "Provide an accurate phone number and, for delivery, a usable address and location details. Do not travel to collect an order until the seller confirms that it is ready and agrees the meeting details. See Delivery and Collection for more information.",
                ]),
                ("Cancellations, returns, refunds, and consumer rights", [
                    "Contact the seller promptly if you want to change or cancel a request, or if an item is incorrect, damaged, or appears faulty. The Returns and Refunds page explains how to raise a request. Any cancellation, cooling-off right, refund, or other remedy provided by applicable law remains available; this policy does not shorten or waive it.",
                    "A request for a return or refund is reviewed against the order and applicable law. Do not send an item back until the store confirms the return method. See the Returns and Refunds Policy for practical reporting guidance.",
                ]),
                ("Website use, reviews, and content", [
                    "Use the site lawfully, do not interfere with its operation or security, and submit information you are authorised to provide. Reviews and other customer submissions must not be unlawful, misleading, abusive, or infringe another person's rights. Reviews may be checked before publication; avoid including private information in public content.",
                    "Product photos, descriptions, and site branding belong to their respective rights holders. Do not copy or reuse material without permission or another lawful basis.",
                ]),
                ("Changes, applicable law, and contact", [
                    "FityStores may update these pages when store processes or legal requirements change. The latest review date appears above. An update does not remove a right or remedy that already applies to an order.",
                    "These terms are subject to applicable Kenyan law, including mandatory consumer protections. If wording on this page conflicts with a right that applies by law, the law prevails. Contact the store through the Contact page with questions or concerns.",
                ]),
            ],
            "faqs": [
                ("Does submitting checkout confirm my order?", "No. It creates a request and records an inventory hold. Wait for the seller to confirm the items, final amount, payment, and fulfilment."),
                ("Does entering an M-Pesa reference confirm payment?", "No. The store checks M-Pesa reports manually. Keep your transaction message and contact the seller if the status needs checking."),
                ("Where can I ask about collection, delivery, or a return?", "Use the Contact page and include your order reference when asking about an existing order."),
            ],
            "sources": [
                ("Kenya Consumer Protection Act, 2012", "https://new.kenyalaw.org/akn/ke/act/2012/46/eng@2022-12-31"),
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
            "title": "Delivery and Collection",
            "description": "How to confirm collection or nearby delivery, fees, addresses, timing, and order handover.",
            "updated_on": updated_on,
            "notice": "Delivery availability, fee, location, and timing are confirmed for each order. Do not travel or rely on delivery until the seller confirms the arrangements.",
            "sections": [
                ("Available delivery area", [
                    settings.delivery_area or "Contact FityStores to check whether delivery is available for your location.",
                    "The area description is a guide, not a confirmation that a particular address can be served. The seller will confirm availability, the address or meeting point, and the expected handover arrangement for each order.",
                ]),
                ("Collection", [
                    settings.collection_instructions or "Contact the seller to confirm the collection point and arrangements.",
                    "Agree the exact meeting place and time with the seller. Wait for confirmation that your order is ready before travelling. The Contact page shows the current public collection reference.",
                ]),
                ("Delivery charges", [
                    ("The currently listed delivery fee is " + money(settings.delivery_fee_cents, settings.currency) + ". The seller must confirm that it applies to your address and order before payment.") if settings.delivery_fee_cents is not None else "No delivery fee is currently listed. The seller will confirm whether a fee applies to your address and order before payment.",
                    "Do not assume that a fee shown for one address or order applies to another. Any delivery charge should be included in the total confirmed with you before payment.",
                ]),
                ("Addresses, contact, and timing", [
                    "For delivery, provide a complete address, area, useful landmark, and reachable phone number. Tell the seller promptly if those details change. Keep your phone available for arrangement updates.",
                    "The store does not publish a guaranteed delivery or collection time. Agree the expected timing directly with the seller. If arrangements change or are delayed, contact the seller before travelling or making other plans.",
                ]),
                ("Order readiness and handover", [
                    "Checkout is an order request, not a confirmed delivery booking or collection appointment. The seller confirms the items, final total, payment status, fulfilment method, and handover details. Order tracking may show updates, but contact the seller if you need clarification.",
                    "If you need to change a delivery or collection arrangement, contact the store as soon as possible. Any replacement arrangement or additional charge must be discussed and confirmed before it is applied.",
                ]),
                ("Problems and consumer rights", [
                    "Contact the store if an order does not arrive as agreed or you cannot complete the collection. Applicable consumer rights and remedies are not limited by this page.",
                ]),
            ],
            "faqs": [
                ("Is delivery available to every address?", "No. Check the displayed delivery area and ask the seller to confirm your specific address."),
                ("Is the displayed delivery fee final?", "It is an indication only. The seller confirms whether it applies to your order before payment."),
                ("Should I travel as soon as I submit checkout?", "No. Wait for the seller to confirm that the order is ready and agree the meeting details."),
            ],
            "sources": [
                ("Kenya Consumer Protection Act, 2012", "https://new.kenyalaw.org/akn/ke/act/2012/46/eng@2022-12-31"),
            ],
        },
        "payment": {
            "title": "Payment Policy",
            "description": "Current payment instructions, manual M-Pesa reconciliation, payment reports, and what to do if a payment needs checking.",
            "updated_on": updated_on,
            "notice": "Payments are checked manually. Confirm the recipient and final amount with the seller before paying; a reference or screenshot is not payment confirmation.",
            "sections": [
                ("Current payment instructions", [
                    settings.payment_instructions or "Contact the seller to confirm the payment method, recipient, and amount for your order.",
                    "The site currently records M-Pesa payment reports for manual review; it does not automatically verify transfers. The payment number and instructions shown on the Contact and checkout pages may change, so confirm them for your order before sending money.",
                ]),
                ("Before making a payment", [
                    "Wait for the seller to confirm the item, selected options, availability, final total, and payment recipient. Check the details in the M-Pesa prompt or message before authorising a transfer.",
                    "Never share your M-Pesa PIN, account password, one-time code, or other sign-in credentials with the store or anyone claiming to represent it.",
                ]),
                ("Payment references and proof", [
                    "If you have already paid, enter the M-Pesa transaction reference at checkout as requested. An optional screenshot may help the seller investigate, but it does not replace the transaction record or automatically verify the transfer.",
                    "A submitted reference, screenshot, or WhatsApp message is not confirmation that payment was received, and it does not by itself mean the order is accepted. The seller updates payment status after checking the report.",
                ]),
                ("Missing, delayed, or duplicate payment reports", [
                    "Keep the M-Pesa confirmation message and your order reference. If a payment is missing, duplicated, incorrect, or not reflected in the order status, contact the seller with the transaction reference and amount.",
                    "Do not send another payment just because the website status has not changed. Ask the seller to check the first transaction before paying again. Never include your PIN or password in a message.",
                ]),
                ("Refunds and payment disputes", [
                    "If you believe money was sent in error, report it promptly and provide the transaction reference. A refund or reversal is not automatic; the seller must check the transaction and your order. For an order return or refund request, see the Returns and Refunds Policy. Any applicable legal right remains unaffected.",
                ]),
            ],
            "faqs": [
                ("Does checkout charge my M-Pesa account?", "No. Payment is made outside the website using instructions confirmed with the seller."),
                ("How long does payment verification take?", "No fixed verification time is promised. Contact the seller with your order and transaction references if you need an update."),
                ("Should I pay again if my status is still awaiting payment?", "No. Ask the seller to check the original transaction first."),
            ],
            "sources": [
                ("Kenya Consumer Protection Act, 2012", "https://new.kenyalaw.org/akn/ke/act/2012/46/eng@2022-12-31"),
            ],
        },
        "returns": {
            "title": "Returns, Refunds, and Cancellations",
            "description": "How to report an order issue, request cancellation or an exchange, and have a refund reviewed.",
            "updated_on": updated_on,
            "notice": "Contact FityStores promptly with your order reference. The suggested 48-hour reporting period below is not a deadline and does not limit rights or remedies provided by law.",
            "sections": [
                ("Report an incorrect, damaged, or faulty item", [
                    "Contact FityStores as soon as practical if an item is incorrect, damaged, or appears faulty. Prompt reporting, ideally within 48 hours of receiving it, helps the seller investigate while details are fresh. This is a request for prompt reporting, not a deadline or a condition that removes a right or remedy provided by law.",
                    "Include your order reference, the item and variant, when you received it, and a clear description of the issue. Photos may help the review. Keep the item and any relevant packaging where practical while the issue is being discussed.",
                ]),
                ("Cancellation or change of mind", [
                    "If you want to cancel or change an order, contact the seller as soon as possible. A request that has not yet been accepted or prepared may be easier to stop, but you should wait for confirmation that the change or cancellation has been recorded.",
                    "A change-of-mind return or exchange may need the seller's agreement, subject always to any cancellation, cooling-off, or other right that applies by law. This page does not exclude or shorten a statutory right.",
                ]),
                ("How a return or exchange is reviewed", [
                    "The seller will review the order details and the issue, then discuss the appropriate next step for the specific case. Do not send or hand over an item until the seller confirms the return method, destination, and any agreed arrangements.",
                    "Any delivery or return transport arrangement and any related charge will be discussed with you before proceeding. Nothing here determines who must bear a cost where applicable law provides otherwise.",
                    "A request begins a review; it does not by itself approve an exchange, return, or refund. This does not affect a remedy that the law requires.",
                ]),
                ("Refunds and payment corrections", [
                    "If a refund is agreed or required, the seller will confirm the amount, payment method, and next steps with you. The store does not promise automatic approval or a fixed processing time on this page.",
                    "If you report a duplicate or mistaken M-Pesa payment, provide the transaction reference so the seller can check it. A refund or reversal cannot be confirmed until the payment and order have been reviewed.",
                ]),
                ("How to contact the store", [
                    "Use the Contact page and include your order reference and a brief description of the request. Do not send your M-Pesa PIN, account password, or one-time sign-in code.",
                ]),
                ("Consumer rights", [
                    "Nothing in this policy limits mandatory consumer rights or remedies that apply under Kenyan law. If you are unsure which right applies, consult the relevant law or obtain independent legal advice.",
                ]),
            ],
            "faqs": [
                ("Is 48 hours the deadline for reporting a problem?", "No. It is a request to report promptly so the store can investigate. It is not a deadline and does not limit legal rights or remedies."),
                ("Does a refund request mean my refund is approved?", "No. The store reviews the order and confirms the outcome and next steps. Any remedy required by law remains unaffected."),
                ("Can I return an item without contacting the store?", "Contact the store first so it can confirm the return method and arrangements. This does not remove any legal right that applies."),
            ],
            "sources": [
                ("Kenya Consumer Protection Act, 2012", "https://new.kenyalaw.org/akn/ke/act/2012/46/eng@2022-12-31"),
            ],
        },
        "privacy": {
            "title": "Privacy Policy",
            "description": "What personal information FityStores handles, why it is used, how it is protected, and how to exercise your data rights.",
            "updated_on": updated_on,
            "notice": "Marketing is optional and separate from account, security, and order messages. You can contact the store about access, correction, objection, or deletion requests.",
            "sections": [
                ("Who is responsible and how to contact us", [
                    "FityStores is the shop name used on this website and identifies Oxy-Jen Tech as its operator on the About page. For a privacy question or request, use the Contact page and ask for the person responsible for data protection. Current contact details are published there; the website does not publish a postal address.",
                    "This notice explains the shop's current website practices. It should be read with the Kenya Data Protection Act, 2019 and the rights available under applicable law.",
                ]),
                ("Information we collect", [
                    "Depending on the features you use, the shop may process your name, email address, phone number, account and verification details, order items and quantities, order history, collection or delivery address and instructions, M-Pesa payer details, transaction reference, and any payment proof you choose to upload.",
                    "The shop also receives information you submit through contact, enquiry, review, suggestion, restock, and newsletter forms. Reviews may be displayed publicly after moderation; do not include private information in a review.",
                    "The site uses session and security information to operate accounts and forms. It may store cart selections and recently viewed products in your browser. A limited technical identifier or one-way IP hash may be used for session operation, abuse prevention, or review moderation. If you use Google sign-in, Google provides account details needed to authenticate you.",
                ]),
                ("How and why information is used", [
                    "The shop uses information to create and secure accounts, verify contact details, provide account and order services, validate availability and stock, review reported payments, arrange collection or delivery, respond to questions, moderate customer content, process requests, prevent misuse, maintain business records, and meet legal obligations.",
                    "Information is used where needed to provide a service you request, to meet legal requirements, with your consent where consent is required, and for legitimate operational and security interests where permitted by law. The shop aims to use information only for the purposes explained here or another compatible purpose allowed by law.",
                ]),
                ("Promotional messages and service communications", [
                    "Promotional email is optional and requires a separate marketing choice. Creating an account or accepting the Terms and Conditions does not subscribe you to marketing. Withdraw consent using the unsubscribe link or the available account preference controls.",
                    "The store may still send necessary account, verification, security, order, payment-status, and service messages. These are not promotional messages and may be needed to provide the account or service you requested.",
                ]),
                ("Cookies, sessions, and device storage", [
                    "The website uses session and security mechanisms needed for sign-in, form protection, and shopping functions. Cart and recently viewed product information may be stored in your browser or account to support shopping. You can clear browser storage, but some site features may then need to be set up again.",
                    "This notice does not state that advertising or analytics trackers are used. If a feature or provider changes how information is collected, this notice should be updated.",
                ]),
                ("Who information may be shared with", [
                    "Information may be processed by the website host, database, email delivery, authentication, and image hosting providers configured for the shop, but only as needed to operate those services. If you request delivery, relevant contact and address details may be shared with the person arranging or completing that delivery.",
                    "If you choose to contact the store through WhatsApp, your message and account details are handled by WhatsApp/Meta under its own privacy terms. Google handles information needed for Google sign-in when you choose that option. M-Pesa transfers take place outside this website; the store receives only the payment details you provide or that are otherwise available to it for reconciliation.",
                    "Some service providers may process information outside Kenya. Where a cross-border transfer occurs, the shop will apply the safeguards required by Kenyan law. Contact the store to ask which providers and safeguards apply to a particular service.",
                ]),
                ("How long information is kept and security", [
                    "Information is kept only for as long as reasonably necessary for the purposes above, account operation, order and financial records, dispute handling, security, and legal obligations. Different records may need different retention periods; the shop does not publish a fixed period for every record type. You may ask about a particular record or request deletion, subject to applicable retention requirements.",
                    "The shop uses account access controls and technical safeguards to protect information. No website, internet transmission, or storage system can be guaranteed completely secure. Contact the store promptly if you believe your account or information has been exposed.",
                ]),
                ("Your data protection rights and choices", [
                    "Subject to applicable law, you may ask to be informed about, access, or correct your personal information; object to processing; request deletion where available; and withdraw consent where processing relies on consent. You may also ask how your information is used and who it is shared with.",
                    "Contact the store through the Contact page and provide enough information to identify the account or request. The store may need to verify your identity before acting. Do not send a password, M-Pesa PIN, or one-time sign-in code. You may also raise a concern with the Office of the Data Protection Commissioner of Kenya.",
                ]),
                ("Children and information about other people", [
                    "If you are under 18, involve a parent or guardian when creating an account or placing an order. Do not submit another person's personal information unless you are authorised to do so. Do not put passwords, M-Pesa PINs, or unrelated sensitive information in reviews, enquiries, or order notes.",
                ]),
            ],
            "faqs": [
                ("Does account registration subscribe me to marketing?", "No. Promotional email is optional and requires a separate choice. Essential account and order messages may still be sent."),
                ("How can I request access or correction?", "Use the Contact page and ask for the person responsible for data protection. The store may verify your identity before responding."),
                ("Can I complain to the regulator?", "You may contact the Office of the Data Protection Commissioner of Kenya about a data protection concern."),
            ],
            "sources": [
                ("Kenya Data Protection Act, 2019", "https://kenyalaw.org/kl/fileadmin/pdfdownloads/Acts/2019/TheDataProtectionAct__No24of2019.pdf"),
                ("Office of the Data Protection Commissioner: Rights of a Data Subject", "https://www.odpc.go.ke/rights-of-a-data-subject/"),
            ],
        },
    }
    policy = policies[slug]
    policy["updated_on"] = updated_on
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


@public_bp.route("/service-worker.js")
def pwa_service_worker():
    response = current_app.send_static_file("pwa/service-worker.js")
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response


@public_bp.route("/.well-known/assetlinks.json")
def android_asset_links():
    """Publish only configured signing fingerprints; never invent release identity."""
    import re

    fingerprints = []
    raw = current_app.config.get("ANDROID_ASSETLINKS_SHA256_CERT_FINGERPRINTS", "")
    for value in (raw.split(",") if isinstance(raw, str) else raw or []):
        fingerprint = str(value).strip().upper()
        if re.fullmatch(r"(?:[0-9A-F]{2}:){31}[0-9A-F]{2}", fingerprint):
            fingerprints.append(fingerprint)
    payload = []
    package_id = str(current_app.config.get("ANDROID_PACKAGE_ID", "")).strip()
    if package_id and fingerprints:
        payload.append({
            "relation": ["delegate_permission/common.handle_all_urls"],
            "target": {
                "namespace": "android_app",
                "package_name": package_id,
                "sha256_cert_fingerprints": sorted(set(fingerprints)),
            },
        })
    response = current_app.response_class(
        current_app.json.dumps(payload), mimetype="application/json"
    )
    response.headers["Cache-Control"] = "public, max-age=300"
    return response

@public_bp.route("/sitemap.xml")
def sitemap():
    urls = [url_for("public.home", _external=True), url_for("public.shop", _external=True), url_for("public.upcoming", _external=True), url_for("public.contact", _external=True), url_for("public.about", _external=True), url_for("public.reviews", _external=True)]
    urls += [url_for("public.product_detail", slug=p.slug, _external=True) for p in Product.query.filter_by(active=True).all()]
    urls += [url_for("public.delivery_policy", _external=True), url_for("public.payment_policy", _external=True), url_for("public.returns_policy", _external=True), url_for("public.privacy_policy", _external=True), url_for("public.terms", _external=True)]
    body = "<?xml version='1.0' encoding='UTF-8'?><urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>" + "".join(f"<url><loc>{escape(u)}</loc></url>" for u in urls) + "</urlset>"
    return Response(body, mimetype="application/xml")










