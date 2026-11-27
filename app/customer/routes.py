from __future__ import annotations

import hashlib
import hmac
import logging
import re
from threading import Lock
from time import monotonic
from datetime import datetime, timedelta
from functools import wraps
from secrets import randbelow, token_urlsafe
from urllib.parse import urlsplit

from email_validator import EmailNotValidError, validate_email
from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user, login_user, logout_user
from werkzeug.security import generate_password_hash
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy import inspect, select

from app.extensions import db, csrf
from app.models import AuthRateLimit, CustomerAccount, CustomerConsentEvent, CustomerOrderLink, CustomerToken, EmailMessage, MarketingSuppressionCase, Order, Product, Review
from app.services.customer_mail import (
    cleanup_expired_authentication_payloads, dispatch_authentication_email_now,
    dispatch_transactional_email_now, mail_configuration_status, queue_branded_email,
)
from app.services.product_images import product_display_image, product_image_alt, product_image_url

customer_bp = Blueprint("customer", __name__, url_prefix="/account")
CONSENT_TEXT_VERSION = "2026-10-explicit-registration-opt-in"
REGISTRATION_CONSENT_TEXT_VERSION = "2026-11-email-first-marketing-consent-v1"
WELCOME_INVITATION_CONSENT_VERSION = "2026-11-welcome-invitation-email-marketing-v1"
TERMS_ACCEPTANCE_VERSION = "2026-11-customer-terms-acknowledgement-v2"
WELCOME_OPT_IN_PURPOSE = "welcome_opt_in"
logger = logging.getLogger(__name__)
_AUTH_PAYLOAD_CLEANUP_LOCK = Lock()
_AUTH_PAYLOAD_CLEANUP_INTERVAL_SECONDS = 60
_last_auth_payload_cleanup = 0.0


@customer_bp.before_app_request
def _cleanup_stale_authentication_email_payloads():
    """Opportunistically erase expired code bodies even with queue workers off."""
    global _last_auth_payload_cleanup
    now = monotonic()
    if now - _last_auth_payload_cleanup < _AUTH_PAYLOAD_CLEANUP_INTERVAL_SECONDS:
        return None
    with _AUTH_PAYLOAD_CLEANUP_LOCK:
        now = monotonic()
        if now - _last_auth_payload_cleanup < _AUTH_PAYLOAD_CLEANUP_INTERVAL_SECONDS:
            return None
        try:
            removed = cleanup_expired_authentication_payloads()
            if removed:
                logger.info("expired_authentication_email_payloads_cleared count=%s", removed)
            _last_auth_payload_cleanup = now
        except Exception as exc:
            db.session.rollback()
            logger.warning(
                "expired_authentication_email_payload_cleanup_failed error_type=%s",
                type(exc).__name__,
            )
            # Cleanup is best effort and must not block customer account routes.
            _last_auth_payload_cleanup = now
    return None


def _login_customer(account):
    """Keep a customer signed in across page navigation and browser restarts."""
    login_user(account, remember=True)
    session.permanent = True


def _has_current_terms_acceptance(account):
    return CustomerConsentEvent.query.filter_by(
        account_id=account.id, event="terms_accepted",
        wording_version=TERMS_ACCEPTANCE_VERSION,
    ).first() is not None


def _login_and_continue(account, next_url=None):
    """Authenticate uniformly, then require current terms for every sign-in method."""
    _login_customer(account)
    next_url = _safe_customer_next(next_url)
    if not _has_current_terms_acceptance(account):
        session["customer_terms_next"] = next_url
        return redirect(url_for("customer.accept_terms"))
    return redirect(next_url)


def _public_url(endpoint, **values):
    path = url_for(endpoint, _external=False, **values)
    base = (current_app.config.get("PUBLIC_BASE_URL") or "https://fitystore.top").strip().rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme.lower() != "https" or not parsed.netloc or parsed.path or parsed.query or parsed.fragment:
        logger.error("PUBLIC_BASE_URL must be an HTTPS origin; using the canonical FityStores host.")
        base = "https://fitystore.top"
    return f"{base}{path}"


def _delivery_message(result, label="verification"):
    status = getattr(result, "status", "")
    kind = getattr(result, "kind", "")
    if kind in {"verify_code", "login_code"} and status == "failed":
        category = getattr(result, "error_category", "")
        if category == "auth_rate_limited":
            retry_at = getattr(result, "next_attempt_at", None)
            if retry_at:
                seconds = max(0, int((retry_at - datetime.utcnow()).total_seconds() + 0.999))
                if 0 < seconds <= 15 * 60:
                    minutes = max(1, (seconds + 59) // 60)
                    return (
                        "Email delivery is temporarily rate-limited. Please wait about "
                        f"{minutes} minute{'s' if minutes != 1 else ''}, then request a fresh code. "
                        "We will not resend this code automatically."
                    )
                if seconds > 15 * 60:
                    return (
                        "Email delivery is rate-limited for now. Please wait until the provider's "
                        "cooldown has passed, then request a fresh code. We will not resend this code automatically."
                    )
            return (
                "Email delivery is temporarily rate-limited. Please wait a little while, "
                "then request a fresh code. We will not resend this code automatically."
            )
        if category == "auth_delivery_unavailable":
            return "We couldn't send your code because email delivery is unavailable. Please try again later."
        if category == "ambiguous_outcome":
            return (
                "We couldn't confirm whether the email provider accepted your code. "
                "Wait a few minutes for the email before requesting another code."
            )
    if status == "queued":
        if kind in {"verify_code", "login_code"}:
            return (
                f"Your {label} email is pending delivery. It can be sent only while the code is valid; "
                "if it does not arrive soon, request a fresh code."
            )
        return f"Your {label} email is in the delivery queue. It will be sent when email delivery is available."
    if status == "sent":
        return (
            f"The provider accepted your {label} email. Inbox delivery is not confirmed; "
            "check your inbox and spam folder before requesting another."
        )
    if status == "delivered":
        return f"The provider confirmed delivery of your {label} email. Check your inbox and spam folder."
    if status == "not_configured":
        return f"We can’t send your {label} email right now. Please try again later."
    if status == "failed":
        return f"We couldn’t send your {label} email. Please try again in a moment."
    return f"We couldn’t prepare your {label} email. Please try again shortly."


def customer_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for("customer.login", next=request.path))
        if not isinstance(current_user._get_current_object(), CustomerAccount):
            flash("Sign in with a customer account to view this page.", "warning")
            return redirect(url_for("customer.login", next=request.path))
        if not current_user.active:
            logout_user()
            flash("This customer account is unavailable. Contact FityStores for help.", "error")
            return redirect(url_for("customer.login"))
        if request.endpoint not in {"customer.accept_terms", "customer.logout"} and not _has_current_terms_acceptance(current_user._get_current_object()):
            session["customer_terms_next"] = _safe_customer_next(request.full_path)
            return redirect(url_for("customer.accept_terms"))
        return view(*args, **kwargs)
    return wrapped


def _safe_customer_next(value):
    parsed = urlsplit((value or "").strip())
    allowed_path = parsed.path in {"/account", "/account/", "/referrals", "/checkout"}
    if parsed.scheme or parsed.netloc or not allowed_path or parsed.path.startswith("//") or "\\" in parsed.path:
        return url_for("customer.dashboard")
    return parsed.path + (("?" + parsed.query) if parsed.query else "")


def normalize_email(value):
    result = validate_email((value or "").strip(), check_deliverability=False)
    return result.normalized.lower()


def _rate_key(action, value=""):
    material = f"{action}:{request.remote_addr or 'unknown'}:{value}".encode()
    return hmac.new(current_app.secret_key.encode(), material, hashlib.sha256).hexdigest()


def _rate_allowed(action, value="", limit=8, window_minutes=15):
    now = datetime.utcnow()
    key = _rate_key(action, value)
    row = AuthRateLimit.query.get(key)
    if row and now - row.window_started_at < timedelta(minutes=window_minutes):
        if row.attempts >= limit:
            return False
        row.attempts += 1
    else:
        if row:
            db.session.delete(row)
        row = AuthRateLimit(key=key, window_started_at=now, attempts=1)
        db.session.add(row)
    db.session.commit()
    return True


def _find_token(raw, purpose):
    if not raw or len(raw) > 200:
        return None
    digest = hashlib.sha256(raw.encode()).hexdigest()
    row = CustomerToken.query.filter_by(token_hash=digest, purpose=purpose, used_at=None).first()
    if not row or row.expires_at <= datetime.utcnow() or not row.account.active:
        return None
    return row


def _send_token_email(account, purpose, raw, next_url=None, *, commit=True):
    if purpose == "verify_email":
        path = "customer.verify_email"
        subject = "Verify your FityStores email"
        heading = "Verify your email address"
        text = "This link verifies your FityStores email address and expires in 24 hours. If you did not create this account, you can ignore this email."
        button_text = "Verify email address"
        minutes = 24 * 60
    elif purpose == "reset_password":
        path = "customer.reset_password"
        subject = "Reset your FityStores password"
        heading = "Reset your password"
        text = "Use this one-time link to choose a new password; it expires in 30 minutes. If you did not request a reset, ignore this email; your password has not changed."
        button_text = "Choose a new password"
        minutes = 30
    else:
        path = "customer.confirm_marketing"
        subject = "Confirm FityStores email offers"
        heading = "Confirm promotional email preferences"
        text = "Confirm only if you want occasional FityStores offers and new-arrival emails. You will not be subscribed unless you confirm."
        button_text = "Confirm email preferences"
        minutes = 24 * 60
    url_values = {"token": raw}
    if purpose == "verify_email" and next_url:
        url_values["next"] = _safe_customer_next(next_url)
    url = _public_url(path, **url_values)
    event_key = hashlib.sha256(raw.encode()).hexdigest()[:24]
    return queue_branded_email(
        account.email, subject, purpose, f"{purpose}:{event_key}", heading, [text], account_id=account.id,
        button_url=url, button_text=button_text, commit=commit,
    )


def _issue_and_send(account, purpose, minutes=None, next_url=None):
    raw = token_urlsafe(32)
    now = datetime.utcnow()
    CustomerToken.query.filter_by(account_id=account.id, purpose=purpose).filter(
        CustomerToken.used_at.is_(None),
    ).update({"used_at": now}, synchronize_session="fetch")
    token_row = CustomerToken(
        account_id=account.id,
        purpose=purpose,
        token_hash=hashlib.sha256(raw.encode()).hexdigest(),
        expires_at=datetime.utcnow() + timedelta(minutes=minutes or (30 if purpose == "reset_password" else 24 * 60)),
    )
    db.session.add(token_row)
    try:
        db.session.flush()
        result = _send_token_email(account, purpose, raw, next_url=next_url, commit=False)
        db.session.commit()
    except Exception:
        db.session.rollback()
        # A queue/render failure must never make an earlier credential valid again.
        CustomerToken.query.filter_by(account_id=account.id, purpose=purpose).filter(
            CustomerToken.used_at.is_(None),
        ).update({"used_at": datetime.utcnow()}, synchronize_session="fetch")
        db.session.commit()
        raise
    from app.services.customer_mail import dispatch_transactional_email_now
    return dispatch_transactional_email_now(result)


def _start_email_code(account, purpose):
    """Issue a short-lived, hashed one-time code for verification or optional 2FA."""
    if purpose not in {"verify_code", "login_code"} or not account or not account.active:
        raise ValueError("A valid active account and authentication-code purpose are required.")
    code = f"{randbelow(1_000_000):06d}"
    now = datetime.utcnow()
    heading = "Verify your FityStores account" if purpose == "verify_code" else "Your FityStores sign-in code"
    subject = "Your FityStores verification code" if purpose == "verify_code" else "Your FityStores sign-in code"
    code_copy = "Enter this code on FityStores within 10 minutes. Never share it with anyone."
    try:
        # Challenge replacement and the delivery claim serialize on this row.
        # A prior challenge cannot be dispatched after this transaction wins.
        locked_account = db.session.execute(
            select(CustomerAccount).where(CustomerAccount.id == account.id).with_for_update()
        ).scalar_one_or_none()
        if not locked_account or not locked_account.active:
            raise ValueError("The customer account is unavailable.")
        CustomerToken.query.filter_by(account_id=account.id, purpose=purpose).filter(
            CustomerToken.used_at.is_(None),
        ).update({"used_at": now}, synchronize_session="fetch")
        row = CustomerToken(
            account_id=account.id, purpose=purpose,
            token_hash=hashlib.sha256(code.encode()).hexdigest(),
            expires_at=now + timedelta(minutes=10),
        )
        db.session.add(row)
        db.session.flush()
        result = queue_branded_email(
            account.email, subject, purpose, f"{purpose}:{row.id}", heading,
            [code_copy], account_id=account.id, security_code=code, commit=False,
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        CustomerToken.query.filter_by(account_id=account.id, purpose=purpose).filter(
            CustomerToken.used_at.is_(None),
        ).update({"used_at": datetime.utcnow()}, synchronize_session="fetch")
        db.session.commit()
        raise
    # The outbox commit happens before provider I/O. This uses the normal
    # allowlist, atomic claim, recipient checks and provider failure handling;
    # when mail is not configured, the queued row remains for the worker.
    return dispatch_authentication_email_now(result, account_id=account.id, purpose=purpose)


def _begin_code_challenge(
    account, purpose, next_url=None, *, accepted_terms_version=None,
    marketing_opt_in=None, new_customer=False,
):
    result = _start_email_code(account, purpose)
    if not result or result.status not in {"sent", "queued"}:
        session.pop("pending_customer_id", None)
        session.pop("pending_customer_purpose", None)
        session.pop("pending_customer_next", None)
        session.pop("pending_customer_terms_version", None)
        session.pop("pending_customer_marketing_opt_in", None)
        session.pop("pending_customer_new", None)
        flash(_delivery_message(result, "verification code"), "warning")
        return redirect(url_for("customer.login", next=_safe_customer_next(next_url)))

    session["pending_customer_id"] = account.id
    session["pending_customer_purpose"] = purpose
    session["pending_customer_next"] = _safe_customer_next(next_url)
    if accepted_terms_version:
        session["pending_customer_terms_version"] = accepted_terms_version
        session["pending_customer_marketing_opt_in"] = bool(marketing_opt_in)
        session["pending_customer_new"] = bool(new_customer)
    else:
        session.pop("pending_customer_terms_version", None)
        session.pop("pending_customer_marketing_opt_in", None)
        session.pop("pending_customer_new", None)
    session.permanent = True
    flash(_delivery_message(result, "verification code"), "success")
    return redirect(url_for("customer.verify_code"))


def _record_consent(account, event, method, wording_version=CONSENT_TEXT_VERSION):
    now = datetime.utcnow()
    db.session.add(CustomerConsentEvent(
        account_id=account.id, event=event, method=method,
        wording_version=wording_version, created_at=now, updated_at=now,
    ))
    if event == "confirmed":
        account.marketing_status = "subscribed"
        account.marketing_consent_at = now
        account.marketing_withdrawn_at = None
    elif event == "requested":
        account.marketing_status = "pending"
    elif event == "withdrawn":
        account.marketing_status = "unsubscribed"
        account.marketing_withdrawn_at = datetime.utcnow()
        CustomerToken.query.filter(
            CustomerToken.account_id == account.id,
            CustomerToken.purpose.in_(("marketing_confirm", WELCOME_OPT_IN_PURPOSE)),
            CustomerToken.used_at.is_(None),
        ).update({"used_at": now}, synchronize_session="fetch")


def _record_terms_acceptance(account, method):
    db.session.add(CustomerConsentEvent(
        account_id=account.id, event="terms_accepted", method=method,
        wording_version=TERMS_ACCEPTANCE_VERSION,
    ))


def _marketing_suppression_state(account):
    if account.marketing_status == "suppressed":
        return True
    try:
        inspector = inspect(db.engine)
        if not inspector.has_table(MarketingSuppressionCase.__tablename__):
            logger.error("customer_marketing_opt_in_suppression_schema_unavailable")
            return None
        columns = {column["name"] for column in inspector.get_columns(MarketingSuppressionCase.__tablename__)}
        if not {"account_id", "cleared_at"} <= columns:
            logger.error("customer_marketing_opt_in_suppression_schema_incomplete")
            return None
        return db.session.execute(
            select(MarketingSuppressionCase.id).where(
                MarketingSuppressionCase.account_id == account.id,
                MarketingSuppressionCase.cleared_at.is_(None),
            ).limit(1)
        ).first() is not None
    except SQLAlchemyError as exc:
        db.session.rollback()
        logger.error(
            "customer_marketing_opt_in_suppression_check_failed account_id=%s error_type=%s",
            account.id, type(exc).__name__,
        )
        return None


def _apply_marketing_opt_in(account, method):
    if account.marketing_status == "subscribed" and account.marketing_withdrawn_at is None:
        return
    suppression_state = _marketing_suppression_state(account)
    if suppression_state is None:
        flash("Your account is ready, but we couldn't save the newsletter choice. You can update it later.", "warning")
        return
    if suppression_state:
        flash("Your account is ready, but this address can't receive promotional email.", "warning")
        return
    _record_consent(
        account, "confirmed", method, REGISTRATION_CONSENT_TEXT_VERSION,
    )
    db.session.commit()


def _send_registration_welcome_email(account):
    idempotency_key = f"welcome:{account.id}"
    existing = EmailMessage.query.filter_by(idempotency_key=idempotency_key).first()
    if existing:
        if existing.kind != "welcome" or existing.purpose != "transactional" or existing.account_id != account.id:
            raise ValueError("Registration welcome idempotency key is bound to another message.")
        result = dispatch_transactional_email_now(existing)
    else:
        consent_active = (
            account.marketing_status == "subscribed"
            and account.marketing_consent_at is not None
            and account.marketing_withdrawn_at is None
        )
        invitation_url = ""
        if not consent_active and account.marketing_status != "suppressed":
            raw_token = token_urlsafe(32)
            db.session.add(CustomerToken(
                account_id=account.id,
                purpose=WELCOME_OPT_IN_PURPOSE,
                token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
                expires_at=datetime.utcnow() + timedelta(hours=24),
            ))
            invitation_url = _public_url("customer.confirm_marketing", token=raw_token)

        if consent_active:
            heading = "Welcome to the FityStores promotions community"
            paragraphs = [
                f"Congratulations, {account.display_name or 'there'}! You're now part of the FityStores promotions community.",
                "You've successfully opted in to receive updates about our latest arrivals, special offers, discounts and exciting product selections.",
                "Stay connected and never miss what's new at FityStores.",
            ]
            button_url = _public_url("public.shop")
            button_text = "Explore the shop"
        elif invitation_url:
            heading = f"Welcome to FityStores, {account.display_name or 'there'}"
            paragraphs = [
                f"Hi {account.display_name or 'there'}, your FityStores account was created successfully.",
                "Would you like to hear about our latest offers?",
                "Join the FityStores promotions community and receive updates about new arrivals, special offers, discounts and exciting products.",
                "If you'd like to receive these updates, you can subscribe below. This invitation link expires in 24 hours; your email preference changes only after you confirm on our website.",
            ]
            button_url = invitation_url
            button_text = "Yes, I'd love to receive FityStores offers."
        else:
            heading = f"Welcome to FityStores, {account.display_name or 'there'}"
            paragraphs = [
                f"Hi {account.display_name or 'there'}, your FityStores account was created successfully.",
                "You can verify your email, view linked orders, and manage your account preferences on our website.",
            ]
            button_url = _public_url("public.shop")
            button_text = "Explore the shop"

        result = queue_branded_email(
            account.email, "Welcome to FityStores", "welcome", idempotency_key,
            heading, paragraphs, account_id=account.id, button_url=button_url,
            button_text=button_text, commit=False,
        )
        db.session.commit()
        result = dispatch_transactional_email_now(result)

    logger.info(
        "registration_welcome_email_outcome account_id=%s status=%s error_category=%s",
        account.id, getattr(result, "status", "unknown"),
        getattr(result, "error_category", None) or "none",
    )
    return result


@customer_bp.route("/terms-acceptance", methods=["GET", "POST"])
def accept_terms():
    account = current_user._get_current_object() if current_user.is_authenticated else None
    if not isinstance(account, CustomerAccount) or not account.active:
        return redirect(url_for("customer.login", next=_safe_customer_next(request.values.get("next"))))
    next_url = _safe_customer_next(request.values.get("next") or session.get("customer_terms_next"))
    if _has_current_terms_acceptance(account):
        session.pop("customer_terms_next", None)
        return redirect(next_url)
    if request.method == "POST":
        if request.form.get("accept_terms") != "yes":
            flash("Please confirm that you have read and agree to the Terms and Conditions and Privacy Notice.", "error")
        else:
            _record_terms_acceptance(account, "post_login_checkbox")
            db.session.commit()
            session.pop("customer_terms_next", None)
            flash("Thanks. Your terms acknowledgement has been saved.", "success")
            return redirect(next_url)
    return render_template("customer/accept_terms.html", title="Review FityStores terms", next_url=next_url)


def verify_google_credential(credential):
    """Verify a Google-signed ID token with Google's maintained auth library."""
    if not current_app.config.get("GOOGLE_CLIENT_ID"):
        return None
    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token
        claims = id_token.verify_oauth2_token(credential, google_requests.Request(), current_app.config["GOOGLE_CLIENT_ID"])
    except Exception as exc:
        # Keep tokens and provider response bodies out of logs; type is enough to diagnose audience/network/library failures.
        logger.warning("Google ID token verification failed error_type=%s", type(exc).__name__)
        return None
    if claims.get("iss") not in {"accounts.google.com", "https://accounts.google.com"} or not claims.get("email_verified"):
        return None
    try:
        email = normalize_email(claims.get("email"))
    except (EmailNotValidError, AttributeError, TypeError):
        return None
    return {"sub": claims.get("sub", ""), "email": email, "name": (claims.get("name") or "")[:120], "email_verified": bool(claims.get("email_verified")), "hd": claims.get("hd", "")}


def _google_email_authoritative(claims):
    domain = claims["email"].rsplit("@", 1)[-1]
    return bool(claims.get("email_verified")) and (domain == "gmail.com" or bool(claims.get("hd")))


@customer_bp.route("/register", methods=["GET", "POST"])
def register():
    next_url = _safe_customer_next(request.values.get("next"))
    if request.method == "POST":
        flash("Use the email sign-in form to create or access your account.", "info")
    return redirect(url_for("customer.login", next=next_url))


def _start_email_code_login():
    email_input = request.form.get("email", "").strip()
    next_url = _safe_customer_next(request.form.get("next"))
    if request.form.get("accept_terms") != "yes":
        flash("Please accept the updated Terms and Conditions to continue.", "error")
        return redirect(url_for("customer.login", next=next_url))
    if not _rate_allowed("email_code_request", email_input.lower()[:254], limit=4, window_minutes=15):
        flash("Too many code requests. Please wait before trying again.", "warning")
        return redirect(url_for("customer.login", next=next_url))
    try:
        email = normalize_email(email_input)
    except (EmailNotValidError, AttributeError, TypeError):
        flash("Enter a valid email address.", "error")
        return redirect(url_for("customer.login", next=next_url))

    account = CustomerAccount.query.filter_by(email=email).first()
    created_account = False
    if account and not account.active:
        flash("If this address can sign in, we'll send a verification code.", "info")
        return redirect(url_for("customer.login", next=next_url))
    if not account:
        account = CustomerAccount(email=email, display_name="")
        db.session.add(account)
        try:
            db.session.flush()
            created_account = True
        except IntegrityError:
            db.session.rollback()
            account = CustomerAccount.query.filter_by(email=email).first()
            if not account or not account.active:
                flash("If this address can sign in, we'll send a verification code.", "info")
                return redirect(url_for("customer.login", next=next_url))
    new_customer = created_account or (
        not account.email_verified_at
        and not _has_current_terms_acceptance(account)
        and not EmailMessage.query.filter_by(
            idempotency_key=f"welcome:{account.id}",
        ).first()
    )

    purpose = "verify_code" if not account.email_verified_at else "login_code"
    return _begin_code_challenge(
        account, purpose, next_url,
        accepted_terms_version=TERMS_ACCEPTANCE_VERSION,
        marketing_opt_in=request.form.get("marketing_opt_in") == "yes",
        new_customer=new_customer,
    )


@customer_bp.route("/login", methods=["GET", "POST"])
def login():
    next_url = _safe_customer_next(request.values.get("next"))
    if current_user.is_authenticated and isinstance(current_user._get_current_object(), CustomerAccount):
        if not _has_current_terms_acceptance(current_user._get_current_object()):
            session["customer_terms_next"] = next_url
            return redirect(url_for("customer.accept_terms"))
        return redirect(next_url)
    if request.method == "POST":
        return _start_email_code_login()
    return render_template(
        "customer/login.html",
        title="Sign in to your FityStores account",
        google_client_id=current_app.config.get("GOOGLE_CLIENT_ID", ""),
        google_login_uri=_public_url("customer.google_signin"),
        next_url=next_url,
    )


@customer_bp.post("/email-code")
def email_code_login():
    return _start_email_code_login()


@customer_bp.route("/verify-code", methods=["GET", "POST"])
def verify_code():
    account_id = session.get("pending_customer_id")
    purpose = session.get("pending_customer_purpose")
    if not account_id or purpose not in {"verify_code", "login_code"}:
        flash("Start sign-in again to request a fresh code.", "warning")
        return redirect(url_for("customer.login"))
    account = db.session.get(CustomerAccount, account_id)
    if not account or not account.active:
        session.pop("pending_customer_id", None)
        session.pop("pending_customer_purpose", None)
        session.pop("pending_customer_next", None)
        session.pop("pending_customer_terms_version", None)
        session.pop("pending_customer_marketing_opt_in", None)
        session.pop("pending_customer_new", None)
        flash("This customer account is unavailable. Contact FityStores for help.", "error")
        return redirect(url_for("customer.login"))
    if request.method == "POST":
        if not _rate_allowed("email_code", str(account.id), limit=6, window_minutes=15):
            flash("Too many code attempts. Wait 15 minutes before trying again.", "warning")
        else:
            code = request.form.get("code", "").strip()
            if not code:
                code = "".join(request.form.getlist("code_digit")).strip()
            if not re.fullmatch(r"[0-9]{6}", code):
                flash("Enter the six-digit code from your email.", "otp-error")
            else:
                digest = hashlib.sha256(code.encode()).hexdigest()
                row = CustomerToken.query.filter_by(account_id=account.id, purpose=purpose, token_hash=digest, used_at=None).first()
                if not row or row.expires_at <= datetime.utcnow():
                    flash("That code is incorrect, expired, or already used. Request another code.", "otp-error")
                else:
                    now = datetime.utcnow()
                    newly_verified = purpose == "verify_code" and account.email_verified_at is None
                    accepted_terms_version = session.get("pending_customer_terms_version")
                    marketing_opt_in = bool(session.get("pending_customer_marketing_opt_in"))
                    new_customer = bool(session.get("pending_customer_new"))
                    row.used_at = now
                    if purpose == "verify_code":
                        account.email_verified_at = account.email_verified_at or now
                        CustomerToken.query.filter(
                            CustomerToken.account_id == account.id,
                            CustomerToken.purpose == "verify_email",
                            CustomerToken.used_at.is_(None),
                        ).update({"used_at": now}, synchronize_session="fetch")
                    if (
                        accepted_terms_version == TERMS_ACCEPTANCE_VERSION
                        and not _has_current_terms_acceptance(account)
                    ):
                        _record_terms_acceptance(
                            account,
                            "email_code_signup" if new_customer else "email_code_signin",
                        )
                    if new_customer and not marketing_opt_in:
                        _record_consent(
                            account, "declined", "email_code_signup",
                            REGISTRATION_CONSENT_TEXT_VERSION,
                        )
                    db.session.commit()
                    next_url = session.pop("pending_customer_next", None)
                    session.pop("pending_customer_id", None)
                    session.pop("pending_customer_purpose", None)
                    session.pop("pending_customer_terms_version", None)
                    session.pop("pending_customer_marketing_opt_in", None)
                    session.pop("pending_customer_new", None)
                    if marketing_opt_in:
                        _apply_marketing_opt_in(account, "email_code_optin")
                    if newly_verified and not EmailMessage.query.filter_by(
                        idempotency_key=f"welcome:{account.id}",
                    ).first():
                        try:
                            _send_registration_welcome_email(account)
                        except Exception:
                            db.session.rollback()
                            logger.exception("registration_welcome_email_failed account_id=%s", account.id)
                    response = _login_and_continue(account, next_url)
                    flash("Your email is verified." if purpose == "verify_code" else "You are signed in.", "otp-success")
                    return response
        db.session.commit()
    return render_template(
        "customer/verify_code.html",
        title="Verify your email" if purpose == "verify_code" else "Enter your sign-in code",
        email=account.email,
        is_verification=(purpose == "verify_code"),
    )


@customer_bp.post("/verify-code/resend")
def resend_email_code():
    account_id = session.get("pending_customer_id")
    purpose = session.get("pending_customer_purpose")
    account = db.session.get(CustomerAccount, account_id) if account_id else None
    if not account or not account.active or purpose not in {"verify_code", "login_code"}:
        return redirect(url_for("customer.login"))
    if not _rate_allowed("email_code_resend", str(account.id), limit=3, window_minutes=15):
        flash("Too many code requests. Wait 15 minutes before requesting another.", "warning")
    else:
        result = _start_email_code(account, purpose)
        flash(_delivery_message(result, "verification code"), "success" if result and result.status == "sent" else "warning")
    return redirect(url_for("customer.verify_code"))


@customer_bp.post("/two-factor")
@customer_required
def set_two_factor():
    if not current_user.email_verified_at:
        flash("Verify your email before changing two-step sign-in.", "warning")
        return redirect(url_for("customer.dashboard"))
    enabled = request.form.get("enabled") == "yes"
    if enabled and not current_user.email:
        flash("A verified email address is required for two-step sign-in.", "error")
    elif enabled and not mail_configuration_status()[0]:
        flash("Email verification is temporarily unavailable, so two-step sign-in cannot be enabled right now.", "error")
    else:
        current_user.two_factor_enabled = enabled
        db.session.commit()
        flash("Two-step sign-in enabled. A code will be emailed at each sign-in." if enabled else "Two-step sign-in disabled.", "success")
    return redirect(url_for("customer.dashboard"))


@customer_bp.post("/logout")
@customer_required
def logout():
    logout_user()
    flash("You have signed out.", "success")
    return redirect(url_for("customer.login"))


@customer_bp.route("/")
@customer_required
def dashboard():
    links = CustomerOrderLink.query.filter_by(account_id=current_user.id).order_by(CustomerOrderLink.created_at.desc()).limit(20).all()
    activity = []

    def add_activity(occurred_at, title, detail, kind="account", href=None):
        if occurred_at:
            activity.append({
                "occurred_at": occurred_at,
                "title": title,
                "detail": detail,
                "kind": kind,
                "href": href,
            })

    add_activity(current_user.created_at, "Account created", "Your FityStores customer account was created.")
    add_activity(current_user.email_verified_at, "Email verified", "Your email address was verified.")
    for link in links:
        order = link.order
        tracking_url = url_for(
            "public.track_order",
            reference=order.reference,
            tracking_token=order.tracking_token,
        )
        add_activity(
            order.created_at,
            "Order placed",
            f"You placed order {order.reference}.",
            "order",
            tracking_url,
        )
        for event in order.history:
            if event.field == "status":
                add_activity(
                    event.created_at,
                    "Order status updated",
                    f"Order {order.reference}: {event.new_value}.",
                    "order",
                    tracking_url,
                )
            elif event.field == "payment_status":
                add_activity(
                    event.created_at,
                    "Payment status updated",
                    f"Order {order.reference}: {event.new_value}.",
                    "payment",
                    tracking_url,
                )

    reviews = Review.query.filter_by(customer_account_id=current_user.id).order_by(
        Review.created_at.desc(), Review.id.desc()
    ).limit(10).all()
    for review in reviews:
        product_name = review.product.name if review.product else "FityStores"
        add_activity(
            review.created_at,
            "Review submitted",
            f"Your review for {product_name} is {review.status}.",
            "review",
            url_for("public.reviews"),
        )

    consent_events = CustomerConsentEvent.query.filter_by(account_id=current_user.id).order_by(
        CustomerConsentEvent.created_at.desc(), CustomerConsentEvent.id.desc()
    ).limit(10).all()
    consent_copy = {
        "requested": ("Promotional email requested", "You requested promotional email confirmation."),
        "confirmed": ("Promotional email confirmed", "You confirmed promotional email."),
        "withdrawn": ("Promotional email stopped", "You withdrew promotional email consent."),
    }
    for event in consent_events:
        title, detail = consent_copy.get(event.event, ("Email preference updated", "Your email preference changed."))
        add_activity(event.created_at, title, detail, "preference")

    activity.sort(key=lambda item: item["occurred_at"], reverse=True)
    activity = activity[:15]
    fresh_arrivals = Product.query.filter_by(active=True).order_by(Product.created_at.desc(), Product.id.desc()).limit(6).all()
    fresh_ids = {product.id for product in fresh_arrivals}
    discounted_products = Product.query.filter(
        Product.active.is_(True),
        Product.previous_price_cents.isnot(None),
        Product.price_cents.isnot(None),
        Product.previous_price_cents > Product.price_cents,
        Product.id.notin_(fresh_ids),
    ).order_by(Product.updated_at.desc(), Product.id.desc()).limit(6).all()
    return render_template("customer/dashboard.html", title="Your FityStores account", account=current_user, order_links=links, activity=activity, fresh_arrivals=fresh_arrivals, discounted_products=discounted_products, mail_configured=mail_configuration_status()[0], google_client_id=current_app.config.get("GOOGLE_CLIENT_ID", ""))


@customer_bp.post("/orders/link")
@customer_required
def link_guest_order():
    """Claim an unlinked order only when the customer proves possession of its tracking token."""
    if not current_user.email_verified_at:
        flash("Verify your email before linking an order.", "warning")
        return redirect(url_for("customer.dashboard"))
    reference = request.form.get("reference", "").strip().upper()[:24]
    tracking_token = request.form.get("tracking_token", "").strip()[:96]
    order = Order.query.filter_by(reference=reference, tracking_token=tracking_token).first() if reference and tracking_token else None
    if not order:
        flash("We could not link that order. Check the reference and private tracking token, then try again.", "error")
        return redirect(url_for("customer.dashboard") + "#account-orders")
    existing = CustomerOrderLink.query.filter_by(order_id=order.id).first()
    if existing and existing.account_id != current_user.id:
        # Do not reveal whether another account owns this order.
        flash("We could not link that order. Check the reference and private tracking token, then try again.", "error")
    elif existing:
        flash("That order is already linked to your account.", "info")
    else:
        db.session.add(CustomerOrderLink(account_id=current_user.id, order_id=order.id))
        try:
            db.session.commit()
            flash("Order linked. You can now view it in your account.", "success")
        except IntegrityError:
            db.session.rollback()
            flash("We could not link that order. Check the reference and private tracking token, then try again.", "error")
    return redirect(url_for("customer.dashboard") + "#account-orders")


@customer_bp.get("/recent-products")
@customer_required
def recent_products():
    """Return active catalogue details for the small set of IDs stored in this browser."""
    ids = []
    for raw_id in request.args.getlist("id")[:8]:
        try:
            product_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if product_id > 0 and product_id not in ids:
            ids.append(product_id)
    if not ids:
        return jsonify(products=[])
    found = Product.query.filter(Product.id.in_(ids), Product.active.is_(True)).all()
    by_id = {product.id: product for product in found}
    products = []
    for product_id in ids:
        product = by_id.get(product_id)
        if not product:
            continue
        image = product_display_image(product)
        products.append({
            "id": product.id,
            "name": product.name,
            "url": url_for("public.product_detail", slug=product.slug),
            "image_url": product_image_url(image) if image else "",
            "image_alt": product_image_alt(image, product.name) if image else product.name,
        })
    return jsonify(products=products)


@customer_bp.post("/profile")
@customer_required
def update_profile():
    if not current_user.email_verified_at:
        abort(403)
    name = request.form.get("display_name", "").strip()[:120]
    if not name:
        flash("Enter your name.", "error")
        return redirect(url_for("customer.dashboard"))
    current_user.display_name = name
    db.session.commit()
    flash("Profile updated.", "success")
    return redirect(url_for("customer.dashboard"))


@customer_bp.post("/marketing/subscribe")
@customer_required
def subscribe_marketing():
    if not current_user.email_verified_at:
        flash("Verify your email before requesting promotional messages.", "error")
        return redirect(url_for("customer.dashboard"))
    if current_user.marketing_status == "suppressed":
        flash("This address is suppressed from marketing email. Contact support if you believe this is an error.", "error")
    elif current_user.marketing_status != "subscribed":
        _record_consent(current_user, "requested", "account_preferences")
        db.session.commit()
        result = _issue_and_send(current_user, "marketing_confirm")
        flash(_delivery_message(result, "promotional confirmation"), "success" if result and result.status in {"sent", "queued"} else "warning")
    return redirect(url_for("customer.dashboard"))


@customer_bp.post("/newsletter")
def footer_newsletter_signup():
    email_input = request.form.get("email", "").strip()
    if not _rate_allowed("footer_newsletter", email_input.lower()[:254], limit=4, window_minutes=60):
        flash("If that address is eligible, we will send an email confirmation. Promotional messages stay off until confirmed.", "info")
        return redirect(url_for("public.home"))
    try:
        email = normalize_email(email_input)
    except (EmailNotValidError, AttributeError, TypeError):
        flash("Enter a valid email address to request newsletter confirmation.", "error")
        return redirect(url_for("public.home"))

    account = CustomerAccount.query.filter_by(email=email, active=True).first()
    if account and account.email_verified_at and account.marketing_status not in {"subscribed", "suppressed"}:
        _record_consent(account, "requested", "footer_newsletter")
        db.session.commit()
        try:
            _issue_and_send(account, "marketing_confirm")
        except Exception as exc:
            logger.warning("Footer newsletter confirmation could not be prepared error_type=%s", type(exc).__name__)
    flash("If that address is eligible, we will send an email confirmation. Promotional messages stay off until confirmed. No account yet? Create one and choose the optional email offers preference.", "info")
    return redirect(url_for("public.home"))



@customer_bp.post("/marketing/unsubscribe")
@customer_required
def unsubscribe_account():
    from app.services.marketing_suppressions import lock_marketing_account
    account = lock_marketing_account(current_user.id)
    if account and account.marketing_status == "subscribed":
        _record_consent(account, "withdrawn", "account_preferences")
        db.session.commit()
    flash("You are unsubscribed from promotional email.", "success")
    return redirect(url_for("customer.dashboard"))

@customer_bp.route("/verify/<token>", methods=["GET", "POST"])
def verify_email(token):
    row = _find_token(token, "verify_email")
    next_url = _safe_customer_next(request.values.get("next"))
    if request.method == "GET":
        valid = bool(row and not row.account.email_verified_at)
        heading = "Verify your email"
        if row and row.account.email_verified_at:
            heading = "Email already verified"
        return render_template("customer/confirm_token.html", title=heading, heading=heading, action=url_for("customer.verify_email", token=token, next=next_url), valid=valid)
    if not row:
        flash("That verification link is invalid, expired, or already used. Sign in and request another if your email is not verified.", "error")
        return redirect(url_for("customer.login", next=next_url))
    if row.account.email_verified_at:
        row.used_at = row.used_at or datetime.utcnow()
        db.session.commit()
        flash("Your email is already verified.", "info")
        return redirect(url_for("customer.login", next=next_url))
    verified_at = datetime.utcnow()
    row.used_at = verified_at
    row.account.email_verified_at = row.account.email_verified_at or verified_at
    CustomerToken.query.filter(
        CustomerToken.account_id == row.account_id,
        CustomerToken.purpose == "verify_email",
        CustomerToken.id != row.id,
        CustomerToken.used_at.is_(None),
        CustomerToken.expires_at > verified_at,
    ).update({"used_at": verified_at}, synchronize_session="fetch")
    db.session.commit()
    flash("Your email is verified.", "success")
    return redirect(url_for("customer.login", next=next_url))


@customer_bp.post("/verification/resend")
def resend_verification():
    account = current_user._get_current_object()
    if not isinstance(account, CustomerAccount):
        pending_id = session.get("pending_customer_id")
        if session.get("pending_customer_purpose") != "verify_code" or not pending_id:
            return redirect(url_for("customer.login"))
        account = db.session.get(CustomerAccount, pending_id)
    if not account or not account.active:
        return redirect(url_for("customer.login"))
    if account.email_verified_at:
        flash("Your email is already verified.", "info")
    elif not _rate_allowed("verify", account.email, limit=3, window_minutes=60):
        flash("Too many verification requests. Please wait before trying again.", "warning")
    else:
        result = _issue_and_send(account, "verify_email")
        flash(_delivery_message(result), "success" if result and result.status in {"sent", "queued"} else "warning")
    return redirect(url_for("customer.dashboard") if isinstance(current_user._get_current_object(), CustomerAccount) else url_for("customer.verify_code"))


@customer_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()[:254]
        if _rate_allowed("forgot", email, limit=4, window_minutes=60):
            account = CustomerAccount.query.filter_by(email=email, active=True).first()
            if account and account.email_verified_at and account.password_hash:
                _issue_and_send(account, "reset_password", 30)
        flash("If a verified account uses that address, we'll send password reset instructions when email service is available.", "info")
        return redirect(url_for("customer.login"))
    return render_template("customer/forgot_password.html", title="Reset password")


@customer_bp.route("/reset/<token>", methods=["GET", "POST"])
def reset_password(token):
    row = _find_token(token, "reset_password")
    if request.method == "GET":
        return render_template("customer/reset_password.html", title="Choose a new password", token=token, valid=bool(row))
    if not row:
        flash("That password reset link is invalid or expired.", "error")
        return redirect(url_for("customer.forgot_password"))
    password = request.form.get("password", "")
    if len(password) < 4 or len(password) > 1024 or password != request.form.get("confirm_password", ""):
        flash("Enter matching passwords between 4 and 1024 characters.", "error")
        return render_template("customer/reset_password.html", title="Choose a new password", token=token, valid=True)
    row.used_at = datetime.utcnow()
    row.account.password_hash = generate_password_hash(password)
    db.session.commit()
    from app.services.customer_mail import queue_branded_email
    queue_branded_email(row.account.email, "FityStores password changed", "account_security", f"password-changed:{row.id}", "Your password was changed", ["If you did not make this change, contact FityStores support immediately."], account_id=row.account.id)
    flash("Password changed. Please sign in.", "success")
    return redirect(url_for("customer.login"))


@customer_bp.route("/marketing/confirm/<token>", methods=["GET", "POST"])
def confirm_marketing(token):
    row = (
        _find_token(token, WELCOME_OPT_IN_PURPOSE)
        or _find_token(token, "marketing_confirm")
    )
    if request.method == "GET":
        return render_template(
            "customer/confirm_marketing.html",
            title="FityStores promotional email preferences",
            action=url_for("customer.confirm_marketing", token=token),
            valid=bool(row),
        )
    if not row:
        flash("That confirmation link is invalid or expired.", "error")
        return redirect(url_for("public.home"))
    # Lock account before token, matching suppression ingestion's lock order.
    from app.services.marketing_suppressions import lock_marketing_account
    account = lock_marketing_account(row.account_id)
    row = db.session.execute(
        select(CustomerToken).where(
            CustomerToken.id == row.id,
            CustomerToken.used_at.is_(None),
        ).with_for_update()
    ).scalar_one_or_none()
    if not account or not row or row.expires_at <= datetime.utcnow() or not account.active:
        db.session.rollback()
        flash("That confirmation link is invalid or expired.", "error")
        return redirect(url_for("public.home"))
    if not account.email_verified_at:
        flash(
            "Verify your account email before confirming promotional email, then return to this invitation. "
            "The link remains available for 24 hours.",
            "warning",
        )
        return redirect(url_for("customer.login"))
    suppression_active = account.marketing_status == "suppressed"
    try:
        from sqlalchemy import inspect
        from app.models import MarketingSuppressionCase
        inspector = inspect(db.engine)
        if not inspector.has_table(MarketingSuppressionCase.__tablename__):
            raise RuntimeError("suppression schema is not installed")
        columns = {column["name"] for column in inspector.get_columns(MarketingSuppressionCase.__tablename__)}
        if not {"account_id", "cleared_at"} <= columns:
            raise RuntimeError("suppression schema is incomplete")
        if not suppression_active:
            suppression_active = db.session.execute(
                select(MarketingSuppressionCase.id).where(
                    MarketingSuppressionCase.account_id == account.id,
                    MarketingSuppressionCase.cleared_at.is_(None),
                ).limit(1)
            ).first() is not None
    except Exception as exc:
        db.session.rollback()
        logger.error("marketing_confirmation_suppression_check_failed error_type=%s", type(exc).__name__)
        flash("We could not confirm your email preference right now. Please try again later.", "warning")
        return redirect(url_for("customer.login"))
    if suppression_active:
        row.used_at = datetime.utcnow()
        db.session.commit()
        flash("This address has an active marketing suppression. Contact support if you believe this is an error.", "warning")
        return redirect(url_for("customer.login"))
    if account.marketing_status == "subscribed" and account.marketing_withdrawn_at is None:
        row.used_at = datetime.utcnow()
        db.session.commit()
        flash("Your promotional email preference is already active.", "info")
        return redirect(url_for("customer.login"))
    row.used_at = datetime.utcnow()
    consent_method = (
        "welcome_invitation" if row.purpose == WELCOME_OPT_IN_PURPOSE else "double_opt_in"
    )
    consent_version = (
        WELCOME_INVITATION_CONSENT_VERSION
        if row.purpose == WELCOME_OPT_IN_PURPOSE
        else CONSENT_TEXT_VERSION
    )
    _record_consent(account, "confirmed", consent_method, consent_version)
    db.session.commit()
    flash("Your promotional email preference is confirmed.", "success")
    return redirect(url_for("customer.login"))

@customer_bp.route("/unsubscribe/<token>", methods=["GET", "POST"])
def unsubscribe(token):
    row = _find_token(token, "unsubscribe")
    if request.method == "GET":
        return render_template("customer/confirm_token.html", title="Unsubscribe", heading="Unsubscribe from FityStores offers", action=url_for("customer.unsubscribe", token=token), valid=bool(row))
    if row:
        from app.services.marketing_suppressions import lock_marketing_account
        account = lock_marketing_account(row.account_id)
        row = db.session.execute(
            select(CustomerToken).where(
                CustomerToken.id == row.id, CustomerToken.used_at.is_(None),
                CustomerToken.expires_at > datetime.utcnow(),
            ).with_for_update()
        ).scalar_one_or_none()
        if account and row:
            row.used_at = datetime.utcnow()
            _record_consent(account, "withdrawn", "unsubscribe_link")
            db.session.commit()
            try:
                queued = queue_branded_email(
                    account.email, "FityStores email preferences updated",
                    "unsubscribe_confirmation", f"unsubscribe-link:{row.id}",
                    "Promotional email stopped",
                    ["You have been unsubscribed from promotional FityStores emails. Essential account and order messages may still be sent."],
                    account_id=account.id,
                )
            except Exception as exc:
                db.session.rollback()
                logger.warning(
                    "unsubscribe_confirmation_queue_failed account_id=%s error_type=%s",
                    account.id, type(exc).__name__,
                )
                flash(
                    "Your unsubscribe is complete, but we couldn't queue a confirmation email.",
                    "warning",
                )
            else:
                status = getattr(queued, "status", None)
                if status not in {"queued", "sent", "delivered"}:
                    db.session.rollback()
                    logger.warning(
                        "unsubscribe_confirmation_queue_failed account_id=%s result_status=%s",
                        account.id, status if isinstance(status, str) else "missing",
                    )
                    flash(
                        "Your unsubscribe is complete, but we couldn't queue a confirmation email.",
                        "warning",
                    )
    return render_template("customer/unsubscribed.html", title="Email preferences updated")


@customer_bp.post("/google")
def google_signin():
    next_url = _safe_customer_next(request.form.get("next"))
    if not current_app.config.get("GOOGLE_CLIENT_ID"):
        flash("Google sign-in is temporarily unavailable. Please use email sign-in instead.", "warning")
        return redirect(url_for("customer.login", next=next_url))
    if not _rate_allowed("google_ip", "", limit=20, window_minutes=15):
        flash("Too many Google sign-in attempts. Please wait and try again.", "warning")
        return redirect(url_for("customer.login", next=next_url))
    credential = request.form.get("credential", "")
    if not credential or len(credential) > 10000:
        flash("Google could not verify this sign-in. Please try again.", "error")
        return redirect(url_for("customer.login", next=next_url))
    claims = verify_google_credential(credential)
    if not claims or not claims.get("sub"):
        flash("Google could not verify this sign-in. Please try again or use email sign-in.", "error")
        return redirect(url_for("customer.login", next=next_url))
    if not _google_email_authoritative(claims):
        flash("Use a Google account with a verified Gmail or Google Workspace email address.", "error")
        return redirect(url_for("customer.login", next=next_url))
    if request.form.get("accept_terms") != "yes":
        flash("Please accept the updated Terms and Conditions to continue.", "error")
        return redirect(url_for("customer.login", next=next_url))
    if not _rate_allowed("google", claims["email"], limit=8):
        flash("Too many sign-in attempts for this account. Please wait and try again.", "warning")
        return redirect(url_for("customer.login", next=next_url))
    account = CustomerAccount.query.filter_by(google_sub=claims["sub"]).first()
    if account:
        if not account.active:
            flash("This customer account is unavailable. Contact FityStores for help.", "error")
            return redirect(url_for("customer.login", next=next_url))
        if not _has_current_terms_acceptance(account):
            _record_terms_acceptance(account, "google_signin")
            db.session.commit()
        if not account.email_verified_at:
            return _begin_code_challenge(
                account, "verify_code", next_url,
                accepted_terms_version=TERMS_ACCEPTANCE_VERSION,
                marketing_opt_in=request.form.get("marketing_opt_in") == "yes",
            )
        if account.two_factor_enabled:
            return _begin_code_challenge(
                account, "login_code", next_url,
                accepted_terms_version=TERMS_ACCEPTANCE_VERSION,
                marketing_opt_in=request.form.get("marketing_opt_in") == "yes",
            )
        if request.form.get("marketing_opt_in") == "yes":
            _apply_marketing_opt_in(account, "google_optin")
        return _login_and_continue(account, next_url)
    if CustomerAccount.query.filter_by(email=claims["email"]).first():
        flash("An account already uses this email. Sign in with its existing method, then link Google from your account.", "warning")
        return redirect(url_for("customer.login", next=next_url))
    account = CustomerAccount(
        email=claims["email"], display_name=claims.get("name", ""),
        google_sub=claims["sub"], email_verified_at=datetime.utcnow(),
    )
    db.session.add(account)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        flash("A FityStores account already exists for that Google identity. Sign in with its existing method.", "warning")
        return redirect(url_for("customer.login", next=next_url))
    _record_terms_acceptance(account, "google_registration")
    if request.form.get("marketing_opt_in") == "yes":
        _record_consent(
            account, "confirmed", "google_checkbox",
            REGISTRATION_CONSENT_TEXT_VERSION,
        )
    else:
        _record_consent(
            account, "declined", "google_checkbox",
            REGISTRATION_CONSENT_TEXT_VERSION,
        )
    db.session.commit()
    try:
        _send_registration_welcome_email(account)
    except Exception:
        db.session.rollback()
        logger.exception("registration_welcome_email_failed account_id=%s", account.id)
    response = _login_and_continue(account, next_url)
    flash("Your Google account is verified and ready to use.", "success")
    return response


@customer_bp.post("/google/link")
@customer_required
def link_google():
    if not current_user.email_verified_at:
        abort(403)
    claims = verify_google_credential(request.form.get("credential", ""))
    if not claims or not _google_email_authoritative(claims) or claims.get("email") != current_user.email:
        flash("Google account email must match your verified FityStores email.", "error")
        return redirect(url_for("customer.dashboard"))
    collision = CustomerAccount.query.filter(CustomerAccount.google_sub == claims["sub"], CustomerAccount.id != current_user.id).first()
    if collision or (current_user.google_sub and current_user.google_sub != claims["sub"]):
        flash("That Google account is already linked elsewhere or this account has a different Google identity.", "error")
    else:
        current_user.google_sub = claims["sub"]
        db.session.commit()
        flash("Google sign-in is linked to your account.", "success")
    return redirect(url_for("customer.dashboard"))
