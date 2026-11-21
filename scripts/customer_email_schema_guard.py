"""Fail-closed guard for the manually triggered customer email migration."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from dataclasses import dataclass

from app.services.sql_schema_utils import normalize_sql_definition

CONFIRMATION = "MIGRATE CUSTOMER EMAIL SCHEMA"
BACKUP_CONFIRMATION = "I HAVE A VERIFIED RESTORABLE BACKUP"
PREFLIGHT_CONFIRMATION = "PREFLIGHT CUSTOMER EMAIL SCHEMA"
SYSTEM_CA_BUNDLE = Path("/etc/ssl/certs/ca-certificates.crt")
EXPECTED_TABLES = {
    "customer_account", "customer_token", "customer_consent_event",
    "customer_order_link", "auth_rate_limit", "email_campaign",
    "email_message", "email_campaign_recipient", "customer_cart",
    "marketing_automation_settings", "marketing_campaign_run",
    "marketing_product_exposure",
    "email_template", "email_draft",
    "email_delivery_attempt", "email_delivery_investigation",
    "email_delivery_investigation_event", "email_worker_run",
    "email_legacy_payload_review_event",
}
EMAIL_STUDIO_TABLES = {"email_template", "email_draft"}
EMAIL_DELIVERY_OPS_TABLES = {
    "email_delivery_attempt", "email_delivery_investigation",
    "email_delivery_investigation_event", "email_worker_run",
}
EMAIL_LEGACY_REVIEW_TABLES = {"email_legacy_payload_review_event"}
PRE_EMAIL_STUDIO_TABLES = EXPECTED_TABLES - EMAIL_STUDIO_TABLES - EMAIL_DELIVERY_OPS_TABLES - EMAIL_LEGACY_REVIEW_TABLES
HISTORICAL_REQUIRED_TABLES = PRE_EMAIL_STUDIO_TABLES | EMAIL_STUDIO_TABLES
HISTORICAL_APPROVED_MISSING_COLUMNS = {
    ("customer_account", "two_factor_enabled"),
    ("email_message", "retry_count"),
    ("email_message", "provider_message_id"),
    ("email_template", "name"),
    ("email_template", "blocks_json"),
    ("email_draft", "blocks_json"),
    ("email_message", "template_id"),
}
HISTORICAL_APPROVED_MISSING_INDEXES = {
    "ix_email_message_provider_message_id",
    "ix_email_message_template_id",
}
APPROVED_MISSING_TABLES = EMAIL_STUDIO_TABLES | EMAIL_DELIVERY_OPS_TABLES | {"email_legacy_payload_review_event"}
BASE_COLUMNS = {"admin_user": {"id"}, "order": {"id"}, "product": {"id"}}
# These are the only missing objects approved for an existing feature schema.
APPROVED_MISSING_COLUMNS = {
    ("customer_account", "two_factor_enabled"),
    ("email_message", "retry_count"),
    ("email_message", "provider_message_id"),
    ("email_template", "name"),
    ("email_template", "blocks_json"),
    ("email_draft", "blocks_json"),
    ("email_message", "template_id"),
    ("email_message", "purpose"),
    ("email_message", "frequency_override"),
    ("email_message", "html_body"),
    ("email_message", "text_body"),
    ("email_message", "claimed_at"),
    ("email_message", "last_attempt_at"),
    ("email_message", "next_attempt_at"),
    ("email_message", "error_category"),
    ("email_message", "claim_token"),
    ("marketing_campaign_run", "queued_count"),
    ("email_campaign", "campaign_type"),
    ("email_campaign", "product_ids_json"),
    ("email_campaign", "scheduled_for"),
    ("email_campaign", "previewed_at"),
    ("email_campaign", "approved_at"),
    ("email_campaign", "generation_key"),
    ("email_campaign", "generation_source"),
    ("email_campaign", "generation_reason"),
    ("email_campaign", "generation_reference"),
    ("email_campaign_recipient", "exclusion_reason"),
    ("email_campaign_recipient", "message_id"),
}
APPROVED_MISSING_INDEXES = {
    "ix_email_message_provider_message_id", "ix_email_message_template_id",
    "ix_email_message_purpose", "ix_email_message_dispatch",
    "ix_email_message_claimed_at", "ix_email_message_next_attempt_at",
    "ix_email_message_claim_token",
    "ix_email_campaign_status_scheduled",
    "ix_email_campaign_recipient_message",
    "uq_email_campaign_generation_key",
}
def migration_index_manifest() -> dict[str, tuple[str, tuple[str, ...], bool, str | None]]:
    sql = migration_sql()
    pattern = re.compile(
        r"CREATE\s+(UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+([a-zA-Z_][a-zA-Z0-9_]*)\s+"
        r"ON\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(([^)]+)\)(?:\s+WHERE\s+([^;]+))?",
        re.IGNORECASE | re.DOTALL,
    )
    result = {}
    for match in pattern.finditer(sql):
        unique, name, table, columns, predicate = match.groups()
        result[name] = (
            table.lower(),
            tuple(part.strip().strip('"').lower() for part in columns.split(",")),
            bool(unique),
            predicate.strip() if predicate else None,
        )
    if not result:
        raise GuardError("Migration index definitions could not be verified.")
    statement_count = len(re.findall(r"CREATE\s+(?:UNIQUE\s+)?INDEX\b", sql, re.I))
    if statement_count != len(result):
        raise GuardError("One or more migration index definitions could not be parsed.")
    return result


def migration_sql() -> str:
    migrations_dir = Path(__file__).resolve().parent.parent / "migrations"
    customer_email = (migrations_dir / "20261017_customer_accounts_email.sql").read_text(encoding="utf-8")
    email_studio = (migrations_dir / "20261027_email_studio.sql").read_text(encoding="utf-8")
    email_builder = (migrations_dir / "20261028_email_builder.sql").read_text(encoding="utf-8")
    consent_wording = (migrations_dir / "20261029_consent_wording_version.sql").read_text(encoding="utf-8")
    outbox = (migrations_dir / "20261113_email_outbox.sql").read_text(encoding="utf-8")
    delivery_ops = (migrations_dir / "20261116_email_delivery_ops.sql").read_text(encoding="utf-8")
    legacy_review = (migrations_dir / "20261117_email_legacy_review.sql").read_text(encoding="utf-8")
    marketing_campaign = (migrations_dir / "20261120_marketing_campaign_queue.sql").read_text(encoding="utf-8")
    campaign_preparation = (migrations_dir / "20261121_marketing_campaign_draft_preparation.sql").read_text(encoding="utf-8")
    return f"{customer_email}\n{email_studio}\n{email_builder}\n{consent_wording}\n{outbox}\n{delivery_ops}\n{legacy_review}\n{marketing_campaign}\n{campaign_preparation}"


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
    type_changes = re.compile(
        r"ALTER\s+TABLE\s+(\w+)\s+ALTER\s+COLUMN\s+(\w+)\s+TYPE\s+(" + _SQL_TYPE + r")\s*;",
        re.I,
    )
    for table, column, sql_type in type_changes.findall(sql):
        key = (table.lower(), column.lower())
        if key not in manifest:
            raise GuardError("Migration type change references an unknown column.")
        previous = manifest[key]
        manifest[key] = ColumnSpec(canonical_sql_type(sql_type), previous.nullable, previous.default)
    defaults = re.compile(
        r"ALTER\s+TABLE\s+(\w+)\s+ALTER\s+COLUMN\s+(\w+)\s+SET\s+DEFAULT\s+('(?:''|[^'])*'|CURRENT_TIMESTAMP|TRUE|FALSE|-?\d+(?:\.\d+)?)\s*;",
        re.I,
    )
    for table, column, default in defaults.findall(sql):
        key = (table.lower(), column.lower())
        if key not in manifest:
            raise GuardError("Migration default references an unknown column.")
        previous = manifest[key]
        manifest[key] = ColumnSpec(previous.sql_type, previous.nullable, default)
    not_null = re.compile(r"ALTER\s+TABLE\s+(\w+)\s+ALTER\s+COLUMN\s+(\w+)\s+SET\s+NOT\s+NULL\s*;", re.I)
    for table, column in not_null.findall(sql):
        key = (table.lower(), column.lower())
        if key not in manifest:
            raise GuardError("Migration nullability change references an unknown column.")
        previous = manifest[key]
        manifest[key] = ColumnSpec(previous.sql_type, False, previous.default)
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
            table_reference = re.match(
                r"(?:CONSTRAINT\s+\w+\s+)?FOREIGN\s+KEY\s*\(([^)]+)\)\s*"
                r"REFERENCES\s+\"?([A-Za-z_][A-Za-z0-9_]*)\"?\s*\(([^)]+)\)",
                declaration, re.I,
            )
            if table_reference:
                local_columns, target_table, target_columns = table_reference.groups()
                locals_ = [part.strip().strip('"').lower() for part in local_columns.split(",")]
                targets_ = [part.strip().strip('"').lower() for part in target_columns.split(",")]
                if len(locals_) != len(targets_):
                    raise GuardError("Migration foreign-key column count is inconsistent.")
                foreign_keys[table].update((local, target_table.lower(), target) for local, target in zip(locals_, targets_))
    alters = re.compile(r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+(.*?);", re.I | re.S)
    for table, declaration in alters.findall(sql):
        parsed = _COLUMN_DECLARATION.match(declaration.strip())
        if not parsed:
            continue
        column, _sql_type, options = parsed.groups()
        reference = re.search(r"\bREFERENCES\s+\"?([A-Za-z_][A-Za-z0-9_]*)\"?\s*\(\s*\"?([A-Za-z_][A-Za-z0-9_]*)\"?\s*\)", options, re.I)
        if reference:
            target_table, target_column = reference.groups()
            foreign_keys[table.lower()].add((column.lower(), target_table.lower(), target_column.lower()))
    if set(unique) != EXPECTED_TABLES or len(foreign_keys) != len(EXPECTED_TABLES):
        raise GuardError("Migration constraint definitions could not be verified.")
    if re.search(r"ALTER\s+TABLE\s+email_template\s+DROP\s+CONSTRAINT\s+IF\s+EXISTS\s+email_template_category_key", sql, re.I):
        unique["email_template"].discard(("category",))
    # This unique partial index is declared in the additive operations schema.
    unique["email_delivery_investigation"].add(("message_id",))
    # Generated draft keys are unique only when present; the exact partial
    # predicate is validated independently through the index manifest.
    unique["email_campaign"].add(("generation_key",))
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


def validate_sqlite_column_snapshot(
    table, column, actual_type, actual_nullable, actual_default, expected, *, primary_key
):
    actual_type = canonical_sql_type(actual_type)
    if actual_type == "DATETIME":
        actual_type = "TIMESTAMP WITHOUT TIME ZONE"
    if actual_type != expected.sql_type:
        raise GuardError(f"Incompatible test-database type for {table}.{column}.")
    if bool(actual_nullable) != expected.nullable:
        raise GuardError(f"Incompatible test-database nullability for {table}.{column}.")
    if expected.default == "__serial__":
        default_matches = primary_key and actual_type == "INTEGER"
    else:
        default_matches = normalize_default(actual_default) == normalize_default(expected.default)
    if not default_matches:
        raise GuardError(f"Incompatible test-database default for {table}.{column}.")


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


def validate_postgres_index_snapshot(
    index_name, expected_table, expected_columns, expected_unique, snapshot, *,
    expected_predicate=None, strict_definition=False,
):
    """Fail closed unless PostgreSQL catalog metadata matches a plain btree index."""
    if not isinstance(snapshot, dict):
        raise GuardError(f"PostgreSQL metadata unavailable for index {index_name}.")
    required = {
        "name", "table", "columns", "unique", "valid", "has_predicate",
        "included_columns", "access_method", "key_count", "total_count",
    }
    if strict_definition:
        required.update({
            "key_order", "collations_match", "operator_classes_default", "ready", "live", "predicate",
        })
    if not required <= snapshot.keys():
        raise GuardError(f"PostgreSQL metadata incomplete for index {index_name}.")
    if (
        snapshot["name"] != index_name
        or snapshot["table"] != expected_table
        or tuple(snapshot["columns"] or ()) != tuple(expected_columns)
        or bool(snapshot["unique"]) != bool(expected_unique)
        or snapshot["valid"] is not True
        or bool(snapshot["has_predicate"]) != (expected_predicate is not None)
        or tuple(snapshot["included_columns"] or ())
        or str(snapshot["access_method"]).lower() != "btree"
        or snapshot["key_count"] != len(expected_columns)
        or snapshot["total_count"] != len(expected_columns)
    ):
        raise GuardError(f"Incompatible PostgreSQL index definition: {index_name}.")
    if strict_definition and (
        tuple(snapshot["key_order"] or ()) != ("ASC NULLS LAST",) * len(expected_columns)
        or tuple(snapshot["collations_match"] or ()) != (True,) * len(expected_columns)
        or tuple(snapshot["operator_classes_default"] or ()) != (True,) * len(expected_columns)
        or normalize_sql_definition(snapshot["predicate"] or "") != normalize_sql_definition(expected_predicate or "")
        or snapshot["ready"] is not True
        or snapshot["live"] is not True
    ):
        raise GuardError(f"Incompatible PostgreSQL index definition: {index_name}.")


def postgres_foreign_key_catalog(connection, schema, table):
    """Read exact PostgreSQL foreign-key definitions and validation state."""
    from sqlalchemy import text

    if connection.dialect.name != "postgresql":
        raise GuardError("PostgreSQL foreign-key catalog requested for a non-PostgreSQL connection.")
    statement = text("""
        SELECT constraint_data.conname AS constraint_name,
               table_class.relname AS table_name,
               referenced_namespace.nspname AS referred_schema,
               referenced_class.relname AS referred_table,
               constraint_data.convalidated AS validated,
               constraint_data.condeferrable AS deferrable,
               constraint_data.condeferred AS initially_deferred,
               CASE constraint_data.confmatchtype
                   WHEN 's' THEN 'SIMPLE'
                   WHEN 'f' THEN 'FULL'
                   WHEN 'p' THEN 'PARTIAL'
                   ELSE 'UNKNOWN'
               END AS match_type,
               CASE constraint_data.confupdtype
                   WHEN 'a' THEN 'NO ACTION'
                   WHEN 'r' THEN 'RESTRICT'
                   WHEN 'c' THEN 'CASCADE'
                   WHEN 'n' THEN 'SET NULL'
                   WHEN 'd' THEN 'SET DEFAULT'
                   ELSE 'UNKNOWN'
               END AS on_update,
               CASE constraint_data.confdeltype
                   WHEN 'a' THEN 'NO ACTION'
                   WHEN 'r' THEN 'RESTRICT'
                   WHEN 'c' THEN 'CASCADE'
                   WHEN 'n' THEN 'SET NULL'
                   WHEN 'd' THEN 'SET DEFAULT'
                   ELSE 'UNKNOWN'
               END AS on_delete,
               ARRAY(
                   SELECT attribute.attname
                   FROM unnest(constraint_data.conkey) WITH ORDINALITY AS key_column(attnum, ordinal)
                   JOIN pg_attribute AS attribute
                     ON attribute.attrelid = table_class.oid
                    AND attribute.attnum = key_column.attnum
                   ORDER BY key_column.ordinal
               ) AS columns,
               ARRAY(
                   SELECT attribute.attname
                   FROM unnest(constraint_data.confkey) WITH ORDINALITY AS key_column(attnum, ordinal)
                   JOIN pg_attribute AS attribute
                     ON attribute.attrelid = referenced_class.oid
                    AND attribute.attnum = key_column.attnum
                   ORDER BY key_column.ordinal
               ) AS referred_columns
        FROM pg_class AS table_class
        JOIN pg_namespace AS table_namespace ON table_namespace.oid = table_class.relnamespace
        JOIN pg_constraint AS constraint_data ON constraint_data.conrelid = table_class.oid
        JOIN pg_class AS referenced_class ON referenced_class.oid = constraint_data.confrelid
        JOIN pg_namespace AS referenced_namespace ON referenced_namespace.oid = referenced_class.relnamespace
        WHERE constraint_data.contype = 'f'
          AND table_namespace.nspname = :schema
          AND table_class.relname = :table
    """)
    try:
        rows = connection.execute(statement, {"schema": schema, "table": table}).mappings()
        return [
            {
                "name": row["constraint_name"],
                "table": row["table_name"],
                "referred_schema": row["referred_schema"],
                "referred_table": row["referred_table"],
                "validated": row["validated"],
                "deferrable": row["deferrable"],
                "initially_deferred": row["initially_deferred"],
                "match_type": row["match_type"],
                "on_update": row["on_update"],
                "on_delete": row["on_delete"],
                "columns": tuple(row["columns"] or ()),
                "referred_columns": tuple(row["referred_columns"] or ()),
            }
            for row in rows
        ]
    except GuardError:
        raise
    except Exception as exc:
        raise GuardError(
            f"PostgreSQL foreign-key catalog inspection failed ({type(exc).__name__}); diagnostic details suppressed."
        ) from None


def validate_postgres_foreign_key_catalog(foreign_keys, catalog_rows, schema, table):
    """Require catalog constraints to exactly match inspected FKs and be validated."""
    if len(foreign_keys) != len(catalog_rows):
        raise GuardError("PostgreSQL foreign-key catalog is incomplete or contains unexpected constraints.")

    remaining = list(catalog_rows)
    for foreign_key in foreign_keys:
        options = foreign_key.get("options") or {}
        expected = {
            "name": foreign_key.get("name"),
            "table": table,
            "referred_schema": foreign_key.get("referred_schema") or schema,
            "referred_table": foreign_key.get("referred_table"),
            "columns": tuple(foreign_key.get("constrained_columns") or ()),
            "referred_columns": tuple(foreign_key.get("referred_columns") or ()),
            "match_type": (options.get("match") or "SIMPLE").upper(),
            "deferrable": bool(options.get("deferrable", False)),
            "initially_deferred": str(options.get("initially") or "").upper() == "DEFERRED",
            "on_update": (options.get("onupdate") or "NO ACTION").upper(),
            "on_delete": (options.get("ondelete") or "NO ACTION").upper(),
        }
        matches = [row for row in remaining if row.get("name") == expected["name"]]
        if len(matches) != 1:
            raise GuardError("PostgreSQL foreign-key catalog does not match inspected constraints.")
        actual = matches[0]
        remaining.remove(actual)
        if actual.get("validated") is not True:
            raise GuardError(f"PostgreSQL foreign key {expected['name']} is not validated.")
        if any(actual.get(key) != value for key, value in expected.items() if key != "name"):
            raise GuardError(f"Incompatible PostgreSQL foreign-key definition: {expected['name']}.")
    if remaining:
        raise GuardError("PostgreSQL foreign-key catalog contains unexpected constraints.")


def postgres_index_catalog(connection, schema, table, index_names, *, strict_definition=False):
    """Read exact index properties from PostgreSQL catalogs; unsupported metadata fails closed."""
    from sqlalchemy import text

    if connection.dialect.name != "postgresql":
        raise GuardError("PostgreSQL index catalog requested for a non-PostgreSQL connection.")
    strict_fields = ""
    if strict_definition:
        strict_fields = """
               , index_data.indisready AS is_ready
               , index_data.indislive AS is_live
               , pg_get_expr(index_data.indpred, index_data.indrelid) AS predicate
               , ARRAY(
                   SELECT
                       CASE WHEN (index_data.indoption[key_column.ordinal - 1] & 1) = 1
                            THEN 'DESC' ELSE 'ASC' END
                       || CASE WHEN (index_data.indoption[key_column.ordinal - 1] & 2) = 2
                               THEN ' NULLS FIRST' ELSE ' NULLS LAST' END
                   FROM unnest(index_data.indkey) WITH ORDINALITY AS key_column(attnum, ordinal)
                   WHERE key_column.ordinal <= index_data.indnkeyatts
                   ORDER BY key_column.ordinal
               ) AS key_order
               , ARRAY(
                   SELECT index_data.indcollation[key_column.ordinal - 1] = attribute.attcollation
                   FROM unnest(index_data.indkey) WITH ORDINALITY AS key_column(attnum, ordinal)
                   JOIN pg_attribute AS attribute
                     ON attribute.attrelid = table_class.oid
                    AND attribute.attnum = key_column.attnum
                   WHERE key_column.ordinal <= index_data.indnkeyatts
                   ORDER BY key_column.ordinal
               ) AS collations_match
               , ARRAY(
                   SELECT operator_class.opcdefault
                   FROM unnest(index_data.indclass) WITH ORDINALITY AS class_column(class_oid, ordinal)
                   JOIN pg_opclass AS operator_class ON operator_class.oid = class_column.class_oid
                   WHERE class_column.ordinal <= index_data.indnkeyatts
                   ORDER BY class_column.ordinal
               ) AS operator_classes_default
        """
    statement = text(f"""
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
               {strict_fields}
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
        snapshots = {}
        for row in rows:
            snapshot = {
                "name": row["index_name"], "table": row["table_name"],
                "columns": tuple(row["key_columns"] or ()), "unique": row["is_unique"],
                "valid": row["is_valid"],
                "has_predicate": row["has_predicate"],
                "included_columns": tuple(row["included_columns"] or ()),
                "access_method": row["access_method"],
                "key_count": row["key_count"], "total_count": row["total_count"],
            }
            if strict_definition:
                snapshot.update({
                    "ready": row["is_ready"], "live": row["is_live"],
                    "predicate": row["predicate"],
                    "key_order": tuple(row["key_order"] or ()),
                    "collations_match": tuple(row["collations_match"] or ()),
                    "operator_classes_default": tuple(row["operator_classes_default"] or ()),
                })
            snapshots[row["index_name"]] = snapshot
        return snapshots
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


class IdentityCheckError(GuardError):
    """A connected database did not match the configured identity."""


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
    if PRE_EMAIL_STUDIO_TABLES <= present and (EXPECTED_TABLES - present) <= APPROVED_MISSING_TABLES:
        return "upgradable"
    raise GuardError("Some but not all customer email tables exist; ambiguous partial migration state.")


def validate_identity(database: str, schema: str, expected_database: str, expected_schema: str) -> None:
    if database != expected_database or schema != expected_schema:
        raise IdentityCheckError("Connected database/schema identity does not match the configured expected identity.")


def trusted_system_ca_bundle() -> Path:
    import ssl

    ca_file = SYSTEM_CA_BUNDLE
    if not ca_file.is_file():
        raise GuardError("The Ubuntu system PostgreSQL CA bundle is missing.")
    try:
        ssl.create_default_context(cafile=str(ca_file))
    except (OSError, ssl.SSLError, ValueError):
        raise GuardError("The Ubuntu system PostgreSQL CA bundle could not be loaded.") from None
    return ca_file


def _engine_url(url: str) -> str:
    # SQLAlchemy uses psycopg 3; caller URL TLS values cannot weaken verification.
    from sqlalchemy.engine import make_url
    ca_file = trusted_system_ca_bundle()
    try:
        parsed = make_url(url)
    except Exception:
        raise GuardError("Database URL could not be prepared for verified TLS; details suppressed.") from None
    query = {k: v for k, v in parsed.query.items() if k.lower() not in {"sslmode", "sslrootcert"}}
    query.update({"sslmode": "verify-full", "sslrootcert": str(ca_file)})
    parsed = parsed.set(
        drivername="postgresql+psycopg",
        query=query,
    )
    return parsed.render_as_string(hide_password=False)


def validate_schema(
    connection, schema: str, *, allow_approved_missing: bool, strict_postgres_catalog: bool = False
) -> list[str]:
    """Compare every expected customer/email object with application metadata."""
    from sqlalchemy import inspect, text
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
        if name not in tables:
            if name in APPROVED_MISSING_TABLES and allow_approved_missing:
                migration = (
                    "20261116_email_delivery_ops.sql" if name in EMAIL_DELIVERY_OPS_TABLES else
                    "20261117_email_legacy_review.sql" if name in EMAIL_LEGACY_REVIEW_TABLES else
                    "20261027_email_studio.sql"
                )
                problems.append(f"approved missing table: {name} (migration {migration})")
                continue
            raise GuardError(f"Unexpected missing table: {name}.")
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
            index_name: (columns, unique, predicate)
            for index_name, (index_table, columns, unique, predicate) in index_manifest.items()
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
            postgres_index_catalog(
                connection, schema, name, expected_indexes,
                strict_definition=strict_postgres_catalog,
            )
            if connection.dialect.name == "postgresql" and expected_indexes else {}
        )
        for index_name, (columns, unique, expected_predicate) in expected_indexes.items():
            if index_name not in actual_indexes:
                if index_name in missing_indexes:
                    problems.append(f"approved missing index: {index_name}")
                    continue
                raise GuardError(f"Unexpected missing index: {index_name}.")
            actual_index = next(index for index in inspected_indexes if index["name"] == index_name)
            dialect_options = actual_index.get("dialect_options") or {}
            actual_predicate = dialect_options.get("postgresql_where") or dialect_options.get("sqlite_where")
            if actual_predicate is None:
                actual_predicate = ""
            if normalize_sql_definition(str(actual_predicate)) != normalize_sql_definition(expected_predicate or ""):
                raise GuardError(f"Incompatible index predicate: {index_name}.")
            if connection.dialect.name == "postgresql":
                validate_postgres_index_snapshot(
                    index_name, name, columns, unique, pg_index_metadata.get(index_name),
                    expected_predicate=expected_predicate,
                    strict_definition=strict_postgres_catalog,
                )
            elif actual_indexes[index_name] != columns:
                raise GuardError(f"Incompatible index definition: {index_name}.")
        expected_unique = unique_manifest.get(name, set())
        for missing_index_name in missing_indexes:
            missing_index = expected_indexes.get(missing_index_name)
            if missing_index and missing_index[1]:
                expected_unique.discard(missing_index[0])
        actual_unique = {
            tuple(row.get("column_names") or ())
            for row in inspector.get_unique_constraints(name, schema=schema_arg)
        }
        # The expected unique-column manifest intentionally includes unique
        # partial indexes as well as table constraints; exact predicates are
        # validated against the index manifest above.
        for unique_index in inspector.get_indexes(name, schema=schema_arg):
            if unique_index.get("unique"):
                actual_unique.add(tuple(unique_index.get("column_names") or ()))
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
        # Operation-table CHECK constraints and trigger/index catalog details
        # are validated by validate_email_delivery_ops_schema below.
        if name not in EMAIL_DELIVERY_OPS_TABLES | EMAIL_LEGACY_REVIEW_TABLES and inspector.get_check_constraints(name, schema=schema_arg):
            raise GuardError(f"Unexpected check constraint on {name}.")
        for constraint_name, columns in named_unique_manifest.get(name, {}).items():
            if actual_named_unique.get(constraint_name) != columns:
                raise GuardError(f"Missing or incompatible named unique constraint on {name}.")
        expected_fk = foreign_key_manifest.get(name, set())
        foreign_keys = inspector.get_foreign_keys(name, schema=schema_arg)
        if connection.dialect.name == "postgresql" and strict_postgres_catalog:
            fk_catalog = postgres_foreign_key_catalog(connection, schema, name)
            validate_postgres_foreign_key_catalog(foreign_keys, fk_catalog, schema, name)
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
        if name in EMAIL_DELIVERY_OPS_TABLES | EMAIL_LEGACY_REVIEW_TABLES:
            expected_fk_with_schema = set()
            for foreign_key in table.foreign_key_constraints:
                for element in foreign_key.elements:
                    expected_fk_with_schema.add((
                        element.parent.name, foreign_key.referred_table.name,
                        element.column.name, schema,
                        foreign_key.ondelete.upper() if foreign_key.ondelete else None,
                        foreign_key.onupdate.upper() if foreign_key.onupdate else None,
                    ))
        else:
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

    if "email_message" in tables:
        history_exists = inspector.has_table("email_schema_migration", schema=schema_arg)
        if not history_exists:
            if allow_approved_missing:
                problems.append("approved missing migration history: email_schema_migration (20261113_email_outbox)")
            else:
                raise GuardError("Email outbox migration history is missing.")
        else:
            from app.services.email_outbox_schema import validate_email_outbox_schema
            try:
                validate_email_outbox_schema(connection, schema=schema)
            except Exception as exc:
                raise GuardError(f"Email outbox schema or migration checksum is incompatible ({type(exc).__name__}).") from None
            migration_ids = set(connection.execute(text(
                "SELECT migration_id FROM email_schema_migration"
            )).scalars())
            for migration_id, service_module, validator_name in (
                ("20261117_email_admin_test_purpose", "app.services.email_test_purpose_migration", "validate_email_test_purpose"),
                ("20261117_customer_consent_audit_immutable", "app.services.consent_audit", "validate_consent_audit_schema"),
                ("20261120_marketing_campaign_queue", "app.services.marketing_campaign_schema", "validate_marketing_campaign_schema"),
                ("20261121_marketing_campaign_draft_preparation", "app.services.marketing_campaign_preparation_schema", "validate_marketing_campaign_preparation_schema"),
            ):
                if migration_id in migration_ids:
                    try:
                        module = __import__(service_module, fromlist=[validator_name])
                        getattr(module, validator_name)(connection, **({"schema": schema} if validator_name == "validate_consent_audit_schema" else {}))
                    except Exception as exc:
                        raise GuardError(f"Customer email additive migration {migration_id} is incompatible ({type(exc).__name__}).") from None
                elif allow_approved_missing:
                    problems.append(f"approved pending additive migration: {migration_id}")
                else:
                    raise GuardError(f"Required customer email migration is missing: {migration_id}.")

    if EMAIL_LEGACY_REVIEW_TABLES <= tables:
        from app.services.email_legacy_reviews import validate_email_legacy_review_schema
        try:
            validate_email_legacy_review_schema(connection)
        except Exception as exc:
            raise GuardError(f"Legacy email review schema or checksum is incompatible ({type(exc).__name__}).") from None

    if EMAIL_DELIVERY_OPS_TABLES <= tables:
        from app.services.email_delivery_ops import validate_email_delivery_ops_schema
        try:
            validate_email_delivery_ops_schema(connection, schema=schema)
        except Exception as exc:
            raise GuardError(f"Email delivery operations schema or migration checksum is incompatible ({type(exc).__name__}).") from None

    return ["Preflight: customer email schema is complete or has only approved additive omissions."] + problems


def validate_marketing_suppression_schema(connection, schema: str) -> list[str]:
    """Validate suppression objects, partial-index predicates, composite FKs and triggers."""
    from app.services.marketing_suppressions import validate_marketing_suppression_schema as validate
    validate(connection, schema=schema)
    return ["Marketing suppression schema, constraints, indexes, ledger trigger, and migration checksum are complete."]


def validate_historical_schema(connection, schema: str = "public") -> list[str]:
    """Require the complete post-state of all four historical email migrations."""
    from sqlalchemy import inspect

    schema_arg = schema if connection.dialect.name == "postgresql" else None
    inspector = inspect(connection)
    existing_tables = set(inspector.get_table_names(schema=schema_arg))
    missing_tables = HISTORICAL_REQUIRED_TABLES - existing_tables
    if missing_tables:
        raise GuardError(
            "Historical reconciliation blocked; required tables are missing: "
            + ", ".join(sorted(missing_tables))
            + ". Apply an explicitly approved non-destructive schema upgrade, then revalidate."
        )

    try:
        notes = validate_schema(
            connection,
            schema,
            allow_approved_missing=True,
            strict_postgres_catalog=True,
        )
    except GuardError as exc:
        raise GuardError(f"Historical schema validation failed: {exc}") from None

    historical_omissions = []
    for note in notes:
        for table in EMAIL_STUDIO_TABLES:
            if note.startswith(f"approved missing table: {table} "):
                historical_omissions.append(note)
        for table, column in HISTORICAL_APPROVED_MISSING_COLUMNS:
            if note == f"approved missing column: {table}.{column}":
                historical_omissions.append(note)
        if any(note == f"approved missing index: {name}" for name in HISTORICAL_APPROVED_MISSING_INDEXES):
            historical_omissions.append(note)

    if historical_omissions:
        raise GuardError(
            "Historical reconciliation blocked; required historical objects are missing: "
            + "; ".join(sorted(set(historical_omissions)))
            + ". No migration history should be recorded until these objects are restored and validated."
        )

    if connection.dialect.name == "sqlite":
        from app import customer_email_schema_tables

        column_manifest = migration_column_manifest()
        tables_by_name = {
            table.name: table for table in customer_email_schema_tables()
        }
        for table_name in HISTORICAL_REQUIRED_TABLES:
            table = tables_by_name[table_name]
            actual_columns = {
                row["name"]: row for row in inspector.get_columns(table_name)
            }
            for (manifest_table, column_name), expected in column_manifest.items():
                if manifest_table != table_name or column_name not in actual_columns:
                    continue
                actual = actual_columns[column_name]
                validate_sqlite_column_snapshot(
                    table_name,
                    column_name,
                    actual["type"].compile(dialect=connection.dialect),
                    actual["nullable"],
                    actual.get("default"),
                    expected,
                    primary_key=table.columns[column_name].primary_key,
                )

    return [
        "Complete historical customer/email schema validated; historical SQL was not executed.",
        *[note for note in notes if not note.startswith("Preflight:")],
    ]


def write_summary(lines: list[str]) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    with open(summary_path, "a", encoding="utf-8") as summary:
        summary.write("## Customer email schema migration guard\n\n")
        summary.write("\n".join(f"- {line}" for line in lines) + "\n")


def _safe_driver_versions() -> str:
    """Read local driver versions without opening a database connection."""
    version = implementation = libpq_version = "unavailable"
    try:
        import psycopg
        version = str(psycopg.__version__)
    except Exception:
        pass
    try:
        from psycopg import pq
        implementation = str(pq.__impl__)
        libpq_version = str(pq.version())
    except Exception:
        pass
    safe = re.compile(r"^[A-Za-z0-9_.+-]{1,40}$")
    version = version if safe.fullmatch(version) else "unavailable"
    implementation = implementation if safe.fullmatch(implementation) else "unavailable"
    libpq_version = libpq_version if safe.fullmatch(libpq_version) else "unavailable"
    return f"psycopg={version} implementation={implementation} libpq={libpq_version}"


def _exception_chain(error: Exception):
    """Yield a bounded exception/cause chain without rendering tracebacks."""
    pending = [error]
    seen: set[int] = set()
    while pending and len(seen) < 12:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        for attribute in ("__cause__", "__context__", "orig"):
            try:
                nested = getattr(current, attribute, None)
            except Exception:
                nested = None
            if isinstance(nested, Exception) and id(nested) not in seen:
                pending.append(nested)


def _connection_failure_category(error: Exception) -> str:
    """Classify using SQLSTATEs and allowlisted phrases across wrapped errors.

    Exception text is inspected only in memory. Only fixed category labels leave
    this function; raw diagnostics can contain connection details.
    """
    chain = list(_exception_chain(error))
    messages: list[str] = []
    states: list[str] = []
    for item in chain:
        try:
            message = str(item).lower()
            if message:
                messages.append(message)
        except Exception:
            pass
        try:
            state = getattr(item, "sqlstate", None)
            if not state:
                state = getattr(getattr(item, "diag", None), "sqlstate", None)
            if isinstance(state, str) and re.fullmatch(r"[0-9A-Z]{5}", state):
                states.append(state)
        except Exception:
            pass
    text = "\n".join(messages)

    # More specific certificate errors take precedence over a wrapper's generic
    # "SSL error" message.
    if any(phrase in text for phrase in (
        "does not match host name", "does not match hostname",
        "hostname mismatch", "host name mismatch",
        "certificate subject name does not match",
    )):
        return "hostname certificate verification failure"
    if any(phrase in text for phrase in (
        "unable to get local issuer certificate", "unable to get issuer certificate",
        "unable to verify the first certificate", "self-signed certificate",
        "certificate verify failed", "certificate verification failed",
        "certificate has expired", "certificate is not yet valid",
    )):
        return "CA trust/certificate-chain verification failure"
    if any(phrase in text for phrase in (
        "invalid sslmode", "invalid sslrootcert", "invalid connection option \"sslmode",
        "could not read root certificate", "root certificate file",
    )):
        return "invalid TLS configuration"
    if any(phrase in text for phrase in (
        "tls handshake", "ssl handshake", "tlsv1 alert", "sslv3 alert",
        "wrong version number", "protocol version", "ssl syscall error", "ssl error",
    )):
        return "other TLS handshake failure"

    if any(state.startswith("28") for state in states):
        return "authentication error"
    if any(state.startswith("08") for state in states):
        return "network/connectivity error"
    if any(phrase in text for phrase in (
        "password authentication failed", "no password supplied", "authentication failed",
    )):
        return "authentication error"
    if any(phrase in text for phrase in ("timeout expired", "timed out", "connection timeout")):
        return "timeout error"
    if any(phrase in text for phrase in (
        "connection refused", "network is unreachable", "no route to host",
        "could not translate host name", "could not connect to server",
    )):
        return "network/connectivity error"
    return "unclassified connection error"


def safe_failure(
    stage: str,
    error: Exception,
    *,
    category: str | None = None,
    include_driver_diagnostics: bool = True,
) -> GuardError:
    """Expose version diagnostics and a bounded category, never exception text."""
    if not include_driver_diagnostics:
        return GuardError(f"{stage} failed; diagnostic details were suppressed.")
    category = category or _connection_failure_category(error)
    allowed_categories = {
        "authentication error", "CA trust/certificate-chain verification failure",
        "hostname certificate verification failure", "invalid TLS configuration",
        "other TLS handshake failure", "network/connectivity error", "timeout error", "identity check failed",
        "unclassified connection error",
    }
    if category not in allowed_categories:
        category = "unclassified connection error"
    versions = _safe_driver_versions()
    return GuardError(
        f"{stage} failed; {versions}; category={category}. Raw diagnostics suppressed."
    )


def _connect() -> object:
    from sqlalchemy import create_engine
    url, expected_database, expected_schema, _expected_host = required_settings()
    engine = None
    try:
        engine = create_engine(
            _engine_url(url),
            connect_args={
                "sslmode": "verify-full",
                "sslrootcert": str(trusted_system_ca_bundle()),
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
    """Export the verified Neon URL and Ubuntu system CA settings to GitHub."""
    from sqlalchemy.engine import make_url

    url, _database, _schema, _host = required_settings()
    github_env = Path(os.environ.get("GITHUB_ENV", ""))
    if not github_env.is_file():
        raise GuardError("GitHub runner environment files are unavailable.")
    try:
        ca_file = trusted_system_ca_bundle()
        parsed = make_url(url)
        query = {key: value for key, value in parsed.query.items() if key.lower() not in {"sslmode", "sslrootcert"}}
        query.update({"sslmode": "verify-full", "sslrootcert": str(ca_file)})
        verified_url = parsed.set(drivername="postgresql", query=query).render_as_string(hide_password=False)
        if "\n" in verified_url or "\r" in verified_url:
            raise GuardError("Database URL format is invalid; details suppressed.")
        with github_env.open("a", encoding="utf-8", newline="\n") as output:
            output.write(f"DATABASE_URL={verified_url}\n")
            output.write(f"DATABASE_CA_CERT_FILE={ca_file}\n")
            output.write("PGSSLMODE=verify-full\n")
            output.write(f"PGSSLROOTCERT={ca_file}\n")
    except GuardError:
        raise
    except Exception:
        raise GuardError("Verified TLS environment could not be prepared; details suppressed.") from None
    print("Verified TLS environment prepared with the Ubuntu system CA bundle; no connection opened.")
    write_summary(["Verified TLS environment prepared with the Ubuntu system CA bundle.",
                   "No database connection opened."])


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
    except IdentityCheckError as exc:
        if mode == "preflight":
            raise safe_failure(
                "Database connection/identity check", exc, category="identity check failed"
            ) from None
        raise
    except GuardError:
        raise
    except Exception as exc:
        raise safe_failure(
            "Database connection/identity check", exc,
            include_driver_diagnostics=(mode == "preflight"),
        ) from None

    try:
        schema = os.environ["EXPECTED_DATABASE_SCHEMA"]
        if mode == "suppression-check":
            notes = validate_marketing_suppression_schema(connection, schema)
        else:
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
        raise safe_failure(
            "Database catalog/schema inspection", exc,
            include_driver_diagnostics=False,
        ) from None
    finally:
        if connection is not None:
            engine = getattr(connection, "engine", None)
            connection.close()
            if engine is not None:
                engine.dispose()


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2 or sys.argv[1] not in {"confirm", "dispatch-check", "settings", "preflight", "postcheck", "suppression-check", "tls-env"}:
            raise GuardError("Usage: customer_email_schema_guard.py confirm|dispatch-check|settings|preflight|postcheck|suppression-check|tls-env")
        if sys.argv[1] == "settings":
            required_settings()
            trusted_system_ca_bundle()
            print("Connection settings and Ubuntu system CA bundle are valid; TLS policy is verify-full. No database connection opened.")
            write_summary(["Required identity settings and Ubuntu system CA bundle are valid.",
                           "TLS policy is verify-full.", "No database connection opened."])
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
