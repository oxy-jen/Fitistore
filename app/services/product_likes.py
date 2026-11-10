"""Small, additive persistence layer for homepage product likes."""

from flask import current_app
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.models import ProductLike


PRODUCT_LIKES_POSTGRES_ADVISORY_LOCK = 724930123


def _ensure_product_like_table(connection):
    table = ProductLike.__table__
    if not inspect(connection).has_table(table.name):
        # Keep automatic DDL scoped to this new feature table only.
        db.metadata.create_all(bind=connection, tables=[table], checkfirst=True)
    else:
        for index in table.indexes:
            index.create(bind=connection, checkfirst=True)
    inspector = inspect(connection)
    if not inspector.has_table(table.name):
        raise RuntimeError("The product_like table was not created.")
    columns = {column["name"] for column in inspector.get_columns(table.name)}
    missing = {column.name for column in table.columns} - columns
    if missing:
        raise RuntimeError("The product_like table is incomplete.")
    unique_constraints = {constraint.get("name") for constraint in inspector.get_unique_constraints(table.name)}
    if "uq_product_like_voter" not in unique_constraints:
        raise RuntimeError("The product_like table is missing its unique voter constraint.")


def ensure_product_like_schema(engine=None):
    """Create only the missing likes table, serialized for concurrent startups."""
    engine = engine or db.engine
    if engine.dialect.name == "postgresql":
        with engine.begin() as connection:
            connection.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"),
                {"lock_id": PRODUCT_LIKES_POSTGRES_ADVISORY_LOCK},
            )
            _ensure_product_like_table(connection)
    elif engine.dialect.name == "sqlite":
        with engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                _ensure_product_like_table(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
    else:
        raise RuntimeError("Unsupported database dialect for product likes.")
    return True


def product_like_schema_ready():
    """Read readiness without attempting request-time schema changes."""
    ready = current_app.extensions.get("product_like_schema_ready")
    if ready is not None:
        return bool(ready)
    try:
        with db.engine.connect() as connection:
            if not inspect(connection).has_table(ProductLike.__tablename__):
                return False
        return True
    except SQLAlchemyError as exc:
        current_app.logger.error("Product likes are unavailable (%s).", type(exc).__name__)
        return False
