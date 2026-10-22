"use client";

import { FormEvent, useEffect, useState } from "react";
import AuthBrand from "../../components/auth-brand";

export default function RegisterPage() {
  const [mode, setMode] = useState<string | null>(null);
  const [form, setForm] = useState({ username: "", display_name: "", password: "", again: "", invite_code: "" });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    fetch("/api/auth/options")
      .then((r) => r.json())
      .then((o) => setMode(o.registration || "closed"))
      .catch(() => setMode("closed"));
  }, []);

  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) => setForm({ ...form, [k]: e.target.value });

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    if (form.password !== form.again) return setError("两次输入的密码不一致");
    setBusy(true);
    try {
      const resp = await fetch("/api/auth/register", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(form),
      });
      if (!resp.ok) throw new Error((await resp.json().catch(() => ({})))?.detail || `注册失败（${resp.status}）`);
      window.location.href = "/";
    } catch (err: any) {
      setError(String(err?.message || err));
      setBusy(false);
    }
  }

  return (
    <div className="auth">
      <AuthBrand />
      <main className="auth-form-side">
        {mode === "closed" ? (
          <div className="auth-form">
            <h1>注册</h1>
            <p>本平台未开放注册，请联系管理员为你开通账号。</p>
            <a href="/login">返回登录</a>
          </div>
        ) : (
          <form className="auth-form" onSubmit={submit}>
            <h1>注册账号</h1>
            <p>注册后直接登录。已有账号？<a href="/login">去登录</a></p>
            {error ? <div className="alert error" role="alert">{error}</div> : null}
            <label className="field">
              <span>用户名</span>
              <input type="text" name="username" autoComplete="username" autoFocus value={form.username} onChange={set("username")} />
              <span className="hint">3～64 位小写字母、数字或 . _ -，登录时使用</span>
            </label>
            <label className="field">
              <span>显示名（可选）</span>
              <input type="text" name="display_name" autoComplete="nickname" value={form.display_name} onChange={set("display_name")} />
            </label>
            <label className="field">
              <span>密码</span>
              <input type="password" name="password" autoComplete="new-password" value={form.password} onChange={set("password")} />
              <span className="hint">至少 8 个字符</span>
            </label>
            <label className="field">
              <span>再输入一次密码</span>
              <input type="password" name="again" autoComplete="new-password" value={form.again} onChange={set("again")} />
            </label>
            {mode === "invite" ? (
              <label className="field">
                <span>邀请码</span>
                <input type="text" name="invite_code" autoComplete="off" value={form.invite_code} onChange={set("invite_code")} />
              </label>
            ) : null}
            <button type="submit" className="lg" disabled={busy || !mode || !form.username.trim() || !form.password || !form.again}>
              {busy ? "注册中…" : "注册并登录"}
            </button>
          </form>
        )}
      </main>
    </div>
  );
}
