import io
from datetime import datetime

import pytest
import app as app_module
from werkzeug.security import check_password_hash, generate_password_hash
from app import create_app
from app.extensions import db
from app.models import AdminUser, AuthRateLimit, Category, CustomerAccount, CustomerConsentEvent, CustomerOrderLink, CustomerToken, EmailCampaign, EmailCampaignRecipient, EmailMessage, Order, OrderItem, Product, ProductImage, ProductSuggestion, Review, ShopSettings
from config import TestConfig

@pytest.fixture()
def app():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        settings = ShopSettings(shop_name="FITY", tagline="Bei Fity, Fits Poa.", whatsapp_number="254700000000")
        category = Category(name="Ankle socks", slug="ankle-socks")
        db.session.add_all([settings, category, AdminUser(username="owner", password_hash=generate_password_hash("secret123"))])
        db.session.flush()
        db.session.add(Product(name="Plain Socks", slug="plain-socks", category=category, description="Soft socks", price_cents=15000, previous_price_cents=18000, stock_quantity=4, low_stock_threshold=2, active=True, featured=True))
        db.session.add(Product(name="Unpriced Cap", slug="unpriced-cap", category=category, description="Price later", price_cents=None, stock_quantity=2, active=True))
        db.session.commit()
    yield app

@pytest.fixture()
def client(app):
    return app.test_client()

def test_homepage_renders(client):
    rv = client.get("/")
    assert rv.status_code == 200
    assert b"Fresh essentials" in rv.data

def test_shop_search_and_category_filter(client):
    rv = client.get("/shop?q=Plain&category=ankle-socks")
    assert rv.status_code == 200
    assert b"Plain Socks" in rv.data
    assert b"Unpriced Cap" not in rv.data

def test_product_detail(client):
    rv = client.get("/product/plain-socks")
    assert rv.status_code == 200
    assert b"Plain Socks" in rv.data
    assert b"KSh 150" in rv.data

def test_cart_validation_limits_stock(client):
    rv = client.post("/api/cart/validate", json={"items":[{"product_id":1,"quantity":9}]})
    data = rv.get_json()
    assert data["lines"][0]["quantity"] == 4
    assert data["errors"]

def test_missing_price_is_not_zero(client):
    rv = client.post("/api/cart/validate", json={"items":[{"product_id":2,"quantity":1}]})
    data = rv.get_json()
    assert data["has_unconfirmed_prices"] is True
    assert data["total_cents"] is None
    assert data["lines"][0]["price_cents"] is None

def test_invalid_product_id(client):
    rv = client.post("/api/cart/validate", json={"items":[{"product_id":999,"quantity":1}]})
    data = rv.get_json()
    assert data["errors"]
    assert data["lines"] == []

def test_whatsapp_checkout_creates_pending_order(client):
    rv = client.post("/checkout", data={"cart_json":"[{\"product_id\":1,\"quantity\":1}]","customer_name":"A","fulfilment_method":"Collection","instructions":"Gate"})
    assert rv.status_code == 200
    assert b"Open WhatsApp" in rv.data
    assert b"awaiting seller confirmation" in rv.data.lower()
    assert b"A submitted M-Pesa reference or screenshot is not confirmation of payment." in rv.data
    assert b"payment-policy" in rv.data

def test_admin_requires_login(client):
    rv = client.get("/admin/", follow_redirects=False)
    assert rv.status_code in (302, 401)

def test_admin_login(client):
    rv = client.post("/admin/login", data={"username":"owner","password":"secret123"}, follow_redirects=True)
    assert b"Admin dashboard" in rv.data

def test_review_moderation(client, app):
    client.post("/reviews", data={"display_name":"Sam","rating":"5","text":"Good"})
    with app.app_context():
        review = Review.query.first()
        assert review.status == "pending"
def test_mamba_suggestion_saves_to_database(client, app):
    rv = client.post("/mamba-suggestion", data={
        "product_name": "Canvas sneakers",
        "category": "Men's shoes and sneakers",
        "preferences": "black size 42",
        "price_range": "KSh 800-1200",
        "details": "Low-cut style",
    }, follow_redirects=True)
    assert rv.status_code == 200
    assert b"suggestion has been saved" in rv.data
    with app.app_context():
        suggestion = ProductSuggestion.query.first()
        assert suggestion.product_name == "Canvas sneakers"
        assert suggestion.category == "Men's shoes and sneakers"
        assert suggestion.status == "pending"


def test_mamba_suggestion_validation_does_not_save(client, app):
    rv = client.post("/mamba-suggestion", data={"product_name": "", "category": "Bad"}, follow_redirects=True)
    assert rv.status_code == 200
    assert b"Enter the product or item name" in rv.data
    with app.app_context():
        assert ProductSuggestion.query.count() == 0


def test_mamba_suggestion_whatsapp_does_not_save(client, app):
    rv = client.post("/mamba-suggestion/whatsapp", data={
        "product_name": "Gym vest",
        "category": "Vests",
        "preferences": "white medium",
        "price_range": "KSh 250",
    }, follow_redirects=False)
    assert rv.status_code == 302
    assert "wa.me/254700000000" in rv.headers["Location"]
    assert "Gym%20vest" in rv.headers["Location"]
    with app.app_context():
        assert ProductSuggestion.query.count() == 0


def test_admin_can_mark_suggestion_reviewed(client, app):
    with app.app_context():
        db.session.add(ProductSuggestion(product_name="Slides", category="Slippers"))
        db.session.commit()
        suggestion_id = ProductSuggestion.query.first().id
    client.post("/admin/login", data={"username":"owner","password":"secret123"})
    rv = client.post(f"/admin/suggestions/{suggestion_id}", data={"status":"reviewed"}, follow_redirects=True)
    assert rv.status_code == 200
    assert b"Suggestion updated" in rv.data
    with app.app_context():
        assert ProductSuggestion.query.get(suggestion_id).status == "reviewed"


def test_order_tracking_requires_private_token(client, app):
    rv = client.post("/checkout", data={"cart_json":"[{\"product_id\":1,\"quantity\":1}]","customer_name":"A","customer_phone":"0700","fulfilment_method":"Collection"})
    assert rv.status_code == 200
    with app.app_context():
        from app.models import Order
        order = Order.query.first()
        reference = order.reference
        token = order.tracking_token
    bad = client.post("/track", data={"reference": reference, "tracking_token": "wrong"}, follow_redirects=True)
    assert b"could not find" in bad.data
    good = client.get("/track", query_string={"reference": reference, "tracking_token": token})
    assert good.status_code == 200
    assert reference.encode() in good.data
    assert b"Order progress" in good.data
    old_link = client.get(f"/track/{reference}/{token}", follow_redirects=False)
    assert old_link.status_code == 302
    private_post = client.post(f"/track/{reference}/{token}", data={"reference": reference, "tracking_token": token})
    assert private_post.status_code == 200
    assert b"Method Not Allowed" not in private_post.data


def test_product_enquiry_and_restock_alert_save(client, app):
    rv = client.post("/product/plain-socks/enquiry", data={"message":"Do you have black?","contact":"0700","preferred_contact":"WhatsApp","consent_to_contact":"on"}, follow_redirects=True)
    assert rv.status_code == 200
    assert b"product enquiry has been saved" in rv.data
    with app.app_context():
        from app.models import Product, ProductEnquiry
        product = Product.query.filter_by(slug="plain-socks").first()
        product.stock_quantity = 0
        db.session.commit()
    rv = client.post("/product/plain-socks/restock-alert", data={"contact":"0700","preferred_contact":"WhatsApp","consent_to_contact":"on"}, follow_redirects=True)
    assert b"Restock alert saved" in rv.data
    with app.app_context():
        from app.models import ProductEnquiry, RestockAlert
        assert ProductEnquiry.query.count() == 1
        assert RestockAlert.query.count() == 1


def test_admin_finance_records_expense(client, app):
    client.post("/admin/login", data={"username":"owner","password":"secret123"})
    rv = client.post("/admin/finance", data={"amount":"25","incurred_on":"2026-10-13","category":"Packaging","description":"Bags"}, follow_redirects=True)
    assert rv.status_code == 200
    assert b"Expense recorded" in rv.data
    with app.app_context():
        from app.models import Expense
        expense = Expense.query.first()
        assert expense.amount_cents == 2500
        assert expense.category == "Packaging"


def test_variant_cart_uses_variant_price_and_stock(client, app):
    with app.app_context():
        from app.models import ProductVariant
        product = Product.query.filter_by(slug="plain-socks").first()
        variant = ProductVariant(product=product, size="M", color="Black", sku="SOCK-M-BLK", price_cents=17500, stock_quantity=2, active=True)
        db.session.add(variant)
        db.session.commit()
        variant_id = variant.id
    rv = client.post("/api/cart/validate", json={"items":[{"product_id":1,"variant_id":variant_id,"quantity":5}]})
    data = rv.get_json()
    assert data["lines"][0]["variant_id"] == variant_id
    assert data["lines"][0]["variant_label"] == "M / Black"
    assert data["lines"][0]["quantity"] == 2
    assert data["lines"][0]["price_cents"] == 17500
    assert data["errors"]


def test_checkout_reserves_selected_variant_not_parent_stock(client, app):
    with app.app_context():
        from app.models import ProductVariant
        product = Product.query.filter_by(slug="plain-socks").first()
        variant = ProductVariant(product=product, size="L", color="White", sku="SOCK-L-WHT", price_cents=16500, stock_quantity=3, active=True)
        db.session.add(variant)
        db.session.commit()
        variant_id = variant.id
    rv = client.post("/checkout", data={"cart_json":f"[{{\"product_id\":1,\"variant_id\":{variant_id},\"quantity\":2}}]","customer_name":"A","customer_phone":"0700","fulfilment_method":"Collection"})
    assert rv.status_code == 200
    assert b"L / White" in rv.data
    with app.app_context():
        from app.models import Order, ProductVariant
        order = Order.query.first()
        variant = ProductVariant.query.get(variant_id)
        product = Product.query.filter_by(slug="plain-socks").first()
        assert order.items[0].variant_id == variant_id
        assert order.items[0].variant_label == "L / White"
        assert order.items[0].price_cents == 16500
        assert variant.reserved_quantity == 2
        assert product.reserved_quantity == 0


def test_payment_verification_awards_loyalty_once(client, app):
    with app.app_context():
        settings = ShopSettings.query.first()
        settings.loyalty_enabled = True
        settings.loyalty_points_per_100 = 10
        db.session.commit()
    client.post("/checkout", data={"cart_json":"[{\"product_id\":1,\"quantity\":1}]","customer_name":"A","customer_phone":"0700","fulfilment_method":"Collection"})
    with app.app_context():
        from app.models import Order
        order_id = Order.query.first().id
    client.post("/admin/login", data={"username":"owner","password":"secret123"})
    for _ in range(2):
        rv = client.post(f"/admin/reconciliation/orders/{order_id}/verify", data={"receipt":"MPESA123","amount":"150","reason":"Verified from M-Pesa statement"}, follow_redirects=True)
        assert rv.status_code == 200
    with app.app_context():
        from app.models import LoyaltyTransaction, PaymentTransaction
        assert PaymentTransaction.query.count() == 1
        assert LoyaltyTransaction.query.count() == 1
        assert LoyaltyTransaction.query.first().points == 10

def test_admin_product_edit_save_updates_product(client, app):
    with app.app_context():
        from app.models import ProductImage
        product = Product.query.filter_by(slug="plain-socks").first()
        product.images.append(ProductImage(filename="sample-ankle-socks.svg", alt_text="Socks", is_primary=True))
        db.session.commit()
        product_id = product.id
        category_id = product.category_id
    client.post("/admin/login", data={"username":"owner","password":"secret123"})
    rv = client.post(f"/admin/products/{product_id}/edit", data={
        "name": "Updated Socks",
        "sku": "SOCK-UPDATED",
        "category_id": str(category_id),
        "description": "Updated description",
        "price": "155",
        "unit_cost": "80",
        "previous_price": "180",
        "stock_quantity": "7",
        "low_stock_threshold": "2",
        "size_options": "M, L",
        "color_options": "Black",
        "variants_text": "",
        "active": "on",
        "featured": "on",
    }, follow_redirects=True)
    assert rv.status_code == 200
    assert b"Product saved" in rv.data
    with app.app_context():
        product = Product.query.get(product_id)
        assert product.name == "Updated Socks"
        assert product.sku == "SOCK-UPDATED"
        assert product.price_cents == 15500
        assert product.stock_quantity == 7


def test_variant_product_shows_price_range_and_options(client, app):
    with app.app_context():
        from app.models import ProductVariant
        product = Product.query.filter_by(slug="plain-socks").first()
        db.session.add_all([
            ProductVariant(product=product, size="Small", color="Black", style="Basic", sku="SOCK-S", price_cents=1000, stock_quantity=5, active=True),
            ProductVariant(product=product, size="Large", color="Black", style="Premium", sku="SOCK-L", price_cents=6000, stock_quantity=3, active=True),
        ])
        db.session.commit()
    shop = client.get("/shop")
    assert b"KSh 10 - KSh 60" in shop.data
    assert b"2 options available" in shop.data
    detail = client.get("/product/plain-socks")
    assert b"Small / Black / Basic" in detail.data
    assert b"Large / Black / Premium" in detail.data
    assert b"SOCK-S" in detail.data

def test_cloudinary_missing_credentials_blocks_admin_upload(client, app):
    client.post("/admin/login", data={"username":"owner","password":"secret123"})
    with app.app_context():
        category_id = Category.query.first().id
    rv = client.post("/admin/products/new", data={
        "name": "Photo Socks",
        "category_id": str(category_id),
        "description": "Needs cloud upload",
        "price": "100",
        "stock_quantity": "1",
        "low_stock_threshold": "1",
        "variants_text": "",
        "images": (io.BytesIO(b"\xff\xd8\xffimage"), "sock.jpg"),
    }, content_type="multipart/form-data", follow_redirects=True)
    assert b"Cloudinary is not configured" in rv.data
    with app.app_context():
        assert Product.query.filter_by(slug="photo-socks").first() is None


def test_admin_image_upload_to_cloudinary_success(client, app, monkeypatch):
    app.config.update(CLOUDINARY_CLOUD_NAME="demo", CLOUDINARY_API_KEY="key", CLOUDINARY_API_SECRET="secret")
    uploaded = []
    def fake_upload(data, public_id, resource_type="image"):
        uploaded.append(public_id)
        return {"public_id": public_id}
    monkeypatch.setattr("app.admin.routes.upload_image_bytes", fake_upload)
    client.post("/admin/login", data={"username":"owner","password":"secret123"})
    with app.app_context():
        category_id = Category.query.first().id
    rv = client.post("/admin/products/new", data={
        "name": "Cloud Socks",
        "category_id": str(category_id),
        "description": "Cloud image",
        "price": "100",
        "stock_quantity": "3",
        "low_stock_threshold": "1",
        "variants_text": "",
        "active": "on",
        "images": (io.BytesIO(b"\xff\xd8\xffimage"), "sock.jpg"),
    }, content_type="multipart/form-data", follow_redirects=True)
    assert b"Product saved" in rv.data
    assert uploaded
    with app.app_context():
        product = Product.query.filter_by(slug="cloud-socks").first()
        assert product.images[0].cloudinary_public_id.startswith("fity/products/product-")
        assert product.images[0].storage_backend == "cloudinary"
        assert product.images[0].is_primary is True


def test_admin_image_upload_cloudinary_failure_does_not_save(client, app, monkeypatch):
    from app.services.product_images import ImageStorageError
    app.config.update(CLOUDINARY_CLOUD_NAME="demo", CLOUDINARY_API_KEY="key", CLOUDINARY_API_SECRET="secret")
    def fake_upload(data, public_id, resource_type="image"):
        raise ImageStorageError("Cloudinary upload failed. Please try again.")
    monkeypatch.setattr("app.admin.routes.upload_image_bytes", fake_upload)
    client.post("/admin/login", data={"username":"owner","password":"secret123"})
    with app.app_context():
        category_id = Category.query.first().id
    rv = client.post("/admin/products/new", data={
        "name": "Broken Cloud Socks",
        "category_id": str(category_id),
        "description": "Cloud failure",
        "price": "100",
        "stock_quantity": "3",
        "low_stock_threshold": "1",
        "variants_text": "",
        "images": (io.BytesIO(b"\xff\xd8\xffimage"), "sock.jpg"),
    }, content_type="multipart/form-data", follow_redirects=True)
    assert b"Cloudinary upload failed" in rv.data
    with app.app_context():
        assert Product.query.filter_by(slug="broken-cloud-socks").first() is None


def test_image_validation_rejects_extension_bytes_and_size(app):
    from app.admin.routes import validate_image_bytes
    with app.app_context():
        assert validate_image_bytes("bad.txt", b"abc")[1]
        assert validate_image_bytes("bad.jpg", b"not-an-image")[1]
        too_large = b"\xff\xd8\xff" + (b"0" * app.config["MAX_CONTENT_LENGTH"])
        assert validate_image_bytes("big.jpg", too_large)[1] == "Image is too large."


def test_multiple_cloudinary_images_primary_selection(client, app, monkeypatch):
    app.config.update(CLOUDINARY_CLOUD_NAME="demo", CLOUDINARY_API_KEY="key", CLOUDINARY_API_SECRET="secret")
    monkeypatch.setattr("app.admin.routes.upload_image_bytes", lambda data, public_id, resource_type="image": {"public_id": public_id})
    client.post("/admin/login", data={"username":"owner","password":"secret123"})
    with app.app_context():
        category_id = Category.query.first().id
    rv = client.post("/admin/products/new", data={
        "name": "Gallery Socks",
        "category_id": str(category_id),
        "description": "Two images",
        "price": "100",
        "stock_quantity": "3",
        "low_stock_threshold": "1",
        "variants_text": "",
        "images": [(io.BytesIO(b"\xff\xd8\xffone"), "one.jpg"), (io.BytesIO(b"\xff\xd8\xfftwo"), "two.jpg")],
    }, content_type="multipart/form-data", follow_redirects=True)
    assert b"Product saved" in rv.data
    with app.app_context():
        product = Product.query.filter_by(slug="gallery-socks").first()
        assert len(product.images) == 2
        assert [image.is_primary for image in product.images] == [True, False]


def test_cloudinary_url_and_legacy_fallback_helpers(app):
    from app.services.product_images import product_image_url, valid_product_image
    app.config.update(CLOUDINARY_CLOUD_NAME="demo")
    with app.test_request_context():
        cloud = ProductImage(filename="sock.jpg", cloudinary_public_id="fity/products/sock", alt_text="Sock")
        assert product_image_url(cloud) == "https://res.cloudinary.com/demo/image/upload/fity/products/sock"
        missing = ProductImage(filename="missing.jpg", alt_text="Missing")
        assert valid_product_image(missing) is False
        assert product_image_url(missing).endswith("/static/images/mainLogo.png")


def test_cloudinary_image_delete_failure_keeps_record(client, app, monkeypatch):
    from app.services.product_images import ImageStorageError
    app.config.update(CLOUDINARY_CLOUD_NAME="demo", CLOUDINARY_API_KEY="key", CLOUDINARY_API_SECRET="secret")
    with app.app_context():
        product = Product.query.filter_by(slug="plain-socks").first()
        image = ProductImage(filename="sock.jpg", cloudinary_public_id="fity/products/sock", storage_backend="cloudinary", alt_text="Sock", is_primary=True)
        product.images.append(image)
        db.session.commit()
        product_id = product.id
        image_id = image.id
    monkeypatch.setattr("app.admin.routes.delete_cloudinary_asset", lambda public_id: (_ for _ in ()).throw(ImageStorageError("Cloudinary deletion was not confirmed.")))
    client.post("/admin/login", data={"username":"owner","password":"secret123"})
    rv = client.post(f"/admin/products/{product_id}/images/{image_id}/delete", follow_redirects=True)
    assert b"Cloudinary deletion was not confirmed" in rv.data
    with app.app_context():
        assert ProductImage.query.get(image_id) is not None


def test_legacy_image_migration_cli(client, app, monkeypatch):
    app.config.update(CLOUDINARY_CLOUD_NAME="demo", CLOUDINARY_API_KEY="key", CLOUDINARY_API_SECRET="secret")
    upload_dir = app.config["UPLOAD_FOLDER"]
    from pathlib import Path
    Path(upload_dir).mkdir(parents=True, exist_ok=True)
    Path(upload_dir, "legacy.jpg").write_bytes(b"\xff\xd8\xfflegacy")
    uploaded = []
    monkeypatch.setattr("app.upload_image_bytes", lambda data, public_id, resource_type="image": uploaded.append(public_id) or {"public_id": public_id})
    with app.app_context():
        product = Product.query.filter_by(slug="plain-socks").first()
        image = ProductImage(filename="legacy.jpg", alt_text="Legacy", is_primary=True)
        product.images.append(image)
        db.session.commit()
        image_id = image.id
    result = app.test_cli_runner().invoke(args=["migrate-product-images"])
    assert result.exit_code == 0
    assert "migrated=1" in result.output
    with app.app_context():
        image = ProductImage.query.get(image_id)
        assert image.cloudinary_public_id == f"fity/legacy/product-image-{image_id}"
        assert image.storage_backend == "cloudinary"


def test_postgresql_startup_does_not_run_sqlite_schema_mutations(monkeypatch):
    def fail_create_all(*args, **kwargs):
        raise AssertionError("db.create_all must not run during PostgreSQL startup")
    def fail_ensure_columns(*args, **kwargs):
        raise AssertionError("ensure_columns must not run during PostgreSQL startup")
    monkeypatch.setattr(app_module.db, "create_all", fail_create_all)
    monkeypatch.setattr(app_module, "ensure_columns", fail_ensure_columns)
    monkeypatch.setattr(app_module, "is_sqlite_database", lambda: False)
    monkeypatch.setattr(app_module, "database_dialect_name", lambda: "postgresql")

    created = create_app(TestConfig)

    assert created.testing is True


def test_cloudinary_schema_migration_postgresql_is_idempotent(monkeypatch):
    statements = []
    commits = []
    monkeypatch.setattr(app_module, "is_postgresql_database", lambda: True)
    monkeypatch.setattr(app_module, "is_sqlite_database", lambda: False)
    monkeypatch.setattr(app_module.db.session, "execute", lambda statement: statements.append(str(statement)))
    monkeypatch.setattr(app_module.db.session, "commit", lambda: commits.append(True))

    app_module.ensure_cloudinary_image_columns()
    app_module.ensure_cloudinary_image_columns()

    assert statements == [
        "ALTER TABLE product_image ADD COLUMN IF NOT EXISTS storage_backend VARCHAR(20) NOT NULL DEFAULT 'local'",
        "ALTER TABLE product_image ADD COLUMN IF NOT EXISTS cloudinary_public_id VARCHAR(255)",
        "ALTER TABLE product_image ADD COLUMN IF NOT EXISTS storage_backend VARCHAR(20) NOT NULL DEFAULT 'local'",
        "ALTER TABLE product_image ADD COLUMN IF NOT EXISTS cloudinary_public_id VARCHAR(255)",
    ]
    assert len(commits) == 2


def test_cloudinary_schema_migration_sqlite_preserves_existing_images(app):
    with app.app_context():
        product = Product.query.filter_by(slug="plain-socks").first()
        image = ProductImage(filename="legacy.jpg", alt_text="Legacy", is_primary=True)
        product.images.append(image)
        db.session.commit()
        image_id = image.id

        app_module.ensure_cloudinary_image_columns()

        preserved = ProductImage.query.get(image_id)
        assert preserved is not None
        assert preserved.filename == "legacy.jpg"
        assert preserved.storage_backend == "local"
        assert preserved.cloudinary_public_id is None

def test_product_display_prefers_cloudinary_over_primary_sample(app):
    from app.services.product_images import product_display_image, product_image_url
    app.config.update(CLOUDINARY_CLOUD_NAME="demo")
    with app.test_request_context():
        product = Product(name="Display Socks", slug="display-socks", description="Display", price_cents=1000, stock_quantity=1)
        sample = ProductImage(filename="sample-ankle-socks.svg", alt_text="Sample", is_primary=True, sort_order=0)
        cloud = ProductImage(filename="real-socks.jpg", cloudinary_public_id="fity/products/real-socks", storage_backend="cloudinary", alt_text="Real", is_primary=False, sort_order=1)
        product.images = [sample, cloud]

        image = product_display_image(product)

        assert image is cloud
        assert product_image_url(image) == "https://res.cloudinary.com/demo/image/upload/fity/products/real-socks"


def test_cloudinary_id_backfill_dry_run_and_apply_preserves_product_data(app, tmp_path):
    with app.app_context():
        product = Product.query.filter_by(slug="plain-socks").first()
        product.sku = "SOCK-PLAIN"
        product.stock_quantity = 4
        product.price_cents = 15000
        product.images.append(ProductImage(filename="sample-ankle-socks.svg", alt_text="Sample", is_primary=True))
        db.session.commit()
        product_id = product.id
        image_id = product.images[-1].id
    mapping = tmp_path / "cloudinary-map.csv"
    mapping.write_text(
        "product_slug,product_sku,filename,cloudinary_public_id\n"
        "plain-socks,SOCK-PLAIN,real-socks.jpg,fity/legacy/product-image-local-1\n",
        encoding="utf-8",
    )
    runner = app.test_cli_runner()

    dry = runner.invoke(args=["backfill-cloudinary-image-ids", "--mapping", str(mapping), "--ignore-filename"])

    assert dry.exit_code == 0
    assert "Done (dry-run). migrated=1 skipped=0 missing=0 failed=0" in dry.output
    with app.app_context():
        image = ProductImage.query.get(image_id)
        product = Product.query.get(product_id)
        assert image.cloudinary_public_id is None
        assert image.storage_backend == "local"
        assert product.stock_quantity == 4
        assert product.price_cents == 15000

    applied = runner.invoke(args=["backfill-cloudinary-image-ids", "--mapping", str(mapping), "--ignore-filename", "--apply"])

    assert applied.exit_code == 0
    assert "Done (applied). migrated=1 skipped=0 missing=0 failed=0" in applied.output
    with app.app_context():
        image = ProductImage.query.get(image_id)
        product = Product.query.get(product_id)
        assert image.cloudinary_public_id == "fity/legacy/product-image-local-1"
        assert image.storage_backend == "cloudinary"
        assert image.filename == "sample-ankle-socks.svg"
        assert product.stock_quantity == 4
        assert product.price_cents == 15000

    rerun = runner.invoke(args=["backfill-cloudinary-image-ids", "--mapping", str(mapping), "--ignore-filename", "--apply"])

    assert rerun.exit_code == 0
    assert "Done (applied). migrated=0 skipped=1 missing=0 failed=0" in rerun.output


def test_cloudinary_id_backfill_requires_unambiguous_match(app, tmp_path):
    with app.app_context():
        category = Category.query.first()
        product = Product(name="Ambiguous", slug="ambiguous", sku="AMB", category=category, description="Ambiguous", price_cents=1000, stock_quantity=1)
        product.images.append(ProductImage(filename="sample-one.svg", alt_text="One", is_primary=True))
        product.images.append(ProductImage(filename="sample-two.svg", alt_text="Two", is_primary=False))
        db.session.add(product)
        db.session.commit()
    mapping = tmp_path / "ambiguous-map.csv"
    mapping.write_text(
        "product_slug,cloudinary_public_id\n"
        "ambiguous,fity/legacy/ambiguous\n",
        encoding="utf-8",
    )

    result = app.test_cli_runner().invoke(args=["backfill-cloudinary-image-ids", "--mapping", str(mapping), "--ignore-filename", "--apply"])

    assert result.exit_code == 0
    assert "matched multiple image records" in result.output
    assert "Done (applied). migrated=0 skipped=0 missing=0 failed=1" in result.output
    with app.app_context():
        product = Product.query.filter_by(slug="ambiguous").first()
        assert [image.cloudinary_public_id for image in product.images] == [None, None]


def test_admin_cloudinary_mapping_page_dry_run_and_apply(client, app):
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    with app.app_context():
        product = Product.query.filter_by(slug="plain-socks").first()
        product.sku = "SOCK-PLAIN"
        product.stock_quantity = 4
        product.price_cents = 15000
        product.images.append(ProductImage(filename="missing-local.jpg", alt_text="Missing local", is_primary=True))
        db.session.commit()
        image_id = product.images[-1].id
        product_id = product.id

    mapping = b"product_slug,product_sku,filename,cloudinary_public_id\nplain-socks,SOCK-PLAIN,real-cloud.jpg,fity/products/plain-socks\n"
    dry = client.post(
        "/admin/cloudinary-images",
        data={"action": "mapping", "ignore_filename": "1", "mapping": (io.BytesIO(mapping), "map.csv")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert dry.status_code == 200
    assert b"Cloudinary mapping dry run: migrated=1 skipped=0 missing=0 failed=0" in dry.data
    with app.app_context():
        image = ProductImage.query.get(image_id)
        assert image.cloudinary_public_id is None

    applied = client.post(
        "/admin/cloudinary-images",
        data={"action": "mapping", "ignore_filename": "1", "apply": "1", "mapping": (io.BytesIO(mapping), "map.csv")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert applied.status_code == 200
    assert b"Cloudinary mapping applied: migrated=1 skipped=0 missing=0 failed=0" in applied.data
    with app.app_context():
        image = ProductImage.query.get(image_id)
        product = Product.query.get(product_id)
        assert image.cloudinary_public_id == "fity/products/plain-socks"
        assert image.storage_backend == "cloudinary"
        assert product.price_cents == 15000
        assert product.stock_quantity == 4

def test_admin_cloudinary_page_accepts_pasted_cloudinary_url(client, app):
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    with app.app_context():
        product = Product.query.filter_by(slug="plain-socks").first()
        product.images.append(ProductImage(filename="missing-local.jpg", alt_text="Missing local", is_primary=True))
        db.session.commit()
        image_id = product.images[-1].id

    response = client.post(
        "/admin/cloudinary-images",
        data={
            "action": "single",
            "image_id": str(image_id),
            "cloudinary_value": "https://res.cloudinary.com/demo/image/upload/v1720000000/fity/products/product-1/abc123-socks.jpg",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Cloudinary image saved" in response.data
    with app.app_context():
        image = ProductImage.query.get(image_id)
        assert image.cloudinary_public_id == "fity/products/product-1/abc123-socks"
        assert image.storage_backend == "cloudinary"
def test_public_policy_pages_and_sitemap_xml(client):
    import xml.etree.ElementTree as ET

    pages = {
        "/delivery-policy": "Delivery and collection",
        "/payment-policy": "Payment information",
        "/returns-policy": "Returns, exchanges, and cancellations",
        "/privacy": "Privacy information",
        "/terms": "Terms and conditions",
    }
    for path, heading in pages.items():
        response = client.get(path)
        assert response.status_code == 200
        assert heading.encode() in response.data
        assert b"<title>" in response.data
        assert b'rel="canonical"' in response.data
    sitemap = client.get("/sitemap.xml")
    assert sitemap.status_code == 200
    root = ET.fromstring(sitemap.data)
    locations = [node.text for node in root.findall("{http://www.sitemaps.org/schemas/sitemap/0.9}url/{http://www.sitemaps.org/schemas/sitemap/0.9}loc")]
    assert "http://example.test/delivery-policy" in locations
    assert "http://example.test/privacy" in locations


def test_seo_metadata_uses_clean_production_canonical(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"<title>" in response.data
    assert b'name="description"' in response.data
    assert b'href="https://fitystore.top/"' in response.data
    assert b'property="og:url" content="https://fitystore.top/"' in response.data

    private_response = client.get('/checkout')
    assert b'name="robots" content="noindex,follow"' in private_response.data
    assert b'rel="canonical"' not in private_response.data
    assert b'property="og:url"' not in private_response.data

def test_product_share_card_is_branded_png_with_price_metadata(client):
    page = client.get("/product/plain-socks")
    assert page.status_code == 200
    assert b'property="og:image" content="https://fitystore.top/share/product/plain-socks.png"' in page.data
    assert b'property="og:image:width" content="1200"' in page.data
    assert b'property="og:image:type" content="image/png"' in page.data
    card = client.get("/share/product/plain-socks.png")
    assert card.status_code == 200
    assert card.mimetype == "image/png"
    from PIL import Image
    preview = Image.open(io.BytesIO(card.data))
    assert preview.size == (1200, 630)
    assert preview.format == "PNG"

def test_product_sharing_uses_canonical_links_and_current_prices(client, app):
    priced = client.get("/product/plain-socks")
    assert priced.status_code == 200
    assert b'data-share-url="https://fitystore.top/product/plain-socks"' in priced.data
    assert b"Price: KSh 150" in priced.data
    assert b"data-share-native" in priced.data
    assert b"data-share-copy" in priced.data
    assert b"WhatsApp a friend" in priced.data
    assert b"Ask on WhatsApp" in priced.data
    assert b"data-product-id=" in priced.data

    unpriced = client.get("/product/unpriced-cap")
    assert unpriced.status_code == 200
    assert b"Contact the store for the current price" in unpriced.data
    assert b"Price: KSh 0" not in unpriced.data
    assert b'data-share-url="https://fitystore.top/product/unpriced-cap"' in unpriced.data
    listing = client.get("/shop")
    assert listing.status_code == 200
    assert b'data-share-url="https://fitystore.top/product/plain-socks"' in listing.data
    assert b'data-share-native' in listing.data


def test_product_whatsapp_share_encodes_special_characters(client, app):
    with app.app_context():
        product = Product.query.filter_by(slug="plain-socks").first()
        product.name = "Plain & Socks"
        db.session.commit()

    response = client.get("/product/plain-socks")
    assert response.status_code == 200
    assert b"Plain &amp; Socks" in response.data
    assert b"Plain%20%26%20Socks" in response.data
    assert b"view%20it%20here" in response.data.lower()


def test_unpublished_product_has_no_shareable_public_page(client, app):
    with app.app_context():
        product = Product.query.filter_by(slug="plain-socks").first()
        product.active = False
        db.session.commit()

    assert client.get("/product/plain-socks").status_code == 404
def test_checkout_and_policy_pages_show_current_store_terms(client, app):
    with app.app_context():
        settings = ShopSettings.query.first()
        settings.collection_instructions = "Meet at the confirmed Othaya collection point."
        settings.delivery_area = "Nearby delivery is arranged by contacting the seller."
        settings.delivery_fee_cents = 5000
        settings.payment_instructions = "Confirm availability, then use the configured manual M-Pesa payment number."
        settings.payment_number = "254700000000"
        db.session.commit()

    checkout = client.get("/checkout")
    assert checkout.status_code == 200
    assert b"Meet at the confirmed Othaya collection point." in checkout.data
    assert b"Nearby delivery is arranged by contacting the seller." in checkout.data
    assert b"KSh 50" in checkout.data
    assert b"Confirm availability, then use the configured manual M-Pesa payment number." in checkout.data
    assert b"Never share your M-Pesa PIN." in checkout.data
    assert b"/returns-policy" in checkout.data

    delivery = client.get("/delivery-policy")
    assert b"KSh 50" in delivery.data
    returns = client.get("/returns-policy")
    assert b"within 48 hours of delivery" in returns.data
    assert b"does not automatically approve a refund or exchange" in returns.data

@pytest.fixture()
def app_context(app):
    with app.app_context():
        yield

def _register_customer(client, email="customer@example.com", marketing=False):
    data = {
        "display_name": "Test Customer",
        "email": email,
        "password": "a-strong-test-password-123",
        "confirm_password": "a-strong-test-password-123",
    }
    if marketing:
        data["marketing_opt_in"] = "yes"
    return client.post("/account/register", data=data, follow_redirects=True)


def test_customer_registration_hashes_password_and_defaults_marketing_off(client, app):
    app.config["MAIL_PROVIDER"] = ""
    response = _register_customer(client)
    assert response.status_code == 200
    assert b"Your FityStores account" in response.data
    with app.app_context():
        account = CustomerAccount.query.filter_by(email="customer@example.com").one()
        assert account.password_hash != "a-strong-test-password-123"
        assert check_password_hash(account.password_hash, "a-strong-test-password-123")
        assert account.email_verified_at is None
        assert account.marketing_status == "none"
        token = CustomerToken.query.filter_by(account_id=account.id, purpose="verify_email").one()
        assert len(token.token_hash) == 64
        assert EmailMessage.query.filter_by(kind="verify_email").one().status == "not_configured"
        assert CustomerConsentEvent.query.filter_by(account_id=account.id).count() == 0


def test_email_verification_is_expiring_and_single_use(client, app, app_context):
    from datetime import datetime, timedelta
    import hashlib
    app.config["MAIL_PROVIDER"] = ""
    account = CustomerAccount(email="verify@example.com", display_name="Verify", password_hash=generate_password_hash("a-strong-test-password-123"))
    db.session.add(account)
    db.session.flush()
    token = "one-time-verification-token"
    token_row = CustomerToken(account_id=account.id, purpose="verify_email", token_hash=hashlib.sha256(token.encode()).hexdigest(), expires_at=datetime.utcnow() + timedelta(minutes=30))
    db.session.add(token_row)
    expired = CustomerToken(account_id=account.id, purpose="verify_email", token_hash=hashlib.sha256(b"expired-token").hexdigest(), expires_at=datetime.utcnow() - timedelta(seconds=1))
    db.session.add(expired)
    db.session.commit()
    assert client.post("/account/verify/expired-token").status_code == 302
    assert client.post(f"/account/verify/{token}").status_code == 302
    db.session.refresh(account)
    assert account.email_verified_at is not None
    assert token_row.used_at is not None
    assert client.post(f"/account/verify/{token}").status_code == 302
    assert CustomerToken.query.filter_by(token_hash=hashlib.sha256(b"expired-token").hexdigest()).one().used_at is None


def test_password_reset_is_single_use_and_requires_verified_account(client, app, app_context):
    from datetime import datetime, timedelta
    import hashlib
    account = CustomerAccount(email="reset@example.com", display_name="Reset", password_hash=generate_password_hash("old-strong-password-123"), email_verified_at=datetime.utcnow())
    db.session.add(account)
    db.session.flush()
    token = "one-time-reset-token"
    row = CustomerToken(account_id=account.id, purpose="reset_password", token_hash=hashlib.sha256(token.encode()).hexdigest(), expires_at=datetime.utcnow() + timedelta(minutes=30))
    db.session.add(row)
    db.session.commit()
    data = {"password": "new-strong-password-456", "confirm_password": "new-strong-password-456"}
    assert client.post(f"/account/reset/{token}", data=data).status_code == 302
    assert check_password_hash(account.password_hash, "new-strong-password-456")
    assert row.used_at is not None
    assert client.post(f"/account/reset/{token}", data=data).status_code == 302
    assert CustomerToken.query.filter_by(purpose="reset_password").count() == 1


def test_marketing_double_opt_in_and_unsubscribe_history(client, app, app_context):
    from datetime import datetime, timedelta
    import hashlib
    app.config["MAIL_PROVIDER"] = ""
    response = _register_customer(client, email="offers@example.com", marketing=True)
    assert response.status_code == 200
    account = CustomerAccount.query.filter_by(email="offers@example.com").one()
    assert account.marketing_status == "pending"
    assert CustomerConsentEvent.query.filter_by(account_id=account.id, event="requested").count() == 1
    account.email_verified_at = datetime.utcnow()
    marketing = CustomerToken.query.filter_by(account_id=account.id, purpose="marketing_confirm").first()
    token = "marketing-confirm-token"
    marketing.token_hash = hashlib.sha256(token.encode()).hexdigest()
    marketing.expires_at = datetime.utcnow() + timedelta(hours=1)
    db.session.commit()
    assert client.post(f"/account/marketing/confirm/{token}").status_code == 302
    assert account.marketing_status == "subscribed"
    assert CustomerConsentEvent.query.filter_by(account_id=account.id, event="confirmed").count() == 1
    unsubscribe_token = "marketing-unsubscribe-token"
    db.session.add(CustomerToken(account_id=account.id, purpose="unsubscribe", token_hash=hashlib.sha256(unsubscribe_token.encode()).hexdigest(), expires_at=datetime.utcnow() + timedelta(days=365)))
    db.session.commit()
    assert client.get(f"/account/unsubscribe/{unsubscribe_token}").status_code == 200
    assert client.post(f"/account/unsubscribe/{unsubscribe_token}").status_code == 200
    db.session.refresh(account)
    assert account.marketing_status == "unsubscribed"
    assert CustomerConsentEvent.query.filter_by(account_id=account.id, event="withdrawn").count() == 1


def test_customer_orders_are_account_scoped_and_admin_is_not_customer(client, app, app_context):
    from datetime import datetime
    first = CustomerAccount(email="first@example.com", display_name="First", password_hash=generate_password_hash("a-strong-test-password-123"), email_verified_at=datetime.utcnow())
    second = CustomerAccount(email="second@example.com", display_name="Second", password_hash=generate_password_hash("a-strong-test-password-123"), email_verified_at=datetime.utcnow())
    db.session.add_all([first, second])
    db.session.flush()
    order1 = Order(reference="FITY-ACCOUNT-1", fulfilment_method="Collection", customer_name="First")
    order2 = Order(reference="FITY-ACCOUNT-2", fulfilment_method="Collection", customer_name="Second")
    db.session.add_all([order1, order2])
    db.session.flush()
    db.session.add_all([CustomerOrderLink(account_id=first.id, order_id=order1.id), CustomerOrderLink(account_id=second.id, order_id=order2.id)])
    db.session.commit()
    response = client.post("/account/login", data={"email": first.email, "password": "a-strong-test-password-123"}, follow_redirects=True)
    assert response.status_code == 200
    assert b"FITY-ACCOUNT-1" in response.data
    assert b"FITY-ACCOUNT-2" not in response.data
    assert client.get("/admin/").status_code == 403


def test_google_signin_uses_mocked_verified_sub_and_refuses_unsafe_link(client, app, monkeypatch, app_context):
    import app.customer.routes as customer_routes
    app.config["GOOGLE_CLIENT_ID"] = "test-client-id"
    claims = {"sub": "google-sub-123", "email": "googleuser@gmail.com", "name": "Google User", "email_verified": True, "hd": ""}
    monkeypatch.setattr(customer_routes, "verify_google_credential", lambda credential: claims)
    client.set_cookie("g_csrf_token", "csrf-state", domain="example.test")
    response = client.post("/account/google", data={"credential": "mocked", "g_csrf_token": "csrf-state"}, follow_redirects=True)
    assert response.status_code == 200
    account = CustomerAccount.query.filter_by(google_sub="google-sub-123").one()
    assert account.email_verified_at is not None
    assert account.password_hash is None
    assert account.marketing_status == "none"

    existing = CustomerAccount(email="owner@example.com", display_name="Existing", password_hash=generate_password_hash("a-strong-test-password-123"), email_verified_at=datetime.utcnow())
    db.session.add(existing)
    db.session.commit()
    claims.update(sub="another-google-sub", email="owner@example.com")
    client.set_cookie("g_csrf_token", "csrf-state-2", domain="example.test")
    client.post("/account/google", data={"credential": "mocked", "g_csrf_token": "csrf-state-2"})
    assert existing.google_sub is None


def test_mail_provider_failure_is_recorded_without_secrets(app, monkeypatch, app_context):
    import smtplib
    import app.services.customer_mail as customer_mail
    app.config.update(MAIL_PROVIDER="smtp", MAIL_FROM_VERIFIED=True, MAIL_FROM_ADDRESS="verified@example.test", MAIL_SERVER="smtp.example.test")
    def fail_send(*args, **kwargs):
        raise smtplib.SMTPAuthenticationError(535, b"private credential rejected")
    monkeypatch.setattr(customer_mail, "_send_smtp", fail_send)
    result = customer_mail.queue_branded_email("person@example.test", "Test", "test", "smtp-failure-test", "Hello", ["Test body"])
    assert result.status == "failed"
    assert "private credential" not in result.failure_reason
    assert result.failure_reason == "SMTP error 535."


def test_campaign_only_targets_confirmed_active_subscribers_and_prevents_resend(client, app, monkeypatch, app_context):
    from datetime import datetime
    import app.services.customer_mail as customer_mail
    app.config.update(MAIL_PROVIDER="smtp", MAIL_FROM_VERIFIED=True, MAIL_FROM_ADDRESS="verified@example.test", MAIL_SERVER="smtp.example.test", MAIL_TEST_ADDRESS="owner@example.test")
    sent = []
    monkeypatch.setattr(customer_mail, "_send_smtp", lambda recipient, subject, html, text: sent.append((recipient, subject, html)))
    eligible = CustomerAccount(email="eligible@example.com", display_name="Eligible", email_verified_at=datetime.utcnow(), marketing_status="subscribed", marketing_consent_at=datetime.utcnow())
    declined = CustomerAccount(email="declined@example.com", display_name="Declined", email_verified_at=datetime.utcnow(), marketing_status="none")
    db.session.add_all([eligible, declined])
    db.session.commit()
    assert client.post("/admin/login", data={"username": "owner", "password": "secret123"}, follow_redirects=True).status_code == 200
    draft = client.post("/admin/email/campaigns", data={"name": "New arrivals", "subject": "New arrivals", "body": "See what is new."})
    campaign = EmailCampaign.query.one()
    assert draft.status_code == 302
    preview = client.get(f"/admin/email/campaigns/{campaign.id}")
    assert b"Current eligible subscriber count: 1" in preview.data
    response = client.post(f"/admin/email/campaigns/{campaign.id}", data={"confirm_send": "SEND"})
    assert response.status_code == 302
    assert [item[0] for item in sent] == ["eligible@example.com"]
    assert "unsubscribe" in sent[0][2]
    assert EmailCampaignRecipient.query.filter_by(campaign_id=campaign.id).count() == 1
    assert EmailCampaignRecipient.query.filter_by(campaign_id=campaign.id, status="sent").count() == 1
    client.post(f"/admin/email/campaigns/{campaign.id}", data={"confirm_send": "SEND"})
    assert len(sent) == 1


def _isolated_legacy_sqlite_app(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "ensure_database_schema", lambda _app: None)
    config = type(
        "MigrationSQLiteConfig",
        (TestConfig,),
        {"SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'legacy-store.sqlite3'}"},
    )
    isolated_app = create_app(config)
    feature_tables = {
        "customer_account", "customer_token", "customer_consent_event",
        "customer_order_link", "auth_rate_limit", "email_message",
        "email_campaign", "email_campaign_recipient",
    }
    with isolated_app.app_context():
        legacy_tables = [table for table in db.metadata.sorted_tables if table.name not in feature_tables]
        db.metadata.create_all(bind=db.engine, tables=legacy_tables, checkfirst=True)
        settings = ShopSettings(shop_name="Existing shop", tagline="Existing tagline", payment_number="123456")
        admin = AdminUser(username="existing-admin", password_hash=generate_password_hash("keep-this-hash"))
        category = Category(name="Existing category", slug="existing-category")
        db.session.add_all([settings, admin, category])
        db.session.flush()
        product = Product(
            name="Existing product", slug="existing-product", category=category,
            description="Preserve this", price_cents=32100, previous_price_cents=40000,
            stock_quantity=7, reserved_quantity=2, sku="KEEP-SKU",
        )
        order = Order(reference="KEEP-ORDER", fulfilment_method="Collection", total_cents=32100)
        db.session.add_all([product, order])
        db.session.flush()
        image = ProductImage(
            product=product, filename="existing-transparent.png", alt_text="Existing image",
            storage_backend="cloudinary", cloudinary_public_id="fity/products/existing-transparent",
            sort_order=4, is_primary=True,
        )
        item = OrderItem(order=order, product=product, product_name=product.name, quantity=1, price_cents=32100, subtotal_cents=32100)
        db.session.add_all([image, item])
        db.session.commit()
        snapshot = {
            "settings": (settings.shop_name, settings.tagline, settings.payment_number),
            "admin_hash": admin.password_hash,
            "product": (product.name, product.price_cents, product.previous_price_cents, product.stock_quantity, product.reserved_quantity, product.sku),
            "image": (image.filename, image.storage_backend, image.cloudinary_public_id, image.sort_order, image.is_primary, image.alt_text),
            "order": (order.reference, order.total_cents, len(order.items)),
        }
    return isolated_app, feature_tables, snapshot


def test_customer_email_migration_creates_schema_on_fresh_sqlite_database(tmp_path, monkeypatch):
    import sys
    monkeypatch.setenv("FLASK_RUN_FROM_CLI", "true")
    monkeypatch.setattr(sys, "argv", ["flask", "--app", "wsgi:app", "migrate-customer-email-schema"])
    config = type(
        "FreshMigrationSQLiteConfig",
        (TestConfig,),
        {"SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'fresh-store.sqlite3'}"},
    )
    isolated_app = create_app(config)
    with isolated_app.app_context():
        from sqlalchemy import inspect
        assert inspect(db.engine).get_table_names() == []
        db.metadata.create_all(bind=db.engine, tables=[AdminUser.__table__, Order.__table__])
    result = isolated_app.test_cli_runner().invoke(args=["migrate-customer-email-schema"])
    assert result.exit_code == 0, result.output
    assert "Created tables:" in result.output
    with isolated_app.app_context():
        from sqlalchemy import inspect
        expected = {
            "admin_user", "order", "customer_account", "customer_token",
            "customer_consent_event", "customer_order_link", "auth_rate_limit",
            "email_message", "email_campaign", "email_campaign_recipient",
        }
        assert set(inspect(db.engine).get_table_names()) == expected


def test_customer_email_postgresql_migration_keeps_locked_idempotent_sql(monkeypatch, app):
    import sqlalchemy
    statements = []

    class FakeConnection:
        def execute(self, statement):
            statements.append(str(statement))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class FakeEngine:
        def begin(self):
            return FakeConnection()

    class FakeInspector:
        calls = 0

        def get_table_names(self):
            self.calls += 1
            base = {"admin_user", "order"}
            if self.calls == 1:
                return sorted(base)
            return sorted(base | {
                "customer_account", "customer_token", "customer_consent_event",
                "customer_order_link", "auth_rate_limit", "email_message",
                "email_campaign", "email_campaign_recipient",
            })

        def get_columns(self, table_name):
            return [{"name": column.name} for column in db.metadata.tables[table_name].columns]

        def get_indexes(self, table_name):
            return [{"name": index.name, "column_names": [column.name for column in index.columns], "unique": index.unique} for index in db.metadata.tables[table_name].indexes]

        def get_unique_constraints(self, table_name):
            return [{"column_names": [column.name for column in constraint.columns]} for constraint in db.metadata.tables[table_name].constraints if constraint.__class__.__name__ == "UniqueConstraint"]

        def get_foreign_keys(self, table_name):
            return [
                {
                    "constrained_columns": [foreign_key.parent.name],
                    "referred_table": foreign_key.column.table.name,
                    "referred_columns": [foreign_key.column.name],
                }
                for foreign_key in db.metadata.tables[table_name].foreign_keys
            ]

    inspector = FakeInspector()
    with app.app_context():
        monkeypatch.setitem(db.engines, None, FakeEngine())
        monkeypatch.setattr(sqlalchemy, "inspect", lambda _engine: inspector)
        monkeypatch.setattr(app_module, "database_dialect_name", lambda: "postgresql")
        result = app.test_cli_runner().invoke(args=["migrate-customer-email-schema"])
    assert result.exit_code == 0, result.output
    assert statements[0] == "SELECT pg_advisory_xact_lock(724930117)"
    assert any("CREATE TABLE IF NOT EXISTS customer_account" in sql for sql in statements)
    assert any("CREATE TABLE IF NOT EXISTS email_campaign_recipient" in sql for sql in statements)
    assert "Created tables:" in result.output


def test_customer_email_migration_creates_missing_sqlite_tables_and_preserves_store_records(tmp_path, monkeypatch):
    isolated_app, feature_tables, expected = _isolated_legacy_sqlite_app(tmp_path, monkeypatch)
    result = isolated_app.test_cli_runner().invoke(args=["migrate-customer-email-schema"])
    assert result.exit_code == 0, result.output
    assert "Created tables:" in result.output
    assert "No existing store records were changed." in result.output
    with isolated_app.app_context():
        from sqlalchemy import inspect
        inspector = inspect(db.engine)
        assert feature_tables <= set(inspector.get_table_names())
        settings = ShopSettings.query.one()
        admin = AdminUser.query.one()
        product = Product.query.filter_by(slug="existing-product").one()
        image = ProductImage.query.filter_by(filename="existing-transparent.png").one()
        order = Order.query.filter_by(reference="KEEP-ORDER").one()
        actual = {
            "settings": (settings.shop_name, settings.tagline, settings.payment_number),
            "admin_hash": admin.password_hash,
            "product": (product.name, product.price_cents, product.previous_price_cents, product.stock_quantity, product.reserved_quantity, product.sku),
            "image": (image.filename, image.storage_backend, image.cloudinary_public_id, image.sort_order, image.is_primary, image.alt_text),
            "order": (order.reference, order.total_cents, len(order.items)),
        }
        assert actual == expected
        assert {fk["constrained_columns"][0] for fk in inspector.get_foreign_keys("customer_token")} == {"account_id"}
        assert "ix_customer_account_marketing_status" in {index["name"] for index in inspector.get_indexes("customer_account")}
        assert any(unique["name"] == "uq_campaign_recipient_account" for unique in inspector.get_unique_constraints("email_campaign_recipient"))


def test_customer_email_sqlite_migration_is_idempotent_and_enforces_uniqueness(tmp_path, monkeypatch):
    isolated_app, feature_tables, _expected = _isolated_legacy_sqlite_app(tmp_path, monkeypatch)
    runner = isolated_app.test_cli_runner()
    first = runner.invoke(args=["migrate-customer-email-schema"])
    second = runner.invoke(args=["migrate-customer-email-schema"])
    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert "Created tables: none" in second.output
    assert feature_tables <= set(db.metadata.tables)
    with isolated_app.app_context():
        db.session.add_all([
            CustomerAccount(email="duplicate@example.com", display_name="First"),
            CustomerAccount(email="duplicate@example.com", display_name="Second"),
        ])
        with pytest.raises(Exception):
            db.session.commit()
        db.session.rollback()


def test_customer_email_sqlite_migration_rolls_back_failed_ddl(tmp_path, monkeypatch):
    isolated_app, feature_tables, _expected = _isolated_legacy_sqlite_app(tmp_path, monkeypatch)
    original_create_all = db.metadata.create_all

    def fail_after_first_table(*, bind, tables, checkfirst=True):
        original_create_all(bind=bind, tables=tables[:1], checkfirst=checkfirst)
        raise RuntimeError("simulated DDL failure")

    with isolated_app.app_context():
        monkeypatch.setattr(db.metadata, "create_all", fail_after_first_table)
        result = isolated_app.test_cli_runner().invoke(args=["migrate-customer-email-schema"])
        assert result.exit_code != 0
        assert "RuntimeError" in result.output
        assert not (feature_tables & set(__import__("sqlalchemy").inspect(db.engine).get_table_names()))


def test_customer_email_migration_fails_safely_on_partial_existing_table(tmp_path, monkeypatch):
    isolated_app, feature_tables, _expected = _isolated_legacy_sqlite_app(tmp_path, monkeypatch)
    with isolated_app.app_context():
        from sqlalchemy import inspect, text
        db.session.execute(text("CREATE TABLE customer_account (id INTEGER PRIMARY KEY, email VARCHAR(254))"))
        db.session.execute(text("INSERT INTO customer_account (id, email) VALUES (1, 'keep@example.test')"))
        db.session.commit()
        result = isolated_app.test_cli_runner().invoke(args=["migrate-customer-email-schema"])
        assert result.exit_code != 0
        assert "customer/email table is incomplete" in result.output
        assert "missing columns" in result.output
        existing = set(inspect(db.engine).get_table_names()) & feature_tables
        assert existing == {"customer_account"}
        assert db.session.execute(text("SELECT email FROM customer_account WHERE id = 1")).scalar_one() == "keep@example.test"


def test_customer_email_migration_rejects_unsupported_dialect(app, monkeypatch):
    monkeypatch.setattr(app_module, "database_dialect_name", lambda: "mysql")
    result = app.test_cli_runner().invoke(args=["migrate-customer-email-schema"])
    assert result.exit_code != 0
    assert "Unsupported database dialect" in result.output


def test_customer_auth_forms_keep_csrf_protection(client, app):
    app.config["WTF_CSRF_ENABLED"] = True
    response = client.post("/account/register", data={"email": "csrf@example.com"})
    assert response.status_code == 400

def test_verified_customer_checkout_links_new_order_and_keeps_guest_path(client, app_context, app):
    from datetime import datetime
    account = CustomerAccount(email="checkout-account@example.com", display_name="Checkout", password_hash=generate_password_hash("a-strong-test-password-123"), email_verified_at=datetime.utcnow())
    db.session.add(account)
    db.session.commit()
    assert client.post("/account/login", data={"email": account.email, "password": "a-strong-test-password-123"}, follow_redirects=True).status_code == 200
    response = client.post("/checkout", data={"cart_json": "[{\"product_id\":1,\"quantity\":1}]", "customer_name": "Checkout", "fulfilment_method": "Collection"}, follow_redirects=True)
    assert response.status_code == 200
    order = Order.query.filter_by(customer_name="Checkout").one()
    assert CustomerOrderLink.query.filter_by(account_id=account.id, order_id=order.id).one()
    assert EmailMessage.query.filter_by(order_id=order.id, kind="order_received").one().status == "not_configured"


def test_payment_confirmation_email_requires_paid_order(client, app_context, app, monkeypatch):
    import app.services.customer_mail as customer_mail
    account = CustomerAccount(email="payment-owner@example.com", display_name="Payment", password_hash=generate_password_hash("a-strong-test-password-123"), email_verified_at=datetime.utcnow())
    order = Order(reference="FITY-EMAIL-PAYMENT", fulfilment_method="Collection", payment_status="Awaiting payment")
    db.session.add_all([account, order])
    db.session.flush()
    db.session.add(CustomerOrderLink(account_id=account.id, order_id=order.id))
    db.session.commit()
    assert customer_mail.notify_order_event(order, "payment_confirmed", "unpaid-payment-event") is None
    assert EmailMessage.query.filter_by(idempotency_key="unpaid-payment-event").count() == 0
    order.payment_status = "Paid"
    db.session.commit()
    assert customer_mail.notify_order_event(order, "payment_confirmed", "verified-payment-event").status == "not_configured"
    assert EmailMessage.query.filter_by(idempotency_key="verified-payment-event").one().status == "not_configured"
