from urllib.parse import quote
from .formatting import money

LOCAL_CONTACT_NUMBER = "0791305329"


def clean_phone(number):
    digits = "".join(ch for ch in (number or "") if ch.isdigit())
    if digits.startswith("0") and len(digits) == 10:
        return "254" + digits[1:]
    return digits


def whatsapp_text_link(settings, message):
    phone = clean_phone(settings.whatsapp_number or LOCAL_CONTACT_NUMBER)
    if not phone:
        return ""
    return f"https://wa.me/{phone}?text={quote(message)}"


def build_product_message(product, settings, page_url="", intent="confirm_price"):
    if intent == "buy":
        opening = "I want to buy this item from FityStores."
    elif product.price_cents is None:
        opening = "I want to confirm the price of this item from FityStores."
    else:
        opening = "I want to confirm availability for this item from FityStores."
    lines = [
        opening,
        "",
        f"Product: {product.name}",
        f"Category: {product.category.name}",
        f"Listed price: {money(product.price_cents, settings.currency)}",
        f"Listed stock: {product.display_stock_quantity}",
    ]
    if page_url:
        lines.append(f"Product page: {page_url}")
    lines.extend([
        "",
        "Please confirm the final price, available colours/sizes, payment details, and collection or delivery options.",
    ])
    return "\n".join(lines)


def product_whatsapp_link(product, settings, page_url="", intent="confirm_price"):
    return whatsapp_text_link(settings, build_product_message(product, settings, page_url, intent))


def build_general_message(settings):
    return "\n".join([
        "I want to ask about FityStores products.",
        "",
        "Please help me confirm prices, availability, payment details, and collection or delivery options.",
    ])


def build_mamba_suggestion_message(product_name, category, preferences="", price_range="", details=""):
    lines = [
        "Hello FityStores! I would like to suggest a product for the Mamba Collection.",
        "",
        f"Product I would like to see: {product_name}",
        f"Category: {category}",
        f"Preferred colour, size, or style: {preferences or 'Not specified'}",
        f"Suggested price range: {price_range or 'Not specified'}",
    ]
    if details:
        lines.extend(["", f"Additional details: {details}"])
    lines.extend(["", "Please consider adding this item to your collection. Thank you!"])
    return "\n".join(lines)


def build_order_message(order, settings):
    lines = [
        "I want to buy these items from FityStores.",
        f"Order enquiry reference: {order.reference}",
        "",
        "I have chosen:",
    ]
    for item in order.items:
        price = money(item.price_cents, settings.currency)
        subtotal = money(item.subtotal_cents, settings.currency) if item.subtotal_cents is not None else "To be confirmed"
        variant_parts = [part for part in [item.size, item.color] if part]
        variant = f" ({', '.join(variant_parts)})" if variant_parts else ""
        lines.append(f"- {item.product_name}{variant} x {item.quantity}: {price} each, subtotal {subtotal}")
    lines.extend([
        "",
        f"Estimated total: {money(order.total_cents, settings.currency) if order.total_cents is not None else 'Some prices to be confirmed'}",
        f"Fulfilment: {order.fulfilment_method}",
        f"Customer name: {order.customer_name or 'Not provided'}",
        f"Customer phone: {order.customer_phone or 'Not provided'}",
        f"Instructions: {order.instructions or 'None'}",
        f"Payment number shown on site: {settings.payment_number or 'Not configured'}",
        f"Payment reference/code: {order.payment_reference or 'Not provided'}",
        "",
        "Please confirm who I am chatting with, item availability, final total, payment status, and collection/delivery details.",
    ])
    return "\n".join(lines)


def whatsapp_link(order, settings):
    return whatsapp_text_link(settings, build_order_message(order, settings))

