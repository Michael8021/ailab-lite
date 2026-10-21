import { NextRequest, NextResponse } from "next/server";
import { COOKIE, isHttps, sameOrigin, UPSTREAM } from "../../../../lib/server";

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

  const headers: Record<string, string> = { "content-type": "application/json" };
  const client = req.headers.get("x-forwarded-for") || req.headers.get("x-real-ip"); // the visitor, for the API's failed-login throttle
  if (client) headers["x-forwarded-for"] = client;

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
  const res = NextResponse.json({ ok: true });
  res.cookies.set(COOKIE, token, {
    httpOnly: true, // not readable from JavaScript, so a script injected into the page cannot steal it
    sameSite: "strict",
    secure: isHttps(req),
    path: "/",
    maxAge: Math.min(Number(expires_in) || 0, 24 * 60 * 60) || 8 * 60 * 60,
  });
  return res;
}
