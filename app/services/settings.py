from app.extensions import db
from app.models import ShopSettings
from .formatting import money, split_options

def current_settings():
    settings = ShopSettings.query.first()
    if not settings:
        settings = ShopSettings(shop_name="FityStores", tagline="FityStores")
    else:
        changed = False
        if settings.shop_name.strip().upper() in {"FITY", "BEI FITS"}:
            settings.shop_name = "FityStores"
            changed = True
        if settings.tagline.strip().lower() == "bei fity, fits poa.":
            settings.tagline = "FityStores"
            changed = True
        if changed:
            db.session.commit()
    return settings

def inject_helpers(app):
    @app.context_processor
    def helpers():
        return {"settings": current_settings(), "money": money, "split_options": split_options}
