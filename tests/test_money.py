"""The money seam: `engine_backend/money.py` is the only place a money figure changes
representation, and the exact string is the authoritative form."""

from decimal import Decimal

import pytest

from engine_backend import money


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (219.00, "219.00"),
        ("219", "219.00"),
        (Decimal("208.049999"), "208.05"),
        ("208.045", "208.05"),  # half-up, not banker's rounding
        ("0.005", "0.01"),
        (49, "49.00"),
    ],
)
def test_exact_quantizes_to_two_places_half_up(value, expected):
    assert money.exact(value) == expected


def test_a_float_does_not_leak_its_binary_expansion():
    # Decimal(219.0 * 0.95) is 208.04999999999998...; str() first keeps it a currency
    # amount, and the point of the module is that this path is never taken at all.
    assert money.exact(219.00) == "219.00"
    assert money.to_decimal(0.1) == Decimal("0.1")


@pytest.mark.parametrize(
    ("price", "pct", "expected"),
    [
        ("219.00", 5.0, "208.05"),  # the float computation gives 208.04999999999998
        ("219.00", 10.0, "197.10"),
        ("159.00", 15.0, "135.15"),
        ("49.00", 33.0, "32.83"),
        ("89.00", 0.0, "89.00"),
    ],
)
def test_discounted_is_a_currency_amount(price, pct, expected):
    assert money.discounted(price, pct) == expected


def test_every_seeded_price_and_discount_combination_stays_a_currency_amount():
    prices = ["219.00", "159.00", "169.00", "49.00", "89.00", "79.00", "45.00"]
    for price in prices:
        for pct in range(0, 51, 5):
            result = money.discounted(price, float(pct))
            assert result == money.exact(result)
            assert len(result.split(".")[1]) == 2


def test_to_float_is_the_display_edge():
    assert money.to_float("208.05") == 208.05
    assert isinstance(money.to_float("208.05"), float)
