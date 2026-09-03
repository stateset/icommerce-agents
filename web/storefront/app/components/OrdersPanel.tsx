import type { Order } from "../../lib/types";

/** Formats the engine's own exact total for display -- never recomputed, never summed
 * with anything else here. The last-step string-to-number conversion happens only at
 * formatting time, same as `CartPanel.displayAmount`. */
function displayTotal(order: Order): string | null {
  const value = order.total_exact ?? order.total;
  if (value === null || value === undefined) return null;
  const amount = typeof value === "string" ? Number(value) : value;
  if (Number.isNaN(amount)) return null;
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: order.currency ?? "USD",
  }).format(amount);
}

export function OrdersPanel({ orders }: { orders: Order[] | null }) {
  const list = orders ?? [];
  return (
    <div className="orders-block">
      <div className="orders-header">
        <h2>Order history</h2>
      </div>
      {list.length === 0 ? (
        <div className="orders-empty">No orders yet.</div>
      ) : (
        <div className="orders-list">
          {list
            .slice()
            .sort((a, b) => b.placed_at.localeCompare(a.placed_at))
            .map((order) => (
              <div className="order-card" key={order.order_id}>
                <div className="order-card-top">
                  <span className="order-number">{order.order_id.slice(0, 8)}</span>
                  <span className={`order-status ${order.status}`}>{order.status}</span>
                </div>
                <div className="order-items">
                  {order.items.map((item, index) => (
                    <span key={index}>
                      {item.quantity}x {item.title}
                      {index < order.items.length - 1 ? ", " : ""}
                    </span>
                  ))}
                </div>
                <div className="order-card-bottom">
                  <span>{new Date(order.placed_at).toLocaleDateString()}</span>
                  <span>{displayTotal(order) ?? "—"}</span>
                </div>
              </div>
            ))}
        </div>
      )}
    </div>
  );
}
