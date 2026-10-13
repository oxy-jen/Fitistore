import pytest
from werkzeug.security import generate_password_hash
from app import create_app
from app.extensions import db
from app.models import AdminUser, Category, Product, ProductSuggestion, Review, ShopSettings
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
