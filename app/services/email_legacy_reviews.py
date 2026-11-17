"""Fail-closed inspection and quarantine for legacy outbox messages.

Diagnostics never read message content or recipient addresses. Invalid messages
are retained in the outbox and cannot be retried by the provider worker.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

from sqlalchemy import inspect, select, text, update

from app.extensions import db
from app.models import EmailLegacyPayloadReviewEvent, EmailMessage

MIGRATION_ID = "20261117_email_legacy_review"
_MIGRATION_FILE = Path(__file__).resolve().parents[2] / "migrations" / f"{MIGRATION_ID}.sql"
_TABLE = EmailLegacyPayloadReviewEvent.__tablename__
_TRIGGER_FUNCTION = "fity_reject_email_legacy_review_mutation"


def migration_checksum() -> str:
    return hashlib.sha256(_MIGRATION_FILE.read_bytes()).hexdigest()


def normalize_check_definition(definition: str) -> str:
    """Normalize CHECK deparser output across SQLite and PostgreSQL casts."""
    value = "".join(str(definition or "").lower().replace('"', "").split())
    for cast in (
        "::charactervarying[]", "::charactervarying", "::varchar[]", "::varchar",
        "::text[]", "::text", "::boolean", "::integer", "::smallint", "::bigint",
    ):
        value = value.replace(cast, "")
    value = value.replace("check", "").replace("(", "").replace(")", "")
    return value


def apply_email_legacy_review_migration(connection, dialect: str) -> bool:
    if dialect not in {"sqlite", "postgresql", "postgres"}:
        raise ValueError("Unsupported legacy email review migration dialect.")
    prior = connection.execute(text(
        "SELECT checksum FROM email_schema_migration WHERE migration_id=:id"
    ), {"id": MIGRATION_ID}).scalar_one_or_none()
    checksum = migration_checksum()
    if prior is not None and prior != checksum:
        raise ValueError("Legacy email review migration checksum differs from this code revision.")
    from app.models import AdminUser
    if not inspect(connection).has_table("email_message") or not inspect(connection).has_table(AdminUser.__tablename__):
        raise ValueError("Email message and administrator tables must exist before legacy review migration.")
    db.metadata.create_all(bind=connection, tables=[EmailLegacyPayloadReviewEvent.__table__], checkfirst=True)
    if dialect in {"postgres", "postgresql"}:
        connection.execute(text(f"""
            CREATE OR REPLACE FUNCTION {_TRIGGER_FUNCTION}()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'legacy email review events are append-only'; END;
            $$
        """))
        connection.execute(text(f"""
            DO $$ BEGIN
              IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_email_legacy_review_immutable'
                  AND tgrelid='{_TABLE}'::regclass) THEN
                CREATE TRIGGER trg_email_legacy_review_immutable
                BEFORE UPDATE OR DELETE ON {_TABLE}
                FOR EACH ROW EXECUTE FUNCTION {_TRIGGER_FUNCTION}();
              END IF;
              IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_email_legacy_review_no_truncate'
                  AND tgrelid='{_TABLE}'::regclass) THEN
                CREATE TRIGGER trg_email_legacy_review_no_truncate
                BEFORE TRUNCATE ON {_TABLE}
                FOR EACH STATEMENT EXECUTE FUNCTION {_TRIGGER_FUNCTION}();
              END IF;
            END $$
        """))
    else:
        connection.execute(text(f"""
            CREATE TRIGGER IF NOT EXISTS trg_email_legacy_review_immutable
            BEFORE UPDATE ON {_TABLE}
            BEGIN SELECT RAISE(ABORT, 'legacy email review events are append-only'); END
        """))
        connection.execute(text(f"""
            CREATE TRIGGER IF NOT EXISTS trg_email_legacy_review_no_delete
            BEFORE DELETE ON {_TABLE}
            BEGIN SELECT RAISE(ABORT, 'legacy email review events are append-only'); END
        """))
    connection.execute(text(
        "INSERT INTO email_schema_migration (migration_id, checksum) VALUES (:id,:checksum) "
        "ON CONFLICT (migration_id) DO NOTHING"
    ), {"id": MIGRATION_ID, "checksum": checksum})
    validate_email_legacy_review_schema(connection)
    if inspect(connection).has_table("email_migration_history"):
        from app.services.email_migrations import record_migration
        record_migration(connection, MIGRATION_ID, state="applied", evidence="legacy review schema validated")
    return prior is None


def validate_email_legacy_review_schema(connection) -> None:
    inspector = inspect(connection)
    if not inspector.has_table(_TABLE):
        raise ValueError("Legacy email review event table is missing.")
    expected_columns = {
        "id", "message_id", "actor_admin_id", "actor_type", "action", "reason",
        "provider_outcome", "provider_evidence", "created_at",
    }
    if {row["name"] for row in inspector.get_columns(_TABLE)} != expected_columns:
        raise ValueError("Legacy email review event columns are incompatible.")
    if tuple(inspector.get_pk_constraint(_TABLE).get("constrained_columns") or ()) != ("id",):
        raise ValueError("Legacy email review event primary key is incompatible.")
    fk_shapes = {
        (tuple(row.get("constrained_columns") or ()), row.get("referred_table"),
         tuple(row.get("referred_columns") or ()), (row.get("options") or {}).get("ondelete", "").upper())
        for row in inspector.get_foreign_keys(_TABLE)
    }
    if fk_shapes != {
        (("message_id",), "email_message", ("id",), "RESTRICT"),
        (("actor_admin_id",), "admin_user", ("id",), "RESTRICT"),
    }:
        raise ValueError("Legacy email review event foreign keys are incompatible.")
    checks = {row.get("name"): normalize_check_definition(row.get("sqltext") or "")
              for row in inspector.get_check_constraints(_TABLE)}
    expected_checks = {
        "ck_email_legacy_review_actor": "(actor_type='admin'andactor_admin_idisnotnull)or(actor_type='system'andactor_admin_idisnull)",
        "ck_email_legacy_review_action": "actionin('quarantined','reviewed')",
        "ck_email_legacy_review_provider_outcome": "provider_outcomein('unknown','confirmed_not_accepted','confirmed_accepted')",
    }
    for name, expected in expected_checks.items():
        actual = checks.get(name, "")
        expected = expected.replace("(", "").replace(")", "")
        any_form = expected.replace("actionin", "action=anyarray", 1) if expected.startswith("actionin") else (
            expected.replace("provider_outcomein", "provider_outcome=anyarray", 1)
            if expected.startswith("provider_outcomein") else ""
        )
        if actual != expected and actual != any_form:
            raise ValueError(f"Legacy email review constraint {name} is missing or altered.")
    index = next((row for row in inspector.get_indexes(_TABLE)
                  if row["name"] == "ix_email_legacy_review_message_created"), None)
    if not index or tuple(index.get("column_names") or ()) != ("message_id", "created_at") or index.get("unique"):
        raise ValueError("Legacy email review event index is missing or incompatible.")
    if connection.dialect.name == "postgresql":
        rows = connection.execute(text("""
            SELECT t.tgname, t.tgenabled, t.tgtype, p.proname, p.prosrc,
                   p.prorettype::regtype::text AS return_type, c.relname, n.nspname
            FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            JOIN pg_proc p ON p.oid=t.tgfoid
            WHERE c.relname=:table AND n.nspname=current_schema() AND NOT t.tgisinternal
        """), {"table": _TABLE}).mappings().all()
        by_name = {row["tgname"]: row for row in rows}
        expected = {"trg_email_legacy_review_immutable": 27, "trg_email_legacy_review_no_truncate": 34}
        for name, trigger_type in expected.items():
            row = by_name.get(name)
            source = "".join((row["prosrc"] if row else "").lower().split())
            if (not row or row["tgenabled"] != "O" or row["tgtype"] != trigger_type
                    or row["proname"] != _TRIGGER_FUNCTION or row["return_type"] != "trigger"
                    or row["relname"] != _TABLE or source != "beginraiseexception'legacyemailrevieweventsareappend-only';end;"):
                raise ValueError(f"Legacy email review trigger {name} is missing, disabled, or altered.")
    else:
        triggers = {row["name"]: "".join((row["sql"] or "").lower().split())
                    for row in connection.execute(text("SELECT name,sql FROM sqlite_master WHERE type='trigger'")).mappings()}
        if not all(name in triggers and "legacyemailrevieweventsareappend-only" in triggers[name]
                   for name in ("trg_email_legacy_review_immutable", "trg_email_legacy_review_no_delete")):
            raise ValueError("SQLite legacy email review append-only triggers are missing or altered.")
    if connection.dialect.name == "postgresql":
        default = connection.execute(text("""
            SELECT pg_get_expr(d.adbin, d.adrelid)
            FROM pg_attrdef d JOIN pg_class c ON c.oid=d.adrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=d.adnum
            WHERE n.nspname=current_schema() AND c.relname=:table AND a.attname='id'
        """), {"table": _TABLE}).scalar_one_or_none()
        sequence = connection.execute(text("""
            SELECT s.relname FROM pg_class t JOIN pg_namespace n ON n.oid=t.relnamespace
            JOIN pg_attribute a ON a.attrelid=t.oid AND a.attname='id'
            JOIN pg_depend d ON d.refobjid=t.oid AND d.refobjsubid=a.attnum AND d.deptype IN ('a','i')
            JOIN pg_class s ON s.oid=d.objid AND s.relkind='S'
            WHERE n.nspname=current_schema() AND t.relname=:table
        """), {"table": _TABLE}).scalar_one_or_none()
        normalized_default = "".join(str(default or "").lower().split())
        if not sequence or not normalized_default.startswith("nextval(") or sequence.lower() not in normalized_default:
            raise ValueError("Legacy email review event ID is not sequence-backed.")
    migration = connection.execute(text("SELECT checksum FROM email_schema_migration WHERE migration_id=:id"),
                                   {"id": MIGRATION_ID}).scalar_one_or_none()
    if migration != migration_checksum():
        raise ValueError("Legacy email review migration checksum is missing or incompatible.")


def inspect_legacy_email_payloads(connection=None) -> list[dict]:
    """Return metadata-only findings; never read recipients, subjects or bodies."""
    owns_connection = connection is None
    connection = connection or db.engine.connect()
    try:
        inspector = inspect(connection)
        if not inspector.has_table("email_message"):
            return []
        columns = {row["name"] for row in inspector.get_columns("email_message")}
        optional = {
            "provider_message_id": "provider_message_id" in columns,
            "sent_at": "sent_at" in columns,
            "html_body": "html_body" in columns,
            "text_body": "text_body" in columns,
            "error_category": "error_category" in columns,
        }
        select_parts = ["id", "kind", "status", "created_at"]
        select_parts.append("provider_message_id" if optional["provider_message_id"] else "NULL AS provider_message_id")
        select_parts.append("sent_at" if optional["sent_at"] else "NULL AS sent_at")
        select_parts.append("html_body" if optional["html_body"] else "NULL AS html_body")
        select_parts.append("text_body" if optional["text_body"] else "NULL AS text_body")
        select_parts.append("error_category" if optional["error_category"] else "NULL AS error_category")
        records = connection.execute(text("SELECT " + ", ".join(select_parts) + " FROM email_message ORDER BY id")).mappings().all()
        attempt_counts = {}
        if inspector.has_table("email_delivery_attempt"):
            for row in connection.execute(text(
                "SELECT message_id, count(*) AS attempt_count, "
                "max(CASE WHEN status IN ('in_flight','ambiguous') THEN 1 ELSE 0 END) AS uncertain, "
                "max(CASE WHEN status='accepted' THEN 1 ELSE 0 END) AS accepted "
                "FROM email_delivery_attempt GROUP BY message_id"
            )).mappings():
                attempt_counts[row["message_id"]] = row
        findings = []
        for row in records:
            missing_html = not isinstance(row["html_body"], str) or not row["html_body"].strip()
            missing_text = not isinstance(row["text_body"], str) or not row["text_body"].strip()
            status = str(row["status"] or "")
            attempts = attempt_counts.get(row["id"], {})
            if missing_html or missing_text:
                if status in {"queued", "sending"}:
                    classification = "legacy_payload_incomplete"
                elif status == "sent" or row["provider_message_id"] or row["sent_at"] or attempts.get("accepted"):
                    classification = "provider_acceptance_evidence_present"
                elif status == "failed" and attempts.get("uncertain"):
                    classification = "ambiguous_provider_outcome"
                else:
                    classification = "incomplete_payload_requires_review"
                findings.append({
                    "message_id": row["id"], "kind": row["kind"], "status": status,
                    "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                    "missing_html": missing_html, "missing_text": missing_text,
                    "attempt_count": int(attempts.get("attempt_count") or 0),
                    "provider_id_present": bool(row["provider_message_id"]),
                    "sent_timestamp_present": row["sent_at"] is not None,
                    "classification": classification,
                    "provider_acceptance_proven": bool(row["provider_message_id"] or row["sent_at"] or attempts.get("accepted")),
                })
            elif status == "failed" and row["error_category"] == "ambiguous_outcome":
                findings.append({
                    "message_id": row["id"], "kind": row["kind"], "status": status,
                    "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                    "missing_html": False, "missing_text": False,
                    "attempt_count": int(attempts.get("attempt_count") or 0),
                    "provider_id_present": bool(row["provider_message_id"]),
                    "sent_timestamp_present": row["sent_at"] is not None,
                    "classification": "ambiguous_provider_outcome",
                    "provider_acceptance_proven": bool(row["provider_message_id"] or attempts.get("accepted")),
                })
        return findings
    finally:
        if owns_connection:
            connection.close()


def quarantine_incomplete_legacy_message(message_id: int, *, actor_admin_id: int | None,
                                         reason: str, provider_outcome: str = "unknown",
                                         provider_evidence: str = "") -> EmailMessage:
    """Keep the original row/payload and permanently remove it from dispatch."""
    reason = str(reason or "").strip()
    if len(reason) < 8 or len(reason) > 2000:
        raise ValueError("Provide a review reason between 8 and 2000 characters.")
    if provider_outcome not in {"unknown", "confirmed_not_accepted", "confirmed_accepted"}:
        raise ValueError("Provider outcome is invalid.")
    provider_evidence = str(provider_evidence or "").strip()[:4000]
    record = db.session.execute(select(EmailMessage).where(
        EmailMessage.id == int(message_id)
    ).with_for_update()).scalar_one_or_none()
    if record is None:
        raise ValueError("Email message was not found.")
    if record.status not in {"queued", "failed"}:
        raise ValueError("Only queued or failed messages can be quarantined for payload review.")
    if (isinstance(record.html_body, str) and record.html_body.strip()
            and isinstance(record.text_body, str) and record.text_body.strip()):
        raise ValueError("This message has complete content and does not need legacy payload quarantine.")
    existing = EmailLegacyPayloadReviewEvent.query.filter_by(message_id=record.id, action="quarantined").first()
    if existing and (actor_admin_id is None or record.error_category != "legacy_payload_review"):
        raise ValueError("This message already has an audited quarantine event.")
    now = datetime.utcnow()
    event = EmailLegacyPayloadReviewEvent(
        message_id=record.id, actor_admin_id=actor_admin_id,
        actor_type="admin" if actor_admin_id is not None else "system",
        action="reviewed" if existing else "quarantined", reason=reason, provider_outcome=provider_outcome,
        provider_evidence=provider_evidence, created_at=now,
    )
    record.status = "failed"
    record.error_category = "legacy_payload_review"
    record.failure_reason = "Legacy email content is incomplete; quarantined for manual review."
    record.claim_token = None
    record.claimed_at = None
    record.next_attempt_at = None
    record.updated_at = now
    db.session.add(event)
    db.session.commit()
    return record


def quarantine_incomplete_queued_messages(*, now: datetime | None = None) -> int:
    """Worker preflight quarantine with a durable system audit event."""
    now = now or datetime.utcnow()
    rows = EmailMessage.query.filter(
        EmailMessage.status == "queued",
        (EmailMessage.html_body.is_(None) | (db.func.trim(EmailMessage.html_body) == "")
         | EmailMessage.text_body.is_(None) | (db.func.trim(EmailMessage.text_body) == "")),
    ).with_for_update().all()
    if not rows:
        return 0
    ids = [row.id for row in rows]
    already_audited = {item[0] for item in db.session.query(
        EmailLegacyPayloadReviewEvent.message_id
    ).filter(EmailLegacyPayloadReviewEvent.message_id.in_(ids)).all()}
    for row in rows:
        if row.id not in already_audited:
            db.session.add(EmailLegacyPayloadReviewEvent(
                message_id=row.id, actor_type="system", action="quarantined",
                reason="Worker preflight found missing or empty stored HTML/text payload; dispatch blocked.",
                provider_outcome="unknown", provider_evidence="No provider outcome inferred from missing legacy payload.",
                created_at=now,
            ))
        row.status = "failed"
        row.error_category = "legacy_payload_review"
        row.failure_reason = "Legacy email content is incomplete; quarantined for manual review."
        row.claim_token = None
        row.claimed_at = None
        row.next_attempt_at = None
        row.updated_at = now
    db.session.commit()
    return len(rows)
