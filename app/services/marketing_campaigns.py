"""Drafting, previewing and scheduling opt-in marketing campaigns."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from secrets import token_urlsafe
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import current_app, url_for
from sqlalchemy import and_, func, or_, select, update

from app.extensions import db
from app.models import (
    CustomerAccount, CustomerCart, CustomerToken, EmailCampaign,
    EmailCampaignRecipient, EmailMessage, Product,
)
from app.services.email_purpose import MARKETING_CAMPAIGN_TYPES, MARKETING_CAMPAIGN_TYPE_KINDS

CAMPAIGN_TYPE_FLAGS = {
    "new_product": "EMAIL_MARKETING_NEW_PRODUCTS_ENABLED",
    "special_offer": "EMAIL_MARKETING_OFFERS_ENABLED",
    "weekly_picks": "EMAIL_MARKETING_WEEKLY_PICKS_ENABLED",
    "abandoned_cart": "EMAIL_MARKETING_ABANDONED_CART_ENABLED",
    "seasonal": "EMAIL_MARKETING_SEASONAL_ENABLED",
    "custom": "EMAIL_MARKETING_CUSTOM_ENABLED",
}


def campaign_type_enabled(campaign_type: str) -> bool:
    return (
        campaign_type in MARKETING_CAMPAIGN_TYPES
        and bool(current_app.config.get(CAMPAIGN_TYPE_FLAGS[campaign_type], False))
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def campaign_product_ids(raw_value: str) -> list[int]:
    try:
        values = json.loads(raw_value or "[]")
    except (TypeError, ValueError):
        raise ValueError("Product selection is invalid.") from None
    if not isinstance(values, list) or len(values) > 10:
        raise ValueError("Select no more than ten products.")
    try:
        ids = list(dict.fromkeys(int(value) for value in values))
    except (TypeError, ValueError):
        raise ValueError("Product selection is invalid.") from None
    if any(value <= 0 for value in ids):
        raise ValueError("Product selection is invalid.")
    products = Product.query.filter(Product.id.in_(ids), Product.active.is_(True)).all() if ids else []
    if len(products) != len(ids):
        raise ValueError("Every selected product must be active.")
    if any(product.display_stock_quantity <= 0 for product in products):
        raise ValueError("Every selected product must currently be in stock.")
    return ids


def validate_campaign_content(*, name: str, subject: str, body: str, campaign_type: str,
                              product_ids: list[int], require_products: bool = True) -> None:
    if campaign_type not in MARKETING_CAMPAIGN_TYPES:
        raise ValueError("Select a supported marketing campaign type.")
    if not name.strip() or len(name.strip()) > 120:
        raise ValueError("Campaign name is required and must be at most 120 characters.")
    if not subject.strip() or len(subject.strip()) > 200:
        raise ValueError("Subject is required and must be at most 200 characters.")
    if not body.strip() or len(body.strip()) > 5000:
        raise ValueError("Campaign message is required and must be at most 5,000 characters.")
    if campaign_type == "special_offer" and (product_ids or require_products):
        products = Product.query.filter(Product.id.in_(product_ids)).all() if product_ids else []
        if not products or any(
            product.previous_price_cents is None
            or product.price_cents is None
            or product.previous_price_cents <= product.price_cents
            for product in products
        ):
            raise ValueError("Special-offer campaigns require selected products with a current reduced price.")
    if require_products and campaign_type in {"new_product", "weekly_picks", "seasonal"} and not product_ids:
        raise ValueError("Select at least one product for this campaign type.")
    if campaign_type == "abandoned_cart" and product_ids:
        raise ValueError("Abandoned-cart campaigns use each customer's own cart; do not select shared products.")


def parse_schedule(value: str, *, now: datetime | None = None) -> datetime:
    try:
        scheduled = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError("Enter a valid campaign date and time.") from None
    if scheduled.tzinfo is None:
        try:
            zone = ZoneInfo(current_app.config.get("TIMEZONE", "Africa/Nairobi"))
        except ZoneInfoNotFoundError:
            zone = ZoneInfo("Africa/Nairobi")
        scheduled = scheduled.replace(tzinfo=zone)
    scheduled_utc = scheduled.astimezone(timezone.utc).replace(tzinfo=None)
    current = now or _utcnow()
    if scheduled_utc < current + timedelta(minutes=5) or scheduled_utc > current + timedelta(days=90):
        raise ValueError("Schedule campaigns from five minutes to 90 days in the future.")
    return scheduled_utc


def _product_payload(product):
    from app.services.formatting import money
    from app.services.product_images import product_display_image, product_image_url

    image = product_display_image(product)
    image_url = product_image_url(image) if image else ""
    if image_url.startswith("/"):
        image_url = current_app.config.get("PUBLIC_BASE_URL", "https://fitystore.top").rstrip("/") + image_url
    if not image_url.startswith("https://"):
        image_url = ""
    return {
        "name": product.name,
        "price": money(product.price_cents, current_app.config.get("CURRENCY", "KES"))
        if product.price_cents is not None else "",
        "previous_price": money(product.previous_price_cents, current_app.config.get("CURRENCY", "KES"))
        if product.previous_price_cents is not None else "",
        "image_url": image_url,
        "url": current_app.config.get("PUBLIC_BASE_URL", "https://fitystore.top").rstrip("/")
        + url_for("public.product_detail", slug=product.slug),
    }


def _account_products(campaign: EmailCampaign, account: CustomerAccount | None):
    if campaign.campaign_type == "abandoned_cart":
        if account is None:
            return []
        cart = CustomerCart.query.filter_by(account_id=account.id).first()
        if not cart:
            return []
        try:
            raw_items = json.loads(cart.items_json or "[]")
        except (TypeError, ValueError):
            return []
        from app.services.cart import validate_cart
        validated = validate_cart(raw_items)
        return [line.product for line in validated.lines
                if line.product and line.product.active and line.stock_quantity > 0][:5]
    ids = json.loads(campaign.product_ids_json or "[]")
    products = Product.query.filter(
        Product.id.in_(ids), Product.active.is_(True),
    ).order_by(Product.id).all() if ids else []
    return [product for product in products if product.display_stock_quantity > 0][:5]


def render_campaign(campaign: EmailCampaign, account: CustomerAccount | None, unsubscribe_url: str):
    products = _account_products(campaign, account)
    product_data = [_product_payload(item) for item in products]
    html = current_app.jinja_env.get_template("emails/marketing_campaign.html").render(
        subject=campaign.subject,
        campaign_name=campaign.name,
        message=campaign.body,
        first_name=((account.display_name or "").strip().split() or ["there"])[0] if account else "there",
        products=product_data,
        unsubscribe_url=unsubscribe_url,
        store_url=current_app.config.get("PUBLIC_BASE_URL", "https://fitystore.top").rstrip("/") + "/shop",
    )
    lines = [f"Hi {((account.display_name or '').strip().split() or ['there'])[0] if account else 'there'},",
             "", campaign.body.strip(), ""]
    for product in product_data:
        lines.extend([product["name"], product["price"], product["url"], ""])
    lines.extend(["Shop FityStores: " + current_app.config.get("PUBLIC_BASE_URL", "https://fitystore.top").rstrip("/") + "/shop",
                  "FityStores · Bei Fity, Fits Poa.", "Unsubscribe: " + unsubscribe_url])
    return html, "\n".join(lines), products


def audience_snapshot(campaign: EmailCampaign, scheduled_for: datetime):
    from app.services.marketing_eligibility import evaluate_marketing_eligibility

    accounts = CustomerAccount.query.filter_by(active=True).order_by(CustomerAccount.id).limit(501).all()
    if len(accounts) > 500:
        raise ValueError("Campaign audience exceeds the 500-account safety limit. Split it into separately approved campaigns.")
    eligible, exclusions = [], {}
    for account in accounts:
        decision = evaluate_marketing_eligibility(account, now=scheduled_for)
        if decision.eligible:
            if campaign.campaign_type == "abandoned_cart" and not _account_products(campaign, account):
                reason = "no_active_cart_items"
            else:
                reason = ""
        else:
            reason = decision.reason_code
        if reason:
            exclusions[reason] = exclusions.get(reason, 0) + 1
        else:
            eligible.append(account)
    return accounts, eligible, exclusions


def schedule_campaign(campaign: EmailCampaign, scheduled_for: datetime, *, confirmed_count: str):
    db.session.execute(
        select(EmailCampaign.id).where(EmailCampaign.id == campaign.id).with_for_update()
    ).scalar_one()
    db.session.refresh(campaign)
    if campaign.status != "draft":
        raise ValueError("Only a draft campaign can be scheduled.")
    if not campaign.previewed_at:
        raise ValueError("Preview the current campaign content before scheduling.")
    if not current_app.config.get("EMAIL_MARKETING_QUEUE_ENABLED", False):
        raise ValueError("Marketing scheduling is disabled by the EMAIL_MARKETING_QUEUE_ENABLED gate.")
    if not campaign_type_enabled(campaign.campaign_type):
        raise ValueError("This marketing campaign type is disabled in application configuration.")
    accounts, eligible, exclusions = audience_snapshot(campaign, scheduled_for)
    if not eligible:
        raise ValueError("No eligible recipients were found.")
    if confirmed_count.strip() != f"SCHEDULE {len(eligible)}":
        raise ValueError(f"Confirmation must exactly match SCHEDULE {len(eligible)}.")

    from app.services.customer_mail import queue_email
    from app.services.marketing_eligibility import evaluate_marketing_eligibility

    campaign.scheduled_for = scheduled_for
    campaign.approved_at = _utcnow()
    campaign.status = "scheduled"
    campaign_exclusion_rows = 0
    eligible_ids = {account.id for account in eligible}
    for account in accounts:
        reason = ""
        if account.id not in eligible_ids:
            decision = evaluate_marketing_eligibility(account, now=scheduled_for)
            reason = decision.reason_code
        if reason:
            db.session.add(EmailCampaignRecipient(
                campaign_id=campaign.id, account_id=account.id, recipient=account.email,
                status="suppressed", failure_reason=reason[:255], exclusion_reason=reason[:48],
            ))
            campaign_exclusion_rows += 1
            continue
        products = _account_products(campaign, account)
        if campaign.campaign_type == "abandoned_cart" and not products:
            db.session.add(EmailCampaignRecipient(
                campaign_id=campaign.id, account_id=account.id, recipient=account.email,
                status="suppressed", failure_reason="no_active_cart_items",
                exclusion_reason="no_active_cart_items",
            ))
            campaign_exclusion_rows += 1
            continue
        token = token_urlsafe(32)
        db.session.add(CustomerToken(
            account_id=account.id, purpose="unsubscribe",
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            expires_at=_utcnow() + timedelta(days=3650),
        ))
        base = current_app.config.get("PUBLIC_BASE_URL", "https://fitystore.top").rstrip("/")
        unsubscribe_url = base + url_for("customer.unsubscribe", token=token)
        html, text_body, _ = render_campaign(campaign, account, unsubscribe_url)
        message = queue_email(
            account.email, campaign.subject, MARKETING_CAMPAIGN_TYPE_KINDS[campaign.campaign_type],
            f"marketing-campaign:{campaign.id}:account:{account.id}",
            account_id=account.id, campaign_id=campaign.id, not_before=scheduled_for,
            html_body=html, text_body=text_body, commit=False,
        )
        db.session.add(EmailCampaignRecipient(
            campaign_id=campaign.id, account_id=account.id, recipient=account.email,
            status="queued" if message.status == "queued" else message.status,
            failure_reason=message.failure_reason,
            exclusion_reason="" if message.status == "queued" else (message.failure_reason or message.status)[:48],
            message_id=message.id,
        ))
    campaign.status = "scheduled"
    db.session.flush()
    return {"eligible": len(eligible), "excluded": campaign_exclusion_rows,
            "exclusion_summary": exclusions}


def set_campaign_status(campaign: EmailCampaign, action: str) -> None:
    db.session.execute(
        select(EmailCampaign.id).where(EmailCampaign.id == campaign.id).with_for_update()
    ).scalar_one()
    db.session.refresh(campaign)
    if action == "pause":
        if campaign.status != "scheduled":
            raise ValueError("Only a scheduled campaign can be paused.")
        campaign.status = "paused"
        return
    if action == "resume":
        if campaign.status != "paused":
            raise ValueError("Only a paused campaign can be resumed.")
        if not campaign.scheduled_for or campaign.scheduled_for > _utcnow():
            campaign.status = "scheduled"
            return
        campaign.scheduled_for = _utcnow()
        campaign.status = "scheduled"
        return
    if action == "cancel":
        if campaign.status not in {"draft", "scheduled", "paused"}:
            raise ValueError("This campaign can no longer be cancelled.")
        if EmailMessage.query.filter_by(campaign_id=campaign.id, status="sending").count():
            raise ValueError("A provider dispatch is already in progress; wait before cancelling.")
        cancellable = EmailMessage.query.filter(
            EmailMessage.campaign_id == campaign.id,
            or_(
                EmailMessage.status == "queued",
                and_(
                    EmailMessage.status == "failed",
                    EmailMessage.error_category.in_((
                        "retryable_provider", "retryable_infrastructure",
                    )),
                    EmailMessage.next_attempt_at.isnot(None),
                ),
            ),
        ).all()
        message_ids = [message.id for message in cancellable]
        if message_ids:
            EmailMessage.query.filter(
                EmailMessage.id.in_(message_ids),
                EmailMessage.status.in_(("queued", "failed")),
            ).update({
                EmailMessage.status: "suppressed",
                EmailMessage.failure_reason: "Campaign cancelled by administrator.",
                EmailMessage.html_body: None,
                EmailMessage.text_body: None,
                EmailMessage.next_attempt_at: None,
            }, synchronize_session=False)
            EmailCampaignRecipient.query.filter(
                EmailCampaignRecipient.campaign_id == campaign.id,
                EmailCampaignRecipient.message_id.in_(message_ids),
                EmailCampaignRecipient.status.in_(("queued", "failed")),
            ).update({
                EmailCampaignRecipient.status: "suppressed",
                EmailCampaignRecipient.failure_reason: "Campaign cancelled by administrator.",
            }, synchronize_session=False)
        campaign.status = "cancelled"
        return
    raise ValueError("Unsupported campaign action.")
