from datetime import datetime
from secrets import token_hex
from sqlalchemy import func
from app.extensions import db
from app.models import Expense, FinancialAuditEvent, LoyaltyTransaction, Order, OrderItem, OrderStatusHistory, PaymentAttempt, PaymentTransaction, Product, RefundRequest
from .cart import validate_cart

FULFILMENT_STATUSES = ["Order received", "Awaiting payment", "Payment confirmed", "Preparing order", "Ready for collection", "Out for delivery", "Completed", "Cancelled"]
PAYMENT_STATUSES = ["Awaiting payment", "Payment reported", "Processing", "Paid", "Failed", "Cancelled", "Unknown", "Reconciliation required", "Refund requested", "Refund processing", "Partially refunded", "Refunded", "Refund failed"]
COMPLETED_STATUSES = ["Completed"]
ACTIVE_SALE_STATUSES = ["Payment confirmed", "Preparing order", "Ready for collection", "Out for delivery", "Completed"]

def make_reference():
    return "FITY-" + datetime.utcnow().strftime("%y%m%d") + "-" + token_hex(3).upper()

def create_checkout_order(raw_items, customer_name, customer_phone, fulfilment_method, instructions, payment_reference="", payment_proof_filename=""):
    validation = validate_cart(raw_items)
    if not validation.lines:
        return None, validation
    order = Order(
        reference=make_reference(),
        customer_name=(customer_name or "").strip(),
        customer_phone=(customer_phone or "").strip(),
        fulfilment_method=fulfilment_method,
        instructions=(instructions or "").strip(),
        total_cents=validation.total_cents,
        has_unconfirmed_prices=validation.has_unconfirmed_prices,
        payment_reference=(payment_reference or "").strip(),
        payment_proof_filename=(payment_proof_filename or "").strip(),
        payment_status="Payment reported" if ((payment_reference or "").strip() or (payment_proof_filename or "").strip()) else "Awaiting payment",
        status="Order received",
    )
    for line in validation.lines:
        if line.variant:
            line.variant.reserved_quantity = (line.variant.reserved_quantity or 0) + line.quantity
        else:
            line.product.reserved_quantity = (line.product.reserved_quantity or 0) + line.quantity
        item = OrderItem(
            product=line.product,
            product_name=line.product.name,
            variant=line.variant,
            variant_label=line.variant_label,
            sku=(line.variant.sku if line.variant and line.variant.sku else line.product.sku) or "",
            quantity=line.quantity,
            price_cents=line.unit_price_cents,
            subtotal_cents=line.subtotal_cents,
            unit_cost_cents=(line.variant.unit_cost_cents if line.variant and line.variant.unit_cost_cents is not None else line.product.unit_cost_cents),
            size=line.variant.size if line.variant else getattr(line, "size", ""),
            color=line.variant.color if line.variant else getattr(line, "color", ""),
        )
        order.items.append(item)
    db.session.add(order)
    db.session.flush()
    db.session.add(OrderStatusHistory(order=order, field="status", old_value="", new_value=order.status, note="Order created from checkout."))
    db.session.add(PaymentAttempt(order=order, expected_amount_cents=order.total_cents, customer_phone=order.customer_phone, status="pending" if order.payment_status == "Awaiting payment" else "reconciliation_required", payment_method="Manual M-Pesa", initiated_at=datetime.utcnow(), result_description="Manual customer payment reference/proof captured." if order.payment_status == "Payment reported" else "Awaiting customer payment."))
    db.session.commit()
    return order, validation

def update_order_status(order, status=None, payment_status=None, notes=None, admin=None):
    changes = []
    if status and status != order.status:
        if order.status == "Completed" and status not in {"Completed"}:
            raise ValueError("Completed orders cannot be moved backwards from this screen.")
        if status == "Completed" and order.payment_status != "Paid":
            raise ValueError("Confirm payment before marking an order completed.")
        changes.append(("status", order.status, status))
        order.status = status
    if payment_status and payment_status != order.payment_status:
        changes.append(("payment_status", order.payment_status, payment_status))
        order.payment_status = payment_status
    if notes is not None:
        order.notes = notes
    for field, old, new in changes:
        db.session.add(OrderStatusHistory(order=order, admin=admin if getattr(admin, "is_authenticated", False) else None, field=field, old_value=old, new_value=new, note=notes or ""))
    if status == "Completed":
        release_reserved_stock(order, complete_sale=True)
    if status == "Cancelled":
        release_reserved_stock(order, complete_sale=False)
    return changes

def release_reserved_stock(order, complete_sale):
    for item in order.items:
        product = item.product
        variant = item.variant
        stock_item = variant or product
        if not stock_item:
            continue
        reserved_release = min(stock_item.reserved_quantity or 0, item.quantity)
        stock_item.reserved_quantity = max(0, (stock_item.reserved_quantity or 0) - reserved_release)
        if complete_sale:
            new_quantity = max(0, stock_item.stock_quantity - item.quantity)
            if new_quantity != stock_item.stock_quantity:
                stock_item.stock_quantity = new_quantity
        db.session.add(stock_item)

def sales_summary(start=None, end=None):
    query = Order.query.filter(Order.status.in_(ACTIVE_SALE_STATUSES), Order.payment_status == "Paid")
    if start:
        query = query.filter(Order.created_at >= start)
    if end:
        query = query.filter(Order.created_at <= end)
    orders = query.all()
    gross = sum(order.total_cents or 0 for order in orders if not order.has_unconfirmed_prices)
    cogs = 0
    missing_costs = False
    units = 0
    for order in orders:
        for item in order.items:
            units += item.quantity
            if item.unit_cost_cents is None:
                missing_costs = True
            else:
                cogs += item.unit_cost_cents * item.quantity
    expenses = Expense.query.filter_by(voided=False)
    if start:
        expenses = expenses.filter(Expense.incurred_on >= start.date())
    if end:
        expenses = expenses.filter(Expense.incurred_on <= end.date())
    expense_total = sum(expense.amount_cents for expense in expenses.all())
    verified = PaymentTransaction.query.filter_by(status="verified")
    if start:
        verified = verified.filter(PaymentTransaction.created_at >= start)
    if end:
        verified = verified.filter(PaymentTransaction.created_at <= end)
    verified_total = sum(transaction.amount_cents for transaction in verified.all())
    inventory_value = 0
    missing_inventory_costs = 0
    for product in Product.query.all():
        if product.active_variants:
            for variant in product.active_variants:
                cost = variant.unit_cost_cents if variant.unit_cost_cents is not None else product.unit_cost_cents
                if cost is None:
                    if variant.available_quantity:
                        missing_inventory_costs += 1
                else:
                    inventory_value += variant.available_quantity * cost
        elif product.unit_cost_cents is None:
            if product.available_quantity:
                missing_inventory_costs += 1
        else:
            inventory_value += product.available_quantity * product.unit_cost_cents
    unpaid = sum(order.total_cents or 0 for order in Order.query.filter(Order.payment_status != "Paid").all() if not order.has_unconfirmed_prices)
    gross_profit = gross - cogs
    return {
        "gross_cents": gross,
        "net_sales_cents": gross,
        "cogs_cents": cogs,
        "gross_profit_cents": gross_profit,
        "expenses_cents": expense_total,
        "estimated_net_profit_cents": gross_profit - expense_total,
        "order_count": len(orders),
        "units_sold": units,
        "missing_costs": missing_costs,
        "inventory_value_cents": inventory_value,
        "missing_inventory_costs": missing_inventory_costs,
        "outstanding_unpaid_cents": unpaid,
        "verified_mpesa_cents": verified_total,
        "discrepancy_count": PaymentTransaction.query.filter(PaymentTransaction.discrepancy != "").count(),
    }

def order_refunded_cents(order):
    return sum(refund.amount_cents for refund in RefundRequest.query.filter(RefundRequest.order_id == order.id, RefundRequest.status == "completed").all())

def refundable_cents(order):
    if order.payment_status not in {"Paid", "Partially refunded", "Refund requested", "Refund processing"}:
        return 0
    return max(0, (order.total_cents or 0) - order_refunded_cents(order))

def loyalty_customer_key(order):
    return (order.customer_phone or order.customer_name or "").strip().lower()

def award_loyalty_for_order(order, settings, admin=None):
    if not settings.loyalty_enabled or not order.total_cents or order.payment_status != "Paid":
        return 0
    key = loyalty_customer_key(order)
    if not key:
        return 0
    existing = LoyaltyTransaction.query.filter_by(order_id=order.id, transaction_type="earn", status="posted").first()
    if existing:
        return 0
    points = ((order.total_cents or 0) // 10000) * max(0, settings.loyalty_points_per_100 or 0)
    if points <= 0:
        return 0
    db.session.add(LoyaltyTransaction(customer_key=key, order=order, transaction_type="earn", points=points, reason="Verified paid order", admin=admin if getattr(admin, "is_authenticated", False) else None))
    db.session.add(FinancialAuditEvent(admin=admin if getattr(admin, "is_authenticated", False) else None, event_type="loyalty_awarded", entity_type="Order", entity_id=order.id, note=f"Awarded {points} point(s)"))
    return points