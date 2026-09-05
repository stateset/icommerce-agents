import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// `next/headers` only exists inside a Next request scope; the tests hand the proxy the
// cookie jar directly.
const cookieJar: { value?: string } = {};
vi.mock("next/headers", () => ({
  cookies: async () => ({ get: () => (cookieJar.value ? { value: cookieJar.value } : undefined) }),
}));

import { proxy, sameOriginMutation, upstreamOrigin } from "./bff";

type FakeNextRequest = Request & { nextUrl: URL };

function request(url: string, init: RequestInit = {}): FakeNextRequest {
  return Object.assign(new Request(url, init), { nextUrl: new URL(url) });
}

const context = (path: string[]) => ({ params: Promise.resolve({ path }) });

describe("upstreamOrigin", () => {
  const original = process.env.ICOMMERCE_API_URL;
  afterEach(() => {
    if (original === undefined) delete process.env.ICOMMERCE_API_URL;
    else process.env.ICOMMERCE_API_URL = original;
  });

  it("defaults to the local host", () => {
    delete process.env.ICOMMERCE_API_URL;
    expect(upstreamOrigin()).toBe("http://127.0.0.1:8000");
  });

  it("accepts HTTPS anywhere and plain HTTP only on loopback", () => {
    process.env.ICOMMERCE_API_URL = "https://api.example.com";
    expect(upstreamOrigin()).toBe("https://api.example.com");
    process.env.ICOMMERCE_API_URL = "http://localhost:8000/";
    expect(upstreamOrigin()).toBe("http://localhost:8000");
    process.env.ICOMMERCE_API_URL = "http://api.example.com";
    expect(() => upstreamOrigin()).toThrow(/HTTPS/);
  });

  it("refuses credentials, queries, and fragments in the origin", () => {
    for (const value of [
      "https://user:pw@api.example.com",
      "https://api.example.com/?x=1",
      "https://api.example.com/#frag",
    ]) {
      process.env.ICOMMERCE_API_URL = value;
      expect(() => upstreamOrigin()).toThrow(/without credentials/);
    }
  });
});

describe("sameOriginMutation", () => {
  const originalEnv = process.env.NODE_ENV;
  afterEach(() => {
    process.env.NODE_ENV = originalEnv;
  });

  it("never blocks a read", () => {
    expect(sameOriginMutation(request("https://shop.example/api/commerce/healthz"))).toBe(true);
  });

  it("rejects a mutation the browser marks cross-site", () => {
    const req = request("https://shop.example/api/commerce/shopping/session", {
      method: "POST",
      headers: { "sec-fetch-site": "cross-site", origin: "https://shop.example", host: "shop.example" },
    });
    expect(sameOriginMutation(req)).toBe(false);
  });

  it("compares the Origin host with the request Host", () => {
    const same = request("https://shop.example/api/commerce/x", {
      method: "POST",
      headers: { origin: "https://shop.example", host: "shop.example" },
    });
    const other = request("https://shop.example/api/commerce/x", {
      method: "POST",
      headers: { origin: "https://evil.example", host: "shop.example" },
    });
    expect(sameOriginMutation(same)).toBe(true);
    expect(sameOriginMutation(other)).toBe(false);
  });

  it("requires an Origin header in production but not in development", () => {
    const req = () =>
      request("https://shop.example/api/commerce/x", { method: "POST", headers: { host: "shop.example" } });
    process.env.NODE_ENV = "production";
    expect(sameOriginMutation(req())).toBe(false);
    process.env.NODE_ENV = "development";
    expect(sameOriginMutation(req())).toBe(true);
  });
});

describe("proxy", () => {
  const originalFetch = globalThis.fetch;
  let seen: { url: string; init: RequestInit } | null;

  beforeEach(() => {
    seen = null;
    cookieJar.value = undefined;
    delete process.env.ICOMMERCE_API_URL;
    globalThis.fetch = (async (url: string | URL | Request, init?: RequestInit) => {
      seen = { url: String(url), init: init ?? {} };
      return new Response('{"ok":true}', {
        status: 202,
        headers: {
          "content-type": "application/json",
          "payment-required": "abc",
          "set-cookie": "leak=1",
          "x-upstream-secret": "never",
        },
      });
    }) as typeof fetch;
  });
  afterEach(() => {
    globalThis.fetch = originalFetch;
  });

  it("rejects a cross-site mutation before contacting the host", async () => {
    const response = await proxy(
      request("https://shop.example/api/commerce/shopping/session", {
        method: "POST",
        headers: { "sec-fetch-site": "cross-site", host: "shop.example" },
      }),
      context(["shopping", "session"]),
    );
    expect(response.status).toBe(403);
    expect(seen).toBeNull();
  });

  it("forwards only the allowed headers, the cookie as a bearer token, and the path", async () => {
    cookieJar.value = "token-123";
    const response = await proxy(
      request("https://shop.example/api/commerce/shopping/cart?x=1", {
        headers: {
          host: "shop.example",
          "x-session-id": "sess-1",
          "x-request-id": "req-1",
          cookie: "__Host-icommerce_access_token=token-123",
          "x-forwarded-for": "10.0.0.1",
        },
      }),
      context(["shopping", "cart"]),
    );
    expect(seen?.url).toBe("http://127.0.0.1:8000/shopping/cart?x=1");
    const headers = new Headers(seen?.init.headers);
    expect(headers.get("authorization")).toBe("Bearer token-123");
    expect(headers.get("x-session-id")).toBe("sess-1");
    expect(headers.get("cookie")).toBeNull();
    expect(headers.get("x-forwarded-for")).toBeNull();
    expect(response.status).toBe(202);
    expect(response.headers.get("payment-required")).toBe("abc");
    expect(response.headers.get("set-cookie")).toBeNull();
    expect(response.headers.get("x-upstream-secret")).toBeNull();
    expect(response.headers.get("cache-control")).toBe("no-store");
  });

  it("encodes each path segment so a segment cannot smuggle a slash", async () => {
    await proxy(
      request("https://shop.example/api/commerce/x", { headers: { host: "shop.example" } }),
      context(["merchant", "changes", "a/../b"]),
    );
    expect(seen?.url).toBe("http://127.0.0.1:8000/merchant/changes/a%2F..%2Fb");
  });
});
