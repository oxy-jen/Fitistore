from app.models import ShopSettings
from .formatting import money, split_options

def current_settings():
    settings = ShopSettings.query.first()
    if not settings:
        settings = ShopSettings(shop_name="FITY", tagline="Bei Fity, Fits Poa.")
    return settings

def inject_helpers(app):
    @app.context_processor
    def helpers():
        return {"settings": current_settings(), "money": money, "split_options": split_options}
