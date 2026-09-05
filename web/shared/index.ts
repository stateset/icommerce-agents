// `./bff` is server-only (it reads `next/headers`); route files import it from
// "icommerce-shared/bff" so it never reaches a client bundle through this index.
export { API_URL, UNREACHABLE, capabilities, controlRequest, healthy } from "./api";
export type { ControlResult } from "./api";
export type { Capabilities, KernelReceipt, StablecoinPayment } from "./types";
