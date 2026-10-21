"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { IconSend } from "../../components/icons";
import { api, Info, Limit, ModelInfo, Run, Usage } from "../../lib/api";
import { formatBytes, formatTokens, statusLabel, timeAgo } from "../../lib/format";

const SUGGESTIONS = [
  { kind: "演示文稿", text: "帮我做一个 PPT，介绍 AILab 平台的分层架构" },
  { kind: "演示文稿", text: "帮我做一个 PPT，主题是「沙箱隔离与最小权限」" },
  { kind: "资料处理", text: "总结这份资料，生成一个 markdown 文件" },
];

function Meter({ label, limit, format }: { label: string; limit: Limit; format: (n: number) => string }) {
  const pct = limit.limit > 0 ? Math.min(100, (limit.used / limit.limit) * 100) : 0;
  const tone = pct >= 100 ? "full" : pct >= 80 ? "warn" : "";
  return (
    <div className="meter">
      <div className="meter-head">
        <span>{label}</span>
        <span>
          {format(limit.used)} / {format(limit.limit)}
        </span>
      </div>
      <div className="meter-bar" role="progressbar" aria-label={label} aria-valuenow={Math.round(pct)} aria-valuemin={0} aria-valuemax={100}>
        <div className={`meter-fill ${tone}`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
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

  useEffect(() => {
    load();
    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
  }, []);

  const modelName = (id: string) => models.find((m) => m.id === id)?.name || id;
  const selected = models.find((m) => m.id === model);

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
    <main className="page">
      <div className="hero">
        <h1>今天想让 AILab 做点什么？</h1>
        <p>描述任务，Agent 会在隔离沙箱里执行，产出自动进入资料库。</p>
      </div>

      <div className="grid-main">
        <div>
          <section className="card composer">
            <textarea
              rows={4}
              aria-label="任务描述"
              placeholder="例如：帮我做一个 PPT，介绍 AILab 平台的分层架构"
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
              onKeyDown={(event) => {
                if ((event.metaKey || event.ctrlKey) && event.key === "Enter") submit();
              }}
            />
            <div className="composer-bar">
              <select
                value={model}
                aria-label="模型"
                title={selected?.description || ""}
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
              <span className="hint">Ctrl / ⌘ + Enter 提交</span>
              <span className="spacer" />
              <button onClick={() => submit()} disabled={busy || !prompt.trim() || !model}>
                <IconSend size={16} />
                {busy ? "创建中…" : "提交任务"}
              </button>
            </div>
          </section>
          {error ? <div className="alert error" style={{ marginTop: 12 }}>{error}</div> : null}

          <div className="suggestions">
            {SUGGESTIONS.map((s) => (
              <button key={s.text} className="suggestion secondary" disabled={busy || !model} onClick={() => submit(s.text)}>
                <small>{s.kind}</small>
                {s.text}
              </button>
            ))}
          </div>

          <section style={{ marginTop: 32 }}>
            <div className="section-title">
              最近任务
              {runs.length ? <span className="count">{runs.length}</span> : null}
              <span className="spacer" />
              <button className="link" onClick={load}>
                刷新
              </button>
            </div>
            <div className="card run-list">
              {loading ? (
                <div className="empty">加载中…</div>
              ) : runs.length === 0 ? (
                <div className="empty">
                  <strong>还没有任务</strong>在上面描述一个，或点一个示例开始。
                </div>
              ) : (
                runs.map((run) => (
                  <a key={run.id} className="run-row" href={`/runs/${run.id}`}>
                    <div className="grow">
                      <div className="title">{run.title || run.prompt.slice(0, 60)}</div>
                      <div className="meta">
                        {timeAgo(run.created_at)} · {modelName(run.model)}
                      </div>
                    </div>
                    <span className={`status ${run.status}`}>{statusLabel(run.status)}</span>
                  </a>
                ))
              )}
            </div>
          </section>
        </div>

        <aside className="stack">
          <section className="card card-pad">
            <div className="card-title">用量额度</div>
            {usage ? (
              <>
                <Meter label="今日" limit={usage.daily_tokens} format={formatTokens} />
                <Meter label="本月" limit={usage.monthly_tokens} format={formatTokens} />
                <Meter label="资料库空间" limit={usage.library_bytes} format={formatBytes} />
                <p className="hint" style={{ margin: "14px 0 0" }}>
                  今日额度已用 {Math.round((usage.daily_tokens.used / Math.max(1, usage.daily_tokens.limit)) * 100)}%，Token 按模型倍率折算。
                </p>
              </>
            ) : (
              <div className="hint">加载中…</div>
            )}
          </section>
          {info ? (
            <section className="card card-pad">
              <div className="card-title">运行环境</div>
              <dl className="kv-list">
                <dt>可选模型</dt>
                <dd>{models.length} 个</dd>
                <dt>沙箱</dt>
                <dd>{info.sandbox_provider}（默认无网络）</dd>
                <dt>存储</dt>
                <dd>{info.storage_backend}</dd>
                <dt>环境</dt>
                <dd>{info.env}</dd>
              </dl>
            </section>
          ) : null}
        </aside>
      </div>
    </main>
  );
}
