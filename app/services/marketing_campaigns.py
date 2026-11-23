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
    EmailCampaignRecipient, EmailDeliveryAttempt, EmailDeliveryInvestigation,
    EmailDeliveryInvestigationEvent, EmailMessage, Product,
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
    targeted_cart = None
    if campaign.campaign_type == "abandoned_cart" and campaign.generation_source == "abandoned_cart":
        try:
            reference = json.loads(campaign.generation_reference or "{}")
            cart_id = int(reference.get("cart_id"))
            episode = str(reference.get("episode_key") or "")
        except (TypeError, ValueError, AttributeError):
            raise ValueError("Generated cart draft has invalid source metadata.") from None
        targeted_cart = CustomerCart.query.filter_by(id=cart_id).first()
        if (not targeted_cart or targeted_cart.account_id is None
                or (targeted_cart.episode_key or targeted_cart.updated_at.isoformat()) != episode):
            return [], [], {"cart_episode_changed": 1}
        accounts = [CustomerAccount.query.get(targeted_cart.account_id)]
        accounts = [account for account in accounts if account]
    else:
        accounts = CustomerAccount.query.filter_by(active=True).order_by(CustomerAccount.id).limit(501).all()
    if len(accounts) > 500:
        raise ValueError("Campaign audience exceeds the 500-account safety limit. Split it into separately approved campaigns.")
    eligible, exclusions = [], {}
    for account in accounts:
        decision = evaluate_marketing_eligibility(account, now=scheduled_for)
        if decision.eligible:
            if campaign.campaign_type == "abandoned_cart":
                from app.services.marketing_campaign_preparation import cart_is_preparable
                cart = targeted_cart or CustomerCart.query.filter_by(account_id=account.id).first()
                valid, reason, _products = cart_is_preparable(cart, scheduled_for) if cart else (False, "no_active_cart_items", [])
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


def _confirmed_brevo_401_messages(campaign: EmailCampaign) -> list[tuple[EmailCampaignRecipient, EmailMessage, CustomerAccount, EmailDeliveryAttempt]]:
    """Find only original custom-campaign messages with one recorded 401 rejection.

    The exact stored provider response plus a failed Brevo attempt is the
    evidence of non-acceptance. Any accepted, in-flight, repeated, or
    ambiguous attempt makes a message ineligible for this recovery path.
    """
    if campaign.status != "partial" or campaign.campaign_type != "custom":
        return []
    from app.services.email_purpose import MARKETING_CAMPAIGN_TYPE_KINDS
    expected_kind = MARKETING_CAMPAIGN_TYPE_KINDS.get("custom")
    rows = EmailCampaignRecipient.query.filter_by(
        campaign_id=campaign.id, status="failed",
    ).order_by(EmailCampaignRecipient.account_id, EmailCampaignRecipient.id).all()
    candidates = []
    for recipient in rows:
        message = db.session.get(EmailMessage, recipient.message_id) if recipient.message_id else None
        account = db.session.get(CustomerAccount, recipient.account_id)
        if not message or not account:
            continue
        if (
            message.campaign_id != campaign.id or message.account_id != account.id
            or message.purpose != "marketing" or message.kind != expected_kind
            or message.status != "failed" or message.error_category != "permanent_provider"
            or message.failure_reason != "Brevo HTTP 401." or message.retry_count != 1
            or message.provider_message_id is not None
            or message.idempotency_key != f"marketing-campaign:{campaign.id}:account:{account.id}"
            or recipient.message_id != message.id
            or recipient.recipient.strip().casefold() != account.email.strip().casefold()
        ):
            continue
        attempts = EmailDeliveryAttempt.query.filter_by(
            message_id=message.id,
        ).order_by(EmailDeliveryAttempt.attempt_number).all()
        if len(attempts) != 1:
            continue
        attempt = attempts[0]
        if not (
            attempt.attempt_number == 1 and attempt.provider == "brevo"
            and attempt.status == "failed"
            and attempt.error_category == "permanent_provider"
        ):
            continue
        prior_recovery = EmailDeliveryInvestigation.query.filter_by(
            recovered_message_id=message.id,
        ).first()
        if prior_recovery:
            continue
        candidates.append((recipient, message, account, attempt))
    return candidates


def campaign_recovery_candidate_count(campaign: EmailCampaign) -> int:
    """Count confirmed, one-attempt Brevo 401 rows without exposing addresses."""
    return len(_confirmed_brevo_401_messages(campaign))


def recover_confirmed_campaign_rejections(
    campaign_id: int, admin_id: int, *, scheduled_for: datetime,
    authorization: str,
) -> dict[str, int]:
    """Requeue the same eligible custom-campaign messages after confirmed 401s.

    No provider call is made here. The same outbox and recipient rows are
    retained. The campaign must be scheduled at least 30 minutes ahead so an
    administrator can verify the queue before a later worker claims it.
    """
    campaign = db.session.execute(select(EmailCampaign).where(
        EmailCampaign.id == campaign_id,
    ).with_for_update()).scalar_one_or_none()
    if not campaign:
        raise ValueError("Campaign was not found.")
    if campaign.status != "partial" or campaign.campaign_type != "custom":
        raise ValueError("Only a partially failed custom campaign can use this recovery action.")
    if not current_app.config.get("EMAIL_MARKETING_QUEUE_ENABLED", False):
        raise ValueError("Marketing queue is disabled; no messages were changed.")
    if not campaign_type_enabled("custom"):
        raise ValueError("The custom campaign gate is disabled; no messages were changed.")
    if scheduled_for < _utcnow() + timedelta(minutes=30):
        raise ValueError("Choose a recovery time at least 30 minutes in the future for review.")
    candidates = _confirmed_brevo_401_messages(campaign)
    if not candidates:
        raise ValueError("No messages have the required confirmed Brevo 401 evidence.")
    if authorization.strip() != f"RECOVER {len(candidates)}":
        raise ValueError(f"Confirmation must exactly match RECOVER {len(candidates)}.")

    # Validate saved campaign data before touching any outbox rows. This uses
    # the original campaign subject/body/product IDs and current product data.
    try:
        product_ids = campaign_product_ids(campaign.product_ids_json or "[]")
        validate_campaign_content(
            name=campaign.name, subject=campaign.subject, body=campaign.body,
            campaign_type="custom", product_ids=product_ids, require_products=False,
        )
    except ValueError as exc:
        raise ValueError(f"Saved campaign content cannot be safely reconstructed: {exc}") from None

    from app.services.marketing_eligibility import evaluate_marketing_eligibility
    from app.services.marketing_suppressions import marketing_account_lock_statement

    recovered = suppressed = 0
    now = _utcnow()
    base = current_app.config.get("PUBLIC_BASE_URL", "https://fitystore.top").rstrip("/")

    def record_recovery_decision(message, attempt, *, outcome, event_type, explanation, link_recovery):
        investigation = EmailDeliveryInvestigation(
            message_id=message.id, original_attempt_id=attempt.id,
            provider="brevo", reason="Confirmed Brevo HTTP 401 campaign recovery.",
            status="RESOLVED", evidence_summary="Stored provider response confirmed HTTP 401 rejection.",
            evidence_source="Email outbox failure record and failed provider attempt",
            investigator_admin_id=admin_id, started_at=now, resolved_at=now,
            resolution_outcome="not_accepted", resolution_explanation=explanation,
            recovered_message_id=message.id if link_recovery else None,
        )
        db.session.add(investigation)
        db.session.flush()
        db.session.add(EmailDeliveryInvestigationEvent(
            investigation_id=investigation.id, actor_admin_id=admin_id,
            event_type=event_type, from_status="failed", to_status=outcome,
            evidence_summary="Brevo HTTP 401 was explicitly rejected; existing campaign rows were retained.",
            evidence_source="Stored Brevo response and single failed delivery attempt",
            explanation=explanation, related_attempt_id=attempt.id,
            created_at=now,
        ))

    for candidate_recipient, candidate_message, _candidate_account, candidate_attempt in candidates:
        # Re-read and lock the existing linked rows after locking the campaign.
        # A stale page or concurrent administrator action must not authorize a
        # changed/ambiguous delivery state.
        recipient = db.session.execute(select(EmailCampaignRecipient).where(
            EmailCampaignRecipient.id == candidate_recipient.id,
        ).with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
        message = db.session.execute(select(EmailMessage).where(
            EmailMessage.id == candidate_message.id,
        ).with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
        attempt = db.session.execute(select(EmailDeliveryAttempt).where(
            EmailDeliveryAttempt.id == candidate_attempt.id,
        ).with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
        if (
            not recipient or not message or not attempt
            or recipient.status != "failed" or recipient.message_id != message.id
            or message.status != "failed" or message.error_category != "permanent_provider"
            or message.failure_reason != "Brevo HTTP 401." or message.retry_count != 1
            or attempt.provider != "brevo" or attempt.status != "failed"
            or attempt.error_category != "permanent_provider"
        ):
            raise ValueError("A recovery row changed while you were reviewing it. Refresh and verify the campaign again.")
        # Lock the same account row used by consent/suppression transitions and
        # the marketing dispatch claim. The worker repeats this final check.
        account_row = db.session.execute(marketing_account_lock_statement(recipient.account_id)).mappings().first()
        account = db.session.get(CustomerAccount, recipient.account_id, populate_existing=True) if account_row else None
        if (not account or recipient.recipient.strip().casefold() != account.email.strip().casefold()
                or message.recipient.strip().casefold() != account.email.strip().casefold()):
            decision = None
        else:
            decision = evaluate_marketing_eligibility(
                account=account, connection=db.session.connection(),
                respect_frequency=True, exclude_message_id=message.id, now=scheduled_for,
            )
        if not decision or not decision.eligible:
            reason = decision.reason_code if decision else "account_or_recipient_changed"
            message.status = "suppressed"
            message.error_category = "eligibility_changed"
            message.failure_reason = reason[:255]
            message.next_attempt_at = None
            message.html_body = message.text_body = None
            recipient.status = "suppressed"
            recipient.failure_reason = reason[:255]
            recipient.exclusion_reason = reason[:48]
            recipient.sent_at = None
            suppressed += 1
            record_recovery_decision(
                message, attempt, outcome="suppressed",
                event_type="campaign_recovery_suppressed",
                explanation=f"Recovery blocked by the current marketing eligibility check: {reason[:300]}.",
                link_recovery=False,
            )
            continue

        # Failed campaign payloads may already have been erased. Re-render from
        # persisted campaign content and current product records when either
        # body part is missing; never substitute invented campaign content.
        if not (message.html_body and message.html_body.strip()
                and message.text_body and message.text_body.strip()):
            token = token_urlsafe(32)
            db.session.add(CustomerToken(
                account_id=recipient.account_id, purpose="unsubscribe",
                token_hash=hashlib.sha256(token.encode()).hexdigest(),
                expires_at=now + timedelta(days=3650),
            ))
            unsubscribe_url = base + url_for("customer.unsubscribe", token=token)
            html, text_body, _products = render_campaign(campaign, account, unsubscribe_url)
            message.html_body, message.text_body = html, text_body
        message.subject = campaign.subject
        message.recipient = recipient.recipient
        message.status = "queued"
        message.failure_reason = ""
        message.error_category = None
        message.next_attempt_at = scheduled_for
        message.claim_token = None
        message.claimed_at = None
        recipient.status = "queued"
        recipient.failure_reason = ""
        recipient.exclusion_reason = ""
        recipient.sent_at = None

        # Existing delivery-investigation tables provide an append-only audit
        # event and link the next provider attempt to the confirmed rejection.
        record_recovery_decision(
            message, attempt, outcome="queued",
            event_type="campaign_recovery_authorized",
            explanation="Campaign recovery scheduled for a later worker run; no provider call was made in this transaction.",
            link_recovery=True,
        )
        recovered += 1

    if recovered == 0:
        raise ValueError("No confirmed 401 recipients remain currently eligible; no campaign messages were requeued.")
    campaign.status = "scheduled"
    campaign.scheduled_for = scheduled_for
    campaign.approved_at = now
    campaign.sent_at = None
    db.session.flush()
    current_app.logger.warning(
        "marketing_campaign_recovery_completed campaign_id=%s admin_id=%s requeued=%s suppressed=%s scheduled_for=%s",
        campaign.id, admin_id, recovered, suppressed, scheduled_for.isoformat(),
    )
    return {"requeued": recovered, "suppressed": suppressed}


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
