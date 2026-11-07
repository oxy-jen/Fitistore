"""Safe startup setup for admin-managed Upcoming page cards."""

from flask import current_app
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.models import UpcomingProduct


UPCOMING_PRODUCT_POSTGRES_ADVISORY_LOCK = 724930120


class UpcomingProductSchemaError(RuntimeError):
    """Raised when the Upcoming Products table cannot be safely initialized."""


def _validate_upcoming_product_schema(connection):
    table = UpcomingProduct.__table__
    inspector = inspect(connection)
    if not inspector.has_table(table.name):
        raise UpcomingProductSchemaError("The upcoming_product table is missing.")
    actual_columns = {column["name"] for column in inspector.get_columns(table.name)}
    missing_columns = {column.name for column in table.columns} - actual_columns
    actual_indexes = {index["name"] for index in inspector.get_indexes(table.name)}
    expected_indexes = {index.name for index in table.indexes if index.name}
    missing_indexes = expected_indexes - actual_indexes
    problems = []
    if missing_columns:
        problems.append("columns: " + ", ".join(sorted(missing_columns)))
    if missing_indexes:
        problems.append("indexes: " + ", ".join(sorted(missing_indexes)))
    if problems:
        raise UpcomingProductSchemaError("Incomplete upcoming_product schema (" + "; ".join(problems) + ").")


def _create_missing_upcoming_product_table(connection):
    table = UpcomingProduct.__table__
    if not inspect(connection).has_table(table.name):
        # Restrict DDL to this one feature table. Existing store tables and rows
        # are not included in metadata creation.
        db.metadata.create_all(bind=connection, tables=[table], checkfirst=True)
    else:
        # create_all(checkfirst=True) does not add indexes to an existing table.
        # Repair only indexes belonging to UpcomingProduct.
        for index in table.indexes:
            index.create(bind=connection, checkfirst=True)
    _validate_upcoming_product_schema(connection)


def ensure_upcoming_product_schema(engine=None):
    """Create only the missing upcoming_product table and its indexes."""
    engine = engine or db.engine
    if engine.dialect.name == "postgresql":
        with engine.begin() as connection:
            connection.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"),
                {"lock_id": UPCOMING_PRODUCT_POSTGRES_ADVISORY_LOCK},
            )
            _create_missing_upcoming_product_table(connection)
    elif engine.dialect.name == "sqlite":
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                _create_missing_upcoming_product_table(connection)
                transaction.commit()
            except Exception:
                transaction.rollback()
                raise
    else:
        raise UpcomingProductSchemaError(f"Unsupported database dialect: {engine.dialect.name}")
    return True


def upcoming_product_schema_ready():
    """Check readiness without attempting to create schema during a request."""
    startup_state = current_app.extensions.get("upcoming_product_schema_ready")
    if startup_state is not None:
        return bool(startup_state)
    try:
        with db.engine.connect() as connection:
            _validate_upcoming_product_schema(connection)
    except SQLAlchemyError as exc:
        current_app.logger.error("Upcoming products are unavailable (%s).", type(exc).__name__)
        return False
    except UpcomingProductSchemaError as exc:
        current_app.logger.error("Upcoming products are unavailable (%s).", str(exc))
        return False
    return True
