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
    import sys

    restore_url = "postgresql://neondb_owner:private@ep-bold-field-arckwyfj.c-4.us-west-2.aws.neon.tech/neondb"
    command, env = prepare_invocation(
        ["check-customer-email-schema"],
        {
            "FITY_NEON_TEST_DATABASE_URL": restore_url,
            "DATABASE_URL": "postgresql://role:private@production.example.com/store",
        },
    )
    assert command[:5] == [
        sys.executable, "-m", "flask", "--app", "manage:app",
    ]
    assert command[-1] == "check-customer-email-schema"
    assert env["DATABASE_URL"].startswith("postgresql+psycopg://")
    assert "sslmode=verify-full" in env["DATABASE_URL"]
    assert "FITY_NEON_TEST_DATABASE_URL" not in env
    assert "production.example.com" not in env["DATABASE_URL"]


def test_isolated_cli_rejects_wrong_target_before_command_creation():
    with pytest.raises(ValueError):
        prepare_invocation(
            ["migrate-customer-email-schema", "--confirm", "APPLY ISOLATED CUSTOMER EMAIL MIGRATION"],
            {"FITY_NEON_TEST_DATABASE_URL": "postgresql://role:private@production.example.com/store"},
        )


def test_isolated_cli_does_not_fall_back_to_database_url():
    with pytest.raises(ValueError, match="FITY_NEON_TEST_DATABASE_URL is not set"):
        prepare_invocation(
            ["check-customer-email-schema"],
            {"DATABASE_URL": "postgresql://role:private@production.example.com/store"},
        )


def test_isolated_cli_main_uses_only_local_restore_url(monkeypatch):
    from types import SimpleNamespace

    from scripts import run_isolated_email_cli

    restore_url = "postgresql://neondb_owner:private@ep-bold-field-arckwyfj.c-4.us-west-2.aws.neon.tech/neondb"
    production_url = "postgresql://role:private@production.example.com/store"
    monkeypatch.setattr(
        run_isolated_email_cli,
        "dotenv_values",
        lambda _path: {
            "FITY_NEON_TEST_DATABASE_URL": restore_url,
            "DATABASE_URL": production_url,
        },
    )
    monkeypatch.setenv("DATABASE_URL", production_url)
    captured = {}

    def fake_run(command, *, env, check):
        captured["command"] = command
        captured["env"] = env
        captured["check"] = check
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(run_isolated_email_cli.subprocess, "run", fake_run)
    result = run_isolated_email_cli.main(["check-customer-email-schema"])

    assert result == 0
    assert captured["command"][:5] == [
        run_isolated_email_cli.sys.executable, "-m", "flask", "--app", "manage:app",
    ]
    assert "production.example.com" not in captured["env"]["DATABASE_URL"]
    assert captured["env"]["DATABASE_URL"].startswith("postgresql+psycopg://")
    assert "sslmode=verify-full" in captured["env"]["DATABASE_URL"]
    assert captured["check"] is False


def test_isolated_cli_main_stops_without_dedicated_restore_url(monkeypatch, capsys):
    from scripts import run_isolated_email_cli

    production_url = "postgresql://role:private@production.example.com/store"
    monkeypatch.setattr(
        run_isolated_email_cli,
        "dotenv_values",
        lambda _path: {"DATABASE_URL": production_url},
    )
    monkeypatch.setenv("DATABASE_URL", production_url)
    monkeypatch.setattr(
        run_isolated_email_cli.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("No command should run without the dedicated URL."),
    )

    assert run_isolated_email_cli.main(["check-customer-email-schema"]) == 2
    assert "Dedicated restore-test URL is missing" in capsys.readouterr().out


def test_isolated_cli_requires_exact_migration_confirmation():
    with pytest.raises(ValueError, match="confirmation"):
        prepare_invocation(
            ["migrate-marketing-suppression-schema"],
            {"FITY_NEON_TEST_DATABASE_URL": "postgresql://neondb_owner:private@ep-bold-field-arckwyfj.c-4.us-west-2.aws.neon.tech/neondb"},
        )


@pytest.mark.parametrize(("command_name", "operation_confirmation", "cli_confirmation"), [
    (
        "migrate-customer-email-schema",
        "APPLY ISOLATED CUSTOMER EMAIL MIGRATION",
        "MIGRATE CUSTOMER EMAIL SCHEMA",
    ),
    (
        "reconcile-customer-email-migration-history",
        "RECONCILE VERIFIED CUSTOMER EMAIL SCHEMA",
        "RECONCILE VERIFIED CUSTOMER EMAIL SCHEMA",
    ),
    (
        "migrate-marketing-suppression-schema",
        "APPLY ISOLATED MARKETING SUPPRESSION MIGRATION",
        "APPLY MARKETING SUPPRESSION MIGRATION",
    ),
])
def test_isolated_cli_passes_separate_confirmation_and_backup_gates(
    command_name, operation_confirmation, cli_confirmation,
):
    command, _env = prepare_invocation(
        [
            command_name,
            "--confirm", operation_confirmation,
            "--backup-confirm", "I HAVE A VERIFIED RESTORABLE BACKUP",
        ],
        {"FITY_NEON_TEST_DATABASE_URL": "postgresql://neondb_owner:private@ep-bold-field-arckwyfj.c-4.us-west-2.aws.neon.tech/neondb"},
    )
    assert command[-4:] == [
        "--confirmation", cli_confirmation,
        "--backup-confirmation", "I HAVE A VERIFIED RESTORABLE BACKUP",
    ]


@pytest.mark.parametrize("definition", [
    "CHECK (action IN ('quarantined', 'reviewed'))",
    "action = ANY ((ARRAY['quarantined'::character varying, 'reviewed'::character varying])::character varying[])",
])
def test_legacy_review_check_normalization_accepts_sqlite_and_postgresql_forms(definition):
    normalized = normalize_check_definition(definition)
    assert normalized == normalize_check_definition("action IN ('quarantined', 'reviewed')")


def test_legacy_review_check_normalization_rejects_changed_allowed_values():
    assert normalize_check_definition("action IN ('quarantined', 'deleted')") != normalize_check_definition(
        "action IN ('quarantined', 'reviewed')"
    )


@pytest.mark.parametrize("url", [
    "postgresql://role:secret@ep-production.c-4.us-west-2.aws.neon.tech/neondb",
    "postgresql://neondb_owner:secret@ep-bold-field-arckwyfj.c-4.us-west-2.aws.neon.tech/production",
    "sqlite:///fity.sqlite3",
])
def test_preflight_rejects_non_isolated_or_non_postgres_targets(url):
    with pytest.raises(ValueError):
        validate_target_url(url)
