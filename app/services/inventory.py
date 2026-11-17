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
        if alert.preferred_contact == "Email":
            from sqlalchemy import inspect
            inspector = inspect(db.engine)
            required = {"recipient", "kind", "subject", "idempotency_key", "status", "purpose", "html_body", "text_body"}
            if inspector.has_table("email_message") and required <= {
                column["name"] for column in inspector.get_columns("email_message")
            }:
                from app.services.customer_mail import notify_restock_alert
                # Queue in the same transaction as the stock and alert update.
                # The caller commits both together; no nested transaction or provider I/O.
                message = notify_restock_alert(alert, f"restock-alert:{alert.id}")
                if message and message.status in {"queued", "sent", "delivered"}:
                    alert.status = "notified" if message.status in {"sent", "delivered"} else "queued"
                    if alert.status == "notified":
                        from datetime import datetime
                        alert.notified_at = message.sent_at or datetime.utcnow()
        else:
            # Non-email contacts remain an explicit admin follow-up item; no
            # external message is sent from the inventory transaction.
            alert.status = "queued"
    return alerts

def low_stock_products():
    products = Product.query.filter(Product.active.is_(True)).all()
    return [product for product in products if product.display_stock_quantity <= product.low_stock_threshold]
