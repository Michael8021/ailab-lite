import { NextRequest, NextResponse } from "next/server";
import { COOKIE, isHttps, sameOrigin } from "../../../../lib/server";

export const dynamic = "force-dynamic";

export async function POST(req: NextRequest): Promise<Response> {
  if (!sameOrigin(req)) return NextResponse.json({ detail: "cross-site request refused" }, { status: 403 });
  const res = NextResponse.json({ ok: true });
  res.cookies.set(COOKIE, "", { httpOnly: true, sameSite: "strict", secure: isHttps(req), path: "/", maxAge: 0 });
  return res;
}
