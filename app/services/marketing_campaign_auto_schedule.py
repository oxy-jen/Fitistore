"""Automatically schedule system-generated marketing campaigns.

Only campaigns created by the deterministic preparation service are eligible.
The normal campaign scheduler creates recipients/outbox rows and the normal
marketing worker performs the final consent, suppression, frequency and
provider checks at dispatch time.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from flask import current_app

from app.extensions import db
from app.models import EmailCampaign

AUTO_TYPES = ("special_offer", "new_product", "weekly_picks", "abandoned_cart")


def auto_schedule_generated_campaigns(*, now: datetime, limit_per_type: int = 1) -> dict:
    """Schedule at most one generated campaign per enabled type per run.

    A short delay gives the independent queue worker time to pick up the
    approved campaign. The shared eligibility evaluator enforces opt-in,
    verification, suppression, unsubscribe history and the 24-hour cap.
    """
    from app.services.marketing_campaigns import (
        audience_snapshot, campaign_product_ids, campaign_type_enabled,
        schedule_campaign, validate_campaign_content,
    )

    if not current_app.config.get("EMAIL_MARKETING_AUTO_SCHEDULE_GENERATED_ENABLED", False):
        return {"status": "disabled", "scheduled": 0, "skipped": 0}
    if not current_app.config.get("EMAIL_MARKETING_QUEUE_ENABLED", False):
        return {"status": "queue_disabled", "scheduled": 0, "skipped": 0}

    scheduled = skipped = 0
    scheduled_for = now + timedelta(minutes=10)
    for campaign_type in AUTO_TYPES:
        if not campaign_type_enabled(campaign_type):
            continue
        candidates = EmailCampaign.query.filter(
            EmailCampaign.status == "draft",
            EmailCampaign.generation_key.like("prepare:%"),
            EmailCampaign.campaign_type == campaign_type,
        ).order_by(EmailCampaign.created_at, EmailCampaign.id).limit(max(1, min(3, limit_per_type))).all()
        for campaign in candidates:
            try:
                product_ids = campaign_product_ids(campaign.product_ids_json or "[]")
                validate_campaign_content(
                    name=campaign.name, subject=campaign.subject, body=campaign.body,
                    campaign_type=campaign.campaign_type,
                    product_ids=product_ids,
                    require_products=campaign_type in {"new_product", "weekly_picks", "special_offer"},
                )
                _accounts, eligible, _exclusions = audience_snapshot(campaign, scheduled_for)
                if not eligible:
                    skipped += 1
                    continue
                with db.session.begin_nested():
                    campaign.previewed_at = now
                    schedule_campaign(
                        campaign, scheduled_for,
                        confirmed_count=f"SCHEDULE {len(eligible)}",
                    )
                scheduled += 1
                # One broadcast per campaign type per poll. A subsequent poll
                # will re-evaluate frequency limits against queued/sent rows.
                break
            except ValueError as exc:
                skipped += 1
                current_app.logger.info(
                    "generated_marketing_campaign_not_scheduled campaign_id=%s type=%s reason=%s",
                    campaign.id, campaign_type, str(exc)[:180],
                )
    db.session.commit()
    return {"status": "processed", "scheduled": scheduled, "skipped": skipped}
