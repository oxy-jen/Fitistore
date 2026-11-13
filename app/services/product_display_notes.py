"""Persistence for concise, admin-managed product option guidance."""

from flask import current_app
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.models import ProductDisplayNote


PRODUCT_DISPLAY_NOTE_POSTGRES_ADVISORY_LOCK = 724930124


def _ensure_note_table(connection):
    table = ProductDisplayNote.__table__
    if not inspect(connection).has_table(table.name):
        db.metadata.create_all(bind=connection, tables=[table], checkfirst=True)
    inspector = inspect(connection)
    if not inspector.has_table(table.name):
        raise RuntimeError("The product display note table was not created.")
    columns = {column["name"] for column in inspector.get_columns(table.name)}
    if {column.name for column in table.columns} - columns:
        raise RuntimeError("The product display note table is incomplete.")


def ensure_product_display_note_schema(engine=None):
    """Create only the new note table, serializing concurrent web startups."""
    engine = engine or db.engine
    if engine.dialect.name == "postgresql":
        with engine.begin() as connection:
            connection.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"),
                {"lock_id": PRODUCT_DISPLAY_NOTE_POSTGRES_ADVISORY_LOCK},
            )
            _ensure_note_table(connection)
    elif engine.dialect.name == "sqlite":
        with engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                _ensure_note_table(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
    else:
        raise RuntimeError("Unsupported database dialect for product display notes.")
    return True


def product_display_note_schema_ready():
    ready = current_app.extensions.get("product_display_note_schema_ready")
    if ready is not None:
        return bool(ready)
    try:
        with db.engine.connect() as connection:
            return inspect(connection).has_table(ProductDisplayNote.__tablename__)
    except SQLAlchemyError as exc:
        current_app.logger.error("Product display notes are unavailable (%s).", type(exc).__name__)
        return False


def get_product_display_note(product_id):
    if not product_display_note_schema_ready():
        return ""
    try:
        row = db.session.get(ProductDisplayNote, product_id)
        return row.note if row else ""
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.warning("Could not load a product display note (%s).", type(exc).__name__)
        return ""


def save_product_display_note(product_id, note):
    """Update the note in the caller's transaction; the caller commits with product edits."""
    if not product_display_note_schema_ready():
        raise RuntimeError("Product note storage is not ready.")
    note = (note or "").strip()[:180]
    row = db.session.get(ProductDisplayNote, product_id)
    if not note:
        if row:
            db.session.delete(row)
        return
    if row is None:
        row = ProductDisplayNote(product_id=product_id, note=note)
        db.session.add(row)
    else:
        row.note = note
