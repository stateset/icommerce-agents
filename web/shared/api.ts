import type { Capabilities } from "./types";

// Production defaults to the same-origin BFF, which reads an access token from an
// HttpOnly cookie. Set NEXT_PUBLIC_API_URL=http://localhost:8000 for the direct local
// demo path used by scripts and browser checks.
export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "/api/commerce";

export const UNREACHABLE = "Couldn't reach the ACME Supply API. Start it and try again.";

export async function healthy(): Promise<boolean> {
  try {
    const response = await fetch(`${API_URL}/healthz`, { cache: "no-store" });
    return response.ok;
  } catch {
    return false;
  }
}

/** `GET /capabilities` -- not session-scoped, no header required. Distinguishes an
 * unconfigured deployment (no model) from an unreachable one (checked separately by
 * `healthy()`); never called unless the API already answered `healthy()`. */
export async function capabilities(): Promise<Capabilities | null> {
  try {
    const response = await fetch(`${API_URL}/capabilities`, { cache: "no-store" });
    if (!response.ok) return null;
    return (await response.json()) as Capabilities;
  } catch {
    return null;
  }
}

export type ControlResult<T> = { ok: true; data: T } | { ok: false; error: string };

/** A control-plane call whose failure detail the operator should see verbatim. */
export async function controlRequest<T>(
  path: string,
  init: RequestInit,
): Promise<ControlResult<T>> {
  try {
    const response = await fetch(`${API_URL}${path}`, init);
    const payload = (await response.json()) as T & { detail?: unknown };
    if (!response.ok) {
      const detail = payload.detail;
      return {
        ok: false,
        error: typeof detail === "string" ? detail : `Request failed (${response.status})`,
      };
    }
    return { ok: true, data: payload };
  } catch {
    return { ok: false, error: UNREACHABLE };
  }
}
