from datetime import datetime, timedelta, timezone

import pytest

from test_app import app, app_context, client
from app.extensions import db
from app.models import (
    CustomerAccount, EmailCampaign, EmailCampaignRecipient, EmailDeliveryAttempt,
    EmailDeliveryInvestigation, EmailDeliveryInvestigationEvent, EmailMessage,
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


def _failed_401_campaign(count=9):
    from app.services.email_purpose import MARKETING_CAMPAIGN_TYPE_KINDS
    campaign = EmailCampaign(
        name="Recovery test", subject="Reviewed campaign subject",
        body="Original saved campaign content.", campaign_type="custom",
        product_ids_json="[]", status="partial", approved_at=_utcnow(),
        previewed_at=_utcnow(), created_by_id=1, created_at=_utcnow(),
    )
    db.session.add(campaign)
    db.session.flush()
    rows = []
    for index in range(count):
        account = _subscribed_account(f"recovery-{index}@example.test")
        message = EmailMessage(
            recipient=account.email, subject=campaign.subject,
            kind=MARKETING_CAMPAIGN_TYPE_KINDS["custom"], purpose="marketing", campaign_id=campaign.id,
            account_id=account.id,
            idempotency_key=f"marketing-campaign:{campaign.id}:account:{account.id}",
            status="failed", error_category="permanent_provider",
            failure_reason="Brevo HTTP 401.", retry_count=1,
            html_body=None, text_body=None,
        )
        db.session.add(message)
        db.session.flush()
        recipient = EmailCampaignRecipient(
            campaign_id=campaign.id, account_id=account.id, recipient=account.email,
            status="failed", failure_reason="Brevo HTTP 401.", message_id=message.id,
        )
        attempt = EmailDeliveryAttempt(
            message_id=message.id, attempt_number=1,
            attempt_key=f"failed-attempt-{campaign.id}-{account.id}",
            provider="brevo", status="failed", started_at=_utcnow(),
            completed_at=_utcnow(), error_category="permanent_provider",
        )
        db.session.add_all([recipient, attempt])
        rows.append((account, message, recipient, attempt))
    db.session.commit()
    db.session.refresh(campaign)
    return campaign, rows


def _recover_campaign(campaign, *, count=9, scheduled_for=None):
    from app.services.marketing_campaigns import recover_confirmed_campaign_rejections
    scheduled_for = scheduled_for or (_utcnow() + timedelta(hours=1))
    return recover_confirmed_campaign_rejections(
        campaign.id, 1, scheduled_for=scheduled_for,
        authorization=f"RECOVER {count}",
    )


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


def test_confirmed_brevo_401_campaign_recovery_reuses_nine_rows_without_sending(marketing_enabled, app_context, monkeypatch):
    from app.services import customer_mail
    from app.services.marketing_campaigns import campaign_recovery_candidate_count

    campaign, originals = _failed_401_campaign()
    original_message_ids = [message.id for _account, message, _recipient, _attempt in originals]
    monkeypatch.setattr(customer_mail, "_send_brevo", lambda *args: pytest.fail("recovery must not send"))

    assert campaign_recovery_candidate_count(campaign) == 9
    result = _recover_campaign(campaign)

    assert result == {"requeued": 9, "suppressed": 0}
    db.session.refresh(campaign)
    assert campaign.status == "scheduled"
    assert campaign.scheduled_for > _utcnow()
    assert EmailMessage.query.filter_by(campaign_id=campaign.id).count() == 9
    assert EmailCampaignRecipient.query.filter_by(campaign_id=campaign.id).count() == 9
    assert [message.id for _account, message, _recipient, _attempt in originals] == original_message_ids
    for account, message, recipient, attempt in originals:
        db.session.refresh(message)
        db.session.refresh(recipient)
        assert message.status == recipient.status == "queued"
        assert message.campaign_id == recipient.campaign_id == campaign.id
        assert message.account_id == recipient.account_id == account.id
        assert message.idempotency_key == f"marketing-campaign:{campaign.id}:account:{account.id}"
        assert "Original saved campaign content." in message.text_body
        assert message.subject == campaign.subject
        assert message.next_attempt_at == campaign.scheduled_for
        investigation = EmailDeliveryInvestigation.query.filter_by(
            message_id=message.id, recovered_message_id=message.id,
        ).one()
        event = EmailDeliveryInvestigationEvent.query.filter_by(
            investigation_id=investigation.id,
        ).one()
        assert investigation.original_attempt_id == attempt.id
        assert investigation.resolution_outcome == "not_accepted"
        assert event.event_type == "campaign_recovery_authorized"
    with pytest.raises(ValueError, match="partially failed"):
        _recover_campaign(campaign)
    assert EmailMessage.query.filter_by(campaign_id=campaign.id).count() == 9


def test_definitive_brevo_401_clears_rendered_payload_but_campaign_can_be_reconstructed(marketing_enabled, app_context, monkeypatch):
    from urllib.error import HTTPError
    from app.services import customer_mail
    from app.services.marketing_campaigns import campaign_recovery_candidate_count

    campaign, _account, message = _campaign_with_message()
    monkeypatch.setattr(customer_mail, "mail_configuration_status", lambda: (True, "configured"))
    def reject(*_args):
        raise HTTPError("https://provider.invalid", 401, "unauthorized", {}, None)
    monkeypatch.setattr(customer_mail, "_send_brevo", reject)

    assert not customer_mail.deliver_email(message)
    db.session.refresh(message)
    assert message.status == "failed"
    assert message.error_category == "permanent_provider"
    assert message.failure_reason == "Brevo HTTP 401."
    assert message.next_attempt_at is None
    assert message.html_body is None and message.text_body is None
    attempt = EmailDeliveryAttempt.query.filter_by(message_id=message.id).one()
    assert attempt.status == "failed"
    assert attempt.error_category == "permanent_provider"

    campaign.status = "partial"
    recipient = EmailCampaignRecipient.query.filter_by(message_id=message.id).one()
    recipient.status = "failed"
    db.session.commit()
    assert campaign_recovery_candidate_count(campaign) == 1

    result = _recover_campaign(campaign, count=1)
    db.session.refresh(message)
    assert result == {"requeued": 1, "suppressed": 0}
    assert "A short message." in message.text_body


def test_campaign_recovery_excludes_ambiguous_or_non_401_messages(marketing_enabled, app_context):
    from app.services.marketing_campaigns import campaign_recovery_candidate_count

    campaign, originals = _failed_401_campaign(count=3)
    _account, ambiguous, _recipient, _attempt = originals[0]
    ambiguous.error_category = "ambiguous_outcome"
    _account, other_status, _recipient, _attempt = originals[1]
    other_status.failure_reason = "Brevo HTTP 403."
    _account, repeated, _recipient, _attempt = originals[2]
    repeated.retry_count = 2
    db.session.commit()

    assert campaign_recovery_candidate_count(campaign) == 0


def test_campaign_recovery_rechecks_eligibility_and_suppresses_withdrawn_account(marketing_enabled, app_context):
    campaign, originals = _failed_401_campaign(count=2)
    withdrawn_account, withdrawn_message, withdrawn_recipient, _attempt = originals[0]
    withdrawn_account.marketing_status = "unsubscribed"
    withdrawn_account.marketing_withdrawn_at = _utcnow()
    db.session.commit()

    result = _recover_campaign(campaign, count=2)
    db.session.refresh(withdrawn_message)
    db.session.refresh(withdrawn_recipient)
    assert result == {"requeued": 1, "suppressed": 1}
    assert withdrawn_message.status == withdrawn_recipient.status == "suppressed"
    assert withdrawn_message.html_body is None and withdrawn_message.text_body is None
    assert EmailMessage.query.filter_by(campaign_id=campaign.id, status="queued").count() == 1
    investigation = EmailDeliveryInvestigation.query.filter_by(
        message_id=withdrawn_message.id,
    ).one()
    assert investigation.resolution_outcome == "not_accepted"
    assert investigation.recovered_message_id is None
    event = EmailDeliveryInvestigationEvent.query.filter_by(
        investigation_id=investigation.id,
    ).one()
    assert event.event_type == "campaign_recovery_suppressed"


def test_campaign_recovery_requires_delayed_exact_confirmation_and_preserves_state(marketing_enabled, app_context):
    from app.services.marketing_campaigns import recover_confirmed_campaign_rejections

    campaign, _originals = _failed_401_campaign(count=2)
    with pytest.raises(ValueError, match="RECOVER 2"):
        recover_confirmed_campaign_rejections(
            campaign.id, 1, scheduled_for=_utcnow() + timedelta(hours=1), authorization="RECOVER 1",
        )
    with pytest.raises(ValueError, match="30 minutes"):
        recover_confirmed_campaign_rejections(
            campaign.id, 1, scheduled_for=_utcnow() + timedelta(minutes=10), authorization="RECOVER 2",
        )
    db.session.rollback()
    db.session.refresh(campaign)
    assert campaign.status == "partial"
    assert EmailMessage.query.filter_by(campaign_id=campaign.id, status="failed").count() == 2


def test_admin_campaign_recovery_action_schedules_only_and_shows_candidate_count(client, marketing_enabled, app_context, monkeypatch):
    from app.services import customer_mail

    campaign, originals = _failed_401_campaign()
    monkeypatch.setattr(customer_mail, "deliver_email", lambda *_args, **_kwargs: pytest.fail("admin recovery must not dispatch"))
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    page = client.get(f"/admin/email/campaigns/{campaign.id}")
    assert page.status_code == 200
    assert b"Recover confirmed Brevo rejections" in page.data
    assert b"RECOVER 9" in page.data

    from zoneinfo import ZoneInfo
    when = datetime.now(ZoneInfo("Africa/Nairobi")) + timedelta(hours=1)
    response = client.post(f"/admin/email/campaigns/{campaign.id}", data={
        "action": "recover_confirmed_401", "confirmation": "RECOVER 9",
        "scheduled_for": when.strftime("%Y-%m-%dT%H:%M"),
    })
    if response.status_code != 302:
        from bs4 import BeautifulSoup
        raise AssertionError(BeautifulSoup(response.data, "html.parser").get_text(" ", strip=True))
    db.session.refresh(campaign)
    assert campaign.status == "scheduled"
    assert EmailMessage.query.filter_by(campaign_id=campaign.id, status="queued").count() == 9
    assert all(recipient.status == "queued" for _account, _message, recipient, _attempt in originals)


def test_recovered_campaign_is_processed_only_by_due_marketing_worker(marketing_enabled, app_context, monkeypatch):
    from app.services import customer_mail

    campaign, originals = _failed_401_campaign(count=1)
    scheduled_for = _utcnow() + timedelta(minutes=45)
    _recover_campaign(campaign, count=1, scheduled_for=scheduled_for)
    _account, message, recipient, prior_attempt = originals[0]
    sent = []
    monkeypatch.setattr(customer_mail, "mail_configuration_status", lambda: (True, "configured"))
    monkeypatch.setattr(customer_mail, "_send_brevo", lambda *args: sent.append(args) or "accepted-id")

    before_due = customer_mail.process_marketing_email_queue(
        now=scheduled_for - timedelta(minutes=1),
        confirmation="RUN MARKETING EMAIL QUEUE",
    )
    db.session.refresh(message)
    assert before_due["sent"] == 0 and not sent
    assert message.status == "queued"

    # Simulate the review window passing. The final dispatch claim uses its
    # own current-time check rather than trusting a caller-supplied clock.
    due_at = _utcnow() - timedelta(minutes=1)
    campaign.scheduled_for = due_at
    message.next_attempt_at = due_at
    db.session.commit()
    result = customer_mail.process_marketing_email_queue(
        now=_utcnow(),
        confirmation="RUN MARKETING EMAIL QUEUE",
    )
    db.session.refresh(message)
    db.session.refresh(recipient)
    assert result["sent"] == 1 and len(sent) == 1
    assert message.status == recipient.status == "sent"
    next_attempt = EmailDeliveryAttempt.query.filter_by(message_id=message.id).order_by(
        EmailDeliveryAttempt.attempt_number.desc(),
    ).first()
    assert next_attempt.attempt_number == 2
    assert next_attempt.recovery_of_attempt_id == prior_attempt.id


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


def test_admin_can_identify_and_discard_an_automatic_draft(client, app_context):
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    campaign = EmailCampaign(
        name="Weekly picks — 2026-W47", subject="This week's FityStores picks",
        body="A selection of available products.", campaign_type="weekly_picks",
        product_ids_json="[]", status="draft", created_by_id=1,
        generation_key="prepare:weekly_picks:2026-W47",
        generation_source="weekly_picks",
        generation_reason="Selected from recorded paid sales and cart additions.",
    )
    db.session.add(campaign)
    db.session.commit()

    listing = client.get("/admin/email/campaigns")
    assert listing.status_code == 200
    assert b"Automatically" in listing.data or b"Automatic" in listing.data
    assert campaign.generation_reason.encode() in listing.data
    response = client.post(
        f"/admin/email/campaigns/{campaign.id}", data={"action": "discard"},
    )

    db.session.refresh(campaign)
    assert response.status_code == 200
    assert campaign.status == "cancelled"
    assert campaign.generation_key == "prepare:weekly_picks:2026-W47"
    assert campaign.approved_at is None and campaign.scheduled_for is None
    assert EmailMessage.query.filter_by(campaign_id=campaign.id).count() == 0
