"use client";

import { FormEvent, useState } from "react";

export default function LoginPage() {
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const resp = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token }),
      });
      if (!resp.ok) throw new Error(resp.status === 401 ? "令牌无效" : `登录失败（${resp.status}）`);
      // The proxy (proxy.ts) serves this page at the URL the visitor asked for; go back there.
      const asked = window.location.pathname === "/login" ? new URLSearchParams(window.location.search).get("next") || "/" : window.location.pathname;
      window.location.href = asked.startsWith("/") && !asked.startsWith("//") ? asked : "/";
    } catch (err: any) {
      setError(String(err?.message || err));
      setBusy(false);
    }
  }

  return (
    <main className="container" style={{ maxWidth: 480 }}>
      <h1>登录</h1>
      <p className="sub">粘贴访问令牌。令牌只保存在本浏览器的 httpOnly Cookie 里，页面脚本读不到，也不会出现在网址中。</p>
      <section className="panel">
        <form onSubmit={submit}>
          <input
            type="password"
            autoComplete="off"
            autoFocus
            placeholder="访问令牌"
            value={token}
            onChange={(event) => setToken(event.target.value)}
            style={{ width: "100%", marginBottom: 12 }}
          />
          <button type="submit" disabled={busy || !token.trim()}>
            {busy ? "验证中…" : "登录"}
          </button>
          {error ? <p className="error">{error}</p> : null}
        </form>
        <p className="meta" style={{ marginTop: 12 }}>
          取令牌：<code>eval &quot;$(bash scripts/k8s_secrets.sh env)&quot; &amp;&amp; echo $AILAB_DEV_TOKEN</code>
        </p>
      </section>
    </main>
  );
}
