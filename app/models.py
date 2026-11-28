from datetime import datetime
from secrets import token_urlsafe
from flask_login import UserMixin
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from .extensions import db, login_manager

class TimestampMixin:
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

class AdminUser(UserMixin, TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    failed_logins = db.Column(db.Integer, default=0, nullable=False)

    @property
    def is_admin(self):
        return True


@login_manager.user_loader
def load_user(user_id):
    try:
        if str(user_id).startswith("customer:"):
            account = CustomerAccount.query.get(int(str(user_id).split(":", 1)[1]))
            return account if account and account.active else None
        return AdminUser.query.get(int(user_id))
    except (ValueError, TypeError):
        return None

class Category(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    slug = db.Column(db.String(100), unique=True, nullable=False, index=True)
    products = db.relationship("Product", back_populates="category")


class CategoryThumbnail(TimestampMixin, db.Model):
    __tablename__ = "home_category_thumbnail"
    id = db.Column(db.Integer, primary_key=True)
    group_key = db.Column(db.String(50), unique=True, nullable=False, index=True)
    filename = db.Column(db.String(255), nullable=False, default="")
    storage_backend = db.Column(db.String(20), nullable=False, default="local")
    cloudinary_public_id = db.Column(db.String(255))
    alt_text = db.Column(db.String(255), nullable=False, default="Category thumbnail")

class CartAddCount(db.Model):
    product_id = db.Column(db.Integer, db.ForeignKey("product.id", ondelete="CASCADE"), primary_key=True)
    add_count = db.Column(db.Integer, nullable=False, default=0)


class ProductLike(db.Model):
    __tablename__ = "product_like"
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id", ondelete="CASCADE"), nullable=False, index=True)
    voter_hash = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    __table_args__ = (UniqueConstraint("product_id", "voter_hash", name="uq_product_like_voter"),)


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
    display_note = db.relationship("ProductDisplayNote", back_populates="product", cascade="all, delete-orphan", uselist=False)
    content = db.relationship("ProductDetails", back_populates="product", cascade="all, delete-orphan", passive_deletes=True, uselist=False)
    content_sections = db.relationship("ProductContentSection", back_populates="product", cascade="all, delete-orphan", passive_deletes=True, order_by="ProductContentSection.sort_order, ProductContentSection.id")
    reviews = db.relationship("Review", back_populates="product")
    mamba_collection_item = db.relationship("MambaCollectionItem", back_populates="product", cascade="all, delete-orphan", passive_deletes=True, uselist=False)
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


class ProductDisplayNote(db.Model):
    """Short admin-written note shown beside price and selectable product options."""
    __tablename__ = "product_display_note"
    product_id = db.Column(db.Integer, db.ForeignKey("product.id", ondelete="CASCADE"), primary_key=True)
    note = db.Column(db.String(240), nullable=False, default="")
    product = db.relationship("Product", back_populates="display_note")


class UpcomingProduct(TimestampMixin, db.Model):
    """Admin-managed editorial tiles for the Upcoming page (not stock promises)."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(140), nullable=False)
    category_label = db.Column(db.String(80), nullable=False, default="")
    description = db.Column(db.Text, nullable=False, default="")
    image_url = db.Column(db.String(1000), nullable=False, default="")
    image_alt = db.Column(db.String(255), nullable=False, default="")
    card_size = db.Column(db.String(16), nullable=False, default="tall")
    display_order = db.Column(db.Integer, nullable=False, default=0)
    is_enabled = db.Column(db.Boolean, nullable=False, default=True, index=True)
    __table_args__ = (
        CheckConstraint("card_size IN ('compact','standard','tall','wide','featured')", name="ck_upcoming_product_card_size"),
        CheckConstraint("display_order >= 0", name="ck_upcoming_product_display_order"),
    )
    votes = db.relationship("UpcomingProductVote", back_populates="product", cascade="all, delete-orphan")


class UpcomingProductVote(TimestampMixin, db.Model):
    """One anonymous or signed-in shopper vote per Upcoming product."""
    id = db.Column(db.Integer, primary_key=True)
    upcoming_product_id = db.Column(
        db.Integer, db.ForeignKey("upcoming_product.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    voter_hash = db.Column(db.String(64), nullable=False)
    product = db.relationship("UpcomingProduct", back_populates="votes")
    __table_args__ = (
        UniqueConstraint("upcoming_product_id", "voter_hash", name="uq_upcoming_product_vote_voter"),
    )


class MediaShowcase(TimestampMixin, db.Model):
    """Admin-managed content rendered only into the explicit slots in SHOWCASE_PLACEMENTS."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, index=True)
    title = db.Column(db.String(160), nullable=False, default="")
    subtitle = db.Column(db.String(200), nullable=False, default="")
    description = db.Column(db.Text, nullable=False, default="")
    slug = db.Column(db.String(150), unique=True, nullable=False, index=True)
    page_identifier = db.Column(db.String(40), nullable=False, index=True)
    placement_identifier = db.Column(db.String(80), nullable=False, index=True)
    layout = db.Column(db.String(40), nullable=False, default="carousel")
    density = db.Column(db.String(16), nullable=False, default="compact")
    card_aspect_ratio = db.Column(db.String(8), nullable=False, default="16:9")
    visible_cards_mobile = db.Column(db.Integer, nullable=False, default=2)
    visible_cards_tablet = db.Column(db.Integer, nullable=False, default=3)
    visible_cards_desktop = db.Column(db.Integer, nullable=False, default=4)
    section_padding_px = db.Column(db.Integer)
    card_gap_px = db.Column(db.Integer)
    heading_color = db.Column(db.String(20), nullable=False, default="#252525")
    description_color = db.Column(db.String(20), nullable=False, default="#666666")
    accent_color = db.Column(db.String(20), nullable=False, default="#e52335")
    card_background_color = db.Column(db.String(20), nullable=False, default="#ffffff")
    border_color = db.Column(db.String(20), nullable=False, default="#e8e8eb")
    button_background_color = db.Column(db.String(20), nullable=False, default="#e52335")
    button_text_color = db.Column(db.String(20), nullable=False, default="#ffffff")
    display_order = db.Column(db.Integer, nullable=False, default=0)
    is_published = db.Column(db.Boolean, nullable=False, default=False, index=True)
    is_enabled = db.Column(db.Boolean, nullable=False, default=True, index=True)
    starts_at = db.Column(db.DateTime)
    expires_at = db.Column(db.DateTime)
    background_color = db.Column(db.String(20), nullable=False, default="#fff0f6")
    created_by_id = db.Column(db.Integer, db.ForeignKey("admin_user.id", ondelete="SET NULL"))
    items = db.relationship("MediaShowcaseItem", back_populates="showcase", cascade="all, delete-orphan", order_by="MediaShowcaseItem.display_order, MediaShowcaseItem.id")
    created_by = db.relationship("AdminUser")
    __table_args__ = (
        CheckConstraint("display_order >= 0", name="ck_media_showcase_order_nonnegative"),
        CheckConstraint("layout IN ('carousel','banner','grid','featured','mixed','single','split')", name="ck_media_showcase_layout"),
        CheckConstraint("density IN ('compact','balanced','spacious')", name="ck_media_showcase_density"),
        CheckConstraint("card_aspect_ratio IN ('1:1','4:3','3:2','16:9')", name="ck_media_showcase_aspect_ratio"),
        CheckConstraint("visible_cards_mobile BETWEEN 1 AND 4", name="ck_media_showcase_mobile_count"),
        CheckConstraint("visible_cards_tablet BETWEEN 1 AND 5", name="ck_media_showcase_tablet_count"),
        CheckConstraint("visible_cards_desktop BETWEEN 1 AND 6", name="ck_media_showcase_desktop_count"),
        CheckConstraint("section_padding_px IS NULL OR section_padding_px BETWEEN 0 AND 40", name="ck_media_showcase_padding"),
        CheckConstraint("card_gap_px IS NULL OR card_gap_px BETWEEN 0 AND 40", name="ck_media_showcase_gap"),
    )


class MediaShowcaseItem(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    showcase_id = db.Column(db.Integer, db.ForeignKey("media_showcase.id", ondelete="CASCADE"), nullable=False, index=True)
    media_type = db.Column(db.String(20), nullable=False)
    source_type = db.Column(db.String(20), nullable=False, default="external_url")
    source_filename = db.Column(db.String(255), nullable=False, default="")
    file_size_bytes = db.Column(db.Integer)
    media_width = db.Column(db.Integer)
    media_height = db.Column(db.Integer)
    duration_seconds = db.Column(db.Float)
    title = db.Column(db.String(160), nullable=False, default="")
    description = db.Column(db.Text, nullable=False, default="")
    media_url = db.Column(db.String(1000), nullable=False, default="")
    poster_url = db.Column(db.String(1000), nullable=False, default="")
    thumbnail_url = db.Column(db.String(1000), nullable=False, default="")
    destination_url = db.Column(db.String(1000), nullable=False, default="")
    linked_product_id = db.Column(db.Integer, db.ForeignKey("product.id", ondelete="SET NULL"))
    cta_label = db.Column(db.String(60), nullable=False, default="")
    alt_text = db.Column(db.String(255), nullable=False, default="")
    display_order = db.Column(db.Integer, nullable=False, default=0)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    autoplay = db.Column(db.Boolean, nullable=False, default=True)
    muted = db.Column(db.Boolean, nullable=False, default=True)
    loop = db.Column(db.Boolean, nullable=False, default=True)
    show_controls = db.Column(db.Boolean, nullable=False, default=False)
    presentation_mode = db.Column(db.String(16), nullable=False, default="original")
    aspect_ratio = db.Column(db.String(24), nullable=False, default="")
    preview_position_x = db.Column(db.Integer, nullable=False, default=50)
    preview_position_y = db.Column(db.Integer, nullable=False, default=50)
    showcase = db.relationship("MediaShowcase", back_populates="items")
    linked_product = db.relationship("Product")
    __table_args__ = (
        CheckConstraint("media_type IN ('video','gif','image','link')", name="ck_media_showcase_item_type"),
        CheckConstraint("display_order >= 0", name="ck_media_showcase_item_order_nonnegative"),
        CheckConstraint("presentation_mode IN ('original','contain','cover')", name="ck_media_showcase_item_presentation"),
        CheckConstraint("preview_position_x BETWEEN 0 AND 100", name="ck_media_showcase_item_preview_x"),
        CheckConstraint("preview_position_y BETWEEN 0 AND 100", name="ck_media_showcase_item_preview_y"),
    )


class DesignStudioRole(db.Model):
    __tablename__ = "design_studio_role"
    id = db.Column(db.Integer, primary_key=True)
    admin_user_id = db.Column(db.Integer, db.ForeignKey("admin_user.id", ondelete="CASCADE"), nullable=False, unique=True)
    role = db.Column(db.String(20), nullable=False, default="designer")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    admin_user = db.relationship("AdminUser")
    __table_args__ = (CheckConstraint("role IN ('owner','designer','editor','approver','publisher','viewer')", name="ck_design_studio_role"),)


class StorefrontDesign(TimestampMixin, db.Model):
    __tablename__ = "storefront_design"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    title = db.Column(db.String(160), nullable=False, default="")
    description = db.Column(db.String(500), nullable=False, default="")
    placement = db.Column(db.String(32), nullable=False, default="storefront_banner")
    status = db.Column(db.String(16), nullable=False, default="draft")
    is_enabled = db.Column(db.Boolean, nullable=False, default=True)
    starts_at = db.Column(db.DateTime)
    expires_at = db.Column(db.DateTime)
    html = db.Column(db.Text, nullable=False, default="")
    css = db.Column(db.Text, nullable=False, default="")
    javascript = db.Column(db.Text, nullable=False, default="")
    lottie_json = db.Column(db.Text, nullable=False, default="")
    product_ids = db.Column(db.Text, nullable=False, default="[]")
    template_key = db.Column(db.String(40), nullable=False, default="editorial")
    motion_preset = db.Column(db.String(20), nullable=False, default="none")
    motion_config = db.Column(db.Text, nullable=False, default="{}")
    version = db.Column(db.Integer, nullable=False, default=1)
    created_by_id = db.Column(db.Integer, db.ForeignKey("admin_user.id", ondelete="SET NULL"))
    updated_by_id = db.Column(db.Integer, db.ForeignKey("admin_user.id", ondelete="SET NULL"))
    approved_by_id = db.Column(db.Integer, db.ForeignKey("admin_user.id", ondelete="SET NULL"))
    published_by_id = db.Column(db.Integer, db.ForeignKey("admin_user.id", ondelete="SET NULL"))
    created_by = db.relationship("AdminUser", foreign_keys=[created_by_id])
    revisions = db.relationship("StorefrontDesignRevision", cascade="all, delete-orphan", order_by="StorefrontDesignRevision.revision_number.desc()")
    __table_args__ = (
        CheckConstraint("placement IN ('storefront_banner','storefront_section')", name="ck_storefront_design_placement"),
        CheckConstraint("status IN ('draft','pending','approved','published')", name="ck_storefront_design_status"),
        CheckConstraint("version >= 1", name="ck_storefront_design_version"),
        Index("ix_storefront_design_publish", "status", "is_enabled", "placement"),
    )


class StorefrontDesignRevision(db.Model):
    __tablename__ = "storefront_design_revision"
    id = db.Column(db.Integer, primary_key=True)
    design_id = db.Column(db.Integer, db.ForeignKey("storefront_design.id", ondelete="CASCADE"), nullable=False, index=True)
    revision_number = db.Column(db.Integer, nullable=False)
    snapshot = db.Column(db.Text, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("admin_user.id", ondelete="SET NULL"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    __table_args__ = (UniqueConstraint("design_id", "revision_number", name="uq_storefront_design_revision_number"),)


class StorefrontDesignAudit(db.Model):
    __tablename__ = "storefront_design_audit"
    id = db.Column(db.Integer, primary_key=True)
    design_id = db.Column(db.Integer, db.ForeignKey("storefront_design.id", ondelete="SET NULL"), index=True)
    admin_user_id = db.Column(db.Integer, db.ForeignKey("admin_user.id", ondelete="SET NULL"), index=True)
    action = db.Column(db.String(32), nullable=False)
    details = db.Column(db.Text, nullable=False, default="{}")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

class MambaCollection(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    is_configured = db.Column(db.Boolean, default=False, nullable=False)


class MambaCollectionItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id", ondelete="CASCADE"), unique=True, nullable=False)
    sort_order = db.Column(db.Integer, default=0, nullable=False)
    product = db.relationship("Product", back_populates="mamba_collection_item")


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
    crop_settings = db.relationship(
        "ProductImageCrop",
        back_populates="image",
        cascade="all, delete-orphan",
        uselist=False,
    )


class ProductImageCrop(db.Model):
    """Independent, non-destructive crop profiles for storefront card contexts."""
    __tablename__ = "product_image_crop"
    image_id = db.Column(
        db.Integer,
        db.ForeignKey("product_image.id", ondelete="CASCADE"),
        primary_key=True,
    )
    normal_settings = db.Column(db.String(160), nullable=False, default="")
    shop_settings = db.Column(db.String(160), nullable=False, default="")
    section_settings = db.Column(db.Text, nullable=False, default="{}", server_default="{}")
    image = db.relationship("ProductImage", back_populates="crop_settings")

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
    previous_price_cents = db.Column(db.Integer)
    unit_cost_cents = db.Column(db.Integer)
    stock_quantity = db.Column(db.Integer, default=0, nullable=False)
    reserved_quantity = db.Column(db.Integer, default=0, nullable=False)
    low_stock_threshold = db.Column(db.Integer, default=3, nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False, index=True)
    image_id = db.Column(db.Integer, db.ForeignKey("product_image.id"))
    sort_order = db.Column(db.Integer, default=0, nullable=False)
    product = db.relationship("Product", back_populates="variants")
    image = db.relationship("ProductImage")
    content = db.relationship("ProductVariantDetails", back_populates="variant", cascade="all, delete-orphan", passive_deletes=True, uselist=False)
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

    @property
    def has_discount(self):
        return bool(
            self.effective_price_cents is not None
            and self.previous_price_cents is not None
            and self.previous_price_cents > self.effective_price_cents
        )

    @property
    def discount_amount_cents(self):
        if not self.has_discount:
            return None
        return self.previous_price_cents - self.effective_price_cents

    @property
    def discount_percent(self):
        if not self.has_discount:
            return None
        return self.discount_amount_cents * 100 // self.previous_price_cents


class ProductDetails(TimestampMixin, db.Model):
    __tablename__ = "product_details"
    product_id = db.Column(db.Integer, db.ForeignKey("product.id", ondelete="CASCADE"), primary_key=True)
    brand = db.Column(db.String(120), nullable=False, default="")
    model = db.Column(db.String(120), nullable=False, default="")
    introduction = db.Column(db.Text, nullable=False, default="")
    detailed_description_html = db.Column(db.Text, nullable=False, default="")
    key_features = db.Column(db.JSON, nullable=False, default=list)
    specifications = db.Column(db.JSON, nullable=False, default=list)
    box_contents = db.Column(db.JSON, nullable=False, default=list)
    manufacturer = db.Column(db.String(160), nullable=False, default="")
    warranty_period = db.Column(db.String(120), nullable=False, default="")
    warranty_type = db.Column(db.String(120), nullable=False, default="")
    care_instructions = db.Column(db.Text, nullable=False, default="")
    additional_information = db.Column(db.Text, nullable=False, default="")
    delivery_note = db.Column(db.Text, nullable=False, default="")
    returns_note = db.Column(db.Text, nullable=False, default="")
    seo_title = db.Column(db.String(180), nullable=False, default="")
    seo_description = db.Column(db.String(320), nullable=False, default="")
    product = db.relationship("Product", back_populates="content")


class ProductVariantDetails(TimestampMixin, db.Model):
    __tablename__ = "product_variant_details"
    variant_id = db.Column(db.Integer, db.ForeignKey("product_variant.id", ondelete="CASCADE"), primary_key=True)
    description_override_html = db.Column(db.Text, nullable=False, default="")
    variant = db.relationship("ProductVariant", back_populates="content")


class ProductContentSection(TimestampMixin, db.Model):
    __tablename__ = "product_content_section"
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id", ondelete="CASCADE"), nullable=False, index=True)
    title = db.Column(db.String(160), nullable=False, default="")
    body_text = db.Column(db.Text, nullable=False, default="")
    image_id = db.Column(db.Integer, db.ForeignKey("product_image.id", ondelete="SET NULL"))
    image_alt = db.Column(db.String(255), nullable=False, default="")
    image_caption = db.Column(db.String(300), nullable=False, default="")
    image_position = db.Column(db.String(12), nullable=False, default="left")
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    product = db.relationship("Product", back_populates="content_sections")
    image = db.relationship("ProductImage")
    __table_args__ = (
        CheckConstraint("image_position IN ('left','right','full')", name="ck_product_content_section_position"),
        CheckConstraint("sort_order >= 0", name="ck_product_content_section_order"),
    )


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
    wording_version = db.Column(db.String(120), default="2026-10", nullable=False)
    account = db.relationship("CustomerAccount")


_SUPPRESSION_ID_TYPE = db.BigInteger().with_variant(db.Integer, "sqlite")
_SUPPRESSION_JSON_TYPE = JSONB().with_variant(db.JSON, "sqlite")


class MarketingSuppressionCase(TimestampMixin, db.Model):
    """Current state of one independently reviewable marketing suppression."""
    __tablename__ = "marketing_suppression_case"
    id = db.Column(_SUPPRESSION_ID_TYPE, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("customer_account.id", ondelete="RESTRICT"), nullable=False)
    source = db.Column(db.String(24), nullable=False)
    reason_code = db.Column(db.String(48), nullable=False)
    detected_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, server_default=db.func.current_timestamp())
    provider_name = db.Column(db.String(48))
    metadata_json = db.Column("metadata", _SUPPRESSION_JSON_TYPE)
    cleared_at = db.Column(db.DateTime)
    cleared_by_admin_id = db.Column(db.Integer, db.ForeignKey("admin_user.id", ondelete="RESTRICT"))
    clearance_reason = db.Column(db.Text)
    account = db.relationship("CustomerAccount")
    cleared_by_admin = db.relationship("AdminUser")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, server_default=db.func.current_timestamp(), nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, server_default=db.func.current_timestamp(), nullable=False)
    __table_args__ = (
        UniqueConstraint("id", "account_id", name="uq_marketing_suppression_case_id_account"),
        CheckConstraint("source IN ('admin', 'provider', 'legacy_unknown')", name="ck_marketing_suppression_case_source"),
        CheckConstraint("reason_code IN ('admin_review', 'hard_bounce', 'spam_complaint', 'invalid_address', 'provider_unsubscribe_unclassified', 'legacy_unknown')", name="ck_marketing_suppression_case_reason"),
        CheckConstraint("source <> 'provider' OR provider_name IS NOT NULL", name="ck_marketing_suppression_case_provider"),
        CheckConstraint("(source = 'admin' AND reason_code = 'admin_review') OR (source = 'provider' AND reason_code IN ('hard_bounce', 'spam_complaint', 'invalid_address', 'provider_unsubscribe_unclassified')) OR (source = 'legacy_unknown' AND reason_code = 'legacy_unknown')", name="ck_marketing_suppression_case_source_reason"),
        CheckConstraint("(cleared_at IS NULL AND cleared_by_admin_id IS NULL AND clearance_reason IS NULL) OR (cleared_at IS NOT NULL AND cleared_by_admin_id IS NOT NULL AND clearance_reason IS NOT NULL AND length(trim(clearance_reason)) > 0)", name="ck_marketing_suppression_case_clearance"),
        Index("ix_marketing_suppression_case_account_detected", "account_id", "detected_at"),
        Index("uq_marketing_suppression_case_active", "account_id", "source", "reason_code", unique=True,
              postgresql_where=text("cleared_at IS NULL"), sqlite_where=text("cleared_at IS NULL")),
    )


class MarketingSuppressionEvent(TimestampMixin, db.Model):
    """Append-only audit event for suppression and clearance lifecycle changes."""
    __tablename__ = "marketing_suppression_event"
    id = db.Column(_SUPPRESSION_ID_TYPE, primary_key=True)
    suppression_case_id = db.Column(_SUPPRESSION_ID_TYPE, nullable=False)
    account_id = db.Column(db.Integer, db.ForeignKey("customer_account.id", ondelete="RESTRICT"), nullable=False)
    event_type = db.Column(db.String(32), nullable=False)
    actor_type = db.Column(db.String(24), nullable=False)
    admin_id = db.Column(db.Integer, db.ForeignKey("admin_user.id", ondelete="RESTRICT"))
    source = db.Column(db.String(24), nullable=False)
    reason_code = db.Column(db.String(48), nullable=False)
    reason_text = db.Column(db.Text)
    occurred_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, server_default=db.func.current_timestamp())
    provider_name = db.Column(db.String(48))
    provider_event_id = db.Column(db.String(255))
    provider_message_id = db.Column(db.String(255))
    dedupe_fingerprint = db.Column(db.String(64))
    legacy_consent_event_id = db.Column(db.Integer, db.ForeignKey("customer_consent_event.id", ondelete="RESTRICT"))
    idempotency_key = db.Column(db.String(255))
    metadata_json = db.Column("metadata", _SUPPRESSION_JSON_TYPE)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, server_default=db.func.current_timestamp(), nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, server_default=db.func.current_timestamp(), nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["suppression_case_id", "account_id"],
            ["marketing_suppression_case.id", "marketing_suppression_case.account_id"],
            name="fk_marketing_suppression_event_case_account", ondelete="RESTRICT",
        ),
        UniqueConstraint("idempotency_key", name="uq_marketing_suppression_event_idempotency_key"),
        CheckConstraint("event_type IN ('suppression_detected', 'suppression_observed', 'review_started', 'suppression_cleared', 'legacy_imported')", name="ck_marketing_suppression_event_type"),
        CheckConstraint("actor_type IN ('admin', 'provider', 'system', 'legacy')", name="ck_marketing_suppression_event_actor"),
        CheckConstraint("(actor_type = 'admin' AND admin_id IS NOT NULL) OR (actor_type <> 'admin' AND admin_id IS NULL)", name="ck_marketing_suppression_event_admin_actor"),
        CheckConstraint("source IN ('admin', 'provider', 'legacy_unknown')", name="ck_marketing_suppression_event_source"),
        CheckConstraint("reason_code IN ('admin_review', 'hard_bounce', 'spam_complaint', 'invalid_address', 'provider_unsubscribe_unclassified', 'legacy_unknown')", name="ck_marketing_suppression_event_reason"),
        CheckConstraint("(source = 'admin' AND actor_type = 'admin' AND reason_code = 'admin_review') OR (source = 'provider' AND actor_type IN ('provider', 'admin') AND provider_name IS NOT NULL AND reason_code IN ('hard_bounce', 'spam_complaint', 'invalid_address', 'provider_unsubscribe_unclassified')) OR (source = 'legacy_unknown' AND actor_type IN ('legacy', 'system', 'admin') AND reason_code = 'legacy_unknown')", name="ck_marketing_suppression_event_source_actor_reason"),
        Index("ix_marketing_suppression_event_account_occurred", "account_id", "occurred_at"),
        Index("ix_marketing_suppression_event_case_occurred", "suppression_case_id", "occurred_at"),
        Index("uq_marketing_suppression_event_provider_id", "provider_name", "provider_event_id", unique=True,
              postgresql_where=text("provider_event_id IS NOT NULL"), sqlite_where=text("provider_event_id IS NOT NULL")),
    )


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


class SupportConversation(TimestampMixin, db.Model):
    __tablename__ = "support_conversation"
    id = db.Column(db.Integer, primary_key=True)
    public_id = db.Column(db.String(32), unique=True, nullable=False, index=True)
    account_id = db.Column(
        db.Integer, db.ForeignKey("customer_account.id", ondelete="CASCADE"), index=True
    )
    guest_token_hash = db.Column(db.String(64), index=True)
    topic = db.Column(db.String(24), nullable=False, default="general")
    status = db.Column(db.String(20), nullable=False, default="waiting", index=True)
    assigned_admin_id = db.Column(
        db.Integer, db.ForeignKey("admin_user.id", ondelete="SET NULL"), index=True
    )
    notification_status = db.Column(
        db.String(20), nullable=False, default="not_configured"
    )
    customer_first_name = db.Column(db.String(80), nullable=False, default="")
    customer_last_name = db.Column(db.String(80), nullable=False, default="")
    escalation_requested_at = db.Column(db.DateTime, nullable=True)
    last_message_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    account = db.relationship("CustomerAccount")
    assigned_admin = db.relationship("AdminUser")
    messages = db.relationship(
        "SupportMessage",
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="SupportMessage.id",
    )
    __table_args__ = (
        CheckConstraint(
            "account_id IS NOT NULL OR guest_token_hash IS NOT NULL",
            name="ck_support_conversation_has_owner",
        ),
        CheckConstraint(
            "topic IN ('general', 'orders', 'payments', 'delivery', 'returns', 'products')",
            name="ck_support_conversation_topic",
        ),
        CheckConstraint(
            "status IN ('waiting', 'assigned', 'closed')",
            name="ck_support_conversation_status",
        ),
        CheckConstraint(
            "notification_status IN ('not_configured', 'queued', 'failed')",
            name="ck_support_conversation_notification_status",
        ),
        Index("ix_support_conversation_queue", "status", "created_at"),
        Index("ix_support_conversation_account_status", "account_id", "status"),
        Index(
            "uq_support_open_conversation_account",
            "account_id",
            unique=True,
            postgresql_where=text("account_id IS NOT NULL AND status <> 'closed'"),
            sqlite_where=text("account_id IS NOT NULL AND status <> 'closed'"),
        ),
        Index(
            "uq_support_open_conversation_guest",
            "guest_token_hash",
            unique=True,
            postgresql_where=text("guest_token_hash IS NOT NULL AND status <> 'closed'"),
            sqlite_where=text("guest_token_hash IS NOT NULL AND status <> 'closed'"),
        ),
    )


class SupportMessage(db.Model):
    __tablename__ = "support_message"
    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(
        db.Integer,
        db.ForeignKey("support_conversation.id", ondelete="CASCADE"),
        nullable=False,
    )
    sender_type = db.Column(db.String(12), nullable=False)
    sender_account_id = db.Column(
        db.Integer, db.ForeignKey("customer_account.id", ondelete="SET NULL")
    )
    sender_admin_id = db.Column(
        db.Integer, db.ForeignKey("admin_user.id", ondelete="SET NULL")
    )
    sender_name = db.Column(db.String(100), nullable=False)
    client_message_id = db.Column(db.String(36), nullable=False)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    conversation = db.relationship("SupportConversation", back_populates="messages")
    sender_account = db.relationship("CustomerAccount")
    sender_admin = db.relationship("AdminUser")
    __table_args__ = (
        CheckConstraint(
            "sender_type IN ('customer', 'agent')",
            name="ck_support_message_sender_type",
        ),
        CheckConstraint(
            "length(trim(body)) BETWEEN 1 AND 2000",
            name="ck_support_message_body_length",
        ),
        UniqueConstraint(
            "conversation_id",
            "client_message_id",
            name="uq_support_message_client_id",
        ),
        Index("ix_support_message_conversation_id", "conversation_id", "id"),
    )


class SupportAgentPresence(db.Model):
    __tablename__ = "support_agent_presence"
    admin_id = db.Column(
        db.Integer,
        db.ForeignKey("admin_user.id", ondelete="CASCADE"),
        primary_key=True,
    )
    status = db.Column(db.String(16), nullable=False, default="away")
    heartbeat_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    admin = db.relationship("AdminUser")
    __table_args__ = (
        CheckConstraint(
            "status IN ('available', 'busy', 'away')",
            name="ck_support_agent_presence_status",
        ),
        Index("ix_support_agent_presence_heartbeat", "heartbeat_at"),
    )


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
    purpose = db.Column(db.String(20), nullable=False, default="transactional", server_default="transactional", index=True)
    frequency_override = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    html_body = db.Column(db.Text)
    text_body = db.Column(db.Text)
    claimed_at = db.Column(db.DateTime, index=True)
    last_attempt_at = db.Column(db.DateTime)
    next_attempt_at = db.Column(db.DateTime, index=True)
    error_category = db.Column(db.String(48))
    claim_token = db.Column(db.String(36), index=True)
    provider_message_id = db.Column(db.String(255), index=True)
    sent_at = db.Column(db.DateTime)
    __table_args__ = (
        Index("ix_email_message_dispatch", "status", "next_attempt_at", "created_at"),
    )


class EmailDeliveryAttempt(db.Model):
    """Durable metadata for each provider handoff; never stores message bodies."""
    __tablename__ = "email_delivery_attempt"
    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, db.ForeignKey("email_message.id", ondelete="RESTRICT"), nullable=False)
    attempt_number = db.Column(db.Integer, nullable=False)
    attempt_key = db.Column(db.String(36), nullable=False, unique=True)
    provider = db.Column(db.String(24), nullable=False)
    status = db.Column(db.String(24), nullable=False, default="in_flight")
    started_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)
    provider_message_id = db.Column(db.String(255))
    error_category = db.Column(db.String(48))
    recovery_of_attempt_id = db.Column(db.Integer, db.ForeignKey("email_delivery_attempt.id", ondelete="RESTRICT"))
    __table_args__ = (
        UniqueConstraint("message_id", "attempt_number", name="uq_email_attempt_message_number"),
        Index("ix_email_attempt_message_started", "message_id", "started_at"),
    )


class EmailLegacyPayloadReviewEvent(db.Model):
    """Append-only audit of malformed legacy outbox messages quarantined for review."""
    __tablename__ = "email_legacy_payload_review_event"
    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, db.ForeignKey("email_message.id", ondelete="RESTRICT"), nullable=False, index=True)
    actor_admin_id = db.Column(db.Integer, db.ForeignKey("admin_user.id", ondelete="RESTRICT"))
    actor_type = db.Column(db.String(16), nullable=False)
    action = db.Column(db.String(24), nullable=False, default="quarantined", server_default=text("'quarantined'"))
    reason = db.Column(db.Text, nullable=False)
    provider_outcome = db.Column(db.String(32), nullable=False, default="unknown", server_default=text("'unknown'"))
    provider_evidence = db.Column(db.Text, nullable=False, default="", server_default=text("''"))
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    __table_args__ = (
        CheckConstraint("(actor_type = 'admin' AND actor_admin_id IS NOT NULL) OR (actor_type = 'system' AND actor_admin_id IS NULL)", name="ck_email_legacy_review_actor"),
        CheckConstraint("action IN ('quarantined', 'reviewed')", name="ck_email_legacy_review_action"),
        CheckConstraint("provider_outcome IN ('unknown', 'confirmed_not_accepted', 'confirmed_accepted')", name="ck_email_legacy_review_provider_outcome"),
        Index("ix_email_legacy_review_message_created", "message_id", "created_at"),
    )


class EmailDeliveryInvestigation(TimestampMixin, db.Model):
    """Current investigation state; every change is mirrored to an audit event."""
    __tablename__ = "email_delivery_investigation"
    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, db.ForeignKey("email_message.id", ondelete="RESTRICT"), nullable=False, index=True)
    original_attempt_id = db.Column(db.Integer, db.ForeignKey("email_delivery_attempt.id", ondelete="RESTRICT"))
    provider = db.Column(db.String(24), nullable=False)
    provider_message_id = db.Column(db.String(255))
    reason = db.Column(db.String(255), nullable=False)
    status = db.Column(db.String(40), nullable=False, default="OPEN")
    evidence_summary = db.Column(db.Text)
    evidence_source = db.Column(db.String(255))
    investigator_admin_id = db.Column(db.Integer, db.ForeignKey("admin_user.id", ondelete="RESTRICT"))
    started_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    resolved_at = db.Column(db.DateTime)
    resolution_outcome = db.Column(db.String(40))
    resolution_explanation = db.Column(db.Text)
    recovered_message_id = db.Column(db.Integer, db.ForeignKey("email_message.id", ondelete="RESTRICT"), unique=True)
    message = db.relationship("EmailMessage", foreign_keys=[message_id])
    __table_args__ = (
        CheckConstraint("status IN ('OPEN','INVESTIGATING','WAITING_FOR_PROVIDER_EVIDENCE','CONFIRMED_ACCEPTED','CONFIRMED_NOT_ACCEPTED','INCONCLUSIVE','RESOLVED')", name="ck_email_investigation_status"),
        CheckConstraint("resolution_outcome IS NULL OR resolution_outcome IN ('accepted','not_accepted','inconclusive')", name="ck_email_investigation_outcome"),
        Index("uq_email_investigation_open_message", "message_id", unique=True,
              postgresql_where=text("status <> 'RESOLVED'"),
              sqlite_where=text("status <> 'RESOLVED'")),
    )


class EmailDeliveryInvestigationEvent(db.Model):
    """Append-only audit trail for investigation notes, decisions and recovery."""
    __tablename__ = "email_delivery_investigation_event"
    id = db.Column(db.Integer, primary_key=True)
    investigation_id = db.Column(db.Integer, db.ForeignKey("email_delivery_investigation.id", ondelete="RESTRICT"), nullable=False, index=True)
    actor_admin_id = db.Column(db.Integer, db.ForeignKey("admin_user.id", ondelete="RESTRICT"), nullable=False)
    event_type = db.Column(db.String(32), nullable=False)
    from_status = db.Column(db.String(40))
    to_status = db.Column(db.String(40))
    evidence_summary = db.Column(db.Text)
    evidence_source = db.Column(db.String(255))
    explanation = db.Column(db.Text)
    related_attempt_id = db.Column(db.Integer, db.ForeignKey("email_delivery_attempt.id", ondelete="RESTRICT"))
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class EmailWorkerRun(db.Model):
    """One-shot worker instance liveness and bounded execution history."""
    __tablename__ = "email_worker_run"
    id = db.Column(db.Integer, primary_key=True)
    instance_id = db.Column(db.String(36), nullable=False, unique=True)
    worker_type = db.Column(db.String(32), nullable=False, default="queue")
    status = db.Column(db.String(16), nullable=False, default="STARTING")
    started_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    last_heartbeat_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    last_success_at = db.Column(db.DateTime)
    last_attempt_at = db.Column(db.DateTime)
    current_batch_count = db.Column(db.Integer, nullable=False, default=0)
    stopped_at = db.Column(db.DateTime)
    last_error_category = db.Column(db.String(48))
    __table_args__ = (
        CheckConstraint("status IN ('STARTING','RUNNING','STOPPING','STOPPED','FAILED')", name="ck_email_worker_run_status"),
        Index("ix_email_worker_run_status_heartbeat", "status", "last_heartbeat_at"),
    )


class EmailCampaign(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    subject = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text, nullable=False)
    campaign_type = db.Column(db.String(32), nullable=False, default="custom", server_default="custom")
    product_ids_json = db.Column(db.Text, nullable=False, default="[]", server_default="[]")
    scheduled_for = db.Column(db.DateTime)
    previewed_at = db.Column(db.DateTime)
    approved_at = db.Column(db.DateTime)
    status = db.Column(db.String(24), default="draft", nullable=False, index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("admin_user.id"), nullable=False)
    sent_at = db.Column(db.DateTime)
    generation_key = db.Column(db.String(160))
    generation_source = db.Column(db.String(32))
    generation_reason = db.Column(db.String(255))
    generation_reference = db.Column(db.Text)
    admin = db.relationship("AdminUser")
    __table_args__ = (
        Index("ix_email_campaign_status_scheduled", "status", "scheduled_for"),
        Index("uq_email_campaign_generation_key", "generation_key", unique=True,
              postgresql_where=text("generation_key IS NOT NULL"),
              sqlite_where=text("generation_key IS NOT NULL")),
    )


class EmailCampaignRecipient(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("email_campaign.id"), nullable=False, index=True)
    account_id = db.Column(db.Integer, db.ForeignKey("customer_account.id"), nullable=False, index=True)
    recipient = db.Column(db.String(254), nullable=False)
    status = db.Column(db.String(24), default="queued", nullable=False, index=True)
    failure_reason = db.Column(db.String(255), default="")
    exclusion_reason = db.Column(db.String(48), nullable=False, default="", server_default="")
    message_id = db.Column(db.Integer)
    sent_at = db.Column(db.DateTime)
    campaign = db.relationship("EmailCampaign")
    account = db.relationship("CustomerAccount")
    __table_args__ = (
        UniqueConstraint("campaign_id", "account_id", name="uq_campaign_recipient_account"),
        Index("ix_email_campaign_recipient_message", "message_id"),
    )


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
    queued_count = db.Column(db.Integer, nullable=False, default=0, server_default="0")
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
