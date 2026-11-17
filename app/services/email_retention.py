"""Bounded privacy cleanup for rendered email payloads."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import and_, or_, select, update

from app.extensions import db
from app.models import EmailMessage

logger = logging.getLogger(__name__)

AUTH_EXPIRIES = {
    "verify_code": timedelta(minutes=10), "login_code": timedelta(minutes=10),
    "reset_password": timedelta(minutes=30), "verify_email": timedelta(hours=24),
    "marketing_confirm": timedelta(hours=24),
}
TERMINAL_STATES = ("sent", "delivered", "bounced", "complained", "failed", "suppressed")


def cleanup_email_payloads(*, retention_days: int = 30, limit: int = 200,
                           dry_run: bool = True, now: datetime | None = None) -> dict:
    """Report or clear old message bodies in one bounded, concurrency-safe batch.

    Active queue/dispatch rows and retryable failures are excluded. Ambiguous
    deliveries can have their bodies erased immediately, but their outcome and
    metadata remain unchanged and they are never requeued.
    """
    retention_days, limit = int(retention_days), int(limit)
    if not 1 <= retention_days <= 3650:
        raise ValueError("retention_days must be between 1 and 3650")
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    # Database timestamps in this application are naive UTC. Build an aware
    # UTC timestamp first, then normalize to that existing storage convention.
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now - timedelta(days=retention_days)
    auth_expiry = or_(*(and_(EmailMessage.kind == kind, EmailMessage.created_at <= now - ttl)
                        for kind, ttl in AUTH_EXPIRIES.items()))
    auth_query = select(EmailMessage.id, EmailMessage.status).where(
        EmailMessage.status != "sending", auth_expiry,
        or_(EmailMessage.html_body.is_not(None), EmailMessage.text_body.is_not(None)),
    ).order_by(EmailMessage.created_at, EmailMessage.id).limit(limit)

    retryable_pending = and_(
        EmailMessage.error_category.in_(("retryable_provider", "retryable_infrastructure")),
        EmailMessage.next_attempt_at.is_not(None), EmailMessage.retry_count <= 3,
    )
    old_terminal = and_(EmailMessage.updated_at <= cutoff, ~retryable_pending)
    terminal_predicate = and_(
        EmailMessage.status.in_(TERMINAL_STATES),
        or_(EmailMessage.html_body.is_not(None), EmailMessage.text_body.is_not(None)),
        or_(EmailMessage.error_category == "ambiguous_outcome", old_terminal),
    )
    run_id = uuid4().hex
    with db.engine.begin() as connection:
        if not dry_run:
            auth_query = auth_query.with_for_update(skip_locked=True)
        auth_rows = list(connection.execute(auth_query).all())
        auth_ids = [row.id for row in auth_rows]
        remaining = max(0, limit - len(auth_ids))
        terminal_query = select(EmailMessage.id).where(terminal_predicate).order_by(
            EmailMessage.updated_at, EmailMessage.id
        ).limit(remaining)
        if auth_ids:
            terminal_query = terminal_query.where(EmailMessage.id.not_in(auth_ids))
        if not dry_run:
            terminal_query = terminal_query.with_for_update(skip_locked=True)
        terminal_ids = list(connection.execute(terminal_query).scalars()) if remaining else []
        if not dry_run and auth_ids:
            # Authentication payloads can include bearer tokens/codes. Clear
            # them as soon as their own validity window ends, regardless of the
            # eventual delivery outcome. Expired retryable messages are also
            # terminalized so they cannot churn through the worker. A currently
            # dispatching row is excluded.
            connection.execute(update(EmailMessage).where(
                EmailMessage.id.in_(auth_ids), EmailMessage.status.in_(("queued", "failed")), auth_expiry,
            ).values(
                status="failed", error_category="expired_auth_payload",
                failure_reason="Authentication email expired before dispatch.",
                next_attempt_at=None,
                claim_token=None, claimed_at=None, updated_at=now,
            ))
            connection.execute(update(EmailMessage).where(
                EmailMessage.id.in_(auth_ids), EmailMessage.status != "sending", auth_expiry,
            ).values(html_body=None, text_body=None))
        if not dry_run and terminal_ids:
            connection.execute(update(EmailMessage).where(
                EmailMessage.id.in_(terminal_ids), terminal_predicate,
            ).values(html_body=None, text_body=None))
    result = {
        "run_id": run_id, "dry_run": bool(dry_run), "retention_days": retention_days,
        "limit": limit, "expired_auth_payloads": len(auth_ids),
        "terminal_payloads": len(terminal_ids), "total_payloads": len(auth_ids) + len(terminal_ids),
    }
    logger.info("email_payload_retention_cleanup %s", result)
    return result
