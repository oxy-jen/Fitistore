from datetime import datetime, timedelta, timezone
import json

from test_app import app, app_context
from app.extensions import db
from app.models import (
    CustomerAccount, CustomerCart, CustomerOrderLink, EmailCampaign, EmailCampaignRecipient,
    EmailMessage, Order, OrderItem, Product,
)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _eligible_account(email):
    now = _now()
    account = CustomerAccount(
        email=email, display_name="Campaign recipient", active=True,
        email_verified_at=now, marketing_status="subscribed",
        marketing_consent_at=now, created_at=now,
    )
    db.session.add(account)
    db.session.flush()
    return account


def test_preparation_gate_defaults_closed_and_writes_no_campaigns(app, app_context):
    from app.services.marketing_campaign_preparation import prepare_marketing_campaign_drafts

    app.config["EMAIL_MARKETING_PREPARATION_ENABLED"] = False
    before = EmailCampaign.query.count()
    result = prepare_marketing_campaign_drafts()

    assert result["status"] == "disabled"
    assert result["created"] == 0
    assert EmailCampaign.query.count() == before
    assert EmailMessage.query.count() == 0


def test_new_arrival_drafts_use_real_product_data_and_are_idempotent(app, app_context):
    from app.services.marketing_campaign_preparation import prepare_marketing_campaign_drafts

    now = _now()
    Product.query.update({Product.created_at: now - timedelta(days=40)})
    product = Product.query.filter_by(slug="plain-socks").one()
    product.created_at = now - timedelta(days=1)
    app.config.update(EMAIL_MARKETING_PREPARATION_ENABLED=True,
                      EMAIL_MARKETING_NEW_ARRIVAL_LOOKBACK_DAYS=7,
                      EMAIL_MARKETING_QUEUE_ENABLED=False)
    db.session.commit()

    first = prepare_marketing_campaign_drafts(now=now)
    second = prepare_marketing_campaign_drafts(now=now + timedelta(minutes=15))
    campaign = EmailCampaign.query.filter_by(generation_key=f"prepare:new_arrival:product:{product.id}").one()

    assert first["new_arrivals"] == 1
    assert second["new_arrivals"] == 0
    assert campaign.product_ids_json == json.dumps([product.id])
    assert product.name in campaign.subject and product.description in campaign.body
    assert campaign.status == "draft" and campaign.approved_at is None and campaign.scheduled_for is None
    assert EmailMessage.query.filter_by(campaign_id=campaign.id).count() == 0


def test_unavailable_new_arrival_is_not_prepared(app, app_context):
    from app.services.marketing_campaign_preparation import prepare_marketing_campaign_drafts

    now = _now()
    Product.query.update({Product.created_at: now - timedelta(days=1)})
    product = Product.query.filter_by(slug="plain-socks").one()
    product.stock_quantity = 0
    app.config["EMAIL_MARKETING_PREPARATION_ENABLED"] = True
    db.session.commit()

    result = prepare_marketing_campaign_drafts(now=now)
    assert EmailCampaign.query.filter_by(generation_key=f"prepare:new_arrival:product:{product.id}").first() is None


def test_weekly_pick_is_unique_and_uses_non_popularity_copy_without_signals(app, app_context):
    from app.services.marketing_campaign_preparation import prepare_marketing_campaign_drafts

    now = _now()
    app.config["EMAIL_MARKETING_PREPARATION_ENABLED"] = True
    db.session.commit()
    first = prepare_marketing_campaign_drafts(now=now)
    second = prepare_marketing_campaign_drafts(now=now + timedelta(days=1))
    week = now.isocalendar()
    campaign = EmailCampaign.query.filter_by(
        generation_key=f"prepare:weekly_picks:{week.year}-W{week.week:02d}"
    ).one()

    assert first["weekly_picks"] == 1
    assert second["weekly_picks"] == 0
    assert campaign.status == "draft" and campaign.approved_at is None
    assert "popular" not in campaign.body.lower() and "most added" not in campaign.body.lower()


def test_abandoned_cart_draft_requires_idle_eligible_cart_and_targets_one_episode(app, app_context):
    from app.services.marketing_campaign_preparation import prepare_marketing_campaign_drafts
    from app.services.marketing_campaigns import audience_snapshot

    now = _now()
    account = _eligible_account("idle-cart@example.test")
    cart = CustomerCart(
        account_id=account.id, items_json='[{"product_id":1,"quantity":1}]',
        episode_key="episode-one", updated_at=now - timedelta(hours=4),
    )
    db.session.add(cart)
    app.config.update(EMAIL_MARKETING_PREPARATION_ENABLED=True,
                      EMAIL_MARKETING_ABANDONED_CART_IDLE_MINUTES=180)
    db.session.commit()

    result = prepare_marketing_campaign_drafts(now=now)
    draft = EmailCampaign.query.filter_by(generation_source="abandoned_cart").one()
    accounts, eligible, _exclusions = audience_snapshot(draft, now)

    assert result["abandoned_carts"] == 1
    assert [row.id for row in accounts] == [account.id]
    assert [row.id for row in eligible] == [account.id]
    assert "idle-cart@example.test" not in (draft.body + draft.subject + (draft.generation_reason or ""))
    assert EmailMessage.query.filter_by(campaign_id=draft.id).count() == 0


def test_recent_cart_and_terminal_order_are_excluded(app, app_context):
    from app.services.marketing_campaign_preparation import cart_is_preparable

    now = _now()
    recent_account = _eligible_account("recent-cart@example.test")
    recent = CustomerCart(account_id=recent_account.id,
                          items_json='[{"product_id":1,"quantity":1}]',
                          episode_key="recent", updated_at=now - timedelta(minutes=10))
    account = _eligible_account("ordered-cart@example.test")
    old_cart = CustomerCart(account_id=account.id,
                            items_json='[{"product_id":1,"quantity":1}]',
                            episode_key="ordered", updated_at=now - timedelta(hours=4))
    order = Order(reference="PREP-ORDER-1", fulfilment_method="Collection",
                  status="Completed", payment_status="Paid", created_at=now - timedelta(hours=1))
    db.session.add_all([recent, old_cart, order])
    db.session.flush()
    db.session.add_all([
        CustomerOrderLink(account_id=account.id, order_id=order.id),
        OrderItem(order_id=order.id, product_id=1, product_name="Plain Socks", quantity=1),
    ])
    db.session.commit()

    assert cart_is_preparable(recent, now)[0] is False
    assert cart_is_preparable(old_cart, now)[:2] == (False, "terminal_order_exists")


def test_special_offer_generation_uses_real_price_markdown(app, app_context):
    from app.services.marketing_campaign_preparation import prepare_marketing_campaign_drafts

    product = Product.query.filter_by(slug="plain-socks").one()
    product.price_cents = 4000
    product.previous_price_cents = 5000
    app.config["EMAIL_MARKETING_PREPARATION_ENABLED"] = True
    db.session.commit()

    result = prepare_marketing_campaign_drafts(now=_now())
    offer = EmailCampaign.query.filter_by(
        generation_key=f"prepare:special_offer:product:{product.id}"
    ).one()

    assert result["special_offers"] == 1
    assert offer.campaign_type == "special_offer"
    assert offer.status == "draft"
    assert "reduced price" in offer.body
    assert EmailMessage.query.filter_by(campaign_id=offer.id).count() == 0


def test_generated_campaign_auto_schedule_is_gated_and_uses_campaign_worker(app, app_context):
    from app.services.marketing_campaign_preparation import prepare_marketing_campaign_drafts

    now = _now()
    account = _eligible_account("auto-schedule@example.test")
    product = Product.query.filter_by(slug="plain-socks").one()
    product.created_at = now - timedelta(days=1)
    app.config.update(
        EMAIL_MARKETING_PREPARATION_ENABLED=True,
        EMAIL_MARKETING_AUTO_SCHEDULE_GENERATED_ENABLED=True,
        EMAIL_MARKETING_QUEUE_ENABLED=True,
        EMAIL_MARKETING_NEW_PRODUCTS_ENABLED=True,
        EMAIL_MARKETING_OFFERS_ENABLED=False,
        EMAIL_MARKETING_WEEKLY_PICKS_ENABLED=False,
        EMAIL_MARKETING_ABANDONED_CART_ENABLED=False,
    )
    db.session.commit()

    result = prepare_marketing_campaign_drafts(now=now)
    campaign = EmailCampaign.query.filter_by(
        generation_key=f"prepare:new_arrival:product:{product.id}"
    ).one()
    recipient = EmailCampaignRecipient.query.filter_by(campaign_id=campaign.id).one()
    message = db.session.get(EmailMessage, recipient.message_id)

    assert result["auto_schedule"]["scheduled"] == 1
    assert campaign.status == "scheduled"
    assert campaign.previewed_at is not None
    assert campaign.approved_at is not None
    assert campaign.scheduled_for >= now + timedelta(minutes=5)
    assert recipient.account_id == account.id
    assert recipient.status == "queued"
    assert message is not None and message.status == "queued"
    assert message.campaign_id == campaign.id
