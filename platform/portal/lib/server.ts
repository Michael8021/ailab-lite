// Server-side only (route handlers, proxy.ts). Nothing here may be imported by client components:
// the access token lives in an httpOnly cookie and must never reach the browser's JavaScript.
import { NextRequest, NextResponse } from "next/server";

export const COOKIE = "ailab_token";
export const COOKIE_MAX_AGE = 8 * 60 * 60;

// Where the Platform API is, from the Portal server's point of view. Not NEXT_PUBLIC_*: not shipped to browsers.
export const UPSTREAM = (process.env.AILAB_API_BASE || "http://127.0.0.1:8080").replace(/\/$/, "");

export function isHttps(req: NextRequest): boolean {
  return (req.headers.get("x-forwarded-proto") || req.nextUrl.protocol.replace(":", "")) === "https";
}

/** CSRF guard for state-changing requests: only our own pages may call the proxy. */
export function sameOrigin(req: NextRequest): boolean {
  const site = req.headers.get("sec-fetch-site");
  if (site) return site === "same-origin" || site === "none";
  const origin = req.headers.get("origin");
  if (!origin) return true; // non-browser client (curl): no ambient cookie to abuse
  try {
    return new URL(origin).host === req.headers.get("host");
  } catch {
    return false;
  }
}

/** Turn the API's {token, expires_in} into the httpOnly session cookie (login and sign-up). */
export function sessionResponse(req: NextRequest, token: string, expiresIn: number) {
  const res = NextResponse.json({ ok: true });
  res.cookies.set(COOKIE, token, {
    httpOnly: true, // not readable from JavaScript, so a script injected into the page cannot steal it
    sameSite: "strict",
    secure: isHttps(req),
    path: "/",
    maxAge: Math.min(Number(expiresIn) || 0, 24 * 60 * 60) || 8 * 60 * 60,
  });
  return res;
}

/** The visitor's address for the API's throttles, when a reverse proxy in front of the Portal provides it. */
export function forwardedFor(req: NextRequest): Record<string, string> {
  const client = req.headers.get("x-forwarded-for") || req.headers.get("x-real-ip");
  return client ? { "x-forwarded-for": client } : {};
}
