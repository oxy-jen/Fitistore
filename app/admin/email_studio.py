"""Admin-only Email Studio and read-only authenticated cart activity view."""
import hashlib
import json
import re
from datetime import datetime, timedelta
from secrets import token_urlsafe
from uuid import UUID, uuid4
from urllib.parse import urlsplit

from flask import current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required
from sqlalchemy import or_
from sqlalchemy.orm import selectinload

from app.extensions import db
from app.models import (
    CustomerAccount, CustomerCart, CustomerConsentEvent, CustomerOrderLink,
    CustomerToken, EmailDraft, EmailMessage, EmailTemplate, FinancialAuditEvent,
    Order, OrderItem, Product,
)
from app.services.cart import validate_cart
from app.services.email_studio import EMAIL_CATEGORIES, EmailContentError, render_studio_message, validate_email_blocks, validate_variables
from app.services.formatting import money
from app.services.product_images import product_display_image, product_image_alt, product_image_url
from app.services.settings import current_settings

_PROMOTIONAL_COPY = re.compile(
    r"\b(?:discounts?|sales?|offers?|promotions?|coupons?|vouchers?|deals?|"
    r"new\s+arrivals|shop\s+now|buy\s+now|limited\s+time|save\s+\d+%|"
    r"featured\s+products|product\s+recommendations)\b",
    re.IGNORECASE,
)


def _requires_marketing_consent(category, subject, headline, message, blocks):
    if EMAIL_CATEGORIES[category]["marketing"]:
        return True
    if any(block["type"] in {"product_grid", "image_banner"} for block in blocks):
        return True
    copy = " ".join([subject or "", headline or "", message or ""] + [block.get("text", "") for block in blocks])
    return bool(_PROMOTIONAL_COPY.search(copy))


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
    if not account or not account.active:
        raise EmailContentError("Choose an active customer for this preview/context.")
    if not account.email_verified_at and not (
        category in {"promotion", "cart_reminder"} and account.marketing_status == "subscribed"
    ):
        raise EmailContentError("This email category requires a verified customer email address.")

    template = EMAIL_CATEGORIES[category]
    saved = None
    try:
        requested_template_id = int(form.get("template_id") or 0)
    except (TypeError, ValueError):
        requested_template_id = 0
    if requested_template_id:
        saved = EmailTemplate.query.filter_by(id=requested_template_id, category=category).first()
        if not saved:
            raise EmailContentError("Choose a saved template from the selected email category.")
    if not saved:
        saved = EmailTemplate.query.filter_by(category=category).order_by(EmailTemplate.id).first()
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

    order = None
    order_lines = []
    order_summary = None
    order_id = (form.get("order_id") or "").strip()
    if order_id and category not in {"order_confirmation", "thank_you", "order_status", "customer_service"}:
        raise EmailContentError("This category does not use order context.")
    if order_id:
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
        if cart.checkout_until and cart.checkout_until > datetime.utcnow():
            raise EmailContentError("This customer's checkout is active; no cart reminder can be sent.")
        if cart.updated_at and cart.updated_at > datetime.utcnow() - timedelta(hours=2):
            raise EmailContentError("This cart has been active within the last two hours.")
        if cart.reminder_sent_at:
            raise EmailContentError("A reminder was already sent for this cart episode.")
        try:
            validation = validate_cart(json.loads(cart.items_json))
        except (TypeError, ValueError):
            raise EmailContentError("The saved cart could not be read safely.") from None
        if not validation.lines:
            raise EmailContentError("The saved cart has no currently available items.")
        for line in validation.lines:
            item = _product_card(line.product)
            item["price"] = money(line.unit_price_cents, current_settings().currency)
            item["quantity"] = line.quantity
            item["variant_label"] = line.variant_label
            if line.variant:
                item["discount"] = False
                item["previous_price"] = ""
            products.append(item)
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
        if category == "promotion" and not products and any(block["type"] in {"product_grid", "image_banner"} for block in blocks):
            raise EmailContentError("Select at least one active, in-stock catalogue product for a promotion.")

    customer_name = (account.display_name or account.email.partition("@")[0]).strip().split()[0]
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
    promotional = _requires_marketing_consent(category, subject, headline, message, blocks)
    unsubscribe_url = ""
    if promotional:
        if account.marketing_status != "subscribed":
            raise EmailContentError("This address is not currently subscribed to marketing email.")
        if test_mode:
            context["unsubscribe_link"] = "[an unsubscribe link is included in a real send]"
        else:
            unsubscribe_url = unsubscribe_url or _issue_unsubscribe(account)
            context["unsubscribe_link"] = unsubscribe_url

    primary_url = _public_url(url_for("public.shop"))
    shop_url = _public_url(url_for("public.shop"))
    logo_url = _public_url(url_for("static", filename="images/mainLogo-160.png"))
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
            if block["type"] == "image_banner" and not product["image_url"]:
                raise EmailContentError("That catalogue product has no accessible HTTPS image. Choose a product with an uploaded image.")
        if block["type"] == "order_summary" and not (order_summary or cart_summary):
            # Optional order/cart sections disappear when this recipient has no corresponding data.
            continue
    rendered_subject, html, text = render_studio_message(
        category, subject, headline, message, context, blocks=blocks, products=products,
        order_lines=order_lines, order_summary=order_summary, cart_summary=cart_summary,
        primary_url=primary_url, primary_label=primary_label, shop_url=shop_url, logo_url=logo_url,
        unsubscribe_url=unsubscribe_url,
    )
    if test_mode:
        html = html.replace("<h1 style=", '<p style="padding:10px;background:#fff3cd;color:#664d03">TEST PREVIEW — this is not an order or payment notice.</p><h1 style=', 1)
        text = "TEST PREVIEW — this is not an order or payment notice.\n\n" + text
    return rendered_subject, html, text, order, products


def register_email_studio_routes(admin_bp):
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
                "python -m flask --app wsgi:app migrate-customer-email-schema, then run "
                "check-customer-email-schema. No migration runs from this page.",
                "error",
            )
            return redirect(url_for("admin.email_center"))
        category = request.values.get("category", "customer_service")
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
        search = request.values.get("product_search", "").strip()[:100]
        account_id = request.values.get("account_id", "").strip()
        accounts = CustomerAccount.query.order_by(CustomerAccount.display_name, CustomerAccount.email).limit(500).all()
        saved_templates = EmailTemplate.query.order_by(EmailTemplate.category, EmailTemplate.name, EmailTemplate.id).all()
        saved_template = selected_template or EmailTemplate.query.filter_by(category=category).order_by(EmailTemplate.id).first()
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
            editor_blocks = validate_email_blocks(blocks_source, category)
        except EmailContentError:
            editor_blocks = []
        if not editor_blocks:
            editor_blocks = [{"type": "heading", "text": headline, "align": "left"}, {"type": "paragraph", "text": message, "align": "left"}]
            if category == "promotion":
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
                "test_email": request.values.get("test_email", "").strip().lower(),
            }
            return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()

        if request.method == "POST":
            try:
                verify_editor_text()
                if action == "save_template":
                    if not subject.strip() or not message.strip():
                        raise EmailContentError("A reusable template needs both a subject and message.")
                    template_name = request.form.get("template_name", "").strip()[:120]
                    row = selected_template or saved_template or EmailTemplate(category=category)
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
                if action in {"preview", "send_test", "send"}:
                    selected_account = account
                    marketing_message = _requires_marketing_consent(
                        category, subject, headline, message,
                        validate_email_blocks(request.form.get("blocks_json", "[]"), category),
                    )
                    if action == "send":
                        if marketing_message:
                            requested_ids = []
                            for raw in request.form.getlist("account_ids"):
                                try: requested_ids.append(int(raw))
                                except (TypeError, ValueError): continue
                            requested_ids = list(dict.fromkeys(requested_ids))
                            if len(requested_ids) > 500:
                                raise EmailContentError("Select no more than 500 recipients in one synchronous send.")
                            if not requested_ids and selected_account and category != "promotion":
                                requested_ids = [selected_account.id]
                            targets = CustomerAccount.query.filter(
                                CustomerAccount.id.in_(requested_ids), CustomerAccount.active.is_(True),
                                CustomerAccount.marketing_status == "subscribed",
                            ).order_by(CustomerAccount.id).all() if requested_ids else []
                        else:
                            targets = [selected_account] if selected_account and selected_account.active and selected_account.email_verified_at else []
                        if not targets:
                            raise EmailContentError("Select at least one eligible recipient. Marketing recipients must be active and subscribed.")
                        phrase = f"SEND {len(targets)}"
                        if request.form.get("confirm_send", "").strip() != phrase:
                            raise EmailContentError(f"Review the preview and type {phrase} to confirm the exact recipient count.")
                        if session.get("email_studio_preview") != preview_fingerprint():
                            raise EmailContentError("Preview this exact content and recipient selection before sending.")
                        configured_now, note_now = mail_configuration_status()
                        if not configured_now:
                            raise EmailContentError("Email was not sent: " + note_now)
                        try: operation_id = str(UUID(request.form.get("operation_id", "")))
                        except (ValueError, TypeError, AttributeError):
                            raise EmailContentError("Refresh the Email Studio and try again; the send operation key is invalid.") from None
                        accepted = failed = suppressed = 0
                        for target in targets:
                            if defaults["marketing"] and category == "cart_reminder":
                                context_form = request.form.copy()
                                context_form["order_id"] = ""
                            else:
                                context_form = request.form
                            try:
                                payload_subject, html, text, order, _cards = _render_for(category, context_form, target)
                            except EmailContentError:
                                suppressed += 1
                                continue
                            kind = "marketing_abandoned_cart" if category == "cart_reminder" else "marketing_campaign" if marketing_message else "admin_" + category
                            if category == "cart_reminder":
                                cart = CustomerCart.query.filter_by(account_id=target.id).first()
                                key = f"cart-reminder:{cart.episode_key}" if cart else f"email-studio:{operation_id}:{target.id}"
                            else:
                                key = f"email-studio:{operation_id}:{target.id}"
                            try:
                                result = queue_email(target.email, payload_subject, kind, key, account_id=target.id, order_id=order.id if order else None, template_id=saved_template.id if saved_template else None, html_body=html, text_body=text)
                            except Exception:
                                db.session.rollback(); failed += 1; continue
                            if result.status in {"sent", "delivered"}: accepted += 1
                            elif result.status == "suppressed": suppressed += 1
                            else: failed += 1
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
                        flash(f"Manual send result: provider accepted {accepted}; failed {failed}; suppressed {suppressed}. Provider acceptance is not inbox delivery.", "success" if accepted and not failed and not suppressed else "warning")
                        session.pop("email_studio_preview", None)
                        return redirect(url_for("admin.email_studio", category=category))

                    if action == "send_test":
                        target_email = request.form.get("test_email", "").strip()
                        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", target_email) or len(target_email) > 254:
                            raise EmailContentError("Enter an email address you control for this test.")
                        if request.form.get("confirm_test", "").strip() != "SEND TEST":
                            raise EmailContentError("Type SEND TEST to confirm the isolated test message.")
                        if session.get("email_studio_preview") != preview_fingerprint():
                            raise EmailContentError("Preview this exact test message before sending it.")
                        configured_now, note_now = mail_configuration_status()
                        if not configured_now:
                            raise EmailContentError("Test was not sent: " + note_now)
                        if not selected_account:
                            raise EmailContentError("Choose a customer context for the preview; test emails never choose one automatically.")
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
                        flash("Test result: " + row.status + "." + detail + " Provider acceptance is not inbox delivery.", "success" if row.status == "sent" else "warning")
                        return redirect(url_for("admin.email_studio", category=category))

                    if not selected_account:
                        raise EmailContentError("Choose a customer context before previewing this category.")
                    rendered_subject, html, text, order, cards = _render_for(category, request.form, selected_account, test_mode=True)
                    eligible_count = 1
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
                    preview = {"subject": rendered_subject, "html": html, "text": text, "products": cards, "order": order, "eligible_count": eligible_count}
                    session["email_studio_preview"] = preview_fingerprint()
                else:
                    raise EmailContentError("Choose a supported Email Studio action.")
            except (EmailContentError, ValueError, TypeError) as exc:
                db.session.rollback()
                flash(str(exc), "error")

        # Carry a one-time operation identifier into the form; it never triggers work on GET.
        operation_id = str(uuid4())
        return render_template(
            "admin/email_studio.html", title="Email Studio", categories=EMAIL_CATEGORIES,
            category=category, accounts=accounts, account=account, linked_orders=linked_orders,
            templates=saved_templates,
            selected_template=saved_template, editor_blocks=editor_blocks,
            products=products, product_choices=product_choices, drafts=drafts, carts=carts, selected_products=set(map(str, product_selection)),
            selected_account_ids=set(map(str, selected_account_ids)),
            subject=subject, headline=headline, message=message, draft=draft, preview=preview,
            configured=configured, provider_note=provider_note, operation_id=operation_id,
            product_search=search,
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
            elif age_hours is not None and age_hours < 2: reason = "Cart activity is less than two hours old."
            elif not validation or not validation.lines: reason = "No currently available cart items."
            elif cart.reminder_sent_at: reason = "A reminder was previously sent for this cart episode."
            rows.append({"cart": cart, "validation": validation, "age_hours": age_hours, "reason": reason, "eligible": not reason})
        return render_template("admin/email_cart_activity.html", title="Cart activity", rows=rows, state=state, search=search, pagination=pagination, now_utc=datetime.utcnow())
