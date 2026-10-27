from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from secrets import token_urlsafe
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from urllib.parse import urlsplit

from flask import current_app, render_template, url_for
from sqlalchemy import func
from sqlalchemy.orm import selectinload

from app.extensions import db
from app.models import (
    CartAddCount, CustomerAccount, CustomerCart, CustomerToken, EmailMessage,
    MarketingAutomationSettings, MarketingCampaignRun, MarketingProductExposure, Order, OrderItem,
    Product, Review,
)
from app.services.customer_mail import mail_configuration_status, queue_email
from app.services.cart import validate_cart
from app.services.formatting import money
from app.services.product_images import product_display_image, product_image_url
from app.services.settings import current_settings


MARKETING_KINDS = (
    "marketing_daily_goodies", "marketing_weekly_picks",
    "marketing_abandoned_cart", "marketing_review_highlight",
)
COMPLETED_ORDER = "Completed"


def _public_url(path: str) -> str:
    base = (current_app.config.get("PUBLIC_BASE_URL") or "https://fitystore.top").rstrip("/") + "/"
    parsed = urlsplit(path or "")
    if parsed.scheme and parsed.netloc:
        path = parsed.path + (("?" + parsed.query) if parsed.query else "")
    return base + (path or "").lstrip("/")


def _absolute_product_image(product):
    image = product_display_image(product)
    value = product_image_url(image) if image else ""
    if value.startswith("https://"):
        return value
    if value.startswith("//"):
        return "https:" + value
    return _public_url(value or "static/images/mainLogo-160.png")


def _available_products():
    return Product.query.options(selectinload(Product.images), selectinload(Product.variants), selectinload(Product.category)).filter_by(active=True).order_by(Product.created_at.desc(), Product.id.desc()).limit(300).all()


def _product_price(product):
    prices = [variant.effective_price_cents for variant in product.variants if variant.active and variant.effective_price_cents is not None]
    low = min(prices) if prices else product.price_cents
    high = max(prices) if prices else product.price_cents
    return money(low, current_settings().currency) if low == high and low is not None else (
        f"{money(low, current_app.config.get('CURRENCY', 'KES'))} – {money(high, current_settings().currency)}"
        if low is not None and high is not None else "Price to be confirmed"
    )


def _product_payload(product):
    description = re.sub(r"<[^>]+>", " ", product.description or "")
    return {
        "name": product.name,
        "description": " ".join(description.split())[:150],
        "category": product.category.name if product.category else "",
        "price": _product_price(product),
        "discount": bool(product.previous_price_cents and product.price_cents is not None and product.previous_price_cents > product.price_cents),
        "image_url": _absolute_product_image(product),
        "image_alt": product.main_image.alt_text if product.main_image and product.main_image.alt_text else product.name,
        "url": _public_url(url_for("public.product_detail", slug=product.slug)),
        "review_url": _public_url(url_for("public.reviews", product_id=product.id)) + "#write-review",
    }


def _with_review(product):
    row = Review.query.filter_by(product_id=product.id, status="published").order_by(Review.created_at.desc(), Review.id.desc()).first()
    return {
        "display_name": row.display_name,
        "rating": row.rating,
        "title": row.title,
        "text": row.text[:320],
    } if row else None


def _ranked_products(limit=5):
    """Rank by paid completed sales from the last 90 days plus recorded cart additions (lifetime aggregate)."""
    scores = {}
    sales = (
        db.session.query(OrderItem.product_id, func.sum(OrderItem.quantity))
        .join(Order, Order.id == OrderItem.order_id)
        .filter(Order.status == COMPLETED_ORDER, Order.payment_status == "Paid", Order.created_at >= datetime.utcnow() - timedelta(days=90), OrderItem.product_id.isnot(None))
        .group_by(OrderItem.product_id).all()
    )
    for product_id, quantity in sales:
        scores[product_id] = scores.get(product_id, 0) + int(quantity or 0) * 3
    for product_id, count in CartAddCount.query.with_entities(CartAddCount.product_id, CartAddCount.add_count).all():
        scores[product_id] = scores.get(product_id, 0) + int(count or 0)
    ids = [product_id for product_id, score in sorted(scores.items(), key=lambda row: (-row[1], row[0])) if score > 0]
    ranked = Product.query.filter(Product.id.in_(ids), Product.active.is_(True)).all() if ids else []
    by_id = {product.id: product for product in ranked}
    products = [by_id[product_id] for product_id in ids if product_id in by_id and by_id[product_id].display_stock_quantity > 0]
    return products[:limit]


def _choose_products(campaign_type, day_index=0):
    available = [p for p in _available_products() if p.display_stock_quantity > 0]
    if campaign_type == "weekly_picks":
        ranked = _ranked_products(12)
        if ranked:
            return ranked[:10], "Shopper picks supported by paid completed sales from the last 90 days and recorded cart additions."
        return available[:5], "A fresh look at currently available FityStores essentials."
    if campaign_type == "offers":
        offers = [p for p in available if p.previous_price_cents and p.price_cents is not None and p.previous_price_cents > p.price_cents]
        return offers[:10], "Current offers on available items."
    if campaign_type == "reviews":
        review_ids = {row[0] for row in db.session.query(Review.product_id).filter(Review.status == "published", Review.product_id.isnot(None)).distinct().all()}
        reviewed = [p for p in available if p.id in review_ids]
        return reviewed[:10], "Genuine customer feedback on these products."
    # Daily assortment: current offers, cart activity, recent items and varied categories.
    offers = [p for p in available if p.previous_price_cents and p.price_cents is not None and p.previous_price_cents > p.price_cents]
    cart_rank = {row.product_id: row.add_count for row in CartAddCount.query.order_by(CartAddCount.add_count.desc()).limit(30).all()}
    popular = sorted((p for p in available if p.id in cart_rank), key=lambda p: (-cart_rank[p.id], p.name.lower()))
    candidates = []
    for group in (offers, popular, available):
        for product in group:
            if product.id not in {p.id for p in candidates}:
                candidates.append(product)
    if candidates:
        offset = day_index % len(candidates)
        candidates = candidates[offset:] + candidates[:offset]
    selected, categories = [], set()
    for product in candidates:
        if product.category_id not in categories:
            selected.append(product); categories.add(product.category_id)
        if len(selected) == 10: break
    if len(selected) < 5:
        for product in candidates:
            if product not in selected: selected.append(product)
            if len(selected) == 10: break
    return selected, "Everyday picks, with current prices and availability."


def _render_products_email(account, subject, heading, intro, products, unsubscribe_url, include_reviews=False):
    payloads = []
    for product in products:
        item = _product_payload(product)
        item["review"] = _with_review(product) if include_reviews else None
        payloads.append(item)
    html = render_template(
        "emails/product_campaign.html", subject=subject, heading=heading, intro=intro,
        first_name=(account.display_name or account.email.split("@", 1)[0]).split()[0],
        products=payloads, unsubscribe_url=unsubscribe_url,
        shop_url=_public_url(url_for("public.shop")),
    )
    lines = [f"Hi {(account.display_name or account.email.split('@', 1)[0]).split()[0]},", "", intro, ""]
    for item in payloads:
        lines.extend([item["name"], item["price"], item["url"], item["description"], ""])
        if item["review"]:
            lines.extend([f"Customer review ({item['review']['rating']}/5): {item['review']['text']}", ""])
    lines.extend(["Shop FityStores: " + _public_url(url_for("public.shop")), "Contact FityStores: " + _public_url(url_for("public.contact")), "", "FityStores · Bei Fity, Fits Poa.", "Unsubscribe: " + unsubscribe_url])
    return html, "\n".join(lines)


def _issue_unsubscribe(account):
    raw = token_urlsafe(32)
    db.session.add(CustomerToken(
        account_id=account.id, purpose="unsubscribe",
        token_hash=hashlib.sha256(raw.encode()).hexdigest(),
        expires_at=datetime.utcnow() + timedelta(days=3650),
    ))
    db.session.commit()
    return _public_url(url_for("customer.unsubscribe", token=raw))


def _eligible_accounts():
    return CustomerAccount.query.filter_by(active=True, marketing_status="subscribed").filter(
        CustomerAccount.email_verified_at.isnot(None)
    ).order_by(CustomerAccount.id)


def _recently_marketed(account_id, now):
    return EmailMessage.query.filter(
        EmailMessage.account_id == account_id,
        EmailMessage.kind.in_(MARKETING_KINDS + ("marketing_campaign",)),
        EmailMessage.status.in_(("sent", "delivered", "bounced", "complained")),
        EmailMessage.sent_at >= now - timedelta(hours=24),
    ).first() is not None


def _deliver_marketing(account, key, kind, subject, heading, intro, products, include_reviews=False):
    from app.models import MarketingCampaignRun
    if not account.active or account.marketing_status != "subscribed" or not account.email_verified_at:
        return "suppressed"
    if _recently_marketed(account.id, datetime.utcnow()):
        return "suppressed"
    cutoff = datetime.utcnow() - timedelta(days=14)
    product_ids = [product.id for product in products]
    recently_seen = {row.product_id for row in MarketingProductExposure.query.filter(
        MarketingProductExposure.account_id == account.id,
        MarketingProductExposure.product_id.in_(product_ids),
        MarketingProductExposure.last_sent_at >= cutoff,
    ).all()} if product_ids else set()
    fresh_products = [product for product in products if product.id not in recently_seen]
    chosen = (fresh_products or products)[:5]
    unsubscribe_url = _issue_unsubscribe(account)
    html, text = _render_products_email(account, subject, heading, intro, chosen, unsubscribe_url, include_reviews)
    result = queue_email(
        account.email, subject, kind, key, account_id=account.id,
        html_body=html, text_body=text,
    )
    if result.status == "sent":
        sent_at = result.sent_at or datetime.utcnow()
        for product in chosen:
            exposure = MarketingProductExposure.query.filter_by(account_id=account.id, product_id=product.id).first()
            if exposure:
                exposure.last_sent_at = sent_at
            else:
                db.session.add(MarketingProductExposure(account_id=account.id, product_id=product.id, last_sent_at=sent_at))
        db.session.commit()
    return result.status


def _run_campaign(campaign_type, local_now, products, intro, *, include_reviews=False):
    configured, reason = mail_configuration_status()
    key = f"{campaign_type}:{local_now.date().isoformat()}"
    run = MarketingCampaignRun.query.filter_by(campaign_key=key).first()
    if run and run.status in {"sent", "sending"}:
        return {"status": run.status, "accepted": run.accepted_count}
    if not run:
        run = MarketingCampaignRun(campaign_key=key, campaign_type=campaign_type, scheduled_for=local_now.replace(tzinfo=None), status="sending")
        db.session.add(run)
        try: db.session.commit()
        except Exception:
            db.session.rollback()
            run = MarketingCampaignRun.query.filter_by(campaign_key=key).first()
            if run and run.status in {"sent", "sending"}: return {"status":run.status,"accepted":run.accepted_count}
            return {"status":"duplicate","accepted":0}
    else:
        run.status="sending";run.started_at=datetime.utcnow();db.session.commit()
    run.started_at=datetime.utcnow()
    if not configured:
        run.status="not_configured";run.failure_summary=reason[:255];run.finished_at=datetime.utcnow();db.session.commit()
        return {"status":"not_configured","accepted":0}
    accepted=failed=suppressed=eligible=0
    fresh_fallback = intro.startswith("A fresh look")
    subject = "Fity Goodies are here" if campaign_type == "daily_goodies" else ("This week's FityStores fresh picks" if fresh_fallback else "This week's FityStores popular picks")
    heading = "Your Fity Goodies" if campaign_type == "daily_goodies" else ("Fresh picks this week" if fresh_fallback else "Popular picks this week")
    for account in _eligible_accounts().yield_per(50):
        eligible += 1
        result = _deliver_marketing(account,f"automation:{key}:account:{account.id}",f"marketing_{campaign_type}",subject,heading,intro,products,include_reviews)
        if result in {"sent","sending"}:accepted+=1
        elif result=="suppressed":suppressed+=1
        else:failed+=1
    run.eligible_count=eligible;run.accepted_count=accepted;run.failed_count=failed;run.suppressed_count=suppressed
    run.status="sent" if failed==0 else "partial"
    run.finished_at=datetime.utcnow();db.session.commit()
    return {"status":run.status,"accepted":accepted,"failed":failed,"suppressed":suppressed}


def _run_abandoned_carts(settings, now_utc):
    configured, reason = mail_configuration_status()
    if not configured:return {"status":"not_configured","accepted":0,"reason":reason}
    cutoff=now_utc-timedelta(minutes=settings.abandoned_cart_minutes)
    carts=CustomerCart.query.join(CustomerAccount).filter(
        CustomerCart.items_json!="[]",CustomerCart.updated_at<=cutoff,
        CustomerAccount.active.is_(True),CustomerAccount.email_verified_at.isnot(None),
        CustomerAccount.marketing_status=="subscribed",
        (CustomerCart.checkout_until.is_(None)) | (CustomerCart.checkout_until<=now_utc),
        CustomerCart.reminder_sent_at.is_(None),
    ).order_by(CustomerCart.updated_at).limit(100).all()
    accepted=failed=suppressed=0
    for cart in carts:
        account=cart.account
        if not account or not account.active or account.marketing_status!="subscribed" or not account.email_verified_at:
            suppressed+=1;continue
        if _recently_marketed(account.id,now_utc):
            suppressed+=1;continue
        try:raw_items=json.loads(cart.items_json)
        except (TypeError,ValueError):raw_items=[]
        validation=validate_cart(raw_items)
        products=[]
        for line in validation.lines:
            if line.stock_quantity>0 and line.product.active:
                products.append(line.product)
        if not products:
            cart.items_json="[]";cart.checkout_until=None;db.session.commit();continue
        key=f"abandoned-cart:{cart.episode_key}"
        unsubscribe=_issue_unsubscribe(account)
        payloads=[]
        for line in validation.lines:
            if line.stock_quantity>0 and line.product.active:
                item=_product_payload(line.product)
                item["price"]=money(line.unit_price_cents,current_settings().currency) if line.unit_price_cents is not None else "Price to be confirmed"
                item["quantity"]=line.quantity
                item["variant_label"]=line.variant_label
                payloads.append(item)
        html=render_template("emails/product_campaign.html",subject="Still interested in your picks at FityStores?",heading="Your cart is still here",intro="You left these items in your cart. Would you like to continue shopping? Availability and prices can change.",first_name=(account.display_name or account.email.split("@",1)[0]).split()[0],products=payloads,unsubscribe_url=unsubscribe,shop_url=_public_url(url_for("public.shop")),cart_url=_public_url(url_for("public.cart")))
        text_lines=[f"Hi {(account.display_name or account.email.split('@',1)[0]).split()[0]},","You left these items in your cart. Availability and prices can change."]
        for item in payloads:text_lines.extend([item["name"],item["price"],item["url"]])
        text_lines.extend(["Continue shopping: "+_public_url(url_for("public.cart")),"Unsubscribe: "+unsubscribe])
        result=queue_email(account.email,"Still interested in your picks at FityStores?","marketing_abandoned_cart",key,account_id=account.id,html_body=html,text_body="\n".join(text_lines))
        if result.status=="sent":
            cart.reminder_sent_at=datetime.utcnow();db.session.commit();accepted+=1
        elif result.status=="suppressed":suppressed+=1
        else:failed+=1
    return {"status":"complete","accepted":accepted,"failed":failed,"suppressed":suppressed}


def run_marketing_automation():
    """Compatibility no-op: marketing and cart-reminder automation is disabled."""
    return {"status": "paused", "campaigns": 0, "abandoned": 0}


def next_scheduled_runs(settings, now=None):
    """Return concise next local run times for enabled schedules."""
    try:
        zone=ZoneInfo(settings.timezone_name)
    except ZoneInfoNotFoundError:
        zone=ZoneInfo("Africa/Nairobi")
    local_now=now or datetime.now(timezone.utc).astimezone(zone)
    rows=[]
    if settings.enabled and settings.daily_goodies_enabled:
        due=local_now.replace(hour=settings.daily_goodies_hour,minute=0,second=0,microsecond=0)
        if due<=local_now:due+=timedelta(days=1)
        rows.append(("Daily Fity Goodies",due))
    if settings.enabled and settings.weekly_picks_enabled:
        days=(settings.weekly_picks_weekday-local_now.weekday())%7
        due=(local_now+timedelta(days=days)).replace(hour=settings.weekly_picks_hour,minute=0,second=0,microsecond=0)
        if due<=local_now:due+=timedelta(days=7)
        rows.append(("Weekly picks",due))
    return sorted(rows,key=lambda row:row[1])
