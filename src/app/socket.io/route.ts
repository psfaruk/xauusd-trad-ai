import { type NextRequest } from "next/server";

/**
 * engine.io polling proxy → market-service (:3003).
 *
 * D-082 robustness — the socket.io client now handshakes over POLLING
 * first and upgrades to websocket when the path allows it:
 *
 *  - through the gateway (Caddy, the normal preview path) both the
 *    polling handshake AND the websocket upgrade are reverse-proxied —
 *    the session ends on true websocket (fast path, unchanged);
 *  - when the page is served from :3000 directly, the websocket upgrade
 *    can't pass through the Next dev server — the polling transport
 *    reaches the service through THIS route instead, so the app never
 *    gets stuck "loading / not connected" on any origin.
 *
 * engine.io polls `GET|POST /socket.io/?EIO=4&transport=polling&…` (long-
 * polls held up to ~25s) — streamed straight through, never buffered.
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
      // engine.io long-polls: no-store keeps every poll live
      cache: "no-store",
    });
    const outHeaders = new Headers(res.headers);
    HOP_BY_HOP.forEach((h) => outHeaders.delete(h));
    // streaming pass-through — a held long-poll flushes packets as they come
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

export async function OPTIONS(req: NextRequest) {
  return proxy(req);
}
