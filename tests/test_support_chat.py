from __future__ import annotations

from uuid import uuid4

import pytest
from werkzeug.security import generate_password_hash

from app import create_app
from app.extensions import db
from app.models import AdminUser, CustomerAccount, ShopSettings
from config import TestConfig


@pytest.fixture()
def support_app():
    app = create_app(TestConfig)
    app.config["SERVER_NAME"] = "localhost"
    app.config.update(
        SUPPORT_EMAIL="orders@fitystore.top",
        PUBLIC_BASE_URL="https://render.example.test",
        MAIL_FROM_VERIFIED=True,
    )
    with app.app_context():
        db.create_all()
        db.session.add_all(
            [
                ShopSettings(
                    shop_name="FITY",
                    tagline="Fits poa.",
                    whatsapp_number="254700000000",
                ),
                AdminUser(
                    username="support-owner",
                    password_hash=generate_password_hash("test-password"),
                ),
            ]
        )
        db.session.commit()
    yield app
    with app.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def support_client(support_app):
    return support_app.test_client()


def _sign_in(client, user_id):
    with client.session_transaction() as browser_session:
        browser_session["_user_id"] = user_id
        browser_session["_fresh"] = True


def test_guest_can_create_and_retrieve_only_their_support_conversation(support_app):
    guest = support_app.test_client()
    other_guest = support_app.test_client()

    created = guest.post("/support/conversations", json={"topic": "orders"})
    assert created.status_code == 201
    conversation_id = created.json["conversation"]["id"]
    assert created.json["conversation"]["status"] == "waiting"
    assert created.json["conversation"]["notification_status"] == "not_configured"

    sent = guest.post(
        f"/support/conversations/{conversation_id}/messages",
        json={"body": "Where is my order?", "client_message_id": str(uuid4())},
    )
    assert sent.status_code == 201
    assert sent.json["message"]["body"] == "Where is my order?"

    history = guest.get(f"/support/conversations/{conversation_id}/messages")
    assert history.status_code == 200
    assert history.json["messages"][0]["body"] == "Where is my order?"
    assert history.json["messages"][1]["sender_name"] == "FityStores"
    assert "Thanks, there." in history.json["messages"][1]["body"]
    assert other_guest.get(f"/support/conversations/{conversation_id}/messages").status_code == 404


def test_customer_chat_is_linked_to_account_and_not_exposed_to_guests(support_app):
    customer = support_app.test_client()
    guest = support_app.test_client()
    with support_app.app_context():
        account = CustomerAccount(email="customer@example.test", display_name="Test Customer")
        db.session.add(account)
        db.session.commit()
        user_id = account.get_id()
    _sign_in(customer, user_id)

    response = customer.post("/support/conversations", json={"topic": "products"})
    assert response.status_code == 201
    conversation_id = response.json["conversation"]["id"]

    assert customer.get(f"/support/conversations/{conversation_id}/messages").status_code == 200
    assert guest.get(f"/support/conversations/{conversation_id}/messages").status_code == 404


def test_retried_customer_message_is_idempotent(support_app):
    from app.models import SupportConversation, SupportMessage

    client = support_app.test_client()
    conversation = client.post("/support/conversations", json={"topic": "general"})
    conversation_id = conversation.json["conversation"]["id"]
    client_message_id = str(uuid4())
    payload = {"body": "Hello, I need help.", "client_message_id": client_message_id}

    first = client.post(
        f"/support/conversations/{conversation_id}/messages",
        json=payload,
    )
    retry = client.post(
        f"/support/conversations/{conversation_id}/messages",
        json=payload,
    )

    assert first.status_code == 201
    assert retry.status_code == 200
    with support_app.app_context():
        conversation_row = SupportConversation.query.filter_by(public_id=conversation_id).one()
        assert SupportMessage.query.filter_by(
            conversation_id=conversation_row.id,
            sender_type="customer",
        ).count() == 1
        assert SupportMessage.query.filter_by(
            conversation_id=conversation_row.id,
            sender_name="FityStores",
        ).count() == 1


def test_first_customer_message_gets_handoff_and_direct_admin_email(support_app, monkeypatch):
    from app.models import EmailMessage
    from app.services import customer_mail

    delivered = {}

    def capture_delivery(recipient, subject, html_body, text_body):
        delivered.update(
            recipient=recipient,
            subject=subject,
            html_body=html_body,
            text_body=text_body,
        )
        return "test-support-notice"

    monkeypatch.setattr(customer_mail, "_send_brevo", capture_delivery)

    client = support_app.test_client()
    created = client.post("/support/conversations", json={"topic": "orders"})
    conversation_id = created.json["conversation"]["id"]
    with support_app.app_context():
        assert EmailMessage.query.count() == 0

    response = client.post(
        f"/support/conversations/{conversation_id}/messages",
        json={
            "body": "My order is <delayed>.",
            "client_message_id": str(uuid4()),
        },
    )

    assert response.status_code == 201
    assert response.json["handoff_created"] is True
    assert response.json["handoff_message"]["sender_name"] == "FityStores"
    assert "customer care team" in response.json["handoff_message"]["body"]
    with support_app.app_context():
        notice = EmailMessage.query.filter_by(
            idempotency_key=f"support_request:{conversation_id}"
        ).one()
        assert notice.recipient == "orders@fitystore.top"
        assert notice.kind == "support_request"
        assert notice.status == "sent"
    assert delivered["recipient"] == "orders@fitystore.top"
    assert (
        "https://render.example.test/admin/support/?conversation=" + conversation_id
        in delivered["text_body"]
    )
    assert "My order is &lt;delayed&gt;." in delivered["html_body"]


def test_support_inbox_requires_admin_and_shows_customer_contact_only_to_admin(support_app):
    guest = support_app.test_client()
    created = guest.post("/support/conversations", json={"topic": "general"})
    assert created.status_code == 201

    unauthenticated = support_app.test_client()
    assert unauthenticated.get("/admin/support/").status_code in {302, 401}

    customer = support_app.test_client()
    with support_app.app_context():
        account = CustomerAccount(email="private@example.test", display_name="Private Customer")
        db.session.add(account)
        db.session.commit()
        customer_id = account.get_id()
    _sign_in(customer, customer_id)
    private_chat = customer.post("/support/conversations", json={"topic": "orders"})
    assert private_chat.status_code == 201

    admin = support_app.test_client()
    _sign_in(admin, "1")
    with support_app.app_context():
        admin_user = AdminUser.query.filter_by(username="support-owner").first()
        assert admin_user.id == 1

    response = admin.get("/admin/support/api/conversations")
    assert response.status_code == 200
    conversations = {item["id"]: item for item in response.json["conversations"]}
    guest_chat = conversations[created.json["conversation"]["id"]]
    account_chat = conversations[private_chat.json["conversation"]["id"]]
    assert guest_chat["customer_email"] is None
    assert guest_chat["customer_name"] == "Guest customer"
    assert account_chat["customer_email"] == "private@example.test"
    assert account_chat["customer_name"] == "Private Customer"
    dashboard = admin.get("/admin/support/")
    assert dashboard.status_code == 200
    assert b"data-support-admin" in dashboard.data
    assert b"support-admin.js" in dashboard.data
    assert b"data-support-widget" not in dashboard.data
    assert customer.get("/admin/support/api/conversations").status_code == 403


def test_support_launcher_is_present_on_storefront_only(support_client):
    storefront = support_client.get("/")
    assert storefront.status_code == 200
    assert b"data-support-widget" in storefront.data
    assert b"data-support-launcher" in storefront.data
    assert b'<span class="fity-support-label">Chat</span>' in storefront.data
    assert b"support-chat.js" in storefront.data
    assert b"support-admin.js" not in storefront.data

    admin_page = support_client.get("/admin/support/")
    assert admin_page.status_code in {302, 401}


def test_agent_can_claim_waiting_conversation(support_app):
    client = support_app.test_client()
    created = client.post("/support/conversations", json={"topic": "payments"})
    conversation_id = created.json["conversation"]["id"]
    admin = support_app.test_client()
    _sign_in(admin, "1")

    response = admin.post(f"/admin/support/api/conversations/{conversation_id}/claim")

    assert response.status_code == 200
    assert response.json["conversation"]["status"] == "assigned"
    assert response.json["conversation"]["assigned"] == "support-owner"


def test_email_notification_failure_does_not_discard_support_chat(support_app, monkeypatch):
    from app.models import SupportConversation
    import app.support.routes as support_routes

    support_app.config["SUPPORT_EMAIL"] = "support@example.test"

    def fail_queue(*_args, **_kwargs):
        raise ValueError("email queue unavailable")

    monkeypatch.setattr(support_routes, "queue_email", fail_queue)
    client = support_app.test_client()
    created = client.post("/support/conversations", json={"topic": "delivery"})
    conversation_id = created.json["conversation"]["id"]
    response = client.post(
        f"/support/conversations/{conversation_id}/messages",
        json={"body": "My delivery is late.", "client_message_id": str(uuid4())},
    )

    assert created.status_code == 201
    assert response.status_code == 201
    with support_app.app_context():
        conversation = SupportConversation.query.filter_by(
            public_id=conversation_id
        ).one()
        assert conversation.notification_status == "failed"
        assert len(conversation.messages) == 2


def test_support_schema_migration_requires_both_confirmations(support_app):
    from sqlalchemy import inspect
    from app.models import SupportAgentPresence, SupportConversation, SupportMessage

    with support_app.app_context():
        SupportMessage.__table__.drop(bind=db.engine)
        SupportAgentPresence.__table__.drop(bind=db.engine)
        SupportConversation.__table__.drop(bind=db.engine)

    runner = support_app.test_cli_runner()
    rejected = runner.invoke(args=["migrate-support-chat-schema"])
    assert rejected.exit_code != 0
    with support_app.app_context():
        assert not inspect(db.engine).has_table("support_conversation")

    applied = runner.invoke(
        args=[
            "migrate-support-chat-schema",
            "--confirmation",
            "APPLY SUPPORT CHAT MIGRATION",
            "--backup-confirmation",
            "I HAVE A VERIFIED RESTORABLE BACKUP",
        ]
    )
    assert applied.exit_code == 0, applied.output
    with support_app.app_context():
        inspector = inspect(db.engine)
        assert inspector.has_table("support_conversation")
        assert inspector.has_table("support_message")
        assert inspector.has_table("support_agent_presence")
