import io
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
import app as app_module
from werkzeug.security import check_password_hash, generate_password_hash
from app import create_app
from app.extensions import db
from app.models import AdminUser, AuthRateLimit, CartAddCount, Category, CustomerAccount, CustomerCart, CustomerConsentEvent, CustomerOrderLink, CustomerToken, EmailCampaign, EmailCampaignRecipient, EmailDraft, EmailMessage, EmailTemplate, MediaShowcase, MediaShowcaseItem, Order, OrderItem, OrderStatusHistory, Product, ProductImage, ProductLike, ProductSuggestion, ProductVariant, Review, ShopSettings, UpcomingProduct, UpcomingProductVote
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

def sign_in_verified_customer(client, email="verified-customer@example.com"):
    from app.customer.routes import TERMS_ACCEPTANCE_VERSION
    with client.application.app_context():
        account = CustomerAccount.query.filter_by(email=email).first()
        if account is None:
            account = CustomerAccount(email=email, display_name="Verified Customer", email_verified_at=datetime.utcnow())
            db.session.add(account)
            db.session.commit()
        if not CustomerConsentEvent.query.filter_by(account_id=account.id, event="terms_accepted", wording_version=TERMS_ACCEPTANCE_VERSION).first():
            db.session.add(CustomerConsentEvent(account_id=account.id, event="terms_accepted", method="test_setup", wording_version=TERMS_ACCEPTANCE_VERSION))
            db.session.commit()
        user_id = account.get_id()
    with client.session_transaction() as browser_session:
        browser_session["_user_id"] = user_id
        browser_session["_fresh"] = True

def accept_current_terms_for_test(account):
    from app.customer.routes import TERMS_ACCEPTANCE_VERSION
    db.session.add(CustomerConsentEvent(account_id=account.id, event="terms_accepted", method="test_setup", wording_version=TERMS_ACCEPTANCE_VERSION))
    db.session.commit()


def _confirmed_customer_email_migration_args():
    return [
        "migrate-customer-email-schema",
        "--confirmation", "MIGRATE CUSTOMER EMAIL SCHEMA",
        "--backup-confirmation", "I HAVE A VERIFIED RESTORABLE BACKUP",
    ]


def test_homepage_renders(client):
    rv = client.get("/")
    assert rv.status_code == 200
    assert b"Shop Smart." in rv.data
    assert b"Black Friday" in rv.data
    assert rv.data.count(b"data-home-banner-slide") == 4
    assert rv.data.index(b"aria-label=\"1 of 4: Get ready for Black Friday\"") < rv.data.index(b"aria-label=\"2 of 4: Shop smart, dress better\"")
    assert b"images/home-banner/home1banner.webp" in rv.data
    assert b"images/home-banner/home2banner.webp" in rv.data
    assert b"images/home-banner/home3banner.webp" in rv.data
    assert b"data-home-banner-next" in rv.data
    assert b"data-home-banner-toggle" in rv.data
    assert b"home-banner-pause-icon" in rv.data
    assert b"home-banner-play-icon" in rv.data
    assert b'home-banner-action-short">Track</span>' in rv.data
    assert b"data-share-facebook" not in rv.data
    assert b"data-share-native" not in rv.data
    assert b"https://www.facebook.com/oxy.jen.975729" in rv.data
    assert b"https://www.tiktok.com/@official_fitystore" in rv.data
    assert b"Manage products" not in rv.data
    assert b"Manage featured products" not in rv.data
    assert b"Manage category images" not in rv.data


def test_homepage_groups_request_and_fulfillment_options(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b'id="stock-request-title"' in response.data
    assert b"Send a stock request" in response.data
    assert b'action="/mamba-suggestion"' in response.data
    assert b"Ask on WhatsApp" in response.data
    assert b'id="home-fulfillment-title"' in response.data
    assert b'class="delivery-motorbike"' in response.data
    assert b"walker-arm-a" in response.data
    assert b"walker-arm-b" in response.data
    assert b"payment-motion" in response.data


def test_homepage_offers_scroll_copy_with_discounted_products(client, app):
    with app.app_context():
        category = Category.query.first()
        for index in range(8):
            db.session.add(Product(
                name=f"New Arrival {index}",
                slug=f"new-arrival-{index}",
                category=category,
                description="New everyday item",
                price_cents=5000,
                stock_quantity=3,
                active=True,
                created_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=index + 1),
            ))
        db.session.commit()

    response = client.get("/")
    assert response.status_code == 200
    offers = response.data.split(b'id="discounted-goods"', 1)[1]
    assert b'data-home-scroll-rail="discounted offers"' in offers
    assert b'data-home-scroll-track' in offers
    assert b'tabindex="0"' in offers
    assert b'data-home-scroll-dots' in offers
    assert b"FityStores offers" in offers
    assert b"Browse offers" in offers
    assert b"Plain Socks" in offers
    assert offers.index(b'class="offers-intro-panel"') < offers.index(b'class="offers-product-card home-quick-add-card"')
    assert b'class="product-marquee"' not in offers
    assert b'class="marquee-track"' not in offers


def test_homepage_keeps_fresh_offers_and_sorts_by_highest_discount(client, app):
    with app.app_context():
        category = Category.query.first()
        variant_offer = Product(
            name="Variant Deal",
            slug="variant-deal",
            category=category,
            description="An option-level offer",
            price_cents=10000,
            stock_quantity=4,
            active=True,
        )
        db.session.add_all([
            Product(
                name="Biggest Deal",
                slug="biggest-deal",
                category=category,
                description="Largest product discount",
                price_cents=2500,
                previous_price_cents=10000,
                stock_quantity=4,
                active=True,
            ),
            Product(
                name="Middle Deal",
                slug="middle-deal",
                category=category,
                description="Middle product discount",
                price_cents=5000,
                previous_price_cents=10000,
                stock_quantity=4,
                active=True,
            ),
            variant_offer,
        ])
        db.session.flush()
        db.session.add(ProductVariant(
            product=variant_offer,
            price_cents=10000,
            previous_price_cents=30000,
            stock_quantity=4,
            active=True,
        ))
        db.session.commit()

    response = client.get("/")
    assert response.status_code == 200
    offers = response.data.split(b'id="discounted-goods"', 1)[1]
    assert offers.index(b"Biggest Deal") < offers.index(b"Variant Deal")
    assert offers.index(b"Variant Deal") < offers.index(b"Middle Deal")
    assert offers.index(b"Middle Deal") < offers.index(b"Plain Socks")
    assert b'class="offers-product-card home-quick-add-card"' in offers
    assert b'class="home-card-quick-add" href="/product/biggest-deal"' in offers


def test_admin_homepage_has_no_management_links_or_share_controls(client, app):
    _sign_in_admin(client, app)
    response = client.get("/")
    assert response.status_code == 200
    assert b"Manage products" not in response.data
    assert b"Manage featured products" not in response.data
    assert b"Manage category images" not in response.data
    assert b"data-share-facebook" not in response.data
    assert b"data-share-native" not in response.data


def test_postgresql_pool_checks_stale_connections_and_recycles():
    class NeonTestConfig(TestConfig):
        SQLALCHEMY_DATABASE_URI = "postgresql+psycopg://test-user:test-password@localhost/fity_test"

    app = create_app(NeonTestConfig)
    with app.app_context():
        pool = db.engine.pool
        assert pool._pre_ping is True
        assert pool._recycle == 300
        db.engine.dispose()


def test_psycopg_admin_shutdown_is_disconnect_when_driver_marks_connection_broken():
    from types import SimpleNamespace
    import psycopg
    from sqlalchemy.dialects.postgresql.psycopg import PGDialect_psycopg

    dialect = PGDialect_psycopg(dbapi=psycopg)
    error = psycopg.errors.AdminShutdown("terminating connection due to administrator command")
    assert dialect.is_disconnect(error, SimpleNamespace(closed=False, broken=True), None)


def test_homepage_reports_database_disconnect_then_recovers_on_next_request(client, monkeypatch):
    import app.public.routes as public_routes
    from sqlalchemy.exc import OperationalError

    client.application.config["PROPAGATE_EXCEPTIONS"] = False
    original_product_list_options = public_routes.product_list_options
    fail_once = {"remaining": 1}

    class FailingFirstQuery:
        def __init__(self, query):
            self.query = query

        def order_by(self, *columns):
            self.query = self.query.order_by(*columns)
            return self

        def limit(self, count):
            self.query = self.query.limit(count)
            return self

        def all(self):
            if fail_once["remaining"]:
                fail_once["remaining"] -= 1
                raise OperationalError(
                    "SELECT homepage products",
                    {},
                    RuntimeError("simulated Neon AdminShutdown"),
                    connection_invalidated=True,
                )
            return self.query.all()

    def fail_one_catalogue_read(query):
        if fail_once["remaining"]:
            return FailingFirstQuery(query)
        return original_product_list_options(query)

    monkeypatch.setattr(public_routes, "product_list_options", fail_one_catalogue_read)
    failed = client.get("/")
    recovered = client.get("/")

    assert failed.status_code == 500
    assert recovered.status_code == 200
    assert b"Plain Socks" in recovered.data
    assert fail_once["remaining"] == 0


def test_pwa_manifest_icons_install_controls_and_worker_are_available(client):
    page = client.get("/")
    assert page.status_code == 200
    assert b'rel="manifest"' in page.data
    assert b'data-pwa-help' in page.data
    assert b"pwa-install.js" in page.data

    manifest_response = client.get("/static/manifest.webmanifest")
    assert manifest_response.status_code == 200
    manifest = manifest_response.get_json()
    assert manifest["name"] == "FityStores"
    assert manifest["start_url"] == "/"
    assert manifest["scope"] == "/"
    assert manifest["display"] == "standalone"
    assert manifest["orientation"] == "any"
    for icon in manifest["icons"]:
        response = client.get(icon["src"])
        assert response.status_code == 200
        assert response.mimetype == "image/png"
        width, height = __import__("struct").unpack(">II", response.data[16:24])
        assert (width, height) == tuple(int(value) for value in icon["sizes"].split("x"))
    assert {item["purpose"] for item in manifest["icons"]} >= {"any", "maskable"}

    offline = client.get("/static/pwa/offline.html")
    assert offline.status_code == 200
    assert b"You're offline" in offline.data

    worker = client.get("/service-worker.js")
    assert worker.status_code == 200
    assert worker.headers["Service-Worker-Allowed"] == "/"
    assert "no-cache" in worker.headers["Cache-Control"]
    assert b"request.method !== \"GET\"" in worker.data
    assert b"/static/css/" in worker.data and b"/static/images/" in worker.data
    assert b"/static/uploads/" not in worker.data
    assert b"request.mode === \"navigate\"" in worker.data
    allowlist = worker.data.split(b"function isSafeStaticAsset(url) {", 1)[1].split(b"}", 1)[0]
    assert b"/static/css/" in allowlist
    assert b"/static/js/" in allowlist
    assert b"/static/images/" in allowlist
    assert b"/api/" not in allowlist and b"/account" not in allowlist


def test_assetlinks_returns_empty_until_real_fingerprint_is_configured(client, app):
    response = client.get("/.well-known/assetlinks.json")
    assert response.status_code == 200
    assert response.get_json() == []

    fingerprint = ":".join(["AB"] * 32)
    with app.app_context():
        app.config["ANDROID_ASSETLINKS_SHA256_CERT_FINGERPRINTS"] = fingerprint
    response = client.get("/.well-known/assetlinks.json")
    assert response.status_code == 200
    assert response.get_json() == [{
        "relation": ["delegate_permission/common.handle_all_urls"],
        "target": {
            "namespace": "android_app",
            "package_name": "top.fitystore.app",
            "sha256_cert_fingerprints": [fingerprint],
        },
    }]

    with app.app_context():
        app.config["ANDROID_ASSETLINKS_SHA256_CERT_FINGERPRINTS"] = "replace-with-real-fingerprint"
    assert client.get("/.well-known/assetlinks.json").get_json() == []


def _sign_in_admin(client, app):
    with app.app_context():
        user_id = str(AdminUser.query.filter_by(username="owner").one().get_id())
    with client.session_transaction() as browser_session:
        browser_session["_user_id"] = user_id
        browser_session["_fresh"] = True


def test_media_showcase_auth_creation_and_homepage_slot(client, app):
    assert client.get("/admin/media-showcases").status_code == 302
    _sign_in_admin(client, app)
    item = [{"type": "image", "title": "Store feature", "url": "https://cdn.example.test/feature.jpg", "alt": "Store feature", "presentation": "contain", "aspect_ratio": "9:16", "destination": "/shop?sort=newest", "cta": "Shop this item"}]
    response = client.post("/admin/media-showcases/new", data={
        "name": "FityStores TV", "title": "See it. Love it. Shop it.", "page": "home",
        "placement": "home_after_hero", "layout": "carousel", "card_aspect_ratio": "3:2",
        "is_published": "1", "is_enabled": "1",
        "items_json": json.dumps(item),
    }, follow_redirects=True)
    assert response.status_code == 200
    assert b"Media showcase created" in response.data
    page = client.get("/")
    assert page.status_code == 200
    media_position = page.data.find(b'id="media-showcase-fitystores-tv"')
    featured_position = page.data.find(b'id="category-showcase"')
    assert media_position >= 0, page.data[page.data.find(b"FityStores TV") - 100:page.data.find(b"FityStores TV") + 300]
    assert featured_position >= 0
    assert media_position < featured_position
    assert b"See it. Love it. Shop it." in page.data
    assert b"media-showcase-items-single" in page.data
    assert b"media-presentation-contain" in page.data
    assert b'data-aspect-ratio="9:16"' in page.data
    assert b"--media-item-ratio:9.0 / 16.0" in page.data
    assert b'class="media-showcase-title-link" href="/shop?sort=newest">Store feature</a>' in page.data
    assert b"Our best picks, videos, and more" in page.data
    assert b"--showcase-media-ratio: 3 / 2" in page.data
    assert b"data-showcase-scroll" not in page.data
    assert b"data-showcase-media-open" in page.data
    assert page.data.count(b"data-showcase-featured") == 1
    with app.app_context():
        showcase = MediaShowcase.query.filter_by(slug="fitystores-tv").one()
        saved = showcase.items[0]
        showcase_id = showcase.id
        assert showcase.card_aspect_ratio == "3:2"
        assert saved.presentation_mode == "contain"
        assert saved.aspect_ratio == "9:16"
    preview = client.get(f"/admin/media-showcases/{showcase_id}/preview")
    assert preview.status_code == 200
    assert b'class="media-showcase-title-link" href="/shop?sort=newest">Store feature</a>' in preview.data
    assert b"media-showcase-heading-icon" in preview.data


def test_media_showcase_auto_opens_first_active_item_once_per_page_by_default(client, app):
    _sign_in_admin(client, app)
    client.post("/admin/media-showcases/new", data={
        "name": "First featured rail", "page": "home", "placement": "home_after_hero",
        "layout": "carousel", "publish_action": "publish", "is_enabled": "1",
        "items_json": json.dumps([
            {"type": "image", "title": "Inactive image", "url": "https://cdn.example.test/inactive.jpg", "active": False},
            {"type": "gif", "title": "Selected animated feature", "url": "https://cdn.example.test/selected.gif"},
            {"type": "image", "title": "Later image", "url": "https://cdn.example.test/later.jpg"},
        ]),
    })
    client.post("/admin/media-showcases/new", data={
        "name": "Second featured rail", "page": "home", "placement": "home_before_departments",
        "layout": "split", "publish_action": "publish", "is_enabled": "1",
        "items_json": json.dumps([
            {"type": "video", "title": "Later video", "url": "https://cdn.example.test/later.mp4"},
        ]),
    })

    page = client.get("/")
    assert page.status_code == 200
    assert page.data.count(b"data-showcase-featured") == 1
    selected = re.search(
        rb'<article[^>]*data-showcase-featured[^>]*>(.*?)</article>',
        page.data,
        re.S,
    )
    assert selected
    assert b"Selected animated feature" in selected.group(1)
    assert b'data-media-kind="gif"' in selected.group(1)
    assert b"Later video" not in selected.group(1)
    with app.app_context():
        preview_id = MediaShowcase.query.filter_by(slug="first-featured-rail").one().id
    preview = client.get(f"/admin/media-showcases/{preview_id}/preview")
    assert preview.status_code == 200
    assert preview.data.count(b"data-showcase-featured") == 1


def test_media_showcase_editor_offers_fields_and_existing_products(client, app):
    _sign_in_admin(client, app)
    quick_publish = client.get("/admin/media-showcases/quick-publish")
    assert quick_publish.status_code == 200
    assert b'name="media"' in quick_publish.data
    assert b'name="media_url"' in quick_publish.data
    assert b'Display name (shown to customers)' in quick_publish.data
    assert b'required type="text" name="title"' in quick_publish.data
    assert b"Use the full showcase editor" in quick_publish.data
    response = client.get("/admin/media-showcases/new")
    assert response.status_code == 200
    assert b"data-add-showcase-item" in response.data
    assert b"showcase-products-data" in response.data
    assert b"Plain Socks" in response.data
    assert b"media-showcase-editor.js" in response.data
    assert b'name="publish_action" value="publish"' in response.data
    assert b'data-bulk-upload' in response.data
    assert b'data-bulk-links' in response.data
    assert b"The floating player opens the first active supported media item" in response.data
    assert b'name="card_aspect_ratio"' in response.data
    assert b'value="16:9" selected' in response.data
    assert b"#fff0f6" in response.data
    editor = client.get("/static/js/media-showcase-editor.js")
    assert b"Set as popup feature" in editor.data
    assert b"Featured popup item" in editor.data
    assert b"Display name (shown to customers)" in editor.data


def test_media_showcase_quick_publish_accepts_upload_or_direct_video_url(client, app):
    from pathlib import Path
    from tempfile import TemporaryDirectory

    _sign_in_admin(client, app)
    static_root = Path(app.static_folder)
    video_bytes = b"\x00\x00\x00\x18ftypisom" + (b"\x00" * 24)
    with TemporaryDirectory(prefix="test-quick-showcase-", dir=static_root) as upload_root:
        app.config["UPLOAD_FOLDER"] = upload_root
        uploaded = client.post("/admin/media-showcases/quick-publish", data={
            "media": (io.BytesIO(video_bytes), "launch.mp4"),
            "page": "home", "placement": "home_after_hero", "title": "Launch clip",
        }, content_type="multipart/form-data", follow_redirects=True)
        assert uploaded.status_code == 200
        assert b"Media published" in uploaded.data
        with app.app_context():
            showcase = MediaShowcase.query.filter_by(name="Quick publish: Launch clip").one()
            uploaded_item = showcase.items[0]
            assert showcase.is_published and showcase.is_enabled
            assert showcase.layout == "single"
            assert uploaded_item.media_type == "video"
            assert uploaded_item.media_url.startswith("/static/")
            assert uploaded_item.source_type == "upload"
            assert uploaded_item.show_controls

    linked = client.post("/admin/media-showcases/quick-publish", data={
        "media_url": "https://cdn.example.test/launch.mp4", "media_type": "video",
        "page": "home", "placement": "home_after_hero", "title": "Hosted clip",
    }, follow_redirects=True)
    assert linked.status_code == 200
    assert b"Media published" in linked.data
    with app.app_context():
        showcase = MediaShowcase.query.filter_by(name="Quick publish: Hosted clip").one()
        assert showcase.items[0].media_url == "https://cdn.example.test/launch.mp4"
        assert showcase.items[0].title == "Hosted clip"
        assert showcase.items[0].source_type == "external_url"
    page = client.get("/")
    assert b'data-media-title="Hosted clip"' in page.data
    assert b'data-preview-src="https://cdn.example.test/launch.mp4"' in page.data
    assert b'data-media-title="https://cdn.example.test/launch.mp4"' not in page.data


def test_media_showcase_quick_publish_requires_a_display_name(client, app):
    _sign_in_admin(client, app)
    response = client.post("/admin/media-showcases/quick-publish", data={
        "media_url": "https://cdn.example.test/launch.mp4", "media_type": "video",
        "page": "home", "placement": "home_after_hero",
    })
    assert response.status_code == 200
    assert b"Enter the display name to show for this media." in response.data
    with app.app_context():
        assert MediaShowcase.query.filter(MediaShowcase.name.like("Quick publish:%")).count() == 0


def test_media_showcase_quick_publish_requires_exactly_one_source(client, app):
    _sign_in_admin(client, app)
    response = client.post("/admin/media-showcases/quick-publish", data={
        "page": "home", "placement": "home_after_hero",
    })
    assert response.status_code == 200
    assert b"Choose a video/image file or paste a direct media URL" in response.data
    with app.app_context():
        assert MediaShowcase.query.filter(MediaShowcase.name.like("Quick publish:%")).count() == 0


def test_media_showcases_sharing_a_slot_render_as_one_carousel(client, app):
    _sign_in_admin(client, app)
    for name in ("Shared first", "Shared second"):
        client.post("/admin/media-showcases/new", data={
            "name": name, "page": "home", "placement": "home_after_hero", "layout": "split",
            "publish_action": "publish", "is_enabled": "1",
            "items_json": json.dumps([{"type": "image", "title": name, "url": f"https://cdn.example.test/{name.replace(' ', '').lower()}.jpg"}]),
        })
    page = client.get("/")
    assert page.status_code == 200
    assert page.data.count(b'data-showcase-count="2"') == 1
    assert page.data.count(b'class="media-showcase-card"') >= 2
    assert b"Shared first" in page.data and b"Shared second" in page.data
    assert b"media-layout-carousel" in page.data
    assert b"View All Media" in page.data
    assert b"media-showcase-heading-icon" in page.data


def test_media_showcase_list_has_publish_and_unpublish_actions(client, app):
    _sign_in_admin(client, app)
    client.post("/admin/media-showcases/new", data={
        "name": "Draft publish control", "page": "home", "placement": "home_after_hero",
        "items_json": json.dumps([{"type": "image", "title": "Draft", "url": "https://cdn.example.test/draft.jpg"}]),
    })
    response = client.get("/admin/media-showcases")
    assert response.status_code == 200
    assert b"Publish now" in response.data
    with app.app_context():
        showcase_id = MediaShowcase.query.filter_by(name="Draft publish control").one().id
    client.post(f"/admin/media-showcases/{showcase_id}/action", data={"action": "publish"})
    with app.app_context():
        assert db.session.get(MediaShowcase, showcase_id).is_published is True


def test_media_showcase_library_upload_embed_colours_and_safe_asset_retention(client, app):
    from pathlib import Path
    from tempfile import TemporaryDirectory
    from PIL import Image

    _sign_in_admin(client, app)
    library = client.get("/admin/media-showcases/library")
    assert library.status_code == 200
    assert library.json["cloudinary"] == "not_configured"
    assert "items" in library.json

    image_bytes = io.BytesIO()
    Image.new("RGB", (18, 32), "red").save(image_bytes, format="GIF")
    payload_bytes = image_bytes.getvalue()
    static_root = Path(app.static_folder)
    with TemporaryDirectory(prefix="test-showcase-uploads-", dir=static_root) as upload_root:
        app.config["UPLOAD_FOLDER"] = upload_root
        first_upload = client.post("/admin/media-showcases/upload", data={"media": (io.BytesIO(payload_bytes), "portrait.gif")}, content_type="multipart/form-data")
        second_upload = client.post("/admin/media-showcases/upload", data={"media": (io.BytesIO(payload_bytes), "portrait.gif")}, content_type="multipart/form-data")
        assert first_upload.status_code == second_upload.status_code == 200
        uploaded = first_upload.json
        assert uploaded["source"] == "upload"
        assert uploaded["type"] == "gif"
        assert uploaded["width"] == 18 and uploaded["height"] == 32
        assert first_upload.json["url"] == second_upload.json["url"]
        assert client.get(uploaded["url"]).status_code == 200
        stored_files = list((Path(upload_root) / "media-showcases").iterdir())
        assert len(stored_files) == 1

        invalid_upload = client.post("/admin/media-showcases/upload", data={"media": (io.BytesIO(b"not an image"), "bad.gif")}, content_type="multipart/form-data")
        assert invalid_upload.status_code == 400

        items = [
            {"type": "gif", "title": "Uploaded portrait", "url": uploaded["url"], "source": uploaded["source"], "filename": uploaded["filename"], "size": uploaded["size"], "width": uploaded["width"], "height": uploaded["height"], "presentation": "contain", "aspect_ratio": "9:16"},
            {"type": "video", "title": "YouTube sample", "url": "https://www.youtube.com/watch?v=7cjVj1ZyzyE", "source": "video_embed", "active": True},
        ]
        saved = client.post("/admin/media-showcases/new", data={
            "name": "Media studio flow", "title": "Mixed media", "page": "home", "placement": "home_after_hero",
            "layout": "carousel", "is_published": "1", "is_enabled": "1", "background_color": "#ffffff",
            "heading_color": "#252525", "description_color": "#666666", "accent_color": "#e52335",
            "card_background_color": "#ffffff", "border_color": "transparent", "button_background_color": "#e52335",
            "button_text_color": "#ffffff", "items_json": json.dumps(items),
        }, follow_redirects=True)
        assert saved.status_code == 200 and b"Media showcase created" in saved.data
        page = client.get("/")
        assert b"youtube-nocookie.com/embed/7cjVj1ZyzyE" in page.data
        assert b"media-presentation-contain" in page.data
        with app.app_context():
            showcase = MediaShowcase.query.filter_by(slug="media-studio-flow").one()
            assert showcase.background_color == "#ffffff"
            assert showcase.items[0].source_type == "upload"
            assert showcase.items[0].media_width == 18 and showcase.items[0].media_height == 32
            assert showcase.items[1].source_type == "video_embed"
            showcase_id = showcase.id
        deleted = client.post(f"/admin/media-showcases/{showcase_id}/action", data={"action": "delete"}, follow_redirects=True)
        assert deleted.status_code == 200
        assert stored_files[0].exists(), "Deleting showcase records must preserve shared media files."


def test_media_showcase_accepts_existing_local_static_gif_and_rejects_traversal(client, app):
    _sign_in_admin(client, app)
    payload = [{"type": "gif", "title": "GIF sample", "url": "/static/gifexample/hurry.gif"}]
    response = client.post("/admin/media-showcases/new", data={
        "name": "Local GIF sample", "page": "home", "placement": "home_after_hero",
        "layout": "single", "items_json": json.dumps(payload),
    }, follow_redirects=True)
    assert response.status_code == 200
    assert b"Media showcase created" in response.data
    with app.app_context():
        showcase_id = MediaShowcase.query.filter_by(slug="local-gif-sample").one().id
    preview = client.get(f"/admin/media-showcases/{showcase_id}/preview")
    assert b'src="/static/gifexample/hurry.gif"' in preview.data
    invalid = client.post("/admin/media-showcases/new", data={
        "name": "Traversal", "page": "home", "placement": "home_after_hero", "layout": "single",
        "items_json": json.dumps([{"type": "gif", "title": "Bad", "url": "/static/../app.py"}]),
    })
    with app.app_context():
        assert MediaShowcase.query.filter_by(name="Traversal").first() is None


def test_media_showcase_rejects_unsafe_media_url_and_customer_is_forbidden(client, app):
    sign_in_verified_customer(client)
    assert client.get("/admin/media-showcases").status_code == 403
    client.get("/admin/logout")
    _sign_in_admin(client, app)
    response = client.post("/admin/media-showcases/new", data={
        "name": "Unsafe", "page": "home", "placement": "home_after_hero", "layout": "carousel",
        "items_json": json.dumps([{"type": "video", "title": "Bad", "url": "javascript:alert(1)"}]),
    })
    assert response.status_code == 200
    assert b"Use a valid http or https URL" in response.data
    with app.app_context():
        assert MediaShowcase.query.count() == 0


def test_media_showcase_duplicate_disable_and_schedule_visibility(client, app):
    _sign_in_admin(client, app)
    item = [{"type": "gif", "title": "Animated card", "url": "https://cdn.example.test/card.gif"}]
    client.post("/admin/media-showcases/new", data={"name": "Cards", "page": "about", "placement": "about_after_banner", "layout": "grid", "is_published": "1", "is_enabled": "1", "items_json": json.dumps(item)})
    with app.app_context():
        original = MediaShowcase.query.one()
        original_id = original.id
    client.post(f"/admin/media-showcases/{original_id}/duplicate")
    with app.app_context():
        copies = MediaShowcase.query.order_by(MediaShowcase.id).all()
        assert len(copies) == 2
        assert copies[1].is_published is False
        duplicate_id = copies[1].id
        original_id = copies[0].id
    client.post(f"/admin/media-showcases/{duplicate_id}/action", data={"action": "up"})
    with app.app_context():
        assert MediaShowcase.query.get(duplicate_id).display_order == 0
        assert MediaShowcase.query.get(original_id).display_order == 1
    client.post(f"/admin/media-showcases/{duplicate_id}/action", data={"action": "toggle"})
    with app.app_context():
        assert MediaShowcase.query.get(duplicate_id).is_enabled is False
    page = client.get("/about")
    assert b"Animated card" in page.data


def test_media_showcase_supports_registered_shop_and_upcoming_slots(client, app):
    _sign_in_admin(client, app)
    for page, placement in (("shop", "shop_before_products"), ("upcoming", "upcoming_before_products")):
        client.post("/admin/media-showcases/new", data={
            "name": f"{page} showcase", "title": f"Feature on {page}", "page": page,
            "placement": placement, "layout": "grid", "is_published": "1", "is_enabled": "1",
            "items_json": json.dumps([{"type": "link", "title": f"{page} link", "destination": "https://example.test/campaign"}]),
        })
    assert b"Feature on shop" in client.get("/shop").data
    assert b"Feature on upcoming" in client.get("/upcoming").data
    upcoming_page = client.get("/upcoming").data
    assert b"upcoming-page-layout" in upcoming_page
    assert b"Feature on upcoming" in upcoming_page


def test_upcoming_dashboard_uses_saved_requests_and_cart_activity(client, app):
    with app.app_context():
        product = Product.query.filter_by(slug="plain-socks").first()
        db.session.add_all([
            ProductSuggestion(product_name="Crew socks", category="Accessories"),
            ProductSuggestion(product_name="Crew socks", category="Accessories"),
            ProductSuggestion(product_name="Slides", category="Shoes"),
            CartAddCount(product_id=product.id, add_count=7),
        ])
        db.session.commit()

    response = client.get("/upcoming")
    assert response.status_code == 200
    assert b"Something Good Is Coming" in response.data
    assert b"Upcoming product grid" in response.data
    assert b"Accessories" in response.data and b"2 saved requests" in response.data
    assert b"Merchandise people are watching" in response.data
    assert b"Recent requests" in response.data and b"Global trend watch" in response.data
    assert b"Plain Socks" in response.data and b"7 cart additions" in response.data
    assert b'name="return_to" value="upcoming"' in response.data
    assert response.data.count(b'name="product_name"') == 1


def test_public_pages_remain_available_before_media_showcase_migration(client, app):
    with app.app_context():
        db.session.remove()
        MediaShowcaseItem.__table__.drop(db.engine)
        MediaShowcase.__table__.drop(db.engine)

    for path in ("/", "/shop", "/about", "/upcoming"):
        response = client.get(path)
        assert response.status_code == 200, path

    _sign_in_admin(client, app)
    response = client.get("/admin/media-showcases")
    assert response.status_code == 200
    assert b"setup is pending or failed" in response.data


def test_showcase_schema_initialization_is_idempotent_and_preserves_existing_rows(app):
    from sqlalchemy import inspect
    from app.services.media_showcases import ensure_media_showcase_schema

    with app.app_context():
        db.session.add(MediaShowcase(
            name="Existing draft", title="Keep this", slug="existing-draft",
            page_identifier="home", placement_identifier="home_after_hero",
        ))
        db.session.commit()
        before_business = {
            "products": [(row.id, row.name, row.stock_quantity, row.price_cents) for row in Product.query.order_by(Product.id)],
            "categories": [(row.id, row.name, row.slug) for row in Category.query.order_by(Category.id)],
            "admins": [(row.id, row.username) for row in AdminUser.query.order_by(AdminUser.id)],
        }
        before_feature = [(row.id, row.slug, row.title, row.is_published) for row in MediaShowcase.query.order_by(MediaShowcase.id)]
        inspector = inspect(db.engine)
        before_tables = set(inspector.get_table_names())
        before_indexes = {
            table.name: {index["name"] for index in inspector.get_indexes(table.name)}
            for table in (MediaShowcase.__table__, MediaShowcaseItem.__table__)
        }

        assert ensure_media_showcase_schema() is True
        assert ensure_media_showcase_schema() is True

        # An individually missing supporting index is repaired without touching
        # the existing showcase row or any business table.
        db.session.remove()
        missing_index = sorted(MediaShowcase.__table__.indexes, key=lambda index: index.name)[0]
        missing_index.drop(bind=db.engine)
        assert ensure_media_showcase_schema() is True

        inspector = inspect(db.engine)
        assert set(inspector.get_table_names()) == before_tables
        after_indexes = {
            table.name: {index["name"] for index in inspector.get_indexes(table.name)}
            for table in (MediaShowcase.__table__, MediaShowcaseItem.__table__)
        }
        after_business = {
            "products": [(row.id, row.name, row.stock_quantity, row.price_cents) for row in Product.query.order_by(Product.id)],
            "categories": [(row.id, row.name, row.slug) for row in Category.query.order_by(Category.id)],
            "admins": [(row.id, row.username) for row in AdminUser.query.order_by(AdminUser.id)],
        }
        after_feature = [(row.id, row.slug, row.title, row.is_published) for row in MediaShowcase.query.order_by(MediaShowcase.id)]
        assert after_business == before_business
        assert after_feature == before_feature
        assert after_indexes == before_indexes


def test_startup_initializes_missing_showcase_tables_and_retries_after_failure(tmp_path, monkeypatch):
    from sqlalchemy import inspect
    from app import create_web_app
    from app.services import media_showcases as showcase_service

    database_uri = f"sqlite:///{(tmp_path / 'startup-showcases.sqlite').as_posix()}"
    bootstrap_config = type("ShowcaseBootstrapConfig", (TestConfig,), {
        "TESTING": True, "SQLALCHEMY_DATABASE_URI": database_uri,
    })
    startup_config = type("ShowcaseStartupConfig", (TestConfig,), {
        "TESTING": False, "SQLALCHEMY_DATABASE_URI": database_uri,
    })
    bootstrap_app = create_app(bootstrap_config)
    showcase_names = {MediaShowcase.__tablename__, MediaShowcaseItem.__tablename__}
    startup_feature_names = showcase_names | {UpcomingProduct.__tablename__}
    base_tables = [table for table in db.metadata.sorted_tables if table.name not in startup_feature_names]
    with bootstrap_app.app_context():
        db.metadata.create_all(bind=db.engine, tables=base_tables)
        before_tables = set(inspect(db.engine).get_table_names())
        category = Category(name="Startup category", slug="startup-category")
        admin = AdminUser(username="owner", password_hash="test-hash")
        db.session.add_all([category, admin, ShopSettings(shop_name="Startup store")])
        db.session.flush()
        product = Product(name="Preserved item", slug="preserved-item", category=category, price_cents=12500, stock_quantity=9)
        db.session.add(product)
        db.session.commit()
        product_id = product.id
        admin_id = admin.id

    def fail_initialization():
        raise RuntimeError("database details must not reach the public page")

    with monkeypatch.context() as patcher:
        patcher.setattr(showcase_service, "ensure_media_showcase_schema", fail_initialization)
        failed_app = create_web_app(startup_config)
        assert failed_app.extensions["media_showcase_schema_ready"] is False
        failed_client = failed_app.test_client()
        assert failed_client.get("/").status_code == 200
        _sign_in_admin(failed_client, failed_app)
        admin_status = failed_client.get("/admin/media-showcases")
        assert admin_status.status_code == 200
        assert b"setup is pending or failed" in admin_status.data

    initialization_calls = []
    real_initializer = showcase_service.ensure_media_showcase_schema

    def counted_initializer():
        initialization_calls.append(True)
        return real_initializer()

    monkeypatch.setattr(showcase_service, "ensure_media_showcase_schema", counted_initializer)
    ready_app = create_web_app(startup_config)
    assert ready_app.extensions["media_showcase_schema_ready"] is True
    assert initialization_calls == [True]
    with ready_app.app_context():
        inspector = inspect(db.engine)
        assert inspector.has_table("media_showcase")
        assert inspector.has_table("media_showcase_item")
        assert inspector.has_table("upcoming_product")
        assert ready_app.extensions["upcoming_product_schema_ready"] is True
        assert set(inspector.get_table_names()) - before_tables == startup_feature_names
        assert db.session.get(Product, product_id).stock_quantity == 9
        assert db.session.get(Product, product_id).price_cents == 12500
        assert db.session.get(AdminUser, admin_id).username == "owner"
        assert MediaShowcase.query.count() == 0
        showcase = MediaShowcase(
            name="Startup feature", title="Startup feature", slug="startup-feature",
            page_identifier="home", placement_identifier="home_after_hero",
            layout="single", is_published=True, is_enabled=True,
        )
        showcase.items.append(MediaShowcaseItem(
            media_type="image", title="Feature image", media_url="https://cdn.example.test/feature.jpg",
        ))
        db.session.add(showcase)
        db.session.commit()
    page = ready_app.test_client().get("/")
    assert page.status_code == 200
    assert b"Startup feature" in page.data
    assert ready_app.test_client().get("/shop").status_code == 200
    assert initialization_calls == [True]
    ready_client = ready_app.test_client()
    _sign_in_admin(ready_client, ready_app)
    admin_page = ready_client.get("/admin/media-showcases")
    assert admin_page.status_code == 200
    assert b"setup is pending or failed" not in admin_page.data


def test_upcoming_schema_initializes_before_public_page_queries(tmp_path):
    from sqlalchemy import inspect
    from app import create_cli_app, create_web_app

    database_uri = f"sqlite:///{(tmp_path / 'startup-upcoming-products.sqlite').as_posix()}"
    bootstrap_config = type("UpcomingBootstrapConfig", (TestConfig,), {
        "TESTING": True, "SQLALCHEMY_DATABASE_URI": database_uri,
    })
    startup_config = type("UpcomingStartupConfig", (TestConfig,), {
        "TESTING": False, "SQLALCHEMY_DATABASE_URI": database_uri,
    })
    bootstrap_app = create_cli_app(bootstrap_config)
    with bootstrap_app.app_context():
        base_tables = [table for table in db.metadata.sorted_tables if table.name != UpcomingProduct.__tablename__]
        db.metadata.create_all(bind=db.engine, tables=base_tables)

    web_app = create_web_app(startup_config)
    assert web_app.extensions["upcoming_product_schema_ready"] is True
    with web_app.app_context():
        assert inspect(db.engine).has_table("upcoming_product")
    response = web_app.test_client().get("/upcoming")
    assert response.status_code == 200


def test_upcoming_public_page_degrades_when_startup_schema_setup_fails(tmp_path, monkeypatch):
    from app import create_cli_app, create_web_app
    from app.services import upcoming_products as upcoming_service

    database_uri = f"sqlite:///{(tmp_path / 'failed-upcoming-products.sqlite').as_posix()}"
    bootstrap_config = type("UpcomingFailureBootstrapConfig", (TestConfig,), {
        "TESTING": True, "SQLALCHEMY_DATABASE_URI": database_uri,
    })
    startup_config = type("UpcomingFailureStartupConfig", (TestConfig,), {
        "TESTING": False, "SQLALCHEMY_DATABASE_URI": database_uri,
    })
    bootstrap_app = create_cli_app(bootstrap_config)
    with bootstrap_app.app_context():
        excluded = {UpcomingProduct.__tablename__, MediaShowcase.__tablename__, MediaShowcaseItem.__tablename__}
        base_tables = [table for table in db.metadata.sorted_tables if table.name not in excluded]
        db.metadata.create_all(bind=db.engine, tables=base_tables)
        db.session.add(AdminUser(username="owner", password_hash=generate_password_hash("secret123")))
        db.session.commit()

    def fail_upcoming_setup():
        raise RuntimeError("database details must not reach the public page")

    monkeypatch.setattr(upcoming_service, "ensure_upcoming_product_schema", fail_upcoming_setup)
    web_app = create_web_app(startup_config)
    assert web_app.extensions["upcoming_product_schema_ready"] is False
    assert web_app.test_client().get("/upcoming").status_code == 200
    admin_client = web_app.test_client()
    _sign_in_admin(admin_client, web_app)
    admin_response = admin_client.get("/admin/upcoming-products")
    assert admin_response.status_code == 200
    assert b"Upcoming product storage is pending" in admin_response.data


def test_cli_app_skips_web_only_showcase_initialization(tmp_path, monkeypatch):
    from sqlalchemy import inspect
    from app import create_cli_app
    from app.services import media_showcases as showcase_service

    database_uri = f"sqlite:///{(tmp_path / 'cli-no-showcase-init.sqlite').as_posix()}"
    config = type("ShowcaseCliConfig", (TestConfig,), {
        "TESTING": False, "SQLALCHEMY_DATABASE_URI": database_uri,
    })
    bootstrap_app = create_cli_app(config)
    showcase_names = {MediaShowcase.__tablename__, MediaShowcaseItem.__tablename__}
    base_tables = [table for table in db.metadata.sorted_tables if table.name not in showcase_names]
    with bootstrap_app.app_context():
        db.metadata.create_all(bind=db.engine, tables=base_tables)

    def unexpected_initialization():
        raise AssertionError("web-only showcase initialization ran during CLI loading")

    monkeypatch.setattr(showcase_service, "ensure_media_showcase_schema", unexpected_initialization)
    cli_app = create_cli_app(config)
    assert cli_app.extensions["media_showcase_schema_ready"] is None
    result = cli_app.test_cli_runner().invoke(args=["migrate-upcoming-products"])
    assert result.exit_code == 0, result.output
    with cli_app.app_context():
        inspector = inspect(db.engine)
        assert not any(inspector.has_table(name) for name in showcase_names)
        assert inspector.has_table("upcoming_product")


def test_concurrent_showcase_initialization_serializes_and_completes(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from sqlalchemy import create_engine, inspect
    from app.services.media_showcases import ensure_media_showcase_schema

    engine = create_engine(f"sqlite:///{(tmp_path / 'concurrent-showcases.sqlite').as_posix()}", connect_args={"timeout": 20})
    showcase_names = {MediaShowcase.__tablename__, MediaShowcaseItem.__tablename__}
    base_tables = [table for table in db.metadata.sorted_tables if table.name not in showcase_names]
    db.metadata.create_all(bind=engine, tables=base_tables)
    try:
        with ThreadPoolExecutor(max_workers=4) as workers:
            results = list(workers.map(lambda _index: ensure_media_showcase_schema(engine), range(4)))
        assert results == [True, True, True, True]
        inspector = inspect(engine)
        assert inspector.has_table("media_showcase")
        assert inspector.has_table("media_showcase_item")
        assert {index["name"] for index in inspector.get_indexes("media_showcase_item")} >= {"ix_media_showcase_item_showcase_id"}
    finally:
        engine.dispose()


def test_postgresql_setup_uses_transaction_scoped_advisory_lock():
    from app.services.media_showcases import SHOWCASE_POSTGRES_ADVISORY_LOCK, _acquire_postgres_showcase_lock

    class RecordingConnection:
        def __init__(self):
            self.statement = None
            self.parameters = None

        def execute(self, statement, parameters):
            self.statement = str(statement)
            self.parameters = parameters

    connection = RecordingConnection()
    _acquire_postgres_showcase_lock(connection)
    assert "pg_advisory_xact_lock" in connection.statement
    assert connection.parameters == {"lock_id": SHOWCASE_POSTGRES_ADVISORY_LOCK}


def test_admin_can_manage_upcoming_product_cards(client, app):
    _sign_in_admin(client, app)
    form = client.get("/admin/upcoming-products/new")
    assert form.status_code == 200
    assert b'name="product_id"' not in form.data

    response = client.post("/admin/upcoming-products/new", data={
        "name": "Campus fits", "category_label": "Outfit bundles",
        "description": "A new collection announcement.",
        "card_size": "wide", "display_order": "0", "is_enabled": "1",
    }, follow_redirects=True)
    assert response.status_code == 200
    assert b"Upcoming product card added" in response.data
    assert b"Campus fits" in client.get("/upcoming").data
    assert b"upcoming-product-card--wide" in client.get("/upcoming").data
    assert b"Image coming soon" in client.get("/upcoming").data
    assert client.get("/admin/upcoming-products").status_code == 200
    with app.app_context():
        item = UpcomingProduct.query.filter_by(name="Campus fits").one()
        item_id = item.id
        assert not item.image_url
    client.post(f"/admin/upcoming-products/{item_id}/action", data={"action": "toggle"})
    assert b"Campus fits" not in client.get("/upcoming").data


def test_upcoming_product_votes_are_unique_and_ranked_in_community_panel(client, app):
    with app.app_context():
        product = UpcomingProduct(name="Trail runners", category_label="Shoes", description="A future release.")
        db.session.add(product)
        db.session.commit()
        product_id = product.id

    page = client.get("/upcoming")
    assert page.status_code == 200
    assert b"Vote for this" in page.data
    assert b"Most-voted upcoming products" in page.data

    voted_page = client.post(f"/upcoming-products/{product_id}/vote", follow_redirects=True)
    assert voted_page.status_code == 200
    assert b"Your vote for" in voted_page.data
    assert b"Trail runners" in voted_page.data
    assert b"1 vote" in voted_page.data
    with app.app_context():
        assert UpcomingProductVote.query.filter_by(upcoming_product_id=product_id).count() == 1

    duplicate_page = client.post(f"/upcoming-products/{product_id}/vote", follow_redirects=True)
    assert duplicate_page.status_code == 200
    assert b"already voted for this product" in duplicate_page.data
    with app.app_context():
        assert UpcomingProductVote.query.filter_by(upcoming_product_id=product_id).count() == 1

    second_browser = app.test_client()
    second_browser.get("/upcoming")
    second_browser.post(f"/upcoming-products/{product_id}/vote")
    with app.app_context():
        assert UpcomingProductVote.query.filter_by(upcoming_product_id=product_id).count() == 2


def test_upcoming_suggestion_returns_to_request_form(client):
    response = client.post("/mamba-suggestion", data={
        "product_name": "Canvas sneakers", "category": "Men's shoes and sneakers",
        "return_to": "upcoming",
    })
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/upcoming#upcoming-request")


def test_upcoming_suggestions_use_all_gender_categories_and_generic_confirmation(client, app):
    page = client.get("/upcoming")
    assert page.status_code == 200
    assert b"Everyone is welcome to suggest future products, regardless of gender" in page.data
    assert b'action="/upcoming-suggestion"' in page.data
    assert b"Men&#39;s shoes and sneakers" not in page.data
    client.post("/mamba-suggestion", data={
        "product_name": "Mamba boxer", "category": "Boxers",
    }, follow_redirects=True)
    response = client.post("/upcoming-suggestion", data={
        "product_name": "Everyday jacket", "category": "Clothing",
        "preferences": "Unisex fit", "return_to": "upcoming",
    }, follow_redirects=True)
    assert response.status_code == 200
    assert b"Your product request has been sent to FityStores for review" in response.data
    assert b"Mamba Collection suggestion has been saved" not in response.data
    assert b"Clothing" in response.data
    with app.app_context():
        suggestion = ProductSuggestion.query.filter_by(product_name="Everyday jacket").one()
        assert suggestion.category == "Clothing"
        assert suggestion.status == "pending"
    invalid = client.post("/upcoming-suggestion", data={
        "product_name": "Another item", "category": "Men's shoes and sneakers",
    }, follow_redirects=True)
    assert b"Choose a valid category for your all-gender product request" in invalid.data
    _sign_in_admin(client, app)
    admin_requests = client.get("/admin/suggestions")
    assert b"Upcoming page - all genders" in admin_requests.data
    assert b"Mamba Collection" in admin_requests.data


def test_upcoming_suggestion_whatsapp_copy_is_not_mamba_specific(client, monkeypatch):
    from app.public import routes as public_routes

    messages = []
    monkeypatch.setattr(public_routes, "whatsapp_text_link", lambda _settings, message: messages.append(message) or "/test-whatsapp")
    response = client.post("/upcoming-suggestion/whatsapp", data={
        "product_name": "Everyday shoes", "category": "Shoes",
        "preferences": "Any gender", "return_to": "upcoming",
    })
    assert response.status_code == 302
    assert response.headers["Location"] == "/test-whatsapp"
    assert "Upcoming page" in messages[0]
    assert "all-gender range" in messages[0]
    assert "Mamba Collection" not in messages[0]


def test_media_showcase_schedule_and_sqlite_migration_command(client, app):
    _sign_in_admin(client, app)
    client.post("/admin/media-showcases/new", data={
        "name": "Scheduled", "title": "Future feature", "page": "home", "placement": "home_after_hero",
        "layout": "single", "is_published": "1", "is_enabled": "1", "starts_at": "2099-01-01T00:00",
        "items_json": json.dumps([{"type": "image", "title": "Future", "url": "https://cdn.example.test/future.webp"}]),
    })
    assert b"Future feature" not in client.get("/").data
    result = app.test_cli_runner().invoke(args=["migrate-media-showcases"])
    assert result.exit_code == 0, result.output

def test_homepage_most_added_carousel_keeps_popularity_order_and_pagination_controls(client, app):
    with app.app_context():
        products = Product.query.order_by(Product.name).all()
        db.session.add_all([
            CartAddCount(product_id=products[0].id, add_count=3),
            CartAddCount(product_id=products[1].id, add_count=9),
        ])
        db.session.commit()

    rv = client.get("/")
    assert rv.status_code == 200
    section = rv.data[rv.data.index(b'id="most-added"'):rv.data.index(b'id="category-showcase"')]
    assert b'data-most-added' in section
    assert b'data-most-added-track' in section
    assert b'data-most-added-prev' in section
    assert b'data-most-added-next' in section
    assert b'Popular picks' in section
    assert b'Most added to cart' in section
    assert b'Discover the products shoppers love.' in section
    assert b'View all products' in section
    assert b'Manage products' not in section
    assert b'most-added-product-name' in section
    assert b'most-added-price-row' in section
    assert b'class="stock' not in section
    assert b'most-added-like' not in section
    assert b'most-added-view-detail' not in section
    assert b'data-cart-count-toggle' in section
    assert b'data-product-id=' in section
    assert b'data-compact-add' in section
    assert b'class="home-card-quick-add" href="/product/unpriced-cap"' in section
    assert b'aria-label="Add Plain Socks to cart"' in section
    assert b'Add to Cart' not in section
    assert b'<span>Choose options</span>' not in section
    assert b'href="/product/plain-socks"' in section
    assert section.index(b"Unpriced Cap") < section.index(b"Plain Socks")


def test_home_featured_cards_use_floating_quick_add_without_changing_shop_cards(client, app):
    with app.app_context():
        category = Category.query.one()
        featured_product = Product(
            name="Featured home test product",
            slug="featured-home-test-product",
            category=category,
            price_cents=5000,
            stock_quantity=2,
            active=True,
            featured=True,
            created_at=datetime(2000, 1, 1),
        )
        options_product = Product(
            name="Featured options home test product",
            slug="featured-options-home-test-product",
            category=category,
            price_cents=5000,
            stock_quantity=2,
            size_options="Small, Large",
            active=True,
            featured=True,
            created_at=datetime(2000, 1, 1),
        )
        db.session.add(featured_product)
        db.session.add(options_product)
        db.session.add_all([
            Product(
                name=f"New home test product {index}",
                slug=f"new-home-test-product-{index}",
                category=category,
                price_cents=5000,
                stock_quantity=2,
                active=True,
                featured=False,
            )
            for index in range(8)
        ])
        db.session.commit()
        product_id = featured_product.id

    home = client.get("/")
    assert home.status_code == 200
    assert b'fresh-arrival-product-card home-quick-add-card' in home.data
    assert b'class="home-card-quick-add add-cart"' in home.data
    assert b'data-compact-add' in home.data
    assert b'aria-label="Add Featured home test product to cart"' in home.data
    assert b'<a class="home-card-quick-add" href="/product/featured-options-home-test-product" aria-label="Choose options for Featured options home test product"' in home.data
    assert b"<span>Choose options</span>" not in home.data

    shop_card = _shop_product_card(client, product_id)
    assert b"Add to Cart" in shop_card
    assert b"data-compact-add" not in shop_card


def test_homepage_most_added_ranks_likes_before_cart_counts(client, app):
    with app.app_context():
        products = {product.name: product for product in Product.query.all()}
        socks = products["Plain Socks"]
        cap = products["Unpriced Cap"]
        db.session.add_all([
            CartAddCount(product_id=socks.id, add_count=20),
            CartAddCount(product_id=cap.id, add_count=1),
            ProductLike(product_id=socks.id, voter_hash="a" * 64),
            ProductLike(product_id=cap.id, voter_hash="b" * 64),
            ProductLike(product_id=cap.id, voter_hash="c" * 64),
        ])
        db.session.commit()

    response = client.get("/")
    assert response.status_code == 200
    section = response.data[response.data.index(b'id="most-added"'):response.data.index(b'id="category-showcase"')]
    assert section.index(b"Unpriced Cap") < section.index(b"Plain Socks")
    assert b"data-cart-count=\"20\"" in section
    assert b"data-cart-count-toggle" in section
    assert b"data-like-count=" in section
    assert b"data-product-like" not in section
    assert b"items added to carts" in section


def test_product_like_endpoint_toggles_one_persistent_vote(client, app):
    with app.app_context():
        product_id = Product.query.filter_by(slug="plain-socks").one().id

    first = client.post(f"/api/products/{product_id}/like", json={})
    assert first.status_code == 200
    assert first.get_json() == {"ok": True, "liked": True, "like_count": 1}
    detail = client.get("/product/plain-socks")
    assert detail.status_code == 200
    assert b'data-product-like' in detail.data
    assert b'aria-pressed="true"' in detail.data
    assert b'<span data-like-count>1</span>' in detail.data

    second = client.post(f"/api/products/{product_id}/like", json={})
    assert second.status_code == 200
    assert second.get_json() == {"ok": True, "liked": False, "like_count": 0}


def test_cart_add_endpoint_returns_updated_real_add_count(client):
    response = client.post("/api/cart/add", json={"product_id": 1, "quantity": 2})
    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "add_count": 2}

def test_about_renders_live_categories_products_and_working_destinations(client):
    rv = client.get("/about")
    assert rv.status_code == 200
    assert b"Bei Fity, Fits Poa." in rv.data
    assert b"Fashion should not have to cost a fortune" in rv.data
    assert b"making everyday fashion essentials accessible, convenient, and affordable" in rv.data
    assert b"Ankle socks" in rv.data
    assert b"Plain Socks" in rv.data
    assert b"data-share-facebook" in rv.data
    assert b'href="/shop?category=ankle-socks"' in rv.data
    assert b'href="/contact"' in rv.data
    assert b'href="#about-help"' in rv.data
    assert b"Affordable choices" in rv.data
    assert b"Honest service" in rv.data
    assert b"Suggest a product" in rv.data
    assert b"Looking for something out of stock?" in rv.data
    assert b"Helpful answers" in rv.data

def test_about_handles_an_empty_catalogue(client, app):
    with app.app_context():
        Product.query.update({Product.active: False})
        db.session.commit()
    rv = client.get("/about")
    assert rv.status_code == 200
    assert b"There are no active products to show right now" in rv.data
    assert b"From the current catalogue" in rv.data
    assert b"Plain Socks" not in rv.data

def test_about_falls_back_when_catalogue_image_is_missing(client, app):
    with app.app_context():
        product = Product.query.filter_by(slug="plain-socks").one()
        db.session.add(ProductImage(product=product, filename="missing-about-test.webp", alt_text="Missing photo", is_primary=True))
        db.session.commit()
    rv = client.get("/about")
    assert rv.status_code == 200
    assert b"Temporary product placeholder" in rv.data
    assert b"/static/images/mainLogo-160.png" in rv.data

def test_shop_search_and_category_filter(client):
    rv = client.get("/shop?q=Plain&category=ankle-socks")
    assert rv.status_code == 200
    grid_start = rv.data.index(b'<div id="product-grid"')
    grid_end = rv.data.index(b'<div id="shop-empty"', grid_start)
    product_grid = rv.data[grid_start:grid_end]
    assert b"Plain Socks" in product_grid
    assert b"Unpriced Cap" not in product_grid


def test_shop_sort_applies_to_category_sections_and_keeps_apply_control(client, app):
    with app.app_context():
        category = Category.query.filter_by(slug="ankle-socks").one()
        cheaper = Product(
            name="Budget Socks",
            slug="budget-socks",
            category=category,
            description="Lower-priced socks",
            price_cents=5000,
            stock_quantity=3,
            active=True,
        )
        db.session.add(cheaper)
        db.session.commit()
        cheaper_id = cheaper.id
        plain_id = Product.query.filter_by(slug="plain-socks").one().id

    low_response = client.get("/shop?sort=price-low")
    high_response = client.get("/shop?sort=price-high")
    assert low_response.status_code == high_response.status_code == 200
    low_html = low_response.data
    high_html = high_response.data
    assert low_html.index(f'data-product-card="{cheaper_id}"'.encode()) < low_html.index(f'data-product-card="{plain_id}"'.encode())
    assert high_html.index(f'data-product-card="{plain_id}"'.encode()) < high_html.index(f'data-product-card="{cheaper_id}"'.encode())
    assert b'<option value="price-low" selected>' in low_html
    assert b'<option value="price-high" selected>' in high_html
    assert b'<select name="category_group"' not in low_html
    assert b'<form class="shop-toolbar"' not in low_html
    assert b'<form class="shop-section-sort"' in low_html
    assert b'<button type="submit">Apply</button>' in low_html


def test_shop_sections_show_inline_product_lists_for_available_subcategories(client, app):
    with app.app_context():
        for name, slug in (
            ("Sports socks", "sports-socks"),
            ("Vests", "vests"),
            ("Boxers", "boxers"),
            ("Panties", "panties"),
        ):
            category = Category(name=name, slug=slug)
            db.session.add(category)
            db.session.flush()
            db.session.add(Product(
                name=f"{name} product",
                slug=f"{slug}-product",
                category=category,
                description=f"{name} description",
                price_cents=1000,
                stock_quantity=2,
                active=True,
            ))
        inactive_category = Category(name="Inactive socks", slug="inactive-socks")
        db.session.add(inactive_category)
        db.session.flush()
        db.session.add(Product(
            name="Inactive socks product",
            slug="inactive-socks-product",
            category=inactive_category,
            description="Not currently available",
            price_cents=1000,
            stock_quantity=0,
            active=False,
        ))
        db.session.commit()

    response = client.get("/shop")
    assert response.status_code == 200
    html = response.data
    banner_end = html.index(b"</section>")
    nav_start = html.index(b'<nav class="shop-category-chips"')
    nav = html[nav_start:html.index(b"</nav>", nav_start)]
    assert nav_start > banner_end
    from app.public.routes import SHOP_CATEGORY_GROUPS
    assert nav.count(b'<a ') == len(SHOP_CATEGORY_GROUPS) + 1
    assert b'href="/shop?category_group=socks-essentials' in nav
    assert b'href="/shop?category_group=other-products' in nav

    panel = response.data.split(b'<aside class="shop-section-category-panel"', 1)[1].split(b"</aside>", 1)[0]
    assert b"Ankle socks" in panel
    assert b"Sports socks" in panel
    assert b"Inactive socks" not in panel
    assert b'name="sort_socks-essentials"' in panel
    assert b'data-shop-category-toggle' in panel
    assert b'aria-controls="shop-subcategories-socks-essentials-sports-socks"' in panel
    assert b'href="/product/sports-socks-product"' in panel
    assert b'data-shop-category-more' not in panel
    assert b'product_count' not in panel
    assert b'shop-category-sidebar' not in response.data


def test_shop_category_panels_apply_independent_sorts(client, app):
    with app.app_context():
        socks = Category.query.filter_by(slug="ankle-socks").one()
        shoes = Category(name="Sports shoes", slug="sports-shoes")
        db.session.add(shoes)
        db.session.flush()
        products = [
            Product(name="Panel low socks", slug="panel-low-socks", category=socks, description="Low", price_cents=1, stock_quantity=1, active=True),
            Product(name="Panel high socks", slug="panel-high-socks", category=socks, description="High", price_cents=999999, stock_quantity=1, active=True),
            Product(name="Panel low shoes", slug="panel-low-shoes", category=shoes, description="Low", price_cents=1, stock_quantity=1, active=True),
            Product(name="Panel high shoes", slug="panel-high-shoes", category=shoes, description="High", price_cents=999999, stock_quantity=1, active=True),
        ]
        db.session.add_all(products)
        db.session.commit()
        product_ids = {product.slug: product.id for product in products}

    response = client.get(
        "/shop?sort_socks-essentials=price-high&sort_shoes-footwear=price-low"
    )
    assert response.status_code == 200
    html = response.data
    socks_start = html.index(b'data-category-group-key="socks-essentials" data-total-products')
    socks_end = html.index(b"</section>", socks_start)
    socks_section = html[socks_start:socks_end]
    shoes_start = html.index(b'data-category-group-key="shoes-footwear" data-total-products')
    shoes_end = html.index(b"</section>", shoes_start)
    shoes_section = html[shoes_start:shoes_end]

    assert socks_section.index(f'data-product-card="{product_ids["panel-high-socks"]}"'.encode()) < socks_section.index(f'data-product-card="{product_ids["panel-low-socks"]}"'.encode())
    assert shoes_section.index(f'data-product-card="{product_ids["panel-low-shoes"]}"'.encode()) < shoes_section.index(f'data-product-card="{product_ids["panel-high-shoes"]}"'.encode())
    assert b'<option value="price-high" selected>' in socks_section
    assert b'<option value="price-low" selected>' in shoes_section


def test_global_shop_search_shows_matches_before_the_rest_of_the_category(client, app):
    with app.app_context():
        plain_id = Product.query.filter_by(slug="plain-socks").one().id
        related_id = Product.query.filter_by(slug="unpriced-cap").one().id

    response = client.get("/shop?q=Plain")
    assert response.status_code == 200
    html = response.data
    matched_card = html.index(f'data-product-card="{plain_id}"'.encode())
    related_heading = html.index(b">You may also like</h2>")
    related_card = html.index(f'data-product-card="{related_id}"'.encode())
    assert matched_card < related_heading < related_card
    assert b"Best match" in html[matched_card:related_heading]
    assert b"Showing products from matching categories" not in html


def _shop_product_card(client, product_id):
    response = client.get("/shop")
    marker = f'data-product-card="{product_id}"'.encode()
    marker_position = response.data.index(marker)
    start = response.data.rfind(b"<article", 0, marker_position)
    end = response.data.index(b"</article>", marker_position) + len(b"</article>")
    return response.data[start:end]


def test_product_card_handles_long_copy_and_missing_image(client, app):
    with app.app_context():
        product = Product.query.filter_by(slug="plain-socks").one()
        product.name = "Everyday Performance Ankle Socks in Breathable Cotton Blend"
        product.description = "A comfortable everyday pair with a soft finish. " * 8
        product.images.append(ProductImage(filename="missing-card-test.webp", alt_text="Unavailable photo", is_primary=True))
        product_id = product.id
        db.session.commit()

    card = _shop_product_card(client, product_id)
    assert b"Everyday Performance Ankle Socks" in card
    assert b"A comfortable everyday pair" in card
    assert b'data-product-image-fallback' in card
    assert b'data-fallback-src="/static/images/mainLogo-160.png"' in card
    assert b'alt="Temporary product placeholder"' in card


def test_product_card_disables_purchase_for_sold_out_product(client, app):
    with app.app_context():
        product = Product.query.filter_by(slug="plain-socks").one()
        product.stock_quantity = 0
        product_id = product.id
        db.session.commit()

    card = _shop_product_card(client, product_id)
    assert b"Out of stock" in card
    assert b"disabled" in card
    assert b'class="btn primary add-cart"' not in card


def test_product_card_marks_sold_out_variants_and_keeps_available_options(client, app):
    with app.app_context():
        from app.models import ProductVariant
        product = Product.query.filter_by(slug="plain-socks").one()
        product.stock_quantity = 20
        product.variants.extend([
            ProductVariant(size="Small", color="Black", sku="SOCK-SOLD", price_cents=1000, stock_quantity=0, active=True),
            ProductVariant(size="Large", color="Black", sku="SOCK-IN", price_cents=2000, stock_quantity=3, active=True),
        ])
        product_id = product.id
        db.session.commit()

    card = _shop_product_card(client, product_id)
    assert b"Choose options" in card
    assert b"3 in stock across options" in card
    detail = client.get("/product/plain-socks")
    assert b"SOCK-SOLD" in detail.data
    assert b"SOCK-IN" in detail.data
    assert re.search(rb'<option(?=[^>]*data-stock="0")(?=[^>]*disabled)[^>]*>[^<]*SOCK-SOLD', detail.data)
    assert re.search(rb'<option(?=[^>]*data-stock="3")[^>]*>[^<]*SOCK-IN', detail.data)


def test_product_card_disables_action_when_every_variant_is_sold_out(client, app):
    with app.app_context():
        from app.models import ProductVariant
        product = Product.query.filter_by(slug="plain-socks").one()
        product.stock_quantity = 20
        product.variants.extend([
            ProductVariant(size="Small", color="Black", sku="SOCK-SOLD-S", price_cents=1000, stock_quantity=0, active=True),
            ProductVariant(size="Large", color="Black", sku="SOCK-SOLD-L", price_cents=2000, stock_quantity=0, active=True),
        ])
        product_id = product.id
        db.session.commit()

    card = _shop_product_card(client, product_id)
    assert b"Out of stock" in card
    assert b"Choose options" not in card
    assert b'class="btn primary add-cart"' not in card

def test_product_detail(client):
    with client.application.app_context():
        from app.models import ProductDisplayNote
        product = Product.query.filter_by(slug="plain-socks").first()
        product.description = "A full-length description that belongs in the Description tab only."
        db.session.add(ProductDisplayNote(product_id=product.id, note="Choose your preferred sock size below."))
        db.session.add_all([
            Review(display_name="Amina", title="Comfortable", rating=5, text="Soft and comfortable.", product_id=product.id, status="approved"),
            Review(display_name="Pending shopper", title="Pending", rating=1, text="This review is awaiting approval.", product_id=product.id, status="pending"),
        ])
        db.session.commit()
    rv = client.get("/product/plain-socks")
    assert rv.status_code == 200
    assert b"Plain Socks" in rv.data
    purchase_panel = rv.data.split(b'<div class="detail-copy product-purchase-panel">', 1)[1].split(b'<div class="product-service-promises"', 1)[0]
    assert b"Choose your preferred sock size below." in purchase_panel
    assert b"A full-length description that belongs in the Description tab only." not in purchase_panel
    assert b"A full-length description that belongs in the Description tab only." in rv.data.split(b'id="product-description"', 1)[1]
    assert b"KSh 150" in rv.data
    assert b"product-gallery" in rv.data
    assert b"Add to Cart" in rv.data
    assert b'data-product-quantity-step="1"' in rv.data
    assert b'id="qty-choice" type="number" min="1" max="4" value="1"' in rv.data
    assert b"Ask on WhatsApp" in rv.data
    assert b"product-enquiry-disclosure" in rv.data
    assert b"Customer reviews" in rv.data
    assert b"You may also like" in rv.data
    assert b"View category" in rv.data
    assert b'class="product-related-grid"' in rv.data
    related_section_start = rv.data.index(b'<section class="product-related-section"')
    product_sidebar_start = rv.data.index(b'<aside class="product-side-column">')
    product_sidebar_end = rv.data.index(b'</aside>', product_sidebar_start)
    assert related_section_start > product_sidebar_end
    assert b'<nav class="product-information-tabs" role="tablist" aria-label="Product information" data-product-detail-tabs>' in rv.data
    assert b'id="product-tab-details" type="button" role="tab" aria-selected="true"' in rv.data
    assert b'id="product-tab-specifications" type="button" role="tab" aria-selected="false"' in rv.data
    assert b'id="product-tab-reviews" type="button" role="tab" aria-selected="false"' in rv.data
    assert b'id="product-description" class="product-info-panel" role="tabpanel"' in rv.data
    assert b'id="product-specifications" class="product-info-section product-info-panel" role="tabpanel"' in rv.data
    assert b'id="product-reviews" class="product-info-section product-review-section product-info-panel" role="tabpanel"' in rv.data
    assert b'class="product-rating-summary"' in rv.data
    assert b'id="product-fulfilment-title">Delivery, collection and returns</h2>' in rv.data
    assert b'class="product-quick-summary" aria-label="Quick product purchase"' in rv.data
    assert b'data-product-quick-buy' in rv.data
    assert b'data-product-sticky-buy' in rv.data
    assert b'class="product-sticky-buy product-purchase-dock"' in rv.data
    assert b'Buy on WhatsApp' in rv.data
    assert b'data-product-like' in rv.data
    assert b'data-product-like-status' in rv.data
    assert b'Collect<br>in person' in rv.data
    assert b'js/product-detail-interactions.js' in rv.data
    assert b"Comfortable" in rv.data
    assert b"Soft and comfortable." in rv.data
    assert b"This review is awaiting approval." not in rv.data


def test_product_details_are_saved_sanitized_and_rendered(client, app, monkeypatch):
    app.config.update(CLOUDINARY_CLOUD_NAME="demo", CLOUDINARY_API_KEY="key", CLOUDINARY_API_SECRET="secret")
    monkeypatch.setattr("app.admin.routes.upload_image_bytes", lambda data, public_id, resource_type="image": {"public_id": public_id})
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    with app.app_context():
        category_id = Category.query.first().id
    response = client.post("/admin/products/new", data={
        "name": "Detailed Travel Bottle",
        "category_id": str(category_id),
        "description": "Reusable bottle",
        "price": "25",
        "previous_price": "30",
        "stock_quantity": "0",
        "low_stock_threshold": "1",
        "active": "on",
        "brand": "Fity Sample Brand",
        "model": "TB-01",
        "introduction": "A reusable bottle for everyday use.",
        "detailed_description_html": '<h2>Built for daily use</h2><p>Strong <b>steel</b>.</p><script>alert("x")</script><img src="https://example.invalid/tracker.png" onerror="alert(1)"><a href="javascript:alert(2)" onclick="alert(3)">Unsafe link</a>',
        "key_features": "Leak-resistant lid\nEasy to clean",
        "specifications": "Capacity | 750 ml\nMaterial | Stainless steel",
        "box_contents": "Bottle\nLid",
        "manufacturer": "Fity Manufacturing",
        "warranty_period": "12 months",
        "warranty_type": "Limited",
        "care_instructions": "Wash inside-out.",
        "additional_information": "Keep the lid closed while carrying.",
        "delivery_note": "Ask us about delivery for this product.",
        "returns_note": "Returns are subject to the store policy.",
        "seo_title": "Fity Sample Bottle",
        "seo_description": "A reusable bottle with a practical design.",
        "variant_size": ["750 ml"],
        "variant_color": ["Blue"],
        "variant_style": ["Standard"],
        "variant_material": ["Steel"],
        "variant_pack_size": ["Single"],
        "variant_sku": ["TB-01-BLUE"],
        "variant_price": ["25"],
        "variant_previous_price": ["30"],
        "variant_unit_cost": ["10"],
        "variant_stock_quantity": ["4"],
        "variant_active": ["1"],
        "variant_image_id": [""],
        "variant_description_override_html": [
            '<p>Blue option details.</p><script>steal()</script>'
        ],
        "images": (io.BytesIO(b"\xff\xd8\xffbottle-image"), "bottle.jpg"),
    }, content_type="multipart/form-data", follow_redirects=True)
    assert response.status_code == 200
    assert b"Product saved" in response.data

    with app.app_context():
        from app.models import ProductDetails, ProductVariantDetails
        product = Product.query.filter_by(slug="detailed-travel-bottle").one()
        details = ProductDetails.query.filter_by(product_id=product.id).one()
        variant = product.variants[0]
        variant_details = ProductVariantDetails.query.filter_by(variant_id=variant.id).one()
        assert details.key_features == ["Leak-resistant lid", "Easy to clean"]
        assert details.specifications == [
            {"name": "Capacity", "value": "750 ml"},
            {"name": "Material", "value": "Stainless steel"},
        ]
        assert "<script" not in details.detailed_description_html
        assert "alert" not in details.detailed_description_html
        assert "example.invalid" not in details.detailed_description_html
        assert "onerror" not in details.detailed_description_html
        assert "javascript:" not in details.detailed_description_html
        assert "onclick" not in details.detailed_description_html
        assert "steal()" not in variant_details.description_override_html
        product_id = product.id
        db.session.add(Review(
            display_name="Amina",
            title="Useful",
            rating=4,
            text="Works well.",
            product_id=product_id,
            status="approved",
        ))
        db.session.commit()

    edit_form = client.get(f"/admin/products/{product_id}/edit")
    assert edit_form.status_code == 200
    assert b'value="Fity Sample Brand"' in edit_form.data
    assert b"Blue option details." in edit_form.data

    page = client.get("/product/detailed-travel-bottle")
    assert page.status_code == 200
    assert b"Fity Sample Brand" in page.data
    assert b"Leak-resistant lid" in page.data
    assert b"Capacity" in page.data
    assert b"750 ml" in page.data
    assert b"Bottle" in page.data
    assert b"12 months" in page.data
    assert b"Wash inside-out." in page.data
    assert b"Ask us about delivery for this product." in page.data
    assert b"Blue option details." in page.data
    assert b"steal()" not in page.data
    assert b"onerror" not in page.data
    assert b"product-review-distribution" in page.data
    assert b"data-recently-viewed" in page.data

    html = page.get_data(as_text=True)
    json_ld_scripts = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    schemas = [json.loads(script) for script in json_ld_scripts]
    schema = next(item for item in schemas if item.get("@type") == "Product")
    assert schema["@type"] == "Product"
    assert schema["offers"]["priceCurrency"] == "KES"
    assert schema["aggregateRating"]["reviewCount"] == 1


def test_product_detail_still_renders_without_optional_product_details_schema(client, app):
    from app.models import ProductContentSection, ProductDetails, ProductVariantDetails
    with app.app_context():
        ProductContentSection.__table__.drop(db.engine, checkfirst=True)
        ProductVariantDetails.__table__.drop(db.engine, checkfirst=True)
        ProductDetails.__table__.drop(db.engine, checkfirst=True)
        db.session.remove()

    response = client.get("/product/plain-socks")
    assert response.status_code == 200
    assert b"Plain Socks" in response.data
    assert b"Soft socks" in response.data
    assert b"Add to Cart" in response.data


def test_product_details_migration_is_additive_and_repeatable(app):
    from sqlalchemy import inspect
    from app.models import ProductContentSection, ProductDetails, ProductVariantDetails
    with app.app_context():
        product_count = Product.query.count()
        product = Product.query.filter_by(slug="plain-socks").one()
        original_stock = product.stock_quantity
        ProductContentSection.__table__.drop(db.engine, checkfirst=True)
        ProductVariantDetails.__table__.drop(db.engine, checkfirst=True)
        ProductDetails.__table__.drop(db.engine, checkfirst=True)
        db.session.remove()

    runner = app.test_cli_runner()
    first = runner.invoke(args=["migrate-product-details-schema"])
    second = runner.invoke(args=["migrate-product-details-schema"])
    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    with app.app_context():
        ProductContentSection.__table__.drop(db.engine, checkfirst=True)
        db.session.remove()
    upgrade = runner.invoke(args=["migrate-product-details-schema"])
    assert upgrade.exit_code == 0, upgrade.output

    with app.app_context():
        assert Product.query.count() == product_count
        assert Product.query.filter_by(slug="plain-socks").one().stock_quantity == original_stock
        assert inspect(db.engine).has_table("product_details")
        assert inspect(db.engine).has_table("product_variant_details")
        assert inspect(db.engine).has_table("product_content_section")


def test_recently_viewed_api_only_returns_active_in_stock_products(client, app):
    with app.app_context():
        category = Category.query.first()
        unavailable = Product(
            name="Sold Out Item",
            slug="sold-out-item",
            category=category,
            price_cents=1000,
            stock_quantity=0,
            active=True,
        )
        inactive = Product(
            name="Hidden Item",
            slug="hidden-item",
            category=category,
            price_cents=1000,
            stock_quantity=3,
            active=False,
        )
        db.session.add_all([unavailable, inactive])
        db.session.commit()
        ids = [
            Product.query.filter_by(slug="plain-socks").one().id,
            unavailable.id,
            inactive.id,
            Product.query.filter_by(slug="unpriced-cap").one().id,
        ]

    response = client.get("/api/products/recently-viewed?ids=" + ",".join(map(str, ids)) + ",invalid")
    assert response.status_code == 200
    products = response.get_json()["products"]
    assert [product["name"] for product in products] == ["Plain Socks", "Unpriced Cap"]
    assert products[0]["price"] == "KSh 150"
    assert products[1]["price"] == "Contact for price"


def test_admin_manages_ordered_product_story_images(client, app):
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    with app.app_context():
        product = Product.query.filter_by(slug="plain-socks").one()
        image = ProductImage(
            product_id=product.id,
            filename="socks-detail.png",
            alt_text="Close view of the sock stitching",
            sort_order=0,
            is_primary=True,
        )
        db.session.add(image)
        db.session.commit()
        product_id = product.id
        image_id = image.id
        category_id = product.category_id

    response = client.post(f"/admin/products/{product_id}/edit", data={
        "name": "Plain Socks",
        "sku": "",
        "category_id": str(category_id),
        "description": "Soft socks",
        "price": "150",
        "previous_price": "180",
        "unit_cost": "",
        "stock_quantity": "4",
        "low_stock_threshold": "2",
        "size_options": "",
        "color_options": "",
        "active": "on",
        "content_section_title": ["Fine stitching", "Comfortable fit"],
        "content_section_body_text": [
            "Reinforced stitching helps the socks hold their shape.",
            "A close fit <script>alert(1)</script>",
        ],
        "content_section_image_id": [str(image_id), ""],
        "content_section_image_alt": ["Close detail of reinforced sock stitching.", ""],
        "content_section_image_caption": ["Reinforced toe and heel", ""],
        "content_section_image_position": ["right", "full"],
    }, follow_redirects=True)
    assert response.status_code == 200
    assert b"Product saved" in response.data

    with app.app_context():
        from app.models import ProductContentSection
        sections = ProductContentSection.query.filter_by(product_id=product_id).order_by(
            ProductContentSection.sort_order
        ).all()
        section = sections[0]
        assert len(sections) == 2
        assert section.title == "Fine stitching"
        assert section.sort_order == 0
        assert section.image_id == image_id
        assert section.image_alt == "Close detail of reinforced sock stitching."
        assert section.image_position == "right"
        assert sections[1].title == "Comfortable fit"
        assert sections[1].sort_order == 1

    edit_page = client.get(f"/admin/products/{product_id}/edit")
    assert b'value="Fine stitching"' in edit_page.data
    assert b"Close detail of reinforced sock stitching." in edit_page.data
    storefront = client.get("/product/plain-socks")
    assert b'class="product-story-section image-right"' in storefront.data
    assert b'class="product-story-section image-full"' in storefront.data
    assert b"Reinforced stitching helps the socks hold their shape." in storefront.data
    assert b"Reinforced toe and heel" in storefront.data
    assert b'alt="Close detail of reinforced sock stitching."' in storefront.data
    assert b'&lt;script&gt;alert(1)&lt;/script&gt;' in storefront.data
    assert b"<script>alert(1)</script>" not in storefront.data


def test_product_detail_keeps_real_variant_options_and_stock(client, app):
    with app.app_context():
        product = Product.query.filter_by(slug="plain-socks").first()
        db.session.add_all([
            ProductVariant(product_id=product.id, size="Large", color="Black", price_cents=17000, stock_quantity=3, active=True),
            ProductVariant(product_id=product.id, size="Medium", color="Black", price_cents=16000, stock_quantity=0, active=True),
            ProductVariant(product_id=product.id, size="Large", color="Pink", price_cents=18000, stock_quantity=2, active=True),
        ])
        db.session.commit()
    rv = client.get("/product/plain-socks")
    assert rv.status_code == 200
    assert b'aria-label="Choose size"' in rv.data
    assert b'data-variant-dimension="size"' in rv.data
    assert b'data-variant-dimension="color"' in rv.data
    assert b'data-variant-value="Black"' in rv.data
    assert b'data-variant-value="Pink"' in rv.data
    assert b"Large / Black" in rv.data
    assert b"3 available" in rv.data
    assert b"Medium / Black" in rv.data
    assert b"disabled" in rv.data
    assert b'data-total-stock="5"' in rv.data

def test_product_detail_recommendations_fall_back_when_category_has_no_siblings(client, app):
    with app.app_context():
        category = Category(name="Travel accessories", slug="travel-accessories")
        db.session.add(category)
        db.session.flush()
        product = Product(name="Travel pouch", slug="travel-pouch", category=category, description="A catalogue item.", price_cents=25000, stock_quantity=3, active=True)
        db.session.add(product)
        db.session.commit()
    rv = client.get("/product/travel-pouch")
    assert rv.status_code == 200
    assert b"You may also like" in rv.data
    assert b"More picks from the FityStores catalogue." in rv.data
    assert b"Plain Socks" in rv.data
    assert b"Travel accessories" in rv.data

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
    sign_in_verified_customer(client)
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
    assert b"Upcoming page studio" in rv.data
    assert b"Add upcoming product" in rv.data
    assert b"Manage upcoming cards" in rv.data

def test_review_moderation(client, app):
    client.post("/reviews", data={"display_name":"Sam","rating":"5","title":"Good service","text":"Good"})
    with app.app_context():
        review = Review.query.first()
        assert review.status == "pending"
def test_reviews_page_renders_empty_summary_and_review_form(client):
    rv = client.get("/reviews")
    assert rv.status_code == 200
    assert b"No approved reviews yet" in rv.data
    assert b"Overall rating" in rv.data
    assert b"Review title" in rv.data
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
    sign_in_verified_customer(client)
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
    assert b"Your question has been saved for the FityStores team." in rv.data
    with app.app_context():
        from app.models import Product, ProductEnquiry
        product = Product.query.filter_by(slug="plain-socks").first()
        product.stock_quantity = 0
        db.session.commit()
    rv = client.post("/product/plain-socks/restock-alert", data={"contact":"0700","preferred_contact":"WhatsApp","consent_to_contact":"on"}, follow_redirects=True)
    assert b"Thanks. Your request is saved." in rv.data
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
    sign_in_verified_customer(client)
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
    sign_in_verified_customer(client)
    with app.app_context():
        settings = ShopSettings.query.first()
        settings.loyalty_enabled = True
        settings.loyalty_points_per_100 = 10
        db.session.commit()
    client.post("/checkout", data={"cart_json":"[{\"product_id\":1,\"quantity\":1}]","customer_name":"A","customer_phone":"0700","fulfilment_method":"Collection"})
    with app.app_context():
        from app.models import Order
        order_id = Order.query.first().id
    client.post("/account/logout")
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
        "display_note": "Choose your preferred size below.",
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
        from app.models import ProductDisplayNote
        assert ProductDisplayNote.query.filter_by(product_id=product_id).one().note == "Choose your preferred size below."


def test_product_image_crop_profiles_save_and_only_apply_to_their_card_contexts(
    client, app, tmp_path, monkeypatch
):
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    (upload_dir / "ankle-socks.png").write_bytes(b"temporary test image")
    monkeypatch.setitem(app.config, "UPLOAD_FOLDER", str(upload_dir))

    with app.app_context():
        product = Product.query.filter_by(slug="plain-socks").first()
        image = ProductImage(
            product=product,
            filename="ankle-socks.png",
            alt_text="Socks",
            is_primary=True,
        )
        db.session.add(image)
        db.session.commit()
        product_id = product.id
        image_id = image.id
        category_id = product.category_id

    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    response = client.post(f"/admin/products/{product_id}/edit", data={
        "name": "Plain Socks",
        "sku": "",
        "category_id": str(category_id),
        "description": "Soft socks",
        "display_note": "",
        "price": "150",
        "previous_price": "180",
        "unit_cost": "",
        "stock_quantity": "4",
        "low_stock_threshold": "2",
        "size_options": "",
        "color_options": "",
        "variants_text": "",
        "active": "on",
        "featured": "on",
        f"image_crop_normal_{image_id}": json.dumps({
            "fit": "cover", "zoom": 150, "x": 25, "y": 35,
            "top": 8, "right": 4, "bottom": 12, "left": 6,
        }),
        f"image_crop_shop_{image_id}": json.dumps({
            "fit": "contain", "zoom": 125, "x": 70, "y": 40,
            "top": 0, "right": 10, "bottom": 5, "left": 2,
            "pan_x": 24, "pan_y": -18,
        }),
        f"image_crop_featured_{image_id}": json.dumps({
            "fit": "cover", "zoom": 140, "x": 35, "y": 65,
            "top": 4, "right": 2, "bottom": 6, "left": 3,
        }),
        f"image_crop_most_added_{image_id}": json.dumps({
            "fit": "contain", "zoom": 135, "x": 60, "y": 45,
            "top": 1, "right": 2, "bottom": 3, "left": 4,
        }),
        f"image_crop_mamba_{image_id}": json.dumps({
            "fit": "cover", "zoom": 165, "x": 25, "y": 50,
            "top": 5, "right": 4, "bottom": 3, "left": 2,
        }),
        f"image_crop_related_{image_id}": json.dumps({
            "fit": "contain", "zoom": 110, "x": 55, "y": 35,
            "top": 0, "right": 1, "bottom": 2, "left": 3,
        }),
        f"image_crop_offers_{image_id}": json.dumps({
            "fit": "cover", "zoom": 120, "x": 80, "y": 30,
            "top": 2, "right": 3, "bottom": 4, "left": 5,
        }),
    }, follow_redirects=True)
    assert response.status_code == 200
    assert b"Product saved" in response.data

    with app.app_context():
        from app.models import ProductImageCrop
        crop = ProductImageCrop.query.filter_by(image_id=image_id).one()
        assert json.loads(crop.normal_settings) == {
            "fit": "cover", "zoom": 150, "x": 25, "y": 35,
            "top": 8, "right": 4, "bottom": 12, "left": 6,
            "pan_x": 0, "pan_y": 0,
        }
        assert json.loads(crop.shop_settings) == {
            "fit": "contain", "zoom": 125, "x": 70, "y": 40,
            "top": 0, "right": 10, "bottom": 5, "left": 2,
            "pan_x": 24, "pan_y": -18,
        }
        section_settings = json.loads(crop.section_settings)
        assert section_settings["featured"] == {
            "fit": "cover", "zoom": 140, "x": 35, "y": 65,
            "top": 4, "right": 2, "bottom": 6, "left": 3,
            "pan_x": 0, "pan_y": 0,
        }
        assert section_settings["mamba"] == {
            "fit": "cover", "zoom": 165, "x": 25, "y": 50,
            "top": 5, "right": 4, "bottom": 3, "left": 2,
            "pan_x": 0, "pan_y": 0,
        }
        assert section_settings["offers"] == {
            "fit": "cover", "zoom": 120, "x": 80, "y": 30,
            "top": 2, "right": 3, "bottom": 4, "left": 5,
            "pan_x": 0, "pan_y": 0,
        }

    admin_form = client.get(f"/admin/products/{product_id}/edit")
    assert admin_form.status_code == 200
    assert b"data-crop-section-select" in admin_form.data
    assert b'<option value="shop" selected>' in admin_form.data
    assert b'value="offers"' in admin_form.data
    assert b"data-crop-preview-image" in admin_form.data
    assert b"object-position: 35% 65%" in admin_form.data
    assert b"fresh-arrival-product-card" in admin_form.data
    assert b"most-added-product-card" in admin_form.data
    assert b"offers-product-card" in admin_form.data
    assert b'data-crop-value="top"' in admin_form.data
    assert b'data-crop-value="right"' in admin_form.data
    assert b'data-crop-value="bottom"' in admin_form.data
    assert b'data-crop-value="left"' in admin_form.data
    assert b'data-crop-value="pan_x"' in admin_form.data
    assert b'data-crop-value="pan_y"' in admin_form.data
    assert b'data-fill-image-area' in admin_form.data
    assert b'data-center-image' in admin_form.data
    assert b'max="500"' in admin_form.data
    assert b"admin-product-image-crops.js" in admin_form.data
    home = client.get("/")
    about = client.get("/about")
    shop = client.get("/shop")
    shop_api = client.get("/api/products?page=1")
    assert home.status_code == 200
    assert about.status_code == 200
    assert shop.status_code == 200
    assert shop_api.status_code == 200
    assert b"object-fit: cover; object-position: 25% 35%; translate: 0% 0%; scale: 1.5; transform-origin: 25% 35%; clip-path: inset(8% 4% 12% 6%);" in about.data
    assert b"object-fit: contain; object-position: 70% 40%; translate: 24% -18%; scale: 1.25; transform-origin: 70% 40%; clip-path: inset(0% 10% 5% 2%);" in shop.data
    assert "translate: 24% -18%" in shop_api.get_json()["html"]
    assert "object-position: 70% 40%" in shop_api.get_json()["html"]
    assert "clip-path: inset(0% 10% 5% 2%)" in shop_api.get_json()["html"]
    offers = home.data.split(b'id="discounted-goods"', 1)[1].split(b"</section>", 1)[0]
    assert b"offers-product-card" in offers
    assert b"object-position: 80% 30%" in offers
    mamba_cards = home.data.split(b'id="mamba-products"', 1)[1].split(b'data-carousel-direction="1"', 1)[0]
    assert b'data-product-card="' + str(product_id).encode() + b'"' in mamba_cards
    assert b"object-position: 25% 50%" in mamba_cards


def test_product_image_crop_settings_reject_invalid_values():
    from app.services.product_image_crops import (
        DEFAULT_PRODUCT_IMAGE_CROP,
        normalize_product_image_crop,
    )

    assert normalize_product_image_crop(DEFAULT_PRODUCT_IMAGE_CROP) == DEFAULT_PRODUCT_IMAGE_CROP
    assert normalize_product_image_crop({"fit": "cover", "zoom": 100, "x": 50, "y": 50}) == {
        **DEFAULT_PRODUCT_IMAGE_CROP,
        "fit": "cover",
    }
    assert normalize_product_image_crop({
        "fit": "cover",
        "zoom": 100,
        "x": 50,
        "y": 50,
        "pan_x": 25,
        "pan_y": -25,
    }) == {
        **DEFAULT_PRODUCT_IMAGE_CROP,
        "fit": "cover",
        "pan_x": 25,
        "pan_y": -25,
    }
    assert normalize_product_image_crop({
        **DEFAULT_PRODUCT_IMAGE_CROP,
        "fit": "cover",
        "zoom": 500,
        "pan_x": -200,
        "pan_y": 200,
    }) == {
        **DEFAULT_PRODUCT_IMAGE_CROP,
        "fit": "cover",
        "zoom": 500,
        "pan_x": -200,
        "pan_y": 200,
    }
    for settings in (
        {"fit": "stretch", "zoom": 100, "x": 50, "y": 50},
        {"fit": [], "zoom": 100, "x": 50, "y": 50},
        {"fit": "cover", "zoom": 501, "x": 50, "y": 50},
        {**DEFAULT_PRODUCT_IMAGE_CROP, "zoom": 501},
        {**DEFAULT_PRODUCT_IMAGE_CROP, "pan_x": -201},
        {**DEFAULT_PRODUCT_IMAGE_CROP, "pan_y": 201},
        {"fit": "contain", "zoom": 100, "x": -1, "y": 50},
        {"fit": "contain", "zoom": 100, "x": 50, "y": True},
        {"fit": "contain", "zoom": 100, "x": 50},
        {**DEFAULT_PRODUCT_IMAGE_CROP, "left": 50},
        {**DEFAULT_PRODUCT_IMAGE_CROP, "top": 40, "bottom": 55},
    ):
        with pytest.raises(ValueError):
            normalize_product_image_crop(settings)


def test_product_image_crop_schema_migration_is_additive_and_idempotent():
    from sqlalchemy import create_engine, inspect
    from app.services.product_image_crops import ensure_product_image_crop_schema

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE product_image (id INTEGER PRIMARY KEY)")

    assert ensure_product_image_crop_schema(engine)
    assert ensure_product_image_crop_schema(engine)
    inspector = inspect(engine)
    assert inspector.has_table("product_image_crop")
    assert {column["name"] for column in inspector.get_columns("product_image_crop")} == {
        "image_id", "normal_settings", "shop_settings", "section_settings"
    }
    assert inspector.get_table_names() == ["product_image", "product_image_crop"]
    engine.dispose()


def test_product_image_crop_schema_adds_section_settings_to_existing_table():
    from sqlalchemy import create_engine, inspect
    from app.services.product_image_crops import ensure_product_image_crop_schema

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE product_image (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql(
            "CREATE TABLE product_image_crop ("
            "image_id INTEGER PRIMARY KEY, "
            "normal_settings VARCHAR(160) NOT NULL DEFAULT '', "
            "shop_settings VARCHAR(160) NOT NULL DEFAULT '')"
        )

    assert ensure_product_image_crop_schema(engine)
    assert ensure_product_image_crop_schema(engine)
    assert {column["name"] for column in inspect(engine).get_columns("product_image_crop")} == {
        "image_id", "normal_settings", "shop_settings", "section_settings"
    }
    engine.dispose()


def test_admin_product_form_has_repeatable_variant_controls(client, app):
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    response = client.get("/admin/products/new")
    assert response.status_code == 200
    assert b"Show in homepage Featured Picks" in response.data
    assert b"Popular Picks are ranked automatically" in response.data
    assert b"Add a variant" in response.data
    assert b'name="variant_color"' in response.data
    assert b'name="variant_material"' in response.data
    assert b'name="variant_pack_size"' in response.data
    assert b'name="variant_previous_price"' in response.data
    assert b'name="variant_image_files"' in response.data
    assert b"admin-product-variants.js" in response.data


def test_admin_can_save_multiple_variant_colours_and_individual_preview_images(client, app, monkeypatch):
    app.config.update(CLOUDINARY_CLOUD_NAME="demo", CLOUDINARY_API_KEY="key", CLOUDINARY_API_SECRET="secret")
    monkeypatch.setattr("app.admin.routes.upload_image_bytes", lambda data, public_id, resource_type="image": {"public_id": public_id})
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    with app.app_context():
        category_id = Category.query.first().id
    response = client.post("/admin/products/new", data={
        "name": "Variant Colour Bands", "category_id": str(category_id), "description": "Colour options",
        "price": "25", "stock_quantity": "0", "low_stock_threshold": "1", "active": "on",
        "variant_size": ["One size", "One size"], "variant_color": ["Black", "Pink"],
        "variant_style": ["Basic", "Basic"], "variant_material": ["Elastic", "Elastic"],
        "variant_pack_size": ["Single", "Single"], "variant_sku": ["BAND-BLK", "BAND-PNK"],
        "variant_price": ["25", "30"], "variant_unit_cost": ["10", "12"],
        "variant_previous_price": ["35", "45"],
        "variant_stock_quantity": ["4", "8"], "variant_active": ["1", "1"],
        "variant_image_id": ["", ""],
        "images": (io.BytesIO(b"\xff\xd8\xffmain-image"), "bands-main.jpg"),
        "variant_image_files": [(io.BytesIO(b""), ""), (io.BytesIO(b"\xff\xd8\xffpink-image"), "bands-pink.jpg")],
    }, content_type="multipart/form-data", follow_redirects=True)
    assert response.status_code == 200
    assert b"Product saved" in response.data
    with app.app_context():
        product = Product.query.filter_by(slug="variant-colour-bands").first()
        product_id = product.id
        variants = {variant.color: variant for variant in product.variants}
        assert set(variants) == {"Black", "Pink"}
        assert variants["Black"].material == "Elastic"
        assert variants["Pink"].pack_size == "Single"
        assert variants["Black"].price_cents == 2500
        assert variants["Black"].previous_price_cents == 3500
        assert variants["Pink"].previous_price_cents == 4500
        assert variants["Pink"].stock_quantity == 8
        assert variants["Black"].image_id is None
        assert variants["Pink"].image_id is not None
        assert variants["Pink"].image.alt_text == "Variant Colour Bands variant image"
    edit_form = client.get(f"/admin/products/{product_id}/edit")
    assert b'value="35.00"' in edit_form.data
    assert b'value="45.00"' in edit_form.data


def test_variant_product_shows_price_range_and_options(client, app):
    with app.app_context():
        from app.models import ProductVariant
        product = Product.query.filter_by(slug="plain-socks").first()
        db.session.add_all([
            ProductVariant(product=product, size="Small", color="Black", style="Basic", sku="SOCK-S", price_cents=1000, previous_price_cents=1500, stock_quantity=5, active=True),
            ProductVariant(product=product, size="Large", color="Black", style="Premium", sku="SOCK-L", price_cents=6000, stock_quantity=3, active=True),
        ])
        db.session.commit()
    shop = client.get("/shop?q=plain+socks")
    assert "KSh 10 – KSh 60".encode() in shop.data
    assert b"2 options available" in shop.data
    detail = client.get("/product/plain-socks")
    assert b"Small / Black / Basic" in detail.data
    assert b"Large / Black / Premium" in detail.data
    assert b"SOCK-S" in detail.data
    assert b'data-previous-price="KSh 15"' in detail.data
    assert b'data-discount="33% OFF"' in detail.data
    assert b'data-savings="Save KSh 5"' in detail.data
    assert b'id="detail-previous-price" hidden' in detail.data


def test_variant_discounts_appear_in_offers_for_available_variants(client, app):
    with app.app_context():
        category = Category.query.first()
        product = Product(
            name="Variant Sale Boots", slug="variant-sale-boots", category=category,
            description="Boots with discounted options", price_cents=None, stock_quantity=0,
            active=True, created_at=datetime(2020, 1, 1),
        )
        db.session.add(product)
        db.session.flush()
        db.session.add_all([
            ProductVariant(product=product, size="Small", price_cents=1200, previous_price_cents=1500, stock_quantity=3, active=True),
            ProductVariant(product=product, size="Large", price_cents=2000, previous_price_cents=2200, stock_quantity=2, active=True),
            ProductVariant(product=product, size="XL", price_cents=3000, previous_price_cents=3500, stock_quantity=2, active=False),
        ])
        for index in range(7):
            db.session.add(Product(
                name=f"New Arrival {index}", slug=f"new-arrival-{index}", category=category,
                description="New product", price_cents=1000, stock_quantity=1, active=True,
            ))
        db.session.commit()
        from app.public.routes import discounted_products_query
        discounted_slugs = {item.slug for item in discounted_products_query().all()}
        assert "plain-socks" in discounted_slugs
        assert "variant-sale-boots" in discounted_slugs

    home = client.get("/")
    assert home.status_code == 200
    assert b"Variant Sale Boots" in home.data
    assert b"KSh 12 - KSh 20" in home.data or b"KSh 12 \xe2\x80\x93 KSh 20" in home.data
    assert b"up to 20% off options" in home.data
    assert b"Save KSh 2 \xe2\x80\x93 KSh 3" in home.data

    detail = client.get("/product/variant-sale-boots")
    assert b'data-previous-price="KSh 15"' in detail.data
    assert b'data-discount="20% OFF"' in detail.data
    assert b'data-previous-price="KSh 22"' in detail.data
    assert b'data-discount="9% OFF"' in detail.data
    assert b'data-savings="Save KSh 2"' in detail.data


def test_variant_discount_schema_setup_adds_missing_column_idempotently():
    from sqlalchemy import create_engine, inspect

    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE product_variant (id INTEGER PRIMARY KEY)")

        assert app_module.ensure_product_variant_discount_schema(engine) is True
        assert app_module.ensure_product_variant_discount_schema(engine) is False
        assert "previous_price_cents" in {
            column["name"] for column in inspect(engine).get_columns("product_variant")
        }
    finally:
        engine.dispose()


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


def test_admin_can_delete_product_when_optional_mamba_collection_table_is_missing(client, app):
    from app.models import MambaCollectionItem

    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    with app.app_context():
        product = Product.query.filter_by(slug="plain-socks").first()
        product_id = product.id
        MambaCollectionItem.__table__.drop(db.engine)

    response = client.post(
        f"/admin/products/{product_id}/delete",
        data={"confirm": "DELETE"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Product deleted." in response.data
    with app.app_context():
        assert db.session.get(Product, product_id) is None


def test_admin_product_delete_removes_existing_mamba_collection_item(client, app):
    from app.models import MambaCollectionItem

    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    with app.app_context():
        product = Product.query.filter_by(slug="plain-socks").first()
        product_id = product.id
        item = MambaCollectionItem(product_id=product_id, sort_order=0)
        db.session.add(item)
        db.session.commit()
        item_id = item.id

    response = client.post(
        f"/admin/products/{product_id}/delete",
        data={"confirm": "DELETE"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    with app.app_context():
        assert db.session.get(Product, product_id) is None
        assert db.session.get(MambaCollectionItem, item_id) is None


def test_duplicate_product_photos_are_reused_and_gallery_is_deduplicated(client, app, monkeypatch):
    app.config.update(CLOUDINARY_CLOUD_NAME="demo", CLOUDINARY_API_KEY="key", CLOUDINARY_API_SECRET="secret")
    uploaded_ids = []
    monkeypatch.setattr(
        "app.admin.routes.upload_image_bytes",
        lambda data, public_id, resource_type="image": uploaded_ids.append(public_id),
    )
    with app.app_context():
        from app.admin.routes import append_cloudinary_image
        from app.services.product_images import image_content_hash, product_gallery_images

        product = Product.query.filter_by(slug="plain-socks").first()
        image_data = b"identical product photo"
        first = append_cloudinary_image(product, "socks.jpg", image_data, "Plain Socks image")
        repeated = append_cloudinary_image(product, "socks-copy.jpg", image_data, "Plain Socks variant image", prefix="variants")
        assert first is repeated
        assert len(uploaded_ids) == 1
        assert image_content_hash(image_data) in first.cloudinary_public_id

        product.images.extend([
            ProductImage(filename="legacy-repeat.jpg", cloudinary_public_id="legacy/one", alt_text="Repeated upload"),
            ProductImage(filename="legacy-repeat.jpg", cloudinary_public_id="legacy/two", alt_text="Repeated upload"),
            ProductImage(filename="legacy-distinct.jpg", cloudinary_public_id="legacy/three", alt_text="Distinct upload"),
        ])
        db.session.commit()
        gallery_images = product_gallery_images(product)
        assert sum(image.filename == "legacy-repeat.jpg" for image in gallery_images) == 1

    response = client.get("/product/plain-socks")
    assert response.status_code == 200
    assert response.data.count(b"data-product-thumb=") == len(gallery_images)
    assert b'data-product-gallery-step="-1"' in response.data
    assert b"data-product-image-dialog" in response.data


def test_cloudinary_url_and_legacy_fallback_helpers(app):
    from app.services.product_images import product_image_url, valid_product_image
    app.config.update(CLOUDINARY_CLOUD_NAME="demo")
    with app.test_request_context():
        cloud = ProductImage(filename="sock.jpg", cloudinary_public_id="fity/products/sock", alt_text="Sock")
        assert product_image_url(cloud) == "https://res.cloudinary.com/demo/image/upload/fity/products/sock"
        missing = ProductImage(filename="missing.jpg", alt_text="Missing")
        assert valid_product_image(missing) is False
        assert product_image_url(missing).endswith("/static/images/mainLogo-160.png")


def test_local_product_image_remains_visible_when_cloudinary_is_unconfigured(app, tmp_path):
    from app import ensure_sample_images
    from app.services.product_images import product_image_url, valid_product_image
    app.config.update(CLOUDINARY_CLOUD_NAME="", UPLOAD_FOLDER=str(tmp_path / "uploads"))
    ensure_sample_images(app)
    image = ProductImage(
        filename="sample-ankle-socks.svg", cloudinary_public_id="fity/legacy/local-sock",
        storage_backend="cloudinary", alt_text="Sock", is_primary=True,
    )
    with app.test_request_context():
        assert valid_product_image(image) is True
        assert product_image_url(image).endswith("/static/uploads/sample-ankle-socks.svg")


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
        "/delivery-policy": "Delivery and Collection",
        "/payment-policy": "Payment Policy",
        "/returns-policy": "Returns, Refunds, and Cancellations",
        "/privacy": "Privacy Policy",
        "/terms": "Terms and Conditions",
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


def test_cookie_notice_is_linked_and_acknowledgement_persists(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b'data-cookie-notice' in response.data
    assert b'data-cookie-notice-accept' in response.data
    assert b'href="/privacy"' in response.data
    assert b"essential cookies" in response.data

    policy_response = client.get("/privacy")
    assert b"remember that you dismissed the cookie notice" in policy_response.data

    client.set_cookie("fity_cookie_notice_acknowledged", "1", domain="example.test")
    dismissed_response = client.get("/")
    assert dismissed_response.status_code == 200
    assert b'data-cookie-notice' not in dismissed_response.data


def test_seo_metadata_uses_clean_production_canonical(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"<title>" in response.data
    assert b'name="description"' in response.data
    assert b'href="https://fitystore.top/"' in response.data
    assert b'property="og:url" content="https://fitystore.top/"' in response.data

    sign_in_verified_customer(client)
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
    listing = client.get("/shop?q=plain+socks")
    assert listing.status_code == 200
    assert b'href="/product/plain-socks"' in listing.data
    assert b'data-share-url=' not in listing.data
    assert b'data-share-native' not in listing.data


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

    sign_in_verified_customer(client)
    checkout = client.get("/checkout")
    assert checkout.status_code == 200
    assert b"Meet at the confirmed Othaya collection point." in checkout.data
    assert b"Nearby delivery is arranged by contacting the seller." in checkout.data
    assert b"KSh 50" in checkout.data
    assert b"Confirm availability, then use the configured manual M-Pesa payment number." in checkout.data
    assert b"254700000000" in checkout.data
    assert b"Never share your M-Pesa PIN." in checkout.data
    assert b"/returns-policy" in checkout.data

    delivery = client.get("/delivery-policy")
    assert b"KSh 50" in delivery.data
    assert b"does not publish a guaranteed delivery" in delivery.data
    payment = client.get("/payment-policy")
    assert b"does not automatically verify" in payment.data
    assert b"Do not send another payment" in payment.data
    returns = client.get("/returns-policy")
    assert b"ideally within 48 hours of receiving it" in returns.data
    assert b"not a deadline" in returns.data
    assert b"does not limit legal rights or remedies" in returns.data
    assert b"report it within the 48-hour period stated above" not in returns.data

    terms = client.get("/terms")
    assert b"records the requested quantities as reserved inventory" in terms.data
    assert b"no automatic expiry time for the hold is published" in terms.data
    assert b"does not by itself reserve stock" not in terms.data

    privacy = client.get("/privacy")
    assert b"Last updated: 27 November 2026" in privacy.data
    assert b"does not subscribe you to marketing" in privacy.data
    assert b"withdraw consent" in privacy.data

@pytest.fixture()
def app_context(app):
    with app.app_context():
        yield

def _email_code_auth(
    client, email, marketing=False, *, follow_redirects=True,
    complete_verification=True, provider="brevo", sender=None,
):
    import app.customer.routes as customer_routes
    import app.services.customer_mail as customer_mail

    app = client.application
    config_keys = (
        "MAIL_PROVIDER", "BREVO_API_KEY", "MAIL_FROM_VERIFIED",
        "MAIL_FROM_ADDRESS", "MAIL_SERVER", "MAIL_PORT", "MAIL_USE_TLS",
        "MAIL_USE_SSL",
    )
    old_config = {key: app.config.get(key) for key in config_keys}
    old_randbelow = customer_routes.randbelow
    old_mail_status = customer_mail.mail_configuration_status
    with app.app_context():
        test_code = f"{(123456 + CustomerToken.query.count()) % 1_000_000:06d}"
    sender_name = "_send_smtp" if provider == "smtp" else "_send_brevo"
    old_sender = getattr(customer_mail, sender_name)
    customer_routes.randbelow = lambda _limit: int(test_code)
    customer_mail.mail_configuration_status = lambda: (True, "configured")
    if sender is None:
        sender = lambda *_args: "test-authentication-provider-id"
    setattr(customer_mail, sender_name, sender)
    app.config.update(
        MAIL_PROVIDER=provider, BREVO_API_KEY="test-brevo-key",
        MAIL_FROM_VERIFIED=True, MAIL_FROM_ADDRESS="verified@example.test",
        MAIL_SERVER="smtp.example.test", MAIL_PORT=587,
        MAIL_USE_TLS=True, MAIL_USE_SSL=False,
    )
    try:
        response = client.post("/account/email-code", data={
            "email": email,
            "accept_terms": "yes",
            "marketing_opt_in": "yes" if marketing else "",
        })
    finally:
        customer_routes.randbelow = old_randbelow
        customer_mail.mail_configuration_status = old_mail_status
        setattr(customer_mail, sender_name, old_sender)
        app.config.update(old_config)
    if (
        complete_verification and response.status_code == 302
        and response.location.endswith("/account/verify-code")
    ):
        return client.post(
            "/account/verify-code", data={"code": test_code},
            follow_redirects=follow_redirects,
        )
    return response


def _register_customer(client, email="customer@example.com", marketing=False, **kwargs):
    response = _email_code_auth(client, email, marketing, **kwargs)
    client.post("/account/logout", follow_redirects=True)
    return response

def test_registration_requires_terms_acceptance(client, app):
    response = client.post("/account/email-code", data={
        "email": "terms@example.com",
    }, follow_redirects=True)
    assert response.status_code == 200
    assert b"Please accept the updated Terms and Conditions" in response.data
    with app.app_context():
        assert CustomerAccount.query.filter_by(email="terms@example.com").count() == 0
        assert EmailMessage.query.filter_by(kind="welcome").count() == 0


def test_email_first_signup_creates_verified_account_and_welcomes_once(client, app):
    app.config["MAIL_PROVIDER"] = ""
    response = _register_customer(client)
    assert response.status_code == 200
    assert b"Your email is verified" in response.data
    with app.app_context():
        account = CustomerAccount.query.filter_by(email="customer@example.com").one()
        assert account.password_hash is None
        assert account.email_verified_at is not None
        assert account.marketing_status == "none"
        token = CustomerToken.query.filter_by(account_id=account.id, purpose="verify_code").one()
        assert len(token.token_hash) == 64
        assert token.used_at is not None
        assert EmailMessage.query.filter_by(kind="verify_code").one().status == "sent"
        welcome = EmailMessage.query.filter_by(kind="welcome", account_id=account.id).one()
        assert welcome.status == "queued"
        assert welcome.purpose == "transactional"
        assert "Would you like to hear about our latest offers?" in welcome.text_body
        assert "Yes, I'd love to receive FityStores offers." in welcome.text_body
        invitation = CustomerToken.query.filter_by(
            account_id=account.id, purpose="welcome_opt_in",
        ).one()
        assert invitation.expires_at > datetime.utcnow()
        assert CustomerConsentEvent.query.filter_by(account_id=account.id, event="terms_accepted").count() == 1
        assert CustomerConsentEvent.query.filter_by(account_id=account.id, event="confirmed").count() == 0
        assert CustomerConsentEvent.query.filter_by(
            account_id=account.id, event="declined", method="email_code_signup",
        ).one().wording_version == "2026-11-email-first-marketing-consent-v1"


def test_email_first_signup_sends_no_welcome_before_code_verification(client, app):
    response = _email_code_auth(
        client, "pending-signup@example.com", complete_verification=False,
    )
    assert response.status_code == 302
    assert response.location.endswith("/account/verify-code")
    with app.app_context():
        account = CustomerAccount.query.filter_by(email="pending-signup@example.com").one()
        assert account.email_verified_at is None
        assert EmailMessage.query.filter_by(kind="welcome", account_id=account.id).count() == 0

    verified = client.post(
        "/account/verify-code", data={"code": "123456"}, follow_redirects=True,
    )
    assert verified.status_code == 200
    assert b"Your email is verified" in verified.data
    with app.app_context():
        account = CustomerAccount.query.filter_by(email="pending-signup@example.com").one()
        assert account.email_verified_at is not None
        assert EmailMessage.query.filter_by(kind="welcome", account_id=account.id).count() == 1


def test_email_first_signup_retry_preserves_new_customer_consent_state(client, app):
    app.config["MAIL_PROVIDER"] = ""
    unavailable = client.post("/account/email-code", data={
        "email": "signup-retry@example.com",
        "accept_terms": "yes",
    })
    assert unavailable.status_code == 302
    with app.app_context():
        account = CustomerAccount.query.filter_by(email="signup-retry@example.com").one()
        assert account.email_verified_at is None
        assert CustomerConsentEvent.query.filter_by(account_id=account.id).count() == 0

    _email_code_auth(client, "signup-retry@example.com")
    with app.app_context():
        account = CustomerAccount.query.filter_by(email="signup-retry@example.com").one()
        assert account.email_verified_at is not None
        assert CustomerConsentEvent.query.filter_by(
            account_id=account.id, event="terms_accepted", method="email_code_signup",
        ).count() == 1
        assert CustomerConsentEvent.query.filter_by(
            account_id=account.id, event="declined", method="email_code_signup",
        ).count() == 1
        assert EmailMessage.query.filter_by(kind="welcome", account_id=account.id).count() == 1


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


def test_registration_checkbox_subscribes_immediately_and_unsubscribe_history(client, app, app_context):
    from datetime import datetime, timedelta
    import hashlib
    app.config["MAIL_PROVIDER"] = ""
    response = _register_customer(client, email="offers@example.com", marketing=True)
    assert response.status_code == 200
    account = CustomerAccount.query.filter_by(email="offers@example.com").one()
    assert account.marketing_status == "subscribed"
    assert account.marketing_consent_at is not None
    assert CustomerConsentEvent.query.filter_by(account_id=account.id, event="confirmed", method="email_code_optin").count() == 1
    welcome = EmailMessage.query.filter_by(kind="welcome", account_id=account.id).one()
    assert "Congratulations" in welcome.text_body
    assert "successfully opted in" in welcome.text_body
    assert "Would you like to hear about our latest offers?" not in welcome.text_body
    assert CustomerConsentEvent.query.filter_by(
        account_id=account.id, event="declined", method="email_code_signup",
    ).count() == 0
    account.email_verified_at = datetime.utcnow()
    assert CustomerToken.query.filter_by(account_id=account.id, purpose="marketing_confirm").count() == 0
    assert CustomerToken.query.filter_by(account_id=account.id, purpose="welcome_opt_in").count() == 0
    assert EmailMessage.query.filter_by(kind="marketing_confirm", account_id=account.id).count() == 0
    invitation_token = CustomerToken(
        account_id=account.id, purpose="welcome_opt_in",
        token_hash=hashlib.sha256(b"stale-welcome-invitation").hexdigest(),
        expires_at=datetime.utcnow() + timedelta(hours=1),
    )
    db.session.add(invitation_token)
    unsubscribe_token = "marketing-unsubscribe-token"
    db.session.add(CustomerToken(account_id=account.id, purpose="unsubscribe", token_hash=hashlib.sha256(unsubscribe_token.encode()).hexdigest(), expires_at=datetime.utcnow() + timedelta(days=365)))
    db.session.commit()
    assert client.get(f"/account/unsubscribe/{unsubscribe_token}").status_code == 200
    assert client.post(f"/account/unsubscribe/{unsubscribe_token}").status_code == 200
    db.session.refresh(account)
    assert account.marketing_status == "unsubscribed"
    assert CustomerConsentEvent.query.filter_by(account_id=account.id, event="withdrawn").count() == 1
    db.session.refresh(invitation_token)
    assert invitation_token.used_at is not None
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    audience = client.get("/admin/email")
    assert b"Marketing consent:" in audience.data
    assert b"Marketing consent: <strong>unsubscribed</strong>" in audience.data
    assert b"withdrawn via unsubscribe_link" in audience.data


def test_registration_welcome_is_submitted_directly_and_recovers_after_temporary_brevo_failure(
    client, app, app_context, monkeypatch,
):
    import urllib.error
    import app.services.customer_mail as customer_mail
    from app.models import EmailDeliveryAttempt

    app.config.update(
        MAIL_PROVIDER="brevo", BREVO_API_KEY="test-brevo-key",
        MAIL_FROM_VERIFIED=True, MAIL_FROM_ADDRESS="verified@example.test",
    )
    attempts = []

    def fake_brevo(recipient, subject, html, text):
        attempts.append((subject, text))
        if subject == "Welcome to FityStores" and len(
            [item for item in attempts if item[0] == subject],
        ) == 1:
            raise urllib.error.HTTPError(
                "https://api.brevo.com/v3/smtp/email", 429,
                "rate limited", {}, None,
            )
        return "mock-brevo-message-id"

    monkeypatch.setattr(customer_mail, "mail_configuration_status", lambda: (True, "configured"))
    monkeypatch.setattr(customer_mail, "_send_brevo", fake_brevo)
    _register_customer(
        client, email="welcome-retry@example.com", sender=fake_brevo,
    )
    account = CustomerAccount.query.filter_by(email="welcome-retry@example.com").one()
    welcome = EmailMessage.query.filter_by(kind="welcome", account_id=account.id).one()

    assert welcome.status == "failed"
    assert welcome.error_category == "retryable_provider"
    assert welcome.next_attempt_at is not None
    assert welcome.html_body and welcome.text_body
    assert EmailDeliveryAttempt.query.filter_by(message_id=welcome.id).one().status == "failed"
    assert sum(subject == "Welcome to FityStores" for subject, _body in attempts) == 1

    result = customer_mail.process_transactional_email_queue(
        now=welcome.next_attempt_at + timedelta(seconds=1),
    )
    db.session.refresh(welcome)
    assert result["sent"] == 1
    assert welcome.status == "sent"
    assert welcome.provider_message_id == "mock-brevo-message-id"
    assert sum(subject == "Welcome to FityStores" for subject, _body in attempts) == 2


def test_google_registration_records_explicit_marketing_choice_and_sends_welcome(
    client, app, app_context, monkeypatch,
):
    import app.customer.routes as customer_routes
    import app.services.customer_mail as customer_mail

    app.config.update(
        GOOGLE_CLIENT_ID="test-client-id", MAIL_PROVIDER="brevo",
        BREVO_API_KEY="test-brevo-key", MAIL_FROM_VERIFIED=True,
        MAIL_FROM_ADDRESS="verified@example.test",
    )
    claims = {
        "sub": "google-welcome-sub", "email": "google-welcome@gmail.com",
        "name": "Google Welcome", "email_verified": True, "hd": "",
    }
    sent = []
    monkeypatch.setattr(customer_routes, "verify_google_credential", lambda _credential: claims)
    monkeypatch.setattr(customer_mail, "mail_configuration_status", lambda: (True, "configured"))
    monkeypatch.setattr(
        customer_mail, "_send_brevo",
        lambda recipient, subject, html, text: sent.append((subject, text)) or "google-welcome-message-id",
    )
    client.set_cookie("g_csrf_token", "google-welcome-csrf", domain="example.test")

    response = client.post("/account/google", data={
        "credential": "mocked", "g_csrf_token": "google-welcome-csrf",
        "accept_terms": "yes", "marketing_opt_in": "yes",
    }, follow_redirects=True)
    account = CustomerAccount.query.filter_by(google_sub="google-welcome-sub").one()
    welcome = EmailMessage.query.filter_by(kind="welcome", account_id=account.id).one()
    consent = CustomerConsentEvent.query.filter_by(
        account_id=account.id, event="confirmed", method="google_checkbox",
    ).one()

    assert response.status_code == 200
    assert account.marketing_status == "subscribed"
    assert account.marketing_consent_at == consent.created_at
    assert welcome.status == "sent"
    welcome_body = next(
        body for subject, body in sent if subject == "Welcome to FityStores"
    )
    assert "Congratulations" in welcome_body
    assert CustomerToken.query.filter_by(
        account_id=account.id, purpose="welcome_opt_in",
    ).count() == 0
    assert EmailMessage.query.filter_by(kind="marketing_confirm", account_id=account.id).count() == 0
    assert sum(subject == "Welcome to FityStores" for subject, _body in sent) == 1


def test_welcome_invitation_requires_explicit_post_and_is_single_use(client, app, app_context):
    app.config["MAIL_PROVIDER"] = ""
    account = CustomerAccount(email="welcome-invitation@example.com", display_name="")
    db.session.add(account)
    db.session.commit()
    import app.customer.routes as customer_routes
    customer_routes._send_registration_welcome_email(account)
    welcome = EmailMessage.query.filter_by(kind="welcome", account_id=account.id).one()
    match = re.search(
        r"https://fitystore\.top/account/marketing/confirm/([A-Za-z0-9_-]+)",
        welcome.text_body,
    )
    assert match
    token = match.group(1)

    page = client.get(f"/account/marketing/confirm/{token}")
    assert page.status_code == 200
    assert b"Opening this page does not subscribe you" in page.data
    assert b"Yes, I'd love to receive FityStores offers." in page.data
    db.session.refresh(account)
    assert account.marketing_status == "none"
    assert CustomerConsentEvent.query.filter_by(account_id=account.id, event="confirmed").count() == 0

    unverified_confirmation = client.post(
        f"/account/marketing/confirm/{token}", follow_redirects=True,
    )
    assert b"Verify your account email before confirming promotional email" in unverified_confirmation.data
    db.session.refresh(account)
    assert account.marketing_status == "none"
    assert CustomerConsentEvent.query.filter_by(account_id=account.id, event="confirmed").count() == 0

    account.email_verified_at = datetime.utcnow()
    db.session.commit()
    confirmation = client.post(f"/account/marketing/confirm/{token}")
    assert confirmation.status_code == 302
    db.session.refresh(account)
    consent = CustomerConsentEvent.query.filter_by(
        account_id=account.id, event="confirmed", method="welcome_invitation",
    ).one()
    assert account.marketing_status == "subscribed"
    assert account.marketing_consent_at == consent.created_at
    assert consent.wording_version == "2026-11-welcome-invitation-email-marketing-v1"
    assert CustomerToken.query.filter_by(
        account_id=account.id, purpose="welcome_opt_in",
    ).one().used_at is not None
    assert EmailMessage.query.filter_by(kind="marketing_confirm", account_id=account.id).count() == 0

    client.post(f"/account/marketing/confirm/{token}")
    assert CustomerConsentEvent.query.filter_by(
        account_id=account.id, event="confirmed", method="welcome_invitation",
    ).count() == 1


def test_login_does_not_send_another_welcome_email(client, app, monkeypatch, app_context):
    import app.services.customer_mail as customer_mail

    app.config.update(
        MAIL_PROVIDER="smtp", MAIL_FROM_VERIFIED=True,
        MAIL_FROM_ADDRESS="verified@example.test", MAIL_SERVER="smtp.example.test",
        MAIL_USE_TLS=True, MAIL_USE_SSL=False,
    )
    sends = []
    monkeypatch.setattr(
        customer_mail, "_send_smtp",
        lambda recipient, subject, html, text: sends.append((recipient, subject, text)),
    )
    _register_customer(client, email="welcome-once@example.com")
    account = CustomerAccount.query.filter_by(email="welcome-once@example.com").one()
    welcome = EmailMessage.query.filter_by(kind="welcome", account_id=account.id).one()
    assert welcome.status == "sent"
    _email_code_auth(client, "welcome-once@example.com")
    assert EmailMessage.query.filter_by(kind="welcome", account_id=account.id).count() == 1
    assert len([item for item in sends if item[1] == "Welcome to FityStores"]) == 1


def test_expired_welcome_invitation_cannot_activate_marketing(client, app, app_context):
    app.config["MAIL_PROVIDER"] = ""
    _register_customer(client, email="expired-welcome-invitation@example.com")
    account = CustomerAccount.query.filter_by(email="expired-welcome-invitation@example.com").one()
    welcome = EmailMessage.query.filter_by(kind="welcome", account_id=account.id).one()
    match = re.search(
        r"https://fitystore\.top/account/marketing/confirm/([A-Za-z0-9_-]+)",
        welcome.text_body,
    )
    assert match
    invitation = CustomerToken.query.filter_by(
        account_id=account.id, purpose="welcome_opt_in",
    ).one()
    invitation.expires_at = datetime.utcnow() - timedelta(seconds=1)
    db.session.commit()

    page = client.get(f"/account/marketing/confirm/{match.group(1)}")
    assert b"invalid, expired, or already used" in page.data
    client.post(f"/account/marketing/confirm/{match.group(1)}")
    db.session.refresh(account)
    assert account.marketing_status == "none"
    assert CustomerConsentEvent.query.filter_by(account_id=account.id, event="confirmed").count() == 0


def test_admin_email_center_excludes_unverified_opt_ins_from_marketing_candidates(client, app, app_context):
    opted_in = CustomerAccount(
        email="checkbox-opt-in@example.test", display_name="Checkbox Subscriber",
        password_hash=generate_password_hash("a-strong-test-password-123"),
        marketing_status="subscribed", marketing_consent_at=datetime.utcnow(),
    )
    verified = CustomerAccount(
        email="verified-marketing@example.test", display_name="Verified Subscriber",
        email_verified_at=datetime.utcnow(), marketing_status="subscribed",
        marketing_consent_at=datetime.utcnow(),
    )
    db.session.add_all([opted_in, verified])
    db.session.commit()
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})

    response = client.get("/admin/email")

    assert response.status_code == 200
    assert b"Checkbox Subscriber" in response.data
    assert b"Eligible verified subscribers</span><strong>1</strong>" in response.data
    assert b"admin-email.css?v=admin-email-responsive-20261119" in response.data
    assert b'class="admin-email-overview"' in response.data
    assert b'class="form-card admin-email-section"' in response.data


def test_admin_customer_removal_anonymizes_identity_and_retains_delivery_audit(client, app, app_context, monkeypatch, tmp_path):
    from app.models import (
        CustomerProfile, CustomerOrderLink, CustomerToken, EmailDeliveryAttempt,
        FinancialAuditEvent, ProductEnquiry, ReferralClick, RestockAlert,
    )
    import app.services.customer_mail as customer_mail

    sent = []
    monkeypatch.setattr(customer_mail, "_send_brevo", lambda *args, **kwargs: sent.append(args))
    admin = AdminUser.query.filter_by(username="owner").one()
    account = CustomerAccount(
        email="remove-customer@example.test",
        display_name="Remove Me",
        password_hash=generate_password_hash("test-password-123"),
        email_verified_at=datetime.utcnow(),
        marketing_status="subscribed",
        marketing_consent_at=datetime.utcnow(),
    )
    db.session.add(account)
    db.session.flush()
    order = Order(
        reference="REMOVE001",
        customer_name="Remove Me",
        customer_phone="254700000001",
        fulfilment_method="collection",
        instructions="Call Remove Me",
        notes="Contact remove-customer@example.test",
        payment_proof_filename="remove-proof.png",
    )
    app.config["PAYMENT_PROOF_FOLDER"] = str(tmp_path)
    proof_path = tmp_path / "remove-proof.png"
    proof_path.write_bytes(b"test proof")
    db.session.add(order)
    db.session.flush()
    message = EmailMessage(
        recipient=account.email,
        account_id=account.id,
        order_id=order.id,
        kind="order_status",
        subject="Hello Remove Me",
        idempotency_key="remove-customer-email-queued",
        status="queued",
        html_body="<p>Remove Me at remove-customer@example.test</p>",
        text_body="Remove Me at remove-customer@example.test",
    )
    legacy_message = EmailMessage(
        recipient=account.email,
        kind="verification",
        subject="Verify remove-customer@example.test",
        idempotency_key="remove-customer-email-legacy",
        status="queued",
        html_body="<p>Verify remove-customer@example.test</p>",
        text_body="Verify remove-customer@example.test",
    )
    ambiguous_message = EmailMessage(
        recipient=account.email,
        account_id=account.id,
        kind="verification",
        subject="Ambiguous verification",
        idempotency_key="remove-customer-email-ambiguous",
        status="failed",
        error_category="ambiguous_outcome",
        html_body="<p>Ambiguous</p>",
        text_body="Ambiguous",
    )
    enquiry = ProductEnquiry(
        product_name="Test product",
        contact=account.email,
        message=f"Please contact Remove Me at {account.email}",
        internal_response=f"Reply to {account.email}",
        consent_to_contact=True,
    )
    restock_alert = RestockAlert(
        product_id=Product.query.first().id,
        contact=account.email,
        preferred_contact="Email",
        consent_to_contact=True,
        status="queued",
    )
    profile = CustomerProfile(
        account_id=account.id,
        phone="254700000001",
        display_name="Remove Me",
        referral_code="REMOVEME01",
    )
    db.session.add_all([
        CustomerToken(
            account_id=account.id,
            purpose="verify_email",
            token_hash=hashlib.sha256(b"remove-token").hexdigest(),
            expires_at=datetime.utcnow(),
        ),
        CustomerCart(account_id=account.id, items_json='[{"id": 1}]'),
        CustomerOrderLink(account_id=account.id, order=order),
        message,
        legacy_message,
        ambiguous_message,
        enquiry,
        restock_alert,
        profile,
        Review(
            customer_account_id=account.id,
            display_name="Remove Me",
            rating=5,
            text="Remove Me shared remove-customer@example.test",
        ),
    ])
    db.session.flush()
    db.session.add_all([
        ReferralClick(
            referrer_id=profile.id,
            referral_code=profile.referral_code,
            visitor_key="anonymous-visitor-hash",
        ),
        EmailDraft(
            name="Customer follow-up",
            category="customer_service",
            subject="Hello Remove Me",
            headline="Remove Me",
            message="Email remove-customer@example.test",
            account_ids_json=json.dumps([account.id]),
            created_by_id=admin.id,
        ),
        EmailDeliveryAttempt(
            message_id=message.id,
            attempt_number=1,
            attempt_key="remove-customer-attempt-key",
            provider="brevo",
            status="accepted",
        ),
    ])
    db.session.commit()
    account_id = account.id
    message_id = message.id
    legacy_message_id = legacy_message.id
    ambiguous_message_id = ambiguous_message.id
    enquiry_id = enquiry.id
    restock_alert_id = restock_alert.id

    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    response = client.post(
        f"/admin/email/customers/{account_id}/remove",
        data={
            "confirmation": f"DELETE CUSTOMER {account_id}",
            "delete_payment_proofs": "yes",
        },
        follow_redirects=True,
    )

    db.session.expire_all()
    removed = db.session.get(CustomerAccount, account_id)
    queued = db.session.get(EmailMessage, message_id)
    legacy_queued = db.session.get(EmailMessage, legacy_message_id)
    ambiguous = db.session.get(EmailMessage, ambiguous_message_id)
    redacted_enquiry = db.session.get(ProductEnquiry, enquiry_id)
    cancelled_alert = db.session.get(RestockAlert, restock_alert_id)
    retained_order = Order.query.filter_by(reference="REMOVE001").one()
    review = Review.query.one()
    draft = EmailDraft.query.one()
    profile = CustomerProfile.query.filter_by(account_id=account_id).one()
    assert response.status_code == 200
    assert b"anonymous account record, linked orders, and financial records remain" in response.data
    assert removed.email == f"removed-customer-{account_id}@redacted.invalid"
    assert removed.active is False
    assert removed.password_hash is None
    assert CustomerToken.query.filter_by(account_id=account_id).count() == 0
    assert CustomerCart.query.filter_by(account_id=account_id).count() == 0
    order_link = CustomerOrderLink.query.filter_by(account_id=account_id).one()
    assert order_link.order_id == retained_order.id
    assert queued.status == "suppressed"
    assert queued.recipient == removed.email
    assert queued.html_body is None and queued.text_body is None
    assert legacy_queued.status == "suppressed"
    assert legacy_queued.recipient == removed.email
    assert ambiguous.status == "failed"
    assert ambiguous.error_category == "ambiguous_outcome"
    assert ambiguous.recipient == removed.email
    assert redacted_enquiry.contact == removed.email
    assert account.email not in redacted_enquiry.message
    assert account.email not in redacted_enquiry.internal_response
    assert redacted_enquiry.consent_to_contact is False
    assert cancelled_alert.contact == removed.email
    assert cancelled_alert.status == "cancelled"
    assert cancelled_alert.consent_to_contact is False
    assert EmailDeliveryAttempt.query.filter_by(message_id=message_id).count() == 1
    assert retained_order.customer_name == "Deleted customer"
    assert retained_order.customer_phone == ""
    assert retained_order.instructions == ""
    assert retained_order.payment_proof_filename == ""
    assert not proof_path.exists()
    assert "remove-customer@example.test" not in retained_order.notes
    assert review.customer_account_id is None
    assert review.display_name == "Deleted customer"
    assert "Remove Me" not in review.text
    assert "remove-customer@example.test" not in review.text
    assert profile.phone.startswith("REMOVED-CUSTOMER-")
    assert ReferralClick.query.filter_by(referrer_id=profile.id).count() == 1
    assert json.loads(draft.account_ids_json) == []
    assert "remove-customer@example.test" not in draft.message
    assert FinancialAuditEvent.query.filter_by(
        event_type="customer_account_anonymized", entity_id=account_id
    ).count() == 1
    assert sent == []

    repeated = client.post(
        f"/admin/email/customers/{account_id}/remove",
        data={"confirmation": f"DELETE CUSTOMER {account_id}"},
        follow_redirects=True,
    )
    assert b"already been removed" in repeated.data
    assert Order.query.filter_by(id=retained_order.id).count() == 1
    assert CustomerOrderLink.query.filter_by(account_id=account_id).count() == 1
    assert FinancialAuditEvent.query.filter_by(
        event_type="customer_account_anonymized", entity_id=account_id
    ).count() == 1

    from types import SimpleNamespace
    stale_alert = SimpleNamespace(
        id=restock_alert_id, status="queued", consent_to_contact=True,
        preferred_contact="Email", contact="remove-customer@example.test",
    )
    assert customer_mail.notify_restock_alert(stale_alert) is None
    assert EmailMessage.query.filter_by(
        idempotency_key=f"restock-alert:{restock_alert_id}"
    ).count() == 0

    late_enqueue = customer_mail.queue_email(
        "remove-customer@example.test", "Customer message", "order_status",
        "remove-customer-late-enqueue", account_id=account_id,
        html_body="<p>Remove Me</p>", text_body="Remove Me",
    )
    assert late_enqueue.status == "suppressed"
    assert late_enqueue.recipient == removed.email
    assert late_enqueue.html_body is None and late_enqueue.text_body is None

    stale_message = EmailMessage(
        recipient="remove-customer@example.test",
        account_id=account_id,
        kind="order_status",
        purpose="transactional",
        subject="Stale transactional message",
        idempotency_key="remove-customer-stale-transactional",
        status="queued",
        html_body="<p>Remove Me</p>",
        text_body="Remove Me",
    )
    db.session.add(stale_message)
    db.session.commit()
    app.config.update(
        MAIL_PROVIDER="brevo",
        MAIL_FROM_VERIFIED=True,
        MAIL_FROM_ADDRESS="orders@example.test",
        BREVO_API_KEY="test-brevo-key",
    )
    assert customer_mail.deliver_email(stale_message) is False
    assert stale_message.status == "suppressed"
    assert stale_message.recipient == removed.email
    assert stale_message.html_body is None and stale_message.text_body is None
    assert sent == []

    with client.session_transaction() as browser_session:
        browser_session["_user_id"] = f"customer:{account_id}"
        browser_session["_fresh"] = True
    denied = client.get("/account/")
    assert denied.status_code == 302
    assert "/account/login" in denied.headers["Location"]


def test_admin_customer_removal_preserves_multiple_orders_and_financial_records(client, app, app_context):
    from app.models import (
        CustomerConsentEvent, CustomerProfile, EmailDraft, MarketingProductExposure,
        MarketingSuppressionCase, MarketingSuppressionEvent, PaymentAttempt,
        PaymentTransaction, Referral, ReferralClick, RefundRequest, LoyaltyTransaction,
        ProductEnquiry, RestockAlert,
    )

    admin = AdminUser.query.filter_by(username="owner").one()
    account = CustomerAccount(email="remove-selected@example.test", display_name="Selected Customer")
    db.session.add(account)
    db.session.flush()
    profile = CustomerProfile(
        account_id=account.id,
        phone="254700000002",
        display_name="Selected Customer",
        referral_code="REMOVEME02",
    )
    db.session.add(profile)
    order = Order(
        reference="REMOVE002",
        customer_name="Selected Customer",
        customer_phone="254700000002",
        fulfilment_method="collection",
        total_cents=1000,
        payment_status="Paid",
    )
    second_order = Order(
        reference="REMOVE003",
        customer_name="Selected Customer",
        customer_phone="254700000002",
        fulfilment_method="delivery",
        total_cents=2000,
        payment_status="Paid",
    )
    db.session.add_all([order, second_order])
    db.session.flush()
    attempts = [
        PaymentAttempt(order_id=linked_order.id, customer_phone="254700000002", raw_payload="phone 254700000002")
        for linked_order in (order, second_order)
    ]
    db.session.add_all(attempts)
    db.session.flush()
    transactions = [
        PaymentTransaction(
            order_id=linked_order.id,
            payment_attempt_id=attempt.id,
            amount_cents=1000 * (index + 1),
            phone="254700000002",
            raw_payload="payer 254700000002",
        )
        for index, (linked_order, attempt) in enumerate(zip((order, second_order), attempts))
    ]
    db.session.add_all(transactions)
    db.session.flush()
    db.session.add_all([
        OrderItem(order_id=order.id, product_name="Historical item one", quantity=1, price_cents=1000),
        OrderItem(order_id=second_order.id, product_name="Historical item two", quantity=1, price_cents=2000),
        OrderStatusHistory(order_id=order.id, field="note", old_value="", new_value="Created", note="Selected Customer contact 254700000002"),
        OrderStatusHistory(order_id=second_order.id, field="note", old_value="", new_value="Created", note="Selected Customer contact 254700000002"),
        ProductEnquiry(
            product_name="Test product",
            contact=account.email,
            message="Remove selected contact activity",
            consent_to_contact=True,
        ),
        RestockAlert(
            product_id=Product.query.first().id,
            contact=account.email,
            preferred_contact="Email",
            consent_to_contact=True,
            status="pending",
        ),
        RefundRequest(
            order_id=order.id,
            payment_transaction_id=transactions[0].id,
            amount_cents=100,
            reason="Customer Selected Customer requested refund",
        ),
        RefundRequest(
            order_id=second_order.id,
            payment_transaction_id=transactions[1].id,
            amount_cents=200,
            reason="Customer Selected Customer requested refund",
        ),
        CustomerOrderLink(account_id=account.id, order_id=order.id),
        CustomerOrderLink(account_id=account.id, order_id=second_order.id),
        Referral(
            referrer_key="254700000002",
            referred_key="254700000003",
            referral_code="REF-HISTORY-002",
            qualifying_order_id=order.id,
        ),
        LoyaltyTransaction(
            customer_key="254700000002",
            order_id=order.id,
            transaction_type="earn",
            points=10,
        ),
        CustomerConsentEvent(
            account_id=account.id,
            event="confirmed",
            method="registration_checkbox",
        ),
        MarketingProductExposure(
            account_id=account.id,
            product_id=Product.query.first().id,
            last_sent_at=datetime.utcnow(),
        ),
        EmailDraft(
            name="Selected customer draft",
            category="customer_service",
            subject="For Selected Customer",
            headline="Selected Customer",
            message="Selected customer email",
            account_ids_json=json.dumps([account.id]),
            created_by_id=admin.id,
        ),
        Review(
            customer_account_id=account.id,
            display_name="Selected Customer",
            rating=4,
            text="Remove this review",
        ),
    ])
    db.session.flush()
    case = MarketingSuppressionCase(
        account_id=account.id,
        source="admin",
        reason_code="admin_review",
        clearance_reason=None,
        metadata_json={"email": account.email},
    )
    db.session.add(case)
    db.session.flush()
    db.session.add(MarketingSuppressionEvent(
        suppression_case_id=case.id,
        account_id=account.id,
        event_type="suppression_detected",
        actor_type="admin",
        admin_id=admin.id,
        source="admin",
        reason_code="admin_review",
        reason_text=f"Review {account.email}",
        idempotency_key="remove-selected-suppression",
        metadata_json={"email": account.email},
    ))
    db.session.commit()
    account_id = account.id
    order_ids = [order.id, second_order.id]

    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    response = client.post(
        f"/admin/email/customers/{account_id}/remove",
        data={
            "confirmation": f"DELETE CUSTOMER {account_id}",
            "delete_orders": "yes",
            "delete_reviews": "yes",
            "delete_referral_history": "yes",
            "delete_product_exposure": "yes",
            "delete_email_drafts": "yes",
            "delete_contact_activity": "yes",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    retained_orders = Order.query.filter(Order.id.in_(order_ids)).order_by(Order.id).all()
    assert len(retained_orders) == 2
    assert [row.total_cents for row in retained_orders] == [1000, 2000]
    assert [row.payment_status for row in retained_orders] == ["Paid", "Paid"]
    assert all(row.customer_name == "Deleted customer" for row in retained_orders)
    assert all(row.customer_phone == "" for row in retained_orders)
    assert CustomerOrderLink.query.filter_by(account_id=account_id).count() == 2
    assert OrderItem.query.filter(OrderItem.order_id.in_(order_ids)).count() == 2
    assert OrderStatusHistory.query.filter(OrderStatusHistory.order_id.in_(order_ids)).count() == 2
    assert all(
        "Selected Customer" not in row.note and "254700000002" not in row.note
        for row in OrderStatusHistory.query.filter(OrderStatusHistory.order_id.in_(order_ids)).all()
    )
    assert PaymentAttempt.query.filter(PaymentAttempt.order_id.in_(order_ids)).count() == 2
    assert PaymentTransaction.query.filter(PaymentTransaction.order_id.in_(order_ids)).count() == 2
    assert RefundRequest.query.filter(RefundRequest.order_id.in_(order_ids)).count() == 2
    assert all(not row.customer_phone and not row.raw_payload for row in PaymentAttempt.query.filter(PaymentAttempt.order_id.in_(order_ids)).all())
    assert all(not row.phone and not row.raw_payload for row in PaymentTransaction.query.filter(PaymentTransaction.order_id.in_(order_ids)).all())
    assert all("Selected Customer" not in row.reason for row in RefundRequest.query.filter(RefundRequest.order_id.in_(order_ids)).all())
    assert Review.query.filter_by(customer_account_id=account_id).count() == 0
    assert db.session.get(CustomerAccount, account_id).active is False
    assert CustomerProfile.query.filter_by(account_id=account_id).count() == 0
    assert ReferralClick.query.count() == 0
    assert Referral.query.filter_by(referral_code="REF-HISTORY-002").count() == 0
    assert LoyaltyTransaction.query.filter_by(customer_key="254700000002").count() == 0
    assert CustomerConsentEvent.query.filter_by(account_id=account_id).count() == 1
    suppression_event = MarketingSuppressionEvent.query.filter_by(account_id=account_id).one()
    suppression_case = MarketingSuppressionCase.query.filter_by(account_id=account_id).one()
    assert suppression_event.reason_text is None
    assert suppression_event.metadata_json is None
    assert suppression_case.metadata_json is None
    assert MarketingProductExposure.query.filter_by(account_id=account_id).count() == 0
    assert EmailDraft.query.count() == 0
    assert ProductEnquiry.query.filter_by(contact="remove-selected@example.test").count() == 0
    assert RestockAlert.query.filter_by(contact="remove-selected@example.test").count() == 0


def test_admin_customer_removal_rolls_back_anonymization_and_preserves_proof_on_commit_error(
    client, app, app_context, monkeypatch, tmp_path,
):
    from app.models import FinancialAuditEvent
    from sqlalchemy.exc import IntegrityError
    import app.services.customer_account_cleanup as cleanup

    account = CustomerAccount(email="rollback-remove@example.test", display_name="Rollback Customer")
    db.session.add(account)
    db.session.flush()
    order = Order(
        reference="ROLLBACK01",
        customer_name="Rollback Customer",
        customer_phone="254700000009",
        fulfilment_method="collection",
        payment_proof_filename="rollback-proof.png",
    )
    db.session.add(order)
    db.session.flush()
    db.session.add(CustomerOrderLink(account_id=account.id, order_id=order.id))
    db.session.commit()
    account_id = account.id
    order_id = order.id
    app.config["PAYMENT_PROOF_FOLDER"] = str(tmp_path)
    proof_path = tmp_path / "rollback-proof.png"
    proof_path.write_bytes(b"payment proof")

    original_anonymize = cleanup.anonymize_customer_account

    def fail_after_database_updates(*args, **kwargs):
        original_anonymize(*args, **kwargs)
        raise IntegrityError("forced commit failure", {}, RuntimeError("test failure"))

    monkeypatch.setattr(cleanup, "anonymize_customer_account", fail_after_database_updates)
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    response = client.post(
        f"/admin/email/customers/{account_id}/remove",
        data={
            "confirmation": f"DELETE CUSTOMER {account_id}",
            "delete_payment_proofs": "yes",
        },
        follow_redirects=True,
    )

    db.session.expire_all()
    assert response.status_code == 200
    assert b"failed and was rolled back" in response.data
    assert db.session.get(CustomerAccount, account_id).email == "rollback-remove@example.test"
    assert db.session.get(CustomerAccount, account_id).active is True
    assert db.session.get(Order, order_id).customer_name == "Rollback Customer"
    assert CustomerOrderLink.query.filter_by(account_id=account_id, order_id=order_id).count() == 1
    assert FinancialAuditEvent.query.filter_by(
        event_type="customer_account_anonymized", entity_id=account_id
    ).count() == 0
    assert proof_path.exists()


def test_admin_customer_removal_requires_confirmation_and_waits_for_dispatch(client, app, app_context):
    account = CustomerAccount(email="remove-in-flight@example.test", display_name="In Flight")
    db.session.add(account)
    db.session.flush()
    message = EmailMessage(
        recipient=account.email,
        account_id=account.id,
        kind="order_status",
        subject="In flight",
        idempotency_key="remove-customer-in-flight",
        status="sending",
        claim_token="in-flight-claim",
        claimed_at=datetime.utcnow(),
    )
    db.session.add(message)
    db.session.commit()
    account_id = account.id

    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    wrong_confirmation = client.post(
        f"/admin/email/customers/{account_id}/remove",
        data={"confirmation": "DELETE"},
        follow_redirects=True,
    )
    db.session.expire_all()
    assert b"Type DELETE CUSTOMER" in wrong_confirmation.data
    assert db.session.get(CustomerAccount, account_id).active is True

    blocked = client.post(
        f"/admin/email/customers/{account_id}/remove",
        data={"confirmation": f"DELETE CUSTOMER {account_id}"},
        follow_redirects=True,
    )
    db.session.expire_all()
    assert b"Wait for the provider attempt to finish" in blocked.data
    assert db.session.get(CustomerAccount, account_id).email == "remove-in-flight@example.test"
    assert EmailMessage.query.filter_by(account_id=account_id).one().status == "sending"


def test_admin_customer_permanent_deletion_removes_linked_rows_and_payment_proof(
    client, app, app_context, tmp_path,
):
    from app.models import (
        CustomerProfile, ReferralClick, Referral, LoyaltyTransaction,
        PaymentAttempt, PaymentTransaction, RefundRequest,
        EmailDeliveryAttempt, EmailDeliveryInvestigation,
        EmailDeliveryInvestigationEvent, EmailLegacyPayloadReviewEvent,
        EmailCampaignRecipient, CustomerConsentEvent,
        MarketingProductExposure, MarketingSuppressionCase,
        MarketingSuppressionEvent, FinancialAuditEvent, ProductEnquiry,
        ProductSuggestion, RestockAlert,
    )

    admin = AdminUser.query.filter_by(username="owner").one()
    account = CustomerAccount(
        email="purge-customer@example.test",
        display_name="Purge Customer",
    )
    db.session.add(account)
    db.session.flush()
    profile = CustomerProfile(
        account_id=account.id,
        phone="254700000077",
        display_name=account.display_name,
        referral_code="PURGEME077",
    )
    cart = CustomerCart(account_id=account.id, items_json='[{"id": 1}]')
    order = Order(
        reference="PURGE001",
        customer_name=account.display_name,
        customer_phone=profile.phone,
        fulfilment_method="collection",
        payment_proof_filename="purge-proof.png",
    )
    campaign = EmailCampaign(
        name="Shared campaign",
        subject="Shared subject",
        body="Shared campaign body",
        created_by_id=admin.id,
    )
    db.session.add_all([profile, cart, order, campaign])
    db.session.flush()
    app.config["PAYMENT_PROOF_FOLDER"] = str(tmp_path)
    proof_path = tmp_path / "purge-proof.png"
    proof_path.write_bytes(b"private payment proof")
    payment_attempt = PaymentAttempt(
        order_id=order.id,
        customer_phone=profile.phone,
        raw_payload="payer 254700000077",
    )
    db.session.add(payment_attempt)
    db.session.flush()
    transaction = PaymentTransaction(
        order_id=order.id,
        payment_attempt_id=payment_attempt.id,
        amount_cents=1000,
        phone=profile.phone,
        raw_payload="payment for Purge Customer",
    )
    db.session.add(transaction)
    db.session.flush()
    message = EmailMessage(
        recipient=account.email,
        account_id=account.id,
        order_id=order.id,
        kind="order_status",
        subject="Purge customer",
        idempotency_key="purge-customer-message",
        status="sent",
        html_body="<p>Purge Customer</p>",
        text_body="Purge Customer",
    )
    db.session.add_all([
        OrderItem(order_id=order.id, product_name="Test item", quantity=1),
        OrderStatusHistory(
            order_id=order.id,
            field="status",
            old_value="",
            new_value="Created",
            note="Purge Customer 254700000077",
        ),
        RefundRequest(
            order_id=order.id,
            payment_transaction_id=transaction.id,
            amount_cents=100,
            reason="Purge Customer requested a refund",
        ),
        CustomerOrderLink(account_id=account.id, order_id=order.id),
        CustomerConsentEvent(
            account_id=account.id,
            event="confirmed",
            method="registration_checkbox",
        ),
        EmailCampaignRecipient(
            campaign_id=campaign.id,
            account_id=account.id,
            recipient=account.email,
        ),
        MarketingProductExposure(
            account_id=account.id,
            product_id=Product.query.first().id,
            last_sent_at=datetime.utcnow(),
        ),
        ProductEnquiry(
            product_name="Test item",
            contact=account.email,
            message="Please contact Purge Customer",
        ),
        ProductEnquiry(
            product_name="Test item",
            contact=profile.phone,
            message="Please contact this customer",
        ),
        RestockAlert(
            product_id=Product.query.first().id,
            contact=account.email,
            preferred_contact="Email",
        ),
        RestockAlert(
            product_id=Product.query.first().id,
            contact=profile.phone,
            preferred_contact="WhatsApp",
        ),
        ProductSuggestion(
            product_name="Test item",
            category="Accessories",
            details=f"Contact {account.email}",
        ),
        Review(
            customer_account_id=account.id,
            display_name=account.display_name,
            rating=5,
            text="Purge this review",
        ),
    ])
    db.session.add(message)
    db.session.flush()
    attempt = EmailDeliveryAttempt(
        message_id=message.id,
        attempt_number=1,
        attempt_key="purge-customer-attempt",
        provider="brevo",
        status="accepted",
    )
    db.session.add(attempt)
    db.session.flush()
    investigation = EmailDeliveryInvestigation(
        message_id=message.id,
        original_attempt_id=attempt.id,
        provider="brevo",
        reason="Test investigation",
    )
    db.session.add(investigation)
    db.session.flush()
    db.session.add_all([
        EmailDeliveryInvestigationEvent(
            investigation_id=investigation.id,
            actor_admin_id=admin.id,
            event_type="note_added",
            explanation="Purge Customer",
            related_attempt_id=attempt.id,
        ),
        EmailLegacyPayloadReviewEvent(
            message_id=message.id,
            actor_admin_id=admin.id,
            actor_type="admin",
            reason="Reviewed during test",
        ),
        EmailDraft(
            name="Purge customer draft",
            category="customer_service",
            subject="Purge Customer",
            headline="Purge Customer",
            message=account.email,
            account_ids_json=json.dumps([account.id]),
            cart_id=cart.id,
            order_id=order.id,
            created_by_id=admin.id,
        ),
        EmailDraft(
            name="Unlinked customer follow-up",
            category="customer_service",
            subject="Contact customer",
            headline="Follow up",
            message=f"Contact {account.email}",
            account_ids_json="[]",
            created_by_id=admin.id,
        ),
        ReferralClick(
            referrer_id=profile.id,
            referral_code=profile.referral_code,
            visitor_key="purge-visitor-hash",
        ),
        Referral(
            referrer_key=profile.phone,
            referred_key="254700000078",
            referral_code="PURGE-REF-077",
            qualifying_order_id=order.id,
        ),
        LoyaltyTransaction(
            customer_key=profile.phone,
            order_id=order.id,
            transaction_type="earn",
            points=10,
        ),
        FinancialAuditEvent(
            admin_id=admin.id,
            event_type="order_updated",
            entity_type="Order",
            entity_id=order.id,
            note="Purge Customer payment history",
        ),
    ])
    suppression_case = MarketingSuppressionCase(
        account_id=account.id,
        source="admin",
        reason_code="admin_review",
        metadata_json={"email": account.email},
    )
    db.session.add(suppression_case)
    db.session.flush()
    db.session.add(MarketingSuppressionEvent(
        suppression_case_id=suppression_case.id,
        account_id=account.id,
        event_type="suppression_detected",
        actor_type="admin",
        admin_id=admin.id,
        source="admin",
        reason_code="admin_review",
        reason_text=f"Review {account.email}",
        idempotency_key="purge-customer-suppression-event",
        metadata_json={"email": account.email},
    ))
    db.session.commit()
    from app.services.email_delivery_ops import apply_email_delivery_ops_migration
    from app.services.email_legacy_reviews import apply_email_legacy_review_migration
    from app.services.consent_audit import (
        apply_consent_audit_migration,
        apply_customer_data_erasure_migration,
    )
    migration_connection = db.session.connection()
    migration_connection.exec_driver_sql(
        "CREATE TABLE IF NOT EXISTS email_schema_migration "
        "(migration_id VARCHAR(120) PRIMARY KEY, checksum VARCHAR(64) NOT NULL)"
    )
    apply_email_delivery_ops_migration(migration_connection, "sqlite")
    apply_email_legacy_review_migration(migration_connection, "sqlite")
    apply_consent_audit_migration(migration_connection, "sqlite")
    apply_customer_data_erasure_migration(migration_connection, "sqlite")
    account_id = account.id
    order_id = order.id
    cart_id = cart.id
    message_id = message.id
    campaign_id = campaign.id

    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    wrong_confirmation = client.post(
        f"/admin/email/customers/{account_id}/permanently-delete",
        data={"confirmation": f"DELETE CUSTOMER {account_id}"},
        follow_redirects=True,
    )
    db.session.expire_all()
    assert b"Type PURGE CUSTOMER" in wrong_confirmation.data
    assert db.session.get(CustomerAccount, account_id) is not None
    assert db.session.get(Order, order_id) is not None
    assert proof_path.exists()

    response = client.post(
        f"/admin/email/customers/{account_id}/permanently-delete",
        data={"confirmation": f"PURGE CUSTOMER {account_id}"},
        follow_redirects=True,
    )
    db.session.expire_all()

    assert response.status_code == 200
    assert b"Permanent deletion completed" in response.data
    assert db.session.get(CustomerAccount, account_id) is None
    assert db.session.get(Order, order_id) is None
    assert db.session.get(CustomerCart, cart_id) is None
    assert EmailMessage.query.filter_by(id=message_id).count() == 0
    assert CustomerProfile.query.filter_by(account_id=account_id).count() == 0
    assert CustomerConsentEvent.query.filter_by(account_id=account_id).count() == 0
    assert CustomerOrderLink.query.filter_by(account_id=account_id).count() == 0
    assert EmailCampaignRecipient.query.filter_by(account_id=account_id).count() == 0
    assert EmailCampaign.query.filter_by(id=campaign_id).count() == 1
    assert PaymentAttempt.query.filter_by(order_id=order_id).count() == 0
    assert PaymentTransaction.query.filter_by(order_id=order_id).count() == 0
    assert RefundRequest.query.filter_by(order_id=order_id).count() == 0
    assert EmailDeliveryAttempt.query.filter_by(message_id=message_id).count() == 0
    assert EmailDeliveryInvestigation.query.filter_by(message_id=message_id).count() == 0
    assert EmailLegacyPayloadReviewEvent.query.filter_by(message_id=message_id).count() == 0
    assert EmailDraft.query.filter(EmailDraft.order_id == order_id).count() == 0
    assert EmailDraft.query.filter(
        EmailDraft.message.ilike("%purge-customer@example.test%")
    ).count() == 0
    assert ReferralClick.query.filter_by(referral_code="PURGEME077").count() == 0
    assert Referral.query.filter_by(referral_code="PURGE-REF-077").count() == 0
    assert LoyaltyTransaction.query.filter_by(customer_key="254700000077").count() == 0
    assert MarketingProductExposure.query.filter_by(account_id=account_id).count() == 0
    assert MarketingSuppressionCase.query.filter_by(account_id=account_id).count() == 0
    assert MarketingSuppressionEvent.query.filter_by(account_id=account_id).count() == 0
    assert ProductEnquiry.query.filter_by(contact="purge-customer@example.test").count() == 0
    assert ProductEnquiry.query.filter_by(contact="254700000077").count() == 0
    assert RestockAlert.query.filter_by(contact="purge-customer@example.test").count() == 0
    assert RestockAlert.query.filter_by(contact="254700000077").count() == 0
    assert ProductSuggestion.query.filter(
        ProductSuggestion.details.ilike("%purge-customer@example.test%")
    ).count() == 0
    assert Review.query.filter_by(display_name="Purge Customer").count() == 0
    assert FinancialAuditEvent.query.filter_by(entity_type="Order", entity_id=order_id).count() == 0
    purge_audit = FinancialAuditEvent.query.filter_by(
        event_type="customer_account_permanently_deleted",
        entity_type="CustomerAccount",
        entity_id=None,
    ).one()
    assert "purge-customer@example.test" not in purge_audit.note
    assert proof_path.exists() is False


def test_admin_customer_permanent_deletion_waits_for_email_handoff(client, app, app_context):
    from app.models import EmailDeliveryAttempt

    admin = AdminUser.query.filter_by(username="owner").one()
    account = CustomerAccount(email="purge-in-flight@example.test", display_name="In Flight")
    db.session.add(account)
    db.session.flush()
    message = EmailMessage(
        recipient=account.email,
        account_id=account.id,
        kind="order_status",
        subject="In flight",
        idempotency_key="purge-customer-in-flight",
        status="failed",
    )
    db.session.add(message)
    db.session.flush()
    db.session.add(EmailDeliveryAttempt(
        message_id=message.id,
        attempt_number=1,
        attempt_key="purge-customer-in-flight-attempt",
        provider="brevo",
        status="in_flight",
    ))
    db.session.commit()
    account_id = account.id

    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    response = client.post(
        f"/admin/email/customers/{account_id}/permanently-delete",
        data={"confirmation": f"PURGE CUSTOMER {account_id}"},
        follow_redirects=True,
    )

    db.session.expire_all()
    assert b"Wait for the provider attempt to finish" in response.data
    assert db.session.get(CustomerAccount, account_id) is not None
    assert EmailDeliveryAttempt.query.filter_by(status="in_flight").count() == 1


def test_admin_can_permanently_erase_previously_anonymized_account_and_leftover_cart(
    client, app, app_context,
):
    account = CustomerAccount(
        email="removed-customer-900@redacted.invalid",
        display_name="Deleted customer",
        password_hash=None,
        active=False,
    )
    db.session.add(account)
    db.session.flush()
    order = Order(
        reference="PURGEOLD1",
        customer_name="Deleted customer",
        customer_phone="",
        fulfilment_method="collection",
    )
    db.session.add(order)
    db.session.flush()
    cart = CustomerCart(account_id=account.id, items_json="[]")
    db.session.add_all([
        CustomerOrderLink(account_id=account.id, order_id=order.id),
        EmailMessage(
            recipient=account.email,
            account_id=account.id,
            kind="order_status",
            subject="Customer data redacted",
            idempotency_key="purge-previously-anonymized",
            status="suppressed",
        ),
    ])
    db.session.add(cart)
    db.session.commit()
    account_id = account.id
    order_id = order.id
    cart_id = cart.id

    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    response = client.post(
        f"/admin/email/customers/{account_id}/permanently-delete",
        data={"confirmation": f"PURGE CUSTOMER {account_id}"},
        follow_redirects=True,
    )

    db.session.expire_all()
    assert response.status_code == 200
    assert b"Permanent deletion completed" in response.data
    assert db.session.get(CustomerAccount, account_id) is None
    assert db.session.get(Order, order_id) is None
    assert db.session.get(CustomerCart, cart_id) is None


def test_admin_customer_permanent_deletion_rolls_back_before_removing_proof_on_failure(
    client, app, app_context, monkeypatch, tmp_path,
):
    from sqlalchemy.exc import IntegrityError
    import app.services.customer_account_cleanup as cleanup

    account = CustomerAccount(
        email="purge-rollback@example.test",
        display_name="Rollback Purge",
    )
    db.session.add(account)
    db.session.flush()
    order = Order(
        reference="PURGERB01",
        customer_name="Rollback Purge",
        customer_phone="254700000088",
        fulfilment_method="collection",
        payment_proof_filename="purge-rollback-proof.png",
    )
    db.session.add(order)
    db.session.flush()
    cart = CustomerCart(account_id=account.id, items_json='[{"id": 1}]')
    db.session.add_all([
        cart,
        CustomerOrderLink(account_id=account.id, order_id=order.id),
    ])
    db.session.commit()
    account_id = account.id
    order_id = order.id
    cart_id = cart.id
    app.config["PAYMENT_PROOF_FOLDER"] = str(tmp_path)
    proof_path = tmp_path / "purge-rollback-proof.png"
    proof_path.write_bytes(b"private payment proof")

    original_delete = cleanup.permanently_delete_customer_account

    def fail_before_commit(*args, **kwargs):
        original_delete(*args, **kwargs)
        raise IntegrityError("forced commit failure", {}, RuntimeError("test failure"))

    monkeypatch.setattr(cleanup, "permanently_delete_customer_account", fail_before_commit)
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    response = client.post(
        f"/admin/email/customers/{account_id}/permanently-delete",
        data={"confirmation": f"PURGE CUSTOMER {account_id}"},
        follow_redirects=True,
    )

    db.session.expire_all()
    assert response.status_code == 200
    assert b"failed and was rolled back" in response.data
    assert db.session.get(CustomerAccount, account_id) is not None
    assert db.session.get(Order, order_id) is not None
    assert db.session.get(CustomerCart, cart_id) is not None
    assert proof_path.exists()


def test_admin_email_history_supports_search_status_and_pagination(client, app, app_context):
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    rows = [
        EmailMessage(
            recipient=f"history-{index}@example.test", subject=f"History subject {index}",
            kind="order_status", idempotency_key=f"email-history-page-{index}",
            status="failed" if index == 0 else "sent",
            failure_reason="provider timeout" if index == 0 else "",
        )
        for index in range(30)
    ]
    db.session.add_all(rows)
    db.session.commit()

    page_two = client.get("/admin/email?message_page=2")
    assert page_two.status_code == 200
    assert b"Page 2 of 2" in page_two.data
    assert b'data-label="Recipient"' in page_two.data
    filtered = client.get("/admin/email?message_q=history-0%40example.test&message_status=failed")
    assert filtered.status_code == 200
    assert b"history-0@example.test" in filtered.data
    assert b"history-1@example.test" not in filtered.data


def test_admin_can_find_studio_messages_and_email_studio_from_admin_pages(client, app, app_context):
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    center = client.get("/admin/email")
    assert center.status_code == 200
    assert b"Studio messages" in center.data
    assert b"Open Email Studio" in center.data
    messages = client.get("/admin/email/studio-messages")
    assert messages.status_code == 200
    assert b"Reusable templates" in messages.data
    assert b"Create in Email Studio" in messages.data
    studio = client.get("/admin/email/studio")
    assert studio.status_code == 200
    assert b"Studio messages" in studio.data


def test_email_studio_library_lists_purposes_and_previews_system_templates_without_sending(client, app, app_context):
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    library = client.get("/admin/email/studio-messages")
    assert library.status_code == 200
    assert b"Daily Fity Goodies" in library.data
    assert b"Weekly product picks" in library.data
    assert b"Operational (administrator-only)" in library.data
    assert b"Not implemented" in library.data

    account_preview = client.get("/admin/email/studio/system-preview/verify_email")
    assert account_preview.status_code == 200
    assert b"does not create tokens" in account_preview.data
    assert b"Verify your email address" in account_preview.data
    assert b'<iframe title="Static email template sample" sandbox' in account_preview.data
    order_preview = client.get("/admin/email/studio/system-preview/order_received")
    assert order_preview.status_code == 200
    assert b"SAMPLE-ORDER" in order_preview.data
    unavailable_preview = client.get("/admin/email/studio/system-preview/refund_confirmation")
    assert unavailable_preview.status_code == 200
    assert b"SAMPLE-REFUND" in unavailable_preview.data


def test_completed_refund_dispatches_an_idempotent_transactional_customer_notice(app, app_context, monkeypatch):
    from app.models import CustomerAccount, CustomerOrderLink, Order, RefundRequest, EmailMessage
    from app.services.customer_mail import notify_refund_completed
    from app.services.email_purpose import email_purpose_for_kind
    import app.services.customer_mail as customer_mail

    sent = []
    app.config.update(MAIL_PROVIDER="brevo")
    monkeypatch.setattr(customer_mail, "mail_configuration_status", lambda: (True, "configured"))
    monkeypatch.setattr(customer_mail, "_send_brevo", lambda *args: sent.append(args) or "refund-provider-id")

    account = CustomerAccount(
        email="refund-customer@example.com", display_name="Refund Customer",
        email_verified_at=datetime.utcnow(), active=True,
    )
    order = Order(reference="FITY-REFUND-TEST", fulfilment_method="collection", total_cents=2000)
    db.session.add_all([account, order])
    db.session.flush()
    db.session.add(CustomerOrderLink(account_id=account.id, order_id=order.id))
    refund = RefundRequest(
        order_id=order.id, amount_cents=700, reason="Test refund",
        status="completed", reference="RFD-TEST-001", completed_at=datetime.utcnow(),
    )
    db.session.add(refund)
    db.session.commit()

    first = notify_refund_completed(refund)
    second = notify_refund_completed(refund)

    assert email_purpose_for_kind("refund_confirmation") == "transactional"
    assert first.status == "sent"
    assert second.id == first.id
    assert EmailMessage.query.filter_by(idempotency_key=f"refund-completed:{refund.id}").count() == 1
    assert first.account_id == account.id
    assert first.order_id == order.id
    assert "RFD-TEST-001" in sent[0][3]
    assert len(sent) == 1


def test_admin_recorded_payment_failure_dispatches_transactional_notice(app, app_context, monkeypatch):
    from app.models import CustomerAccount, CustomerOrderLink, Order
    from app.services.customer_mail import notify_order_event
    from app.services.email_purpose import email_purpose_for_kind
    import app.services.customer_mail as customer_mail

    sent = []
    app.config.update(MAIL_PROVIDER="brevo")
    monkeypatch.setattr(customer_mail, "mail_configuration_status", lambda: (True, "configured"))
    monkeypatch.setattr(customer_mail, "_send_brevo", lambda *args: sent.append(args) or "payment-provider-id")

    account = CustomerAccount(
        email="failed-payment@example.com", display_name="Payment Customer",
        email_verified_at=datetime.utcnow(), active=True,
    )
    order = Order(
        reference="FITY-PAYMENT-FAIL-TEST", fulfilment_method="collection",
        payment_status="Failed", total_cents=1900,
    )
    db.session.add_all([account, order])
    db.session.flush()
    db.session.add(CustomerOrderLink(account_id=account.id, order_id=order.id))
    db.session.commit()

    result = notify_order_event(order, "payment_failed", "payment-failed:test-event")

    assert email_purpose_for_kind("payment_failed") == "transactional"
    assert result.status == "sent"
    assert result.order_id == order.id
    assert "recorded" in sent[0][3]
    assert "contact us before making another payment" in sent[0][3]


def test_explicit_email_restock_request_dispatches_after_stock_commit_without_marketing_consent(app, app_context, monkeypatch):
    from app.models import EmailMessage, Product, RestockAlert
    from app.services.email_purpose import email_purpose_for_kind
    from app.services.inventory import adjust_stock, dispatch_queued_restock_alerts

    product = Product.query.filter_by(slug="plain-socks").first()
    product.stock_quantity = 0
    product.reserved_quantity = 0
    alert = RestockAlert(
        product_id=product.id, contact="restock@example.com",
        preferred_contact="Email", consent_to_contact=True, status="pending",
        size="M", color="Navy",
    )
    db.session.add(alert)
    db.session.commit()

    sent = []
    import app.services.customer_mail as customer_mail
    app.config["MAIL_PROVIDER"] = "brevo"
    monkeypatch.setattr(customer_mail, "mail_configuration_status", lambda: (True, "configured"))
    monkeypatch.setattr(customer_mail, "_send_brevo", lambda *args: sent.append(args) or "restock-provider-id")
    adjust_stock(product, 3, "Restock email test")
    assert not sent
    db.session.commit()
    assert alert.status == "queued"
    queued_text = EmailMessage.query.filter_by(idempotency_key=f"restock-alert:{alert.id}").one().text_body
    dispatch_queued_restock_alerts(product)
    assert len(sent) == 1

    message = EmailMessage.query.filter_by(idempotency_key=f"restock-alert:{alert.id}").one()
    assert email_purpose_for_kind("restock_notification") == "transactional"
    assert message.purpose == "transactional"
    assert message.recipient == "restock@example.com"
    assert message.account_id is None
    assert message.status == "sent"
    db.session.refresh(alert)
    assert alert.status == "notified"
    assert message.provider_message_id == "restock-provider-id"
    assert len(sent) == 1
    assert "only about your restock request" in queued_text


def test_terminal_restock_delivery_failure_returns_alert_to_pending_review(app, app_context):
    from app.models import Product, RestockAlert
    from app.services.customer_mail import _finalize_dispatch

    product = Product.query.filter_by(slug="plain-socks").first()
    alert = RestockAlert(
        product_id=product.id, contact="failed-restock@example.test",
        preferred_contact="Email", consent_to_contact=True, status="queued",
    )
    db.session.add(alert)
    db.session.flush()
    row = EmailMessage(
        recipient=alert.contact, subject="Requested item available",
        kind="restock_notification", purpose="transactional",
        idempotency_key=f"restock-alert:{alert.id}", status="sending",
        claim_token="restock-terminal-claim", html_body="<p>Ready</p>", text_body="Ready",
    )
    db.session.add(row)
    db.session.commit()

    assert _finalize_dispatch(
        row, "restock-terminal-claim", status="failed",
        error_category="permanent_provider", next_attempt_at=None,
        failure_reason="Permanent provider rejection", clear_payload=True,
    )
    db.session.refresh(alert)
    assert alert.status == "pending"
    assert alert.notified_at is None


def test_stale_restock_dispatch_returns_alert_to_pending_review(app, app_context):
    from datetime import timedelta
    from app.models import Product, RestockAlert
    from app.services.customer_mail import recover_stale_email_claims

    now = datetime.utcnow()
    product = Product.query.filter_by(slug="plain-socks").first()
    alert = RestockAlert(
        product_id=product.id, contact="stale-restock@example.test",
        preferred_contact="Email", consent_to_contact=True, status="queued",
    )
    db.session.add(alert)
    db.session.flush()
    row = EmailMessage(
        recipient=alert.contact, subject="Requested item available",
        kind="restock_notification", purpose="transactional",
        idempotency_key=f"restock-alert:{alert.id}", status="sending",
        claim_token="restock-stale-claim", claimed_at=now - timedelta(hours=1),
        html_body="<p>Ready</p>", text_body="Ready",
    )
    db.session.add(row)
    db.session.commit()

    assert recover_stale_email_claims(age_minutes=30, now=now) == 1
    db.session.refresh(alert)
    assert alert.status == "pending"
    assert alert.notified_at is None


def test_studio_messages_page_reports_missing_email_tables_without_orm_query(client, app, app_context, monkeypatch):
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    from app.admin import routes as admin_routes

    original = admin_routes.model_schema_missing

    def report_missing(model, inspector=None):
        if model.__tablename__ == "email_template":
            return ["email_template table"]
        return original(model, inspector)

    monkeypatch.setattr(admin_routes, "model_schema_missing", report_missing)

    response = client.get("/admin/email/studio-messages")

    assert response.status_code == 200
    assert b"Email Studio storage is not ready" in response.data
    assert b"email_template table" in response.data
    assert b"migrate-customer-email-schema" in response.data
    assert b"Create in Email Studio" not in response.data


def test_customer_orders_are_account_scoped_and_admin_is_not_customer(client, app, app_context):
    from datetime import datetime
    first = CustomerAccount(email="first@example.com", display_name="First", password_hash=generate_password_hash("a-strong-test-password-123"), email_verified_at=datetime.utcnow())
    second = CustomerAccount(email="second@example.com", display_name="Second", password_hash=generate_password_hash("a-strong-test-password-123"), email_verified_at=datetime.utcnow())
    db.session.add_all([first, second])
    db.session.flush()
    accept_current_terms_for_test(first)
    accept_current_terms_for_test(second)
    order1 = Order(reference="FITY-ACCOUNT-1", fulfilment_method="Collection", customer_name="First")
    order2 = Order(reference="FITY-ACCOUNT-2", fulfilment_method="Collection", customer_name="Second")
    db.session.add_all([order1, order2])
    db.session.flush()
    db.session.add_all([CustomerOrderLink(account_id=first.id, order_id=order1.id), CustomerOrderLink(account_id=second.id, order_id=order2.id)])
    db.session.commit()
    sign_in_verified_customer(client, first.email)
    response = client.get("/account/", follow_redirects=True)
    assert response.status_code == 200
    assert b"FITY-ACCOUNT-1" in response.data
    assert b"FITY-ACCOUNT-2" not in response.data
    assert client.get("/admin/").status_code == 403


def test_customer_dashboard_activity_uses_owned_order_history_without_internal_notes(client, app, app_context):
    account = CustomerAccount(email="activity-owner@example.com", display_name="Activity Owner", email_verified_at=datetime.utcnow())
    db.session.add(account)
    db.session.flush()
    order = Order(reference="FITY-ACTIVITY-1", fulfilment_method="Collection", status="Ready for collection")
    db.session.add(order)
    db.session.flush()
    db.session.add(CustomerOrderLink(account_id=account.id, order_id=order.id))
    db.session.add(OrderStatusHistory(
        order_id=order.id, field="status", old_value="Order received",
        new_value="Ready for collection", note="Internal stockroom note: keep private",
    ))
    db.session.commit()
    _email_code_auth(client, account.email)

    response = client.get("/account/")
    assert response.status_code == 200
    assert b"Order placed" in response.data
    assert b"Order status updated" in response.data
    assert b"FITY-ACTIVITY-1" in response.data
    assert b"Internal stockroom note" not in response.data


def test_customer_profile_menu_and_remembered_session(client, app, app_context):
    account = CustomerAccount(
        email="profile-menu@example.com", display_name="Profile Shopper",
        password_hash=generate_password_hash("a-strong-test-password-123"),
        email_verified_at=datetime.utcnow(),
    )
    db.session.add(account)
    db.session.commit()
    accept_current_terms_for_test(account)

    signed_in = _email_code_auth(
        client, account.email, follow_redirects=False,
    )
    assert signed_in.status_code == 302
    cookies = signed_in.headers.getlist("Set-Cookie")
    assert any(cookie.startswith("remember_token=") and "Expires=" in cookie for cookie in cookies), cookies
    home = client.get("/")
    assert b"Profile Shopper" in home.data
    assert b"My orders" in home.data
    client.delete_cookie("session", domain="example.test")
    dashboard = client.get("/account/")
    assert dashboard.status_code == 200
    assert b"Profile Shopper" in dashboard.data
    assert b"Recently viewed" in dashboard.data
    assert b"Recent activity" in dashboard.data

    signed_out = client.post("/account/logout", data={"csrf_token": "test"}, follow_redirects=True)
    assert signed_out.status_code == 200
    guest_home = client.get("/")
    assert b"Sign in" in guest_home.data
    assert b"Profile Shopper" not in guest_home.data


def test_customer_can_claim_only_unlinked_order_with_private_tracking_token(client, app, app_context):
    first = CustomerAccount(email="claim-first@example.com", display_name="First", email_verified_at=datetime.utcnow())
    second = CustomerAccount(email="claim-second@example.com", display_name="Second", email_verified_at=datetime.utcnow())
    unlinked = Order(reference="FITY-CLAIM-1", fulfilment_method="Collection", tracking_token="private-token-for-first-order")
    owned = Order(reference="FITY-CLAIM-2", fulfilment_method="Collection", tracking_token="private-token-for-second-order")
    db.session.add_all([first, second, unlinked, owned])
    db.session.flush()
    db.session.add(CustomerOrderLink(account_id=second.id, order_id=owned.id))
    db.session.commit()
    sign_in_verified_customer(client, first.email)

    wrong = client.post("/account/orders/link", data={"reference": unlinked.reference, "tracking_token": "wrong-token"})
    assert wrong.status_code == 302
    assert CustomerOrderLink.query.filter_by(order_id=unlinked.id).count() == 0
    linked = client.post("/account/orders/link", data={"reference": unlinked.reference, "tracking_token": unlinked.tracking_token})
    assert linked.status_code == 302
    assert CustomerOrderLink.query.filter_by(order_id=unlinked.id, account_id=first.id).one()
    refused = client.post("/account/orders/link", data={"reference": owned.reference, "tracking_token": owned.tracking_token})
    assert refused.status_code == 302
    assert CustomerOrderLink.query.filter_by(order_id=owned.id).one().account_id == second.id
    dashboard = client.get("/account/")
    assert b"FITY-CLAIM-1" in dashboard.data
    assert b"FITY-CLAIM-2" not in dashboard.data


def test_recently_viewed_endpoint_returns_only_requested_active_catalogue_products(client, app, app_context):
    sign_in_verified_customer(client)
    response = client.get("/account/recent-products?id=1&id=999999&id=bad")
    assert response.status_code == 200
    assert [item["id"] for item in response.json["products"]] == [1]


def test_checkout_redirect_preserves_checkout_return_for_login_and_registration(client):
    response = client.get("/checkout", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/account/login?next=/checkout")
    login = client.get(response.headers["Location"])
    assert b'name="next" value="/checkout"' in login.data
    assert b'action="/account/email-code"' in login.data
    assert b'name="email"' in login.data


def test_google_signin_uses_mocked_verified_sub_and_refuses_unsafe_link(client, app, monkeypatch, app_context):
    import app.customer.routes as customer_routes
    app.config["GOOGLE_CLIENT_ID"] = "test-client-id"
    claims = {"sub": "google-sub-123", "email": "googleuser@gmail.com", "name": "Google User", "email_verified": True, "hd": ""}
    monkeypatch.setattr(customer_routes, "verify_google_credential", lambda credential: claims)
    client.set_cookie("g_csrf_token", "csrf-state", domain="example.test")
    declined = client.post("/account/google", data={"credential": "mocked", "g_csrf_token": "csrf-state"})
    assert declined.status_code == 302
    assert CustomerAccount.query.filter_by(google_sub="google-sub-123").count() == 0
    response = client.post("/account/google", data={"credential": "mocked", "g_csrf_token": "csrf-state", "accept_terms": "yes"}, follow_redirects=True)
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
    assert result.status == "queued"
    customer_mail.process_email_queue(limit=5)
    db.session.refresh(result)
    assert result.status == "failed"
    assert "private credential" not in result.failure_reason
    assert result.failure_reason == "SMTP error 535."


def test_campaign_routes_require_admin_and_create_only_a_draft(client, app_context):
    assert client.post("/admin/login", data={"username": "owner", "password": "secret123"}, follow_redirects=True).status_code == 200
    listing = client.get("/admin/email/campaigns")
    assert listing.status_code == 200
    assert b"Marketing campaigns" in listing.data
    created = client.post("/admin/email/campaigns", data={
        "name": "New arrivals", "subject": "New arrivals",
        "body": "See what is new.", "campaign_type": "custom",
    })
    assert created.status_code == 302
    campaign = EmailCampaign.query.filter_by(name="New arrivals").one()
    assert campaign.status == "draft"
    assert EmailMessage.query.filter_by(campaign_id=campaign.id).count() == 0


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
        "email_campaign", "email_campaign_recipient", "customer_cart", "email_template", "email_draft",
        "marketing_automation_settings", "marketing_campaign_run", "marketing_product_exposure",
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


def _create_historical_reconciliation_schema(isolated_app, omitted_tables=()):
    from app import customer_email_schema_tables
    from scripts.customer_email_schema_guard import (
        HISTORICAL_REQUIRED_TABLES, migration_column_manifest,
    )
    from sqlalchemy import text
    from sqlalchemy.schema import DefaultClause

    with isolated_app.app_context():
        with db.engine.begin() as connection:
            for table in (
                "email_delivery_investigation_event",
                "email_delivery_investigation",
                "email_delivery_attempt",
                "email_worker_run",
                "email_legacy_payload_review_event",
            ):
                connection.exec_driver_sql(f"DROP TABLE IF EXISTS {table}")
        tables = [
            table for table in customer_email_schema_tables()
            if table.name in HISTORICAL_REQUIRED_TABLES and table.name not in omitted_tables
        ]
        manifest = migration_column_manifest()
        original_defaults = []
        for table in tables:
            for column in table.columns:
                spec = manifest.get((table.name, column.name))
                if spec:
                    original_defaults.append((column, column.server_default, column.nullable))
                    column.nullable = spec.nullable
                    if spec.default not in {None, "__serial__"}:
                        column.server_default = DefaultClause(text(spec.default))
        try:
            db.metadata.create_all(bind=db.engine, tables=tables, checkfirst=True)
        finally:
            for column, server_default, nullable in original_defaults:
                column.server_default = server_default
                column.nullable = nullable


def test_customer_email_migration_creates_schema_on_fresh_sqlite_database(tmp_path, monkeypatch):
    import sys
    monkeypatch.setenv("FLASK_RUN_FROM_CLI", "true")
    monkeypatch.setattr(sys, "argv", ["flask", "--app", "wsgi:app", *_confirmed_customer_email_migration_args()])
    config = type(
        "FreshMigrationSQLiteConfig",
        (TestConfig,),
        {"SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'fresh-store.sqlite3'}"},
    )
    isolated_app = create_app(config)
    with isolated_app.app_context():
        from sqlalchemy import inspect
        assert inspect(db.engine).get_table_names() == []
        db.metadata.create_all(bind=db.engine, tables=[AdminUser.__table__, Order.__table__, Category.__table__, Product.__table__])
    result = isolated_app.test_cli_runner().invoke(args=_confirmed_customer_email_migration_args())
    assert result.exit_code == 0, result.output
    assert "Created tables:" in result.output
    with isolated_app.app_context():
        from sqlalchemy import inspect
        expected = {
            "admin_user", "order", "category", "product", "customer_account", "customer_token",
            "customer_consent_event", "customer_order_link", "auth_rate_limit",
            "email_message", "email_campaign", "email_campaign_recipient",
            "customer_cart", "email_template", "email_draft", "marketing_automation_settings", "marketing_campaign_run", "marketing_product_exposure", "email_schema_migration",
            "email_delivery_attempt", "email_delivery_investigation",
            "email_delivery_investigation_event", "email_worker_run",
            "email_legacy_payload_review_event",
        }
        assert set(inspect(db.engine).get_table_names()) == expected


def test_customer_email_schema_preflight_is_read_only_and_recommends_migration(tmp_path, monkeypatch):
    isolated_app, feature_tables, expected = _isolated_legacy_sqlite_app(tmp_path, monkeypatch)
    with isolated_app.app_context():
        from sqlalchemy import inspect
        before_tables = set(inspect(db.engine).get_table_names())
        before_product_count = Product.query.count()
        before_admin_hash = AdminUser.query.one().password_hash
    result = isolated_app.test_cli_runner().invoke(args=["check-customer-email-schema"])
    assert result.exit_code == 0, result.output
    assert "Database dialect: sqlite" in result.output
    assert "Schema status: incomplete" in result.output
    assert "customer_account: missing table" in result.output
    assert "migrate-customer-email-schema" in result.output
    with isolated_app.app_context():
        from sqlalchemy import inspect
        assert set(inspect(db.engine).get_table_names()) == before_tables
        assert not (feature_tables & before_tables)
        assert Product.query.count() == before_product_count
        assert AdminUser.query.one().password_hash == before_admin_hash


def test_customer_email_migration_requires_exact_operation_and_backup_confirmations(app, app_context):
    from sqlalchemy import inspect

    before = set(inspect(db.engine).get_table_names())
    runner = app.test_cli_runner()
    missing = runner.invoke(args=["migrate-customer-email-schema"])
    wrong_backup = runner.invoke(args=[
        "migrate-customer-email-schema",
        "--confirmation", "MIGRATE CUSTOMER EMAIL SCHEMA",
        "--backup-confirmation", "wrong",
    ])
    assert missing.exit_code != 0
    assert wrong_backup.exit_code != 0
    assert "verified-backup confirmations are required" in missing.output
    assert "verified-backup confirmations are required" in wrong_backup.output
    assert set(inspect(db.engine).get_table_names()) == before


def test_customer_email_history_reconciliation_requires_both_confirmations(app, app_context):
    from sqlalchemy import inspect

    before = set(inspect(db.engine).get_table_names())
    result = app.test_cli_runner().invoke(args=[
        "reconcile-customer-email-migration-history",
        "--confirmation", "RECONCILE VERIFIED CUSTOMER EMAIL SCHEMA",
    ])
    assert result.exit_code != 0
    assert "verified-backup confirmations are required" in result.output
    assert set(inspect(db.engine).get_table_names()) == before


def _confirmed_reconciliation_args():
    return [
        "reconcile-customer-email-migration-history",
        "--confirmation", "RECONCILE VERIFIED CUSTOMER EMAIL SCHEMA",
        "--backup-confirmation", "I HAVE A VERIFIED RESTORABLE BACKUP",
    ]


def test_reconciliation_rejects_missing_historical_table_without_recording_history(tmp_path, monkeypatch):
    from sqlalchemy import inspect

    isolated_app, _feature_tables, expected = _isolated_legacy_sqlite_app(tmp_path, monkeypatch)
    _create_historical_reconciliation_schema(isolated_app, {"email_template"})
    with isolated_app.app_context():
        result = isolated_app.test_cli_runner().invoke(args=_confirmed_reconciliation_args())
        assert result.exit_code != 0
        assert "required tables are missing: email_template" in result.output
        assert not inspect(db.engine).has_table("email_migration_history")
        product = Product.query.filter_by(slug="existing-product").one()
        order = Order.query.filter_by(reference="KEEP-ORDER").one()
        assert (product.name, product.stock_quantity, product.reserved_quantity) == (
            expected["product"][0], expected["product"][3], expected["product"][4]
        )
        assert (order.reference, order.total_cents, len(order.items)) == expected["order"]


def test_reconciliation_records_history_only_after_complete_schema_and_preserves_records(tmp_path, monkeypatch):
    from sqlalchemy import inspect, text

    isolated_app, _feature_tables, expected = _isolated_legacy_sqlite_app(tmp_path, monkeypatch)
    _create_historical_reconciliation_schema(isolated_app)
    with isolated_app.app_context():
        account = CustomerAccount(email="reconcile@example.test", display_name="Keep customer")
        db.session.add(account)
        db.session.flush()
        consent = CustomerConsentEvent(
            account_id=account.id, event="consent_granted", method="test",
            wording_version="2026-10",
        )
        message = EmailMessage(
            recipient=account.email, account_id=account.id,
            subject="Keep email", kind="order_received",
            idempotency_key="reconcile-preserve-email",
        )
        link = CustomerOrderLink(account_id=account.id, order_id=Order.query.filter_by(reference="KEEP-ORDER").one().id)
        db.session.add_all([consent, message, link])
        db.session.commit()
        ids = (account.id, consent.id, message.id, link.id)

        result = isolated_app.test_cli_runner().invoke(args=_confirmed_reconciliation_args())
        assert result.exit_code == 0, result.output
        history = db.session.execute(text(
            "SELECT migration_id, state FROM email_migration_history ORDER BY migration_id"
        )).all()
        assert {migration_id: state for migration_id, state in history} == {
            "20261117_email_migration_history": "applied",
            "20261017_customer_accounts_email": "reconciled_schema",
            "20261027_email_studio": "reconciled_schema",
            "20261028_email_builder": "reconciled_schema",
            "20261029_consent_wording_version": "reconciled_schema",
        }
        account_after = db.session.get(CustomerAccount, ids[0])
        consent_after = db.session.get(CustomerConsentEvent, ids[1])
        message_after = db.session.get(EmailMessage, ids[2])
        link_after = db.session.get(CustomerOrderLink, ids[3])
        assert (account_after.email, account_after.display_name) == ("reconcile@example.test", "Keep customer")
        assert (consent_after.event, consent_after.method, consent_after.wording_version) == (
            "consent_granted", "test", "2026-10"
        )
        assert (message_after.recipient, message_after.subject, message_after.kind) == (
            "reconcile@example.test", "Keep email", "order_received"
        )
        assert link_after.order.reference == "KEEP-ORDER"
        product = Product.query.filter_by(slug="existing-product").one()
        order = Order.query.filter_by(reference="KEEP-ORDER").one()
        assert (product.name, product.stock_quantity, product.reserved_quantity) == (
            expected["product"][0], expected["product"][3], expected["product"][4]
        )
        assert (order.reference, order.total_cents, len(order.items)) == expected["order"]


def test_reconciliation_rolls_back_ledger_and_entries_if_history_insert_fails(tmp_path, monkeypatch):
    from sqlalchemy import inspect
    from app.services import email_migrations

    isolated_app, _feature_tables, _expected = _isolated_legacy_sqlite_app(tmp_path, monkeypatch)
    _create_historical_reconciliation_schema(isolated_app)
    original_record = email_migrations.record_migration

    def fail_after_one_history_row(connection, migration_id, *, state, evidence):
        if migration_id == "20261027_email_studio":
            raise RuntimeError("simulated history insert failure")
        original_record(connection, migration_id, state=state, evidence=evidence)

    monkeypatch.setattr(email_migrations, "record_migration", fail_after_one_history_row)
    with isolated_app.app_context():
        result = isolated_app.test_cli_runner().invoke(args=_confirmed_reconciliation_args())
        assert result.exit_code != 0
        assert "RuntimeError" in result.output
        assert not inspect(db.engine).has_table("email_migration_history")


def test_customer_email_migration_rejects_incomplete_history_before_applying_additive_schema(app, app_context, monkeypatch):
    from sqlalchemy import inspect
    from app.services.email_migrations import ensure_history_table, record_migration

    monkeypatch.setattr(app_module, "database_dialect_name", lambda: "postgresql")
    with db.engine.begin() as connection:
        ensure_history_table(connection)
        record_migration(
            connection, "20261117_email_migration_history",
            state="applied", evidence="test",
        )
    result = app.test_cli_runner().invoke(args=_confirmed_customer_email_migration_args())
    assert result.exit_code != 0
    assert "Required customer/email migration history entries are missing" in result.output
    assert not inspect(db.engine).has_table("email_schema_migration")


def test_customer_email_migration_rejects_invalid_history_checksum_before_applying(app, app_context, monkeypatch):
    from sqlalchemy import inspect, text
    from app.services.email_migrations import (
        HISTORICAL_MIGRATIONS, ensure_history_table, record_migration,
    )

    monkeypatch.setattr(app_module, "database_dialect_name", lambda: "postgresql")
    with db.engine.begin() as connection:
        ensure_history_table(connection)
        record_migration(
            connection, "20261117_email_migration_history",
            state="applied", evidence="test ledger",
        )
        for migration_id in HISTORICAL_MIGRATIONS:
            record_migration(
                connection, migration_id, state="reconciled_schema", evidence="test schema"
            )
        connection.execute(text(
            "UPDATE email_migration_history SET checksum=:checksum "
            "WHERE migration_id=:migration_id"
        ), {"checksum": "0" * 64, "migration_id": HISTORICAL_MIGRATIONS[0]})

    result = app.test_cli_runner().invoke(args=_confirmed_customer_email_migration_args())
    assert result.exit_code != 0
    assert "checksum mismatch" in result.output
    assert not inspect(db.engine).has_table("email_schema_migration")


def test_customer_email_schema_preflight_reports_incomplete_table_without_changes(tmp_path, monkeypatch):
    isolated_app, feature_tables, _expected = _isolated_legacy_sqlite_app(tmp_path, monkeypatch)
    with isolated_app.app_context():
        from sqlalchemy import inspect, text
        db.session.execute(text("CREATE TABLE customer_account (id INTEGER PRIMARY KEY, email VARCHAR(254))"))
        db.session.execute(text("INSERT INTO customer_account (id, email) VALUES (1, 'preserve@example.test')"))
        db.session.commit()
        before_tables = set(inspect(db.engine).get_table_names())
        result = isolated_app.test_cli_runner().invoke(args=["check-customer-email-schema"])
        assert result.exit_code == 0, result.output
        assert "Schema status: incomplete" in result.output
        assert "customer_account: missing columns:" in result.output
        assert "marketing_product_exposure: missing table" in result.output
        assert set(inspect(db.engine).get_table_names()) == before_tables
        assert (set(inspect(db.engine).get_table_names()) & feature_tables) == {"customer_account"}
        assert db.session.execute(text("SELECT email FROM customer_account WHERE id=1")).scalar_one() == "preserve@example.test"


def test_app_factory_does_not_apply_schema_changes_implicitly(tmp_path):
    config = type(
        "ReadOnlyStartupConfig",
        (TestConfig,),
        {"SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'no-startup-ddl.sqlite3'}"},
    )
    isolated_app = create_app(config)
    with isolated_app.app_context():
        from sqlalchemy import inspect
        assert inspect(db.engine).get_table_names() == []


def test_customer_email_migrations_share_one_lock_definition():
    import inspect
    from app.services import email_migrations, marketing_suppressions

    assert email_migrations.MIGRATION_LOCK_ID == marketing_suppressions.MIGRATION_LOCK_ID
    assert "pg_try_advisory_xact_lock" in inspect.getsource(email_migrations.acquire_migration_lock)


def test_customer_email_migration_creates_missing_sqlite_tables_and_preserves_store_records(tmp_path, monkeypatch):
    isolated_app, feature_tables, expected = _isolated_legacy_sqlite_app(tmp_path, monkeypatch)
    runner = isolated_app.test_cli_runner()
    result = runner.invoke(args=_confirmed_customer_email_migration_args())
    assert result.exit_code == 0, result.output
    assert "Created tables:" in result.output
    assert "Existing customer, order, and product records were not modified." in result.output
    status = runner.invoke(args=["check-customer-email-schema"])
    assert status.exit_code == 0, status.output
    assert "Schema status: complete" in status.output
    with isolated_app.app_context():
        from sqlalchemy import inspect
        inspector = inspect(db.engine)
        assert feature_tables <= set(inspector.get_table_names())
        studio_columns = {
            "email_template": {"id", "category", "name", "subject", "headline", "message", "blocks_json", "created_at", "updated_at"},
            "email_draft": {
                "id", "name", "category", "subject", "headline", "message",
                "blocks_json", "product_ids_json", "account_ids_json", "order_id", "cart_id",
                "status", "created_by_id", "created_at", "updated_at",
            },
        }
        for table_name, required_columns in studio_columns.items():
            actual_columns = {column["name"] for column in inspector.get_columns(table_name)}
            assert required_columns <= actual_columns
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
    first = runner.invoke(args=_confirmed_customer_email_migration_args())
    second = runner.invoke(args=_confirmed_customer_email_migration_args())
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


def test_customer_email_builder_migration_preserves_legacy_template_and_allows_duplicates(tmp_path, monkeypatch):
    isolated_app, _feature_tables, _expected = _isolated_legacy_sqlite_app(tmp_path, monkeypatch)
    with isolated_app.app_context():
        from sqlalchemy import inspect, text
        db.session.execute(text("CREATE TABLE email_template (id INTEGER PRIMARY KEY, category VARCHAR(32) NOT NULL UNIQUE, subject VARCHAR(200) NOT NULL, headline VARCHAR(200) NOT NULL DEFAULT '', message TEXT NOT NULL DEFAULT '', created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"))
        db.session.execute(text("CREATE INDEX ix_email_template_category ON email_template (category)"))
        db.session.execute(text("INSERT INTO email_template (id, category, subject, headline, message, created_at, updated_at) VALUES (7, 'promotion', 'Old subject', 'Old headline', 'Old body', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
        db.session.commit()
        result = isolated_app.test_cli_runner().invoke(args=_confirmed_customer_email_migration_args())
        assert result.exit_code == 0, result.output
        assert db.session.execute(text("SELECT subject, message, name, blocks_json FROM email_template WHERE id = 7")).one() == ("Old subject", "Old body", "Saved template", "[]")
        db.session.execute(text("INSERT INTO email_template (category, name, subject, headline, message, blocks_json, created_at, updated_at) VALUES ('promotion', 'Second', 'Another', '', 'Body', '[]', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
        db.session.commit()
        assert db.session.execute(text("SELECT COUNT(*) FROM email_template WHERE category = 'promotion'")).scalar_one() == 2
        assert not any(tuple(row.get("column_names") or ()) == ("category",) for row in inspect(db.engine).get_unique_constraints("email_template"))


def test_customer_email_sqlite_migration_rejects_unsupported_template_columns_without_changes(tmp_path, monkeypatch):
    isolated_app, _feature_tables, _expected = _isolated_legacy_sqlite_app(tmp_path, monkeypatch)
    with isolated_app.app_context():
        from sqlalchemy import inspect, text

        db.session.execute(text(
            "CREATE TABLE email_template ("
            "id INTEGER PRIMARY KEY, category VARCHAR(32) NOT NULL UNIQUE, "
            "subject VARCHAR(200) NOT NULL, headline VARCHAR(200) NOT NULL DEFAULT '', "
            "message TEXT NOT NULL DEFAULT '', created_at DATETIME NOT NULL, "
            "updated_at DATETIME NOT NULL, custom_payload TEXT NOT NULL DEFAULT '')"
        ))
        db.session.execute(text(
            "CREATE INDEX ix_email_template_category ON email_template (category)"
        ))
        db.session.execute(text(
            "INSERT INTO email_template "
            "(id, category, subject, headline, message, created_at, updated_at, custom_payload) "
            "VALUES (9, 'saved', 'Subject', 'Headline', 'Body', CURRENT_TIMESTAMP, "
            "CURRENT_TIMESTAMP, 'keep-this-value')"
        ))
        db.session.commit()
        inspector = inspect(db.engine)
        before_tables = set(inspector.get_table_names())
        before_columns = [column["name"] for column in inspector.get_columns("email_template")]
        before_indexes = inspector.get_indexes("email_template")
        before_unique = inspector.get_unique_constraints("email_template")

        result = isolated_app.test_cli_runner().invoke(
            args=_confirmed_customer_email_migration_args()
        )

        assert result.exit_code != 0
        assert "unsupported column(s) would be lost: custom_payload" in result.output
        inspector = inspect(db.engine)
        assert set(inspector.get_table_names()) == before_tables
        assert [column["name"] for column in inspector.get_columns("email_template")] == before_columns
        assert inspector.get_indexes("email_template") == before_indexes
        assert inspector.get_unique_constraints("email_template") == before_unique
        assert db.session.execute(text(
            "SELECT subject, message, custom_payload FROM email_template WHERE id = 9"
        )).one() == ("Subject", "Body", "keep-this-value")


def test_customer_email_sqlite_migration_rolls_back_failed_ddl(tmp_path, monkeypatch):
    isolated_app, feature_tables, _expected = _isolated_legacy_sqlite_app(tmp_path, monkeypatch)
    original_create_all = db.metadata.create_all

    def fail_after_first_table(*, bind, tables, checkfirst=True):
        original_create_all(bind=bind, tables=tables[:1], checkfirst=checkfirst)
        raise RuntimeError("simulated DDL failure")

    with isolated_app.app_context():
        monkeypatch.setattr(db.metadata, "create_all", fail_after_first_table)
        result = isolated_app.test_cli_runner().invoke(args=_confirmed_customer_email_migration_args())
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
        result = isolated_app.test_cli_runner().invoke(args=_confirmed_customer_email_migration_args())
        assert result.exit_code != 0
        assert "customer/email table is incomplete" in result.output
        assert "missing columns" in result.output
        existing = set(inspect(db.engine).get_table_names()) & feature_tables
        assert existing == {"customer_account"}
        assert db.session.execute(text("SELECT email FROM customer_account WHERE id = 1")).scalar_one() == "keep@example.test"


def test_customer_email_migration_rejects_incomplete_email_studio_table_without_replacing_it(tmp_path, monkeypatch):
    isolated_app, _feature_tables, _expected = _isolated_legacy_sqlite_app(tmp_path, monkeypatch)
    with isolated_app.app_context():
        from sqlalchemy import inspect, text
        db.session.execute(text("CREATE TABLE email_template (id INTEGER PRIMARY KEY, category VARCHAR(32))"))
        db.session.execute(text("INSERT INTO email_template (id, category) VALUES (7, 'saved')"))
        db.session.commit()
        result = isolated_app.test_cli_runner().invoke(args=_confirmed_customer_email_migration_args())
        assert result.exit_code != 0
        assert "email_template (missing columns" in result.output
        inspector = inspect(db.engine)
        assert {column["name"] for column in inspector.get_columns("email_template")} == {"id", "category"}
        assert db.session.execute(text("SELECT category FROM email_template WHERE id = 7")).scalar_one() == "saved"
        assert "email_draft" not in inspector.get_table_names()


def test_customer_email_migration_rejects_unsupported_dialect(app, monkeypatch):
    monkeypatch.setattr(app_module, "database_dialect_name", lambda: "mysql")
    result = app.test_cli_runner().invoke(args=_confirmed_customer_email_migration_args())
    assert result.exit_code != 0
    assert "Unsupported database dialect" in result.output


def test_customer_email_migration_refuses_sqlite_on_render(app, monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setattr(app_module, "database_dialect_name", lambda: "sqlite")
    result = app.test_cli_runner().invoke(args=_confirmed_customer_email_migration_args())
    assert result.exit_code != 0
    assert "requires the configured PostgreSQL DATABASE_URL" in result.output


def test_customer_auth_forms_keep_csrf_protection(client, app):
    app.config["WTF_CSRF_ENABLED"] = True
    response = client.post("/account/email-code", data={"email": "csrf@example.com"})
    assert response.status_code == 400


def test_verification_code_page_has_six_inputs_and_requested_loader(client, app):
    login_response = client.get("/account/login")
    assert login_response.status_code == 200
    assert b"verification-code-loader.css" in login_response.data
    assert b"verification-code-loader.js" in login_response.data
    assert b"otp-code-ui-20261127-v1" in login_response.data
    assert login_response.data.count(b'data-code-loader') == 1
    assert b"Sending your verification code..." in login_response.data

    with app.app_context():
        account = CustomerAccount(email="verification-loader@example.test", display_name="Loader test")
        db.session.add(account)
        db.session.commit()
        account_id = account.id
    with client.session_transaction() as browser_session:
        browser_session["pending_customer_id"] = account_id
        browser_session["pending_customer_purpose"] = "verify_code"

    verify_response = client.get("/account/verify-code")
    assert verify_response.status_code == 200
    assert verify_response.data.count(b'data-code-loader') == 2
    assert b"Checking your verification code..." in verify_response.data
    assert b"Sending a new verification code..." in verify_response.data
    assert verify_response.data.count(b'class="qf-otp-input__digit"') == 6
    assert b'role="group" aria-label="6-digit verification code"' in verify_response.data
    assert b'data-otp-form' in verify_response.data
    assert verify_response.data.count(b'class="fity-otp-loader__dot"') == 10


def test_verify_code_accepts_digit_fields_and_shows_success_card(client, app):
    from werkzeug.datastructures import MultiDict

    code = "048291"
    with app.app_context():
        account = CustomerAccount(email="otp-success@example.test", display_name="OTP success")
        db.session.add(account)
        db.session.flush()
        token = CustomerToken(
            account_id=account.id,
            purpose="verify_code",
            token_hash=hashlib.sha256(code.encode()).hexdigest(),
            expires_at=datetime.utcnow() + timedelta(minutes=10),
        )
        db.session.add(token)
        db.session.commit()
        account_id = account.id
        token_id = token.id
    with client.session_transaction() as browser_session:
        browser_session["pending_customer_id"] = account_id
        browser_session["pending_customer_purpose"] = "verify_code"

    response = client.post(
        "/account/verify-code",
        data=MultiDict([("code_digit", digit) for digit in code]),
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"fity-otp-feedback--success" in response.data
    assert b"otp-feedback-20261127-v1" in response.data
    assert b"Your email is verified." in response.data
    with app.app_context():
        account = db.session.get(CustomerAccount, account_id)
        token = db.session.get(CustomerToken, token_id)
        assert account.email_verified_at is not None
        assert token.used_at is not None


@pytest.mark.parametrize("expired", [False, True])
def test_verify_code_wrong_or_expired_shows_error_card(client, app, expired):
    from datetime import timedelta

    code = "048291"
    with app.app_context():
        account = CustomerAccount(email=f"otp-error-{expired}@example.test", display_name="OTP error")
        db.session.add(account)
        db.session.flush()
        if expired:
            db.session.add(CustomerToken(
                account_id=account.id,
                purpose="verify_code",
                token_hash=hashlib.sha256(code.encode()).hexdigest(),
                expires_at=datetime.utcnow() - timedelta(seconds=1),
            ))
        db.session.commit()
        account_id = account.id
    with client.session_transaction() as browser_session:
        browser_session["pending_customer_id"] = account_id
        browser_session["pending_customer_purpose"] = "verify_code"

    response = client.post(
        "/account/verify-code",
        data={"code": code if expired else "000000"},
    )

    assert response.status_code == 200
    assert b"fity-otp-feedback--error" in response.data
    assert b"That code is incorrect, expired, or already used." in response.data


def test_checkout_requires_verified_account_and_links_order_to_it(client, app_context, app, monkeypatch):
    import app.services.customer_mail as customer_mail
    guest_get = client.get("/checkout", follow_redirects=False)
    assert guest_get.status_code == 302
    assert guest_get.headers["Location"].endswith("/account/login?next=/checkout")
    guest_post = client.post("/checkout", data={"cart_json": "[{\"product_id\":1,\"quantity\":1}]", "customer_name": "Guest", "fulfilment_method": "Collection"}, follow_redirects=False)
    assert guest_post.status_code == 302
    assert Order.query.count() == 0
    from datetime import datetime
    account = CustomerAccount(email="checkout-account@example.com", display_name="Checkout", password_hash=generate_password_hash("a-strong-test-password-123"), email_verified_at=datetime.utcnow())
    db.session.add(account)
    db.session.commit()
    accept_current_terms_for_test(account)
    _email_code_auth(client, account.email)
    app.config.update({
        "MAIL_PROVIDER": "brevo",
        "MAIL_FROM_ADDRESS": "orders@fitystore.top",
        "MAIL_FROM_NAME": "FityStores",
        "MAIL_FROM_VERIFIED": True,
        "BREVO_API_KEY": "test-brevo-api-key",
    })
    response = client.post("/checkout", data={"cart_json": "[{\"product_id\":1,\"quantity\":1}]", "customer_name": "Checkout", "fulfilment_method": "Collection"}, follow_redirects=True)
    assert response.status_code == 200
    order = Order.query.filter_by(customer_name="Checkout").one()
    assert CustomerOrderLink.query.filter_by(account_id=account.id, order_id=order.id).one()
    email = EmailMessage.query.filter_by(order_id=order.id, kind="order_received").one()
    assert email.status == "sent"
    assert email.provider_message_id == "test-provider-message-id"


def test_branded_auth_email_renders_simple_code_and_security_details(app_context):
    import app.services.customer_mail as customer_mail

    for kind in ("verify_code", "login_code"):
        html_body, text_body = customer_mail.render_branded_email(
            "Your FityStores sign-in code",
            ["Enter this code on FityStores to continue. Never share it with anyone."],
            kind=kind,
            security_code="048291",
        )

        assert html_body.count('class="email-code-value"') == 1
        assert ">048291</p>" in html_body
        assert "Your verification code" in html_body
        assert "This code expires 10 minutes after it was requested." in html_body
        assert "Never share it with anyone." in html_body
        assert "Select code to copy" not in html_body
        assert "email-code-digit" not in html_body
        assert "<script" not in html_body.lower()
        assert "onclick=" not in html_body.lower()
        assert "<button" not in html_body.lower()
        assert "Your one-time code:\n048291" in text_body

    for kind, expiry in (
        ("verify_email", "24 hours"),
        ("reset_password", "30 minutes"),
        ("marketing_confirm", "24 hours"),
    ):
        expiring_html, _ = customer_mail.render_branded_email(
            "Account update", ["Use the link below."], kind=kind
        )
        assert f"expires {expiry} after it was requested." in expiring_html
        assert ("Preference reminder" if kind == "marketing_confirm" else "Security reminder") in expiring_html


def test_order_received_email_renders_real_cart_and_secure_tracking_link(app, app_context):
    import app.services.customer_mail as customer_mail
    account = CustomerAccount(email="order-email@example.com", display_name="Email Buyer", email_verified_at=datetime.utcnow())
    product = Product.query.filter_by(slug="plain-socks").one()
    db.session.add(account)
    db.session.flush()
    image = ProductImage(product_id=product.id, filename="", storage_backend="cloudinary", cloudinary_public_id="catalog/socks", alt_text="Black and white socks", is_primary=True)
    product.images.append(image)
    order = Order(reference="FITY-EMAIL-CART-1", customer_name="Email Buyer", customer_phone="0700123456", fulfilment_method="Collection", status="Order received", payment_status="Awaiting payment", total_cents=43000, delivery_fee_cents=4000, discount_cents=1000)
    order.items.extend([
        OrderItem(product=product, product_name=product.name, quantity=2, price_cents=15000, subtotal_cents=30000, variant_label="Black · Size 8", sku="SOCK-8"),
        OrderItem(product_name="Legacy product", quantity=1, price_cents=10000, subtotal_cents=10000),
    ])
    db.session.add(order)
    db.session.flush()
    app.config.update(CLOUDINARY_CLOUD_NAME="fity-test", PUBLIC_BASE_URL="https://fitystore.top")
    html_body, text_body = customer_mail._render_order_received_email(order, account)
    assert "FITY-EMAIL-CART-1" in html_body
    assert "Select this reference to copy it for your records." in html_body
    assert "View order status" in html_body
    assert "https://res.cloudinary.com/fity-test/image/upload/catalog/socks" in html_body
    assert "Image unavailable" in html_body
    assert "Black · Size 8" in html_body
    assert "KSh 430" in html_body
    assert "Total order amount" in html_body
    assert "Total paid" not in html_body
    assert f"https://fitystore.top/track/{order.reference}/{order.tracking_token}" in html_body
    assert "The store will review the order and update its status." in html_body
    assert 'class="desktop-progress"' in html_body
    assert 'class="mobile-progress"' in html_body
    assert "background:#0B8F52" in html_body
    assert "background:#DCE3EA" in html_body
    assert "SKU: SOCK-8" in html_body
    assert "Unit price:" in html_body and "Quantity:" in html_body and "Line total:" in html_body
    assert "<built-in method" not in html_body
    assert "https://wa.me/254700000000?text=Hello%20FITY%2C%20I%20need%20help%20with%20order%20FITY-EMAIL-CART-1." in html_body
    assert "Collection is not ready yet" in html_body
    assert "Confirm the exact meeting spot" in html_body
    assert "M-Pesa payment number" in html_body
    assert "Legacy product" in text_body
    assert "Awaiting payment" in text_body

    order.payment_status = "Payment reported"
    reported_html, _ = customer_mail._render_order_received_email(order, account)
    assert "The seller still needs to verify it" in reported_html
    assert "Total paid" not in reported_html
    order.payment_status = "Paid"
    paid_html, _ = customer_mail._render_order_received_email(order, account)
    assert "Payment has been confirmed by the store" in paid_html
    assert "Total paid" in paid_html
    order.payment_status = "Not required"
    no_payment_html, _ = customer_mail._render_order_received_email(order, account)
    assert "No payment is required for this order" in no_payment_html

    settings = ShopSettings.query.first()
    settings.whatsapp_number = "abc123456789"
    db.session.commit()
    fallback_html, _ = customer_mail._render_order_received_email(order, account)
    assert "Need help? Contact us" in fallback_html
    assert 'href="https://fitystore.top/contact"' in fallback_html

    order.fulfilment_method = "Nearby delivery"
    order.instructions = "M-Pesa payer: private phone\nDelivery address: Othaya town"
    delivery_html, _ = customer_mail._render_order_received_email(order, account)
    assert "Delivery location: Othaya town" in delivery_html
    assert "M-Pesa payer" not in delivery_html
    assert "Delivery arrangements are awaiting confirmation" in delivery_html


def test_order_status_and_completion_review_notifications_are_idempotent(app_context):
    import app.services.customer_mail as customer_mail
    account = CustomerAccount(email="status-email@example.com", display_name="Status Buyer", email_verified_at=datetime.utcnow())
    order = Order(reference="FITY-STATUS-EMAIL", fulfilment_method="Collection", status="Ready for collection", payment_status="Paid")
    db.session.add_all([account, order])
    db.session.flush()
    db.session.add(CustomerOrderLink(account_id=account.id, order_id=order.id))
    db.session.commit()

    first = customer_mail.notify_order_event(order, "order_status", "order-status:history-1")
    second = customer_mail.notify_order_event(order, "order_status", "order-status:history-1")
    assert first.id == second.id
    assert first.kind == "ready_for_collection"
    assert EmailMessage.query.filter_by(idempotency_key="order-status:history-1").count() == 1

    assert customer_mail.notify_order_event(order, "review_request", "order-review-request:status-order") is None
    order.status = "Completed"
    review_email = customer_mail.notify_order_event(order, "review_request", "order-review-request:status-order")
    duplicate = customer_mail.notify_order_event(order, "review_request", "order-review-request:status-order")
    assert review_email.id == duplicate.id
    assert EmailMessage.query.filter_by(kind="review_request", order_id=order.id).count() == 1


@pytest.mark.parametrize(
    ("event_name", "payment_status", "order_status", "fulfilment_method", "expected_copy"),
    [
        ("payment_confirmed", "Paid", "Order received", "Collection", "Payment confirmed"),
        ("order_dispatched", "Paid", "Out for delivery", "Delivery", "out for delivery"),
    ],
)
def test_payment_and_shipping_transactional_events_dispatch_directly(
    app, app_context, monkeypatch, event_name, payment_status, order_status,
    fulfilment_method, expected_copy,
):
    import app.services.customer_mail as customer_mail

    sent = []
    app.config["MAIL_PROVIDER"] = "brevo"
    monkeypatch.setattr(customer_mail, "mail_configuration_status", lambda: (True, "configured"))
    monkeypatch.setattr(
        customer_mail, "_send_brevo",
        lambda *args: sent.append(args) or "lifecycle-provider-id",
    )
    account = CustomerAccount(
        email=f"{event_name}@example.test", display_name="Lifecycle Buyer",
        email_verified_at=datetime.utcnow(),
    )
    order = Order(
        reference=f"FITY-{event_name.upper()}", fulfilment_method=fulfilment_method,
        status=order_status, payment_status=payment_status, total_cents=1900,
    )
    db.session.add_all([account, order])
    db.session.flush()
    db.session.add(CustomerOrderLink(account_id=account.id, order_id=order.id))
    db.session.commit()

    message = customer_mail.notify_order_event(order, event_name, f"{event_name}:direct")
    duplicate = customer_mail.notify_order_event(order, event_name, f"{event_name}:direct")

    assert message.status == "sent"
    assert duplicate.id == message.id
    assert expected_copy.lower() in sent[0][3].lower()
    assert len(sent) == 1


@pytest.mark.parametrize(
    ("order_status", "fulfilment_method", "expected_kind", "expected_text"),
    [
        ("Preparing order", "Collection", "order_processing", "Your order is being prepared"),
        ("Ready for collection", "Collection", "ready_for_collection", "ready for collection"),
        ("Out for delivery", "Delivery", "order_dispatched", "out for delivery"),
        ("Cancelled", "Delivery", "order_cancellation", "cancelled"),
        ("Completed", "Collection", "order_completed", "completed"),
    ],
)
def test_order_status_workflows_are_distinct_transactional_notifications(app_context, order_status, fulfilment_method, expected_kind, expected_text):
    import app.services.customer_mail as customer_mail
    account = CustomerAccount(email=f"{order_status.lower().replace(' ', '-')}-workflow@example.com", display_name="Workflow Buyer", email_verified_at=datetime.utcnow())
    order = Order(reference=f"FITY-{order_status.upper().replace(' ', '-')}", fulfilment_method=fulfilment_method, status=order_status, payment_status="Paid")
    db.session.add_all([account, order])
    db.session.flush()
    db.session.add(CustomerOrderLink(account_id=account.id, order_id=order.id))
    db.session.commit()

    message = customer_mail.notify_order_event(order, "order_status", f"order-status:{order_status}:workflow")
    assert message is not None
    assert message.kind == expected_kind
    assert message.purpose == "transactional"
    body = (message.html_body or "") + "\n" + (message.text_body or "")
    assert expected_text.lower() in body.lower()
    if order_status == "Completed":
        assert "delivered" not in body.lower()


def test_order_status_workflow_skips_guest_and_unverified_customers(app_context):
    import app.services.customer_mail as customer_mail
    guest_order = Order(reference="FITY-GUEST-STATUS", fulfilment_method="Collection", status="Preparing order", payment_status="Paid")
    db.session.add(guest_order)
    db.session.commit()
    assert customer_mail.notify_order_event(guest_order, "order_status", "guest-order-status") is None

    verified_account = CustomerAccount(email="unverified-order@example.com", display_name="Pending Buyer", email_verified_at=None)
    linked_order = Order(reference="FITY-UNVERIFIED-STATUS", fulfilment_method="Collection", status="Preparing order", payment_status="Paid")
    db.session.add_all([verified_account, linked_order])
    db.session.flush()
    db.session.add(CustomerOrderLink(account_id=verified_account.id, order_id=linked_order.id))
    db.session.commit()
    assert customer_mail.notify_order_event(linked_order, "order_status", "unverified-order-status") is None


def test_admin_completion_transition_sends_one_review_request(client, app_context):
    account = CustomerAccount(email="completed-review@example.com", display_name="Completed Buyer", email_verified_at=datetime.utcnow())
    order = Order(reference="FITY-COMPLETED-REVIEW", fulfilment_method="Collection", status="Ready for collection", payment_status="Paid")
    db.session.add_all([account, order])
    db.session.flush()
    db.session.add(CustomerOrderLink(account_id=account.id, order_id=order.id))
    db.session.commit()

    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    first = client.post(f"/admin/orders/{order.id}", data={"status": "Completed", "payment_status": "Paid", "notes": ""})
    second = client.post(f"/admin/orders/{order.id}", data={"status": "Completed", "payment_status": "Paid", "notes": ""})
    assert first.status_code == 302
    assert second.status_code == 302
    assert EmailMessage.query.filter_by(kind="order_completed", order_id=order.id).count() == 1
    assert EmailMessage.query.filter_by(kind="review_request", order_id=order.id).count() == 1


APPROVED_ORDER_TRANSITIONS = {
    "Order received": {"Awaiting payment", "Payment confirmed", "Preparing order", "Cancelled"},
    "Awaiting payment": {"Payment confirmed", "Preparing order", "Cancelled"},
    "Payment confirmed": {"Preparing order", "Ready for collection", "Out for delivery", "Cancelled"},
    "Preparing order": {"Ready for collection", "Out for delivery", "Cancelled"},
    "Ready for collection": {"Completed", "Cancelled"},
    "Out for delivery": {"Completed", "Cancelled"},
    "Completed": set(),
    "Cancelled": set(),
}

PERMITTED_ADMIN_TRANSITIONS = [
    ("Order received", "Collection", "Awaiting payment", "Awaiting payment", "Awaiting payment", "order_status"),
    ("Order received", "Collection", "Payment confirmed", "Awaiting payment", "Paid", "order_status"),
    ("Order received", "Collection", "Preparing order", "Awaiting payment", "Awaiting payment", "order_processing"),
    ("Order received", "Nearby delivery", "Cancelled", "Awaiting payment", "Awaiting payment", "order_cancellation"),
    ("Awaiting payment", "Collection", "Payment confirmed", "Awaiting payment", "Paid", "order_status"),
    ("Awaiting payment", "Collection", "Preparing order", "Awaiting payment", "Awaiting payment", "order_processing"),
    ("Awaiting payment", "Nearby delivery", "Cancelled", "Awaiting payment", "Awaiting payment", "order_cancellation"),
    ("Payment confirmed", "Collection", "Preparing order", "Paid", "Paid", "order_processing"),
    ("Payment confirmed", "Collection", "Ready for collection", "Paid", "Paid", "ready_for_collection"),
    ("Payment confirmed", "Nearby delivery", "Out for delivery", "Paid", "Paid", "order_dispatched"),
    ("Payment confirmed", "Collection", "Cancelled", "Paid", "Paid", "order_cancellation"),
    ("Preparing order", "Collection", "Ready for collection", "Paid", "Paid", "ready_for_collection"),
    ("Preparing order", "Nearby delivery", "Out for delivery", "Paid", "Paid", "order_dispatched"),
    ("Preparing order", "Collection", "Cancelled", "Awaiting payment", "Awaiting payment", "order_cancellation"),
    ("Ready for collection", "Collection", "Completed", "Paid", "Paid", "order_completed"),
    ("Ready for collection", "Collection", "Cancelled", "Paid", "Paid", "order_cancellation"),
    ("Out for delivery", "Nearby delivery", "Completed", "Paid", "Paid", "order_completed"),
    ("Out for delivery", "Nearby delivery", "Cancelled", "Awaiting payment", "Awaiting payment", "order_cancellation"),
]


@pytest.mark.parametrize(
    (
        "starting_status", "fulfilment_method", "new_status",
        "current_payment_status", "submitted_payment_status", "email_kind",
    ),
    PERMITTED_ADMIN_TRANSITIONS,
)
def test_admin_order_status_transitions_queue_distinct_idempotent_emails(
    client, app_context, starting_status, fulfilment_method, new_status,
    current_payment_status, submitted_payment_status, email_kind,
):
    account = CustomerAccount(
        email=f"{email_kind}-route@example.com",
        display_name="Route Buyer",
        email_verified_at=datetime.utcnow(),
    )
    order = Order(
        reference=f"FITY-RT-{email_kind[:15]}",
        fulfilment_method=fulfilment_method,
        status=starting_status,
        payment_status=current_payment_status,
    )
    db.session.add_all([account, order])
    db.session.flush()
    db.session.add(CustomerOrderLink(account_id=account.id, order_id=order.id))
    db.session.commit()

    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    form = {"status": new_status, "payment_status": submitted_payment_status, "notes": ""}
    first = client.post(f"/admin/orders/{order.id}", data=form, follow_redirects=True)
    duplicate = client.post(f"/admin/orders/{order.id}", data=form, follow_redirects=True)

    assert first.status_code == duplicate.status_code == 200
    assert b"Customer status email is awaiting provider delivery or recovery" in first.data
    assert b"Customer status email is awaiting provider delivery or recovery" not in duplicate.data
    assert b"No status change was needed." in duplicate.data
    status_events = OrderStatusHistory.query.filter_by(
        order_id=order.id, field="status", new_value=new_status,
    ).all()
    assert len(status_events) == 1
    messages = EmailMessage.query.filter_by(order_id=order.id, kind=email_kind).all()
    assert len(messages) == 1
    assert messages[0].purpose == "transactional"
    assert messages[0].idempotency_key == f"{email_kind}:{status_events[0].id}"
    if new_status == "Completed":
        assert EmailMessage.query.filter_by(order_id=order.id, kind="review_request").count() == 1


@pytest.mark.parametrize(
    ("starting_status", "fulfilment_method", "new_status", "current_payment_status", "submitted_payment_status"),
    [
        ("Preparing order", "Collection", "Out for delivery", "Paid", "Paid"),
        ("Preparing order", "Nearby delivery", "Ready for collection", "Paid", "Paid"),
        ("Preparing order", "Collection", "Payment confirmed", "Paid", "Paid"),
        ("Cancelled", "Collection", "Preparing order", "Paid", "Paid"),
        ("Completed", "Nearby delivery", "Preparing order", "Paid", "Paid"),
        ("Payment confirmed", "Collection", "Ready for collection", "Awaiting payment", "Awaiting payment"),
        ("Payment confirmed", "Nearby delivery", "Out for delivery", "Payment reported", "Payment reported"),
    ],
)
def test_admin_rejects_inconsistent_or_invalid_order_status_transitions(
    client, app_context, starting_status, fulfilment_method, new_status,
    current_payment_status, submitted_payment_status,
):
    order = Order(
        reference=f"FITY-MISMATCH-{new_status.split()[0]}",
        fulfilment_method=fulfilment_method,
        status=starting_status,
        payment_status=current_payment_status,
    )
    db.session.add(order)
    db.session.commit()

    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    response = client.post(
        f"/admin/orders/{order.id}",
        data={"status": new_status, "payment_status": submitted_payment_status, "notes": ""},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert order.status == starting_status
    assert OrderStatusHistory.query.filter_by(order_id=order.id, field="status").count() == 0
    assert EmailMessage.query.filter_by(order_id=order.id).count() == 0


ALL_ORDER_STATUSES = tuple(APPROVED_ORDER_TRANSITIONS)
REJECTED_GRAPH_TRANSITIONS = tuple(
    (starting_status, new_status)
    for starting_status in ALL_ORDER_STATUSES
    for new_status in ALL_ORDER_STATUSES
    if new_status != starting_status
    and new_status not in APPROVED_ORDER_TRANSITIONS[starting_status]
) + (("Order received", "Delivered"),)


def test_permitted_transition_cases_cover_the_complete_approved_matrix():
    tested_edges = {
        (starting_status, new_status)
        for starting_status, _method, new_status, _old_payment, _new_payment, _kind
        in PERMITTED_ADMIN_TRANSITIONS
    }
    expected_edges = {
        (starting_status, new_status)
        for starting_status, next_statuses in APPROVED_ORDER_TRANSITIONS.items()
        for new_status in next_statuses
    }
    assert tested_edges == expected_edges


@pytest.mark.parametrize(("starting_status", "new_status"), REJECTED_GRAPH_TRANSITIONS)
def test_service_rejects_every_unlisted_order_status_transition(starting_status, new_status):
    from types import SimpleNamespace
    from app.services.orders import validate_order_status_transition

    order = SimpleNamespace(
        status=starting_status,
        fulfilment_method="Collection",
        payment_status="Paid",
    )
    with pytest.raises(ValueError):
        validate_order_status_transition(order, new_status)


@pytest.mark.parametrize(
    ("starting_status", "fulfilment_method", "new_status"),
    [
        ("Payment confirmed", "Nearby delivery", "Ready for collection"),
        ("Preparing order", "Nearby delivery", "Ready for collection"),
        ("Payment confirmed", "Collection", "Out for delivery"),
        ("Preparing order", "Collection", "Out for delivery"),
    ],
)
def test_service_rejects_every_cross_fulfilment_stage(
    starting_status, fulfilment_method, new_status,
):
    from types import SimpleNamespace
    from app.services.orders import validate_order_status_transition

    order = SimpleNamespace(
        status=starting_status,
        fulfilment_method=fulfilment_method,
        payment_status="Paid",
    )
    with pytest.raises(ValueError, match="Only .* orders can be marked"):
        validate_order_status_transition(order, new_status)


UNPAID_FULFILMENT_EDGES = [
    ("Payment confirmed", "Collection", "Ready for collection"),
    ("Payment confirmed", "Nearby delivery", "Out for delivery"),
    ("Preparing order", "Collection", "Ready for collection"),
    ("Preparing order", "Nearby delivery", "Out for delivery"),
    ("Ready for collection", "Collection", "Completed"),
    ("Out for delivery", "Nearby delivery", "Completed"),
]
UNCONFIRMED_PAYMENT_STATUSES = [
    "Awaiting payment", "Payment reported", "Processing", "Failed", "Cancelled",
    "Unknown", "Reconciliation required", "Refund requested", "Refund processing",
    "Partially refunded", "Refunded", "Refund failed",
]
UNPAID_TRANSITION_CASES = [
    (*edge, payment_status)
    for edge in UNPAID_FULFILMENT_EDGES
    for payment_status in UNCONFIRMED_PAYMENT_STATUSES
]


@pytest.mark.parametrize(
    ("starting_status", "fulfilment_method", "new_status", "payment_status"),
    UNPAID_TRANSITION_CASES,
)
def test_service_rejects_fulfilment_and_completion_without_paid_status(
    starting_status, fulfilment_method, new_status, payment_status,
):
    from types import SimpleNamespace
    from app.services.orders import validate_order_status_transition

    order = SimpleNamespace(
        status=starting_status,
        fulfilment_method=fulfilment_method,
        payment_status=payment_status,
    )
    with pytest.raises(ValueError, match="Confirm payment"):
        validate_order_status_transition(order, new_status, payment_status=payment_status)


def test_admin_order_form_only_offers_permitted_statuses(client, app_context):
    collection_order = Order(
        reference="FITY-OPTIONS-COLLECTION",
        fulfilment_method="Collection",
        status="Payment confirmed",
        payment_status="Paid",
    )
    delivery_order = Order(
        reference="FITY-OPTIONS-DELIVERY",
        fulfilment_method="Nearby delivery",
        status="Payment confirmed",
        payment_status="Paid",
    )
    unpaid_order = Order(
        reference="FITY-OPTIONS-UNPAID",
        fulfilment_method="Collection",
        status="Payment confirmed",
        payment_status="Awaiting payment",
    )
    db.session.add_all([collection_order, delivery_order, unpaid_order])
    db.session.commit()
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})

    response = client.get("/admin/orders")
    assert response.status_code == 200
    html = response.data.decode()
    expected_options = {
        collection_order.reference: {"Payment confirmed", "Preparing order", "Ready for collection", "Cancelled"},
        delivery_order.reference: {"Payment confirmed", "Preparing order", "Out for delivery", "Cancelled"},
        unpaid_order.reference: {"Payment confirmed", "Preparing order", "Cancelled"},
    }
    for reference, expected in expected_options.items():
        position = html.index(f"<h2>{reference}</h2>")
        card_start = html.rfind('<article class="order-card">', 0, position)
        card_end = html.index("</article>", position)
        card = html[card_start:card_end]
        status_select = card.split('<select name="status">', 1)[1].split("</select>", 1)[0]
        options = set(re.findall(r"<option[^>]*>(.*?)</option>", status_select))
        assert options == expected


def test_distinct_status_history_events_queue_distinct_notifications(client, app_context):
    account = CustomerAccount(
        email="distinct-status-events@example.com",
        display_name="Event Buyer",
        email_verified_at=datetime.utcnow(),
    )
    order = Order(
        reference="FITY-DISTINCT-EVENTS",
        fulfilment_method="Collection",
        status="Payment confirmed",
        payment_status="Paid",
    )
    db.session.add_all([account, order])
    db.session.flush()
    db.session.add(CustomerOrderLink(account_id=account.id, order_id=order.id))
    db.session.commit()
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})

    client.post(f"/admin/orders/{order.id}", data={"status": "Preparing order", "payment_status": "Paid", "notes": ""})
    client.post(f"/admin/orders/{order.id}", data={"status": "Ready for collection", "payment_status": "Paid", "notes": ""})

    events = OrderStatusHistory.query.filter(
        OrderStatusHistory.order_id == order.id,
        OrderStatusHistory.field == "status",
        OrderStatusHistory.new_value.in_(["Preparing order", "Ready for collection"]),
    ).order_by(OrderStatusHistory.id).all()
    messages = EmailMessage.query.filter_by(order_id=order.id).order_by(EmailMessage.id).all()
    assert [message.kind for message in messages] == ["order_processing", "ready_for_collection"]
    assert [message.idempotency_key for message in messages] == [
        f"order_processing:{events[0].id}",
        f"ready_for_collection:{events[1].id}",
    ]


@pytest.mark.parametrize("queue_result", ["exception", "none", "unsuccessful"])
def test_admin_reports_status_queue_failure_without_reversing_order_update(
    client, app_context, monkeypatch, queue_result,
):
    from types import SimpleNamespace
    import app.services.customer_mail as customer_mail

    account = CustomerAccount(
        email=f"route-queue-{queue_result}@example.com",
        display_name="Queue Buyer",
        email_verified_at=datetime.utcnow(),
    )
    order = Order(
        reference=f"FITY-ROUTE-QUEUE-{queue_result[:4].upper()}",
        fulfilment_method="Collection",
        status="Payment confirmed",
        payment_status="Paid",
    )
    db.session.add_all([account, order])
    db.session.flush()
    db.session.add(CustomerOrderLink(account_id=account.id, order_id=order.id))
    db.session.commit()

    def fake_queue(*args, **kwargs):
        if queue_result == "exception":
            raise RuntimeError("queue unavailable")
        if queue_result == "unsuccessful":
            return SimpleNamespace(status="failed")
        return None

    monkeypatch.setattr(customer_mail, "queue_branded_email", fake_queue)
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    response = client.post(
        f"/admin/orders/{order.id}",
        data={"status": "Preparing order", "payment_status": "Paid", "notes": ""},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Order updated." in response.data
    assert b"customer status email failed" in response.data
    assert b"Customer status email queued" not in response.data
    assert order.status == "Preparing order"
    assert EmailMessage.query.filter_by(order_id=order.id).count() == 0


def test_repeated_completion_deducts_each_product_and_variant_once(client, app_context):
    product = Product.query.filter_by(slug="plain-socks").first()
    product.stock_quantity = 10
    product.reserved_quantity = 3
    variant = ProductVariant(
        product=product,
        size="M",
        color="Navy",
        stock_quantity=8,
        reserved_quantity=4,
    )
    order = Order(
        reference="FITY-COMPLETE-STOCK",
        fulfilment_method="Collection",
        status="Ready for collection",
        payment_status="Paid",
    )
    order.items.extend([
        OrderItem(product=product, product_name=product.name, quantity=1, price_cents=100, subtotal_cents=100),
        OrderItem(product=product, product_name=product.name, quantity=2, price_cents=100, subtotal_cents=200),
        OrderItem(product=product, variant=variant, product_name=product.name, quantity=4, price_cents=100, subtotal_cents=400),
    ])
    db.session.add(order)
    db.session.commit()
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})

    for _ in range(2):
        response = client.post(
            f"/admin/orders/{order.id}",
            data={"status": "Completed", "payment_status": "Paid", "notes": ""},
        )
        assert response.status_code == 302

    db.session.expire_all()
    product = Product.query.filter_by(slug="plain-socks").first()
    variant = ProductVariant.query.filter_by(product_id=product.id, size="M", color="Navy").first()
    assert product.stock_quantity == 7
    assert product.reserved_quantity == 0
    assert variant.stock_quantity == 4
    assert variant.reserved_quantity == 0
    assert OrderStatusHistory.query.filter_by(order_id=order.id, field="status", new_value="Completed").count() == 1


def test_repeated_cancellation_releases_reservations_without_deducting_stock(client, app_context):
    product = Product.query.filter_by(slug="plain-socks").first()
    product.stock_quantity = 10
    product.reserved_quantity = 3
    variant = ProductVariant(
        product=product,
        size="L",
        color="Green",
        stock_quantity=8,
        reserved_quantity=2,
    )
    order = Order(
        reference="FITY-CANCEL-STOCK",
        fulfilment_method="Collection",
        status="Preparing order",
        payment_status="Paid",
    )
    order.items.extend([
        OrderItem(product=product, product_name=product.name, quantity=3, price_cents=100, subtotal_cents=300),
        OrderItem(product=product, variant=variant, product_name=product.name, quantity=2, price_cents=100, subtotal_cents=200),
    ])
    db.session.add(order)
    db.session.commit()
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})

    for _ in range(2):
        response = client.post(
            f"/admin/orders/{order.id}",
            data={"status": "Cancelled", "payment_status": "Paid", "notes": ""},
        )
        assert response.status_code == 302

    db.session.expire_all()
    product = Product.query.filter_by(slug="plain-socks").first()
    variant = ProductVariant.query.filter_by(product_id=product.id, size="L", color="Green").first()
    assert product.stock_quantity == 10
    assert product.reserved_quantity == 0
    assert variant.stock_quantity == 8
    assert variant.reserved_quantity == 0
    assert OrderStatusHistory.query.filter_by(order_id=order.id, field="status", new_value="Cancelled").count() == 1


def test_failed_completion_commit_rolls_back_inventory_and_retry_deducts_once(
    client, app_context, monkeypatch,
):
    product = Product.query.filter_by(slug="plain-socks").first()
    product.stock_quantity = 5
    product.reserved_quantity = 2
    order = Order(
        reference="FITY-COMPLETE-RETRY",
        fulfilment_method="Collection",
        status="Ready for collection",
        payment_status="Paid",
    )
    order.items.append(OrderItem(
        product=product,
        product_name=product.name,
        quantity=2,
        price_cents=100,
        subtotal_cents=200,
    ))
    db.session.add(order)
    db.session.commit()
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})

    original_commit = Session.commit
    state = {"fail": True}

    def fail_first_commit(session):
        if state["fail"]:
            state["fail"] = False
            session.flush()
            raise SQLAlchemyError("simulated commit failure")
        return original_commit(session)

    monkeypatch.setattr(Session, "commit", fail_first_commit)
    failed = client.post(
        f"/admin/orders/{order.id}",
        data={"status": "Completed", "payment_status": "Paid", "notes": ""},
        follow_redirects=True,
    )
    assert b"order update could not be confirmed" in failed.data
    db.session.expire_all()
    assert Order.query.get(order.id).status == "Ready for collection"
    assert Product.query.filter_by(slug="plain-socks").first().stock_quantity == 5
    assert Product.query.filter_by(slug="plain-socks").first().reserved_quantity == 2

    retry = client.post(
        f"/admin/orders/{order.id}",
        data={"status": "Completed", "payment_status": "Paid", "notes": ""},
        follow_redirects=True,
    )
    assert b"Order updated." in retry.data
    db.session.expire_all()
    assert Order.query.get(order.id).status == "Completed"
    assert Product.query.filter_by(slug="plain-socks").first().stock_quantity == 3
    assert Product.query.filter_by(slug="plain-socks").first().reserved_quantity == 0
    assert OrderStatusHistory.query.filter_by(order_id=order.id, field="status", new_value="Completed").count() == 1


@pytest.mark.parametrize("link_account", [False, True])
def test_admin_order_status_transition_skips_guest_and_unverified_accounts(
    client, app_context, link_account,
):
    order = Order(
        reference=f"FITY-STATUS-{'UNVERIFIED' if link_account else 'GUEST'}",
        fulfilment_method="Collection",
        status="Payment confirmed",
        payment_status="Paid",
    )
    db.session.add(order)
    if link_account:
        account = CustomerAccount(
            email="unverified-route@example.com",
            display_name="Unverified Buyer",
            email_verified_at=None,
        )
        db.session.add(account)
        db.session.flush()
        db.session.add(CustomerOrderLink(account_id=account.id, order_id=order.id))
    db.session.commit()

    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    response = client.post(
        f"/admin/orders/{order.id}",
        data={"status": "Preparing order", "payment_status": "Paid", "notes": ""},
    )

    assert response.status_code == 302
    assert order.status == "Preparing order"
    assert EmailMessage.query.filter_by(order_id=order.id).count() == 0


@pytest.mark.parametrize("queue_result", [None, "failed"], ids=["no-message", "failed-message"])
def test_order_status_notification_does_not_report_unsuccessful_queue_result(
    app_context, monkeypatch, queue_result,
):
    from types import SimpleNamespace
    import app.services.customer_mail as customer_mail

    account = CustomerAccount(
        email=f"queue-result-{queue_result or 'none'}@example.com",
        display_name="Queue Buyer",
        email_verified_at=datetime.utcnow(),
    )
    order = Order(
        reference=f"FITY-QUEUE-{queue_result or 'NONE'}",
        fulfilment_method="Collection",
        status="Preparing order",
        payment_status="Paid",
    )
    db.session.add_all([account, order])
    db.session.flush()
    db.session.add(CustomerOrderLink(account_id=account.id, order_id=order.id))
    db.session.commit()

    message = None if queue_result is None else SimpleNamespace(status=queue_result)
    monkeypatch.setattr(customer_mail, "queue_branded_email", lambda *args, **kwargs: message)

    assert customer_mail.notify_order_event(order, "order_status", "failed-queue-event") is None
    assert EmailMessage.query.filter_by(order_id=order.id).count() == 0


def test_order_status_notification_propagates_queue_exception(app_context, monkeypatch):
    import app.services.customer_mail as customer_mail

    account = CustomerAccount(
        email="queue-exception@example.com",
        display_name="Queue Buyer",
        email_verified_at=datetime.utcnow(),
    )
    order = Order(
        reference="FITY-QUEUE-EXCEPTION",
        fulfilment_method="Collection",
        status="Preparing order",
        payment_status="Paid",
    )
    db.session.add_all([account, order])
    db.session.flush()
    db.session.add(CustomerOrderLink(account_id=account.id, order_id=order.id))
    db.session.commit()

    def fail_queue(*args, **kwargs):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(customer_mail, "queue_branded_email", fail_queue)

    with pytest.raises(RuntimeError, match="queue unavailable"):
        customer_mail.notify_order_event(order, "order_status", "queue-exception-event")
    assert EmailMessage.query.filter_by(order_id=order.id).count() == 0


@pytest.mark.parametrize(
    ("order_status", "fulfilment_method", "event_name"),
    [
        ("Out for delivery", "Collection", "order_status"),
        ("Ready for collection", "Nearby delivery", "order_status"),
        ("Preparing order", "Collection", "order_dispatched"),
    ],
)
def test_order_status_notification_refuses_mismatched_fulfilment_or_status(
    app_context, order_status, fulfilment_method, event_name,
):
    import app.services.customer_mail as customer_mail

    account = CustomerAccount(
        email=f"mismatched-{order_status.lower().replace(' ', '-')}@example.com",
        display_name="Mismatch Buyer",
        email_verified_at=datetime.utcnow(),
    )
    order = Order(
        reference=f"FITY-MISMATCH-{order_status.split()[0]}",
        fulfilment_method=fulfilment_method,
        status=order_status,
        payment_status="Paid",
    )
    db.session.add_all([account, order])
    db.session.flush()
    db.session.add(CustomerOrderLink(account_id=account.id, order_id=order.id))
    db.session.commit()

    assert customer_mail.notify_order_event(order, event_name, "mismatched-status-event") is None
    assert EmailMessage.query.filter_by(order_id=order.id).count() == 0


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
    assert customer_mail.notify_order_event(order, "payment_confirmed", "verified-payment-event").status == "queued"
    assert EmailMessage.query.filter_by(idempotency_key="verified-payment-event").one().status == "queued"


def test_email_code_delivery_reports_provider_acceptance_without_claiming_inbox_delivery(
    client, app, monkeypatch,
):
    import app.services.customer_mail as customer_mail
    app.config.update(
        MAIL_PROVIDER="smtp", MAIL_FROM_VERIFIED=True,
        MAIL_FROM_ADDRESS="verified@example.test", MAIL_SERVER="smtp.example.test",
        MAIL_USE_TLS=True, MAIL_USE_SSL=False, PUBLIC_BASE_URL="https://fitystore.top",
    )
    sent = []
    monkeypatch.setattr(
        customer_mail, "_send_smtp",
        lambda recipient, subject, html, text: sent.append((subject, text)),
    )
    response = _register_customer(
        client, email="mail-status@example.com", provider="smtp",
        sender=lambda recipient, subject, html, text: sent.append((subject, text)),
    )
    assert response.status_code == 200
    assert b"Your email is verified" in response.data
    assert b"The provider accepted your verification code" in response.data
    assert b"Inbox delivery is not confirmed" in response.data
    assert len(sent) == 2
    code_bodies = [body for subject, body in sent if "verification code" in subject.lower()]
    assert code_bodies and "123456" in code_bodies[0]
    with app.app_context():
        row = EmailMessage.query.filter_by(kind="verify_code").one()
        assert row.status == "sent"
        welcome = EmailMessage.query.filter_by(kind="welcome").one()
        assert welcome.status == "sent"


def test_resend_verification_reports_not_configured_and_rate_limits(client, app):
    app.config["MAIL_PROVIDER"] = ""
    with app.app_context():
        account = CustomerAccount(email="resend@example.com", display_name="Resend", password_hash=generate_password_hash("a-strong-test-password-123"))
        db.session.add(account)
        db.session.flush()
        account_id = account.id
        db.session.commit()
    with client.session_transaction() as session:
        session["pending_customer_id"] = account_id
        session["pending_customer_purpose"] = "verify_code"
    first = client.post("/account/verification/resend", follow_redirects=True)
    assert b"in the delivery queue" in first.data
    client.post("/account/verification/resend", follow_redirects=True)
    client.post("/account/verification/resend", follow_redirects=True)
    limited = client.post("/account/verification/resend", follow_redirects=True)
    assert b"Too many verification requests" in limited.data
    with app.app_context():
        assert EmailMessage.query.filter_by(kind="verify_email", status="queued").count() == 3

def test_queued_resend_supersedes_previous_token_even_when_delivery_fails(app, monkeypatch, app_context):
    import hashlib
    import app.customer.routes as customer_routes
    import app.services.customer_mail as customer_mail
    from datetime import timedelta
    account = CustomerAccount(email="preserve@example.com", display_name="Preserve")
    db.session.add(account)
    db.session.flush()
    old_raw = "previous-delivered-token"
    old = CustomerToken(
        account_id=account.id, purpose="verify_email",
        token_hash=hashlib.sha256(old_raw.encode()).hexdigest(),
        expires_at=datetime.utcnow() + timedelta(hours=1),
    )
    db.session.add(old)
    db.session.commit()
    app.config.update(
        MAIL_PROVIDER="smtp", MAIL_FROM_VERIFIED=True,
        MAIL_FROM_ADDRESS="verified@example.test", MAIL_SERVER="smtp.example.test",
        MAIL_USE_TLS=True, MAIL_USE_SSL=False,
    )
    monkeypatch.setattr(customer_mail, "_send_smtp", lambda *args: (_ for _ in ()).throw(TimeoutError("simulated provider outage")))
    result = customer_routes._issue_and_send(account, "verify_email")
    assert result.status == "failed"
    assert result.error_category == "ambiguous_outcome"
    assert old.used_at is not None
    new_row = CustomerToken.query.filter_by(account_id=account.id, purpose="verify_email").filter(CustomerToken.id != old.id).one()
    assert new_row.used_at is None
    customer_mail.process_email_queue(limit=5)
    db.session.refresh(old)
    db.session.refresh(new_row)
    assert old.used_at is not None
    assert new_row.used_at is None
    newest = CustomerToken.query.filter_by(account_id=account.id, purpose="verify_email").order_by(CustomerToken.id.desc()).first()
    assert newest.used_at is None


def test_smtp_timeout_is_recorded_without_exception_details(app, monkeypatch, app_context):
    import socket
    import app.services.customer_mail as customer_mail
    app.config.update(
        MAIL_PROVIDER="smtp", MAIL_FROM_VERIFIED=True,
        MAIL_FROM_ADDRESS="verified@example.test", MAIL_SERVER="smtp.example.test",
        MAIL_USE_TLS=True, MAIL_USE_SSL=False,
    )
    monkeypatch.setattr(customer_mail, "_send_smtp", lambda *args: (_ for _ in ()).throw(socket.timeout("private timeout detail")))
    result = customer_mail.queue_branded_email("person@example.test", "Test", "test", "smtp-timeout-test", "Hello", ["Test body"])
    assert result.status == "queued"
    customer_mail.process_email_queue(limit=5)
    db.session.refresh(result)
    assert result.status == "failed"
    assert result.failure_reason == "SMTP send failed (TimeoutError)."
    assert "private timeout detail" not in result.failure_reason


def test_smtp_success_and_ssl_configuration_validation(app, monkeypatch, app_context):
    import app.services.customer_mail as customer_mail
    app.config.update(
        MAIL_PROVIDER="smtp", MAIL_FROM_VERIFIED=True,
        MAIL_FROM_ADDRESS="verified@example.test", MAIL_SERVER="smtp.example.test",
        MAIL_USE_TLS=True, MAIL_USE_SSL=False,
    )
    monkeypatch.setattr(customer_mail, "_send_smtp", lambda *args: None)
    result = customer_mail.queue_branded_email("person@example.test", "Test", "test", "smtp-success-test", "Hello", ["Test body"])
    assert result.status == "queued"
    customer_mail.process_email_queue(limit=5)
    db.session.refresh(result)
    assert result.status == "sent"
    app.config.update(MAIL_USE_TLS=True, MAIL_USE_SSL=True)
    assert customer_mail.mail_configuration_status() == (False, "Choose either STARTTLS or implicit TLS, not both.")
    app.config.update(MAIL_USE_TLS=False, MAIL_USE_SSL=False)
    assert customer_mail.mail_configuration_status()[0] is False


def test_google_id_token_audience_rejection_is_logged_without_token(app, monkeypatch, caplog, app_context):
    import sys
    import types
    import app.customer.routes as customer_routes
    app.config["GOOGLE_CLIENT_ID"] = "expected-web-client-id"
    google = types.ModuleType("google"); google.__path__ = []
    auth = types.ModuleType("google.auth"); auth.__path__ = []
    transport = types.ModuleType("google.auth.transport"); transport.__path__ = []
    requests = types.ModuleType("google.auth.transport.requests")
    requests.Request = lambda: object()
    oauth2 = types.ModuleType("google.oauth2"); oauth2.__path__ = []
    id_token = types.ModuleType("google.oauth2.id_token")
    def reject_wrong_audience(token, request, audience):
        assert token == "do-not-log-this-token"
        assert audience == "expected-web-client-id"
        raise ValueError("sensitive token contents")
    id_token.verify_oauth2_token = reject_wrong_audience
    transport.requests = requests
    oauth2.id_token = id_token
    modules = {
        "google": google, "google.auth": auth, "google.auth.transport": transport,
        "google.auth.transport.requests": requests, "google.oauth2": oauth2,
        "google.oauth2.id_token": id_token,
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    assert customer_routes.verify_google_credential("do-not-log-this-token") is None
    assert "ValueError" in caplog.text
    assert "do-not-log-this-token" not in caplog.text
    assert "sensitive token contents" not in caplog.text


def test_google_login_redirect_uri_uses_configured_canonical_host(client, app):
    app.config["GOOGLE_CLIENT_ID"] = "web-client-id"
    app.config["PUBLIC_BASE_URL"] = "https://fitystore.top"
    response = client.get("/account/login")
    assert b'data-login_uri="https://fitystore.top/account/google"' in response.data


def test_admin_account_controls_do_not_manually_verify_email(client, app, app_context):
    account = CustomerAccount(email="admin-view@example.com", display_name="Admin View")
    db.session.add(account)
    db.session.commit()
    assert client.post("/admin/login", data={"username": "owner", "password": "secret123"}).status_code == 302
    page = client.get("/admin/email")
    assert b"email not verified" in page.data
    assert client.post(f"/admin/email/customers/{account.id}/active", data={"active": "no"}).status_code == 302
    db.session.refresh(account)
    assert account.active is False
    assert account.email_verified_at is None


def _configure_brevo_test(app, api_key="test-brevo-key"):
    app.config.update(
        MAIL_PROVIDER="brevo", BREVO_API_KEY=api_key,
        MAIL_FROM_VERIFIED=True, MAIL_FROM_ADDRESS="verified@example.test",
        MAIL_FROM_NAME="Test Shop", MAIL_REPLY_TO="replies@example.test",
    )


def test_resend_success_still_uses_https_provider(app, monkeypatch, app_context):
    import json
    import app.services.customer_mail as customer_mail

    app.config.update(
        MAIL_PROVIDER="resend", RESEND_API_KEY="test-resend-key",
        MAIL_FROM_VERIFIED=True, MAIL_FROM_ADDRESS="verified@example.test",
        MAIL_FROM_NAME="Test Shop", MAIL_REPLY_TO="replies@example.test",
    )
    captured = {}
    class Response:
        status = 200
        def read(self):
            return b'{"id":"resend-provider-message-id"}'
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
    def fake_urlopen(request, timeout):
        captured.update(request=request, timeout=timeout)
        return Response()
    monkeypatch.setattr(customer_mail.urllib.request, "urlopen", fake_urlopen)

    result = customer_mail.queue_email(
        "buyer@example.test", "Order ready", "order_update", "resend-success",
        html_body="<p>Ready</p>", text_body="Ready",
    )

    assert result.status == "sent"
    assert captured
    counts = customer_mail.process_email_queue(limit=5)
    assert counts["sent"] == 0

    request = captured["request"]
    payload = json.loads(request.data.decode("utf-8"))
    assert result.status == "sent"
    assert result.provider_message_id == "resend-provider-message-id"
    assert request.full_url == "https://api.resend.com/emails"
    assert request.get_header("Authorization") == "Bearer test-resend-key"
    assert captured["timeout"] == 15
    assert payload["to"] == ["buyer@example.test"]
    assert payload["html"] == "<p>Ready</p>"
    assert payload["text"] == "Ready"
    assert payload["reply_to"] == "replies@example.test"

def test_brevo_success_posts_expected_payload_over_https(app, monkeypatch, app_context):
    import json
    import app.services.customer_mail as customer_mail

    _configure_brevo_test(app)
    captured = {}

    class Response:
        status = 201
        def read(self):
            return b'{"messageId":"brevo-message-id"}'
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(customer_mail.urllib.request, "urlopen", fake_urlopen)
    result = customer_mail.queue_email(
        "buyer@example.test", "Order ready", "order_update", "brevo-success",
        html_body="<p>Ready</p>", text_body="Ready",
    )

    assert result.status == "sent"
    assert captured
    assert customer_mail.process_email_queue(limit=5)["sent"] == 0

    request = captured["request"]
    payload = json.loads(request.data.decode("utf-8"))
    assert result.status == "sent"
    assert result.provider_message_id == "brevo-message-id"
    assert request.full_url == "https://api.brevo.com/v3/smtp/email"
    assert request.method == "POST"
    assert request.get_header("Api-key") == "test-brevo-key"
    assert request.get_header("Content-type") == "application/json"
    assert captured["timeout"] == 15
    assert payload == {
        "sender": {"name": "Test Shop", "email": "verified@example.test"},
        "to": [{"email": "buyer@example.test"}],
        "subject": "Order ready",
        "htmlContent": "<p>Ready</p>",
        "textContent": "Ready",
        "replyTo": {"email": "replies@example.test"},
    }


def test_brevo_timeout_is_recorded_without_exception_details(app, monkeypatch, app_context, caplog):
    import socket
    import app.services.customer_mail as customer_mail

    _configure_brevo_test(app)
    def timeout(*_args, **_kwargs):
        raise socket.timeout("private timeout and code detail")
    monkeypatch.setattr(customer_mail.urllib.request, "urlopen", timeout)
    result = customer_mail.queue_email(
        "buyer@example.test", "Test", "test", "brevo-timeout",
        html_body="<p>private verification code 123456</p>", text_body="private body",
    )

    assert result.status == "queued"
    assert not hasattr(result, "provider_message_id") or result.provider_message_id is None
    customer_mail.process_email_queue(limit=5)
    db.session.refresh(result)

    assert result.status == "failed"
    assert result.failure_reason == "Brevo send failed (TimeoutError)."
    assert "private timeout" not in caplog.text
    assert "123456" not in caplog.text
    assert "test-brevo-key" not in caplog.text


def test_brevo_http_error_is_generic_and_does_not_expose_response(app, monkeypatch, app_context, caplog):
    import io
    import urllib.error
    import app.services.customer_mail as customer_mail

    _configure_brevo_test(app)
    sensitive_response = b'{"message":"private token test-brevo-key and verification code 654321"}'
    def fail(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "https://api.brevo.com/v3/smtp/email", 401, "Unauthorized", {},
            io.BytesIO(sensitive_response),
        )
    monkeypatch.setattr(customer_mail.urllib.request, "urlopen", fail)
    result = customer_mail.queue_email(
        "buyer@example.test", "Test", "test", "brevo-http-error",
        html_body="<p>private email body</p>", text_body="private text",
    )

    assert result.status == "queued"
    customer_mail.process_email_queue(limit=5)
    db.session.refresh(result)

    assert result.status == "failed"
    assert result.failure_reason == "Brevo HTTP 401."
    assert "test-brevo-key" not in result.failure_reason + caplog.text
    assert "654321" not in result.failure_reason + caplog.text
    assert "private token" not in result.failure_reason + caplog.text
    assert result.failure_reason not in sensitive_response.decode()


def test_brevo_invalid_sender_configuration_does_not_call_api(app, monkeypatch, app_context):
    import app.services.customer_mail as customer_mail

    _configure_brevo_test(app)
    app.config["MAIL_FROM_VERIFIED"] = False
    calls = []
    monkeypatch.setattr(customer_mail.urllib.request, "urlopen", lambda *a, **k: calls.append(True))
    result = customer_mail.queue_email(
        "buyer@example.test", "Test", "test", "brevo-invalid-config",
        html_body="<p>Test</p>", text_body="Test",
    )

    assert result.status == "queued"
    assert calls == []


def test_brevo_missing_api_key_is_not_configured(app, monkeypatch, app_context):
    import app.services.customer_mail as customer_mail

    _configure_brevo_test(app, api_key="")
    calls = []
    monkeypatch.setattr(customer_mail.urllib.request, "urlopen", lambda *a, **k: calls.append(True))
    result = customer_mail.queue_email(
        "buyer@example.test", "Test", "test", "brevo-missing-key",
        html_body="<p>Test</p>", text_body="Test",
    )

    assert result.status == "queued"
    assert calls == []


def test_brevo_webhook_requires_secret_tracks_delivery_and_deduplicates_bounce(client, app, app_context):
    from app.models import CustomerConsentEvent
    account = CustomerAccount(
        email="webhook-owner@example.com", display_name="Webhook owner",
        email_verified_at=datetime.utcnow(), marketing_status="subscribed",
        marketing_consent_at=datetime.utcnow(),
    )
    db.session.add(account)
    db.session.flush()
    bounced = EmailMessage(
        recipient=account.email, account_id=account.id, kind="marketing_weekly_picks",
        subject="Picks", idempotency_key="webhook-bounce", status="sent",
        provider_message_id="brevo-bounce-id", sent_at=datetime.utcnow(),
    )
    delivered = EmailMessage(
        recipient=account.email, account_id=account.id, kind="marketing_daily_goodies",
        subject="Goodies", idempotency_key="webhook-delivery", status="sent",
        provider_message_id="brevo-delivery-id", sent_at=datetime.utcnow(),
    )
    db.session.add_all([bounced, delivered])
    db.session.commit()
    app.config["BREVO_WEBHOOK_TOKEN"] = "test-shared-hook-token"
    body = {"event": "hardBounce", "email": account.email, "message-id": "brevo-bounce-id"}
    denied = client.post("/webhooks/brevo/email-events", json=body, headers={"X-Fity-Brevo-Token": "wrong"})
    assert denied.status_code == 403
    assert db.session.get(CustomerAccount, account.id).marketing_status == "subscribed"
    headers = {"X-Fity-Brevo-Token": "test-shared-hook-token"}
    first = client.post("/webhooks/brevo/email-events", json=body, headers=headers)
    second = client.post("/webhooks/brevo/email-events", json=body, headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.get_json()["suppressed"] == 1
    assert second.get_json()["suppressed"] == 0
    db.session.refresh(bounced)
    assert bounced.status == "bounced"
    assert db.session.get(CustomerAccount, account.id).marketing_status == "suppressed"
    assert CustomerConsentEvent.query.filter_by(account_id=account.id, method="brevo_webhook").count() == 1
    delivered_response = client.post(
        "/webhooks/brevo/email-events",
        json={"event": "delivered", "email": account.email, "message-id": "brevo-delivery-id"},
        headers=headers,
    )
    assert delivered_response.status_code == 200
    db.session.refresh(delivered)
    assert delivered.status == "delivered"
    from app.services.marketing_automation import _recently_marketed
    assert _recently_marketed(account.id, datetime.utcnow())


def test_brevo_webhook_canonical_duplicate_ignores_webhook_id(client, app, app_context):
    from app.models import MarketingSuppressionCase, MarketingSuppressionEvent
    account = CustomerAccount(email="canonical-hook@example.test", display_name="Webhook", email_verified_at=datetime.utcnow(), marketing_status="subscribed", marketing_consent_at=datetime.utcnow())
    db.session.add(account)
    db.session.commit()
    app.config["BREVO_WEBHOOK_TOKEN"] = "test-shared-hook-token"
    headers = {"X-Fity-Brevo-Token": "test-shared-hook-token"}
    first = client.post("/webhooks/brevo/email-events", json={
        "event": "unsubscribed", "email": account.email, "description": "preference changed", "ts_event": 1731528000, "id": 21,
    }, headers=headers)
    replay = client.post("/webhooks/brevo/email-events", json={
        "description": "preference changed", "id": 99, "ts_event": 1731528000, "email": account.email, "event": "unsubscribed",
    }, headers=headers)
    assert first.status_code == replay.status_code == 200
    assert MarketingSuppressionEvent.query.filter_by(account_id=account.id).count() == 1
    assert MarketingSuppressionCase.query.filter_by(account_id=account.id, cleared_at=None).count() == 1


def test_brevo_webhook_quarantines_event_without_sufficient_identity(client, app, app_context):
    from app.models import MarketingSuppressionCase
    account = CustomerAccount(email="unidentifiable-hook@example.test", display_name="Webhook", email_verified_at=datetime.utcnow(), marketing_status="subscribed", marketing_consent_at=datetime.utcnow())
    db.session.add(account)
    db.session.commit()
    app.config["BREVO_WEBHOOK_TOKEN"] = "test-shared-hook-token"
    response = client.post("/webhooks/brevo/email-events", json={
        "event": "unsubscribed", "email": account.email, "id": 21,
    }, headers={"X-Fity-Brevo-Token": "test-shared-hook-token"})
    assert response.status_code == 422
    db.session.refresh(account)
    assert account.marketing_status == "subscribed"
    assert MarketingSuppressionCase.query.filter_by(account_id=account.id).count() == 0


def test_registered_customer_list_excludes_anonymized_accounts(client, app, app_context):
    active = CustomerAccount(
        email="active-customer@example.test",
        display_name="Active Customer",
        active=True,
    )
    removed = CustomerAccount(
        email="removed-customer-2@redacted.invalid",
        display_name="Deleted customer",
        active=False,
    )
    db.session.add_all([active, removed])
    db.session.commit()

    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    response = client.get("/admin/email")

    assert b"active-customer@example.test" in response.data
    assert b"removed-customer-2@redacted.invalid" not in response.data
    assert b"Registered customers" in response.data
    assert b"Anonymized records available for permanent erasure" in response.data
    assert f"Record {removed.id}".encode() in response.data


def test_complete_data_export_skips_optional_missing_mapped_tables(app, app_context):
    from sqlalchemy import inspect, text
    from app.admin.routes import _collect_admin_data_records

    with db.engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS home_category_thumbnail"))

    records = _collect_admin_data_records()

    assert "home_category_thumbnail" not in records
    assert "category" in records
    assert "product" in records
    assert not inspect(db.engine).has_table("home_category_thumbnail")


def test_email_payload_retention_is_dry_run_bounded_and_preserves_active_states(app, app_context):
    from datetime import timedelta
    from app.services.email_retention import cleanup_email_payloads
    now = datetime.utcnow()
    def message(key, status, **values):
        row = EmailMessage(
            recipient="retention@example.test", kind=values.pop("kind", "order_received"),
            subject="Test", idempotency_key=key, status=status,
            html_body="<p>private payload</p>", text_body="private payload",
            created_at=values.pop("created_at", now - timedelta(days=60)),
            updated_at=values.pop("updated_at", now - timedelta(days=60)), **values,
        )
        db.session.add(row)
        return row
    sent = message("retention-sent", "sent", sent_at=now - timedelta(days=60))
    expired_auth = message("retention-auth", "queued", kind="verify_code", created_at=now - timedelta(minutes=11), updated_at=now - timedelta(minutes=11))
    fresh_auth = message("retention-fresh-auth", "queued", kind="verify_code", created_at=now - timedelta(minutes=9), updated_at=now - timedelta(minutes=9))
    active = message("retention-active", "sending", claimed_at=now, claim_token="active-claim")
    retryable = message("retention-retryable", "failed", error_category="retryable_provider", retry_count=1, next_attempt_at=now + timedelta(minutes=5))
    ambiguous = message("retention-ambiguous", "failed", error_category="ambiguous_outcome", updated_at=now)
    db.session.commit()

    preview = cleanup_email_payloads(retention_days=30, limit=3, dry_run=True, now=now)
    assert preview["total_payloads"] == 3
    assert preview["dry_run"] is True
    assert EmailMessage.query.filter_by(id=sent.id).one().html_body is not None
    applied = cleanup_email_payloads(retention_days=30, limit=3, dry_run=False, now=now)
    assert applied["total_payloads"] == 3
    db.session.expire_all()
    assert EmailMessage.query.filter_by(id=sent.id).one().status == "sent"
    assert EmailMessage.query.filter_by(id=sent.id).one().html_body is None
    assert EmailMessage.query.filter_by(id=expired_auth.id).one().status == "failed"
    assert EmailMessage.query.filter_by(id=fresh_auth.id).one().html_body is not None
    assert EmailMessage.query.filter_by(id=active.id).one().status == "sending"
    assert EmailMessage.query.filter_by(id=active.id).one().html_body is not None
    assert EmailMessage.query.filter_by(id=retryable.id).one().html_body is not None
    assert EmailMessage.query.filter_by(id=ambiguous.id).one().status == "failed"
    assert EmailMessage.query.filter_by(id=ambiguous.id).one().html_body is None
    rerun = cleanup_email_payloads(retention_days=30, limit=3, dry_run=False, now=now)
    assert rerun["total_payloads"] == 0


def test_email_payload_retention_covers_terminal_statuses_and_boundary(app, app_context):
    from datetime import timedelta
    from app.services.email_retention import cleanup_email_payloads
    now = datetime.utcnow().replace(microsecond=0)
    records = []
    for index, status in enumerate(("delivered", "bounced", "complained", "suppressed")):
        records.append(EmailMessage(
            recipient="retention-status@example.test", kind="order_received", subject="Test",
            idempotency_key=f"retention-status-{index}", status=status,
            html_body="private", text_body="private", created_at=now - timedelta(days=31),
            updated_at=now - timedelta(days=31),
        ))
    boundary = EmailMessage(
        recipient="retention-boundary@example.test", kind="order_received", subject="Test",
        idempotency_key="retention-boundary", status="sent", html_body="private", text_body="private",
        created_at=now - timedelta(days=30), updated_at=now - timedelta(days=30),
    )
    inside_boundary = EmailMessage(
        recipient="retention-fresh-boundary@example.test", kind="order_received", subject="Test",
        idempotency_key="retention-fresh-boundary", status="sent", html_body="private", text_body="private",
        created_at=now - timedelta(days=30) + timedelta(seconds=1),
        updated_at=now - timedelta(days=30) + timedelta(seconds=1),
    )
    db.session.add_all([*records, boundary, inside_boundary])
    db.session.commit()
    result = cleanup_email_payloads(retention_days=30, limit=500, dry_run=False, now=now)
    assert result["terminal_payloads"] == 5
    db.session.expire_all()
    assert all(db.session.get(EmailMessage, row.id).html_body is None for row in records)
    assert db.session.get(EmailMessage, boundary.id).html_body is None
    assert db.session.get(EmailMessage, inside_boundary.id).html_body == "private"


def test_queue_health_and_retention_cli_are_read_only_or_dry_run_by_default(app, app_context):
    from app.services.customer_mail import email_queue_health
    health_data = email_queue_health()
    assert {"scheduled", "retrying", "retry_attempts", "failed_last_24h", "stale_claims_30m"} <= set(health_data)
    result = app.test_cli_runner().invoke(args=["cleanup-email-payloads", "--limit", "1"])
    assert result.exit_code == 0
    assert '"dry_run": true' in result.output
    health = app.test_cli_runner().invoke(args=["email-queue-health"])
    assert health.exit_code == 0
    assert '"event": "email_queue_health"' in health.output


def test_queue_cli_reports_unconfigured_sender_as_nonzero_without_sending(app, app_context, monkeypatch):
    from app.services import customer_mail
    monkeypatch.setattr(customer_mail, "mail_configuration_status", lambda: (False, "not configured"))
    result = app.test_cli_runner().invoke(args=[
        "process-email-queue", "--limit", "1", "--confirmation", "RUN EMAIL QUEUE",
    ])
    assert result.exit_code != 0
    assert "Email provider is not configured" in result.output


@pytest.mark.parametrize("confirmation", [None, "not the phrase"])
def test_queue_cli_requires_explicit_confirmation_before_processing(
    app, app_context, monkeypatch, confirmation,
):
    from app.services import customer_mail

    def unexpected_processing(**kwargs):
        pytest.fail("The queue worker must not run without the exact confirmation.")

    monkeypatch.setattr(customer_mail, "process_email_queue", unexpected_processing)
    args = ["process-email-queue", "--limit", "1"]
    if confirmation is not None:
        args.extend(["--confirmation", confirmation])
    result = app.test_cli_runner().invoke(args=args)

    assert result.exit_code != 0
    assert "requires --confirmation 'RUN EMAIL QUEUE'" in result.output


def test_queue_worker_honors_graceful_stop_between_messages(app, app_context, monkeypatch):
    from app.services import customer_mail
    now = datetime.utcnow()
    rows = [EmailMessage(recipient=f"worker-{i}@example.test", kind="order_received", subject="Order", idempotency_key=f"worker-stop-{i}", status="queued", html_body="<p>ok</p>", text_body="ok", created_at=now, updated_at=now) for i in range(2)]
    db.session.add_all(rows)
    db.session.commit()
    monkeypatch.setattr(customer_mail, "mail_configuration_status", lambda: (True, "configured"))
    stop = {"requested": False}
    def dispatched(_row):
        stop["requested"] = True
        return True
    monkeypatch.setattr(customer_mail, "deliver_email", dispatched)
    result = customer_mail.process_email_queue(limit=2, should_stop=lambda: stop["requested"])
    assert result["claimed"] == 1
    assert result["stopped"] == 1
    assert EmailMessage.query.filter(EmailMessage.id.in_([row.id for row in rows]), EmailMessage.status == "queued").count() == 2


def test_customer_cart_snapshot_requires_verified_account_and_tracks_episode(client, app, app_context):
    from datetime import datetime
    guest=app.test_client()
    assert guest.post("/api/cart/snapshot",json={"items":[{"product_id":1,"quantity":1}]}).status_code==401
    account=CustomerAccount(email="cart-owner@example.com",display_name="Cart Owner",password_hash=generate_password_hash("a-strong-test-password-123"),email_verified_at=datetime.utcnow(),marketing_status="subscribed",marketing_consent_at=datetime.utcnow())
    db.session.add(account);db.session.commit()
    accept_current_terms_for_test(account)
    _email_code_auth(client, account.email)
    response=client.post("/api/cart/snapshot",json={"items":[{"product_id":1,"quantity":2}]})
    assert response.status_code==200 and response.get_json()["ok"]
    cart=CustomerCart.query.filter_by(account_id=account.id).one()
    assert '"quantity":2' in cart.items_json
    first_episode=cart.episode_key
    assert client.post("/api/cart/snapshot",json={"action":"checkout"}).status_code==200
    db.session.refresh(cart)
    assert cart.checkout_until is not None
    assert client.post("/api/cart/snapshot",json={"action":"clear"}).status_code==200
    db.session.refresh(cart)
    assert cart.items_json=="[]" and cart.episode_key!=first_episode


def test_marketing_outbox_suppresses_without_current_consent(app, app_context):
    from app.services.customer_mail import queue_email
    account=CustomerAccount(email="not-consented@example.com",display_name="No Consent",email_verified_at=datetime.utcnow(),marketing_status="none")
    db.session.add(account);db.session.commit()
    row=queue_email(account.email,"A promotion","marketing_daily_goodies","test-marketing-suppression",account_id=account.id,html_body="<p>Sale</p>",text_body="Sale")
    assert row.status=="suppressed"
    assert row.sent_at is None


def test_scheduled_automation_is_paused_by_default(app, app_context):
    from app.models import MarketingAutomationSettings
    from app.services.marketing_automation import run_marketing_automation
    assert MarketingAutomationSettings.query.get(1) is None
    assert run_marketing_automation()["status"]=="paused"


def test_marketing_email_renders_absolute_product_image_and_link(app, app_context):
    from app.services.marketing_automation import _render_products_email
    account=CustomerAccount(email="preview@example.com",display_name="Test Shopper")
    product=Product.query.filter_by(slug="plain-socks").one()
    html,text=_render_products_email(account,"Today's picks","Fity Goodies","Fresh picks", [product], "https://fitystore.top/account/unsubscribe/test")
    assert "https://fitystore.top/product/plain-socks" in html
    assert "https://fitystore.top/static/images/mainLogo-160.png" in html
    assert "Plain Socks" in html and "Plain Socks" in text
    assert "unsubscribe/test" in html
    assert 'name="color-scheme" content="light only"' in html


def test_promotion_studio_email_preserves_light_brand_in_dark_mode(app, app_context):
    from app.services.email_studio import render_studio_message
    _subject, html, _text = render_studio_message(
        "promotion", "Offer", "Special picks", "A note for you", {},
        shop_url="https://fitystore.top/shop", logo_url="https://fitystore.top/logo.png",
        hero_image_url="https://fitystore.top/hero.png", whatsapp_url="https://wa.me/254700000000",
    )
    assert 'name="color-scheme" content="light only"' in html
    assert "prefers-color-scheme:dark" in html
    assert "#f0f2f8" in html and "#182033" in html


def test_weekly_popular_ranking_uses_only_paid_completed_sales(app, app_context):
    from app.services.marketing_automation import _ranked_products
    product=Product.query.filter_by(slug="plain-socks").one()
    pending=Order(reference="PENDING-SALE",fulfilment_method="Collection",status="Order received",payment_status="Awaiting payment")
    completed=Order(reference="COMPLETED-SALE",fulfilment_method="Collection",status="Completed",payment_status="Paid")
    db.session.add_all([pending,completed]);db.session.flush()
    db.session.add_all([
        OrderItem(order_id=pending.id,product_id=product.id,product_name=product.name,quantity=99,price_cents=product.price_cents,subtotal_cents=product.price_cents*99),
        OrderItem(order_id=completed.id,product_id=product.id,product_name=product.name,quantity=1,price_cents=product.price_cents,subtotal_cents=product.price_cents),
    ])
    db.session.commit()
    assert _ranked_products(5)[0].id==product.id


def test_manual_email_page_uses_real_linked_orders_and_admin_auth(client, app, app_context):
    from datetime import datetime
    from app.models import CustomerOrderLink
    assert client.get("/admin/email").status_code in {302, 401}
    account = CustomerAccount(email="manual-order@example.test", display_name="Manual Customer", email_verified_at=datetime.utcnow(), marketing_status="subscribed")
    order = Order(reference="REAL-ORDER-101", customer_name="Manual Customer", fulfilment_method="Collection", status="Processing")
    db.session.add_all([account, order]); db.session.flush()
    db.session.add(CustomerOrderLink(account_id=account.id, order_id=order.id)); db.session.commit()
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    response = client.get("/admin/email?q=REAL-ORDER-101")
    assert response.status_code == 200
    assert b"REAL-ORDER-101" in response.data
    assert b"Processing" in response.data
    assert b"Promotional Campaign Workflow" in response.data
    assert b"select eligible recipients" not in response.data.lower()


def test_manual_email_uses_brevo_products_and_idempotent_send(client, app, app_context, monkeypatch):
    import app.services.customer_mail as customer_mail
    sent = []
    monkeypatch.setattr(customer_mail, "_send_brevo", lambda *args: sent.append(args))
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})

    response = client.post("/admin/email/send-manual", data={
        "send_key": "d7c71045a5f2441cb7de09f22f4db250",
        "subject": "Real product update", "body": "A promotional update",
    }, follow_redirects=True)

    assert response.status_code == 200
    assert b"Legacy manual promotional sends are paused" in response.data
    assert EmailMessage.query.filter(EmailMessage.idempotency_key.like("admin-manual:%")).count() == 0
    assert sent == []


def test_manual_email_retry_reuses_idempotency_key_after_explicit_provider_throttle(client, app, app_context, monkeypatch):
    import app.services.customer_mail as customer_mail
    attempts = {"count": 0}

    def provider_must_not_be_called(*_args):
        attempts["count"] += 1
        raise AssertionError("legacy marketing route must not call the provider")

    monkeypatch.setattr(customer_mail, "_send_brevo", provider_must_not_be_called)
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    payload = {"send_key": "eaa613c35a6e4d828552cc42203e8193", "subject": "Store update", "body": "A product update."}

    first = client.post("/admin/email/send-manual", data=payload, follow_redirects=True)
    customer_mail.process_email_queue(limit=5)
    second = client.post("/admin/email/send-manual", data=payload, follow_redirects=True)
    customer_mail.process_email_queue(limit=5)

    assert first.status_code == second.status_code == 200
    assert attempts["count"] == 0
    assert EmailMessage.query.filter(EmailMessage.idempotency_key.like("admin-manual:%")).count() == 0


@pytest.mark.parametrize("status_code", [408, 500, 501, 502, 503, 504, 599])
def test_provider_server_error_is_ambiguous_and_never_automatically_retried(app, app_context, monkeypatch, status_code):
    import urllib.error
    import app.services.customer_mail as customer_mail

    _configure_brevo_test(app)
    attempts = {"count": 0}

    def provider_accepted_then_gateway_failed(*_args):
        attempts["count"] += 1
        raise urllib.error.HTTPError("https://provider.invalid", status_code, "gateway failure", {}, None)

    monkeypatch.setattr(customer_mail, "_send_brevo", provider_accepted_then_gateway_failed)
    message = customer_mail.queue_email(
        "ambiguous@example.test", "Test", "test", f"ambiguous-provider-{status_code}",
        html_body="<p>Test body</p>", text_body="Test body",
    )

    customer_mail.process_email_queue(limit=5)
    db.session.refresh(message)
    assert attempts["count"] == 1
    assert message.status == "failed"
    assert message.error_category == "ambiguous_outcome"
    assert message.next_attempt_at is None
    assert message.html_body is None and message.text_body is None

    customer_mail.process_email_queue(limit=5)
    db.session.refresh(message)
    assert attempts["count"] == 1
    assert message.status == "failed"


def test_transient_smtp_rejection_retries_but_permanent_smtp_rejection_does_not(app, app_context, monkeypatch):
    from datetime import datetime, timedelta
    import smtplib
    import app.services.customer_mail as customer_mail

    app.config.update(
        MAIL_PROVIDER="smtp", MAIL_FROM_VERIFIED=True,
        MAIL_FROM_ADDRESS="verified@example.test", MAIL_SERVER="smtp.example.test",
        MAIL_USE_TLS=True, MAIL_USE_SSL=False,
    )
    attempts = {"count": 0}

    def transient_rejection(*_args):
        attempts["count"] += 1
        raise smtplib.SMTPDataError(451, b"temporary refusal")

    monkeypatch.setattr(customer_mail, "_send_smtp", transient_rejection)
    message = customer_mail.queue_email(
        "smtp-transient@example.test", "Test", "test", "smtp-transient-retry",
        html_body="<p>Test body</p>", text_body="Test body",
    )
    customer_mail.process_email_queue(limit=5)
    db.session.refresh(message)
    assert attempts["count"] == 1
    assert message.error_category == "retryable_provider"
    assert message.next_attempt_at is not None

    permanent_attempts = {"count": 0}
    def permanent_rejection(*_args):
        permanent_attempts["count"] += 1
        raise smtplib.SMTPDataError(550, b"permanent refusal")
    monkeypatch.setattr(customer_mail, "_send_smtp", permanent_rejection)
    message.next_attempt_at = datetime.utcnow() - timedelta(seconds=1)
    db.session.commit()
    customer_mail.process_email_queue(limit=5)
    db.session.refresh(message)
    assert message.retry_count == 2
    assert message.error_category == "permanent_provider"
    assert message.next_attempt_at is None
    assert permanent_attempts["count"] == 1

    customer_mail.process_email_queue(limit=5)
    db.session.refresh(message)
    assert permanent_attempts["count"] == 1
    assert message.status == "failed"


def test_manual_bulk_send_requires_confirmation_and_only_sends_eligible_accounts(client, app, app_context, monkeypatch):
    import app.services.customer_mail as customer_mail
    sent = []
    monkeypatch.setattr(customer_mail, "_send_brevo", lambda *args: sent.append(args))
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    response = client.post("/admin/email/send-manual", data={
        "send_key": "76c2a76846a04bdbac49a15954045bf2",
        "subject": "Store news", "body": "A plain update",
    }, follow_redirects=True)

    assert response.status_code == 200
    customer_mail.process_email_queue(limit=5)
    assert b"Legacy manual promotional sends are paused" in response.data
    assert EmailMessage.query.filter(EmailMessage.idempotency_key.like("admin-manual:%")).count() == 0
    assert sent == []


def test_manual_email_provider_failure_is_reported_and_recorded(client, app, app_context, monkeypatch):
    import app.services.customer_mail as customer_mail
    def fail_send(*_args):
        raise AssertionError("legacy marketing route must not call the provider")
    monkeypatch.setattr(customer_mail, "_send_brevo", fail_send)
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    response = client.post("/admin/email/send-manual", data={
        "send_key": "c3b8fb7731ec41f4b442ed38ecf1b4dd",
        "subject": "Manual note", "body": "Message",
    }, follow_redirects=True)
    assert b"Legacy manual promotional sends are paused" in response.data
    assert EmailMessage.query.filter(EmailMessage.idempotency_key.like("admin-manual:%")).count() == 0


def test_legacy_individual_email_route_never_queues_even_with_incomplete_schema(client, app, app_context, monkeypatch):
    import app.services.customer_mail as customer_mail
    account = CustomerAccount(email="partial-schema@example.test", display_name="Partial Schema", email_verified_at=datetime.utcnow())
    db.session.add(account)
    db.session.commit()
    account_id = account.id
    db.session.remove()
    with db.engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE email_message DROP COLUMN retry_count")
    sent = []
    monkeypatch.setattr(customer_mail, "queue_branded_email", lambda *args, **kwargs: sent.append(args))
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})

    response = client.post(
        f"/admin/email/customers/{account_id}/message",
        data={"subject": "Service update", "body": "Please contact the store.", "confirm_send": "yes"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"no message was queued" in response.data
    assert sent == []


def test_legacy_manual_marketing_route_never_queues_with_incomplete_schema(client, app, app_context, monkeypatch):
    import app.services.customer_mail as customer_mail
    account = CustomerAccount(email="token-schema@example.test", display_name="Token Schema", email_verified_at=datetime.utcnow(), marketing_status="subscribed")
    db.session.add(account)
    db.session.commit()
    account_id = account.id
    db.session.remove()
    with db.engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE customer_token DROP COLUMN used_at")
    sent = []
    monkeypatch.setattr(customer_mail, "_send_brevo", lambda *args, **kwargs: sent.append(args))
    _configure_brevo_test(app)
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})

    response = client.post(
        "/admin/email/send-manual",
        data={"send_key": "e3aef5d5c7d7485697796bfa33d8cc5e", "account_ids": [str(account_id)], "subject": "Store update", "body": "A product update."},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Legacy manual promotional sends are paused" in response.data
    assert sent == []


def test_brevo_webhook_returns_unavailable_when_email_schema_is_incomplete(client, app, app_context):
    EmailCampaignRecipient.__table__.drop(db.engine)
    app.config["BREVO_WEBHOOK_TOKEN"] = "test-shared-hook-token"

    response = client.post(
        "/webhooks/brevo/email-events",
        json={"event": "hardBounce", "email": "person@example.test"},
        headers={"X-Fity-Brevo-Token": "test-shared-hook-token"},
    )

    assert response.status_code == 503
    assert response.get_json() == {"ok": False, "error": "email schema unavailable"}


def test_automation_page_is_disabled_without_accessing_automation_tables(client, app, app_context):
    from app.models import MarketingAutomationSettings, MarketingCampaignRun
    MarketingCampaignRun.__table__.drop(db.engine)
    MarketingAutomationSettings.__table__.drop(db.engine)
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    response = client.get("/admin/email/automation")
    assert response.status_code == 200
    assert b"Automation is disabled" in response.data
    assert b"manual customer email centre" in response.data.lower()


def test_email_studio_templates_and_drafts_persist_without_sending(client, app, app_context, monkeypatch):
    import app.services.customer_mail as customer_mail
    sent = []
    monkeypatch.setattr(customer_mail, "_send_brevo", lambda *args: sent.append(args))
    account = CustomerAccount(email="studio-draft@example.test", display_name="Draft Customer", email_verified_at=datetime.utcnow())
    db.session.add(account); db.session.commit()
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    template_response = client.post("/admin/email/studio", data={
        "category": "customer_service", "account_id": str(account.id),
        "subject": "A note for {{ customer_name }}", "headline": "Hello {{ customer_name }}",
        "message": "Saved reusable content", "action": "save_template",
    }, follow_redirects=True)
    assert template_response.status_code == 200
    saved = EmailTemplate.query.filter_by(category="customer_service").one()
    assert saved.subject == "A note for {{ customer_name }}"
    assert sent == []
    loaded = client.get(f"/admin/email/studio?category=customer_service&account_id={account.id}")
    assert b"saved template" in loaded.data
    assert b'value="A note for {{ customer_name }}"' in loaded.data
    assert b"Saved reusable content" in loaded.data

    draft_response = client.post("/admin/email/studio", data={
        "category": "customer_service", "account_id": str(account.id),
        "subject": "One-off note", "headline": "A draft", "message": "Not sent",
        "draft_name": "Customer follow-up", "action": "save_draft",
    }, follow_redirects=True)
    assert draft_response.status_code == 200
    draft = EmailDraft.query.filter_by(name="Customer follow-up").one()
    assert draft.category == "customer_service"
    assert draft.subject == "One-off note"
    assert "preview_account_id" in draft.account_ids_json
    assert b"Editing draft: Customer follow-up" in draft_response.data
    assert sent == []


def test_email_studio_preview_is_personalized_escaped_and_does_not_send(client, app, app_context, monkeypatch):
    import app.services.customer_mail as customer_mail
    sent = []
    monkeypatch.setattr(customer_mail, "_send_brevo", lambda *args: sent.append(args))
    account = CustomerAccount(email="studio-preview@example.test", display_name="<script>Customer</script>", email_verified_at=datetime.utcnow())
    db.session.add(account); db.session.commit()
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    response = client.post("/admin/email/studio", data={
        "category": "customer_service", "account_id": str(account.id),
        "subject": "Hello {{ customer_name }}", "headline": "Welcome {{ customer_name }}",
        "message": "This is a preview.", "action": "preview",
    })
    assert response.status_code == 200
    assert b"Preview" in response.data
    assert b"&lt;script&gt;Customer&lt;/script&gt;" in response.data
    assert b'<iframe title="Email preview" sandbox' in response.data
    assert b'<iframe id="email-live-preview-frame" title="Rendered email preview" sandbox' in response.data
    assert sent == []


def test_email_studio_saved_customer_service_content_obeys_marketing_consent(client, app, app_context, monkeypatch):
    import app.services.customer_mail as customer_mail
    _configure_brevo_test(app)
    app.config["EMAIL_MARKETING_CUSTOM_ENABLED"] = True
    sent = []
    monkeypatch.setattr(
        customer_mail, "_send_brevo",
        lambda recipient, subject, html, text: sent.append((recipient, subject, html, text)) or "saved-template-id",
    )
    account = CustomerAccount(
        email="saved-template-recipient@example.test", display_name="Saved Recipient",
        email_verified_at=datetime.utcnow(), marketing_status="subscribed", marketing_consent_at=datetime.utcnow(),
    )
    db.session.add(account)
    db.session.add(EmailTemplate(
        category="customer_service", subject="Service for {{ customer_name }}",
        headline="Saved greeting", message="Saved template body for {{ customer_name }}.",
    ))
    db.session.commit()
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    payload = {
        "category": "customer_service", "account_id": str(account.id),
        "subject": "Service for {{ customer_name }}", "headline": "Saved greeting",
        "message": "Saved template body for {{ customer_name }}.",
    }
    preview = client.post("/admin/email/studio", data={**payload, "action": "preview"})
    assert preview.status_code == 200, [line for line in preview.get_data(as_text=True).splitlines() if 'flash ' in line]
    assert b"flash error" not in preview.data, [line for line in preview.get_data(as_text=True).splitlines() if 'flash ' in line]
    result = client.post("/admin/email/studio", data={
        **payload, "action": "send", "confirm_send": "SEND 1",
        "operation_id": "12345678-1234-5678-1234-567812345678",
    }, follow_redirects=True)
    assert result.status_code == 200
    assert len(sent) == 1
    row = EmailMessage.query.filter_by(account_id=account.id).one()
    assert row.status == "sent" and row.purpose == "marketing"
    assert b"provider accepted 1" in result.data


def test_email_studio_mixed_promotional_copy_cannot_use_service_category_to_bypass_consent(client, app, app_context):
    account = CustomerAccount(
        email="service-category-no-consent@example.test", display_name="No Consent",
        email_verified_at=datetime.utcnow(), marketing_status="none",
    )
    db.session.add(account)
    db.session.commit()
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    payload = {
        "category": "customer_service", "account_id": str(account.id),
        "subject": "Special offer just for you", "headline": "Save today",
        "message": "Our limited time discount is available now.",
    }
    preview = client.post("/admin/email/studio", data={**payload, "action": "preview"})
    assert preview.status_code == 200
    response = client.post("/admin/email/studio", data={
        **payload, "action": "send", "confirm_send": "SEND 1",
        "operation_id": "12345678-1234-5678-1234-567812345678",
    }, follow_redirects=True)
    assert response.status_code == 200
    assert b"Select at least one active, verified subscribed customer" in response.data
    assert EmailMessage.query.filter_by(account_id=account.id).count() == 0


def test_email_studio_visual_template_duplicate_blocks_preview_and_send(client, app, app_context, monkeypatch):
    import app.services.customer_mail as customer_mail
    _configure_brevo_test(app)
    app.config.update(PUBLIC_BASE_URL="https://fitystore.top", CLOUDINARY_CLOUD_NAME="fity-test")
    app.config["EMAIL_MARKETING_CUSTOM_ENABLED"] = True
    sent = []
    monkeypatch.setattr(
        customer_mail, "_send_brevo",
        lambda recipient, subject, html, text: sent.append((recipient, subject, html, text)) or "visual-template-id",
    )
    account = CustomerAccount(
        email="visual-builder@example.test", display_name="Visual Buyer",
        email_verified_at=datetime.utcnow(), marketing_status="subscribed",
        marketing_consent_at=datetime.utcnow(),
    )
    product = Product.query.filter_by(slug="plain-socks").one()
    product.images.append(ProductImage(
        filename="banner.webp", storage_backend="cloudinary",
        cloudinary_public_id="fity/banners/plain-socks", alt_text="Socks on display", is_primary=True,
    ))
    db.session.add(account)
    db.session.commit()
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    blocks = [
        {"type": "design", "hero_background": "#191919", "text_color": "#FDFDFD", "accent_color": "#127A45", "section_background": "#FAFAFA", "footer_note": "Test footer brand"},
        {"type": "heading", "text": "New for {{ customer_name }}", "align": "center"},
        {"type": "paragraph", "text": "A real catalogue selection."},
        {"type": "image_banner", "product_id": product.id, "alt": "New socks in the collection"},
        {"type": "product_grid"},
        {"type": "button", "text": "View socks", "target": "product", "product_id": product.id},
        {"type": "divider"},
        {"type": "contact", "text": "Questions?"},
    ]
    payload = {
        "category": "promotion", "account_id": str(account.id),
        "account_ids": [str(account.id)], "product_ids": [str(product.id)],
        "subject": "New Fity arrivals", "headline": "New collection",
        "message": "Visual promotional template", "blocks_json": json.dumps(blocks),
        "template_name": "New arrivals canvas",
    }
    saved_response = client.post("/admin/email/studio", data={**payload, "action": "save_template_as"}, follow_redirects=True)
    assert saved_response.status_code == 200
    saved = EmailTemplate.query.filter_by(name="New arrivals canvas").one()
    saved_blocks = json.loads(saved.blocks_json)
    assert [block["type"] for block in saved_blocks] == [block["type"] for block in blocks]
    assert saved_blocks[0]["accent_color"] == "#127A45"
    assert saved_blocks[3]["product_id"] == product.id
    assert b"Design controls" in saved_response.data
    assert b"email-block-list" in saved_response.data
    assert b"Desktop preview" in saved_response.data and b"Mobile preview" in saved_response.data

    duplicate = client.post("/admin/email/studio", data={
        **payload, "template_id": str(saved.id), "template_name": "New arrivals copy",
        "action": "duplicate_template",
    }, follow_redirects=True)
    assert duplicate.status_code == 200
    copy = EmailTemplate.query.filter_by(name="New arrivals copy").one()
    assert copy.category == "promotion" and json.loads(copy.blocks_json) == saved_blocks

    payload["template_id"] = str(copy.id)
    payload["blocks_json"] = json.dumps(saved_blocks)
    preview = client.post("/admin/email/studio", data={**payload, "action": "preview"})
    assert preview.status_code == 200
    assert b"https://res.cloudinary.com/fity-test/image/upload/fity/banners/plain-socks" in preview.data
    assert b"New for Visual" in preview.data
    assert b"#127A45" in preview.data and b"#191919" in preview.data
    result = client.post("/admin/email/studio", data={
        **payload, "action": "send", "confirm_send": "SEND 1",
        "operation_id": "87654321-1234-5678-1234-567812345678",
    }, follow_redirects=True)
    assert result.status_code == 200
    assert len(sent) == 1
    row = EmailMessage.query.filter_by(account_id=account.id).one()
    assert row.status == "sent" and row.purpose == "marketing"
    assert b"provider accepted 1" in result.data


def test_email_studio_test_send_requires_prior_preview_and_explicit_address(client, app, app_context, monkeypatch):
    import app.services.customer_mail as customer_mail
    _configure_brevo_test(app)
    app.config["MAIL_TEST_ADDRESS"] = "owner-controlled@example.test"
    sent = []
    monkeypatch.setattr(customer_mail, "_send_brevo", lambda recipient, subject, html, text: sent.append((recipient, subject, html, text)) or "studio-test-id")
    account = CustomerAccount(email="studio-test-context@example.test", display_name="Test Context", email_verified_at=datetime.utcnow())
    db.session.add(account); db.session.commit()
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    payload = {
        "category": "customer_service", "account_id": str(account.id),
        "subject": "Hello {{ customer_name }}", "headline": "Test greeting",
        "message": "This is test data.",
    }
    preview = client.post("/admin/email/studio", data={**payload, "action": "preview"})
    assert preview.status_code == 200 and sent == []
    result = client.post(
        "/admin/email/studio",
        data={**payload, "action": "send_test", "confirm_test": "SEND TEST",
              "test_email": "attacker-controlled@example.test"},
        follow_redirects=True,
    )
    assert result.status_code == 200
    assert sent == []
    test_message = EmailMessage.query.filter_by(kind="admin_test").one()
    customer_mail.process_admin_test_email(test_message.id)
    assert len(sent) == 1
    assert sent[0][0] == "owner-controlled@example.test"
    assert sent[0][1].startswith("[TEST PREVIEW]")
    assert b"TEST PREVIEW" in sent[0][2].encode()
    assert EmailMessage.query.filter_by(kind="admin_test", status="sent").count() == 1


def test_email_studio_test_send_rejects_missing_or_customer_test_recipient(client, app, app_context):
    account = CustomerAccount(
        email="studio-test-collision@example.test",
        display_name="Test Context",
        email_verified_at=datetime.utcnow(),
    )
    db.session.add(account)
    db.session.commit()
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    payload = {
        "category": "customer_service", "account_id": str(account.id),
        "subject": "Hello {{ customer_name }}", "headline": "Test greeting",
        "message": "This is test data.", "action": "send_test",
        "confirm_test": "SEND TEST",
    }

    app.config["MAIL_TEST_ADDRESS"] = ""
    result = client.post("/admin/email/studio", data=payload, follow_redirects=True)
    assert b"Set MAIL_TEST_ADDRESS" in result.data
    assert EmailMessage.query.filter_by(kind="admin_test").count() == 0

    app.config["MAIL_TEST_ADDRESS"] = account.email
    result = client.post("/admin/email/studio", data=payload, follow_redirects=True)
    assert b"matches a customer account" in result.data
    assert EmailMessage.query.filter_by(kind="admin_test").count() == 0


def test_email_studio_previews_marketing_without_customer_and_keeps_csrf(client, app, app_context):
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    app.config["WTF_CSRF_ENABLED"] = True
    page = client.get("/admin/email/studio?category=promotion")
    assert page.status_code == 200
    assert b"Sample Customer" in page.data
    assert b"sample@example.invalid" in page.data
    assert b"generic preview" in page.data
    csrf_token = re.search(rb'name="csrf_token" value="([^"]+)"', page.data).group(1).decode()

    payload = {
        "category": "promotion",
        "subject": "Welcome to FityStores updates",
        "headline": "Welcome to FityStores updates",
        "message": "Hi {{ customer_name }}, a sample promotional preview.",
        "action": "preview",
    }
    rejected = client.post("/admin/email/studio", data=payload)
    assert rejected.status_code == 400
    assert EmailMessage.query.count() == 0

    preview = client.post("/admin/email/studio", data={**payload, "csrf_token": csrf_token})
    assert preview.status_code == 200
    assert b"Sample Customer" in preview.data
    assert b"sample@example.invalid" in preview.data
    assert b"TEST PREVIEW" in preview.data
    assert EmailMessage.query.count() == 0
    assert CustomerAccount.query.filter_by(email="sample@example.invalid").count() == 0
    assert CustomerToken.query.filter_by(purpose="unsubscribe").count() == 0


def test_email_studio_sample_test_is_confirmed_and_queued_only_to_configured_recipient(client, app, app_context):
    _configure_brevo_test(app)
    app.config["MAIL_TEST_ADDRESS"] = "owner-controlled@example.test"
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    payload = {
        "category": "promotion",
        "subject": "Welcome to FityStores updates",
        "headline": "Welcome to FityStores updates",
        "message": "Hi {{ customer_name }}, a sample promotional preview.",
    }
    preview = client.post("/admin/email/studio", data={**payload, "action": "preview"})
    assert preview.status_code == 200

    not_confirmed = client.post("/admin/email/studio", data={
        **payload, "action": "send_test", "confirm_test": "SEND",
        "test_email": "attacker-controlled@example.test",
    }, follow_redirects=True)
    assert not_confirmed.status_code == 200
    assert b"Type SEND TEST" in not_confirmed.data
    assert EmailMessage.query.filter_by(kind="admin_test").count() == 0

    queued = client.post("/admin/email/studio", data={
        **payload, "action": "send_test", "confirm_test": "SEND TEST",
        "test_email": "attacker-controlled@example.test",
    }, follow_redirects=True)
    assert queued.status_code == 200
    row = EmailMessage.query.filter_by(kind="admin_test").one()
    assert row.status == "queued"
    assert row.recipient == "owner-controlled@example.test"
    assert row.account_id is None
    assert row.purpose == "test"
    assert row.subject.startswith("[TEST PREVIEW]")
    assert "TEST PREVIEW" in row.html_body
    assert "TEST PREVIEW" in row.text_body
    assert "Sample Customer" in row.html_body
    assert CustomerAccount.query.filter_by(email="sample@example.invalid").count() == 0
    assert CustomerToken.query.filter_by(purpose="unsubscribe").count() == 0


def test_email_studio_cart_and_order_categories_require_real_context(client, app, app_context):
    account = CustomerAccount(
        email="studio-context@example.test", display_name="Real Context",
        email_verified_at=datetime.utcnow(),
    )
    db.session.add(account)
    db.session.commit()
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})

    cart_page = client.get("/admin/email/studio?category=cart_reminder")
    assert b"actual saved cart" in cart_page.data
    cart_preview = client.post("/admin/email/studio", data={
        "category": "cart_reminder", "subject": "Cart", "headline": "Your cart",
        "message": "Your saved cart.", "action": "preview",
    })
    assert b"Choose an actual customer for this email context" in cart_preview.data

    cart_without_items = client.post("/admin/email/studio", data={
        "category": "cart_reminder", "account_id": str(account.id),
        "subject": "Cart", "headline": "Your cart", "message": "Your saved cart.",
        "action": "preview",
    })
    assert b"no saved cart items" in cart_without_items.data

    for category in ("order_confirmation", "thank_you", "order_status"):
        page = client.get(f"/admin/email/studio?category={category}&account_id={account.id}")
        assert b"No order is linked to this customer" in page.data
        response = client.post("/admin/email/studio", data={
            "category": category, "account_id": str(account.id), "subject": "Order",
            "headline": "Order update", "message": "An order update.", "action": "preview",
        })
        assert b"Select an order linked to the chosen customer" in response.data

    assert EmailMessage.query.count() == 0


def test_email_studio_manual_marketing_send_rejects_unconsented_and_suppressed_accounts(client, app, app_context):
    from app.services.marketing_suppressions import record_suppression

    _configure_brevo_test(app)
    unconsented = CustomerAccount(
        email="studio-no-consent@example.test", display_name="No Consent",
        email_verified_at=datetime.utcnow(), marketing_status="none",
    )
    suppressed = CustomerAccount(
        email="studio-suppressed@example.test", display_name="Suppressed",
        email_verified_at=datetime.utcnow(), marketing_status="subscribed",
        marketing_consent_at=datetime.utcnow(),
    )
    db.session.add_all([unconsented, suppressed])
    db.session.commit()
    record_suppression(
        suppressed.id, source="provider", reason_code="hard_bounce",
        reason_text="Test suppression", actor_type="provider", provider_name="brevo",
    )
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})

    for account in (unconsented, suppressed):
        payload = {
            "category": "marketing_welcome", "account_id": str(account.id),
            "account_ids": [str(account.id)], "subject": "Welcome",
            "headline": "Welcome", "message": "A promotional message.",
        }
        preview = client.post("/admin/email/studio", data={**payload, "action": "preview"})
        assert preview.status_code == 200
        result = client.post("/admin/email/studio", data={
            **payload, "action": "send", "confirm_send": "SEND 1",
            "operation_id": "12345678-1234-5678-1234-567812345678",
        }, follow_redirects=True)
        assert result.status_code == 200
        assert b"Select at least one active, verified subscribed customer" in result.data

    assert EmailMessage.query.filter_by(account_id=unconsented.id).count() == 0
    assert EmailMessage.query.filter_by(account_id=suppressed.id).count() == 0


def test_email_studio_promotion_requires_preview_consent_and_real_catalogue_items(client, app, app_context, monkeypatch):
    from uuid import uuid4
    import app.services.customer_mail as customer_mail
    _configure_brevo_test(app)
    app.config["PUBLIC_BASE_URL"] = "https://fitystore.top"
    app.config["CLOUDINARY_CLOUD_NAME"] = "fity-test"
    app.config["EMAIL_MARKETING_CUSTOM_ENABLED"] = True
    app.config["EMAIL_MARKETING_QUEUE_ENABLED"] = False
    sent = []
    monkeypatch.setattr(customer_mail, "_send_brevo", lambda recipient, subject, html, text: sent.append((recipient, subject, html, text)) or "studio-promo-id")
    account = CustomerAccount(email="studio-promo@example.test", display_name="Promo Buyer", email_verified_at=datetime.utcnow(), marketing_status="subscribed", marketing_consent_at=datetime.utcnow())
    denied = CustomerAccount(email="studio-denied@example.test", display_name="Not Subscribed", email_verified_at=datetime.utcnow(), marketing_status="none")
    product = Product.query.filter_by(slug="plain-socks").one()
    product.images.append(ProductImage(filename="catalog-image.webp", storage_backend="cloudinary", cloudinary_public_id="fity/products/catalog", alt_text="Real catalogue image", is_primary=True))
    db.session.add_all([account, denied]); db.session.commit()
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    payload = {
        "category": "promotion", "account_id": str(account.id), "account_ids": [str(account.id), str(denied.id)],
        "product_ids": [str(product.id)], "subject": "Current picks", "headline": "For {{ customer_name }}",
        "message": "See {{ customer_name }}'s selection. {{ unsubscribe_link }}",
        "blocks_json": json.dumps([
            {"type": "heading", "text": "Picked for you"},
            {"type": "paragraph", "text": "A few catalogue favourites."},
            {"type": "product_grid"},
            {"type": "image_banner", "product_id": product.id, "alt": "Plain Socks"},
            {"type": "product_grid"},
            {"type": "image_banner", "product_id": product.id, "alt": "Plain Socks"},
        ]),
    }
    preview = client.post("/admin/email/studio", data={**payload, "action": "preview"})
    assert preview.status_code == 200 and sent == []
    tokens_after_preview = CustomerToken.query.filter_by(
        account_id=account.id, purpose="unsubscribe",
    ).count()
    result = client.post("/admin/email/studio", data={**payload, "action": "send", "confirm_send": "SEND 1", "operation_id": str(uuid4())}, follow_redirects=True)
    assert result.status_code == 200
    assert len(sent) == 1
    row = EmailMessage.query.filter_by(account_id=account.id).one()
    campaign = EmailCampaign.query.filter_by(id=row.campaign_id).one()
    recipient = EmailCampaignRecipient.query.filter_by(campaign_id=campaign.id).one()
    assert row.status == recipient.status == campaign.status == "sent"
    assert row.purpose == "marketing"
    assert campaign.generation_source == "email_studio"
    assert CustomerToken.query.filter_by(
        account_id=account.id, purpose="unsubscribe",
    ).count() == tokens_after_preview + 1
    assert b"provider accepted 1" in result.data


def test_email_studio_schedules_only_previewed_marketing_for_future_queue_delivery(client, app, app_context, monkeypatch):
    import app.services.customer_mail as customer_mail

    _configure_brevo_test(app)
    app.config["TIMEZONE"] = "Africa/Nairobi"
    account = CustomerAccount(
        email="scheduled-campaign@example.test", display_name="Scheduled Shopper",
        email_verified_at=datetime.utcnow(), marketing_status="subscribed",
        marketing_consent_at=datetime.utcnow(),
    )
    db.session.add(account)
    db.session.commit()
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    deliveries = []
    monkeypatch.setattr(customer_mail, "_send_brevo", lambda *args: deliveries.append(args) or "scheduled-message-id")
    payload = {
        "category": "promotion", "account_id": str(account.id),
        "account_ids": [str(account.id)], "subject": "Scheduled offer",
        "headline": "A future offer", "message": "A scheduled note for {{ customer_name }}.",
        "blocks_json": json.dumps([{"type": "heading", "text": "A future offer"}, {"type": "paragraph", "text": "A scheduled note."}]),
    }
    preview = client.post("/admin/email/studio", data={**payload, "action": "preview"})
    assert preview.status_code == 200 and deliveries == []
    scheduled = client.post("/admin/email/studio", data={
        **payload, "action": "schedule",
        "confirm_schedule": "SCHEDULE 1",
    }, follow_redirects=True)
    assert scheduled.status_code == 200
    assert b"Use the marketing campaign centre to schedule a campaign" in scheduled.data
    assert EmailMessage.query.filter_by(account_id=account.id).count() == 0
    assert deliveries == []
    customer_mail.process_email_queue(limit=5)
    assert deliveries == []


def test_queue_rejects_future_schedule_for_transactional_email(app, app_context):
    from datetime import timedelta
    from app.services.customer_mail import queue_email

    with pytest.raises(ValueError, match="Only marketing messages"):
        queue_email(
            "customer@example.test", "Order update", "order_received", "schedule-transactional-denied",
            not_before=datetime.utcnow() + timedelta(hours=1),
            html_body="<p>Order received</p>", text_body="Order received",
        )


def test_marketing_cooldown_uses_scheduled_delivery_time(app, app_context):
    from datetime import timedelta
    from app.services.customer_mail import queue_email

    account = CustomerAccount(
        email="scheduled-cooldown@example.test", display_name="Scheduled Cooldown",
        email_verified_at=datetime.utcnow(), marketing_status="subscribed",
        marketing_consent_at=datetime.utcnow(),
    )
    db.session.add(account)
    db.session.commit()
    first_time = datetime.utcnow() + timedelta(days=10)
    second_time = first_time + timedelta(hours=2)
    far_time = first_time + timedelta(days=2)

    first = queue_email(
        account.email, "First scheduled note", "marketing_campaign", "scheduled-cooldown-first",
        account_id=account.id, not_before=first_time,
        html_body="<p>First</p>", text_body="First",
    )
    too_close = queue_email(
        account.email, "Second scheduled note", "marketing_campaign", "scheduled-cooldown-too-close",
        account_id=account.id, not_before=second_time,
        html_body="<p>Second</p>", text_body="Second",
    )
    later = queue_email(
        account.email, "Later scheduled note", "marketing_campaign", "scheduled-cooldown-later",
        account_id=account.id, not_before=far_time,
        html_body="<p>Later</p>", text_body="Later",
    )

    assert first.status == "queued"
    assert too_close.status == "suppressed"
    assert later.status == "queued"


def test_cart_activity_shows_real_account_cart_and_manual_eligibility(client, app, app_context):
    from datetime import timedelta
    from secrets import token_urlsafe
    account = CustomerAccount(email="cart-activity@example.test", display_name="Cart Shopper", email_verified_at=datetime.utcnow(), marketing_status="subscribed")
    db.session.add(account); db.session.flush()
    cart = CustomerCart(account_id=account.id, items_json='[{"product_id":1,"quantity":2}]', episode_key=token_urlsafe(24), updated_at=datetime.utcnow() - timedelta(hours=3))
    db.session.add(cart); db.session.commit()
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    response = client.get("/admin/email/cart-activity")
    assert response.status_code == 200
    assert b"Cart Shopper" in response.data
    assert b"Plain Socks" in response.data
    assert b"Eligible for manual reminder" in response.data
    assert b"No email is sent automatically" in response.data


def test_email_studio_order_preview_uses_owned_order_and_recorded_financial_status(client, app, app_context):
    account = CustomerAccount(email="studio-order-owner@example.test", display_name="Order Owner", email_verified_at=datetime.utcnow())
    stranger = CustomerAccount(email="studio-order-stranger@example.test", display_name="Stranger", email_verified_at=datetime.utcnow())
    db.session.add_all([account, stranger]); db.session.flush()
    order = Order(reference="STUDIO-ORDER-1", fulfilment_method="Collection", status="Processing", payment_status="Awaiting payment", total_cents=18000, discount_cents=1000, delivery_fee_cents=500)
    db.session.add(order); db.session.flush()
    db.session.add_all([
        CustomerOrderLink(account_id=account.id, order_id=order.id),
        OrderItem(order_id=order.id, product_id=1, product_name="Plain Socks", quantity=1, price_cents=18500, subtotal_cents=18500),
    ])
    db.session.commit()
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    payload = {
        "category": "order_confirmation", "account_id": str(account.id), "order_id": str(order.id),
        "subject": "Order {{ order_reference }}", "headline": "Order details",
        "message": "Payment: {{ payment_status }}. Order: {{ order_status }}.",
        "blocks_json": json.dumps([
            {"type": "heading", "text": "Order {{ order_reference }}"},
            {"type": "order_summary"},
            {"type": "button", "text": "Track order", "target": "order"},
        ]),
        "action": "preview",
    }
    response = client.post("/admin/email/studio", data=payload)
    assert response.status_code == 200
    assert b"Awaiting payment" in response.data
    assert b"Processing" in response.data
    assert b"KSh 185" in response.data
    assert b"KSh 10" in response.data
    assert b"KSh 5" in response.data
    assert b"KSh 180" in response.data
    assert b"Order STUDIO-ORDER-1" in response.data
    assert b"View order" in response.data or b"Track this order" in response.data or b"Track order" in response.data

    payload["account_id"] = str(stranger.id)
    denied = client.post("/admin/email/studio", data=payload)
    assert denied.status_code == 200
    assert b"not linked to this customer" in denied.data


@pytest.mark.parametrize(
    ("category", "expected_kind"),
    [
        ("order_confirmation", "studio_order_confirmation"),
        ("thank_you", "studio_thank_you"),
        ("order_status", "studio_order_status"),
    ],
)
def test_email_studio_code_defined_order_notices_are_transactional_without_marketing_consent(
    client, app, app_context, monkeypatch, category, expected_kind,
):
    import app.services.customer_mail as customer_mail
    import app.services.marketing_eligibility as marketing_eligibility
    from app.services.email_studio import EMAIL_CATEGORIES

    _configure_brevo_test(app)
    app.config["PUBLIC_BASE_URL"] = "https://fitystore.top"
    delivered = []
    monkeypatch.setattr(
        customer_mail, "_send_brevo",
        lambda recipient, subject, html, text: delivered.append((recipient, subject, html, text)) or "studio-transactional-id",
    )
    # Neither enqueue nor the final worker dispatch may consult marketing
    # eligibility for these constrained transactional workflows.
    monkeypatch.setattr(
        marketing_eligibility, "evaluate_marketing_eligibility",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("transactional email consulted marketing eligibility")),
    )
    account = CustomerAccount(
        email=f"{category.replace('_', '-')}-no-consent@example.test",
        display_name="Order Customer", email_verified_at=datetime.utcnow(),
        marketing_status="none", marketing_consent_at=None,
    )
    db.session.add(account)
    db.session.flush()
    order = Order(
        reference=f"STUDIO-{category.upper()}", fulfilment_method="Collection",
        status="Processing", payment_status="Awaiting payment", total_cents=15000,
    )
    db.session.add(order)
    db.session.flush()
    db.session.add_all([
        CustomerOrderLink(account_id=account.id, order_id=order.id),
        OrderItem(order_id=order.id, product_id=1, product_name="Plain Socks", quantity=1,
                  price_cents=15000, subtotal_cents=15000),
    ])
    db.session.commit()
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})

    defaults = EMAIL_CATEGORIES[category]
    payload = {
        "category": category, "account_id": str(account.id), "order_id": str(order.id),
        "subject": defaults["subject"], "headline": defaults["headline"],
        "message": defaults["message"], "blocks_json": "[]",
    }
    preview = client.post("/admin/email/studio", data={**payload, "action": "preview"})
    assert preview.status_code == 200
    assert delivered == []

    operation_id = {
        "order_confirmation": "11111111-2222-4333-8444-555555555551",
        "thank_you": "11111111-2222-4333-8444-555555555552",
        "order_status": "11111111-2222-4333-8444-555555555553",
    }[category]
    result = client.post("/admin/email/studio", data={
        **payload, "action": "send", "confirm_send": "SEND 1", "operation_id": operation_id,
    }, follow_redirects=True)
    assert result.status_code == 200
    row = EmailMessage.query.filter_by(
        idempotency_key=f"email-studio:{operation_id}:{account.id}",
    ).one()
    assert row.kind == expected_kind
    assert row.purpose == "transactional"
    assert row.status == "sent"
    outcome = customer_mail.process_email_queue(limit=5)
    assert outcome["sent"] == 0
    assert len(delivered) == 1
    assert delivered[0][0] == account.email
    assert "unsubscribe" not in delivered[0][2].lower()


def test_email_studio_custom_order_category_content_remains_marketing(client, app, app_context):
    from app.services.email_studio import EMAIL_CATEGORIES

    account = CustomerAccount(
        email="custom-order-marketing@example.test", display_name="No Consent",
        email_verified_at=datetime.utcnow(), marketing_status="none",
    )
    db.session.add(account)
    db.session.flush()
    order = Order(reference="CUSTOM-ORDER-COPY", fulfilment_method="Collection")
    db.session.add(order)
    db.session.flush()
    db.session.add(CustomerOrderLink(account_id=account.id, order_id=order.id))
    db.session.commit()
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    payload = {
        "category": "order_confirmation", "account_id": str(account.id), "order_id": str(order.id),
        "subject": "A special offer for {{ customer_name }}",
        "headline": "Shop our new arrivals", "message": "A limited offer is waiting.",
        "blocks_json": "[]",
    }
    preview = client.post("/admin/email/studio", data={**payload, "action": "preview"})
    assert preview.status_code == 200
    response = client.post("/admin/email/studio", data={
        **payload, "action": "send", "confirm_send": "SEND 1",
        "operation_id": "22222222-3333-4444-8555-666666666666",
    }, follow_redirects=True)
    assert response.status_code == 200
    assert b"Select at least one active, verified subscribed customer" in response.data
    assert EmailMessage.query.filter_by(account_id=account.id).count() == 0

    defaults = EMAIL_CATEGORIES["order_confirmation"]
    design_copy = {
        "category": "order_confirmation", "account_id": str(account.id), "order_id": str(order.id),
        "subject": defaults["subject"], "headline": defaults["headline"],
        "message": defaults["message"],
        "blocks_json": json.dumps([{"type": "design", "preheader": "New arrivals and limited offers"}]),
    }
    assert client.post("/admin/email/studio", data={**design_copy, "action": "preview"}).status_code == 200
    design_response = client.post("/admin/email/studio", data={
        **design_copy, "action": "send", "confirm_send": "SEND 1",
        "operation_id": "33333333-4444-4555-8666-777777777777",
    }, follow_redirects=True)
    assert design_response.status_code == 200
    assert b"Select at least one active, verified subscribed customer" in design_response.data
    assert EmailMessage.query.filter_by(account_id=account.id).count() == 0


def test_email_purpose_mapping_rejects_unknown_and_untrusted_test_kinds(app, app_context):
    from app.services.email_purpose import UnknownEmailKind, email_purpose_for_kind
    from app.services.customer_mail import queue_email

    assert email_purpose_for_kind("studio_order_confirmation") == "transactional"
    assert email_purpose_for_kind("marketing_campaign") == "marketing"
    assert email_purpose_for_kind("admin_test") == "test"
    with pytest.raises(UnknownEmailKind):
        email_purpose_for_kind("admin_arbitrary")
    with pytest.raises(UnknownEmailKind):
        email_purpose_for_kind("test", testing=False)
    with pytest.raises(UnknownEmailKind):
        queue_email(
            "unknown@example.test", "Unknown", "admin_arbitrary", "unknown-purpose-kind",
            html_body="<p>Must not send</p>", text_body="Must not send",
        )


def test_unknown_persisted_email_kind_is_quarantined_before_provider_dispatch(app, app_context, monkeypatch):
    import app.services.customer_mail as customer_mail
    _configure_brevo_test(app)
    provider_calls = []
    monkeypatch.setattr(
        customer_mail, "_send_brevo",
        lambda *args: provider_calls.append(args) or "should-not-be-called",
    )
    row = EmailMessage(
        recipient="unknown-kind@example.test", subject="Unknown workflow",
        kind="admin_arbitrary", purpose="transactional",
        idempotency_key="persisted-unknown-kind", status="queued",
        html_body="<p>Unknown</p>", text_body="Unknown",
    )
    db.session.add(row)
    db.session.commit()

    counts = customer_mail.process_email_queue(limit=5)
    db.session.refresh(row)
    assert counts["failed"] == 1
    assert row.status == "failed"
    assert row.error_category == "invalid_purpose"
    assert provider_calls == []


def test_email_studio_rejects_unknown_variables_and_admin_only_access(client, app, app_context):
    assert client.get("/admin/email/studio").status_code == 302
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    response = client.post("/admin/email/studio", data={
        "category": "customer_service", "subject": "Unsafe {{ config }}", "headline": "Hello",
        "message": "No arbitrary template code", "action": "save_template",
    }, follow_redirects=True)
    assert response.status_code == 200
    assert b"Unsupported variable" in response.data
    assert EmailTemplate.query.filter_by(category="customer_service").count() == 0


def test_manual_cart_reminder_revalidates_and_records_one_cart_episode(client, app, app_context, monkeypatch):
    from datetime import timedelta
    from secrets import token_urlsafe
    import app.services.customer_mail as customer_mail
    _configure_brevo_test(app)
    app.config["EMAIL_MARKETING_ABANDONED_CART_ENABLED"] = True
    sent = []
    monkeypatch.setattr(customer_mail, "_send_brevo", lambda recipient, subject, html, text: sent.append((recipient, html, text)) or "cart-reminder-provider-id")
    account = CustomerAccount(email="studio-cart-reminder@example.test", display_name="Cart Reminder", email_verified_at=datetime.utcnow(), marketing_status="subscribed", marketing_consent_at=datetime.utcnow())
    db.session.add(account); db.session.flush()
    cart = CustomerCart(account_id=account.id, items_json='[{"product_id":1,"quantity":2}]', episode_key=token_urlsafe(24), updated_at=datetime.utcnow() - timedelta(hours=3))
    db.session.add(cart); db.session.commit()
    original_activity = cart.updated_at
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})
    payload = {
        "category": "cart_reminder", "account_id": str(account.id), "account_ids": [str(account.id)],
        "subject": "Still interested?", "headline": "Your saved items", "message": "Current prices are shown.",
    }
    preview = client.post("/admin/email/studio", data={**payload, "action": "preview"})
    assert preview.status_code == 200 and sent == []
    result = client.post("/admin/email/studio", data={**payload, "action": "send", "confirm_send": "SEND 1", "operation_id": "12345678-1234-5678-1234-567812345678"}, follow_redirects=True)
    assert result.status_code == 200
    assert len(sent) == 1
    db.session.refresh(cart)
    assert b"provider accepted 1" in result.data
    assert cart.reminder_sent_at is not None
    row = EmailMessage.query.filter_by(account_id=account.id).one()
    assert row.status == "sent" and row.purpose == "marketing"
    assert cart.updated_at == original_activity


def test_media_showcase_video_preview_crop_saves_and_renders_without_cropping_source(client, app):
    _sign_in_admin(client, app)
    items = [
        {"type": "video", "title": "Portrait film", "url": "https://cdn.example.test/portrait.mp4",
         "poster": "https://cdn.example.test/portrait.jpg", "presentation": "cover", "aspect_ratio": "9:16",
         "width": 1080, "height": 1920, "preview_position_x": 73, "preview_position_y": 24},
        {"type": "video", "title": "Landscape film", "url": "https://cdn.example.test/landscape.mp4",
         "poster": "https://cdn.example.test/landscape.jpg", "presentation": "contain", "aspect_ratio": "16:9",
         "width": 1920, "height": 1080, "autoplay": False},
    ]
    response = client.post("/admin/media-showcases/new", data={
        "name": "Video crop test", "title": "Video preview", "page": "home",
        "placement": "home_after_hero", "layout": "grid", "publish_action": "publish",
        "is_enabled": "1", "items_json": json.dumps(items),
    })
    assert response.status_code == 302
    with app.app_context():
        showcase = MediaShowcase.query.filter_by(slug="video-crop-test").one()
        showcase_id = showcase.id
        portrait = next(item for item in showcase.items if item.title == "Portrait film")
        assert (portrait.preview_position_x, portrait.preview_position_y) == (73, 24)

    edit_page = client.get(f"/admin/media-showcases/{showcase_id}/edit")
    encoded = re.search(rb'<script type="application/json" id="showcase-items-data">(.*?)</script>', edit_page.data, re.S)
    assert encoded
    editable_items = json.loads(json.loads(encoded.group(1)))
    portrait_settings = next(item for item in editable_items if item["title"] == "Portrait film")
    assert (portrait_settings["preview_position_x"], portrait_settings["preview_position_y"]) == (73, 24)

    portrait_settings["preview_position_x"] = 50
    portrait_settings["preview_position_y"] = 50
    update = client.post(f"/admin/media-showcases/{showcase_id}/edit", data={
        "name": "Video crop test", "title": "Video preview", "page": "home",
        "placement": "home_after_hero", "layout": "grid", "publish_action": "keep",
        "is_published": "1", "is_enabled": "1", "items_json": json.dumps(editable_items),
    })
    assert update.status_code == 302
    with app.app_context():
        portrait = next(item for item in MediaShowcase.query.get(showcase_id).items if item.title == "Portrait film")
        assert (portrait.preview_position_x, portrait.preview_position_y) == (50, 50)

    page = client.get("/")
    assert page.status_code == 200
    assert b"data-showcase-video-open" in page.data
    assert b"data-video-src=\"https://cdn.example.test/portrait.mp4\"" in page.data
    assert b'data-media-autoplay="1"' in page.data
    video_tags = re.findall(rb'<video\b[^>]*data-showcase-video-preview[^>]*>', page.data)
    assert len(video_tags) == 2
    autoplay_video = next(tag for tag in video_tags if b'data-autoplay="1"' in tag)
    non_autoplay_video = next(tag for tag in video_tags if b'data-autoplay="0"' in tag)
    assert b"autoplay" in autoplay_video and b"muted" in autoplay_video
    assert b"loop" in autoplay_video and b"playsinline" in autoplay_video
    assert b'data-preview-src="https://cdn.example.test/portrait.mp4"' in autoplay_video
    assert not re.search(rb'\ssrc=', autoplay_video)
    assert not re.search(rb'\sautoplay(?:\s|>)', non_autoplay_video)
    assert b'data-media-autoplay="0"' in page.data
    assert b"--media-item-ratio:9.0 / 16.0" in page.data
    assert b"--media-item-ratio:16.0 / 9.0" in page.data
    assert b"data-media-showcase-player" in page.data
    assert b"media-showcase-player-content" in page.data
    assert b'data-media-player-minimized hidden' in page.data
    assert b'href="/reviews#write-review">Rate us</a>' in page.data
    editor_js = client.get("/static/js/media-showcase-editor.js")
    assert editor_js.status_code == 200
    assert b"Horizontal position" in editor_js.data and b"Vertical position" in editor_js.data
    assert b"Reset position" in editor_js.data


def test_media_showcase_preview_position_upgrade_is_additive_and_idempotent():
    from sqlalchemy import create_engine, inspect, text
    from app.services.media_showcases import _add_missing_preview_position_columns

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE media_showcase_item (id INTEGER PRIMARY KEY, title VARCHAR(160) NOT NULL)"))
        connection.execute(text("INSERT INTO media_showcase_item (id, title) VALUES (7, 'Saved video')"))
        _add_missing_preview_position_columns(connection)
        _add_missing_preview_position_columns(connection)
        columns = {column["name"] for column in inspect(connection).get_columns("media_showcase_item")}
        saved = connection.execute(text("SELECT id, title, preview_position_x, preview_position_y FROM media_showcase_item WHERE id=7")).one()

    assert {"id", "title", "preview_position_x", "preview_position_y"} <= columns
    assert tuple(saved) == (7, "Saved video", 50, 50)
    engine.dispose()


def test_auth_token_supersession_is_purpose_scoped_and_transactional(app, app_context, monkeypatch):
    from datetime import timedelta
    import hashlib
    import app.customer.routes as customer_routes
    from app.services import customer_mail

    monkeypatch.setattr(customer_mail, "_send_brevo", lambda *_args: "mocked-auth-provider-id")
    monkeypatch.setattr(customer_mail, "_send_resend", lambda *_args: "mocked-auth-provider-id")
    monkeypatch.setattr(customer_mail, "_send_smtp", lambda *_args: None)
    monkeypatch.setattr(customer_mail, "mail_configuration_status", lambda: (True, "test transport"))

    account = CustomerAccount(
        email="token-supersession@example.test", display_name="Token Test",
        email_verified_at=datetime.utcnow(),
    )
    db.session.add(account)
    db.session.flush()
    now = datetime.utcnow()
    existing = {}
    for purpose in ("verify_email", "reset_password", "login_code", "verify_code"):
        raw = f"old-{purpose}"
        row = CustomerToken(
            account_id=account.id, purpose=purpose,
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            expires_at=now + timedelta(hours=1),
        )
        existing[purpose] = row
        db.session.add(row)
    db.session.commit()

    verify_result = customer_routes._issue_and_send(account, "verify_email")
    reset_result = customer_routes._issue_and_send(account, "reset_password", 30)
    code_result = customer_routes._start_email_code(account, "login_code")

    assert verify_result.status == reset_result.status == "sent"
    assert code_result.status == "sent"
    for purpose in ("verify_email", "reset_password", "login_code"):
        db.session.refresh(existing[purpose])
        assert existing[purpose].used_at is not None
    db.session.refresh(existing["verify_code"])
    assert existing["verify_code"].used_at is None


def test_auth_queue_failure_never_reactivates_superseded_token(app, app_context, monkeypatch):
    from datetime import timedelta
    import hashlib
    import app.customer.routes as customer_routes

    account = CustomerAccount(email="token-queue-failure@example.test", display_name="Token Failure")
    db.session.add(account)
    db.session.flush()
    previous = CustomerToken(
        account_id=account.id, purpose="reset_password",
        token_hash=hashlib.sha256(b"previous-reset").hexdigest(),
        expires_at=datetime.utcnow() + timedelta(hours=1),
    )
    db.session.add(previous)
    db.session.commit()
    monkeypatch.setattr(customer_routes, "_send_token_email", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("simulated queue failure")))

    with pytest.raises(RuntimeError):
        customer_routes._issue_and_send(account, "reset_password", 30)

    db.session.refresh(previous)
    assert previous.used_at is not None
    assert CustomerToken.query.filter_by(account_id=account.id, purpose="reset_password", used_at=None).count() == 0
    assert EmailMessage.query.filter_by(account_id=account.id, kind="reset_password").count() == 0


def test_marketing_frequency_cannot_be_bypassed_by_manual_queue_calls(app, app_context):
    from app.services.customer_mail import queue_email

    account = CustomerAccount(
        email="frequency@example.test", display_name="Frequency",
        email_verified_at=datetime.utcnow(), marketing_status="subscribed",
        marketing_consent_at=datetime.utcnow(),
    )
    db.session.add(account)
    db.session.commit()
    first = queue_email(account.email, "First", "marketing_campaign", "frequency-first",
                        account_id=account.id, html_body="<p>First</p>", text_body="First")
    second = queue_email(account.email, "Second", "marketing_campaign", "frequency-second",
                         account_id=account.id, html_body="<p>Second</p>", text_body="Second")

    assert first.status == "queued"
    assert second.status == "suppressed"
    assert first.frequency_override is False
    assert second.frequency_override is False


def test_legacy_admin_authored_service_copy_is_not_queued(client, app, app_context, monkeypatch):
    import app.services.customer_mail as customer_mail
    monkeypatch.setattr(customer_mail, "mail_configuration_status", lambda: (True, "test transport"))
    account = CustomerAccount(
        email="admin-freeform-no-consent@example.test", display_name="No Consent",
        email_verified_at=datetime.utcnow(), marketing_status="none",
    )
    db.session.add(account)
    db.session.commit()
    client.post("/admin/login", data={"username": "owner", "password": "secret123"})

    response = client.post(f"/admin/email/customers/{account.id}/message", data={
        "confirm_send": "yes", "send_key": "12345678-1234-5678-1234-567812345678",
        "subject": "A message", "body": "A personal note.",
    }, follow_redirects=True)

    assert response.status_code == 200
    assert b"no message was queued" in response.data
    assert EmailMessage.query.filter_by(
        idempotency_key=f"admin-message:{account.id}:12345678-1234-5678-1234-567812345678"
    ).count() == 0

    client.post("/admin/email/send-service", data={
        "service_send_key": "87654321-1234-5678-1234-567812345678",
        "account_ids": [str(account.id)], "service_subject": "Product note",
        "service_body": "A note about a new offer.",
    }, follow_redirects=True)
    batch_row = EmailMessage.query.filter_by(
        idempotency_key=f"admin-service-batch:87654321-1234-5678-1234-567812345678:{account.id}"
    ).first()
    assert batch_row is None


def test_malformed_queued_rows_are_quarantined_without_starving_valid_mail(app, app_context, monkeypatch):
    import app.services.customer_mail as customer_mail
    _configure_brevo_test(app)
    sent = []
    monkeypatch.setattr(customer_mail, "_send_brevo", lambda recipient, *_args: sent.append(recipient) or "valid-id")
    malformed = [
        EmailMessage(recipient=f"bad-{index}@example.test", subject="Bad", kind="order_update",
                     idempotency_key=f"malformed-{index}", status="queued", html_body=None, text_body="missing html")
        for index in range(2)
    ]
    valid = EmailMessage(recipient="valid@example.test", subject="Good", kind="order_update",
                         idempotency_key="valid-after-malformed", status="queued",
                         html_body="<p>Good</p>", text_body="Good")
    db.session.add_all([*malformed, valid])
    db.session.commit()

    counts = customer_mail.process_email_queue(limit=1)

    assert counts["failed"] == 2
    assert counts["sent"] == 1
    assert sent == ["valid@example.test"]
    for row in malformed:
        db.session.refresh(row)
        assert row.status == "failed"
        assert row.error_category == "legacy_payload_review"
        assert "incomplete" in row.failure_reason
        assert row.html_body is None and row.text_body == "missing html"
        from app.models import EmailLegacyPayloadReviewEvent
        event = EmailLegacyPayloadReviewEvent.query.filter_by(message_id=row.id).one()
        assert event.actor_type == "system"
        assert event.provider_outcome == "unknown"


def test_general_worker_does_not_process_unlinked_marketing_rows(app, app_context, monkeypatch):
    import app.services.customer_mail as customer_mail
    import app.services.marketing_eligibility as eligibility
    from app.models import MarketingSuppressionCase
    _configure_brevo_test(app)
    sent = []
    monkeypatch.setattr(customer_mail, "_send_brevo", lambda recipient, *_args: sent.append(recipient) or "retry-id")
    account = CustomerAccount(
        email="eligibility-retry@example.test", display_name="Retry",
        email_verified_at=datetime.utcnow(), marketing_status="subscribed",
        marketing_consent_at=datetime.utcnow(),
    )
    db.session.add(account)
    db.session.commit()
    row = customer_mail.queue_email(account.email, "Campaign", "marketing_campaign", "eligibility-retry",
                                    account_id=account.id, html_body="<p>Offer</p>", text_body="Offer")
    monkeypatch.setattr(eligibility, "evaluate_marketing_eligibility", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("database unavailable")))

    first_counts = customer_mail.process_email_queue(limit=5)
    db.session.refresh(row)
    assert first_counts["sent"] == 0
    assert first_counts["failed"] == 0
    assert row.status == "queued"
    assert sent == []
    assert MarketingSuppressionCase.query.filter_by(account_id=account.id, cleared_at=None).count() == 0

    retry_counts = customer_mail.process_email_queue(limit=5)
    db.session.refresh(row)
    assert retry_counts["sent"] == 0
    assert retry_counts["failed"] == 0
    assert row.status == "queued"
    assert sent == []


def test_transactional_email_is_independent_of_marketing_consent(app, app_context, monkeypatch):
    import app.services.customer_mail as customer_mail
    _configure_brevo_test(app)
    sent = []
    monkeypatch.setattr(customer_mail, "_send_brevo", lambda recipient, *_args: sent.append(recipient) or "transaction-id")
    account = CustomerAccount(email="transactional@example.test", display_name="No Consent", marketing_status="none")
    db.session.add(account)
    db.session.commit()
    row = customer_mail.queue_email(account.email, "Order update", "order_status", "transactional-no-consent",
                                    account_id=account.id, html_body="<p>Order status</p>", text_body="Order status")

    assert row.purpose == "transactional"
    assert row.status == "sent"
    assert customer_mail.process_email_queue(limit=5)["sent"] == 0
    assert sent == [account.email]


def test_email_outbox_migration_is_checksum_tracked_and_repeat_safe(app, app_context):
    from app.services.email_outbox_schema import apply_email_outbox_migration, validate_email_outbox_schema
    message = EmailMessage(
        recipient="legacy@example.test", subject="Legacy", kind="marketing_campaign",
        idempotency_key="legacy-purpose", purpose="transactional", frequency_override=True,
        status="queued", html_body="<p>Legacy</p>", text_body="Legacy",
    )
    db.session.add(message)
    db.session.commit()
    with db.engine.begin() as connection:
        applied = apply_email_outbox_migration(connection, "sqlite")
    db.session.refresh(message)
    assert applied is True
    assert message.purpose == "marketing"
    assert message.frequency_override is False
    original_subject = message.subject
    with db.engine.begin() as connection:
        reapplied = apply_email_outbox_migration(connection, "sqlite")
        validate_email_outbox_schema(connection)
    db.session.refresh(message)
    assert reapplied is False
    assert message.subject == original_subject


def test_email_outbox_migration_adds_sqlite_datetime_columns_to_legacy_table():
    from sqlalchemy import create_engine, inspect
    from app.services.email_outbox_schema import apply_email_outbox_migration, validate_email_outbox_schema

    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE email_message (id INTEGER PRIMARY KEY, recipient VARCHAR(254) NOT NULL, "
                "status VARCHAR(24) NOT NULL, kind VARCHAR(32) NOT NULL, created_at DATETIME NOT NULL, "
                "provider_message_id VARCHAR(255), retry_count INTEGER NOT NULL DEFAULT 0)"
            )
            connection.exec_driver_sql("CREATE TABLE marketing_campaign_run (id INTEGER PRIMARY KEY)")
            assert apply_email_outbox_migration(connection, "sqlite") is True
            columns = {column["name"]: column for column in inspect(connection).get_columns("email_message")}
            for name in ("claimed_at", "last_attempt_at", "next_attempt_at"):
                assert columns[name]["type"].compile(dialect=connection.dialect).upper() == "DATETIME"
            validate_email_outbox_schema(connection)
    finally:
        engine.dispose()


def test_queue_email_rejects_truncated_or_rebound_idempotency_keys(app, app_context):
    from app.services.customer_mail import queue_email

    with pytest.raises(ValueError, match="cannot exceed 160"):
        queue_email("key-limit@example.test", "Test", "order_received", "x" * 161,
                    html_body="<p>test</p>", text_body="test")
    first = queue_email("first@example.test", "Test", "order_received", "bound-key",
                        html_body="<p>test</p>", text_body="test")
    with pytest.raises(ValueError, match="different recipient or context"):
        queue_email("second@example.test", "Test", "order_received", "bound-key",
                    html_body="<p>test</p>", text_body="test")
    assert EmailMessage.query.filter_by(idempotency_key="bound-key").count() == 1
    assert first.recipient == "first@example.test"


def test_stale_claim_recovery_marks_linked_attempt_ambiguous(app, app_context):
    from datetime import timedelta
    from app.models import EmailDeliveryAttempt
    from app.services.customer_mail import recover_stale_email_claims

    now = datetime.utcnow()
    row = EmailMessage(
        recipient="stale@example.test", subject="Test", kind="order_received",
        idempotency_key="stale-attempt-message", status="sending",
        claim_token="stale-attempt-key", claimed_at=now - timedelta(hours=1),
        html_body="<p>test</p>", text_body="test",
    )
    db.session.add(row)
    db.session.flush()
    attempt = EmailDeliveryAttempt(
        message_id=row.id, attempt_number=1, attempt_key="stale-attempt-key",
        provider="mock", status="in_flight", started_at=now - timedelta(hours=1),
    )
    db.session.add(attempt)
    db.session.commit()

    assert recover_stale_email_claims(age_minutes=30, now=now) == 1
    db.session.refresh(row)
    db.session.refresh(attempt)
    assert row.status == "failed"
    assert row.error_category == "ambiguous_outcome"
    assert row.html_body is None
    assert row.text_body is None
    assert attempt.status == "ambiguous"
    assert attempt.error_category == "ambiguous_outcome"
    assert attempt.completed_at is not None


def test_customer_data_erasure_guards_are_scoped_and_idempotent_on_sqlite():
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import DBAPIError
    from app.services.consent_audit import (
        apply_consent_audit_migration,
        apply_customer_data_erasure_migration,
    )

    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.execute(text(
                "CREATE TABLE customer_account ("
                "id INTEGER PRIMARY KEY, email TEXT NOT NULL, display_name TEXT NOT NULL, "
                "password_hash TEXT, google_sub TEXT, email_verified_at DATETIME, "
                "two_factor_enabled BOOLEAN NOT NULL DEFAULT 0, marketing_status TEXT NOT NULL, "
                "marketing_consent_at DATETIME, marketing_withdrawn_at DATETIME, "
                "active BOOLEAN NOT NULL)"
            ))
            connection.execute(text(
                "CREATE TABLE customer_consent_event (id INTEGER PRIMARY KEY, "
                "account_id INTEGER NOT NULL, event TEXT, method TEXT, created_at DATETIME)"
            ))
            connection.execute(text(
                "CREATE TABLE email_message (id INTEGER PRIMARY KEY, account_id INTEGER)"
            ))
            connection.execute(text(
                "CREATE TABLE email_delivery_investigation (id INTEGER PRIMARY KEY, message_id INTEGER)"
            ))
            connection.execute(text(
                "CREATE TABLE email_delivery_investigation_event "
                "(id INTEGER PRIMARY KEY, investigation_id INTEGER)"
            ))
            connection.execute(text(
                "CREATE TABLE email_legacy_payload_review_event "
                "(id INTEGER PRIMARY KEY, message_id INTEGER)"
            ))
            connection.execute(text(
                "CREATE TABLE marketing_suppression_event (id INTEGER PRIMARY KEY, account_id INTEGER)"
            ))
            connection.execute(text("CREATE TABLE email_schema_migration (migration_id VARCHAR(120) PRIMARY KEY, checksum VARCHAR(64) NOT NULL)"))
            assert apply_consent_audit_migration(connection, "sqlite") is True
            assert apply_consent_audit_migration(connection, "sqlite") is False
            assert apply_customer_data_erasure_migration(connection, "sqlite") is True
            assert apply_customer_data_erasure_migration(connection, "sqlite") is False
            connection.execute(text(
                "INSERT INTO customer_account "
                "(id, email, display_name, password_hash, google_sub, email_verified_at, "
                "two_factor_enabled, marketing_status, marketing_consent_at, "
                "marketing_withdrawn_at, active) "
                "VALUES (10, 'customer@example.test', 'Customer', 'hash', 'google', "
                "CURRENT_TIMESTAMP, 1, 'subscribed', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 1), "
                "(11, 'other@example.test', 'Other', NULL, NULL, NULL, 0, "
                "'unsubscribed', NULL, NULL, 1)"
            ))
            connection.execute(text(
                "INSERT INTO email_message (id, account_id) VALUES (1, 10), (2, 11)"
            ))
            connection.execute(text(
                "INSERT INTO email_delivery_investigation (id, message_id) "
                "VALUES (1, 1), (2, 2)"
            ))
            connection.execute(text(
                "INSERT INTO email_delivery_investigation_event (id, investigation_id) "
                "VALUES (1, 1), (2, 2)"
            ))
            connection.execute(text(
                "INSERT INTO email_legacy_payload_review_event (id, message_id) "
                "VALUES (1, 1), (2, 2)"
            ))
            connection.execute(text(
                "INSERT INTO marketing_suppression_event (id, account_id) "
                "VALUES (1, 10), (2, 11)"
            ))
            connection.execute(text(
                "INSERT INTO customer_consent_event "
                "(id, account_id, event, method, created_at) "
                "VALUES (1, 10, 'consent_granted', 'test', CURRENT_TIMESTAMP)"
            ))
            with pytest.raises(DBAPIError):
                connection.execute(text("UPDATE customer_consent_event SET method='changed' WHERE id=1"))
            with pytest.raises(DBAPIError):
                connection.execute(text("DELETE FROM customer_consent_event WHERE id=1"))
            for table in (
                "marketing_suppression_event",
                "email_delivery_investigation_event",
                "email_legacy_payload_review_event",
            ):
                with pytest.raises(DBAPIError):
                    connection.execute(text(f"DELETE FROM {table} WHERE id=1"))
            for statement in (
                "UPDATE marketing_suppression_event SET account_id=11 WHERE id=1",
                "UPDATE email_delivery_investigation_event SET investigation_id=2 WHERE id=1",
                "UPDATE email_legacy_payload_review_event SET message_id=2 WHERE id=1",
            ):
                with pytest.raises(DBAPIError):
                    connection.execute(text(statement))
            assert connection.execute(text("SELECT count(*) FROM customer_consent_event")).scalar_one() == 1
            connection.execute(text(
                "UPDATE customer_account SET email='removed-customer-10@redacted.invalid', "
                "display_name='Deleted customer', password_hash=NULL, google_sub=NULL, "
                "email_verified_at=NULL, two_factor_enabled=0, marketing_status='unsubscribed', "
                "marketing_consent_at=NULL, marketing_withdrawn_at=NULL, active=0 WHERE id=10"
            ))
            with pytest.raises(DBAPIError):
                connection.execute(text(
                    "UPDATE customer_consent_event SET method='still immutable' WHERE id=1"
                ))
            connection.execute(text("DELETE FROM customer_consent_event WHERE id=1"))
            connection.execute(text("DELETE FROM marketing_suppression_event WHERE id=1"))
            connection.execute(text("DELETE FROM email_delivery_investigation_event WHERE id=1"))
            connection.execute(text("DELETE FROM email_legacy_payload_review_event WHERE id=1"))
            assert connection.execute(
                text("SELECT count(*) FROM customer_consent_event")
            ).scalar_one() == 0
            for table in (
                "marketing_suppression_event",
                "email_delivery_investigation_event",
                "email_legacy_payload_review_event",
            ):
                with pytest.raises(DBAPIError):
                    connection.execute(text(f"DELETE FROM {table} WHERE id=2"))
    finally:
        engine.dispose()


def test_legacy_email_admin_routes_require_admin_identity(client, app):
    from app.models import CustomerAccount
    from app.customer.routes import TERMS_ACCEPTANCE_VERSION

    assert client.get("/admin/email").status_code == 302
    with app.app_context():
        account = CustomerAccount(email="not-admin@example.test", display_name="Customer", email_verified_at=datetime.utcnow())
        db.session.add(account)
        db.session.flush()
        db.session.add(CustomerConsentEvent(account_id=account.id, event="terms_accepted", method="test", wording_version=TERMS_ACCEPTANCE_VERSION))
        db.session.commit()
        customer_id = account.get_id()
    with client.session_transaction() as browser_session:
        browser_session["_user_id"] = customer_id
        browser_session["_fresh"] = True
    assert client.get("/admin/email").status_code == 403
    assert client.get("/admin/email/studio").status_code == 403
    assert client.post("/admin/email/studio", data={"action": "send_test"}).status_code == 403
    assert client.get("/admin/email/studio-messages").status_code == 403
    _sign_in_admin(client, app)
    app.config["WTF_CSRF_ENABLED"] = True
    assert client.post("/admin/email/send-manual", data={}).status_code == 400
    assert client.post("/admin/email/studio", data={"action": "send_test"}).status_code == 400


def test_expired_authentication_payload_cleanup_stops_retry_and_erases_body(app, app_context):
    from datetime import timedelta
    from app.services.email_retention import cleanup_email_payloads

    now = datetime.utcnow()
    row = EmailMessage(
        recipient="expired-auth@example.test", subject="Verify", kind="verify_code",
        idempotency_key="expired-auth-retry", status="failed",
        error_category="retryable_provider", retry_count=1,
        next_attempt_at=now + timedelta(minutes=10),
        html_body="<p>123456</p>", text_body="123456",
        created_at=now - timedelta(minutes=11), updated_at=now,
    )
    db.session.add(row)
    db.session.commit()

    result = cleanup_email_payloads(limit=10, dry_run=False, now=now)
    db.session.refresh(row)
    assert result["expired_auth_payloads"] == 1
    assert row.status == "failed"
    assert row.error_category == "expired_auth_payload"
    assert row.next_attempt_at is None
    assert row.html_body is None and row.text_body is None


def test_admin_test_purpose_migration_corrects_only_legacy_test_rows():
    from sqlalchemy import create_engine, text
    from app.services.email_test_purpose_migration import apply_email_test_purpose_migration

    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE email_message (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, purpose TEXT NOT NULL)"))
            connection.execute(text("CREATE TABLE email_schema_migration (migration_id VARCHAR(120) PRIMARY KEY, checksum VARCHAR(64) NOT NULL)"))
            connection.execute(text("INSERT INTO email_message VALUES (1, 'admin_test', 'transactional'), (2, 'order_received', 'transactional')"))
            assert apply_email_test_purpose_migration(connection, "sqlite") is True
            assert apply_email_test_purpose_migration(connection, "sqlite") is False
            rows = connection.execute(text("SELECT id, purpose FROM email_message ORDER BY id")).all()
            assert rows == [(1, "test"), (2, "transactional")]
    finally:
        engine.dispose()


def test_admin_email_center_uses_campaign_workflow_and_preserves_transactional_tools(client, app):
    _sign_in_admin(client, app)

    response = client.get("/admin/email")

    assert response.status_code == 200
    assert b"Promotional Campaign Workflow" in response.data
    assert b'href="/admin/email/campaigns"' in response.data
    assert client.get("/admin/email/campaigns").status_code == 200
    assert b"Open Email Studio" in response.data
    assert b"Email queue and recent history" in response.data
    assert b"Send a promotional email" not in response.data
    assert b"Send an administrator-authored message" not in response.data
    assert b"Service message subject" not in response.data
    assert b"For multiple recipients, type the exact confirmation" not in response.data
    assert b'name="confirm_send"' not in response.data
    assert b'name="service_confirm_send"' not in response.data
    assert b'url_for("admin.send_manual_customer_email")' not in response.data
    assert b'url_for("admin.send_service_customer_email")' not in response.data
