"""Every money value the seed writes reaches the engine through `engine_backend.money`,
never as an un-quantized `float`. The engine's own product-variant binding has no
currency-scale check: `CreateProductVariantInput(price=2.675, ...)` persists the
three-decimal string `"2.675"`, not a rejected input. `Payments.create_exact` does
validate, so a payment amount is asserted literally in its two-place form; a variant
price and an order total go through `money.exact` before comparison, exactly as
`engine_backend.merchant` and `engine_backend.storefront` already do on every read --
but they must still carry no more precision than a currency amount holds.
"""

from decimal import Decimal

from engine_backend import money
from engine_backend.catalog import catalog_rows

# Every seeded variant's price, in dollars -- the source of truth this test checks the
# store against, independent of `engine_backend/seed.py`'s own arithmetic.
_EXPECTED_PRICES = {
    "TENT-RIDGE-GRN": "219.00",
    "TENT-RIDGE-TAN": "219.00",
    "BAG-SUMMIT-REG": "159.00",
    "BAG-SUMMIT-LNG": "169.00",
    "STOVE-TRAIL-1": "49.00",
    "PACK-SWITCH-SLT": "89.00",
    "PACK-SWITCH-MOS": "89.00",
    "FILTER-CLEAR-1": "79.00",
    "LAMP-BEACON-BLK": "45.00",
    "LAMP-BEACON-ORG": "45.00",
    # Split from a $250.70 case of four: 62.675, not cent-exact. This is the one that
    # would have caught the latent problem -- see the module docstring.
    "TABLE-CAMP-FOLD": "62.68",
}


def _no_precision_was_lost_or_invented(price_exact: str) -> bool:
    """`price_exact` already carries no more than two decimal places.

    A currency amount computed correctly holds still when parsed and re-quantized; one
    seeded with extra precision (`"2.675"`, `"33.335"`) does not -- `Decimal("2.675") !=
    Decimal("2.68")`. This is the check a plain `len(price_exact.split("."))[1]) == 2`
    on the raw string cannot make: the engine formats a *clean* two-place value like
    `219.00` as the trimmed `"219"`, which is not wrong, just undecorated.
    """
    return Decimal(price_exact) == Decimal(money.exact(price_exact))


async def test_every_seeded_variant_price_is_an_exact_currency_amount(store):
    rows = await catalog_rows(store)
    by_sku = {row.variant.sku: row for row in rows}
    assert by_sku.keys() == _EXPECTED_PRICES.keys()
    for sku, expected in _EXPECTED_PRICES.items():
        price_exact = by_sku[sku].variant.price_exact
        assert _no_precision_was_lost_or_invented(price_exact), (
            f"{sku}: {price_exact!r} carries more than two decimal places"
        )
        assert money.exact(price_exact) == expected


async def test_the_seeded_order_total_is_an_exact_currency_amount(store):
    orders = await store.call(lambda c: c.orders.list())
    tent_order = next(o for o in orders if o.total_amount_exact and float(o.total_amount) == 219.0)
    assert _no_precision_was_lost_or_invented(tent_order.total_amount_exact)
    assert money.exact(tent_order.total_amount_exact) == "219.00"


async def test_the_seeded_payment_amount_is_a_two_place_decimal_string(store):
    payments = await store.call(lambda c: c.payments.list())
    assert payments, "seed_store did not create a payment"
    payment = payments[0]
    # `Payments.create_exact` is validated at write time, so this one *is* asserted
    # literally -- no `money.exact` needed to make it well-formed.
    assert payment.amount_exact == "219.00"
