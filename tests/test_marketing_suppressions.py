from datetime import datetime, timedelta
import hashlib

import pytest
from sqlalchemy import inspect

# Reuse the suite's in-memory SQLite application fixtures; never use a persisted DB.
from test_app import app, app_context, client

from app.extensions import db
from app.models import (
    AdminUser, CustomerAccount, CustomerConsentEvent, CustomerToken, EmailMessage,
    MarketingSuppressionCase, MarketingSuppressionEvent,
)
from app.services.marketing_suppressions import (
    LEGACY_IMPORT_DDL, MIGRATION_ID, SUPPRESSION_DDL,
    apply_marketing_suppression_migration, fingerprint_provider_event,
    migration_checksum, record_suppression,
)
from app.services.customer_mail import deliver_email, queue_email


def _account(email="suppression@example.test"):
    account = CustomerAccount(
        email=email, display_name="Suppression Test",
        email_verified_at=datetime.utcnow(), marketing_status="subscribed",
        marketing_consent_at=datetime.utcnow(),
    )
    db.session.add(account)
    db.session.flush()
    return account


def test_suppression_models_create_additive_tables_in_isolated_sqlite(app, app_context):
    names = set(inspect(db.engine).get_table_names())
    assert "marketing_suppression_case" in names
    assert "marketing_suppression_event" in names
    assert "marketing_suppression_schema_migration" not in names
    assert "order" in names and "product" in names and "customer_account" in names


def test_provider_suppression_creates_case_and_ledger_once_for_duplicate_fingerprint(app, app_context):
    account = _account()
    fingerprint = "a" * 64
    first = record_suppression(
        account.id, source="provider", reason_code="hard_bounce",
        reason_text="Provider reported a permanent delivery failure.",
        actor_type="provider", provider_name="brevo", dedupe_fingerprint=fingerprint,
    )
    db.session.commit()
    second = record_suppression(
        account.id, source="provider", reason_code="hard_bounce",
        reason_text="Provider reported a permanent delivery failure.",
        actor_type="provider", provider_name="brevo", dedupe_fingerprint=fingerprint,
    )
    db.session.commit()
    assert first.id == second.id
    assert account.marketing_status == "suppressed"
    assert MarketingSuppressionCase.query.filter_by(account_id=account.id, cleared_at=None).count() == 1
    events = MarketingSuppressionEvent.query.filter_by(account_id=account.id).all()
    assert len(events) == 1
    assert events[0].dedupe_fingerprint == fingerprint
    assert events[0].event_type == "suppression_detected"


def test_active_structured_case_blocks_marketing_even_if_legacy_status_is_subscribed(app, app_context, monkeypatch):
    account = _account("sticky-case@example.test")
    record_suppression(
        account.id, source="provider", reason_code="spam_complaint",
        reason_text="Spam complaint received.", actor_type="provider", provider_name="brevo",
    )
    db.session.commit()
    account.marketing_status = "subscribed"  # simulate an inconsistent/stale legacy field
    db.session.commit()
    sent = []
    monkeypatch.setattr("app.services.customer_mail.deliver_email", lambda *args: sent.append(args) or True)
    result = queue_email(
        account.email, "A promotion", "marketing_daily_goodies", "suppressed-case-test",
        account_id=account.id, html_body="<p>Offer</p>", text_body="Offer",
    )
    assert result.status == "suppressed"
    assert result.failure_reason == "active_suppression"
    assert not sent


def test_central_marketing_eligibility_requires_verified_consent_and_valid_address(app, app_context):
    from app.services.marketing_eligibility import evaluate_marketing_eligibility

    account = _account("central-eligibility@example.test")
    assert evaluate_marketing_eligibility(account).eligible
    account.email_verified_at = None
    assert evaluate_marketing_eligibility(account).reason_code == "email_unverified"
    account.email_verified_at = datetime.utcnow()
    account.marketing_consent_at = None
    assert evaluate_marketing_eligibility(account).reason_code == "consent_missing"
    account.marketing_consent_at = datetime.utcnow()
    account.marketing_withdrawn_at = datetime.utcnow()
    assert evaluate_marketing_eligibility(account).reason_code == "consent_withdrawn"
    account.marketing_withdrawn_at = None
    account.marketing_status = "unsubscribed"
    assert evaluate_marketing_eligibility(account).reason_code == "not_subscribed"
    account.marketing_status = "subscribed"
    account.active = False
    assert evaluate_marketing_eligibility(account).reason_code == "account_inactive"


def test_central_eligibility_fails_closed_when_suppression_case_table_is_missing(
    app, app_context, monkeypatch,
):
    from types import SimpleNamespace
    from app.services.marketing_eligibility import evaluate_marketing_eligibility

    account = SimpleNamespace(
        id=901, email="missing-suppression-schema@example.test", active=True,
        email_verified_at=datetime.utcnow(), marketing_status="subscribed",
        marketing_consent_at=datetime.utcnow(), marketing_withdrawn_at=None,
        created_at=datetime.utcnow(),
    )

    class MissingCaseInspector:
        @staticmethod
        def has_table(_table_name):
            return False

    monkeypatch.setattr("sqlalchemy.inspect", lambda _connection: MissingCaseInspector())
    result = evaluate_marketing_eligibility(account)
    assert not result.eligible
    assert result.reason_code == "suppression_schema_unavailable"


def test_central_eligibility_fails_closed_when_suppression_case_columns_are_incomplete(
    app, app_context, monkeypatch,
):
    from types import SimpleNamespace
    from app.services.marketing_eligibility import evaluate_marketing_eligibility

    account = SimpleNamespace(
        id=902, email="incomplete-suppression-schema@example.test", active=True,
        email_verified_at=datetime.utcnow(), marketing_status="subscribed",
        marketing_consent_at=datetime.utcnow(), marketing_withdrawn_at=None,
        created_at=datetime.utcnow(),
    )

    class IncompleteCaseInspector:
        @staticmethod
        def has_table(_table_name):
            return True

        @staticmethod
        def get_columns(_table_name):
            return [{"name": "account_id"}]

    monkeypatch.setattr("sqlalchemy.inspect", lambda _connection: IncompleteCaseInspector())
    result = evaluate_marketing_eligibility(account)
    assert not result.eligible
    assert result.reason_code == "suppression_schema_incomplete"


def test_central_eligibility_fails_closed_when_schema_inspection_raises(
    app, app_context, monkeypatch,
):
    from types import SimpleNamespace
    from app.services.marketing_eligibility import evaluate_marketing_eligibility

    account = SimpleNamespace(
        id=903, email="inspection-error@example.test", active=True,
        email_verified_at=datetime.utcnow(), marketing_status="subscribed",
        marketing_consent_at=datetime.utcnow(), marketing_withdrawn_at=None,
        created_at=datetime.utcnow(),
    )

    def fail_inspection(_connection):
        raise RuntimeError("simulated catalog inspection failure")

    monkeypatch.setattr("sqlalchemy.inspect", fail_inspection)
    result = evaluate_marketing_eligibility(account)
    assert not result.eligible
    assert result.reason_code == "eligibility_unavailable"


def test_central_eligibility_blocks_active_case_and_allows_only_cleared_case(
    app, app_context,
):
    from app.services.marketing_eligibility import evaluate_marketing_eligibility

    account = _account("suppression-clearance-eligibility@example.test")
    case = record_suppression(
        account.id, source="admin", reason_code="admin_review",
        reason_text="Review pending.", actor_type="admin",
        admin_id=AdminUser.query.filter_by(username="owner").one().id,
    )
    db.session.commit()
    account.marketing_status = "subscribed"
    assert evaluate_marketing_eligibility(account).reason_code == "active_suppression"

    admin_id = AdminUser.query.filter_by(username="owner").one().id
    case.cleared_at = datetime.utcnow()
    case.cleared_by_admin_id = admin_id
    case.clearance_reason = "Administrator reviewed the suppression."
    account.marketing_status = "subscribed"
    db.session.commit()
    assert evaluate_marketing_eligibility(account).eligible


def test_transactional_email_waits_for_outer_commit_then_dispatches_without_worker(app, app_context, monkeypatch):
    from app.services.customer_mail import process_email_queue

    calls = []
    monkeypatch.setattr("app.services.customer_mail.mail_configuration_status", lambda: (True, "configured"))
    monkeypatch.setattr("app.services.customer_mail._send_brevo", lambda *args: calls.append(args) or "queued-provider-id")
    app.config["MAIL_PROVIDER"] = "brevo"
    row = queue_email(
        "queue@example.test", "Order update", "order_status", "durable-queue-test",
        html_body="<p>Persisted</p>", text_body="Persisted", commit=False,
    )
    assert row.status == "queued"
    assert row.html_body == "<p>Persisted</p>"
    assert calls == []
    db.session.commit()
    from app.services.customer_mail import dispatch_transactional_email_now
    result = dispatch_transactional_email_now(row)
    db.session.refresh(row)
    assert result.status == "sent"
    assert row.provider_message_id == "queued-provider-id"
    assert row.html_body is None and row.text_body is None
    assert len(calls) == 1
    duplicate = queue_email(
        "queue@example.test", "Order update", "order_status", "durable-queue-test",
        html_body="<p>Persisted</p>", text_body="Persisted",
    )
    assert duplicate.id == row.id
    assert duplicate.status == "sent"
    assert len(calls) == 1
    outcome = process_email_queue(limit=5)
    db.session.refresh(row)
    assert outcome["sent"] == 0
    assert row.status == "sent"
    assert len(calls) == 1


def test_transactional_dispatch_ignores_marketing_consent_while_marketing_stays_queued(app, app_context, monkeypatch):
    sent = []
    monkeypatch.setattr("app.services.customer_mail.mail_configuration_status", lambda: (True, "configured"))
    monkeypatch.setattr("app.services.customer_mail._send_brevo", lambda *args: sent.append(args) or "direct-provider-id")
    app.config["MAIL_PROVIDER"] = "brevo"
    account = CustomerAccount(
        email="no-consent@example.test", display_name="No consent",
        marketing_status="unsubscribed",
    )
    db.session.add(account)
    db.session.commit()
    marketing = queue_email(
        account.email, "A promotion", "marketing_campaign", "no-consent-promotion",
        account_id=account.id, html_body="<p>Offer</p>", text_body="Offer",
    )
    transactional = queue_email(
        account.email, "Order update", "order_status", "no-consent-order",
        account_id=account.id, html_body="<p>Order</p>", text_body="Order",
    )
    assert marketing.status == "suppressed"
    assert marketing.failure_reason == "account_inactive" or marketing.failure_reason in {
        "email_unverified", "not_subscribed", "consent_missing",
    }
    assert transactional.status == "sent"
    assert transactional.purpose == "transactional"
    assert len(sent) == 1


def test_promotional_queue_does_not_dispatch_directly(app, app_context, monkeypatch):
    sent = []
    account = _account("marketing-stays-queued@example.test")
    monkeypatch.setattr("app.services.customer_mail.mail_configuration_status", lambda: (True, "configured"))
    monkeypatch.setattr("app.services.customer_mail._send_brevo", lambda *args: sent.append(args) or "unexpected")
    app.config["MAIL_PROVIDER"] = "brevo"

    message = queue_email(
        account.email, "Daily Goodies", "marketing_daily_goodies", "marketing-remains-queued",
        account_id=account.id, html_body="<p>Offer</p>", text_body="Offer",
    )

    assert message.status == "queued"
    assert message.purpose == "marketing"
    assert not sent


def test_new_provider_event_after_clearance_creates_a_new_active_case(app, app_context):
    account = _account("repeat-provider-event@example.test")
    first = record_suppression(
        account.id, source="provider", reason_code="hard_bounce",
        reason_text="First permanent bounce.", actor_type="provider", provider_name="brevo",
        dedupe_fingerprint="1" * 64,
    )
    db.session.flush()
    admin = AdminUser.query.filter_by(username="owner").one()
    first.cleared_at = datetime.utcnow()
    first.cleared_by_admin_id = admin.id
    first.clearance_reason = "Address was reviewed and corrected."
    db.session.add(MarketingSuppressionEvent(
        suppression_case_id=first.id, account_id=account.id,
        event_type="suppression_cleared", actor_type="admin", admin_id=admin.id,
        source="provider", reason_code="hard_bounce", provider_name="brevo",
        reason_text=first.clearance_reason, idempotency_key="test-clear-first-case",
    ))
    account.marketing_status = "unsubscribed"
    db.session.flush()
    second = record_suppression(
        account.id, source="provider", reason_code="hard_bounce",
        reason_text="A later permanent bounce.", actor_type="provider", provider_name="brevo",
        dedupe_fingerprint="2" * 64,
    )
    db.session.commit()
    assert second.id != first.id
    assert second.cleared_at is None
    assert account.marketing_status == "suppressed"


def test_admin_suppression_invalidates_pending_marketing_token_and_clear_does_not_subscribe(app, app_context, client):
    admin = AdminUser.query.filter_by(username="owner").one()
    account = _account("admin-suppression@example.test")
    raw_token = "fresh-consent-token"
    token = CustomerToken(
        account_id=account.id, purpose="marketing_confirm",
        token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
        expires_at=datetime.utcnow() + timedelta(hours=2),
    )
    db.session.add(token)
    db.session.commit()
    with client.session_transaction() as browser_session:
        browser_session["_user_id"] = str(admin.get_id())
        browser_session["_fresh"] = True

    response = client.post(f"/admin/email/customers/{account.id}/suppress", data={"reason": "Provider review requested a pause."})
    assert response.status_code == 302
    account = db.session.get(CustomerAccount, account.id)
    db.session.refresh(token)
    assert account.marketing_status == "suppressed"
    assert token.used_at is not None
    case = MarketingSuppressionCase.query.filter_by(account_id=account.id, source="admin", cleared_at=None).one()
    event = MarketingSuppressionEvent.query.filter_by(suppression_case_id=case.id).one()
    assert event.admin_id == admin.id
    assert event.reason_text == "Provider review requested a pause."

    response = client.post(f"/account/marketing/confirm/{raw_token}")
    assert response.status_code == 302
    db.session.refresh(account)
    assert account.marketing_status == "suppressed"

    response = client.post(f"/admin/email/suppression-cases/{case.id}/clear", data={"reason": "Reviewed and confirmed that the provider issue is resolved."})
    assert response.status_code == 302
    db.session.refresh(account)
    db.session.refresh(case)
    assert case.cleared_at is not None
    assert case.cleared_by_admin_id == admin.id
    assert case.clearance_reason == "Reviewed and confirmed that the provider issue is resolved."
    assert account.marketing_status == "unsubscribed"
    assert account.marketing_consent_at is not None
    assert MarketingSuppressionEvent.query.filter_by(suppression_case_id=case.id, event_type="suppression_cleared").count() == 1


def test_voluntary_unsubscribe_remains_separate_from_sticky_suppression(app, app_context, client):
    account = _account("voluntary-unsubscribe@example.test")
    raw_token = "unsubscribe-token"
    db.session.add(CustomerToken(
        account_id=account.id, purpose="unsubscribe",
        token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
        expires_at=datetime.utcnow() + timedelta(hours=2),
    ))
    db.session.commit()
    response = client.post(f"/account/unsubscribe/{raw_token}")
    assert response.status_code == 200
    db.session.refresh(account)
    assert account.marketing_status == "unsubscribed"
    assert account.marketing_withdrawn_at is not None
    assert MarketingSuppressionCase.query.filter_by(account_id=account.id).count() == 0
    confirmation = EmailMessage.query.filter_by(
        idempotency_key=f"unsubscribe-link:{CustomerToken.query.filter_by(purpose='unsubscribe').one().id}",
    ).one()
    assert confirmation.status == "queued"


@pytest.mark.parametrize("queue_result", ["exception", "none", "failed"])
def test_unsubscribe_stays_effective_when_confirmation_queue_fails(
    app, app_context, client, monkeypatch, caplog, queue_result,
):
    from types import SimpleNamespace

    account = _account("unsubscribe-queue-failure@example.test")
    raw_token = "unsubscribe-queue-failure-token"
    token = CustomerToken(
        account_id=account.id, purpose="unsubscribe",
        token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
        expires_at=datetime.utcnow() + timedelta(hours=2),
    )
    db.session.add(token)
    db.session.commit()

    if queue_result == "exception":
        def fail_confirmation(*_args, **_kwargs):
            raise RuntimeError("queue unavailable")
        monkeypatch.setattr("app.customer.routes.queue_branded_email", fail_confirmation)
    elif queue_result == "none":
        monkeypatch.setattr("app.customer.routes.queue_branded_email", lambda *_args, **_kwargs: None)
    else:
        monkeypatch.setattr(
            "app.customer.routes.queue_branded_email",
            lambda *_args, **_kwargs: SimpleNamespace(status="failed"),
        )
    response = client.post(f"/account/unsubscribe/{raw_token}")

    db.session.refresh(account)
    db.session.refresh(token)
    assert response.status_code == 200
    assert account.marketing_status == "unsubscribed"
    assert account.marketing_withdrawn_at is not None
    assert token.used_at is not None
    assert CustomerConsentEvent.query.filter_by(
        account_id=account.id, event="withdrawn", method="unsubscribe_link",
    ).count() == 1
    response_text = response.get_data(as_text=True)
    assert "Your unsubscribe is complete" in response_text
    assert "confirmation email" in response_text
    assert "unsubscribe_confirmation_queue_failed" in caplog.text


def test_marketing_generic_unsubscribe_maps_to_unclassified_provider_suppression(client, app, app_context, monkeypatch):
    account = _account("provider-unsubscribe@example.test")
    account_id = account.id
    db.session.commit()
    app.config["BREVO_WEBHOOK_TOKEN"] = "test-webhook-token"
    response = client.post(
        "/webhooks/brevo/email-events",
        json={"event": "unsubscribe", "email": account.email, "ts_event": 1234567890},
        headers={"X-Fity-Brevo-Token": "test-webhook-token"},
    )
    assert response.status_code == 200
    account = db.session.get(CustomerAccount, account_id)
    assert account.marketing_status == "suppressed"
    assert account.marketing_withdrawn_at is None
    case = MarketingSuppressionCase.query.filter_by(account_id=account.id, source="provider").one()
    assert case.reason_code == "provider_unsubscribe_unclassified"


def test_marketing_dispatch_claim_is_blocked_when_suppression_precedes_claim(app, app_context):
    from app.services.customer_mail import _claim_marketing_dispatch

    account = _account("claim-after-suppression@example.test")
    record = EmailMessage(
        recipient=account.email, account_id=account.id, kind="marketing_daily_goodies",
        subject="Daily Goodies", idempotency_key="claim-after-suppression", status="queued",
    )
    db.session.add(record)
    db.session.commit()
    record_suppression(
        account.id, source="provider", reason_code="hard_bounce",
        reason_text="Provider suppression was recorded first.", actor_type="provider", provider_name="brevo",
    )
    db.session.commit()

    claimed, reason, status = _claim_marketing_dispatch(record)
    assert not claimed
    assert reason == "not_subscribed"
    assert status == "suppressed"
    db.session.refresh(record)
    assert record.status == "suppressed"


def test_dispatch_claim_checks_active_case_even_if_legacy_status_is_stale(app, app_context):
    from app.services.customer_mail import _claim_marketing_dispatch

    account = _account("claim-active-case@example.test")
    record = EmailMessage(
        recipient=account.email, account_id=account.id, kind="marketing_weekly_picks",
        subject="Weekly Picks", idempotency_key="claim-active-case", status="queued",
    )
    db.session.add(record)
    db.session.commit()
    record_suppression(
        account.id, source="provider", reason_code="spam_complaint",
        reason_text="Provider complaint.", actor_type="provider", provider_name="brevo",
    )
    db.session.commit()
    account.marketing_status = "subscribed"
    db.session.commit()

    claimed, reason, status = _claim_marketing_dispatch(record)
    assert not claimed
    assert reason == "active_suppression"
    assert status == "suppressed"


def test_transactional_message_is_independent_of_marketing_suppression(app, app_context, monkeypatch):
    account = _account("transactional-under-suppression@example.test")
    record_suppression(
        account.id, source="admin", reason_code="admin_review",
        reason_text="Pause promotions pending review.", actor_type="admin",
        admin_id=AdminUser.query.filter_by(username="owner").one().id,
    )
    record = EmailMessage(
        recipient=account.email, account_id=account.id, kind="order_received",
        subject="Order received", idempotency_key="transactional-under-suppression", status="queued",
    )
    db.session.add(record)
    db.session.commit()
    monkeypatch.setattr("app.services.customer_mail.mail_configuration_status", lambda: (True, "configured"))
    monkeypatch.setattr("app.services.customer_mail._send_brevo", lambda *args: "test-provider-id")
    assert deliver_email(record, "<p>Order received</p>", "Order received")
    assert record.status == "sent"


def test_suppression_administration_rejects_anonymous_access(client, app, app_context):
    account = _account("anonymous-admin-test@example.test")
    db.session.commit()
    response = client.post(f"/admin/email/customers/{account.id}/suppress", data={"reason": "Review this address."})
    assert response.status_code in {302, 401}


def test_suppression_administration_rejects_customer_access(client, app, app_context):
    account = _account("non-admin-suppression-test@example.test")
    db.session.commit()
    with client.session_transaction() as browser_session:
        browser_session["_user_id"] = str(account.get_id())
        browser_session["_fresh"] = True
    response = client.post(f"/admin/email/customers/{account.id}/suppress", data={"reason": "Review this address."})
    assert response.status_code == 403


def test_suppression_administration_requires_csrf_for_admin_post(client, app, app_context):
    admin = AdminUser.query.filter_by(username="owner").one()
    account = _account("csrf-suppression-test@example.test")
    db.session.commit()
    previous = app.config.get("WTF_CSRF_ENABLED", False)
    app.config["WTF_CSRF_ENABLED"] = True
    try:
        with client.session_transaction() as browser_session:
            browser_session["_user_id"] = str(admin.get_id())
            browser_session["_fresh"] = True
        response = client.post(f"/admin/email/customers/{account.id}/suppress", data={"reason": "Review this address."})
        assert response.status_code == 400
    finally:
        app.config["WTF_CSRF_ENABLED"] = previous


def test_provider_fingerprint_uses_message_or_event_time_not_webhook_configuration_id():
    payload = {"id": 44, "event": "hardBounce", "email": "a@example.test", "message-id": "msg-1", "ts_event": 123}
    first = fingerprint_provider_event(payload, normalized_event="hardbounce", email="a@example.test")
    repeated = fingerprint_provider_event(dict(payload, id=45), normalized_event="hardbounce", email="a@example.test")
    assert first == repeated
    assert fingerprint_provider_event({"id": 44}, normalized_event="unsubscribed", email="a@example.test") is None


def test_provider_fingerprint_canonicalizes_key_order_and_ignores_webhook_id():
    first = {"event": "hardBounce", "email": "A@example.test", "description": "mailbox unavailable", "messageId": "m-1", "ts_event": 123, "id": 1}
    reordered = {"description": "mailbox unavailable", "id": 91, "ts_event": 123, "messageId": "m-1", "email": "a@example.test", "event": "hardBounce"}
    assert fingerprint_provider_event(first, normalized_event="hardbounce", email="a@example.test") == fingerprint_provider_event(
        reordered, normalized_event="hardbounce", email="A@example.test"
    )


def test_provider_fingerprint_requires_detail_beyond_event_and_recipient():
    assert fingerprint_provider_event(
        {"event": "unsubscribed", "email": "a@example.test", "id": 12},
        normalized_event="unsubscribed", email="a@example.test",
    ) is None
    assert fingerprint_provider_event(
        {"event": "unsubscribed", "email": "a@example.test", "description": "confirmed"},
        normalized_event="unsubscribed", email="a@example.test",
    ) is None
    assert fingerprint_provider_event(
        {"event": "unsubscribed", "email": "a@example.test", "description": "confirmed", "ts_event": 123},
        normalized_event="unsubscribed", email="a@example.test",
    )


def test_provider_fingerprint_preserves_distinct_event_details_and_types():
    base = {"event": "hardBounce", "email": "a@example.test", "message-id": "msg-1", "ts_event": 123, "description": "reason A"}
    first = fingerprint_provider_event(base, normalized_event="hardbounce", email="a@example.test")
    second = fingerprint_provider_event({**base, "description": "reason B"}, normalized_event="hardbounce", email="a@example.test")
    other_type = fingerprint_provider_event(base, normalized_event="spam", email="a@example.test")
    assert first != second
    assert first != other_type


def test_migration_is_postgres_only_and_has_whole_trigger_statement(app, app_context):
    with db.engine.begin() as connection:
        with pytest.raises(ValueError, match="PostgreSQL-only"):
            apply_marketing_suppression_migration(connection)
    trigger_functions = [statement for statement in SUPPRESSION_DDL if "CREATE OR REPLACE FUNCTION" in statement]
    assert len(trigger_functions) == 1
    assert "RAISE EXCEPTION" in trigger_functions[0]
    assert ";" in trigger_functions[0]
    assert len(LEGACY_IMPORT_DDL) == 3
    assert MIGRATION_ID == "20261113_marketing_suppression"
    assert len(migration_checksum()) == 64


def test_migration_cli_is_explicit_and_rejects_sqlite_without_schema_changes(app, app_context):
    before = set(inspect(db.engine).get_table_names())
    missing_confirmation = app.test_cli_runner().invoke(
        args=["migrate-marketing-suppression-schema"]
    )
    assert missing_confirmation.exit_code != 0
    assert "verified-backup confirmations are required" in missing_confirmation.output
    assert set(inspect(db.engine).get_table_names()) == before
    result = app.test_cli_runner().invoke(args=[
        "migrate-marketing-suppression-schema",
        "--confirmation", "APPLY MARKETING SUPPRESSION MIGRATION",
        "--backup-confirmation", "I HAVE A VERIFIED RESTORABLE BACKUP",
    ])
    assert result.exit_code != 0
    assert "PostgreSQL-only" in result.output
    assert set(inspect(db.engine).get_table_names()) == before


def test_suppression_migration_rejects_incomplete_customer_email_history(app, app_context, monkeypatch):
    import app as app_module
    from app.services.email_migrations import ensure_history_table, record_migration

    monkeypatch.setattr(app_module, "database_dialect_name", lambda: "postgresql")
    with db.engine.begin() as connection:
        ensure_history_table(connection)
        record_migration(
            connection, "20261117_email_migration_history",
            state="applied", evidence="test ledger",
        )

    result = app.test_cli_runner().invoke(args=[
        "migrate-marketing-suppression-schema",
        "--confirmation", "APPLY MARKETING SUPPRESSION MIGRATION",
        "--backup-confirmation", "I HAVE A VERIFIED RESTORABLE BACKUP",
    ])
    assert result.exit_code != 0
    assert "Required customer/email migration history entries are missing" in result.output
    assert not inspect(db.engine).has_table("marketing_suppression_schema_migration")
