"""The one place a money figure changes representation.

The engine stores money as an exact decimal string (``product_variants.price`` is a
``TEXT`` column, and every ``*_exact`` field the binding returns is a string). **That
string form is authoritative**: it is what the engine reads back, what a cart line and
an order total are computed from, and what this repo's own writes must put back. A
``float`` is a display convenience only — never an intermediate for arithmetic that is
later persisted, because binary floating point cannot represent a currency amount
exactly (``219.00 * 0.95`` is ``208.04999999999998``).

The rule this module exists to hold: arithmetic on money happens in :class:`~decimal.Decimal`
and lands back on a two-place string via :func:`exact`; conversion to ``float`` happens
once, at the display edge, via :func:`to_float`.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

CENTS = Decimal("0.01")


def to_decimal(value: str | int | float | Decimal) -> Decimal:
    """The engine's exact string (or any scalar amount) as a ``Decimal``.

    A ``float`` goes through ``str`` first, so ``219.0`` becomes ``Decimal("219.0")``
    rather than the full binary expansion.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    return Decimal(value)


def exact(value: str | int | float | Decimal) -> str:
    """``value`` as the authoritative two-place decimal string, half-up.

    This is the only form written back to an engine money column.
    """
    return str(to_decimal(value).quantize(CENTS, rounding=ROUND_HALF_UP))


def to_float(value: str | int | float | Decimal) -> float:
    """An exact amount as a ``float``, **for display only**.

    Every ``price``/``total`` field on the ``commerce-agents`` types is a ``float``, so
    this conversion is unavoidable at the interface; it must be the last thing that
    happens to a figure, never a step on the way back into the engine.
    """
    return float(to_decimal(value))


def discounted(price_exact: str, discount_pct: float) -> str:
    """A promotion price: ``price_exact`` less ``discount_pct`` percent, quantized.

    Computed entirely in ``Decimal`` from the engine's own string, so the result is a
    currency amount and not the nearest binary float to one.
    """
    factor = Decimal(1) - (to_decimal(discount_pct) / Decimal(100))
    return exact(to_decimal(price_exact) * factor)


__all__ = ["CENTS", "discounted", "exact", "to_decimal", "to_float"]
