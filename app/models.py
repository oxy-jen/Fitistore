from datetime import datetime
from secrets import token_urlsafe
from flask_login import UserMixin
from sqlalchemy import CheckConstraint, UniqueConstraint
from .extensions import db, login_manager

class TimestampMixin:
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

class AdminUser(UserMixin, TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    failed_logins = db.Column(db.Integer, default=0, nullable=False)

@login_manager.user_loader
def load_user(user_id):
    try:
        if str(user_id).startswith("customer:"):
            return CustomerAccount.query.get(int(str(user_id).split(":", 1)[1]))
        return AdminUser.query.get(int(user_id))
    except (ValueError, TypeError):
        return None

class Category(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    slug = db.Column(db.String(100), unique=True, nullable=False, index=True)
    products = db.relationship("Product", back_populates="category")

class CartAddCount(db.Model):
    product_id = db.Column(db.Integer, db.ForeignKey("product.id", ondelete="CASCADE"), primary_key=True)
    add_count = db.Column(db.Integer, nullable=False, default=0)


class Product(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(140), nullable=False, index=True)
    slug = db.Column(db.String(170), unique=True, nullable=False, index=True)
    sku = db.Column(db.String(80), default="", index=True)
    description = db.Column(db.Text, nullable=False, default="")
    price_cents = db.Column(db.Integer)
    previous_price_cents = db.Column(db.Integer)
    stock_quantity = db.Column(db.Integer, default=0, nullable=False)
    reserved_quantity = db.Column(db.Integer, default=0, nullable=False)
    unit_cost_cents = db.Column(db.Integer)
    low_stock_threshold = db.Column(db.Integer, default=3, nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)
    featured = db.Column(db.Boolean, default=False, nullable=False)
    size_options = db.Column(db.String(255), default="")
    color_options = db.Column(db.String(255), default="")
    category_id = db.Column(db.Integer, db.ForeignKey("category.id"), nullable=False)
    category = db.relationship("Category", back_populates="products")
    images = db.relationship("ProductImage", back_populates="product", cascade="all, delete-orphan", order_by="ProductImage.sort_order")
    variants = db.relationship("ProductVariant", back_populates="product", cascade="all, delete-orphan", order_by="ProductVariant.sort_order")
    reviews = db.relationship("Review", back_populates="product")
    __table_args__ = (
        CheckConstraint("stock_quantity >= 0", name="ck_product_stock_nonnegative"),
        CheckConstraint("reserved_quantity >= 0", name="ck_product_reserved_nonnegative"),
        CheckConstraint("low_stock_threshold >= 0", name="ck_product_low_stock_nonnegative"),
        CheckConstraint("price_cents IS NULL OR price_cents >= 0", name="ck_product_price_nonnegative"),
        CheckConstraint("unit_cost_cents IS NULL OR unit_cost_cents >= 0", name="ck_product_unit_cost_nonnegative"),
    )

    @property
    def main_image(self):
        primary = [img for img in self.images if img.is_primary]
        return (primary or self.images)[0] if self.images else None

    @property
    def has_discount(self):
        return bool(self.price_cents is not None and self.previous_price_cents and self.previous_price_cents > self.price_cents)

    @property
    def available_quantity(self):
        return max(0, (self.stock_quantity or 0) - (self.reserved_quantity or 0))

    @property
    def active_variants(self):
        return [variant for variant in self.variants if variant.active]

    @property
    def variant_available_quantity(self):
        variants = self.active_variants
        if not variants:
            return self.available_quantity
        return sum(variant.available_quantity for variant in variants)

    @property
    def display_stock_quantity(self):
        return self.variant_available_quantity if self.active_variants else self.available_quantity

    @property
    def active_variant_prices(self):
        return [variant.effective_price_cents for variant in self.active_variants if variant.effective_price_cents is not None]

    @property
    def min_variant_price_cents(self):
        prices = self.active_variant_prices
        return min(prices) if prices else None

    @property
    def max_variant_price_cents(self):
        prices = self.active_variant_prices
        return max(prices) if prices else None

class ProductImage(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    storage_backend = db.Column(db.String(20), default="local", nullable=False)
    cloudinary_public_id = db.Column(db.String(255))
    alt_text = db.Column(db.String(255), nullable=False, default="Product image")
    sort_order = db.Column(db.Integer, default=0, nullable=False)
    is_primary = db.Column(db.Boolean, default=False, nullable=False)
    product = db.relationship("Product", back_populates="images")

class ProductVariant(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False, index=True)
    sku = db.Column(db.String(80), default="", index=True)
    size = db.Column(db.String(50), default="")
    color = db.Column(db.String(50), default="")
    style = db.Column(db.String(80), default="")
    material = db.Column(db.String(80), default="")
    pack_size = db.Column(db.String(80), default="")
    price_cents = db.Column(db.Integer)
    unit_cost_cents = db.Column(db.Integer)
    stock_quantity = db.Column(db.Integer, default=0, nullable=False)
    reserved_quantity = db.Column(db.Integer, default=0, nullable=False)
    low_stock_threshold = db.Column(db.Integer, default=3, nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False, index=True)
    image_id = db.Column(db.Integer, db.ForeignKey("product_image.id"))
    sort_order = db.Column(db.Integer, default=0, nullable=False)
    product = db.relationship("Product", back_populates="variants")
    image = db.relationship("ProductImage")
    __table_args__ = (
        UniqueConstraint("product_id", "size", "color", "style", "material", "pack_size", name="uq_variant_options"),
        CheckConstraint("stock_quantity >= 0", name="ck_variant_stock_nonnegative"),
        CheckConstraint("reserved_quantity >= 0", name="ck_variant_reserved_nonnegative"),
        CheckConstraint("low_stock_threshold >= 0", name="ck_variant_low_stock_nonnegative"),
        CheckConstraint("price_cents IS NULL OR price_cents >= 0", name="ck_variant_price_nonnegative"),
        CheckConstraint("unit_cost_cents IS NULL OR unit_cost_cents >= 0", name="ck_variant_unit_cost_nonnegative"),
    )

    @property
    def label(self):
        parts = [self.size, self.color, self.style, self.material, self.pack_size]
        return " / ".join([part for part in parts if part]) or "Default"

    @property
    def available_quantity(self):
        return max(0, (self.stock_quantity or 0) - (self.reserved_quantity or 0))

    @property
    def effective_price_cents(self):
        return self.price_cents if self.price_cents is not None else self.product.price_cents

class InventoryMovement(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False)
    variant_id = db.Column(db.Integer, db.ForeignKey("product_variant.id"))
    change = db.Column(db.Integer, nullable=False)
    quantity_after = db.Column(db.Integer, nullable=False)
    reason = db.Column(db.String(255), nullable=False)
    admin_id = db.Column(db.Integer, db.ForeignKey("admin_user.id"))
    product = db.relationship("Product")
    variant = db.relationship("ProductVariant")
    admin = db.relationship("AdminUser")

class Order(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    reference = db.Column(db.String(24), unique=True, nullable=False, index=True)
    tracking_token = db.Column(db.String(96), unique=True, nullable=False, default=lambda: token_urlsafe(32), index=True)
    customer_name = db.Column(db.String(120))
    customer_phone = db.Column(db.String(40))
    fulfilment_method = db.Column(db.String(30), nullable=False)
    instructions = db.Column(db.Text, default="")
    status = db.Column(db.String(40), default="Order received", nullable=False)
    payment_status = db.Column(db.String(40), default="Awaiting payment", nullable=False)
    payment_reference = db.Column(db.String(120), default="")
    payment_proof_filename = db.Column(db.String(255), default="")
    total_cents = db.Column(db.Integer)
    delivery_fee_cents = db.Column(db.Integer, default=0, nullable=False)
    discount_cents = db.Column(db.Integer, default=0, nullable=False)
    has_unconfirmed_prices = db.Column(db.Boolean, default=False, nullable=False)
    referral_code = db.Column(db.String(40), default="", index=True)
    referrer_key = db.Column(db.String(120), default="", index=True)
    notes = db.Column(db.Text, default="")
    items = db.relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")
    history = db.relationship("OrderStatusHistory", back_populates="order", cascade="all, delete-orphan", order_by="OrderStatusHistory.created_at.desc()")
    payment_attempts = db.relationship("PaymentAttempt", back_populates="order", cascade="all, delete-orphan", order_by="PaymentAttempt.created_at.desc()")

class OrderItem(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"))
    product_name = db.Column(db.String(140), nullable=False)
    variant_id = db.Column(db.Integer, db.ForeignKey("product_variant.id"))
    variant_label = db.Column(db.String(255), default="")
    sku = db.Column(db.String(80), default="")
    quantity = db.Column(db.Integer, nullable=False)
    price_cents = db.Column(db.Integer)
    subtotal_cents = db.Column(db.Integer)
    unit_cost_cents = db.Column(db.Integer)
    size = db.Column(db.String(50))
    color = db.Column(db.String(50))
    order = db.relationship("Order", back_populates="items")
    product = db.relationship("Product")
    variant = db.relationship("ProductVariant")

class OrderStatusHistory(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"), nullable=False, index=True)
    admin_id = db.Column(db.Integer, db.ForeignKey("admin_user.id"))
    field = db.Column(db.String(40), nullable=False)
    old_value = db.Column(db.String(80), default="")
    new_value = db.Column(db.String(80), nullable=False)
    note = db.Column(db.Text, default="")
    order = db.relationship("Order", back_populates="history")
    admin = db.relationship("AdminUser")

class Review(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    display_name = db.Column(db.String(80), nullable=False)
    title = db.Column(db.String(120), nullable=False, default="")
    rating = db.Column(db.Integer, nullable=False)
    text = db.Column(db.Text, default="")
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"))
    customer_account_id = db.Column(db.Integer, db.ForeignKey("customer_account.id"), index=True)
    status = db.Column(db.String(20), default="pending", nullable=False, index=True)
    ip_hash = db.Column(db.String(64))
    product = db.relationship("Product", back_populates="reviews")
    customer_account = db.relationship("CustomerAccount")
    __table_args__ = (CheckConstraint("rating >= 1 AND rating <= 5", name="ck_review_rating_range"),)

class ProductSuggestion(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_name = db.Column(db.String(140), nullable=False, index=True)
    category = db.Column(db.String(80), nullable=False, index=True)
    preferences = db.Column(db.String(255), default="")
    price_range = db.Column(db.String(80), default="")
    details = db.Column(db.Text, default="")
    status = db.Column(db.String(20), default="pending", nullable=False, index=True)
    ip_hash = db.Column(db.String(64), index=True)
    __table_args__ = (CheckConstraint("status IN ('pending', 'reviewed')", name="ck_product_suggestion_status"),)

class ProductEnquiry(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"))
    product_name = db.Column(db.String(140), nullable=False, index=True)
    size = db.Column(db.String(50), default="")
    color = db.Column(db.String(50), default="")
    message = db.Column(db.Text, default="")
    contact = db.Column(db.String(120), default="")
    preferred_contact = db.Column(db.String(30), default="WhatsApp")
    consent_to_contact = db.Column(db.Boolean, default=False, nullable=False)
    status = db.Column(db.String(20), default="pending", nullable=False, index=True)
    internal_response = db.Column(db.Text, default="")
    ip_hash = db.Column(db.String(64), index=True)
    product = db.relationship("Product")
    __table_args__ = (CheckConstraint("status IN ('pending', 'in_progress', 'resolved')", name="ck_product_enquiry_status"),)

class RestockAlert(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False, index=True)
    size = db.Column(db.String(50), default="")
    color = db.Column(db.String(50), default="")
    contact = db.Column(db.String(120), nullable=False)
    preferred_contact = db.Column(db.String(30), default="WhatsApp")
    consent_to_contact = db.Column(db.Boolean, default=False, nullable=False)
    status = db.Column(db.String(20), default="pending", nullable=False, index=True)
    notified_at = db.Column(db.DateTime)
    product = db.relationship("Product")
    __table_args__ = (
        UniqueConstraint("product_id", "size", "color", "contact", "status", name="uq_restock_alert_open"),
        CheckConstraint("status IN ('pending', 'queued', 'notified', 'cancelled')", name="ck_restock_alert_status"),
    )

class Expense(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    amount_cents = db.Column(db.Integer, nullable=False)
    category = db.Column(db.String(60), nullable=False, index=True)
    incurred_on = db.Column(db.Date, nullable=False, index=True)
    description = db.Column(db.String(255), nullable=False)
    reference = db.Column(db.String(120), default="")
    admin_id = db.Column(db.Integer, db.ForeignKey("admin_user.id"))
    voided = db.Column(db.Boolean, default=False, nullable=False)
    correction_note = db.Column(db.Text, default="")
    admin = db.relationship("AdminUser")
    __table_args__ = (CheckConstraint("amount_cents >= 0", name="ck_expense_amount_nonnegative"),)

class PaymentAttempt(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"), nullable=False, index=True)
    provider = db.Column(db.String(40), default="Manual M-Pesa", nullable=False)
    status = db.Column(db.String(30), default="pending", nullable=False, index=True)
    expected_amount_cents = db.Column(db.Integer)
    merchant_request_id = db.Column(db.String(120), default="", index=True)
    checkout_request_id = db.Column(db.String(120), default="", index=True)
    customer_phone = db.Column(db.String(40), default="")
    result_code = db.Column(db.String(30), default="")
    result_description = db.Column(db.String(255), default="")
    raw_payload = db.Column(db.Text, default="")
    payment_method = db.Column(db.String(40), default="Manual M-Pesa")
    initiated_at = db.Column(db.DateTime)
    confirmed_at = db.Column(db.DateTime)
    last_reconciled_at = db.Column(db.DateTime)
    investigation_note = db.Column(db.Text, default="")
    order = db.relationship("Order", back_populates="payment_attempts")

class PaymentTransaction(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"), index=True)
    payment_attempt_id = db.Column(db.Integer, db.ForeignKey("payment_attempt.id"), index=True)
    provider = db.Column(db.String(40), default="M-Pesa", nullable=False)
    mpesa_receipt = db.Column(db.String(120), unique=True, index=True)
    amount_cents = db.Column(db.Integer, nullable=False)
    phone = db.Column(db.String(40), default="")
    transaction_time = db.Column(db.DateTime)
    status = db.Column(db.String(30), default="verified", nullable=False, index=True)
    discrepancy = db.Column(db.String(255), default="")
    raw_payload = db.Column(db.Text, default="")
    order = db.relationship("Order")
    payment_attempt = db.relationship("PaymentAttempt")

class RefundRequest(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"), nullable=False, index=True)
    payment_transaction_id = db.Column(db.Integer, db.ForeignKey("payment_transaction.id"), index=True)
    amount_cents = db.Column(db.Integer, nullable=False)
    reason = db.Column(db.String(255), nullable=False)
    status = db.Column(db.String(30), default="requested", nullable=False, index=True)
    reference = db.Column(db.String(120), default="", index=True)
    completed_at = db.Column(db.DateTime)
    admin_id = db.Column(db.Integer, db.ForeignKey("admin_user.id"))
    note = db.Column(db.Text, default="")
    order = db.relationship("Order")
    payment_transaction = db.relationship("PaymentTransaction")
    admin = db.relationship("AdminUser")
    __table_args__ = (CheckConstraint("amount_cents > 0", name="ck_refund_amount_positive"),)

class CustomerProfile(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("customer_account.id"), unique=True, index=True)
    phone = db.Column(db.String(40), unique=True, nullable=False, index=True)
    display_name = db.Column(db.String(120), default="")
    referral_code = db.Column(db.String(40), unique=True, nullable=False, index=True)
    referred_by_code = db.Column(db.String(40), default="", index=True)
    account = db.relationship("CustomerAccount")

class ReferralClick(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    referrer_id = db.Column(db.Integer, db.ForeignKey("customer_profile.id"), nullable=False, index=True)
    referral_code = db.Column(db.String(40), nullable=False, index=True)
    visitor_key = db.Column(db.String(96), nullable=False, index=True)
    rewarded = db.Column(db.Boolean, default=False, nullable=False)
    reward_points = db.Column(db.Integer, default=0, nullable=False)
    referrer = db.relationship("CustomerProfile")
    __table_args__ = (
        UniqueConstraint("referral_code", "visitor_key", name="uq_referral_click_visitor"),
    )
class LoyaltyTransaction(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    customer_key = db.Column(db.String(120), nullable=False, index=True)
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"), index=True)
    transaction_type = db.Column(db.String(40), nullable=False, index=True)
    points = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(30), default="posted", nullable=False, index=True)
    reason = db.Column(db.String(255), default="")
    admin_id = db.Column(db.Integer, db.ForeignKey("admin_user.id"))
    order = db.relationship("Order")
    admin = db.relationship("AdminUser")

class Referral(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    referrer_key = db.Column(db.String(120), nullable=False, index=True)
    referred_key = db.Column(db.String(120), default="", index=True)
    referral_code = db.Column(db.String(40), unique=True, nullable=False, index=True)
    qualifying_order_id = db.Column(db.Integer, db.ForeignKey("order.id"), unique=True)
    status = db.Column(db.String(30), default="pending", nullable=False, index=True)
    reward_points = db.Column(db.Integer, default=0, nullable=False)
    note = db.Column(db.Text, default="")
    qualifying_order = db.relationship("Order")

class FinancialAuditEvent(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    admin_id = db.Column(db.Integer, db.ForeignKey("admin_user.id"))
    event_type = db.Column(db.String(80), nullable=False, index=True)
    entity_type = db.Column(db.String(80), nullable=False)
    entity_id = db.Column(db.Integer)
    note = db.Column(db.Text, default="")
    admin = db.relationship("AdminUser")

class ShopSettings(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    shop_name = db.Column(db.String(120), default="FityStores", nullable=False)
    tagline = db.Column(db.String(160), default="FityStores", nullable=False)
    whatsapp_number = db.Column(db.String(32), default="254791305329")
    payment_number = db.Column(db.String(32), default="0141809661")
    contact_email = db.Column(db.String(120), default="")
    public_location = db.Column(db.String(255), default="Othaya Mathenge Technical Training Institute")
    collection_instructions = db.Column(db.Text, default="Collection point: Othaya Mathenge Technical Training Institute. Confirm the exact meeting spot with the seller before travelling.")
    delivery_area = db.Column(db.Text, default="Nearby delivery can be discussed on WhatsApp or by calling 0791305329 before payment.")
    delivery_fee_cents = db.Column(db.Integer)
    payment_instructions = db.Column(db.Text, default="M-Pesa payment number: 0141809661. Please confirm item availability first, then enter the M-Pesa reference code at checkout so it appears in the WhatsApp message.")
    currency = db.Column(db.String(10), default="KSh", nullable=False)
    default_low_stock_threshold = db.Column(db.Integer, default=3, nullable=False)
    loyalty_enabled = db.Column(db.Boolean, default=False, nullable=False)
    loyalty_points_per_100 = db.Column(db.Integer, default=1, nullable=False)
    loyalty_min_redeem_points = db.Column(db.Integer, default=100, nullable=False)
    loyalty_point_value_cents = db.Column(db.Integer, default=100, nullable=False)
    referral_enabled = db.Column(db.Boolean, default=False, nullable=False)
    referral_reward_points = db.Column(db.Integer, default=50, nullable=False)
    referral_min_order_cents = db.Column(db.Integer, default=0, nullable=False)




class CustomerAccount(UserMixin, TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(254), unique=True, nullable=False, index=True)
    display_name = db.Column(db.String(120), default="", nullable=False)
    password_hash = db.Column(db.String(255))
    google_sub = db.Column(db.String(255), unique=True, index=True)
    email_verified_at = db.Column(db.DateTime)
    two_factor_enabled = db.Column(db.Boolean, default=False, nullable=False)
    marketing_status = db.Column(db.String(20), default="none", nullable=False, index=True)
    marketing_consent_at = db.Column(db.DateTime)
    marketing_withdrawn_at = db.Column(db.DateTime)
    active = db.Column(db.Boolean, default=True, nullable=False)

    def get_id(self):
        return f"customer:{self.id}"

    @property
    def is_admin(self):
        return False


class CustomerToken(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("customer_account.id"), nullable=False, index=True)
    purpose = db.Column(db.String(24), nullable=False, index=True)
    token_hash = db.Column(db.String(64), unique=True, nullable=False, index=True)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    used_at = db.Column(db.DateTime)
    account = db.relationship("CustomerAccount")


class CustomerConsentEvent(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("customer_account.id"), nullable=False, index=True)
    event = db.Column(db.String(24), nullable=False)
    method = db.Column(db.String(24), nullable=False)
    wording_version = db.Column(db.String(32), default="2026-10", nullable=False)
    account = db.relationship("CustomerAccount")


class CustomerOrderLink(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("customer_account.id"), nullable=False, index=True)
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"), nullable=False, unique=True, index=True)
    account = db.relationship("CustomerAccount")
    order = db.relationship("Order")


class AuthRateLimit(db.Model):
    key = db.Column(db.String(64), primary_key=True)
    window_started_at = db.Column(db.DateTime, nullable=False)
    attempts = db.Column(db.Integer, default=0, nullable=False)


class EmailMessage(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    recipient = db.Column(db.String(254), nullable=False, index=True)
    account_id = db.Column(db.Integer, db.ForeignKey("customer_account.id"), index=True)
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"), index=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("email_campaign.id"), index=True)
    template_id = db.Column(db.Integer, index=True)
    kind = db.Column(db.String(32), nullable=False, index=True)
    subject = db.Column(db.String(200), nullable=False)
    idempotency_key = db.Column(db.String(160), unique=True, nullable=False)
    status = db.Column(db.String(24), default="queued", nullable=False, index=True)
    failure_reason = db.Column(db.String(255), default="")
    retry_count = db.Column(db.Integer, nullable=False, default=0)
    provider_message_id = db.Column(db.String(255), index=True)
    sent_at = db.Column(db.DateTime)


class EmailCampaign(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    subject = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(24), default="draft", nullable=False, index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("admin_user.id"), nullable=False)
    sent_at = db.Column(db.DateTime)
    admin = db.relationship("AdminUser")


class EmailCampaignRecipient(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("email_campaign.id"), nullable=False, index=True)
    account_id = db.Column(db.Integer, db.ForeignKey("customer_account.id"), nullable=False, index=True)
    recipient = db.Column(db.String(254), nullable=False)
    status = db.Column(db.String(24), default="queued", nullable=False, index=True)
    failure_reason = db.Column(db.String(255), default="")
    sent_at = db.Column(db.DateTime)
    campaign = db.relationship("EmailCampaign")
    account = db.relationship("CustomerAccount")
    __table_args__ = (UniqueConstraint("campaign_id", "account_id", name="uq_campaign_recipient_account"),)


class EmailTemplate(TimestampMixin, db.Model):
    """Persistent, named reusable email template; never sends by itself."""
    id = db.Column(db.Integer, primary_key=True)
    category = db.Column(db.String(32), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False, default="Saved template")
    subject = db.Column(db.String(200), nullable=False)
    headline = db.Column(db.String(200), nullable=False, default="")
    message = db.Column(db.Text, nullable=False, default="")
    blocks_json = db.Column(db.Text, nullable=False, default="[]")


class EmailDraft(TimestampMixin, db.Model):
    """Persistent one-off manual email draft, separate from reusable templates."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    category = db.Column(db.String(32), nullable=False, index=True)
    subject = db.Column(db.String(200), nullable=False)
    headline = db.Column(db.String(200), nullable=False, default="")
    message = db.Column(db.Text, nullable=False, default="")
    blocks_json = db.Column(db.Text, nullable=False, default="[]")
    product_ids_json = db.Column(db.Text, nullable=False, default="[]")
    account_ids_json = db.Column(db.Text, nullable=False, default="[]")
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"), index=True)
    cart_id = db.Column(db.Integer, db.ForeignKey("customer_cart.id"), index=True)
    status = db.Column(db.String(24), nullable=False, default="draft", index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("admin_user.id"), nullable=False)


class CustomerCart(TimestampMixin, db.Model):
    """Minimal authenticated cart snapshot used for safe abandonment reminders."""
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("customer_account.id"), nullable=False, unique=True, index=True)
    items_json = db.Column(db.Text, nullable=False, default="[]")
    checkout_until = db.Column(db.DateTime, index=True)
    reminder_sent_at = db.Column(db.DateTime)
    episode_key = db.Column(db.String(64), nullable=False, default="")
    account = db.relationship("CustomerAccount")


class MarketingAutomationSettings(TimestampMixin, db.Model):
    """Singleton configuration for low-volume scheduled marketing jobs."""
    id = db.Column(db.Integer, primary_key=True, default=1)
    enabled = db.Column(db.Boolean, nullable=False, default=False)
    daily_goodies_enabled = db.Column(db.Boolean, nullable=False, default=False)
    daily_goodies_hour = db.Column(db.Integer, nullable=False, default=9)
    weekly_picks_enabled = db.Column(db.Boolean, nullable=False, default=False)
    weekly_picks_weekday = db.Column(db.Integer, nullable=False, default=0)
    weekly_picks_hour = db.Column(db.Integer, nullable=False, default=10)
    abandoned_cart_enabled = db.Column(db.Boolean, nullable=False, default=False)
    abandoned_cart_minutes = db.Column(db.Integer, nullable=False, default=180)
    timezone_name = db.Column(db.String(64), nullable=False, default="Africa/Nairobi")


class MarketingCampaignRun(TimestampMixin, db.Model):
    """Persistent idempotency and outcome record for scheduled automation."""
    id = db.Column(db.Integer, primary_key=True)
    campaign_key = db.Column(db.String(80), nullable=False, unique=True, index=True)
    campaign_type = db.Column(db.String(32), nullable=False, index=True)
    scheduled_for = db.Column(db.DateTime, nullable=False, index=True)
    started_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)
    status = db.Column(db.String(24), nullable=False, default="queued", index=True)
    eligible_count = db.Column(db.Integer, nullable=False, default=0)
    accepted_count = db.Column(db.Integer, nullable=False, default=0)
    failed_count = db.Column(db.Integer, nullable=False, default=0)
    suppressed_count = db.Column(db.Integer, nullable=False, default=0)
    failure_summary = db.Column(db.String(255), nullable=False, default="")


class MarketingProductExposure(TimestampMixin, db.Model):
    """Small per-customer history used to rotate product recommendations."""
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("customer_account.id"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False, index=True)
    last_sent_at = db.Column(db.DateTime, nullable=False, index=True)
    __table_args__ = (UniqueConstraint("account_id", "product_id", name="uq_marketing_exposure_account_product"),)
