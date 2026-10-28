from __future__ import annotations

import json
import logging
import re
import smtplib
import ssl
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage as MimeMessage
from email.utils import formataddr, parseaddr
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import current_app, render_template, url_for

from app.extensions import db
from app.models import CustomerAccount, EmailMessage

logger = logging.getLogger(__name__)


def _email_public_base_url() -> str:
    candidate = (current_app.config.get("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    parsed = urlsplit(candidate)
    if (parsed.scheme == "https" and parsed.netloc and not parsed.username and not parsed.password
            and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment):
        return candidate
    return "https://fitystore.top"

def mail_configuration_status() -> tuple[bool, str]:
    cfg = current_app.config
    provider = (cfg.get("MAIL_PROVIDER") or "").strip().lower()
    if provider not in {"smtp", "resend", "brevo"}:
        return False, "Email sending is not configured (set MAIL_PROVIDER to smtp, resend, or brevo)."
    if not cfg.get("MAIL_FROM_VERIFIED"):
        return False, "Sender address is not marked verified with the email provider."
    sender = (cfg.get("MAIL_FROM_ADDRESS") or "").strip()
    if not sender or parseaddr(sender)[1] != sender or "@" not in sender:
        return False, "A valid provider-verified MAIL_FROM_ADDRESS is required."
    if provider == "resend":
        if not (cfg.get("RESEND_API_KEY") or "").strip():
            return False, "RESEND_API_KEY is required when MAIL_PROVIDER=resend."
        return True, "Resend HTTPS API sender is configured."
    if provider == "brevo":
        if not (cfg.get("BREVO_API_KEY") or "").strip():
            return False, "BREVO_API_KEY is required when MAIL_PROVIDER=brevo."
        return True, "Brevo HTTPS API sender is configured."
    if not (cfg.get("MAIL_SERVER") or "").strip():
        return False, "MAIL_SERVER is required."
    try:
        port = int(cfg.get("MAIL_PORT", 587))
        if not 1 <= port <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        return False, "MAIL_PORT must be a number from 1 to 65535."
    use_tls = bool(cfg.get("MAIL_USE_TLS", True))
    use_ssl = bool(cfg.get("MAIL_USE_SSL", False))
    if use_tls and use_ssl:
        return False, "Choose either STARTTLS or implicit TLS, not both."
    if not use_tls and not use_ssl:
        return False, "SMTP must use TLS (MAIL_USE_TLS=1) or implicit TLS (MAIL_USE_SSL=1)."
    if bool(cfg.get("MAIL_USERNAME")) != bool(cfg.get("MAIL_PASSWORD")):
        return False, "Provide both SMTP username and password, or leave both blank."
    return True, "SMTP sender is configured with encrypted transport."


def _send_smtp(recipient: str, subject: str, html_body: str, text_body: str) -> None:
    cfg = current_app.config
    message = MimeMessage()
    message["Subject"] = (subject or "FityStores update").replace("\r", " ").replace("\n", " ")
    message["From"] = formataddr((cfg.get("MAIL_FROM_NAME") or "FityStores", cfg["MAIL_FROM_ADDRESS"]))
    message["To"] = recipient
    if cfg.get("MAIL_REPLY_TO"):
        message["Reply-To"] = cfg["MAIL_REPLY_TO"]
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")
    timeout = 12
    if cfg.get("MAIL_USE_SSL", False):
        with smtplib.SMTP_SSL(
            cfg["MAIL_SERVER"], int(cfg.get("MAIL_PORT", 465)), timeout=timeout,
            context=ssl.create_default_context(),
        ) as smtp:
            if cfg.get("MAIL_USERNAME"):
                smtp.login(cfg["MAIL_USERNAME"], cfg["MAIL_PASSWORD"])
            smtp.send_message(message)
        return
    with smtplib.SMTP(cfg["MAIL_SERVER"], int(cfg.get("MAIL_PORT", 587)), timeout=timeout) as smtp:
        smtp.ehlo()
        if cfg.get("MAIL_USE_TLS", True):
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
        if cfg.get("MAIL_USERNAME"):
            smtp.login(cfg["MAIL_USERNAME"], cfg["MAIL_PASSWORD"])
        smtp.send_message(message)



def _send_resend(recipient: str, subject: str, html_body: str, text_body: str) -> None:
    cfg = current_app.config
    payload = {
        "from": formataddr((cfg.get("MAIL_FROM_NAME") or "FityStores", cfg["MAIL_FROM_ADDRESS"])),
        "to": [recipient],
        "subject": (subject or "FityStores update").replace("\r", " ").replace("\n", " "),
        "html": html_body,
        "text": text_body,
    }
    if cfg.get("MAIL_REPLY_TO"):
        payload["reply_to"] = cfg["MAIL_REPLY_TO"]
    request = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {cfg['RESEND_API_KEY'].strip()}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError(f"Resend API returned HTTP {response.status}.")


def _send_brevo(recipient: str, subject: str, html_body: str, text_body: str) -> str | None:
    cfg = current_app.config
    payload = {
        "sender": {
            "name": cfg.get("MAIL_FROM_NAME") or "FityStores",
            "email": cfg["MAIL_FROM_ADDRESS"],
        },
        "to": [{"email": recipient}],
        "subject": (subject or "FityStores update").replace("\r", " ").replace("\n", " "),
        "htmlContent": html_body,
        "textContent": text_body,
    }
    if cfg.get("MAIL_REPLY_TO"):
        payload["replyTo"] = {"email": cfg["MAIL_REPLY_TO"]}
    request = urllib.request.Request(
        "https://api.brevo.com/v3/smtp/email",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "api-key": cfg["BREVO_API_KEY"].strip(),
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError(f"Brevo API returned HTTP {response.status}.")
        try:
            result = json.loads(response.read().decode("utf-8"))
        except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        message_id = result.get("messageId") if isinstance(result, dict) else None
        return str(message_id)[:255] if message_id else None


def queue_email(recipient: str, subject: str, kind: str, idempotency_key: str, *, account_id=None, order_id=None, campaign_id=None, template_id=None, bypass_marketing_cooldown: bool = False, html_body: str, text_body: str) -> EmailMessage:
    record = EmailMessage.query.filter_by(idempotency_key=idempotency_key).first()
    marketing = kind == "marketing_campaign" or kind.startswith("marketing_")
    if marketing and account_id:
        account = db.session.get(CustomerAccount, account_id)
        allowed = bool(account and account.active and account.marketing_status == "subscribed")
        suppression_reason = "Recipient is no longer eligible for marketing email."
        if allowed and not bypass_marketing_cooldown and (not record or record.status != "sent"):
            recent = EmailMessage.query.filter(
                EmailMessage.account_id == account.id,
                EmailMessage.kind.in_(("marketing_campaign", "marketing_daily_goodies", "marketing_weekly_picks", "marketing_abandoned_cart", "marketing_review_highlight")),
                EmailMessage.status.in_(("sent", "delivered", "bounced", "complained")),
                EmailMessage.sent_at >= datetime.utcnow() - timedelta(hours=24),
            ).first()
            if recent:
                allowed = False
                suppression_reason = "A marketing email was accepted for this recipient within the last 24 hours."
        if not allowed:
            if record and record.status not in {"sent", "sending"}:
                record.status = "suppressed"
                record.failure_reason = suppression_reason
                db.session.commit()
                return record
            if not record:
                record = EmailMessage(recipient=recipient[:254],subject=subject[:200],kind=kind[:32],idempotency_key=idempotency_key[:160],account_id=account_id,order_id=order_id,campaign_id=campaign_id,template_id=template_id,status="suppressed",failure_reason=suppression_reason)
                db.session.add(record);db.session.commit()
            return record
    if record:
        if record.status == "not_configured":
            configured, _reason = mail_configuration_status()
            if configured:
                record.status = "queued";record.failure_reason = "";db.session.commit()
                deliver_email(record, html_body, text_body)
            return record
        retryable = ("HTTP 429" in record.failure_reason or any(f"HTTP {code}" in record.failure_reason for code in (500,502,503,504)) or any(f"SMTP error {code}" in record.failure_reason for code in (400,421,450,451,452)))
        retry_delay = timedelta(minutes=min(240, 15 * (2 ** max(0, record.retry_count))))
        if record.status == "failed" and retryable and record.retry_count < 3 and record.updated_at <= datetime.utcnow() - retry_delay:
            record.status = "queued";record.failure_reason = "";record.retry_count += 1;db.session.commit()
            deliver_email(record, html_body, text_body)
        return record
    record = EmailMessage(
        recipient=recipient[:254], subject=subject[:200], kind=kind[:32],
        idempotency_key=idempotency_key[:160], account_id=account_id,
        order_id=order_id, campaign_id=campaign_id, template_id=template_id, status="queued",
    )
    db.session.add(record)
    db.session.commit()
    deliver_email(record, html_body, text_body)
    return record


def deliver_email(record: EmailMessage, html_body: str, text_body: str) -> bool:
    configured, reason = mail_configuration_status()
    if not configured:
        record.status = "not_configured"
        record.failure_reason = reason[:255]
        db.session.commit()
        logger.warning("email_delivery_not_configured message_id=%s reason=%s", record.id, record.failure_reason)
        return False
    if record.status != "queued":
        return record.status == "sent"
    record.status = "sending"
    db.session.commit()
    try:
        provider = (current_app.config.get("MAIL_PROVIDER") or "").strip().lower()
        if provider == "resend":
            _send_resend(record.recipient, record.subject, html_body, text_body)
        elif provider == "brevo":
            record.provider_message_id = _send_brevo(record.recipient, record.subject, html_body, text_body)
        else:
            _send_smtp(record.recipient, record.subject, html_body, text_body)
    except Exception as exc:
        code = getattr(exc, "smtp_code", None)
        provider = (current_app.config.get("MAIL_PROVIDER") or "smtp").strip().lower()
        http_status = getattr(exc, "code", None) if provider in {"resend", "brevo"} else None
        record.status = "failed"
        if provider == "smtp" and code:
            record.failure_reason = f"SMTP error {code}."
        elif provider in {"resend", "brevo"} and isinstance(http_status, int):
            record.failure_reason = f"{provider.title()} HTTP {http_status}."
        else:
            provider_name = "SMTP" if provider == "smtp" else provider.title()
            record.failure_reason = f"{provider_name} send failed ({type(exc).__name__})."
        db.session.commit()
        logger.warning(
            "email_delivery_failed message_id=%s provider=%s error_type=%s smtp_code=%s http_status=%s",
            record.id, provider, type(exc).__name__, code if isinstance(code, int) else "none",
            http_status if isinstance(http_status, int) else "none",
        )
        return False
    record.status = "sent"
    record.failure_reason = ""
    record.sent_at = datetime.utcnow()
    db.session.commit()
    return True


def render_branded_email(heading: str, paragraphs: list[str], *, button_url: str = "", button_text: str = "", unsubscribe_url: str = "", kind: str = "", order_reference: str = "", order_progress=None) -> tuple[str, str]:
    paragraphs = [
        block.strip()
        for paragraph in paragraphs
        for block in paragraph.replace("\r\n", "\n").split("\n\n")
        if block.strip()
    ]
    security_code = ""
    if kind in {"verify_code", "login_code"}:
        for paragraph in paragraphs:
            match = re.search(r"(?<!\d)\d{6}(?!\d)", paragraph)
            if match:
                security_code = match.group(0)
                break
        if security_code:
            paragraphs = [paragraph for paragraph in paragraphs if security_code not in paragraph]
    preview_text = (paragraphs[0] if paragraphs else heading).strip()[:180]
    site_url = _email_public_base_url()
    logo_url = site_url + "/static/images/mainLogo-160.png"
    contact_url = site_url + url_for("public.contact", _external=False)
    accent_color = "#0B8F52" if kind == "payment_confirmed" else "#ED1B2F"
    hero_background = "#0B8F52" if kind == "payment_confirmed" else "#111111"
    footer_notes = {
        "welcome": "Thanks for joining FITY. We’re glad you’re here.",
        "account_security": "If you did not make this change, contact FityStores support immediately.",
        "unsubscribe_confirmation": "Your promotional email preference has been updated. Essential account and order messages may still be sent.",
        "verify_email": "This secure link is personal to you. Never forward it or share a verification code.",
        "reset_password": "If you did not request a password reset, you can ignore this email and keep your current password.",
        "verify_code": "For your security, never share this code with anyone.",
        "login_code": "For your security, never share this code with anyone.",
    }
    html_body = render_template(
        "emails/base_branded.html", heading=heading, paragraphs=paragraphs,
        preheader=preview_text, button_url=button_url, button_text=button_text,
        unsubscribe_url=unsubscribe_url, site_url=site_url, logo_url=logo_url,
        contact_url=contact_url, footer_note=footer_notes.get(kind, ""),
        security_code=security_code, accent_color=accent_color,
        hero_background=hero_background,
        hero_text_color="#FFFFFF", order_reference=order_reference,
        order_progress=order_progress or (),
    )
    lines = [heading, "", *paragraphs]
    if button_url:
        lines.extend(["", f"{button_text}: {button_url}"])
    if order_reference:
        lines.extend(["", f"Order reference: {order_reference}"])
    if order_progress:
        lines.extend(["", "Order progress:"] + [f"{stage['label']}: {stage['description']}" for stage in order_progress])
    if unsubscribe_url:
        lines.extend(["", f"Unsubscribe from promotional email: {unsubscribe_url}"])
    lines.extend(["", "FityStores", "Bei Fity, Fits Poa."])
    return html_body, "\n".join(lines)


def queue_branded_email(recipient: str, subject: str, kind: str, idempotency_key: str, heading: str, paragraphs: list[str], *, account_id=None, order_id=None, campaign_id=None, button_url="", button_text="", unsubscribe_url="", order_reference="", order_progress=None) -> EmailMessage:
    html_body, text_body = render_branded_email(
        heading, paragraphs, button_url=button_url, button_text=button_text,
        unsubscribe_url=unsubscribe_url, kind=kind, order_reference=order_reference,
        order_progress=order_progress,
    )
    return queue_email(recipient, subject, kind, idempotency_key, account_id=account_id, order_id=order_id, campaign_id=campaign_id, html_body=html_body, text_body=text_body)


def _order_progress_stages(order):
    fulfillment = order.fulfilment_method or "To be confirmed"
    fulfillment_stage = "Ready for collection" if fulfillment.lower() == "collection" else "Out for delivery"
    reached_processing = order.status in {"Payment confirmed", "Preparing order", "Ready for collection", "Out for delivery", "Completed"}
    reached_fulfillment = order.status in {fulfillment_stage, "Completed"}
    stages = [
        {"label": "Order received", "description": "We’ve received your order request.", "state": "current" if order.status == "Order received" else "done"},
        {"label": "Processing", "description": "The store will review the order and update its status.", "state": "current" if reached_processing and order.status != "Completed" and not reached_fulfillment else "done" if reached_processing else "upcoming"},
        {"label": fulfillment_stage, "description": "We’ll let you know when it is ready.", "state": "current" if reached_fulfillment and order.status != "Completed" else "done" if reached_fulfillment else "upcoming"},
        {"label": "Completed", "description": "Enjoy your FITY items.", "state": "done" if order.status == "Completed" else "upcoming"},
    ]
    if order.status == "Cancelled":
        return [
            {"label": "Order received", "description": "We received the order request.", "state": "done"},
            {"label": "Cancelled", "description": "The store cancelled this order.", "state": "current"},
        ]
    return stages


def _render_order_received_email(order, account):
    from app.services.formatting import money
    from app.services.product_images import product_display_image, product_image_alt, product_image_url, valid_product_image
    from app.services.settings import current_settings

    settings = current_settings()
    base_url = _email_public_base_url()
    tracking_path = url_for("public.track_order_link", reference=order.reference, tracking_token=order.tracking_token, _external=False)
    tracking_url = base_url + tracking_path

    rows = []
    subtotal_cents = 0
    subtotal_known = True
    for item in order.items:
        line_total = item.subtotal_cents
        if line_total is None and item.price_cents is not None:
            line_total = item.price_cents * item.quantity
        if line_total is None:
            subtotal_known = False
        else:
            subtotal_cents += line_total
        image_url = ""
        image_alt = f"Product image: {item.product_name}"
        image = item.variant.image if item.variant and valid_product_image(item.variant.image) else None
        if image is None and item.product:
            image = product_display_image(item.product)
        if image:
            candidate = product_image_url(image)
            image_url = base_url + candidate if candidate.startswith("/") else candidate
            if not image_url.startswith("https://"):
                image_url = ""
            image_alt = product_image_alt(image, image_alt)
        variant_label = (item.variant_label or "").strip()
        if not variant_label:
            variant_label = " · ".join(
                f"{label}: {value}"
                for label, value in (("Colour", item.color), ("Size", item.size))
                if value
            )
        rows.append({
            "name": item.product_name,
            "variant": variant_label,
            "sku": (item.sku or (item.product.sku if item.product else "") or "").strip(),
            "image_url": image_url,
            "image_alt": image_alt,
            "quantity": item.quantity,
            "unit_price": money(item.price_cents, settings.currency),
            "line_total": money(line_total, settings.currency),
        })

    discount_cents = max(0, order.discount_cents or 0)
    delivery_cents = max(0, order.delivery_fee_cents or 0)
    total_cents = order.total_cents

    try:
        local_zone = ZoneInfo(current_app.config.get("TIMEZONE", "Africa/Nairobi"))
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        local_zone = ZoneInfo("Africa/Nairobi")
    created_at = order.created_at
    if created_at:
        created_at = (created_at.replace(tzinfo=timezone.utc) if created_at.tzinfo is None else created_at).astimezone(local_zone)

    payment_attempts = list(order.payment_attempts or [])
    payment_method = payment_attempts[0].payment_method if payment_attempts else "Manual M-Pesa"
    fulfillment = order.fulfilment_method or "To be confirmed"
    delivery_details = "The store will confirm the delivery details with you."
    if fulfillment.lower() == "collection":
        delivery_details = settings.collection_instructions or "The store will contact you with collection instructions."
    else:
        for instruction_line in (order.instructions or "").splitlines():
            label, separator, value = instruction_line.partition(":")
            if separator and label.strip().lower() == "delivery address" and value.strip():
                delivery_details = value.strip()
                break

    from app.services.whatsapp import clean_phone
    raw_phone = settings.whatsapp_number or ""
    phone_digits = clean_phone(raw_phone) if not any(char.isalpha() for char in raw_phone) else ""
    if 9 <= len(phone_digits) <= 15 and not phone_digits.startswith("0"):
        support_url = f"https://wa.me/{phone_digits}?text=" + quote(f"Hello FITY, I need help with order {order.reference}.", safe="")
        support_label = "Need help? WhatsApp"
    else:
        support_url = base_url + url_for("public.contact", _external=False)
        support_label = "Need help? Contact us"
    customer_name = (order.customer_name or account.display_name or "Customer").strip()

    payment_status = order.payment_status or "Awaiting payment"
    if payment_status == "Paid":
        payment_guidance = "Payment has been confirmed by the store."
    elif payment_status == "Payment reported":
        payment_guidance = "We received your payment reference or proof. The seller still needs to verify it; a reference or screenshot alone is not payment confirmation. Please do not pay again until you have checked with the seller."
    elif payment_status == "Awaiting payment":
        if "m-pesa" in (payment_method or "").lower():
            payment_guidance = (settings.payment_instructions or "Contact the store to confirm payment instructions before paying.") + " A submitted reference or screenshot is reviewed by the seller and does not itself confirm payment."
        else:
            payment_guidance = "Payment is still pending. The seller will confirm the next step for this order."
    elif payment_status.lower() in {"not required", "no payment required"}:
        payment_guidance = "No payment is required for this order at this time."
    else:
        payment_guidance = f"Payment status: {payment_status}. Contact the store if you need clarification."

    if fulfillment.lower() == "collection":
        delivery_heading = "Collection Information"
        if order.status == "Ready for collection":
            fulfillment_guidance = "Your order is ready for collection. Follow the instructions below and confirm the meeting point with the store before travelling."
        elif order.status == "Completed":
            fulfillment_guidance = "This order has been marked completed."
        elif order.status == "Cancelled":
            fulfillment_guidance = "This order has been cancelled. Contact the store if you need help."
        else:
            fulfillment_guidance = "Collection is not ready yet. Wait for the store to confirm that it is ready before travelling."
    else:
        delivery_heading = "Delivery Information"
        if order.status == "Out for delivery":
            fulfillment_guidance = "Your order is out for delivery. Keep your phone available for delivery coordination."
        elif order.status == "Completed":
            fulfillment_guidance = "This order has been marked completed."
        elif order.status == "Cancelled":
            fulfillment_guidance = "This order has been cancelled. Contact the store if you need help."
        else:
            fulfillment_guidance = "Delivery arrangements are awaiting confirmation from the store. No delivery date has been promised yet."

    stages = _order_progress_stages(order)

    context = {
        "order": order,
        "customer_name": customer_name,
        "customer_email": account.email,
        "customer_phone": order.customer_phone or "Not provided",
        "items": rows,
        "line_count": len(rows),
        "item_count": sum(max(0, item.quantity or 0) for item in order.items),
        "subtotal": money(subtotal_cents, settings.currency) if subtotal_known else "To be confirmed",
        "discount": money(discount_cents, settings.currency),
        "delivery_fee": money(delivery_cents, settings.currency),
        "total": money(total_cents, settings.currency),
        "show_discount": discount_cents > 0,
        "show_delivery": delivery_cents > 0,
        "payment_method": payment_method,
        "payment_status": payment_status,
        "payment_guidance": payment_guidance,
        "paid": payment_status == "Paid",
        "fulfillment": fulfillment,
        "delivery_heading": delivery_heading,
        "delivery_details": delivery_details,
        "fulfillment_guidance": fulfillment_guidance,
        "created_at": created_at,
        "tracking_url": tracking_url,
        "support_url": support_url,
        "support_label": support_label,
        "stages": stages,
        "preheader": f"We received order {order.reference}. See your items, totals, and next steps.",
        "logo_url": base_url + "/static/images/mainLogo-160.png",
        "site_url": base_url,
    }
    html_body = render_template("emails/order_received.html", **context)
    text_lines = [
        "Order Received!", f"Thank you for shopping with FITY, {customer_name}.",
        f"Order reference: {order.reference}", "", "YOUR ORDER",
    ]
    for item in rows:
        item_description = item["name"]
        if item["variant"]:
            item_description += " — " + item["variant"]
        if item["sku"]:
            item_description += " — SKU: " + item["sku"]
        text_lines.append(f"{item_description} — {item['quantity']} × {item['unit_price']} = {item['line_total']}")
    text_lines.extend([
        "", f"Subtotal: {context['subtotal']}",
    ])
    if context["show_discount"]:
        text_lines.append(f"Discount: -{context['discount']}")
    if context["show_delivery"]:
        text_lines.append(f"Delivery fee: {context['delivery_fee']}")
    text_lines.extend([
        f"{'Total paid' if context['paid'] else 'Total order amount'}: {context['total']}",
        f"Payment: {context['payment_method']} — {context['payment_status']}",
        payment_guidance,
        f"{delivery_heading}: {fulfillment} — {delivery_details}",
        fulfillment_guidance,
        f"Current order status: {order.status}", f"View order status: {tracking_url}",
        "", "FityStores | Bei Fity, Fits Poa.", base_url,
    ])
    return html_body, "\n".join(text_lines)


def notify_order_event(order, event_name: str, idempotency_key: str):
    from sqlalchemy.orm import selectinload
    from app.models import CustomerOrderLink, Order, OrderItem, Product, ProductVariant
    from app.services.customer_mail import queue_branded_email
    link = CustomerOrderLink.query.filter_by(order_id=order.id).first()
    if not link or not link.account.active or not link.account.email_verified_at:
        return None
    if event_name == "payment_confirmed" and order.payment_status != "Paid":
        return None
    payment_method = "Not recorded"
    if event_name == "order_received":
        order = Order.query.options(
            selectinload(Order.items).selectinload(OrderItem.product).selectinload(Product.images),
            selectinload(Order.items).selectinload(OrderItem.variant).selectinload(ProductVariant.image),
            selectinload(Order.payment_attempts),
        ).filter_by(id=order.id).first() or order
        link = CustomerOrderLink.query.filter_by(order_id=order.id).first() or link
        subject = f"Order received: {order.reference} | FITYStores"
        html_body, text_body = _render_order_received_email(order, link.account)
        return queue_email(
            link.account.email, subject, event_name, idempotency_key,
            account_id=link.account.id, order_id=order.id,
            html_body=html_body, text_body=text_body,
        )
    elif event_name == "payment_confirmed":
        subject = "FityStores payment confirmed"
        heading = "Payment confirmed"
        payment_attempt = next(iter(order.payment_attempts or []), None)
        payment_method = payment_attempt.payment_method if payment_attempt else "Not recorded"
        from app.services.settings import current_settings
        from app.services.formatting import money
        detail = f"Payment for order {order.reference} has been verified by FityStores. Confirmed amount: {money(order.total_cents, current_settings().currency)}. Payment method: {payment_method}. Payment status: {order.payment_status}. The store will now continue with {('collection preparation' if (order.fulfilment_method or '').lower() == 'collection' else 'delivery preparation')}."
    elif event_name == "review_request":
        if order.status != "Completed" or order.payment_status != "Paid":
            return None
        subject = f"How was your FityStores order? | {order.reference}"
        heading = "How was your order?"
        detail = "Your order has been marked completed. If you would like, share an honest review of your experience. Reviews are optional and are not tied to a reward."
        return queue_branded_email(
            link.account.email, subject, event_name, idempotency_key,
            heading, [f"Order reference: {order.reference}.", detail],
            account_id=link.account.id, order_id=order.id,
            button_url=(_email_public_base_url() + url_for("public.reviews", _external=False)),
            button_text="Write a review",
        )
    elif event_name == "order_status":
        payment_attempt = next(iter(order.payment_attempts or []), None)
        payment_method = payment_attempt.payment_method if payment_attempt else "Not recorded"
        status_copy = {
            "Order received": ("We received your order", "Your order request is waiting for seller review."),
            "Awaiting payment": ("Payment is pending", "The order is awaiting payment. Follow the payment instructions confirmed by the store; a reference or screenshot alone does not confirm payment."),
            "Payment confirmed": ("Order update: payment confirmed", "The store recorded this order at the payment-confirmed stage."),
            "Preparing order": ("Your order is being prepared", "The store has started preparing your order."),
            "Ready for collection": ("Your order is ready for collection", "Your order is ready. Follow the collection instructions and confirm the meeting point before travelling."),
            "Out for delivery": ("Your order is out for delivery", "Your order is out for delivery. Keep your phone available for delivery coordination."),
            "Completed": ("Your order is complete", "The store has marked this order completed."),
            "Cancelled": ("Your order was cancelled", "The store has marked this order cancelled. Contact us if you need help."),
        }
        heading, detail = status_copy.get(order.status, ("Your order status was updated", f"The current order status is {order.status}."))
        subject = f"{heading}: {order.reference} | FITYStores"
        if order.status == "Payment confirmed" and order.payment_status != "Paid":
            heading = "Order status updated"
            detail = f"The order status is Payment confirmed, while the recorded payment status remains {order.payment_status}. Contact the store if you need clarification."
    else:
        subject = f"FityStores order update: {order.status}"
        heading = "Your order status was updated"
        detail = f"The current order status is {order.status}."
    if event_name == "order_status" and order.status == "Awaiting payment":
        from app.services.settings import current_settings
        configured_instructions = current_settings().payment_instructions
        if configured_instructions and "m-pesa" in payment_method.lower():
            detail += " " + configured_instructions
    base_url = _email_public_base_url()
    tracking_url = base_url + url_for("public.track_order_link", reference=order.reference, tracking_token=order.tracking_token, _external=False)
    return queue_branded_email(
        link.account.email, subject, event_name, idempotency_key,
        heading, [
            f"Order reference: {order.reference}.", detail,
            f"Payment method: {payment_method}. Payment status: {order.payment_status or 'Not recorded'}.",
            f"Fulfilment method: {order.fulfilment_method or 'To be confirmed'}.",
        ],
        account_id=link.account.id, order_id=order.id,
        button_url=tracking_url,
        button_text="View order status",
        order_reference=order.reference,
        order_progress=_order_progress_stages(order),
    )
