"""Fail-closed guard for the manually triggered customer email migration."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from dataclasses import dataclass

CONFIRMATION = "MIGRATE CUSTOMER EMAIL SCHEMA"
BACKUP_CONFIRMATION = "I HAVE A VERIFIED RESTORABLE BACKUP"
PREFLIGHT_CONFIRMATION = "PREFLIGHT CUSTOMER EMAIL SCHEMA"
EXPECTED_TABLES = {
    "customer_account", "customer_token", "customer_consent_event",
    "customer_order_link", "auth_rate_limit", "email_campaign",
    "email_message", "email_campaign_recipient", "customer_cart",
    "marketing_automation_settings", "marketing_campaign_run",
    "marketing_product_exposure",
}
BASE_COLUMNS = {"admin_user": {"id"}, "order": {"id"}, "product": {"id"}}
# These are the only missing objects approved for an existing feature schema.
APPROVED_MISSING_COLUMNS = {
    ("customer_account", "two_factor_enabled"),
    ("email_message", "retry_count"),
    ("email_message", "provider_message_id"),
}
APPROVED_MISSING_INDEXES = {"ix_email_message_provider_message_id"}
def migration_index_manifest() -> dict[str, tuple[str, tuple[str, ...], bool]]:
    sql = migration_sql()
    pattern = re.compile(
        r"CREATE\s+(UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+([a-zA-Z_][a-zA-Z0-9_]*)\s+"
        r"ON\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(([^)]+)\)",
        re.IGNORECASE,
    )
    result = {}
    for match in pattern.finditer(sql):
        unique, name, table, columns = match.groups()
        result[name] = (table.lower(), tuple(part.strip().strip('"').lower() for part in columns.split(",")), bool(unique))
    if not result:
        raise GuardError("Migration index definitions could not be verified.")
    statement_count = len(re.findall(r"CREATE\s+(?:UNIQUE\s+)?INDEX\b", sql, re.I))
    if statement_count != len(result):
        raise GuardError("One or more migration index definitions could not be parsed.")
    return result


def migration_sql() -> str:
    sql_path = Path(__file__).resolve().parent.parent / "migrations" / "20261017_customer_accounts_email.sql"
    return sql_path.read_text(encoding="utf-8")


def migration_column_defaults() -> dict[tuple[str, str], str | None]:
    return {key: spec.default for key, spec in migration_column_manifest().items()}


@dataclass(frozen=True)
class ColumnSpec:
    sql_type: str
    nullable: bool
    default: str | None


def _split_sql_items(body: str) -> list[str]:
    parts, start, depth, quoted = [], 0, 0, False
    index = 0
    while index < len(body):
        char = body[index]
        if char == "'":
            if quoted and index + 1 < len(body) and body[index + 1] == "'":
                index += 1
            else:
                quoted = not quoted
        elif not quoted:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif char == "," and depth == 0:
                parts.append(body[start:index].strip())
                start = index + 1
        index += 1
    parts.append(body[start:].strip())
    return [part for part in parts if part]


_SQL_TYPE = (
    r"SERIAL|SMALLSERIAL|BIGSERIAL|SMALLINT|INTEGER|INT|BIGINT|BOOLEAN|BOOL|TEXT|"
    r"VARCHAR\s*\(\s*\d+\s*\)|CHARACTER\s+VARYING\s*\(\s*\d+\s*\)|"
    r"CHAR\s*\(\s*\d+\s*\)|NUMERIC(?:\s*\(\s*\d+\s*,\s*\d+\s*\))?|"
    r"DECIMAL(?:\s*\(\s*\d+\s*,\s*\d+\s*\))?|REAL|DOUBLE\s+PRECISION|"
    r"TIMESTAMP(?:\s+(?:WITH|WITHOUT)\s+TIME\s+ZONE)?|"
    r"TIME(?:\s+(?:WITH|WITHOUT)\s+TIME\s+ZONE)?|DATE|BYTEA"
)
_COLUMN_DECLARATION = re.compile(rf'^"?([A-Za-z_][A-Za-z0-9_]*)"?\s+({_SQL_TYPE})(?=\s|$)(.*)$', re.I | re.S)
_DEFAULT = re.compile(r"\bDEFAULT\s+('(?:''|[^'])*'|CURRENT_TIMESTAMP|TRUE|FALSE|-?\d+(?:\.\d+)?)", re.I)


def canonical_sql_type(value: str) -> str:
    result = re.sub(r"\s+", " ", value.strip().upper())
    result = re.sub(r"\s*\(\s*", "(", result)
    result = re.sub(r"\s*\)", ")", result)
    result = re.sub(r"\s*,\s*", ",", result)
    aliases = {"INT": "INTEGER", "BOOL": "BOOLEAN", "SMALLSERIAL": "SMALLINT", "SERIAL": "INTEGER", "BIGSERIAL": "BIGINT", "TIMESTAMP": "TIMESTAMP WITHOUT TIME ZONE", "TIME": "TIME WITHOUT TIME ZONE"}
    return aliases.get(result, result)


def migration_column_manifest() -> dict[tuple[str, str], ColumnSpec]:
    sql = migration_sql()
    manifest: dict[tuple[str, str], ColumnSpec] = {}
    tables = re.compile(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(\w+)\s*\((.*?)\)\s*;", re.I | re.S)
    for match in tables.finditer(sql):
        table, body = match.groups()
        for declaration in _split_sql_items(body):
            if declaration.upper().startswith(("CONSTRAINT ", "PRIMARY KEY", "UNIQUE ", "FOREIGN KEY")):
                continue
            parsed = _COLUMN_DECLARATION.match(declaration)
            if not parsed:
                raise GuardError("Migration column definition could not be parsed.")
            column, sql_type, options = parsed.groups()
            default_match = _DEFAULT.search(options)
            if re.search(r"\bDEFAULT\b", options, re.I) and not default_match:
                raise GuardError("Migration column default could not be parsed.")
            primary_key = bool(re.search(r"\bPRIMARY\s+KEY\b", options, re.I))
            nullable = not primary_key and not bool(re.search(r"\bNOT\s+NULL\b", options, re.I))
            default = "__serial__" if sql_type.upper() in {"SERIAL", "SMALLSERIAL", "BIGSERIAL"} else (default_match.group(1) if default_match else None)
            manifest[(table.lower(), column.lower())] = ColumnSpec(canonical_sql_type(sql_type), nullable, default)
    alters = re.compile(r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+(.*?);", re.I | re.S)
    for table, declaration in alters.findall(sql):
        parsed = _COLUMN_DECLARATION.match(declaration.strip())
        if not parsed:
            raise GuardError("Migration ALTER COLUMN definition could not be parsed.")
        column, sql_type, options = parsed.groups()
        default_match = _DEFAULT.search(options)
        if re.search(r"\bDEFAULT\b", options, re.I) and not default_match:
            raise GuardError("Migration ALTER default could not be parsed.")
        nullable = not bool(re.search(r"\bNOT\s+NULL\b", options, re.I))
        manifest[(table.lower(), column.lower())] = ColumnSpec(
            canonical_sql_type(sql_type), nullable, default_match.group(1) if default_match else None
        )
    if not manifest:
        raise GuardError("Migration column definitions could not be verified.")
    if {table for table, _column in manifest} != EXPECTED_TABLES:
        raise GuardError("Migration table manifest does not match the expected customer/email table set.")
    return manifest


def migration_constraint_manifest():
    sql = migration_sql()
    tables = re.compile(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(\w+)\s*\((.*?)\)\s*;", re.I | re.S)
    unique = {table: set() for table in EXPECTED_TABLES}
    named_unique = {table: {} for table in EXPECTED_TABLES}
    foreign_keys = {table: set() for table in EXPECTED_TABLES}
    for match in tables.finditer(sql):
        table, body = match.groups()
        table = table.lower()
        for declaration in _split_sql_items(body):
            named = re.match(r"CONSTRAINT\s+(\w+)\s+UNIQUE\s*\(([^)]+)\)", declaration, re.I)
            if named:
                constraint_name, columns = named.groups()
                cols = tuple(column.strip().strip('"').lower() for column in columns.split(","))
                unique[table].add(cols)
                named_unique[table][constraint_name] = cols
                continue
            parsed = _COLUMN_DECLARATION.match(declaration)
            if not parsed:
                continue
            column, _sql_type, options = parsed.groups()
            if re.search(r"\bUNIQUE\b", options, re.I):
                unique[table].add((column.lower(),))
            reference = re.search(r"\bREFERENCES\s+\"?([A-Za-z_][A-Za-z0-9_]*)\"?\s*\(\s*\"?([A-Za-z_][A-Za-z0-9_]*)\"?\s*\)", options, re.I)
            if reference:
                target_table, target_column = reference.groups()
                foreign_keys[table].add((column.lower(), target_table.lower(), target_column.lower()))
    if set(unique) != EXPECTED_TABLES or len(foreign_keys) != len(EXPECTED_TABLES):
        raise GuardError("Migration constraint definitions could not be verified.")
    return unique, named_unique, foreign_keys


def validate_column_snapshot(table, column, actual_type, actual_nullable, actual_default, expected):
    if canonical_sql_type(actual_type) != expected.sql_type:
        raise GuardError(f"Incompatible PostgreSQL type for {table}.{column}.")
    if bool(actual_nullable) != expected.nullable:
        raise GuardError(f"Incompatible nullability for {table}.{column}.")
    if expected.default == "__serial__":
        default_matches = bool(actual_default and "nextval(" in actual_default.lower())
    else:
        default_matches = normalize_default(actual_default) == normalize_default(expected.default)
    if not default_matches:
        raise GuardError(f"Incompatible default for {table}.{column}.")


def missing_objects(table, expected, actual, approved, kind):
    missing = expected - actual
    if missing - approved:
        raise GuardError(f"Unexpected missing {kind} in {table}.")
    return missing


def validate_missing_state(table, expected, actual, approved, allow_approved, kind):
    missing = missing_objects(table, expected, actual, approved, kind)
    if missing and not allow_approved:
        raise GuardError(f"Post-migration missing {kind} remain in {table}.")
    return missing


def validate_fk_options(options):
    options = options or {}
    if (options.get("match") or "SIMPLE").upper() != "SIMPLE":
        raise GuardError("Incompatible foreign-key match mode.")
    if bool(options.get("deferrable", False)):
        raise GuardError("Migration foreign keys must not be deferrable.")
    if options.get("initially") not in {None, "IMMEDIATE", "immediate"}:
        raise GuardError("Incompatible foreign-key timing mode.")


def validate_postgres_index_snapshot(index_name, expected_table, expected_columns, expected_unique, snapshot):
    """Fail closed unless PostgreSQL catalog metadata matches a plain btree index."""
    if not isinstance(snapshot, dict):
        raise GuardError(f"PostgreSQL metadata unavailable for index {index_name}.")
    required = {
        "name", "table", "columns", "unique", "valid", "has_predicate",
        "included_columns", "access_method", "key_count", "total_count",
    }
    if not required <= snapshot.keys():
        raise GuardError(f"PostgreSQL metadata incomplete for index {index_name}.")
    if (
        snapshot["name"] != index_name
        or snapshot["table"] != expected_table
        or tuple(snapshot["columns"] or ()) != tuple(expected_columns)
        or bool(snapshot["unique"]) != bool(expected_unique)
        or snapshot["valid"] is not True
        or snapshot["has_predicate"] is not False
        or tuple(snapshot["included_columns"] or ())
        or str(snapshot["access_method"]).lower() != "btree"
        or snapshot["key_count"] != len(expected_columns)
        or snapshot["total_count"] != len(expected_columns)
    ):
        raise GuardError(f"Incompatible PostgreSQL index definition: {index_name}.")


def postgres_index_catalog(connection, schema, table, index_names):
    """Read exact index properties from PostgreSQL catalogs; unsupported metadata fails closed."""
    from sqlalchemy import text

    if connection.dialect.name != "postgresql":
        raise GuardError("PostgreSQL index catalog requested for a non-PostgreSQL connection.")
    statement = text("""
        SELECT index_class.relname AS index_name,
               table_class.relname AS table_name,
               index_data.indisunique AS is_unique,
               index_data.indisvalid AS is_valid,
               index_data.indpred IS NOT NULL AS has_predicate,
               index_data.indnkeyatts AS key_count,
               index_data.indnatts AS total_count,
               access_method.amname AS access_method,
               ARRAY(
                   SELECT attribute.attname
                   FROM unnest(index_data.indkey) WITH ORDINALITY AS key_column(attnum, ordinal)
                   JOIN pg_attribute AS attribute
                     ON attribute.attrelid = table_class.oid
                    AND attribute.attnum = key_column.attnum
                   WHERE key_column.ordinal <= index_data.indnkeyatts
                   ORDER BY key_column.ordinal
               ) AS key_columns,
               ARRAY(
                   SELECT attribute.attname
                   FROM unnest(index_data.indkey) WITH ORDINALITY AS key_column(attnum, ordinal)
                   JOIN pg_attribute AS attribute
                     ON attribute.attrelid = table_class.oid
                    AND attribute.attnum = key_column.attnum
                   WHERE key_column.ordinal > index_data.indnkeyatts
                   ORDER BY key_column.ordinal
               ) AS included_columns
        FROM pg_class AS table_class
        JOIN pg_namespace AS table_namespace ON table_namespace.oid = table_class.relnamespace
        JOIN pg_index AS index_data ON index_data.indrelid = table_class.oid
        JOIN pg_class AS index_class ON index_class.oid = index_data.indexrelid
        JOIN pg_am AS access_method ON access_method.oid = index_class.relam
        WHERE table_namespace.nspname = :schema
          AND table_class.relname = :table
          AND index_class.relname = ANY(:index_names)
    """)
    try:
        rows = connection.execute(statement, {"schema": schema, "table": table, "index_names": list(index_names)}).mappings()
        return {
            row["index_name"]: {
                "name": row["index_name"], "table": row["table_name"],
                "columns": tuple(row["key_columns"] or ()), "unique": row["is_unique"],
                "valid": row["is_valid"],
                "has_predicate": row["has_predicate"],
                "included_columns": tuple(row["included_columns"] or ()),
                "access_method": row["access_method"],
                "key_count": row["key_count"], "total_count": row["total_count"],
            }
            for row in rows
        }
    except GuardError:
        raise
    except Exception as exc:
        raise GuardError(
            f"PostgreSQL index catalog inspection failed ({type(exc).__name__}); diagnostic details suppressed."
        ) from None


def normalize_default(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower().strip("()")
    normalized = re.sub(r"::[a-z_ ]+(?:\(\d+(?:,\d+)?\))?$", "", normalized)
    normalized = normalized.strip().strip("()")
    if len(normalized) >= 2 and normalized[0] == normalized[-1] == "'":
        normalized = normalized[1:-1].replace("''", "'")
    return normalized


class GuardError(Exception):
    pass


def require_confirmation(value: str | None, backup_value: str | None) -> None:
    if value != CONFIRMATION or backup_value != BACKUP_CONFIRMATION:
        raise GuardError("Migration or verified-backup confirmation rejected; no database connection was opened.")


def validate_dispatch_request(operation: str | None, preflight_value: str | None, migration_value: str | None, backup_value: str | None) -> None:
    if operation == "preflight":
        if preflight_value != PREFLIGHT_CONFIRMATION:
            raise GuardError("Preflight confirmation rejected; no database connection was opened.")
        return
    if operation == "migrate":
        require_confirmation(migration_value, backup_value)
        return
    raise GuardError("Unsupported operation; no database connection was opened.")


def required_settings(environ=os.environ) -> tuple[str, str, str, str]:
    from psycopg.conninfo import conninfo_to_dict
    url = environ.get("DATABASE_URL", "")
    database = environ.get("EXPECTED_DATABASE_NAME", "")
    schema = environ.get("EXPECTED_DATABASE_SCHEMA", "")
    expected_host = environ.get("EXPECTED_DATABASE_HOST", "")
    if not url or not database or not schema or not expected_host:
        raise GuardError(
            "Missing required protected settings: database URL, expected database name, schema, and host."
        )
    if not url.startswith(("postgresql://", "postgres://")):
        raise GuardError("Only PostgreSQL connection URLs are accepted.")
    try:
        values = conninfo_to_dict(url)
    except Exception:
        raise GuardError("Database URL could not be parsed; details suppressed.") from None
    if values.get("dbname") != database:
        raise GuardError("Configured database URL does not match the expected database name.")
    if values.get("host", "").lower() != expected_host.lower():
        raise GuardError("Configured database URL does not match the expected database host.")
    # TLS URL values are always overwritten below; never rely on caller-provided settings.
    return url, database, schema, expected_host


def classify_tables(existing: set[str]) -> str:
    present = EXPECTED_TABLES & existing
    if not present:
        return "new"
    if present == EXPECTED_TABLES:
        return "existing"
    raise GuardError("Some but not all customer email tables exist; ambiguous partial migration state.")


def validate_identity(database: str, schema: str, expected_database: str, expected_schema: str) -> None:
    if database != expected_database or schema != expected_schema:
        raise GuardError("Connected database/schema identity does not match the configured expected identity.")


def _engine_url(url: str) -> str:
    # SQLAlchemy uses psycopg 3; TLS options are passed separately and cannot be weakened by URL params.
    from sqlalchemy.engine import make_url
    try:
        parsed = make_url(url)
    except Exception:
        raise GuardError("Database URL could not be prepared for verified TLS; details suppressed.") from None
    query = {k: v for k, v in parsed.query.items() if k.lower() not in {"sslmode", "sslrootcert"}}
    query.update({"sslmode": "verify-full", "sslrootcert": "system"})
    parsed = parsed.set(
        drivername="postgresql+psycopg",
        query=query,
    )
    return parsed.render_as_string(hide_password=False)


def validate_schema(connection, schema: str, *, allow_approved_missing: bool) -> list[str]:
    """Compare every expected customer/email object with application metadata."""
    from sqlalchemy import inspect
    from app import customer_email_schema_tables

    inspector = inspect(connection)
    # SQLite is used only by isolated unit tests; production is required to be PostgreSQL.
    schema_arg = schema if connection.dialect.name == "postgresql" else None
    tables = set(inspector.get_table_names(schema=schema_arg))
    base_missing = []
    for table, required_columns in BASE_COLUMNS.items():
        if table not in tables:
            base_missing.append(table)
            continue
        columns = {row["name"] for row in inspector.get_columns(table, schema=schema_arg)}
        if not required_columns <= columns:
            raise GuardError(f"Required parent table {table} lacks required ID column.")
        id_column = next(row for row in inspector.get_columns(table, schema=schema_arg) if row["name"] == "id")
        if not isinstance(id_column["type"].python_type, type) or id_column["type"].python_type is not int:
            raise GuardError(f"Required parent table {table} has an incompatible ID type.")
        if tuple(inspector.get_pk_constraint(table, schema=schema_arg).get("constrained_columns") or ()) != ("id",):
            raise GuardError(f"Required parent table {table} has an incompatible ID constraint.")
    if base_missing:
        raise GuardError("Required parent tables are missing: " + ", ".join(sorted(base_missing)))

    column_manifest = migration_column_manifest()
    index_manifest = migration_index_manifest()
    unique_manifest, named_unique_manifest, foreign_key_manifest = migration_constraint_manifest()
    mode = classify_tables(tables)
    if mode == "new":
        return ["Preflight: new customer email schema; all required parent tables and IDs are present."]

    problems: list[str] = []
    for table in customer_email_schema_tables():
        name = table.name
        actual_columns = {row["name"]: row for row in inspector.get_columns(name, schema=schema_arg)}
        expected_columns = {column for table_name, column in column_manifest if table_name == name}
        if set(actual_columns) - expected_columns:
            raise GuardError(f"Unexpected columns detected in {name}.")
        allowed_missing = {column for table_name, column in APPROVED_MISSING_COLUMNS if table_name == name}
        missing = validate_missing_state(
            name, expected_columns, set(actual_columns), allowed_missing,
            allow_approved_missing, "columns",
        )
        for col_name in sorted(expected_columns):
            actual = actual_columns.get(col_name)
            if actual is None:
                if allow_approved_missing and col_name in missing:
                    problems.append(f"approved missing column: {name}.{col_name}")
                    continue
                raise GuardError(f"Unexpected missing column: {name}.{col_name}.")
            if connection.dialect.name == "postgresql":
                actual_type = actual["type"].compile(dialect=connection.dialect)
                validate_column_snapshot(
                    name, col_name, actual_type, actual["nullable"], actual.get("default"),
                    column_manifest[(name, col_name)],
                )
            elif actual["type"].python_type is not table.columns[col_name].type.python_type:
                raise GuardError(f"Incompatible test-database type for {name}.{col_name}.")

        expected_indexes = {
            index_name: (columns, unique)
            for index_name, (index_table, columns, unique) in index_manifest.items()
            if index_table == name
        }
        inspected_indexes = inspector.get_indexes(name, schema=schema_arg)
        actual_indexes = {index["name"]: tuple(index.get("column_names") or ()) for index in inspected_indexes}
        missing_indexes = validate_missing_state(
            name, set(expected_indexes), set(actual_indexes),
            APPROVED_MISSING_INDEXES & set(expected_indexes),
            allow_approved_missing, "indexes",
        )
        # Policy: unrelated additional indexes are allowed. Every index defined
        # by the migration is still checked exactly; unique extras are caught by
        # the exact unique-constraint comparison below.
        pg_index_metadata = (
            postgres_index_catalog(connection, schema, name, expected_indexes)
            if connection.dialect.name == "postgresql" and expected_indexes else {}
        )
        for index_name, (columns, unique) in expected_indexes.items():
            if index_name not in actual_indexes:
                if index_name in missing_indexes:
                    problems.append(f"approved missing index: {index_name}")
                    continue
                raise GuardError(f"Unexpected missing index: {index_name}.")
            if connection.dialect.name == "postgresql":
                validate_postgres_index_snapshot(
                    index_name, name, columns, unique, pg_index_metadata.get(index_name)
                )
            elif actual_indexes[index_name] != columns:
                raise GuardError(f"Incompatible index definition: {index_name}.")
        expected_unique = unique_manifest.get(name, set())
        actual_unique = {
            tuple(row.get("column_names") or ())
            for row in inspector.get_unique_constraints(name, schema=schema_arg)
        }
        actual_unique.update(
            tuple(row.get("column_names") or ()) for row in inspector.get_indexes(name, schema=schema_arg) if row.get("unique")
        )
        if expected_unique != actual_unique:
            raise GuardError(f"Missing or incompatible unique constraint on {name}.")
        actual_named_unique = {
            row.get("name"): tuple(row.get("column_names") or ())
            for row in inspector.get_unique_constraints(name, schema=schema_arg)
        }
        for unique_constraint in inspector.get_unique_constraints(name, schema=schema_arg):
            unique_options = unique_constraint.get("dialect_options") or {}
            if (
                unique_options.get("deferrable")
                or unique_options.get("initially") not in {None, "IMMEDIATE", "immediate"}
                or unique_options.get("postgresql_nulls_not_distinct")
            ):
                raise GuardError(f"Incompatible unique-constraint timing on {name}.")
        if inspector.get_check_constraints(name, schema=schema_arg):
            raise GuardError(f"Unexpected check constraint on {name}.")
        for constraint_name, columns in named_unique_manifest.get(name, {}).items():
            if actual_named_unique.get(constraint_name) != columns:
                raise GuardError(f"Missing or incompatible named unique constraint on {name}.")
        expected_fk = foreign_key_manifest.get(name, set())
        foreign_keys = inspector.get_foreign_keys(name, schema=schema_arg)
        for foreign_key in foreign_keys:
            validate_fk_options(foreign_key.get("options"))
        actual_fk = {
            (
                local, row.get("referred_table"), remote,
                row.get("referred_schema") or schema,
                (row.get("options") or {}).get("ondelete"),
                (row.get("options") or {}).get("onupdate"),
            )
            for row in foreign_keys
            for local, remote in zip(row.get("constrained_columns") or (), row.get("referred_columns") or ())
        }
        expected_fk_with_schema = {
            (local, target_table, target_col, schema, None, None)
            for local, target_table, target_col in expected_fk
        }
        if expected_fk_with_schema != actual_fk:
            raise GuardError(f"Missing or incompatible foreign key on {name}.")
        expected_pk = tuple(col.name for col in table.primary_key.columns)
        primary_key = inspector.get_pk_constraint(name, schema=schema_arg)
        primary_key_options = primary_key.get("dialect_options") or {}
        if primary_key_options.get("deferrable") or primary_key_options.get("initially") not in {None, "IMMEDIATE", "immediate"}:
            raise GuardError(f"Incompatible primary-key timing on {name}.")
        actual_pk = tuple(primary_key.get("constrained_columns") or ())
        if actual_pk != expected_pk:
            raise GuardError(f"Missing or incompatible primary key on {name}.")

    return ["Preflight: customer email schema is complete or has only approved additive omissions."] + problems


def write_summary(lines: list[str]) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    with open(summary_path, "a", encoding="utf-8") as summary:
        summary.write("## Customer email schema migration guard\n\n")
        summary.write("\n".join(f"- {line}" for line in lines) + "\n")


def safe_failure(stage: str, error: Exception) -> GuardError:
    """Expose only the operation and exception class, never its message or URL."""
    return GuardError(f"{stage} failed ({type(error).__name__}); diagnostic details were suppressed.")


def _connect() -> object:
    from sqlalchemy import create_engine
    url, expected_database, expected_schema, _expected_host = required_settings()
    engine = None
    try:
        engine = create_engine(
            _engine_url(url),
            connect_args={
                "sslmode": "verify-full",
                "sslrootcert": "system",
                "options": "-c default_transaction_read_only=on",
            },
            hide_parameters=True,
            pool_pre_ping=True,
        )
        connection = engine.connect()
        row = connection.exec_driver_sql("SELECT current_database(), current_schema()").one()
        validate_identity(row[0], row[1], expected_database, expected_schema)
        return connection
    except Exception:
        if engine is not None:
            engine.dispose()
        raise


def export_verified_migration_environment() -> None:
    """Write a strict-TLS URL to GitHub's private per-job environment file."""
    from sqlalchemy.engine import make_url

    url, _database, _schema, _host = required_settings()
    github_env = Path(os.environ.get("GITHUB_ENV", ""))
    if not github_env.is_file():
        raise GuardError("GitHub runner environment files are unavailable.")
    try:
        parsed = make_url(url)
        query = {key: value for key, value in parsed.query.items() if key.lower() not in {"sslmode", "sslrootcert"}}
        query.update({"sslmode": "verify-full", "sslrootcert": "system"})
        verified_url = parsed.set(drivername="postgresql+psycopg", query=query).render_as_string(hide_password=False)
        if "\n" in verified_url or "\r" in verified_url:
            raise GuardError("Database URL format is invalid; details suppressed.")
        with github_env.open("a", encoding="utf-8", newline="\n") as output:
            output.write(f"DATABASE_URL={verified_url}\n")
            output.write("PGSSLMODE=verify-full\n")
            output.write("PGSSLROOTCERT=system\n")
    except GuardError:
        raise
    except Exception:
        raise GuardError("Verified TLS environment could not be prepared; details suppressed.") from None
    print("Migration environment prepared with verified TLS; no connection opened.")
    write_summary(["Migration environment prepared with verify-full TLS."])


def run(mode: str) -> None:
    if mode == "confirm":
        require_confirmation(
            os.environ.get("CONFIRMATION_INPUT"),
            os.environ.get("BACKUP_CONFIRMATION_INPUT"),
        )
        print("Migration and verified-backup confirmations accepted; no database connection opened.")
        write_summary(["Migration confirmation passed.", "Verified-backup confirmation passed.", "No database connection opened."])
        return
    if mode == "dispatch-check":
        validate_dispatch_request(
            os.environ.get("OPERATION"),
            os.environ.get("PREFLIGHT_CONFIRMATION_INPUT"),
            os.environ.get("CONFIRMATION_INPUT"),
            os.environ.get("BACKUP_CONFIRMATION_INPUT"),
        )
        print("Requested operation confirmation passed; no database connection opened.")
        write_summary([f"{os.environ.get('OPERATION')} operation confirmation passed.", "No database connection opened."])
        return
    try:
        connection = _connect()
    except GuardError:
        raise
    except Exception as exc:
        raise safe_failure("Database connection/identity check", exc) from None

    try:
        schema = os.environ["EXPECTED_DATABASE_SCHEMA"]
        notes = validate_schema(connection, schema, allow_approved_missing=(mode == "preflight"))
        if mode == "postcheck" and any(note.startswith("approved missing") for note in notes):
            raise GuardError("Post-migration validation found approved objects still missing.")
        for note in notes:
            print(note)
        print("Schema validation passed.")
        write_summary(notes + ["Schema validation passed."])
    except GuardError:
        raise
    except Exception as exc:
        raise safe_failure("Database catalog/schema inspection", exc) from None
    finally:
        if connection is not None:
            engine = getattr(connection, "engine", None)
            connection.close()
            if engine is not None:
                engine.dispose()


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2 or sys.argv[1] not in {"confirm", "dispatch-check", "settings", "preflight", "postcheck", "tls-env"}:
            raise GuardError("Usage: customer_email_schema_guard.py confirm|dispatch-check|settings|preflight|postcheck|tls-env")
        if sys.argv[1] == "settings":
            required_settings()
            print("Connection settings are present; TLS policy is verify-full. No database connection opened.")
            write_summary(["Required identity and CA settings are present.", "TLS policy is verify-full.", "No database connection opened."])
        else:
            if sys.argv[1] == "tls-env":
                export_verified_migration_environment()
            else:
                run(sys.argv[1])
    except GuardError as exc:
        print(f"Guard failed: {exc}", file=sys.stderr)
        write_summary([f"Guard failed: {exc}"])
        sys.exit(1)
    except Exception:
        print("Guard failed; diagnostic details suppressed.", file=sys.stderr)
        write_summary(["Guard failed; diagnostic details suppressed."])
        sys.exit(1)
