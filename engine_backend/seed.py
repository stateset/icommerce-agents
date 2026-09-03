"""A fictional ACME Supply store. No real brand, product, or person appears."""

from __future__ import annotations

import json

from stateset_embedded import (
    Commerce,
    CreateOrderItemInput,
    CreateProductVariantInput,
)

from engine_backend.catalog import MERCHANDISING_TYPE, Merchandising, ensure_types

_CATALOG = [
    {
        "name": "Ridgeline 2-Person Tent",
        "description": "A three-season backpacking tent for two.",
        "variants": [
            {"sku": "TENT-RIDGE-GRN", "name": "Green", "price": 219.00, "stock": 24},
            {"sku": "TENT-RIDGE-TAN", "name": "Tan", "price": 219.00, "stock": 4},
        ],
        "merch": {
            "brand": "ACME Outdoors",
            "category": "camping",
            "rating": 4.6,
            "review_count": 212,
            "unit_cost": 128.00,
            "option_names": ["colour"],
            "variant_options": {
                "TENT-RIDGE-GRN": {"colour": "green"},
                "TENT-RIDGE-TAN": {"colour": "tan"},
            },
            "attributes": {"season": "3-season", "capacity": "2"},
            "specs": {"packed_weight": "2.4 kg", "floor_area": "2.9 m2"},
            "long_description": "Aluminium poles, taped seams, two vestibules.",
        },
    },
    {
        "name": "Summit -7C Sleeping Bag",
        "description": "A mummy-cut sleeping bag rated to -7C.",
        "variants": [
            {"sku": "BAG-SUMMIT-REG", "name": "Regular", "price": 159.00, "stock": 18},
            {"sku": "BAG-SUMMIT-LNG", "name": "Long", "price": 169.00, "stock": 11},
        ],
        "merch": {
            "brand": "ACME Outdoors",
            "category": "camping",
            "rating": 4.4,
            "review_count": 96,
            "unit_cost": 74.00,
            "option_names": ["length"],
            "variant_options": {
                "BAG-SUMMIT-REG": {"length": "regular"},
                "BAG-SUMMIT-LNG": {"length": "long"},
            },
            "attributes": {"temperature_rating": "-7C", "fill": "synthetic"},
            "specs": {"packed_weight": "1.6 kg"},
            "long_description": "Synthetic fill holds warmth even when damp.",
        },
    },
    {
        "name": "Trailhead Camp Stove",
        "description": "A single-burner canister stove for backcountry cooking.",
        "variants": [
            {"sku": "STOVE-TRAIL-1", "name": "Standard", "price": 49.00, "stock": 0},
        ],
        "merch": {
            "brand": "ACME Outdoors",
            "category": "camping",
            "rating": 3.6,
            "review_count": 41,
            "unit_cost": 19.00,
            "attributes": {"fuel": "canister", "boil_time": "3.5 min/L"},
            "specs": {"weight": "0.1 kg"},
            "long_description": "Piezo ignition, folds to fit in a mug.",
        },
    },
    {
        "name": "Switchback 22L Daypack",
        "description": "A 22-liter daypack for day hikes and commuting.",
        "variants": [
            {"sku": "PACK-SWITCH-SLT", "name": "Slate", "price": 89.00, "stock": 30},
            {"sku": "PACK-SWITCH-MOS", "name": "Moss", "price": 89.00, "stock": 22},
        ],
        "merch": {
            "brand": "ACME Gear",
            "category": "packs",
            "rating": 4.2,
            "review_count": 133,
            "unit_cost": 38.00,
            "option_names": ["colour"],
            "variant_options": {
                "PACK-SWITCH-SLT": {"colour": "slate"},
                "PACK-SWITCH-MOS": {"colour": "moss"},
            },
            "attributes": {"capacity": "22L"},
            "specs": {"weight": "0.6 kg"},
            "long_description": "Padded hip belt and a dedicated hydration sleeve.",
        },
    },
    {
        "name": "Clearwater Pump Filter",
        "description": "A hand-pump water filter for backcountry sources.",
        "variants": [
            {"sku": "FILTER-CLEAR-1", "name": "Standard", "price": 79.00, "stock": 15},
        ],
        "merch": {
            "brand": "ACME Gear",
            "category": "hydration",
            "rating": 4.5,
            "review_count": 88,
            "unit_cost": 31.00,
            "attributes": {"filter_rating": "0.2 micron"},
            "specs": {"flow_rate": "1 L/min"},
            "long_description": "Removes bacteria and protozoa without chemicals.",
        },
    },
    {
        "name": "Beacon 300 Headlamp",
        "description": "A rechargeable 300-lumen headlamp with a red night mode.",
        "variants": [
            {"sku": "LAMP-BEACON-BLK", "name": "Black", "price": 45.00, "stock": 27},
            {"sku": "LAMP-BEACON-ORG", "name": "Orange", "price": 45.00, "stock": 9},
        ],
        "merch": {
            "brand": "ACME Gear",
            "category": "lighting",
            "rating": 4.1,
            "review_count": 57,
            "unit_cost": 17.00,
            "option_names": ["colour"],
            "variant_options": {
                "LAMP-BEACON-BLK": {"colour": "black"},
                "LAMP-BEACON-ORG": {"colour": "orange"},
            },
            "attributes": {"lumens": "300", "rechargeable": "true"},
            "specs": {"battery": "USB-C, 1200 mAh"},
            "long_description": "A red night mode preserves night vision at camp.",
        },
    },
]


def seed_store(commerce: Commerce) -> None:
    """Idempotent: returns immediately when the catalog is already present."""
    ensure_types(commerce)
    if commerce.products.count() > 0:
        return

    for entry in _CATALOG:
        product = commerce.products.create(
            name=entry["name"],
            description=entry["description"],
            variants=[
                CreateProductVariantInput(sku=v["sku"], price=v["price"], name=v["name"])
                for v in entry["variants"]
            ],
        )
        commerce.custom_objects.create_object(
            type_handle=MERCHANDISING_TYPE,
            values_json=json.dumps({"payload": Merchandising(**entry["merch"]).model_dump()}),
            owner_type="product",
            owner_id=product.id,
        )
        for variant in entry["variants"]:
            commerce.inventory.create_item(
                sku=variant["sku"],
                name=f"{entry['name']} ({variant['name']})",
                initial_quantity=float(variant["stock"]),
                reorder_point=5.0,
            )

    customer1 = commerce.customers.create(
        email="rowan@example.invalid", first_name="Rowan", last_name="Ellis"
    )
    customer2 = commerce.customers.create(
        email="parker@example.invalid", first_name="Parker", last_name="Nguyen"
    )

    order1 = commerce.orders.create(
        customer_id=customer1.id,
        items=[
            CreateOrderItemInput(
                sku="TENT-RIDGE-GRN",
                name="Ridgeline 2-Person Tent (Green)",
                quantity=1,
                unit_price=219.00,
            )
        ],
    )
    payment = commerce.payments.create(
        amount=219.00,
        currency="USD",
        order_id=order1.id,
        customer_id=customer1.id,
        payment_method="credit_card",
    )
    commerce.payments.complete(payment.id)

    commerce.orders.create(
        customer_id=customer1.id,
        items=[
            CreateOrderItemInput(
                sku="BAG-SUMMIT-REG",
                name="Summit -7C Sleeping Bag (Regular)",
                quantity=1,
                unit_price=159.00,
            )
        ],
    )

    commerce.orders.create(
        customer_id=customer2.id,
        items=[
            CreateOrderItemInput(
                sku="PACK-SWITCH-SLT",
                name="Switchback 22L Daypack (Slate)",
                quantity=1,
                unit_price=89.00,
            ),
            CreateOrderItemInput(
                sku="LAMP-BEACON-BLK",
                name="Beacon 300 Headlamp (Black)",
                quantity=1,
                unit_price=45.00,
            ),
        ],
    )
