"""Deterministic keyword and facet search.

The engine's semantic search needs an OpenAI key (Commerce.vector(openai_api_key)), which
a Claude reference will not require, so matching here is explicit and inspectable. A
deployment with its own search service replaces this module and nothing else.
"""

from __future__ import annotations

from shopping_agent.types import SearchFilters

from engine_backend.catalog import CatalogRow, catalog_rows
from engine_backend.store import EngineStore


def _haystack(row: CatalogRow) -> str:
    parts = [
        row.product.name,
        row.product.description or "",
        row.variant.name or "",
        row.merch.brand or "",
        row.merch.category or "",
        " ".join(row.merch.labels),
        " ".join(f"{k} {v}" for k, v in row.merch.attributes.items()),
    ]
    return " ".join(parts).lower()


def score(row: CatalogRow, terms: list[str]) -> float:
    """2 points for a term in the product name, 1 for anywhere else."""
    if not terms:
        return 1.0
    name = row.product.name.lower()
    hay = _haystack(row)
    total = 0.0
    for term in terms:
        if term in name:
            total += 2.0
        elif term in hay:
            total += 1.0
    return total


def _passes(row: CatalogRow, filters: SearchFilters) -> bool:
    if filters.category and row.merch.category != filters.category:
        return False
    if filters.min_price is not None and row.variant.price < filters.min_price:
        return False
    if filters.max_price is not None and row.variant.price > filters.max_price:
        return False
    if filters.min_rating is not None and (row.merch.rating or 0.0) < filters.min_rating:
        return False
    return all(row.merch.attributes.get(k) == v for k, v in filters.attributes.items())


async def search(
    store: EngineStore, query: str, filters: SearchFilters | None, limit: int
) -> list[CatalogRow]:
    filters = filters or SearchFilters()
    terms = [t for t in query.lower().split() if t]
    rows = [r for r in await catalog_rows(store) if _passes(r, filters)]
    if terms:
        rows = [r for r in rows if score(r, terms) > 0]

    # One row per family: the cheapest in-stock variant represents it.
    by_family: dict[str, CatalogRow] = {}
    for row in sorted(rows, key=lambda r: (r.stock <= 0, r.variant.price)):
        by_family.setdefault(row.product.id, row)
    chosen = list(by_family.values())

    if filters.sort == "price_asc":
        chosen.sort(key=lambda r: r.variant.price)
    elif filters.sort == "price_desc":
        chosen.sort(key=lambda r: -r.variant.price)
    elif filters.sort == "rating":
        chosen.sort(key=lambda r: -(r.merch.rating or 0.0))
    else:
        chosen.sort(key=lambda r: (-score(r, terms), r.variant.price))
    return chosen[:limit]
