"""Single fail-closed marketing eligibility policy for every send path."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import re
from email.utils import parseaddr
import logging

from sqlalchemy import and_, func, or_, select

from app.extensions import db
from app.models import (
    CustomerAccount, CustomerConsentEvent, EmailMessage, MarketingSuppressionCase,
)

logger = logging.getLogger(__name__)

# Before the explicit opt-in wording shipped, existing accounts could be
# legitimately subscribed without a consent timestamp. Grandfather only those
# pre-rollout accounts; newer accounts need a recorded consent/confirmation.
LEGACY_SUBSCRIBER_CUTOFF = datetime(2026, 10, 1)


@dataclass(frozen=True)
class EligibilityDecision:
    eligible: bool
    reason_code: str
    diagnostic: dict


def _decision(allowed: bool, reason: str, account_id=None) -> EligibilityDecision:
    return EligibilityDecision(allowed, reason, {"account_id": account_id})


def _email_is_valid(value) -> bool:
    email = str(value or "").strip()
    parsed = parseaddr(email)[1]
    return bool(
        email == parsed and len(email) <= 254 and "@" in email
        and not any(char.isspace() for char in email)
        and re.fullmatch(r"[^@<>]+@[^@<>]+\.[^@<>]+", email)
    )


def evaluate_marketing_eligibility(
    account=None, *, account_id=None, connection=None, respect_frequency=True,
    frequency_override=False, exclude_message_id=None, now=None,
) -> EligibilityDecision:
    """Return a stable reason code, including the narrow legacy opt-in rule.

    `connection` lets the dispatcher evaluate while holding the shared account
    row lock in the same transaction that persists its dispatch claim.
    """
    if account is None and account_id is not None:
        if connection is not None:
            account = connection.execute(
                select(CustomerAccount.id, CustomerAccount.email, CustomerAccount.active,
                       CustomerAccount.email_verified_at, CustomerAccount.marketing_status,
                       CustomerAccount.marketing_consent_at, CustomerAccount.marketing_withdrawn_at,
                       CustomerAccount.created_at)
                .where(CustomerAccount.id == account_id)
            ).mappings().first()
        else:
            account = db.session.get(CustomerAccount, account_id)
    if not account:
        return _decision(False, "account_missing", account_id)
    get_value = account.get if hasattr(account, "get") else lambda key, default=None: getattr(account, key, default)
    resolved_id = get_value("id", account_id)
    if not get_value("active", False):
        return _decision(False, "account_inactive", resolved_id)
    if not get_value("email_verified_at"):
        return _decision(False, "email_unverified", resolved_id)
    if get_value("marketing_status") != "subscribed":
        return _decision(False, "not_subscribed", resolved_id)
    consent_at = get_value("marketing_consent_at")
    if get_value("marketing_withdrawn_at"):
        return _decision(False, "consent_withdrawn", resolved_id)
    email = get_value("email")
    if not _email_is_valid(email):
        return _decision(False, "email_invalid", resolved_id)

    try:
        from sqlalchemy import inspect
        inspector = inspect(connection if connection is not None else db.engine)
        if not inspector.has_table(MarketingSuppressionCase.__tablename__):
            return _decision(False, "suppression_schema_unavailable", resolved_id)
        columns = {column["name"] for column in inspector.get_columns(MarketingSuppressionCase.__tablename__)}
        if not {"account_id", "cleared_at"} <= columns:
            return _decision(False, "suppression_schema_incomplete", resolved_id)
        statement = select(MarketingSuppressionCase.id).where(
            MarketingSuppressionCase.account_id == resolved_id,
            MarketingSuppressionCase.cleared_at.is_(None),
        ).limit(1)
        active_case = (connection.execute(statement).first() if connection is not None
                       else db.session.execute(statement).first())
        if active_case:
            return _decision(False, "active_suppression", resolved_id)

        latest_withdrawal = select(func.max(CustomerConsentEvent.created_at)).where(
            CustomerConsentEvent.account_id == resolved_id,
            CustomerConsentEvent.event.in_(("withdrawn", "unsubscribed")),
        )
        withdrawal_at = (connection.execute(latest_withdrawal).scalar_one_or_none() if connection is not None
                         else db.session.execute(latest_withdrawal).scalar_one_or_none())
        latest_confirmation = select(func.max(CustomerConsentEvent.created_at)).where(
            CustomerConsentEvent.account_id == resolved_id,
            CustomerConsentEvent.event == "confirmed",
        )
        confirmation_at = (connection.execute(latest_confirmation).scalar_one_or_none() if connection is not None
                           else db.session.execute(latest_confirmation).scalar_one_or_none())
    except Exception as exc:
        logger.error("marketing_eligibility_lookup_failed account_id=%s error_type=%s", resolved_id, type(exc).__name__)
        if connection is not None:
            raise
        db.session.rollback()
        return _decision(False, "eligibility_unavailable", resolved_id)
    # Older application versions represented an opted-in customer only with
    # marketing_status='subscribed'. Preserve that established state only for
    # accounts created before explicit registration consent was introduced.
    # New accounts need a timestamp or confirmed consent event. Any withdrawal
    # remains blocking unless a later explicit confirmation exists.
    consent_at = max(
        (value for value in (consent_at, confirmation_at) if value is not None),
        default=None,
    )
    if consent_at is None:
        created_at = get_value("created_at")
        if created_at and created_at < LEGACY_SUBSCRIBER_CUTOFF:
            consent_at = created_at
    if not consent_at:
        return _decision(False, "consent_missing", resolved_id)
    if withdrawal_at and withdrawal_at >= consent_at:
        return _decision(False, "consent_withdrawal_history", resolved_id)

    # Legacy arguments remain accepted for compatibility, but never turn off
    # the frequency window. There is no override path in this release.
    eligibility_time = now or datetime.utcnow()
    cutoff = eligibility_time - timedelta(hours=24)
    queued_delivery_time = func.coalesce(EmailMessage.next_attempt_at, EmailMessage.created_at)
    frequency_query = select(EmailMessage.id).where(
        EmailMessage.account_id == resolved_id,
        EmailMessage.purpose == "marketing",
        EmailMessage.id != (exclude_message_id or -1),
        or_(
            and_(EmailMessage.status.in_(("queued", "sending")),
                 queued_delivery_time >= cutoff, queued_delivery_time <= eligibility_time),
            and_(EmailMessage.status.in_(("sent", "delivered", "bounced", "complained")),
                 EmailMessage.sent_at >= cutoff, EmailMessage.sent_at <= eligibility_time),
        ),
    ).limit(1)
    recent = (connection.execute(frequency_query).first() if connection is not None
              else db.session.execute(frequency_query).first())
    if recent:
        return _decision(False, "frequency_limit", resolved_id)
    return _decision(True, "eligible", resolved_id)
