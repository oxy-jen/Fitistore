from datetime import datetime

from flask import current_app
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import selectinload

from app.extensions import db
from app.models import MediaShowcase, MediaShowcaseItem


# Every key below has an explicit corresponding include in the named template.
SHOWCASE_PLACEMENTS = {
    "home": {
        "home_after_hero": "After hero and trust indicators (before Featured Picks)",
        "home_before_departments": "Before Departments",
        "home_before_mamba": "Before Mamba Collection",
        "home_before_upcoming": "Before Upcoming Merchandise",
        "home_before_footer": "Before homepage footer content",
    },
    "about": {"about_after_banner": "After About page banner", "about_before_story": "After navigation, before Our Story", "about_before_footer": "Before final About call to action"},
    "shop": {"shop_before_products": "Before product grid", "shop_before_footer": "Before Shop page footer content"},
    "upcoming": {"upcoming_before_products": "After banner, before upcoming merchandise", "upcoming_before_footer": "Before stock request form"},
}

LAYOUTS = {
    "carousel": "Horizontal media carousel", "banner": "Full-width promotional banner",
    "grid": "Responsive media grid", "featured": "Featured media player",
    "mixed": "Mixed-media gallery", "single": "Single media card", "split": "Split promotional section",
}

DENSITIES = {"compact": "Compact", "balanced": "Balanced", "spacious": "Spacious"}
CARD_ASPECT_RATIOS = {"1:1": "Square (1:1)", "4:3": "Classic (4:3)", "3:2": "Photo (3:2)", "16:9": "Widescreen (16:9)"}
SHOWCASE_POSTGRES_ADVISORY_LOCK = 724930119
SHOWCASE_TABLES = (MediaShowcase.__table__, MediaShowcaseItem.__table__)
SHOWCASE_BASE_TABLES = {"admin_user", "product"}


class MediaShowcaseSchemaError(RuntimeError):
    """Raised when feature tables cannot be safely created or validated."""


def _acquire_postgres_showcase_lock(connection):
    """Hold a transaction-scoped lock so concurrent workers serialize setup."""
    connection.execute(
        text("SELECT pg_advisory_xact_lock(:lock_id)"),
        {"lock_id": SHOWCASE_POSTGRES_ADVISORY_LOCK},
    )


def _validate_showcase_schema(connection):
    inspector = inspect(connection)
    problems = []
    for table in SHOWCASE_TABLES:
        if not inspector.has_table(table.name):
            problems.append(f"{table.name} table")
            continue
        actual_columns = {column["name"] for column in inspector.get_columns(table.name)}
        missing_columns = {column.name for column in table.columns} - actual_columns
        actual_indexes = {index["name"] for index in inspector.get_indexes(table.name)}
        expected_indexes = {index.name for index in table.indexes if index.name}
        missing_indexes = expected_indexes - actual_indexes
        if missing_columns:
            problems.append(f"{table.name} columns: " + ", ".join(sorted(missing_columns)))
        if missing_indexes:
            problems.append(f"{table.name} indexes: " + ", ".join(sorted(missing_indexes)))
    if problems:
        raise MediaShowcaseSchemaError("Incomplete Media Showcase schema: " + "; ".join(problems))


def _create_missing_showcase_tables(connection):
    names = set(inspect(connection).get_table_names())
    missing_base = SHOWCASE_BASE_TABLES - names
    if missing_base:
        raise MediaShowcaseSchemaError(
            "Required existing store tables are missing: " + ", ".join(sorted(missing_base))
        )
    # Restrict metadata creation to these two feature tables. Existing tables
    # are never altered; SQLAlchemy creates only absent tables and their indexes.
    db.metadata.create_all(bind=connection, tables=list(SHOWCASE_TABLES), checkfirst=True)
    # Repair only missing supporting indexes. This does not rewrite table data
    # or touch any unrelated store table.
    for table in SHOWCASE_TABLES:
        for index in table.indexes:
            index.create(bind=connection, checkfirst=True)
    _validate_showcase_schema(connection)


def ensure_media_showcase_schema(engine=None):
    """Create only absent Media Showcase tables, atomically and idempotently."""
    engine = engine or db.engine
    dialect = engine.dialect.name
    if dialect == "postgresql":
        # PostgreSQL transaction-level advisory locks release on commit, rollback,
        # or connection loss, including when a worker is interrupted at startup.
        with engine.begin() as connection:
            _acquire_postgres_showcase_lock(connection)
            _create_missing_showcase_tables(connection)
    elif dialect == "sqlite":
        # BEGIN IMMEDIATE serializes local SQLite workers before checking tables.
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                _create_missing_showcase_tables(connection)
                transaction.commit()
            except Exception:
                transaction.rollback()
                raise
    else:
        raise MediaShowcaseSchemaError(f"Unsupported database dialect: {dialect}")
    return True


def showcase_schema_ready():
    """Check the complete feature schema before running showcase ORM queries."""
    # Production app factories set this once at process startup. Tests and tools
    # that construct a test app can still inspect their schema on demand.
    startup_state = current_app.extensions.get("media_showcase_schema_ready")
    if startup_state is not None:
        return bool(startup_state)
    try:
        inspector = inspect(db.engine)
        for model in (MediaShowcase, MediaShowcaseItem):
            if not inspector.has_table(model.__tablename__):
                current_app.logger.error("Media showcases are unavailable because table %s is missing; run migrate-media-showcases.", model.__tablename__)
                return False
            available_columns = {column["name"] for column in inspector.get_columns(model.__tablename__)}
            missing_columns = {column.name for column in model.__table__.columns} - available_columns
            if missing_columns:
                current_app.logger.error("Media showcases are unavailable because table %s is incomplete; run migrate-media-showcases.", model.__tablename__)
                return False
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.error(
            "Media showcase schema check failed (%s).",
            type(exc).__name__,
        )
        return False
    return True


def active_showcases(page):
    """Load active, scheduled showcases for one public page in two queries."""
    # Keep public storefront routes available if web-startup setup is unavailable.
    if not showcase_schema_ready():
        return []

    now = datetime.utcnow()
    try:
        return (MediaShowcase.query.options(selectinload(MediaShowcase.items).selectinload(MediaShowcaseItem.linked_product))
                .filter_by(page_identifier=page, is_enabled=True, is_published=True)
                .filter(MediaShowcase.items.any(MediaShowcaseItem.is_active.is_(True)))
                .filter((MediaShowcase.starts_at.is_(None)) | (MediaShowcase.starts_at <= now))
                .filter((MediaShowcase.expires_at.is_(None)) | (MediaShowcase.expires_at > now))
                .order_by(MediaShowcase.placement_identifier, MediaShowcase.display_order, MediaShowcase.id).all())
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.error(
            "Media showcases are hidden because their query failed (%s); check the feature migration.",
            type(exc).__name__,
        )
        return []


def showcases_by_placement(page):
    """Combine all active showcases in each page slot into one shared media rail."""
    groups = {}
    for showcase in active_showcases(page):
        group = groups.setdefault(showcase.placement_identifier, {
            "primary": showcase, "showcases": [], "items": [],
        })
        group["showcases"].append(showcase)
        group["items"].extend(
            {"showcase": showcase, "item": item}
            for item in showcase.items if item.is_active
        )
    return {
        key: ([groups[key]] if key in groups else [])
        for key in SHOWCASE_PLACEMENTS.get(page, {})
    }

