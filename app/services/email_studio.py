"""Safe, data-backed rendering helpers for administrator-initiated emails."""
import re

from flask import render_template


EMAIL_CATEGORIES = {
    "order_confirmation": {
        "label": "Order confirmation",
        "subject": "We received order {{ order_reference }}",
        "headline": "We received your order",
        "message": "Hi {{ customer_name }},\n\nYour order request is awaiting seller review. Payment status: {{ payment_status }}. Current order status: {{ order_status }}.",
        "variables": ("customer_name", "order_reference", "order_date", "order_subtotal", "discount_amount", "delivery_fee", "order_total", "payment_status", "order_status", "fulfilment_method", "fulfilment_instructions"),
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
        "variables": ("customer_name", "order_reference", "order_date", "order_subtotal", "discount_amount", "delivery_fee", "order_total", "order_status", "payment_status", "fulfilment_instructions"),
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
        "variables": ("customer_name", "order_reference", "order_date", "order_status", "payment_status", "fulfilment_method", "fulfilment_instructions"),
        "marketing": False,
    },
    "customer_service": {
        "label": "Custom customer-service email",
        "subject": "A message from FityStores",
        "headline": "A message from FityStores",
        "message": "Hi {{ customer_name }},\n\n",
        "variables": ("customer_name", "order_reference", "order_date", "order_status", "fulfilment_instructions"),
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


def validate_email_blocks(raw, category):
    """Validate the small, data-only visual builder format; no HTML or URLs execute."""
    import json

    try:
        blocks = json.loads(raw or "[]") if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        raise EmailContentError("The visual email blocks could not be read. Rebuild the content and try again.") from None
    if not isinstance(blocks, list) or len(blocks) > 30:
        raise EmailContentError("Use no more than 30 visual content blocks.")
    allowed_types = {"heading", "paragraph", "image_banner", "product_grid", "order_summary", "button", "divider", "spacer", "contact"}
    result = []
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") not in allowed_types:
            raise EmailContentError("One of the visual email blocks is not supported.")
        item = {"type": block["type"]}
        if item["type"] in {"heading", "paragraph", "contact"}:
            item["text"] = str(block.get("text") or "")[:2000]
            alignment = block.get("align", "left")
            if alignment not in {"left", "center"}:
                raise EmailContentError("Choose left or centered alignment for text blocks.")
            item["align"] = alignment
            validate_variables(item["text"], category)
        elif item["type"] == "image_banner":
            try:
                item["product_id"] = int(block.get("product_id"))
            except (TypeError, ValueError):
                raise EmailContentError("Choose a catalogue product image for each banner.") from None
            item["alt"] = str(block.get("alt") or "")[:255]
            if not item["alt"].strip():
                raise EmailContentError("Add alternative text for each image banner.")
        elif item["type"] == "button":
            item["text"] = str(block.get("text") or "View details")[:80]
            item["target"] = block.get("target", "shop")
            if item["target"] not in {"shop", "order", "cart", "product"}:
                raise EmailContentError("Choose a supported destination for the button.")
            if item["target"] == "product":
                try:
                    item["product_id"] = int(block.get("product_id"))
                except (TypeError, ValueError):
                    raise EmailContentError("Choose a catalogue product for the button destination.") from None
        result.append(item)
    return result


def render_studio_message(category, subject, headline, message, context, *, blocks=(), products=(), order_lines=(), order_summary=None, cart_summary=None, primary_url="", primary_label="", shop_url="", logo_url="", unsubscribe_url=""):
    if category not in EMAIL_CATEGORIES:
        raise EmailContentError("Choose a supported email category.")
    subject = replace_variables(subject, category, context).replace("\r", " ").replace("\n", " ").strip()[:200]
    headline = replace_variables(headline, category, context).strip()[:200]
    message = replace_variables(message, category, context).strip()[:5000]
    resolved_blocks = []
    for block in blocks:
        item = dict(block)
        if item["type"] in {"heading", "paragraph", "contact"}:
            item["text"] = replace_variables(item.get("text", ""), category, context).strip()
        elif item["type"] == "image_banner":
            product = next((row for row in products if row["id"] == item["product_id"]), None)
            item["product"] = product
            item["image_url"] = product["image_url"] if product else ""
            item["url"] = product["url"] if product else ""
        elif item["type"] == "button":
            item["text"] = replace_variables(item["text"], category, context).strip()
            item["url"] = shop_url if item["target"] == "shop" else primary_url if item["target"] in {"order", "cart"} else next((row["url"] for row in products if row["id"] == item.get("product_id")), "")
            if not item["url"]:
                raise EmailContentError("The selected button destination is unavailable for this recipient.")
        resolved_blocks.append(item)
    html = render_template(
        "emails/studio_message.html", category=category, subject=subject, headline=headline,
        message=message, blocks=resolved_blocks, products=products, order_lines=order_lines, order_summary=order_summary, cart_summary=cart_summary, shop_url=shop_url, logo_url=logo_url,
        primary_url=primary_url, primary_label=primary_label,
        order_date=context.get("order_date", ""), order_reference=context.get("order_reference", ""),
        unsubscribe_url=unsubscribe_url,
    )
    text_lines = [headline, "", message]
    if resolved_blocks:
        text_lines = []
        for block in resolved_blocks:
            if block["type"] in {"heading", "paragraph", "contact"} and block.get("text"):
                text_lines.extend([block["text"], ""])
            elif block["type"] == "button":
                text_lines.append(f"{block['text']}: {block['url']}")
            elif block["type"] == "image_banner" and block.get("product"):
                text_lines.append(f"{block['alt']}: {block['url']}")
    for item in order_lines if not resolved_blocks or any(block["type"] == "order_summary" for block in resolved_blocks) else ():
        text_lines.append(f"{item['name']} x {item['quantity']} — {item['total']}")
    if order_summary and (not resolved_blocks or any(block["type"] == "order_summary" for block in resolved_blocks)):
        order_details = ["Order " + order_summary["reference"]]
        if order_summary.get("date"):
            order_details.append(order_summary["date"])
        if order_summary.get("status"):
            order_details.append("Status: " + order_summary["status"])
        if order_summary.get("payment_status"):
            order_details.append("Payment: " + order_summary["payment_status"])
        if order_summary.get("fulfilment_method"):
            order_details.append("Fulfilment: " + order_summary["fulfilment_method"])
        text_lines.extend(order_details)
        text_lines.extend(["", "Subtotal: " + order_summary["subtotal"], "Discounts: " + order_summary["discount"], "Delivery fee: " + order_summary["delivery"], "Order total: " + order_summary["total"]])
        if order_summary.get("instructions"):
            text_lines.extend(["Fulfilment/payment instructions recorded:", order_summary["instructions"]])
    if cart_summary and (not resolved_blocks or any(block["type"] == "order_summary" for block in resolved_blocks)):
        text_lines.extend(["", f"Cart items: {cart_summary['item_count']}"])
        if cart_summary["total"]:
            text_lines.append("Current cart total: " + cart_summary["total"])
    for item in products if not resolved_blocks or any(block["type"] == "product_grid" for block in resolved_blocks) else ():
        text_lines.extend(["", item["name"], item["price"], item["url"]])
    if primary_url:
        text_lines.extend(["", f"{primary_label}: {primary_url}"])
    if shop_url:
        text_lines.extend(["Continue browsing: " + shop_url])
    text_lines.extend(["", "FityStores", "Bei Fity, Fits Poa."])
    if unsubscribe_url:
        text_lines.extend(["", "Unsubscribe from promotional email: " + unsubscribe_url])
    return subject, html, "\n".join(text_lines)
