"""Code-backed Email Studio inventory; status labels describe implemented code only."""

SYSTEM_TEMPLATES = (
    # Existing account and security workflows.
    {"id": "verify_email", "name": "Email verification", "group": "account", "purpose": "Verify a new account email", "trigger": "Registration or verification resend", "source": "emails/base_branded.html", "implemented": True, "preview": True},
    {"id": "welcome", "name": "Registration welcome", "group": "account", "purpose": "Welcome after account registration", "trigger": "Successful account creation; verification link is sent separately", "source": "emails/base_branded.html", "implemented": True, "preview": True},
    {"id": "verify_code", "name": "Email verification code", "group": "account", "purpose": "Verify account using a one-time code", "trigger": "Customer requests verification code", "source": "emails/base_branded.html", "implemented": True, "preview": True},
    {"id": "login_code", "name": "Login verification code", "group": "account", "purpose": "One-time sign-in code", "trigger": "Customer requests sign-in code", "source": "emails/base_branded.html", "implemented": True, "preview": True},
    {"id": "reset_password", "name": "Password reset", "group": "account", "purpose": "Secure password reset link", "trigger": "Customer requests password reset", "source": "emails/base_branded.html", "implemented": True, "preview": True},
    {"id": "account_security", "name": "Password changed notice", "group": "account", "purpose": "Notify customer of a password change", "trigger": "Successful password change", "source": "emails/base_branded.html", "implemented": True, "preview": True},
    {"id": "account_security_alert", "name": "Security alert", "group": "account", "purpose": "Notify customer about a security event", "trigger": "No distinct alert trigger found", "source": "emails/base_branded.html", "implemented": False, "preview": False},
    {"id": "email_change_verification", "name": "Email address change verification", "group": "account", "purpose": "Verify a changed account email", "trigger": "No email-change workflow found", "source": "", "implemented": False, "preview": False},
    {"id": "account_deactivation", "name": "Account deactivation confirmation", "group": "account", "purpose": "Confirm account deactivation", "trigger": "No deactivation email workflow found", "source": "", "implemented": False, "preview": False},
    {"id": "marketing_confirm", "name": "Marketing consent confirmation", "group": "account", "purpose": "Double-opt-in confirmation", "trigger": "Customer requests marketing subscription", "source": "emails/base_branded.html", "implemented": True, "preview": True},
    {"id": "unsubscribe_confirmation", "name": "Unsubscribe confirmation", "group": "account", "purpose": "Confirm promotional email preference update", "trigger": "Successful unsubscribe", "source": "emails/base_branded.html", "implemented": True, "preview": True},
    # Existing order workflows.
    {"id": "order_received", "name": "Order confirmation", "group": "transactional", "purpose": "Confirm that an order request was received", "trigger": "Order creation with linked active verified customer", "source": "emails/order_received.html", "implemented": True, "preview": True},
    {"id": "payment_confirmed", "name": "Payment confirmation", "group": "transactional", "purpose": "Confirm verified payment", "trigger": "Payment status becomes Paid", "source": "emails/base_branded.html", "implemented": True, "preview": True},
    {"id": "payment_failed", "name": "Payment status failure notice", "group": "transactional", "purpose": "Tell a linked customer that an administrator recorded the order payment status as failed", "trigger": "Authenticated admin changes a linked order payment status to Failed", "source": "emails/base_branded.html", "implemented": True, "preview": True},
    {"id": "restock_notification", "name": "Requested item back in stock", "group": "transactional", "purpose": "Notify a customer who explicitly requested an email restock contact", "trigger": "An inventory increase makes an item available for a consented email restock request", "source": "emails/base_branded.html", "implemented": True, "preview": True},
    {"id": "review_request", "name": "Order review request", "group": "transactional", "purpose": "Invite feedback about an actual completed paid order", "trigger": "Order is Completed and Paid", "source": "emails/base_branded.html", "implemented": True, "preview": True},
    {"id": "order_processing", "name": "Order processing", "group": "transactional", "purpose": "Confirm preparation has started", "trigger": "Admin updates an order to Preparing order for a linked, verified customer", "source": "emails/base_branded.html", "implemented": True, "preview": True},
    {"id": "ready_for_collection", "name": "Ready for collection", "group": "transactional", "purpose": "Provide collection readiness update", "trigger": "Admin updates an order to Ready for collection for a linked, verified customer", "source": "emails/base_branded.html", "implemented": True, "preview": True},
    {"id": "order_dispatched", "name": "Order dispatched", "group": "transactional", "purpose": "Report dispatch for delivery orders", "trigger": "Admin updates an order to Out for delivery for a linked, verified customer", "source": "emails/base_branded.html", "implemented": True, "preview": True},
    {"id": "order_delivered", "name": "Order delivered", "group": "transactional", "purpose": "Report confirmed delivery", "trigger": "No delivery confirmation event found; confirmed delivery emails are intentionally not sent without application evidence", "source": "", "implemented": False, "preview": False},
    {"id": "order_cancellation", "name": "Order cancellation", "group": "transactional", "purpose": "Confirm cancellation", "trigger": "Admin updates an order to Cancelled for a linked, verified customer", "source": "emails/base_branded.html", "implemented": True, "preview": True},
    {"id": "order_completed", "name": "Order completed", "group": "transactional", "purpose": "Confirm order completion without implying delivery", "trigger": "Admin updates an order to Completed for a linked, verified customer", "source": "emails/base_branded.html", "implemented": True, "preview": True},
    {"id": "refund_confirmation", "name": "Refund confirmation", "group": "transactional", "purpose": "Confirm a completed refund", "trigger": "Admin records a completed refund with a verified reference for a linked active, verified customer", "source": "emails/base_branded.html", "implemented": True, "preview": True},
    # Structured service and internal operations are intentionally not sent via arbitrary Studio copy.
    {"id": "structured_customer_service", "name": "Structured customer-service reply", "group": "customer_service", "purpose": "Controlled reply linked to a genuine support case or order", "trigger": "No structured support-case workflow found; arbitrary service messages remain marketing", "source": "", "implemented": False, "preview": False},
    {"id": "ops_delivery_failure", "name": "Critical email delivery failure", "group": "operational", "purpose": "Alert authorized administrators about critical delivery failure", "trigger": "No dedicated alert delivery workflow found", "source": "", "implemented": False, "preview": False},
    {"id": "ops_queue_health", "name": "Queue health alert", "group": "operational", "purpose": "Alert authorized administrators about queue health", "trigger": "No dedicated alert delivery workflow found", "source": "", "implemented": False, "preview": False},
    {"id": "ops_worker_failure", "name": "Worker failure", "group": "operational", "purpose": "Alert authorized administrators about worker failure", "trigger": "No dedicated alert delivery workflow found", "source": "", "implemented": False, "preview": False},
    {"id": "ops_automation_report", "name": "Automation execution report", "group": "operational", "purpose": "Summarize an automation run for administrators", "trigger": "Marketing automation runner is paused; no report delivery found", "source": "", "implemented": False, "preview": False},
    {"id": "ops_campaign_report", "name": "Campaign completion report", "group": "operational", "purpose": "Summarize a completed campaign for administrators", "trigger": "No campaign completion report found", "source": "", "implemented": False, "preview": False},
    {"id": "ops_suppression", "name": "Significant suppression event", "group": "operational", "purpose": "Alert authorized administrators to significant suppression activity", "trigger": "Audited suppression exists; no notification trigger found", "source": "", "implemented": False, "preview": False},
)


GROUP_LABELS = {
    "marketing": "Marketing and promotional",
    "transactional": "Transactional and order service",
    "account": "Account and security (system-managed)",
    "customer_service": "Customer service",
    "operational": "Operational (administrator-only)",
}


def build_email_template_inventory(categories):
    """Return grouped, auditable definitions without inventing database records."""
    grouped = {key: [] for key in GROUP_LABELS}
    for key, definition in categories.items():
        group = definition.get("group", "marketing" if definition.get("marketing") else "transactional")
        item = {
            "id": key, "name": definition["label"], "group": group,
            "purpose": definition.get("purpose", definition["label"]),
            "trigger": definition.get("trigger", "Administrator-selected manual compose/preview; automation is not implied"),
            "source": "emails/promotion_message.html" if key in {"promotion", "daily_goodies", "weekly_picks", "new_product_announcement", "special_offers", "back_in_stock", "marketing_welcome", "re_engagement", "custom_campaign"} else "emails/studio_message.html",
            "implemented": True, "preview": True, "enabled": True,
            "management": "Administrator-editable",
        }
        grouped.setdefault(group, []).append(item)
    for definition in SYSTEM_TEMPLATES:
        existing = next((row for rows in grouped.values() for row in rows if row["id"] == definition["id"]), None)
        if existing:
            existing.update({
                "trigger": definition["trigger"], "source": definition["source"],
                "management": "System-triggered + constrained Studio",
                "preview": definition["preview"],
                "preview_url": definition["id"] if definition["preview"] else "",
            })
            continue
        grouped.setdefault(definition["group"], []).append({
            **definition, "enabled": definition["implemented"],
            "management": "System-managed", "preview_url": definition["id"] if definition["preview"] else "",
        })
    return [(key, GROUP_LABELS[key], grouped.get(key, [])) for key in GROUP_LABELS]
