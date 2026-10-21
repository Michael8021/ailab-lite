"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api, Info, ModelInfo, Run, Usage } from "../lib/api";

const QUICK = [
  "帮我做一个 PPT，介绍 AILab 平台的分层架构",
  "帮我做一个 PPT，主题是「沙箱隔离与最小权限」",
  "总结这份资料，生成一个 markdown 文件",
];

function statusClass(status: string) {
  return `status ${status}`;
}

function shortDate(value: string) {
  try {
    return new Date(value).toLocaleString("zh-CN", { hour12: false });
  } catch {
    return value;
  }
}

function formatTokens(n: number): string {
  return n >= 1_000_000 ? `${(n / 1_000_000).toFixed(1)}M` : n >= 1000 ? `${Math.round(n / 1000)}k` : String(n);
}

export default function HomePage() {
  const router = useRouter();
  const [runs, setRuns] = useState<Run[]>([]);
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [info, setInfo] = useState<Info | null>(null);
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [model, setModel] = useState("");
  const [usage, setUsage] = useState<Usage | null>(null);

  async function load() {
    try {
      setRuns(await api<Run[]>("/v1/runs?limit=50"));
      setError("");
    } catch (err: any) {
      setError(String(err?.message || err));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    api<Info>("/v1/info").then(setInfo).catch(() => setInfo(null));
    // The platform decides which models exist; the user only picks one (their last choice is remembered).
    api<{ default: string | null; models: ModelInfo[] }>("/v1/models")
      .then((res) => {
        setModels(res.models);
        const remembered = localStorage.getItem("ailab.model");
        setModel(res.models.some((m) => m.id === remembered) ? remembered! : res.default || res.models[0]?.id || "");
      })
      .catch(() => setModels([]));
  }, []);

  useEffect(() => {
    const loadUsage = () => api<Usage>("/v1/usage").then(setUsage).catch(() => setUsage(null));
    loadUsage();
    const timer = setInterval(loadUsage, 15000);
    return () => clearInterval(timer);
  }, []);

  const modelName = (id: string) => models.find((m) => m.id === id)?.name || id;

  useEffect(() => {
    load();
    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
  }, []);

  async function submit(text?: string) {
    const value = (text ?? prompt).trim();
    if (!value || busy) return;
    setBusy(true);
    setError("");
    try {
      const run = await api<Run>("/v1/runs", {
        method: "POST",
        body: JSON.stringify({ prompt: value, model: model || undefined }),
      });
      setPrompt("");
      router.push(`/runs/${run.id}`);
    } catch (err: any) {
      setError(String(err?.message || err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="container">
      <h1>工作台</h1>
      <p className="sub">
        描述任务，Agent Service 会在隔离沙箱里执行，产出自动进入资料库。
        {info
          ? ` 当前：${models.length} 个可选模型，沙箱 ${info.sandbox_provider}（默认无网络）。`
          : ""}
      </p>

      <section className="panel">
        <h2>新建任务</h2>
        <textarea
          rows={4}
          placeholder="例如：帮我做一个 PPT，介绍 AILab 平台的分层架构"
          value={prompt}
          onChange={(event) => setPrompt(event.target.value)}
          onKeyDown={(event) => {
            if ((event.metaKey || event.ctrlKey) && event.key === "Enter") submit();
          }}
        />
        <div className="row" style={{ marginTop: 12 }}>
          <button onClick={() => submit()} disabled={busy || !prompt.trim() || !model}>
            {busy ? "创建中…" : "提交任务"}
          </button>
          <select
            value={model}
            aria-label="模型"
            style={{ width: "auto", minWidth: 220 }}
            title={models.find((m) => m.id === model)?.description || ""}
            onChange={(event) => {
              setModel(event.target.value);
              localStorage.setItem("ailab.model", event.target.value);
            }}
          >
            {models.map((m) => (
              <option key={m.id} value={m.id}>
                {m.name}
                {m.weight !== 1 ? `（消耗 ×${m.weight}）` : ""}
              </option>
            ))}
          </select>
          <span className="sub" style={{ margin: 0, fontSize: 12 }}>
            Ctrl / ⌘ + Enter 提交
            {usage
              ? ` · 今日额度已用 ${Math.round((usage.daily_tokens.used / usage.daily_tokens.limit) * 100)}%（${formatTokens(usage.daily_tokens.used)} / ${formatTokens(usage.daily_tokens.limit)}，按模型倍率折算）`
              : ""}
          </span>
        </div>
        <div className="chips">
          {QUICK.map((text) => (
            <span key={text} className="chip" onClick={() => submit(text)}>
              {text}
            </span>
          ))}
        </div>
        {error ? <div className="error">{error}</div> : null}
      </section>

      <section>
        <div className="row" style={{ marginBottom: 12 }}>
          <h2 style={{ margin: 0 }}>最近任务</h2>
          <div className="spacer" style={{ flex: 1 }} />
          <button className="ghost" onClick={load}>
            刷新
          </button>
        </div>

        {loading ? (
          <div className="empty">加载中…</div>
        ) : runs.length === 0 ? (
          <div className="empty">还没有任务，先在上面提交一个。</div>
        ) : (
          <div className="run-list">
            {runs.map((run) => (
              <a key={run.id} className="run-card" href={`/runs/${run.id}`}>
                <div className="grow">
                  <div className="title">{run.title || run.prompt.slice(0, 60)}</div>
                  <div className="meta">
                    {shortDate(run.created_at)} · {modelName(run.model)} · {run.id}
                  </div>
                </div>
                <span className={statusClass(run.status)}>{run.status}</span>
              </a>
            ))}
          </div>
        )}
      </section>
    </main>
  );
}
