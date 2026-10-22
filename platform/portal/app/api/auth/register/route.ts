import { NextRequest, NextResponse } from "next/server";
import { forwardedFor, sameOrigin, sessionResponse, UPSTREAM } from "../../../../lib/server";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

// Sign-up: the API creates the account (or says why not, in Chinese) and answers like a login,
// so the new user lands in the Portal already logged in.
export async function POST(req: NextRequest): Promise<Response> {
  if (!sameOrigin(req)) return NextResponse.json({ detail: "cross-site request refused" }, { status: 403 });
  const body = await req.json().catch(() => ({}));
  const pick = (k: string) => (typeof body?.[k] === "string" ? body[k] : "");
  const payload = { username: pick("username").trim(), password: pick("password"), display_name: pick("display_name").trim(), invite_code: pick("invite_code") };
  if (!payload.username || !payload.password) return NextResponse.json({ detail: "请填写用户名和密码" }, { status: 400 });

  let upstream: Response;
  try {
    upstream = await fetch(`${UPSTREAM}/v1/auth/register`, {
      method: "POST",
      headers: { "content-type": "application/json", ...forwardedFor(req) },
      body: JSON.stringify(payload),
      cache: "no-store",
    });
  } catch {
    return NextResponse.json({ detail: "平台 API 无法访问" }, { status: 502 });
  }
  if (!upstream.ok) {
    const detail = (await upstream.json().catch(() => ({})))?.detail;
    return NextResponse.json({ detail: typeof detail === "string" ? detail : `注册失败（${upstream.status}）` }, { status: upstream.status });
  }
  const { token, expires_in } = await upstream.json();
  return sessionResponse(req, token, expires_in);
}
