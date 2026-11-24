"""Admin-only Email Studio and read-only authenticated cart activity view."""
import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from secrets import token_urlsafe
from uuid import UUID, uuid4
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, or_
from sqlalchemy.orm import selectinload

from app.extensions import db
from app.models import (
    CustomerAccount, CustomerCart, CustomerConsentEvent, CustomerOrderLink,
    CustomerToken, EmailDraft, EmailMessage, EmailTemplate, FinancialAuditEvent,
    Order, OrderItem, Product,
)
from app.services.cart import validate_cart
from app.services.email_studio import (
    EMAIL_CATEGORIES, EMAIL_CATEGORIES_REQUIRING_REAL_CONTEXT,
    EMAIL_MARKETING_CATEGORY_KEYS, EMAIL_PRODUCT_CATEGORY_KEYS,
    SAMPLE_CUSTOMER_CONTEXT, EmailContentError, render_studio_message,
    validate_email_blocks, validate_variables,
)
from app.services.email_purpose import studio_email_kind_for_category, STUDIO_TRANSACTIONAL_CATEGORY_KINDS
from app.services.email_template_inventory import SYSTEM_TEMPLATES, build_email_template_inventory
from app.services.formatting import money
from app.services.product_images import product_display_image, product_image_alt, product_image_url
from app.services.settings import current_settings
from app.services.whatsapp import build_general_message, whatsapp_text_link

logger = logging.getLogger(__name__)

def _is_server_defined_transactional_content(category, form, *, blocks=None):
    """Allow transactional Studio sends only for unchanged code-defined notices.

    A category label, saved template, draft, or admin-authored block never grants
    transactional privileges. The order itself is reloaded and ownership checked
    by ``_render_for`` before a message can be queued.
    """
    defaults = EMAIL_CATEGORIES.get(category)
    if category not in STUDIO_TRANSACTIONAL_CATEGORY_KINDS or not defaults:
        return False
    if any(str(form.get(key) or "").strip() for key in ("template_id", "draft_id")):
        return False
    try:
        if form.getlist("product_ids"):
            return False
    except AttributeError:
        pass
    if (form.get("subject", defaults["subject"]) != defaults["subject"]
            or form.get("headline", defaults["headline"]) != defaults["headline"]
            or form.get("message", defaults["message"]) != defaults["message"]):
        return False
    if blocks is None:
        try:
            blocks = validate_email_blocks(form.get("blocks_json", "[]"), category)
        except EmailContentError:
            return False
    design_blocks = [block for block in blocks if block["type"] == "design"]
    if any(
        block.get("preheader") != "A note from FityStores"
        or block.get("footer_note") != "Bei Fity, Fits Poa"
        for block in design_blocks
    ):
        return False
    content_blocks = [block for block in blocks if block["type"] != "design"]
    if not content_blocks:
        return True
    expected_blocks = [
        {"type": "heading", "text": defaults["headline"], "align": "left"},
        {"type": "paragraph", "text": defaults["message"], "align": "left"},
        {"type": "order_summary"},
        {"type": "button", "text": "View order", "target": "order"},
        {"type": "contact", "text": "Questions?", "align": "left"},
    ]
    return content_blocks == expected_blocks


def _requires_marketing_consent(category, subject, headline, message, blocks, *, trusted_transactional=False):
    """Classify editable Studio content conservatively as marketing."""
    if category in STUDIO_TRANSACTIONAL_CATEGORY_KINDS:
        return not trusted_transactional
    return bool(EMAIL_CATEGORIES.get(category, {}).get("marketing", True))


def _public_url(path):
    base = (current_app.config.get("PUBLIC_BASE_URL") or "").rstrip("/")
    parsed_base = urlsplit(base)
    if parsed_base.scheme != "https" or not parsed_base.netloc:
        raise EmailContentError("Set PUBLIC_BASE_URL to the canonical HTTPS store address before previewing or sending.")
    parsed = urlsplit(path or "")
    if parsed.scheme and parsed.netloc:
        path = parsed.path + (("?" + parsed.query) if parsed.query else "")
    return base + "/" + (path or "").lstrip("/")


def _product_card(product):
    image = product_display_image(product)
    image_url = product_image_url(image) if image else ""
    if image_url.startswith("/"):
        image_url = _public_url(image_url)
    elif image_url.startswith("//"):
        image_url = "https:" + image_url
    if not image_url.startswith("https://"):
        image_url = ""
    variant_prices = [variant.effective_price_cents for variant in product.variants if variant.active and variant.available_quantity > 0 and variant.effective_price_cents is not None]
    if variant_prices:
        low, high = min(variant_prices), max(variant_prices)
        current_price = money(low, current_settings().currency) if low == high else f"{money(low, current_settings().currency)} – {money(high, current_settings().currency)}"
    else:
        current_price = money(product.price_cents, current_settings().currency)
    discount = bool(not variant_prices and product.previous_price_cents is not None and product.price_cents is not None and product.previous_price_cents > product.price_cents)
    return {
        "id": product.id,
        "name": product.name,
        "price": current_price,
        "current_price_cents": product.price_cents,
        "previous_price": money(product.previous_price_cents, current_settings().currency) if discount else "",
        "discount": discount,
        "image_url": image_url,
        "image_alt": product_image_alt(image, f"Product image: {product.name}") if image else f"Product image: {product.name}",
        "url": _public_url(url_for("public.product_detail", slug=product.slug)),
        "category": product.category.name if product.category else "",
        "description": (product.description or "").strip(),
        "stock_quantity": product.display_stock_quantity,
        "sku": product.sku or "",
    }


def _issue_unsubscribe(account):
    raw = token_urlsafe(32)
    db.session.add(CustomerToken(
        account_id=account.id, purpose="unsubscribe",
        token_hash=hashlib.sha256(raw.encode()).hexdigest(),
        expires_at=datetime.utcnow() + timedelta(days=3650),
    ))
    db.session.commit()
    return _public_url(url_for("customer.unsubscribe", token=raw))


def _account_for_preview(raw_id):
    if not raw_id:
        return None
    try:
        return CustomerAccount.query.filter_by(id=int(raw_id), active=True).first()
    except (TypeError, ValueError):
        return None


def _render_for(category, form, account, *, test_mode=False, unsubscribe_url=""):
    """Re-fetch and validate all structured context from the database."""
    if not account and (
        not test_mode or category in EMAIL_CATEGORIES_REQUIRING_REAL_CONTEXT
    ):
        raise EmailContentError("Choose an actual customer for this email context.")
    if account and not account.active:
        raise EmailContentError("Choose an active customer for this preview/context.")
    if account and not account.email_verified_at and not (
        category in EMAIL_MARKETING_CATEGORY_KEYS and account.marketing_status == "subscribed"
    ):
        raise EmailContentError("This email category requires a verified customer email address.")

    template = EMAIL_CATEGORIES[category]
    trusted_transactional_request = _is_server_defined_transactional_content(category, form)
    saved = None
    try:
        requested_template_id = int(form.get("template_id") or 0)
    except (TypeError, ValueError):
        requested_template_id = 0
    if requested_template_id:
        saved = EmailTemplate.query.filter_by(id=requested_template_id, category=category).first()
        if not saved:
            raise EmailContentError("Choose a saved template from the selected email category.")
    if not saved and not trusted_transactional_request:
        saved = EmailTemplate.query.filter_by(category=category, name="BEI FITS Promotional Default").first() if category == "promotion" else EmailTemplate.query.filter_by(category=category).order_by(EmailTemplate.id).first()
    default = template
    subject = (form.get("subject") if "subject" in form else (saved.subject if saved else default["subject"])) or ""
    headline = (form.get("headline") if "headline" in form else (saved.headline if saved else default["headline"])) or ""
    message = (form.get("message") if "message" in form else (saved.message if saved else default["message"])) or ""
    blocks_source = form.get("blocks_json") if "blocks_json" in form else (saved.blocks_json if saved else "[]")
    if not subject.strip() or not message.strip():
        raise EmailContentError("Enter both a subject and a message before previewing or sending.")
    try:
        validate_variables(subject, category)
        validate_variables(headline, category)
        validate_variables(message, category)
        blocks = validate_email_blocks(blocks_source, category)
    except EmailContentError:
        raise
    design = next((block for block in blocks if block["type"] == "design"), None)
    blocks = [block for block in blocks if block["type"] != "design"]
    if category == "cart_reminder":
        # Cart items are one personalized section, not repeated for each image/grid
        # block saved in the visual template. Keep its first product-section position.
        normalized_blocks = []
        inserted_cart_items = False
        for block in blocks:
            if block["type"] in {"image_banner", "product_grid", "order_summary"}:
                if not inserted_cart_items:
                    normalized_blocks.append({"type": "product_grid"})
                    inserted_cart_items = True
                continue
            normalized_blocks.append(block)
        if not inserted_cart_items:
            insert_at = next((index for index, block in enumerate(normalized_blocks) if block["type"] in {"order_summary", "button", "contact"}), len(normalized_blocks))
            normalized_blocks.insert(insert_at, {"type": "product_grid"})
        blocks = normalized_blocks

    order = None
    order_lines = []
    order_summary = None
    order_id = (form.get("order_id") or "").strip()
    if order_id and category not in {"order_confirmation", "thank_you", "order_status", "customer_service"}:
        raise EmailContentError("This category does not use order context.")
    if order_id:
        if not account:
            raise EmailContentError("Select an actual customer for linked order context.")
        try:
            order = Order.query.join(CustomerOrderLink, CustomerOrderLink.order_id == Order.id).filter(
                Order.id == int(order_id), CustomerOrderLink.account_id == account.id,
            ).options(selectinload(Order.items).selectinload(OrderItem.product)).first()
        except (TypeError, ValueError):
            order = None
        if not order:
            raise EmailContentError("The selected order is not linked to this customer.")
        for line in order.items:
            product = line.product
            image = product_display_image(product) if product else None
            image_url = product_image_url(image) if image else ""
            if image_url.startswith("/"):
                image_url = _public_url(image_url)
            elif image_url.startswith("//"):
                image_url = "https:" + image_url
            if not image_url.startswith("https://"):
                image_url = ""
            subtotal = line.subtotal_cents
            if subtotal is None and line.price_cents is not None:
                subtotal = line.price_cents * line.quantity
            order_lines.append({
                "name": line.product_name,
                "quantity": line.quantity,
                "total": money(subtotal, current_settings().currency),
                "image_url": image_url,
                "image_alt": product_image_alt(image, line.product_name) if image else line.product_name,
                "url": _public_url(url_for("public.product_detail", slug=product.slug)) if product and product.active else "",
            })
        subtotal_values = [line.subtotal_cents if line.subtotal_cents is not None else line.price_cents * line.quantity if line.price_cents is not None else None for line in order.items]
        order_summary = {
            "reference": order.reference,
            "date": order.created_at.strftime("%Y-%m-%d") if order.created_at else "",
            "status": order.status or "",
            "payment_status": order.payment_status or "",
            "fulfilment_method": order.fulfilment_method or "",
            "subtotal": money(sum(subtotal_values), current_settings().currency) if all(value is not None for value in subtotal_values) else "Price to be confirmed",
            "discount": money(order.discount_cents, current_settings().currency),
            "delivery": money(order.delivery_fee_cents, current_settings().currency),
            "total": money(order.total_cents, current_settings().currency),
            "instructions": order.instructions.strip() if order.instructions and order.instructions.strip() else "",
        }
    if category in {"order_confirmation", "thank_you", "order_status"} and not order:
        raise EmailContentError("Select an order linked to the chosen customer.")

    products = []
    cart_lines = []
    cart_summary = None
    if category == "cart_reminder":
        cart = CustomerCart.query.filter_by(account_id=account.id).first()
        if not cart or not cart.items_json or cart.items_json == "[]":
            raise EmailContentError("This customer has no saved cart items.")
        if not test_mode and cart.checkout_until and cart.checkout_until > datetime.utcnow():
            raise EmailContentError("This customer's checkout is active; no cart reminder can be sent.")
        idle_minutes = max(1, int(current_app.config.get("CART_REMINDER_IDLE_MINUTES", 5)))
        if not test_mode and cart.updated_at and cart.updated_at > datetime.utcnow() - timedelta(minutes=idle_minutes):
            raise EmailContentError(f"This cart has been active within the last {idle_minutes} minutes.")
        if not test_mode and cart.reminder_sent_at:
            raise EmailContentError("A reminder was already sent for this cart episode.")
        try:
            validation = validate_cart(json.loads(cart.items_json))
        except (TypeError, ValueError):
            raise EmailContentError("The saved cart could not be read safely.") from None
        if not validation.lines:
            raise EmailContentError("The saved cart has no currently available items.")
        grouped_cart_products = {}
        for line in validation.lines:
            item = _product_card(line.product)
            item["price"] = money(line.unit_price_cents, current_settings().currency)
            item["quantity"] = line.quantity
            item["variant_label"] = line.variant_label
            item["line_total"] = money(line.subtotal_cents, current_settings().currency) if line.subtotal_cents is not None else "Price to be confirmed"
            item["_line_total_cents"] = line.subtotal_cents
            if line.variant:
                item["discount"] = False
                item["previous_price"] = ""
            grouping_key = (line.product.id, line.variant.id if line.variant else None)
            existing = grouped_cart_products.get(grouping_key)
            if existing:
                existing["quantity"] += line.quantity
                if existing["_line_total_cents"] is None or line.subtotal_cents is None:
                    existing["_line_total_cents"] = None
                    existing["line_total"] = "Price to be confirmed"
                else:
                    existing["_line_total_cents"] += line.subtotal_cents
                    existing["line_total"] = money(existing["_line_total_cents"], current_settings().currency)
            else:
                grouped_cart_products[grouping_key] = item
        products = list(grouped_cart_products.values())
        # A reminder must reflect the recipient's own cart. Visual product blocks
        # follow that live cart instead of retaining a stale template product ID.
        if products:
            first_cart_product_id = products[0]["id"]
            for block in blocks:
                if block["type"] == "image_banner" or block["type"] == "button" and block.get("target") == "product":
                    block["product_id"] = first_cart_product_id
        cart_lines = validation.lines
        cart_total = money(validation.total_cents, current_settings().currency) if validation.total_cents is not None else None
        cart_summary = {
            "item_count": sum(line.quantity for line in cart_lines),
            "total": cart_total,
        }
    else:
        cart_total = None
        product_ids = []
        for raw_id in form.getlist("product_ids"):
            try:
                product_ids.append(int(raw_id))
            except (TypeError, ValueError):
                raise EmailContentError("One or more selected product IDs are invalid.") from None
        product_ids = list(dict.fromkeys(product_ids))[:30]
        if product_ids:
            found = Product.query.options(selectinload(Product.images), selectinload(Product.variants), selectinload(Product.category)).filter(
                Product.id.in_(product_ids), Product.active.is_(True),
            ).all()
            by_id = {product.id: product for product in found}
            if len(by_id) != len(product_ids) or any(product.display_stock_quantity <= 0 for product in found):
                raise EmailContentError("A selected product is inactive, unavailable, or no longer in stock. Refresh the picker.")
            products = [_product_card(by_id[product_id]) for product_id in product_ids]
        if category in EMAIL_PRODUCT_CATEGORY_KEYS and not products and any(block["type"] in {"product_grid", "image_banner"} for block in blocks):
            raise EmailContentError("Select at least one active, in-stock catalogue product for this campaign.")

    customer_name = (
        (account.display_name or account.email.partition("@")[0]).strip().split()[0]
        if account else SAMPLE_CUSTOMER_CONTEXT["customer_name"]
    )
    context = {"customer_name": customer_name}
    if order:
        context.update({
            "order_reference": order.reference,
            "order_date": order.created_at.strftime("%Y-%m-%d") if order.created_at else None,
            "order_subtotal": order_summary["subtotal"],
            "discount_amount": order_summary["discount"],
            "delivery_fee": order_summary["delivery"],
            "order_total": money(order.total_cents, current_settings().currency) if order.total_cents is not None else None,
            "payment_status": order.payment_status,
            "order_status": order.status,
            "fulfilment_method": order.fulfilment_method,
            "fulfilment_instructions": order.instructions.strip() if order.instructions and order.instructions.strip() else None,
        })
    if category == "cart_reminder":
        context["cart_item_count"] = sum(line.quantity for line in cart_lines)
        context["cart_total"] = cart_total
    trusted_transactional = _is_server_defined_transactional_content(category, form, blocks=blocks)
    promotional = _requires_marketing_consent(
        category, subject, headline, message, blocks,
        trusted_transactional=trusted_transactional,
    )
    unsubscribe_url = ""
    if promotional:
        if account and account.marketing_status != "subscribed" and not test_mode:
            raise EmailContentError("This address is not currently subscribed to marketing email.")
        if test_mode:
            context["unsubscribe_link"] = "[an unsubscribe link is included in a real send]"
        else:
            if not account:
                raise EmailContentError("A real customer account is required for marketing delivery.")
            unsubscribe_url = unsubscribe_url or _issue_unsubscribe(account)
            context["unsubscribe_link"] = unsubscribe_url

    primary_url = _public_url(url_for("public.shop"))
    shop_url = _public_url(url_for("public.shop"))
    logo_url = _public_url(url_for("static", filename="images/mainLogo-160.png"))
    hero_image_url = _public_url(url_for("static", filename="images/shop-hero/email-removebg-preview.png"))
    primary_label = "Continue shopping"
    if order:
        primary_url = _public_url(url_for("public.track_order_link", reference=order.reference, tracking_token=order.tracking_token))
        primary_label = "View order status"
    elif category == "cart_reminder":
        primary_url = _public_url(url_for("public.cart"))
        primary_label = "Return to your cart"
    product_map = {product["id"]: product for product in products}
    for block in blocks:
        if block["type"] == "button" and block.get("target") == "order" and not order:
            raise EmailContentError("Choose an order linked to this customer before adding an order-tracking button.")
        if block["type"] == "button" and block.get("target") == "cart" and category != "cart_reminder":
            raise EmailContentError("Cart buttons are only available in cart-reminder emails.")
        if block["type"] in {"image_banner", "button"} and block.get("product_id"):
            product = product_map.get(block["product_id"])
            if not product:
                raise EmailContentError("Add each banner or product-button item to the selected catalogue products before previewing.")
            if block["type"] == "image_banner" and not product["image_url"] and category != "cart_reminder":
                raise EmailContentError("That catalogue product has no accessible HTTPS image. Choose a product with an uploaded image.")
        if block["type"] == "order_summary" and not (order_summary or cart_summary):
            # Optional order/cart sections disappear when this recipient has no corresponding data.
            continue
    rendered_subject, html, text = render_studio_message(
        category, subject, headline, message, context, blocks=blocks, products=products,
        order_lines=order_lines, order_summary=order_summary, cart_summary=cart_summary,
        primary_url=primary_url, primary_label=primary_label, shop_url=shop_url, logo_url=logo_url,
        site_url=_public_url("/"), contact_url=_public_url(url_for("public.contact")),
        unsubscribe_url=unsubscribe_url, design=design,
        hero_image_url=hero_image_url,
        about_url=_public_url(url_for("public.about")),
        preferences_url=_public_url(url_for("customer.dashboard")),
        whatsapp_url=whatsapp_text_link(current_settings(), build_general_message(current_settings())),
        social_icon_urls={name: _public_url(url_for("static", filename=f"images/email-icons/fa-{name}.png")) for name in ("facebook", "instagram", "tiktok", "whatsapp")},
    )
    if test_mode:
        html = html.replace("<h1 style=", '<p style="padding:10px;background:#fff3cd;color:#664d03">TEST PREVIEW — this is not an order or payment notice.</p><h1 style=', 1)
        text = "TEST PREVIEW — this is not an order or payment notice.\n\n" + text
    return rendered_subject, html, text, order, products


def register_email_studio_routes(admin_bp):
    @admin_bp.get("/email/studio/system-preview/<template_id>")
    @login_required
    def email_studio_system_preview(template_id):
        """Render a fixed sample of an existing system template; never queues mail."""
        from flask import abort
        from types import SimpleNamespace

        definition = next((item for item in SYSTEM_TEMPLATES if item["id"] == template_id), None)
        if not definition or not definition["implemented"] or not definition["preview"]:
            abort(404)
        if template_id == "order_received":
            from flask import render_template as render
            from datetime import datetime
            html = render(
                "emails/order_received.html", preheader="Sample preview only; no email was sent.",
                site_url="https://fitystore.top", logo_url="https://fitystore.top/static/images/mainLogo-160.png",
                support_url="https://fitystore.top/contact", support_label="Contact support",
                customer_name="Sample Customer", customer_email="sample@example.invalid", customer_phone="Not provided",
                order=SimpleNamespace(reference="SAMPLE-ORDER", status="Order received"),
                line_count=1, item_count=1,
                items=[{"image_url": "", "image_alt": "", "name": "Sample catalogue item", "variant": "", "sku": "", "unit_price": "KSh 100", "quantity": 1, "line_total": "KSh 100"}],
                subtotal="KSh 100", discount="KSh 0", delivery_fee="To be confirmed", total="KSh 100",
                show_discount=False, show_delivery=False, paid=False, created_at=datetime(2026, 1, 1),
                payment_method="Not recorded", payment_status="Awaiting payment", fulfillment="To be confirmed",
                delivery_heading="Delivery details", delivery_details="The store will confirm the delivery details with you.",
                payment_guidance="Sample payment guidance.", fulfillment_guidance="Sample fulfillment guidance.",
                stages=[{"label":"Order received","description":"Sample status","state":"current"}],
                tracking_url="https://fitystore.top/track-order", contact_url="https://fitystore.top/contact",
            )
            text = "Sample order received preview. No email was sent."
        else:
            from app.services.customer_mail import render_branded_email
            copy = {
                "verify_email": ("Verify your email address", ["Sample verification link. No real token is created."]),
                "welcome": ("Welcome to FityStores", ["This is a static template preview. No email was sent."]),
                "verify_code": ("Verify your FityStores account", ["Sample code only. Never use this code to sign in."]),
                "login_code": ("Your FityStores sign-in code", ["Sample code only. Never use this code to sign in."]),
                "reset_password": ("Reset your password", ["Sample reset link. No real token is created."]),
                "account_security": ("Your password was changed", ["This is a static security notice preview."]),
                "marketing_confirm": ("Confirm promotional email preferences", ["Sample consent confirmation. No subscription is changed."]),
                "unsubscribe_confirmation": ("Promotional email stopped", ["Sample unsubscribe confirmation. No preference is changed."]),
                "payment_confirmed": ("Payment confirmed", ["Sample verified payment confirmation."]),
                "payment_failed": ("Payment status needs your attention", ["Sample notice: FityStores recorded the order payment status as failed. If you have already paid, contact the store before making another payment."]),
                "restock_notification": ("Your requested item is available", ["Sample notice: the item you requested is available again. This message is only about your restock request."]),
                "refund_confirmation": ("Refund completed", ["Sample notice: a refund was recorded as completed.", "Recorded refund amount: KSh 100.", "Refund reference: SAMPLE-REFUND."]),
                "order_status": ("Your order status was updated", ["Sample order status update."]),
                "review_request": ("How was your order?", ["Sample request for feedback on an actual transaction."]),
                "thank_you": ("Thank you for shopping with FityStores", ["Sample order-linked thank-you message."]),
            }
            heading, paragraphs = copy[template_id]
            html, text = render_branded_email(
                heading, paragraphs, kind=template_id,
                button_url="https://fitystore.top/" if template_id not in {"verify_code", "login_code"} else "",
                button_text="Open FityStores" if template_id not in {"verify_code", "login_code"} else "",
                security_code="123456" if template_id in {"verify_code", "login_code"} else "",
                order_reference="SAMPLE-ORDER" if template_id in {"order_status", "review_request", "thank_you"} else "",
            )
        return render_template(
            "admin/email_template_preview.html", title=definition["name"],
            definition=definition, html=html, text=text,
        )

    @admin_bp.get("/email/studio-messages")
    @login_required
    def email_studio_messages():
        from app.services.email_studio import EMAIL_CATEGORIES

        from sqlalchemy import inspect
        from app.admin.routes import model_schema_missing

        inspector = inspect(db.engine)
        missing = sorted({item for model in (EmailTemplate, EmailDraft)
                          for item in model_schema_missing(model, inspector)})
        schema_ready = not missing
        if schema_ready:
            templates = EmailTemplate.query.order_by(
                EmailTemplate.category, EmailTemplate.updated_at.desc(), EmailTemplate.id.desc()
            ).all()
            drafts = EmailDraft.query.filter_by(
                created_by_id=current_user.id, status="draft"
            ).order_by(EmailDraft.updated_at.desc(), EmailDraft.id.desc()).limit(100).all()
        else:
            # Do not let an unapplied local schema migration turn this admin
            # page into a 500. The migration remains an explicit operator step.
            templates, drafts = [], []
        return render_template(
            "admin/studio_messages.html", title="Studio messages",
            categories=EMAIL_CATEGORIES, templates=templates, drafts=drafts,
            inventory=build_email_template_inventory(EMAIL_CATEGORIES),
            schema_ready=schema_ready, missing_schema=missing,
        )

    @admin_bp.route("/email/studio", methods=["GET", "POST"])
    @login_required
    def email_studio():
        from app.admin.routes import model_schema_missing
        from app.services.customer_mail import mail_configuration_status, queue_email
        from app.services.email_studio import validate_email_blocks, validate_variables

        required = (CustomerAccount, CustomerCart, CustomerOrderLink, CustomerToken, EmailDraft, EmailMessage, EmailTemplate, FinancialAuditEvent, Order, Product)
        from sqlalchemy import inspect
        inspector = inspect(db.engine)
        missing = sorted({item for model in required for item in model_schema_missing(model, inspector)})
        if missing:
            flash(
                "Email Studio is unavailable because the active database is missing: "
                + ", ".join(missing)
                + ". After confirming the database and a restorable backup, run "
                "python -m flask --app manage:app migrate-customer-email-schema, then run "
                "check-customer-email-schema. No migration runs from this page.",
                "error",
            )
            return redirect(url_for("admin.email_center"))
        category = request.values.get("category", "customer_service")
        preview_size = request.values.get("preview_size", "desktop")
        if preview_size not in {"desktop", "mobile"}:
            preview_size = "desktop"
        raw_template_id = request.values.get("template_id", "").strip()
        try:
            requested_template_id = int(raw_template_id) if raw_template_id else None
        except ValueError:
            requested_template_id = None
        selected_template = db.session.get(EmailTemplate, requested_template_id) if requested_template_id else None
        if selected_template and request.method == "GET":
            category = selected_template.category
        elif selected_template and selected_template.category != category:
            flash("The selected template does not match this email category.", "error")
            return redirect(url_for("admin.email_studio", category="customer_service"))
        if request.method == "POST" and not request.values.get("category"):
            flash("Choose a supported Email Studio category. Nothing was sent.", "error")
            return redirect(url_for("admin.email_studio"))
        if category not in EMAIL_CATEGORIES:
            if request.method == "POST":
                flash("Choose a supported Email Studio category. Nothing was sent.", "error")
                return redirect(url_for("admin.email_studio"))
            category = "customer_service"
        if not selected_template and category == "promotion":
            selected_template = EmailTemplate.query.filter_by(
                category="promotion", name="BEI FITS Promotional Default"
            ).first()
        search = request.values.get("product_search", "").strip()[:100]
        account_id = request.values.get("account_id", "").strip()
        accounts = CustomerAccount.query.order_by(CustomerAccount.display_name, CustomerAccount.email).limit(500).all()
        saved_templates = EmailTemplate.query.order_by(EmailTemplate.category, EmailTemplate.name, EmailTemplate.id).all()
        if selected_template:
            saved_template = selected_template
        elif category == "promotion":
            saved_template = EmailTemplate.query.filter_by(category="promotion", name="BEI FITS Promotional Default").first()
        elif category in STUDIO_TRANSACTIONAL_CATEGORY_KINDS:
            # Transactional defaults are code-backed; a saved admin template
            # must be explicitly selected and will then require marketing consent.
            saved_template = None
        else:
            saved_template = EmailTemplate.query.filter_by(category=category).order_by(EmailTemplate.id).first()
        defaults = EMAIL_CATEGORIES[category]
        draft_id = request.values.get("draft_id", "").strip()
        draft = None
        if draft_id:
            try:
                draft = EmailDraft.query.filter_by(id=int(draft_id), created_by_id=current_user.id, status="draft").first()
            except (TypeError, ValueError):
                draft = None
        selected_account_ids = request.values.getlist("account_ids")
        if draft and "account_ids" not in request.values:
            try:
                saved_recipient_data = json.loads(draft.account_ids_json or "[]")
            except (TypeError, ValueError):
                saved_recipient_data = []
            if isinstance(saved_recipient_data, dict):
                selected_account_ids = [str(value) for value in saved_recipient_data.get("recipients", [])]
                if not account_id:
                    account_id = str(saved_recipient_data.get("preview_account_id") or "")
            elif isinstance(saved_recipient_data, list):
                selected_account_ids = [str(value) for value in saved_recipient_data]
        account = _account_for_preview(account_id)
        subject = request.values.get("subject", draft.subject if draft else saved_template.subject if saved_template else defaults["subject"])
        headline = request.values.get("headline", draft.headline if draft else saved_template.headline if saved_template else defaults["headline"])
        message = request.values.get("message", draft.message if draft else saved_template.message if saved_template else defaults["message"])
        blocks_source = request.values.get("blocks_json", draft.blocks_json if draft else saved_template.blocks_json if saved_template else "[]")
        try:
            editor_document = validate_email_blocks(blocks_source, category)
        except EmailContentError:
            editor_document = []
        editor_design = next((block for block in editor_document if block["type"] == "design"), {"type": "design", "hero_background": "#F0F2F8", "text_color": "#172033", "accent_color": "#ED1B2F", "section_background": "#FFFFFF", "footer_note": "Bei Fity, Fits Poa", "preheader": "A note from FityStores"})
        if category != "promotion":
            editor_design = {
                **editor_design,
                "hero_background": "#F0F2F8",
                "text_color": "#172033",
                "accent_color": "#ED1B36",
                "section_background": "#FFFFFF",
            }
        editor_blocks = [block for block in editor_document if block["type"] != "design"]
        if not editor_blocks:
            editor_blocks = [{"type": "heading", "text": headline, "align": "left"}, {"type": "paragraph", "text": message, "align": "left"}]
            if category in EMAIL_PRODUCT_CATEGORY_KEYS:
                editor_blocks.append({"type": "product_grid"})
            if category == "cart_reminder":
                editor_blocks.extend([{"type": "product_grid"}, {"type": "order_summary"}])
            if category in {"order_confirmation", "thank_you", "order_status"}:
                editor_blocks.append({"type": "order_summary"})
            order_category = category in {"order_confirmation", "thank_you", "order_status"}
            editor_blocks.extend([{"type": "button", "text": "View order" if order_category else "Return to your cart" if category == "cart_reminder" else "View details", "target": "order" if order_category else "cart" if category == "cart_reminder" else "shop"}, {"type": "contact", "text": "Questions?", "align": "left"}])
        if draft and "product_ids" not in request.values:
            product_selection = json.loads(draft.product_ids_json or "[]")
        else:
            product_selection = request.values.getlist("product_ids")
        product_query = Product.query.filter(Product.active.is_(True))
        if search:
            product_query = product_query.filter(Product.name.ilike(f"%{search}%"))
        products = [product for product in product_query.options(selectinload(Product.variants), selectinload(Product.images), selectinload(Product.category)).order_by(Product.name).limit(300).all() if product.display_stock_quantity > 0][:100]
        product_choices = [_product_card(product) for product in products]
        linked_orders = []
        if account:
            linked_orders = [link.order for link in CustomerOrderLink.query.filter_by(account_id=account.id).order_by(CustomerOrderLink.created_at.desc()).limit(50).all()]
        carts = CustomerCart.query.join(CustomerAccount).filter(CustomerCart.items_json != "[]").order_by(CustomerCart.updated_at.desc()).limit(100).all()
        drafts = EmailDraft.query.filter_by(created_by_id=current_user.id, status="draft").order_by(EmailDraft.updated_at.desc()).limit(50).all()
        configured, provider_note = mail_configuration_status()
        preview = None
        action = request.form.get("action") if request.method == "POST" else ""

        def verify_editor_text():
            for value in (subject, headline, message):
                validate_variables(value, category)
            validate_email_blocks(request.form.get("blocks_json", "[]"), category)

        def preview_fingerprint():
            fields = {
                "category": category, "subject": subject, "headline": headline,
                "message": message, "account_id": account_id,
                "template_id": request.values.get("template_id", ""),
                "blocks_json": request.values.get("blocks_json", "[]"),
                "order_id": request.values.get("order_id", ""),
                "products": sorted(str(value) for value in request.values.getlist("product_ids")),
                "accounts": sorted(str(value) for value in request.values.getlist("account_ids")),
                "test_email": (current_app.config.get("MAIL_TEST_ADDRESS") or "").strip().lower(),
            }
            return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()

        if request.method == "POST":
            try:
                verify_editor_text()
                if action == "save_template":
                    if not subject.strip() or not message.strip():
                        raise EmailContentError("A reusable template needs both a subject and message.")
                    template_name = request.form.get("template_name", "").strip()[:120]
                    row = selected_template or saved_template or EmailTemplate(category=category, name="BEI FITS Promotional Default" if category == "promotion" else "Saved template")
                    row.category = category
                    if template_name:
                        row.name = template_name
                    row.subject, row.headline, row.message = subject[:200], headline[:200], message[:5000]
                    row.blocks_json = json.dumps(validate_email_blocks(request.form.get("blocks_json", "[]"), category))
                    db.session.add(row); db.session.commit()
                    flash("Reusable template saved. No email was sent.", "success")
                    return redirect(url_for("admin.email_studio", category=category, template_id=row.id))
                if action == "save_template_as":
                    template_name = request.form.get("template_name", "").strip()[:120]
                    if not template_name or not subject.strip() or not message.strip():
                        raise EmailContentError("Enter a name, subject, and message to save a new template.")
                    row = EmailTemplate(
                        category=category, name=template_name, subject=subject[:200],
                        headline=headline[:200], message=message[:5000],
                        blocks_json=json.dumps(validate_email_blocks(request.form.get("blocks_json", "[]"), category)),
                    )
                    db.session.add(row); db.session.commit()
                    flash("New reusable template saved. No email was sent.", "success")
                    return redirect(url_for("admin.email_studio", category=category, template_id=row.id))
                if action == "duplicate_template":
                    template_name = request.form.get("template_name", "").strip()[:120]
                    if not template_name:
                        raise EmailContentError("Give the duplicated template a name before saving it.")
                    row = EmailTemplate(
                        category=category, name=template_name, subject=subject[:200],
                        headline=headline[:200], message=message[:5000],
                        blocks_json=json.dumps(validate_email_blocks(request.form.get("blocks_json", "[]"), category)),
                    )
                    db.session.add(row); db.session.commit()
                    flash("Template duplicated. Edit and reuse this saved copy.", "success")
                    return redirect(url_for("admin.email_studio", category=category, template_id=row.id))
                if action == "reset_template":
                    if request.form.get("confirm_reset") != "RESET":
                        raise EmailContentError("Type RESET to confirm returning this category to its code-backed default.")
                    if saved_template:
                        db.session.delete(saved_template); db.session.commit()
                    flash("Reusable template reset to its default. No email was sent.", "success")
                    return redirect(url_for("admin.email_studio", category=category))
                if action == "save_draft":
                    name = request.form.get("draft_name", "").strip()[:120]
                    if not name or not subject.strip() or not message.strip():
                        raise EmailContentError("A draft name, subject, and message are required.")
                    selected_accounts = []
                    for raw in request.form.getlist("account_ids"):
                        try: selected_accounts.append(int(raw))
                        except (TypeError, ValueError): continue
                    selected_products = []
                    for raw in request.form.getlist("product_ids"):
                        try: selected_products.append(int(raw))
                        except (TypeError, ValueError): continue
                    row = draft or EmailDraft(name=name, created_by_id=current_user.id)
                    row.name, row.category = name, category
                    row.subject, row.headline, row.message = subject[:200], headline[:200], message[:5000]
                    row.blocks_json = json.dumps(validate_email_blocks(request.form.get("blocks_json", "[]"), category))
                    row.product_ids_json = json.dumps(list(dict.fromkeys(selected_products))[:30])
                    row.account_ids_json = json.dumps({
                        "recipients": list(dict.fromkeys(selected_accounts))[:300],
                        "preview_account_id": account.id if account else None,
                    })
                    try: row.order_id = int(request.values.get("order_id") or "") or None
                    except (TypeError, ValueError): row.order_id = None
                    try: row.cart_id = int(request.values.get("cart_id") or "") or None
                    except (TypeError, ValueError): row.cart_id = None
                    db.session.add(row); db.session.commit()
                    flash("Draft saved. No email was sent.", "success")
                    return redirect(url_for("admin.email_studio", category=category, draft_id=row.id, account_id=account_id))
                if action == "delete_draft":
                    try: requested_draft_id = int(request.form.get("draft_id", "0"))
                    except (TypeError, ValueError): requested_draft_id = 0
                    row = EmailDraft.query.filter_by(id=requested_draft_id, created_by_id=current_user.id, status="draft").first()
                    if not row or request.form.get("confirm_delete") != "DELETE":
                        raise EmailContentError("Choose one of your drafts and type DELETE to confirm.")
                    db.session.delete(row); db.session.commit()
                    flash("Draft deleted. No email was sent.", "success")
                    return redirect(url_for("admin.email_studio", category=category))
                if action in {"preview", "send_test", "send", "schedule"}:
                    selected_account = account
                    submitted_blocks = validate_email_blocks(request.form.get("blocks_json", "[]"), category)
                    trusted_transactional = _is_server_defined_transactional_content(
                        category, request.form, blocks=submitted_blocks,
                    )
                    marketing_message = _requires_marketing_consent(
                        category, subject, headline, message, submitted_blocks,
                        trusted_transactional=trusted_transactional,
                    )
                    if action in {"send", "schedule"}:
                        if marketing_message:
                            raise EmailContentError(
                                "Marketing sends and scheduling now use the isolated marketing campaign centre. "
                                "Nothing was queued."
                            )
                        scheduled_at = None
                        if action == "schedule":
                            if not marketing_message:
                                raise EmailContentError("Only marketing campaigns can be scheduled; transactional messages remain immediate.")
                            raw_schedule = request.form.get("schedule_at", "").strip()
                            try:
                                local_schedule = datetime.strptime(raw_schedule, "%Y-%m-%dT%H:%M")
                                zone_name = current_app.config.get("TIMEZONE", "Africa/Nairobi")
                                try:
                                    zone = ZoneInfo(zone_name)
                                except (ZoneInfoNotFoundError, TypeError, ValueError):
                                    zone = ZoneInfo("Africa/Nairobi")
                                scheduled_at = local_schedule.replace(tzinfo=zone).astimezone(timezone.utc).replace(tzinfo=None)
                            except (TypeError, ValueError, OverflowError):
                                raise EmailContentError("Choose a valid future date and time for the campaign.") from None
                            now_utc = datetime.utcnow()
                            if scheduled_at < now_utc + timedelta(minutes=5) or scheduled_at > now_utc + timedelta(days=90):
                                raise EmailContentError("Schedule campaigns at least 5 minutes ahead and within the next 90 days.")
                        if marketing_message:
                            requested_ids = []
                            for raw in request.form.getlist("account_ids"):
                                try: requested_ids.append(int(raw))
                                except (TypeError, ValueError): continue
                            requested_ids = list(dict.fromkeys(requested_ids))
                            if len(requested_ids) > 500:
                                raise EmailContentError("Select no more than 500 recipients in one synchronous send.")
                            if not requested_ids and selected_account and category in {"cart_reminder", "customer_service"}:
                                requested_ids = [selected_account.id]
                            candidates = CustomerAccount.query.filter(
                                CustomerAccount.id.in_(requested_ids), CustomerAccount.active.is_(True),
                                CustomerAccount.marketing_status == "subscribed",
                            ).order_by(CustomerAccount.id).all() if requested_ids else []
                            from app.services.marketing_eligibility import evaluate_marketing_eligibility
                            targets = [candidate for candidate in candidates if evaluate_marketing_eligibility(candidate).eligible]
                        else:
                            targets = [selected_account] if selected_account and selected_account.active and selected_account.email_verified_at else []
                        if not targets:
                            raise EmailContentError(
                                "Select at least one active, verified subscribed customer who remains eligible "
                                "after consent, unsubscribe, suppression, and frequency checks."
                            )
                        phrase = f"SCHEDULE {len(targets)}" if action == "schedule" else f"SEND {len(targets)}"
                        confirmation_field = "confirm_schedule" if action == "schedule" else "confirm_send"
                        if request.form.get(confirmation_field, "").strip() != phrase:
                            raise EmailContentError(f"Review the preview and type {phrase} to confirm the exact recipient count.")
                        if session.get("email_studio_preview") != preview_fingerprint():
                            raise EmailContentError("Preview this exact content and recipient selection before sending.")
                        configured_now, note_now = mail_configuration_status()
                        if not configured_now:
                            raise EmailContentError("Email was not sent: " + note_now)
                        try: operation_id = str(UUID(request.form.get("operation_id", "")))
                        except (ValueError, TypeError, AttributeError):
                            raise EmailContentError("Refresh the Email Studio and try again; the send operation key is invalid.") from None
                        accepted = queued = failed = suppressed = duplicate = 0
                        suppression_reasons = []
                        provider_failures = []
                        for target in targets:
                            if defaults["marketing"] and category == "cart_reminder":
                                context_form = request.form.copy()
                                context_form["order_id"] = ""
                            else:
                                context_form = request.form
                            try:
                                payload_subject, html, text, order, _cards = _render_for(category, context_form, target)
                            except EmailContentError as exc:
                                suppressed += 1
                                suppression_reasons.append(str(exc))
                                continue
                            kind = studio_email_kind_for_category(category, marketing=marketing_message)
                            if not kind:
                                raise EmailContentError("This Email Studio purpose has no trusted server-side delivery classification.")
                            if category == "cart_reminder":
                                cart = CustomerCart.query.filter_by(account_id=target.id).first()
                                key = f"cart-reminder:{cart.episode_key}" if cart else f"email-studio:{operation_id}:{target.id}"
                            else:
                                key = f"email-studio:{operation_id}:{target.id}"
                            prior_message = EmailMessage.query.filter_by(idempotency_key=key).first()
                            prior_accepted = bool(prior_message and prior_message.status in {"sent", "delivered"})
                            try:
                                result = queue_email(
                                    target.email, payload_subject, kind, key,
                                    account_id=target.id, order_id=order.id if order else None,
                                    template_id=saved_template.id if saved_template else None,
                                    retry_suppressed=(category == "cart_reminder"),
                                    not_before=scheduled_at,
                                    html_body=html, text_body=text,
                                )
                            except Exception:
                                logger.exception("email_studio_manual_send_failed category=%s account_id=%s", category, target.id)
                                db.session.rollback(); failed += 1; provider_failures.append(f"{target.email}: internal send error; check server logs."); continue
                            if result.status in {"sent", "delivered"} and prior_accepted: duplicate += 1
                            elif result.status in {"sent", "delivered"}: accepted += 1
                            elif result.status == "queued": queued += 1
                            elif result.status == "suppressed":
                                suppressed += 1
                                if result.failure_reason:
                                    suppression_reasons.append(f"{target.email}: {result.failure_reason[:180]}")
                            else:
                                failed += 1
                                if result.failure_reason:
                                    provider_failures.append(f"{target.email}: {result.failure_reason[:180]}")
                            if category == "cart_reminder" and result.status in {"sent", "delivered"}:
                                cart = CustomerCart.query.filter_by(account_id=target.id).first()
                                if cart:
                                    last_activity = cart.updated_at
                                    db.session.query(CustomerCart).filter_by(id=cart.id).update({
                                        "reminder_sent_at": datetime.utcnow(),
                                        "updated_at": last_activity,
                                    }, synchronize_session=False)
                                    db.session.expire(cart)
                                    db.session.commit()
                        suppression_detail = " Reasons: " + "; ".join(dict.fromkeys(suppression_reasons[:3])) if suppression_reasons else ""
                        delivery_detail = " Errors: " + "; ".join(dict.fromkeys(provider_failures[:3])) if provider_failures else ""
                        duplicate_detail = f" {duplicate} already accepted earlier; no duplicate message was sent." if duplicate else ""
                        if action == "schedule":
                            flash(f"Campaign schedule result: {queued} queued for {scheduled_at.strftime('%Y-%m-%d %H:%M UTC')}; failed {failed}; suppressed {suppressed}.{duplicate_detail}{suppression_detail}{delivery_detail} Scheduled messages are not sent until their time and have not been provider-accepted.", "success" if queued and not failed and not suppressed else "warning")
                        else:
                            flash(f"Manual send result: provider accepted {accepted}; awaiting recovery {queued}; failed {failed}; suppressed {suppressed}.{duplicate_detail}{suppression_detail}{delivery_detail} Provider acceptance is not confirmation of inbox delivery.", "success" if (accepted or queued) and not failed and not suppressed else "warning")
                        session.pop("email_studio_preview", None)
                        return redirect(url_for("admin.email_studio", category=category))

                    if action == "send_test":
                        target_email = (current_app.config.get("MAIL_TEST_ADDRESS") or "").strip().lower()
                        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", target_email) or len(target_email) > 254:
                            raise EmailContentError("Set MAIL_TEST_ADDRESS to a valid address you control before sending a test.")
                        if CustomerAccount.query.filter(func.lower(CustomerAccount.email) == target_email).first():
                            raise EmailContentError("MAIL_TEST_ADDRESS matches a customer account; change it before sending a test.")
                        if request.form.get("confirm_test", "").strip() != "SEND TEST":
                            raise EmailContentError("Type SEND TEST to confirm the isolated test message.")
                        if session.get("email_studio_preview") != preview_fingerprint():
                            raise EmailContentError("Preview this exact test message before sending it.")
                        configured_now, note_now = mail_configuration_status()
                        if not configured_now:
                            raise EmailContentError("Test was not sent: " + note_now)
                        if not selected_account and category in EMAIL_CATEGORIES_REQUIRING_REAL_CONTEXT:
                            raise EmailContentError("Choose the actual customer and cart/order context before testing this category.")
                        recent_tests = FinancialAuditEvent.query.filter_by(admin_id=current_user.id, event_type="email_studio_test").filter(
                            FinancialAuditEvent.created_at >= datetime.utcnow() - timedelta(hours=1)
                        ).count()
                        if recent_tests >= 3:
                            raise EmailContentError("Test-email limit reached. Try again later.")
                        rendered_subject, html, text, order, _cards = _render_for(category, request.form, selected_account, test_mode=True)
                        html = html.replace("<title>", "<title>[TEST PREVIEW] ", 1)
                        try:
                            row = queue_email(target_email, "[TEST PREVIEW] " + rendered_subject, "admin_test", "email-studio-test:" + token_urlsafe(24), account_id=None, order_id=order.id if order else None, template_id=saved_template.id if saved_template else None, html_body=html, text_body="TEST PREVIEW ONLY\n\n" + text)
                        except Exception:
                            db.session.rollback()
                            raise EmailContentError("The provider did not accept the test message.") from None
                        db.session.add(FinancialAuditEvent(admin=current_user, event_type="email_studio_test", entity_type="EmailMessage", entity_id=row.id, note="Email Studio test status=" + row.status))
                        db.session.commit()
                        detail = (" " + row.failure_reason) if row.failure_reason else ""
                        flash("Test result: " + ("queued for delivery" if row.status == "queued" else row.status) + "." + detail + (" The message is not yet provider-accepted or delivered." if row.status == "queued" else " Provider acceptance is not inbox delivery."), "success" if row.status in {"sent", "queued"} else "warning")
                        return redirect(url_for("admin.email_studio", category=category))

                    rendered_subject, html, text, order, cards = _render_for(category, request.form, selected_account, test_mode=True)
                    eligible_count = 1 if selected_account else 0
                    if marketing_message:
                        try:
                            selected_ids = list(dict.fromkeys(int(value) for value in request.form.getlist("account_ids")))
                        except (TypeError, ValueError):
                            selected_ids = []
                        if not selected_ids and selected_account and category != "promotion":
                            selected_ids = [selected_account.id]
                        targets = CustomerAccount.query.filter(
                            CustomerAccount.id.in_(selected_ids), CustomerAccount.active.is_(True),
                            CustomerAccount.marketing_status == "subscribed",
                        ).order_by(CustomerAccount.id).all() if selected_ids else []
                        eligible_count = 0
                        for target in targets:
                            try:
                                _render_for(category, request.form, target, test_mode=True)
                                eligible_count += 1
                            except EmailContentError:
                                continue
                    preview_notice = ""
                    if category == "cart_reminder":
                        preview_cart = CustomerCart.query.filter_by(account_id=selected_account.id).first()
                        if preview_cart and preview_cart.checkout_until and preview_cart.checkout_until > datetime.utcnow():
                            preview_notice = "Preview only: checkout is active for this cart, so it cannot be sent yet."
                        elif preview_cart and preview_cart.updated_at and preview_cart.updated_at > datetime.utcnow() - timedelta(minutes=max(1, int(current_app.config.get("CART_REMINDER_IDLE_MINUTES", 5)))):
                            preview_notice = f"Preview only: this cart needs {max(1, int(current_app.config.get('CART_REMINDER_IDLE_MINUTES', 5)))} minutes without activity before a reminder can be sent."
                        elif preview_cart and preview_cart.reminder_sent_at:
                            preview_notice = "Preview only: a reminder was already sent for this cart episode."
                    preview = {"subject": rendered_subject, "html": html, "text": text, "products": cards, "order": order, "eligible_count": eligible_count, "notice": preview_notice}
                    session["email_studio_preview"] = preview_fingerprint()
                else:
                    raise EmailContentError("Choose a supported Email Studio action.")
            except (EmailContentError, ValueError, TypeError) as exc:
                db.session.rollback()
                flash(str(exc), "error")

        # Carry a one-time operation identifier into the form; it never triggers work on GET.
        operation_id = str(uuid4())
        editor_marketing_message = _requires_marketing_consent(
            category, subject, headline, message, editor_blocks,
            trusted_transactional=_is_server_defined_transactional_content(
                category, request.values, blocks=editor_blocks,
            ),
        )
        return render_template(
            "admin/email_studio.html", title="Email Studio", categories=EMAIL_CATEGORIES,
            category=category, accounts=accounts, account=account, linked_orders=linked_orders,
            templates=saved_templates,
            selected_template=saved_template, editor_blocks=editor_blocks,
            editor_design=editor_design,
            products=products, product_choices=product_choices, drafts=drafts, carts=carts, selected_products=set(map(str, product_selection)),
            selected_account_ids=set(map(str, selected_account_ids)),
            subject=subject, headline=headline, message=message, draft=draft, preview=preview,
            preview_size=preview_size,
            configured=configured, provider_note=provider_note, operation_id=operation_id,
            test_address=(current_app.config.get("MAIL_TEST_ADDRESS") or "").strip(),
            product_search=search, email_is_marketing=editor_marketing_message,
            requires_customer_context=(
                category in EMAIL_CATEGORIES_REQUIRING_REAL_CONTEXT
                or bool(request.values.get("order_id"))
            ),
            sample_customer_context=SAMPLE_CUSTOMER_CONTEXT,
        )

    @admin_bp.get("/email/cart-activity")
    @login_required
    def email_cart_activity():
        from app.admin.routes import model_schema_ready
        if not all(model_schema_ready(model) for model in (CustomerCart, CustomerAccount)):
            flash("Cart activity is unavailable until the customer/email schema is migrated.", "warning")
            return redirect(url_for("admin.email_center"))
        state = request.args.get("state", "active")
        search = request.args.get("q", "").strip()[:120]
        query = CustomerCart.query.join(CustomerAccount)
        if search:
            term = f"%{search}%"
            query = query.filter(or_(CustomerAccount.email.ilike(term), CustomerAccount.display_name.ilike(term)))
        if state == "active":
            query = query.filter(CustomerCart.items_json != "[]")
        elif state == "cleared":
            query = query.filter(CustomerCart.items_json == "[]")
        pagination = query.order_by(CustomerCart.updated_at.desc()).paginate(page=max(1, request.args.get("page", 1, type=int)), per_page=50, error_out=False)
        carts = pagination.items
        rows = []
        for cart in carts:
            try: validation = validate_cart(json.loads(cart.items_json or "[]"))
            except (TypeError, ValueError): validation = None
            age_hours = max(0, int((datetime.utcnow() - cart.updated_at).total_seconds() // 3600)) if cart.updated_at else None
            reason = ""
            if not cart.items_json or cart.items_json == "[]": reason = "Cart cleared or completed; no items to send."
            elif cart.account.marketing_status != "subscribed": reason = "Customer has not subscribed to marketing."
            elif not cart.account.email_verified_at: reason = "Customer email is not verified."
            elif not cart.account.active: reason = "Customer account is inactive."
            elif cart.checkout_until and cart.checkout_until > datetime.utcnow(): reason = "Checkout is active."
            elif cart.updated_at and cart.updated_at > datetime.utcnow() - timedelta(minutes=max(1, int(current_app.config.get("CART_REMINDER_IDLE_MINUTES", 5)))): reason = f"Cart activity is less than {max(1, int(current_app.config.get('CART_REMINDER_IDLE_MINUTES', 5)))} minutes old."
            elif not validation or not validation.lines: reason = "No currently available cart items."
            elif cart.reminder_sent_at: reason = "A reminder was previously sent for this cart episode."
            rows.append({"cart": cart, "validation": validation, "age_hours": age_hours, "reason": reason, "eligible": not reason})
        return render_template("admin/email_cart_activity.html", title="Cart activity", rows=rows, state=state, search=search, pagination=pagination, now_utc=datetime.utcnow())
