from __future__ import annotations

from collections.abc import Callable
import json
import logging
import re
import smtplib
import ssl
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage as MimeMessage
from email.utils import formataddr, parseaddr
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import current_app, render_template, url_for
from sqlalchemy import func, or_, select, update
from typing import TYPE_CHECKING
from uuid import uuid4

from app.extensions import db
from app.services.email_purpose import (
    TEST_EMAIL_KINDS,
    TRANSACTIONAL_QUEUE_EMAIL_KINDS,
    UnknownEmailKind,
    email_purpose_for_kind,
)
from app.models import (CustomerAccount, CustomerCart, EmailDeliveryAttempt,
                        EmailDeliveryInvestigation, EmailMessage, MarketingCampaignRun,
                        RestockAlert)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.services.email_worker import EmailWorkerHeartbeat


def _email_public_base_url() -> str:
    candidate = (current_app.config.get("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    parsed = urlsplit(candidate)
    if (parsed.scheme == "https" and parsed.netloc and not parsed.username and not parsed.password
            and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment):
        return candidate
    return "https://fitystore.top"

def mail_configuration_status() -> tuple[bool, str]:
    cfg = current_app.config
    provider = (cfg.get("MAIL_PROVIDER") or "").strip().lower()
    if provider not in {"smtp", "resend", "brevo"}:
        return False, "Email sending is not configured (set MAIL_PROVIDER to smtp, resend, or brevo)."
    if not cfg.get("MAIL_FROM_VERIFIED"):
        return False, "Sender address is not marked verified with the email provider."
    sender = (cfg.get("MAIL_FROM_ADDRESS") or "").strip()
    if not sender or parseaddr(sender)[1] != sender or "@" not in sender:
        return False, "A valid provider-verified MAIL_FROM_ADDRESS is required."
    if provider == "resend":
        if not (cfg.get("RESEND_API_KEY") or "").strip():
            return False, "RESEND_API_KEY is required when MAIL_PROVIDER=resend."
        return True, "Resend HTTPS API sender is configured."
    if provider == "brevo":
        if not (cfg.get("BREVO_API_KEY") or "").strip():
            return False, "BREVO_API_KEY is required when MAIL_PROVIDER=brevo."
        return True, "Brevo HTTPS API sender is configured."
    if not (cfg.get("MAIL_SERVER") or "").strip():
        return False, "MAIL_SERVER is required."
    try:
        port = int(cfg.get("MAIL_PORT", 587))
        if not 1 <= port <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        return False, "MAIL_PORT must be a number from 1 to 65535."
    use_tls = bool(cfg.get("MAIL_USE_TLS", True))
    use_ssl = bool(cfg.get("MAIL_USE_SSL", False))
    if use_tls and use_ssl:
        return False, "Choose either STARTTLS or implicit TLS, not both."
    if not use_tls and not use_ssl:
        return False, "SMTP must use TLS (MAIL_USE_TLS=1) or implicit TLS (MAIL_USE_SSL=1)."
    if bool(cfg.get("MAIL_USERNAME")) != bool(cfg.get("MAIL_PASSWORD")):
        return False, "Provide both SMTP username and password, or leave both blank."
    return True, "SMTP sender is configured with encrypted transport."


def _send_smtp(recipient: str, subject: str, html_body: str, text_body: str) -> None:
    cfg = current_app.config
    message = MimeMessage()
    message["Subject"] = (subject or "FityStores update").replace("\r", " ").replace("\n", " ")
    message["From"] = formataddr((cfg.get("MAIL_FROM_NAME") or "FityStores", cfg["MAIL_FROM_ADDRESS"]))
    message["To"] = recipient
    if cfg.get("MAIL_REPLY_TO"):
        message["Reply-To"] = cfg["MAIL_REPLY_TO"]
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")
    timeout = 12
    if cfg.get("MAIL_USE_SSL", False):
        with smtplib.SMTP_SSL(
            cfg["MAIL_SERVER"], int(cfg.get("MAIL_PORT", 465)), timeout=timeout,
            context=ssl.create_default_context(),
        ) as smtp:
            if cfg.get("MAIL_USERNAME"):
                smtp.login(cfg["MAIL_USERNAME"], cfg["MAIL_PASSWORD"])
            smtp.send_message(message)
        return
    with smtplib.SMTP(cfg["MAIL_SERVER"], int(cfg.get("MAIL_PORT", 587)), timeout=timeout) as smtp:
        smtp.ehlo()
        if cfg.get("MAIL_USE_TLS", True):
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
        if cfg.get("MAIL_USERNAME"):
            smtp.login(cfg["MAIL_USERNAME"], cfg["MAIL_PASSWORD"])
        smtp.send_message(message)



def _send_resend(recipient: str, subject: str, html_body: str, text_body: str) -> str | None:
    cfg = current_app.config
    payload = {
        "from": formataddr((cfg.get("MAIL_FROM_NAME") or "FityStores", cfg["MAIL_FROM_ADDRESS"])),
        "to": [recipient],
        "subject": (subject or "FityStores update").replace("\r", " ").replace("\n", " "),
        "html": html_body,
        "text": text_body,
    }
    if cfg.get("MAIL_REPLY_TO"):
        payload["reply_to"] = cfg["MAIL_REPLY_TO"]
    request = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {cfg['RESEND_API_KEY'].strip()}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError(f"Resend API returned HTTP {response.status}.")
        try:
            result = json.loads(response.read().decode("utf-8"))
        except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        message_id = result.get("id") if isinstance(result, dict) else None
        return str(message_id)[:255] if message_id else None


def _send_brevo(recipient: str, subject: str, html_body: str, text_body: str) -> str | None:
    cfg = current_app.config
    payload = {
        "sender": {
            "name": cfg.get("MAIL_FROM_NAME") or "FityStores",
            "email": cfg["MAIL_FROM_ADDRESS"],
        },
        "to": [{"email": recipient}],
        "subject": (subject or "FityStores update").replace("\r", " ").replace("\n", " "),
        "htmlContent": html_body,
        "textContent": text_body,
    }
    if cfg.get("MAIL_REPLY_TO"):
        payload["replyTo"] = {"email": cfg["MAIL_REPLY_TO"]}
    request = urllib.request.Request(
        "https://api.brevo.com/v3/smtp/email",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "api-key": cfg["BREVO_API_KEY"].strip(),
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError(f"Brevo API returned HTTP {response.status}.")
        try:
            result = json.loads(response.read().decode("utf-8"))
        except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        message_id = result.get("messageId") if isinstance(result, dict) else None
        return str(message_id)[:255] if message_id else None


def _is_marketing_kind(kind: str) -> bool:
    return email_purpose_for_kind(kind, testing=current_app.testing) == "marketing"


def queue_email(recipient: str, subject: str, kind: str, idempotency_key: str, *, account_id=None, order_id=None, campaign_id=None, template_id=None, retry_suppressed: bool = False, not_before: datetime | None = None, html_body: str, text_body: str, commit: bool = True) -> EmailMessage:
    """Persist a message to the outbox. Provider I/O happens only in the worker.

    Purpose is assigned from the trusted server-side message kind; callers cannot
    label promotional content transactional by passing a form value.
    """
    del retry_suppressed  # Suppressed idempotency keys are intentionally sticky.
    normalized_kind = str(kind or "").strip().lower()
    purpose = email_purpose_for_kind(normalized_kind, testing=current_app.testing)
    if not_before is not None and purpose != "marketing":
        raise ValueError("Only marketing messages can be scheduled for future delivery.")
    if purpose == "test" and account_id is not None:
        raise ValueError("Test email messages cannot be linked to a customer account.")
    key = str(idempotency_key or "")
    if not key:
        raise ValueError("An idempotency key is required for queued email.")
    if len(key) > 160:
        raise ValueError("Email idempotency keys cannot exceed 160 characters.")
    normalized_recipient = str(recipient or "").strip()
    if len(normalized_recipient) > 254:
        raise ValueError("Email recipient addresses cannot exceed 254 characters.")
    removed_account = False
    if account_id is not None:
        account_identity = db.session.execute(
            select(CustomerAccount.email, CustomerAccount.active)
            .where(CustomerAccount.id == account_id)
            .with_for_update()
        ).one_or_none()
        if (account_identity and not account_identity.active
                and account_identity.email == f"removed-customer-{account_id}@redacted.invalid"):
            account_email = account_identity.email
            normalized_recipient = account_email
            subject = "Customer data redacted"
            removed_account = True

    def reuse_existing(existing: EmailMessage) -> EmailMessage:
        if existing.kind != normalized_kind or existing.purpose != purpose:
            raise ValueError("Email idempotency key is already bound to a different message purpose.")
        if (existing.recipient != normalized_recipient or existing.account_id != account_id
                or existing.order_id != order_id or existing.campaign_id != campaign_id):
            raise ValueError("Email idempotency key is already bound to a different recipient or context.")
        return existing

    existing = EmailMessage.query.filter_by(idempotency_key=key).first()
    if existing:
        return reuse_existing(existing)
    marketing = purpose == "marketing"
    decision = None
    if marketing:
        from app.services.marketing_eligibility import evaluate_marketing_eligibility
        decision = evaluate_marketing_eligibility(
            account_id=account_id, respect_frequency=True,
            now=not_before if not_before is not None else None,
        )
    infrastructure_failure = bool(decision and decision.reason_code in {
        "eligibility_unavailable", "suppression_schema_unavailable", "suppression_schema_incomplete",
    })
    queued_status = (
        "suppressed" if removed_account else
        "failed" if infrastructure_failure else
        "queued" if not decision or decision.eligible else "suppressed"
    )
    record = EmailMessage(
        recipient=normalized_recipient, subject=str(subject or "")[:200],
        kind=normalized_kind[:32], idempotency_key=key, account_id=account_id,
        order_id=order_id, campaign_id=campaign_id, template_id=template_id,
        purpose=purpose,
        # Legacy column retained for compatibility; all current sends enforce policy.
        frequency_override=False,
        status=queued_status,
        failure_reason=(
            "Customer account removed; message content was redacted."
            if removed_account else
            "Marketing eligibility could not be verified; retry after the service recovers."
            if infrastructure_failure else
            "" if not decision or decision.eligible else decision.reason_code[:255]
        ),
        error_category="retryable_infrastructure" if infrastructure_failure and not removed_account else None,
        next_attempt_at=(
            None if removed_account else
            datetime.utcnow() + timedelta(minutes=15) if infrastructure_failure else not_before
        ),
        html_body=html_body if not removed_account and (not decision or decision.eligible or infrastructure_failure) else None,
        text_body=text_body if not removed_account and (not decision or decision.eligible or infrastructure_failure) else None,
    )
    db.session.add(record)
    try:
        if commit:
            db.session.commit()
        else:
            db.session.flush()
    except Exception:
        if not commit:
            raise
        db.session.rollback()
        # Concurrent idempotent enqueue: return the row committed by the winner.
        existing = EmailMessage.query.filter_by(idempotency_key=key).first()
        if existing:
            return reuse_existing(existing)
        raise
    return record


def deliver_email(record: EmailMessage, html_body: str | None = None, text_body: str | None = None,
                  *, expected_test_recipient: str | None = None) -> bool:
    """Dispatch one persisted outbox row; this is called by the explicit worker."""
    if expected_test_recipient is not None:
        target = _normalized_test_address(expected_test_recipient)
        if (record.status != "queued" or not target or record.kind != "admin_test"
                or record.purpose != "test" or record.account_id is not None
                or _normalized_test_address(record.recipient) != target):
            return False
    if record.status != "queued":
        return record.status == "sent"
    try:
        expected_purpose = email_purpose_for_kind(record.kind, testing=current_app.testing)
    except UnknownEmailKind:
        expected_purpose = None
    if expected_purpose is None or record.purpose != expected_purpose:
        reason = "Unknown or inconsistent email purpose; dispatch was blocked."
        db.session.execute(update(EmailMessage).where(
            EmailMessage.id == record.id, EmailMessage.status == "queued",
        ).values(status="failed", error_category="invalid_purpose",
                 failure_reason=reason, html_body=None, text_body=None,
                 next_attempt_at=None, claim_token=None, claimed_at=None,
                 updated_at=datetime.utcnow()))
        db.session.commit()
        db.session.refresh(record)
        return False
    if record.kind in TEST_EMAIL_KINDS and expected_test_recipient is None:
        logger.warning("admin_test_requires_isolated_dispatch message_id=%s", record.id)
        return False
    marketing = expected_purpose == "marketing"
    configured, reason = mail_configuration_status()
    if not configured:
        logger.warning("email_delivery_not_configured message_id=%s reason=%s", record.id, reason)
        return False
    html_body = record.html_body if record.html_body is not None else html_body
    text_body = record.text_body if record.text_body is not None else text_body
    if not isinstance(html_body, str) or not html_body.strip() or not isinstance(text_body, str) or not text_body.strip():
        logger.error("email_outbox_payload_missing message_id=%s", record.id)
        from sqlalchemy import inspect
        if inspect(db.engine).has_table("email_legacy_payload_review_event"):
            from app.services.email_legacy_reviews import quarantine_incomplete_legacy_message
            try:
                quarantine_incomplete_legacy_message(
                    record.id, actor_admin_id=None,
                    reason="Delivery preflight found missing or empty stored HTML/text content; dispatch blocked.",
                )
                db.session.refresh(record)
            except ValueError:
                db.session.rollback()
        return False
    if record.html_body is None or record.text_body is None:
        # Compatibility for internal callers that create the outbox row first:
        # payload persistence must commit before any provider request.
        record.html_body = html_body
        record.text_body = text_body
        db.session.commit()
    claim_token = str(uuid4())
    if marketing:
        claimed, reason, observed_status = _claim_marketing_dispatch(record, claim_token=claim_token)
        if not claimed:
            if reason and observed_status == "suppressed":
                record.status = "suppressed"
                record.failure_reason = reason[:255]
                db.session.commit()
            elif reason and observed_status == "retryable_failure":
                retry_count = record.retry_count + 1
                retryable = retry_count <= 3
                failure_values = dict(
                    status="failed", retry_count=retry_count,
                    error_category="retryable_infrastructure" if retryable else "infrastructure_failure",
                    failure_reason=reason[:255],
                    next_attempt_at=(datetime.utcnow() + timedelta(minutes=min(240, 15 * (2 ** (retry_count - 1)))) if retryable else None),
                    updated_at=datetime.utcnow(),
                )
                if not retryable:
                    failure_values.update(html_body=None, text_body=None)
                db.session.execute(update(EmailMessage).where(
                    EmailMessage.id == record.id, EmailMessage.status == "queued",
                ).values(**failure_values))
                db.session.commit()
                db.session.refresh(record)
            return False
        # The committed claim is the point of no return: the provider request
        # may now be in flight and cannot reliably be recalled. The claim
        # transaction released its account-row lock before this network call.
        record.status = "sending"
        record.failure_reason = ""
    elif expected_test_recipient is not None:
        if not _claim_admin_test_dispatch(record, claim_token, expected_test_recipient):
            return False
        record.status = "sending"
    else:
        if not _claim_transactional_dispatch(record, claim_token):
            return False
        record.status = "sending"
    try:
        provider = (current_app.config.get("MAIL_PROVIDER") or "").strip().lower()
        if provider == "resend":
            record.provider_message_id = _send_resend(record.recipient, record.subject, html_body, text_body)
        elif provider == "brevo":
            record.provider_message_id = _send_brevo(record.recipient, record.subject, html_body, text_body)
        else:
            _send_smtp(record.recipient, record.subject, html_body, text_body)
    except Exception as exc:
        code = getattr(exc, "smtp_code", None)
        provider = (current_app.config.get("MAIL_PROVIDER") or "smtp").strip().lower()
        http_status = getattr(exc, "code", None) if provider in {"resend", "brevo"} else None
        # A provider 5xx (or gateway timeout) does not prove that the request
        # was rejected: acceptance may have happened before the error response.
        # Only explicit throttling responses are safe to retry automatically.
        transient_smtp_codes = {421, 450, 451, 452}
        retryable = ((provider in {"resend", "brevo"} and http_status == 429)
                     or (provider == "smtp" and code in transient_smtp_codes))
        retry_count = record.retry_count + 1
        next_attempt_at = (
            datetime.utcnow() + timedelta(minutes=min(240, 15 * (2 ** max(0, retry_count - 1))))
            if retryable and retry_count <= 3 else None
        )
        if provider == "smtp" and code:
            failure_reason = f"SMTP error {code}."
        elif provider in {"resend", "brevo"} and isinstance(http_status, int):
            failure_reason = f"{provider.title()} HTTP {http_status}."
        else:
            provider_name = "SMTP" if provider == "smtp" else provider.title()
            failure_reason = f"{provider_name} send failed ({type(exc).__name__})."
        if provider in {"resend", "brevo"} and (
            http_status == 408 or isinstance(http_status, int) and 500 <= http_status <= 599
        ):
            error_category = "ambiguous_outcome"
        elif retryable and next_attempt_at:
            error_category = "retryable_provider"
        elif retryable:
            error_category = "retry_exhausted"
        elif provider in {"resend", "brevo"} and isinstance(http_status, int):
            error_category = "permanent_provider"
        elif provider == "smtp" and isinstance(code, int):
            error_category = "permanent_provider"
        else:
            # Transport exceptions without an explicit provider rejection may
            # have occurred after the provider accepted the message.
            error_category = "ambiguous_outcome"
        _finalize_dispatch(
            record, claim_token, status="failed", failure_reason=failure_reason,
            error_category=error_category,
            retry_count=retry_count, next_attempt_at=next_attempt_at,
            clear_payload=not bool(next_attempt_at),
        )
        logger.warning(
            "email_delivery_failed message_id=%s provider=%s error_type=%s smtp_code=%s http_status=%s",
            record.id, provider, type(exc).__name__, code if isinstance(code, int) else "none",
            http_status if isinstance(http_status, int) else "none",
        )
        return False
    return _finalize_dispatch(
        record, claim_token, status="sent", failure_reason="", sent_at=datetime.utcnow(),
        error_category=None, next_attempt_at=None, provider_message_id=record.provider_message_id,
        clear_payload=True,
    )


def _claim_marketing_dispatch(record: EmailMessage, *, claim_token: str | None = None) -> tuple[bool, str | None, str | None]:
    """Atomically recheck eligibility and commit a dispatch claim.

    Suppression, voluntary withdrawal, and account deactivation serialize on
    the same CustomerAccount row. A committed suppression wins if it gets the
    lock first; a committed claim wins if it gets the lock first. No lock is
    held while contacting the provider.
    """
    account_id, message_id = record.account_id, record.id
    claim_token = claim_token or str(uuid4())
    if not account_id:
        return False, "Marketing message has no linked customer account; sending is paused.", "suppressed"
    try:
        with db.engine.begin() as connection:
            from app.services.marketing_suppressions import marketing_account_lock_statement
            account = connection.execute(marketing_account_lock_statement(account_id)).mappings().first()
            from app.services.marketing_eligibility import evaluate_marketing_eligibility
            decision = evaluate_marketing_eligibility(
                account_id=account_id, connection=connection,
                respect_frequency=True,
                frequency_override=False,
                exclude_message_id=message_id,
            ) if account else None
            reason = None if decision and decision.eligible else (decision.reason_code if decision else "account_missing")
            if reason:
                changed = connection.execute(
                    update(EmailMessage).where(EmailMessage.id == message_id, EmailMessage.status == "queued")
                    .values(status="suppressed", failure_reason=reason[:255], html_body=None, text_body=None,
                            claim_token=None, updated_at=datetime.utcnow())
                ).rowcount
                status = connection.execute(select(EmailMessage.status).where(EmailMessage.id == message_id)).scalar_one_or_none()
                if changed == 1:
                    _refresh_automation_campaign_counts(connection, record)
                return False, reason if changed == 1 else None, status
            result = connection.execute(
                update(EmailMessage).where(EmailMessage.id == message_id, EmailMessage.status == "queued")
                .values(status="sending", failure_reason="", claim_token=claim_token,
                        claimed_at=datetime.utcnow(), last_attempt_at=datetime.utcnow(), updated_at=datetime.utcnow())
            )
            if result.rowcount != 1:
                status = connection.execute(select(EmailMessage.status).where(EmailMessage.id == message_id)).scalar_one_or_none()
                return False, None, status
            attempt_id = _insert_delivery_attempt(connection, record, claim_token)
        record._active_attempt_id = attempt_id
        return True, None, "sending"
    except Exception as exc:
        logger.error("marketing_dispatch_claim_failed message_id=%s error_type=%s", message_id, type(exc).__name__)
        return False, "Marketing eligibility could not be verified; retry after the service recovers.", "retryable_failure"


def _claim_transactional_dispatch(record: EmailMessage, claim_token: str) -> bool:
    """Claim transactional messages without consulting marketing consent state."""
    with db.engine.begin() as connection:
        if record.account_id is not None:
            account_identity = connection.execute(
                select(CustomerAccount.email, CustomerAccount.active)
                .where(CustomerAccount.id == record.account_id)
                .with_for_update()
            ).one_or_none()
            if (account_identity and not account_identity.active
                    and account_identity.email == f"removed-customer-{record.account_id}@redacted.invalid"):
                account_email = account_identity.email
                redacted = connection.execute(
                    update(EmailMessage).where(
                        EmailMessage.id == record.id,
                        EmailMessage.status == "queued",
                    ).values(
                        status="suppressed",
                        recipient=account_email,
                        subject="Customer data redacted",
                        failure_reason="Customer account removed; message content was redacted.",
                        html_body=None,
                        text_body=None,
                        next_attempt_at=None,
                        claim_token=None,
                        claimed_at=None,
                        updated_at=datetime.utcnow(),
                    )
                ).rowcount
                if redacted == 1:
                    record.status = "suppressed"
                    record.recipient = account_email
                    record.subject = "Customer data redacted"
                    record.failure_reason = "Customer account removed; message content was redacted."
                    record.html_body = None
                    record.text_body = None
                    record.next_attempt_at = None
                    record.claim_token = None
                    record.claimed_at = None
                return False
        result = connection.execute(
            update(EmailMessage).where(EmailMessage.id == record.id, EmailMessage.status == "queued")
            .values(status="sending", claim_token=claim_token, claimed_at=datetime.utcnow(),
                    last_attempt_at=datetime.utcnow(), updated_at=datetime.utcnow())
        )
        if result.rowcount != 1:
            return False
        record._active_attempt_id = _insert_delivery_attempt(connection, record, claim_token)
        return True


def _claim_admin_test_dispatch(record: EmailMessage, claim_token: str, recipient: str) -> bool:
    """Atomically claim only a queued, unlinked admin test for the configured address."""
    normalized = _normalized_test_address(recipient)
    if not normalized:
        return False
    with db.engine.begin() as connection:
        result = connection.execute(
            update(EmailMessage).where(
                EmailMessage.id == record.id,
                EmailMessage.status == "queued",
                EmailMessage.kind == "admin_test",
                EmailMessage.purpose == "test",
                EmailMessage.account_id.is_(None),
                func.lower(func.trim(EmailMessage.recipient)) == normalized,
                EmailMessage.html_body.is_not(None),
                func.trim(EmailMessage.html_body) != "",
                EmailMessage.text_body.is_not(None),
                func.trim(EmailMessage.text_body) != "",
                ~select(CustomerAccount.id).where(
                    func.lower(func.trim(CustomerAccount.email)) == normalized,
                ).exists(),
            ).values(status="sending", claim_token=claim_token, claimed_at=datetime.utcnow(),
                     last_attempt_at=datetime.utcnow(), updated_at=datetime.utcnow())
        )
        if result.rowcount != 1:
            return False
        record._active_attempt_id = _insert_delivery_attempt(connection, record, claim_token)
        record._admin_test_claimed = True
        return True


def _insert_delivery_attempt(connection, record: EmailMessage, claim_token: str) -> int:
    """Persist provider handoff metadata in the same transaction as the claim."""
    attempt_number = connection.execute(
        select(func.count(EmailDeliveryAttempt.id)).where(EmailDeliveryAttempt.message_id == record.id)
    ).scalar_one() + 1
    recovery_of_attempt_id = connection.execute(select(EmailDeliveryInvestigation.original_attempt_id).where(
        EmailDeliveryInvestigation.recovered_message_id == record.id,
    )).scalar_one_or_none()
    return connection.execute(EmailDeliveryAttempt.__table__.insert().values(
        message_id=record.id,
        attempt_number=attempt_number,
        attempt_key=claim_token,
        provider=(current_app.config.get("MAIL_PROVIDER") or "unknown").strip().lower()[:24],
        status="in_flight",
        started_at=datetime.utcnow(),
        recovery_of_attempt_id=recovery_of_attempt_id,
    ).returning(EmailDeliveryAttempt.id)).scalar_one()


def _refresh_automation_campaign_counts(connection, record: EmailMessage) -> None:
    key = str(record.idempotency_key or "")
    marker = ":account:"
    if not key.startswith("automation:") or marker not in key:
        return
    run_key = key[len("automation:"):].split(marker, 1)[0]
    prefix = f"automation:{run_key}:account:"
    rows = connection.execute(
        select(EmailMessage.status, func.count(EmailMessage.id))
        .where(func.substr(EmailMessage.idempotency_key, 1, len(prefix)) == prefix)
        .group_by(EmailMessage.status)
    ).all()
    states = {status: count for status, count in rows}
    queued_count = states.get("queued", 0) + states.get("sending", 0)
    sent_count = states.get("sent", 0) + states.get("delivered", 0)
    failed_count = states.get("failed", 0)
    suppressed_count = states.get("suppressed", 0)
    run_status = "queued" if queued_count else "partial" if failed_count else "sent" if sent_count else "complete"
    connection.execute(update(MarketingCampaignRun).where(
        MarketingCampaignRun.campaign_key == run_key,
    ).values(
        queued_count=queued_count, accepted_count=sent_count,
        failed_count=failed_count, suppressed_count=suppressed_count,
        status=run_status,
        finished_at=datetime.utcnow() if not queued_count else MarketingCampaignRun.finished_at,
        updated_at=datetime.utcnow(),
    ))


def _finalize_dispatch(record: EmailMessage, claim_token: str, **values) -> bool:
    """Fence final writes so stale/recovered workers cannot overwrite newer state."""
    updates = {
        "status": values["status"], "failure_reason": values.get("failure_reason", ""),
        "error_category": values.get("error_category"),
        "retry_count": values.get("retry_count", record.retry_count),
        "next_attempt_at": values.get("next_attempt_at"), "sent_at": values.get("sent_at"),
        "provider_message_id": values.get("provider_message_id"),
        "claim_token": None, "claimed_at": None, "updated_at": datetime.utcnow(),
    }
    if values.get("clear_payload"):
        updates.update(html_body=None, text_body=None)
    with db.engine.begin() as connection:
        changed = connection.execute(update(EmailMessage).where(
            EmailMessage.id == record.id, EmailMessage.status == "sending",
            EmailMessage.claim_token == claim_token,
        ).values(**updates)).rowcount
        if changed == 1 and values["status"] == "sent" and _is_marketing_kind(record.kind) and record.kind == "marketing_abandoned_cart":
            episode_key = str(record.idempotency_key or "").split(":", 1)[-1]
            if episode_key:
                connection.execute(update(CustomerCart).where(
                    CustomerCart.account_id == record.account_id,
                    CustomerCart.episode_key == episode_key,
                ).values(
                    reminder_sent_at=values.get("sent_at") or datetime.utcnow(),
                    # Reminder bookkeeping must not make an idle cart look newly active.
                    updated_at=CustomerCart.updated_at,
                ))
        if changed == 1 and record.kind == "restock_notification":
            alert_id = _restock_alert_id(record.kind, record.idempotency_key)
            if alert_id is not None and values["status"] in {"sent", "delivered"}:
                connection.execute(update(RestockAlert).where(
                    RestockAlert.id == alert_id, RestockAlert.status == "queued",
                ).values(status="notified", notified_at=values.get("sent_at") or datetime.utcnow()))
            elif (alert_id is not None and values["status"] == "failed"
                  and (values.get("error_category") == "ambiguous_outcome" or not values.get("next_attempt_at"))):
                # A permanent or uncertain outcome is no longer an active queue
                # item. Return it to pending for visible admin follow-up; the
                # email's sticky idempotency key prevents an automatic resend.
                connection.execute(update(RestockAlert).where(
                    RestockAlert.id == alert_id, RestockAlert.status == "queued",
                ).values(status="pending", notified_at=None, updated_at=datetime.utcnow()))
        if changed == 1:
            active_attempt_id = getattr(record, "_active_attempt_id", None)
            if active_attempt_id:
                attempt_status = {
                    "sent": "accepted", "delivered": "accepted",
                    "failed": "ambiguous" if values.get("error_category") == "ambiguous_outcome" else "failed",
                    "suppressed": "suppressed",
                }.get(values["status"], "failed")
                connection.execute(update(EmailDeliveryAttempt.__table__).where(
                    EmailDeliveryAttempt.id == active_attempt_id,
                    EmailDeliveryAttempt.attempt_key == claim_token,
                ).values(
                    status=attempt_status, completed_at=datetime.utcnow(),
                    provider_message_id=values.get("provider_message_id"),
                    error_category=values.get("error_category"),
                ))
            _refresh_automation_campaign_counts(connection, record)
    db.session.expire(record)
    return changed == 1


def _restock_alert_id(kind: str, idempotency_key: str | None) -> int | None:
    if kind != "restock_notification":
        return None
    key = str(idempotency_key or "")
    prefix = "restock-alert:"
    suffix = key[len(prefix):] if key.startswith(prefix) else ""
    return int(suffix) if suffix.isdigit() else None


def _run_email_worker(
    process_batch: Callable[["EmailWorkerHeartbeat"], dict[str, int]],
    *,
    worker_type: str = "queue",
) -> dict[str, int]:
    from app.services.email_worker import EmailWorkerHeartbeat
    heartbeat = EmailWorkerHeartbeat(worker_type=worker_type)
    try:
        heartbeat.start()
    except Exception as exc:
        db.session.rollback()
        logger.error("email_worker_start_failed error_type=%s", type(exc).__name__)
        raise RuntimeError("Email worker tracking is unavailable; no messages were dispatched.") from None
    failed = False
    try:
        return process_batch(heartbeat)
    except Exception:
        failed = True
        raise
    finally:
        heartbeat.stop(failed=failed, error_category="worker_batch_failed" if failed else None)


def process_email_queue(*, limit: int = 50, now: datetime | None = None,
                        max_seconds: int | None = None, should_stop=None) -> dict[str, int]:
    """Run one bounded worker process with durable liveness evidence."""
    return _run_email_worker(lambda heartbeat: _process_email_queue_batch(
        limit=limit, now=now, max_seconds=max_seconds,
        should_stop=should_stop, worker_heartbeat=heartbeat,
    ))


def process_transactional_email_queue(*, limit: int = 50, now: datetime | None = None,
                                      max_seconds: int | None = None,
                                      should_stop=None) -> dict[str, int]:
    """Dispatch only approved account-security and order-lifecycle messages."""
    return _run_email_worker(lambda heartbeat: _process_email_queue_batch(
        limit=limit, now=now, max_seconds=max_seconds, should_stop=should_stop,
        worker_heartbeat=heartbeat, allowed_kinds=TRANSACTIONAL_QUEUE_EMAIL_KINDS,
        required_purpose="transactional",
    ), worker_type="transactional_queue")


def _normalized_test_address(address: str | None) -> str:
    value = str(address or "").strip()
    parsed = parseaddr(value)[1]
    if (parsed != value or len(value) > 254
            or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value)):
        return ""
    return value.casefold()


def _validate_admin_test_message(record: EmailMessage | None, recipient: str) -> EmailMessage:
    if (record is None or record.status != "queued" or record.kind != "admin_test"
            or record.purpose != "test" or record.account_id is not None
            or _normalized_test_address(record.recipient) != recipient):
        raise ValueError("Only a queued, unlinked admin test for MAIL_TEST_ADDRESS can be dispatched.")
    if CustomerAccount.query.filter(
        func.lower(func.trim(CustomerAccount.email)) == recipient,
    ).first():
        raise ValueError("MAIL_TEST_ADDRESS must not match a customer account.")
    if (not isinstance(record.html_body, str) or not record.html_body.strip()
            or not isinstance(record.text_body, str) or not record.text_body.strip()):
        raise ValueError("The admin test message must have complete stored content.")
    return record


def process_admin_test_email(message_id: int) -> dict[str, int]:
    """Dispatch exactly one queued admin test message to MAIL_TEST_ADDRESS."""
    recipient = _normalized_test_address(current_app.config.get("MAIL_TEST_ADDRESS"))
    if not recipient:
        raise ValueError("A valid MAIL_TEST_ADDRESS is required for single-message test dispatch.")
    record = _validate_admin_test_message(db.session.get(EmailMessage, int(message_id)), recipient)
    configured, _reason = mail_configuration_status()
    if not configured:
        return {"claimed": 0, "sent": 0, "failed": 0, "suppressed": 0, "not_configured": 1}

    def dispatch_one(heartbeat: EmailWorkerHeartbeat) -> dict[str, int]:
        row = _validate_admin_test_message(db.session.get(EmailMessage, record.id), recipient)
        heartbeat.mark_attempt(1)
        row._admin_test_claimed = False
        delivered = deliver_email(row, expected_test_recipient=recipient)
        db.session.refresh(row)
        sent = int(delivered and row.status == "sent")
        if sent:
            heartbeat.mark_success()
        return {
            "claimed": int(row._admin_test_claimed),
            "sent": sent,
            "failed": int(row.status == "failed"),
            "suppressed": int(row.status == "suppressed"),
            "not_configured": 0,
        }

    return _run_email_worker(dispatch_one, worker_type="admin_test")


def _process_email_queue_batch(*, limit: int = 50, now: datetime | None = None,
                               max_seconds: int | None = None, should_stop=None,
                               worker_heartbeat=None, allowed_kinds: frozenset[str] | None = None,
                               required_purpose: str | None = None) -> dict[str, int]:
    """Dispatch due persisted messages. No scheduler is enabled by this command."""
    now = now or datetime.utcnow()
    limit = max(1, min(int(limit), 500))
    # Authentication links/codes are never dispatched after their own validity
    # window. Quarantine and erase their rendered payload on the next worker run.
    auth_ttls = {
        "verify_code": timedelta(minutes=10), "login_code": timedelta(minutes=10),
        "reset_password": timedelta(minutes=30), "verify_email": timedelta(hours=24),
        "marketing_confirm": timedelta(hours=24),
    }
    for kind, ttl in auth_ttls.items():
        if allowed_kinds is not None and kind not in allowed_kinds:
            continue
        expiry_query = update(EmailMessage).where(
            EmailMessage.status.in_(("queued", "failed")), EmailMessage.kind == kind,
            EmailMessage.created_at <= now - ttl,
        )
        if required_purpose is not None:
            expiry_query = expiry_query.where(EmailMessage.purpose == required_purpose)
        db.session.execute(expiry_query.values(
            status="failed", error_category="expired_auth_payload",
            failure_reason="Authentication email expired before dispatch.",
            html_body=None, text_body=None, next_attempt_at=None,
            claim_token=None, claimed_at=None, updated_at=now,
        ))
    db.session.commit()
    from sqlalchemy import inspect
    if not inspect(db.engine).has_table("email_legacy_payload_review_event"):
        # Fail closed for the whole batch rather than dispatching around malformed
        # legacy rows without an auditable quarantine path.
        return {"claimed": 0, "sent": 0, "failed": 0, "suppressed": 0,
                "not_configured": 0, "legacy_review_schema_unavailable": 1}
    from app.services.email_legacy_reviews import quarantine_incomplete_queued_messages
    excluded_test_kinds = frozenset({"admin_test"}) if allowed_kinds is None else None
    malformed_count = quarantine_incomplete_queued_messages(
        now=now, kinds=allowed_kinds, purpose=required_purpose,
        excluded_kinds=excluded_test_kinds,
    )
    configured, _reason = mail_configuration_status()
    if not configured:
        return {"claimed": 0, "sent": 0, "failed": int(malformed_count or 0), "suppressed": 0, "not_configured": 1}
    due_failed_query = EmailMessage.query.filter(
        EmailMessage.status == "failed", EmailMessage.error_category.in_(("retryable_provider", "retryable_infrastructure")),
        EmailMessage.retry_count <= 3, EmailMessage.next_attempt_at <= now,
        EmailMessage.kind != "admin_test",
    )
    if allowed_kinds is not None:
        due_failed_query = due_failed_query.filter(EmailMessage.kind.in_(allowed_kinds))
    if required_purpose is not None:
        due_failed_query = due_failed_query.filter(EmailMessage.purpose == required_purpose)
    due_failed = due_failed_query.order_by(EmailMessage.next_attempt_at, EmailMessage.id).limit(limit).all()
    for failed in due_failed:
        db.session.execute(update(EmailMessage).where(
            EmailMessage.id == failed.id, EmailMessage.status == "failed",
        ).values(status="queued", failure_reason="", updated_at=now))
    if due_failed:
        db.session.commit()
    remaining = max(0, limit - len(due_failed))
    queued_query = EmailMessage.query.filter(
        EmailMessage.status == "queued",
        (EmailMessage.next_attempt_at.is_(None) | (EmailMessage.next_attempt_at <= now)),
        EmailMessage.kind != "admin_test",
    )
    if allowed_kinds is not None:
        queued_query = queued_query.filter(EmailMessage.kind.in_(allowed_kinds))
    if required_purpose is not None:
        queued_query = queued_query.filter(EmailMessage.purpose == required_purpose)
    queued = queued_query.order_by(EmailMessage.created_at, EmailMessage.id).limit(remaining).all() if remaining else []
    row_ids = list(dict.fromkeys([row.id for row in due_failed] + [row.id for row in queued]))
    rows = [db.session.get(EmailMessage, row_id) for row_id in row_ids]
    counts = {"claimed": 0, "sent": 0, "failed": int(malformed_count or 0), "suppressed": 0, "not_configured": 0}
    started = time.monotonic()
    counts["stopped"] = 0
    for row in rows:
        if (should_stop and should_stop()) or (max_seconds and time.monotonic() - started >= max_seconds):
            counts["stopped"] = 1
            break
        if row is None:
            continue
        counts["claimed"] += 1
        if worker_heartbeat:
            worker_heartbeat.mark_attempt(counts["claimed"])
        deliver_email(row)
        db.session.refresh(row)
        if row.status == "sent":
            counts["sent"] += 1
            if worker_heartbeat:
                worker_heartbeat.mark_success()
        elif row.status == "suppressed":
            counts["suppressed"] += 1
        elif row.status == "failed":
            counts["failed"] += 1
    return counts


def email_queue_health() -> dict:
    """Return metadata-only queue health without dispatching or exposing payloads."""
    now = datetime.utcnow()
    grouped = db.session.query(EmailMessage.status, func.count(EmailMessage.id)).group_by(EmailMessage.status).all()
    counts = {status: int(count) for status, count in grouped}
    oldest = db.session.query(func.min(EmailMessage.created_at)).filter(
        EmailMessage.status == "queued",
        or_(EmailMessage.next_attempt_at.is_(None), EmailMessage.next_attempt_at <= now),
    ).scalar()
    return {
        "statuses": counts,
        "queued_oldest_at": oldest.isoformat() if oldest else None,
        "scheduled": EmailMessage.query.filter(
            EmailMessage.status == "queued", EmailMessage.next_attempt_at > now,
        ).count(),
        "retrying": EmailMessage.query.filter(
            EmailMessage.status == "failed",
            EmailMessage.error_category.in_(("retryable_provider", "retryable_infrastructure")),
            EmailMessage.next_attempt_at.is_not(None),
        ).count(),
        "retry_attempts": int(db.session.query(func.coalesce(func.sum(EmailMessage.retry_count), 0)).scalar() or 0),
        "failed_last_24h": EmailMessage.query.filter(
            EmailMessage.status == "failed", EmailMessage.updated_at >= now - timedelta(hours=24),
        ).count(),
        "stale_claims_30m": EmailMessage.query.filter(
            EmailMessage.status == "sending", EmailMessage.claimed_at <= now - timedelta(minutes=30),
        ).count(),
        "ambiguous_outcomes": EmailMessage.query.filter_by(error_category="ambiguous_outcome").count(),
        "legacy_payload_review": EmailMessage.query.filter_by(error_category="legacy_payload_review").count(),
    }


def recover_stale_email_claims(*, age_minutes: int = 30, now: datetime | None = None) -> int:
    """Fence stale worker claims as ambiguous; never automatically resend them."""
    now = now or datetime.utcnow()
    cutoff = now - timedelta(minutes=max(1, int(age_minutes)))
    stale_claims = db.session.execute(select(
        EmailMessage.id, EmailMessage.claim_token, EmailMessage.kind, EmailMessage.idempotency_key,
    ).where(
        EmailMessage.status == "sending", EmailMessage.claimed_at < cutoff,
    )).all()
    if not stale_claims:
        db.session.commit()
        return 0
    recovered = 0
    from sqlalchemy import inspect
    has_attempts = inspect(db.engine).has_table("email_delivery_attempt")
    for message_id, claim_token, kind, idempotency_key in stale_claims:
        changed = db.session.execute(
            update(EmailMessage).where(
                EmailMessage.id == message_id, EmailMessage.status == "sending",
                EmailMessage.claimed_at < cutoff, EmailMessage.claim_token == claim_token,
            ).values(status="failed", error_category="ambiguous_outcome", next_attempt_at=None,
                     claim_token=None, claimed_at=None,
                     failure_reason="Worker stopped after dispatch claim; provider acceptance is unknown.",
                     html_body=None, text_body=None,
                     updated_at=now)
        ).rowcount
        if changed == 1:
            recovered += 1
            alert_id = _restock_alert_id(kind, idempotency_key)
            if alert_id is not None:
                db.session.execute(update(RestockAlert).where(
                    RestockAlert.id == alert_id, RestockAlert.status == "queued",
                ).values(status="pending", notified_at=None, updated_at=now))
            if has_attempts and claim_token:
                db.session.execute(update(EmailDeliveryAttempt).where(
                    EmailDeliveryAttempt.message_id == message_id,
                    EmailDeliveryAttempt.attempt_key == claim_token,
                    EmailDeliveryAttempt.status == "in_flight",
                ).values(status="ambiguous", error_category="ambiguous_outcome", completed_at=now))
    db.session.commit()
    return recovered


def _marketing_suppression_reason(account):
    """Compatibility helper backed by the authoritative eligibility policy."""
    from app.services.marketing_eligibility import evaluate_marketing_eligibility
    decision = evaluate_marketing_eligibility(account)
    return None if decision.eligible else decision.reason_code


def render_branded_email(heading: str, paragraphs: list[str], *, button_url: str = "", button_text: str = "", unsubscribe_url: str = "", kind: str = "", order_reference: str = "", order_progress=None, security_code: str = "") -> tuple[str, str]:
    paragraphs = [
        block.strip()
        for paragraph in paragraphs
        for block in paragraph.replace("\r\n", "\n").split("\n\n")
        if block.strip()
    ]
    if kind in {"verify_code", "login_code"}:
        security_code = security_code if re.fullmatch(r"\d{6}", security_code or "") else ""
        if not security_code:
            for paragraph in paragraphs:
                match = re.search(r"(?<!\d)\d{6}(?!\d)", paragraph)
                if match:
                    security_code = match.group(0)
                    break
        if security_code:
            paragraphs = [paragraph for paragraph in paragraphs if security_code not in paragraph]
    preview_text = (paragraphs[0] if paragraphs else heading).strip()[:180]
    site_url = _email_public_base_url()
    logo_url = site_url + "/static/images/mainLogo-160.png"
    contact_url = site_url + url_for("public.contact", _external=False)
    accent_color = "#0B8F52" if kind == "payment_confirmed" else "#ED1B2F"
    hero_background = "#E8F7EF" if kind == "payment_confirmed" else "#F7F8FC"
    footer_notes = {
        "welcome": "Thanks for joining FITY. We’re glad you’re here.",
        "account_security": "",
        "unsubscribe_confirmation": "",
        "verify_email": "",
        "reset_password": "",
        "verify_code": "",
        "login_code": "",
    }
    html_body = render_template(
        "emails/base_branded.html", heading=heading, paragraphs=paragraphs,
        preheader=preview_text, button_url=button_url, button_text=button_text,
        unsubscribe_url=unsubscribe_url, site_url=site_url, logo_url=logo_url,
        contact_url=contact_url, footer_note=footer_notes.get(kind, ""),
        security_code=security_code, accent_color=accent_color,
        hero_background=hero_background,
        hero_text_color="#252525", order_reference=order_reference,
        order_progress=order_progress or (),
    )
    lines = [heading, ""]
    if security_code:
        lines.extend(["Your one-time code (tap and hold to copy):", security_code, ""])
    lines.extend(paragraphs)
    if button_url:
        lines.extend(["", f"{button_text}: {button_url}"])
    if order_reference:
        lines.extend(["", f"Order reference: {order_reference}"])
    if order_progress:
        lines.extend(["", "Order progress:"] + [f"{stage['label']}: {stage['description']}" for stage in order_progress])
    if unsubscribe_url:
        lines.extend(["", f"Unsubscribe from promotional email: {unsubscribe_url}"])
    lines.extend(["", "FityStores", "Bei Fity, Fits Poa."])
    return html_body, "\n".join(lines)


def queue_branded_email(recipient: str, subject: str, kind: str, idempotency_key: str, heading: str, paragraphs: list[str], *, account_id=None, order_id=None, campaign_id=None, button_url="", button_text="", unsubscribe_url="", order_reference="", order_progress=None, security_code="", commit: bool = True) -> EmailMessage:
    html_body, text_body = render_branded_email(
        heading, paragraphs, button_url=button_url, button_text=button_text,
        unsubscribe_url=unsubscribe_url, kind=kind, order_reference=order_reference,
        order_progress=order_progress, security_code=security_code,
    )
    return queue_email(recipient, subject, kind, idempotency_key, account_id=account_id, order_id=order_id, campaign_id=campaign_id, html_body=html_body, text_body=text_body, commit=commit)


def _order_progress_stages(order):
    fulfillment = order.fulfilment_method or "To be confirmed"
    fulfillment_stage = "Ready for collection" if fulfillment.lower() == "collection" else "Out for delivery"
    reached_processing = order.status in {"Payment confirmed", "Preparing order", "Ready for collection", "Out for delivery", "Completed"}
    reached_fulfillment = order.status in {fulfillment_stage, "Completed"}
    stages = [
        {"label": "Order received", "description": "We’ve received your order request.", "state": "current" if order.status == "Order received" else "done"},
        {"label": "Processing", "description": "The store will review the order and update its status.", "state": "current" if reached_processing and order.status != "Completed" and not reached_fulfillment else "done" if reached_processing else "upcoming"},
        {"label": fulfillment_stage, "description": "We’ll let you know when it is ready.", "state": "current" if reached_fulfillment and order.status != "Completed" else "done" if reached_fulfillment else "upcoming"},
        {"label": "Completed", "description": "Enjoy your FITY items.", "state": "done" if order.status == "Completed" else "upcoming"},
    ]
    if order.status == "Cancelled":
        return [
            {"label": "Order received", "description": "We received the order request.", "state": "done"},
            {"label": "Cancelled", "description": "The store cancelled this order.", "state": "current"},
        ]
    return stages


def _render_order_received_email(order, account):
    from app.services.formatting import money
    from app.services.product_images import product_display_image, product_image_alt, product_image_url, valid_product_image
    from app.services.settings import current_settings

    settings = current_settings()
    base_url = _email_public_base_url()
    tracking_path = url_for("public.track_order_link", reference=order.reference, tracking_token=order.tracking_token, _external=False)
    tracking_url = base_url + tracking_path

    rows = []
    subtotal_cents = 0
    subtotal_known = True
    for item in order.items:
        line_total = item.subtotal_cents
        if line_total is None and item.price_cents is not None:
            line_total = item.price_cents * item.quantity
        if line_total is None:
            subtotal_known = False
        else:
            subtotal_cents += line_total
        image_url = ""
        image_alt = f"Product image: {item.product_name}"
        image = item.variant.image if item.variant and valid_product_image(item.variant.image) else None
        if image is None and item.product:
            image = product_display_image(item.product)
        if image:
            candidate = product_image_url(image)
            image_url = base_url + candidate if candidate.startswith("/") else candidate
            if not image_url.startswith("https://"):
                image_url = ""
            image_alt = product_image_alt(image, image_alt)
        variant_label = (item.variant_label or "").strip()
        if not variant_label:
            variant_label = " · ".join(
                f"{label}: {value}"
                for label, value in (("Colour", item.color), ("Size", item.size))
                if value
            )
        rows.append({
            "name": item.product_name,
            "variant": variant_label,
            "sku": (item.sku or (item.product.sku if item.product else "") or "").strip(),
            "image_url": image_url,
            "image_alt": image_alt,
            "quantity": item.quantity,
            "unit_price": money(item.price_cents, settings.currency),
            "line_total": money(line_total, settings.currency),
        })

    discount_cents = max(0, order.discount_cents or 0)
    delivery_cents = max(0, order.delivery_fee_cents or 0)
    total_cents = order.total_cents

    try:
        local_zone = ZoneInfo(current_app.config.get("TIMEZONE", "Africa/Nairobi"))
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        local_zone = ZoneInfo("Africa/Nairobi")
    created_at = order.created_at
    if created_at:
        created_at = (created_at.replace(tzinfo=timezone.utc) if created_at.tzinfo is None else created_at).astimezone(local_zone)

    payment_attempts = list(order.payment_attempts or [])
    payment_method = payment_attempts[0].payment_method if payment_attempts else "Manual M-Pesa"
    fulfillment = order.fulfilment_method or "To be confirmed"
    delivery_details = "The store will confirm the delivery details with you."
    if fulfillment.lower() == "collection":
        delivery_details = settings.collection_instructions or "The store will contact you with collection instructions."
    else:
        for instruction_line in (order.instructions or "").splitlines():
            label, separator, value = instruction_line.partition(":")
            if separator and label.strip().lower() == "delivery address" and value.strip():
                delivery_details = value.strip()
                break

    from app.services.whatsapp import clean_phone
    raw_phone = settings.whatsapp_number or ""
    phone_digits = clean_phone(raw_phone) if not any(char.isalpha() for char in raw_phone) else ""
    if 9 <= len(phone_digits) <= 15 and not phone_digits.startswith("0"):
        support_url = f"https://wa.me/{phone_digits}?text=" + quote(f"Hello FITY, I need help with order {order.reference}.", safe="")
        support_label = "Need help? WhatsApp"
    else:
        support_url = base_url + url_for("public.contact", _external=False)
        support_label = "Need help? Contact us"
    customer_name = (order.customer_name or account.display_name or "Customer").strip()

    payment_status = order.payment_status or "Awaiting payment"
    if payment_status == "Paid":
        payment_guidance = "Payment has been confirmed by the store."
    elif payment_status == "Payment reported":
        payment_guidance = "We received your payment reference or proof. The seller still needs to verify it; a reference or screenshot alone is not payment confirmation. Please do not pay again until you have checked with the seller."
    elif payment_status == "Awaiting payment":
        if "m-pesa" in (payment_method or "").lower():
            payment_guidance = (settings.payment_instructions or "Contact the store to confirm payment instructions before paying.") + " A submitted reference or screenshot is reviewed by the seller and does not itself confirm payment."
        else:
            payment_guidance = "Payment is still pending. The seller will confirm the next step for this order."
    elif payment_status.lower() in {"not required", "no payment required"}:
        payment_guidance = "No payment is required for this order at this time."
    else:
        payment_guidance = f"Payment status: {payment_status}. Contact the store if you need clarification."

    if fulfillment.lower() == "collection":
        delivery_heading = "Collection Information"
        if order.status == "Ready for collection":
            fulfillment_guidance = "Your order is ready for collection. Follow the instructions below and confirm the meeting point with the store before travelling."
        elif order.status == "Completed":
            fulfillment_guidance = "This order has been marked completed."
        elif order.status == "Cancelled":
            fulfillment_guidance = "This order has been cancelled. Contact the store if you need help."
        else:
            fulfillment_guidance = "Collection is not ready yet. Wait for the store to confirm that it is ready before travelling."
    else:
        delivery_heading = "Delivery Information"
        if order.status == "Out for delivery":
            fulfillment_guidance = "Your order is out for delivery. Keep your phone available for delivery coordination."
        elif order.status == "Completed":
            fulfillment_guidance = "This order has been marked completed."
        elif order.status == "Cancelled":
            fulfillment_guidance = "This order has been cancelled. Contact the store if you need help."
        else:
            fulfillment_guidance = "Delivery arrangements are awaiting confirmation from the store. No delivery date has been promised yet."

    stages = _order_progress_stages(order)

    context = {
        "order": order,
        "customer_name": customer_name,
        "customer_email": account.email,
        "customer_phone": order.customer_phone or "Not provided",
        "items": rows,
        "line_count": len(rows),
        "item_count": sum(max(0, item.quantity or 0) for item in order.items),
        "subtotal": money(subtotal_cents, settings.currency) if subtotal_known else "To be confirmed",
        "discount": money(discount_cents, settings.currency),
        "delivery_fee": money(delivery_cents, settings.currency),
        "total": money(total_cents, settings.currency),
        "show_discount": discount_cents > 0,
        "show_delivery": delivery_cents > 0,
        "payment_method": payment_method,
        "payment_status": payment_status,
        "payment_guidance": payment_guidance,
        "paid": payment_status == "Paid",
        "fulfillment": fulfillment,
        "delivery_heading": delivery_heading,
        "delivery_details": delivery_details,
        "fulfillment_guidance": fulfillment_guidance,
        "created_at": created_at,
        "tracking_url": tracking_url,
        "support_url": support_url,
        "support_label": support_label,
        "stages": stages,
        "preheader": f"We received order {order.reference}. See your items, totals, and next steps.",
        "logo_url": base_url + "/static/images/mainLogo-160.png",
        "site_url": base_url,
    }
    html_body = render_template("emails/order_received.html", **context)
    text_lines = [
        "Order Received!", f"Thank you for shopping with FITY, {customer_name}.",
        f"Order reference: {order.reference}", "", "YOUR ORDER",
    ]
    for item in rows:
        item_description = item["name"]
        if item["variant"]:
            item_description += " — " + item["variant"]
        if item["sku"]:
            item_description += " — SKU: " + item["sku"]
        text_lines.append(f"{item_description} — {item['quantity']} × {item['unit_price']} = {item['line_total']}")
    text_lines.extend([
        "", f"Subtotal: {context['subtotal']}",
    ])
    if context["show_discount"]:
        text_lines.append(f"Discount: -{context['discount']}")
    if context["show_delivery"]:
        text_lines.append(f"Delivery fee: {context['delivery_fee']}")
    text_lines.extend([
        f"{'Total paid' if context['paid'] else 'Total order amount'}: {context['total']}",
        f"Payment: {context['payment_method']} — {context['payment_status']}",
        payment_guidance,
        f"{delivery_heading}: {fulfillment} — {delivery_details}",
        fulfillment_guidance,
        f"Current order status: {order.status}", f"View order status: {tracking_url}",
        "", "FityStores | Bei Fity, Fits Poa.", base_url,
    ])
    return html_body, "\n".join(text_lines)


ORDER_STATUS_EMAIL_KINDS = {
    "Preparing order": "order_processing",
    "Ready for collection": "ready_for_collection",
    "Out for delivery": "order_dispatched",
    "Cancelled": "order_cancellation",
    "Completed": "order_completed",
}


def order_status_event_kind(order_status: str | None) -> str:
    return ORDER_STATUS_EMAIL_KINDS.get((order_status or "").strip(), "order_status")


def _order_status_notification_matches_fulfilment(order, event_name: str) -> bool:
    if order_status_event_kind(order.status) != event_name:
        return False
    if event_name == "ready_for_collection":
        from app.services.orders import fulfilment_type
        return fulfilment_type(order.fulfilment_method) == "collection"
    if event_name == "order_dispatched":
        from app.services.orders import fulfilment_type
        return fulfilment_type(order.fulfilment_method) == "delivery"
    return True


def notify_order_event(order, event_name: str, idempotency_key: str):
    from sqlalchemy.orm import selectinload
    from app.models import CustomerOrderLink, Order, OrderItem, Product, ProductVariant
    from app.services.customer_mail import queue_branded_email
    link = CustomerOrderLink.query.filter_by(order_id=order.id).first()
    if not link or not link.account.active or not link.account.email_verified_at:
        return None
    if event_name == "payment_confirmed" and order.payment_status != "Paid":
        return None
    if event_name == "payment_failed" and order.payment_status != "Failed":
        return None
    if event_name == "order_status":
        event_name = order_status_event_kind(order.status)
    if event_name in ORDER_STATUS_EMAIL_KINDS.values():
        if not _order_status_notification_matches_fulfilment(order, event_name):
            return None
    payment_method = "Not recorded"
    if event_name == "order_received":
        order = Order.query.options(
            selectinload(Order.items).selectinload(OrderItem.product).selectinload(Product.images),
            selectinload(Order.items).selectinload(OrderItem.variant).selectinload(ProductVariant.image),
            selectinload(Order.payment_attempts),
        ).filter_by(id=order.id).first() or order
        link = CustomerOrderLink.query.filter_by(order_id=order.id).first() or link
        subject = f"Order received: {order.reference} | FITYStores"
        html_body, text_body = _render_order_received_email(order, link.account)
        return queue_email(
            link.account.email, subject, event_name, idempotency_key,
            account_id=link.account.id, order_id=order.id,
            html_body=html_body, text_body=text_body,
        )
    elif event_name == "payment_confirmed":
        subject = "FityStores payment confirmed"
        heading = "Payment confirmed"
        payment_attempt = next(iter(order.payment_attempts or []), None)
        payment_method = payment_attempt.payment_method if payment_attempt else "Not recorded"
        from app.services.settings import current_settings
        from app.services.formatting import money
        detail = f"Payment for order {order.reference} has been verified by FityStores. Confirmed amount: {money(order.total_cents, current_settings().currency)}. Payment method: {payment_method}. Payment status: {order.payment_status}. The store will now continue with {('collection preparation' if (order.fulfilment_method or '').lower() == 'collection' else 'delivery preparation')}."
    elif event_name == "payment_failed":
        subject = f"Payment status update: {order.reference} | FityStores"
        heading = "Payment status needs your attention"
        detail = (
            f"FityStores has recorded the payment status for order {order.reference} as failed. "
            "If you have already paid, please contact us before making another payment. "
            "Otherwise, contact the store for help with the next step."
        )
    elif event_name == "review_request":
        if order.status != "Completed" or order.payment_status != "Paid":
            return None
        subject = f"How was your FityStores order? | {order.reference}"
        heading = "How was your order?"
        detail = "Your order has been marked completed. If you would like, share an honest review of your experience. Reviews are optional and are not tied to a reward."
        return queue_branded_email(
            link.account.email, subject, event_name, idempotency_key,
            heading, [f"Order reference: {order.reference}.", detail],
            account_id=link.account.id, order_id=order.id,
            button_url=(_email_public_base_url() + url_for("public.reviews", _external=False)),
            button_text="Write a review",
        )
    elif event_name in ORDER_STATUS_EMAIL_KINDS.values():
        payment_attempt = next(iter(order.payment_attempts or []), None)
        payment_method = payment_attempt.payment_method if payment_attempt else "Not recorded"
        status_copy = {
            "order_processing": ("Your order is being prepared", "The store has started preparing your order."),
            "ready_for_collection": ("Your order is ready for collection", "Your order is ready. Follow the collection instructions and confirm the meeting point before travelling."),
            "order_dispatched": ("Your order is out for delivery", "Your order is out for delivery. Keep your phone available for delivery coordination."),
            "order_cancellation": ("Your order was cancelled", "The store has marked this order cancelled. Contact us if you need help."),
            "order_completed": ("Your order is complete", "The store has marked this order completed."),
        }
        heading, detail = status_copy[event_name]
        subject = f"{heading}: {order.reference} | FITYStores"
    elif event_name == "order_status":
        payment_attempt = next(iter(order.payment_attempts or []), None)
        payment_method = payment_attempt.payment_method if payment_attempt else "Not recorded"
        status_copy = {
            "Order received": ("We received your order", "Your order request is waiting for seller review."),
            "Awaiting payment": ("Payment is pending", "The order is awaiting payment. Follow the payment instructions confirmed by the store; a reference or screenshot alone does not confirm payment."),
            "Payment confirmed": ("Order update: payment confirmed", "The store recorded this order at the payment-confirmed stage."),
        }
        heading, detail = status_copy.get(order.status, ("Your order status was updated", f"The current order status is {order.status}."))
        subject = f"{heading}: {order.reference} | FITYStores"
        if order.status == "Payment confirmed" and order.payment_status != "Paid":
            heading = "Order status updated"
            detail = f"The order status is Payment confirmed, while the recorded payment status remains {order.payment_status}. Contact the store if you need clarification."
    else:
        subject = f"FityStores order update: {order.status}"
        heading = "Your order status was updated"
        detail = f"The current order status is {order.status}."
    if event_name == "order_status" and order.status == "Awaiting payment":
        from app.services.settings import current_settings
        configured_instructions = current_settings().payment_instructions
        if configured_instructions and "m-pesa" in payment_method.lower():
            detail += " " + configured_instructions
    base_url = _email_public_base_url()
    tracking_url = base_url + url_for("public.track_order_link", reference=order.reference, tracking_token=order.tracking_token, _external=False)
    message = queue_branded_email(
        link.account.email, subject, event_name, idempotency_key,
        heading, [
            f"Order reference: {order.reference}.", detail,
            f"Payment method: {payment_method}. Payment status: {order.payment_status or 'Not recorded'}.",
            f"Fulfilment method: {order.fulfilment_method or 'To be confirmed'}.",
        ],
        account_id=link.account.id, order_id=order.id,
        button_url=tracking_url,
        button_text="View order status",
        order_reference=order.reference,
        order_progress=_order_progress_stages(order),
    )
    if event_name in {*ORDER_STATUS_EMAIL_KINDS.values(), "order_status"}:
        if not message:
            logger.error("order_status_email_queue_returned_no_message order_id=%s kind=%s", order.id, event_name)
            return None
        message_status = getattr(message, "status", None)
        if message_status not in {"queued", "sending", "sent"}:
            logger.error(
                "order_status_email_queue_not_accepted order_id=%s kind=%s status=%s",
                order.id, event_name, str(message_status or "unknown")[:48],
            )
            return None
    return message


def notify_refund_completed(refund, idempotency_key: str | None = None):
    """Queue a factual notice after an administrator records a completed refund."""
    from app.models import CustomerOrderLink
    from app.services.formatting import money
    from app.services.settings import current_settings

    if (not refund or refund.status != "completed" or not refund.completed_at
            or not str(refund.reference or "").strip() or not refund.order):
        return None
    link = CustomerOrderLink.query.filter_by(order_id=refund.order_id).first()
    if not link or not link.account or not link.account.active or not link.account.email_verified_at:
        return None
    amount = money(refund.amount_cents, current_settings().currency)
    return queue_branded_email(
        link.account.email,
        f"Refund completed: {refund.order.reference} | FityStores",
        "refund_confirmation",
        idempotency_key or f"refund-completed:{refund.id}",
        "Your refund has been recorded as complete",
        [
            f"Refund for order {refund.order.reference} was recorded as completed by FityStores.",
            f"Recorded refund amount: {amount}.",
            f"Refund reference: {refund.reference.strip()[:120]}.",
        ],
        account_id=link.account.id,
        order_id=refund.order_id,
    )


def notify_restock_alert(alert, idempotency_key: str | None = None):
    """Queue a requested, email-only restock notice without promotional content."""
    from email.utils import parseaddr
    from app.models import Product

    if not alert or not getattr(alert, "id", None):
        return None
    alert = db.session.execute(
        select(RestockAlert)
        .where(RestockAlert.id == alert.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if (not alert or not alert.consent_to_contact or alert.preferred_contact != "Email"
            or alert.status not in {"pending", "queued"}):
        return None
    recipient = str(alert.contact or "").strip()
    if (parseaddr(recipient)[1] != recipient or len(recipient) > 254
            or not re.fullmatch(r"[^@<>\s]+@[^@<>\s]+\.[^@<>\s]+", recipient)):
        return None
    product = alert.product or db.session.get(Product, alert.product_id)
    if not product or not product.active or product.display_stock_quantity <= 0:
        return None
    variation = " ".join(part for part in (alert.size, alert.color) if part)
    paragraphs = [f"The item you asked us to contact you about is available again: {product.name}."]
    if variation:
        paragraphs.append(f"Requested variation: {variation}.")
    paragraphs.append("Availability can change. This message is only about your restock request.")
    return queue_branded_email(
        recipient, f"Requested item available: {product.name} | FityStores",
        "restock_notification", idempotency_key or f"restock-alert:{alert.id}",
        "Your requested item is available", paragraphs,
        button_url=_email_public_base_url() + url_for("public.product_detail", slug=product.slug),
        button_text="View item",
        commit=False,
    )
