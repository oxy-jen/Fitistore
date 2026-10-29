from sqlalchemy import inspect

from app.extensions import db
from app.models import Category, MambaCollection, MambaCollectionItem, Product


MAMBA_CATEGORY_NAMES = ("Boxers", "Shoes", "Vests", "Ankle socks", "Socks")


def mamba_collection_schema_ready():
    inspector = inspect(db.engine)
    for model in (MambaCollection, MambaCollectionItem):
        table = model.__table__
        if not inspector.has_table(table.name):
            return False
        columns = {column["name"] for column in inspector.get_columns(table.name)}
        if {column.name for column in table.columns} - columns:
            return False
    return True


def mamba_products_query():
    """Return active curated Mamba products, or the legacy category list until configured."""
    if mamba_collection_schema_ready():
        collection = MambaCollection.query.order_by(MambaCollection.id.asc()).first()
        if collection and collection.is_configured:
            return (
                Product.query.join(MambaCollectionItem, MambaCollectionItem.product_id == Product.id)
                .filter(Product.active.is_(True))
                .order_by(MambaCollectionItem.sort_order.asc(), MambaCollectionItem.id.asc()),
                True,
            )

    return (
        Product.query.join(Category)
        .filter(Product.active.is_(True), Category.name.in_(MAMBA_CATEGORY_NAMES))
        .order_by(Category.name.asc(), Product.updated_at.desc(), Product.id.desc()),
        False,
    )
