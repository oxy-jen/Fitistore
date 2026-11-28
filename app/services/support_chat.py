from __future__ import annotations

from datetime import datetime, timedelta

from flask import url_for
from flask_login import current_user

from app.models import AdminUser, CustomerAccount, SupportConversation, SupportMessage

SUPPORT_TOPICS = {
    "general": "General help",
    "orders": "Orders",
    "payments": "Payments",
    "delivery": "Delivery and collection",
    "returns": "Returns",
    "products": "Products",
}
PRESENCE_FRESHNESS = timedelta(seconds=75)
_RESOLUTION_REPLIES = (
    {"label": "That answered my question", "action": "resolve"},
    {"label": "Connect me to customer care", "action": "escalate"},
)


def _message_replies(*items: tuple[str, str]) -> list[dict[str, str]]:
    return [
        {"label": label, "message": message, "action": "reply"}
        for label, message in items
    ]


def _topic_replies() -> list[dict[str, str]]:
    return _message_replies(
        ("Orders", "I need help with an order"),
        ("Payments", "I have a payment question"),
        ("Delivery", "I have a delivery question"),
        ("Returns", "I need help with a return or refund"),
        ("Products", "I have a product question"),
    )


def support_bot_opening(topic: str, first_name: str, first_message: str) -> dict:
    answer, replies = support_bot_reply(topic, first_message)
    return {
        "body": (
            f"Hi {first_name}, I’m the FityStores virtual assistant. "
            f"{answer}"
        ),
        "quick_replies": replies,
    }


def support_bot_reply(topic: str, message: str) -> tuple[str, list[dict[str, str]]]:
    text = " ".join((message or "").lower().split())
    if topic == "general":
        if any(word in text for word in ("order", "tracking", "track")):
            topic = "orders"
        elif any(word in text for word in ("payment", "paid", "mpesa", "m-pesa")):
            topic = "payments"
        elif any(word in text for word in ("delivery", "deliver", "shipping")):
            topic = "delivery"
        elif any(word in text for word in ("return", "refund", "warranty")):
            topic = "returns"
        elif any(word in text for word in ("product", "stock", "available")):
            topic = "products"
        else:
            return "Choose a topic and I’ll guide you through the next steps.", (
                _topic_replies()
                + [{"label": item["label"], "action": item["action"]} for item in _RESOLUTION_REPLIES]
            )

    if topic == "orders":
        if any(word in text for word in ("place an order", "new order", "how to order", "buy")):
            answer = (
                "To place an order, browse the shop, add your items to the cart, and continue "
                "to checkout. The checkout page shows the available payment and delivery choices."
            )
            return answer, list(_RESOLUTION_REPLIES)
        if any(word in text for word in ("track", "tracking", "where is", "order status", "arrive")):
            answer = (
                "Open Orders in your FityStores account to check the latest status. If it has "
                "not updated, share your order number and customer care can check it for you."
            )
            return answer, list(_RESOLUTION_REPLIES)
        if any(word in text for word in ("confirm", "confirmation", "receipt", "email")):
            answer = (
                "Check Orders in your account for the order details, and check your inbox or "
                "spam folder for an email. If you still cannot find it, share the order number "
                "and customer care can verify it."
            )
            return answer, list(_RESOLUTION_REPLIES)
        if any(word in text for word in ("payment", "paid", "mpesa", "m-pesa", "transaction")):
            return _payment_guidance()
        if any(word in text for word in ("deliver", "delivery", "collect", "collection")):
            return _delivery_guidance()
        if any(word in text for word in ("cancel", "cancellation")):
            return (
                "Send the order number here. Customer care can check whether the order can "
                "still be cancelled.",
                list(_RESOLUTION_REPLIES),
            )
        if any(word in text for word in ("return", "refund", "warranty")):
            return (
                "Share your order number and the item you need help with. Customer care will "
                "confirm the available return or refund steps.",
                list(_RESOLUTION_REPLIES),
            )
        if text in {"yes", "yes, i have an existing order", "i have an existing order"}:
            return (
                "What do you need help with?",
                _message_replies(
                    ("Order confirmation", "I need an order confirmation"),
                    ("Order tracking", "I need to track my order"),
                    ("Payment issue", "I have a payment issue"),
                    ("Delivery issue", "I have a delivery issue"),
                    ("Cancellation", "I want to cancel an order"),
                    ("Return or refund", "I need help with a return or refund"),
                    ("Something else", "I have another order question"),
                ) + [{"label": item["label"], "action": item["action"]} for item in _RESOLUTION_REPLIES],
            )
        if text in {"no", "no, help me place an order", "i need help placing an order"}:
            return support_bot_reply(topic, "I need help placing an order")
        if any(word in text for word in ("something else", "another order question", "other order")):
            return (
                "Tell me what happened with your order. You can include the order number, "
                "but never share a payment PIN or account password.",
                list(_RESOLUTION_REPLIES),
            )
        return (
            "Is this about an order you have already placed?",
            _message_replies(
                ("Yes, an existing order", "Yes, I have an existing order"),
                ("No, help me shop", "No, help me place an order"),
            ) + [{"label": item["label"], "action": item["action"]} for item in _RESOLUTION_REPLIES],
        )

    if topic == "payments":
        if any(word in text for word in ("method", "how do i pay", "payment option", "ways to pay")):
            return (
                "The payment methods available for your items are shown during checkout. "
                "Choose one there before placing the order.",
                list(_RESOLUTION_REPLIES),
            )
        return _payment_guidance()

    if topic == "delivery":
        if any(word in text for word in ("option", "how much", "delivery choice", "collect")):
            return (
                "Available delivery and collection choices are shown at checkout for your "
                "order. Select the option that works for you before confirming.",
                list(_RESOLUTION_REPLIES),
            )
        return _delivery_guidance()

    if topic == "returns":
        return (
            "Share your order number and the item you need help with. Customer care will "
            "check the order and confirm the next steps for a return or refund.",
            list(_RESOLUTION_REPLIES),
        )

    if topic == "products":
        return (
            "Open the product page to see its current details and availability. If you still "
            "need help, send the product name or link and customer care can check it.",
            list(_RESOLUTION_REPLIES),
        )

    return (
        "Tell me a little more about what you need, or choose another support topic.",
        _topic_replies() + [{"label": item["label"], "action": item["action"]} for item in _RESOLUTION_REPLIES],
    )


def _payment_guidance() -> tuple[str, list[dict[str, str]]]:
    return (
        "If you have already paid, check Orders in your account for the latest status. "
        "Never share a PIN, password, or one-time verification code in chat.",
        list(_RESOLUTION_REPLIES),
    )


def _delivery_guidance() -> tuple[str, list[dict[str, str]]]:
    return (
        "For an existing order, check its latest status in Orders in your account. If the "
        "status looks out of date, send the order number and customer care can follow up.",
        list(_RESOLUTION_REPLIES),
    )


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


def owns_conversation(conversation: SupportConversation) -> bool:
    customer = current_customer()
    return bool(customer and conversation.account_id == customer.id)


def conversation_room(conversation: SupportConversation) -> str:
    return f"support-conversation:{conversation.id}"


def conversation_payload(conversation: SupportConversation) -> dict:
    return {
        "id": conversation.public_id,
        "topic": conversation.topic,
        "status": conversation.status,
        "assigned": conversation.assigned_admin.username if conversation.assigned_admin else None,
        "notification_status": conversation.notification_status,
        "escalation_requested": conversation.escalation_requested_at is not None,
        "created_at": conversation.created_at.isoformat() + "Z",
        "last_message_at": conversation.last_message_at.isoformat() + "Z",
    }


def message_payload(message: SupportMessage) -> dict:
    attachment = message.attachment
    return {
        "id": message.id,
        "conversation_id": message.conversation.public_id,
        "sender_type": message.sender_type,
        "sender_name": message.sender_name,
        "body": message.body,
        "client_message_id": message.client_message_id,
        "created_at": message.created_at.isoformat() + "Z",
        "attachment": (
            {
                "id": attachment.id,
                "filename": attachment.filename,
                "size_bytes": attachment.size_bytes,
                "content_type": attachment.content_type,
                "url": url_for(
                    "support.download_attachment",
                    public_id=message.conversation.public_id,
                    attachment_id=attachment.id,
                ),
            }
            if attachment
            else None
        ),
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
