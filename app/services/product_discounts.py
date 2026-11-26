from sqlalchemy import and_, func, or_
from sqlalchemy.orm import joinedload, selectinload

from app.models import Product, ProductVariant


def discounted_products_query():
    variant_price = func.coalesce(ProductVariant.price_cents, Product.price_cents)
    variant_discount = Product.variants.any(and_(
        ProductVariant.active.is_(True),
        ProductVariant.previous_price_cents.isnot(None),
        variant_price.isnot(None),
        ProductVariant.previous_price_cents > variant_price,
    ))
    product_discount = and_(
        ~Product.variants.any(ProductVariant.active.is_(True)),
        Product.previous_price_cents.isnot(None),
        Product.price_cents.isnot(None),
        Product.previous_price_cents > Product.price_cents,
    )
    return Product.query.filter(Product.active.is_(True), or_(variant_discount, product_discount))


def _product_discount_percent(product):
    variant_discounts = [
        variant.discount_percent
        for variant in product.active_variants
        if variant.has_discount
    ]
    if variant_discounts:
        return max(variant_discounts)
    if product.has_discount:
        return (product.previous_price_cents - product.price_cents) * 100 // product.previous_price_cents
    return 0


def current_discounted_products(limit=8):
    products = discounted_products_query().options(
        joinedload(Product.category),
        selectinload(Product.images),
        selectinload(Product.variants).joinedload(ProductVariant.image),
    ).order_by(Product.updated_at.desc(), Product.id.desc()).all()
    products.sort(key=_product_discount_percent, reverse=True)
    return products[:max(0, int(limit))]
