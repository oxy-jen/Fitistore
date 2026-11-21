"""Read-only, identity-pinned report for the isolated FityStores restore database.

No dotenv files are loaded. The caller must explicitly set
FITY_NEON_TEST_DATABASE_URL in the process environment. The connection is
pinned to Neon restore-test identity and every SQL transaction is READ ONLY.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path

from sqlalchemy import MetaData, Table, create_engine, func, inspect, select, text
from sqlalchemy.engine import make_url

EXPECTED_HOST = "ep-bold-field-arckwyfj.c-4.us-west-2.aws.neon.tech"
EXPECTED_DATABASE = "backup_restore_check"
EXPECTED_ROLE = "neondb_owner"
EXPECTED_SCHEMA = "public"
EXPECTED_PG_MAJOR = 18
BACKUP_SHA256 = "aed7ba20c65708c994550d19cdbad34b6ffdaa422f4f9415ce58f62ea33fbb2"
BACKUP_SIZE = 202955
BACKUP_FILE = Path(__file__).resolve().parents[1] / "fitystore_neon_20261113_080959.dump"
EXPECTED_MIGRATIONS = [
    "20261117_email_migration_history",
    "20261017_customer_accounts_email", "20261027_email_studio",
    "20261028_email_builder", "20261029_consent_wording_version",
    "20261113_email_outbox", "20261116_email_delivery_ops",
    "20261117_email_legacy_review", "20261117_email_admin_test_purpose",
    "20261117_customer_consent_audit_immutable", "20261113_marketing_suppression",
    "20261121_marketing_campaign_draft_preparation",
]


def validate_target_url(raw_url: str):
    if not raw_url:
        raise ValueError("FITY_NEON_TEST_DATABASE_URL is not set.")
    url = make_url(raw_url)
    if (url.host or "").lower() != EXPECTED_HOST or url.database != EXPECTED_DATABASE or url.username != EXPECTED_ROLE:
        raise ValueError("Configured endpoint does not match the pinned isolated Neon restore-test identity.")
    if url.drivername not in {"postgresql", "postgresql+psycopg", "postgresql+psycopg2", "postgres"}:
        raise ValueError("Only PostgreSQL URLs are supported.")
    # SQLAlchemy's generic `postgresql` name defaults to psycopg2. Prefer an
    # installed psycopg3 driver when psycopg2 is absent; this changes only the
    # local DBAPI selection, never the pinned endpoint or database identity.
    if url.drivername in {"postgresql", "postgres"}:
        if importlib.util.find_spec("psycopg2") is None and importlib.util.find_spec("psycopg") is not None:
            url = url.set(drivername="postgresql+psycopg")
    return url


def backup_evidence() -> dict:
    if not BACKUP_FILE.is_file():
        return {"path": BACKUP_FILE.name, "exists": False, "matches_recorded_size": False,
                "matches_recorded_sha256": False}
    digest = hashlib.sha256()
    with BACKUP_FILE.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {"path": BACKUP_FILE.name, "exists": True,
            "size_bytes": BACKUP_FILE.stat().st_size,
            "matches_recorded_size": BACKUP_FILE.stat().st_size == BACKUP_SIZE,
            "matches_recorded_sha256": digest.hexdigest() == BACKUP_SHA256}


def build_report(connection) -> dict:
    identity = connection.execute(text("""
        SELECT current_database() AS database, current_user AS role,
               current_schema() AS schema, version() AS server_version,
               current_setting('server_version_num') AS server_version_num
    """)).mappings().one()
    major = int(identity["server_version_num"]) // 10000
    if (identity["database"], identity["role"], identity["schema"], major) != (
        EXPECTED_DATABASE, EXPECTED_ROLE, EXPECTED_SCHEMA, EXPECTED_PG_MAJOR
    ):
        raise ValueError("Connected SQL identity differs from the pinned isolated restore-test identity.")

    inspector = inspect(connection)
    tables = sorted(inspector.get_table_names(schema=EXPECTED_SCHEMA))
    details = []
    for name in tables:
        table = Table(name, MetaData(), schema=EXPECTED_SCHEMA, autoload_with=connection)
        count = connection.execute(select(func.count()).select_from(table)).scalar_one()
        details.append({
            "name": name, "row_count": int(count),
            "columns": [{"name": col["name"], "type": str(col["type"]),
                         "nullable": col["nullable"], "default": col.get("default")}
                        for col in inspector.get_columns(name, schema=EXPECTED_SCHEMA)],
            "indexes": inspector.get_indexes(name, schema=EXPECTED_SCHEMA),
            "constraints": {
                "primary_key": inspector.get_pk_constraint(name, schema=EXPECTED_SCHEMA),
                "unique": inspector.get_unique_constraints(name, schema=EXPECTED_SCHEMA),
                "checks": inspector.get_check_constraints(name, schema=EXPECTED_SCHEMA),
                "foreign_keys": inspector.get_foreign_keys(name, schema=EXPECTED_SCHEMA),
            },
        })
    catalog = connection.execute(text("""
        SELECT 'trigger' AS kind, n.nspname AS schema_name, c.relname AS table_name,
               t.tgname AS object_name, pg_get_triggerdef(t.oid, true) AS definition,
               t.tgenabled AS enabled
        FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=:schema AND NOT t.tgisinternal
        UNION ALL
        SELECT 'sequence', n.nspname, NULL, c.relname, NULL, NULL
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=:schema AND c.relkind='S'
        ORDER BY kind, object_name
    """), {"schema": EXPECTED_SCHEMA}).mappings().all()
    ledger = []
    if "email_migration_history" in tables:
        ledger = [{**dict(row), "evidence_present": bool(row["evidence"]), "evidence": None}
                  for row in connection.execute(text(
            "SELECT migration_id, checksum, state, evidence, recorded_at "
            "FROM email_migration_history ORDER BY migration_id"
        )).mappings()]
    existing_ledger = []
    if "email_schema_migration" in tables:
        existing_ledger = [dict(row) for row in connection.execute(text(
            "SELECT migration_id, checksum, applied_at FROM email_schema_migration ORDER BY migration_id"
        )).mappings()]

    suppression_import = {}
    if "customer_account" in tables:
        account_columns = {col["name"] for col in inspector.get_columns("customer_account", schema=EXPECTED_SCHEMA)}
        if "marketing_status" in account_columns:
            suppression_import["legacy_suppressed_account_status"] = connection.execute(text(
                "SELECT count(*) FROM customer_account WHERE marketing_status='suppressed'"
            )).scalar_one()
        if "customer_consent_event" in tables:
            suppression_import["legacy_generic_suppression_consent_events"] = connection.execute(text(
                "SELECT count(*) FROM customer_consent_event WHERE event='suppressed'"
            )).scalar_one()
        if "marketing_suppression_case" in tables:
            suppression_import["existing_suppression_cases"] = connection.execute(text(
                "SELECT count(*) FROM marketing_suppression_case"
            )).scalar_one()

    marketing_counts = {}
    if "email_message" in tables:
        columns = {col["name"] for col in inspector.get_columns("email_message", schema=EXPECTED_SCHEMA)}
        marketing_expr = "(kind='marketing_campaign' OR (kind LIKE 'marketing_%' AND kind NOT IN ('marketing_confirm','marketing_confirmation')) OR (kind LIKE 'admin_%' AND kind <> 'admin_test'))"
        marketing_counts = dict(connection.execute(text(
            f"SELECT count(*) FILTER (WHERE {marketing_expr}) AS marketing_messages, "
            "count(*) FILTER (WHERE status='queued') AS queued_messages "
            "FROM email_message"
        )).mappings().one())
        if {"html_body", "text_body"} <= columns:
            marketing_counts["queued_missing_or_empty_payloads"] = connection.execute(text(
                "SELECT count(*) FROM email_message WHERE status='queued' AND "
                "(html_body IS NULL OR btrim(html_body)='' OR text_body IS NULL OR btrim(text_body)='')"
            )).scalar_one()
        else:
            marketing_counts["queued_missing_or_empty_payloads"] = "all queued rows lack persisted HTML/text columns; dispatch must remain blocked until migration and quarantine"
        if "frequency_override" in columns:
            marketing_counts["frequency_override_values_to_normalize_to_false"] = connection.execute(text(
                "SELECT count(*) FROM email_message WHERE frequency_override IS DISTINCT FROM FALSE"
            )).scalar_one()
        else:
            marketing_counts["frequency_override_values_to_normalize_to_false"] = "not applicable; column absent and new column defaults to false"
            marketing_counts["frequency_override_column"] = "missing; outbox migration would add NOT NULL FALSE default"
        if "purpose" in columns:
            marketing_counts["purpose_values_migration_would_reclassify"] = connection.execute(text(
                "SELECT count(*) FROM email_message WHERE purpose IS NULL OR purpose NOT IN ('marketing','transactional') "
                "OR (purpose='transactional' AND " + marketing_expr + ")"
            )).scalar_one()
        else:
            marketing_counts["purpose_values_migration_would_reclassify"] = "all existing messages classified by kind expression; no data changed by this preflight"
    if "marketing_campaign_run" in tables:
        run_columns = {col["name"] for col in inspector.get_columns("marketing_campaign_run", schema=EXPECTED_SCHEMA)}
        marketing_counts["queued_count_column"] = (
            "present; inspect reported column default" if "queued_count" in run_columns
            else "missing; outbox migration would add NOT NULL default 0"
        )
    return {
        "mode": "read_only",
        "identity": {"database": identity["database"], "role": identity["role"],
                     "schema": identity["schema"], "postgres_major": major,
                     "server_version": identity["server_version"], "project_id": "blue-band-99013811",
                     "branch_id": "br-tiny-scene-argzuxlu", "compute_id": "ep-bold-field-arckwyfj"},
        "tables": details, "triggers_and_sequences": [dict(row) for row in catalog],
        "migration_ids_expected": EXPECTED_MIGRATIONS,
        "migration_history": ledger, "legacy_migration_ledger": existing_ledger,
        "marketing_impact_counts": marketing_counts,
        "suppression_legacy_import_candidates": suppression_import,
        "proposed_changes_only": {
            "expected_schema_changes": EXPECTED_MIGRATIONS,
            "expected_marketing_message_classification": "all 66 previously observed marketing-kind messages are classified, not sent by this tool",
            "frequency_override": "normalization count is reported only if column exists; no update is executed",
            "queued_count": "new column default 0 only if absent; existing values are not reset",
            "suppression_legacy_import": "run suppression preflight separately; this report does not import or alter rows",
        },
        "backup_evidence": backup_evidence(),
        "limits": ["Current table counts do not prove exact equivalence to the recorded dump.",
                   "The previously observed 66 marketing-message count is a historical reference; current count is reported above."],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Identity-pinned, read-only email migration preflight")
    parser.add_argument("--output", type=Path, help="Write the report JSON to this local file")
    args = parser.parse_args()
    try:
        url = validate_target_url(os.environ.get("FITY_NEON_TEST_DATABASE_URL", ""))
        engine = create_engine(url, pool_pre_ping=True, hide_parameters=True)
        try:
            with engine.connect() as connection:
                transaction = connection.begin()
                try:
                    connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                    report = build_report(connection)
                    transaction.rollback()
                except Exception:
                    transaction.rollback()
                    raise
        finally:
            engine.dispose()
        rendered = json.dumps(report, indent=2, sort_keys=True, default=str)
        if args.output:
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(rendered + "\n")
        else:
            print(rendered)
        return 0
    except Exception as exc:
        print(f"Preflight stopped ({type(exc).__name__}); details suppressed.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
