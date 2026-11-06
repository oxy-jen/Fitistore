from datetime import datetime

from sqlalchemy.orm import selectinload

from app.extensions import db
from app.models import MediaShowcase, MediaShowcaseItem


# Every key below has an explicit corresponding include in the named template.
SHOWCASE_PLACEMENTS = {
    "home": {
        "home_after_hero": "After hero and trust indicators (before Featured Picks)",
        "home_before_departments": "Before Departments",
        "home_before_mamba": "Before Mamba Collection",
        "home_before_upcoming": "Before Upcoming Merchandise",
        "home_before_footer": "Before homepage footer content",
    },
    "about": {"about_after_banner": "After About page banner", "about_before_story": "After navigation, before Our Story", "about_before_footer": "Before final About call to action"},
    "shop": {"shop_before_products": "Before product grid", "shop_before_footer": "Before Shop page footer content"},
    "upcoming": {"upcoming_before_products": "After banner, before upcoming merchandise", "upcoming_before_footer": "Before stock request form"},
}

LAYOUTS = {
    "carousel": "Horizontal media carousel", "banner": "Full-width promotional banner",
    "grid": "Responsive media grid", "featured": "Featured media player",
    "mixed": "Mixed-media gallery", "single": "Single media card", "split": "Split promotional section",
}

DENSITIES = {"compact": "Compact", "balanced": "Balanced", "spacious": "Spacious"}
CARD_ASPECT_RATIOS = {"1:1": "Square (1:1)", "4:3": "Classic (4:3)", "3:2": "Photo (3:2)", "16:9": "Widescreen (16:9)"}


def active_showcases(page):
    """Load active, scheduled showcases for one public page in two queries."""
    now = datetime.utcnow()
    return (MediaShowcase.query.options(selectinload(MediaShowcase.items).selectinload(MediaShowcaseItem.linked_product))
            .filter_by(page_identifier=page, is_enabled=True, is_published=True)
            .filter(MediaShowcase.items.any(MediaShowcaseItem.is_active.is_(True)))
            .filter((MediaShowcase.starts_at.is_(None)) | (MediaShowcase.starts_at <= now))
            .filter((MediaShowcase.expires_at.is_(None)) | (MediaShowcase.expires_at > now))
            .order_by(MediaShowcase.placement_identifier, MediaShowcase.display_order, MediaShowcase.id).all())


def showcases_by_placement(page):
    """Combine all active showcases in each page slot into one shared media rail."""
    groups = {}
    for showcase in active_showcases(page):
        group = groups.setdefault(showcase.placement_identifier, {
            "primary": showcase, "showcases": [], "items": [],
        })
        group["showcases"].append(showcase)
        group["items"].extend(
            {"showcase": showcase, "item": item}
            for item in showcase.items if item.is_active
        )
    return {
        key: ([groups[key]] if key in groups else [])
        for key in SHOWCASE_PLACEMENTS.get(page, {})
    }

