"use client";

import { useEffect, useState } from "react";
import { usePathname } from "next/navigation";
import { api, Me } from "../lib/api";

const LINKS = [
  { href: "/", label: "对话" },
  { href: "/files", label: "资料库" },
];

function active(pathname: string, href: string): boolean {
  return href === "/" ? pathname === "/" || pathname.startsWith("/c/") || pathname.startsWith("/runs") : pathname.startsWith(href);
}

export default function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname() || "/";
  const [me, setMe] = useState<Me | null>(null);

  useEffect(() => {
    api<Me>("/v1/auth/me").then(setMe).catch(() => setMe(null));
  }, []);

  async function logout() {
    await fetch("/api/auth/logout", { method: "POST" });
    window.location.href = "/login";
  }

  const name = me?.display_name || me?.username || "";

  return (
    <>
      <header className="topbar">
        <div className="topbar-inner">
          <a className="brand" href="/">
            <span className="brand-mark">A</span>
            AILab <small>Lite</small>
          </a>
          <nav className="nav" aria-label="主导航">
            {LINKS.map((l) => (
              <a key={l.href} href={l.href} aria-current={active(pathname, l.href) ? "page" : undefined}>
                {l.label}
              </a>
            ))}
          </nav>
          <div className="topbar-end">
            {me ? (
              <a className="user-chip" href="/account" title="账号设置">
                <span className="avatar">{(name[0] || "?").toUpperCase()}</span>
                <span className="user-name">{name}</span>
              </a>
            ) : null}
            <button className="link" onClick={logout}>
              退出
            </button>
          </div>
        </div>
      </header>
      {children}
    </>
  );
}
