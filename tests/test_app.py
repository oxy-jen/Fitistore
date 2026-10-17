import io

import pytest
import app as app_module
from werkzeug.security import generate_password_hash
from app import create_app
from app.extensions import db
from app.models import AdminUser, Category, Product, ProductImage, ProductSuggestion, Review, ShopSettings
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
