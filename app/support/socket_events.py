from __future__ import annotations

from datetime import datetime

from flask import current_app, request
from flask_socketio import emit, join_room
from flask_wtf.csrf import validate_csrf
from wtforms.validators import ValidationError

from app.extensions import db, socketio
from app.models import SupportAgentPresence, SupportConversation
from app.services.support_chat import (
    PRESENCE_FRESHNESS,
    SUPPORT_GUEST_COOKIE,
    allow_support_action,
    conversation_payload,
    conversation_room,
    current_admin,
    guest_token_digest,
    owns_conversation,
)


def _valid_csrf(payload: dict | None) -> bool:
    if not current_app.config.get("WTF_CSRF_ENABLED", True):
        return True
    try:
        validate_csrf(str((payload or {}).get("csrf_token") or ""))
    except ValidationError:
        return False
    return True


@socketio.on("connect", namespace="/support")
def support_connect(auth=None):
    return True


@socketio.on("join_conversation", namespace="/support")
def join_conversation(payload):
    if not _valid_csrf(payload):
        return {"ok": False}
    public_id = str((payload or {}).get("conversation_id") or "")[:32]
    conversation = SupportConversation.query.filter_by(public_id=public_id).first()
    if not conversation:
        return {"ok": False}
    if not current_admin() and not owns_conversation(conversation):
        return {"ok": False}
    join_room(conversation_room(conversation))
    return {"ok": True, "conversation": conversation_payload(conversation)}


@socketio.on("join_support_queue", namespace="/support")
def join_support_queue(payload):
    admin = current_admin()
    if not admin or not _valid_csrf(payload):
        return {"ok": False}
    join_room("support-agents")
    return {"ok": True}


@socketio.on("typing", namespace="/support")
def typing(payload):
    if not _valid_csrf(payload):
        return {"ok": False}
    public_id = str((payload or {}).get("conversation_id") or "")[:32]
    conversation = SupportConversation.query.filter_by(public_id=public_id).first()
    if not conversation or conversation.status == "closed":
        return {"ok": False}
    admin = current_admin()
    if admin:
        if conversation.assigned_admin_id != admin.id:
            return {"ok": False}
        identity = f"agent:{admin.id}"
        sender_type = "agent"
    else:
        if not owns_conversation(conversation):
            return {"ok": False}
        digest = guest_token_digest(request.cookies.get(SUPPORT_GUEST_COOKIE))
        identity = f"guest:{digest or conversation.account_id}"
        sender_type = "customer"
    if not allow_support_action(
        "support_typing",
        identity,
        limit=60,
        window_minutes=1,
    ):
        return {"ok": False}
    emit(
        "typing",
        {
            "conversation_id": conversation.public_id,
            "sender_type": sender_type,
            "typing": bool((payload or {}).get("typing")),
        },
        to=conversation_room(conversation),
        skip_sid=request.sid,
    )
    return {"ok": True}


@socketio.on("agent_heartbeat", namespace="/support")
def agent_heartbeat(payload):
    admin = current_admin()
    if not admin or not _valid_csrf(payload):
        return {"ok": False}
    status = str((payload or {}).get("status") or "away").strip().lower()
    if status not in {"available", "busy", "away"}:
        return {"ok": False}
    if not allow_support_action(
        "support_presence",
        str(admin.id),
        limit=8,
        window_minutes=1,
    ):
        return {"ok": False}

    presence = db.session.get(SupportAgentPresence, admin.id)
    if not presence:
        presence = SupportAgentPresence(admin_id=admin.id, status=status)
        db.session.add(presence)
    presence.status = status
    presence.heartbeat_at = datetime.utcnow()
    db.session.commit()

    available = SupportAgentPresence.query.filter(
        SupportAgentPresence.status == "available",
        SupportAgentPresence.heartbeat_at >= datetime.utcnow()
        - PRESENCE_FRESHNESS,
    ).count()
    emit("presence", {"available": available > 0}, namespace="/support", broadcast=True)
    return {"ok": True, "available": available > 0}
