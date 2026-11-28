from __future__ import annotations

import logging
import re
from html import escape as escape_html
from datetime import datetime
from secrets import token_urlsafe
from uuid import NAMESPACE_URL, UUID, uuid5

from email_validator import EmailNotValidError, validate_email
from flask import (
    Blueprint,
    abort,
    current_app,
    jsonify,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.extensions import db, socketio
from app.models import (
    AdminUser,
    EmailMessage,
    SupportAgentPresence,
    SupportConversation,
    SupportMessage,
)
from app.services.customer_mail import deliver_email, queue_email
from app.services.support_chat import (
    PRESENCE_FRESHNESS,
    SUPPORT_TOPICS,
    allow_support_action,
    conversation_payload,
    conversation_room,
    current_admin,
    current_customer,
    message_payload,
    owns_conversation,
    presence_is_fresh,
    support_bot_opening,
    support_bot_reply,
)

logger = logging.getLogger(__name__)
support_bp = Blueprint("support", __name__, url_prefix="/support")
support_admin_bp = Blueprint("support_admin", __name__, url_prefix="/admin/support")
_MESSAGE_ID_PATTERN = re.compile(r"^[0-9a-fA-F-]{36}$")


@support_bp.after_request
def prevent_support_api_caching(response):
    response.headers["Cache-Control"] = "no-store"
    return response


def _support_identity() -> str:
    customer = current_customer()
    return f"customer:{customer.id}" if customer else "customer:missing"


def _new_or_existing_conversation(
    topic: str,
    customer,
    first_name: str,
    last_name: str,
):
    identity_filter = SupportConversation.account_id == customer.id
    existing = SupportConversation.query.filter(
        identity_filter,
        SupportConversation.status != "closed",
    ).order_by(SupportConversation.created_at.desc()).first()
    if existing:
        existing.topic = topic
        existing.customer_first_name = first_name
        existing.customer_last_name = last_name
        existing.updated_at = datetime.utcnow()
        db.session.commit()
        return existing, False

    conversation = SupportConversation(
        public_id=token_urlsafe(18),
        account_id=customer.id,
        customer_first_name=first_name,
        customer_last_name=last_name,
        topic=topic,
        status="waiting",
        last_message_at=datetime.utcnow(),
    )
    db.session.add(conversation)
    try:
        db.session.commit()
        return conversation, True
    except IntegrityError:
        db.session.rollback()
        raced = SupportConversation.query.filter(
            identity_filter,
            SupportConversation.status != "closed",
        ).order_by(SupportConversation.created_at.desc()).first()
        if raced:
            raced.topic = topic
            raced.customer_first_name = first_name
            raced.customer_last_name = last_name
            db.session.commit()
            return raced, False
        raise


def _valid_message_id(value: str) -> bool:
    if not _MESSAGE_ID_PATTERN.fullmatch(value):
        return False
    try:
        UUID(value)
    except ValueError:
        return False
    return True


def _clean_chat_name(value) -> str:
    return " ".join(str(value or "").split())[:80]


def _queue_support_notice(conversation: SupportConversation, first_message: SupportMessage) -> None:
    recipient = (current_app.config.get("SUPPORT_EMAIL") or "").strip()
    if not recipient:
        conversation.notification_status = "not_configured"
        db.session.commit()
        return
    if conversation.notification_status == "queued":
        return
    try:
        recipient = validate_email(recipient, check_deliverability=False).normalized
    except EmailNotValidError:
        conversation.notification_status = "failed"
        db.session.commit()
        logger.error("support_notice_recipient_invalid conversation_id=%s", conversation.id)
        return

    support_url = (
        (current_app.config.get("PUBLIC_BASE_URL") or "https://fitystore.top").rstrip("/")
        + url_for("support_admin.dashboard", conversation=conversation.public_id)
    )
    message_preview = first_message.body[:500]
    customer_name = (
        f"{conversation.customer_first_name} {conversation.customer_last_name}".strip()
    )
    customer_email = conversation.account.email if conversation.account else ""
    safe_preview = escape_html(message_preview)
    safe_url = escape_html(support_url, quote=True)
    safe_name = escape_html(customer_name)
    safe_email = escape_html(customer_email)
    try:
        notice = queue_email(
            recipient,
            f"New FityStores support message: {SUPPORT_TOPICS[conversation.topic]}",
            "support_request",
            f"support_request:{conversation.public_id}",
            html_body=(
                "<p>A customer has sent a message to FityStores support.</p>"
                f"<p>Customer: {safe_name} ({safe_email})</p>"
                f"<p>Topic: {SUPPORT_TOPICS[conversation.topic]}</p>"
                f"<p>Message: {safe_preview}</p>"
                f'<p><a href="{safe_url}">Open this conversation in the FityStores support inbox</a></p>'
            ),
            text_body=(
                "A customer has sent a message to FityStores support.\n"
                f"Customer: {customer_name} ({customer_email})\n"
                f"Topic: {SUPPORT_TOPICS[conversation.topic]}\n"
                f"Message: {message_preview}\n"
                f"Open this conversation in the FityStores support inbox: {support_url}"
            ),
            commit=False,
        )
        conversation.notification_status = "queued"
        db.session.commit()
        if notice.status == "queued":
            app = current_app._get_current_object()
            try:
                socketio.start_background_task(
                    _dispatch_support_notice,
                    app,
                    notice.id,
                )
            except Exception:
                logger.exception(
                    "support_notice_background_dispatch_start_failed conversation_id=%s",
                    conversation.id,
                )
    except (SQLAlchemyError, ValueError):
        db.session.rollback()
        logger.exception("support_notice_enqueue_failed conversation_id=%s", conversation.id)
        db.session.execute(
            update(SupportConversation)
            .where(SupportConversation.id == conversation.id)
            .values(notification_status="failed", updated_at=datetime.utcnow())
        )
        db.session.commit()


def _dispatch_support_notice(app, message_id: int) -> None:
    with app.app_context():
        try:
            notice = db.session.get(EmailMessage, message_id)
            if notice and notice.status == "queued":
                deliver_email(notice)
        except Exception:
            db.session.rollback()
            logger.exception("support_notice_background_dispatch_failed message_id=%s", message_id)
        finally:
            db.session.remove()


def _find_owned_conversation(public_id: str) -> SupportConversation:
    conversation = SupportConversation.query.filter_by(public_id=public_id).first()
    if not conversation or not owns_conversation(conversation):
        abort(404)
    return conversation


def _emit_message(conversation: SupportConversation, message: SupportMessage) -> None:
    payload = message_payload(message)
    socketio.emit(
        "message",
        payload,
        to=conversation_room(conversation),
        namespace="/support",
    )
    socketio.emit(
        "queue_update",
        {"conversation_id": conversation.public_id, "status": conversation.status},
        to="support-agents",
        namespace="/support",
    )


def _store_message(
    conversation: SupportConversation,
    body: str,
    client_message_id: str,
    *,
    sender_type: str,
    sender_name: str,
    sender_account_id: int | None = None,
    sender_admin_id: int | None = None,
):
    existing = SupportMessage.query.filter_by(
        conversation_id=conversation.id,
        client_message_id=client_message_id,
    ).first()
    if existing:
        if existing.sender_type != sender_type or existing.body != body:
            return None, False
        return existing, False

    message = SupportMessage(
        conversation_id=conversation.id,
        sender_type=sender_type,
        sender_name=sender_name[:100],
        sender_account_id=sender_account_id,
        sender_admin_id=sender_admin_id,
        client_message_id=client_message_id,
        body=body,
    )
    conversation.last_message_at = datetime.utcnow()
    db.session.add(message)
    try:
        db.session.commit()
        return message, True
    except IntegrityError:
        db.session.rollback()
        existing = SupportMessage.query.filter_by(
            conversation_id=conversation.id,
            client_message_id=client_message_id,
        ).first()
        if existing and existing.sender_type == sender_type and existing.body == body:
            return existing, False
        raise


def _store_bot_message(
    conversation: SupportConversation,
    body: str,
    triggering_message_id: str,
) -> tuple[SupportMessage, bool]:
    namespace = uuid5(NAMESPACE_URL, str(current_app.secret_key))
    bot_message_id = str(uuid5(
        namespace,
        f"support-bot:{conversation.public_id}:{triggering_message_id}",
    ))
    return _store_message(
        conversation,
        body,
        bot_message_id,
        sender_type="agent",
        sender_name="FityStores Assistant",
    )


@support_bp.get("/presence")
def customer_presence():
    now = datetime.utcnow()
    agents = SupportAgentPresence.query.filter(
        SupportAgentPresence.status == "available",
        SupportAgentPresence.heartbeat_at >= now - PRESENCE_FRESHNESS,
    ).count()
    return jsonify({"available": agents > 0, "available_agents": agents})


@support_bp.get("/conversations")
def customer_conversations():
    customer = current_customer()
    if not customer:
        return jsonify({"error": "Sign in with your FityStores account to use chat."}), 401
    query = SupportConversation.query.filter(SupportConversation.status != "closed")
    query = query.filter(
        SupportConversation.account_id == customer.id,
        SupportConversation.customer_first_name != "",
        SupportConversation.customer_last_name != "",
    )
    conversations = query.order_by(SupportConversation.last_message_at.desc()).limit(10).all()
    return jsonify({"conversations": [conversation_payload(item) for item in conversations]})


@support_bp.post("/conversations")
def create_conversation():
    if current_admin():
        abort(403)
    customer = current_customer()
    if not customer:
        return jsonify({"error": "Sign in with your FityStores account to use chat."}), 401
    payload = request.get_json(silent=True) or {}
    topic = str(payload.get("topic") or "general").strip().lower()
    if topic not in SUPPORT_TOPICS:
        return jsonify({"error": "Choose a valid support topic."}), 400
    first_name = _clean_chat_name(payload.get("first_name"))
    last_name = _clean_chat_name(payload.get("last_name"))
    body = str(payload.get("body") or "").strip()
    client_message_id = str(payload.get("client_message_id") or "").strip()
    if not first_name or not last_name:
        return jsonify({"error": "Enter your first and last name for this chat."}), 400
    if not body or len(body) > 2000:
        return jsonify({"error": "Your first message must contain 1–2000 characters."}), 400
    if not _valid_message_id(client_message_id):
        return jsonify({"error": "Invalid message identifier."}), 400
    if not allow_support_action(
        "support_conversation",
        _support_identity(),
        limit=5,
        window_minutes=15,
    ):
        return jsonify({"error": "Please wait before starting another support chat."}), 429

    try:
        conversation, created = _new_or_existing_conversation(
            topic, customer, first_name, last_name
        )
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("support_conversation_create_failed")
        return jsonify({"error": "Support chat is temporarily unavailable. Please try again."}), 503

    try:
        message, message_created = _store_message(
            conversation,
            body,
            client_message_id,
            sender_type="customer",
            sender_name=f"{first_name} {last_name}"[:100],
            sender_account_id=customer.id,
        )
        if not message:
            return jsonify({"error": "Message identifier was already used."}), 409
        opening = support_bot_opening(topic, first_name, body)
        bot_message, bot_created = _store_bot_message(
            conversation,
            opening["body"],
            client_message_id,
        )
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("support_initial_message_create_failed conversation_id=%s", conversation.id)
        return jsonify({"error": "Your message could not be saved. Please try again."}), 503

    if message_created:
        _emit_message(conversation, message)
    if bot_created:
        _emit_message(conversation, bot_message)
    _queue_support_notice(conversation, message)
    return jsonify(
        {
            "conversation": conversation_payload(conversation),
            "message": message_payload(message),
            "bot_message": message_payload(bot_message),
            "quick_replies": opening["quick_replies"],
            "created": created,
        }
    ), 201 if message_created else 200


@support_bp.get("/conversations/<public_id>/messages")
def customer_messages(public_id: str):
    conversation = _find_owned_conversation(public_id)
    try:
        after_id = max(0, int(request.args.get("after", "0")))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid message cursor."}), 400
    messages = SupportMessage.query.filter(
        SupportMessage.conversation_id == conversation.id,
        SupportMessage.id > after_id,
    ).order_by(SupportMessage.id).limit(100).all()
    return jsonify(
        {
            "conversation": conversation_payload(conversation),
            "messages": [message_payload(item) for item in messages],
        }
    )


@support_bp.post("/conversations/<public_id>/messages")
def customer_send_message(public_id: str):
    if not current_customer():
        return jsonify({"error": "Sign in with your FityStores account to use chat."}), 401
    conversation = _find_owned_conversation(public_id)
    if conversation.status == "closed":
        return jsonify({"error": "This conversation is closed. Start a new chat for more help."}), 409
    payload = request.get_json(silent=True) or {}
    body = str(payload.get("body") or "").strip()
    client_message_id = str(payload.get("client_message_id") or "").strip()
    if not body or len(body) > 2000:
        return jsonify({"error": "Messages must contain 1–2000 characters."}), 400
    if not _valid_message_id(client_message_id):
        return jsonify({"error": "Invalid message identifier."}), 400
    customer = current_customer()
    sender_name = (
        f"{conversation.customer_first_name} {conversation.customer_last_name}".strip()
    )
    if not sender_name:
        return jsonify({"error": "Add your chat name before sending a message."}), 409
    identity = f"customer:{customer.id}"
    existing = SupportMessage.query.filter_by(
        conversation_id=conversation.id,
        client_message_id=client_message_id,
    ).first()
    if existing:
        if existing.sender_type != "customer" or existing.body != body:
            return jsonify({"error": "Message identifier was already used."}), 409
        message = existing
        created = False
    else:
        if not allow_support_action("support_message", identity, limit=20, window_minutes=5):
            return jsonify({"error": "You are sending messages too quickly. Please wait a moment."}), 429
        try:
            message, created = _store_message(
                conversation,
                body,
                client_message_id,
                sender_type="customer",
                sender_name=sender_name,
                sender_account_id=customer.id if customer else None,
            )
        except SQLAlchemyError:
            db.session.rollback()
            logger.exception("support_message_create_failed conversation_id=%s", conversation.id)
            return jsonify({"error": "Your message could not be saved. Please try again."}), 503
        if not message:
            return jsonify({"error": "Message identifier was already used."}), 409

    if created:
        _emit_message(conversation, message)
    bot_message = None
    quick_replies = []
    if conversation.status == "waiting" and not conversation.escalation_requested_at:
        try:
            answer, quick_replies = support_bot_reply(conversation.topic, body)
            bot_message, bot_created = _store_bot_message(
                conversation,
                answer,
                client_message_id,
            )
        except SQLAlchemyError:
            db.session.rollback()
            logger.exception("support_bot_reply_create_failed conversation_id=%s", conversation.id)
            return jsonify(
                {
                    "error": "Your message was saved, but the FityStores assistant could not reply."
                }
            ), 503
        if bot_created:
            _emit_message(conversation, bot_message)
    return jsonify(
        {
            "message": message_payload(message),
            "bot_message": message_payload(bot_message) if bot_message else None,
            "quick_replies": quick_replies,
        }
    ), 201 if created else 200


@support_bp.post("/conversations/<public_id>/escalate")
def customer_request_agent(public_id: str):
    customer = current_customer()
    if not customer:
        return jsonify({"error": "Sign in with your FityStores account to use chat."}), 401
    conversation = _find_owned_conversation(public_id)
    if conversation.status == "closed":
        return jsonify({"error": "This conversation is closed."}), 409
    if conversation.status == "assigned":
        return jsonify(
            {
                "conversation": conversation_payload(conversation),
                "customer_message": None,
                "bot_message": None,
            }
        ), 200

    if not allow_support_action(
        "support_escalation",
        f"customer:{customer.id}",
        limit=5,
        window_minutes=15,
    ):
        return jsonify({"error": "Please wait before requesting customer care again."}), 429

    request_id = str(uuid5(
        uuid5(NAMESPACE_URL, str(current_app.secret_key)),
        f"support-escalation-request:{conversation.public_id}",
    ))
    try:
        conversation.escalation_requested_at = (
            conversation.escalation_requested_at or datetime.utcnow()
        )
        db.session.commit()
        customer_message, customer_created = _store_message(
            conversation,
            "Please connect me with customer care.",
            request_id,
            sender_type="customer",
            sender_name=(
                f"{conversation.customer_first_name} {conversation.customer_last_name}".strip()
            ),
            sender_account_id=customer.id,
        )
        bot_message, bot_created = _store_bot_message(
            conversation,
            (
                "I’ve notified customer care. Thanks for your patience while we find an "
                "available agent; we’ll update this chat as soon as someone joins."
            ),
            request_id,
        )
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("support_escalation_request_failed conversation_id=%s", conversation.id)
        return jsonify({"error": "Customer care could not be contacted. Please try again."}), 503
    if customer_created:
        _emit_message(conversation, customer_message)
    if bot_created:
        _emit_message(conversation, bot_message)
    socketio.emit(
        "queue_update",
        {"conversation_id": conversation.public_id, "status": conversation.status},
        to="support-agents",
        namespace="/support",
    )
    return jsonify(
        {
            "conversation": conversation_payload(conversation),
            "customer_message": message_payload(customer_message),
            "bot_message": message_payload(bot_message),
        }
    ), 200 if not customer_created else 201


@support_bp.post("/conversations/<public_id>/resolve")
def customer_resolve_conversation(public_id: str):
    customer = current_customer()
    if not customer:
        return jsonify({"error": "Sign in with your FityStores account to use chat."}), 401
    conversation = _find_owned_conversation(public_id)
    if conversation.status != "waiting" or conversation.escalation_requested_at:
        return jsonify({"error": "This conversation can no longer be resolved by the assistant."}), 409
    if not allow_support_action(
        "support_resolution",
        f"customer:{customer.id}",
        limit=10,
        window_minutes=15,
    ):
        return jsonify({"error": "Please wait before resolving another support chat."}), 429

    resolution_id = str(uuid5(
        uuid5(NAMESPACE_URL, str(current_app.secret_key)),
        f"support-resolved:{conversation.public_id}",
    ))
    try:
        customer_message, customer_created = _store_message(
            conversation,
            "That answered my question.",
            resolution_id,
            sender_type="customer",
            sender_name=(
                f"{conversation.customer_first_name} {conversation.customer_last_name}".strip()
            ),
            sender_account_id=customer.id,
        )
        bot_message, bot_created = _store_bot_message(
            conversation,
            "Glad I could help. You can start another chat any time you need us.",
            resolution_id,
        )
        conversation.status = "closed"
        conversation.updated_at = datetime.utcnow()
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("support_conversation_resolve_failed conversation_id=%s", conversation.id)
        return jsonify({"error": "Your support chat could not be closed. Please try again."}), 503
    if customer_created:
        _emit_message(conversation, customer_message)
    if bot_created:
        _emit_message(conversation, bot_message)
    socketio.emit(
        "closed",
        {"conversation": conversation_payload(conversation)},
        to=conversation_room(conversation),
        namespace="/support",
    )
    return jsonify(
        {
            "conversation": conversation_payload(conversation),
            "customer_message": message_payload(customer_message),
            "bot_message": message_payload(bot_message),
        }
    ), 200 if not customer_created else 201


def _require_support_admin():
    admin = current_admin()
    if not admin:
        abort(403)
    return admin


@support_admin_bp.get("/")
@login_required
def dashboard():
    _require_support_admin()
    return render_template("admin/support_dashboard.html", title="Support inbox")


@support_admin_bp.get("/api/conversations")
@login_required
def admin_conversations():
    _require_support_admin()
    conversations = SupportConversation.query.filter(
        SupportConversation.status != "closed",
    ).order_by(
        SupportConversation.status.asc(),
        SupportConversation.last_message_at.desc(),
    ).limit(100).all()
    result = []
    for conversation in conversations:
        item = conversation_payload(conversation)
        item["customer_name"] = (
            f"{conversation.customer_first_name} {conversation.customer_last_name}".strip()
            or (conversation.account.display_name if conversation.account else "Customer")
        )
        item["customer_email"] = conversation.account.email if conversation.account else None
        item["notification_status"] = conversation.notification_status
        email = EmailMessage.query.filter_by(
            idempotency_key=f"support_request:{conversation.public_id}"
        ).first()
        if email:
            item["notification_status"] = email.status
        last_message = SupportMessage.query.filter_by(
            conversation_id=conversation.id
        ).order_by(SupportMessage.id.desc()).first()
        item["last_message"] = message_payload(last_message) if last_message else None
        result.append(item)
    return jsonify({"conversations": result})


@support_admin_bp.get("/api/conversations/<public_id>/messages")
@login_required
def admin_messages(public_id: str):
    _require_support_admin()
    conversation = SupportConversation.query.filter_by(public_id=public_id).first_or_404()
    try:
        after_id = max(0, int(request.args.get("after", "0")))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid message cursor."}), 400
    messages = SupportMessage.query.filter(
        SupportMessage.conversation_id == conversation.id,
        SupportMessage.id > after_id,
    ).order_by(SupportMessage.id).limit(100).all()
    return jsonify(
        {
            "conversation": conversation_payload(conversation),
            "messages": [message_payload(item) for item in messages],
        }
    )


@support_admin_bp.post("/api/conversations/<public_id>/claim")
@login_required
def claim_conversation(public_id: str):
    admin = _require_support_admin()
    conversation = SupportConversation.query.filter_by(public_id=public_id).first_or_404()
    if not conversation.escalation_requested_at:
        return jsonify({"error": "The customer has not requested a support agent."}), 409
    if conversation.status == "assigned" and conversation.assigned_admin_id == admin.id:
        return jsonify({"conversation": conversation_payload(conversation)})
    if conversation.status != "waiting":
        return jsonify({"error": "This conversation is no longer waiting for an agent."}), 409
    now = datetime.utcnow()
    result = db.session.execute(
        update(SupportConversation)
        .where(
            SupportConversation.id == conversation.id,
            SupportConversation.status == "waiting",
            SupportConversation.assigned_admin_id.is_(None),
            SupportConversation.escalation_requested_at.is_not(None),
        )
        .values(
            status="assigned",
            assigned_admin_id=admin.id,
            updated_at=now,
        )
    )
    db.session.commit()
    if result.rowcount != 1:
        return jsonify({"error": "Another agent claimed this conversation first."}), 409
    conversation = db.session.get(SupportConversation, conversation.id)
    payload = conversation_payload(conversation)
    socketio.emit(
        "assigned",
        {"conversation": payload},
        to=conversation_room(conversation),
        namespace="/support",
    )
    socketio.emit(
        "queue_update",
        {"conversation_id": conversation.public_id, "status": conversation.status},
        to="support-agents",
        namespace="/support",
    )
    return jsonify({"conversation": payload})


@support_admin_bp.post("/api/conversations/<public_id>/messages")
@login_required
def admin_send_message(public_id: str):
    admin = _require_support_admin()
    conversation = SupportConversation.query.filter_by(public_id=public_id).first_or_404()
    if not conversation.escalation_requested_at:
        return jsonify({"error": "The customer has not requested a support agent."}), 409
    if conversation.status == "closed":
        return jsonify({"error": "This conversation is closed."}), 409
    if conversation.assigned_admin_id != admin.id:
        return jsonify({"error": "Claim this conversation before replying."}), 409
    payload = request.get_json(silent=True) or {}
    body = str(payload.get("body") or "").strip()
    client_message_id = str(payload.get("client_message_id") or "").strip()
    if not body or len(body) > 2000:
        return jsonify({"error": "Messages must contain 1–2000 characters."}), 400
    if not _MESSAGE_ID_PATTERN.fullmatch(client_message_id):
        return jsonify({"error": "Invalid message identifier."}), 400
    try:
        UUID(client_message_id)
    except ValueError:
        return jsonify({"error": "Invalid message identifier."}), 400
    existing = SupportMessage.query.filter_by(
        conversation_id=conversation.id,
        client_message_id=client_message_id,
    ).first()
    if existing:
        if existing.sender_type != "agent" or existing.body != body:
            return jsonify({"error": "Message identifier was already used."}), 409
        return jsonify({"message": message_payload(existing)}), 200
    if not allow_support_action(
        "support_agent_message",
        str(admin.id),
        limit=60,
        window_minutes=5,
    ):
        return jsonify({"error": "Please slow down before sending another reply."}), 429
    try:
        message, created = _store_message(
            conversation,
            body,
            client_message_id,
            sender_type="agent",
            sender_name=admin.username,
            sender_admin_id=admin.id,
        )
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("support_agent_message_create_failed conversation_id=%s", conversation.id)
        return jsonify({"error": "Your reply could not be saved. Please try again."}), 503
    if not message:
        return jsonify({"error": "Message identifier was already used."}), 409
    if created:
        _emit_message(conversation, message)
    return jsonify({"message": message_payload(message)}), 201 if created else 200


@support_admin_bp.post("/api/conversations/<public_id>/close")
@login_required
def close_conversation(public_id: str):
    admin = _require_support_admin()
    conversation = SupportConversation.query.filter_by(public_id=public_id).first_or_404()
    if not conversation.escalation_requested_at:
        return jsonify({"error": "The customer has not requested a support agent."}), 409
    if conversation.assigned_admin_id not in {None, admin.id}:
        return jsonify({"error": "Only the assigned agent can close this conversation."}), 403
    conversation.status = "closed"
    conversation.updated_at = datetime.utcnow()
    db.session.commit()
    payload = conversation_payload(conversation)
    socketio.emit(
        "closed",
        {"conversation": payload},
        to=conversation_room(conversation),
        namespace="/support",
    )
    socketio.emit(
        "queue_update",
        {"conversation_id": conversation.public_id, "status": "closed"},
        to="support-agents",
        namespace="/support",
    )
    return jsonify({"conversation": payload})


@support_admin_bp.get("/api/presence")
@login_required
def agent_presence():
    _require_support_admin()
    now = datetime.utcnow()
    agents = SupportAgentPresence.query.all()
    return jsonify(
        {
            "agents": [
                {
                    "username": row.admin.username,
                    "status": row.status,
                    "online": presence_is_fresh(row.heartbeat_at, now=now),
                }
                for row in agents
            ]
        }
    )
