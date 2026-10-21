import { NextRequest, NextResponse } from "next/server";
import { COOKIE, COOKIE_MAX_AGE, isHttps, sameOrigin, UPSTREAM } from "../../../../lib/server";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function POST(req: NextRequest): Promise<Response> {
  if (!sameOrigin(req)) return NextResponse.json({ detail: "cross-site request refused" }, { status: 403 });
  const body = await req.json().catch(() => ({}));
  const token = typeof body?.token === "string" ? body.token.trim() : "";
  if (!token) return NextResponse.json({ detail: "token required" }, { status: 400 });

  // Only accept a token the API itself accepts.
  let ok = false;
  try {
    const check = await fetch(`${UPSTREAM}/v1/info`, { headers: { authorization: `Bearer ${token}` }, cache: "no-store" });
    ok = check.ok;
  } catch {
    return NextResponse.json({ detail: "platform API unreachable" }, { status: 502 });
  }
  if (!ok) {
    await new Promise((r) => setTimeout(r, 600)); // blunt brute-force brake
    return NextResponse.json({ detail: "invalid token" }, { status: 401 });
  }

  const res = NextResponse.json({ ok: true });
  res.cookies.set(COOKIE, token, {
    httpOnly: true, // not readable from JavaScript, so a script injected into the page cannot steal it
    sameSite: "strict",
    secure: isHttps(req),
    path: "/",
    maxAge: COOKIE_MAX_AGE,
  });
  return res;
}
