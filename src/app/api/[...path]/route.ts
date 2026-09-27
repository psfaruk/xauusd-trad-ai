import { type NextRequest } from "next/server";

/**
 * Catch-all API proxy → market-service (:3003).
 *
 * The browser normally reaches the backend through the gateway (any
 * /api/...?XTransformPort=3003 request is routed by Caddy directly to the
 * mini-service). This route covers the other path — a page served from
 * :3000 directly — by forwarding same-origin /api/* requests server-side
 * to the market-service. Both paths coexist: gateway requests never hit
 * this route, direct ones transparently proxy.
 */

const SERVICE = "http://localhost:3003";

const HOP_BY_HOP = new Set([
  "host",
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
]);

async function proxy(req: NextRequest): Promise<Response> {
  const url = new URL(req.url);
  let search = url.search;
  if (url.searchParams.has("XTransformPort")) {
    // the upstream already IS the target service — drop the gateway hint
    const params = new URLSearchParams(url.searchParams);
    params.delete("XTransformPort");
    const qs = params.toString();
    search = qs ? `?${qs}` : "";
  }

  const headers = new Headers(req.headers);
  HOP_BY_HOP.forEach((h) => headers.delete(h));

  const hasBody = !["GET", "HEAD", "OPTIONS"].includes(req.method);
  const body = hasBody ? await req.arrayBuffer() : undefined;

  try {
    const res = await fetch(`${SERVICE}${url.pathname}${search}`, {
      method: req.method,
      headers,
      body: body === undefined || body.byteLength === 0 ? undefined : body,
      cache: "no-store",
    });
    const outHeaders = new Headers(res.headers);
    HOP_BY_HOP.forEach((h) => outHeaders.delete(h));
    return new Response(res.body, { status: res.status, headers: outHeaders });
  } catch (err) {
    return Response.json(
      { detail: `market-service unreachable: ${err instanceof Error ? err.message : "error"}` },
      { status: 502 },
    );
  }
}

export async function GET(req: NextRequest) {
  return proxy(req);
}

export async function POST(req: NextRequest) {
  return proxy(req);
}

export async function PUT(req: NextRequest) {
  return proxy(req);
}

export async function DELETE(req: NextRequest) {
  return proxy(req);
}

export async function PATCH(req: NextRequest) {
  return proxy(req);
}

export async function OPTIONS(req: NextRequest) {
  return proxy(req);
}
