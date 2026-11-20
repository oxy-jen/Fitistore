from datetime import datetime, timedelta, timezone

import pytest

from test_app import app, app_context, client
from app.extensions import db
from app.models import (
    CustomerAccount, EmailCampaign, EmailCampaignRecipient, EmailMessage,
)

def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _subscribed_account(email="campaign@example.test"):
    account = CustomerAccount(
        email=email, display_name="Campaign Customer",
        email_verified_at=_utcnow(), marketing_status="subscribed",
        marketing_consent_at=_utcnow(), created_at=_utcnow(),
    )
    db.session.add(account)
    db.session.flush()
    return account


def _campaign_with_message(campaign_type="custom", *, status="scheduled", email="campaign@example.test"):
    account = _subscribed_account(email)
    account_id = account.id
    scheduled_for = _utcnow() - timedelta(minutes=1)
    campaign = EmailCampaign(
        name="Campaign test", subject="A reviewed update", body="A short message.",
        campaign_type=campaign_type, product_ids_json="[]",
        status=status, scheduled_for=scheduled_for, approved_at=_utcnow(),
        previewed_at=_utcnow(), created_by_id=1, created_at=_utcnow(),
    )
    db.session.add(campaign)
    db.session.flush()
    campaign_id = campaign.id
    from app.services.email_purpose import MARKETING_CAMPAIGN_TYPE_KINDS
    message = EmailMessage(
        recipient=account.email, subject=campaign.subject,
        kind=MARKETING_CAMPAIGN_TYPE_KINDS[campaign_type], purpose="marketing",
        idempotency_key=f"marketing-campaign:{campaign_id}:account:{account_id}",
        account_id=account_id, campaign_id=campaign_id, status="queued",
        next_attempt_at=scheduled_for,
        html_body="<p>A short message.</p>", text_body="A short message.",
    )
    db.session.add(message)
    db.session.flush()
    db.session.add(EmailCampaignRecipient(
        campaign_id=campaign_id, account_id=account_id,
        recipient=email, status=message.status, message_id=message.id,
    ))
    db.session.commit()
    campaign = db.session.get(EmailCampaign, campaign_id)
    assert campaign is not None
    return campaign, account, message


@pytest.fixture
def marketing_enabled(app):
    app.config.update(
        EMAIL_MARKETING_QUEUE_ENABLED=True,
        EMAIL_MARKETING_CUSTOM_ENABLED=True,
        EMAIL_MARKETING_SEASONAL_ENABLED=False,
        EMAIL_MARKETING_NEW_PRODUCTS_ENABLED=False,
        EMAIL_MARKETING_OFFERS_ENABLED=False,
        EMAIL_MARKETING_WEEKLY_PICKS_ENABLED=False,
        EMAIL_MARKETING_ABANDONED_CART_ENABLED=False,
    )
    return app


def test_custom_campaign_uses_its_own_type_gate(marketing_enabled, app_context):
    from app.services.customer_mail import _marketing_campaign_type_enabled

    assert _marketing_campaign_type_enabled("custom")
    marketing_enabled.config["EMAIL_MARKETING_CUSTOM_ENABLED"] = False
    marketing_enabled.config["EMAIL_MARKETING_SEASONAL_ENABLED"] = True
    assert not _marketing_campaign_type_enabled("custom")


def test_campaign_type_gate_and_master_gate_both_fail_closed(app, app_context):
    campaign, _account, message = _campaign_with_message()
    from app.services import customer_mail

    app.config["EMAIL_MARKETING_QUEUE_ENABLED"] = False
    with pytest.raises(ValueError, match="disabled"):
        customer_mail.process_marketing_email_queue(
            confirmation="RUN MARKETING EMAIL QUEUE",
        )
    app.config["EMAIL_MARKETING_QUEUE_ENABLED"] = True
    app.config["EMAIL_MARKETING_CUSTOM_ENABLED"] = False
    result = customer_mail.process_marketing_email_queue(
        confirmation="RUN MARKETING EMAIL QUEUE",
    )
    db.session.refresh(message)
    assert result["marketing_types_disabled"] == 1
    assert message.status == "queued"
    assert campaign.status == "scheduled"


def test_campaign_worker_sends_only_due_campaign_linked_messages(marketing_enabled, app_context, monkeypatch):
    from app.services import customer_mail

    campaign, _account, message = _campaign_with_message()
    unrelated = EmailMessage(
        recipient="legacy@example.test", subject="Legacy campaign",
        kind="marketing_campaign", purpose="marketing",
        idempotency_key="legacy-marketing-row", status="queued",
        html_body=None, text_body=None,
    )
    db.session.add(unrelated)
    db.session.commit()
    monkeypatch.setattr(customer_mail, "mail_configuration_status", lambda: (True, "configured"))
    sent = []
    monkeypatch.setattr(customer_mail, "_send_brevo", lambda *args: sent.append(args) or "provider-id")

    result = customer_mail.process_marketing_email_queue(
        limit=10, confirmation="RUN MARKETING EMAIL QUEUE",
    )

    db.session.refresh(message)
    db.session.refresh(unrelated)
    db.session.refresh(campaign)
    recipient = EmailCampaignRecipient.query.filter_by(campaign_id=campaign.id).one()
    assert result["sent"] == 1
    assert len(sent) == 1
    assert message.status == "sent"
    assert unrelated.status == "queued"
    assert recipient.status == "sent"
    assert campaign.status == "sent"


def test_marketing_claim_is_single_use_for_campaign_message(marketing_enabled, app_context):
    from app.services.customer_mail import _claim_marketing_dispatch

    _campaign, _account, message = _campaign_with_message()
    first = _claim_marketing_dispatch(message, claim_token="marketing-claim-first")
    second = _claim_marketing_dispatch(message, claim_token="marketing-claim-second")

    assert first == (True, None, "sending")
    assert second == (False, None, "sending")
    assert EmailMessage.query.filter_by(id=message.id, status="sending").count() == 1


def test_paused_or_not_due_campaign_is_not_suppressed_by_dispatch_recheck(marketing_enabled, app_context):
    from app.services.customer_mail import _claim_marketing_dispatch

    campaign, _account, message = _campaign_with_message(status="paused")
    claimed, reason, observed_status = _claim_marketing_dispatch(message)

    db.session.refresh(message)
    db.session.refresh(campaign)
    assert not claimed
    assert reason is None
    assert observed_status == "queued"
    assert message.status == "queued"
    assert campaign.status == "paused"


def test_due_retry_for_non_campaign_marketing_kind_is_not_requeued(marketing_enabled, app_context, monkeypatch):
    from app.services import customer_mail

    row = EmailMessage(
        recipient="legacy@example.test", subject="Legacy",
        kind="marketing_campaign", purpose="marketing",
        idempotency_key="legacy-failed-marketing", status="failed",
        error_category="retryable_provider", retry_count=1,
        next_attempt_at=_utcnow() - timedelta(minutes=1), created_at=_utcnow(),
        html_body="<p>Legacy</p>", text_body="Legacy",
    )
    db.session.add(row)
    db.session.commit()
    monkeypatch.setattr(customer_mail, "mail_configuration_status", lambda: (True, "configured"))
    monkeypatch.setattr(customer_mail, "_send_brevo", lambda *args: pytest.fail("unapproved kind dispatched"))

    customer_mail.process_marketing_email_queue(
        limit=10, confirmation="RUN MARKETING EMAIL QUEUE",
    )

    db.session.refresh(row)
    assert row.status == "failed"


def test_due_campaign_retry_updates_recipient_and_dispatches(marketing_enabled, app_context, monkeypatch):
    from app.services import customer_mail

    campaign, _account, message = _campaign_with_message()
    recipient = EmailCampaignRecipient.query.filter_by(campaign_id=campaign.id).one()
    message.status = "failed"
    message.error_category = "retryable_provider"
    message.retry_count = 1
    message.next_attempt_at = _utcnow() - timedelta(minutes=1)
    recipient.status = "failed"
    recipient.failure_reason = "Previous throttling response."
    db.session.commit()
    monkeypatch.setattr(customer_mail, "mail_configuration_status", lambda: (True, "configured"))
    monkeypatch.setattr(customer_mail, "_send_brevo", lambda *args: "retry-provider-id")

    customer_mail.process_marketing_email_queue(
        confirmation="RUN MARKETING EMAIL QUEUE",
    )

    db.session.refresh(message)
    db.session.refresh(recipient)
    assert message.status == "sent"
    assert recipient.status == "sent"
    assert recipient.failure_reason == ""


def test_campaign_type_kind_mismatch_is_failed_without_dispatch(marketing_enabled, app_context, monkeypatch):
    from app.services import customer_mail

    marketing_enabled.config["EMAIL_MARKETING_NEW_PRODUCTS_ENABLED"] = True
    campaign, _account, message = _campaign_with_message()
    message.kind = "marketing_new_product"
    db.session.commit()
    monkeypatch.setattr(customer_mail, "mail_configuration_status", lambda: (True, "configured"))
    monkeypatch.setattr(customer_mail, "_send_brevo", lambda *args: pytest.fail("mismatched campaign dispatched"))

    result = customer_mail.process_marketing_email_queue(
        limit=10, confirmation="RUN MARKETING EMAIL QUEUE",
    )

    db.session.refresh(message)
    db.session.refresh(campaign)
    recipient = EmailCampaignRecipient.query.filter_by(campaign_id=campaign.id).one()
    assert result["failed"] == 1
    assert message.status == "failed"
    assert message.error_category == "invalid_campaign_message"
    assert recipient.status == "failed"
    assert campaign.status == "partial"


def test_campaign_cancel_preserves_ambiguous_provider_outcome(marketing_enabled, app_context):
    from app.services.marketing_campaigns import set_campaign_status

    campaign, _account, message = _campaign_with_message()
    recipient = EmailCampaignRecipient.query.filter_by(campaign_id=campaign.id).one()
    message.status = "failed"
    message.error_category = "ambiguous_outcome"
    message.next_attempt_at = None
    recipient.status = "failed"
    db.session.commit()

    set_campaign_status(campaign, "cancel")
    db.session.commit()
    db.session.refresh(message)
    db.session.refresh(recipient)

    assert campaign.status == "cancelled"
    assert message.status == "failed"
    assert message.error_category == "ambiguous_outcome"
    assert recipient.status == "failed"


def test_admin_campaign_preview_is_visible_and_does_not_queue_email(client, app_context):
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    created = client.post("/admin/email/campaigns", data={
        "name": "Preview test", "subject": "A preview",
        "body": "This content is only a preview.", "campaign_type": "custom",
    })
    assert created.status_code == 302
    campaign = EmailCampaign.query.filter_by(name="Preview test").one()
    response = client.post(f"/admin/email/campaigns/{campaign.id}", data={
        "name": campaign.name, "subject": campaign.subject, "body": campaign.body,
        "campaign_type": campaign.campaign_type, "action": "preview",
    })
    assert response.status_code == 200
    assert b"Preview (not sent)" in response.data
    assert EmailMessage.query.filter_by(campaign_id=campaign.id).count() == 0
