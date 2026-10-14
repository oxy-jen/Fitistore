from dataclasses import dataclass
from app.models import Product, ProductVariant

@dataclass
class CartLine:
    product: Product
    quantity: int
    subtotal_cents: int | None
    message: str = ""
    variant: ProductVariant | None = None

    @property
    def unit_price_cents(self):
        if self.variant:
            return self.variant.effective_price_cents
        return self.product.price_cents

    @property
    def stock_quantity(self):
        if self.variant:
            return self.variant.available_quantity
        return self.product.available_quantity

    @property
    def variant_label(self):
        return self.variant.label if self.variant else ""

@dataclass
class CartIssue:
    product: Product | None
    quantity: int
    reason: str
    variant: ProductVariant | None = None

    @property
    def stock_quantity(self):
        if self.variant:
            return self.variant.available_quantity
        if self.product:
            return self.product.available_quantity
        return 0

    @property
    def variant_label(self):
        return self.variant.label if self.variant else ""

@dataclass
class CartValidation:
    lines: list
    errors: list
    total_cents: int | None
    has_unconfirmed_prices: bool
    issues: list

def normalize_cart(raw_items):
    normalized = []
    for item in raw_items or []:
        try:
            product_id = int(item.get("product_id") or item.get("id"))
            quantity = int(item.get("quantity", 1))
            variant_id = item.get("variant_id") or item.get("variantId") or None
            variant_id = int(variant_id) if variant_id else None
        except (TypeError, ValueError):
            continue
        if quantity > 0:
            normalized.append({
                "product_id": product_id,
                "variant_id": variant_id,
                "quantity": min(quantity, 99),
                "size": (item.get("size", "") or "").strip(),
                "color": (item.get("color", "") or "").strip(),
            })
    return normalized

def resolve_variant(product, item):
    active_variants = [variant for variant in product.variants if variant.active]
    if item.get("variant_id"):
        variant = ProductVariant.query.filter_by(id=item["variant_id"], product_id=product.id, active=True).first()
        return variant, "Choose an available product variant." if not variant else ""
    if active_variants:
        for variant in active_variants:
            if (not item.get("size") or (variant.size or "") == item.get("size")) and (not item.get("color") or (variant.color or "") == item.get("color")):
                return variant, ""
        return None, "Choose a valid size and colour combination."
    return None, ""

def validate_cart(raw_items):
    lines = []
    errors = []
    issues = []
    total = 0
    has_unknown = False
    for item in normalize_cart(raw_items):
        product = Product.query.get(item["product_id"])
        if not product or not product.active:
            reason = "This product is no longer available."
            errors.append("A product in your cart is no longer available.")
            issues.append(CartIssue(product=product, variant=None, quantity=item["quantity"], reason=reason))
            continue
        variant, variant_error = resolve_variant(product, item)
        if variant_error:
            errors.append(f"{product.name}: {variant_error}")
            issues.append(CartIssue(product=product, variant=variant, quantity=item["quantity"], reason=variant_error))
            continue
        available = variant.available_quantity if variant else product.available_quantity
        requested = item["quantity"]
        display_name = f"{product.name} ({variant.label})" if variant else product.name
        if available <= 0:
            reason = "Currently out of stock."
            errors.append(f"{display_name} is currently out of stock.")
            issues.append(CartIssue(product=product, variant=variant, quantity=requested, reason=reason))
            continue
        quantity = min(requested, available)
        if quantity < requested:
            errors.append(f"Only {available} unit(s) of {display_name} are currently available.")
        unit_price = variant.effective_price_cents if variant else product.price_cents
        subtotal = unit_price * quantity if unit_price is not None else None
        if subtotal is None:
            has_unknown = True
        else:
            total += subtotal
        lines.append(CartLine(product=product, variant=variant, quantity=quantity, subtotal_cents=subtotal))
    return CartValidation(lines=lines, errors=errors, total_cents=None if has_unknown else total, has_unconfirmed_prices=has_unknown, issues=issues)
