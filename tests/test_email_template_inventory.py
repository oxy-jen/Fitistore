from app.services.email_purpose import (
    STUDIO_MARKETING_CATEGORY_KINDS,
    STUDIO_TRANSACTIONAL_CATEGORY_KINDS,
    email_purpose_for_kind,
    studio_email_kind_for_category,
)
from app.services.email_studio import EMAIL_CATEGORIES, EMAIL_PROMOTIONAL_LAYOUT_CATEGORIES
from app.services.email_template_inventory import SYSTEM_TEMPLATES, build_email_template_inventory
from flask import Flask
from pathlib import Path
from app.services.email_studio import render_studio_message


def test_all_studio_marketing_purposes_have_server_owned_marketing_kinds():
    expected = {
        "cart_reminder", "promotion", "daily_goodies", "weekly_picks",
        "new_product_announcement", "special_offers", "back_in_stock",
        "marketing_welcome", "re_engagement", "custom_campaign", "customer_service",
    }
    assert expected <= set(STUDIO_MARKETING_CATEGORY_KINDS)
    for category in expected:
        kind = STUDIO_MARKETING_CATEGORY_KINDS[category]
        assert email_purpose_for_kind(kind) == "marketing"
        assert EMAIL_CATEGORIES[category]["marketing"] is True


def test_only_code_defined_order_studio_categories_can_be_transactional():
    assert set(STUDIO_TRANSACTIONAL_CATEGORY_KINDS) == {
        "order_confirmation", "thank_you", "order_status",
    }
    for category in STUDIO_TRANSACTIONAL_CATEGORY_KINDS:
        assert EMAIL_CATEGORIES[category]["marketing"] is False
    assert EMAIL_CATEGORIES["customer_service"]["marketing"] is True


def test_customized_order_notice_loses_transactional_kind_and_stays_marketing():
    for category, transactional_kind in STUDIO_TRANSACTIONAL_CATEGORY_KINDS.items():
        assert studio_email_kind_for_category(category, marketing=False) == transactional_kind
        marketing_kind = studio_email_kind_for_category(category, marketing=True)
        assert marketing_kind == "marketing_campaign"
        assert email_purpose_for_kind(marketing_kind) == "marketing"
    assert studio_email_kind_for_category("unknown", marketing=True) is None


def test_promotional_campaign_layout_is_shared_by_campaign_purposes():
    assert {
        "promotion", "daily_goodies", "weekly_picks", "new_product_announcement",
        "special_offers", "back_in_stock", "marketing_welcome", "re_engagement",
        "custom_campaign", "customer_service",
    } <= EMAIL_PROMOTIONAL_LAYOUT_CATEGORIES
    assert "cart_reminder" not in EMAIL_PROMOTIONAL_LAYOUT_CATEGORIES


def test_inventory_groups_existing_and_missing_system_templates_without_duplicates():
    inventory = build_email_template_inventory(EMAIL_CATEGORIES)
    entries = [item for _, _, group in inventory for item in group]
    ids = [item["id"] for item in entries]
    assert len(ids) == len(set(ids))
    assert {"daily_goodies", "weekly_picks", "custom_campaign"} <= set(ids)
    assert "verify_email" in set(ids)
    assert "order_received" in set(ids)
    assert "refund_confirmation" in set(ids)
    assert next(item for item in entries if item["id"] == "refund_confirmation")["enabled"] is True
    assert next(item for item in entries if item["id"] == "verify_email")["preview"] is True


def test_operational_inventory_is_not_a_customer_campaign():
    operations = [item for item in SYSTEM_TEMPLATES if item["group"] == "operational"]
    assert operations
    assert all(item["implemented"] is False and item["preview"] is False for item in operations)


def test_daily_goodies_uses_the_promotional_brand_layout_without_sending():
    template_root = Path(__file__).resolve().parents[1] / "app" / "templates"
    flask_app = Flask("email-template-layout-test", template_folder=str(template_root))
    with flask_app.app_context():
        subject, html, text = render_studio_message(
            "daily_goodies", "Today's picks", "Fity finds", "A look at today's catalogue.",
            {"customer_name": "Sample", "unsubscribe_link": "https://example.invalid/unsubscribe"},
            blocks=[{"type": "heading", "text": "Fity finds", "align": "left"},
                    {"type": "paragraph", "text": "A look at today's catalogue.", "align": "left"}],
            shop_url="https://example.invalid/shop", logo_url="https://example.invalid/logo.png",
            site_url="https://example.invalid", contact_url="https://example.invalid/contact",
            unsubscribe_url="https://example.invalid/unsubscribe",
            preferences_url="https://example.invalid/preferences",
            whatsapp_url="https://example.invalid/whatsapp",
            social_icon_urls={name: "https://example.invalid/icon.png" for name in ("facebook", "instagram", "tiktok", "whatsapp")},
        )
    assert subject == "Today's picks"
    assert "Shop Smart," in html
    assert "Fity finds" in html
    assert "unsubscribe" in html
    assert "Fity finds" in text
