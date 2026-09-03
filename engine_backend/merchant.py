"""``MerchantBackend`` over the engine: analytics, catalog and order reads; staging
(``stage_*`` / ``get_pending_changes`` / ``discard_change``) delegates to
``engine_backend.staging``, and applying (``apply_change``) delegates to
``engine_backend.apply`` -- the only place this package mutates live state.

Money mirrors ``storefront.py`` and goes through ``engine_backend.money``: a figure
already computed by ``commerce.analytics`` is a plain float returned as-is; a figure
sourced from an exact-string field is converted once, at the display edge, with
``money.to_float``. A figure the engine genuinely cannot supply (traffic, conversion,
campaign spend/revenue with no campaign) is ``None`` with a ``note``, never a zero.
"""

from __future__ import annotations

from typing import Any

from merchant_agent.backend import MerchantBackend
from merchant_agent.changes import ChangeNotApplicable, GuardrailViolation, check_guardrails
from merchant_agent.config import MerchantAgentConfig
from merchant_agent.types import (
    ActorKind,
    AnalysisTable,
    BusinessSnapshot,
    Campaign,
    CampaignDraft,
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
from engine_backend.apply import CAMPAIGN_TYPE, ApplyContext
from engine_backend.apply import apply_change as _apply_change
from engine_backend.catalog import CatalogRow, catalog_rows, list_variants
from engine_backend.kernel import KernelClient
from engine_backend.listings import (
    FamilyResolution,
    VariantResolution,
    family_listing,
    resolve_family_or_variant,
    to_listing,
)
from engine_backend.search import search as engine_search
from engine_backend.store import EngineStore

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
                listings.append(family_listing(row.product, variants, row.merch, all_rows))
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
        resolution = await resolve_family_or_variant(self.store, listing_id)

        if isinstance(resolution, FamilyResolution):
            family = family_listing(
                resolution.product, resolution.variants, resolution.merch, resolution.rows
            )
            variant_listings = [
                to_listing(r, variant_count=len(resolution.rows), variant_of=resolution.product.id)
                for r in resolution.rows
            ]
            return ListingDetails(
                **family.model_dump(),
                long_description=resolution.merch.long_description,
                variants=variant_listings,
            )

        if isinstance(resolution, VariantResolution):
            listing = to_listing(
                resolution.row,
                variant_count=resolution.variant_count,
                variant_of=resolution.row.product.id,
            )
            return ListingDetails(
                **listing.model_dump(),
                long_description=resolution.row.merch.long_description,
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

        # A read, off the loop via ``store.call`` -- like ``catalog.list_variants``, the
        # other caller of ``readonly_sql``, this runs on a worker thread so the
        # thread-local connection it opens is never touched from the event-loop thread.
        def body(_c: Any) -> AnalysisTable:
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

        return await self.store.call(body)

    async def get_analysis_schema(self, session: MerchantSessionContext) -> str | None:
        return _ANALYSIS_SCHEMA

    # -- Merchant context ----------------------------------------------------------

    async def get_merchant_context(self, session: MerchantSessionContext) -> dict[str, Any] | None:
        note = "Campaigns are not managed by the engine; no channel spend or revenue is available."
        return {
            "store_name": "ACME Supply",
            "reporting_period": "current",
            "limitations": [DataLimitation(source="campaigns", note=note)],
        }

    # -- Staged writes ----------------------------------------------------------------

    async def stage_listing_update(
        self,
        session: MerchantSessionContext,
        listing_id: str,
        fields: dict[str, Any],
        note: str | None = None,
    ) -> StagedChange:
        return await staging.stage_listing_update(
            self.store, self.config, session.operator, listing_id, fields
        )

    async def stage_price_update(
        self,
        session: MerchantSessionContext,
        items: list[PriceUpdateItem],
        note: str | None = None,
    ) -> StagedChange:
        return await staging.stage_price_update(self.store, self.config, session.operator, items)

    async def stage_inventory_action(
        self,
        session: MerchantSessionContext,
        items: list[InventoryActionItem],
        note: str | None = None,
    ) -> StagedChange:
        return await staging.stage_inventory_action(
            self.store, self.config, session.operator, items
        )

    async def stage_promotion(
        self, session: MerchantSessionContext, promotion: PromotionDraft
    ) -> StagedChange:
        return await staging.stage_promotion(self.store, self.config, session.operator, promotion)

    async def stage_campaign(
        self, session: MerchantSessionContext, campaign: CampaignDraft
    ) -> StagedChange:
        existing: Campaign | None = None
        if campaign.campaign_id is not None:
            matches = await self.get_campaign_performance(session, campaign.campaign_id)
            existing = matches[0] if matches else None
        return await staging.stage_campaign(
            self.store, self.config, session.operator, campaign, existing
        )

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

        # Guardrails run again against the config now in force, not the one at stage time.
        violations = check_guardrails(change.kind, change.items, self.config)
        if violations:
            raise GuardrailViolation(violations)

        # The five-kind write dispatch lives in engine_backend/apply.py; see its module
        # docstring and docs/enforcement.md for the governed/ungoverned finding.
        payload = await staging.load_change_payload(self.store, change_id)
        ctx = ApplyContext(store=self.store, kernel=self.kernel, operator=session.operator)
        applied, evidence = await _apply_change(ctx, change, payload)
        await staging.save(self.store, applied, evidence=evidence)
        return applied

    async def discard_change(
        self,
        session: MerchantSessionContext,
        change_id: str,
        actor_kind: ActorKind = ActorKind.OPERATOR,
    ) -> StagedChange:
        return await staging.discard(self.store, change_id, session.operator, actor_kind)
