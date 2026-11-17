import pytest

from scripts.email_migration_preflight import validate_target_url
from scripts.run_isolated_email_cli import prepare_invocation
from app.services.email_legacy_reviews import normalize_check_definition


def test_preflight_accepts_only_exact_isolated_target_identity():
    url = validate_target_url(
        "postgresql://neondb_owner:placeholder@ep-bold-field-arckwyfj.c-4.us-west-2.aws.neon.tech/neondb"
    )
    assert url.host == "ep-bold-field-arckwyfj.c-4.us-west-2.aws.neon.tech"
    assert url.database == "neondb"
    assert url.username == "neondb_owner"


def test_preflight_selects_installed_psycopg3_for_generic_postgresql_url(monkeypatch):
    import importlib.util
    monkeypatch.setattr(
        importlib.util, "find_spec",
        lambda name: None if name == "psycopg2" else object() if name == "psycopg" else None,
    )
    url = validate_target_url(
        "postgresql://neondb_owner:placeholder@ep-bold-field-arckwyfj.c-4.us-west-2.aws.neon.tech/neondb"
    )
    assert url.drivername == "postgresql+psycopg"
    assert url.host == "ep-bold-field-arckwyfj.c-4.us-west-2.aws.neon.tech"
    assert url.database == "neondb"


def test_isolated_cli_pins_database_before_building_flask_command():
    command, env = prepare_invocation(
        ["check-customer-email-schema"],
        {"DATABASE_URL": "postgresql://neondb_owner:private@ep-bold-field-arckwyfj.c-4.us-west-2.aws.neon.tech/neondb"},
    )
    assert command[-1] == "check-customer-email-schema"
    assert env["DATABASE_URL"].startswith("postgresql+psycopg://")
    assert "sslmode=verify-full" in env["DATABASE_URL"]


def test_isolated_cli_rejects_wrong_target_before_command_creation():
    with pytest.raises(ValueError):
        prepare_invocation(
            ["migrate-customer-email-schema", "--confirm", "APPLY ISOLATED CUSTOMER EMAIL MIGRATION"],
            {"DATABASE_URL": "postgresql://role:private@production.example.com/store"},
        )


def test_isolated_cli_requires_exact_migration_confirmation():
    with pytest.raises(ValueError, match="confirmation"):
        prepare_invocation(
            ["migrate-marketing-suppression-schema"],
            {"DATABASE_URL": "postgresql://neondb_owner:private@ep-bold-field-arckwyfj.c-4.us-west-2.aws.neon.tech/neondb"},
        )


@pytest.mark.parametrize("definition", [
    "CHECK (action IN ('quarantined', 'reviewed'))",
    "CHECK (((action)::character varying = ANY ((ARRAY['quarantined'::character varying, 'reviewed'::character varying])::character varying[])))",
])
def test_legacy_review_check_normalization_accepts_sqlite_and_postgresql_forms(definition):
    normalized = normalize_check_definition(definition)
    assert normalized in {
        "actionin'quarantined','reviewed'",
        "action=anyarray['quarantined','reviewed']",
    }


@pytest.mark.parametrize("url", [
    "postgresql://role:secret@ep-production.c-4.us-west-2.aws.neon.tech/neondb",
    "postgresql://neondb_owner:secret@ep-bold-field-arckwyfj.c-4.us-west-2.aws.neon.tech/production",
    "sqlite:///fity.sqlite3",
])
def test_preflight_rejects_non_isolated_or_non_postgres_targets(url):
    with pytest.raises(ValueError):
        validate_target_url(url)
