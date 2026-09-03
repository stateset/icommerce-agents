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
}

export interface CartPayload {
  items: CartItem[];
  currency: string;
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
