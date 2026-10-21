"use client";

import { FormEvent, useEffect, useState } from "react";
import { api, Me } from "../../../lib/api";

export default function AccountPage() {
  const [me, setMe] = useState<Me | null>(null);
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    api<Me>("/v1/auth/me").then(setMe).catch(() => setMe(null));
  }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    if (next !== again) return setError("两次输入的新密码不一致");
    setBusy(true);
    try {
      const resp = await fetch("/api/v1/auth/password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ current_password: current, new_password: next }),
      });
      if (!resp.ok) {
        const detail = (await resp.json().catch(() => ({})))?.detail;
        throw new Error(resp.status === 403 ? "当前密码不对" : typeof detail === "string" ? detail : `修改失败（${resp.status}）`);
      }
      // Changing the password ended every session, this one too: sign in again with the new one.
      await fetch("/api/auth/logout", { method: "POST" });
      window.location.href = "/login";
    } catch (err: any) {
      setError(String(err?.message || err));
      setBusy(false);
    }
  }

  return (
    <main className="page page-narrow">
      <div className="page-head">
        <h1>账号设置</h1>
        <p>修改密码后，所有设备上的登录都会失效，需要用新密码重新登录。</p>
      </div>

      <section className="card card-pad">
        <div className="card-title">账号</div>
        <dl className="kv-list">
          <dt>用户名</dt>
          <dd>{me?.username || "-"}</dd>
          <dt>邮箱</dt>
          <dd>{me?.email || "-"}</dd>
        </dl>
      </section>

      {me?.username ? (
        <form className="card card-pad" style={{ marginTop: 16 }} onSubmit={submit}>
          <div className="card-title">修改密码</div>
          {error ? <div className="alert error" role="alert" style={{ marginBottom: 14 }}>{error}</div> : null}
          <label className="field">
            <span>当前密码</span>
            <input type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} />
          </label>
          <label className="field">
            <span>新密码</span>
            <input type="password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} />
            <span className="hint" style={{ marginTop: 4 }}>至少 8 个字符。</span>
          </label>
          <label className="field">
            <span>再输入一次新密码</span>
            <input type="password" autoComplete="new-password" value={again} onChange={(e) => setAgain(e.target.value)} />
          </label>
          <button type="submit" disabled={busy || !current || !next || !again}>
            {busy ? "提交中…" : "修改密码"}
          </button>
        </form>
      ) : me ? (
        <p className="hint" style={{ marginTop: 16 }}>这个账号没有设置密码登录（使用 API 令牌），无法在这里修改密码。</p>
      ) : null}
    </main>
  );
}
