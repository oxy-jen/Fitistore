from __future__ import annotations

import click
from pathlib import Path
from flask import Flask
from werkzeug.security import generate_password_hash

from config import Config
from .extensions import db, login_manager, csrf
from .models import AdminUser, Category, Order, OrderItem, Product, ProductImage, ShopSettings
from .services.product_images import ImageStorageError, legacy_local_image_path, upload_image_bytes

DEFAULT_CATEGORIES = [
    "Ankle socks", "Hair bonnets", "Shower caps", "Panties", "Vests", "Boxers", "Hair bands", "Shoes"
]

STARTER_PRODUCTS = [
    {
        "name": "Satin hair bonnet",
        "category": "Hair bonnets",
        "price_cents": 12000,
        "stock_quantity": 12,
        "description": "Starter listing for satin hair bonnets. Replace the sample image with your real stock photo before selling.",
        "sku": "BF-BONNET-120",
        "featured": True,
        "image": "sample-bonnet.svg",
        "alt": "Sample satin hair bonnet product artwork",
    },
    {
        "name": "Ankle socks pair",
        "category": "Ankle socks",
        "price_cents": 4000,
        "stock_quantity": 30,
        "description": "Confirmed price: KSh 40 per pair. Add colours and real stock images from the admin dashboard.",
        "sku": "BF-SOCKS-40",
        "featured": True,
        "image": "sample-ankle-socks.svg",
        "alt": "Sample ankle socks product artwork",
    },
    {
        "name": "Hair band - basic",
        "category": "Hair bands",
        "price_cents": 1000,
        "stock_quantity": 40,
        "description": "Basic hair band at the confirmed KSh 10 selling price.",
        "sku": "BF-HAIRBAND-10",
        "image": "sample-hair-band-basic.svg",
        "alt": "Sample basic hair band product artwork",
    },
    {
        "name": "Hair band - standard",
        "category": "Hair bands",
        "price_cents": 5000,
        "stock_quantity": 20,
        "description": "Standard hair band at the confirmed KSh 50 selling price.",
        "sku": "BF-HAIRBAND-50",
        "image": "sample-hair-band-standard.svg",
        "alt": "Sample standard hair band product artwork",
    },
    {
        "name": "Hair band - premium",
        "category": "Hair bands",
        "price_cents": 6000,
        "stock_quantity": 15,
        "description": "Premium hair band at the confirmed KSh 60 selling price.",
        "sku": "BF-HAIRBAND-60",
        "featured": True,
        "image": "sample-hair-band-premium.svg",
        "alt": "Sample premium hair band product artwork",
    },
    {
        "name": "Shower cap",
        "category": "Shower caps",
        "price_cents": None,
        "stock_quantity": 0,
        "description": "Price to be entered by the owner. This listing stays inactive until real price and stock are confirmed.",
        "sku": "BF-SHOWER-CAP",
        "active": False,
        "image": "sample-shower-cap.svg",
        "alt": "Sample shower cap product artwork",
    },
    {
        "name": "Underwear",
        "category": "Panties",
        "price_cents": None,
        "stock_quantity": 0,
        "description": "Owner-managed listing. Add confirmed price, sizes, stock, and real product photos in admin.",
        "sku": "BF-UNDERWEAR",
        "active": False,
        "image": "sample-underwear.svg",
        "alt": "Sample underwear product artwork",
    },
    {
        "name": "Vest",
        "category": "Vests",
        "price_cents": None,
        "stock_quantity": 0,
        "description": "Owner-managed listing. Add confirmed price, sizes, stock, and real product photos in admin.",
        "sku": "BF-VEST",
        "active": False,
        "image": "sample-vest.svg",
        "alt": "Sample vest product artwork",
    },
    {
        "name": "Boxer",
        "category": "Boxers",
        "price_cents": None,
        "stock_quantity": 0,
        "description": "Owner-managed listing. Add confirmed price, sizes, stock, and real product photos in admin.",
        "sku": "BF-BOXER",
        "active": False,
        "image": "sample-boxer.svg",
        "alt": "Sample boxer product artwork",
    },
    {
        "name": "Shoes",
        "category": "Shoes",
        "price_cents": None,
        "stock_quantity": 0,
        "description": "Owner-managed listing. Add confirmed price, sizes, stock, and real product photos in admin.",
        "sku": "BF-SHOES",
        "active": False,
        "image": "sample-shoes.svg",
        "alt": "Sample shoes product artwork",
    },
]

SAMPLE_IMAGES = {
    "sample-bonnet.svg": ("Satin bonnet", "#E52335", "M 120 245 C 140 120 300 75 405 170 C 470 230 420 365 270 365 C 170 365 95 315 120 245 Z"),
    "sample-ankle-socks.svg": ("Ankle socks", "#252525", "M 150 120 H 230 V 265 H 360 C 410 265 430 330 385 350 H 185 C 155 350 135 330 135 300 V 120 Z M 285 120 H 365 V 250"),
    "sample-hair-band-basic.svg": ("Hair band", "#08A64A", "M 170 255 C 170 145 370 145 370 255 C 370 365 170 365 170 255 Z M 220 255 C 220 205 320 205 320 255 C 320 305 220 305 220 255 Z"),
    "sample-hair-band-standard.svg": ("Hair band", "#E52335", "M 165 260 C 165 130 375 130 375 260 C 375 390 165 390 165 260 Z M 220 260 C 220 195 320 195 320 260 C 320 325 220 325 220 260 Z"),
    "sample-hair-band-premium.svg": ("Hair band", "#252525", "M 160 260 C 160 125 380 125 380 260 C 380 395 160 395 160 260 Z M 225 260 C 225 200 315 200 315 260 C 315 320 225 320 225 260 Z"),
    "sample-shower-cap.svg": ("Shower cap", "#08A64A", "M 105 275 C 120 140 420 140 435 275 C 430 345 365 382 270 382 C 175 382 110 345 105 275 Z"),
    "sample-underwear.svg": ("Underwear", "#E52335", "M 135 170 H 405 L 360 360 H 300 L 270 275 L 240 360 H 180 Z"),
    "sample-vest.svg": ("Vest", "#252525", "M 180 105 H 235 C 240 160 300 160 305 105 H 360 L 405 380 H 135 Z"),
    "sample-boxer.svg": ("Boxer", "#08A64A", "M 135 150 H 405 V 370 H 300 L 270 245 L 240 370 H 135 Z"),
    "sample-shoes.svg": ("Shoes", "#E52335", "M 115 315 C 170 300 205 260 225 210 L 295 210 C 315 275 375 300 430 315 C 455 322 465 360 435 370 H 130 C 100 365 90 325 115 315 Z"),
}

def create_app(config_object=Config):
    app = Flask(__name__)
    app.config.from_object(config_object)
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)

    from .public.routes import public_bp
    from .admin.routes import admin_bp
    app.register_blueprint(public_bp)
    app.register_blueprint(admin_bp, url_prefix="/admin")

    register_cli(app)
    register_errors(app)
    ensure_database_schema(app)
    return app

def ensure_database_schema(app: Flask):
    """Keep local SQLite installs aligned before the first request queries models."""
    with app.app_context():
        db.create_all()
        ensure_columns()
def register_cli(app: Flask):
    @app.cli.command("init-db")
    def init_db():
        """Create tables, default categories, settings, starter catalogue, and optional first admin from env."""
        db.create_all()
        ensure_columns()
        ensure_sample_images(app)
        categories = ensure_categories()
        ensure_starter_products(categories)
        env = __import__("os").environ
        whatsapp_number = env.get("SHOP_WHATSAPP_NUMBER", "254791305329")
        payment_number = env.get("SHOP_PAYMENT_NUMBER", "0141809661")
        settings = ShopSettings.query.first()
        if not settings:
            settings = ShopSettings(shop_name="FITY", tagline="Bei Fity, Fits Poa.")
            db.session.add(settings)
        if not settings.whatsapp_number:
            settings.whatsapp_number = whatsapp_number
        if not settings.payment_number:
            settings.payment_number = payment_number
        username = app.config.get("ADMIN_USERNAME") or env.get("ADMIN_USERNAME")
        password = app.config.get("ADMIN_PASSWORD") or env.get("ADMIN_PASSWORD")
        if username and password and not AdminUser.query.filter_by(username=username).first():
            db.session.add(AdminUser(username=username, password_hash=generate_password_hash(password)))
            click.echo(f"Created admin user: {username}")
        db.session.commit()
        click.echo("FITY database is ready.")


    @app.cli.command("migrate-product-images")
    def migrate_product_images():
        """Upload accessible legacy local product images to Cloudinary."""
        db.create_all()
        ensure_columns()
        migrated = skipped = failed = missing = 0
        for image in ProductImage.query.order_by(ProductImage.id).all():
            if image.cloudinary_public_id:
                skipped += 1
                click.echo(f"skip image {image.id}: already has Cloudinary public id")
                continue
            if image.filename.startswith("sample-"):
                skipped += 1
                click.echo(f"skip image {image.id}: sample image")
                continue
            image_path = legacy_local_image_path(image)
            if not image_path or not image_path.is_file():
                missing += 1
                click.echo(f"missing image {image.id}: {image.filename}")
                continue
            public_id = f"fity/legacy/product-image-{image.id}"
            try:
                upload_image_bytes(image_path.read_bytes(), public_id)
            except ImageStorageError as exc:
                failed += 1
                db.session.rollback()
                click.echo(f"fail image {image.id}: {exc}")
                continue
            image.cloudinary_public_id = public_id
            image.storage_backend = "cloudinary"
            db.session.commit()
            migrated += 1
            click.echo(f"migrated image {image.id}: {image.filename}")
        click.echo(f"Done. migrated={migrated} skipped={skipped} missing={missing} failed={failed}")

    @app.cli.command("create-admin")
    @click.argument("username")
    @click.password_option()
    def create_admin(username, password):
        db.create_all()
        ensure_columns()
        if AdminUser.query.filter_by(username=username).first():
            raise click.ClickException("That username already exists.")
        db.session.add(AdminUser(username=username, password_hash=generate_password_hash(password)))
        db.session.commit()
        click.echo(f"Created admin user: {username}")

def register_errors(app: Flask):
    @app.errorhandler(404)
    def not_found(error):
        return __import__("flask").render_template("errors/404.html"), 404

    @app.errorhandler(500)
    def server_error(error):
        return __import__("flask").render_template("errors/500.html"), 500

def slugify(value: str) -> str:
    import re
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value.lower()).strip("-")
    return value or "item"

def ensure_categories():
    categories = {}
    for name in DEFAULT_CATEGORIES:
        category = Category.query.filter_by(name=name).first()
        if not category:
            category = Category(name=name, slug=slugify(name))
            db.session.add(category)
        categories[name] = category
    db.session.flush()
    return categories

def ensure_starter_products(categories):
    for item in STARTER_PRODUCTS:
        product = Product.query.filter_by(sku=item["sku"]).first() or Product.query.filter_by(slug=slugify(item["name"])).first()
        if product:
            continue
        product = Product(
            name=item["name"],
            slug=unique_product_slug(item["name"]),
            sku=item["sku"],
            category=categories[item["category"]],
            description=item["description"],
            price_cents=item["price_cents"],
            stock_quantity=item["stock_quantity"],
            low_stock_threshold=3,
            active=item.get("active", True),
            featured=item.get("featured", False),
        )
        product.images.append(ProductImage(filename=item["image"], alt_text=item["alt"], is_primary=True))
        db.session.add(product)

def unique_product_slug(name, product_id=None):
    base = slugify(name)
    slug = base
    i = 2
    while True:
        query = Product.query.filter_by(slug=slug)
        if product_id:
            query = query.filter(Product.id != product_id)
        if not query.first():
            return slug
        slug = f"{base}-{i}"
        i += 1

def ensure_sample_images(app):
    upload_dir = Path(app.config["UPLOAD_FOLDER"])
    upload_dir.mkdir(parents=True, exist_ok=True)
    for filename, (label, color, path_data) in SAMPLE_IMAGES.items():
        path = upload_dir / filename
        if path.exists():
            continue
        svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 540 540" role="img" aria-label="{label} sample product image"><rect width="540" height="540" rx="34" fill="#fff"/><circle cx="420" cy="120" r="54" fill="#F2F2F2"/><path d="{path_data}" fill="{color}" opacity="0.94"/><path d="M90 420 H450" stroke="#D1D5DB" stroke-width="18" stroke-linecap="round"/><text x="270" y="475" text-anchor="middle" font-family="Arial, sans-serif" font-size="28" font-weight="700" fill="#252525">{label}</text><text x="270" y="505" text-anchor="middle" font-family="Arial, sans-serif" font-size="16" fill="#606060">sample image - replace with owner photo</text></svg>'''
        path.write_text(svg, encoding="utf-8")

def ensure_columns():
    """Small SQLite-friendly migration helper for beginner local installs."""
    from sqlalchemy import inspect, text
    inspector = inspect(db.engine)
    table_names = inspector.get_table_names()

    if "product" in table_names:
        product_columns = {column["name"] for column in inspector.get_columns("product")}
        if "sku" not in product_columns:
            db.session.execute(text('ALTER TABLE product ADD COLUMN sku VARCHAR(80) DEFAULT ""'))
        if "previous_price_cents" not in product_columns:
            db.session.execute(text('ALTER TABLE product ADD COLUMN previous_price_cents INTEGER'))
        if "reserved_quantity" not in product_columns:
            db.session.execute(text('ALTER TABLE product ADD COLUMN reserved_quantity INTEGER DEFAULT 0 NOT NULL'))
        if "unit_cost_cents" not in product_columns:
            db.session.execute(text('ALTER TABLE product ADD COLUMN unit_cost_cents INTEGER'))
        if "low_stock_threshold" not in product_columns:
            db.session.execute(text('ALTER TABLE product ADD COLUMN low_stock_threshold INTEGER DEFAULT 3 NOT NULL'))
        if "active" not in product_columns:
            db.session.execute(text('ALTER TABLE product ADD COLUMN active BOOLEAN DEFAULT 1 NOT NULL'))
        if "featured" not in product_columns:
            db.session.execute(text('ALTER TABLE product ADD COLUMN featured BOOLEAN DEFAULT 0 NOT NULL'))
        if "size_options" not in product_columns:
            db.session.execute(text('ALTER TABLE product ADD COLUMN size_options VARCHAR(255) DEFAULT ""'))
        if "color_options" not in product_columns:
            db.session.execute(text('ALTER TABLE product ADD COLUMN color_options VARCHAR(255) DEFAULT ""'))

    if "product_image" in table_names:
        image_columns = {column["name"] for column in inspector.get_columns("product_image")}
        if "alt_text" not in image_columns:
            db.session.execute(text('ALTER TABLE product_image ADD COLUMN alt_text VARCHAR(255) DEFAULT "Product image" NOT NULL'))
        if "sort_order" not in image_columns:
            db.session.execute(text('ALTER TABLE product_image ADD COLUMN sort_order INTEGER DEFAULT 0 NOT NULL'))
        if "is_primary" not in image_columns:
            db.session.execute(text('ALTER TABLE product_image ADD COLUMN is_primary BOOLEAN DEFAULT 0 NOT NULL'))
        if "storage_backend" not in image_columns:
            db.session.execute(text("ALTER TABLE product_image ADD COLUMN storage_backend VARCHAR(20) DEFAULT 'local' NOT NULL"))
        if "cloudinary_public_id" not in image_columns:
            db.session.execute(text('ALTER TABLE product_image ADD COLUMN cloudinary_public_id VARCHAR(255)'))

    if "product_variant" in table_names:
        variant_columns = {column["name"] for column in inspector.get_columns("product_variant")}
        if "sku" not in variant_columns:
            db.session.execute(text('ALTER TABLE product_variant ADD COLUMN sku VARCHAR(80) DEFAULT ""'))
        if "size" not in variant_columns:
            db.session.execute(text('ALTER TABLE product_variant ADD COLUMN size VARCHAR(50) DEFAULT ""'))
        if "color" not in variant_columns:
            db.session.execute(text('ALTER TABLE product_variant ADD COLUMN color VARCHAR(50) DEFAULT ""'))
        if "style" not in variant_columns:
            db.session.execute(text('ALTER TABLE product_variant ADD COLUMN style VARCHAR(80) DEFAULT ""'))
        if "material" not in variant_columns:
            db.session.execute(text('ALTER TABLE product_variant ADD COLUMN material VARCHAR(80) DEFAULT ""'))
        if "pack_size" not in variant_columns:
            db.session.execute(text('ALTER TABLE product_variant ADD COLUMN pack_size VARCHAR(80) DEFAULT ""'))
        if "price_cents" not in variant_columns:
            db.session.execute(text('ALTER TABLE product_variant ADD COLUMN price_cents INTEGER'))
        if "unit_cost_cents" not in variant_columns:
            db.session.execute(text('ALTER TABLE product_variant ADD COLUMN unit_cost_cents INTEGER'))
        if "stock_quantity" not in variant_columns:
            db.session.execute(text('ALTER TABLE product_variant ADD COLUMN stock_quantity INTEGER DEFAULT 0 NOT NULL'))
        if "reserved_quantity" not in variant_columns:
            db.session.execute(text('ALTER TABLE product_variant ADD COLUMN reserved_quantity INTEGER DEFAULT 0 NOT NULL'))
        if "low_stock_threshold" not in variant_columns:
            db.session.execute(text('ALTER TABLE product_variant ADD COLUMN low_stock_threshold INTEGER DEFAULT 3 NOT NULL'))
        if "active" not in variant_columns:
            db.session.execute(text('ALTER TABLE product_variant ADD COLUMN active BOOLEAN DEFAULT 1 NOT NULL'))
        if "image_id" not in variant_columns:
            db.session.execute(text('ALTER TABLE product_variant ADD COLUMN image_id INTEGER'))
        if "sort_order" not in variant_columns:
            db.session.execute(text('ALTER TABLE product_variant ADD COLUMN sort_order INTEGER DEFAULT 0 NOT NULL'))

    if "inventory_movement" in table_names:
        movement_columns = {column["name"] for column in inspector.get_columns("inventory_movement")}
        if "admin_id" not in movement_columns:
            db.session.execute(text('ALTER TABLE inventory_movement ADD COLUMN admin_id INTEGER'))
        if "variant_id" not in movement_columns:
            db.session.execute(text('ALTER TABLE inventory_movement ADD COLUMN variant_id INTEGER'))

    if "order" in table_names:
        order_columns = {column["name"] for column in inspector.get_columns("order")}
        if "tracking_token" not in order_columns:
            db.session.execute(text('ALTER TABLE "order" ADD COLUMN tracking_token VARCHAR(96) DEFAULT "" NOT NULL'))
        if "payment_reference" not in order_columns:
            db.session.execute(text('ALTER TABLE "order" ADD COLUMN payment_reference VARCHAR(120) DEFAULT ""'))
        if "payment_proof_filename" not in order_columns:
            db.session.execute(text('ALTER TABLE "order" ADD COLUMN payment_proof_filename VARCHAR(255) DEFAULT ""'))
        if "delivery_fee_cents" not in order_columns:
            db.session.execute(text('ALTER TABLE "order" ADD COLUMN delivery_fee_cents INTEGER DEFAULT 0 NOT NULL'))
        if "discount_cents" not in order_columns:
            db.session.execute(text('ALTER TABLE "order" ADD COLUMN discount_cents INTEGER DEFAULT 0 NOT NULL'))
        if "has_unconfirmed_prices" not in order_columns:
            db.session.execute(text('ALTER TABLE "order" ADD COLUMN has_unconfirmed_prices BOOLEAN DEFAULT 0 NOT NULL'))
        if "notes" not in order_columns:
            db.session.execute(text('ALTER TABLE "order" ADD COLUMN notes TEXT DEFAULT ""'))
        for order in Order.query.filter((Order.tracking_token == None) | (Order.tracking_token == "")).all():
            from secrets import token_urlsafe
            order.tracking_token = token_urlsafe(32)

    if "order_item" in table_names:
        item_columns = {column["name"] for column in inspector.get_columns("order_item")}
        if "unit_cost_cents" not in item_columns:
            db.session.execute(text('ALTER TABLE order_item ADD COLUMN unit_cost_cents INTEGER'))
        if "variant_id" not in item_columns:
            db.session.execute(text('ALTER TABLE order_item ADD COLUMN variant_id INTEGER'))
        if "variant_label" not in item_columns:
            db.session.execute(text('ALTER TABLE order_item ADD COLUMN variant_label VARCHAR(255) DEFAULT ""'))
        if "sku" not in item_columns:
            db.session.execute(text('ALTER TABLE order_item ADD COLUMN sku VARCHAR(80) DEFAULT ""'))
        if "size" not in item_columns:
            db.session.execute(text('ALTER TABLE order_item ADD COLUMN size VARCHAR(50)'))
        if "color" not in item_columns:
            db.session.execute(text('ALTER TABLE order_item ADD COLUMN color VARCHAR(50)'))

    if "payment_attempt" in table_names:
        attempt_columns = {column["name"] for column in inspector.get_columns("payment_attempt")}
        if "provider" not in attempt_columns:
            db.session.execute(text('ALTER TABLE payment_attempt ADD COLUMN provider VARCHAR(40) DEFAULT "Manual M-Pesa" NOT NULL'))
        if "status" not in attempt_columns:
            db.session.execute(text('ALTER TABLE payment_attempt ADD COLUMN status VARCHAR(30) DEFAULT "pending" NOT NULL'))
        if "expected_amount_cents" not in attempt_columns:
            db.session.execute(text('ALTER TABLE payment_attempt ADD COLUMN expected_amount_cents INTEGER'))
        if "merchant_request_id" not in attempt_columns:
            db.session.execute(text('ALTER TABLE payment_attempt ADD COLUMN merchant_request_id VARCHAR(120) DEFAULT ""'))
        if "checkout_request_id" not in attempt_columns:
            db.session.execute(text('ALTER TABLE payment_attempt ADD COLUMN checkout_request_id VARCHAR(120) DEFAULT ""'))
        if "customer_phone" not in attempt_columns:
            db.session.execute(text('ALTER TABLE payment_attempt ADD COLUMN customer_phone VARCHAR(40) DEFAULT ""'))
        if "result_code" not in attempt_columns:
            db.session.execute(text('ALTER TABLE payment_attempt ADD COLUMN result_code VARCHAR(30) DEFAULT ""'))
        if "result_description" not in attempt_columns:
            db.session.execute(text('ALTER TABLE payment_attempt ADD COLUMN result_description VARCHAR(255) DEFAULT ""'))
        if "raw_payload" not in attempt_columns:
            db.session.execute(text('ALTER TABLE payment_attempt ADD COLUMN raw_payload TEXT DEFAULT ""'))
        if "payment_method" not in attempt_columns:
            db.session.execute(text('ALTER TABLE payment_attempt ADD COLUMN payment_method VARCHAR(40) DEFAULT "Manual M-Pesa"'))
        if "initiated_at" not in attempt_columns:
            db.session.execute(text('ALTER TABLE payment_attempt ADD COLUMN initiated_at DATETIME'))
        if "confirmed_at" not in attempt_columns:
            db.session.execute(text('ALTER TABLE payment_attempt ADD COLUMN confirmed_at DATETIME'))
        if "last_reconciled_at" not in attempt_columns:
            db.session.execute(text('ALTER TABLE payment_attempt ADD COLUMN last_reconciled_at DATETIME'))
        if "investigation_note" not in attempt_columns:
            db.session.execute(text('ALTER TABLE payment_attempt ADD COLUMN investigation_note TEXT DEFAULT ""'))

    if "payment_transaction" in table_names:
        transaction_columns = {column["name"] for column in inspector.get_columns("payment_transaction")}
        if "order_id" not in transaction_columns:
            db.session.execute(text('ALTER TABLE payment_transaction ADD COLUMN order_id INTEGER'))
        if "payment_attempt_id" not in transaction_columns:
            db.session.execute(text('ALTER TABLE payment_transaction ADD COLUMN payment_attempt_id INTEGER'))
        if "provider" not in transaction_columns:
            db.session.execute(text('ALTER TABLE payment_transaction ADD COLUMN provider VARCHAR(40) DEFAULT "M-Pesa" NOT NULL'))
        if "mpesa_receipt" not in transaction_columns:
            db.session.execute(text('ALTER TABLE payment_transaction ADD COLUMN mpesa_receipt VARCHAR(120)'))
        if "amount_cents" not in transaction_columns:
            db.session.execute(text('ALTER TABLE payment_transaction ADD COLUMN amount_cents INTEGER DEFAULT 0 NOT NULL'))
        if "phone" not in transaction_columns:
            db.session.execute(text('ALTER TABLE payment_transaction ADD COLUMN phone VARCHAR(40) DEFAULT ""'))
        if "transaction_time" not in transaction_columns:
            db.session.execute(text('ALTER TABLE payment_transaction ADD COLUMN transaction_time DATETIME'))
        if "status" not in transaction_columns:
            db.session.execute(text('ALTER TABLE payment_transaction ADD COLUMN status VARCHAR(30) DEFAULT "verified" NOT NULL'))
        if "discrepancy" not in transaction_columns:
            db.session.execute(text('ALTER TABLE payment_transaction ADD COLUMN discrepancy VARCHAR(255) DEFAULT ""'))
        if "raw_payload" not in transaction_columns:
            db.session.execute(text('ALTER TABLE payment_transaction ADD COLUMN raw_payload TEXT DEFAULT ""'))

    if "refund_request" in table_names:
        refund_columns = {column["name"] for column in inspector.get_columns("refund_request")}
        if "payment_transaction_id" not in refund_columns:
            db.session.execute(text('ALTER TABLE refund_request ADD COLUMN payment_transaction_id INTEGER'))
        if "status" not in refund_columns:
            db.session.execute(text('ALTER TABLE refund_request ADD COLUMN status VARCHAR(30) DEFAULT "requested" NOT NULL'))
        if "reference" not in refund_columns:
            db.session.execute(text('ALTER TABLE refund_request ADD COLUMN reference VARCHAR(120) DEFAULT ""'))
        if "completed_at" not in refund_columns:
            db.session.execute(text('ALTER TABLE refund_request ADD COLUMN completed_at DATETIME'))
        if "admin_id" not in refund_columns:
            db.session.execute(text('ALTER TABLE refund_request ADD COLUMN admin_id INTEGER'))
        if "note" not in refund_columns:
            db.session.execute(text('ALTER TABLE refund_request ADD COLUMN note TEXT DEFAULT ""'))

    if "expense" in table_names:
        expense_columns = {column["name"] for column in inspector.get_columns("expense")}
        if "reference" not in expense_columns:
            db.session.execute(text('ALTER TABLE expense ADD COLUMN reference VARCHAR(120) DEFAULT ""'))
        if "admin_id" not in expense_columns:
            db.session.execute(text('ALTER TABLE expense ADD COLUMN admin_id INTEGER'))
        if "voided" not in expense_columns:
            db.session.execute(text('ALTER TABLE expense ADD COLUMN voided BOOLEAN DEFAULT 0 NOT NULL'))
        if "correction_note" not in expense_columns:
            db.session.execute(text('ALTER TABLE expense ADD COLUMN correction_note TEXT DEFAULT ""'))
    if "shop_settings" in table_names:
        settings_columns = {column["name"] for column in inspector.get_columns("shop_settings")}
        if "payment_number" not in settings_columns:
            db.session.execute(text('ALTER TABLE shop_settings ADD COLUMN payment_number VARCHAR(32) DEFAULT "0141809661"'))
        if "contact_email" not in settings_columns:
            db.session.execute(text('ALTER TABLE shop_settings ADD COLUMN contact_email VARCHAR(120) DEFAULT ""'))
        if "public_location" not in settings_columns:
            db.session.execute(text('ALTER TABLE shop_settings ADD COLUMN public_location VARCHAR(255) DEFAULT "Othaya Mathenge Technical Training Institute"'))
        if "collection_instructions" not in settings_columns:
            db.session.execute(text('ALTER TABLE shop_settings ADD COLUMN collection_instructions TEXT DEFAULT "Collection point: Othaya Mathenge Technical Training Institute. Confirm the exact meeting spot with the seller before travelling."'))
        if "delivery_area" not in settings_columns:
            db.session.execute(text('ALTER TABLE shop_settings ADD COLUMN delivery_area TEXT DEFAULT "Nearby delivery can be discussed on WhatsApp or by calling 0791305329 before payment."'))
        if "delivery_fee_cents" not in settings_columns:
            db.session.execute(text('ALTER TABLE shop_settings ADD COLUMN delivery_fee_cents INTEGER'))
        if "payment_instructions" not in settings_columns:
            db.session.execute(text('ALTER TABLE shop_settings ADD COLUMN payment_instructions TEXT DEFAULT "M-Pesa payment number: 0141809661. Please confirm item availability first, then enter the M-Pesa reference code at checkout so it appears in the WhatsApp message."'))
        if "currency" not in settings_columns:
            db.session.execute(text('ALTER TABLE shop_settings ADD COLUMN currency VARCHAR(10) DEFAULT "KSh" NOT NULL'))
        if "default_low_stock_threshold" not in settings_columns:
            db.session.execute(text('ALTER TABLE shop_settings ADD COLUMN default_low_stock_threshold INTEGER DEFAULT 3 NOT NULL'))
        if "loyalty_enabled" not in settings_columns:
            db.session.execute(text('ALTER TABLE shop_settings ADD COLUMN loyalty_enabled BOOLEAN DEFAULT 0 NOT NULL'))
        if "loyalty_points_per_100" not in settings_columns:
            db.session.execute(text('ALTER TABLE shop_settings ADD COLUMN loyalty_points_per_100 INTEGER DEFAULT 1 NOT NULL'))
        if "loyalty_min_redeem_points" not in settings_columns:
            db.session.execute(text('ALTER TABLE shop_settings ADD COLUMN loyalty_min_redeem_points INTEGER DEFAULT 100 NOT NULL'))
        if "loyalty_point_value_cents" not in settings_columns:
            db.session.execute(text('ALTER TABLE shop_settings ADD COLUMN loyalty_point_value_cents INTEGER DEFAULT 100 NOT NULL'))
        if "referral_enabled" not in settings_columns:
            db.session.execute(text('ALTER TABLE shop_settings ADD COLUMN referral_enabled BOOLEAN DEFAULT 0 NOT NULL'))
        if "referral_reward_points" not in settings_columns:
            db.session.execute(text('ALTER TABLE shop_settings ADD COLUMN referral_reward_points INTEGER DEFAULT 50 NOT NULL'))
        if "referral_min_order_cents" not in settings_columns:
            db.session.execute(text('ALTER TABLE shop_settings ADD COLUMN referral_min_order_cents INTEGER DEFAULT 0 NOT NULL'))

    db.session.commit()


