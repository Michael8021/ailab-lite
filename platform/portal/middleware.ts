import { NextRequest, NextResponse } from "next/server";

// Pages need a session cookie; without one the visitor gets the login page *at the same URL*
// (a rewrite, not a redirect: Next rewrites the host inside redirect URLs, which sends visitors
// arriving via 127.0.0.1, a LAN name or a proxy to another origin where this cookie does not
// exist). The login page sends them back here once they are in. Whether the token is still valid
// is the API's call: the client returns here on a 401.
export function middleware(req: NextRequest) {
  if (req.cookies.get("ailab_token")?.value) return NextResponse.next();
  return NextResponse.rewrite(new URL("/login", req.url));
}

export const config = { matcher: ["/((?!login|api|_next|favicon.ico).*)"] };
