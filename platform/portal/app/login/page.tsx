"use client";

import { FormEvent, useState } from "react";

export default function LoginPage() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
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
        body: JSON.stringify({ username, password }),
      });
      if (!resp.ok) {
        const detail = (await resp.json().catch(() => ({})))?.detail;
        throw new Error(resp.status === 401 ? "用户名或密码错误" : typeof detail === "string" ? detail : `登录失败（${resp.status}）`);
      }
      // The proxy (proxy.ts) serves this page at the URL the visitor asked for; go back there.
      const asked = window.location.pathname === "/login" ? new URLSearchParams(window.location.search).get("next") || "/" : window.location.pathname;
      window.location.href = asked.startsWith("/") && !asked.startsWith("//") ? asked : "/";
    } catch (err: any) {
      setError(String(err?.message || err));
      setBusy(false);
    }
  }

  return (
    <div className="auth">
      <aside className="auth-brand">
        <div className="brand">
          <span className="brand-mark">A</span>
          AILab
        </div>
        <div>
          <h2>在隔离沙箱里，把任务交给 Agent</h2>
          <p>描述你要做的事，Agent 会在受限的沙箱里写代码、跑命令、生成文件，产出自动归档到你的资料库。</p>
        </div>
        <ul className="auth-points">
          <li>沙箱默认无网络，代码与数据互相隔离</li>
          <li>模型由平台统一接入，无需自带 API Key</li>
          <li>用量按模型倍率折算，每日 / 每月额度一目了然</li>
        </ul>
      </aside>

      <main className="auth-form-side">
        <form className="auth-form" onSubmit={submit}>
          <h1>登录</h1>
          <p>使用平台为你创建的账号。</p>
          {error ? <div className="alert error" role="alert">{error}</div> : null}
          <label className="field">
            <span>用户名</span>
            <input type="text" name="username" autoComplete="username" autoFocus value={username} onChange={(e) => setUsername(e.target.value)} />
          </label>
          <label className="field">
            <span>密码</span>
            <input type="password" name="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} />
          </label>
          <button type="submit" className="lg" disabled={busy || !username.trim() || !password}>
            {busy ? "登录中…" : "登录"}
          </button>
          <p className="hint" style={{ marginTop: 16 }}>
            没有账号或忘记密码，请联系管理员。
          </p>
        </form>
      </main>
    </div>
  );
}
