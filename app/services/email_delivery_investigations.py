"""Audited investigation and narrowly gated recovery of ambiguous deliveries."""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from secrets import token_urlsafe
from uuid import uuid4

from sqlalchemy import select
from flask import url_for

from app.extensions import db
from app.models import (
    EmailDeliveryAttempt, EmailDeliveryInvestigation,
    EmailDeliveryInvestigationEvent, EmailMessage,
)

ACTIVE_STATUSES = {
    "OPEN", "INVESTIGATING", "WAITING_FOR_PROVIDER_EVIDENCE",
    "CONFIRMED_ACCEPTED", "CONFIRMED_NOT_ACCEPTED", "INCONCLUSIVE",
}
TERMINAL_STATUSES = {"RESOLVED"}
ALLOWED = {
    "OPEN": {"INVESTIGATING", "WAITING_FOR_PROVIDER_EVIDENCE", "CONFIRMED_ACCEPTED", "CONFIRMED_NOT_ACCEPTED", "INCONCLUSIVE"},
    "INVESTIGATING": {"WAITING_FOR_PROVIDER_EVIDENCE", "CONFIRMED_ACCEPTED", "CONFIRMED_NOT_ACCEPTED", "INCONCLUSIVE"},
    "WAITING_FOR_PROVIDER_EVIDENCE": {"INVESTIGATING", "CONFIRMED_ACCEPTED", "CONFIRMED_NOT_ACCEPTED", "INCONCLUSIVE"},
    "INCONCLUSIVE": {"INVESTIGATING", "WAITING_FOR_PROVIDER_EVIDENCE"},
    "CONFIRMED_ACCEPTED": {"RESOLVED"},
    "CONFIRMED_NOT_ACCEPTED": {"RESOLVED"},
    "RESOLVED": set(),
}


class InvestigationError(ValueError):
    pass


def _event(investigation, admin_id, event_type, *, from_status=None, to_status=None,
           evidence_summary=None, evidence_source=None, explanation=None, related_attempt_id=None):
    db.session.add(EmailDeliveryInvestigationEvent(
        investigation_id=investigation.id, actor_admin_id=admin_id,
        event_type=event_type, from_status=from_status, to_status=to_status,
        evidence_summary=evidence_summary, evidence_source=evidence_source,
        explanation=explanation, related_attempt_id=related_attempt_id,
    ))


def open_investigation(message_id: int, admin_id: int, reason: str):
    reason = (reason or "").strip()[:255]
    if len(reason) < 8:
        raise InvestigationError("Provide an investigation reason of at least 8 characters.")
    message = db.session.execute(select(EmailMessage).where(EmailMessage.id == message_id).with_for_update()).scalar_one_or_none()
    if not message or message.error_category != "ambiguous_outcome":
        raise InvestigationError("Only messages with an ambiguous provider outcome can be investigated.")
    existing = db.session.execute(select(EmailDeliveryInvestigation).where(
        EmailDeliveryInvestigation.message_id == message_id,
        EmailDeliveryInvestigation.status.in_(ACTIVE_STATUSES),
    ).with_for_update()).scalar_one_or_none()
    if existing:
        raise InvestigationError("An open investigation already exists for this message.")
    attempt = db.session.execute(select(EmailDeliveryAttempt).where(
        EmailDeliveryAttempt.message_id == message_id,
    ).order_by(EmailDeliveryAttempt.attempt_number.desc()).limit(1)).scalar_one_or_none()
    investigation = EmailDeliveryInvestigation(
        message_id=message.id, original_attempt_id=attempt.id if attempt else None,
        provider=attempt.provider if attempt else "unknown",
        provider_message_id=message.provider_message_id or (attempt.provider_message_id if attempt else None),
        reason=reason, status="OPEN", started_at=datetime.utcnow(),
    )
    db.session.add(investigation)
    db.session.flush()
    _event(investigation, admin_id, "opened", to_status="OPEN", explanation=reason,
           related_attempt_id=investigation.original_attempt_id)
    db.session.commit()
    return investigation


def update_investigation(investigation_id: int, admin_id: int, *, status: str,
                         evidence_summary: str = "", evidence_source: str = "",
                         explanation: str = ""):
    status = (status or "").strip().upper()
    evidence_summary = (evidence_summary or "").strip()[:4000]
    evidence_source = (evidence_source or "").strip()[:255]
    explanation = (explanation or "").strip()[:4000]
    investigation = db.session.execute(select(EmailDeliveryInvestigation).where(
        EmailDeliveryInvestigation.id == investigation_id,
    ).with_for_update()).scalar_one_or_none()
    if not investigation:
        raise InvestigationError("Investigation not found.")
    old_status = investigation.status
    if status not in ALLOWED.get(old_status, set()):
        raise InvestigationError(f"Invalid investigation transition from {old_status} to {status}.")
    if status in {"CONFIRMED_ACCEPTED", "CONFIRMED_NOT_ACCEPTED", "INCONCLUSIVE"}:
        if len(evidence_summary) < 12 or len(evidence_source) < 3 or len(explanation) < 12:
            raise InvestigationError("An outcome requires evidence, its source, and an explanation.")
    if status == "RESOLVED" and not explanation:
        raise InvestigationError("A resolution explanation is required.")
    investigation.status = status
    investigation.investigator_admin_id = admin_id
    if evidence_summary:
        investigation.evidence_summary = evidence_summary
    if evidence_source:
        investigation.evidence_source = evidence_source
    investigation.resolution_outcome = {
        "CONFIRMED_ACCEPTED": "accepted",
        "CONFIRMED_NOT_ACCEPTED": "not_accepted",
        "INCONCLUSIVE": "inconclusive",
    }.get(status, investigation.resolution_outcome)
    if status == "RESOLVED":
        investigation.resolved_at = datetime.utcnow()
        investigation.resolution_explanation = explanation
    if status == "CONFIRMED_ACCEPTED":
        message = db.session.execute(select(EmailMessage).where(
            EmailMessage.id == investigation.message_id,
        ).with_for_update()).scalar_one_or_none()
        if not message or message.status != "failed" or message.error_category != "ambiguous_outcome":
            raise InvestigationError("The original message is no longer in an ambiguous state.")
        message.status = "sent"
        message.sent_at = datetime.utcnow()
        message.error_category = None
        message.failure_reason = "Provider acceptance confirmed during investigation."
        if investigation.original_attempt_id:
            attempt = db.session.get(EmailDeliveryAttempt, investigation.original_attempt_id)
            if attempt:
                attempt.status = "accepted"
                attempt.completed_at = attempt.completed_at or datetime.utcnow()
                attempt.provider_message_id = attempt.provider_message_id or investigation.provider_message_id
    _event(investigation, admin_id, "status_changed", from_status=old_status, to_status=status,
           evidence_summary=evidence_summary or None, evidence_source=evidence_source or None,
           explanation=explanation or None, related_attempt_id=investigation.original_attempt_id)
    db.session.commit()
    return investigation


def recover_confirmed_non_acceptance(investigation_id: int, admin_id: int, *, authorization: str,
                                     replacement_subject: str = "", replacement_text: str = ""):
    """Queue an explicitly composed replacement after verified non-acceptance.

    The original ambiguous payload is intentionally erased by retention. This
    operation never replays it: an administrator composes plain text, which is
    escaped into the branded template, and the normal dispatcher rechecks
    marketing eligibility at its final account-locked claim.
    """
    if authorization != "RECOVER AFTER VERIFIED NON-ACCEPTANCE":
        raise InvestigationError("Explicit recovery confirmation is required.")
    replacement_subject = " ".join((replacement_subject or "").split())[:200]
    replacement_text = (replacement_text or "").strip()
    if not replacement_subject or len(replacement_text) < 12 or len(replacement_text) > 8000:
        raise InvestigationError("Provide a replacement subject and 12 to 8000 characters of reviewed plain-text content.")
    investigation = db.session.execute(select(EmailDeliveryInvestigation).where(
        EmailDeliveryInvestigation.id == investigation_id,
    ).with_for_update()).scalar_one_or_none()
    if not investigation or investigation.status != "CONFIRMED_NOT_ACCEPTED":
        raise InvestigationError("Recovery requires confirmed provider non-acceptance.")
    if investigation.recovered_message_id:
        raise InvestigationError("A recovery message already exists for this investigation.")
    original = db.session.execute(select(EmailMessage).where(
        EmailMessage.id == investigation.message_id,
    ).with_for_update()).scalar_one_or_none()
    if not original or original.status != "failed" or original.error_category != "ambiguous_outcome":
        raise InvestigationError("The original message is no longer in an ambiguous state.")
    unsubscribe_url = ""
    if original.purpose == "marketing":
        from app.models import CustomerAccount, CustomerToken
        from app.admin.email_studio import _public_url
        from app.services.marketing_eligibility import evaluate_marketing_eligibility
        from app.services.marketing_suppressions import marketing_account_lock_statement
        connection = db.session.connection()
        account = connection.execute(marketing_account_lock_statement(original.account_id)).mappings().first() if original.account_id else None
        decision = evaluate_marketing_eligibility(
            account_id=original.account_id, connection=connection,
            respect_frequency=True, frequency_override=False,
            exclude_message_id=original.id,
        ) if account else None
        if not decision or not decision.eligible:
            raise InvestigationError("Current marketing eligibility check blocked recovery.")
        raw_token = token_urlsafe(32)
        db.session.add(CustomerToken(
            account_id=original.account_id, purpose="unsubscribe",
            token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
            expires_at=datetime.utcnow() + timedelta(days=3650),
        ))
        unsubscribe_url = _public_url(url_for("customer.unsubscribe", token=raw_token))
    from app.services.customer_mail import render_branded_email
    heading = replacement_subject
    paragraphs = [line.strip() for line in replacement_text.splitlines() if line.strip()]
    html_body, text_body = render_branded_email(
        heading, paragraphs, unsubscribe_url=unsubscribe_url,
        kind=original.kind,
    )
    recovered = EmailMessage(
        recipient=original.recipient, account_id=original.account_id,
        order_id=original.order_id, campaign_id=original.campaign_id,
        template_id=original.template_id, kind=original.kind,
        subject=replacement_subject,
        idempotency_key=f"recovery:{investigation.id}:{uuid4().hex}"[:160],
        status="queued", purpose=original.purpose, frequency_override=False,
        html_body=html_body, text_body=text_body,
    )
    db.session.add(recovered)
    db.session.flush()
    investigation.recovered_message_id = recovered.id
    investigation.investigator_admin_id = admin_id
    investigation.status = "RESOLVED"
    investigation.resolved_at = datetime.utcnow()
    investigation.resolution_outcome = "not_accepted"
    investigation.resolution_explanation = "A new outbox message was authorized after provider non-acceptance was verified."
    _event(investigation, admin_id, "recovery_authorized", from_status="CONFIRMED_NOT_ACCEPTED",
           to_status="RESOLVED", explanation=investigation.resolution_explanation,
           related_attempt_id=investigation.original_attempt_id)
    db.session.commit()
    return recovered
