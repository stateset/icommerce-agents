/** The same-origin backend-for-frontend both web apps mount at `/api/commerce/[...path]`.
 * It keeps the production bearer token in an HttpOnly cookie, forwards only the headers
 * the host needs, and rejects cross-site mutations. Each app's route file re-exports
 * `proxy` for every method and declares its own `runtime`/`dynamic` literals, which
 * Next requires to be written in the route module itself. */
import { cookies } from "next/headers";
import type { NextRequest } from "next/server";

const REQUEST_HEADERS = [
  "content-type",
  "payment-signature",
  "x-request-id",
  "x-session-id",
] as const;
const RESPONSE_HEADERS = [
  "cache-control",
  "content-type",
  "payment-required",
  "payment-response",
  "www-authenticate",
  "x-request-id",
] as const;
const MUTATING = new Set(["POST", "PUT", "PATCH", "DELETE"]);

function upstreamOrigin(): string {
  const value = process.env.ICOMMERCE_API_URL ?? "http://127.0.0.1:8000";
  const parsed = new URL(value);
  const local = parsed.hostname === "127.0.0.1" || parsed.hostname === "localhost";
  if (parsed.username || parsed.password || parsed.search || parsed.hash) {
    throw new Error("ICOMMERCE_API_URL must be an origin without credentials");
  }
  if (parsed.protocol !== "https:" && !(local && parsed.protocol === "http:")) {
    throw new Error("ICOMMERCE_API_URL must use HTTPS (local HTTP is allowed)");
  }
  return parsed.origin;
}

function sameOriginMutation(request: NextRequest): boolean {
  if (!MUTATING.has(request.method)) return true;
  if (request.headers.get("sec-fetch-site") === "cross-site") return false;
  const origin = request.headers.get("origin");
  if (!origin) return process.env.NODE_ENV !== "production";
  try {
    return new URL(origin).host === request.headers.get("host");
  } catch {
    return false;
  }
}

export async function proxy(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> },
): Promise<Response> {
  if (!sameOriginMutation(request)) {
    return Response.json({ detail: "cross-site request rejected" }, { status: 403 });
  }

  const { path } = await context.params;
  const target = new URL(
    `/${path.map(encodeURIComponent).join("/")}${request.nextUrl.search}`,
    upstreamOrigin(),
  );
  const headers = new Headers({ accept: "application/json" });
  for (const name of REQUEST_HEADERS) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  const cookieName = process.env.ICOMMERCE_AUTH_COOKIE ?? "__Host-icommerce_access_token";
  const token = (await cookies()).get(cookieName)?.value;
  if (token) headers.set("authorization", `Bearer ${token}`);

  const upstream = await fetch(target, {
    method: request.method,
    headers,
    body: MUTATING.has(request.method) ? await request.arrayBuffer() : undefined,
    cache: "no-store",
    redirect: "manual",
  });
  const responseHeaders = new Headers();
  for (const name of RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value) responseHeaders.set(name, value);
  }
  responseHeaders.set("cache-control", "no-store");
  return new Response(upstream.body, {
    status: upstream.status,
    headers: responseHeaders,
  });
}
