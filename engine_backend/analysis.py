"""The merchant's read-only analysis surface: the schema the operator's agent is told
about, and the one capped ``SELECT`` it may run.

This is not a fallback for a missing binding method the way ``catalog.list_variants``
is -- it is a deliberate feature. ``EngineMerchant.execute_analysis_query`` delegates
here; the caps and the statement check live in one place so a second entry point cannot
be added that keeps the connection and forgets the limits.

Three things stand between a model-written string and the store, and all three are
needed: the statement must be a single ``SELECT`` (a heuristic, checked here), the
connection is opened ``mode=ro`` so SQLite itself refuses a write regardless of what the
heuristic missed (``EngineStore.readonly_sql``), and the result is capped at
:data:`ROW_CAP` rows and :data:`CHAR_CAP` characters before it becomes a tool result.
"""

from __future__ import annotations

from typing import Any

from merchant_agent.types import AnalysisTable

from engine_backend.store import EngineStore

ROW_CAP = 100
CHAR_CAP = 8000

SCHEMA = """\
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


def check_statement(sql: str) -> str:
    """``sql`` as the single ``SELECT`` this surface accepts, or ``ValueError``."""
    statement = sql.strip()
    if not statement:
        raise ValueError("empty query")
    if ";" in statement.rstrip(";"):
        raise ValueError("only a single statement is allowed")
    if statement.split(None, 1)[0].lower() != "select":
        raise ValueError("only SELECT statements are allowed")
    return statement.rstrip(";")


async def run_query(store: EngineStore, sql: str) -> AnalysisTable:
    """One capped, read-only ``SELECT`` against the store's own SQLite file."""
    statement = check_statement(sql)

    # A read, off the loop via ``store.call`` -- like ``catalog.list_variants``, the
    # other caller of ``readonly_sql``, this runs on a worker thread so the
    # thread-local connection it opens is never touched from the event-loop thread.
    def body(_c: Any) -> AnalysisTable:
        cursor = store.readonly_sql().execute(statement)
        columns = [d[0] for d in cursor.description] if cursor.description else []
        fetched = cursor.fetchmany(ROW_CAP + 1)
        truncated = len(fetched) > ROW_CAP
        fetched = fetched[:ROW_CAP]

        rows: list[list[Any]] = []
        chars = 0
        for record in fetched:
            values = list(record)
            row_chars = sum(len(str(v)) for v in values)
            if chars + row_chars > CHAR_CAP:
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

    return await store.call(body)
