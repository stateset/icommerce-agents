"""``MerchantBackend`` over the engine: analytics, catalog and order reads, and the
staged-write half (``stage_*`` / ``apply_change`` / ``discard_change``), whose applies
are the only place this package mutates live state.

Money mirrors ``storefront.py`` and goes through ``engine_backend.money``: figures
already computed by ``commerce.analytics`` are plain floats returned as-is; a figure
sourced from an exact-string field is converted once, at the display edge, with
``money.to_float``. A price a change *writes back* stays an exact two-place decimal
string end to end — computed in ``Decimal`` and never through a ``float``. A figure the
engine genuinely cannot supply (traffic, conversion, campaign spend/revenue when no
campaign exists) is ``None`` with a ``note``, never a defaulted zero.
"""

from __future__ import annotations

import json
from typing import Any, Literal
from uuid import uuid4

from commerce_common.fencing import truncate_display
from merchant_agent.backend import MerchantBackend
from merchant_agent.changes import ChangeNotApplicable, GuardrailViolation, check_guardrails
from merchant_agent.config import MerchantAgentConfig
from merchant_agent.types import (
    ActorKind,
    AnalysisTable,
    BusinessSnapshot,
    Campaign,
    CampaignDraft,
    ChangeItem,
    ChangeKind,
    ChangeStatus,
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

from engine_backend import custom_objects, money, staging
from engine_backend.catalog import (
    CatalogRow,
    Merchandising,
    catalog_rows,
    list_variants,
    read_merchandising,
    write_merchandising,
)
from engine_backend.kernel import KernelClient
from engine_backend.search import search as engine_search
from engine_backend.store import EngineStore

PROMOTION_TYPE = "promotion"
CAMPAIGN_TYPE = "campaign"

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
        price=money.to_float(row.variant.price_exact),
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

    prices = [money.to_float(v.price_exact) for v in variants]
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
        self.config = MerchantAgentConfig()
        self._approved: set[str] = set()

    # -- Host approval surface ---------------------------------------------------

    def approve(self, change_id: str, approved_by: str) -> None:
        """Record that ``approved_by`` (the host's operator, never a tool argument) has
        approved ``change_id``. A preview card and a chat approval approve nothing —
        only this method, called from the host's own approval surface, does."""
        self._approved.add(change_id)

    @property
    def approved_ids(self) -> set[str]:
        return set(self._approved)

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
        def body(c: Commerce) -> list[Any]:
            if c.custom_objects.get_type_by_handle(CAMPAIGN_TYPE) is None:
                return []
            return custom_objects.list_payloads(c, CAMPAIGN_TYPE)

        payloads = await self.store.call(body)
        campaigns = [Campaign.model_validate(p) for p in payloads]
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
        # One catalog scan for the whole result set, not one per family result.
        every_row: list[CatalogRow] | None = None
        for row in rows:
            variants = await list_variants(self.store, row.product.id)
            if len(variants) > 1:
                if every_row is None:
                    every_row = await catalog_rows(self.store)
                all_rows = [r for r in every_row if r.product.id == row.product.id]
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
        price = money.to_float(row.variant.price_exact)
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

    # -- Staged writes ----------------------------------------------------------------

    async def _resolve_variant_row(self, listing_id: str) -> CatalogRow | None:
        rows = await catalog_rows(self.store)
        return next((r for r in rows if r.variant.sku == listing_id), None)

    async def _resolve_product_and_merch(self, listing_id: str) -> tuple[Any, Merchandising] | None:
        """``listing_id`` may name a plain listing/variant or a product family."""
        product = await self.store.call(lambda c: c.products.get(listing_id))
        if product is not None:
            return product, await read_merchandising(self.store, listing_id)
        row = await self._resolve_variant_row(listing_id)
        if row is not None:
            return row.product, row.merch
        return None

    async def stage_listing_update(
        self,
        session: MerchantSessionContext,
        listing_id: str,
        fields: dict[str, Any],
        note: str | None = None,
    ) -> StagedChange:
        """Content and attribute edits. ``description`` writes the ``Product`` record's
        own description column (the engine's binding has no ``products.update``, so this
        goes through the direct SQL path in :meth:`_apply_listing_update`, guarded the
        same way ``catalog.py`` reads variants the binding does not expose); every other
        known field maps onto its ``Merchandising`` counterpart in the listing's own
        custom object. ``price`` and ``stock`` are refused here by upstream's
        guardrails — they are staged as a price update or an inventory action instead."""
        resolved = await self._resolve_product_and_merch(listing_id)
        if resolved is None:
            raise ChangeNotApplicable(f"no listing with id {listing_id!r}")
        product, merch = resolved

        field_map = {
            "brand": "brand",
            "category": "category",
            "image_url": "image_url",
            "long_description": "long_description",
            "unit_cost": "unit_cost",
        }
        change_items: list[ChangeItem] = []
        for field, value in fields.items():
            if field == "description":
                before = product.description
            elif field in field_map:
                before = getattr(merch, field_map[field])
            elif field in ("attributes", "specs") and isinstance(getattr(merch, field, None), dict):
                before = dict(getattr(merch, field))
            elif field in ("labels",):
                before = list(merch.labels)
            else:
                before = merch.attributes.get(field)
            change_items.append(
                ChangeItem(target=listing_id, field=field, before=before, after=value)
            )

        violations = check_guardrails(ChangeKind.LISTING_UPDATE, change_items, self.config)
        if violations:
            raise GuardrailViolation(violations)

        summary = truncate_display(f"Update listing content for {listing_id}", 200)
        change = staging.new_change(
            ChangeKind.LISTING_UPDATE, summary, change_items, session.operator
        )
        await staging.save(self.store, change)
        return change

    async def stage_price_update(
        self,
        session: MerchantSessionContext,
        items: list[PriceUpdateItem],
        note: str | None = None,
    ) -> StagedChange:
        rows = await catalog_rows(self.store)
        by_sku = {row.variant.sku: row for row in rows}
        change_items: list[ChangeItem] = []
        for item in items:
            row = by_sku.get(item.listing_id)
            if row is None:
                raise ChangeNotApplicable(f"no listing or variant with id {item.listing_id!r}")
            # Both sides stay exact decimal strings: `after` is what `_apply_price_update`
            # writes into `product_variants.price`, and upstream's guardrails read either
            # side with `float(...)`, which parses a string just as well.
            change_items.append(
                ChangeItem(
                    target=item.listing_id,
                    field="price",
                    before=money.exact(row.variant.price_exact),
                    after=money.exact(item.new_price),
                )
            )

        violations = check_guardrails(ChangeKind.PRICE_UPDATE, change_items, self.config)
        if violations:
            raise GuardrailViolation(violations)

        summary = truncate_display(
            "Update price for " + ", ".join(i.listing_id for i in items), 200
        )
        change = staging.new_change(
            ChangeKind.PRICE_UPDATE, summary, change_items, session.operator, currency="USD"
        )
        await staging.save(self.store, change)
        return change

    async def stage_inventory_action(
        self,
        session: MerchantSessionContext,
        items: list[InventoryActionItem],
        note: str | None = None,
    ) -> StagedChange:
        """A restock's ``before``/``after`` are stock levels; a pause/reactivation's are
        the product's status. Only a restock of a SKU with no inventory item yet is
        governed (``inventory.item.create``) — that determination is made again, against
        live state, at apply time; see :meth:`apply_change`."""
        change_items: list[ChangeItem] = []
        for item in items:
            if item.action == "restock":
                stock = await self.store.call(
                    lambda c, sku=item.listing_id: c.inventory.get_stock(sku)
                )
                before = float(stock.total_available) if stock is not None else 0.0
                quantity = item.quantity or 0
                change_items.append(
                    ChangeItem(
                        target=item.listing_id,
                        field="stock",
                        before=before,
                        after=before + quantity,
                    )
                )
            else:
                resolved = await self._resolve_product_and_merch(item.listing_id)
                if resolved is None:
                    raise ChangeNotApplicable(f"no listing with id {item.listing_id!r}")
                product, _merch = resolved
                before_status = product.status
                after_status = "active" if item.action == "activate" else "paused"
                change_items.append(
                    ChangeItem(
                        target=item.listing_id,
                        field="status",
                        before=before_status,
                        after=after_status,
                    )
                )

        violations = check_guardrails(ChangeKind.INVENTORY_ACTION, change_items, self.config)
        if violations:
            raise GuardrailViolation(violations)

        summary = truncate_display(
            "Inventory action for " + ", ".join(i.listing_id for i in items), 200
        )
        change = staging.new_change(
            ChangeKind.INVENTORY_ACTION, summary, change_items, session.operator
        )
        await staging.save(self.store, change)
        return change

    async def stage_promotion(
        self, session: MerchantSessionContext, promotion: PromotionDraft
    ) -> StagedChange:
        rows = await catalog_rows(self.store)
        by_sku = {row.variant.sku: row for row in rows}
        change_items: list[ChangeItem] = []
        for listing_id in promotion.listing_ids:
            row = by_sku.get(listing_id)
            if row is None:
                raise ChangeNotApplicable(f"no listing or variant with id {listing_id!r}")
            # The discount is applied in `Decimal`, to the engine's own exact string,
            # and quantized back to two places. Computing it in `float` would stage (and
            # then persist) a figure like 208.04999999999998 as this variant's price.
            before = money.exact(row.variant.price_exact)
            after = money.discounted(before, promotion.discount_pct)
            change_items.append(
                ChangeItem(target=listing_id, field="price", before=before, after=after)
            )

        violations = check_guardrails(ChangeKind.PROMOTION, change_items, self.config)
        if violations:
            raise GuardrailViolation(violations)

        summary = truncate_display(f"Promotion {promotion.name!r}", 200)
        change = staging.new_change(
            ChangeKind.PROMOTION,
            summary,
            change_items,
            session.operator,
            currency="USD",
            guardrail_notes=[promotion.model_dump_json()],
        )
        await staging.save(self.store, change)
        return change

    async def stage_campaign(
        self, session: MerchantSessionContext, campaign: CampaignDraft
    ) -> StagedChange:
        existing: Campaign | None = None
        if campaign.campaign_id is not None:
            matches = await self.get_campaign_performance(session, campaign.campaign_id)
            existing = matches[0] if matches else None

        change_items: list[ChangeItem] = []
        if campaign.budget is not None:
            change_items.append(
                ChangeItem(
                    target=campaign.campaign_id or campaign.name,
                    field="budget",
                    before=existing.budget if existing else None,
                    after=campaign.budget,
                )
            )
        else:
            change_items.append(
                ChangeItem(
                    target=campaign.campaign_id or campaign.name,
                    field="name",
                    before=existing.name if existing else None,
                    after=campaign.name,
                )
            )

        violations = check_guardrails(ChangeKind.CAMPAIGN, change_items, self.config)
        if violations:
            raise GuardrailViolation(violations)

        summary = truncate_display(f"Campaign {campaign.name!r}", 200)
        change = staging.new_change(
            ChangeKind.CAMPAIGN,
            summary,
            change_items,
            session.operator,
            currency="USD" if campaign.budget is not None else None,
            guardrail_notes=[campaign.model_dump_json()],
        )
        await staging.save(self.store, change)
        return change

    async def get_pending_changes(self, session: MerchantSessionContext) -> list[StagedChange]:
        return await staging.pending(self.store)

    # -- Apply dispatch (the one place that mutates) ------------------------------

    async def apply_change(self, session: MerchantSessionContext, change_id: str) -> StagedChange:
        change = await staging.load(self.store, change_id)
        if change is None:
            raise ChangeNotApplicable(f"no change with id {change_id!r} to apply")
        if change.status is not ChangeStatus.STAGED:
            raise ChangeNotApplicable(
                f"change {change_id} is {change.status.value}, not staged — nothing to apply"
            )
        if change_id not in self._approved:
            raise ChangeNotApplicable(f"change {change_id} has not been approved")

        # Guardrails run again against the config in force now, not the one in force at
        # stage time.
        violations = check_guardrails(change.kind, change.items, self.config)
        if violations:
            raise GuardrailViolation(violations)

        extra_notes = await self._apply_write(change, session.operator)

        applied = change.model_copy(
            update={
                "status": ChangeStatus.APPLIED,
                "applied_at": staging.datetime.now(staging.UTC),
                "applied_by": session.operator,
                "guardrail_notes": [*change.guardrail_notes, *extra_notes],
            }
        )
        await staging.save(self.store, applied)
        return applied

    async def _apply_write(self, change: StagedChange, operator: str) -> list[str]:
        """The platform write for one staged change. Returns extra ``guardrail_notes``
        recording the evidence: an activity-log id for an ungoverned direct binding
        write, or a sealed kernel receipt id for a governed command. Raises, and leaves
        the change staged, on a failed write."""
        if change.kind is ChangeKind.PRICE_UPDATE:
            return await self._apply_price_update(change, operator)
        if change.kind is ChangeKind.INVENTORY_ACTION:
            return await self._apply_inventory_action(change, operator)
        if change.kind is ChangeKind.LISTING_UPDATE:
            return await self._apply_listing_update(change, operator)
        if change.kind is ChangeKind.PROMOTION:
            return await self._apply_promotion(change, operator)
        if change.kind is ChangeKind.CAMPAIGN:
            return await self._apply_campaign(change, operator)
        raise ChangeNotApplicable(f"unknown change kind {change.kind!r}")

    async def _write_sql(self, sql: str, params: tuple[Any, ...]) -> None:
        """A write for a field the Python binding exposes no mutator for at all
        (``product_variants.price``, ``products.status``, ``products.description``) --
        the engine's own binding has no ``products.update`` or a variant price setter.
        This mirrors ``catalog.py``'s ``list_variants``, which reads what the binding
        does not expose; here it is a write.

        The write itself and its serialization live in :meth:`EngineStore.write_sql`.
        That method is only sound in combination with the read-only connection pinned in
        :meth:`EngineStore._pin_connection` -- see that method's docstring for what goes
        wrong without it. The pin keeps the database file from reaching zero connections,
        so the WAL index is never torn down and rebuilt while the engine handle's cache
        is live, preventing it from serving pre-write values for the rest of its life.

        Schema and triggers on ``products`` / ``product_variants`` were inspected
        directly (``PRAGMA table_info`` and ``sqlite_master`` triggers), not assumed:
        neither table has a trigger that maintains ``updated_at`` or ``version``
        (unlike, say, ``warehouses``), so setting both by hand here matches what a
        native write does and there is nothing else on either table for this path to
        miss. ``products`` does carry ``product_fts_{ai,ad,au}`` triggers that keep its
        full-text index in sync from ``name``/``description``/``slug``; those are
        schema-level and fire for any UPDATE on the table regardless of which
        connection issues it, so a status/description write through this path still
        keeps the search index correct. ``product_variants`` has no trigger at all.
        """
        await self.store.write_sql(sql, params)

    def _log_apply(self, change: StagedChange, operator: str, summary: str) -> str:
        """Record the apply as an activity-log entry, the evidence for an ungoverned
        write. ``activity_logs.record`` requires a real UUID for ``subject_id`` — the
        engine has no notion of a ``chg-...`` staged-change id — so the change is
        referenced by its own id in ``metadata`` instead, under the ``staged_change``
        subject type."""
        entry = self.store.commerce.activity_logs.record(
            subject_type=staging.STAGED_TYPE,
            subject_id=str(uuid4()),
            action="apply",
            summary=summary,
            actor_kind="user",
            actor=operator,
            metadata=json.dumps({"change_id": change.change_id}),
        )
        return f"applied via direct binding write; activity log {entry.id}"

    async def _apply_price_update(self, change: StagedChange, operator: str) -> list[str]:
        notes: list[str] = []
        for item in change.items:
            if item.field != "price":
                continue
            price = money.exact(item.after)
            await self._write_sql(
                "UPDATE product_variants SET price = ?, "
                "updated_at = datetime('now'), version = version + 1 WHERE sku = ?",
                (price, item.target),
            )
            notes.append(
                self._log_apply(change, operator, f"set price of {item.target} to {price}")
            )
        return notes

    async def _apply_inventory_action(self, change: StagedChange, operator: str) -> list[str]:
        notes: list[str] = []
        for item in change.items:
            if item.field == "stock":
                notes.append(await self._apply_restock(change, item, operator))
            elif item.field == "status":
                notes.append(await self._apply_status_change(change, item, operator))
            else:
                raise ChangeNotApplicable(f"unsupported inventory field {item.field!r}")
        return notes

    async def _apply_restock(self, change: StagedChange, item: ChangeItem, operator: str) -> str:
        sku = item.target
        added = float(item.after) - float(item.before)
        stock = await self.store.call(lambda c: c.inventory.get_stock(sku))
        if stock is None:
            # No inventory item exists yet: the engine only governs bringing a SKU into
            # its stock ledger, so this restock goes through the kernel.
            product_row = await self._resolve_variant_row(sku)
            name = product_row.product.name if product_row else sku
            receipt = await self.kernel.execute(
                "inventory.item.create",
                {
                    "sku": sku,
                    "name": name,
                    "initial_quantity": str(added),
                    "reorder_point": "5",
                },
                idempotency_key=f"{change.change_id}:{sku}",
            )
            if not receipt.ok or not receipt.sealed:
                # `sealed` is False for a receipt this process synthesized locally
                # (kernel.py) rather than one the engine actually sealed — that is
                # never evidence of a governed write, whatever `ok` says.
                raise RuntimeError(
                    f"inventory.item.create for {sku!r} failed: "
                    f"{receipt.error_code} {receipt.error_message}"
                )
            self._log_apply(change, operator, f"created inventory item {sku} via kernel command")
            return (
                "governed via kernel command inventory.item.create; "
                f"sealed receipt {receipt.receipt_id}"
            )

        def body(c: Commerce) -> None:
            c.inventory.adjust(sku, added, reason="restock")

        await self.store.write(f"stock:{sku}", body)
        return self._log_apply(change, operator, f"restocked {sku} by {added}")

    async def _apply_status_change(
        self, change: StagedChange, item: ChangeItem, operator: str
    ) -> str:
        resolved = await self._resolve_product_and_merch(item.target)
        if resolved is None:
            raise ChangeNotApplicable(f"no listing with id {item.target!r}")
        product, _merch = resolved
        await self._write_sql(
            "UPDATE products SET status = ?, "
            "updated_at = datetime('now'), version = version + 1 WHERE id = ?",
            (str(item.after), product.id),
        )
        return self._log_apply(change, operator, f"set status of {item.target} to {item.after}")

    async def _apply_listing_update(self, change: StagedChange, operator: str) -> list[str]:
        notes: list[str] = []
        resolved = await self._resolve_product_and_merch(change.items[0].target)
        if resolved is None:
            raise ChangeNotApplicable(f"no listing with id {change.items[0].target!r}")
        product, merch = resolved

        merch_field_names = {"brand", "category", "image_url", "long_description", "unit_cost"}
        updates: dict[str, Any] = {}
        attributes = dict(merch.attributes)
        for item in change.items:
            if item.field == "description":
                await self._write_sql(
                    "UPDATE products SET description = ?, "
                    "updated_at = datetime('now'), version = version + 1 WHERE id = ?",
                    (str(item.after), product.id),
                )
            elif item.field in merch_field_names:
                updates[item.field] = item.after
            elif item.field == "attributes" and isinstance(item.after, dict):
                attributes.update(item.after)
            elif item.field == "specs" and isinstance(item.after, dict):
                updates["specs"] = {**merch.specs, **item.after}
            elif item.field == "labels" and isinstance(item.after, list):
                updates["labels"] = list(item.after)
            else:
                attributes[item.field] = item.after
        updates["attributes"] = attributes

        updated_merch = merch.model_copy(update=updates)
        await write_merchandising(self.store, product.id, updated_merch)
        notes.append(
            self._log_apply(
                change, operator, f"updated listing content for {change.items[0].target}"
            )
        )
        return notes

    async def _apply_promotion(self, change: StagedChange, operator: str) -> list[str]:
        notes: list[str] = []
        for item in change.items:
            if item.field != "price":
                continue
            await self._write_sql(
                "UPDATE product_variants SET price = ?, "
                "updated_at = datetime('now'), version = version + 1 WHERE sku = ?",
                (money.exact(item.after), item.target),
            )
        self._record_custom_object(PROMOTION_TYPE, change.change_id, change.guardrail_notes[0])
        notes.append(self._log_apply(change, operator, f"applied promotion {change.change_id}"))
        return notes

    async def _apply_campaign(self, change: StagedChange, operator: str) -> list[str]:
        payload = json.loads(change.guardrail_notes[0])
        campaign_id = payload.get("campaign_id") or f"camp-{uuid4().hex[:8]}"
        campaign = Campaign(
            campaign_id=campaign_id,
            name=payload["name"],
            status="active",
            objective=payload.get("objective"),
            budget=payload.get("budget") or 0.0,
            starts=payload.get("starts"),
            ends=payload.get("ends"),
        )
        self._record_custom_object(CAMPAIGN_TYPE, campaign_id, campaign.model_dump_json())
        return [self._log_apply(change, operator, f"wrote campaign {campaign_id}")]

    def _record_custom_object(self, type_handle: str, handle: str, payload_json: str) -> None:
        """Synchronous and unlocked, unlike ``staging``'s writes: it runs inside an apply
        that already holds the change, on the store's own ``Commerce`` handle."""
        custom_objects.put_payload(
            self.store.commerce,
            type_handle,
            type_handle.replace("_", " ").title(),
            json.loads(payload_json),
            object_handle=handle,
        )

    async def discard_change(
        self,
        session: MerchantSessionContext,
        change_id: str,
        actor_kind: ActorKind = ActorKind.OPERATOR,
    ) -> StagedChange:
        change = await staging.load(self.store, change_id)
        if change is None:
            raise ChangeNotApplicable(f"no change with id {change_id!r} to discard")
        if change.status is not ChangeStatus.STAGED:
            raise ChangeNotApplicable(
                f"change {change_id} is {change.status.value}, not staged — nothing to discard"
            )
        discarded = change.model_copy(
            update={
                "status": ChangeStatus.DISCARDED,
                "discarded_at": staging.datetime.now(staging.UTC),
                "discarded_by": session.operator,
                "discarded_by_kind": actor_kind,
            }
        )
        await staging.save(self.store, discarded)
        return discarded
