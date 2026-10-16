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
    return AdminUser.query.get(int(user_id))

class Category(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    slug = db.Column(db.String(100), unique=True, nullable=False, index=True)
    products = db.relationship("Product", back_populates="category")

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
    rating = db.Column(db.Integer, nullable=False)
    text = db.Column(db.Text, default="")
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"))
    status = db.Column(db.String(20), default="pending", nullable=False, index=True)
    ip_hash = db.Column(db.String(64))
    product = db.relationship("Product", back_populates="reviews")
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
    phone = db.Column(db.String(40), unique=True, nullable=False, index=True)
    display_name = db.Column(db.String(120), default="")
    referral_code = db.Column(db.String(40), unique=True, nullable=False, index=True)
    referred_by_code = db.Column(db.String(40), default="", index=True)

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



