import sqlite3

import pytest
from merchant_agent.types import MerchantSessionContext

from engine_backend.merchant import EngineMerchant


def session():
    return MerchantSessionContext(
        session_id="m-1", merchant_id="acme", operator="user:acme-operator"
    )


async def test_snapshot_figures_come_from_the_engine(store, kernel):
    snapshot = await EngineMerchant(store, kernel).get_business_snapshot(session())
    assert snapshot.sales is not None


async def test_listings_carry_stock_and_price(store, kernel):
    listings = await EngineMerchant(store, kernel).search_listings(session(), "tent")
    assert listings
    assert listings[0].price > 0
    assert listings[0].stock >= 0


async def test_listing_details_of_a_family_carry_variants(store, kernel):
    backend = EngineMerchant(store, kernel)
    family_id = (await backend.search_listings(session(), "tent"))[0].listing_id
    details = await backend.get_listing(session(), family_id)
    assert details is not None
    assert {v.listing_id for v in details.variants} == {"TENT-RIDGE-GRN", "TENT-RIDGE-TAN"}


async def test_inventory_alerts_flag_the_low_sku(store, kernel):
    alerts = await EngineMerchant(store, kernel).get_inventory_alerts(session())
    assert any(a.listing_id == "TENT-RIDGE-TAN" for a in alerts)


async def test_pricing_context_reports_unit_cost(store, kernel):
    context = await EngineMerchant(store, kernel).get_pricing_context(session(), "TENT-RIDGE-GRN")
    assert context is not None
    assert context.unit_cost == 128.00


async def test_analysis_query_is_select_only_and_capped(store, kernel):
    backend = EngineMerchant(store, kernel)
    table = await backend.execute_analysis_query(session(), "SELECT COUNT(*) AS n FROM orders")
    assert table is not None and table.rows
    assert await backend.get_analysis_schema(session())

    with pytest.raises(ValueError, match="only SELECT statements are allowed"):
        await backend.execute_analysis_query(session(), "DELETE FROM orders")

    with pytest.raises(ValueError, match="single statement"):
        await backend.execute_analysis_query(session(), "SELECT 1; DELETE FROM orders")


def test_the_readonly_connection_itself_refuses_a_write(store):
    """The heuristic in execute_analysis_query is the first line of defense, but the
    brief calls the mode=ro connection the check that actually holds. Prove that
    directly: bypass the heuristic entirely (call the connection, not the method) and
    confirm the engine's own read-only connection raises rather than writing."""
    connection = store.readonly_sql()
    with pytest.raises(sqlite3.OperationalError, match="readonly database"):
        connection.execute("DELETE FROM orders")


async def test_order_issues_flag_an_order_past_the_48_hour_threshold(store, kernel):
    """`get_order_issues` compares each unfulfilled order's age against `session.now`,
    the reference time the caller supplies. A freshly seeded store's orders are minutes
    old, so the threshold is exercised by asking the question as of three days later --
    the same branch, reached through the interface's own reference-time parameter rather
    than by rewriting the engine's `created_at` behind its back."""
    from datetime import UTC, datetime, timedelta

    backend = EngineMerchant(store, kernel)

    fresh = MerchantSessionContext(
        session_id="m-1", merchant_id="acme", operator="user:acme-operator", now=datetime.now(UTC)
    )
    assert await backend.get_order_issues(fresh) == []

    later = MerchantSessionContext(
        session_id="m-1",
        merchant_id="acme",
        operator="user:acme-operator",
        now=datetime.now(UTC) + timedelta(days=3),
    )
    issues = await backend.get_order_issues(later)
    assert issues, "no unfulfilled order tripped the 48-hour threshold"
    assert {i.kind for i in issues} == {"delayed"}
    assert all(i.order_id and i.issue_id.startswith("issue-") for i in issues)
    assert all("day(s)" in i.summary for i in issues)
