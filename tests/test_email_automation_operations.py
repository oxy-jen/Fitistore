from pathlib import Path


def test_email_queue_workflow_is_explicitly_disabled_until_repository_variable_enabled():
    workflow = Path(".github/workflows/process-email-queue.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow
    assert "schedule:" in workflow
    assert "vars.EMAIL_QUEUE_ENABLED == 'true'" in workflow
    assert "inputs.confirmation == 'RUN EMAIL QUEUE'" in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "PYTHONPATH: ${{ github.workspace }}" in workflow
    assert '--confirmation "$QUEUE_CONFIRMATION"' in workflow
    assert "DATABASE_URL: ${{ secrets.NEON_DATABASE_URL }}" in workflow
    assert "vars.NEON_DATABASE_HOST" in workflow
    assert "vars.NEON_DATABASE_NAME" in workflow
    assert "vars.NEON_DATABASE_SCHEMA" in workflow
    assert "RENDER_DATABASE_" not in workflow
    assert "python scripts/customer_email_schema_guard.py preflight" in workflow
    assert "process-email-queue" in workflow
    assert "migrate-customer-email-schema" not in workflow
    assert "contents: read" in workflow


def test_email_queue_mail_port_defaults_when_unset_and_preserves_explicit_value():
    workflow = Path(".github/workflows/process-email-queue.yml").read_text(encoding="utf-8")
    assert "MAIL_PORT: ${{ vars.MAIL_PORT || '587' }}" in workflow


def test_transactional_worker_has_independent_disabled_gate_and_isolated_command():
    workflow = Path(".github/workflows/process-transactional-email-queue.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow
    assert "schedule:" in workflow
    assert "vars.EMAIL_TRANSACTIONAL_QUEUE_ENABLED == 'true'" in workflow
    assert "inputs.confirmation == 'RUN TRANSACTIONAL EMAIL QUEUE'" in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "PYTHONPATH: ${{ github.workspace }}" in workflow
    assert "python scripts/customer_email_schema_guard.py tls-env" in workflow
    assert "python scripts/customer_email_schema_guard.py preflight" in workflow
    assert "process-transactional-email-queue" in workflow
    assert "process-email-queue " not in workflow
    assert "migrate-customer-email-schema" not in workflow


def test_admin_test_workflow_is_manual_gated_and_passes_only_one_message_id():
    workflow = Path(".github/workflows/dispatch-admin-test-email.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow
    assert "schedule:" not in workflow
    assert "vars.EMAIL_TEST_DISPATCH_ENABLED == 'true'" in workflow
    assert "inputs.confirmation == 'RUN SINGLE ADMIN TEST EMAIL'" in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "PYTHONPATH: ${{ github.workspace }}" in workflow
    assert "MAIL_TEST_ADDRESS: ${{ secrets.MAIL_TEST_ADDRESS }}" in workflow
    assert "dispatch-admin-test-email" in workflow
    assert "process-email-queue " not in workflow


def test_marketing_schedule_remains_unavailable():
    workflow = Path(".github/workflows/marketing-automation.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow
    assert "schedule:" not in workflow
    assert "if: ${{ false }}" in workflow
