import { NextResponse } from "next/server";
import { UPSTREAM } from "../../../../lib/server";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

// What the login page offers (sign-up or not). Public: it is needed before anyone is logged in.
export async function GET(): Promise<Response> {
  try {
    const upstream = await fetch(`${UPSTREAM}/v1/auth/options`, { cache: "no-store" });
    if (upstream.ok) return NextResponse.json(await upstream.json());
  } catch {
    // fall through
  }
  return NextResponse.json({ registration: "closed" });
}
