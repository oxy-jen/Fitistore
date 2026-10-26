from __future__ import annotations

import json
import logging
import smtplib
import ssl
import urllib.request
from datetime import datetime, timedelta
from email.message import EmailMessage as MimeMessage
from email.utils import formataddr, parseaddr

from flask import current_app, render_template

from app.extensions import db
from app.models import CustomerAccount, EmailMessage

logger = logging.getLogger(__name__)

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


def queue_email(recipient: str, subject: str, kind: str, idempotency_key: str, *, account_id=None, order_id=None, campaign_id=None, html_body: str, text_body: str) -> EmailMessage:
    record = EmailMessage.query.filter_by(idempotency_key=idempotency_key).first()
    marketing = kind == "marketing_campaign" or kind.startswith("marketing_")
    if marketing and account_id:
        account = db.session.get(CustomerAccount, account_id)
        allowed = bool(account and account.active and account.email_verified_at and account.marketing_status == "subscribed")
        if allowed and (not record or record.status != "sent"):
            recent = EmailMessage.query.filter(
                EmailMessage.account_id == account.id,
                EmailMessage.kind.in_(("marketing_campaign", "marketing_daily_goodies", "marketing_weekly_picks", "marketing_abandoned_cart", "marketing_review_highlight")),
                EmailMessage.status.in_(("sent", "delivered", "bounced", "complained")),
                EmailMessage.sent_at >= datetime.utcnow() - timedelta(hours=24),
            ).first()
            if recent:
                allowed = False
        if not allowed:
            if record and record.status not in {"sent", "sending"}:
                record.status = "suppressed"
                record.failure_reason = "Recipient is no longer eligible for marketing email."
                db.session.commit()
                return record
            if not record:
                record = EmailMessage(recipient=recipient[:254],subject=subject[:200],kind=kind[:32],idempotency_key=idempotency_key[:160],account_id=account_id,order_id=order_id,campaign_id=campaign_id,status="suppressed",failure_reason="Recipient is no longer eligible for marketing email.")
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


def render_branded_email(heading: str, paragraphs: list[str], *, button_url: str = "", button_text: str = "", unsubscribe_url: str = "") -> tuple[str, str]:
    paragraphs = [
        block.strip()
        for paragraph in paragraphs
        for block in paragraph.replace("\r\n", "\n").split("\n\n")
        if block.strip()
    ]
    preview_text = (paragraphs[0] if paragraphs else heading).strip()[:180]
    html_body = render_template("emails/base.html", heading=heading, paragraphs=paragraphs, preheader=preview_text, button_url=button_url, button_text=button_text, unsubscribe_url=unsubscribe_url)
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
