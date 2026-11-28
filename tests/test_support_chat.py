from __future__ import annotations

from io import BytesIO
from datetime import datetime
from uuid import uuid4

import pytest
from werkzeug.security import generate_password_hash

from app import create_app
from app.extensions import db
from app.models import AdminUser, CustomerAccount, ShopSettings
from config import TestConfig


@pytest.fixture()
def support_app(monkeypatch):
    app = create_app(TestConfig)
    app.config["SERVER_NAME"] = "localhost"
    app.config.update(
        SUPPORT_EMAIL="oxygenvessels@gmail.com",
        PUBLIC_BASE_URL="https://render.example.test",
        MAIL_FROM_ADDRESS="orders@fitystore.top",
        MAIL_FROM_VERIFIED=True,
    )
    import app.support.routes as support_routes
    monkeypatch.setattr(support_routes.socketio, "start_background_task", lambda *_args: None)
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


def _sign_in_customer(client, app, email="customer@example.test", display_name="Account Holder"):
    with app.app_context():
        account = CustomerAccount(
            email=email,
            display_name=display_name,
            email_verified_at=datetime.utcnow(),
        )
        db.session.add(account)
        db.session.commit()
        customer_id = account.get_id()
    _sign_in(client, customer_id)
    return customer_id


def _start_chat(client, *, topic="orders", first_name="Amina", last_name="Wanjiku",
                body="Where is my order?", client_message_id=None):
    return client.post(
        "/support/conversations",
        json={
            "topic": topic,
            "first_name": first_name,
            "last_name": last_name,
            "body": body,
            "client_message_id": client_message_id or str(uuid4()),
        },
    )


def test_chat_is_unavailable_to_guests_and_requires_verified_account(support_app):
    guest = support_app.test_client()

    assert guest.get("/support/conversations").status_code == 401
    assert guest.post("/support/conversations", json={"topic": "orders"}).status_code == 401


def test_customer_chat_collects_names_and_first_message_only_for_chat(support_app):
    from app.models import SupportConversation, SupportMessage

    customer = support_app.test_client()
    customer_id = _sign_in_customer(customer, support_app)
    other_customer = support_app.test_client()
    _sign_in_customer(other_customer, support_app, email="other@example.test")

    started = _start_chat(customer)
    assert started.status_code == 201
    conversation_id = started.json["conversation"]["id"]
    assert started.json["message"]["body"] == "Where is my order?"
    assert started.json["message"]["sender_name"] == "Amina Wanjiku"
    assert started.json["bot_message"]["sender_name"] == "FityStores Assistant"
    assert "Hi Amina" in started.json["bot_message"]["body"]
    assert "Orders in your FityStores account" in started.json["bot_message"]["body"]
    assert any(reply["action"] == "escalate" for reply in started.json["quick_replies"])

    history = customer.get(f"/support/conversations/{conversation_id}/messages")
    assert history.status_code == 200
    assert [message["body"] for message in history.json["messages"]] == [
        "Where is my order?",
        started.json["bot_message"]["body"],
    ]
    assert other_customer.get(
        f"/support/conversations/{conversation_id}/messages"
    ).status_code == 404

    with support_app.app_context():
        conversation = SupportConversation.query.filter_by(public_id=conversation_id).one()
        account = db.session.get(CustomerAccount, int(customer_id.split(":", 1)[1]))
        assert conversation.account_id == account.id
        assert conversation.guest_token_hash is None
        assert (conversation.customer_first_name, conversation.customer_last_name) == (
            "Amina", "Wanjiku"
        )
        assert account.display_name == "Account Holder"
        assert SupportMessage.query.filter_by(
            conversation_id=conversation.id, sender_type="customer"
        ).count() == 1
        assert SupportMessage.query.filter_by(
            conversation_id=conversation.id,
            sender_name="FityStores Assistant",
        ).count() == 1


def test_customer_chat_is_linked_to_account_and_not_exposed_to_guests(support_app):
    customer = support_app.test_client()
    guest = support_app.test_client()
    _sign_in_customer(customer, support_app)

    response = _start_chat(customer, topic="products", body="Is this available?")
    assert response.status_code == 201
    conversation_id = response.json["conversation"]["id"]

    assert customer.get(f"/support/conversations/{conversation_id}/messages").status_code == 200
    assert guest.get(f"/support/conversations/{conversation_id}/messages").status_code == 404


def test_retried_customer_message_is_idempotent(support_app):
    from app.models import SupportConversation, SupportMessage

    client = support_app.test_client()
    _sign_in_customer(client, support_app)
    client_message_id = str(uuid4())
    payload = {
        "topic": "general",
        "first_name": "Amina",
        "last_name": "Wanjiku",
        "body": "Hello, I need help.",
        "client_message_id": client_message_id,
    }
    conversation = client.post("/support/conversations", json=payload)
    conversation_id = conversation.json["conversation"]["id"]
    first = client.post("/support/conversations", json=payload)
    retry = client.post("/support/conversations", json=payload)

    assert conversation.status_code == 201
    assert first.status_code == 200
    assert retry.status_code == 200
    with support_app.app_context():
        conversation_row = SupportConversation.query.filter_by(public_id=conversation_id).one()
        assert SupportMessage.query.filter_by(
            conversation_id=conversation_row.id,
            sender_type="customer",
        ).count() == 1
        assert SupportMessage.query.filter_by(
            conversation_id=conversation_row.id,
            sender_name="FityStores Assistant",
        ).count() == 1


def test_first_customer_message_queues_email_without_blocking_bot_reply(support_app, monkeypatch):
    from app.models import EmailMessage
    from app.services import customer_mail
    from flask import current_app
    import app.support.routes as support_routes

    delivered = {}
    scheduled = []

    def capture_delivery(recipient, subject, html_body, text_body):
        delivered.update(
            recipient=recipient,
            subject=subject,
            html_body=html_body,
            text_body=text_body,
            from_address=current_app.config["MAIL_FROM_ADDRESS"],
        )
        return "test-support-notice"

    monkeypatch.setattr(customer_mail, "_send_brevo", capture_delivery)
    monkeypatch.setattr(
        support_routes.socketio,
        "start_background_task",
        lambda target, *args: scheduled.append((target, args)),
    )

    client = support_app.test_client()
    _sign_in_customer(client, support_app)
    created = _start_chat(client, topic="orders", body="My order is <delayed>.")
    conversation_id = created.json["conversation"]["id"]
    assert created.status_code == 201
    assert created.json["bot_message"]["sender_name"] == "FityStores Assistant"
    assert "order you have already placed" in created.json["bot_message"]["body"]
    with support_app.app_context():
        notice = EmailMessage.query.filter_by(
            idempotency_key=f"support_request:{conversation_id}"
        ).one()
        assert notice.recipient == "oxygenvessels@gmail.com"
        assert notice.kind == "support_request"
        assert notice.status == "queued"
    assert not delivered
    assert len(scheduled) == 1

    dispatch, args = scheduled[0]
    dispatch(*args)

    with support_app.app_context():
        notice = EmailMessage.query.filter_by(
            idempotency_key=f"support_request:{conversation_id}"
        ).one()
        assert notice.status == "sent"
    assert delivered["recipient"] == "oxygenvessels@gmail.com"
    assert delivered["from_address"] == "orders@fitystore.top"
    assert (
        "https://render.example.test/admin/support/?conversation=" + conversation_id
        in delivered["text_body"]
    )
    assert "My order is &lt;delayed&gt;." in delivered["html_body"]
    assert "Amina Wanjiku" in delivered["text_body"]


def test_customer_gets_topic_specific_bot_answers_and_can_escalate(support_app):
    from app.models import EmailMessage, SupportConversation, SupportMessage

    client = support_app.test_client()
    _sign_in_customer(client, support_app)
    started = _start_chat(client, topic="orders", body="I already placed an order.")
    conversation_id = started.json["conversation"]["id"]

    choice = client.post(
        f"/support/conversations/{conversation_id}/messages",
        json={
            "body": "Yes, I have an existing order",
            "client_message_id": str(uuid4()),
        },
    )
    assert choice.status_code == 201
    assert "What do you need help with?" in choice.json["bot_message"]["body"]
    assert any(reply["label"] == "Order tracking" for reply in choice.json["quick_replies"])

    answer = client.post(
        f"/support/conversations/{conversation_id}/messages",
        json={"body": "I need to track my order", "client_message_id": str(uuid4())},
    )
    assert answer.status_code == 201
    assert "latest status" in answer.json["bot_message"]["body"]
    assert {reply["action"] for reply in answer.json["quick_replies"]} == {
        "resolve",
        "escalate",
    }

    admin = support_app.test_client()
    _sign_in(admin, "1")
    waiting = admin.get("/admin/support/api/conversations").json["conversations"]
    assert len(waiting) == 1
    assert waiting[0]["escalation_requested"] is False
    not_escalated = admin.post(f"/admin/support/api/conversations/{conversation_id}/claim")
    assert not_escalated.status_code == 409
    cannot_close = admin.post(f"/admin/support/api/conversations/{conversation_id}/close")
    assert cannot_close.status_code == 409

    escalated = client.post(f"/support/conversations/{conversation_id}/escalate")
    assert escalated.status_code == 201
    assert escalated.json["conversation"]["escalation_requested"] is True
    assert "Thanks for your patience" in escalated.json["bot_message"]["body"]
    with support_app.app_context():
        conversation = SupportConversation.query.filter_by(public_id=conversation_id).one()
        assert conversation.escalation_requested_at is not None
        assert SupportMessage.query.filter_by(
            conversation_id=conversation.id,
            sender_name="FityStores Assistant",
        ).count() >= 3
        assert EmailMessage.query.filter_by(kind="support_request").count() == 1
    assert len(admin.get("/admin/support/api/conversations").json["conversations"]) == 1


def test_customer_can_finish_self_service_without_agent(support_app):
    client = support_app.test_client()
    _sign_in_customer(client, support_app)
    started = _start_chat(client, topic="payments", body="I already paid.")
    conversation_id = started.json["conversation"]["id"]
    assert any(reply["action"] == "resolve" for reply in started.json["quick_replies"])

    resolved = client.post(f"/support/conversations/{conversation_id}/resolve")

    assert resolved.status_code == 201
    assert resolved.json["conversation"]["status"] == "closed"
    assert resolved.json["conversation"]["escalation_requested"] is False
    assert "Glad I could help" in resolved.json["bot_message"]["body"]
    admin = support_app.test_client()
    _sign_in(admin, "1")
    assert admin.get("/admin/support/api/conversations").json["conversations"] == []


def test_customer_attachments_are_private_and_available_to_claimed_agent(support_app):
    from app.models import SupportAttachment

    customer = support_app.test_client()
    _sign_in_customer(customer, support_app)
    started = _start_chat(customer, topic="payments", body="I need help with payment proof.")
    conversation_id = started.json["conversation"]["id"]
    attachment_response = customer.post(
        f"/support/conversations/{conversation_id}/messages",
        data={
            "body": "Please review this receipt.",
            "client_message_id": str(uuid4()),
            "attachment": (BytesIO(b"%PDF-1.7\nreceipt"), "receipt.pdf"),
        },
        content_type="multipart/form-data",
    )

    assert attachment_response.status_code == 201
    attachment = attachment_response.json["message"]["attachment"]
    assert attachment["filename"] == "receipt.pdf"
    assert attachment["content_type"] == "application/pdf"
    assert attachment_response.json["bot_message"]["body"].startswith("I’ve received your file.")
    with support_app.app_context():
        stored = db.session.get(SupportAttachment, attachment["id"])
        assert stored.data == b"%PDF-1.7\nreceipt"

    stranger = support_app.test_client()
    assert stranger.get(attachment["url"]).status_code == 404
    downloaded = customer.get(attachment["url"])
    assert downloaded.status_code == 200
    assert downloaded.data == b"%PDF-1.7\nreceipt"
    assert downloaded.headers["Content-Disposition"].startswith("attachment;")

    admin = support_app.test_client()
    _sign_in(admin, "1")
    assert admin.get(attachment["url"]).status_code == 200
    assert admin.post(f"/admin/support/api/conversations/{conversation_id}/claim").status_code == 409
    assert customer.post(f"/support/conversations/{conversation_id}/escalate").status_code == 201
    assert admin.post(f"/admin/support/api/conversations/{conversation_id}/claim").status_code == 200
    agent_attachment = admin.post(
        f"/admin/support/api/conversations/{conversation_id}/messages",
        data={
            "body": "Thanks, I can see the receipt.",
            "client_message_id": str(uuid4()),
            "attachment": (BytesIO(b"\x89PNG\r\n\x1a\nimage"), "annotated.png"),
        },
        content_type="multipart/form-data",
    )
    assert agent_attachment.status_code == 201
    assert agent_attachment.json["message"]["attachment"]["filename"] == "annotated.png"
    assert customer.get(agent_attachment.json["message"]["attachment"]["url"]).status_code == 200


def test_support_attachment_rejects_mismatched_and_unsupported_files(support_app):
    customer = support_app.test_client()
    _sign_in_customer(customer, support_app)
    started = _start_chat(customer)
    conversation_id = started.json["conversation"]["id"]

    mismatched = customer.post(
        f"/support/conversations/{conversation_id}/messages",
        data={
            "body": "",
            "client_message_id": str(uuid4()),
            "attachment": (BytesIO(b"not a pdf"), "receipt.pdf"),
        },
        content_type="multipart/form-data",
    )
    unsupported = customer.post(
        f"/support/conversations/{conversation_id}/messages",
        data={
            "body": "",
            "client_message_id": str(uuid4()),
            "attachment": (BytesIO(b"<script>alert(1)</script>"), "receipt.svg"),
        },
        content_type="multipart/form-data",
    )

    assert mismatched.status_code == 400
    assert "contents do not match" in mismatched.json["error"]
    assert unsupported.status_code == 400
    assert "PDF, PNG" in unsupported.json["error"]

    oversized = customer.post(
        f"/support/conversations/{conversation_id}/messages",
        data={
            "body": "",
            "client_message_id": str(uuid4()),
            "attachment": (
                BytesIO(b"x" * (3 * 1024 * 1024 + 1)),
                "oversized.txt",
            ),
        },
        content_type="multipart/form-data",
    )
    assert oversized.status_code == 400
    assert "3 MB or smaller" in oversized.json["error"]


def test_customer_can_download_only_their_chat_transcript(support_app):
    customer = support_app.test_client()
    _sign_in_customer(customer, support_app)
    started = _start_chat(customer, body="Can you help me?")
    conversation_id = started.json["conversation"]["id"]

    transcript = customer.get(f"/support/conversations/{conversation_id}/transcript")
    stranger = support_app.test_client().get(
        f"/support/conversations/{conversation_id}/transcript"
    )

    assert transcript.status_code == 200
    assert "attachment" in transcript.headers["Content-Disposition"]
    assert b"Can you help me?" in transcript.data
    assert b"FityStores Assistant" in transcript.data
    assert stranger.status_code == 404


def test_support_inbox_requires_admin_and_shows_customer_contact_only_to_admin(support_app):
    unauthenticated = support_app.test_client()
    assert unauthenticated.get("/admin/support/").status_code in {302, 401}

    customer = support_app.test_client()
    _sign_in_customer(
        customer,
        support_app,
        email="private@example.test",
        display_name="Private Customer",
    )
    private_chat = _start_chat(
        customer,
        topic="orders",
        first_name="Chat",
        last_name="Name",
        body="I need help with an order.",
    )
    assert private_chat.status_code == 201
    conversation_id = private_chat.json["conversation"]["id"]

    admin = support_app.test_client()
    _sign_in(admin, "1")
    with support_app.app_context():
        admin_user = AdminUser.query.filter_by(username="support-owner").first()
        assert admin_user.id == 1

    response = admin.get("/admin/support/api/conversations")
    assert response.status_code == 200
    assert len(response.json["conversations"]) == 1

    escalation = customer.post(f"/support/conversations/{conversation_id}/escalate")
    assert escalation.status_code == 201
    response = admin.get("/admin/support/api/conversations")
    conversations = {item["id"]: item for item in response.json["conversations"]}
    account_chat = conversations[conversation_id]
    assert account_chat["customer_email"] == "private@example.test"
    assert account_chat["customer_name"] == "Chat Name"
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
    assert b'data-support-signin' in storefront.data
    assert b"data-support-settings-toggle" in storefront.data
    assert b"data-support-download" in storefront.data
    assert b"data-support-file" in storefront.data
    assert b"data-support-spinner" in storefront.data
    assert b'href="/account/login?next=/' in storefront.data
    assert b"Continue with email" in storefront.data
    assert b"support-chat.js" in storefront.data
    assert b"support-admin.js" not in storefront.data

    login = support_client.get("/account/login?next=%2F%3Fsupport_chat%3D1")
    assert login.status_code == 200
    assert b'name="next" value="/?support_chat=1"' in login.data

    admin_page = support_client.get("/admin/support/")
    assert admin_page.status_code in {302, 401}


def test_agent_can_claim_waiting_conversation(support_app):
    client = support_app.test_client()
    _sign_in_customer(client, support_app)
    created = _start_chat(
        client,
        topic="payments",
        first_name="Amina",
        last_name="Wanjiku",
        body="I need to confirm a payment.",
    )
    assert created.status_code == 201
    conversation_id = created.json["conversation"]["id"]
    assert client.post(
        f"/support/conversations/{conversation_id}/escalate"
    ).status_code == 201
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
    _sign_in_customer(client, support_app)
    created = _start_chat(
        client,
        topic="delivery",
        body="My delivery is late.",
    )
    conversation_id = created.json["conversation"]["id"]

    assert created.status_code == 201
    with support_app.app_context():
        conversation = SupportConversation.query.filter_by(
            public_id=conversation_id
        ).one()
        assert conversation.notification_status == "failed"
        assert len(conversation.messages) == 2


def test_support_schema_migration_requires_both_confirmations(support_app):
    from sqlalchemy import inspect, text
    from app.models import SupportAgentPresence, SupportAttachment, SupportConversation, SupportMessage

    with support_app.app_context():
        SupportAttachment.__table__.drop(bind=db.engine)
        SupportMessage.__table__.drop(bind=db.engine)
        SupportAgentPresence.__table__.drop(bind=db.engine)
        SupportConversation.__table__.drop(bind=db.engine)
        db.session.execute(text(
            "CREATE TABLE support_conversation ("
            "id INTEGER PRIMARY KEY, public_id VARCHAR(32) NOT NULL UNIQUE, "
            "account_id INTEGER, guest_token_hash VARCHAR(64), topic VARCHAR(24) NOT NULL, "
            "status VARCHAR(20) NOT NULL, assigned_admin_id INTEGER, "
            "notification_status VARCHAR(20) NOT NULL, created_at DATETIME NOT NULL, "
            "updated_at DATETIME NOT NULL, last_message_at DATETIME NOT NULL)"
        ))
        db.session.commit()

    runner = support_app.test_cli_runner()
    rejected = runner.invoke(args=["migrate-support-chat-schema"])
    assert rejected.exit_code != 0
    with support_app.app_context():
        assert inspect(db.engine).has_table("support_conversation")
        assert "customer_first_name" not in {
            column["name"] for column in inspect(db.engine).get_columns("support_conversation")
        }

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
        assert inspector.has_table("support_attachment")
        assert {
            "customer_first_name",
            "customer_last_name",
            "escalation_requested_at",
        } <= {
            column["name"] for column in inspector.get_columns("support_conversation")
        }
