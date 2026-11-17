from datetime import datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from test_app import app, app_context, client
from app.extensions import db
from app.models import (
    AdminUser, EmailDeliveryAttempt, EmailDeliveryInvestigation,
    EmailDeliveryInvestigationEvent, EmailMessage, EmailWorkerRun,
)
from app.services.email_delivery_investigations import (
    InvestigationError, open_investigation, recover_confirmed_non_acceptance,
    update_investigation,
)


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


def test_operations_schema_validator_rejects_missing_required_index(app, app_context):
    from app.services.email_delivery_ops import validate_email_delivery_ops_schema
    with db.engine.begin() as connection:
        connection.execute(text("DROP INDEX ix_email_delivery_investigation_message_id"))
    with db.engine.connect() as connection:
        with pytest.raises(ValueError, match="index ix_email_delivery_investigation_message_id"):
            validate_email_delivery_ops_schema(connection)
