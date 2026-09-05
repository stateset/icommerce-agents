import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { UNREACHABLE, capabilities, controlRequest, healthy } from "./api";

describe("controlRequest", () => {
  const originalFetch = globalThis.fetch;
  let next: () => Response | Promise<Response>;
  beforeEach(() => {
    globalThis.fetch = (async () => next()) as typeof fetch;
  });
  afterEach(() => {
    globalThis.fetch = originalFetch;
  });

  it("returns the payload on success", async () => {
    next = () => Response.json({ change_id: "c1" });
    expect(await controlRequest("/merchant/x", {})).toEqual({ ok: true, data: { change_id: "c1" } });
  });

  it("surfaces the host's string detail verbatim on failure", async () => {
    next = () => Response.json({ detail: "proposal digest changed" }, { status: 409 });
    expect(await controlRequest("/merchant/x", {})).toEqual({
      ok: false,
      error: "proposal digest changed",
    });
  });

  it("falls back to the status when the detail is structured", async () => {
    next = () => Response.json({ detail: { error_code: "x" } }, { status: 422 });
    expect(await controlRequest("/merchant/x", {})).toEqual({
      ok: false,
      error: "Request failed (422)",
    });
  });

  it("reports an unreachable host instead of throwing", async () => {
    next = () => {
      throw new TypeError("fetch failed");
    };
    expect(await controlRequest("/merchant/x", {})).toEqual({ ok: false, error: UNREACHABLE });
    expect(await healthy()).toBe(false);
    expect(await capabilities()).toBeNull();
  });
});
