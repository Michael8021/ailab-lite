import { NextRequest, NextResponse } from "next/server";
import { COOKIE, sameOrigin, UPSTREAM } from "../../../../lib/server";

// Same-origin proxy to the Platform API. Only /v1/* exists here, so /internal/* is unreachable
// from a browser; the access token is read from the httpOnly cookie and added server-side.
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const FORWARD_REQUEST_HEADERS = ["content-type", "content-length", "accept", "last-event-id", "x-request-id", "if-none-match"];
// cache-control / etag: the API marks what may be kept (immutable thumbnails); everything else stays no-store.
const FORWARD_RESPONSE_HEADERS = ["content-type", "content-disposition", "content-length", "x-accel-buffering", "x-request-id", "cache-control", "etag"];

async function proxy(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }): Promise<Response> {
  const { path } = await ctx.params;
  if (path.some((s) => s === ".." || s === "." || s.includes("/") || s.includes("\\"))) {
    return NextResponse.json({ detail: "bad path" }, { status: 400 });
  }
  const unsafe = !["GET", "HEAD"].includes(req.method);
  if (unsafe && !sameOrigin(req)) return NextResponse.json({ detail: "cross-site request refused" }, { status: 403 });

  const token = req.cookies.get(COOKIE)?.value;
  if (!token) return NextResponse.json({ detail: "not logged in" }, { status: 401 });

  const headers = new Headers();
  for (const name of FORWARD_REQUEST_HEADERS) {
    const value = req.headers.get(name);
    if (value) headers.set(name, value);
  }
  headers.set("authorization", `Bearer ${token}`);

  const init: RequestInit & { duplex?: "half" } = { method: req.method, headers, signal: req.signal, cache: "no-store" };
  if (unsafe && req.body) {
    init.body = req.body;
    init.duplex = "half";
  }
  let upstream: Response;
  try {
    upstream = await fetch(`${UPSTREAM}/v1/${path.map(encodeURIComponent).join("/")}${req.nextUrl.search}`, init);
  } catch {
    return NextResponse.json({ detail: "platform API unreachable" }, { status: 502 });
  }

  const out = new Headers({ "cache-control": "no-store" }); // unless the API says otherwise below
  for (const name of FORWARD_RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value) out.set(name, value);
  }
  return new Response(upstream.body, { status: upstream.status, headers: out });
}

export { proxy as GET, proxy as POST, proxy as PUT, proxy as PATCH, proxy as DELETE };
