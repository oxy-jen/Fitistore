from __future__ import annotations

import hashlib
import hmac
import logging
import re
from datetime import datetime, timedelta
from functools import wraps
from secrets import token_urlsafe
from urllib.parse import urlsplit

from email_validator import EmailNotValidError, validate_email
from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_user, logout_user
from werkzeug.security import check_password_hash, generate_password_hash
from sqlalchemy.exc import IntegrityError

from app.extensions import db, csrf
from app.models import AuthRateLimit, CustomerAccount, CustomerConsentEvent, CustomerOrderLink, CustomerToken
from app.services.customer_mail import mail_configuration_status, queue_branded_email

customer_bp = Blueprint("customer", __name__, url_prefix="/account")
CONSENT_TEXT_VERSION = "2026-10"
logger = logging.getLogger(__name__)


def _public_url(endpoint, **values):
    path = url_for(endpoint, **values)
    base = (current_app.config.get("PUBLIC_BASE_URL") or "https://fitystore.top").strip().rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme.lower() != "https" or not parsed.netloc or parsed.path or parsed.query or parsed.fragment:
        logger.error("PUBLIC_BASE_URL must be an HTTPS origin; using the canonical FityStores host.")
        base = "https://fitystore.top"
    return f"{base}{path}"


def _delivery_message(result, label="verification"):
    status = getattr(result, "status", "")
    if status == "sent":
        return f"The {label} email was accepted by the mail server. Check your inbox and spam folder; acceptance does not guarantee inbox delivery."
    if status == "not_configured":
        return f"No {label} email was sent because email delivery is not configured. Please try again later."
    if status == "failed":
        return f"The {label} email could not be sent. Please try again later."
    return f"The {label} email could not be queued. Please try again later."


def customer_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for("customer.login", next=request.path))
        if not isinstance(current_user._get_current_object(), CustomerAccount):
            abort(403)
        if not current_user.active:
            logout_user()
            abort(403)
        return view(*args, **kwargs)
    return wrapped


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


def _send_token_email(account, purpose, raw):
    if purpose == "verify_email":
        path = "customer.verify_email"
        subject = "Verify your FityStores email"
        heading = "Verify your email address"
        text = "Use this one-time link to verify your FityStores account. It expires in 24 hours."
        minutes = 24 * 60
    elif purpose == "reset_password":
        path = "customer.reset_password"
        subject = "Reset your FityStores password"
        heading = "Reset your password"
        text = "Use this one-time link to choose a new password. It expires in 30 minutes."
        minutes = 30
    else:
        path = "customer.confirm_marketing"
        subject = "Confirm FityStores email offers"
        heading = "Confirm promotional email preferences"
        text = "Confirm that you would like to receive occasional FityStores offers, new arrivals, and promotions by email."
        minutes = 24 * 60
    url = _public_url(path, token=raw)
    event_key = hashlib.sha256(raw.encode()).hexdigest()[:24]
    return queue_branded_email(
        account.email, subject, purpose, f"{purpose}:{event_key}", heading, [text], account_id=account.id,
        button_url=url, button_text="Continue securely",
    )


def _issue_and_send(account, purpose, minutes=None):
    raw = token_urlsafe(32)
    token_row = CustomerToken(
        account_id=account.id,
        purpose=purpose,
        token_hash=hashlib.sha256(raw.encode()).hexdigest(),
        expires_at=datetime.utcnow() + timedelta(minutes=minutes or (30 if purpose == "reset_password" else 24 * 60)),
    )
    db.session.add(token_row)
    db.session.commit()
    try:
        result = _send_token_email(account, purpose, raw)
    except Exception:
        token_row.used_at = datetime.utcnow()
        db.session.commit()
        raise

    now = datetime.utcnow()
    if result and result.status == "sent":
        CustomerToken.query.filter(
            CustomerToken.account_id == account.id,
            CustomerToken.purpose == purpose,
            CustomerToken.id != token_row.id,
            CustomerToken.used_at.is_(None),
        ).update({"used_at": now}, synchronize_session="fetch")
    elif result and result.status == "not_configured":
        # No mail was attempted, so retire this unsent token while retaining a prior usable link.
        token_row.used_at = now
    # A failed SMTP call can be ambiguous if the server accepted the message before a
    # connection dropped. Keep both links usable; the next accepted send retires older tokens.
    db.session.commit()
    return result


def _record_consent(account, event, method):
    db.session.add(CustomerConsentEvent(account_id=account.id, event=event, method=method, wording_version=CONSENT_TEXT_VERSION))
    if event == "confirmed":
        account.marketing_status = "subscribed"
        account.marketing_consent_at = datetime.utcnow()
        account.marketing_withdrawn_at = None
    elif event == "requested":
        account.marketing_status = "pending"
    elif event == "withdrawn":
        account.marketing_status = "unsubscribed"
        account.marketing_withdrawn_at = datetime.utcnow()


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


def _google_csrf_ok():
    cookie = request.cookies.get("g_csrf_token", "")
    submitted = request.form.get("g_csrf_token", "")
    return bool(cookie and submitted and hmac.compare_digest(cookie, submitted))


@customer_bp.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email_input = request.form.get("email", "")
        key = email_input.strip().lower()[:254]
        if not _rate_allowed("register", key):
            flash("Please wait before trying registration again.", "warning")
            return render_template("customer/register.html", title="Create account")
        try:
            email = normalize_email(email_input)
        except (EmailNotValidError, AttributeError, TypeError):
            flash("Enter a valid email address.", "error")
            return render_template("customer/register.html", title="Create account")
        name = request.form.get("display_name", "").strip()[:120]
        password = request.form.get("password", "")
        if len(name) < 1 or len(password) < 12 or len(password) > 128 or password != request.form.get("confirm_password", ""):
            flash("Enter your name and a matching password of at least 12 characters.", "error")
            return render_template("customer/register.html", title="Create account")
        if CustomerAccount.query.filter_by(email=email).first():
            flash("If this address can be registered, we will send the next steps to it.", "info")
            return redirect(url_for("customer.login"))
        account = CustomerAccount(email=email, display_name=name, password_hash=generate_password_hash(password))
        db.session.add(account)
        try:
            db.session.flush()
        except IntegrityError:
            db.session.rollback()
            flash("If this address can be registered, we will send the next steps to it.", "info")
            return redirect(url_for("customer.login"))
        if request.form.get("marketing_opt_in") == "yes":
            _record_consent(account, "requested", "registration")
        db.session.commit()
        login_user(account)
        verification_result = _issue_and_send(account, "verify_email")
        if account.marketing_status == "pending":
            _issue_and_send(account, "marketing_confirm")
        flash("Your FityStores account was created. Promotional email requires a separate confirmation.", "success")
        flash(_delivery_message(verification_result), "success" if verification_result and verification_result.status == "sent" else "warning")
        return redirect(url_for("customer.dashboard"))
    return render_template("customer/register.html", title="Create account")


@customer_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email_input = request.form.get("email", "").strip().lower()
        if not _rate_allowed("login", email_input[:254], limit=10):
            flash("Invalid email or password. Please try again later.", "error")
            return render_template("customer/login.html", title="Customer sign in", google_client_id=current_app.config.get("GOOGLE_CLIENT_ID", ""), google_login_uri=_public_url("customer.google_signin"))
        account = CustomerAccount.query.filter_by(email=email_input).first()
        password = request.form.get("password", "")
        if account and account.active and account.password_hash and check_password_hash(account.password_hash, password):
            login_user(account)
            return redirect(request.args.get("next") if request.args.get("next", "").startswith("/account") else url_for("customer.dashboard"))
        flash("Invalid email or password.", "error")
    return render_template("customer/login.html", title="Customer sign in", google_client_id=current_app.config.get("GOOGLE_CLIENT_ID", ""), google_login_uri=_public_url("customer.google_signin"))


@customer_bp.post("/logout")
@customer_required
def logout():
    logout_user()
    flash("You have signed out.", "success")
    return redirect(url_for("customer.login"))


@customer_bp.route("/")
@customer_required
def dashboard():
    links = CustomerOrderLink.query.filter_by(account_id=current_user.id).order_by(CustomerOrderLink.created_at.desc()).all()
    return render_template("customer/dashboard.html", title="Your FityStores account", account=current_user, order_links=links, mail_status=mail_configuration_status()[1], google_client_id=current_app.config.get("GOOGLE_CLIENT_ID", ""))


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
        flash(_delivery_message(result, "promotional confirmation"), "success" if result and result.status == "sent" else "warning")
    return redirect(url_for("customer.dashboard"))



@customer_bp.post("/marketing/unsubscribe")
@customer_required
def unsubscribe_account():
    if current_user.marketing_status == "subscribed":
        _record_consent(current_user, "withdrawn", "account_preferences")
        db.session.commit()
    flash("You are unsubscribed from promotional email.", "success")
    return redirect(url_for("customer.dashboard"))

@customer_bp.route("/verify/<token>", methods=["GET", "POST"])
def verify_email(token):
    row = _find_token(token, "verify_email")
    if request.method == "GET":
        valid = bool(row and not row.account.email_verified_at)
        heading = "Verify your email"
        if row and row.account.email_verified_at:
            heading = "Email already verified"
        return render_template("customer/confirm_token.html", title=heading, heading=heading, action=url_for("customer.verify_email", token=token), valid=valid)
    if not row:
        flash("That verification link is invalid, expired, or already used. Sign in and request another if your email is not verified.", "error")
        return redirect(url_for("customer.login"))
    if row.account.email_verified_at:
        row.used_at = row.used_at or datetime.utcnow()
        db.session.commit()
        flash("Your email is already verified.", "info")
        return redirect(url_for("customer.login"))
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
    queue_branded_email(row.account.email, "Welcome to FityStores", "welcome", f"welcome:{row.account.id}", "Welcome to FityStores", ["Your account is ready. You can view your linked orders and manage your account preferences on our website."], account_id=row.account.id, button_url=_public_url("customer.dashboard"), button_text="View your account")
    flash("Your email is verified.", "success")
    return redirect(url_for("customer.login"))


@customer_bp.post("/verification/resend")
@customer_required
def resend_verification():
    if current_user.email_verified_at:
        flash("Your email is already verified.", "info")
    elif not _rate_allowed("verify", current_user.email, limit=3, window_minutes=60):
        flash("Too many verification requests. Please wait before trying again.", "warning")
    else:
        result = _issue_and_send(current_user, "verify_email")
        flash(_delivery_message(result), "success" if result and result.status == "sent" else "warning")
    return redirect(url_for("customer.dashboard"))


@customer_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()[:254]
        if _rate_allowed("forgot", email, limit=4, window_minutes=60):
            account = CustomerAccount.query.filter_by(email=email, active=True).first()
            if account and account.email_verified_at and account.password_hash:
                _issue_and_send(account, "reset_password", 30)
        flash("If a verified account uses that address, password reset instructions will be sent when email is configured.", "info")
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
    if len(password) < 12 or len(password) > 128 or password != request.form.get("confirm_password", ""):
        flash("Enter matching passwords of at least 12 characters.", "error")
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
    row = _find_token(token, "marketing_confirm")
    if request.method == "GET":
        return render_template("customer/confirm_token.html", title="Confirm offers", heading="Confirm promotional emails", action=url_for("customer.confirm_marketing", token=token), valid=bool(row))
    if not row:
        flash("That confirmation link is invalid or expired.", "error")
        return redirect(url_for("public.home"))
    account = row.account
    if not account.email_verified_at:
        flash("Verify your account email before confirming promotional email. The confirmation link remains available for 24 hours.", "warning")
        return redirect(url_for("customer.login"))
    row.used_at = datetime.utcnow()
    _record_consent(account, "confirmed", "double_opt_in")
    db.session.commit()
    flash("Your promotional email preference is confirmed.", "success")
    return redirect(url_for("customer.login"))

@customer_bp.route("/unsubscribe/<token>", methods=["GET", "POST"])
def unsubscribe(token):
    row = _find_token(token, "unsubscribe")
    if request.method == "GET":
        return render_template("customer/confirm_token.html", title="Unsubscribe", heading="Unsubscribe from FityStores offers", action=url_for("customer.unsubscribe", token=token), valid=bool(row))
    if row:
        row.used_at = datetime.utcnow()
        account = row.account
        _record_consent(account, "withdrawn", "unsubscribe_link")
        db.session.commit()
        from app.services.customer_mail import queue_branded_email
        queue_branded_email(account.email, "FityStores email preferences updated", "unsubscribe_confirmation", f"unsubscribe-link:{row.id}", "Promotional email stopped", ["You have been unsubscribed from promotional FityStores emails. Essential account and order messages may still be sent."], account_id=account.id)
    return render_template("customer/unsubscribed.html", title="Email preferences updated")


@csrf.exempt
@customer_bp.post("/google")
def google_signin():
    if not current_app.config.get("GOOGLE_CLIENT_ID"):
        flash("Google sign-in is not configured yet.", "warning")
        return redirect(url_for("customer.login"))
    if not _rate_allowed("google_ip", "", limit=20, window_minutes=15):
        abort(429)
    credential = request.form.get("credential", "")
    if not _google_csrf_ok() or not credential or len(credential) > 10000:
        abort(400)
    claims = verify_google_credential(credential)
    if not claims or not claims.get("sub"):
        flash("Google could not verify this sign-in. Check the Web client ID configured for this site and try again.", "error")
        return redirect(url_for("customer.login"))
    if not _google_email_authoritative(claims):
        flash("Use a Google account with a verified Gmail or Google Workspace email address.", "error")
        return redirect(url_for("customer.login"))
    if not _rate_allowed("google", claims["email"], limit=8):
        abort(429)
    account = CustomerAccount.query.filter_by(google_sub=claims["sub"]).first()
    if account:
        if not account.active:
            abort(403)
        login_user(account)
        return redirect(url_for("customer.dashboard"))
    if CustomerAccount.query.filter_by(email=claims["email"]).first():
        flash("An account already uses this email. Sign in with its existing method, then link Google from your account.", "warning")
        return redirect(url_for("customer.login"))
    account = CustomerAccount(email=claims["email"], display_name=claims.get("name", ""), google_sub=claims["sub"], email_verified_at=datetime.utcnow())
    db.session.add(account)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        flash("A FityStores account already exists for that Google identity. Sign in with its existing method.", "warning")
        return redirect(url_for("customer.login"))
    if request.form.get("marketing_opt_in") == "yes":
        _record_consent(account, "requested", "google_registration")
    db.session.commit()
    login_user(account)
    if account.marketing_status == "pending":
        _issue_and_send(account, "marketing_confirm")
    queue_branded_email(account.email, "Welcome to FityStores", "welcome", f"welcome:{account.id}", "Welcome to FityStores", ["Your account is ready. You can view your linked orders and manage your account preferences on our website."], account_id=account.id, button_url=_public_url("customer.dashboard"), button_text="View your account")
    return redirect(url_for("customer.dashboard"))


@customer_bp.post("/google/link")
@customer_required
def link_google():
    if not current_user.email_verified_at:
        abort(403)
    if not _google_csrf_ok():
        abort(400)
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
