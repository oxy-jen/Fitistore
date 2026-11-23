from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_marketing_queue_workflow_keeps_schedule_and_dispatch_gates():
    workflow = (ROOT / ".github" / "workflows" / "process-marketing-email-queue.yml").read_text(
        encoding="utf-8"
    )

    assert 'cron: "*/5 * * * *"' in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "vars.EMAIL_MARKETING_QUEUE_ENABLED == 'true'" in workflow
    assert "inputs.confirmation == 'RUN MARKETING EMAIL QUEUE'" in workflow
    assert "process-marketing-email-queue" in workflow
    assert "vars.EMAIL_MARKETING_PREPARATION_ENABLED == 'true'" in workflow
    assert "prepare-marketing-campaign-drafts" in workflow
    assert "PREPARE MARKETING CAMPAIGN DRAFTS" in workflow
    assert "jobs:\n  prepare:" in workflow
    for gate in (
        "EMAIL_MARKETING_NEW_PRODUCTS_ENABLED",
        "EMAIL_MARKETING_OFFERS_ENABLED",
        "EMAIL_MARKETING_WEEKLY_PICKS_ENABLED",
        "EMAIL_MARKETING_ABANDONED_CART_ENABLED",
        "EMAIL_MARKETING_SEASONAL_ENABLED",
        "EMAIL_MARKETING_CUSTOM_ENABLED",
    ):
        assert f"vars.{gate}" in workflow
        assert f"{gate}:" in workflow


def test_legacy_marketing_automation_workflow_cannot_run_a_job():
    workflow = (ROOT / ".github" / "workflows" / "marketing-automation.yml").read_text(
        encoding="utf-8"
    )

    assert "schedule:" not in workflow
    assert "if: ${{ false }}" in workflow
