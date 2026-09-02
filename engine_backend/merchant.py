"""``MerchantBackend`` reads over the engine's analytics, catalog, and order data.

The write half (stage_* / apply_change / discard_change) is Task 10's; those methods
raise ``NotImplementedError`` here so the class stays instantiable for the reads this
module implements.

Money mirrors ``storefront.py``: figures already computed by ``commerce.analytics`` are
plain floats returned as-is; a figure sourced from an exact-string field is converted
once with ``float(Decimal(...))``. A figure the engine genuinely cannot supply (traffic,
conversion, campaign spend/revenue when no campaign exists) is ``None`` with a ``note``,
never a defaulted zero.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Literal

from merchant_agent.backend import MerchantBackend
from merchant_agent.types import (
    ActorKind,
    AnalysisTable,
    BusinessSnapshot,
    Campaign,
    CampaignDraft,
    DataLimitation,
    InventoryActionItem,
    InventoryAlert,
    Listing,
    ListingDetails,
    ListingFilters,
    MerchantSessionContext,
    MetricPoint,
    MetricSeries,
    OrderIssue,
    PriceUpdateItem,
    PricingContext,
    PromotionDraft,
    StagedChange,
)
from shopping_agent.types import SearchFilters
from stateset_embedded import Commerce

from engine_backend.catalog import CatalogRow, Merchandising, catalog_rows, list_variants
from engine_backend.kernel import KernelClient
from engine_backend.search import search as engine_search
from engine_backend.store import EngineStore

_STATUSES = {"active", "paused", "draft", "out_of_stock"}
_ANALYSIS_ROW_CAP = 100
_ANALYSIS_CHAR_CAP = 8000

_ANALYSIS_SCHEMA = """\
Read-only tables (a single SELECT, capped at 100 rows / 8000 characters):
- orders(id, order_number, customer_id, status, total_amount, currency,
  payment_status, fulfillment_status, tracking_number, created_at, updated_at)
- order_items(id, order_id, product_id, variant_id, sku, name, quantity,
  unit_price, discount, tax_amount, total)
- products(id, name, slug, description, status, created_at, updated_at)
- product_variants(id, product_id, sku, name, price)
- customers(id, email, first_name, last_name, created_at)
- inventory_items(sku, name, quantity_on_hand, quantity_allocated, reorder_point)
"""


def _title(product: Any, variant: Any, variant_count: int) -> str:
    if variant_count > 1 and variant.name:
        return f"{product.name} ({variant.name})"
    return product.name


def _content_quality(merch: Merchandising) -> Literal["good", "needs_work", "poor"]:
    has_description = bool(merch.long_description)
    has_specs = bool(merch.specs)
    if has_description and has_specs:
        return "good"
    if has_description or has_specs:
        return "needs_work"
    return "poor"


def _status(
    product_status: str, in_stock: bool
) -> Literal["active", "paused", "draft", "out_of_stock"]:
    base = product_status if product_status in _STATUSES else "active"
    if base == "active" and not in_stock:
        return "out_of_stock"
    return base


def to_listing(row: CatalogRow, variant_count: int = 1, variant_of: str | None = None) -> Listing:
    """A plain listing, or one variant of a family (``variant_of`` set)."""
    return Listing(
        listing_id=row.variant.sku,
        title=_title(row.product, row.variant, variant_count),
        status=_status(row.product.status, row.stock > 0),
        price=float(Decimal(row.variant.price_exact)),
        stock=int(row.stock),
        category=row.merch.category,
        content_quality=_content_quality(row.merch),
        attributes=dict(row.merch.attributes),
        image_url=row.merch.image_url,
        short_description=row.product.description or None,
        option_values=dict(row.merch.variant_options.get(row.variant.sku, {})),
        variant_of=variant_of,
    )


def _to_family_listing(
    product: Any, variants: list[Any], merch: Merchandising, rows: list[CatalogRow]
) -> Listing:
    by_sku = {row.variant.sku: row for row in rows}
    options: dict[str, list[str]] = {name: [] for name in merch.option_names}
    for variant in variants:
        values = merch.variant_options.get(variant.sku, {})
        for name in merch.option_names:
            value = values.get(name)
            if value is not None and value not in options[name]:
                options[name].append(value)

    prices = [float(Decimal(v.price_exact)) for v in variants]
    total_stock = sum(int(by_sku[v.sku].stock) for v in variants if v.sku in by_sku)
    any_in_stock = any(by_sku[v.sku].stock > 0 for v in variants if v.sku in by_sku)

    return Listing(
        listing_id=product.id,
        title=product.name,
        status=_status(product.status, any_in_stock),
        price=min(prices) if prices else 0.0,
        stock=total_stock,
        category=merch.category,
        content_quality=_content_quality(merch),
        attributes=dict(merch.attributes),
        image_url=merch.image_url,
        short_description=product.description or None,
        options=options,
    )


class EngineMerchant(MerchantBackend):
    def __init__(self, store: EngineStore, kernel: KernelClient) -> None:
        self.store = store
        self.kernel = kernel

    # -- Performance -------------------------------------------------------------

    async def get_business_snapshot(
        self, session: MerchantSessionContext, period: str | None = None
    ) -> BusinessSnapshot:
        summary = await self.store.call(lambda c: c.analytics.sales_summary(period, None))
        alerts = await self.get_inventory_alerts(session)
        return BusinessSnapshot(
            period=period or "current",
            sales=float(summary.total_revenue),
            orders=int(summary.order_count),
            average_order_value=float(summary.average_order_value),
            traffic=None,
            conversion_rate=None,
            alerts={"low_stock": len(alerts)},
            note="traffic and conversion are not tracked by this deployment's engine",
        )

    async def query_metrics(
        self,
        session: MerchantSessionContext,
        metric: str,
        period: str | None = None,
        granularity: str = "day",
        segment: str | None = None,
    ) -> MetricSeries:
        if metric == "revenue":
            if segment is not None:
                return MetricSeries(
                    metric=metric,
                    period=period,
                    segment=segment,
                    granularity=granularity,
                    note="revenue cannot be segmented; the engine's analytics report it store-wide",
                )
            by_period = await self.store.call(
                lambda c: c.analytics.revenue_by_period(period, granularity)
            )
            return MetricSeries(
                metric=metric,
                unit="USD",
                granularity=granularity,
                period=period,
                points=[MetricPoint(date=p.period, value=float(p.revenue)) for p in by_period],
            )
        if metric == "units":
            if segment is not None:
                return MetricSeries(
                    metric=metric,
                    period=period,
                    segment=segment,
                    granularity=granularity,
                    note=(
                        "units sold cannot be segmented by category; "
                        "top_products carries no category"
                    ),
                )
            top = await self.store.call(lambda c: c.analytics.top_products(period, 1000))
            if not top:
                return MetricSeries(
                    metric=metric,
                    period=period,
                    granularity=granularity,
                    note="no product sales are recorded for this period",
                )
            total_units = sum(p.units_sold for p in top)
            return MetricSeries(
                metric=metric,
                unit="units",
                granularity=granularity,
                period=period,
                points=[MetricPoint(date=period or "current", value=float(total_units))],
            )
        return MetricSeries(
            metric=metric,
            period=period,
            granularity=granularity,
            segment=segment,
            note=f"{metric!r} is not a metric the engine's analytics can supply",
        )

    async def get_campaign_performance(
        self, session: MerchantSessionContext, campaign_id: str | None = None
    ) -> list[Campaign]:
        def body(c: Commerce):
            if c.custom_objects.get_type_by_handle("campaign") is None:
                return []
            return c.custom_objects.list_objects(type_handle="campaign")

        records = await self.store.call(body)
        campaigns = [Campaign.model_validate(json.loads(r.values_json)["payload"]) for r in records]
        if campaign_id is not None:
            campaigns = [c for c in campaigns if c.campaign_id == campaign_id]
        return campaigns

    # -- Catalog -------------------------------------------------------------------

    async def search_listings(
        self,
        session: MerchantSessionContext,
        query: str,
        filters: ListingFilters | None = None,
        limit: int = 8,
    ) -> list[Listing]:
        search_filters = SearchFilters(category=filters.category) if filters else None
        rows = await engine_search(self.store, query, search_filters, max(limit * 3, limit))
        listings: list[Listing] = []
        for row in rows:
            variants = await list_variants(self.store, row.product.id)
            if len(variants) > 1:
                all_rows = [
                    r for r in await catalog_rows(self.store) if r.product.id == row.product.id
                ]
                listings.append(_to_family_listing(row.product, variants, row.merch, all_rows))
            else:
                listings.append(to_listing(row, variant_count=1))

        if filters is not None:
            if filters.status is not None:
                listings = [item for item in listings if item.status == filters.status]
            if filters.max_stock is not None:
                listings = [item for item in listings if item.stock <= filters.max_stock]
            if filters.content_quality is not None:
                listings = [
                    item for item in listings if item.content_quality == filters.content_quality
                ]
            if filters.sort == "stock_asc":
                listings.sort(key=lambda item: item.stock)
            elif filters.sort == "price_desc":
                listings.sort(key=lambda item: -item.price)
            elif filters.sort == "price_asc":
                listings.sort(key=lambda item: item.price)

        return listings[:limit]

    async def get_listing(
        self, session: MerchantSessionContext, listing_id: str
    ) -> ListingDetails | None:
        rows = await catalog_rows(self.store)

        family_rows = [r for r in rows if r.product.id == listing_id]
        if family_rows:
            product = family_rows[0].product
            merch = family_rows[0].merch
            variants = await list_variants(self.store, product.id)
            family = _to_family_listing(product, variants, merch, family_rows)
            variant_listings = [
                to_listing(r, variant_count=len(family_rows), variant_of=product.id)
                for r in family_rows
            ]
            return ListingDetails(
                **family.model_dump(),
                long_description=merch.long_description,
                variants=variant_listings,
            )

        variant_row = next((r for r in rows if r.variant.sku == listing_id), None)
        if variant_row is not None:
            variant_count = len([r for r in rows if r.product.id == variant_row.product.id])
            listing = to_listing(
                variant_row, variant_count=variant_count, variant_of=variant_row.product.id
            )
            return ListingDetails(
                **listing.model_dump(),
                long_description=variant_row.merch.long_description,
                variants=[],
            )

        return None

    # -- Inventory and order health --------------------------------------------------

    async def get_inventory_alerts(self, session: MerchantSessionContext) -> list[InventoryAlert]:
        low_stock = await self.store.call(lambda c: c.analytics.low_stock_items(None))
        if not low_stock:
            return []
        rows = await catalog_rows(self.store)
        by_sku = {row.variant.sku: row for row in rows}

        alerts: list[InventoryAlert] = []
        for item in low_stock:
            row = by_sku.get(item.sku)
            option_values = row.merch.variant_options.get(item.sku, {}) if row else {}
            variant_of = row.product.id if row else None
            title = row.product.name if row else item.name
            alerts.append(
                InventoryAlert(
                    listing_id=item.sku,
                    title=title,
                    kind="low_stock",
                    option_values=dict(option_values),
                    variant_of=variant_of,
                    stock=int(item.available if item.available is not None else item.on_hand),
                    threshold=int(item.reorder_point) if item.reorder_point is not None else None,
                    days_of_cover=item.days_of_stock,
                    sales_last_30d=None,
                    storefront_visible=None,
                )
            )
        return alerts

    async def get_order_issues(self, session: MerchantSessionContext) -> list[OrderIssue]:
        from datetime import UTC, datetime

        reference = session.now or datetime.now(UTC)
        orders = await self.store.call(lambda c: c.orders.list())

        issues: list[OrderIssue] = []
        for order in orders:
            if order.fulfillment_status not in ("unfulfilled", "pending"):
                continue
            opened_at = datetime.fromisoformat(order.created_at.replace("Z", "+00:00"))
            age_hours = (reference - opened_at).total_seconds() / 3600
            if age_hours < 48:
                continue
            issues.append(
                OrderIssue(
                    issue_id=f"issue-{order.id}",
                    order_id=order.id,
                    kind="delayed",
                    summary=(
                        f"Order {order.order_number} has been {order.fulfillment_status} for "
                        f"over {int(age_hours // 24)} day(s)"
                    ),
                    opened_at=opened_at,
                )
            )
        return issues

    # -- Pricing ---------------------------------------------------------------------

    async def get_pricing_context(
        self, session: MerchantSessionContext, listing_id: str
    ) -> PricingContext | None:
        rows = await catalog_rows(self.store)

        family_rows = [r for r in rows if r.product.id == listing_id]
        if family_rows:
            variant_contexts = [self._variant_pricing_context(r) for r in family_rows]
            prices = [ctx.current_price for ctx in variant_contexts]
            return PricingContext(
                listing_id=listing_id,
                current_price=min(prices) if prices else 0.0,
                unit_cost=family_rows[0].merch.unit_cost,
                variants=variant_contexts,
            )

        variant_row = next((r for r in rows if r.variant.sku == listing_id), None)
        if variant_row is not None:
            return self._variant_pricing_context(variant_row)

        return None

    def _variant_pricing_context(self, row: CatalogRow) -> PricingContext:
        price = float(Decimal(row.variant.price_exact))
        unit_cost = row.merch.unit_cost
        margin_pct = None
        if unit_cost is not None and price > 0:
            margin_pct = ((price - unit_cost) / price) * 100
        return PricingContext(
            listing_id=row.variant.sku,
            current_price=price,
            unit_cost=unit_cost,
            margin_pct=margin_pct,
            min_price_basis="cost" if unit_cost is not None else None,
            option_values=dict(row.merch.variant_options.get(row.variant.sku, {})),
        )

    # -- Analysis ----------------------------------------------------------------

    async def execute_analysis_query(
        self, session: MerchantSessionContext, sql: str
    ) -> AnalysisTable | None:
        statement = sql.strip()
        if not statement:
            raise ValueError("empty query")
        if ";" in statement.rstrip(";"):
            raise ValueError("only a single statement is allowed")
        first_word = statement.split(None, 1)[0].lower() if statement else ""
        if first_word != "select":
            raise ValueError("only SELECT statements are allowed")

        connection = self.store.readonly_sql()
        cursor = connection.execute(statement.rstrip(";"))
        columns = [d[0] for d in cursor.description] if cursor.description else []
        fetched = cursor.fetchmany(_ANALYSIS_ROW_CAP + 1)
        truncated = len(fetched) > _ANALYSIS_ROW_CAP
        fetched = fetched[:_ANALYSIS_ROW_CAP]

        rows: list[list[Any]] = []
        chars = 0
        for record in fetched:
            values = list(record)
            row_chars = sum(len(str(v)) for v in values)
            if chars + row_chars > _ANALYSIS_CHAR_CAP:
                truncated = True
                break
            chars += row_chars
            rows.append(values)

        return AnalysisTable(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            note="results are capped at 100 rows and 8000 characters" if truncated else None,
        )

    async def get_analysis_schema(self, session: MerchantSessionContext) -> str | None:
        return _ANALYSIS_SCHEMA

    # -- Merchant context ----------------------------------------------------------

    async def get_merchant_context(self, session: MerchantSessionContext) -> dict[str, Any] | None:
        return {
            "store_name": "ACME Supply",
            "reporting_period": "current",
            "limitations": [
                DataLimitation(
                    source="campaigns",
                    note=(
                        "Campaigns are not managed by the engine; "
                        "no channel spend or revenue is available."
                    ),
                )
            ],
        }

    # -- Staged writes (Task 10) ------------------------------------------------------

    async def stage_listing_update(
        self,
        session: MerchantSessionContext,
        listing_id: str,
        fields: dict[str, Any],
        note: str | None = None,
    ) -> StagedChange:
        raise NotImplementedError

    async def stage_price_update(
        self,
        session: MerchantSessionContext,
        items: list[PriceUpdateItem],
        note: str | None = None,
    ) -> StagedChange:
        raise NotImplementedError

    async def stage_inventory_action(
        self,
        session: MerchantSessionContext,
        items: list[InventoryActionItem],
        note: str | None = None,
    ) -> StagedChange:
        raise NotImplementedError

    async def stage_promotion(
        self, session: MerchantSessionContext, promotion: PromotionDraft
    ) -> StagedChange:
        raise NotImplementedError

    async def stage_campaign(
        self, session: MerchantSessionContext, campaign: CampaignDraft
    ) -> StagedChange:
        raise NotImplementedError

    async def get_pending_changes(self, session: MerchantSessionContext) -> list[StagedChange]:
        raise NotImplementedError

    async def apply_change(self, session: MerchantSessionContext, change_id: str) -> StagedChange:
        raise NotImplementedError

    async def discard_change(
        self,
        session: MerchantSessionContext,
        change_id: str,
        actor_kind: ActorKind = ActorKind.OPERATOR,
    ) -> StagedChange:
        raise NotImplementedError
