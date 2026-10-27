"""Safe, data-backed rendering helpers for administrator-initiated emails."""
import re

from flask import render_template


EMAIL_CATEGORIES = {
    "order_confirmation": {
        "label": "Order confirmation",
        "subject": "We received order {{ order_reference }}",
        "headline": "We received your order",
        "message": "Hi {{ customer_name }},\n\nYour order request is awaiting seller review. Payment status: {{ payment_status }}. Current order status: {{ order_status }}.",
        "variables": ("customer_name", "order_reference", "order_subtotal", "discount_amount", "delivery_fee", "order_total", "payment_status", "order_status", "fulfilment_method", "fulfilment_instructions"),
        "marketing": False,
    },
    "cart_reminder": {
        "label": "Cart reminder (manual only)",
        "subject": "Still interested in your picks at FityStores?",
        "headline": "Your cart is still here",
        "message": "Hi {{ customer_name }}, you left these items in your cart. Would you like to continue shopping? Availability and prices can change.",
        "variables": ("customer_name", "cart_item_count", "cart_total", "unsubscribe_link"),
        "marketing": True,
    },
    "thank_you": {
        "label": "Thank-you email",
        "subject": "Thank you for shopping with FityStores",
        "headline": "Thank you, {{ customer_name }}",
        "message": "Thank you for choosing FityStores. Order {{ order_reference }} is currently {{ order_status }} and the recorded total is {{ order_total }}.",
        "variables": ("customer_name", "order_reference", "order_subtotal", "discount_amount", "delivery_fee", "order_total", "order_status", "payment_status", "fulfilment_instructions"),
        "marketing": False,
    },
    "promotion": {
        "label": "Promotions and new arrivals",
        "subject": "Fresh finds from FityStores",
        "headline": "Selected for you",
        "message": "Hi {{ customer_name }}, take a look at these selected products from our current catalogue.\n\n{{ unsubscribe_link }}",
        "variables": ("customer_name", "unsubscribe_link"),
        "marketing": True,
    },
    "order_status": {
        "label": "Order status / fulfilment update",
        "subject": "An update about order {{ order_reference }}",
        "headline": "Your order status was updated",
        "message": "Order {{ order_reference }} is currently {{ order_status }}. Payment status: {{ payment_status }}. Fulfilment method: {{ fulfilment_method }}.",
        "variables": ("customer_name", "order_reference", "order_status", "payment_status", "fulfilment_method", "fulfilment_instructions"),
        "marketing": False,
    },
    "customer_service": {
        "label": "Custom customer-service email",
        "subject": "A message from FityStores",
        "headline": "A message from FityStores",
        "message": "Hi {{ customer_name }},\n\n",
        "variables": ("customer_name", "order_reference", "order_status", "fulfilment_instructions"),
        "marketing": False,
    },
}

_VARIABLE = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}}")


class EmailContentError(ValueError):
    pass


def replace_variables(value, category, context):
    allowed = set(EMAIL_CATEGORIES[category]["variables"])
    value = value or ""
    matches = list(_VARIABLE.finditer(value))
    residue = _VARIABLE.sub("", value)
    if "{{" in residue or "}}" in residue:
        raise EmailContentError("Variable syntax is invalid. Use a supported name such as {{ customer_name }}.")
    found = {match.group(1) for match in matches}
    unknown = found - allowed
    if unknown:
        raise EmailContentError("Unsupported variable(s): " + ", ".join(sorted(unknown)))
    unavailable = {name for name in found if context.get(name) in (None, "")}
    if unavailable:
        raise EmailContentError("Select the required customer/order/cart context for: " + ", ".join(sorted(unavailable)))
    return _VARIABLE.sub(lambda match: str(context[match.group(1)]), value or "")


def validate_variables(value, category):
    if category not in EMAIL_CATEGORIES:
        raise EmailContentError("Choose a supported email category.")
    value = value or ""
    residue = _VARIABLE.sub("", value)
    if "{{" in residue or "}}" in residue:
        raise EmailContentError("Variable syntax is invalid. Use a supported name such as {{ customer_name }}.")
    unknown = set(_VARIABLE.findall(value)) - set(EMAIL_CATEGORIES[category]["variables"])
    if unknown:
        raise EmailContentError("Unsupported variable(s): " + ", ".join(sorted(unknown)))
    return True


def render_studio_message(category, subject, headline, message, context, *, products=(), order_lines=(), order_summary=None, cart_summary=None, primary_url="", primary_label="", shop_url="", logo_url="", unsubscribe_url=""):
    if category not in EMAIL_CATEGORIES:
        raise EmailContentError("Choose a supported email category.")
    subject = replace_variables(subject, category, context).replace("\r", " ").replace("\n", " ").strip()[:200]
    headline = replace_variables(headline, category, context).strip()[:200]
    message = replace_variables(message, category, context).strip()[:5000]
    html = render_template(
        "emails/studio_message.html", category=category, subject=subject, headline=headline,
        message=message, products=products, order_lines=order_lines, order_summary=order_summary, cart_summary=cart_summary, shop_url=shop_url, logo_url=logo_url,
        primary_url=primary_url, primary_label=primary_label,
        unsubscribe_url=unsubscribe_url,
    )
    text_lines = [headline, "", message]
    for item in order_lines:
        text_lines.append(f"{item['name']} x {item['quantity']} — {item['total']}")
    if order_summary:
        text_lines.extend(["", "Subtotal: " + order_summary["subtotal"], "Discounts: " + order_summary["discount"], "Delivery fee: " + order_summary["delivery"], "Order total: " + order_summary["total"]])
        if order_summary.get("instructions"):
            text_lines.extend(["Fulfilment/payment instructions recorded:", order_summary["instructions"]])
    if cart_summary:
        text_lines.extend(["", f"Cart items: {cart_summary['item_count']}"])
        if cart_summary["total"]:
            text_lines.append("Current cart total: " + cart_summary["total"])
    for item in products:
        text_lines.extend(["", item["name"], item["price"], item["url"]])
    if primary_url:
        text_lines.extend(["", f"{primary_label}: {primary_url}"])
    if shop_url:
        text_lines.extend(["Continue browsing: " + shop_url])
    text_lines.extend(["", "FityStores", "Bei Fity, Fits Poa."])
    if unsubscribe_url:
        text_lines.extend(["", "Unsubscribe from promotional email: " + unsubscribe_url])
    return subject, html, "\n".join(text_lines)
