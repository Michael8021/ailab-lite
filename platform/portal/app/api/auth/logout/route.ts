import { NextRequest, NextResponse } from "next/server";
import { COOKIE, isHttps, sameOrigin, UPSTREAM } from "../../../../lib/server";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function POST(req: NextRequest): Promise<Response> {
  if (!sameOrigin(req)) return NextResponse.json({ detail: "cross-site request refused" }, { status: 403 });
  // End the session on the API too, so a copied cookie stops working. Best effort: the cookie is cleared regardless.
  const token = req.cookies.get(COOKIE)?.value;
  if (token) {
    await fetch(`${UPSTREAM}/v1/auth/logout`, { method: "POST", headers: { authorization: `Bearer ${token}` }, cache: "no-store" }).catch(() => undefined);
  }
  const res = NextResponse.json({ ok: true });
  res.cookies.set(COOKIE, "", { httpOnly: true, sameSite: "strict", secure: isHttps(req), path: "/", maxAge: 0 });
  return res;
}
