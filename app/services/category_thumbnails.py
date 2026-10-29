import re

from sqlalchemy import inspect

from app.extensions import db
from app.models import CategoryThumbnail


CATEGORY_GROUPS = (
    {
        "key": "socks-essentials",
        "name": "Socks & Essentials",
        "description": "Everyday basics and innerwear.",
        "aliases": ("ankle socks", "sports socks", "socks", "vests", "boxers", "panties", "underwear", "innerwear", "essential wear"),
        "default_image": "images/shop-hero/hair_bonnet-removebg-preview.webp",
    },
    {
        "key": "clothing-apparel",
        "name": "Clothing & Apparel",
        "description": "Wearable fashion for everyday use.",
        "aliases": ("t shirts", "t shirt", "tshirts", "hoodies", "hoodies and sweatshirts", "sweatshirts", "shirts", "trousers", "pants", "shorts", "jackets", "clothing", "apparel"),
        "default_image": "images/shop-hero/home-banner.png",
    },
    {
        "key": "shoes-footwear",
        "name": "Shoes & Footwear",
        "description": "Footwear for different occasions.",
        "aliases": ("shoes", "sneakers", "casual shoes", "sports shoes", "sandals", "slippers", "footwear", "boots"),
        "default_image": "images/shop-hero/shoe3.fitistores-removebg-preview.png",
    },
    {
        "key": "beauty-accessories",
        "name": "Beauty & Accessories",
        "description": "Hair accessories and everyday extras.",
        "aliases": ("hair bonnets", "hair bonnet", "hair bands", "hair bands and scrunchies", "scrunchies", "shower caps", "caps and hats", "caps", "hats", "fashion accessories", "beauty accessories", "accessories"),
        "default_image": "images/shop-hero/hair_bonnet-removebg-preview.webp",
    },
    {
        "key": "bags-lifestyle",
        "name": "Bags & Lifestyle",
        "description": "Useful everyday items and merchandise.",
        "aliases": ("backpacks and bags", "backpacks", "bags", "wallets", "wallet", "belts", "lifestyle products", "other lifestyle products", "branded merchandise", "fity branded merchandise", "merchandise"),
        "default_image": "images/shop-hero/home-banner.png",
    },
)

CATEGORY_GROUPS_BY_KEY = {group["key"]: group for group in CATEGORY_GROUPS}


def normalize_category_name(value):
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


_GROUPS_BY_ALIAS = {
    normalize_category_name(alias): group["key"]
    for group in CATEGORY_GROUPS
    for alias in group["aliases"]
}


def category_group_key(category_name):
    normalized = normalize_category_name(category_name)
    if normalized in _GROUPS_BY_ALIAS:
        return _GROUPS_BY_ALIAS[normalized]
    # Also accept catalogue labels such as "Mens Sports Shoes" and "Other Clothing".
    if any(word in normalized for word in ("sock", "vest", "boxer", "panty", "underwear", "innerwear")):
        return "socks-essentials"
    if any(word in normalized for word in ("shoe", "sneaker", "footwear", "sandal", "slipper", "boot")):
        return "shoes-footwear"
    if any(word in normalized for word in ("shirt", "hoodie", "sweatshirt", "trouser", "pant", "short", "jacket", "clothing", "apparel", "fashion")):
        return "clothing-apparel"
    if any(word in normalized for word in ("hair", "bonnet", "scrunchie", "shower cap", "accessor", "fashion cap", "hat")):
        return "beauty-accessories"
    if any(word in normalized for word in ("backpack", "bag", "wallet", "belt", "lifestyle", "merchandise")):
        return "bags-lifestyle"
    return None


def category_thumbnails_schema_ready():
    inspector = inspect(db.engine)
    table = CategoryThumbnail.__table__
    if not inspector.has_table(table.name):
        return False
    columns = {column["name"] for column in inspector.get_columns(table.name)}
    return not ({column.name for column in table.columns} - columns)
