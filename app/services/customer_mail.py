from __future__ import annotations

import smtplib
from datetime import datetime
from email.message import EmailMessage as MimeMessage
from email.utils import formataddr

from flask import current_app, render_template

from app.extensions import db
from app.models import EmailMessage


def mail_configuration_status() -> tuple[bool, str]:
    cfg = current_app.config
    if cfg.get("MAIL_PROVIDER", "").lower() != "smtp":
        return False, "Email sending is not configured."
    if not cfg.get("MAIL_FROM_VERIFIED"):
        return False, "Sender address is not marked verified with the email provider."
    if not cfg.get("MAIL_FROM_ADDRESS") or not cfg.get("MAIL_SERVER"):
        return False, "Sender address and SMTP server are required."
    if bool(cfg.get("MAIL_USERNAME")) != bool(cfg.get("MAIL_PASSWORD")):
        return False, "Provide both SMTP username and password, or leave both blank."
    return True, "SMTP sender is configured."


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
    with smtplib.SMTP(cfg["MAIL_SERVER"], cfg.get("MAIL_PORT", 587), timeout=12) as smtp:
        if cfg.get("MAIL_USE_TLS", True):
            smtp.starttls()
        if cfg.get("MAIL_USERNAME"):
            smtp.login(cfg["MAIL_USERNAME"], cfg["MAIL_PASSWORD"])
        smtp.send_message(message)


def queue_email(recipient: str, subject: str, kind: str, idempotency_key: str, *, account_id=None, order_id=None, campaign_id=None, html_body: str, text_body: str) -> EmailMessage:
    record = EmailMessage.query.filter_by(idempotency_key=idempotency_key).first()
    if record:
        return record
    record = EmailMessage(
        recipient=recipient[:254], subject=subject[:200], kind=kind[:32],
        idempotency_key=idempotency_key[:160], account_id=account_id,
        order_id=order_id, campaign_id=campaign_id, status="queued",
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
        return False
    if record.status != "queued":
        return record.status == "sent"
    record.status = "sending"
    db.session.commit()
    try:
        _send_smtp(record.recipient, record.subject, html_body, text_body)
    except Exception as exc:
        code = getattr(exc, "smtp_code", None)
        record.status = "failed"
        record.failure_reason = (f"SMTP error {code}." if code else f"SMTP send failed ({type(exc).__name__}).")
        db.session.commit()
        return False
    record.status = "sent"
    record.failure_reason = ""
    record.sent_at = datetime.utcnow()
    db.session.commit()
    return True


def render_branded_email(heading: str, paragraphs: list[str], *, button_url: str = "", button_text: str = "", unsubscribe_url: str = "") -> tuple[str, str]:
    html_body = render_template("emails/base.html", heading=heading, paragraphs=paragraphs, button_url=button_url, button_text=button_text, unsubscribe_url=unsubscribe_url)
    lines = [heading, "", *paragraphs]
    if button_url:
        lines.extend(["", f"{button_text}: {button_url}"])
    if unsubscribe_url:
        lines.extend(["", f"Unsubscribe from promotional email: {unsubscribe_url}"])
    lines.extend(["", "FityStores", "Bei Fity, Fits Poa."])
    return html_body, "\n".join(lines)


def queue_branded_email(recipient: str, subject: str, kind: str, idempotency_key: str, heading: str, paragraphs: list[str], *, account_id=None, order_id=None, campaign_id=None, button_url="", button_text="", unsubscribe_url="") -> EmailMessage:
    html_body, text_body = render_branded_email(heading, paragraphs, button_url=button_url, button_text=button_text, unsubscribe_url=unsubscribe_url)
    return queue_email(recipient, subject, kind, idempotency_key, account_id=account_id, order_id=order_id, campaign_id=campaign_id, html_body=html_body, text_body=text_body)


def notify_order_event(order, event_name: str, idempotency_key: str):
    from app.models import CustomerOrderLink
    from app.services.customer_mail import queue_branded_email
    link = CustomerOrderLink.query.filter_by(order_id=order.id).first()
    if not link or not link.account.active or not link.account.email_verified_at:
        return None
    if event_name == "payment_confirmed" and order.payment_status != "Paid":
        return None
    if event_name == "order_received":
        subject = "We received your FityStores order"
        heading = "We received your order"
        detail = "Your order request is awaiting seller review. A payment reference or screenshot is not payment confirmation."
    elif event_name == "payment_confirmed":
        subject = "FityStores payment confirmed"
        heading = "Payment confirmed"
        detail = "FityStores has recorded the payment as confirmed for this order."
    else:
        subject = f"FityStores order update: {order.status}"
        heading = "Your order status was updated"
        detail = f"The current order status is {order.status}."
    return queue_branded_email(
        link.account.email, subject, event_name, idempotency_key,
        heading, [f"Order reference: {order.reference}.", detail],
        account_id=link.account.id, order_id=order.id,
        button_url="https://fitystore.top/account/",
        button_text="View order status",
    )
