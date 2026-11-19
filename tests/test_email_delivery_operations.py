from datetime import datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from test_app import app, app_context, client
from app.extensions import db
from app.models import (
    AdminUser, CustomerAccount, CustomerToken, EmailDeliveryAttempt,
    EmailDeliveryInvestigation, EmailDeliveryInvestigationEvent, EmailMessage,
    EmailWorkerRun,
)
from app.services.email_delivery_investigations import (
    InvestigationError, open_investigation, recover_confirmed_non_acceptance,
    update_investigation,
)
from app.services.marketing_suppressions import record_suppression
from app.services.sql_schema_utils import normalize_sql_definition


@pytest.fixture(autouse=True)
def _install_delivery_ops_schema(app, app_context):
    from app.services.email_delivery_ops import apply_email_delivery_ops_migration
    from app.services.email_outbox_schema import apply_email_outbox_migration
    with db.engine.begin() as connection:
        apply_email_outbox_migration(connection, db.engine.dialect.name)
        apply_email_delivery_ops_migration(connection, db.engine.dialect.name)


def _ambiguous_message(key="ambiguous-test", *, payload=False, purpose="marketing", kind="marketing_campaign"):
    row = EmailMessage(
        recipient="recipient@example.test", kind=kind, purpose=purpose,
        subject="Campaign", idempotency_key=key, status="failed",
        error_category="ambiguous_outcome", failure_reason="Provider outcome unknown.",
        html_body="<p>Reviewed content</p>" if payload else None,
        text_body="Reviewed content" if payload else None,
    )
    db.session.add(row)
    db.session.flush()
    attempt = EmailDeliveryAttempt(
        message_id=row.id, attempt_number=1, attempt_key="attempt-" + key,
        provider="brevo", status="ambiguous", started_at=datetime.utcnow(),
    )
    db.session.add(attempt)
    db.session.commit()
    return row, attempt


def _queued_worker_message(key, kind, purpose, *, status="queued", recipient="recipient@example.test",
                           account_id=None, created_at=None, next_attempt_at=None, retry_count=0):
    row = EmailMessage(
        recipient=recipient, account_id=account_id, kind=kind, purpose=purpose,
        subject="Worker test", idempotency_key=key, status=status,
        html_body="<p>Worker test</p>", text_body="Worker test",
        created_at=created_at or datetime.utcnow(),
        next_attempt_at=next_attempt_at, retry_count=retry_count,
        error_category="retryable_provider" if status == "failed" else None,
    )
    db.session.add(row)
    db.session.commit()
    return row


def test_investigation_requires_ambiguous_message_and_preserves_append_only_history(app, app_context):
    admin = AdminUser.query.filter_by(username="owner").one()
    message, attempt = _ambiguous_message("investigation-open")
    investigation = open_investigation(message.id, admin.id, "Confirm provider handoff outcome")
    with pytest.raises(InvestigationError, match="already exists"):
        open_investigation(message.id, admin.id, "Duplicate investigation request")
    with pytest.raises(InvestigationError, match="Invalid investigation transition"):
        update_investigation(investigation.id, admin.id, status="RESOLVED", explanation="Skip evidence")
    investigation = update_investigation(
        investigation.id, admin.id, status="CONFIRMED_NOT_ACCEPTED",
        evidence_summary="Provider search returned no matching accepted message.",
        evidence_source="Brevo transaction log reference 12345",
        explanation="Checked matching recipient and attempt time; no accepted request exists.",
    )
    assert investigation.status == "CONFIRMED_NOT_ACCEPTED"
    events = EmailDeliveryInvestigationEvent.query.filter_by(investigation_id=investigation.id).all()
    assert len(events) == 2
    assert events[-1].actor_admin_id == admin.id
    assert events[-1].related_attempt_id == attempt.id
    events[0].explanation = "tamper"
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_recovery_requires_explicit_phrase_and_creates_reviewed_replacement(app, app_context, monkeypatch):
    admin = AdminUser.query.filter_by(username="owner").one()
    message, original_attempt = _ambiguous_message(
        "investigation-no-payload", purpose="transactional", kind="order_received",
    )
    investigation = open_investigation(message.id, admin.id, "Investigate provider timeout")
    update_investigation(
        investigation.id, admin.id, status="CONFIRMED_NOT_ACCEPTED",
        evidence_summary="Provider lookup confirms the request was not accepted.",
        evidence_source="Brevo transaction log reference 67890",
        explanation="Provider support confirmed no message was accepted for this attempt.",
    )
    with pytest.raises(InvestigationError, match="confirmation"):
        recover_confirmed_non_acceptance(
            investigation.id, admin.id, authorization="yes",
            replacement_subject="Reviewed replacement", replacement_text="A reviewed replacement message.",
        )
    recovered = recover_confirmed_non_acceptance(
        investigation.id, admin.id,
        authorization="RECOVER AFTER VERIFIED NON-ACCEPTANCE",
        replacement_subject="Reviewed replacement", replacement_text="A reviewed replacement message.",
    )
    assert message.status == "failed"
    assert message.error_category == "ambiguous_outcome"
    assert message.html_body is None and message.text_body is None
    assert "A reviewed replacement message." in recovered.text_body
    assert investigation.recovered_message_id == recovered.id
    from app.services import customer_mail
    monkeypatch.setattr(customer_mail, "mail_configuration_status", lambda: (True, "configured"))
    monkeypatch.setattr(customer_mail, "_send_brevo", lambda *args: "recovery-provider-id")
    result = customer_mail.process_email_queue(limit=1)
    assert result["sent"] == 1
    recovery_attempt = EmailDeliveryAttempt.query.filter_by(message_id=recovered.id).one()
    assert recovery_attempt.recovery_of_attempt_id == original_attempt.id


def test_confirmed_provider_acceptance_reconciles_message_without_resend(app, app_context):
    admin = AdminUser.query.filter_by(username="owner").one()
    message, attempt = _ambiguous_message(
        "investigation-accepted", purpose="transactional", kind="order_received",
    )
    investigation = open_investigation(message.id, admin.id, "Verify provider accepted the message")
    update_investigation(
        investigation.id, admin.id, status="CONFIRMED_ACCEPTED",
        evidence_summary="Provider logs show the accepted message and matching identifier.",
        evidence_source="Brevo transaction log reference accepted-1",
        explanation="The provider confirms acceptance for the original attempt.",
    )
    db.session.refresh(message)
    db.session.refresh(attempt)
    assert message.status == "sent"
    assert message.error_category is None
    assert attempt.status == "accepted"
    assert investigation.status == "CONFIRMED_ACCEPTED"


def test_worker_heartbeat_records_liveness_and_marks_stale_workers(app, app_context):
    from app.services.email_worker import EmailWorkerHeartbeat, worker_health
    worker = EmailWorkerHeartbeat(interval_seconds=2, instance_id="test-worker-instance")
    worker.start()
    worker.mark_attempt(1)
    worker.mark_success()
    worker.stop()
    run = EmailWorkerRun.query.filter_by(instance_id="test-worker-instance").one()
    assert run.status == "STOPPED"
    assert run.last_attempt_at is not None and run.last_success_at is not None
    old = EmailWorkerRun(
        instance_id="stale-worker-instance", worker_type="queue", status="RUNNING",
        started_at=datetime.utcnow() - timedelta(hours=1),
        last_heartbeat_at=datetime.utcnow() - timedelta(hours=1),
    )
    db.session.add(old)
    db.session.commit()
    health = worker_health(timeout_seconds=30)
    assert health["live_count"] == 0
    assert health["stale_count"] == 1
    assert any(item["status"] == "STALE" for item in health["workers"])


def test_delivery_claim_persists_attempt_and_stops_worker_history(app, app_context, monkeypatch):
    from app.services import customer_mail
    row = customer_mail.queue_email(
        "transactional@example.test", "Order update", "order_received", "attempt-ledger-test",
        html_body="<p>Order update</p>", text_body="Order update",
    )
    monkeypatch.setattr(customer_mail, "mail_configuration_status", lambda: (True, "configured"))
    monkeypatch.setattr(customer_mail, "_send_brevo", lambda *args: "provider-message-123")
    result = customer_mail.process_email_queue(limit=1)
    attempt = EmailDeliveryAttempt.query.filter_by(message_id=row.id).one()
    assert result["sent"] == 1
    assert attempt.status == "accepted"
    assert attempt.completed_at is not None
    assert EmailWorkerRun.query.filter_by(status="STOPPED").count() == 1


def test_transactional_worker_dispatches_only_account_and_order_allowlist(app, app_context, monkeypatch):
    from app.services import customer_mail

    permitted = [
        _queued_worker_message("isolated-verify", "verify_code", "transactional"),
        _queued_worker_message("isolated-order", "order_received", "transactional"),
    ]
    excluded = [
        _queued_worker_message("isolated-marketing", "marketing_campaign", "marketing"),
        _queued_worker_message("isolated-admin-test", "admin_test", "test"),
        _queued_worker_message("isolated-restock", "restock_notification", "transactional"),
        _queued_worker_message("isolated-review", "review_request", "transactional"),
        _queued_worker_message("isolated-marketing-confirm", "marketing_confirm", "transactional"),
        _queued_worker_message("isolated-wrong-purpose", "order_update", "marketing"),
    ]
    sent_ids = []
    monkeypatch.setattr(customer_mail, "mail_configuration_status", lambda: (True, "configured"))
    monkeypatch.setattr(customer_mail, "_send_brevo", lambda *_args: sent_ids.append(1) or "isolated-provider-id")

    result = customer_mail.process_transactional_email_queue(limit=50)

    assert result["sent"] == 2
    assert len(sent_ids) == 2
    assert all(EmailMessage.query.get(row.id).status == "sent" for row in permitted)
    assert all(EmailMessage.query.get(row.id).status == row.status == "queued" for row in excluded)
    assert EmailWorkerRun.query.filter_by(worker_type="transactional_queue", status="STOPPED").count() == 1


def test_transactional_worker_retries_only_due_allowlisted_failures(app, app_context, monkeypatch):
    from app.services import customer_mail

    now = datetime.utcnow()
    due = _queued_worker_message(
        "isolated-retry-order", "order_received", "transactional",
        status="failed", next_attempt_at=now - timedelta(seconds=1), retry_count=1,
    )
    marketing = _queued_worker_message(
        "isolated-retry-marketing", "marketing_campaign", "marketing",
        status="failed", next_attempt_at=now - timedelta(seconds=1), retry_count=1,
    )
    excluded_transactional = _queued_worker_message(
        "isolated-retry-review", "review_request", "transactional",
        status="failed", next_attempt_at=now - timedelta(seconds=1), retry_count=1,
    )
    monkeypatch.setattr(customer_mail, "mail_configuration_status", lambda: (True, "configured"))
    monkeypatch.setattr(customer_mail, "_send_brevo", lambda *_args: "isolated-retry-provider-id")

    result = customer_mail.process_transactional_email_queue(limit=50, now=now)

    db.session.refresh(due)
    db.session.refresh(marketing)
    db.session.refresh(excluded_transactional)
    assert result["sent"] == 1
    assert due.status == "sent"
    assert marketing.status == "failed"
    assert excluded_transactional.status == "failed"


def test_transactional_worker_expires_old_codes_without_touching_excluded_kinds(app, app_context, monkeypatch):
    from app.services import customer_mail

    now = datetime.utcnow()
    expired_code = _queued_worker_message(
        "isolated-expired-code", "verify_code", "transactional",
        created_at=now - timedelta(minutes=11),
    )
    expired_retry = _queued_worker_message(
        "isolated-expired-reset", "reset_password", "transactional",
        status="failed", created_at=now - timedelta(minutes=31),
        next_attempt_at=now - timedelta(seconds=1), retry_count=1,
    )
    excluded_expired_confirmation = _queued_worker_message(
        "isolated-expired-marketing-confirm", "marketing_confirm", "transactional",
        created_at=now - timedelta(days=2),
    )
    send_attempts = []
    monkeypatch.setattr(customer_mail, "mail_configuration_status", lambda: (True, "configured"))
    monkeypatch.setattr(customer_mail, "_send_brevo", lambda *_args: send_attempts.append(1) or "unused-id")

    result = customer_mail.process_transactional_email_queue(limit=50, now=now)

    db.session.refresh(expired_code)
    db.session.refresh(expired_retry)
    db.session.refresh(excluded_expired_confirmation)
    assert result["sent"] == 0
    assert expired_code.status == "failed"
    assert expired_code.error_category == "expired_auth_payload"
    assert expired_code.html_body is None and expired_code.text_body is None
    assert expired_retry.status == "failed"
    assert expired_retry.error_category == "expired_auth_payload"
    assert expired_retry.next_attempt_at is None
    assert expired_retry.html_body is None and expired_retry.text_body is None
    assert excluded_expired_confirmation.status == "queued"
    assert not send_attempts


def test_admin_test_claim_is_atomic_and_prevents_duplicate_provider_attempts(app, app_context):
    from app.services import customer_mail

    row = _queued_worker_message("single-admin-test-claim-race", "admin_test", "test",
                                 recipient="admin@example.test")
    first_claimed = customer_mail._claim_admin_test_dispatch(row, "test-claim-first", "admin@example.test")
    second_claimed = customer_mail._claim_admin_test_dispatch(row, "test-claim-second", "admin@example.test")

    assert first_claimed is True
    assert second_claimed is False
    assert EmailDeliveryAttempt.query.filter_by(message_id=row.id).count() == 1


def test_single_admin_test_dispatch_is_recipient_scoped_and_not_repeatable(app, app_context, monkeypatch):
    from app.services import customer_mail

    app.config["MAIL_TEST_ADDRESS"] = "admin@example.test"
    row = _queued_worker_message(
        "single-admin-test", "admin_test", "test", recipient="ADMIN@example.test",
    )
    sent = []
    monkeypatch.setattr(customer_mail, "mail_configuration_status", lambda: (True, "configured"))
    monkeypatch.setattr(customer_mail, "_send_brevo", lambda *_args: sent.append(1) or "admin-test-provider-id")

    result = customer_mail.process_admin_test_email(row.id)

    db.session.refresh(row)
    assert result["claimed"] == 1
    assert result["sent"] == 1
    assert row.status == "sent"
    assert len(sent) == 1
    assert EmailDeliveryAttempt.query.filter_by(message_id=row.id).count() == 1
    assert EmailWorkerRun.query.filter_by(worker_type="admin_test", status="STOPPED").count() == 1
    with pytest.raises(ValueError, match="queued, unlinked admin test"):
        customer_mail.process_admin_test_email(row.id)
    assert len(sent) == 1
    assert CustomerAccount.query.count() == 0


def test_general_worker_leaves_admin_tests_for_single_message_dispatch(app, app_context, monkeypatch):
    from app.services import customer_mail

    app.config["MAIL_TEST_ADDRESS"] = "admin@example.test"
    row = _queued_worker_message(
        "general-worker-admin-test", "admin_test", "test",
        recipient="admin@example.test",
    )
    malformed_test = _queued_worker_message(
        "general-worker-malformed-test", "admin_test", "test",
        recipient="admin@example.test",
    )
    malformed_test.html_body = None
    malformed_test.text_body = ""
    db.session.commit()
    sends = []
    monkeypatch.setattr(customer_mail, "mail_configuration_status", lambda: (True, "configured"))
    monkeypatch.setattr(customer_mail, "_send_brevo", lambda *_args: sends.append(1) or "must-not-send")

    assert customer_mail.deliver_email(row) is False
    result = customer_mail.process_email_queue(limit=50)

    db.session.refresh(row)
    db.session.refresh(malformed_test)
    assert result["claimed"] == 0
    assert row.status == "queued"
    assert malformed_test.status == "queued"
    assert not sends
    assert EmailDeliveryAttempt.query.filter_by(message_id=row.id).count() == 0


def test_single_admin_test_dispatch_rejects_customer_link_and_wrong_recipient(app, app_context, monkeypatch):
    from app.services import customer_mail

    app.config["MAIL_TEST_ADDRESS"] = "admin@example.test"
    account = CustomerAccount(email="linked-customer@example.test", display_name="Linked")
    db.session.add(account)
    db.session.commit()
    linked = _queued_worker_message(
        "single-admin-test-linked", "admin_test", "test",
        recipient="admin@example.test", account_id=account.id,
    )
    wrong_recipient = _queued_worker_message(
        "single-admin-test-wrong-recipient", "admin_test", "test",
        recipient="customer@example.test",
    )
    sends = []
    monkeypatch.setattr(customer_mail, "mail_configuration_status", lambda: (True, "configured"))
    monkeypatch.setattr(customer_mail, "_send_brevo", lambda *_args: sends.append(1) or "must-not-send")

    for row in (linked, wrong_recipient):
        with pytest.raises(ValueError, match="queued, unlinked admin test"):
            customer_mail.process_admin_test_email(row.id)
    assert not sends
    assert linked.status == wrong_recipient.status == "queued"


def test_single_admin_test_dispatch_rejects_test_address_owned_by_customer(app, app_context, monkeypatch):
    from app.services import customer_mail

    app.config["MAIL_TEST_ADDRESS"] = "customer@example.test"
    account = CustomerAccount(email="customer@example.test", display_name="Owner")
    db.session.add(account)
    db.session.commit()
    row = _queued_worker_message(
        "single-admin-test-customer-address", "admin_test", "test",
        recipient="customer@example.test",
    )
    sends = []
    monkeypatch.setattr(customer_mail, "mail_configuration_status", lambda: (True, "configured"))
    monkeypatch.setattr(customer_mail, "_send_brevo", lambda *_args: sends.append(1) or "must-not-send")

    with pytest.raises(ValueError, match="must not match a customer account"):
        customer_mail.process_admin_test_email(row.id)
    assert not sends
    assert row.status == "queued"


def test_isolated_email_cli_commands_require_exact_confirmation_before_database_work(app, app_context):
    runner = app.test_cli_runner()

    transactional = runner.invoke(
        args=["process-transactional-email-queue", "--confirmation", "run transactional email queue"],
    )
    test_dispatch = runner.invoke(
        args=["dispatch-admin-test-email", "--message-id", "1", "--confirmation", "SEND TEST"],
    )

    assert transactional.exit_code != 0
    assert "no messages were dispatched" in transactional.output
    assert test_dispatch.exit_code != 0
    assert "no message was dispatched" in test_dispatch.output
    assert EmailWorkerRun.query.count() == 0


def test_transactional_cli_surfaces_missing_legacy_review_schema(app, app_context, monkeypatch):
    from app.services import customer_mail

    monkeypatch.setattr(
        customer_mail, "process_transactional_email_queue",
        lambda **_kwargs: {"legacy_review_schema_unavailable": 1},
    )
    result = app.test_cli_runner().invoke(args=[
        "process-transactional-email-queue",
        "--confirmation", "RUN TRANSACTIONAL EMAIL QUEUE",
    ])

    assert result.exit_code != 0
    assert "Legacy email payload review schema is unavailable" in result.output


def test_investigation_routes_require_admin_and_csrf(client, app, app_context):
    message, _ = _ambiguous_message("investigation-route-access")
    response = client.post(
        f"/admin/email/messages/{message.id}/investigate",
        data={"reason": "Check provider outcome"},
    )
    assert response.status_code in {302, 401}
    assert EmailDeliveryInvestigation.query.count() == 0

    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    app.config["WTF_CSRF_ENABLED"] = True
    rejected = client.post(
        f"/admin/email/messages/{message.id}/investigate",
        data={"reason": "Check provider outcome"},
    )
    assert rejected.status_code == 400
    assert EmailDeliveryInvestigation.query.count() == 0


def test_operations_schema_validator_checks_append_only_triggers(app, app_context):
    from app.services.email_delivery_ops import validate_email_delivery_ops_schema
    with db.engine.connect() as connection:
        assert validate_email_delivery_ops_schema(connection)
    triggers = db.session.execute(text(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_email_investigation_event_%'"
    )).all()
    assert len(triggers) == 2
    assert all("append-only" in row.sql for row in triggers)


def test_postgresql_check_normalization_accepts_equivalent_any_array_and_rejects_changed_values():
    expected = (
        "status IN ('OPEN','INVESTIGATING','WAITING_FOR_PROVIDER_EVIDENCE',"
        "'CONFIRMED_ACCEPTED','CONFIRMED_NOT_ACCEPTED','INCONCLUSIVE','RESOLVED')"
    )
    deparsed = (
        "(((status)::character varying = ANY ((ARRAY['OPEN'::character varying, "
        "'INVESTIGATING'::character varying, 'WAITING_FOR_PROVIDER_EVIDENCE'::character varying, "
        "'CONFIRMED_ACCEPTED'::character varying, 'CONFIRMED_NOT_ACCEPTED'::character varying, "
        "'INCONCLUSIVE'::character varying, 'RESOLVED'::character varying])::character varying[])))"
    )

    assert normalize_sql_definition(deparsed) == normalize_sql_definition(expected)
    assert normalize_sql_definition(deparsed) != normalize_sql_definition(
        expected.replace("'RESOLVED'", "'CLOSED'")
    )


def test_postgresql_check_normalization_accepts_parenthesized_null_or_any_and_rejects_changes():
    expected = "resolution_outcome IS NULL OR resolution_outcome IN ('accepted','not_accepted','inconclusive')"
    deparsed = (
        "((resolution_outcome IS NULL) OR ((resolution_outcome)::character varying = "
        "ANY ((ARRAY['accepted'::character varying, 'not_accepted'::character varying, "
        "'inconclusive'::character varying])::character varying[])))"
    )

    assert normalize_sql_definition(deparsed) == normalize_sql_definition(expected)
    assert normalize_sql_definition(deparsed) != normalize_sql_definition(
        expected.replace("'inconclusive'", "'unknown'")
    )


def test_postgresql_check_normalization_preserves_actor_boolean_grouping():
    expected = (
        "(actor_type = 'admin' AND actor_admin_id IS NOT NULL) OR "
        "(actor_type = 'system' AND actor_admin_id IS NULL)"
    )
    deparsed_forms = (
        "actor_type::text = 'admin'::text AND actor_admin_id IS NOT NULL OR "
        "actor_type::text = 'system'::text AND actor_admin_id IS NULL",
        "(((actor_type)::character varying = 'admin'::character varying) "
        "AND (actor_admin_id IS NOT NULL)) OR "
        "(((actor_type)::character varying = 'system'::character varying) "
        "AND (actor_admin_id IS NULL))"
    )

    for deparsed in deparsed_forms:
        assert normalize_sql_definition(deparsed) == normalize_sql_definition(expected)
        assert normalize_sql_definition(deparsed) != normalize_sql_definition(
            expected.replace("'system'", "'provider'")
        )
    assert normalize_sql_definition("NOT (a = 'x' AND b = 'y')") != normalize_sql_definition(
        "NOT a = 'x' AND b = 'y'"
    )


def test_operations_schema_validator_rejects_missing_required_index(app, app_context):
    from app.services.email_delivery_ops import validate_email_delivery_ops_schema
    with db.engine.begin() as connection:
        connection.execute(text("DROP INDEX ix_email_delivery_investigation_message_id"))
    with db.engine.connect() as connection:
        with pytest.raises(ValueError, match="index ix_email_delivery_investigation_message_id"):
            validate_email_delivery_ops_schema(connection)


def _confirmed_marketing_recovery(key):
    admin = AdminUser.query.filter_by(username="owner").one()
    now = datetime.utcnow()
    account = CustomerAccount(
        email=f"{key}@example.test",
        display_name="Marketing Recovery Test",
        email_verified_at=now,
        marketing_status="subscribed",
        marketing_consent_at=now,
    )
    db.session.add(account)
    db.session.flush()
    message = EmailMessage(
        recipient=account.email,
        account_id=account.id,
        kind="marketing_campaign",
        purpose="marketing",
        subject="Original campaign",
        idempotency_key=f"marketing-recovery-{key}",
        status="failed",
        error_category="ambiguous_outcome",
        failure_reason="Provider outcome unknown.",
    )
    db.session.add(message)
    db.session.flush()
    attempt = EmailDeliveryAttempt(
        message_id=message.id,
        attempt_number=1,
        attempt_key=f"marketing-recovery-attempt-{key}",
        provider="brevo",
        status="ambiguous",
        started_at=now,
    )
    db.session.add(attempt)
    db.session.commit()
    investigation = open_investigation(
        message.id, admin.id, "Verify marketing provider outcome",
    )
    investigation = update_investigation(
        investigation.id, admin.id,
        status="CONFIRMED_NOT_ACCEPTED",
        evidence_summary="Provider lookup confirms the message was not accepted.",
        evidence_source="Brevo transaction log review",
        explanation="The provider confirms no accepted message for this attempt.",
    )
    return account, message, attempt, admin, investigation


def _recover_marketing(investigation, admin):
    return recover_confirmed_non_acceptance(
        investigation.id, admin.id,
        authorization="RECOVER AFTER VERIFIED NON-ACCEPTANCE",
        replacement_subject="Reviewed marketing update",
        replacement_text="This is the reviewed replacement marketing message.",
    )


def test_marketing_recovery_rejects_unsubscribed_account(app, app_context):
    account, message, _attempt, admin, investigation = _confirmed_marketing_recovery(
        "unsubscribed-recovery",
    )
    account.marketing_status = "unsubscribed"
    db.session.commit()
    original_count = EmailMessage.query.filter_by(account_id=account.id).count()

    with pytest.raises(InvestigationError, match="eligibility check blocked recovery"):
        _recover_marketing(investigation, admin)

    assert EmailMessage.query.filter_by(account_id=account.id).count() == original_count
    assert investigation.recovered_message_id is None
    assert EmailMessage.query.filter_by(id=message.id).one()


def test_marketing_recovery_rejects_active_suppression(app, app_context):
    account, _message, _attempt, admin, investigation = _confirmed_marketing_recovery(
        "suppressed-recovery",
    )
    record_suppression(
        account.id,
        source="provider",
        reason_code="hard_bounce",
        reason_text="Provider reported a permanent delivery failure.",
        actor_type="provider",
        provider_name="brevo",
    )
    db.session.commit()
    original_count = EmailMessage.query.filter_by(account_id=account.id).count()

    with pytest.raises(InvestigationError, match="eligibility check blocked recovery"):
        _recover_marketing(investigation, admin)

    assert EmailMessage.query.filter_by(account_id=account.id).count() == original_count
    assert investigation.recovered_message_id is None


def test_marketing_recovery_rejects_recent_marketing_delivery(app, app_context):
    account, _message, _attempt, admin, investigation = _confirmed_marketing_recovery(
        "frequency-limited-recovery",
    )
    recent_message = EmailMessage(
        recipient=account.email,
        account_id=account.id,
        kind="marketing_campaign",
        purpose="marketing",
        subject="Recent campaign",
        idempotency_key="recent-marketing-delivery-frequency-limited",
        status="sent",
        sent_at=datetime.utcnow(),
    )
    db.session.add(recent_message)
    db.session.commit()
    original_count = EmailMessage.query.filter_by(account_id=account.id).count()

    with pytest.raises(InvestigationError, match="eligibility check blocked recovery"):
        _recover_marketing(investigation, admin)

    assert EmailMessage.query.filter_by(account_id=account.id).count() == original_count
    assert investigation.recovered_message_id is None


def test_eligible_marketing_recovery_includes_unsubscribe_link(app, app_context):
    app.config["PUBLIC_BASE_URL"] = "https://fitystore.top"
    account, _message, _attempt, admin, investigation = _confirmed_marketing_recovery(
        "eligible-recovery",
    )

    recovered = _recover_marketing(investigation, admin)

    assert recovered.status == "queued"
    assert recovered.account_id == account.id
    assert recovered.html_body
    assert 'href="https://fitystore.top/account/unsubscribe/' in recovered.html_body
    assert "https://fitystore.top/account/unsubscribe/" in recovered.text_body
    token = CustomerToken.query.filter_by(
        account_id=account.id, purpose="unsubscribe",
    ).one()
    assert token.token_hash
    assert len(token.token_hash) == 64
    assert investigation.recovered_message_id == recovered.id
