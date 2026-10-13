from app.extensions import db
from app.models import InventoryMovement, Product, ProductVariant, RestockAlert

class InventoryError(ValueError):
    pass

def adjust_stock(product: Product, change: int, reason: str, admin=None):
    new_quantity = product.stock_quantity + int(change)
    if new_quantity < 0:
        raise InventoryError("Stock cannot go below zero.")
    product.stock_quantity = new_quantity
    movement = InventoryMovement(product=product, change=int(change), quantity_after=new_quantity, reason=reason.strip() or "Manual adjustment", admin=admin if getattr(admin, "is_authenticated", False) else None)
    db.session.add(movement)
    if int(change) > 0:
        queue_restock_alerts(product)
    return movement

def adjust_variant_stock(variant: ProductVariant, change: int, reason: str, admin=None):
    new_quantity = variant.stock_quantity + int(change)
    if new_quantity < 0:
        raise InventoryError("Stock cannot go below zero.")
    variant.stock_quantity = new_quantity
    movement = InventoryMovement(product=variant.product, variant=variant, change=int(change), quantity_after=new_quantity, reason=reason.strip() or "Manual variant adjustment", admin=admin if getattr(admin, "is_authenticated", False) else None)
    db.session.add(movement)
    if int(change) > 0:
        queue_restock_alerts(variant.product)
    return movement

def queue_restock_alerts(product: Product):
    if product.available_quantity <= 0:
        return []
    alerts = RestockAlert.query.filter_by(product_id=product.id, status="pending", consent_to_contact=True).all()
    for alert in alerts:
        alert.status = "queued"
    return alerts

def low_stock_products():
    products = Product.query.filter(Product.active.is_(True)).all()
    return [product for product in products if product.display_stock_quantity <= product.low_stock_threshold]
