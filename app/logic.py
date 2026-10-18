"""Pure money calculations. Every amount is an integer number of kopecks."""


def price_with_discount(price: int, has_promo: bool) -> tuple[int, int]:
    # Tariff prices are in full rubles; the percent discount is exact in kopecks.
    discount = price * 10 // 100 if has_promo else 0
    return price - discount, discount


def split_installments(amount: int, months: int) -> list[int]:
    """Split amount exactly; distribute remaining kopecks to earliest payments."""
    base, remaining = divmod(amount, months)
    return [base + (index < remaining) for index in range(months)]


ALLOWED_TRANSITIONS = {
    "pending": {"succeeded", "failed"},
    "succeeded": {"refunded"},
    "failed": set(),
    "refunded": set(),
}
