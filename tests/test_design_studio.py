import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import inspect
from werkzeug.security import generate_password_hash

from app import create_app
from app.extensions import db
from app.models import AdminUser, Category, CustomerAccount, DesignStudioRole, Product, ProductVariant, ShopSettings, StorefrontDesign, StorefrontDesignAudit, StorefrontDesignRevision
from app.services.design_studio import (
    DESIGN_TABLES, ensure_design_studio_schema, role_for, sanitize_design_html, save_revision, validate_design_payload,
)
from config import TestConfig


@pytest.fixture
def app():
    application = create_app(TestConfig)
    with application.app_context():
        db.create_all()
        category = Category(name="Socks", slug="socks")
        db.session.add_all([
            ShopSettings(shop_name="FITY", tagline="FITY"),
            category,
            AdminUser(username="owner", password_hash=generate_password_hash("test-password")),
        ])
        db.session.flush()
        db.session.add(Product(
            name="Plain Socks", slug="plain-socks", category=category,
            description="Everyday socks", price_cents=15000, stock_quantity=4,
            active=True,
        ))
        db.session.commit()
    yield application
    with application.app_context():
        db.session.remove()


@pytest.fixture
def client(app):
    return app.test_client()


def _sign_in(client, admin_id):
    with client.session_transaction() as browser_session:
        browser_session["_user_id"] = str(admin_id)
        browser_session["_fresh"] = True


def _design(**overrides):
    values = {
        "name": "Weekend picks",
        "title": "Fresh finds",
        "description": "Everyday favourites",
        "placement": "storefront_banner",
        "status": "published",
        "approved_by_id": 1,
        "is_enabled": True,
        "html": '<section><h2>Fresh finds</h2>%%FITYSTORES_PRODUCTS%%</section>',
        "css": ".promo{color:#222}",
        "javascript": "",
        "product_ids": "[]",
        "template_key": "editorial",
        "motion_preset": "none",
        "motion_config": '{"duration":600}',
        "version": 1,
    }
    values.update(overrides)
    return StorefrontDesign(**values)


def test_schema_migration_is_repeatable_and_bootstraps_roles(app):
    with app.app_context():
        ensure_design_studio_schema()
        ensure_design_studio_schema()
        assignments = DesignStudioRole.query.order_by(DesignStudioRole.admin_user_id).all()
        assert len(assignments) == 1
        assert assignments[0].role == "owner"
        assert StorefrontDesign.__tablename__ in inspect(db.engine).get_table_names()


def test_schema_migration_cli_is_explicit_and_idempotent(app):
    runner = app.test_cli_runner()
    first = runner.invoke(args=["migrate-design-studio-schema"])
    second = runner.invoke(args=["migrate-design-studio-schema"])
    assert first.exit_code == 0
    assert second.exit_code == 0
    assert "schema is ready" in first.output


def test_schema_migration_restores_owner_if_assignments_exist_without_one(app):
    with app.app_context():
        first_admin = AdminUser.query.filter_by(username="owner").one()
        second_admin = AdminUser(username="second", password_hash="test")
        db.session.add(second_admin)
        db.session.flush()
        db.session.add_all([
            DesignStudioRole(admin_user_id=first_admin.id, role="viewer"),
            DesignStudioRole(admin_user_id=second_admin.id, role="designer"),
        ])
        db.session.commit()
        ensure_design_studio_schema()
        assert DesignStudioRole.query.filter_by(admin_user_id=first_admin.id).one().role == "owner"
        assert DesignStudioRole.query.filter_by(admin_user_id=second_admin.id).one().role == "designer"


def test_new_admin_receives_a_conservative_studio_role(app):
    with app.app_context():
        ensure_design_studio_schema()
        admin = AdminUser(username="new-admin", password_hash="test")
        db.session.add(admin)
        db.session.flush()
        admin_id = admin.id
        assert role_for(admin) == "designer"
        assert DesignStudioRole.query.filter_by(admin_user_id=admin_id).one().role == "designer"


def test_design_studio_ui_is_admin_only_and_role_scoped(app, client):
    with app.app_context():
        ensure_design_studio_schema()
        owner_id = AdminUser.query.filter_by(username="owner").one().id
        customer = CustomerAccount(id=owner_id, email="shopper@example.test", display_name="Shopper")
        db.session.add(customer)
        db.session.flush()
        customer_id = customer.id
        designer = AdminUser(username="viewer", password_hash="test")
        db.session.add(designer)
        db.session.flush()
        db.session.add(DesignStudioRole(admin_user_id=designer.id, role="viewer"))
        designer_id = designer.id
        db.session.commit()
    assert client.get("/admin/design-studio").status_code == 302
    _sign_in(client, owner_id)
    assert client.get("/admin/design-studio").status_code == 200
    assert client.get("/admin/design-studio/new").status_code == 200
    _sign_in(client, designer_id)
    assert client.get("/admin/design-studio").status_code == 200
    assert client.get("/admin/design-studio/new").status_code == 403
    _sign_in(client, f"customer:{customer_id}")
    assert client.get("/admin/design-studio").status_code == 403
    assert client.get("/admin/design-studio/new").status_code == 403


def test_new_design_uses_live_offers_and_previews_unsaved_edits(app, client):
    with app.app_context():
        ensure_design_studio_schema()
        owner_id = AdminUser.query.filter_by(username="owner").one().id
        product = Product.query.filter_by(slug="plain-socks").one()
        product.previous_price_cents = 18000
        db.session.add(ProductVariant(
            product=product,
            size="Regular",
            price_cents=15000,
            previous_price_cents=18000,
            stock_quantity=4,
            active=True,
        ))
        db.session.commit()
        product_id = product.id
    _sign_in(client, owner_id)

    editor = client.get("/admin/design-studio/new")
    assert editor.status_code == 200
    assert b'data-template="offers"' in editor.data
    assert b'data-studio-product-picker' in editor.data
    assert b'data-studio-lottie-target' in editor.data
    assert b'catalogue product below to add it to this design' in editor.data
    assert b'data-discount-percent="16"' in editor.data
    for template_key in (
        "deal-of-day", "black-friday-feature", "smartphones", "beauty", "home",
    ):
        assert f'data-template="{template_key}"'.encode() in editor.data
    assert b'data-template-cutouts' in editor.data
    assert f'dataset.selectedProducts="[{product_id}]"'.encode() in editor.data
    assert b'data-preview-url="/admin/design-studio/preview"' in editor.data

    initial_preview = client.get("/admin/design-studio/preview")
    assert initial_preview.status_code == 200
    assert b"Discounted" in initial_preview.data
    assert b"Plain Socks" in initial_preview.data
    assert b"offers-product-pedestal-base" in initial_preview.data
    assert b"16% off" in initial_preview.data
    assert b"offers-original-price" in initial_preview.data
    assert b"%%FITYSTORES_OFFERS%%" not in initial_preview.data
    homepage = client.get("/")
    assert homepage.status_code == 200
    assert b"offers-product-pedestal-base" in homepage.data
    with app.app_context():
        assert StorefrontDesign.query.count() == 0

    updated_preview = client.post("/admin/design-studio/preview", data={
        "name": "",
        "title": "Unsaved campaign",
        "description": "",
        "placement": "storefront_section",
        "html": '<section><script>bad()</script><h2>Unsaved campaign</h2></section>',
        "css": ".draft{scroll-behavior:auto!important}",
        "javascript": "",
        "lottie_json": "",
        "product_ids": [str(product_id)],
        "template_key": "offers",
        "motion_preset": "none",
        "motion_config": '{"duration":600}',
        "starts_at": "",
        "expires_at": "",
    })
    assert updated_preview.status_code == 200
    assert b"<h2>Unsaved campaign</h2>" in updated_preview.data
    assert b"scroll-behavior:auto!important" in updated_preview.data
    assert b"bad()" not in updated_preview.data
    assert b"Plain Socks" not in updated_preview.data
    with app.app_context():
        assert StorefrontDesign.query.count() == 0


def test_product_grid_template_preview_includes_layered_product_pedestals(app, client):
    with app.app_context():
        ensure_design_studio_schema()
        owner_id = AdminUser.query.filter_by(username="owner").one().id
        product = Product.query.filter_by(slug="plain-socks").one()
        product.previous_price_cents = 18000
        db.session.commit()
        product_id = product.id
    _sign_in(client, owner_id)

    response = client.post("/admin/design-studio/preview", data={
        "name": "Pedestal product grid",
        "title": "Product picks",
        "description": "",
        "placement": "storefront_section",
        "html": "<section>%%FITYSTORES_PRODUCTS%%</section>",
        "css": ".studio-product-grid{display:grid}",
        "javascript": "",
        "lottie_json": "",
        "product_ids": [str(product_id)],
        "template_key": "product-grid",
        "motion_preset": "none",
        "motion_config": '{"duration":600}',
        "starts_at": "",
        "expires_at": "",
    })

    assert response.status_code == 200
    assert b"studio-product-pedestal-base" in response.data
    assert b"studio-product-pedestal-top" in response.data
    assert b"studio-product-discount" in response.data
    assert b"16% OFF" in response.data


def test_editor_prefills_all_active_discounted_catalogue_products(app, client):
    with app.app_context():
        ensure_design_studio_schema()
        owner_id = AdminUser.query.filter_by(username="owner").one().id
        category = Product.query.filter_by(slug="plain-socks").one().category
        products = [
            Product(
                name=f"Sale item {index}",
                slug=f"sale-item-{index}",
                category=category,
                price_cents=1000,
                previous_price_cents=2000,
                stock_quantity=5,
                active=True,
            )
            for index in range(10)
        ]
        variant_sale = Product(
            name="Variant sale",
            slug="variant-sale",
            category=category,
            price_cents=10000,
            stock_quantity=5,
            active=True,
        )
        variant_sale.variants.append(ProductVariant(
            size="Small",
            price_cents=8000,
            previous_price_cents=10000,
            stock_quantity=5,
            active=True,
        ))
        products.append(variant_sale)
        db.session.add_all(products)
        db.session.commit()
        product_ids = {product.id for product in products}
    _sign_in(client, owner_id)

    editor = client.get("/admin/design-studio/new")
    assert editor.status_code == 200
    selected_json = editor.data.split(b'dataset.selectedProducts="', 1)[1].split(b'"', 1)[0]
    assert set(json.loads(selected_json.decode())) == product_ids
    assert editor.data.count(b'data-discount-percent="50"') == 10
    assert b'data-discount-percent="20"' in editor.data
    assert b'data-current-price="KSh 80"' in editor.data
    assert b'data-studio-card-builder' in editor.data
    assert b'value="/product/plain-socks"' in editor.data
    assert b'data-current-cents="15000"' in editor.data


def test_design_code_validation_sanitizes_markup_and_rejects_unsafe_operations(app):
    with app.app_context():
        product = Product.query.filter_by(slug="plain-socks").one()
        payload = validate_design_payload({
            "name": "Safe markup",
            "html": '<section onclick="alert(1)"><script>alert(1)</script><img src="javascript:alert(1)"><h2>Sale</h2></section>',
            "product_ids": [product.id],
        })
        assert "onclick" not in payload["html"]
        assert "<script" not in payload["html"]
        assert "javascript:" not in payload["html"]
        assert "<h2>Sale</h2>" in payload["html"]
        with pytest.raises(ValueError, match="Network APIs"):
            validate_design_payload({"name": "Unsafe", "javascript": "fetch('/api')"})
        with pytest.raises(ValueError, match="Network APIs"):
            validate_design_payload({"name": "Unsafe", "javascript": "setTimeout(() => {}, 1)"})
        with pytest.raises(ValueError, match="CSS imports and executable CSS"):
            validate_design_payload({"name": "Unsafe CSS", "css": ".legacy{behavior:url(#default#VML)}"})
        with pytest.raises(ValueError, match="CSS imports and executable CSS"):
            validate_design_payload({"name": "Obfuscated CSS", "css": ".legacy{behavior/**/:url(#default#VML)}"})
        assert "scroll-behavior:auto!important" in validate_design_payload({
            "name": "Accessible motion",
            "css": ".campaign *{scroll-behavior:auto!important}",
        })["css"]
        assert validate_design_payload({"name": "Worker loop", "javascript": "while (true) {}"})["javascript"] == "while (true) {}"
        assert sanitize_design_html('<iframe src="https://evil.test"></iframe><p>ok</p>') == "<p>ok</p>"
        product_card = sanitize_design_html(
            '<article><a href="/product/plain-socks"><img src="/static/socks.webp" alt="Socks">'
            '<s>KSh 180</s></a><a href="https://evil.test">unsafe</a></article>'
        )
        assert 'href="/product/plain-socks"' in product_card
        assert "<s>KSh 180</s>" in product_card
        assert 'href="https://evil.test"' not in product_card


@pytest.mark.parametrize(
    "template_key",
    ["deal-of-day", "black-friday-feature", "smartphones", "beauty", "home"],
)
def test_campaign_templates_keep_editable_safe_template_metadata(app, template_key):
    with app.app_context():
        payload = validate_design_payload({
            "name": "Editable campaign",
            "template_key": template_key,
            "html": (
                f'<section class="ds-campaign" data-template-key="{template_key}" '
                'data-template-headline="Smartphones" data-template-cutouts="true">'
                "<h2>Smartphones</h2></section>"
            ),
        })

        assert payload["template_key"] == template_key
        assert 'data-template-headline="Smartphones"' in payload["html"]
        assert 'data-template-cutouts="true"' in payload["html"]


def test_lottie_json_is_bounded_and_rejects_external_assets():
    animation = {
        "v": "5.12.2", "fr": 30, "ip": 0, "op": 60,
        "w": 320, "h": 180, "layers": [{"ty": 4, "ind": 1, "shapes": []}],
    }
    payload = validate_design_payload({
        "name": "Animated banner",
        "html": '<div data-studio-lottie aria-hidden="true"></div>',
        "lottie_json": json.dumps(animation),
    })
    assert json.loads(payload["lottie_json"]) == animation
    external_animation = dict(animation, assets=[{"u": "https://example.test/", "p": "image.png"}])
    with pytest.raises(ValueError, match="external files"):
        validate_design_payload({"name": "Remote animation", "lottie_json": json.dumps(external_animation)})
    too_long_animation = dict(animation, fr=1, op=31)
    with pytest.raises(ValueError, match="duration"):
        validate_design_payload({"name": "Long animation", "lottie_json": json.dumps(too_long_animation)})
    with pytest.raises(ValueError, match="300 KB"):
        validate_design_payload({"name": "Large animation", "lottie_json": " " * 300_001})


def test_visual_editor_metadata_survives_markup_sanitization():
    markup = (
        '<section data-studio-product-canvas data-template-key="deal-of-day" data-studio-lottie-target="p-12">'
        '<p data-studio-edit-id="e12" data-studio-edit-field="headline">Today only</p>'
        '<article data-studio-card-key="c-uploaded-card">Product</article>'
        '<article data-studio-card-key="p-12">Invalid custom key</article>'
        '<div data-studio-lottie-target="javascript:alert(1)"></div>'
        '</section>'
    )

    sanitized = sanitize_design_html(markup)

    assert 'data-studio-product-canvas=""' in sanitized
    assert 'data-template-key="deal-of-day"' in sanitized
    assert 'data-studio-lottie-target="p-12"' in sanitized
    assert 'data-studio-lottie-target="javascript:alert(1)"' not in sanitized
    assert 'data-studio-edit-id="e12"' in sanitized
    assert 'data-studio-edit-field="headline"' in sanitized
    assert 'data-studio-card-key="c-uploaded-card"' in sanitized
    assert 'data-studio-card-key="p-12"' not in sanitized


def test_public_placements_obey_schedule_and_private_page_exclusions(app, client):
    with app.app_context():
        ensure_design_studio_schema()
        now = datetime.utcnow()
        db.session.add_all([
            _design(starts_at=now - timedelta(minutes=1), expires_at=now + timedelta(minutes=1)),
            _design(name="Future", starts_at=now + timedelta(minutes=1)),
            _design(name="Expired", starts_at=now - timedelta(days=2), expires_at=now - timedelta(days=1)),
            _design(name="Disabled", is_enabled=False),
            _design(name="Pending", status="pending"),
        ])
        db.session.commit()
    home = client.get("/")
    assert home.status_code == 200
    assert home.data.count(b"data-fity-studio-frame") == 1
    checkout = client.get("/checkout", follow_redirects=True)
    assert checkout.status_code == 200
    assert b"data-fity-studio-frame" not in checkout.data
    assert b"data-fity-studio-frame" not in client.get("/cart").data
    assert b"data-fity-studio-frame" not in client.get("/track").data
    assert b"data-fity-studio-frame" not in client.get("/account/login").data


def test_storefront_fails_closed_when_studio_schema_is_absent(app, client):
    with app.app_context():
        db.metadata.drop_all(bind=db.engine, tables=list(DESIGN_TABLES))
    response = client.get("/")
    assert response.status_code == 200
    assert b"data-fity-studio-frame" not in response.data


def test_sandbox_headers_and_live_product_api_validation(app, client):
    with app.app_context():
        ensure_design_studio_schema()
        product = Product.query.filter_by(slug="plain-socks").one()
        design = _design(
            product_ids=json.dumps([product.id]),
            javascript="FityStudio.on('ready', () => FityStudio.resize(240));",
        )
        db.session.add(design)
        db.session.commit()
        design_id = design.id
        product_id = product.id
    response = client.get(f"/design-studio/render/{design_id}")
    assert response.status_code == 200
    assert "default-src 'none'" in response.headers["Content-Security-Policy"]
    assert "sandbox allow-scripts" in response.headers["Content-Security-Policy"]
    assert "worker-src blob:" in response.headers["Content-Security-Policy"]
    assert b'id="studio-user-code"' in response.data
    assert b"studio-event-complete" in response.data
    assert "unsafe-eval" not in response.headers["Content-Security-Policy"]
    assert b"Plain Socks" in response.data
    valid = client.post("/api/design-studio/validate-product", json={"product_id": product_id, "quantity": 4})
    assert valid.status_code == 200
    unavailable = client.post("/api/design-studio/validate-product", json={"product_id": product_id, "quantity": 5})
    assert unavailable.status_code == 409
    assert unavailable.json["message"] == "This product or option is no longer available in the requested quantity."
    assert client.post("/api/design-studio/validate-product", json={"product_id": 999999, "quantity": 1}).status_code == 404


def test_published_lottie_uses_the_local_player_and_self_contained_data(app, client):
    animation = {
        "v": "5.12.2", "fr": 30, "ip": 0, "op": 60,
        "w": 320, "h": 180, "layers": [{"ty": 4, "ind": 1, "shapes": []}],
    }
    with app.app_context():
        ensure_design_studio_schema()
        product = Product.query.filter_by(slug="plain-socks").one()
        design = _design(
            html=f'<section data-studio-lottie-target="p-{product.id}">%%FITYSTORES_PRODUCTS%%</section>',
            product_ids=json.dumps([product.id]),
            lottie_json=json.dumps(animation),
        )
        db.session.add(design)
        db.session.commit()
        product_id = product.id
        design_id = design.id
    response = client.get(f"/design-studio/render/{design_id}")
    assert response.status_code == 200
    assert b"/static/vendor/lottie.min.js" in response.data
    assert b'id="fity-lottie-data"' in response.data
    assert b"lottie.loadAnimation" in response.data
    assert f'data-studio-card-key="p-{product_id}"'.encode() in response.data
    assert b"studio-lottie-premium-background" in response.data
    assert "http://example.test/static/" in response.headers["Content-Security-Policy"]


def test_designer_cannot_approve_or_publish_and_owner_can_rollback(app, client):
    with app.app_context():
        ensure_design_studio_schema()
        designer = AdminUser(username="designer", password_hash="test")
        db.session.add(designer)
        db.session.flush()
        db.session.add(DesignStudioRole(admin_user_id=designer.id, role="designer"))
        design = _design(status="approved")
        db.session.add(design)
        db.session.commit()
        save_revision(design, AdminUser.query.filter_by(username="owner").one(), "created")
        db.session.commit()
        designer_id, design_id = designer.id, design.id
        owner_id = AdminUser.query.filter_by(username="owner").one().id
    _sign_in(client, designer_id)
    denied = client.post(f"/admin/design-studio/{design_id}/workflow", data={"action": "publish"})
    assert denied.status_code == 403
    _sign_in(client, owner_id)
    response = client.post("/admin/design-studio/save", data={
        "design_id": str(design_id), "name": "Edited", "title": "", "description": "",
        "placement": "storefront_banner", "html": "<section>Changed</section>",
        "css": "", "javascript": "", "product_ids": [], "template_key": "editorial",
        "motion_preset": "none", "motion_config": '{"duration":700}',
    })
    assert response.status_code == 302
    with app.app_context():
        design = db.session.get(StorefrontDesign, design_id)
        revisions = StorefrontDesignRevision.query.filter_by(design_id=design_id).order_by(StorefrontDesignRevision.revision_number).all()
        audit_events = StorefrontDesignAudit.query.filter_by(design_id=design_id).all()
        assert design.version == 2
        assert len(revisions) == 2
        assert any(event.action == "edited" for event in audit_events)
        revision_id = next(revision.id for revision in revisions if revision.revision_number == 1)
    restored = client.post(f"/admin/design-studio/{design_id}/rollback/{revision_id}")
    assert restored.status_code == 302
    with app.app_context():
        design = db.session.get(StorefrontDesign, design_id)
        assert design.version == 3
        assert design.status == "draft"
        assert "Fresh finds" in design.html
        assert StorefrontDesignAudit.query.filter_by(design_id=design_id, action="rolled_back").count() == 1


def test_owner_approval_is_required_before_publishing(app, client):
    with app.app_context():
        ensure_design_studio_schema()
        owner_id = AdminUser.query.filter_by(username="owner").one().id
        design = _design(status="pending")
        db.session.add(design)
        db.session.commit()
        design_id = design.id
    _sign_in(client, owner_id)
    approved = client.post(f"/admin/design-studio/{design_id}/workflow", data={"action": "approve"})
    assert approved.status_code == 302
    with app.app_context():
        design = db.session.get(StorefrontDesign, design_id)
        assert design.status == "approved"
        assert design.approved_by_id == owner_id
    published = client.post(f"/admin/design-studio/{design_id}/workflow", data={"action": "publish"})
    assert published.status_code == 302
    with app.app_context():
        design = db.session.get(StorefrontDesign, design_id)
        assert design.status == "published"
        assert design.published_by_id == owner_id
        assert StorefrontDesignAudit.query.filter_by(design_id=design_id, action="approve").count() == 1
        assert StorefrontDesignAudit.query.filter_by(design_id=design_id, action="publish").count() == 1
