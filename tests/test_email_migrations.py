from sqlalchemy import create_engine, text
import pytest

from app.services.email_migrations import (
    HISTORY_TABLE, MIGRATION_HISTORY_ID, MigrationLockBusy, acquire_migration_lock,
    ensure_history_table, record_migration, validate_history_checksums,
)


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value


class _Connection:
    class dialect:
        name = "postgresql"

    def __init__(self, acquired):
        self.acquired = acquired
        self.statements = []

    def execute(self, statement, params):
        self.statements.append((str(statement), params))
        return _ScalarResult(self.acquired)


def test_shared_migration_lock_is_nonblocking_and_transaction_scoped():
    connection = _Connection(True)
    acquire_migration_lock(connection)
    assert "pg_try_advisory_xact_lock" in connection.statements[0][0]

    with pytest.raises(MigrationLockBusy, match="Another customer/email migration"):
        acquire_migration_lock(_Connection(False))


def test_migration_history_is_idempotent_and_checksum_mismatch_fails_closed():
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            ensure_history_table(connection)
            record_migration(connection, MIGRATION_HISTORY_ID, state="applied", evidence="test ledger")
            record_migration(connection, MIGRATION_HISTORY_ID, state="applied", evidence="repeat")
            assert validate_history_checksums(connection)
            connection.execute(text(
                "UPDATE email_migration_history SET checksum=:checksum WHERE migration_id=:migration_id"
            ), {"checksum": "0" * 64, "migration_id": MIGRATION_HISTORY_ID})
            with pytest.raises(ValueError, match="checksum mismatch"):
                validate_history_checksums(connection)
    finally:
        engine.dispose()
