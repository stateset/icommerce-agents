"use client";

import type { ProductsPayload } from "../../lib/types";
import { addToCart } from "../../lib/api";

function money(value: number, currency = "USD"): string {
  return new Intl.NumberFormat("en-US", { style: "currency", currency }).format(value);
}

export function ProductCards({
  payload,
  onAdded,
}: {
  payload: ProductsPayload;
  onAdded: (cart: unknown) => void;
}) {
  const items = payload.items ?? [];
  if (!items.length) return null;
  return (
    <div>
      {payload.title ? <p style={{ fontSize: 13, fontWeight: 600, marginBottom: 6 }}>{payload.title}</p> : null}
      <div className="product-grid">
        {items.map(({ product, reason }) => (
          <div className="product-card" key={product.product_id}>
            <div className="thumb">{product.image_url ? null : "No image"}</div>
            <div className="title">{product.title}</div>
            <div className="price">{money(product.price, product.currency)}</div>
            {reason ? <div className="reason">{reason}</div> : null}
            <button
              type="button"
              onClick={async () => {
                const cart = await addToCart(product.product_id, 1);
                if (cart) onAdded(cart);
              }}
            >
              Add to bag
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}
