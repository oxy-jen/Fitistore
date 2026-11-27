from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta

from flask import current_app, request
from flask_login import current_user

from app.models import AdminUser, CustomerAccount, SupportConversation, SupportMessage

SUPPORT_GUEST_COOKIE = "fity_support_access"
SUPPORT_TOPICS = {
    "general": "General help",
    "orders": "Orders",
    "payments": "Payments",
    "delivery": "Delivery and collection",
    "returns": "Returns",
    "products": "Products",
}
PRESENCE_FRESHNESS = timedelta(seconds=75)


def current_customer() -> CustomerAccount | None:
    if not current_user.is_authenticated:
        return None
    user = current_user._get_current_object()
    return user if isinstance(user, CustomerAccount) and user.active else None


def current_admin() -> AdminUser | None:
    if not current_user.is_authenticated:
        return None
    user = current_user._get_current_object()
    return user if isinstance(user, AdminUser) else None


def guest_token_digest(raw_token: str | None) -> str | None:
    if not raw_token or len(raw_token) > 160:
        return None
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def owns_conversation(conversation: SupportConversation) -> bool:
    customer = current_customer()
    if customer and conversation.account_id == customer.id:
        return True
    cookie_digest = guest_token_digest(request.cookies.get(SUPPORT_GUEST_COOKIE))
    stored_digest = conversation.guest_token_hash
    return bool(
        cookie_digest
        and stored_digest
        and hmac.compare_digest(cookie_digest, stored_digest)
    )


def conversation_room(conversation: SupportConversation) -> str:
    return f"support-conversation:{conversation.id}"


def conversation_payload(conversation: SupportConversation) -> dict:
    return {
        "id": conversation.public_id,
        "topic": conversation.topic,
        "status": conversation.status,
        "assigned": conversation.assigned_admin.username if conversation.assigned_admin else None,
        "notification_status": conversation.notification_status,
        "created_at": conversation.created_at.isoformat() + "Z",
        "last_message_at": conversation.last_message_at.isoformat() + "Z",
    }


def message_payload(message: SupportMessage) -> dict:
    return {
        "id": message.id,
        "sender_type": message.sender_type,
        "sender_name": message.sender_name,
        "body": message.body,
        "created_at": message.created_at.isoformat() + "Z",
    }


def allow_support_action(action: str, identity: str, *, limit: int, window_minutes: int) -> bool:
    from app.customer.routes import _rate_allowed

    return _rate_allowed(
        action,
        identity,
        limit=limit,
        window_minutes=window_minutes,
    )


def presence_is_fresh(heartbeat_at: datetime | None, *, now: datetime | None = None) -> bool:
    if heartbeat_at is None:
        return False
    now = now or datetime.utcnow()
    return now - heartbeat_at <= PRESENCE_FRESHNESS
