import { NextRequest, NextResponse } from "next/server";
import { forwardedFor, sameOrigin, sessionResponse, UPSTREAM } from "../../../../lib/server";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

// Username + password go to the Platform API, which answers with a session token. The token is
// kept in an httpOnly cookie; the browser never sees it and the password is not stored anywhere here.
export async function POST(req: NextRequest): Promise<Response> {
  if (!sameOrigin(req)) return NextResponse.json({ detail: "cross-site request refused" }, { status: 403 });
  const body = await req.json().catch(() => ({}));
  const username = typeof body?.username === "string" ? body.username.trim() : "";
  const password = typeof body?.password === "string" ? body.password : "";
  if (!username || !password) return NextResponse.json({ detail: "username and password required" }, { status: 400 });

  const headers: Record<string, string> = { "content-type": "application/json", ...forwardedFor(req) };

  let upstream: Response;
  try {
    upstream = await fetch(`${UPSTREAM}/v1/auth/login`, { method: "POST", headers, body: JSON.stringify({ username, password }), cache: "no-store" });
  } catch {
    return NextResponse.json({ detail: "platform API unreachable" }, { status: 502 });
  }
  if (!upstream.ok) {
    const detail = (await upstream.json().catch(() => ({})))?.detail;
    const res = NextResponse.json({ detail: typeof detail === "string" ? detail : "login failed" }, { status: upstream.status === 422 ? 400 : upstream.status });
    const retry = upstream.headers.get("retry-after");
    if (retry) res.headers.set("retry-after", retry);
    return res;
  }

  const { token, expires_in } = await upstream.json();
  return sessionResponse(req, token, expires_in);
}
