"""Authoritative server-side purpose mapping for customer email kinds."""

from __future__ import annotations


class UnknownEmailKind(ValueError):
    """Raised when a caller tries to queue an unrecognized email workflow."""


TRANSACTIONAL_EMAIL_KINDS = frozenset({
    "verify_email", "verify_code", "login_code", "reset_password",
    "welcome", "account_security", "unsubscribe_confirmation",
    "order_received", "order_update", "payment_confirmed", "payment_failed",
    "order_status", "order_processing", "ready_for_collection", "order_dispatched",
    "order_cancellation", "order_completed", "refund_confirmation",
    "restock_notification", "review_request", "marketing_confirm",
    # Email Studio only emits these for unchanged, code-defined order notices.
    "studio_order_confirmation", "studio_thank_you", "studio_order_status",
})

MARKETING_EMAIL_KINDS = frozenset({
    "marketing_campaign", "marketing_daily_goodies", "marketing_weekly_picks",
    "marketing_abandoned_cart", "marketing_review_highlight",
    "marketing_new_product", "marketing_special_offer", "marketing_back_in_stock",
    "marketing_welcome", "marketing_reengagement", "marketing_custom_campaign",
    # Explicit legacy kinds remain marketing; no new caller should emit these.
    "admin_order_confirmation", "admin_thank_you", "admin_order_status",
    "admin_cart_reminder", "admin_promotion", "admin_customer_service",
})

TEST_EMAIL_KINDS = frozenset({"admin_test"})

STUDIO_TRANSACTIONAL_CATEGORY_KINDS = {
    "order_confirmation": "studio_order_confirmation",
    "thank_you": "studio_thank_you",
    "order_status": "studio_order_status",
}

# These are trusted Studio category-to-purpose mappings. The submitted category
# never gets to choose a queue purpose directly; every mapping below resolves to
# a server-known marketing kind, so dispatch always applies eligibility rules.
STUDIO_MARKETING_CATEGORY_KINDS = {
    "cart_reminder": "marketing_abandoned_cart",
    "promotion": "marketing_campaign",
    "daily_goodies": "marketing_daily_goodies",
    "weekly_picks": "marketing_weekly_picks",
    "new_product_announcement": "marketing_new_product",
    "special_offers": "marketing_special_offer",
    "back_in_stock": "marketing_back_in_stock",
    "marketing_welcome": "marketing_welcome",
    "re_engagement": "marketing_reengagement",
    "custom_campaign": "marketing_custom_campaign",
    "customer_service": "admin_customer_service",
}


def studio_email_kind_for_category(category: str, *, marketing: bool) -> str | None:
    """Resolve a Studio category only after trusted content classification.

    An edited order-notice category is still allowed as a marketing message,
    but never keeps its transactional kind.
    """
    if category in STUDIO_TRANSACTIONAL_CATEGORY_KINDS:
        if marketing:
            return "marketing_campaign"
        return STUDIO_TRANSACTIONAL_CATEGORY_KINDS[category]
    return STUDIO_MARKETING_CATEGORY_KINDS.get(category)


def email_purpose_for_kind(kind: str, *, testing: bool = False) -> str:
    """Map a trusted workflow kind to a purpose; unknown kinds fail closed.

    The legacy ``test`` alias is accepted only by Flask's test application. A
    production caller must use the explicit, isolated ``admin_test`` kind.
    """
    normalized = str(kind or "").strip().lower()
    if normalized in TRANSACTIONAL_EMAIL_KINDS:
        return "transactional"
    if normalized in MARKETING_EMAIL_KINDS:
        return "marketing"
    if normalized in TEST_EMAIL_KINDS or (testing and normalized == "test"):
        return "test"
    raise UnknownEmailKind("Unrecognized email workflow kind.")
