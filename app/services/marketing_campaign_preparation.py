"""Prepare review-only marketing campaign drafts; never queues or sends mail."""
from __future__ import annotations

import html
import json
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    AdminUser, CustomerAccount, CustomerCart, CustomerOrderLink,
    EmailCampaign, InventoryMovement, Order, OrderItem, Product,
)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _description(value: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]*>", " ", value or "")).split())[:1200]


def _available(product: Product) -> bool:
    return bool(product.active and product.display_stock_quantity > 0)


def _cart_products(cart: CustomerCart) -> list[Product]:
    try:
        from app.services.cart import validate_cart
        validation = validate_cart(json.loads(cart.items_json or "[]"))
    except (TypeError, ValueError):
        return []
    products = []
    for line in validation.lines:
        if line.product and line.product.id not in {item.id for item in products} and _available(line.product) and line.stock_quantity > 0:
            products.append(line.product)
    return products[:5]


def cart_has_terminal_order(cart: CustomerCart, products: list[Product]) -> bool:
    """Exclude a cart episode if a linked terminal order contains one of its items."""
    product_ids = [product.id for product in products]
    if not product_ids:
        return True
    terminal = func.lower(Order.status).in_(("completed", "cancelled")) | func.lower(Order.payment_status).in_(("cancelled", "refunded"))
    return bool(
        CustomerOrderLink.query.join(Order, Order.id == CustomerOrderLink.order_id)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .filter(
            CustomerOrderLink.account_id == cart.account_id,
            Order.created_at >= cart.updated_at,
            OrderItem.product_id.in_(product_ids), terminal,
        ).first()
    )


def _cart_threshold() -> timedelta:
    from flask import current_app
    minutes = max(5, min(10080, int(current_app.config.get("EMAIL_MARKETING_ABANDONED_CART_IDLE_MINUTES", 180))))
    return timedelta(minutes=minutes)


def cart_is_preparable(cart: CustomerCart, now: datetime) -> tuple[bool, str, list[Product]]:
    if cart.checkout_until and cart.checkout_until > now:
        return False, "checkout_in_progress", []
    if cart.updated_at > now - _cart_threshold():
        return False, "cart_not_idle", []
    products = _cart_products(cart)
    if not products:
        return False, "no_available_cart_items", []
    if cart_has_terminal_order(cart, products):
        return False, "terminal_order_exists", []
    return True, "", products


def _campaign_key(source: str, reference: str) -> str:
    return f"prepare:{source}:{reference}"


def _save_generated_draft(*, admin: AdminUser, source: str, key_ref: str,
                          name: str, subject: str, body: str,
                          campaign_type: str, products: list[Product],
                          reason: str, reference: dict | None = None) -> bool:
    key = _campaign_key(source, key_ref)
    if EmailCampaign.query.filter_by(generation_key=key).first():
        return False
    campaign = EmailCampaign(
        name=name[:120], subject=subject[:200], body=body[:5000],
        campaign_type=campaign_type,
        product_ids_json=json.dumps([product.id for product in products[:10]]),
        status="draft", created_by_id=admin.id,
        generation_key=key, generation_source=source,
        generation_reason=reason[:255],
        generation_reference=json.dumps(reference, separators=(",", ":")) if reference else None,
    )
    try:
        with db.session.begin_nested():
            db.session.add(campaign)
            db.session.flush()
    except IntegrityError:
        # The unique partial index is the cross-worker idempotency authority.
        return False
    return True


def _eligible_cart_accounts(now: datetime):
    from app.services.marketing_eligibility import evaluate_marketing_eligibility
    last_id = 0
    while True:
        carts = CustomerCart.query.join(CustomerAccount).filter(
            CustomerCart.id > last_id, CustomerAccount.active.is_(True),
            CustomerCart.items_json != "[]",
            CustomerCart.updated_at <= now - _cart_threshold(),
            (CustomerCart.checkout_until.is_(None)) | (CustomerCart.checkout_until <= now),
        ).order_by(CustomerCart.id).limit(100).all()
        if not carts:
            break
        for cart in carts:
            last_id = cart.id
            account = db.session.get(CustomerAccount, cart.account_id)
            if not account or not evaluate_marketing_eligibility(account, now=now).eligible:
                continue
            valid, _reason, products = cart_is_preparable(cart, now)
            if valid:
                yield cart, products


def prepare_marketing_campaign_drafts(*, now: datetime | None = None) -> dict:
    """Create only unscheduled drafts when the separate preparation gate is on."""
    from flask import current_app
    if not current_app.config.get("EMAIL_MARKETING_PREPARATION_ENABLED", False):
        return {"status": "disabled", "created": 0,
                "new_arrivals": 0, "abandoned_carts": 0, "weekly_picks": 0}

    now = now or _now()
    admin = AdminUser.query.order_by(AdminUser.id).first()
    if not admin:
        return {"status": "no_admin", "created": 0,
                "new_arrivals": 0, "abandoned_carts": 0, "weekly_picks": 0}

    created = {"new_arrivals": 0, "abandoned_carts": 0, "weekly_picks": 0}
    lookback_days = max(1, min(30, int(current_app.config.get("EMAIL_MARKETING_NEW_ARRIVAL_LOOKBACK_DAYS", 7))))
    lookback_start = now - timedelta(days=lookback_days)
    last_product_id = 0
    while created["new_arrivals"] < 200:
        recent_products = Product.query.filter(
            Product.id > last_product_id, Product.active.is_(True),
            or_(
                Product.created_at >= lookback_start,
                Product.id.in_(select(InventoryMovement.product_id).where(
                    InventoryMovement.created_at >= lookback_start,
                    InventoryMovement.change > 0,
                )),
            ),
        ).order_by(Product.id).limit(100).all()
        if not recent_products:
            break
        for product in recent_products:
            last_product_id = product.id
            if not _available(product):
                continue
            description = _description(product.description)
            factual_body = f"New product: {product.name}."
            if description:
                factual_body += "\n\n" + description
            added_recently = product.created_at >= lookback_start
            reason = (
                f"Recently listed and currently available within the {lookback_days}-day arrival window."
                if added_recently else
                f"A positive inventory movement was recorded within the {lookback_days}-day window; the product is currently available."
            )
            if _save_generated_draft(
                admin=admin, source="new_arrival", key_ref=f"product:{product.id}",
                name=f"New arrival: {product.name}", subject=f"New at FityStores: {product.name}",
                body=factual_body, campaign_type="new_product", products=[product],
                reason=reason,
            ):
                created["new_arrivals"] += 1
                if created["new_arrivals"] >= 200:
                    break

    for cart, products in _eligible_cart_accounts(now):
        episode = (cart.episode_key or cart.updated_at.isoformat())[:64]
        key_ref = f"cart:{cart.id}:{episode}"
        reason = f"Cart inactive for at least {int(_cart_threshold().total_seconds() // 60)} minutes; {len(products)} currently available item(s)."
        if _save_generated_draft(
            admin=admin, source="abandoned_cart", key_ref=key_ref,
            name=f"Abandoned cart follow-up — {now:%Y-%m-%d} #{cart.id}",
            subject="Still interested in your FityStores picks?",
            body="Your cart is still available to review. Item availability and prices may change.",
            campaign_type="abandoned_cart", products=[], reason=reason,
            reference={"cart_id": cart.id, "episode_key": episode},
        ):
            created["abandoned_carts"] += 1
            if created["abandoned_carts"] >= 100:
                break

    iso_year, iso_week, _ = now.isocalendar()
    weekly_key = f"{iso_year}-W{iso_week:02d}"
    if not EmailCampaign.query.filter_by(generation_key=_campaign_key("weekly_picks", weekly_key)).first():
        # Reuse the established paid-sales/cart-add ranking. If it has no
        # evidence, choose available catalogue entries without popularity claims.
        from app.services.marketing_automation import _ranked_products
        products = [product for product in _ranked_products(10) if _available(product)]
        reason = "Selected using recorded paid completed sales and cart additions." if products else "No reliable popularity signal was available; selected currently available catalogue products."
        if not products:
            products = Product.query.filter(Product.active.is_(True)).order_by(Product.created_at.desc(), Product.id.desc()).limit(10).all()
            products = [product for product in products if _available(product)]
        if products and _save_generated_draft(
            admin=admin, source="weekly_picks", key_ref=weekly_key,
            name=f"Weekly picks — {weekly_key}", subject="This week's FityStores picks",
            body="A selection of currently available products from FityStores.",
            campaign_type="weekly_picks", products=products,
            reason=reason[:255],
        ):
            created["weekly_picks"] += 1

    db.session.commit()
    return {"status": "prepared", "created": sum(created.values()), **created}
