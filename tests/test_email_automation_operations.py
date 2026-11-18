from pathlib import Path


def test_email_queue_workflow_is_explicitly_disabled_until_repository_variable_enabled():
    workflow = Path(".github/workflows/process-email-queue.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow
    assert "schedule:" in workflow
    assert "vars.EMAIL_QUEUE_ENABLED == 'true'" in workflow
    assert "inputs.confirmation == 'RUN EMAIL QUEUE'" in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    assert '--confirmation "$QUEUE_CONFIRMATION"' in workflow
    assert "DATABASE_URL: ${{ secrets.RENDER_DATABASE_URL }}" in workflow
    assert "python scripts/customer_email_schema_guard.py preflight" in workflow
    assert "process-email-queue" in workflow
    assert "migrate-customer-email-schema" not in workflow
    assert "contents: read" in workflow


def test_marketing_schedule_remains_unavailable():
    workflow = Path(".github/workflows/marketing-automation.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow
    assert "schedule:" not in workflow
    assert "if: ${{ false }}" in workflow
