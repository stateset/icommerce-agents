export interface Product {
  product_id: string;
  title: string;
  brand?: string | null;
  price: number;
  currency?: string;
  rating?: number | null;
  review_count?: number | null;
  image_url?: string | null;
  category?: string | null;
}

export interface ProductsPayload {
  title?: string;
  layout?: string;
  items: { product: Product; reason?: string }[];
}

export interface CartItem {
  product_id: string;
  title: string;
  price: number;
  quantity: number;
  image_url?: string | null;
  option_values?: Record<string, string>;
  /** The engine's exact decimal line total for this item (`POST /shopping/cart/add`).
   * Given by the host -- never recomputed here from `price * quantity`. */
  total_exact?: string | null;
  /** A `cart_update` chat event's own server-computed line total (vendor's
   * `cart_payload`); also given, not client arithmetic, but rounded rather than exact. */
  line_total?: number;
}

export interface CartPayload {
  items: CartItem[];
  currency: string;
  /** The engine's exact decimal subtotal, given by the host. */
  subtotal_exact?: string | null;
  grand_total_exact?: string | null;
  /** A `cart_update` chat event's own server-computed subtotal; given, not computed
   * here, but rounded rather than exact. */
  subtotal?: number;
}

export interface CheckoutResponse {
  order_number?: string;
  receipt: {
    ok: boolean;
    sealed: boolean;
    receipt_id?: string | null;
    error_code?: string | null;
    error_message?: string | null;
  };
}

export interface OrderItem {
  product_id: string;
  title: string;
  quantity: number;
  price: number;
  option_values?: Record<string, string>;
  variant_of?: string | null;
}

export interface Order {
  order_id: string;
  status: string;
  placed_at: string;
  items: OrderItem[];
  total: number;
  currency?: string;
  estimated_delivery?: string | null;
  tracking_url?: string | null;
  /** The engine's own exact total for this order (`GET /shopping/orders`), read from
   * the matching engine order -- never recomputed from the `float` `total` above. */
  total_exact?: string | null;
  /** The engine's own human-facing order number, read from the matching engine order.
   * This is what the checkout response and the tour print -- `order_id` is the
   * engine's internal id, a different value a reader cannot match to it. */
  order_number?: string | null;
}

export interface OrdersPayload {
  orders: Order[];
}

/** `GET /capabilities` -- whether a model is configured for this deployment. Present
 * or absent only, never valid or invalid. */
export interface Capabilities {
  assistant: "available" | "unconfigured";
}
