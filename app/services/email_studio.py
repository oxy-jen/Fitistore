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


def _contrast_ratio(first, second):
    def luminance(color):
        channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
    values = sorted((luminance(first), luminance(second)))
    return (values[1] + 0.05) / (values[0] + 0.05)


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
    allowed_types = {"design", "heading", "paragraph", "image_banner", "product_grid", "order_summary", "button", "divider", "spacer", "contact"}
    result = []
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") not in allowed_types:
            raise EmailContentError("One of the visual email blocks is not supported.")
        item = {"type": block["type"]}
        if item["type"] == "design":
            defaults = {"hero_background": "#111111", "text_color": "#FFFFFF", "accent_color": "#ED1B2F", "section_background": "#FFFFFF", "footer_note": "Bei Fity, Fits Poa", "preheader": "A note from FityStores"}
            for key, fallback in defaults.items():
                value = str(block.get(key) or fallback)[:180 if key == "preheader" else 200]
                if key in {"hero_background", "text_color", "accent_color", "section_background"} and not re.fullmatch(r"#[0-9A-Fa-f]{6}", value):
                    raise EmailContentError("Choose valid six-digit hex colours for the email design.")
                item[key] = value
        elif item["type"] in {"heading", "paragraph", "contact"}:
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
                if category == "cart_reminder":
                    item["product_id"] = 0  # Replaced with the first current cart item at render time.
                else:
                    raise EmailContentError("Choose a catalogue product image for each banner.") from None
            item["alt"] = str(block.get("alt") or "")[:255]
            if not item["alt"].strip():
                raise EmailContentError("Add alternative text for each image banner.")
            item["align"] = block.get("align", "center")
            if item["align"] not in {"left", "center", "right"}:
                raise EmailContentError("Choose left, centered, or right alignment for the banner image.")
            try:
                item["width"] = int(block.get("width", 552))
            except (TypeError, ValueError):
                item["width"] = 552
            if item["width"] not in {160, 240, 360, 552}:
                raise EmailContentError("Choose a supported banner image size.")
        elif item["type"] == "button":
            item["text"] = str(block.get("text") or "View details")[:80]
            item["target"] = block.get("target", "shop")
            if item["target"] not in {"shop", "order", "cart", "product"}:
                raise EmailContentError("Choose a supported destination for the button.")
            if item["target"] == "product":
                try:
                    item["product_id"] = int(block.get("product_id"))
                except (TypeError, ValueError):
                    if category == "cart_reminder":
                        item["product_id"] = 0  # The destination follows the recipient's first cart item.
                    else:
                        raise EmailContentError("Choose a catalogue product for the button destination.") from None
        result.append(item)
    return result


def render_studio_message(category, subject, headline, message, context, *, blocks=(), products=(), order_lines=(), order_summary=None, cart_summary=None, primary_url="", primary_label="", shop_url="", logo_url="", unsubscribe_url="", design=None, site_url="", contact_url="https://fitystore.top/contact"):
    if category not in EMAIL_CATEGORIES:
        raise EmailContentError("Choose a supported email category.")
    subject = replace_variables(subject, category, context).replace("\r", " ").replace("\n", " ").strip()[:200]
    headline = replace_variables(headline, category, context).strip()[:200]
    message = replace_variables(message, category, context).strip()[:5000]
    design_defaults = {"hero_background": "#111111", "text_color": "#FFFFFF", "accent_color": "#ED1B2F", "section_background": "#FFFFFF", "footer_note": "Bei Fity, Fits Poa", "preheader": "A note from FityStores"}
    design = {**design_defaults, **(design or {})}
    design = {key: value for key, value in design.items() if key in design_defaults}
    if _contrast_ratio(design["text_color"], design["hero_background"]) < 4.5:
        raise EmailContentError("Choose hero background and text colours with readable contrast.")
    design["button_text_color"] = "#111111" if _contrast_ratio("#111111", design["accent_color"]) >= _contrast_ratio("#FFFFFF", design["accent_color"]) else "#FFFFFF"
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
        unsubscribe_url=unsubscribe_url, design=design, site_url=site_url or shop_url,
        contact_url=contact_url,
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
    if category != "cart_reminder" and cart_summary and (not resolved_blocks or any(block["type"] == "order_summary" for block in resolved_blocks)):
        text_lines.extend(["", f"Cart items: {cart_summary['item_count']}"])
        if cart_summary["total"]:
            text_lines.append("Current cart total: " + cart_summary["total"])
    has_product_grid = not resolved_blocks or any(block["type"] == "product_grid" for block in resolved_blocks)
    if category == "cart_reminder" and has_product_grid:
        text_lines.extend(["", "Your cart:"])
        for item in products:
            detail = item["name"] + (f" — {item['variant_label']}" if item.get("variant_label") else "")
            text_lines.append(f"{detail} | Quantity {item.get('quantity', 1)} | {item.get('price', '')} each | Line total {item.get('line_total', item.get('price', ''))}")
            text_lines.append(item["url"])
        if cart_summary and cart_summary.get("total"):
            text_lines.append("Current cart total: " + cart_summary["total"])
    elif has_product_grid:
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
