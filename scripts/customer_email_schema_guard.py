"""Fail-closed guard for the manually triggered customer email migration."""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

CONFIRMATION = "MIGRATE CUSTOMER EMAIL SCHEMA"
BACKUP_CONFIRMATION = "I HAVE A VERIFIED RESTORABLE BACKUP"
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
SQL_UNIQUE_CONSTRAINTS = {
    "customer_account": {("email",), ("google_sub",)},
    "customer_token": {("token_hash",)},
    "customer_order_link": {("order_id",)},
    "email_message": {("idempotency_key",)},
    "email_campaign_recipient": {("campaign_id", "account_id")},
    "customer_cart": {("account_id",)},
    "marketing_campaign_run": {("campaign_key",)},
    "marketing_product_exposure": {("account_id", "product_id")},
}
SQL_NAMED_UNIQUE_CONSTRAINTS = {
    "email_campaign_recipient": {"uq_campaign_recipient_account": ("campaign_id", "account_id")},
    "marketing_product_exposure": {"uq_marketing_exposure_account_product": ("account_id", "product_id")},
}


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
    return result


def migration_sql() -> str:
    sql_path = Path(__file__).resolve().parent.parent / "migrations" / "20261017_customer_accounts_email.sql"
    return sql_path.read_text(encoding="utf-8")


def migration_column_defaults() -> dict[tuple[str, str], str | None]:
    sql = migration_sql()
    defaults: dict[tuple[str, str], str | None] = {}
    table_pattern = re.compile(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(\w+)\s*\((.*?)\)\s*;", re.I | re.S)
    default_pattern = re.compile(r"\bDEFAULT\s+('(?:''|[^'])*'|CURRENT_TIMESTAMP|TRUE|FALSE|-?\d+)", re.I)
    for table_match in table_pattern.finditer(sql):
        table, body = table_match.groups()
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
        for part in parts:
            if not part or part.upper().startswith(("CONSTRAINT ", "PRIMARY KEY", "UNIQUE ", "FOREIGN KEY")):
                continue
            tokens = part.split(None, 1)
            if len(tokens) != 2:
                continue
            column = tokens[0].strip('"').lower()
            if re.match(r"SERIAL\b", tokens[1], re.I):
                defaults[(table.lower(), column)] = "__serial__"
            else:
                match = default_pattern.search(tokens[1])
                defaults[(table.lower(), column)] = match.group(1) if match else None
    for table, column, statement in re.findall(
        r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+(\w+)([^;]*);", sql, re.I
    ):
        match = default_pattern.search(statement)
        defaults[(table.lower(), column.lower())] = match.group(1) if match else None
    return defaults


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


def required_settings(environ=os.environ) -> tuple[str, str, str, str, str]:
    from psycopg.conninfo import conninfo_to_dict
    url = environ.get("DATABASE_URL", "")
    cert = environ.get("DATABASE_CA_CERT", "")
    database = environ.get("EXPECTED_DATABASE_NAME", "")
    schema = environ.get("EXPECTED_DATABASE_SCHEMA", "")
    expected_host = environ.get("EXPECTED_DATABASE_HOST", "")
    if not url or not cert or not database or not schema or not expected_host:
        raise GuardError(
            "Missing required protected settings: database URL, CA certificate, expected database name, schema, and host."
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
    # Ignore URL TLS flags here; psycopg connect_args below unconditionally forces verify-full.
    return url, cert, database, schema, expected_host


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
    parsed = parsed.set(
        drivername="postgresql+psycopg",
        query={k: v for k, v in parsed.query.items() if k.lower() not in {"sslmode", "sslrootcert"}},
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

    mode = classify_tables(tables)
    if mode == "new":
        return ["Preflight: new customer email schema; all required parent tables and IDs are present."]

    problems: list[str] = []
    expected_defaults = migration_column_defaults()
    for table in customer_email_schema_tables():
        name = table.name
        actual_columns = {row["name"]: row for row in inspector.get_columns(name, schema=schema_arg)}
        expected_columns = {column.name: column for column in table.columns}
        unexpected = set(actual_columns) - set(expected_columns)
        if unexpected:
            raise GuardError(f"Unexpected columns detected in {name}.")
        for col_name, expected in expected_columns.items():
            actual = actual_columns.get(col_name)
            if actual is None:
                if allow_approved_missing and (name, col_name) in APPROVED_MISSING_COLUMNS:
                    problems.append(f"approved missing column: {name}.{col_name}")
                    continue
                raise GuardError(f"Unexpected missing column: {name}.{col_name}.")
            if actual["type"].python_type is not expected.type.python_type:
                raise GuardError(f"Incompatible type for {name}.{col_name}.")
            if bool(actual["nullable"]) != bool(expected.nullable):
                raise GuardError(f"Incompatible nullability for {name}.{col_name}.")
            if connection.dialect.name == "postgresql":
                expected_default = expected_defaults.get((name, col_name))
                actual_default = actual.get("default")
                default_matches = (
                    bool(actual_default and "nextval(" in actual_default.lower())
                    if expected_default == "__serial__"
                    else normalize_default(actual_default) == normalize_default(expected_default)
                )
                if not default_matches:
                    raise GuardError(f"Incompatible default for {name}.{col_name}.")

        expected_indexes = {
            index_name: (columns, unique)
            for index_name, (index_table, columns, unique) in migration_index_manifest().items()
            if index_table == name
        }
        actual_indexes = {index["name"]: tuple(index.get("column_names") or ()) for index in inspector.get_indexes(name, schema=schema_arg)}
        actual_indexes_meta = {index["name"]: index for index in inspector.get_indexes(name, schema=schema_arg)}
        for index_name, (columns, unique) in expected_indexes.items():
            if index_name not in actual_indexes:
                if allow_approved_missing and index_name in APPROVED_MISSING_INDEXES:
                    problems.append(f"approved missing index: {index_name}")
                    continue
                raise GuardError(f"Unexpected missing index: {index_name}.")
            if actual_indexes[index_name] != columns or (
                connection.dialect.name == "postgresql"
                and bool(actual_indexes_meta[index_name].get("unique")) != unique
            ):
                raise GuardError(f"Incompatible index definition: {index_name}.")
        expected_unique = SQL_UNIQUE_CONSTRAINTS.get(name, set())
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
        for constraint_name, columns in SQL_NAMED_UNIQUE_CONSTRAINTS.get(name, {}).items():
            if actual_named_unique.get(constraint_name) != columns:
                raise GuardError(f"Missing or incompatible named unique constraint on {name}.")
        expected_fk = {
            (fk.parent.name, fk.column.table.name, fk.column.name)
            for fk in table.foreign_keys
        }
        actual_fk = {
            (
                local, row.get("referred_table"), remote,
                row.get("referred_schema") or schema,
                (row.get("options") or {}).get("ondelete"),
                (row.get("options") or {}).get("onupdate"),
            )
            for row in inspector.get_foreign_keys(name, schema=schema_arg)
            for local, remote in zip(row.get("constrained_columns") or (), row.get("referred_columns") or ())
        }
        expected_fk_with_schema = {
            (local, target_table, target_col, schema, None, None)
            for local, target_table, target_col in expected_fk
        }
        if expected_fk_with_schema != actual_fk:
            raise GuardError(f"Missing or incompatible foreign key on {name}.")
        expected_pk = tuple(col.name for col in table.primary_key.columns)
        actual_pk = tuple(inspector.get_pk_constraint(name, schema=schema_arg).get("constrained_columns") or ())
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


def _connect() -> tuple[object, tempfile.TemporaryDirectory]:
    from sqlalchemy import create_engine
    url, cert, expected_database, expected_schema, _expected_host = required_settings()
    tempdir = tempfile.TemporaryDirectory(prefix="fity-pg-ca-")
    cert_path = Path(tempdir.name) / "ca.pem"
    cert_path.write_text(cert, encoding="utf-8")
    os.chmod(cert_path, 0o600)
    engine = None
    try:
        engine = create_engine(
            _engine_url(url),
            connect_args={"sslmode": "verify-full", "sslrootcert": str(cert_path)},
            hide_parameters=True,
            pool_pre_ping=True,
        )
        connection = engine.connect()
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        row = connection.exec_driver_sql("SELECT current_database(), current_schema()").one()
        validate_identity(row[0], row[1], expected_database, expected_schema)
        return connection, tempdir
    except Exception:
        if engine is not None:
            engine.dispose()
        tempdir.cleanup()
        raise


def export_verified_migration_environment() -> None:
    """Write a strict-TLS URL to GitHub's private per-job environment file."""
    from sqlalchemy.engine import make_url

    url, cert, _database, _schema, _host = required_settings()
    runner_temp = Path(os.environ.get("RUNNER_TEMP", ""))
    github_env = Path(os.environ.get("GITHUB_ENV", ""))
    if not runner_temp.is_dir() or not github_env.is_file():
        raise GuardError("GitHub runner environment files are unavailable.")
    cert_path = runner_temp / "fity-postgres-ca.pem"
    cert_path.write_text(cert, encoding="utf-8")
    os.chmod(cert_path, 0o600)
    parsed = make_url(url)
    query = {key: value for key, value in parsed.query.items() if key.lower() not in {"sslmode", "sslrootcert"}}
    query.update({"sslmode": "verify-full", "sslrootcert": str(cert_path)})
    verified_url = parsed.set(drivername="postgresql+psycopg", query=query).render_as_string(hide_password=False)
    if "\n" in verified_url or "\r" in verified_url:
        cert_path.unlink(missing_ok=True)
        raise GuardError("Database URL format is invalid; details suppressed.")
    with github_env.open("a", encoding="utf-8", newline="\n") as output:
        output.write(f"DATABASE_URL={verified_url}\n")
        output.write("PGSSLMODE=verify-full\n")
        output.write(f"PGSSLROOTCERT={cert_path}\n")
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
    connection = None
    tempdir = None
    try:
        connection, tempdir = _connect()
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
    except Exception:
        # Do not display driver exceptions: they can contain connection data.
        raise GuardError("Database/schema validation failed; diagnostic details were suppressed.") from None
    finally:
        if connection is not None:
            engine = getattr(connection, "engine", None)
            connection.close()
            if engine is not None:
                engine.dispose()
        if tempdir is not None:
            tempdir.cleanup()


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2 or sys.argv[1] not in {"confirm", "settings", "preflight", "postcheck", "tls-env"}:
            raise GuardError("Usage: customer_email_schema_guard.py confirm|settings|preflight|postcheck|tls-env")
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
