from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

from scripts.email_migration_preflight import validate_target_url

COMMANDS = {
    "check-customer-email-schema": None,
    "migrate-customer-email-schema": "APPLY ISOLATED CUSTOMER EMAIL MIGRATION",
    "reconcile-customer-email-migration-history": "RECONCILE VERIFIED CUSTOMER EMAIL SCHEMA",
    "check-marketing-suppression-schema": None,
    "migrate-marketing-suppression-schema": "APPLY ISOLATED MARKETING SUPPRESSION MIGRATION",
}
BACKUP_CONFIRMATION = "I HAVE A VERIFIED RESTORABLE BACKUP"


def prepare_invocation(arguments: list[str], env: dict[str, str] | None = None) -> tuple[list[str], dict[str, str]]:
    env = dict(os.environ if env is None else env)
    if not arguments or arguments[0] not in COMMANDS:
        raise ValueError("Choose one supported isolated email schema check or migration command.")
    command = arguments[0]
    confirmation = COMMANDS[command]
    rest = arguments[1:]
    if confirmation:
        if (len(rest) != 4 or rest[0] != "--confirm" or rest[1] != confirmation
                or rest[2] != "--backup-confirm" or rest[3] != BACKUP_CONFIRMATION):
            raise ValueError("Exact operation and verified-backup confirmations are required.")
        rest = ["--confirmation", confirmation] if command == "reconcile-customer-email-migration-history" else []
    elif rest:
        raise ValueError("Arguments are not accepted for this read-only schema check.")
    url = validate_target_url(env.get("DATABASE_URL", ""))
    query = dict(url.query)
    query["sslmode"] = "verify-full"
    env["DATABASE_URL"] = url.set(
        drivername="postgresql+psycopg", query=query,
    ).render_as_string(hide_password=False)
    return [sys.executable, "-m", "flask", "--app", "manage:app", command, *rest], env


def main(arguments: list[str] | None = None) -> int:
    if not load_dotenv(Path(__file__).resolve().parents[1] / ".env.neon-test", override=True):
        print("Isolated environment file unavailable; no command was run.")
        return 2
    try:
        command, env = prepare_invocation(list(sys.argv[1:] if arguments is None else arguments))
        completed = subprocess.run(command, env=env, check=False)
        return completed.returncode
    except Exception as exc:
        print(f"Isolated command stopped ({type(exc).__name__}); no command was run.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
