import hashlib
import re
from secrets import token_hex
from app.extensions import db
from app.models import CustomerProfile, LoyaltyTransaction, ReferralClick

PHONE_RE = re.compile(r"\D+")


def normalize_phone(value):
    digits = PHONE_RE.sub("", value or "")
    if digits.startswith("0") and len(digits) >= 10:
        digits = "254" + digits[1:]
    return digits


def referral_customer_key(phone):
    return normalize_phone(phone)


def make_referral_code(phone):
    base = normalize_phone(phone) or token_hex(4)
    seed = hashlib.sha1(base.encode("utf-8")).hexdigest()[:8].upper()
    code = f"BF{seed}"
    if not CustomerProfile.query.filter_by(referral_code=code).first():
        return code
    while True:
        code = f"BF{token_hex(4).upper()}"
        if not CustomerProfile.query.filter_by(referral_code=code).first():
            return code


def get_or_create_profile(phone, display_name="", referred_by_code=""):
    normalized = normalize_phone(phone)
    if not normalized:
        raise ValueError("Enter a phone number to create your referral profile.")
    profile = CustomerProfile.query.filter_by(phone=normalized).first()
    if not profile:
        profile = CustomerProfile(phone=normalized, display_name=(display_name or "").strip(), referral_code=make_referral_code(normalized), referred_by_code=(referred_by_code or "").strip().upper())
        db.session.add(profile)
    else:
        if display_name is not None:
            profile.display_name = (display_name or profile.display_name or "").strip()
        if referred_by_code and not profile.referred_by_code and referred_by_code.upper() != profile.referral_code:
            profile.referred_by_code = referred_by_code.upper()
    return profile


def visitor_key_for_request(request):
    raw = "|".join([
        request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip(),
        request.headers.get("User-Agent", "")[:160],
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def referral_points_balance(phone):
    key = referral_customer_key(phone)
    if not key:
        return 0
    rows = LoyaltyTransaction.query.filter_by(customer_key=key, status="posted").all()
    return sum(row.points for row in rows)


def record_referral_click(code, request, settings, current_phone=""):
    referral_code = (code or "").strip().upper()
    profile = CustomerProfile.query.filter_by(referral_code=referral_code).first()
    if not profile:
        return None, False, "Referral link not found."
    if normalize_phone(current_phone) and normalize_phone(current_phone) == profile.phone:
        return profile, False, "Your own referral link is ready to share."
    visitor_key = visitor_key_for_request(request)
    existing = ReferralClick.query.filter_by(referral_code=referral_code, visitor_key=visitor_key).first()
    if existing:
        return profile, False, "This visit was already counted."
    points = max(0, settings.referral_reward_points or 0) if settings and settings.referral_enabled else 0
    click = ReferralClick(referrer=profile, referral_code=referral_code, visitor_key=visitor_key, rewarded=points > 0, reward_points=points)
    db.session.add(click)
    if points > 0:
        db.session.add(LoyaltyTransaction(customer_key=profile.phone, transaction_type="referral_click", points=points, reason=f"Referral link visit {referral_code}"))
    db.session.commit()
    return profile, points > 0, "Referral visit counted."

