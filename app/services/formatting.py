def money(cents, currency="KSh"):
    if cents is None:
        return "Price to be confirmed"
    amount = cents // 100
    remainder = cents % 100
    if remainder:
        return f"{currency} {amount}.{remainder:02d}"
    return f"{currency} {amount}"

def parse_money(value):
    if value is None or str(value).strip() == "":
        return None
    cleaned = str(value).replace(",", "").strip()
    return int(round(float(cleaned) * 100))

def split_options(value):
    return [item.strip() for item in (value or "").split(",") if item.strip()]
